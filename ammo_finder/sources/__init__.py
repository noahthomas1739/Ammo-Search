"""Gather listings from every enabled source.

Per source (retailer or aggregator) in config.yaml, in order of preference:
  feeds:    RSS/Atom deal feeds (read directly)
  sitemaps: XML sitemaps -> matching product pages (read via their product data)
  pages:    listing/category pages (rendered in a headless browser)
"""
from __future__ import annotations

import inspect
import logging
import re
from typing import Callable, Optional
from urllib.parse import urlparse

from ..fetch import FetchError
from ..models import Listing
from .extract import extract
from .feeds import parse_feed, sitemap_product_urls
from .shopify import parse_products_json, product_count

log = logging.getLogger(__name__)

# Narrow sitemap URLs to likely matches before loading any product pages
DEFAULT_SITEMAP_INCLUDE = {
    "9mm": r"((?<![\dx.])9-?mm|9x19).*(fmj|115|124|ball|range|target)|(fmj|115|124).*((?<![\dx.])9-?mm|9x19)",
    "12ga": r"(12-?ga|12-gauge).*(1-1-4|1-1-8|1-25|1-125|oz|ounce|heavy|high-velocity|field|dove|game)",
    "762x54r": r"7-?62-?x-?54|762x54",
}


def _sources(cfg: dict):
    for r in cfg.get("retailers", []):
        if r.get("enabled") is not False:
            yield r, False
    for a in cfg.get("aggregators", []):
        if a.get("enabled") is not False:
            yield a, True


def iter_pages(cfg: dict):
    for src, agg in _sources(cfg):
        for cal, urls in (src.get("pages") or {}).items():
            for u in urls:
                yield src["name"], cal, u, agg


def source_base(src: dict) -> Optional[str]:
    """https://host of a source: base_url, else the host its pages use, else its domain."""
    if src.get("base_url"):
        return src["base_url"].rstrip("/")
    for urls in (src.get("pages") or {}).values():
        if urls:
            p = urlparse(urls[0])
            return f"{p.scheme}://{p.netloc}"
    return f"https://www.{src['domain']}" if src.get("domain") else None


def _err_info(exc: Optional[Exception]) -> tuple[str, str, str]:
    if exc is None:
        return "", "ok", ""
    if isinstance(exc, FetchError):
        return exc.kind, exc.category, str(exc)
    return "parse_error", "other", f"{type(exc).__name__}: {exc}"


def coverage(cfg: dict, listings: list[Listing], report: dict) -> dict[str, dict]:
    """Per retailer: did we reach it directly, through an alternative source
    (an aggregator feed attributing listings to it), or not at all this run?
    'not reached' is NOT the same as 'no deals': it means we couldn't look."""
    out: dict[str, dict] = {}
    via_alt: dict[str, dict[str, int]] = {}
    agg_names = {a["name"] for a in cfg.get("aggregators", [])}
    for l in listings:
        src = report.get(l.source, {}).get("source")
        if src in agg_names:
            via_alt.setdefault(l.retailer, {}).setdefault(src, 0)
            via_alt[l.retailer][src] += 1
    for r in cfg.get("retailers", []):
        mine = [v for v in report.values() if v["source"] == r["name"]]
        direct = [v for v in mine if v["ok"] and v["count"] > 0]
        alt = via_alt.get(r["name"], {})
        cats = sorted({v["category"] for v in mine if not v["ok"]})
        if direct:
            status = "direct"
        elif alt:
            status = "alternative"
        elif not mine:
            status = "not configured" if r.get("enabled") is not False else "disabled"
        elif "temporary" in cats:
            status = "temporary failure"
        elif "restricted" in cats:
            status = "restricted"
        else:
            status = "no readable listings"
        out[r["name"]] = {
            "status": status,
            "direct_routes": sorted({v["via"] for v in direct}),
            "alternatives": alt,
            "failures": sorted({f"{v['via']}: {v['error_kind']}" for v in mine if not v["ok"]}),
        }
    return out


def _wrap(fetcher) -> Callable:
    """Accept a Fetcher, a get(url, kind, ttl) function, or a simple test stub fetcher(url)."""
    if hasattr(fetcher, "get") and hasattr(fetcher, "robots_txt"):
        return fetcher.get
    try:
        n = len(inspect.signature(fetcher).parameters)
    except (TypeError, ValueError):
        n = 3
    if n >= 2:
        return fetcher
    return lambda url, kind="html", ttl=None: fetcher(url)


