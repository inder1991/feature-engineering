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
    clear_projection_unavailable,
    seed_conflict,
    seed_fork,
    seed_hash_mismatch,
    seed_no_value,
    seed_projection_unavailable,
    seed_resolved,
    seed_retired,
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


# ── no_value: a live decision on a RECOMMENDATION-ceiling field — never operational ───────────────
def test_seed_no_value_reads_no_value(db):
    col = seed_no_value(db)
    ov = _read(db, col)
    assert ov.status == "no_value" == col.expected_status
    assert ov.conflict_status == "influence_not_operational"
    assert ov.decision_event_id is not None           # the decision exists; it is just not operational


# ── conflict: two top-strength evidences that disagree — the resolver cannot pick one ─────────────
def test_seed_conflict_reads_conflict(db):
    col = seed_conflict(db)
    ov = _read(db, col)
    assert ov.status == "conflict" == col.expected_status
    assert ov.conflict_status == "conflict"           # the resolver's genuine conflict reason


# ── fork (GATE 1): no single unambiguous latest non-retired decision head ─────────────────────────
def test_seed_fork_reads_fork(db):
    col = seed_fork(db)
    ov = _read(db, col)
    assert ov.status == "fork" == col.expected_status
    assert ov.conflict_status == "forked_decision_head"
    assert ov.value is None and ov.producer is None   # fail-closed: no operational value served


# ── hash_mismatch (GATE 2): the flat value tampered out from under the decision ───────────────────
def test_seed_hash_mismatch_reads_hash_mismatch(db):
    col = seed_hash_mismatch(db)
    ov = _read(db, col)
    assert ov.status == "hash_mismatch" == col.expected_status
    assert ov.conflict_status == "value_hash_mismatch"
    assert ov.value is None                           # the tampered value is never served
    assert ov.decision_event_id is not None           # audit ref carried, no authority served


# ── projection_unavailable (GATE 3): the load-bearing overlay projection is degraded ──────────────
def test_seed_projection_unavailable_reads_projection_unavailable(db):
    col = seed_projection_unavailable(db)
    ov = _read(db, col)
    assert ov.status == "projection_unavailable" == col.expected_status
    assert "DEGRADED" in ov.conflict_status
    assert ov.value is None and ov.producer is None   # nothing trusted on a degraded projection

    # Clearing the degradation restores the underlying clean resolved column — the gate keyed off
    # LIVE projection health, and the fixture's seeding really was the clean governed path.
    clear_projection_unavailable(db)
    assert _read(db, col).status == "resolved"


# ── retired: the latest decision is a retiring STALED — never served as operational ───────────────
def test_seed_retired_reads_retired(db):
    col = seed_retired(db)
    ov = _read(db, col)
    assert ov.status == "retired" == col.expected_status
    assert ov.producer is None and ov.strength is None   # no manufactured authority
    assert ov.value == "non_additive"                 # echoes display, but NOT load-bearing
