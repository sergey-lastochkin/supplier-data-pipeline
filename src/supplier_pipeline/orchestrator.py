from __future__ import annotations

import asyncio
import json
import random
from collections.abc import Awaitable, Callable
from hashlib import sha256

from .domain import Metrics, SchemaChanged, SourceUnavailable, SupplierAdapter
from .matching import Matcher
from .normalization import parse_product


class InMemoryMetrics:
    def __init__(self) -> None:
        self.values: dict[str, int] = {}

    def increment(self, name: str, value: int = 1) -> None:
        self.values[name] = self.values.get(name, 0) + value


async def retry[Result](
    operation: Callable[[], Awaitable[Result]],
    attempts: int = 3,
    base_delay: float = 0.01,
    jitter: float = 0.01,
    random_value: Callable[[], float] = random.random,
) -> Result:
    error: Exception | None = None
    for attempt in range(attempts):
        try:
            return await operation()
        except (TimeoutError, ConnectionError, SourceUnavailable) as exc:
            error = exc
            if attempt + 1 < attempts:
                await asyncio.sleep(base_delay * (2**attempt) + random_value() * jitter)
    assert error is not None
    raise error


class Pipeline:
    def __init__(
        self, store, metrics: Metrics | None = None, concurrency: int = 4
    ) -> None:
        self.store = store
        self.metrics = metrics or InMemoryMetrics()
        self.semaphore = asyncio.Semaphore(concurrency)
        self.matcher = Matcher()

    async def crawl(
        self, adapter: SupplierAdapter, batch_id: str
    ) -> list[dict[str, str]]:
        if self.store.batch_exists(batch_id):
            return []
        state = self.store.get_state(adapter.name)
        self.store.begin_batch(batch_id, adapter.name)
        try:
            async with self.semaphore:
                async for rows, metadata in adapter.pages(state.cursor, state.etag):
                    products = [parse_product(adapter.name, row) for row in rows]
                    identifiers = [product.external_id for product in products]
                    if len(identifiers) != len(set(identifiers)):
                        raise SchemaChanged("duplicate external_id in source page")
                    candidates = (
                        self.store.canonical_candidates(adapter.name)
                        if hasattr(self.store, "canonical_candidates")
                        else []
                    )
                    for product in products:
                        decision = self.matcher.match(product, candidates)
                        self.metrics.increment(
                            "matches_" + decision["decision"].lower()
                        )
                        if decision["decision"] == "MANUAL_REVIEW":
                            self.store.stage_review(
                                batch_id,
                                ":".join(product.key()),
                                decision["candidates"],
                            )
                    fingerprint = sha256(
                        json.dumps(rows, sort_keys=True).encode()
                    ).hexdigest()
                    self.store.stage_page(
                        batch_id,
                        products,
                        state.cursor + 1,
                        metadata.get("etag", ""),
                        metadata.get("last_modified", ""),
                        fingerprint,
                    )
                    state = self.store.get_state(adapter.name)
                    self.metrics.increment("pages_ingested")
            deltas = self.store.commit_batch(batch_id)
            self.metrics.increment("runs_completed")
            return deltas
        except Exception:
            self.store.fail_batch(batch_id)
            self.metrics.increment("runs_failed")
            raise


__all__ = ["InMemoryMetrics", "Pipeline", "retry"]
