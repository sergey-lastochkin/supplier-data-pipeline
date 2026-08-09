from __future__ import annotations

from urllib.parse import urlparse
from uuid import uuid4

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field

from .adapters import InternalJsonEndpointAdapter
from .orchestrator import Pipeline
from .persistence import SQLAlchemyRepository


class CrawlRequest(BaseModel):
    supplier: str = Field(min_length=1)
    url: str
    batch_id: str | None = None


def synthetic_url(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host in {"localhost", "127.0.0.1", "::1"} or host.endswith(
        (".local", ".test")
    )


def create_app(store: SQLAlchemyRepository | None = None) -> FastAPI:
    app = FastAPI(title="Supplier Data Pipeline", version="1.0")
    app.state.store = store or SQLAlchemyRepository()

    @app.post("/crawl")
    async def crawl(request: CrawlRequest) -> dict[str, object]:
        if not synthetic_url(request.url):
            raise HTTPException(
                400, detail="only localhost/.local/.test synthetic sources are allowed"
            )
        batch_id = request.batch_id or str(uuid4())
        adapter = InternalJsonEndpointAdapter(request.supplier, request.url)
        try:
            return {
                "batch_id": batch_id,
                "deltas": await Pipeline(app.state.store).crawl(adapter, batch_id),
            }
        except Exception as exc:
            raise HTTPException(422, detail=str(exc)) from exc

    @app.get("/runs")
    async def runs() -> list[dict[str, object]]:
        return app.state.store.runs()

    @app.get("/products")
    async def products() -> list[dict[str, object]]:
        return app.state.store.products()

    @app.get("/deltas")
    async def deltas() -> list[dict[str, object]]:
        return app.state.store.deltas()

    @app.get("/review")
    async def review() -> list[dict[str, object]]:
        return app.state.store.reviews()

    @app.post("/review/{review_id}/resolve")
    async def resolve(review_id: int, resolution: str) -> dict[str, object]:
        if not app.state.store.resolve_review(review_id, resolution):
            raise HTTPException(404, detail="pending review not found")
        return {"id": review_id, "resolution": resolution}

    return app


app = create_app()
