"""Transport adapters. All concrete examples are intended for local synthetic endpoints."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
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
    ) -> None:
        self.name, self.url, self.client, self.delay = (
            name,
            url,
            client,
            1 / rate_per_second,
        )

    async def _get(
        self, params: dict[str, Any], headers: dict[str, str]
    ) -> httpx.Response:
        async def call():
            client = self.client or httpx.AsyncClient()
            try:
                response = await client.get(
                    self.url, params=params, headers=headers, timeout=0.2
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
