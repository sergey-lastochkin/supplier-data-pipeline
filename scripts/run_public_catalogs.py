"""Run one bounded, read-only sample from each declared public catalog.

Raw API responses are intentionally written outside the repository.  The
committed manifest records their hashes and retrieval metadata instead.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import sys
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import httpx

from supplier_pipeline.adapters import OPEN_FACTS_SOURCES, OpenFactsSearchAdapter
from supplier_pipeline.domain import PipelineError
from supplier_pipeline.orchestrator import InMemoryMetrics, Pipeline
from supplier_pipeline.store import SQLiteStore


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def git_revision() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"], text=True, cwd=Path(__file__).parents[1]
    ).strip()


async def collect(args: argparse.Namespace) -> tuple[dict[str, object], dict[str, object]]:
    started = time.perf_counter()
    retrieved_at = datetime.now(UTC).isoformat()
    raw_dir = args.work_dir / "raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    store = SQLiteStore(str(args.work_dir / "pipeline.sqlite"))
    source_rows: list[dict[str, object]] = []
    errors: list[dict[str, str]] = []
    totals: Counter[str] = Counter()

    for position, source in enumerate(OPEN_FACTS_SOURCES):
        if position:
            # One request was already emitted.  Keep globally below the
            # published 10 search requests/min/IP policy across sibling APIs.
            await asyncio.sleep(args.inter_source_delay)
        adapter = OpenFactsSearchAdapter(
            source, page_size=args.page_size, rate_per_second=10_000
        )
        metrics = InMemoryMetrics()
        batch_id = f"{args.run_id}-{source.slug}-{uuid4().hex[:8]}"
        source_started = time.perf_counter()
        try:
            deltas = await Pipeline(store, metrics=metrics, concurrency=1).crawl(
                adapter, batch_id
            )
            raw_path = raw_dir / f"{source.slug}.json"
            raw_path.write_bytes(adapter.raw_response)
            observation = adapter.observation
            source_rows.append(
                {
                    "source": source.slug,
                    "label": source.label,
                    "product_type": source.product_type,
                    "base_url": source.base_url,
                    "documentation_url": source.documentation_url,
                    "license": source.license,
                    "request_url": observation.request_url,
                    "http_status": observation.status_code,
                    "response_sha256": observation.response_sha256,
                    "response_bytes": observation.response_bytes,
                    "response_headers": observation.response_headers,
                    "raw_snapshot": f"raw/{source.slug}.json (outside repository)",
                    "fields": observation.fields,
                    "schema_required": ["code", "product_name"],
                    "schema_status": "accepted",
                    "records_returned": observation.accepted + observation.skipped,
                    "records_accepted": observation.accepted,
                    "records_skipped": observation.skipped,
                    "skipped_reasons": observation.skipped_reasons,
                    "deltas": dict(sorted(Counter(delta["kind"] for delta in deltas).items())),
                    "matching": {
                        "exact_auto": metrics.values.get("matches_match", 0),
                        "manual_review": metrics.values.get("matches_manual_review", 0),
                        "no_match": metrics.values.get("matches_no_match", 0),
                    },
                    "duration_ms": round((time.perf_counter() - source_started) * 1000, 2),
                }
            )
            totals.update(metrics.values)
        except (httpx.HTTPError, PipelineError, TimeoutError, ValueError) as exc:
            errors.append(
                {
                    "source": source.slug,
                    "error_type": type(exc).__name__,
                    "message": str(exc),
                }
            )

    products = store.products()
    accepted = sum(int(row["records_accepted"]) for row in source_rows)
    exact_auto = sum(int(row["matching"]["exact_auto"]) for row in source_rows)
    manual_review = sum(
        int(row["matching"]["manual_review"]) for row in source_rows
    )
    manifest = {
        "run_id": args.run_id,
        "retrieved_at": retrieved_at,
        "raw_storage": {
            "location": "external --work-dir/raw (not committed)",
            "committed_to_repository": False,
            "reason": "source responses are external database contents; only provenance is committed",
        },
        "sources": source_rows,
    }
    results = {
        "run_id": args.run_id,
        "retrieved_at": retrieved_at,
        "code_commit": git_revision(),
        "python": sys.version.split()[0],
        "parameters": {
            "page_size": args.page_size,
            "inter_source_delay_seconds": args.inter_source_delay,
            "api": "Open Facts v2 search, read-only",
        },
        "summary": {
            "sources_attempted": len(OPEN_FACTS_SOURCES),
            "sources_succeeded": len(source_rows),
            "sources_failed": len(errors),
            "records_accepted": accepted,
            "unique_source_keys": len(products),
            "unique_normalized_skus": len(
                {product["payload"]["sku"] for product in products}
            ),
            "exact_auto_matches": exact_auto,
            "manual_review": manual_review,
            "manual_review_rate": round(manual_review / accepted, 6) if accepted else None,
            "error_rate": round(len(errors) / len(OPEN_FACTS_SOURCES), 6),
            "duration_seconds": round(time.perf_counter() - started, 3),
            "metrics": dict(sorted(totals.items())),
        },
        "errors": errors,
        "sources": source_rows,
        "limitations": [
            "These are public collaborative product catalogs, not authenticated commercial supplier feeds.",
            "The run requests a bounded first page only; it is not a full catalog mirror.",
            "No prices or stock quantities are inferred when the public response does not provide them.",
            "Exact cross-source matches are reported only when the conservative matcher observes them.",
        ],
    }
    return manifest, results


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("studies/openfacts-catalog-run-2026-08-10"),
    )
    parser.add_argument("--run-id", default="openfacts-catalog-2026-08-10")
    parser.add_argument("--page-size", type=int, default=12)
    parser.add_argument("--inter-source-delay", type=float, default=7.0)
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.page_size < 1 or args.page_size > 100:
        raise SystemExit("--page-size must be in [1, 100]")
    manifest, results = asyncio.run(collect(args))
    write_json(args.output_dir / "source-manifest.json", manifest)
    write_json(args.output_dir / "results.json", results)
    print(json.dumps(results["summary"], ensure_ascii=False, sort_keys=True))


if __name__ == "__main__":
    main()
