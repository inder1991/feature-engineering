"""Delivery F0 Task 1 — the per-column CAPABILITY MATRIX readiness diagnostic.

Proves ``column_readiness`` reports FIVE independent capabilities over one column (a column may be
ready as a grain key yet blocked as a measure), with each requirement's status/authority/provenance
SOURCED from C1 (:func:`operational_facts.read_operational_value`) — never re-derived. Also proves
the diagnostic-PREVIEW vs blocking distinction: an external-check preview (``TYPE_IS_NUMERIC``,
``CURRENCY_CONSISTENT``, ...) is advisory (never a fabricated pass, never a blocker), while a
capability a column plainly cannot serve is ``blocked`` with a clear reason (not an error).

Seeding mirrors the shipped suites: the real resolver (``resolve_and_project`` over seeded
``field_evidence``) for governed decision fields, and a direct graph_node write for the
SPECIALIZED_FACT (grain / availability) provenance, exactly as test_operational_facts does.
"""
from __future__ import annotations

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.column_readiness import (
    ColumnCapability,
    ColumnReadiness,
    ColumnRequirement,
    column_readiness,
)
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.operational_facts import read_operational_value

_SOURCE = "deposits"


def _seed(db, ref, field_name, value, producer, strength):
    record_field_evidence(
        db, logical_ref=ref, field_name=field_name, proposed_value=value,
        producer=producer, strength=strength, producer_ref="test-producer",
        source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=ref, field_name=field_name, material=value))


def _set_col(db, object_ref, **cols):
    assignments = ", ".join(f"{k} = %s" for k in cols)
    db.execute(
        f"UPDATE graph_node SET {assignments} WHERE catalog_source = %s AND object_ref = %s",
        [*cols.values(), _SOURCE, object_ref])


def _req(cap: ColumnCapability, req_id: str) -> ColumnRequirement:
    for r in cap.requirements:
        if r.requirement_id == req_id:
            return r
    raise AssertionError(
        f"no requirement {req_id!r} in {cap.use}: {[r.requirement_id for r in cap.requirements]}")


def _has(cap: ColumnCapability, req_id: str) -> bool:
    return any(r.requirement_id == req_id for r in cap.requirements)


# 1. A governed grain column (is_grain + grain_fact_event_id) -> as_grain_key READY (grain confirmed);
#    the same varchar key plainly cannot be a measure -> as_measure BLOCKED (not an error).
def test_governed_grain_column_ready_as_grain_blocked_as_measure(db):
    build_graph(db, _SOURCE, [CanonicalRow(_SOURCE, "accounts", "acct_id", "varchar")])
    _set_col(db, "public.accounts.acct_id", is_grain=True, grain_fact_event_id="evt_grain_1")

    cr = column_readiness(db, source=_SOURCE, object_ref="public.accounts.acct_id")
    assert isinstance(cr, ColumnReadiness)

    assert cr.as_grain_key.operational_status == "ready"
    grain = _req(cr.as_grain_key, "grain")
    assert grain.status == "confirmed"
    assert grain.blocking is False
    assert grain.authority == "governed"
    assert grain.fact_event_id == "evt_grain_1"

    # as_measure: a varchar key is positively non-numeric -> a clear blocking reason, NOT an error.
    assert cr.as_measure.operational_status == "blocked"
    otype = _req(cr.as_measure, "operational_type")
    assert otype.blocking is True
    assert otype.reason.startswith("operational_type_not_numeric")


