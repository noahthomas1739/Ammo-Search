"""Polite, rule-following page fetching.

- HTML pages load in a standard headless Chromium (Playwright), so pages that
  build their product lists with JavaScript can be read. No stealth settings:
  the browser identifies itself as headless Chrome.
- Feeds, sitemaps, JSON and robots.txt use plain HTTP with an honest user agent.
- robots.txt is obeyed.
- Random 8-20 s pause between loads on the same site; responses cached on disk.

Every failure is classified (see FAILURE_KINDS) and handled by kind:
  temporary  (429, 5xx, timeout, network): retried with backoff, honoring
             Retry-After; never cached, so the next run tries again.
  restricted (401/403, CAPTCHA / bot check, robots.txt disallow): the site has
             said no to automated access. Not retried or worked around; the
             answer is cached and re-checked after `restricted_recheck_days`.
  not_found  (404/410): the URL is wrong or gone; cached for a day.
A failed fetch only means *this* route didn't work. It is never treated as
"no products" or "no deals".
"""
from __future__ import annotations

import email.utils
import gzip
import hashlib
import json
import logging
import os
import random
import re
import time
from typing import Optional
from urllib.parse import urlparse
from urllib.robotparser import RobotFileParser

import requests

log = logging.getLogger(__name__)

FEED_UA = "AmmoFinder/1.0 (personal price monitor; low volume)"
_BLOCK_TITLES = ("just a moment", "attention required", "access denied", "are you a robot", "pardon our interruption", "captcha", "verify you are human")
_CAPTCHA_MARKERS = re.compile(r"g-recaptcha|h-captcha|hcaptcha\.com|cf-turnstile|challenges\.cloudflare\.com|cf-chl|px-captcha|/captcha/", re.I)

FAILURE_KINDS = {
    "rate_limited": "temporary",
    "server_error": "temporary",
    "timeout": "temporary",
    "network": "temporary",
    "forbidden": "restricted",
    "bot_check": "restricted",
    "robots_disallowed": "restricted",
    "not_found": "not_found",
    "other": "temporary",
}


class FetchError(Exception):
    def __init__(self, url: str, kind: str, detail: str = "", retry_after: Optional[float] = None):
        self.url, self.kind, self.detail, self.retry_after = url, kind, detail, retry_after
        super().__init__(f"{url}: {kind}{' (' + detail + ')' if detail else ''}")

    @property
    def category(self) -> str:
        return FAILURE_KINDS.get(self.kind, "temporary")


def _status_error(url: str, status: int, retry_after: Optional[float] = None) -> FetchError:
    if status == 429:
        return FetchError(url, "rate_limited", "HTTP 429", retry_after)
    if status in (401, 403):
        return FetchError(url, "forbidden", f"HTTP {status}")
    if status in (404, 410):
        return FetchError(url, "not_found", f"HTTP {status}")
    if status >= 500:
        return FetchError(url, "server_error", f"HTTP {status}", retry_after)
    return FetchError(url, "other", f"HTTP {status}")


def _parse_retry_after(v: Optional[str]) -> Optional[float]:
    if not v:
        return None
    if v.strip().isdigit():
        return float(v.strip())
    try:
        return max(0.0, email.utils.parsedate_to_datetime(v).timestamp() - time.time())
    except (TypeError, ValueError):
        return None


