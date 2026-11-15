"""Polite HTTP fetching. Pages that answer with a bot check are treated as blocked and skipped."""
from __future__ import annotations

import random
import time
from urllib.parse import urlparse

import requests as http

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

_last_hit: dict[str, float] = {}


class FetchError(Exception):
    pass


def get(url: str, timeout: int = 30, retries: int = 2, min_gap: float = 3.0) -> str:
    host = urlparse(url).netloc
    wait = _last_hit.get(host, 0) + min_gap + random.uniform(0, 2) - time.time()
    if wait > 0:
        time.sleep(wait)
    err: Exception | None = None
    for attempt in range(retries + 1):
        try:
            r = http.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
            _last_hit[host] = time.time()
            if r.status_code == 200 and r.text:
                body = r.text
                if "cf-chl" in body[:5000] or "Just a moment..." in body[:3000]:
                    raise FetchError(f"{url}: blocked (site shows a bot check)")
                return body
            raise FetchError(f"{url}: HTTP {r.status_code}")
        except Exception as e:  # network error, HTTP error, bot wall
            err = e
            if "blocked" in str(e) or "HTTP 403" in str(e) or "HTTP 404" in str(e):
                break  # a refusal won't change on retry; respect it
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    raise FetchError(str(err))
