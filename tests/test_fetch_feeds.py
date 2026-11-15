import time

import pytest

from ammo_finder import fetch as F
from ammo_finder.fetch import Fetcher, FetchError
from ammo_finder.sources import collect
from ammo_finder.sources.feeds import parse_feed, parse_sitemap, sitemap_product_urls


@pytest.fixture
def fx(tmp_path, monkeypatch):
    f = Fetcher(cache_dir=str(tmp_path), min_delay=0, max_delay=0, use_browser=False, backoff_seconds=1)
    calls, slept = [], []
    monkeypatch.setattr(F.time, "sleep", lambda s: slept.append(s))
    pages = {
        "https://shop.test/robots.txt": "User-agent: *\nDisallow: /search\nSitemap: https://shop.test/sitemap.xml\n",
        "https://shop.test/a": "<html>ok</html>",
    }
    script = {}  # url -> list of errors to raise before succeeding

    def fake(url, kind):
        calls.append(url)
        if script.get(url):
            raise script[url].pop(0)
        if url.startswith("https://blocked.test/p"):
            raise F._status_error(url, 403)
        if url == "https://walled.test/robots.txt":
            raise F._status_error(url, 403)
        if url not in pages:
            raise F._status_error(url, 404)
        return pages[url]

    monkeypatch.setattr(f, "_http_get", fake)
    return f, calls, slept, pages, script


def test_cache_robots_and_restricted(fx):
    f, calls, slept, pages, script = fx
    assert f.get("https://shop.test/a") == "<html>ok</html>"
    assert f.get("https://shop.test/a") == "<html>ok</html>"
    assert calls.count("https://shop.test/a") == 1 and f.last_from_cache
    with pytest.raises(FetchError) as e:
        f.get("https://shop.test/search?q=9mm")
    assert e.value.kind == "robots_disallowed" and e.value.category == "restricted"
    assert "https://shop.test/search?q=9mm" not in calls  # never requested
    with pytest.raises(FetchError) as e:
        f.get("https://blocked.test/p1")
    assert e.value.kind == "forbidden" and e.value.category == "restricted"
    assert calls.count("https://blocked.test/p1") == 1  # restricted: not retried
    with pytest.raises(FetchError, match="earlier this run"):
        f.get("https://blocked.test/p2")
    assert "https://blocked.test/p2" not in calls
    # robots.txt itself refused -> treat the whole site as off limits
    with pytest.raises(FetchError) as e:
        f.get("https://walled.test/x")
    assert e.value.kind == "robots_disallowed" and "https://walled.test/x" not in calls


def test_temporary_errors_retry_with_backoff(fx):
    f, calls, slept, pages, script = fx
    pages["https://shop.test/b"] = "fine"
    script["https://shop.test/b"] = [F.FetchError("https://shop.test/b", "timeout"), F._status_error("https://shop.test/b", 503)]
    assert f.get("https://shop.test/b") == "fine"
    assert calls.count("https://shop.test/b") == 3 and len(slept) == 2 and slept[1] > slept[0]


def test_rate_limit_honors_retry_after(fx):
    f, calls, slept, pages, script = fx
    pages["https://shop.test/c"] = "ok"
    script["https://shop.test/c"] = [F._status_error("https://shop.test/c", 429, retry_after=30)]
    assert f.get("https://shop.test/c") == "ok" and 30 in slept
    # a Retry-After longer than we're willing to wait: give up for this run, don't cache, stop hitting the site
    script["https://shop.test/d"] = [F._status_error("https://shop.test/d", 429, retry_after=3600)]
    with pytest.raises(FetchError) as e:
        f.get("https://shop.test/d")
    assert e.value.kind == "rate_limited" and "next run" in e.value.detail
    with pytest.raises(FetchError, match="earlier this run"):
        f.get("https://shop.test/a2")
    assert f._cache_read("https://shop.test/d", "html", 999) is None  # temporary: next run tries again


def test_temporary_failure_not_cached(fx):
    f, calls, slept, pages, script = fx
    script["https://shop.test/e"] = [F.FetchError("https://shop.test/e", "network")] * 3
    with pytest.raises(FetchError):
        f.get("https://shop.test/e")
    assert f._cache_read("https://shop.test/e", "html", 999) is None


def test_bot_check_detected():
    assert F._looks_blocked("<html><head><title>Just a moment...</title>")
    assert F._looks_blocked('<html><div class="g-recaptcha"></div></html>')
    assert not F._looks_blocked("<html><title>9mm Ammo</title>" + "x" * 70000 + "g-recaptcha")


def test_pacing(tmp_path, monkeypatch):
    f = Fetcher(cache_dir=str(tmp_path), min_delay=5, max_delay=5, use_browser=False, respect_robots=False)
    slept = []
    monkeypatch.setattr(F.time, "sleep", lambda s: slept.append(s))
    monkeypatch.setattr(f, "_http_get", lambda url, kind: "x")
    f.get("https://a.test/1")
    f.get("https://a.test/2")
    f.get("https://b.test/1")  # different site: no wait
    assert len(slept) == 1 and 4 < slept[0] <= 5


