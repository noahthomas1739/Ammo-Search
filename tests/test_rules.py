from ammo_finder.models import Listing
from ammo_finder.rules import evaluate
from ammo_finder.sources.extract import extract
from conftest import fixture


def L(title, price, retailer="LuckyGunner", url="https://www.luckygunner.com/x", **kw):
    return Listing(title=title, retailer=retailer, url=url, price=price, **kw)


def test_9mm_tiers(cfg):
    # $180 + $14.99 ship, *1.0825 tax = $211.08 -> 21.1¢ = GREAT (EXCEPTIONAL is <= 21¢)
    m, d = evaluate(L("Magtech 9mm 115gr FMJ Brass 1000 Rounds", 180.0), cfg)
    assert d.tier == "GREAT" and abs(d.cpr - 0.2111) < 0.0005
    assert d.shipping == 14.99 and d.shipping_estimated
    m, d = evaluate(L("Magtech 9mm 115gr FMJ Brass 1000 Rounds", 175.0), cfg)
    assert d.tier == "EXCEPTIONAL" and any("2,000" in n for n in d.notes)
    m, d = evaluate(L("Magtech 9mm 115gr FMJ Brass 1000 Rounds", 240.0), cfg)
    assert d is None and m is not None  # right product, too expensive -> still recorded for trend


def test_shipping_wrecks_cheap_box(cfg):
    # $10.50 per 50 looks like 21¢, but 10 boxes + shipping + tax = 26¢: no deal
    m, d = evaluate(L("Federal 9mm 115gr FMJ brass 50 Rounds", 10.50), cfg)
    assert d is None and abs(m.cpr - 0.2598) < 0.0005
    m, d = evaluate(L("Federal 9mm 115gr FMJ brass 50 Rounds", 10.00), cfg)
    assert d.tier == "GOOD" and d.units == 10 and d.total_rounds == 500


def test_free_shipping_threshold_bundles(cfg):
    # Brownells: free over $199 (config). 1000rd at $190 -> buy... still 1 unit (cheaper per round with shipping?)
    m, d = evaluate(L("Brownells 9mm 124gr FMJ brass 1000 rounds", 190.0, retailer="Brownells", url="https://www.brownells.com/x"), cfg)
    # one case: (190+14.99)*1.0825/1000 = 22.19¢; two cases: 380*1.0825/2000 = 20.57¢
    assert d.units == 2 and d.tier == "EXCEPTIONAL" and d.shipping == 0


def test_9mm_rejects(cfg):
    for t in ["Tula 9mm 115gr FMJ steel case 1000 rounds", "Hornady 9mm 115gr JHP 1000 rounds",
              "9mm 147gr FMJ brass 1000 rounds", "9mm 115gr FMJ brass 1000 rounds reloaded"]:
        assert evaluate(L(t, 100.0), cfg) == (None, None), t
    assert evaluate(L("9mm 115gr FMJ brass 1000 rounds", 100.0, in_stock=False), cfg) == (None, None)
    assert evaluate(L("9mm 115gr FMJ brass 1000 rounds", 100.0, marketplace=True), cfg) == (None, None)


def test_suspicious_low(cfg):
    m, d = evaluate(L("9mm 115gr FMJ brass 1000 rounds", 100.0), cfg)
    assert d.suspicious and d.notes[0].startswith("VERIFY")


def test_12ga_priority_a_and_b(cfg):
    m, d = evaluate(L('Fiocchi 12 Gauge 2-3/4" 1-1/4 oz #7.5 1330 fps 250 rounds', 119.99), cfg)
    # (119.99+14.99)*1.0825/250 = 58.4¢ -> just over GOOD
    assert d is None and m.spec == "12ga_a"
    m, d = evaluate(L('Fiocchi 12 Gauge 2-3/4" 1-1/4 oz #7.5 1330 fps 250 rounds', 105.0), cfg)
    assert d.tier == "GOOD" and any("ONE case" in n for n in d.notes)
    m, d = evaluate(L('Fiocchi Shooting Dynamics 12ga 2-3/4" 1-1/8 oz #8 1280 fps 250 rds', 70.0), cfg)
    assert m.spec == "12ga_b" and d.tier == "GOOD"  # 36.8¢
    m, d = evaluate(L('Fiocchi Shooting Dynamics 12ga 2-3/4" 1-1/8 oz #8 1280 fps 250 rds', 65.0), cfg)
    assert d.tier == "GREAT"  # 34.6¢
    # 1-1/8 at standard 1200 fps won't cycle -> excluded
    assert evaluate(L('Winchester 12ga 2-3/4" 1-1/8 oz #8 1200 fps 250 rds', 50.0), cfg)[0] is None