class Fetcher:
    def __init__(
        self,
        cache_dir: str = ".cache",
        cache_hours: float = 5,
        min_delay: float = 8,
        max_delay: float = 20,
        use_browser: bool = True,
        respect_robots: bool = True,
        max_retries: int = 2,
        backoff_seconds: float = 15,
        max_retry_wait: float = 120,
        restricted_recheck_days: float = 7,
    ):
        self.cache_dir = cache_dir
        self.ttl = cache_hours * 3600
        self.delay = (min_delay, max_delay)
        self.use_browser = use_browser
        self.respect_robots = respect_robots
        self.max_retries = max_retries
        self.backoff = backoff_seconds
        self.max_retry_wait = max_retry_wait
        self.restricted_ttl = restricted_recheck_days * 86400
        self.host_delay: dict = {}  # host -> (min, max) seconds between loads, for slower sites
        self._last_hit: dict[str, float] = {}
        self._robots: dict = {}  # base -> RobotFileParser | None (no rules) | FetchError (temporary)
        self._host_stop: dict[str, FetchError] = {}  # host -> reason to stop for this run
        self._pw = self._browser = self._page = None
        self.last_from_cache = False
        os.makedirs(cache_dir, exist_ok=True)

    # ------------------------------------------------------------ public
    def get(self, url: str, kind: str = "html", ttl: Optional[float] = None) -> str:
        """kind="html": rendered page (browser if available). kind="raw": the bytes as served (feeds, XML, JSON)."""
        cached = self._cache_read(url, kind, self.ttl if ttl is None else ttl)
        self.last_from_cache = cached is not None
        if cached is not None:
            if cached.get("error"):
                e = cached["error"]
                raise FetchError(url, e["kind"], f"{e.get('detail', '')}, cached answer".strip(", "))
            return cached["body"]

        host = urlparse(url).netloc.lower()
        if host in self._host_stop:
            prev = self._host_stop[host]
            raise FetchError(url, prev.kind, f"skipped: site answered {prev.detail or prev.kind} earlier this run")
        if self.respect_robots and not self.allowed(url):
            err = FetchError(url, "robots_disallowed", "robots.txt")
            self._cache_error(url, kind, err)
            raise err

        attempt = 0
        while True:
            self._pace(host)
            try:
                if kind == "html" and self.use_browser and _playwright_installed():
                    body = self._browser_get(url)
                else:
                    body = self._http_get(url, kind)
                self._cache_write(url, kind, body=body)
                return body
            except FetchError as e:
                if e.kind == "rate_limited":  # the site says we're too fast: slow down for the rest of the run
                    lo, hi = self.host_delay.get(host, self.delay)
                    self.host_delay[host] = (lo * 2, hi * 2)
                    log.info("%s asked us to slow down; now %.0f-%.0fs between its pages", host, lo * 2, hi * 2)
                if e.category == "temporary" and attempt < self.max_retries:
                    wait = e.retry_after if e.retry_after is not None else self.backoff * (3 ** attempt) * random.uniform(0.8, 1.2)
                    if wait <= self.max_retry_wait:
                        log.info("%s: %s, retrying in %.0fs", url, e.kind, wait)
                        time.sleep(wait)
                        attempt += 1
                        continue
                    e.detail += f", asked to wait {wait:.0f}s, will retry next run"
                if e.category == "restricted" or e.kind == "rate_limited":
                    self._host_stop[host] = e  # respect it: no more requests to this site this run
                self._cache_error(url, kind, e)
                raise

    def allowed(self, url: str) -> bool:
        """robots.txt check. 404 = no rules published -> allowed; 401/403 = treated as
        'no automated access'; a temporary failure raises (temporary), so the page is
        simply tried again next run rather than being marked restricted."""
        p = urlparse(url)
        base = f"{p.scheme}://{p.netloc}"
        if base not in self._robots:
            rp = RobotFileParser()
            try:
                rp.parse(self.robots_txt(base).splitlines())
            except FetchError as e:
                if e.kind == "not_found":
                    rp = None
                elif e.category == "temporary":
                    self._robots[base] = e  # don't re-request this run; don't cache
                    raise FetchError(url, e.kind, "robots.txt unavailable, will retry next run") from e
                else:
                    rp.parse(["User-agent: *", "Disallow: /"])
            self._robots[base] = rp
        rp = self._robots[base]
        if isinstance(rp, FetchError):
            raise FetchError(url, rp.kind, "robots.txt unavailable, will retry next run")
        return True if rp is None else rp.can_fetch("*", url)

    def robots_txt(self, base: str) -> str:
        url = base.rstrip("/") + "/robots.txt"
        cached = self._cache_read(url, "raw", 24 * 3600)
        if cached is not None:
            if cached.get("error"):
                raise FetchError(url, cached["error"]["kind"], "cached answer")
            return cached["body"]
        self._pace(urlparse(url).netloc.lower())
        try:
            body = self._http_get(url, "raw")
        except FetchError as e:
            self._cache_error(url, "raw", e)
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

    def prune_cache(self, max_age_days: float = 10) -> None:
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
            lo, hi = self.host_delay.get(host, self.delay)
            wait = last + random.uniform(lo, hi) - time.time()
            if wait > 0:
                time.sleep(wait)
        self._last_hit[host] = time.time()

    def _http_get(self, url: str, kind: str) -> str:
        try:
            r = requests.get(url, headers={"User-Agent": FEED_UA, "Accept-Language": "en-US,en;q=0.9"}, timeout=30)
        except requests.Timeout as e:
            raise FetchError(url, "timeout") from e
        except requests.RequestException as e:
            raise FetchError(url, "network", type(e).__name__) from e
        if r.status_code != 200:
            raise _status_error(url, r.status_code, _parse_retry_after(r.headers.get("Retry-After")))
        data = r.content
        if data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        body = data.decode(r.encoding or "utf-8", errors="replace")
        if kind == "html" and _looks_blocked(body):
            raise FetchError(url, "bot_check", "CAPTCHA / bot check page")
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
                self._page.wait_for_load_state("networkidle", timeout=6000)
            except Exception:
                pass  # some pages never go fully idle; what has loaded is enough
            status = resp.status if resp else 0
            retry_after = _parse_retry_after((resp.headers or {}).get("retry-after")) if resp else None
            title = (self._page.title() or "").lower()
            html = self._page.content()
        except Exception as e:
            kind = "timeout" if "Timeout" in type(e).__name__ else "network"
            raise FetchError(url, kind, f"browser: {type(e).__name__}") from e
        if any(t in title for t in _BLOCK_TITLES) or _looks_blocked(html):
            raise FetchError(url, "bot_check", "CAPTCHA / bot check page")
        if status >= 400:
            raise _status_error(url, status, retry_after)
        return html

    def _cache_path(self, url: str, kind: str) -> str:
        return os.path.join(self.cache_dir, hashlib.sha1(f"{kind}:{url}".encode()).hexdigest() + ".json")

    def _cache_read(self, url: str, kind: str, ttl: float) -> Optional[dict]:
        try:
            with open(self._cache_path(url, kind)) as f:
                entry = json.load(f)
        except (OSError, ValueError):
            return None
        if entry.get("error"):
            ttl = entry.get("ttl", ttl)
        return entry if time.time() - entry.get("at", 0) < ttl else None

    def _cache_write(self, url: str, kind: str, body: str = "", error: Optional[dict] = None, ttl: Optional[float] = None) -> None:
        with open(self._cache_path(url, kind), "w") as f:
            json.dump({"url": url, "at": time.time(), "body": body, "error": error, "ttl": ttl}, f)

    def _cache_error(self, url: str, kind: str, e: FetchError) -> None:
        if e.category == "temporary":
            return  # next run should try again
        ttl = self.restricted_ttl if e.category == "restricted" else 86400
        self._cache_write(url, kind, error={"kind": e.kind, "detail": e.detail}, ttl=ttl)


