"""`python -m ammo_finder discover`: look for machine-readable sources on each site.

For every retailer and aggregator in config.yaml (enabled or not) this reads
robots.txt and tries a few standard feed/sitemap/JSON locations, all with
plain HTTP, robots.txt rules, caching and the usual delays. It reports what
exists so it can be added to config.yaml as `feeds:` or `sitemaps:`.
"""
from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from .fetch import Fetcher, FetchError
from .sources import source_base
from .sources.feeds import SLUG_PATTERNS, parse_sitemap

CANDIDATES = ["/sitemap.xml", "/sitemap_index.xml", "/rss.xml", "/rss", "/feed", "/products.json"]


def classify(body: str) -> str:
    head = body.lstrip()[:600].lower()
    if head.startswith(("{", "[")):
        try:
            json.loads(body)
            return "json"
        except ValueError:
            pass
    if "<rss" in head:
        return "rss"
    if "<feed" in head and "atom" in head:
        return "atom"
    if "<sitemapindex" in head:
        return "sitemap-index"
    if "<urlset" in head:
        return "sitemap"
    if "<html" in head or "<!doctype html" in head:
        return "html page"
    return "other"


def _sitemap_summary(f: Fetcher, url: str, body: str) -> str:
    children, pages = parse_sitemap(body)
    if children:
        sub = []
        for c in children[:6]:  # peek into a few child sitemaps
            try:
                _, p = parse_sitemap(f.get(c, "raw", 24 * 3600))
                pages += p
                sub.append(c.rsplit("/", 1)[-1])
            except FetchError:
                continue
        note = f"{len(children)} child sitemaps (peeked at {len(sub)})"
    else:
        note = f"{len(pages)} URLs"
    hits = {cal: sum(1 for p in pages if re.search(pat, p, re.I)) for cal, pat in SLUG_PATTERNS.items()}
    return f"{note}; caliber-matching URLs: " + ", ".join(f"{k} {v}" for k, v in hits.items())


def run_discover(cfg: dict, f: Fetcher) -> str:
    lines = []
    suggestions = []
    sources = [*cfg.get("retailers", []), *cfg.get("aggregators", [])]
    for src in sources:
        base = source_base(src)
        if not base:
            continue
        lines.append(f"\n{src['name']}  ({base})")
        try:
            robots = f.robots_txt(base)
            sms = re.findall(r"(?im)^\s*sitemap:\s*(\S+)", robots)
            lines.append(f"  robots.txt: found, {len(sms)} sitemap(s) listed")
        except FetchError as e:
            robots, sms = "", []
            note = {"not_found": "none published (no rules)", "forbidden": "refused (treated as: no automated access)"}.get(e.kind, f"{e.category}: {e.kind}")
            lines.append(f"  robots.txt: {note}")
        for cal, urls in (src.get("pages") or {}).items():
            for u in urls[:1]:
                if robots and not f.allowed(u):
                    lines.append(f"  configured {cal} page is DISALLOWED by robots.txt")
        found_any, absent, unknown = False, 0, []
        for url in list(dict.fromkeys(sms + [base + c for c in CANDIDATES])):
            if not f.allowed(url):
                unknown.append(f"{url.replace(base, '')} (robots.txt disallows)")
                continue
            try:
                body = f.get(url, "raw", 24 * 3600)
            except FetchError as e:
                if e.kind == "not_found":
                    absent += 1
                else:
                    unknown.append(f"{url.replace(base, '') or url} ({e.category}: {e.kind})")
                continue
            kind = classify(body)
            if kind in ("html page", "other"):
                absent += 1  # the URL serves an ordinary page, not a feed
                continue
            found_any = True
            detail = ""
            if kind.startswith("sitemap"):
                detail = " - " + _sitemap_summary(f, url, body)
                suggestions.append(f"{src['name']}: sitemaps: {{urls: [{url}]}}")
            elif kind in ("rss", "atom"):
                n = len(re.findall(r"<(item|entry)[\s>]", body))
                detail = f" - {n} items"
                suggestions.append(f"{src['name']}: feeds: [{url}]")
            elif kind == "json":
                detail = f" - {len(body)} bytes"
                suggestions.append(f"{src['name']}: JSON at {url} (send this to Claude)")
            lines.append(f"  FOUND {kind}: {url}{detail}")
        if unknown:
            # a failed check is not evidence that nothing exists
            lines.append(f"  COULDN'T CHECK {len(unknown)} location(s): " + "; ".join(unknown[:4]))
        if not found_any:
            lines.append(
                "  nothing found at the standard locations that could be checked"
                if not unknown else
                "  result UNKNOWN for the locations above; a feed may still exist (check the site's footer/help pages)"
            )
    if suggestions:
        lines += ["", "Suggested additions to config.yaml:", *[f"  - {s}" for s in suggestions]]
    text = "\n".join(lines)
    print(text)
    return text
