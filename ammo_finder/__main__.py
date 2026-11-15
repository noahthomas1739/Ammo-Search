"""Command line:

  python -m ammo_finder check      # scrape, text GREAT/EXCEPTIONAL now, queue GOOD for digest
  python -m ammo_finder digest     # send the queued GOOD deals (only if any)
  python -m ammo_finder monthly    # last month vs the month before, with threshold suggestions
  python -m ammo_finder auto       # check + digest/monthly if due (for a Mac that isn't always on)
  python -m ammo_finder doctor     # show how many products each configured page yields
  python -m ammo_finder discover   # look for public feeds / sitemaps / JSON on each site
  python -m ammo_finder sources    # which sources work, which fail (and how), across runs
  python -m ammo_finder test-alert # send a test message on every configured channel
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
from collections import defaultdict
from datetime import date
from typing import Optional

import yaml

from . import notify
from .models import TIER_ORDER, Deal
from .report import format_alert, format_digest, monthly_report, cents
from .rules import evaluate
from .fetch import from_config
from .sources import collect, coverage
from .state import State

log = logging.getLogger("ammo_finder")
MAX_DEALS_PER_TEXT = 4


def load_config(path: str) -> dict:
    with open(path) as f:
        return yaml.safe_load(f)


def run_check(cfg: dict, state: State, fetcher) -> dict:
    listings, pages = collect(cfg, fetcher)
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
    cov = coverage(cfg, listings, pages)
    state.record_sources(pages, cov)
    # healthy = at least one real product of the right kind was read (any price).
    # Pages full of menu items or junk don't count.
    state.record_health(sum(len(v) for v in obs.values()) > 0)

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

    streak = state.needs_health_warning(alerts_cfg)
    if streak:
        lines = [f"{p['source']} {p['caliber']} ({p['via']}): {p['category']}, {p['error_kind']}" for p in pages.values() if not p["ok"]]
        notify.send(
            "Ammo monitor: couldn't read prices",
            f"The last {streak} checks couldn't read any matching products, so deals may be missed. "
            "This means the stores couldn't be checked, not that there are no deals.\n"
            "temporary = will retry automatically; restricted = the site refuses automated access "
            "(not worked around, re-checked weekly).\n\n" + "\n".join(lines[:20]),
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


def wait_for_network(timeout: int = 180) -> bool:
    """Right after the Mac wakes, Wi-Fi can take a minute to reconnect."""
    import socket
    import time
    end = time.time() + timeout
    while time.time() < end:
        try:
            socket.create_connection(("ammoseek.com", 443), timeout=5).close()
            return True
        except OSError:
            time.sleep(10)
    return False


def run_auto(cfg: dict, state: State, fetcher) -> None:
    """One command for computers that aren't always on: check now, then send the
    daily digest and monthly report if they're due."""
    if not wait_for_network():
        log.warning("No internet connection; skipping this run.")
        return
    run_check(cfg, state, fetcher)
    state.save()
    if state.digest_due():
        run_digest(cfg, state)
        state.mark_digest()
        state.save()
    if state.monthly_due():
        title, body = monthly_report(state.data["history"], cfg, date.today(), state.data.get("coverage"))
        notify.send(title, body)
        state.mark_monthly()
    state.save()


