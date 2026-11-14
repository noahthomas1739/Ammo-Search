"""Delivered cost: item price + shipping + Texas sales tax.

Shipping is the part that wrecks "cheap" listings, so every retailer has a
shipping rule in config.yaml. When an aggregator states the shipping cost for a
listing, that number wins over the rule.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Optional

from .models import Listing


@dataclass
class Plan:
    units: int
    rounds: int
    subtotal: float
    shipping: float
    tax: float
    total: float
    cpr: float
    shipping_estimated: bool


def shipping_for(rule: dict, subtotal: float, unit_rounds: int, caliber: str, listing_shipping: Optional[float]) -> tuple[float, bool]:
    """Return (shipping cost, is_estimate)."""
    if listing_shipping is not None:
        return listing_shipping, False
    free_over = rule.get("free_over")
    if free_over is not None and subtotal >= free_over:
        return 0.0, not rule.get("verified", False)
    free_min = (rule.get("free_min_rounds") or {}).get(caliber)
    if free_min and unit_rounds >= free_min:
        return 0.0, not rule.get("verified", False)
    return float(rule.get("flat", 14.99)), not rule.get("verified", False)


def plan_purchase(
    listing: Listing,
    unit_rounds: int,
    caliber: str,
    min_rounds: int,
    ship_rule: dict,
    tax_rate: float,
    tax_shipping: bool,
    collects_tax: bool,
    max_order_total: float,
) -> Optional[Plan]:
    """Cheapest sensible way to buy this listing.

    Candidates: the fewest units that reach the category minimum (e.g. ten
    50-round boxes for 500 rounds of 9mm), and the fewest units that unlock free
    shipping. Larger orders are not considered, since a flat shipping fee
    spread over an ever-bigger order would make anything look cheap.
    """
    if unit_rounds <= 0 or listing.price <= 0:
        return None
    n_min = max(1, math.ceil(min_rounds / unit_rounds))
    candidates = {n_min}
    free_over = ship_rule.get("free_over")
    if free_over and listing.shipping is None:
        candidates.add(max(n_min, math.ceil(free_over / listing.price)))

    best: Optional[Plan] = None
    for n in sorted(candidates):
        if listing.stock_qty is not None and n > listing.stock_qty:
            continue
        subtotal = listing.price * n
        if n > n_min and subtotal > max_order_total:
            continue
        ship, est = shipping_for(ship_rule, subtotal, unit_rounds, caliber, listing.shipping)
        rate = tax_rate if collects_tax else 0.0
        tax = (subtotal + ship) * rate if tax_shipping else subtotal * rate
        total = round(subtotal + ship + tax, 2)
        rounds = unit_rounds * n
        p = Plan(n, rounds, round(subtotal, 2), round(ship, 2), round(tax, 2), total, total / rounds, est)
        if best is None or p.cpr < best.cpr - 1e-9:
            best = p
    return best
