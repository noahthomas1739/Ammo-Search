"""Message formatting and the monthly trend report."""
from __future__ import annotations

from datetime import date
from typing import Optional

from .models import Deal

ICON = {"EXCEPTIONAL": "🚨", "GREAT": "🔥", "GOOD": "✅"}
SPEC_NAMES = {
    "9mm": "9mm range ammo",
    "12ga_a": "12ga Priority A (1-1/4 oz HV)",
    "12ga_b": "12ga Priority B (1-1/8 oz 1250+ fps)",
    "762x54r": "7.62x54R",
}
SPEC_TIERS = {"9mm": "9mm", "12ga_a": "12ga_a", "12ga_b": "12ga_b", "762x54r": "762x54r"}


def cents(v: float) -> str:
    return f"{v * 100:.1f}¢"


def format_deal(d: Deal) -> str:
    l = d.listing
    stock = f"{l.stock_qty} available" if l.stock_qty is not None else "in stock (qty not shown)"
    ship = "free" if d.shipping == 0 else f"${d.shipping:.2f}"
    if d.shipping_estimated:
        ship += " est."
    lines = [
        f"{'⚠️ ' if d.suspicious else ''}{ICON[d.tier]} {d.tier}: {d.label}",
        l.title[:140],
        f"Retailer: {l.retailer}",
        f"{cents(d.cpr)}/rd delivered",
        f"Total ${d.total:.2f} for {d.total_rounds} rds (ship {ship}, tax ${d.tax:.2f})",
        f"Stock: {stock}",
    ]
    if d.flags:
        lines.append(" | ".join(d.flags))
    lines += [f"• {n}" for n in d.notes]
    lines.append(l.url)
    return "\n".join(lines)


def format_alert(deals: list[Deal]) -> tuple[str, str]:
    top = deals[0]
    title = f"{top.tier} ammo deal: {top.label} {cents(top.cpr)}/rd"
    if len(deals) > 1:
        title = f"{len(deals)} hot ammo deals ({top.tier} {cents(top.cpr)}/rd)"
    return title, "\n\n".join(format_deal(d) for d in deals)


def format_digest(deals: list[Deal]) -> tuple[str, str]:
    title = f"Ammo daily digest: {len(deals)} GOOD deal{'s' if len(deals) != 1 else ''}"
    return title, "\n\n".join(format_deal(d) for d in deals)


# ------------------------------------------------------------------ monthly
def _month_key(d: date) -> str:
    return d.strftime("%Y-%m")


def _prev_month(ym: str) -> str:
    y, m = map(int, ym.split("-"))
    return f"{y - 1}-12" if m == 1 else f"{y}-{m - 1:02d}"


def _round_half_cent(v: float) -> float:
    return round(v * 200) / 200


def month_stats(hist: dict, ym: str) -> Optional[dict]:
    days = [v for d, v in hist.items() if d.startswith(ym)]
    if not days:
        return None
    mins = [v["min"] for v in days]
    return {"avg_low": sum(mins) / len(mins), "lowest": min(mins), "days": len(days)}


def monthly_report(history: dict, cfg: dict, today: date) -> tuple[str, str]:
    """Compares last full month against the month before it."""
    first = today.replace(day=1)
    last_month = _prev_month(_month_key(first))
    before = _prev_month(last_month)
    lines = [f"Monthly ammo price report: {last_month} vs {before}", "(avg of each day's lowest delivered price that fit your specs)", ""]
    for spec, name in SPEC_NAMES.items():
        hist = history.get(spec, {})
        cur, prev = month_stats(hist, last_month), month_stats(hist, before)
        tcfg = cfg["categories"][SPEC_TIERS[spec]]
        if not cur:
            lines.append(f"{name}: no data last month.")
            lines.append("")
            continue
        s = f"{name}: avg low {cents(cur['avg_low'])}/rd, lowest {cents(cur['lowest'])} ({cur['days']} days of data)"
        if prev:
            chg = (cur["avg_low"] - prev["avg_low"]) / prev["avg_low"]
            trend = "UP" if chg > 0.03 else "DOWN" if chg < -0.03 else "FLAT"
            s += f". {trend} {chg * 100:+.1f}% vs {cents(prev['avg_low'])}"
            lines.append(s)
            if abs(chg) >= 0.05:
                ratio = cur["avg_low"] / prev["avg_low"]
                sug = ", ".join(
                    f"{t.upper()} {cents(tcfg[t])}→{cents(_round_half_cent(tcfg[t] * ratio))}"
                    for t in ("good", "great", "exceptional")
                    if tcfg.get(t) is not None
                )
                lines.append(f"  Market moved {chg * 100:+.0f}%. Consider: {sug}")
        else:
            lines.append(s + ". No prior month to compare.")
        good = tcfg.get("good")
        if good and cur["lowest"] > good:
            lines.append(f"  Never reached GOOD ({cents(good)}) last month. If you need ammo, consider raising thresholds.")
        elif tcfg.get("great") and cur["avg_low"] <= tcfg["great"]:
            lines.append(f"  Prices sat at or below GREAT most of the month. You could tighten thresholds.")
        lines.append("")
    return f"Ammo monthly report {last_month}", "\n".join(lines).strip()