def _looks_blocked(body: str) -> bool:
    head = body[:6000].lower()
    if any(f"<title>{t}" in head for t in _BLOCK_TITLES):
        return True
    # a CAPTCHA widget on a short page = challenge page (product pages are long)
    return bool(_CAPTCHA_MARKERS.search(body[:20000])) and len(body) < 60000


def _playwright_installed() -> bool:
    try:
        import playwright  # noqa: F401
        return True
    except ImportError:
        return False


def from_config(cfg: dict, cache_dir: str) -> Fetcher:
    f = cfg.get("fetch") or {}
    fx = Fetcher(
        cache_dir=cache_dir,
        cache_hours=f.get("cache_hours", 5),
        min_delay=f.get("min_delay_seconds", 8),
        max_delay=f.get("max_delay_seconds", 20),
        use_browser=f.get("browser", True),
        respect_robots=f.get("respect_robots_txt", True),
        max_retries=f.get("max_retries", 2),
        backoff_seconds=f.get("retry_backoff_seconds", 15),
        max_retry_wait=f.get("max_retry_wait_seconds", 120),
        restricted_recheck_days=f.get("restricted_recheck_days", 7),
    )
    # per-store pacing: `delay_seconds: [min, max]` on a retailer/aggregator
    for src in [*cfg.get("retailers", []), *cfg.get("aggregators", [])]:
        d = src.get("delay_seconds")
        if not d:
            continue
        hosts = {urlparse(u).netloc.lower() for urls in (src.get("pages") or {}).values() for u in urls}
        if src.get("domain"):
            hosts |= {src["domain"], "www." + src["domain"]}
        if src.get("base_url"):
            hosts.add(urlparse(src["base_url"]).netloc.lower())
        for h in hosts:
            fx.host_delay[h] = (float(d[0]), float(d[1]))
    return fx