def run_doctor(cfg: dict, fetcher, only: Optional[str]) -> None:
    listings, pages = collect(cfg, fetcher, only=only)
    by_src = defaultdict(list)
    for l in listings:
        by_src[l.source].append(l)
    for url, p in pages.items():
        status = f"{p['count']:3d} products" if p["ok"] else f"FAILED: {p['error']}"
        print(f"{p['source']:<24} {p['caliber']:<8} {p.get('via', ''):<8} {status}\n    {url}")
        for l in by_src.get(url, [])[:3]:
            print(f"      - ${l.price:.2f} | {l.rounds or '?'} rds | {'in stock' if l.in_stock else 'OOS'} | {l.retailer} | {l.title[:70]}")
    print("\nCoverage this run (a store that couldn't be reached is NOT evidence of no deals):")
    for name, c in coverage(cfg, listings, pages).items():
        extra = ""
        if c["direct_routes"]:
            extra = "via " + ", ".join(c["direct_routes"])
        elif c["alternatives"]:
            extra = "via " + ", ".join(f"{k} ({v} listings)" for k, v in c["alternatives"].items())
        elif c["failures"]:
            extra = "; ".join(c["failures"][:3])
        print(f"  {name:<24} {c['status']:<20} {extra}")
    _step_summary({"doctor": True, "pages_ok": sum(1 for p in pages.values() if p["count"])}, pages, [])


def run_sources(state: State) -> None:
    """What has worked, what hasn't, and when, across all past runs."""
    print("Retailers (how each was last reached):")
    for name, c in sorted(state.data.get("coverage", {}).items()):
        print(f"  {name:<24} {c['status']:<20} last covered: {c.get('last_covered', 'never')[:16]}"
              f"  {'; '.join(c.get('failures', [])[:2])}")
    print("\nRoutes needing an alternative (failing now):")
    rows = [(k, v) for k, v in state.data.get("sources", {}).items() if v.get("status") not in ("ok",)]
    for k, v in sorted(rows, key=lambda kv: (kv[1].get("source", ""), kv[0])):
        print(f"  {v.get('source', ''):<22} {v.get('via', ''):<8} {v.get('status', ''):<11} {v.get('last_error', '')}"
              f"  fails x{v.get('fails', 0)}, last ok {v.get('last_ok', 'never')[:16]}\n      {k}")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="ammo_finder")
    ap.add_argument("command", choices=["check", "digest", "monthly", "auto", "doctor", "discover", "sources", "test-alert"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--state", default=os.environ.get("AMMO_STATE") or "state/state.json")
    ap.add_argument("--only", help="doctor: only sources whose name contains this")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    cfg = load_config(args.config)
    state = State(args.state)
    fetcher = from_config(cfg, os.path.join(os.path.dirname(os.path.abspath(args.state)), "cache"))
    try:
        return _dispatch(args, cfg, state, fetcher)
    finally:
        fetcher.close()
        fetcher.prune_cache()


def _dispatch(args, cfg: dict, state: State, fetcher) -> int:
    if args.command == "check":
        run_check(cfg, state, fetcher)
        state.save()
    elif args.command == "auto":
        run_auto(cfg, state, fetcher)
    elif args.command == "digest":
        run_digest(cfg, state)
        state.save()
    elif args.command == "monthly":
        title, body = monthly_report(state.data["history"], cfg, date.today(), state.data.get("coverage"))
        notify.send(title, body)
    elif args.command == "doctor":
        run_doctor(cfg, fetcher, args.only)
    elif args.command == "sources":
        run_sources(state)
    elif args.command == "discover":
        from .discover import run_discover
        run_discover(cfg, fetcher)
    elif args.command == "test-alert":
        if not notify.channels() and not os.environ.get("DRY_RUN"):
            have = [k for k in ("SMTP_USER", "SMTP_PASSWORD", "SMTP_HOST", "ALERT_EMAIL", "NTFY_TOPIC", "TWILIO_ACCOUNT_SID") if os.environ.get(k)]
            print(
                "ERROR: no alert channel is configured. For Gmail, add BOTH repository secrets "
                "SMTP_USER (your Gmail address) and SMTP_PASSWORD (a Google App Password).\n"
                f"Secrets this run received: {', '.join(have) or 'none'}"
            )
            return 1
        used = notify.send("Ammo monitor test", "Test alert from your ammo deal monitor. If you got this, alerts work.", urgent=True)
        print("Channels:", used)
    return 0


if __name__ == "__main__":
    sys.exit(main())
