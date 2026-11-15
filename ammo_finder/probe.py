"""Diagnostics: fetch every configured page two ways (plain HTTP with a Chrome
fingerprint, and a real headless Chromium) and save what came back, so we can
see which stores block GitHub's servers and which pages load products with
JavaScript.  Output goes to debug/ (uploaded as a workflow artifact).
"""
from __future__ import annotations

import json
import os
import re

from .fetch import FetchError, get
from .sources import iter_pages
from .sources.extract import extract


def _slug(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60]


def _count(cfg, html, url, name, cal, agg) -> int:
    try:
        return len(extract(html, url, name, cal, cfg["retailers"], cfg.get("marketplace_sellers", []), aggregator=agg))
    except Exception:
        return -1


def run_probe(cfg: dict, out_dir: str = "debug", only: str | None = None) -> list[dict]:
    os.makedirs(out_dir, exist_ok=True)
    results = []
    pages = [p for p in iter_pages(cfg) if not only or only.lower() in p[0].lower()]

    for name, cal, url, agg in pages:
        r = {"source": name, "caliber": cal, "url": url}
        try:
            html = get(url, retries=0)
            r["http"] = "200"
            r["http_count"] = _count(cfg, html, url, name, cal, agg)
            open(f"{out_dir}/{_slug(name)}-{cal}-http.html", "w").write(html)
        except FetchError as e:
            r["http"] = str(e)[-60:]
        results.append(r)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("playwright not installed; skipping browser probe")
    else:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
                viewport={"width": 1366, "height": 900},
                locale="en-US",
            )
            for r in results:
                page = ctx.new_page()
                try:
                    resp = page.goto(r["url"], wait_until="domcontentloaded", timeout=45000)
                    page.wait_for_timeout(8000)  # let JS / bot challenges finish
                    html = page.content()
                    r["browser"] = str(resp.status if resp else "?")
                    r["browser_title"] = (page.title() or "")[:60]
                    r["browser_count"] = _count(cfg, html, r["url"], r["source"], r["caliber"], r["source"] in ("AmmoSeek", "gun.deals"))
                    open(f"{out_dir}/{_slug(r['source'])}-{r['caliber']}-browser.html", "w").write(html)
                except Exception as e:
                    r["browser"] = f"error: {str(e)[:60]}"
                finally:
                    page.close()
            browser.close()

    with open(f"{out_dir}/probe.json", "w") as f:
        json.dump(results, f, indent=1)
    lines = ["| Source | Cal | HTTP | # | Browser | # | Page title |", "|---|---|---|---|---|---|---|"]
    for r in results:
        lines.append(
            f"| {r['source']} | {r['caliber']} | {r.get('http','')} | {r.get('http_count','')} | "
            f"{r.get('browser','')} | {r.get('browser_count','')} | {r.get('browser_title','')} |"
        )
    text = "\n".join(lines)
    print(text)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as f:
            f.write("## Probe\n\n" + text + "\n")
    return results
