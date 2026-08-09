# Architecture

Adapters yield pages plus conditional-source metadata; they do not write products. The orchestrator validates and normalizes each page, persists a cursor/checkpoint, evaluates cross-supplier candidates, and commits products, deltas and staged review decisions in one local database transaction. Persistence is behind a repository protocol with both SQLAlchemy Core and sqlite3 SQLite implementations. Current products, immutable snapshots, deltas and review decisions are separate records. The API starts control operations but does not bypass the batch boundary.

Crash recovery transfers the newest failed staged checkpoint into a new batch and resumes from the persisted cursor. A committed batch ID is a no-op. Only safe read transport failures are retried.
