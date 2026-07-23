"""Task 5 — C1 status fixtures: seed one column through the REAL governed authority path.

Reusable helpers for the TypedFormula suites (Task 6 output-authority tests): each ``seed_*`` helper
seeds ONE column (a ``logical_ref`` + a field) through the REAL evidence -> decision -> projection
machinery — ``build_graph`` + ``record_field_evidence`` + ``resolve_and_project`` /
``record_field_decision`` — exactly as the shipped operational-facts suites do
(``tests/featuregen/overlay/upload/test_operational_facts*.py``). NEVER a flat ``graph_node`` insert
that skips decisions/projections: a flat insert creates no decision lifecycle, so
``read_operational_value`` could not return the governed statuses these fixtures exist to produce.

Each helper returns a :class:`SeededColumn` carrying what a caller needs to read the column back
through the REAL C1 adapter::

    col = seed_resolved(db)
    ov = read_operational_value(db, col.logical_ref, col.field_name)
    assert ov.status == col.expected_status

COMPOSABILITY: ``build_graph`` DELETES every graph row for its ``catalog_source`` before rebuilding,
so each helper defaults to a DISTINCT source (``c1fx_<status>``) — multiple fixtures coexist in one
test database. Exception: :func:`seed_projection_unavailable` degrades the overlay projection
GLOBALLY (GATE 3 is checked before any per-column read), so while it is in effect EVERY
``read_operational_value`` in the same database fails closed with ``projection_unavailable``; use it
last, or restore the other fixtures' readability with :func:`clear_projection_unavailable`.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import (
    canonical_hash,
    field_input_hash,
    record_field_evidence,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import (
    FIELD_POLICY_VERSION,
    RESOLVER_VERSION,
    resolve_and_project,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref

# Pinned instants for the decision-log fixtures that need explicit ordering (fork/retired).
_T1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_T2 = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)


@dataclass(frozen=True)
class SeededColumn:
    """One column seeded through the real governed path, plus the status it must read back as."""

    logical_ref: str
    field_name: str
    expected_status: str
    source: str
    table: str
    column: str

    @property
    def object_ref(self) -> str:
        """The flat ``graph_node`` key (``build_graph`` public-flattens schema-less rows)."""
        return f"public.{self.table}.{self.column}"


def _build_column(db, source: str, table: str, column: str, data_type: str = "numeric") -> str:
    """One physical column node via the REAL graph builder; returns its schema-preserving ref."""
    build_graph(db, source, [CanonicalRow(source, table, column, data_type)])
    return normalize_ref(source, None, table, column)


def _record_evidence(db, logical_ref: str, field_name: str, value, producer, strength) -> str:
    """Real field evidence (mirrors the shipped operational-facts suites' ``_seed``)."""
    return record_field_evidence(
        db, logical_ref=logical_ref, field_name=field_name, proposed_value=value,
        producer=producer, strength=strength, producer_ref="c1-fixture",
        source_snapshot_id="c1-fixture-snap",
        input_hash=field_input_hash(logical_ref=logical_ref, field_name=field_name, material=value))


# ── resolved: a governed decision field (additivity) with clean source-attested evidence ──────────
def seed_resolved(db, *, source: str = "c1fx_resolved", table: str = "accounts",
                  column: str = "balance") -> SeededColumn:
    """``status="resolved"``: source-ATTESTED ``additivity`` evidence resolved + projected by the
    real resolver — a governed decision field with a hash-verified load-bearing value."""
    ref = _build_column(db, source, table, column)
    _record_evidence(db, ref, "additivity", "non_additive",
                     EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    resolve_and_project(db, source=source, logical_refs=[ref])
    return SeededColumn(ref, "additivity", "resolved", source, table, column)


# ── no_value: a live decision on a RECOMMENDATION-ceiling field (business_term) ───────────────────
def seed_no_value(db, *, source: str = "c1fx_no_value", table: str = "accounts",
                  column: str = "balance") -> SeededColumn:
    """``status="no_value"``: source-ATTESTED ``business_term`` evidence resolved by the real
    resolver — the field's influence ceiling is RECOMMENDATION, so the live decision is never
    operational (``conflict_status="influence_not_operational"``)."""
    ref = _build_column(db, source, table, column)
    _record_evidence(db, ref, "business_term", "Account Balance",
                     EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    resolve_and_project(db, source=source, logical_refs=[ref])
    return SeededColumn(ref, "business_term", "no_value", source, table, column)


# ── conflict: two top-strength evidences that DISAGREE on a governed field ────────────────────────
def seed_conflict(db, *, source: str = "c1fx_conflict", table: str = "accounts",
                  column: str = "balance") -> SeededColumn:
    """``status="conflict"``: two source-ATTESTED ``additivity`` evidences with DISTINCT values tied
    at the top strength — the real resolver records a genuine ``conflict`` decision (it cannot pick
    one value)."""
    ref = _build_column(db, source, table, column)
    _record_evidence(db, ref, "additivity", "additive",
                     EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    _record_evidence(db, ref, "additivity", "non_additive",
                     EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    resolve_and_project(db, source=source, logical_refs=[ref])
    return SeededColumn(ref, "additivity", "conflict", source, table, column)


def _record_resolved_decision(db, logical_ref: str, *, load_bearing: str, now: datetime) -> str:
    """One real RESOLVED decision event via the append-only decision command (mirrors the shipped
    fail-closed suite's ``_record_decision``)."""
    return record_field_decision(
        db, logical_ref=logical_ref, field_name="additivity",
        event_type=FieldDecisionEventType.RESOLVED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]),
        display_value_hash=canonical_hash(load_bearing),
        load_bearing_value_hash=canonical_hash(load_bearing),
        conflict_status="resolved", reason_codes=[],
        field_policy_version=FIELD_POLICY_VERSION, resolver_version=RESOLVER_VERSION,
        actor_ref=None, supersedes_event_id=None, now=now)


# ── fork (GATE 1): a temporal-tie ambiguous head in the decision log ──────────────────────────────
def seed_fork(db, *, source: str = "c1fx_fork", table: str = "accounts",
              column: str = "balance") -> SeededColumn:
    """``status="fork"``: two non-retired RESOLVED ``additivity`` decisions recorded at the SAME
    pinned instant that DISAGREE on their load-bearing value — the head is ambiguous (a violated
    write invariant), so GATE 1 fails closed with ``forked_decision_head``."""
    ref = _build_column(db, source, table, column)
    _record_resolved_decision(db, ref, load_bearing="non_additive", now=_T1)
    _record_resolved_decision(db, ref, load_bearing="additive", now=_T1)
    return SeededColumn(ref, "additivity", "fork", source, table, column)


# ── hash_mismatch (GATE 2): the flat display value tampered out from under the decision ───────────
def seed_hash_mismatch(db, *, source: str = "c1fx_hash_mismatch", table: str = "accounts",
                       column: str = "balance") -> SeededColumn:
    """``status="hash_mismatch"``: a CLEAN resolved ``additivity`` decision (same seeding as
    :func:`seed_resolved`), then the flat ``graph_node`` column is tampered out from under it —
    ``canonical_hash(value)`` no longer matches the decision's ``load_bearing_value_hash``, so
    GATE 2 fails closed with ``value_hash_mismatch``."""
    col = seed_resolved(db, source=source, table=table, column=column)
    db.execute(
        "UPDATE graph_node SET additivity = %s WHERE catalog_source = %s AND object_ref = %s",
        ["tampered_value", source, col.object_ref])
    return SeededColumn(col.logical_ref, "additivity", "hash_mismatch", source, table, column)


# ── projection_unavailable (GATE 3): the load-bearing overlay projection is DEGRADED ──────────────
def seed_projection_unavailable(db, *, source: str = "c1fx_proj_unavailable",
                                table: str = "accounts", column: str = "balance") -> SeededColumn:
    """``status="projection_unavailable"``: a CLEAN resolved column (same seeding as
    :func:`seed_resolved`), then the overlay projection is marked DEGRADED (the marker the store
    runner's ``_mark_degraded`` writes) — GATE 3 refuses to trust ANY downstream read.

    GLOBAL: GATE 3 runs before any per-column read, so while the degradation marker is present
    EVERY ``read_operational_value`` in this database returns ``projection_unavailable``. Seed this
    fixture LAST, or call :func:`clear_projection_unavailable` to restore the other fixtures'
    readability (this fixture's own column then reads ``resolved`` — its seeding is clean)."""
    col = seed_resolved(db, source=source, table=table, column=column)
    db.execute(
        "INSERT INTO projection_degraded "
        "(projection_name, aggregate, aggregate_id, reason, poison_seq) "
        "VALUES (%s, %s, %s, %s, %s)",
        ["overlay", "overlay_fact", "c1-fixture-poison", "poison", 1])
    return SeededColumn(
        col.logical_ref, "additivity", "projection_unavailable", source, table, column)


def clear_projection_unavailable(db) -> None:
    """Remove :func:`seed_projection_unavailable`'s degradation marker: GATE 3 keys off LIVE
    projection health, so every fixture column becomes readable again (the degraded fixture's own
    column reads ``resolved``)."""
    db.execute(
        "DELETE FROM projection_degraded WHERE projection_name = %s AND aggregate_id = %s",
        ["overlay", "c1-fixture-poison"])
