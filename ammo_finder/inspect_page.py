"""`python -m ammo_finder inspect URL`: a short, pasteable summary of how a
product page or listing page is built, used to tune the readers for a store.
It loads the page once (same polite fetcher: robots.txt, cache, delays)."""
from __future__ import annotations

import json
import re
from collections import Counter

from bs4 import BeautifulSoup

from .parse import detect_caliber, normalize
from .sources.extract import _walk, extract


def run_inspect(cfg: dict, fetcher, url: str) -> None:
    html = fetcher.get(url, "html")
    soup = BeautifulSoup(html, "lxml")
    print(f"URL: {url}\nTitle: {normalize(soup.title.get_text()) if soup.title else '-'}\nHTML size: {len(html):,} chars")

    # JSON-LD
    prods = []
    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except (ValueError, TypeError):
            continue
        for n in _walk(data):
            t = n.get("@type")
            if t == "Product" or (isinstance(t, list) and "Product" in t):
                prods.append(n)
    print(f"\nJSON-LD products: {len(prods)}")
    for p in prods[:3]:
        offers = p.get("offers")
        o = offers[0] if isinstance(offers, list) and offers else offers or {}
        print(f"  - name: {str(p.get('name'))[:90]}")
        print(f"    keys: {sorted(p.keys())}")
        print(f"    offer: price={o.get('price') or o.get('lowPrice')} availability={str(o.get('availability'))[-20:]} url={str(o.get('url') or p.get('url'))[:90]}")
        for k in ("description", "additionalProperty", "size", "weight", "sku"):
            if p.get(k):
                print(f"    {k}: {normalize(str(p[k]))[:160]}")

    # Which element classes hold product cards? Count classes on elements that contain a $ and a caliber.
    counter: Counter = Counter()
    for el in soup.find_all(True):
        cls = " ".join(el.get("class", []))
        if not cls:
            continue
        txt = el.get_text(" ", strip=True)
        if 30 < len(txt) < 1500 and "$" in txt and detect_caliber(txt) and el.find("a", href=True):
            counter[(el.name, cls[:80])] += 1
    print("\nLikely product-card elements (tag.class: count):")
    for (tag, cls), n in counter.most_common(8):
        print(f"  {tag}.{cls}: {n}")
    if counter:
        (tag, cls), _ = counter.most_common(1)[0]
        el = soup.find(tag, class_=cls.split()[0])
        if el is not None:
            print(f"\nSample card text:\n  {normalize(el.get_text(' ', strip=True))[:600]}")

    # Round counts: where on the page do numbers like "50 Rounds" / "Box of 20" appear?
    hits = re.findall(r"[^.>]{0,40}\b\d{1,4}\s*(?:rounds?|rds?|ct|count|shells|per box|/box)\b[^<]{0,20}", soup.get_text(" "), re.I)
    print(f"\nRound-count phrases on the page: {len(hits)}")
    for h in hits[:6]:
        print(f"  ...{normalize(h)}...")

    # Pagination links
    pages = sorted({a['href'] for a in soup.find_all('a', href=True) if re.search(r'[?&](p|page|pagenum|pg|start)=\d', a['href'])})[:6]
    print(f"\nPagination-looking links: {pages or 'none found'}")

    ls = extract(html, url, "inspect", detect_caliber(url) or None, cfg["retailers"], cfg.get("marketplace_sellers", []))
    print(f"\nWhat the reader currently gets: {len(ls)} listings, {sum(1 for l in ls if l.rounds)} with a round count")
    for l in ls[:5]:
        print(f"  - ${l.price:.2f} | {l.rounds or '?'} rds | {'in stock' if l.in_stock else 'OOS'} | {l.title[:80]}")
