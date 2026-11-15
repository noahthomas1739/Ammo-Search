"""Persistent state (a JSON file committed back to the repo by the workflow)."""
from __future__ import annotations

import json
import os
import statistics
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import TIER_ORDER, Deal


def now() -> datetime:
    return datetime.now(timezone.utc)


def _ts(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: str) -> datetime:
    return datetime.fromisoformat(ts)


class State:
    def __init__(self, path: str):
        self.path = path
        self.data = {"alerted": {}, "digest_pending": {}, "digest_sent": {}, "history": {}, "health": {}, "sources": {}, "coverage": {}}
        if os.path.exists(path):
            with open(path) as f:
                self.data.update(json.load(f))

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self.data, f, indent=1, sort_keys=True)
        os.replace(tmp, self.path)

    # ---------------------------------------------------- urgent alerts
    def should_alert(self, d: Deal, alerts_cfg: dict, at: Optional[datetime] = None) -> bool:
        at = at or now()
        prev = self.data["alerted"].get(d.key)
        if not prev:
            return True
        if TIER_ORDER[d.tier] > TIER_ORDER.get(prev["tier"], 0):
            return True
        if d.cpr <= prev["cpr"] - alerts_cfg.get("realert_price_drop", 0.005):
            return True
        return at - _parse(prev["ts"]) >= timedelta(days=alerts_cfg.get("realert_after_days", 7))

    def mark_alerted(self, d: Deal, at: Optional[datetime] = None) -> None:
        self.data["alerted"][d.key] = {"tier": d.tier, "cpr": d.cpr, "ts": _ts(at or now())}

    # ---------------------------------------------------- daily digest
    def queue_digest(self, d: Deal, alerts_cfg: dict, at: Optional[datetime] = None) -> bool:
        at = at or now()
        if d.key in self.data["alerted"] and not self.should_alert(d, alerts_cfg, at):
            return False  # already texted as GREAT recently
        sent = self.data["digest_sent"].get(d.key)
        if sent:
            prev_cpr = sent.get("cpr", 9e9)
            fresh = at - _parse(sent["ts"]) >= timedelta(days=alerts_cfg.get("digest_repeat_days", 3))
            if not fresh and d.cpr > prev_cpr - alerts_cfg.get("realert_price_drop", 0.005):
                return False
        cur = self.data["digest_pending"].get(d.key)
        if cur is None or d.cpr <= cur["cpr"]:
            dd = d.to_dict()
            dd["found_at"] = _ts(at)
            self.data["digest_pending"][d.key] = dd
        return True

    def pop_digest(self, at: Optional[datetime] = None) -> list[Deal]:
        at = at or now()
        deals = []
        for key, dd in self.data["digest_pending"].items():
            dd = dict(dd)
            dd.pop("found_at", None)
            deal = Deal.from_dict(dd)
            deals.append(deal)
            self.data["digest_sent"][key] = {"cpr": deal.cpr, "ts": _ts(at)}
        self.data["digest_pending"] = {}
        return sorted(deals, key=lambda d: (d.category, d.cpr))

    # ---------------------------------------------------- price history
    def record_prices(self, obs: dict[str, list[float]], at: Optional[datetime] = None) -> None:
        """obs: spec -> delivered cpr of every in-stock listing that fit the spec (any price)."""
        day = (at or now()).date().isoformat()
        hist = self.data["history"]
        for spec, vals in obs.items():
            if not vals:
                continue
            h = hist.setdefault(spec, {})
            prev = h.get(day)
            lo = min(vals)
            med = statistics.median(vals)
            if prev:
                lo = min(lo, prev["min"])
                med = (med + prev["median"]) / 2
            h[day] = {"min": round(lo, 4), "median": round(med, 4), "n": len(vals) + (prev or {}).get("n", 0)}
        cutoff = ((at or now()) - timedelta(days=430)).date().isoformat()
        for spec in hist:
            hist[spec] = {d: v for d, v in hist[spec].items() if d >= cutoff}

    def prune(self, at: Optional[datetime] = None) -> None:
        cutoff = (at or now()) - timedelta(days=30)
        for k in ("alerted", "digest_sent"):
            self.data[k] = {key: v for key, v in self.data[k].items() if _parse(v["ts"]) >= cutoff}

    # ---------------------------------------------------- per-source tracking
    def record_sources(self, report: dict, cov: dict, at: Optional[datetime] = None) -> None:
        """Remember, per route (feed / sitemap / page URL), when it last worked and
        how it last failed; and per retailer, how it was last reached."""
        ts = _ts(at or now())
        src = self.data.setdefault("sources", {})
        for key, r in report.items():
            e = src.setdefault(key, {"fails": 0})
            e.update({"source": r["source"], "caliber": r["caliber"], "via": r["via"], "last_attempt": ts})
            if r["ok"]:
                e.update({"last_ok": ts, "fails": 0, "last_count": r["count"], "status": "ok" if r["count"] else "ok, no listings read"})
            else:
                e.update({"fails": e.get("fails", 0) + 1, "status": r["category"], "last_error": r["error_kind"], "last_error_detail": r["error"][:160]})
        covd = self.data.setdefault("coverage", {})
        for name, c in cov.items():
            e = covd.setdefault(name, {})
            e.update({"status": c["status"], "routes": c["direct_routes"], "alternatives": c["alternatives"], "failures": c["failures"], "last_checked": ts})
            if c["status"] == "direct":
                e["last_direct"] = ts
            if c["status"] in ("direct", "alternative"):
                e["last_covered"] = ts

    # ---------------------------------------------------- source health
    def record_health(self, any_success: bool, at: Optional[datetime] = None) -> None:
        """Counts consecutive runs that found no usable products. Counting runs
        (not hours) means a laptop that was off for a week doesn't trigger a
        false "no data" warning."""
        h = self.data["health"]
        if any_success:
            h["last_any_success"] = _ts(at or now())
            h["fail_streak"] = 0
        else:
            h["fail_streak"] = h.get("fail_streak", 0) + 1

    def needs_health_warning(self, alerts_cfg: dict, at: Optional[datetime] = None) -> Optional[int]:
        at = at or now()
        h = self.data["health"]
        streak = h.get("fail_streak", 0)
        if streak < alerts_cfg.get("health_warning_after_runs", 4):
            return None
        last = h.get("last_warning")
        if last and at - _parse(last) < timedelta(days=alerts_cfg.get("health_warning_every_days", 3)):
            return None
        h["last_warning"] = _ts(at)
        return streak

    # ---------------------------------------------------- digest / monthly timing (used by `auto`)
    def digest_due(self, at: Optional[datetime] = None) -> bool:
        last = self.data.get("last_digest")
        return not last or (at or now()) - _parse(last) >= timedelta(hours=20)

    def mark_digest(self, at: Optional[datetime] = None) -> None:
        self.data["last_digest"] = _ts(at or now())

    def monthly_due(self, at: Optional[datetime] = None) -> bool:
        """True on the first run in a new calendar month (once there is history to report)."""
        month = (at or now()).strftime("%Y-%m")
        last = self.data.get("last_monthly")
        if last is None:
            self.data["last_monthly"] = month  # first install: start counting from here
            return False
        return last != month

    def mark_monthly(self, at: Optional[datetime] = None) -> None:
        self.data["last_monthly"] = (at or now()).strftime("%Y-%m")