def collect(cfg: dict, fetcher: Callable, only: Optional[str] = None) -> tuple[list[Listing], dict]:
    """Returns (listings, report {key: {source, caliber, ok, count, error, via}})."""
    fetch = _wrap(fetcher)
    retailers, markets = cfg["retailers"], cfg.get("marketplace_sellers", [])
    listings: list[Listing] = []
    report: dict[str, dict] = {}
    cals = ("9mm", "12ga", "762x54r")

    def record(key, name, cal, via, found, exc: Optional[Exception] = None):
        for l in found:
            l.source = key  # lets `doctor` show samples per source
        kind, category, msg = _err_info(exc)
        report[key] = {
            "source": name, "caliber": cal, "via": via, "count": len(found), "ok": exc is None,
            "error": msg[:200], "error_kind": kind, "category": category,
        }
        if exc is not None:
            log.warning("%-24s %-8s %-8s %-10s %s", name, cal, via, category.upper(), msg)
        else:
            log.info("%-24s %-8s %-8s %3d listings  %s", name, cal, via, len(found), key)

    for src, agg in _sources(cfg):
        name = src["name"]
        if only and only.lower() not in name.lower():
            continue

        done: set = set()  # calibers already covered by a structured source; pages for them are skipped

        # 0) public catalog JSON (Shopify stores)
        base = source_base(src)
        for cal, handles in (src.get("shopify_collections") or {}).items():
            found, err, first = [], None, None
            for h in handles:
                for page in range(1, (src.get("json_max_pages") or 4) + 1):
                    url = f"{base}/collections/{h}/products.json?limit=250&page={page}"
                    first = first or url
                    try:
                        body = fetch(url, "raw", None)
                    except Exception as e:
                        err = e
                        break
                    found += parse_products_json(body, base, name, cal)
                    if product_count(body) < 250:
                        break
            listings += found
            record(first, name, cal, "json", found, None if found or err is None else err)
            if found:
                done.add(cal)

        # 1) feeds
        for url in src.get("feeds") or []:
            try:
                found = parse_feed(fetch(url, "raw", None), name, retailers, markets)
                listings += found
                record(url, name, "all", "feed", found)
            except Exception as e:
                record(url, name, "all", "feed", [], e)

        # 2) listing pages. A category page shows many products in one load,
        # so it's lighter on the store than the sitemap route below.
        for cal, urls in (src.get("pages") or {}).items():
            if cal in done:
                continue
            for url in urls:
                try:
                    found = extract(fetch(url, "html", None), url, name, cal, retailers, markets, aggregator=agg)
                    listings += found
                    record(url, name, cal, "page", found)
                    if found:
                        done.add(cal)
                except Exception as e:
                    record(url, name, cal, "page", [], e)

        # 3) sitemap -> product pages, for calibers nothing above covered
        sm = src.get("sitemaps")
        todo = [c for c in cals if c not in done]
        if sm and todo:
            try:
                sm_urls = _sitemap_roots(sm.get("urls", ["robots"]), src, fetch, fetcher)
            except Exception as e:
                record(f"{name} sitemaps", name, "all", "sitemap", [], e)
                sm_urls = []
            for cal in todo:
                if sm_urls:
                    _from_sitemap(cfg, src, cal, sm_urls, sm, fetch, fetcher, listings, record)
    _fill_round_counts(cfg, listings, fetch, fetcher, report)
    return listings, report


_SPEC_COUNT = re.compile(
    r"(?<![a-z])(?:round count|rounds? per (?:box|case|pack)|rounds?/(?:box|case)|box (?:qty|quantity|count)|"
    r"quantity|qty|count|# of rounds|number of rounds|rounds)\s*[:\-]?\s*(\d{1,2},\d{3}|\d{1,4})\b",
    re.I,
)


