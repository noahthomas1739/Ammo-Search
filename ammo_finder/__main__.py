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
import time
from collections import Counter, defaultdict
from datetime import date, datetime, timezone
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


def run_check(cfg: dict, state: State, fetcher, only: Optional[str] = None) -> dict:
    alerts_cfg = cfg["alerts"]
    emailed_now: set = set()

    def alert_now(name: str, ls) -> None:
        """Email GREAT/EXCEPTIONAL deals from a store as soon as it's been read."""
        found: dict[str, Deal] = {}
        for l in ls:
            d = evaluate(l, cfg)[1]
            if d and d.tier in ("GREAT", "EXCEPTIONAL") and (d.key not in found or d.cpr < found[d.key].cpr):
                found[d.key] = d
        new = [d for d in sorted(found.values(), key=lambda d: (-TIER_ORDER[d.tier], d.cpr)) if state.should_alert(d, alerts_cfg)]
        for i in range(0, min(len(new), MAX_DEALS_PER_TEXT * 3), MAX_DEALS_PER_TEXT):
            batch = new[i : i + MAX_DEALS_PER_TEXT]
            title, body = format_alert(batch)
            notify.send(title, body, urgent=True)
            for d in batch:
                state.mark_alerted(d)
                emailed_now.add(d.key)
            state.save()  # remember right away, even if the run is interrupted later

    minutes = (cfg.get("fetch") or {}).get("max_run_minutes", 25)
    listings, pages = collect(cfg, fetcher, only=only, on_source_done=alert_now, deadline=time.time() + minutes * 60)
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

    sent_urgent = len(emailed_now)

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
        "deal_list": [
            {"tier": d.tier, "cpr": round(d.cpr, 4), "title": d.listing.title[:90], "retailer": d.listing.retailer,
             "url": d.listing.url, "emailed_now": d.key in emailed_now}
            for d in deals[:15]
        ],
    }
    _step_summary(summary, pages, deals)
    _LAST_SUMMARY.clear()
    _LAST_SUMMARY.update(summary)
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
    seen_sets: dict = {}
    by_src = defaultdict(list)
    for l in listings:
        by_src[l.source].append(l)
    for url, p in pages.items():
        status = f"{p['count']:3d} products" if p["ok"] else f"FAILED: {p['error']}"
        print(f"{p['source']:<24} {p['caliber']:<8} {p.get('via', ''):<8} {status}\n    {url}")
        mine = by_src.get(url, [])
        for l in mine[:3]:
            print(f"      - ${l.price:.2f} | {l.rounds or '?'} rds | {'in stock' if l.in_stock else 'OOS'} | {l.retailer} | {l.title[:70]}")
        if mine:
            why = _drop_reasons(cfg, mine)
            print("      usable: " + ", ".join(f"{k} {v}" for k, v in why.items()))
        sig = frozenset(l.url for l in mine)
        if sig and sig in seen_sets:
            print(f"      NOTE: same products as {seen_sets[sig]} (this page number seems to be ignored)")
        elif sig:
            seen_sets[sig] = url
    deals = [d for l in listings for d in [evaluate(l, cfg)[1]] if d]
    if deals:
        print("\nDeals found:")
        for d in sorted(deals, key=lambda d: d.cpr):
            print(f"  {d.tier:<11} {d.cpr * 100:.1f}¢/rd  ${d.total:.2f} for {d.total_rounds} | {d.listing.retailer} | {d.listing.title[:70]}\n      {d.listing.url}")
    print("\nCoverage this run (a store that couldn't be reached is NOT evidence of no deals):")
    for name, c in coverage(cfg, listings, pages).items():
        if only and only.lower() not in name.lower():
            continue
        extra = ""
        if c["direct_routes"]:
            extra = "via " + ", ".join(c["direct_routes"])
        elif c["alternatives"]:
            extra = "via " + ", ".join(f"{k} ({v} listings)" for k, v in c["alternatives"].items())
        elif c["failures"]:
            extra = "; ".join(c["failures"][:3])
        print(f"  {name:<24} {c['status']:<20} {extra}")
    _step_summary({"doctor": True, "pages_ok": sum(1 for p in pages.values() if p["count"])}, pages, [])


def _drop_reasons(cfg: dict, ls) -> dict:
    """Why listings from one page were or weren't usable."""
    from .parse import parse
    c = Counter()
    for l in ls:
        a = parse(f"{l.title} {l.extra_text}", l.caliber_hint)
        m, d = evaluate(l, cfg)
        if d:
            c["deal"] += 1
        elif m:
            c["right product, price above GOOD"] += 1
        elif not l.in_stock:
            c["out of stock"] += 1
        elif not (l.rounds or a.rounds):
            c["no round count"] += 1
        else:
            c["not your spec"] += 1
    return dict(c)


_LAST_SUMMARY: dict = {}


def _record_run(state_path: str, command: str, started, error) -> None:
    """Append this run (what it found, which emails went out or failed) to state, for `status`."""
    try:
        st = State(state_path)  # re-read: the command may have saved newer data
        st.data.pop("current_run", None)
        runs = st.data.setdefault("runs", [])
        runs.append({
            "command": command,
            "started": started.isoformat(timespec="seconds"),
            "finished": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "error": error,
            "summary": {k: v for k, v in _LAST_SUMMARY.items()},
            "emails": list(notify.SENT),
        })
        st.data["runs"] = runs[-30:]
        st.save()
    except Exception as e:  # never let bookkeeping hide the real result
        log.warning("could not record run: %s", e)


