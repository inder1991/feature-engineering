"""Delivery C1 Task 2 — the FAIL-CLOSED verification gates on read_operational_value.

Each test proves a gate FIRES: an ambiguous / tampered / degraded read returns NO operational value
(``value``/``producer``/``strength`` all None) with a distinct ``status`` + machine reason — never a
fabricated authority. The five gate cases plus the preserved happy path:

* GATE 1 fork — a temporal-tie ambiguous head, and a forked supersession chain -> ``status="fork"``.
  A single-head invariant test documents that normal (distinct-timestamp) re-resolution does NOT
  fork, so the guard only fires on a violated write invariant.
* GATE 2 hash — a decision whose stored ``evidence_set_hash`` no longer recomputes, and a tampered
  flat display value whose hash no longer matches ``load_bearing_value_hash`` -> hash_mismatch.
* GATE 3 projection — the overlay projection DEGRADED, and LAGGED -> ``"projection_unavailable"``
  (and clearing the degradation restores ``"resolved"``).
* Happy path — the clean governed additive column STILL returns the full operational value.
"""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.contracts import IdentityEnvelope, NewEvent, ProvenanceEnvelope
from featuregen.events.registry import event_registry
from featuregen.events.store import append_event
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_decision import (
    FieldDecisionEventType,
    read_field_decisions,
    record_field_decision,
)
from featuregen.overlay.field_evidence import (
    canonical_hash,
    field_input_hash,
    record_field_evidence,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import _VALUE_COLUMN, read_column_facts
from featuregen.overlay.upload.field_resolution import (
    _DISPLAY_COLUMN,
    FIELD_POLICY_VERSION,
    RESOLVER_VERSION,
    resolve_and_project,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.operational_facts import (
    _VALUE_IS_DISPLAY_PROJECTION,
    read_operational_value,
)
from featuregen.projections.runner import _checkpoint_seq, _head_seq

_SOURCE = "deposits"
_ROW = CanonicalRow(_SOURCE, "accounts", "balance", "numeric")
_REF = normalize_ref(_SOURCE, None, "accounts", "balance")
_OBJECT_REF = "public.accounts.balance"

_DT1 = datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)
_DT2 = datetime(2026, 1, 1, 12, 0, 1, tzinfo=UTC)
_DT3 = datetime(2026, 1, 1, 12, 0, 2, tzinfo=UTC)


def _seed_evidence(db, field_name, value, producer, strength):
    return record_field_evidence(
        db, logical_ref=_REF, field_name=field_name, proposed_value=value,
        producer=producer, strength=strength, producer_ref="test-producer",
        source_snapshot_id="snap-1",
        input_hash=field_input_hash(logical_ref=_REF, field_name=field_name, material=value))


def _resolve_clean_additive(db):
    """The C1-T1 happy path: a source-attested additivity decision resolved + projected, with
    correct evidence-set and value hashes. read_operational_value returns the full operational
    value."""
    build_graph(db, _SOURCE, [_ROW])
    _seed_evidence(db, "additivity", "non_additive", EvidenceProducer.SOURCE,
                   AssertionStrength.ATTESTED)
    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF])


def _record_decision(db, *, field_name="additivity", now, load_bearing="non_additive",
                     evidence_set_hash=None, selected_evidence_ids=(),
                     event_type=FieldDecisionEventType.RESOLVED, supersedes_event_id=None):
    return record_field_decision(
        db, logical_ref=_REF, field_name=field_name, event_type=event_type,
        selected_evidence_ids=list(selected_evidence_ids),
        evidence_set_hash=evidence_set_hash if evidence_set_hash is not None
        else canonical_hash(list(selected_evidence_ids)),
        display_value_hash=canonical_hash(load_bearing) if load_bearing is not None else None,
        load_bearing_value_hash=canonical_hash(load_bearing) if load_bearing is not None else None,
        conflict_status="resolved", reason_codes=[],
        field_policy_version=FIELD_POLICY_VERSION, resolver_version=RESOLVER_VERSION,
        actor_ref=None, supersedes_event_id=supersedes_event_id, now=now)


