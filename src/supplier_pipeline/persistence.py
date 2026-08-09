"""Persistence contracts and a real SQLAlchemy Core SQLite implementation."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import create_engine, text
from sqlalchemy.pool import StaticPool

from .domain import CrawlState, Product


class Repository(Protocol):
    def batch_exists(self, batch_id: str) -> bool: ...
    def begin_batch(self, batch_id: str, supplier: str) -> None: ...
    def get_state(self, supplier: str) -> CrawlState: ...
    def stage_page(
        self,
        batch_id: str,
        products: list[Product],
        cursor: int,
        etag: str,
        last_modified: str,
        fingerprint: str,
    ) -> None: ...
    def commit_batch(self, batch_id: str) -> list[dict[str, str]]: ...
    def fail_batch(self, batch_id: str) -> None: ...
    def stage_review(
        self, batch_id: str, source_key: str, candidates: list[str]
    ) -> None: ...


SCHEMA = (
    """CREATE TABLE IF NOT EXISTS states (
        supplier TEXT PRIMARY KEY, cursor INTEGER NOT NULL DEFAULT 0,
        completed INTEGER NOT NULL DEFAULT 0, etag TEXT NOT NULL DEFAULT '',
        last_modified TEXT NOT NULL DEFAULT '', source_fingerprint TEXT NOT NULL DEFAULT '')""",
    """CREATE TABLE IF NOT EXISTS batches (
        id TEXT PRIMARY KEY, supplier TEXT NOT NULL, status TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS staged (
        batch_id TEXT NOT NULL, payload TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS products (
        supplier TEXT NOT NULL, external_id TEXT NOT NULL, payload TEXT NOT NULL,
        fingerprint TEXT NOT NULL, missing INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(supplier, external_id))""",
    """CREATE TABLE IF NOT EXISTS deltas (
        id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
        supplier TEXT NOT NULL, external_id TEXT NOT NULL, kind TEXT NOT NULL,
        created_at TEXT NOT NULL)""",
    """CREATE TABLE IF NOT EXISTS reviews (
        id INTEGER PRIMARY KEY AUTOINCREMENT, source_key TEXT NOT NULL,
        decision TEXT NOT NULL, candidates TEXT NOT NULL, resolution TEXT)""",
    """CREATE TABLE IF NOT EXISTS staged_reviews (
        batch_id TEXT NOT NULL, source_key TEXT NOT NULL, candidates TEXT NOT NULL,
        UNIQUE(batch_id, source_key))""",
    """CREATE TABLE IF NOT EXISTS snapshots (
        id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id TEXT NOT NULL,
        supplier TEXT NOT NULL, external_id TEXT NOT NULL, payload TEXT NOT NULL,
        fingerprint TEXT NOT NULL, created_at TEXT NOT NULL)""",
)


class SQLAlchemyRepository:
    """SQLAlchemy-backed local repository; no sqlite3 side channel is used."""

    def __init__(self, path: str = ":memory:") -> None:
        if path == ":memory:":
            self.engine = create_engine(
                "sqlite+pysqlite:///:memory:",
                poolclass=StaticPool,
                connect_args={"check_same_thread": False},
            )
        else:
            self.engine = create_engine(f"sqlite+pysqlite:///{path}")
        with self.engine.begin() as connection:
            for statement in SCHEMA:
                connection.execute(text(statement))

    @staticmethod
    def _dump(product: Product) -> str:
        payload = asdict(product)
        payload["price"] = str(product.price) if product.price is not None else None
        payload["stock"] = str(product.stock)
        return json.dumps(payload, sort_keys=True)

    @staticmethod
    def _load(payload: str) -> Product:
        data = json.loads(payload)
        data["price"] = Decimal(data["price"]) if data["price"] is not None else None
        data["stock"] = Decimal(data["stock"])
        return Product(**data)

    def batch_exists(self, batch_id: str) -> bool:
        with self.engine.connect() as connection:
            return (
                connection.execute(
                    text("SELECT 1 FROM batches WHERE id=:id AND status='committed'"),
                    {"id": batch_id},
                ).first()
                is not None
            )

    def begin_batch(self, batch_id: str, supplier: str) -> None:
        with self.engine.begin() as connection:
            previous = connection.execute(
                text(
                    "SELECT id FROM batches WHERE supplier=:supplier AND status='failed' "
                    "AND id<>:id ORDER BY created_at DESC LIMIT 1"
                ),
                {"supplier": supplier, "id": batch_id},
            ).scalar_one_or_none()
            connection.execute(
                text(
                    "INSERT INTO batches(id,supplier,status,created_at) VALUES(:id,:supplier,'running',:now) "
                    "ON CONFLICT(id) DO UPDATE SET status='running'"
                ),
                {
                    "id": batch_id,
                    "supplier": supplier,
                    "now": datetime.now(UTC).isoformat(),
                },
            )
            if previous:
                connection.execute(
                    text(
                        "INSERT INTO staged(batch_id,payload) "
                        "SELECT :new_id,payload FROM staged WHERE batch_id=:old_id"
                    ),
                    {"new_id": batch_id, "old_id": previous},
                )
                connection.execute(
                    text("DELETE FROM staged WHERE batch_id=:old_id"),
                    {"old_id": previous},
                )

    def fail_batch(self, batch_id: str) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text("UPDATE batches SET status='failed' WHERE id=:id"),
                {"id": batch_id},
            )
            connection.execute(
                text("DELETE FROM staged_reviews WHERE batch_id=:id"),
                {"id": batch_id},
            )

    def get_state(self, supplier: str) -> CrawlState:
        with self.engine.begin() as connection:
            row = (
                connection.execute(
                    text("SELECT * FROM states WHERE supplier=:supplier"),
                    {"supplier": supplier},
                )
                .mappings()
                .first()
            )
            if row is None:
                connection.execute(
                    text("INSERT INTO states(supplier) VALUES(:supplier)"),
                    {"supplier": supplier},
                )
                return CrawlState()
            return CrawlState(
                row["cursor"],
                bool(row["completed"]),
                row["etag"],
                row["last_modified"],
                row["source_fingerprint"],
            )

    def stage_page(
        self,
        batch_id: str,
        products: list[Product],
        cursor: int,
        etag: str,
        last_modified: str,
        fingerprint: str,
    ) -> None:
        with self.engine.begin() as connection:
            if products:
                supplier = products[0].supplier
                connection.execute(
                    text(
                        "INSERT INTO staged(batch_id,payload) VALUES(:batch_id,:payload)"
                    ),
                    [
                        {"batch_id": batch_id, "payload": self._dump(product)}
                        for product in products
                    ],
                )
            else:
                supplier = connection.execute(
                    text("SELECT supplier FROM batches WHERE id=:id"), {"id": batch_id}
                ).scalar_one()
            connection.execute(
                text(
                    "UPDATE states SET cursor=:cursor,completed=0,etag=:etag,last_modified=:modified,"
                    "source_fingerprint=:fingerprint WHERE supplier=:supplier"
                ),
                {
                    "cursor": cursor,
                    "etag": etag,
                    "modified": last_modified,
                    "fingerprint": fingerprint,
                    "supplier": supplier,
                },
            )

    def commit_batch(self, batch_id: str) -> list[dict[str, str]]:
        now = datetime.now(UTC).isoformat()
        deltas: list[dict[str, str]] = []
        with self.engine.begin() as connection:
            supplier = connection.execute(
                text("SELECT supplier FROM batches WHERE id=:id"), {"id": batch_id}
            ).scalar_one()
            staged = [
                self._load(row[0])
                for row in connection.execute(
                    text("SELECT payload FROM staged WHERE batch_id=:id"),
                    {"id": batch_id},
                )
            ]
            seen: set[str] = set()
            for product in staged:
                seen.add(product.external_id)
                old = (
                    connection.execute(
                        text(
                            "SELECT fingerprint,missing FROM products "
                            "WHERE supplier=:supplier AND external_id=:external_id"
                        ),
                        {
                            "supplier": product.supplier,
                            "external_id": product.external_id,
                        },
                    )
                    .mappings()
                    .first()
                )
                kind = (
                    "created"
                    if old is None
                    else "restored"
                    if old["missing"]
                    else "unchanged"
                    if old["fingerprint"] == product.fingerprint()
                    else "updated"
                )
                payload = self._dump(product)
                connection.execute(
                    text(
                        "INSERT INTO products(supplier,external_id,payload,fingerprint,missing) "
                        "VALUES(:supplier,:external_id,:payload,:fingerprint,0) "
                        "ON CONFLICT(supplier,external_id) DO UPDATE SET "
                        "payload=excluded.payload,fingerprint=excluded.fingerprint,missing=0"
                    ),
                    {
                        "supplier": product.supplier,
                        "external_id": product.external_id,
                        "payload": payload,
                        "fingerprint": product.fingerprint(),
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO snapshots(batch_id,supplier,external_id,payload,fingerprint,created_at) "
                        "VALUES(:batch_id,:supplier,:external_id,:payload,:fingerprint,:created_at)"
                    ),
                    {
                        "batch_id": batch_id,
                        "supplier": product.supplier,
                        "external_id": product.external_id,
                        "payload": payload,
                        "fingerprint": product.fingerprint(),
                        "created_at": now,
                    },
                )
                deltas.append({"external_id": product.external_id, "kind": kind})
            current = connection.execute(
                text(
                    "SELECT external_id FROM products WHERE supplier=:supplier AND missing=0"
                ),
                {"supplier": supplier},
            ).scalars()
            for external_id in current:
                if external_id not in seen:
                    connection.execute(
                        text(
                            "UPDATE products SET missing=1 "
                            "WHERE supplier=:supplier AND external_id=:external_id"
                        ),
                        {"supplier": supplier, "external_id": external_id},
                    )
                    deltas.append({"external_id": external_id, "kind": "missing"})
            if deltas:
                connection.execute(
                    text(
                        "INSERT INTO deltas(batch_id,supplier,external_id,kind,created_at) "
                        "VALUES(:batch_id,:supplier,:external_id,:kind,:created_at)"
                    ),
                    [
                        {
                            "batch_id": batch_id,
                            "supplier": supplier,
                            "external_id": delta["external_id"],
                            "kind": delta["kind"],
                            "created_at": now,
                        }
                        for delta in deltas
                    ],
                )
            connection.execute(
                text(
                    "INSERT INTO reviews(source_key,decision,candidates) "
                    "SELECT source_key,'pending',candidates FROM staged_reviews "
                    "WHERE batch_id=:id"
                ),
                {"id": batch_id},
            )
            connection.execute(
                text("DELETE FROM staged_reviews WHERE batch_id=:id"),
                {"id": batch_id},
            )
            connection.execute(
                text("DELETE FROM staged WHERE batch_id=:id"), {"id": batch_id}
            )
            connection.execute(
                text("UPDATE batches SET status='committed' WHERE id=:id"),
                {"id": batch_id},
            )
            connection.execute(
                text("UPDATE states SET completed=1,cursor=0 WHERE supplier=:supplier"),
                {"supplier": supplier},
            )
        return deltas

    def _mapping_list(self, statement: str) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            return [dict(row) for row in connection.execute(text(statement)).mappings()]

    def runs(self) -> list[dict[str, object]]:
        return self._mapping_list("SELECT * FROM batches ORDER BY created_at DESC")

    def products(self) -> list[dict[str, object]]:
        rows = self._mapping_list(
            "SELECT * FROM products ORDER BY supplier,external_id"
        )
        for row in rows:
            row["payload"] = json.loads(str(row["payload"]))
        return rows

    def deltas(self) -> list[dict[str, object]]:
        return self._mapping_list("SELECT * FROM deltas ORDER BY id DESC")

    def reviews(self) -> list[dict[str, object]]:
        return self._mapping_list("SELECT * FROM reviews ORDER BY id")

    def snapshot_history(
        self, supplier: str, external_id: str
    ) -> list[dict[str, object]]:
        with self.engine.connect() as connection:
            rows = [
                dict(row)
                for row in connection.execute(
                    text(
                        "SELECT * FROM snapshots WHERE supplier=:supplier "
                        "AND external_id=:external_id ORDER BY id"
                    ),
                    {"supplier": supplier, "external_id": external_id},
                ).mappings()
            ]
        for row in rows:
            row["payload"] = json.loads(str(row["payload"]))
        return rows

    def canonical_candidates(self, supplier: str) -> list[Product]:
        with self.engine.connect() as connection:
            payloads = connection.execute(
                text(
                    "SELECT payload FROM products WHERE supplier<>:supplier AND missing=0"
                ),
                {"supplier": supplier},
            ).scalars()
            return [self._load(payload) for payload in payloads]

    def add_review(self, source_key: str, candidates: list[str]) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO reviews(source_key,decision,candidates) "
                    "VALUES(:source_key,'pending',:candidates)"
                ),
                {"source_key": source_key, "candidates": json.dumps(candidates)},
            )

    def stage_review(
        self, batch_id: str, source_key: str, candidates: list[str]
    ) -> None:
        with self.engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO staged_reviews(batch_id,source_key,candidates) "
                    "VALUES(:batch_id,:source_key,:candidates) "
                    "ON CONFLICT(batch_id,source_key) DO NOTHING"
                ),
                {
                    "batch_id": batch_id,
                    "source_key": source_key,
                    "candidates": json.dumps(candidates),
                },
            )

    def resolve_review(self, review_id: int, resolution: str) -> bool:
        with self.engine.begin() as connection:
            result = connection.execute(
                text(
                    "UPDATE reviews SET decision='resolved',resolution=:resolution "
                    "WHERE id=:id AND decision='pending'"
                ),
                {"resolution": resolution, "id": review_id},
            )
            return result.rowcount == 1
