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

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref


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
