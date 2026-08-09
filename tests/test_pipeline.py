import asyncio
from decimal import Decimal

import httpx
import pytest
from fastapi.testclient import TestClient
from supplier_pipeline.adapters import (
    HtmlSupplierAdapter,
    RestSupplierAdapter,
    playwright_fetch_page,
)
from supplier_pipeline.api import create_app
from supplier_pipeline.persistence import SQLAlchemyRepository
from supplier_pipeline.pipeline import (
    Matcher,
    Pipeline,
    SchemaChanged,
    SourceUnavailable,
    parse_product,
)
from supplier_pipeline.store import SQLiteStore


def row(**changes):
    base = dict(
        external_id="1",
        sku=" ab-c 1 ",
        manufacturer_sku="m-1",
        name="Widget",
        brand="Acme",
        price="10,50",
        currency="rur",
        stock="3",
        unit="PCS",
    )
    base.update(changes)
    return base


class Pages:
    def __init__(self, name, pages, fail_at=None):
        self.name, self.data, self.fail_at = name, pages, fail_at

    async def pages(self, cursor=0, etag=""):
        for i in range(cursor, len(self.data)):
            if i == self.fail_at:
                raise TimeoutError("synthetic crash")
            yield self.data[i], {"etag": f"v{i}"}


def run(adapter, batch, store):
    return asyncio.run(Pipeline(store).crawl(adapter, batch))


def test_normalization():
    product = parse_product("s", row())
    assert (product.sku, product.price, product.currency) == (
        "ABC1",
        Decimal("10.50"),
        "RUB",
    )


def test_schema_drift():
    bad = row()
    del bad["price"]
    with pytest.raises(SchemaChanged):
        parse_product("s", bad)


def test_duplicate_sku_external_id_is_rejected():
    with pytest.raises(SchemaChanged):
        run(Pages("s", [[row(), row()]]), "x", SQLiteStore())


def test_ambiguous_mapping_queues_manual_decision():
    a = parse_product("s", row(external_id="a"))
    b = parse_product("x", row(external_id="b"))
    c = parse_product("y", row(external_id="c"))
    assert Matcher().match(a, [b, c])["decision"] == "MANUAL_REVIEW"


def test_resume_after_crash():
    store = SQLiteStore()
    bad = Pages("s", [[row(external_id="1")], [row(external_id="2")]], 1)
    with pytest.raises(TimeoutError):
        run(bad, "failed", store)
    assert store.get_state("s").cursor == 1
    assert run(
        Pages("s", [[row(external_id="1")], [row(external_id="2")]]), "resume", store
    )
    assert store.get_state("s").completed


def test_idempotent_batch():
    store = SQLiteStore()
    adapter = Pages("s", [[row()]])
    assert run(adapter, "same", store)
    assert run(adapter, "same", store) == []


def test_removed_and_restored_product():
    store = SQLiteStore()
    run(Pages("s", [[row(external_id="a"), row(external_id="b")]]), "first", store)
    assert {
        x["kind"] for x in run(Pages("s", [[row(external_id="a")]]), "second", store)
    } == {"unchanged", "missing"}
    assert any(
        x["kind"] == "restored"
        for x in run(
            Pages("s", [[row(external_id="a"), row(external_id="b")]]), "third", store
        )
    )


def test_http_429_retries_then_succeeds():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(
            429 if calls["n"] == 1 else 200, json={"items": [row()], "next_page": None}
        )

    adapter = RestSupplierAdapter(
        "s",
        "https://fixture.local/items",
        httpx.AsyncClient(transport=httpx.MockTransport(handler)),
    )
    assert (
        run(adapter, "http", SQLiteStore())[0]["kind"] == "created" and calls["n"] == 2
    )


def test_http_500_and_timeout_fail_after_retry():
    for handler in (
        lambda r: httpx.Response(500),
        lambda r: (_ for _ in ()).throw(httpx.ReadTimeout("x")),
    ):
        adapter = RestSupplierAdapter(
            "s",
            "https://fixture.local/items",
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )
        with pytest.raises((SourceUnavailable, httpx.ReadTimeout)):
            run(adapter, "fail" + str(id(handler)), SQLiteStore())


