# Supplier Data Pipeline

## What

Async, local-first supplier ingestion reference implementation. It crawls paginated synthetic REST, HTML, dynamic and internal JSON sources; normalizes products; proposes conservative matches; persists checkpoints and snapshots; and exposes deltas/review through a control API.

## Why

A supplier import is not just “download JSON and upsert.” Sources throttle, time out, change schemas and remove products; retries can repeat batches; a matching tie can merge different SKUs. This project preserves transport cursors, batch identity, snapshot history and manual-review evidence separately.

## Architecture

- `adapters.py`: async REST, HTML, internal JSON and dynamic/optional Playwright boundaries.
- Domain/normalization/matching modules: canonical product and conservative candidate decisions.
- `persistence.py`: repository contract and real SQLAlchemy Core SQLite implementation.
- `store.py`: dependency-light sqlite3 implementation used for parity/failure tests.
- Orchestrator: bounded async page concurrency, rate control, conditional metadata, checkpoints and commit.
- `api.py`: crawl/run/product/delta/review control surface.

## Key engineering decisions

- Only localhost, `.local` and `.test` sources are accepted by the demo API.
- Retry uses exponential backoff with jitter only for transport-safe reads.
- Page checkpoints survive a crash; only the newest failed checkpoint is transferred.
- Source and product fingerprints distinguish drift from unchanged data.
- Every committed product revision is stored in snapshot history.
- Matching ties and weak fuzzy candidates enter a review queue instead of auto-merging.
- Candidate reviews are staged with their batch and become visible only with the same local commit as products and deltas; failed batches discard them.
- Deltas explicitly distinguish `created`, `updated`, `unchanged`, `missing`, `restored`.

## Run

```bash
python3.12 -m venv .venv
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e .
.venv/bin/uvicorn supplier_pipeline.api:app
```

The minimal install includes REST/HTML/internal JSON adapters, SQLAlchemy-backed local SQLite mode and the API. It does not require Playwright.

Optional browser adapter:

```bash
.venv/bin/python -m pip install -e '.[playwright]'
# Then install a browser with: .venv/bin/playwright install chromium
```

## Test

```bash
.venv/bin/pip install -e '.[dev]'
.venv/bin/python -m pytest -q
```

API: `POST /crawl`, `GET /runs`, `GET /products`, `GET /deltas`, `GET /review`, `POST /review/{id}/resolve`.

Tests use `httpx.MockTransport` and synthetic HTML/JSON. They cover 429, 500, timeout, broken HTML, conditional 304, schema drift, duplicate identifiers, ambiguous mapping, review creation, pagination, crash resume, SQLite/SQLAlchemy persistence, snapshot history, idempotent batches and missing/restored products.

## Limitations

- External supplier websites are intentionally replaced by local synthetic fixtures.
- Playwright adapter code is present, but browser binaries were not installed or executed.
- `bsl/ImportEndpoint.bsl` is an illustrative configuration-mapped adapter, not a standalone runnable 1C module.
- No production auth, proxy policy, distributed worker coordination or PostgreSQL migration is included.
- The SQLite adapters are local single-writer demos; bounded crawl concurrency is not a claim of a generally thread-safe multi-process crawler.