# 2. A governed ADDITIVE NUMERIC measure -> additivity + operational_type confirmed; a monetary one
#    previews CURRENCY_CONSISTENT (advisory, never blocking).
def test_governed_additive_numeric_monetary_measure(db):
    build_graph(db, _SOURCE, [CanonicalRow(_SOURCE, "accounts", "balance", "numeric")])
    ref = normalize_ref(_SOURCE, None, "accounts", "balance")
    _seed(db, ref, "additivity", "additive", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    _seed(db, ref, "logical_representation", "numeric",
          EvidenceProducer.PARSER, AssertionStrength.SUPPORTED)
    resolve_and_project(db, source=_SOURCE, logical_refs=[ref])
    _set_col(db, "public.accounts.balance", currency="USD")   # makes it a monetary measure

    cr = column_readiness(db, source=_SOURCE, object_ref="public.accounts.balance")

    additivity = _req(cr.as_measure, "additivity")
    assert additivity.status == "confirmed"
    assert additivity.authority == "governed"

    otype = _req(cr.as_measure, "operational_type")
    assert otype.status == "confirmed"
    assert otype.blocking is False

    currency = _req(cr.as_measure, "external:CURRENCY_CONSISTENT")
    assert currency.external_preview is True
    assert currency.status == "review"
    assert currency.blocking is False


# 3. An operational-UNKNOWN type column -> a TYPE_IS_NUMERIC external-check PREVIEW (review), NOT a
#    fabricated operational_type pass.
def test_unknown_type_previews_type_is_numeric_not_a_pass(db):
    build_graph(db, _SOURCE, [CanonicalRow(_SOURCE, "accounts", "memo", UNKNOWN_TYPE)])

    cr = column_readiness(db, source=_SOURCE, object_ref="public.accounts.memo")
    preview = _req(cr.as_measure, "external:TYPE_IS_NUMERIC")
    assert preview.external_preview is True
    assert preview.status == "review"
    assert preview.blocking is False
    assert preview.authority == "external_check"
    # NOT a fabricated pass: there is NO confirmed operational_type requirement standing in for it.
    assert not _has(cr.as_measure, "operational_type")


# 4. An as-of / event-time column (governed availability fact) -> as_event_time event-time confirmed;
#    the temporal external checks are advisory previews.
def test_governed_availability_column_as_event_time(db):
    build_graph(db, _SOURCE, [CanonicalRow(_SOURCE, "events", "occurred_at", "timestamp")])
    _set_col(db, "public.events.occurred_at", is_as_of=True,
             availability_fact_event_id="evt_av_1")

    cr = column_readiness(db, source=_SOURCE, object_ref="public.events.occurred_at")
    event_time = _req(cr.as_event_time, "event_time")
    assert event_time.status == "confirmed"
    assert event_time.authority == "governed"
    assert event_time.fact_event_id == "evt_av_1"
    assert event_time.blocking is False
    assert cr.as_event_time.operational_status == "ready"

    assert _req(cr.as_event_time, "external:TEMPORAL_IS_POPULATED").external_preview is True
    assert _req(cr.as_event_time, "external:TEMPORAL_LAG_BOUNDED").blocking is False


# 5. A plain TEXT column -> as_measure AND as_grain_key blocked with clear reasons (never errors).
def test_plain_text_column_blocked_with_clear_reasons(db):
    build_graph(db, _SOURCE, [CanonicalRow(_SOURCE, "customers", "notes", "text")])

    cr = column_readiness(db, source=_SOURCE, object_ref="public.customers.notes")

    assert cr.as_measure.operational_status == "blocked"
    otype = _req(cr.as_measure, "operational_type")
    assert otype.blocking is True
    assert otype.reason.startswith("operational_type_not_numeric")

    assert cr.as_grain_key.operational_status == "blocked"
    grain = _req(cr.as_grain_key, "grain")
    assert grain.blocking is True
    assert grain.status == "missing"
    assert grain.reason == "grain_no_verified_fact"   # a clear reason, not an exception


# 6. The requirement's status/authority/provenance come STRAIGHT from C1's read_operational_value —
#    never re-derived (the guard invariant: the diagnostic reuses C1, it does not reimplement it).
def test_requirement_authority_sourced_from_c1(db):
    build_graph(db, _SOURCE, [CanonicalRow(_SOURCE, "accounts", "balance", "numeric")])
    ref = normalize_ref(_SOURCE, None, "accounts", "balance")
    _seed(db, ref, "additivity", "non_additive", EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
    resolve_and_project(db, source=_SOURCE, logical_refs=[ref])

    ov = read_operational_value(db, ref, "additivity")
    cr = column_readiness(db, source=_SOURCE, object_ref="public.accounts.balance")
    additivity = _req(cr.as_measure, "additivity")

    assert additivity.c1_status == ov.status == "resolved"
    assert additivity.evidence_ids == tuple(ov.selected_evidence_ids)
    assert additivity.decision_event_id == ov.decision_event_id
    assert additivity.authority == "governed"
    assert additivity.status == "confirmed"
