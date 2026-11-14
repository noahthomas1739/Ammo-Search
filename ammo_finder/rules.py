"""Decide whether a listing matches one of the three shopping lists, and how good the deal is."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlparse

from .models import Deal, Listing
from .parse import Attrs, parse
from .pricing import plan_purchase

_BAD_STOCK = re.compile(
    r"out of stock|sold out|backorder|back-order|back order|pre-?order|notify me|coming soon|unavailable|"
    r"cannot ship to (tx|texas)|not available in (tx|texas)|special order",
    re.I,
)


@dataclass
class SpecMatch:
    category: str  # 9mm / 12ga / 762x54r
    spec: str  # config key: 9mm / 12ga_a / 12ga_b / 762x54r
    label: str
    unit_rounds: int
    flags: list = field(default_factory=list)
    notes: list = field(default_factory=list)
    cpr: Optional[float] = None  # delivered cost per round, set by evaluate()


def find_retailer(name: str, url: str, retailers: list[dict]) -> Optional[dict]:
    n = (name or "").lower()
    host = urlparse(url or "").netloc.lower().split(":")[0]
    for r in retailers:
        d = (r.get("domain") or "").lower()
        if d and (host == d or host.endswith("." + d)):
            return r
    pairs = [(a.lower(), r) for r in retailers for a in [r["name"], *r.get("aliases", [])]]
    for alias, r in sorted(pairs, key=lambda p: len(p[0]), reverse=True):
        if re.search(r"(?<![a-z])" + re.escape(alias) + r"(?![a-z])", n):
            return r
    return None


def _yn(v: Optional[bool], likely: bool = False) -> str:
    if v is True:
        return "YES"
    if v is False:
        return "no"
    return "likely (not stated)" if likely else "not stated"


def match_spec(listing: Listing, a: Attrs, cfg: dict) -> Optional[SpecMatch]:
    cats = cfg["categories"]
    unit_rounds = listing.rounds or a.rounds
    if not unit_rounds:
        return None

    # ---------------- 9mm ----------------
    if a.caliber == "9mm" and cats["9mm"].get("enabled", True):
        c = cats["9mm"]
        if a.case not in ("brass", None) or a.expanding or not a.fmj:
            return None
        if a.grain not in c["grains"]:
            return None
        m = SpecMatch("9mm", "9mm", f"9mm {a.grain}gr FMJ brass", unit_rounds)
        if a.case is None:
            m.notes.append("Case material not listed. Make sure it's brass.")
        return m

    # ---------------- 12 gauge ----------------
    if a.caliber == "12ga":
        if a.not_birdshot or a.shot_material in ("steel", "bismuth", "tungsten"):
            return None
        if a.shell_len is not None and a.shell_len != 2.75:
            return None
        text = f"{listing.title} {listing.extra_text}"
        if a.shell_len is None and re.search(r"magnum", text, re.I):
            return None
        notes = []
        if a.shell_len is None:
            notes.append('Shell length not listed. Make sure it\'s 2-3/4".')
        if not cats.get("a5_cycling_confirmed", False):
            notes.append("Buy ONE case only until you confirm it cycles your A5.")

        ca, cb = cats["12ga_a"], cats["12ga_b"]
        if ca.get("enabled", True) and a.shot_oz is not None and abs(a.shot_oz - ca["shot_oz"]) < 0.01:
            if a.shot_sizes and not (a.shot_sizes & set(map(str, ca["shot_sizes"]))):
                return None
            if a.velocity is not None and a.velocity < ca["min_velocity"]:
                return None
            if a.velocity is None and not ca.get("allow_unknown_velocity", True):
                return None
            n = list(notes)
            if a.velocity is None:
                n.insert(0, f"Velocity not listed. Make sure it's ~{ca['min_velocity']}+ fps.")
            if not a.shot_sizes:
                n.insert(0, "Shot size not listed. Make sure it's #6, #7.5, #8 or #9.")
            size = "/".join(sorted(a.shot_sizes)) or "?"
            vel = f" {a.velocity}fps" if a.velocity else ""
            return SpecMatch("12ga", "12ga_a", f"12ga Priority A: 1-1/4oz #{size}{vel}", unit_rounds, [], n)
        if cb.get("enabled", True) and a.shot_oz is not None and abs(a.shot_oz - cb["shot_oz"]) < 0.01:
            if a.velocity is None or a.velocity < cb["min_velocity"]:
                return None
            n = ["Test load: 1-1/8 oz may or may not cycle your A5.", *notes]
            return SpecMatch("12ga", "12ga_b", f"12ga Priority B (test): 1-1/8oz {a.velocity}fps", unit_rounds, [], n)
        return None

    # ---------------- 7.62x54R ----------------
    if a.caliber == "762x54r" and cats["762x54r"].get("enabled", True):
        if a.expanding:
            return None
        notes = []
        if a.surplus:
            if unit_rounds % 440 != 0 or unit_rounds > 880:
                return None  # only sealed 440 / 880 round cans
            kind = f"surplus {unit_rounds}rd can"
            likely = True
        else:
            if a.case == "steel" or a.case == "aluminum":
                return None
            if not a.fmj:
                return None
            if a.case is None:
                notes.append("Case material not listed. Make sure it's brass, not steel.")
            kind = "new-mfg brass FMJ"
            likely = False
        flags = [
            f"Corrosive: {_yn(a.corrosive, likely)}",
            f"Steel core: {_yn(a.steel_core, likely)}",
            f"Berdan: {_yn(a.berdan, likely)}",
        ]
        return SpecMatch("762x54r", "762x54r", f"7.62x54R {kind}", unit_rounds, flags, notes)
    return None


def tier_for(cpr: float, spec_cfg: dict) -> Optional[str]:
    for t in ("exceptional", "great", "good"):
        lim = spec_cfg.get(t)
        if lim is not None and cpr <= lim + 1e-9:
            return t.upper()
    return None


def evaluate(listing: Listing, cfg: dict) -> tuple[Optional[SpecMatch], Optional[Deal]]:
    """Returns (spec match or None, deal or None). A spec match without a deal
    means the product is the right kind but too expensive. Those still feed the
    monthly price trend."""
    text = f"{listing.title} {listing.extra_text}"
    a = parse(text, listing.caliber_hint)
    if not listing.in_stock or listing.marketplace or _BAD_STOCK.search(listing.title):
        return None, None
    if a.reloaded or a.not_ammo or a.used:
        return None, None

    retailers = cfg["retailers"]
    r = find_retailer(listing.retailer, listing.url, retailers)
    if r is None:
        if any(m.lower() in (listing.retailer or "").lower() for m in cfg.get("marketplace_sellers", [])):
            return None, None
        if not cfg.get("allow_unlisted_retailers", True):
            return None, None
    elif not r.get("ships_to_tx", True):
        return None, None

    m = match_spec(listing, a, cfg)
    if m is None:
        return None, None

    loc = cfg["location"]
    spec_cfg = cfg["categories"][m.spec]
    ship_rule = (r or {}).get("shipping") or cfg["default_shipping"]
    plan = plan_purchase(
        listing,
        m.unit_rounds,
        m.category,
        spec_cfg.get("min_rounds", 0),
        ship_rule,
        loc["sales_tax_rate"],
        loc.get("tax_shipping", True),
        (r or {}).get("collects_tax", True),
        cfg["purchase"]["max_order_total"],
    )
    if plan is None:
        return m, None
    m.cpr = plan.cpr

    tier = tier_for(plan.cpr, spec_cfg)
    notes = list(m.notes)
    # 440-round can rule for 7.62x54R
    if m.category == "762x54r" and m.unit_rounds == 440 and plan.units == 1:
        can_tier = None
        if plan.total <= spec_cfg["can_440_great_total"]:
            can_tier = "GREAT"
        elif plan.total <= spec_cfg["can_440_good_total"]:
            can_tier = "GOOD"
        if can_tier and (tier is None or _rank(can_tier) > _rank(tier)):
            tier = can_tier
            notes.append(f"440rd can at ${plan.total:.2f} delivered.")
    if tier is None:
        return m, None

    if tier == "EXCEPTIONAL" and m.category == "9mm":
        notes.append("EXCEPTIONAL: stock up, buy 2,000+ rounds.")
    if plan.units > 1:
        notes.append(f"Price assumes buying {plan.units} x {m.unit_rounds}rd.")
    if plan.shipping_estimated:
        notes.append("Shipping is an estimate. Check at checkout.")
    if r is None:
        notes.append(f"Retailer '{listing.retailer}' isn't on your list. Check that it isn't a marketplace seller.")

    suspicious = plan.cpr < spec_cfg.get("suspicious_below", 0)
    if suspicious:
        notes.insert(0, f"VERIFY: {plan.cpr*100:.1f}¢/rd is suspiciously low. Could be a listing error, a wrong round count, or a scam.")

    deal = Deal(
        listing=listing,
        category=m.category,
        label=m.label,
        tier=tier,
        cpr=round(plan.cpr, 4),
        total=plan.total,
        units=plan.units,
        total_rounds=plan.rounds,
        shipping=plan.shipping,
        tax=plan.tax,
        shipping_estimated=plan.shipping_estimated,
        flags=m.flags,
        notes=notes,
        suspicious=suspicious,
    )
    if r is not None:
        deal.listing.retailer = r["name"]
    return m, deal


def _rank(t: str) -> int:
    return {"GOOD": 1, "GREAT": 2, "EXCEPTIONAL": 3}[t]