def test_12ga_exclusions(cfg):
    for t in ['Winchester 12ga 3" 1-1/4 oz #6 250 rds', 'Kent 12ga 2-3/4" 1-1/4 oz #4 steel 1400 fps 250 rds',
              'Bismuth 12ga 2-3/4" 1-1/4 oz #5 250 rds', '12ga 2-3/4" 1-1/4 oz #4 1330fps 250 rds',
              '12ga 2-3/4" 00 Buckshot 250 rds', '12ga 2-3/4" 1-1/4 oz #7.5 1220 fps 250 rds']:
        assert evaluate(L(t, 60.0), cfg) == (None, None), t


def test_12ga_unknown_velocity_gets_note(cfg):
    m, d = evaluate(L('Remington 12ga 2-3/4" 1-1/4 oz #7.5 250 rds', 100.0), cfg)
    assert d is not None and any("Velocity not listed" in n for n in d.notes)


def test_54r(cfg):
    m, d = evaluate(L("PPU 7.62x54R 182gr FMJ boxer brass 20 rounds", 9.00), cfg)
    # (9+14.99)*1.0825/20 = $1.30/rd: not a deal, shipping kills small boxes
    assert d is None
    m, d = evaluate(L("Bulgarian surplus 7.62x54R FMJ 440 round sealed can", 185.0, shipping=0.0), cfg)
    # 185*1.0825 = $200.26 -> 45.5¢ = GOOD by cpr; can rule: <= $240 -> GOOD
    assert d.tier == "GOOD" and "Corrosive: likely (not stated)" in d.flags
    m, d = evaluate(L("Bulgarian surplus 7.62x54R FMJ 440 round sealed can", 184.0, shipping=0.0), cfg)
    assert d.total <= 200 and d.tier == "GREAT"
    assert evaluate(L("Wolf 7.62x54R 148gr FMJ steel case 500 rounds", 150.0), cfg)[0] is None
    assert evaluate(L("Russian surplus 7.62x54R 300 rounds loose", 90.0), cfg)[0] is None
    m, d = evaluate(L("Indian surplus 7.62x54R 440 rd can", 60.0, shipping=0.0), cfg)
    assert d.suspicious  # 14.8¢


def test_extract_cards_and_rules(cfg):
    ls = extract(fixture("retailer_cards.html"), "https://www.luckygunner.com/handgun/9mm-ammo", "LuckyGunner", "9mm", cfg["retailers"], [])
    by = {l.url.rsplit("/", 1)[-1]: l for l in ls}
    mag = by["magtech-9mm-115gr-fmj-1000rd.html"]
    assert mag.price == 199.0 and mag.rounds == 1000 and mag.stock_qty == 37 and mag.in_stock
    assert not by["fed-9mm-oos.html"].in_stock
    deals = [evaluate(l, cfg)[1] for l in ls]
    deals = [d for d in deals if d]
    assert [d.listing.url.rsplit("/", 1)[-1] for d in deals] == ["magtech-9mm-115gr-fmj-1000rd.html"]
    assert deals[0].tier == "GOOD"  # (199+14.99)*1.0825/1000 = 23.2¢


def test_extract_jsonld(cfg):
    ls = extract(fixture("jsonld.html"), "https://www.example.com/12ga", "MidwayUSA", "12ga", cfg["retailers"], [])
    assert len(ls) == 4
    fi = [l for l in ls if l.url.endswith("fiocchi-12ga")][0]
    assert fi.price == 119.99 and fi.stock_qty == 12 and fi.rounds == 250
    assert not [l for l in ls if l.url.endswith("fiocchi-sd")][0].in_stock


def test_extract_aggregator_table(cfg):
    ls = extract(fixture("aggregator_table.html"), "https://ammoseek.com/ammo/7.62x54mmr", "AmmoSeek", "762x54r",
                 cfg["retailers"], cfg["marketplace_sellers"], aggregator=True)
    assert len(ls) == 4
    bul = ls[0]
    assert bul.retailer == "Target Sports USA" and bul.rounds == 440 and bul.shipping == 0.0 and bul.price == 189.0
    assert ls[1].marketplace
    m, d = evaluate(bul, cfg)
    assert d.tier == "GOOD" and "Corrosive: YES" in d.flags and "Steel core: YES" in d.flags and "Berdan: YES" in d.flags
    assert evaluate(ls[1], cfg) == (None, None)
    m, d = evaluate(ls[3], cfg)
    assert d.suspicious and d.tier == "EXCEPTIONAL"


def test_retailer_matching(cfg):
    from ammo_finder.rules import find_retailer
    assert find_retailer("BulkAmmo.com", "https://ammoseek.com/go.to/1", cfg["retailers"])["name"] == "Bulk Ammo"
    assert find_retailer("Ammo.com", "https://ammoseek.com/go.to/1", cfg["retailers"])["name"] == "Ammo.com"
    assert find_retailer("x", "https://www.bulkammo.com/p", cfg["retailers"])["name"] == "Bulk Ammo"
    assert find_retailer("Joe's Guns", "https://ammoseek.com/go.to/1", cfg["retailers"]) is None
