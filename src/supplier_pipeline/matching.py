from __future__ import annotations

from difflib import SequenceMatcher
from typing import Any

from .domain import Product


class Matcher:
    """Exact matches may resolve; fuzzy/tied candidates require review."""

    def match(self, product: Product, candidates: list[Product]) -> dict[str, Any]:
        ranked: list[tuple[float, Product, list[str]]] = []
        for candidate in candidates:
            score = 0.0
            reasons: list[str] = []
            if product.sku and product.sku == candidate.sku:
                score += 1.0
                reasons.append("normalized_sku")
            if (
                product.brand
                and product.brand == candidate.brand
                and product.manufacturer_sku
                and product.manufacturer_sku == candidate.manufacturer_sku
            ):
                score += 0.8
                reasons.append("brand_manufacturer_sku")
            if product.brand and candidate.brand and product.brand != candidate.brand:
                score -= 0.7
                reasons.append("brand_conflict")
            if product.brand == candidate.brand and product.name and candidate.name:
                similarity = SequenceMatcher(
                    None, product.name.casefold(), candidate.name.casefold()
                ).ratio()
                if similarity >= 0.72:
                    score = max(score, similarity * 0.65)
                    reasons.append("fuzzy_name_candidate")
            if score > 0:
                ranked.append((score, candidate, reasons))

        ranked.sort(key=lambda item: (-item[0], item[1].external_id))
        if not ranked:
            return {"decision": "NO_MATCH", "candidates": []}
        top = ranked[0]
        ties = [item for item in ranked if abs(item[0] - top[0]) < 1e-9]
        if len(ties) > 1 or top[0] < 0.8:
            return {
                "decision": "MANUAL_REVIEW",
                "confidence": top[0],
                "candidates": [item[1].external_id for item in ties],
                "reason": top[2],
            }
        return {
            "decision": "MATCH",
            "confidence": top[0],
            "canonical": top[1].external_id,
            "reason": top[2],
        }


__all__ = ["Matcher"]
