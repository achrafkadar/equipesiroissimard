#!/usr/bin/env python3
"""Refresh the 3 featured property cards on the buyer landing page.

Pulls the newest listings from equipesiroissimard.com/proprietes/,
keeps only those whose Centris photo returns HTTP 200, then rewrites
the cards + FR/EN i18n meta strings in index.html.
"""

from __future__ import annotations

import argparse
import re
import ssl
import subprocess
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from html import unescape
from pathlib import Path

LISTINGS_URL = "https://www.equipesiroissimard.com/proprietes/"
USER_AGENT = "EquipeSiroisSimard-LandingBot/1.0 (+https://equipesiroissimard.ca)"
ROOT = Path(__file__).resolve().parents[1]
INDEX_PATH = ROOT / "index.html"


@dataclass(frozen=True)
class Listing:
    """One Centris listing card scraped from the public properties page."""

    centris_id: str
    image_url: str
    street: str
    city: str
    price: str
    beds: int | None
    baths: int | None

    @property
    def address(self) -> str:
        city_short = re.sub(r"\s*\([^)]*\)\s*", "", self.city).strip()
        return f"{self.street}, {city_short}"

    @property
    def alt_city(self) -> str:
        city_short = re.sub(r"\s*\([^)]*\)\s*", "", self.city).strip()
        return city_short or "Outaouais"

    @property
    def meta_fr(self) -> str:
        parts: list[str] = []
        if self.beds is not None:
            label = "chambre" if self.beds == 1 else "chambres"
            parts.append(f"{self.beds} {label}")
        if self.baths is not None:
            label = "salle de bain" if self.baths == 1 else "salles de bain"
            parts.append(f"{self.baths} {label}")
        if not parts:
            parts.append(self.alt_city)
        elif self.alt_city and self.alt_city not in " · ".join(parts):
            # Prefer city as second beat when baths missing.
            if self.baths is None:
                parts.append(self.alt_city)
        return " · ".join(parts[:2])

    @property
    def meta_en(self) -> str:
        parts: list[str] = []
        if self.beds is not None:
            label = "bedroom" if self.beds == 1 else "bedrooms"
            parts.append(f"{self.beds} {label}")
        if self.baths is not None:
            label = "bathroom" if self.baths == 1 else "bathrooms"
            parts.append(f"{self.baths} {label}")
        if not parts:
            parts.append(self.alt_city)
        elif self.baths is None and self.alt_city:
            parts.append(self.alt_city)
        return " · ".join(parts[:2])


