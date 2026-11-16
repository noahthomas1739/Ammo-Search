"""Shopify stores publish their catalog as public JSON:
  /collections/<handle>/products.json?limit=250&page=N
Each product lists its variants ("Box of 50", "Case of 500") with price and an
in-stock flag, so this is cleaner than reading rendered pages."""
from __future__ import annotations

import json
import re

from ..models import Listing
from ..parse import detect_rounds, normalize


def _strip(html: str) -> str:
    return normalize(re.sub(r"<[^>]+>", " ", html or ""))


def parse_products_json(body: str, base: str, retailer: str, caliber: str) -> list[Listing]:
    data = json.loads(body)
    out = []
    for p in data.get("products", []):
        ptitle = normalize(p.get("title", ""))
        desc = _strip(p.get("body_html", ""))[:400]
        url = f"{base}/products/{p.get('handle')}"
        for v in p.get("variants", []):
            try:
                price = float(v.get("price") or 0)
            except (TypeError, ValueError):
                continue
            if price <= 0:
                continue
            vt = normalize(v.get("title") or "")
            title = ptitle if vt.lower() in ("", "default title") else f"{ptitle} - {vt}"
            qty = v.get("inventory_quantity")
            out.append(
                Listing(
                    title=title,
                    retailer=retailer,
                    url=f"{url}?variant={v.get('id')}" if v.get("id") else url,
                    price=price,
                    rounds=detect_rounds(vt) or detect_rounds(ptitle) or detect_rounds(desc),
                    in_stock=bool(v.get("available", False)),
                    stock_qty=qty if isinstance(qty, int) and qty > 0 else None,
                    source=base,
                    caliber_hint=caliber,
                    extra_text=desc,
                )
            )
    return out


def product_count(body: str) -> int:
    try:
        return len(json.loads(body).get("products", []))
    except ValueError:
        return 0
