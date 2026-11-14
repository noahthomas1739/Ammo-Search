"""Command line:

  python -m ammo_finder check      # scrape, text GREAT/EXCEPTIONAL now, queue GOOD for digest
  python -m ammo_finder digest     # send the queued GOOD deals (only if any)
  python -m ammo_finder monthly    # last month vs the month before, with threshold suggestions
  python -m ammo_finder doctor     # show how many products each configured page yields
  python -m ammo_finder test-alert # send a test message on every configured channel
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date

import yaml

from . import notify
from .models import TIER_ORDER, Deal
from .report import format_alert, format_digest, monthly_report, cents
from .rules import evaluate
from .sources import collect
from .state import State

log = logging.getLogger("ammo_finder")
MAX_DEALS_PER_TEXT = 4


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run_check(cfg: dict, state: State, fetcher=None) -> dict:
    listings, pages = collect(cfg, **({"fetcher": fetcher} if fetcher else {}))
    obs: dict[str, list[float]] = defaultdict(list)
    best: dict[str, Deal] = {}
    for l in listings:
        m, d = evaluate(l, cfg)
        if m is not None and m.cpr is not None:
            obs[m.spec].append(m.cpr)
        if d is not None and (d.key not in best or d.cpr < best[d.key].cpr):
            best[d.key] = d
    deals = sorted(best.values(), key=lambda d: (-TIER_ORDER[d.tier], d.cpr))

    state.record_prices(obs)
    ok_pages = sum(1 for p in pages.values() if p["ok"] and p["count"] > 0)
    state.record_health(ok_pages > 0)

    alerts_cfg = cfg["alerts"]
    urgent = [d for d in deals if d.tier in ("GREAT", "EXCEPTIONAL") and state.should_alert(d, alerts_cfg)]
    sent_urgent = 0
    for i in range(0, min(len(urgent), MAX_DEALS_PER_TEXT * 3), MAX_DEALS_PER_TEXT):
        batch = urgent[i : i + MAX_DEALS_PER_TEXT]
        title, body = format_alert(batch)
        notify.send(title, body, urgent=True)
        for d in batch:
            state.mark_alerted(d)
        sent_urgent += len(batch)

    queued = sum(1 for d in deals if d.tier == "GOOD" and state.queue_digest(d, alerts_cfg))

    hours = state.needs_health_warning(alerts_cfg)
    if hours:
        failing = [f"{p['source']} {p['caliber']}: {p['error'] or 'no products found'}" for p in pages.values() if not (p["ok"] and p["count"])]
        notify.send(
            "Ammo monitor: no data",
            f"No store page has returned products for {hours:.0f} hours, so deals may be missed.\n"
            "Run the 'doctor' workflow to see which pages fail.\n\n" + "\n".join(failing[:15]),
        )

    state.prune()
    summary = {
        "pages_ok": ok_pages,
        "pages_total": len(pages),
        "listings": len(listings),
        "matched": {k: len(v) for k, v in obs.items()},
        "best_price": {k: cents(min(v)) for k, v in obs.items() if v},
        "deals": len(deals),
        "texted": sent_urgent,
        "queued_for_digest": queued,
    }
    _step_summary(summary, pages, deals)
    return summary


def _step_summary(summary: dict, pages: dict, deals: list[Deal]) -> None:
    lines = ["## Ammo monitor run", "", "```", yaml.safe_dump(summary, sort_keys=False).strip(), "```", "", "| Source | Caliber | Products | Error |", "|---|---|---|---|"]
    for url, p in pages.items():
        lines.append(f"| [{p['source']}]({url}) | {p['caliber']} | {p['count']} | {p['error'][:80]} |")
    if deals:
        lines += ["", "### Qualifying deals", ""]
        lines += [f"- {d.tier} {cents(d.cpr)}/rd: {d.label} at {d.listing.retailer} ({d.listing.url})" for d in deals]
    text = "\n".join(lines)
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as f:
            f.write(text + "\n")
    log.info("summary: %s", summary)


def run_digest(cfg: dict, state: State) -> int:
    deals = state.pop_digest()
    if deals:
        title, body = format_digest(deals)
        notify.send(title, body)
    else:
        log.info("No GOOD deals since the last digest. Nothing sent.")
    return len(deals)


def run_doctor(cfg: dict, only: str | None) -> None:
    listings, pages = collect(cfg, only=only)
    by_src = defaultdict(list)
    for l in listings:
        by_src[l.source].append(l)
    for url, p in pages.items():
        status = f"{p['count']:3d} products" if p["ok"] else f"FAILED: {p['error']}"
        print(f"{p['source']:<24} {p['caliber']:<8} {status}\n    {url}")
        for l in by_src.get(url, [])[:3]:
            print(f"      - ${l.price:.2f} | {l.rounds or '?'} rds | {'in stock' if l.in_stock else 'OOS'} | {l.retailer} | {l.title[:70]}")
    _step_summary({"doctor": True, "pages_ok": sum(1 for p in pages.values() if p["count"])}, pages, [])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ammo_finder")
    ap.add_argument("command", choices=["check", "digest", "monthly", "doctor", "test-alert"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--state", default="state/state.json")
    ap.add_argument("--only", help="doctor: only sources whose name contains this")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    cfg = load_config(args.config)
    state = State(args.state)
    if args.command == "check":
        run_check(cfg, state)
        state.save()
    elif args.command == "digest":
        run_digest(cfg, state)
        state.save()
    elif args.command == "monthly":
        title, body = monthly_report(state.data["history"], cfg, date.today())
        notify.send(title, body)
    elif args.command == "doctor":
        run_doctor(cfg, args.only)
    elif args.command == "test-alert":
        used = notify.send("Ammo monitor test", "Test alert from your ammo deal monitor. If you got this, alerts work.", urgent=True)
        print("Channels:", used or "none configured")
    return 0


if __name__ == "__main__":
    sys.exit(main())