def _append_event(db, expected_version: int):
    """Append one real event so the event head (max(global_seq)) advances above the overlay
    checkpoint (which the migration seeds at 0). The 'run' aggregate is a no-op for
    OverlayProjection, so the projection is left LAGGED — exactly the read-time degradation
    gate 3 must catch."""
    event_registry().register_schema("E", 1, {"type": "object"}, owner="o")
    return append_event(
        db,
        NewEvent(
            aggregate="run", aggregate_id="r", type="E", schema_version=1,
            payload={"i": expected_version},
            actor=IdentityEnvelope(subject="u", actor_kind="human", authenticated=True,
                                   auth_method="oidc", role_claims=()),
            provenance=ProvenanceEnvelope(artifact_type="DRAFT_CONTRACT", schema_version=1,
                                          producing_component="t@1"),
            run_id="r"),
        expected_version=expected_version, table_version=1)


# ── Happy path preserved: the clean governed additive column STILL returns the operational value ──
def test_happy_path_still_returns_operational_value(db):
    _resolve_clean_additive(db)

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "resolved"
    assert ov.producer is EvidenceProducer.SOURCE
    assert ov.strength is AssertionStrength.ATTESTED
    # byte-consistent with read_column_facts; the gates did NOT fire on the clean case.
    assert ov.value == read_column_facts(db, _REF, "additivity").value == "non_additive"


# ── GATE 1 (fork): a TEMPORAL TIE at the head between disagreeing non-retired decisions ───────────
def test_fork_temporal_tie_ambiguous_head(db):
    build_graph(db, _SOURCE, [_ROW])
    # Two non-retired RESOLVED heads recorded at the SAME instant (violating the field_decision
    # distinct-per-decision created_at invariant) that DISAGREE on their load-bearing value: the
    # "latest" head is then decided only by an arbitrary decision_event_id tiebreak → ambiguous.
    _record_decision(db, now=_DT1, load_bearing="non_additive")
    _record_decision(db, now=_DT1, load_bearing="additive")

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "fork"
    assert ov.conflict_status == "forked_decision_head"
    assert ov.value is None and ov.producer is None and ov.strength is None


# ── GATE 1 (fork): a FORKED SUPERSESSION CHAIN — one parent superseded by >=2 non-retired branches
def test_fork_supersession_chain(db):
    build_graph(db, _SOURCE, [_ROW])
    parent = _record_decision(db, now=_DT1, load_bearing="non_additive")
    # Two CONFIRMED (non-retired) branches BOTH supersede the same parent — a structural fork the
    # write path never produces (it supersedes a parent at most once, only via a retiring STALED).
    _record_decision(db, now=_DT2, load_bearing="non_additive",
                     event_type=FieldDecisionEventType.CONFIRMED, supersedes_event_id=parent)
    _record_decision(db, now=_DT3, load_bearing="additive",
                     event_type=FieldDecisionEventType.CONFIRMED, supersedes_event_id=parent)

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "fork"
    assert ov.conflict_status == "forked_supersession_chain"
    assert ov.value is None


# ── GATE 1 documentation: normal DISTINCT-timestamp re-resolution is a SINGLE head (no fork) ──────
def test_distinct_timestamp_reresolution_is_single_head(db):
    """A genuine fork cannot arise from normal resolution: the append path stamps a distinct
    created_at per decision, so "latest wins" yields one unambiguous head. Two RESOLVED decisions at
    DISTINCT timestamps must NOT trip the fork guard — the later one is the head, served as live.
    (The guard fires only on a violated write invariant, as the two fork tests above show.)"""
    _resolve_clean_additive(db)
    # A second resolve at a LATER instant appends another RESOLVED (supersedes=None) — the norm.
    resolve_and_project(db, source=_SOURCE, logical_refs=[_REF], now=_DT2)
    decisions = read_field_decisions(db, _REF, "additivity")
    assert len(decisions) >= 2                       # multiple non-superseding decisions by design
    assert len({d.created_at for d in decisions}) == len(decisions)   # all DISTINCT timestamps

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "resolved"                   # single unambiguous head — no fork
    assert ov.value == "non_additive"


