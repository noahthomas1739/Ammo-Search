"""Fetch every configured page and extract listings."""
from __future__ import annotations

import logging
from typing import Callable, Optional

from ..fetch import FetchError, get
from ..models import Listing
from .extract import extract

log = logging.getLogger(__name__)


def iter_pages(cfg: dict):
    for r in cfg.get("retailers", []):
        for cal, urls in (r.get("pages") or {}).items():
            for u in urls:
                yield r["name"], cal, u, False
    for a in cfg.get("aggregators", []):
        for cal, urls in (a.get("pages") or {}).items():
            for u in urls:
                yield a["name"], cal, u, True


def collect(cfg: dict, fetcher: Callable[[str], str] = get, only: Optional[str] = None) -> tuple[list[Listing], dict]:
    """Returns (listings, per-page report {url: {"ok": bool, "count": int, "error": str}})."""
    listings: list[Listing] = []
    report: dict[str, dict] = {}
    for name, cal, url, is_agg in iter_pages(cfg):
        if only and only.lower() not in name.lower():
            continue
        try:
            html = fetcher(url)
            found = extract(html, url, name, cal, cfg["retailers"], cfg.get("marketplace_sellers", []), aggregator=is_agg)
            listings += found
            report[url] = {"source": name, "caliber": cal, "ok": True, "count": len(found), "error": ""}
            log.info("%-24s %-8s %3d listings  %s", name, cal, len(found), url)
        except (FetchError, Exception) as e:  # one broken page must not stop the run
            report[url] = {"source": name, "caliber": cal, "ok": False, "count": 0, "error": str(e)[:200]}
            log.warning("%-24s %-8s FAILED %s: %s", name, cal, url, e)
    return listings, report
