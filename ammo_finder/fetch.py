"""Polite page fetching.

- HTML pages load in a standard headless Chromium (Playwright), so pages that
  build their product lists with JavaScript can be read. No stealth tweaks: the
  browser identifies itself as headless Chrome.
- Feeds, sitemaps, JSON and robots.txt use plain HTTP with an honest user agent.
- robots.txt is obeyed. A URL a site disallows is skipped.
- Every response (including refusals) is cached on disk, so running the monitor,
  `doctor`, etc. several times in a row doesn't re-hit the store.
- Waits a random, human-like 8-20 s between loads on the same site.
- A 403/429 or a bot-check page marks the site as blocked for the rest of the
  run. It is skipped, never retried or worked around.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import logging
import os
import random
import time
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger(__name__)

FEED_UA = "AmmoFinder/1.0 (personal price monitor; low volume)"
_BLOCK_TITLES = ("just a moment", "attention required", "access denied", "are you a robot", "pardon our interruption")


class FetchError(Exception):
    pass


class Fetcher:
    def __init__(
        self,
        cache_dir: str = ".cache",
        cache_hours: float = 5,
        min_delay: float = 8,
        max_delay: float = 20,
        use_browser: bool = True,
        respect_robots: bool = True,
    ):
        self.cache_dir = cache_dir
        self.ttl = cache_hours * 3600
        self.delay = (min_delay, max_delay)
        self.use_browser = use_browser
        self.respect_robots = respect_robots
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, Optional[RobotFileParser]] = {}
        self._blocked: dict[str, str] = {}  # host -> reason, for this run
        self._pw = self._browser = self._page = None
        self.last_from_cache = False
        os.makedirs(cache_dir, exist_ok=True)

    # ------------------------------------------------------------ public
    def get(self, url: str, kind: str = "html", ttl: Optional[float] = None) -> str:
        """kind="html": rendered page (browser if available). kind="raw": the bytes as served (feeds, XML, JSON)."""
        ttl = self.ttl if ttl is None else ttl
        cached = self._cache_read(url, kind, ttl)
        self.last_from_cache = cached is not None
        if cached is not None:
            if cached.get("error"):
                raise FetchError(cached["error"] + " (cached)")
            return cached["body"]

        host = urlparse(url).netloc.lower()
        if host in self._blocked:
            raise FetchError(f"{url}: skipped, site blocked automated requests earlier this run")
        if self.respect_robots and not self.allowed(url):
            err = f"{url}: disallowed by the site's robots.txt"
            self._cache_write(url, kind, error=err)
            raise FetchError(err)

        self._pace(host)
        try:
            if kind == "html" and self.use_browser and _playwright_installed():
                body = self._browser_get(url)
            else:
                body = self._http_get(url, kind)
        except FetchError as e:
            msg = str(e)
            if "HTTP 403" in msg or "HTTP 429" in msg or "bot check" in msg:
                self._blocked[host] = msg
            if "HTTP " in msg or "bot check" in msg:  # a site's answer; connection errors aren't cached
                self._cache_write(url, kind, error=msg)
            raise
        self._cache_write(url, kind, body=body)
        return body

    def allowed(self, url: str) -> bool:
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp: Optional[RobotFileParser] = RobotFileParser()
            try:
                rp.parse(self.robots_txt(base).splitlines())
            except FetchError:
                rp = None  # no readable robots.txt -> the site publishes no restrictions
            self._robots[base] = rp
        rp = self._robots[base]
        return True if rp is None else rp.can_fetch("*", url)

    def robots_txt(self, base: str) -> str:
        url = base.rstrip("/") + "/robots.txt"
        cached = self._cache_read(url, "raw", 24 * 3600)
        if cached is not None:
            if cached.get("error"):
                raise FetchError(cached["error"])
            return cached["body"]
        self._pace(urlparse(url).netloc.lower())
        try:
            body = self._http_get(url, "raw")
        except FetchError as e:
            self._cache_write(url, "raw", error=str(e))
            raise
        self._cache_write(url, "raw", body=body)
        return body

    def close(self) -> None:
        try:
            if self._browser:
                self._browser.close()
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self._pw = self._browser = self._page = None

    def prune_cache(self, max_age_days: float = 3) -> None:
        cutoff = time.time() - max_age_days * 86400
        for name in os.listdir(self.cache_dir):
            p = os.path.join(self.cache_dir, name)
            try:
                if os.path.getmtime(p) < cutoff:
                    os.remove(p)
            except OSError:
                pass

    # ------------------------------------------------------------ internals
    def _pace(self, host: str) -> None:
        last = self._last_hit.get(host)
        if last is not None:
            wait = last + random.uniform(*self.delay) - time.time()
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.time()

    def _http_get(self, url: str, kind: str) -> str:
        try:
            r = requests.get(url, headers={"User-Agent": FEED_UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
        except requests.RequestException as e:
            raise FetchError(f"{url}: {type(e).__name__}") from e
        if r.status_code != 200:
            raise FetchError(f"{url}: HTTP {r.status_code}")
        data = r.content
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        body = data.decode(r.encoding or "utf-8", errors="replace")
        if kind == "html" and _looks_blocked(body):
            raise FetchError(f"{url}: site shows a bot check")
        return body

    def _browser_get(self, url: str) -> str:
        if self._page is None:
            from playwright.sync_api import sync_playwright

            self._pw = sync_playwright().start()
            self._browser = self._pw.chromium.launch(headless=True)
            self._page = self._browser.new_context(locale="en-US").new_page()
        try:
            resp = self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
            try:
                self._page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                pass  # some pages never go fully idle; what has loaded is enough
            status = resp.status if resp else 0
            title = (self._page.title() or "").lower()
            html = self._page.content()
        except Exception as e:
            raise FetchError(f"{url}: browser error {type(e).__name__}") from e
        if status in (403, 429):
            raise FetchError(f"{url}: HTTP {status}")
        if any(t in title for t in _BLOCK_TITLES):
            raise FetchError(f"{url}: site shows a bot check")
        if status >= 400:
            raise FetchError(f"{url}: HTTP {status}")
        return html

    def _cache_path(self, url: str, kind: str) -> str:
        return os.path.join(self.cache_dir, hashlib.sha1(f"{kind}:{url}".encode()).hexdigest() + ".json")

    def _cache_read(self, url: str, kind: str, ttl: float) -> Optional[dict]:
        try:
            with open(self._cache_path(url, kind)) as f:
                entry = json.load(f)
        except (OSError, ValueError):
            return None
        return entry if time.time() - entry.get("at", 0) < ttl else None

    def _cache_write(self, url: str, kind: str, body: str = "", error: str = "") -> None:
        with open(self._cache_path(url, kind), "w") as f:
            json.dump({"url": url, "at": time.time(), "body": body, "error": error}, f)


def _looks_blocked(body: str) -> bool:
    head = body[:4000].lower()
    return "cf-chl" in head or any(f"<title>{t}" in head for t in _BLOCK_TITLES)


def _playwright_installed() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def from_config(cfg: dict, cache_dir: str) -> Fetcher:
    f = cfg.get("fetch") or {}
    return Fetcher(
        cache_dir=cache_dir,
        cache_hours=f.get("cache_hours", 5),
        min_delay=f.get("min_delay_seconds", 8),
        max_delay=f.get("max_delay_seconds", 20),
        use_browser=f.get("browser", True),
        respect_robots=f.get("respect_robots_txt", True),
    )
