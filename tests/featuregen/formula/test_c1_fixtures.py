"""Task 5 — prove the C1 fixtures drive the REAL operational-authority read.

Each ``seed_*`` helper in :mod:`tests.featuregen.formula.c1_fixtures` seeds ONE column through the
REAL governed path (evidence -> decision -> projection, exactly as the shipped operational-facts
suites do — never a flat ``graph_node`` insert that skips decisions), and each test here asserts the
DISCRIMINATING check: ``read_operational_value(...).status`` equals the status the helper claims.
That is what proves Task 6's output-authority tests exercise C1 itself, not a mock.
"""
from __future__ import annotations

from featuregen.overlay.upload.operational_facts import read_operational_value

from tests.featuregen.formula.c1_fixtures import (
    seed_resolved,
)


def _read(db, col):
    return read_operational_value(db, col.logical_ref, col.field_name)


# ── resolved: a governed decision field with a clean, hash-verified load-bearing value ────────────
def test_seed_resolved_reads_resolved(db):
    col = seed_resolved(db)
    ov = _read(db, col)
    assert ov.status == "resolved" == col.expected_status
    assert ov.value == "non_additive"                 # the projected display value, verified
    assert ov.producer is not None and ov.strength is not None   # real selected evidence
    assert ov.decision_event_id is not None           # a real decision, not a flat insert
