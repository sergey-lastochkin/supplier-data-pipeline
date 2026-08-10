"""Render compact SVG evidence from a public-catalog result JSON."""

from __future__ import annotations

import argparse
import html
import json
from itertools import pairwise
from pathlib import Path


def write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")


def svg_document(width: int, height: int, content: str) -> str:
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">
<style>text {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; fill: #172033; }} .box {{ fill: #f5f8fc; stroke: #455a80; stroke-width: 1.5; }} .arrow {{ stroke: #6d7f9f; stroke-width: 2; marker-end: url(#arrow); }} .label {{ font-size: 14px; font-weight: 600; }} .small {{ font-size: 12px; fill: #4d607e; }}</style>
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="3" orient="auto"><path d="M0,0 L0,6 L7,3 z" fill="#6d7f9f"/></marker></defs>{content}</svg>\n'''


def flow(data: dict[str, object]) -> str:
    sources = data["sources"]
    source_names = " / ".join(item["label"] for item in sources)
    boxes = [
        (30, "Public APIs", source_names),
        (270, "Schema gate", "code + product_name"),
        (510, "Normalize / match", "conservative review"),
        (750, "SQLite snapshots", "deltas + history"),
    ]
    nodes = "".join(
        f'<rect class="box" x="{x}" y="72" width="190" height="86" rx="8"/>'
        f'<text class="label" x="{x + 12}" y="102">{html.escape(title)}</text>'
        f'<text class="small" x="{x + 12}" y="128">{html.escape(detail)}</text>'
        for x, title, detail in boxes
    )
    arrows = "".join(
        f'<line class="arrow" x1="{x + 190}" y1="115" x2="{next_x - 8}" y2="115"/>'
        for (x, _, _), (next_x, _, _) in pairwise(boxes)
    )
    return svg_document(
        970,
        230,
        '<text class="label" x="30" y="35">Bounded public catalog ingestion</text>'
        + nodes
        + arrows,
    )


def metrics(data: dict[str, object]) -> str:
    sources = data["sources"]
    maximum = max((item["records_accepted"] for item in sources), default=1)
    rows = []
    for index, item in enumerate(sources):
        y = 58 + index * 54
        width = 500 * item["records_accepted"] / maximum
        rows.append(
            f'<text class="small" x="30" y="{y}">{html.escape(item["label"])}</text>'
            f'<rect x="255" y="{y - 15}" width="{width:.1f}" height="20" rx="3" fill="#3b82f6"/>'
            f'<text class="small" x="770" y="{y}">{item["records_accepted"]} accepted, {item["records_skipped"]} skipped</text>'
        )
    summary = data["summary"]
    rows.append(
        f'<text class="small" x="30" y="220">unique source keys: {summary["unique_source_keys"]}; exact auto matches: {summary["exact_auto_matches"]}; manual review: {summary["manual_review"]}; error rate: {summary["error_rate"]}</text>'
    )
    return svg_document(
        980,
        250,
        '<text class="label" x="30" y="28">Actual run: accepted records by source</text>'
        + "".join(rows),
    )


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("results", type=Path)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.results.read_text())
    write(args.output_dir / "ingestion-flow.svg", flow(data))
    write(args.output_dir / "source-metrics.svg", metrics(data))


if __name__ == "__main__":
    main()
