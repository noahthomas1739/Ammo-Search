from datetime import date, datetime, timedelta, timezone

from ammo_finder.__main__ import run_check, run_digest
from ammo_finder.models import Listing
from ammo_finder.report import format_deal, monthly_report
from ammo_finder.rules import evaluate
from ammo_finder.state import State
from conftest import fixture

T0 = datetime(2026, 11, 1, 12, tzinfo=timezone.utc)


def deal(cfg, price):
    return evaluate(Listing("Magtech 9mm 115gr FMJ Brass 1000 Rounds", "LuckyGunner", "https://www.luckygunner.com/m", price), cfg)[1]


def test_dedupe(cfg, tmp_path):
    s = State(str(tmp_path / "s.json"))
    a = cfg["alerts"]
    d = deal(cfg, 190)
    assert s.should_alert(d, a, T0)
    s.mark_alerted(d, T0)
    assert not s.should_alert(deal(cfg, 190), a, T0 + timedelta(hours=6))
    assert s.should_alert(deal(cfg, 183), a, T0 + timedelta(hours=6))  # dropped > 0.5¢
    assert s.should_alert(deal(cfg, 175), a, T0 + timedelta(hours=6))  # tier upgrade
    assert s.should_alert(deal(cfg, 190), a, T0 + timedelta(days=8))


def test_digest_queue(cfg, tmp_path):
    s = State(str(tmp_path / "s.json"))
    a = cfg["alerts"]
    d = deal(cfg, 205)
    assert d.tier == "GOOD"
    assert s.queue_digest(d, a, T0)
    out = s.pop_digest(T0 + timedelta(hours=10))
    assert len(out) == 1 and out[0].cpr == d.cpr
    assert not s.queue_digest(deal(cfg, 205), a, T0 + timedelta(days=1))  # sent yesterday
    assert s.queue_digest(deal(cfg, 205), a, T0 + timedelta(days=4))
    s.save()
    assert State(str(tmp_path / "s.json")).data["digest_pending"]


def test_run_check_end_to_end(cfg, tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "1")
    cfg = dict(cfg)
    cfg["retailers"] = [dict(r, pages={}, enabled=True) for r in cfg["retailers"]]
    cfg["retailers"][0]["pages"] = {"9mm": ["https://www.luckygunner.com/handgun/9mm-ammo"]}
    cfg["aggregators"] = [{"name": "AmmoSeek", "pages": {"762x54r": ["https://ammoseek.com/ammo/7.62x54mmr"]}}]
    pages = {"https://www.luckygunner.com/handgun/9mm-ammo": fixture("retailer_cards.html"),
             "https://ammoseek.com/ammo/7.62x54mmr": fixture("aggregator_table.html")}
    s = State(str(tmp_path / "s.json"))
    summary = run_check(cfg, s, fetcher=pages.__getitem__)
    assert summary["pages_ok"] == 2
    assert summary["texted"] == 1  # Indian surplus can (suspicious, EXCEPTIONAL)
    assert summary["queued_for_digest"] == 2  # Magtech 9mm + Bulgarian can
    assert set(s.data["history"]) == {"9mm", "762x54r"}
    # second run: nothing new to text
    assert run_check(cfg, s, fetcher=pages.__getitem__)["texted"] == 0
    assert run_digest(cfg, s) == 2
    assert run_digest(cfg, s) == 0


def test_format_deal_has_required_fields(cfg):
    d = evaluate(Listing("Bulgarian surplus 7.62x54R FMJ corrosive 440 round sealed can", "Bulk Ammo",
                         "https://www.bulkammo.com/can", 180.0, stock_qty=5), cfg)[1]
    txt = format_deal(d)
    for needle in ["7.62x54R", "Bulk Ammo", "¢/rd", "Total $", "5 available", "Corrosive: YES", "https://www.bulkammo.com/can"]:
        assert needle in txt, needle


def test_monthly_report(cfg):
    hist = {"9mm": {}, "762x54r": {}}
    for day in range(1, 29):
        hist["9mm"][f"2026-09-{day:02d}"] = {"min": 0.27, "median": 0.30, "n": 5}
        hist["9mm"][f"2026-10-{day:02d}"] = {"min": 0.30, "median": 0.33, "n": 5}
        hist["762x54r"][f"2026-10-{day:02d}"] = {"min": 0.50, "median": 0.6, "n": 3}
    title, body = monthly_report(hist, cfg, date(2026, 11, 1))
    assert "2026-10" in title
    assert "UP +11.1%" in body and "GOOD 25.0¢→28.0¢" in body
    assert "Never reached GOOD" in body
    assert "7.62x54R: avg low 50.0¢/rd" in body and "No prior month" in body


def test_gmail_defaults(monkeypatch):
    from ammo_finder import notify
    for k in ["TWILIO_ACCOUNT_SID", "NTFY_TOPIC", "SMTP_HOST", "ALERT_EMAIL"]:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("SMTP_USER", "me@gmail.com")
    monkeypatch.setenv("SMTP_PASSWORD", "x")
    assert notify.channels() == ["email"]
    sent = {}

    class FakeSMTP:
        def __init__(self, host, port, timeout): sent["host"] = (host, port)
        def starttls(self): pass
        def login(self, u, p): sent["login"] = u
        def send_message(self, m): sent["to"] = m["To"]
        def __enter__(self): return self
        def __exit__(self, *a): pass

    monkeypatch.setattr(notify.smtplib, "SMTP", FakeSMTP)
    notify._email("t", "b")
    assert sent == {"host": ("smtp.gmail.com", 587), "login": "me@gmail.com", "to": "me@gmail.com"}
