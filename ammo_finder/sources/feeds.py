"""Structured sources: RSS/Atom deal feeds and XML sitemaps.

Feeds and sitemaps are published for machines to read, so they're the
preferred way in. A feed item becomes a Listing directly. A sitemap only lists
product URLs, so matching product pages are then loaded (capped per run) and
read through their schema.org product data.
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from typing import Optional

from ..models import Listing
from ..parse import detect_caliber, detect_rounds, normalize
from .extract import _OOS, _match_marketplace, _match_retailer, _prices_in

# URL-slug patterns used to pick product pages out of a sitemap
SLUG_PATTERNS = {
    "9mm": r"(?<![\dx.])9-?mm|9x19",
    "12ga": r"12-?ga|12-gauge|12gauge",
    "762x54r": r"7-?62-?x-?54|762x54",
}


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].lower()


def _text(el: Optional[ET.Element]) -> str:
    return normalize("".join(el.itertext())) if el is not None else ""


def _strip_html(s: str) -> str:
    return normalize(re.sub(r"<[^>]+>", " ", s or ""))


def parse_feed(xml: str, source_name: str, retailers: list[dict], markets: list[str]) -> list[Listing]:
    """RSS 2.0 or Atom. Each item that names one of our calibers and a $ price becomes a Listing."""
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError:
        return []
    items = [e for e in root.iter() if _local(e.tag) in ("item", "entry")]
    out = []
    for it in items:
        fields = {_local(c.tag): c for c in it}
        title = _text(fields.get("title"))
        link = ""
        if "link" in fields:
            l = fields["link"]
            link = (l.get("href") or _text(l)).strip()
        desc = _strip_html(_text(fields.get("description")) or _text(fields.get("summary")) or _text(fields.get("content")))
        text = f"{title} {desc}"
        cal = detect_caliber(title) or detect_caliber(text)
        if not cal or not link:
            continue
        prices = _prices_in(title) or _prices_in(desc)
        if not prices:
            continue
        store = _match_retailer(text, retailers) or f"Unknown store (via {source_name})"
        out.append(
            Listing(
                title=title,
                retailer=store,
                url=link,
                price=prices[0],
                rounds=detect_rounds(title) or detect_rounds(desc),
                in_stock=not _OOS.search(text),
                shipping=0.0 if re.search(r"free shipping|ships free|\bfs\b", text, re.I) else None,
                source=f"{source_name} feed",
                caliber_hint=cal,
                extra_text=desc[:400],
                marketplace=bool(_match_marketplace(text, markets)),
            )
        )
    return out


def parse_sitemap(xml: str) -> tuple[list[str], list[str]]:
    """Returns (child sitemap URLs, page URLs)."""
    try:
        root = ET.fromstring(xml.encode("utf-8") if isinstance(xml, str) else xml)
    except ET.ParseError:
        return [], []
    children, pages = [], []
    for el in root:
        tag = _local(el.tag)
        loc = next((c.text.strip() for c in el if _local(c.tag) == "loc" and c.text), None)
        if not loc:
            continue
        (children if tag == "sitemap" else pages).append(loc)
    return children, pages


def sitemap_product_urls(fetch, sitemap_urls: list[str], caliber: str, include: Optional[str] = None, max_sitemaps: int = 20) -> list[str]:
    """Walk sitemaps (following indexes) and return page URLs whose slug matches the caliber."""
    pat = re.compile(include or SLUG_PATTERNS[caliber], re.I)
    queue, seen, found = list(sitemap_urls), set(), []
    while queue and len(seen) < max_sitemaps:
        sm = queue.pop(0)
        if sm in seen:
            continue
        seen.add(sm)
        children, pages = parse_sitemap(fetch(sm, "raw", 24 * 3600))
        # only follow child sitemaps that can hold products
        queue += [c for c in children if re.search(r"product|catalog|item|sitemap\d|sitemap_index|\.xml", c, re.I)]
        found += [p for p in pages if pat.search(p.rsplit("/", 1)[-1] or p)]
    return list(dict.fromkeys(found))