def rounds_from_product_page(html: str) -> Optional[int]:
    """Round count from a product page: its schema.org name/description, a specs
    line like "Rounds Per Box: 50", or the main heading. Related-product blocks
    are ignored by preferring those structured spots over free text."""
    from bs4 import BeautifulSoup
    from ..parse import detect_rounds, normalize
    from .extract import from_jsonld

    soup = BeautifulSoup(html, "lxml")
    for l in from_jsonld(soup, "https://x", "x", None)[:1]:
        if l.rounds:
            return l.rounds
    for el in soup.find_all(["tr", "td", "th", "li", "dl", "dt", "dd", "div", "span", "p"]):
        t = normalize(el.get_text(" ", strip=True))
        if len(t) > 80:
            continue
        m = _SPEC_COUNT.search(t)
        if m:
            n = int(m.group(1).replace(",", ""))
            if 5 <= n <= 5000:
                return n
    h1 = soup.find("h1")
    return detect_rounds(normalize(h1.get_text(" "))) if h1 else None


def _fill_round_counts(cfg, listings, fetch, fetcher, report) -> None:
    """Listings without a round count can't be priced per round. For in-stock
    listings that otherwise look like a match, read the count from the product
    page (cached a week, at most `fetch.round_count_lookups` new loads per site per run)."""
    from ..parse import parse
    limit = (cfg.get("fetch") or {}).get("round_count_lookups", 15)
    loads: dict[str, int] = {}
    for l in listings:
        if l.rounds or not l.in_stock or l.marketplace or "/" not in l.url[8:]:
            continue
        if any(v.get("via") == "feed" and k == l.source for k, v in report.items()):
            continue  # feed items link to the aggregator, not the product
        a = parse(f"{l.title} {l.extra_text}", l.caliber_hint)
        if a.caliber not in ("9mm", "12ga", "762x54r") or a.reloaded or a.not_ammo or a.expanding:
            continue
        if a.caliber == "9mm" and (not a.fmj or a.case in ("steel", "aluminum") or (a.grain and a.grain not in (115, 124))):
            continue
        if a.caliber == "12ga" and (a.not_birdshot or a.shot_material in ("steel", "bismuth", "tungsten") or (a.shot_oz and a.shot_oz not in (1.125, 1.25))):
            continue
        host = urlparse(l.url).netloc
        if loads.get(host, 0) >= limit:
            continue
        try:
            html = fetch(l.url, "html", 7 * 86400)
        except Exception as e:
            if isinstance(e, FetchError) and (e.category == "restricted" or e.kind == "rate_limited"):
                loads[host] = limit  # stop asking this site this run
            continue
        if not getattr(fetcher, "last_from_cache", False):
            loads[host] = loads.get(host, 0) + 1
        n = rounds_from_product_page(html)
        if n:
            l.rounds = n
            l.extra_text = f"{l.extra_text} [round count from product page]"


def _sitemap_roots(urls: list[str], src: dict, fetch, fetcher) -> list[str]:
    out = []
    for u in urls:
        if u != "robots":
            out.append(u)
            continue
        base = source_base(src)
        if not base:
            continue
        txt = fetcher.robots_txt(base) if hasattr(fetcher, "robots_txt") else fetch(base + "/robots.txt", "raw", None)
        found = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", txt)
        out += found or [base + "/sitemap.xml"]
    return out


def _from_sitemap(cfg, src, cal, sm_urls, sm, fetch, fetcher, listings, record):
    name = src["name"]
    include = (sm.get("include") or {}).get(cal) or DEFAULT_SITEMAP_INCLUDE[cal]
    try:
        urls = sitemap_product_urls(fetch, sm_urls, cal, include)
    except Exception as e:
        record(f"{name} {cal} sitemap", name, cal, "sitemap", [], e)
        return
    limit = sm.get("max_products", 15)  # new (uncached) product pages loaded per run
    loaded, found = 0, []
    for url in urls:
        if loaded >= limit:
            break
        try:
            html = fetch(url, "html", None)
            if not getattr(fetcher, "last_from_cache", False):
                loaded += 1
            found += extract(html, url, name, cal, cfg["retailers"], cfg.get("marketplace_sellers", []))[:1]
        except FetchError as e:
            if e.category == "restricted" or e.kind == "rate_limited":
                listings += found
                record(f"{name} {cal} sitemap", name, cal, "sitemap", found, e)
                return
        except Exception:
            continue
    listings += found
    record(f"{name} {cal} sitemap ({len(urls)} matching URLs)", name, cal, "sitemap", found)
