"""A concept's additivity must not survive contact with an incompatible column type.

`cust_aecb_dt` is `timestamp(0)` — the DATE an Al Etihad Credit Bureau inquiry happened. The LLM
gave it the concept `bureau_inquiry`, which declares `additivity="additive"` and says why in its own
description: *"Count of recent hard inquiries is the feature (additive)."* That is correct for the
COUNT of inquiries and wrong for the DATE of one, and the column inherited it wholesale.

Additivity is not cosmetic: the planner reads it to decide what may be summed across a grain, so an
`additive` timestamp is an invitation to generate `SUM(cust_aecb_dt)` — nonsense that would look
like a valid feature.

The guard is deliberately narrow. It does NOT try to judge whether the concept is right; that is the
vocabulary's job and a human's. It refuses exactly one provable contradiction: a temporal column
cannot be additive, whatever its concept claims. A concept mis-assignment then costs a missing
behaviour rather than a wrong one.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.evidence import AssertionStrength
from featuregen.overlay.upload.taxonomy_evidence import derive_concept_evidence

_S = AssertionStrength.PROPOSED


def _fields(concept: str, declared_type: str = "") -> dict[str, object]:
    return {name: value
            for name, value, _ in derive_concept_evidence(concept, _S, declared_type=declared_type)}


# ── the defect ───────────────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("declared_type", [
    "timestamp(0)", "timestamp", "date", "datetime", "TIMESTAMP(6)", "timestamp without time zone",
])
def test_a_temporal_column_never_inherits_additive(declared_type):
    """THE case from the live catalog: bureau_inquiry declares `additive`, cust_aecb_dt is a
    timestamp, and SUM(a timestamp) is meaningless."""
    assert "additivity" not in _fields("bureau_inquiry", declared_type)


def test_the_same_concept_on_a_NUMERIC_column_still_derives_additive():
    """The concept is not being overruled in general — only where the type contradicts it. A count
    of bureau inquiries is genuinely additive and must stay so."""
    assert _fields("bureau_inquiry", "integer")["additivity"] == "additive"


def test_the_guard_is_silent_when_no_type_is_declared():
    """Absent a declared type there is no contradiction to prove, so the concept stands. Suppressing
    on ignorance would quietly strip additivity from every source that omits the column."""
    assert _fields("bureau_inquiry", "")["additivity"] == "additive"


# ── the guard is narrow: it suppresses ONE field, on ONE provable contradiction ───────────────────

def test_only_additivity_is_suppressed_not_the_safety_fields():
    """temporal_role, sensitivity_floor and leakage_anchor are unaffected — a date column still has
    a PIT role and a sensitivity floor, and dropping those would weaken safety to fix semantics."""
    derived = _fields("bureau_inquiry", "timestamp(0)")
    assert {"temporal_role", "sensitivity_floor", "leakage_anchor"} <= set(derived)


def test_a_non_temporal_type_is_left_alone():
    for declared_type in ("varchar(150)", "numeric(18,2)", "integer", "boolean", "text"):
        assert _fields("bureau_inquiry", declared_type)["additivity"] == "additive"


def test_a_concept_that_declares_no_additivity_is_unchanged_either_way():
    """The `n/a` concepts emitted nothing before and must still emit nothing — the guard must not
    become a second, different reason for absence."""
    assert "additivity" not in _fields("customer_id", "timestamp(0)")
    assert "additivity" not in _fields("customer_id", "integer")


def test_an_unknown_concept_still_derives_nothing():
    assert _fields("no_such_concept", "timestamp(0)") == {}


# ── the existing contract is untouched ───────────────────────────────────────────────────────────

def test_declared_type_is_optional_so_every_existing_caller_is_unchanged():
    """Called without the new argument, behaviour is exactly what it was."""
    assert dict(
        (n, v) for n, v, _ in derive_concept_evidence("bureau_inquiry", _S)
    )["additivity"] == "additive"


def test_strength_still_propagates_unchanged():
    """The load-bearing invariant of this module: a derivation is never more certain than its
    concept. Suppression must not disturb the strength of what survives."""
    for _, _, strength in derive_concept_evidence("bureau_inquiry", _S, declared_type="timestamp"):
        assert strength is _S


# ── the guard must hold on the OTHER path that re-derives the cascade ────────────────────────────

def test_the_correction_path_reads_the_declared_type_too():
    """`derive_and_write_concept_cascade` runs from TWO places: glossary ingest and a human `concept`
    correction. If only the ingest path passed the type, correcting a concept would re-introduce
    `additive` on a timestamp and quietly defeat the guard until the next re-upload.

    Asserted structurally (both call sites pass it) because the correction path needs a full
    projecting-correction fixture to exercise end-to-end.
    """
    import inspect

    from featuregen.overlay.upload import field_correction, ingest

    for module in (ingest, field_correction):
        src = inspect.getsource(module)
        start = src.index("derive_and_write_concept_cascade(\n", src.index("def ") )
        call = src[start:start + 400]
        assert "declared_type" in call, f"{module.__name__} calls the cascade without declared_type"