# ── GATE 2 (hash): the head's stored evidence_set_hash no longer recomputes ───────────────────────
def test_hash_mismatch_on_stale_evidence_set_hash(db):
    build_graph(db, _SOURCE, [_ROW])
    fev = _seed_evidence(db, "additivity", "non_additive", EvidenceProducer.SOURCE,
                         AssertionStrength.ATTESTED)
    db.execute(
        "UPDATE graph_node SET additivity = %s WHERE catalog_source = %s AND object_ref = %s",
        ["non_additive", _SOURCE, _OBJECT_REF])
    # A RESOLVED head naming the real evidence but carrying a WRONG evidence_set_hash (stale/forged
    # hash): the value hash is correct, so only the evidence-set verification can fail.
    _record_decision(db, now=_DT1, load_bearing="non_additive", selected_evidence_ids=(fev,),
                     evidence_set_hash="0" * 64)

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "hash_mismatch"
    assert ov.conflict_status == "evidence_set_hash_mismatch"
    assert ov.value is None and ov.producer is None
    assert ov.decision_event_id is not None          # audit ref carried, but NO authority served


# ── GATE 2 (hash): a tampered flat display value no longer hashes to the decision's load-bearing ha
def test_hash_mismatch_on_tampered_display_value(db):
    _resolve_clean_additive(db)                       # correct evidence-set + value hashes
    # Tamper ONLY the flat display column, out from under the decision that authorized it.
    db.execute(
        "UPDATE graph_node SET additivity = %s WHERE catalog_source = %s AND object_ref = %s",
        ["tampered_value", _SOURCE, _OBJECT_REF])
    assert read_column_facts(db, _REF, "additivity").value == "tampered_value"

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "hash_mismatch"
    assert ov.conflict_status == "value_hash_mismatch"
    assert ov.value is None


# ── GATE 3 (projection): a DEGRADED overlay projection serves no operational value; clearing restor
def test_projection_degraded_then_cleared(db):
    _resolve_clean_additive(db)
    assert read_operational_value(db, _REF, "additivity").status == "resolved"   # baseline

    # Mark the load-bearing overlay projection degraded (the store runner._mark_degraded writes).
    db.execute(
        "INSERT INTO projection_degraded "
        "(projection_name, aggregate, aggregate_id, reason, poison_seq) "
        "VALUES (%s, %s, %s, %s, %s)",
        ["overlay", "overlay_fact", "poisoned", "poison", 1])

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "projection_unavailable"
    assert "DEGRADED" in ov.conflict_status
    assert ov.value is None and ov.producer is None

    # Clearing the degradation restores the operational value — the gate keyed off live health.
    db.execute("DELETE FROM projection_degraded WHERE projection_name = %s", ["overlay"])
    assert read_operational_value(db, _REF, "additivity").status == "resolved"


# ── GATE 3 (projection): a LAGGED overlay projection (checkpoint behind the event head) fails close
def test_projection_lagged_fails_closed(db):
    _resolve_clean_additive(db)
    assert read_operational_value(db, _REF, "additivity").status == "resolved"   # baseline

    _append_event(db, 0)                               # head → 1, overlay checkpoint stays at 0
    assert _checkpoint_seq(db, "overlay") < _head_seq(db)   # lagged

    ov = read_operational_value(db, _REF, "additivity")
    assert ov.status == "projection_unavailable"
    assert "LAGGED" in ov.conflict_status
    assert ov.value is None


# ── Guard: _VALUE_IS_DISPLAY_PROJECTION stays in sync with the two source mappings it is derived fr
def test_value_hash_scope_matches_derivation():
    """The value-hash gate applies exactly to fields whose read_column_facts flat value column IS
    the resolver's display projection (column names coincide). Assert the hand-maintained constant
    equals that computed intersection so it can never silently drift out of sync."""
    derived = frozenset(
        f for f, col in _VALUE_COLUMN.items() if _DISPLAY_COLUMN.get(f) == col
    )
    assert _VALUE_IS_DISPLAY_PROJECTION == derived
