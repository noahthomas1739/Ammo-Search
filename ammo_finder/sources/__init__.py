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

    def record(key, name, cal, via, found, err=""):
        for l in found:
            l.source = key  # lets `doctor` show samples per source
        report[key] = {"source": name, "caliber": cal, "ok": not err, "count": len(found), "error": err[:200], "via": via}
        if err:
            log.warning("%-24s %-8s %-8s FAILED %s", name, cal, via, err)
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
                record(url, name, "all", "feed", [], str(e))

        # 2) sitemaps
        sm = src.get("sitemaps")
        if sm:
            try:
                sm_urls = _sitemap_roots(sm.get("urls", ["robots"]), src, fetch, fetcher)
            except Exception as e:
                record(f"{name} sitemaps", name, "all", "sitemap", [], str(e))
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
                    record(url, name, cal, "page", [], str(e))
    return listings, report


def _sitemap_roots(urls: list[str], src: dict, fetch, fetcher) -> list[str]:
    out = []
    for u in urls:
        if u != "robots":
            out.append(u)
            continue
        base = "https://" + src["domain"] if src.get("domain") else None
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
        record(f"{name} {cal} sitemap", name, cal, "sitemap", [], str(e))
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
            if "blocked" in str(e) or "HTTP 403" in str(e):
                record(f"{name} {cal} sitemap", name, cal, "sitemap", found, str(e))
                listings += found
                return
        except Exception:
            continue
    listings += found
    record(f"{name} {cal} sitemap ({len(urls)} matching URLs)", name, cal, "sitemap", found)