def test_broken_html_is_schema_drift():
    adapter = HtmlSupplierAdapter(
        "s",
        "https://fixture.local/html",
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda r: httpx.Response(200, text="<main>broken</main>")
            )
        ),
    )
    with pytest.raises(SourceUnavailable):
        run(adapter, "html", SQLiteStore())


def test_playwright_extra_is_optional_and_fails_actionably_without_it():
    with pytest.raises(RuntimeError, match="playwright"):
        asyncio.run(playwright_fetch_page(0, "https://fixture.local/items"))


def test_api_control_endpoints():
    store = SQLiteStore()
    store.add_review("s:1", ["a", "b"])
    client = TestClient(create_app(store))
    assert client.get("/runs").status_code == 200
    assert client.get("/review").json()[0]["decision"] == "pending"
    assert (
        client.post("/review/1/resolve", params={"resolution": "a"}).status_code == 200
    )
    assert (
        client.post(
            "/crawl", json={"supplier": "s", "url": "https://example.com/items"}
        ).status_code
        == 400
    )
    assert create_app(store).openapi()["info"]["title"] == "Supplier Data Pipeline"


def test_snapshot_history_records_versions():
    store = SQLiteStore()
    run(Pages("s", [[row(price="10")]]), "a", store)
    run(Pages("s", [[row(price="11")]]), "b", store)
    assert len(store.snapshot_history("s", "1")) == 2


def test_sqlalchemy_adapter_and_reopen(tmp_path):
    path = str(tmp_path / "demo.db")
    store = SQLAlchemyRepository(path)
    run(Pages("s", [[row()]]), "a", store)
    assert SQLAlchemyRepository(path).products()[0]["external_id"] == "1"


def test_pagination_calls_second_page():
    calls = []

    def handler(request):
        page = int(request.url.params["page"])
        calls.append(page)
        return httpx.Response(
            200,
            json={
                "items": [row(external_id=str(page))],
                "next_page": 1 if page == 0 else None,
            },
        )

    assert len(
        run(
            RestSupplierAdapter(
                "s",
                "https://local",
                httpx.AsyncClient(transport=httpx.MockTransport(handler)),
            ),
            "p",
            SQLiteStore(),
        )
    ) == 2 and calls == [0, 1]


def test_conditional_fetch_sends_etag():
    headers = []

    def handler(request):
        headers.append(request.headers.get("if-none-match"))
        return httpx.Response(304)

    store = SQLiteStore()
    store.get_state("s")
    store.db.execute("UPDATE states SET etag='v1' WHERE supplier='s'")
    store.db.commit()
    assert run(
        RestSupplierAdapter(
            "s",
            "https://local",
            httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        ),
        "conditional",
        store,
    ) == [] and headers == ["v1"]


def test_review_is_integrated_in_crawl():
    store = SQLiteStore()
    run(Pages("a", [[row(external_id="a")]]), "a", store)
    run(Pages("b", [[row(external_id="b")]]), "b", store)
    run(Pages("c", [[row(external_id="c")]]), "c", store)
    assert store.reviews()[0]["decision"] == "pending"


@pytest.mark.parametrize("factory", [SQLiteStore, SQLAlchemyRepository])
def test_failed_batch_discards_staged_reviews_with_products(factory):
    store = factory()
    # Two canonical candidates create a tied manual-review decision for source c.
    run(Pages("a", [[row(external_id="a")]]), "a", store)
    run(Pages("b", [[row(external_id="b")]]), "b", store)
    failing = Pages(
        "c",
        [[row(external_id="c")], [row(external_id="never")]],
        fail_at=1,
    )
    with pytest.raises(TimeoutError):
        run(failing, "failed-review", store)
    assert all(product["external_id"] != "c" for product in store.products())
    assert store.reviews() == []


def test_metrics_count_successful_run():
    from supplier_pipeline.pipeline import InMemoryMetrics

    metrics = InMemoryMetrics()
    asyncio.run(Pipeline(SQLiteStore(), metrics).crawl(Pages("s", [[row()]]), "m"))
    assert metrics.values["runs_completed"] == 1


def test_reopen_preserves_state(tmp_path):
    path = str(tmp_path / "state.db")
    store = SQLiteStore(path)
    run(Pages("s", [[row()]]), "a", store)
    assert SQLiteStore(path).get_state("s").completed