def run_status(state: State) -> None:
    runs = state.data.get("runs", [])
    cur = state.data.get("current_run")
    if cur:
        local = datetime.fromisoformat(cur["started"]).astimezone().strftime("%a %b %d %I:%M %p")
        print(f"A '{cur['command']}' run started {local} hasn't finished: it's still running, or it was stopped "
              "(Mac asleep/shut down). Deals it found before stopping were already emailed.\n")
    if not runs:
        print("No finished runs recorded yet.")
    for r in runs[-8:]:
        local = datetime.fromisoformat(r["started"]).astimezone().strftime("%a %b %d %I:%M %p")
        sm = r.get("summary") or {}
        line = f"{local}  {r['command']:<6}"
        if sm:
            line += f"  stores read: {sm.get('pages_ok')}/{sm.get('pages_total')}  deals: {sm.get('deals')}  emailed now: {sm.get('texted')}  queued for digest: {sm.get('queued_for_digest')}"
        print(line)
        if r.get("error"):
            print(f"    RUN FAILED: {r['error']}")
        for d in sm.get("deal_list", []):
            note = "emailed this run" if d.get("emailed_now") else ("already emailed earlier" if d["tier"] != "GOOD" else "goes in daily digest")
            print(f"    {d['tier']:<11} {d['cpr'] * 100:.1f}¢/rd  {d['retailer']} | {d['title'][:60]}  ({note})")
        for e in r.get("emails", []):
            status = "SENT via " + ", ".join(e["ok"]) if e["ok"] else "NOT SENT: " + "; ".join(e["errors"])
            print(f"    email: \"{e['title'][:70]}\"  {status}")
    pend = state.data.get("digest_pending", {})
    print(f"\nGOOD deals waiting for the next daily digest: {len(pend)}")
    print(f"Last digest sent: {state.data.get('last_digest', 'never')}")


def run_best(cfg: dict, fetcher, caliber: str, only: Optional[str], top: int = 12) -> None:
    """Cheapest in-stock products that fit your spec right now, regardless of thresholds."""
    spec_names = {"9mm": ["9mm"], "12ga": ["12ga_a", "12ga_b"], "762x54r": ["762x54r"]}
    if caliber not in spec_names:
        print("usage: python -m ammo_finder best 9mm|12ga|762x54r [--only STORE]")
        return
    listings, pages = collect(cfg, fetcher, only=only)
    rows = {}
    for l in listings:
        m, d = evaluate(l, cfg)
        if m is None or m.cpr is None or m.spec not in spec_names[caliber]:
            continue
        key = l.url.split("#")[0]
        if key not in rows or m.cpr < rows[key][0].cpr:
            rows[key] = (m, l, d)
    ranked = sorted(rows.values(), key=lambda r: r[0].cpr)[:top]
    tiers = cfg["categories"][spec_names[caliber][0]]
    print(f"\nCheapest in-stock {caliber} that fits your spec (delivered to TX, shipping + tax included)")
    print(f"Your lines: GOOD {tiers['good'] * 100:.0f}¢, GREAT {tiers['great'] * 100:.0f}¢\n")
    if not ranked:
        print("Nothing in stock that fits your spec at the stores that could be read.")
    for m, l, d in ranked:
        buy = f"{m.units} x {m.unit_rounds}rd" if (m.units or 1) > 1 else f"{m.unit_rounds}rd"
        ship = " (shipping estimated)" if m.shipping_estimated else ""
        tier = f"  [{d.tier}]" if d else ""
        print(f"  {m.cpr * 100:5.1f}¢/rd  ${m.total:,.2f} for {m.total_rounds} rds ({buy}){ship}{tier}")
        print(f"           {l.retailer} | {l.title[:80]}")
        print(f"           {l.url}")
    unread = sorted({p['source'] for p in pages.values() if not p['ok']} - {p['source'] for p in pages.values() if p['ok'] and p['count']})
    if unread:
        print(f"\nCouldn't read (prices there unknown, not necessarily higher): {', '.join(unread)}")


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
    ap.add_argument("command", choices=["check", "digest", "monthly", "auto", "status", "best", "doctor", "discover", "sources", "inspect", "test-alert"])
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--state", default=os.environ.get("AMMO_STATE") or "state/state.json")
    ap.add_argument("--only", help="doctor/check: only stores whose name contains this")
    ap.add_argument("url", nargs="?", help="inspect: the page to summarize; best: 9mm, 12ga or 762x54r")
    ap.add_argument("--dry-run", action="store_true", help="print alerts instead of sending")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    if args.dry_run:
        os.environ["DRY_RUN"] = "1"

    cfg = load_config(args.config)
    state = State(args.state)
    fetcher = from_config(cfg, os.path.join(os.path.dirname(os.path.abspath(args.state)), "cache"))
    started = datetime.now(timezone.utc)
    error = None
    if args.command in ("check", "auto"):
        state.data["current_run"] = {"command": args.command, "started": started.isoformat(timespec="seconds")}
        state.save()
    try:
        rc = _dispatch(args, cfg, state, fetcher)
    except KeyboardInterrupt:
        print("\nStopped.")
        os._exit(130)  # skip browser shutdown, which would print noise after Ctrl+C
    except Exception as e:
        error = f"{type(e).__name__}: {e}"
        raise
    finally:
        if args.command in ("check", "auto", "digest", "monthly", "test-alert"):
            _record_run(args.state, args.command, started, error)
    fetcher.close()
    fetcher.prune_cache()
    return rc


def _dispatch(args, cfg: dict, state: State, fetcher) -> int:
    if args.command == "check":
        run_check(cfg, state, fetcher, only=args.only)
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
    elif args.command == "inspect":
        if not args.url:
            print("usage: python -m ammo_finder inspect URL")
            return 2
        from .inspect_page import run_inspect
        run_inspect(cfg, fetcher, args.url)
    elif args.command == "best":
        run_best(cfg, fetcher, (args.url or "9mm").lower(), args.only)
    elif args.command == "status":
        run_status(state)
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
