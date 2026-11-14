"""HTTP fetching with a real-browser TLS fingerprint and polite pacing."""
from __future__ import annotations

import random
import time
from urllib.parse import urlparse

try:
    from curl_cffi import requests as http  # impersonates Chrome; gets past basic bot walls
    _IMPERSONATE = {"impersonate": "chrome"}
except ImportError:  # pragma: no cover
    import requests as http  # type: ignore
    _IMPERSONATE = {}

HEADERS = {
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
            r = http.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True, **_IMPERSONATE)
            _last_hit[host] = time.time()
            if r.status_code == 200 and r.text:
                body = r.text
                if "cf-chl" in body[:5000] or "Just a moment..." in body[:3000]:
                    raise FetchError(f"{url}: blocked by a Cloudflare challenge")
                return body
            raise FetchError(f"{url}: HTTP {r.status_code}")
        except Exception as e:  # network error, HTTP error, bot wall
            err = e
            if attempt < retries:
                time.sleep(5 * (attempt + 1))
    raise FetchError(str(err))
