"""Turn a retailer or aggregator HTML page into Listing objects.

Store layouts change often and per-store CSS selectors break constantly, so
this uses three generic strategies and merges the results:

1. schema.org JSON-LD (Product / ItemList / Offer), which most stores embed for Google.
2. HTML tables with a header row (AmmoSeek-style: retailer | description | price | rounds | ...).
3. Product "cards": the innermost block holding a product link and a $ price.
"""
from __future__ import annotations

import json
import re
from typing import Iterable, Optional
from urllib.parse import urljoin

from bs4 import BeautifulSoup, Tag

from ..models import Listing
from ..parse import detect_caliber, detect_rounds, normalize

_PRICE = re.compile(r"\$\s?(\d{1,3}(?:,\d{3})*(?:\.\d{2})?|\d+(?:\.\d{2})?)(?!\s*(?:/|per|each|a)\s*(?:rd|round|shell|cartridge))(?!\s*¢)")
_OOS = re.compile(
    r"out of stock|sold out|backorder|back-order|back order|pre-?order|notify me|coming soon|currently unavailable|"
    r"email when available|special order",
    re.I,
)
_QTY = re.compile(r"(?:only\s+)?(\d{1,5})\s+(?:in stock|available|left|units? available|remaining|ready to ship)", re.I)
_BAD_PRICE_CLASS = re.compile(
    r"(?<![a-z])(old|was|regular|msrp|strike|compare|save|saving|cpr|unit|retail)|per-?(round|rd)|list-?price", re.I
)
_CARD_SELECTORS = [
    "li.product-item", "div.product-item", "li.item", ".products-grid > li", ".product-item-info", "li.item.product", ".product-card",
    "[class*=product-card]", "[class*=productCard]", "[class*=ProductCard]", "[class*=product-tile]",
    "[class*=productTile]", "[class*=product-grid-item]", ".product", "article", "[data-product-id]",
    "[data-sku]", "[class*=deal-item]", "[class*=listing]", "[class*=result-item]", "[class*=search-result]",
    ".card",
]


def _price_value(s: str) -> Optional[float]:
    try:
        return float(s.replace(",", ""))
    except ValueError:
        return None


def _prices_in(text: str) -> list[float]:
    vals = []
    for m in _PRICE.finditer(text):
        pre = text[max(0, m.start() - 12):m.start()].lower()
        if "save" in pre or "off" in pre or "over" in pre:
            continue
        v = _price_value(m.group(1))
        if v is not None and v >= 2:
            vals.append(v)
    return vals


def _inside_bad(el: Tag, card: Tag) -> bool:
    p = el.parent
    while p is not None and p is not card:
        if _BAD_PRICE_CLASS.search(" ".join(p.get("class", []) or [])):
            return True
        p = p.parent
    return False


def _card_price(card: Tag) -> Optional[float]:
    vals: list[float] = []
    for el in card.find_all(class_=re.compile("price", re.I)):
        classes = " ".join(el.get("class", []))
        if _BAD_PRICE_CLASS.search(classes.replace("price", "")):
            continue
        if _inside_bad(el, card):
            continue
        if el.find(["s", "del", "strike"]):
            continue
        vals += _prices_in(el.get_text(" ", strip=True))
    if not vals:
        for s in card.find_all(["s", "del", "strike"]):
            s.extract()
        vals = _prices_in(card.get_text(" ", strip=True))
    return min(vals) if vals else None


def _match_retailer(text: str, retailers: list[dict]) -> Optional[str]:
    t = text.lower()
    pairs = [(a.lower(), r["name"]) for r in retailers for a in [r["name"], *r.get("aliases", [])]]
    for alias, name in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", t):
            return name
    return None


def _match_marketplace(text: str, markets: list[str]) -> Optional[str]:
    t = text.lower()
    for m in markets:
        if m.lower() in t:
            return m
    return None


