"""Gold-set quality gate for batched concept enrichment. Marked 'eval' — run on demand:

    uv run pytest -m eval tests/eval/ -q

Default CI EXCLUDES it (pyproject `addopts = ... -m "not eval"`); it GATES flipping a task's
default enrichment mode to `batch`. Hermetic mode uses a scripted FakeLLM (a self-check of the
harness itself). Live mode: set FEATUREGEN_LLM_PROVIDER=anthropic and OVERLAY_ENRICH_CONCEPT_MODE=
batch, then run against a throwaway DB to measure the real provider against the gold set.
"""
from __future__ import annotations

import pytest
from tests.eval.gold_columns import CRITICAL, GOLD

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.concepts import is_known_concept
from featuregen.overlay.upload.enrich import content_hash, enrich_concepts

pytestmark = pytest.mark.eval

GATE = 0.90   # minimum gold accuracy required to promote a task's default to batch mode


def _is_known_or_unclassified(concept: str) -> bool:
    return concept == "unclassified" or is_known_concept(concept)


def _scripted_batch() -> FakeLLM:
    """Hermetic self-check: script the model to return each gold column's expected concept, keyed by
    the column's content hash (the batch path's per-item ref)."""
    expected = {content_hash(r): c for r, c, _alts in GOLD}
    results = [{"ref": h, "concept": expected[h]} for h in expected]
    return FakeLLM(script={"overlay.enrich.concept": FakeResponse(output={"results": results})})


def test_gold_set_is_internally_consistent() -> None:
    """Cheap, DB-free guard so a future gold edit can't silently break the gate: every expected
    concept is real (or 'unclassified') and is itself an accepted alternative, every alternative is
    real, rows are uniquely hashed, and every CRITICAL concept is actually covered."""
    hashes = [content_hash(r) for r, _c, _a in GOLD]
    assert len(hashes) == len(set(hashes)), "gold rows must hash uniquely (duplicate column?)"
    for row, expected, alts in GOLD:
        assert _is_known_or_unclassified(expected), (
            f"expected concept {expected!r} for {row.table}.{row.column} is not in the registry")
        assert expected in alts, f"expected {expected!r} must be an acceptable alternative"
        for alt in alts:
            assert _is_known_or_unclassified(alt), f"alternative {alt!r} is not in the registry"
    covered = {expected for _r, expected, _a in GOLD}
    assert CRITICAL <= covered, f"CRITICAL concepts not exercised by any gold row: {CRITICAL - covered}"


def test_concept_gold_accuracy_meets_gate(db, monkeypatch) -> None:
    monkeypatch.setenv("OVERLAY_ENRICH_CONCEPT_MODE", "batch")
    rows = [r for r, _c, _a in GOLD]
    client = _scripted_batch()
    out = enrich_concepts(db, rows, client)

    hits = crit_hits = crit_total = 0
    for row, expected, alts in GOLD:
        got = out.get(content_hash(row))
        ok = got in alts
        hits += ok
        if expected in CRITICAL:
            crit_total += 1
            crit_hits += ok
        # never a hallucination: any returned value is a known concept or the literal 'unclassified'
        assert got is None or is_known_concept(got) or got == "unclassified"
    accuracy = hits / len(GOLD)
    assert accuracy >= GATE, f"gold accuracy {accuracy:.2%} below {GATE:.0%} gate"
    assert crit_hits == crit_total, "a critical concept regressed (zero-regression gate)"
