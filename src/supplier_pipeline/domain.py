from __future__ import annotations

import json
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass
from decimal import Decimal
from hashlib import sha256
from typing import Any, Protocol


class PipelineError(RuntimeError):
    code = "PIPELINE_ERROR"


class SchemaChanged(PipelineError):
    code = "SCHEMA_CHANGED"


class SourceUnavailable(PipelineError):
    code = "SOURCE_UNAVAILABLE"


@dataclass(frozen=True, slots=True)
class Product:
    supplier: str
    external_id: str
    sku: str
    manufacturer_sku: str
    name: str
    brand: str
    price: Decimal | None
    currency: str
    stock: Decimal
    unit: str
    url: str = ""
    source_updated_at: str = ""

    def key(self) -> tuple[str, str]:
        return self.supplier, self.external_id

    def fingerprint(self) -> str:
        payload = {**asdict(self), "price": str(self.price), "stock": str(self.stock)}
        return sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


@dataclass(slots=True)
class CrawlState:
    cursor: int = 0
    completed: bool = False
    etag: str = ""
    last_modified: str = ""
    source_fingerprint: str = ""


class SupplierAdapter(Protocol):
    name: str

    async def pages(
        self, cursor: int = 0, etag: str = ""
    ) -> AsyncIterator[tuple[list[dict[str, Any]], dict[str, str]]]: ...


class Metrics(Protocol):
    def increment(self, name: str, value: int = 1) -> None: ...


__all__ = [
    "CrawlState",
    "Metrics",
    "PipelineError",
    "Product",
    "SchemaChanged",
    "SourceUnavailable",
    "SupplierAdapter",
]