# ---------------------------------------------------------------- JSON-LD
def _walk(obj) -> Iterable[dict]:
    if isinstance(obj, dict):
        yield obj
        for v in obj.values():
            yield from _walk(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _walk(v)


def from_jsonld(soup: BeautifulSoup, base_url: str, retailer: str, caliber: Optional[str]) -> list[Listing]:
    out = []
    for s in soup.find_all("script", type=re.compile("ld\\+json", re.I)):
        try:
            data = json.loads(s.string or s.get_text() or "")
        except (json.JSONDecodeError, TypeError):
            continue
        for node in _walk(data):
            t = node.get("@type")
            types = t if isinstance(t, list) else [t]
            if "Product" not in types:
                continue
            name = normalize(str(node.get("name", "")))
            offers = node.get("offers") or {}
            offer_list = offers if isinstance(offers, list) else [offers]
            for o in offer_list:
                if not isinstance(o, dict):
                    continue
                price = _price_value(str(o.get("price") or o.get("lowPrice") or ""))
                if not price:
                    continue
                avail = str(o.get("availability", "InStock"))
                inv = o.get("inventoryLevel")
                qty = None
                if isinstance(inv, dict) and str(inv.get("value", "")).isdigit():
                    qty = int(inv["value"])
                url = urljoin(base_url, str(o.get("url") or node.get("url") or base_url))
                seller = o.get("seller")
                seller_name = seller.get("name") if isinstance(seller, dict) else None
                out.append(
                    Listing(
                        title=name,
                        retailer=seller_name or retailer,
                        url=url,
                        price=price,
                        rounds=detect_rounds(name) or detect_rounds(normalize(str(node.get("description", "")))),
                        in_stock=not re.search(r"OutOfStock|BackOrder|PreOrder|SoldOut|Discontinued", avail, re.I),
                        stock_qty=qty,
                        source=base_url,
                        caliber_hint=caliber,
                        extra_text=normalize(str(node.get("description", "")))[:400],
                    )
                )
                break  # one offer per product
    return out


# ---------------------------------------------------------------- tables
def from_tables(soup: BeautifulSoup, base_url: str, retailer: str, caliber: Optional[str], retailers: list[dict], markets: list[str]) -> list[Listing]:
    out = []
    for table in soup.find_all("table"):
        header_cells = None
        thead = table.find("thead")
        if thead:
            header_cells = thead.find_all(["th", "td"])
        else:
            first = table.find("tr")
            if first and first.find("th"):
                header_cells = first.find_all(["th", "td"])
        if not header_cells:
            continue
        heads = [h.get_text(" ", strip=True).lower() for h in header_cells]

        def col(*names):
            for i, h in enumerate(heads):
                if any(n in h for n in names):
                    return i
            return None

        c_price = col("price", "cost")
        c_rounds = col("rounds", "count", "qty", "quantity", "# rds", "rds")
        c_store = col("retailer", "store", "vendor", "seller", "dealer")
        c_desc = col("description", "product", "item", "title", "name")
        c_ship = col("shipping", "ship")
        if c_price is None or (c_desc is None and c_rounds is None):
            continue
        body_rows = (table.find("tbody") or table).find_all("tr")
        for tr in body_rows:
            cells = tr.find_all(["td", "th"])
            if len(cells) < len(heads) - 1 or tr.find("th") and not tr.find("td"):
                continue

            def cell(i):
                return cells[i].get_text(" ", strip=True) if i is not None and i < len(cells) else ""

            row_text = tr.get_text(" ", strip=True)
            prices = _prices_in(cell(c_price))
            if not prices:
                continue
            title = normalize(cell(c_desc) or row_text)
            rounds = None
            rc = re.sub(r"[^\d]", "", cell(c_rounds)) if c_rounds is not None else ""
            if rc and 0 < int(rc) <= 20000:
                rounds = int(rc)
            ship = None
            if c_ship is not None:
                st = cell(c_ship).lower()
                if "free" in st:
                    ship = 0.0
                else:
                    sp = _prices_in(st.replace("$", " $")) or [
                        float(x) for x in re.findall(r"\$\s?(\d+\.\d{2})", st)
                    ]
                    ship = sp[0] if sp else None
            store_text = cell(c_store)
            if not store_text:
                img = cells[c_store].find("img") if c_store is not None and c_store < len(cells) else None
                store_text = img.get("alt", "") if img else ""
            store = _match_retailer(store_text or row_text, retailers) or store_text or retailer
            link = tr.find("a", href=True)
            url = urljoin(base_url, link["href"]) if link else base_url
            out.append(
                Listing(
                    title=title,
                    retailer=store,
                    url=url,
                    price=min(prices),
                    rounds=rounds or detect_rounds(title),
                    in_stock=not _OOS.search(row_text),
                    stock_qty=int(q.group(1)) if (q := _QTY.search(row_text)) else None,
                    shipping=ship,
                    source=base_url,
                    caliber_hint=caliber,
                    extra_text=row_text[:400],
                    marketplace=bool(_match_marketplace(store_text or row_text, markets)),
                )
            )
    return out


# ---------------------------------------------------------------- cards
def _title_link(card: Tag) -> Optional[Tag]:
    best, best_len = None, 0
    for a in card.find_all("a", href=True):
        txt = a.get_text(" ", strip=True) or a.get("title", "")
        if a["href"].startswith(("#", "javascript", "mailto")):
            continue
        score = len(txt) + (200 if detect_caliber(txt) else 0)
        if score > best_len:
            best, best_len = a, score
    return best


def from_cards(soup: BeautifulSoup, base_url: str, retailer: str, caliber: Optional[str], retailers: list[dict], markets: list[str], aggregator: bool) -> list[Listing]:
    cands: list[Tag] = []
    seen = set()
    for sel in _CARD_SELECTORS:
        try:
            found = soup.select(sel)
        except Exception:
            continue
        for el in found:
            if id(el) not in seen:
                seen.add(id(el))
                cands.append(el)

    def qualifies(el: Tag) -> bool:
        txt = el.get_text(" ", strip=True)
        return len(txt) < 3000 and "$" in txt and el.find("a", href=True) is not None and detect_caliber(txt) is not None

    good = [el for el in cands if qualifies(el)]
    good_ids = {id(e) for e in good}
    # keep innermost qualifying blocks only
    inner = [el for el in good if not any(id(d) in good_ids for d in el.find_all(True))]

    if not inner:  # fallback: climb up from caliber-named links until a price appears
        for a in soup.find_all("a", href=True):
            if not detect_caliber(a.get_text(" ", strip=True)):
                continue
            node = a
            for _ in range(5):
                node = node.parent
                if node is None or not isinstance(node, Tag):
                    break
                if _prices_in(node.get_text(" ", strip=True)):
                    if id(node) not in {id(x) for x in inner}:
                        inner.append(node)
                    break

    out = []
    for card in inner:
        link = _title_link(card)
        if link is None:
            continue
        text = normalize(card.get_text(" ", strip=True))
        title = normalize(link.get_text(" ", strip=True) or link.get("title", ""))
        if not detect_caliber(title):
            h = card.find(["h1", "h2", "h3", "h4", "h5"])
            if h and detect_caliber(h.get_text()):
                title = normalize(h.get_text(" ", strip=True))
            elif len(title) < 15:
                title = text[:200]
        price = _card_price(card)
        if price is None:
            continue
        store = retailer
        market = None
        if aggregator:
            store = _match_retailer(text, retailers) or f"Unknown store (via {retailer})"
            market = _match_marketplace(text, markets)
        ship = 0.0 if re.search(r"free shipping|ships free", text, re.I) and aggregator else None
        out.append(
            Listing(
                title=title,
                retailer=store,
                url=urljoin(base_url, link["href"]),
                price=price,
                rounds=detect_rounds(title) or detect_rounds(text),
                in_stock=not _OOS.search(text),
                stock_qty=int(q.group(1)) if (q := _QTY.search(text)) else None,
                shipping=ship,
                source=base_url,
                caliber_hint=caliber,
                extra_text=text[:400],
                marketplace=bool(market),
            )
        )
    return out


def extract(html: str, base_url: str, retailer: str, caliber: Optional[str], retailers: list[dict], markets: list[str], aggregator: bool = False) -> list[Listing]:
    soup = BeautifulSoup(html, "lxml")
    results: list[Listing] = []
    results += from_jsonld(soup, base_url, retailer, caliber)
    results += from_tables(soup, base_url, retailer, caliber, retailers, markets)
    results += from_cards(soup, base_url, retailer, caliber, retailers, markets, aggregator)
    # de-duplicate: prefer the first (structured) occurrence of each URL
    uniq: dict[str, Listing] = {}
    for l in results:
        k = l.url if aggregator else l.url.split("?")[0]
        if k not in uniq:
            uniq[k] = l
        elif uniq[k].rounds is None and l.rounds:
            uniq[k].rounds = l.rounds
    return list(uniq.values())
