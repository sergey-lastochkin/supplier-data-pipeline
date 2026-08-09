"""sqlite3 parity adapter for zero-setup runs and repository contract tests."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal

from .domain import CrawlState, Product


class SQLiteStore:
    def __init__(self, path: str = ":memory:") -> None:
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""CREATE TABLE IF NOT EXISTS states(supplier TEXT PRIMARY KEY,cursor INTEGER NOT NULL DEFAULT 0,completed INTEGER NOT NULL DEFAULT 0,etag TEXT NOT NULL DEFAULT '',last_modified TEXT NOT NULL DEFAULT '',source_fingerprint TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS batches(id TEXT PRIMARY KEY,supplier TEXT NOT NULL,status TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS staged(batch_id TEXT NOT NULL,payload TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS products(supplier TEXT NOT NULL,external_id TEXT NOT NULL,payload TEXT NOT NULL,fingerprint TEXT NOT NULL,missing INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(supplier,external_id));
CREATE TABLE IF NOT EXISTS deltas(id INTEGER PRIMARY KEY,batch_id TEXT NOT NULL,supplier TEXT NOT NULL,external_id TEXT NOT NULL,kind TEXT NOT NULL,created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY,source_key TEXT NOT NULL,decision TEXT NOT NULL,candidates TEXT NOT NULL,resolution TEXT);
CREATE TABLE IF NOT EXISTS staged_reviews(batch_id TEXT NOT NULL,source_key TEXT NOT NULL,candidates TEXT NOT NULL,UNIQUE(batch_id,source_key));
CREATE TABLE IF NOT EXISTS snapshots(id INTEGER PRIMARY KEY,batch_id TEXT NOT NULL,supplier TEXT NOT NULL,external_id TEXT NOT NULL,payload TEXT NOT NULL,fingerprint TEXT NOT NULL,created_at TEXT NOT NULL);""")

    def batch_exists(self, batch_id: str) -> bool:
        return (
            self.db.execute(
                "SELECT 1 FROM batches WHERE id=? AND status='committed'", (batch_id,)
            ).fetchone()
            is not None
        )

    def begin_batch(self, batch_id: str, supplier: str) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO batches VALUES(?,?,?,?)",
            (batch_id, supplier, "running", datetime.now(UTC).isoformat()),
        )
        # Transfer only the newest interrupted checkpoint, then consume it. This
        # prevents older failed attempts from being copied into every future run.
        previous = self.db.execute(
            "SELECT id FROM batches WHERE supplier=? AND status='failed' AND id<>? ORDER BY created_at DESC LIMIT 1",
            (supplier, batch_id),
        ).fetchone()
        if previous:
            self.db.execute(
                "INSERT INTO staged(batch_id,payload) SELECT ?,payload FROM staged WHERE batch_id=?",
                (batch_id, previous[0]),
            )
            self.db.execute("DELETE FROM staged WHERE batch_id=?", (previous[0],))
        self.db.commit()

    def fail_batch(self, batch_id: str) -> None:
        self.db.execute("UPDATE batches SET status='failed' WHERE id=?", (batch_id,))
        # Checkpoints stay for resume; pending reviews must not escape a failed run.
        self.db.execute("DELETE FROM staged_reviews WHERE batch_id=?", (batch_id,))
        self.db.commit()

    def get_state(self, supplier: str) -> CrawlState:
        row = self.db.execute(
            "SELECT * FROM states WHERE supplier=?", (supplier,)
        ).fetchone()
        if row is None:
            self.db.execute("INSERT INTO states(supplier) VALUES(?)", (supplier,))
            self.db.commit()
            return CrawlState()
        return CrawlState(
            row["cursor"],
            bool(row["completed"]),
            row["etag"],
            row["last_modified"],
            row["source_fingerprint"],
        )

    @staticmethod
    def _dump(product: Product) -> str:
        d = asdict(product)
        d["price"] = str(product.price) if product.price is not None else None
        d["stock"] = str(product.stock)
        return json.dumps(d, sort_keys=True)

    @staticmethod
    def _load(payload: str) -> Product:
        d = json.loads(payload)
        d["price"] = Decimal(d["price"]) if d["price"] is not None else None
        d["stock"] = Decimal(d["stock"])
        return Product(**d)

    def stage_page(
        self,
        batch_id: str,
        products: list[Product],
        cursor: int,
        etag: str,
        last_modified: str,
        fingerprint: str,
    ) -> None:
        self.db.executemany(
            "INSERT INTO staged VALUES(?,?)",
            [(batch_id, self._dump(p)) for p in products],
        )
        supplier = (
            products[0].supplier
            if products
            else self.db.execute(
                "SELECT supplier FROM batches WHERE id=?", (batch_id,)
            ).fetchone()[0]
        )
        self.db.execute(
            "UPDATE states SET cursor=?,completed=0,etag=?,last_modified=?,source_fingerprint=? WHERE supplier=?",
            (cursor, etag, last_modified, fingerprint, supplier),
        )
        self.db.commit()

    def commit_batch(self, batch_id: str) -> list[dict[str, str]]:
        try:
            self.db.execute("BEGIN")
            supplier = self.db.execute(
                "SELECT supplier FROM batches WHERE id=?", (batch_id,)
            ).fetchone()[0]
            staged = [
                self._load(x[0])
                for x in self.db.execute(
                    "SELECT payload FROM staged WHERE batch_id=?", (batch_id,)
                )
            ]
            deltas = []
            seen = set()
            for p in staged:
                seen.add(p.external_id)
                old = self.db.execute(
                    "SELECT fingerprint,missing FROM products WHERE supplier=? AND external_id=?",
                    p.key(),
                ).fetchone()
                kind = (
                    "created"
                    if old is None
                    else "restored"
                    if old["missing"]
                    else "unchanged"
                    if old["fingerprint"] == p.fingerprint()
                    else "updated"
                )
                payload = self._dump(p)
                self.db.execute(
                    "INSERT OR REPLACE INTO products VALUES(?,?,?,?,0)",
                    (p.supplier, p.external_id, payload, p.fingerprint()),
                )
                self.db.execute(
                    "INSERT INTO snapshots(batch_id,supplier,external_id,payload,fingerprint,created_at) VALUES(?,?,?,?,?,?)",
                    (
                        batch_id,
                        p.supplier,
                        p.external_id,
                        payload,
                        p.fingerprint(),
                        datetime.now(UTC).isoformat(),
                    ),
                )
                deltas.append({"external_id": p.external_id, "kind": kind})
            for row in self.db.execute(
                "SELECT external_id FROM products WHERE supplier=? AND missing=0",
                (supplier,),
            ):
                if row["external_id"] not in seen:
                    self.db.execute(
                        "UPDATE products SET missing=1 WHERE supplier=? AND external_id=?",
                        (supplier, row["external_id"]),
                    )
                    deltas.append(
                        {"external_id": row["external_id"], "kind": "missing"}
                    )
            now = datetime.now(UTC).isoformat()
            self.db.executemany(
                "INSERT INTO deltas(batch_id,supplier,external_id,kind,created_at) VALUES(?,?,?,?,?)",
                [
                    (batch_id, supplier, d["external_id"], d["kind"], now)
                    for d in deltas
                ],
            )
            # Reviews become visible only with the successfully committed batch.
            self.db.execute(
                "INSERT INTO reviews(source_key,decision,candidates) "
                "SELECT source_key,'pending',candidates FROM staged_reviews WHERE batch_id=?",
                (batch_id,),
            )
            self.db.execute("DELETE FROM staged_reviews WHERE batch_id=?", (batch_id,))
            self.db.execute("DELETE FROM staged WHERE batch_id=?", (batch_id,))
            self.db.execute(
                "UPDATE batches SET status='committed' WHERE id=?", (batch_id,)
            )
            self.db.execute(
                "UPDATE states SET completed=1,cursor=0 WHERE supplier=?", (supplier,)
            )
            self.db.commit()
            return deltas
        except BaseException:
            self.db.rollback()
            raise

    def runs(self) -> list[dict[str, object]]:
        return [
            dict(r)
            for r in self.db.execute("SELECT * FROM batches ORDER BY created_at DESC")
        ]

    def products(self) -> list[dict[str, object]]:
        return [
            dict(r, payload=json.loads(r["payload"]))
            for r in self.db.execute(
                "SELECT * FROM products ORDER BY supplier,external_id"
            )
        ]

    def deltas(self) -> list[dict[str, object]]:
        return [
            dict(r) for r in self.db.execute("SELECT * FROM deltas ORDER BY id DESC")
        ]

    def reviews(self) -> list[dict[str, object]]:
        return [dict(r) for r in self.db.execute("SELECT * FROM reviews ORDER BY id")]

    def snapshot_history(
        self, supplier: str, external_id: str
    ) -> list[dict[str, object]]:
        return [
            dict(r, payload=json.loads(r["payload"]))
            for r in self.db.execute(
                "SELECT * FROM snapshots WHERE supplier=? AND external_id=? ORDER BY id",
                (supplier, external_id),
            )
        ]

    def canonical_candidates(self, supplier: str) -> list[Product]:
        return [
            self._load(r[0])
            for r in self.db.execute(
                "SELECT payload FROM products WHERE supplier<>? AND missing=0",
                (supplier,),
            )
        ]

    def add_review(self, source_key: str, candidates: list[str]) -> None:
        self.db.execute(
            "INSERT INTO reviews(source_key,decision,candidates) VALUES(?,?,?)",
            (source_key, "pending", json.dumps(candidates)),
        )
        self.db.commit()

    def stage_review(
        self, batch_id: str, source_key: str, candidates: list[str]
    ) -> None:
        self.db.execute(
            "INSERT OR IGNORE INTO staged_reviews(batch_id,source_key,candidates) VALUES(?,?,?)",
            (batch_id, source_key, json.dumps(candidates)),
        )
        self.db.commit()

    def resolve_review(self, review_id: int, resolution: str) -> bool:
        cur = self.db.execute(
            "UPDATE reviews SET decision='resolved',resolution=? WHERE id=? AND decision='pending'",
            (resolution, review_id),
        )
        self.db.commit()
        return cur.rowcount == 1
