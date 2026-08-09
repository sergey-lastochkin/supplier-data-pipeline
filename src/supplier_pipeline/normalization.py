from __future__ import annotations

import re
import unicodedata
from decimal import Decimal, InvalidOperation
from typing import Any

from .domain import Product, SchemaChanged


def norm_text(value: object) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(value or "")).strip().split())


def norm_sku(value: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", norm_text(value).upper())


def norm_currency(value: object) -> str:
    source = norm_text(value).upper()
    return {"RUR": "RUB", "РУБ": "RUB", "$": "USD", "€": "EUR"}.get(source, source)


def norm_decimal(value: object) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value).replace(" ", "").replace(",", "."))
    except InvalidOperation as exc:
        raise SchemaChanged(f"invalid decimal: {value}") from exc


def parse_product(supplier: str, row: dict[str, Any]) -> Product:
    required = {"external_id", "sku", "name", "price", "currency", "stock", "unit"}
    if missing := required - row.keys():
        raise SchemaChanged("missing fields: " + ", ".join(sorted(missing)))
    return Product(
        supplier=supplier,
        external_id=str(row["external_id"]),
        sku=norm_sku(row["sku"]),
        manufacturer_sku=norm_sku(row.get("manufacturer_sku", "")),
        name=norm_text(row["name"]),
        brand=norm_text(row.get("brand", "")).upper(),
        price=norm_decimal(row["price"]),
        currency=norm_currency(row["currency"]),
        stock=norm_decimal(row["stock"]) or Decimal(0),
        unit=norm_text(row["unit"]).lower(),
        url=str(row.get("url", "")),
        source_updated_at=str(row.get("source_updated_at", "")),
    )


__all__ = ["norm_currency", "norm_decimal", "norm_sku", "norm_text", "parse_product"]
