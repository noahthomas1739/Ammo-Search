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

log = logging.getLogger(__name__)

# Narrow sitemap URLs to likely matches before loading any product pages
DEFAULT_SITEMAP_INCLUDE = {
    "9mm": r"(9mm|9-mm|9x19).*(fmj|115|124|ball|range|target)|(fmj|115|124).*(9mm|9-mm|9x19)",
    "12ga": r"12-?ga|12-gauge.*(1-1-4|1-1-8|1-25|1-125|oz|heavy|high-velocity|field|dove|game)",
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

        # 1) feeds
        for url in src.get("feeds") or []:
            try:
                found = parse_feed(fetch(url, "raw", None), name, retailers, markets)
                listings += found
                record(url, name, "all", "feed", found)
            except Exception as e:
                record(url, name, "all", "feed", [], e)

        # 2) sitemaps
        sm = src.get("sitemaps")
        if sm:
            try:
                sm_urls = _sitemap_roots(sm.get("urls", ["robots"]), src, fetch, fetcher)
            except Exception as e:
                record(f"{name} sitemaps", name, "all", "sitemap", [], e)
                sm_urls = []
            for cal in cals:
                if sm_urls:
                    _from_sitemap(cfg, src, cal, sm_urls, sm, fetch, fetcher, listings, record)

        # 3) listing pages
        for cal, urls in (src.get("pages") or {}).items():
            for url in urls:
                try:
                    found = extract(fetch(url, "html", None), url, name, cal, retailers, markets, aggregator=agg)
                    listings += found
                    record(url, name, cal, "page", found)
                except Exception as e:
                    record(url, name, cal, "page", [], e)
    return listings, report


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
