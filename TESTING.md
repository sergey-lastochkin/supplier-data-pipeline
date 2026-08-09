# Testing

After the README test install, run `.venv/bin/python -m pytest -q`. Network tests use `httpx.MockTransport`; no external site is contacted. Coverage includes normalization, conditional requests, pagination, 429/500/timeout, broken HTML, missing fields, duplicate external IDs, matching ambiguity/review, batch replay, crash resume, repository reopen, snapshot history and removed/restored products. FastAPI routes and the synthetic-source URL restriction are exercised with TestClient; the absent-Playwright boundary has its own test.
