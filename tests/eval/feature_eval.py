"""Pure, hermetic metrics for the Slice-3 feature-gen quality gate (spec §9).

DB-free and SDK-free on purpose: the metric logic is a CI gate (test_feature_eval.py) even though the
key-gated eval that consumes it (test_feature_gen_eval.py) only runs with a live provider key."""
from __future__ import annotations

from dataclasses import dataclass


def _tokens(text: str) -> set[str]:
    cleaned = "".join(c.lower() if c.isalnum() else " " for c in text)
    return {t for t in cleaned.split() if len(t) > 2}


@dataclass(frozen=True, slots=True)
class GenFeature:
    """The eval's transport-agnostic view of one generated feature."""
    name: str
    derives_from: tuple[str, ...]
    operation_kind: str
    validation_status: str
    requirement_count: int


def is_relevant(gen: GenFeature, expected_columns: frozenset[str],
                relevance_terms: frozenset[str]) -> bool:
    """Objective (no LLM judge): a feature is relevant if it derives from an expert-expected column,
    or its name shares a relevance term with the objective."""
    if any(ref in expected_columns for ref in gen.derives_from):
        return True
    return bool(_tokens(gen.name) & relevance_terms)


def relevance_rate(gens: list[GenFeature], expected_columns: frozenset[str],
                   relevance_terms: frozenset[str]) -> float:
    if not gens:
        return 0.0
    hits = sum(1 for g in gens if is_relevant(g, expected_columns, relevance_terms))
    return hits / len(gens)


def relevance_lift(baseline_rate: float, enriched_rate: float) -> float:
    """Relative lift of enriched over baseline. A zero baseline with any enriched hits is unbounded
    improvement (inf); zero over zero is no change (0.0)."""
    if baseline_rate <= 0.0:
        return float("inf") if enriched_rate > 0.0 else 0.0
    return (enriched_rate - baseline_rate) / baseline_rate


def unsafe_accepted(gens: list[GenFeature]) -> list[GenFeature]:
    """The hard-safety bar: a DESIGN_CHECKED feature must NEVER carry an unresolved requirement — that
    is exactly the NEEDS_EXTERNAL_VALIDATION contract. Any such feature is an unsafe acceptance."""
    return [g for g in gens
            if g.validation_status == "DESIGN_CHECKED" and g.requirement_count > 0]


def token_total(cost_metadata: dict) -> int:
    """Sum whatever input/output token counts the recorded cost_metadata carries; absent -> 0."""
    return int(cost_metadata.get("input_tokens", 0) or 0) + \
        int(cost_metadata.get("output_tokens", 0) or 0)


def cost_regression(baseline_tokens: int, enriched_tokens: int) -> float:
    """Relative token regression of enriched over baseline (0.25 == +25%)."""
    if baseline_tokens <= 0:
        return 0.0 if enriched_tokens <= 0 else float("inf")
    return (enriched_tokens - baseline_tokens) / baseline_tokens


def restricted_leaks(payloads: list[str], sentinels: frozenset[str]) -> list[str]:
    """Sentinels (seeded sample/PII markers) that survived into any recorded egress payload."""
    return [s for s in sentinels if any(s in p for p in payloads)]
