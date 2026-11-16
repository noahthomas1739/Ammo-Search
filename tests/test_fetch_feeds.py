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


def test_round_count_from_product_page(cfg):
    from ammo_finder.sources import rounds_from_product_page, collect
    page = """<html><h1>CCI Blazer Brass 9mm Ammo 115 Grain FMJ</h1>
      <table><tr><td>Rounds Per Box:</td><td>50</td></tr></table>
      <div class="related">Magtech 9mm 1000 Rounds $299</div></html>"""
    assert rounds_from_product_page(page) == 50
    listing_page = """<html><li class="product-item"><a href="https://www.targetsportsusa.com/blazer-p-1.aspx">CCI Blazer Brass 9mm Ammo 115 Grain Full Metal Jacket</a>
      <span class="price">$12.00</span></li>
      <li class="product-item"><a href="https://www.targetsportsusa.com/hst-p-2.aspx">Federal HST 9mm 124 Grain JHP</a><span class="price">$30.00</span></li></html>"""
    docs = {"https://www.targetsportsusa.com/9mm-luger-ammo-c-51.aspx": listing_page,
            "https://www.targetsportsusa.com/blazer-p-1.aspx": page}
    asked = []
    def fetch(url, kind="html", ttl=None):
        asked.append(url)
        return docs[url]
    cfg = dict(cfg)
    cfg["aggregators"] = []
    cfg["retailers"] = [dict(r, sitemaps=None, pages=({"9mm": ["https://www.targetsportsusa.com/9mm-luger-ammo-c-51.aspx"]} if r["name"] == "Target Sports USA" else {})) for r in cfg["retailers"]]
    ls, _ = collect(cfg, fetch)
    blazer = [l for l in ls if "blazer" in l.url][0]
    assert blazer.rounds == 50
    assert "https://www.targetsportsusa.com/hst-p-2.aspx" not in asked  # JHP: not worth a lookup


SHOPIFY = """{"products": [
 {"title": "Winchester Target & Practice 9mm Ammo 124 Grain Full Metal Jacket - USA9MM", "handle": "win-usa9mm",
  "body_html": "<p>Brass case</p>",
  "variants": [{"id": 1, "title": "Box of 50", "price": "14.25", "available": false},
               {"id": 2, "title": "Case of 500 (10 Boxes)", "price": "142.50", "available": true}]},
 {"title": "Federal HST 9mm 124gr JHP - 50 Rounds", "handle": "hst", "variants": [{"id": 3, "title": "Default Title", "price": "30.00", "available": true}]}
]}"""


def test_shopify_json(cfg):
    from ammo_finder.sources.shopify import parse_products_json
    ls = parse_products_json(SHOPIFY, "https://www.targetsportsusa.com", "Target Sports USA", "9mm")
    assert [(l.rounds, l.price, l.in_stock) for l in ls] == [(50, 14.25, False), (500, 142.5, True), (50, 30.0, True)]
    assert ls[1].url == "https://www.targetsportsusa.com/products/win-usa9mm?variant=2"
    assert ls[2].title.endswith("50 Rounds")  # "Default Title" dropped


def test_collect_prefers_json_over_pages(cfg):
    cfg = dict(cfg)
    cfg["aggregators"] = []
    cfg["retailers"] = [dict(r, enabled=(r["name"] == "Target Sports USA"), sitemaps=None) for r in cfg["retailers"]]
    asked = []
    def fetch(url, kind="html", ttl=None):
        asked.append(url)
        if "9mm-luger-ammo-c-51/products.json" in url:
            return SHOPIFY
        raise F._status_error(url, 404)
    ls, report = collect(cfg, fetch)
    assert not any(u.endswith(".aspx") and "9mm" in u for u in asked)  # 9mm page skipped: JSON covered it
    assert any("12-gauge-shotgun-ammo-c-747" in u and "products.json" not in u for u in asked)  # 12ga fell back to page
    from ammo_finder.rules import evaluate
    m, d = evaluate([l for l in ls if l.rounds == 500][0], cfg)
    assert m is not None and d is None  # right product; 142.50 shipped+taxed = 30.9¢, above GOOD


def test_bulkammo_style_cards(cfg):
    from ammo_finder.sources.extract import extract
    html = """<ul class="products-grid">
     <li class="item first"><a href="/250-rounds-federal-1-1-8">250 Rounds of 12ga Ammo by Federal - 1 1/8 ounce #7 1/2 shot</a>
       <div class="price-box"><p class="old-price">Regular Price: <span class="price">$94</span></p>
       <p class="special-price">On Sale: <span class="price">$90</span></p></div>102 Ready to Ship
       <a href="/250-rounds-federal-1-1-8">Buy this 250 Rounds of Bulk 12ga Ammo by Federal</a></li>
     <li class="item"><a href="/1000-rounds-9mm-pmc">1000 Rounds of 9mm Ammo by PMC - 115gr FMJ</a><span class="price">$259</span>5 Ready to Ship</li>
    </ul>"""
    ls = extract(html, "https://www.bulkammo.com/shotgun/bulk-12ga-ammo", "Bulk Ammo", "12ga", cfg["retailers"], [])
    got = sorted((l.price, l.rounds, l.stock_qty) for l in ls)
    assert got == [(90.0, 250, 102), (259.0, 1000, 5)]


def test_sitemap_rotation(cfg, tmp_path):
    from ammo_finder.sources import _from_sitemap
    urls = [f"https://www.rogerssportinggoods.com/7-62x54r-fmj-{i}" for i in range(5)]
    sm_xml = "<urlset>" + "".join(f"<url><loc>{u}</loc></url>" for u in urls) + "</urlset>"
    class Fx:
        cache_dir = str(tmp_path)
        last_from_cache = False
    seen = []
    def fetch(url, kind="html", ttl=None):
        if url.endswith("sitemap.xml"):
            return sm_xml
        seen.append(url)
        return "<html></html>"
    src = {"name": "Rogers Sporting Goods"}
    for _ in range(3):
        _from_sitemap(cfg, src, "762x54r", ["https://x/sitemap.xml"], {"max_products": 2}, fetch, Fx(), [], lambda *a, **k: None)
    assert seen == urls[:5] + urls[:1]  # 0,1 then 2,3 then 4,0
