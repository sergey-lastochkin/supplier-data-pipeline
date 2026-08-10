from .persistence import SQLAlchemyRepository
from .pipeline import Matcher, Pipeline, Product, SchemaChanged
from .store import SQLiteStore

__all__ = [
    "Matcher",
    "Pipeline",
    "Product",
    "SQLAlchemyRepository",
    "SQLiteStore",
    "SchemaChanged",
]
"""Incremental product-catalog ingestion primitives."""

from .adapters import OPEN_FACTS_SOURCES, OpenFactsSearchAdapter, OpenFactsSource

__all__ = ["OPEN_FACTS_SOURCES", "OpenFactsSearchAdapter", "OpenFactsSource"]
