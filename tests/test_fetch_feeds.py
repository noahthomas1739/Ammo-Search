import time

import pytest

from ammo_finder import fetch as F
from ammo_finder.fetch import Fetcher, FetchError
from ammo_finder.sources import collect
from ammo_finder.sources.feeds import parse_feed, parse_sitemap, sitemap_product_urls


@pytest.fixture
def fx(tmp_path, monkeypatch):
    f = Fetcher(cache_dir=str(tmp_path), min_delay=0, max_delay=0, use_browser=False)
    calls = []
    pages = {
        "https://shop.test/robots.txt": "User-agent: *\nDisallow: /search\nSitemap: https://shop.test/sitemap.xml\n",
        "https://shop.test/a": "<html>ok</html>",
        "https://blocked.test/robots.txt": "",
    }

    def fake(url, kind):
        calls.append(url)
        if url.startswith("https://blocked.test/p"):
            raise FetchError(f"{url}: HTTP 403")
        if url not in pages:
            raise FetchError(f"{url}: HTTP 404")
        return pages[url]

    monkeypatch.setattr(f, "_http_get", fake)
    return f, calls


def test_cache_robots_and_block(fx):
    f, calls = fx
    assert f.get("https://shop.test/a") == "<html>ok</html>"
    assert f.get("https://shop.test/a") == "<html>ok</html>"
    assert calls.count("https://shop.test/a") == 1 and f.last_from_cache  # second read from cache
    with pytest.raises(FetchError, match="robots.txt"):
        f.get("https://shop.test/search?q=9mm")
    assert "https://shop.test/search?q=9mm" not in calls  # never requested
    with pytest.raises(FetchError, match="403"):
        f.get("https://blocked.test/p1")
    with pytest.raises(FetchError, match="skipped"):
        f.get("https://blocked.test/p2")  # host blocked for the rest of the run
    assert "https://blocked.test/p2" not in calls


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