RSS = """<?xml version="1.0"?><rss version="2.0"><channel>
<item><title>Magtech 9mm 115gr FMJ 1000rds - $189.99 at Target Sports USA</title><link>https://gun.deals/x/1</link>
<description>&lt;p&gt;Free shipping. Brass case.&lt;/p&gt;</description></item>
<item><title>Vortex scope sale $299</title><link>https://gun.deals/x/2</link></item>
<item><title>Bulgarian surplus 7.62x54R 440rd can $199</title><link>https://gun.deals/x/3</link><description>GunBroker seller</description></item>
</channel></rss>"""


def test_parse_feed(cfg):
    ls = parse_feed(RSS, "gun.deals", cfg["retailers"], cfg["marketplace_sellers"])
    assert len(ls) == 2
    a, b = ls
    assert (a.retailer, a.price, a.rounds, a.shipping, a.caliber_hint) == ("Target Sports USA", 189.99, 1000, 0.0, "9mm")
    assert b.marketplace and b.rounds == 440


def test_sitemaps():
    idx = """<sitemapindex xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <sitemap><loc>https://s.test/sitemap-products-1.xml</loc></sitemap></sitemapindex>"""
    prods = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://s.test/magtech-9mm-115gr-fmj-1000-rounds</loc></url>
      <url><loc>https://s.test/hornady-9mm-jhp</loc></url>
      <url><loc>https://s.test/7-62x54r-surplus-440</loc></url></urlset>"""
    assert parse_sitemap(idx) == (["https://s.test/sitemap-products-1.xml"], [])
    docs = {"https://s.test/sitemap.xml": idx, "https://s.test/sitemap-products-1.xml": prods}
    fetch = lambda url, kind, ttl: docs[url]
    assert sitemap_product_urls(fetch, ["https://s.test/sitemap.xml"], "762x54r") == ["https://s.test/7-62x54r-surplus-440"]
    from ammo_finder.sources import DEFAULT_SITEMAP_INCLUDE
    assert sitemap_product_urls(fetch, ["https://s.test/sitemap.xml"], "9mm", DEFAULT_SITEMAP_INCLUDE["9mm"]) == [
        "https://s.test/magtech-9mm-115gr-fmj-1000-rounds"
    ]


def test_collect_sitemap_product_pages(cfg):
    from conftest import fixture
    cfg = dict(cfg)
    cfg["aggregators"] = []
    cfg["retailers"] = [dict(r, pages={}, enabled=(r["name"] == "MidwayUSA")) for r in cfg["retailers"]]
    mid = next(r for r in cfg["retailers"] if r["name"] == "MidwayUSA")
    mid["sitemaps"] = {"urls": ["https://www.midwayusa.com/sitemap.xml"], "max_products": 5}
    prods = """<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">
      <url><loc>https://www.midwayusa.com/product/fiocchi-12-gauge-1-1-4-oz-7-5</loc></url></urlset>"""
    docs = {"https://www.midwayusa.com/sitemap.xml": prods,
            "https://www.midwayusa.com/product/fiocchi-12-gauge-1-1-4-oz-7-5": fixture("jsonld.html")}
    listings, report = collect(cfg, lambda url, kind="html", ttl=None: docs[url])
    assert len(listings) == 1 and listings[0].price == 119.99


def test_coverage_tracks_routes(cfg):
    from ammo_finder.sources import coverage
    cfg = dict(cfg)
    cfg["retailers"] = [dict(r, enabled=True) for r in cfg["retailers"]]
    feed_items = parse_feed(RSS, "gun.deals", cfg["retailers"], cfg["marketplace_sellers"])
    for l in feed_items:
        l.source = "https://gun.deals/rss.xml"
    report = {
        "https://gun.deals/rss.xml": {"source": "gun.deals", "caliber": "all", "via": "feed", "count": 2, "ok": True, "error": "", "error_kind": "", "category": "ok"},
        "https://www.targetsportsusa.com/a": {"source": "Target Sports USA", "caliber": "9mm", "via": "page", "count": 0, "ok": False, "error": "x", "error_kind": "forbidden", "category": "restricted"},
        "https://www.bulkammo.com/a": {"source": "Bulk Ammo", "caliber": "9mm", "via": "page", "count": 0, "ok": False, "error": "x", "error_kind": "timeout", "category": "temporary"},
        "https://www.midwayusa.com/a": {"source": "MidwayUSA", "caliber": "9mm", "via": "page", "count": 0, "ok": False, "error": "x", "error_kind": "forbidden", "category": "restricted"},
    }
    cov = coverage(cfg, feed_items, report)
    assert cov["Target Sports USA"]["status"] == "alternative"
    assert cov["Target Sports USA"]["alternatives"] == {"gun.deals": 1}
    assert cov["Bulk Ammo"]["status"] == "temporary failure"
    assert cov["MidwayUSA"]["status"] == "restricted"


def test_robots_temporary_failure_is_not_restriction(fx):
    f, calls, slept, pages, script = fx
    script["https://flaky.test/robots.txt"] = [F.FetchError("https://flaky.test/robots.txt", "timeout")] * 3
    with pytest.raises(FetchError) as e:
        f.get("https://flaky.test/x")
    assert e.value.category == "temporary" and "https://flaky.test/x" not in calls
    assert f._cache_read("https://flaky.test/x", "html", 999) is None
