from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Optional

TIER_ORDER = {"GOOD": 1, "GREAT": 2, "EXCEPTIONAL": 3}


@dataclass
class Listing:
    """One product as scraped from a retailer or aggregator page."""

    title: str
    retailer: str
    url: str
    price: float  # price of one unit as listed (a box, case, or can), USD
    rounds: Optional[int] = None  # rounds in one unit, if the source states it
    in_stock: bool = True
    stock_qty: Optional[int] = None  # units available, if the page shows it
    shipping: Optional[float] = None  # shipping cost, if the source states it
    source: str = ""  # which page this came from
    caliber_hint: Optional[str] = None  # caliber of the page it was found on
    extra_text: str = ""  # extra description text (card text, table cells)
    marketplace: bool = False


@dataclass
class Deal:
    listing: Listing
    category: str  # "9mm", "12ga", "762x54r"
    label: str  # e.g. "12 GA Priority A (1-1/4 oz)"
    tier: str  # GOOD / GREAT / EXCEPTIONAL
    cpr: float  # delivered cost per round, dollars
    total: float  # delivered total for the purchase described
    units: int  # how many listing units are in that purchase
    total_rounds: int
    shipping: float
    tax: float
    shipping_estimated: bool
    flags: list[str] = field(default_factory=list)  # corrosive / steel core / Berdan
    notes: list[str] = field(default_factory=list)  # advice + things to verify
    suspicious: bool = False

    @property
    def key(self) -> str:
        return self.listing.url.split("#")[0].rstrip("/").lower()

    def to_dict(self) -> dict:
        d = asdict(self)
        d["key"] = self.key
        return d

    @staticmethod
    def from_dict(d: dict) -> "Deal":
        d = dict(d)
        d.pop("key", None)
        d["listing"] = Listing(**d["listing"])
        return Deal(**d)