def fetch(url: str, *, timeout: int = 30) -> bytes:
    """GET a URL and return response bytes."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    contexts = (ssl.create_default_context(), ssl._create_unverified_context())
    last_error: Exception | None = None
    for ctx in contexts:
        try:
            with urllib.request.urlopen(req, timeout=timeout, context=ctx) as resp:
                return resp.read()
        except Exception as exc:  # noqa: BLE001 — try next strategy
            last_error = exc
    result = subprocess.run(
        [
            "curl",
            "-fsSL",
            "-A",
            USER_AGENT,
            "--max-time",
            str(timeout),
            url,
        ],
        capture_output=True,
    )
    if result.returncode == 0:
        return result.stdout
    raise RuntimeError(f"Failed to fetch {url}: {last_error or result.stderr!r}")


def image_ok(url: str) -> bool:
    """Return True when the listing photo is reachable."""
    probe = subprocess.run(
        [
            "curl",
            "-sI",
            "-o",
            "/dev/null",
            "-w",
            "%{http_code}",
            "-A",
            USER_AGENT,
            "--max-time",
            "20",
            url,
        ],
        capture_output=True,
        text=True,
    )
    code = (probe.stdout or "").strip()
    if code.isdigit() and code.startswith("2"):
        return True
    if code in {"403", "405", "000"}:
        try:
            return len(fetch(url, timeout=20)) > 500
        except Exception:
            return False
    return False


def _clean(text: str) -> str:
    text = unescape(text)
    text = text.replace("\xa0", " ").replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def parse_listings(html: str) -> list[Listing]:
    """Parse Centris listing cards in page order (newest first on site)."""
    listings: list[Listing] = []
    for part in html.split('<div class="centris_listing4_box')[1:]:
        chunk = part.split("</a>", 1)[0]
        img_m = re.search(
            r'src="(https://[^"]+/photos/(\d+)/1_t\.jpg)"',
            chunk,
        )
        street_m = re.search(r'<span class="card_title">\s*([^<]+?)\s*</span>', chunk)
        city_m = re.search(r'<span class="card_address[^"]*">\s*([^<]+?)\s*</span>', chunk)
        price_m = re.search(r'<span class="price">\s*([^<]+?)\s*</span>', chunk)
        beds_m = re.search(
            r'title="Chambre\(s\)"[^>]*>.*?<span>(\d+)</span>',
            chunk,
            flags=re.S,
        )
        baths_m = re.search(
            r'title="Salle\(s\) de bains?"[^>]*>.*?<span>(\d+)</span>',
            chunk,
            flags=re.S,
        )
        if not (img_m and street_m and city_m and price_m):
            continue
        price = _clean(price_m.group(1))
        price = price.replace("$", "").strip() + " $"
        listings.append(
            Listing(
                centris_id=img_m.group(2),
                image_url=img_m.group(1),
                street=_clean(street_m.group(1)),
                city=_clean(city_m.group(1)),
                price=price,
                beds=int(beds_m.group(1)) if beds_m else None,
                baths=int(baths_m.group(1)) if baths_m else None,
            )
        )
    return listings


def pick_featured(listings: list[Listing], count: int = 3) -> list[Listing]:
    """Keep newest listings with a working photo; prefer those with bedrooms."""
    selected: list[Listing] = []
    seen: set[str] = set()

    def try_add(items: list[Listing]) -> None:
        for item in items:
            if len(selected) >= count:
                return
            if item.centris_id in seen:
                continue
            if not image_ok(item.image_url):
                print(f"skip broken photo {item.centris_id}: {item.address}", file=sys.stderr)
                continue
            selected.append(item)
            seen.add(item.centris_id)

    with_beds = [x for x in listings if x.beds is not None]
    try_add(with_beds)
    try_add(listings)
    if len(selected) < count:
        raise RuntimeError(
            f"Only found {len(selected)} listings with working photos (need {count})."
        )
    return selected[:count]


def replace_props_block(html: str, listings: list[Listing]) -> str:
    """Replace the inner `.props` cards."""
    pattern = re.compile(
        r'<div class="props">\s*.*?\s*</div>\s*(?=<div class="props-cta">)',
        flags=re.S,
    )
    block = '<div class="props">\n' + render_props_block(listings) + "\n    </div>\n    "
    updated, n = pattern.subn(block, html, count=1)
    if n != 1:
        raise RuntimeError("Could not locate .props block in index.html")
    return updated


def render_props_block(listings: list[Listing]) -> str:
    """Render the three `.prop` cards HTML."""
    cards: list[str] = []
    for idx, listing in enumerate(listings, start=1):
        lazy = ' loading="lazy"' if idx > 1 else ""
        cards.append(
            "      <div class=\"prop\">\n"
            f'        <div class="ph"><img src="{listing.image_url}" '
            f'alt="{listing.alt_city}"{lazy}></div>\n'
            f'        <div class="body"><div class="price">{listing.price}</div>'
            f'<div class="addr">{listing.address}</div>'
            f'<div class="meta" data-i18n="prop{idx}_meta">{listing.meta_fr}</div></div>\n'
            "      </div>"
        )
    return "\n".join(cards)


def replace_i18n_meta(html: str, listings: list[Listing]) -> str:
    """Update FR and EN prop*_meta dictionary entries."""
    for idx, listing in enumerate(listings, start=1):
        # French block comes first; English second — replace each occurrence in order.
        fr_pat = re.compile(rf'(prop{idx}_meta:\s*")([^"]*)(")')
        matches = list(fr_pat.finditer(html))
        if len(matches) < 2:
            raise RuntimeError(f"Expected 2 i18n entries for prop{idx}_meta, found {len(matches)}")
        # Replace from the end so offsets stay valid.
        en_m, fr_m = matches[1], matches[0]
        html = html[: en_m.start(2)] + listing.meta_en + html[en_m.end(2) :]
        html = html[: fr_m.start(2)] + listing.meta_fr + html[fr_m.end(2) :]
    return html


def current_ids(html: str) -> list[str]:
    """Centris IDs currently embedded in the featured cards."""
    block = re.search(r'<div class="props">(.*?)</div>\s*<div class="props-cta">', html, flags=re.S)
    if not block:
        return []
    return re.findall(r"/photos/(\d+)/1_t\.jpg", block.group(1))


def update_index(path: Path, listings: list[Listing], *, force: bool = False) -> bool:
    """Rewrite index.html if featured listings changed. Return True if written."""
    html = path.read_text(encoding="utf-8")
    old_ids = current_ids(html)
    new_ids = [x.centris_id for x in listings]

    if old_ids == new_ids and not force:
        print("Featured listings already up to date:", ", ".join(new_ids))
        return False

    html = replace_props_block(html, listings)
    html = replace_i18n_meta(html, listings)
    path.write_text(html, encoding="utf-8")
    print("Updated featured listings:")
    for listing in listings:
        print(f"  - {listing.centris_id}: {listing.address} — {listing.price}")
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Rewrite index.html even when Centris IDs are unchanged.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch and print selected listings without writing files.",
    )
    args = parser.parse_args()

    print(f"Fetching {LISTINGS_URL}")
    html = fetch(LISTINGS_URL).decode("utf-8", errors="replace")
    listings = parse_listings(html)
    print(f"Parsed {len(listings)} listings from page 1")
    featured = pick_featured(listings, count=3)

    if args.dry_run:
        for listing in featured:
            print(f"{listing.centris_id}\t{listing.price}\t{listing.address}\t{listing.meta_fr}")
        return 0

    changed = update_index(INDEX_PATH, featured, force=args.force)
    return 0 if changed or not args.force else 0


if __name__ == "__main__":
    raise SystemExit(main())
