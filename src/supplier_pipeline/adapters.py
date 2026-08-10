"""Transport adapters for controlled and public catalog sources."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Any

import httpx
from bs4 import BeautifulSoup

from .domain import SourceUnavailable
from .orchestrator import retry


class _HttpBase:
    def __init__(
        self,
        name: str,
        url: str,
        client: httpx.AsyncClient | None = None,
        rate_per_second: float = 100.0,
        user_agent: str = "supplier-data-pipeline/0.2 (github.com/sergey-lastochkin/supplier-data-pipeline)",
    ) -> None:
        self.name, self.url, self.client, self.delay, self.user_agent = (
            name,
            url,
            client,
            1 / rate_per_second,
            user_agent,
        )

    async def _get(
        self, params: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        async def call():
            client = self.client or httpx.AsyncClient()
            try:
                response = await client.get(
                    self.url,
                    params=params,
                    headers={"User-Agent": self.user_agent, **headers},
                    timeout=20,
                )
                if response.status_code == 304:
                    return response
                if response.status_code in (429, 500, 502, 503, 504):
                    raise SourceUnavailable(f"HTTP {response.status_code}")
                response.raise_for_status()
                return response
            finally:
                if self.client is None:
                    await client.aclose()

        result = await retry(call)
        await asyncio.sleep(self.delay)
        return result


class RestSupplierAdapter(_HttpBase):
    async def pages(
        self, cursor: int = 0, etag: str = ""
    ) -> AsyncIterator[tuple[list[dict[str, Any]], dict[str, str]]]:
        page = cursor
        while True:
            response = await self._get(
                {"page": page}, {"If-None-Match": etag} if etag else {}
            )
            if response.status_code == 304:
                return
            payload = response.json()
            rows = payload.get("items")
            if not isinstance(rows, list):
                raise SourceUnavailable("JSON payload lacks items list")
            yield (
                rows,
                {
                    "etag": response.headers.get("etag", ""),
                    "last_modified": response.headers.get("last-modified", ""),
                },
            )
            if not payload.get("next_page"):
                return
            page = int(payload["next_page"])


class InternalJsonEndpointAdapter(RestSupplierAdapter):
    """Same controlled JSON contract, named separately for internal endpoint ownership."""


class HtmlSupplierAdapter(_HttpBase):
    async def pages(
        self, cursor: int = 0, etag: str = ""
    ) -> AsyncIterator[tuple[list[dict[str, Any]], dict[str, str]]]:
        page = cursor
        while True:
            response = await self._get(
                {"page": page}, {"If-None-Match": etag} if etag else {}
            )
            soup = BeautifulSoup(response.text, "html.parser")
            cards = soup.select("[data-product]")
            if not cards:
                raise SourceUnavailable("HTML schema drift: no [data-product] cards")
            rows = []
            for card in cards:
                data = {
                    key.removeprefix("data-"): value
                    for key, value in card.attrs.items()
                    if key.startswith("data-")
                }
                rows.append(data)
            yield (
                rows,
                {
                    "etag": response.headers.get("etag", ""),
                    "last_modified": response.headers.get("last-modified", ""),
                },
            )
            next_link = soup.select_one("a[rel=next]")
            if not next_link:
                return
            page += 1


class DynamicSupplierAdapter:
    """Playwright-compatible interface; browser setup remains an explicit deployment choice."""

    def __init__(
        self, name: str, fetch_page: Callable[[int], Awaitable[list[dict[str, Any]]]]
    ) -> None:
        self.name, self.fetch_page = name, fetch_page

    async def pages(
        self, cursor: int = 0, etag: str = ""
    ) -> AsyncIterator[tuple[list[dict[str, Any]], dict[str, str]]]:
        page = cursor
        while True:
            rows = await self.fetch_page(page)
            if not rows:
                return
            yield rows, {"etag": f"local-dynamic-{page}"}
            page += 1


async def playwright_fetch_page(page, url: str) -> list[dict[str, Any]]:
    """Optional real browser adapter for a local fixture; imported lazily."""
    try:
        from playwright.async_api import async_playwright
    except ImportError as exc:
        raise RuntimeError(
            "install supplier-data-pipeline[playwright] to use browser adapter"
        ) from exc
    async with async_playwright() as browser:
        chromium = await browser.chromium.launch()
        tab = await chromium.new_page()
        await tab.goto(f"{url}?page={page}")
        rows = await tab.locator("[data-product]").evaluate_all(
            "els => els.map(e => Object.fromEntries([...e.attributes].filter(a=>a.name.startsWith('data-')).map(a=>[a.name.slice(5),a.value])))"
        )
        await chromium.close()
        return rows


@dataclass(frozen=True, slots=True)
class OpenFactsSource:
    """A documented public product catalog in the Open Facts family."""

    slug: str
    label: str
    base_url: str
    product_type: str
    documentation_url: str = (
        "https://openfoodfacts.github.io/documentation/docs/Product-Opener/api/"
    )
    license: str = "ODbL-1.0 / Database Contents License"


OPEN_FACTS_SOURCES = (
    OpenFactsSource(
        "open_food_facts",
        "Open Food Facts",
        "https://world.openfoodfacts.org",
        "food",
    ),
    OpenFactsSource(
        "open_beauty_facts",
        "Open Beauty Facts",
        "https://world.openbeautyfacts.org",
        "beauty",
    ),
    OpenFactsSource(
        "open_pet_food_facts",
        "Open Pet Food Facts",
        "https://world.openpetfoodfacts.org",
        "petfood",
    ),
)


@dataclass(slots=True)
class SourceObservation:
    """Provenance captured for a single immutable source response."""

    request_url: str = ""
    status_code: int = 0
    response_sha256: str = ""
    response_bytes: int = 0
    response_headers: dict[str, str] = field(default_factory=dict)
    fields: list[str] = field(default_factory=list)
    accepted: int = 0
    skipped: int = 0
    skipped_reasons: dict[str, int] = field(default_factory=dict)


class OpenFactsSearchAdapter(_HttpBase):
    """Read-only adapter for one bounded Open Facts v2 search response.

    Open Facts documents a limit of 10 search requests/min/IP.  The adapter's
    default 7 second delay therefore stays below that limit for repeated use.
    The public-run script asks for one small page from each declared catalog.
    """

    fields = "code,product_name,brands,quantity,last_modified_t,url"

    def __init__(
        self,
        source: OpenFactsSource,
        page_size: int = 12,
        client: httpx.AsyncClient | None = None,
        rate_per_second: float = 1 / 7,
    ) -> None:
        super().__init__(
            source.slug,
            f"{source.base_url}/api/v2/search",
            client=client,
            rate_per_second=rate_per_second,
        )
        self.source = source
        self.page_size = page_size
        self.observation = SourceObservation()
        self.raw_response = b""

    async def pages(
        self, cursor: int = 0, etag: str = ""
    ) -> AsyncIterator[tuple[list[dict[str, Any]], dict[str, str]]]:
        # A bounded, single-page sample is intentional.  Larger imports should
        # use the project's documented data exports instead of search crawling.
        if cursor:
            return
        response = await self._get(
            {
                "fields": self.fields,
                "sort_by": "last_modified_t",
                "page_size": self.page_size,
                "page": 1,
            },
            {"If-None-Match": etag} if etag else {},
        )
        if response.status_code == 304:
            return
        payload = response.json()
        products = payload.get("products")
        if not isinstance(products, list):
            raise SourceUnavailable("Open Facts schema drift: products is not a list")
        field_names = sorted(
            {str(key) for product in products if isinstance(product, dict) for key in product}
        )
        missing = {"code", "product_name"} - set(field_names)
        if missing:
            raise SourceUnavailable(
                "Open Facts schema drift: missing " + ", ".join(sorted(missing))
            )
        rows: list[dict[str, Any]] = []
        skipped: dict[str, int] = {}
        for product in products:
            if not isinstance(product, dict):
                skipped["not_object"] = skipped.get("not_object", 0) + 1
                continue
            code = str(product.get("code") or "").strip()
            name = str(product.get("product_name") or "").strip()
            if not code or not name:
                reason = "missing_code" if not code else "missing_product_name"
                skipped[reason] = skipped.get(reason, 0) + 1
                continue
            rows.append(
                {
                    "external_id": code,
                    "sku": code,
                    "manufacturer_sku": "",
                    "name": name,
                    "brand": product.get("brands", ""),
                    "price": None,
                    "currency": "",
                    "stock": "0",
                    "unit": "catalog_item",
                    "url": product.get("url")
                    or f"{self.source.base_url}/product/{code}",
                    "source_updated_at": str(product.get("last_modified_t") or ""),
                }
            )
        self.raw_response = response.content
        self.observation = SourceObservation(
            request_url=str(response.request.url),
            status_code=response.status_code,
            response_sha256=sha256(response.content).hexdigest(),
            response_bytes=len(response.content),
            response_headers={
                key.lower(): value
                for key, value in response.headers.items()
                if key.lower() in {"etag", "last-modified", "content-type"}
            },
            fields=field_names,
            accepted=len(rows),
            skipped=sum(skipped.values()),
            skipped_reasons=skipped,
        )
        yield rows, {
            "etag": response.headers.get("etag", ""),
            "last_modified": response.headers.get("last-modified", ""),
        }
