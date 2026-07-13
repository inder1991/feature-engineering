"""Task 7 — the PROPOSE wiring.

A STRONG, grain-inferred candidate becomes a governed `approved_join` PROPOSED (folds to DRAFT —
never VERIFIED: the dual human gate stays in charge), with the candidate's self-explaining
evidence pre-minted as an immutable `overlay_evidence` row and threaded through the Command's
`evidence_ref` — the join value schema is `additionalProperties:false`, so evidence can NEVER
ride `proposed_value`. The reviewer sees WHY through `get_task_proposal(...)["evidence"]`
(`metric_values` = score / reason codes / explanation / signals), which is what these tests
assert against — the reviewer read, not the raw payload.

Weak / cardinality-less candidates are NEVER proposed here (grain-gate; they are ledger
diagnostics for Tasks 9/10), and the loop is fail-soft: a propose error is swallowed (counter),
the per-candidate savepoint rolls its writes back, and the NEXT candidate still proposes.
"""
from __future__ import annotations

import json
from dataclasses import asdict, replace

from tests.featuregen.overlay.upload.passc.conftest import _open_join_tasks, _propose_join

from featuregen.overlay.identity import fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.task_read import get_task_proposal
from featuregen.overlay.upload.passc.candidates import block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.lifecycle import (
    build_join_ref,
    candidate_fingerprint,
    unordered_pair,
)
from featuregen.overlay.upload.passc.propose import propose_join_candidates
from featuregen.runtime.observability import counters

_CIF_TERM = "Customer Information File Identifier"


def _c(table, column, **kw):
    b = dict(object_ref=f"src::public.{table}.{column}", table=table, column=column,
             data_type="text", term_name="", term_type="", concept="", synonyms="",
             bian_leaf="", fibo_leaf="", table_entity="", column_entity="",
             data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


def _evidence(a, b):
    pairs = block_candidates([a, b])
    assert len(pairs) == 1, "test setup must yield exactly one blocked pair"
    return score(pairs[0], source_snapshot_id="snap-1")


def _strong_evidence(from_table="transactions", to_table="customers", column="cif_id"):
    """A strong, grain-inferred N:1 candidate: {from_table}.{column} -> {to_table}.{column}."""
    ev = _evidence(_c(from_table, column, term_name=_CIF_TERM),
                   _c(to_table, column, term_name=_CIF_TERM, is_grain=True))
    assert ev.bucket == "strong" and ev.proposed_cardinality == "N:1"
    return ev


def _weak_evidence():
    """NEITHER side is a grain -> MANY_TO_MANY_RISK, forced weak, NO cardinality (scorer rule 1)."""
    ev = _evidence(_c("transactions", "cif_id", term_name=_CIF_TERM),
                   _c("customers", "cif_id", term_name=_CIF_TERM))
    assert ev.bucket == "weak" and ev.proposed_cardinality is None
    return ev


def _ledger_insert(conn, evidence, *, key=None, source="src"):
    """Simulate the Task-10 ledger write: one row per UNORDERED (sorted) column-ref pair."""
    lo, hi = unordered_pair(evidence)
    conn.execute(
        "INSERT INTO pass_c_candidate_evidence (catalog_source, candidate_id,"
        " candidate_fingerprint, from_ref, to_ref, fact_key, proposed_event_id, bucket,"
        " namespace_compatibility, lifecycle, evidence_json, source_snapshot_id, config_version,"
        " candidate_algorithm_version) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
        (source, evidence.candidate_id, candidate_fingerprint(evidence), lo, hi, key, None,
         evidence.bucket, evidence.namespace_compatibility.value, "proposed",
         json.dumps(asdict(evidence)), evidence.source_snapshot_id,
         evidence.config_version, evidence.candidate_algorithm_version))


def _count(conn, table):
    return conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]  # noqa: S608 — test-only


def _counter(name):
    return counters.snapshot()["counters"].get(name, 0)


# ── The happy path: DRAFT + the reviewer evidence round-trip ─────────────────────────────────────


def test_strong_candidate_proposes_draft_not_verified(passc_conn, service_actor):
    ev = _strong_evidence()
    propose_join_candidates(passc_conn, "src", [ev], actor=service_actor)
    key = fact_key(build_join_ref(ev, "src"), "approved_join")
    state = fold_overlay_state(load_fact(passc_conn, key))
    assert state.status == "DRAFT"          # PROPOSED, awaiting the dual human gate — NOT VERIFIED
    # Both-unknown endpoints -> TWO side-labelled platform-admin gate tasks (dual confirmation).
    assert set(_open_join_tasks(passc_conn, key)) == {"from", "to"}


def test_reviewer_sees_candidate_evidence_via_get_task_proposal(passc_conn, service_actor,
                                                                human_admin_1):
    ev = _strong_evidence()
    propose_join_candidates(passc_conn, "src", [ev], actor=service_actor)
    ref = build_join_ref(ev, "src")
    key = fact_key(ref, "approved_join")
    task_id = _open_join_tasks(passc_conn, key)["from"]

    proposal = get_task_proposal(passc_conn, task_id, human_admin_1)
    # The FIXED join payload carries NO candidate evidence (additionalProperties:false) …
    assert set(proposal["proposed_value"]) == {"from_ref", "to_ref", "column_pairs", "cardinality"}
    assert proposal["proposed_value"]["cardinality"] == "N:1"
    # … the evidence rides the pre-minted evidence row, resolved through `evidence_ref`.
    evidence = proposal["evidence"]
    assert evidence is not None and evidence.fact_key == key
    assert evidence.producer == "structural_connector" and evidence.strength == "proposed"
    mv = evidence.metric_values
    assert mv["score"] == ev.score
    assert mv["explanation"] == ev.explanation
    assert mv["namespace_reason_codes"] == list(ev.namespace_reason_codes)
    assert mv["bucket"] == "strong"
    assert {s["signal_name"] for s in mv["positive_signals"]} \
        == {s.signal_name for s in ev.positive_signals}


def test_propose_stamps_the_ledger_row(passc_conn, service_actor):
    ev = _strong_evidence()
    _ledger_insert(passc_conn, ev)                              # Task-10 row, not yet proposed
    propose_join_candidates(passc_conn, "src", [ev], actor=service_actor)
    key = fact_key(build_join_ref(ev, "src"), "approved_join")
    draft_id = fold_overlay_state(load_fact(passc_conn, key)).draft_event_id
    lo, hi = unordered_pair(ev)
    row = passc_conn.execute(
        "SELECT fact_key, proposed_event_id FROM pass_c_candidate_evidence"
        " WHERE catalog_source=%s AND from_ref=%s AND to_ref=%s", ("src", lo, hi)).fetchone()
    assert row == (key, draft_id)


# ── The grain-gate: weak / cardinality-less candidates NEVER propose ─────────────────────────────


def test_weak_candidate_is_not_proposed(passc_conn, service_actor):
    propose_join_candidates(passc_conn, "src", [_weak_evidence()], actor=service_actor)
    assert _count(passc_conn, "human_tasks") == 0
    assert _count(passc_conn, "overlay_evidence") == 0
    assert passc_conn.execute(
        "SELECT count(*) FROM events WHERE aggregate='overlay_fact'").fetchone()[0] == 0


def test_strong_without_cardinality_is_skipped_loud(passc_conn, service_actor):
    # Belt-and-suspenders: the scorer never emits this shape (rule 1 forces non-inferred weak),
    # so a strong+grain-inferred candidate WITHOUT a cardinality is skipped with a counter —
    # an ApprovedJoinRef(cardinality=None) would schema-deny and must never be built.
    ev = replace(_strong_evidence(), proposed_cardinality=None)
    before = _counter("overlay.passc.propose.skipped_no_cardinality")
    propose_join_candidates(passc_conn, "src", [ev], actor=service_actor)
    assert _counter("overlay.passc.propose.skipped_no_cardinality") == before + 1
    assert _count(passc_conn, "human_tasks") == 0
    assert _count(passc_conn, "overlay_evidence") == 0


# ── Dedupe / conflict wiring ─────────────────────────────────────────────────────────────────────


def test_second_run_skips_active_draft(passc_conn, service_actor):
    ev = _strong_evidence()
    propose_join_candidates(passc_conn, "src", [ev], actor=service_actor)
    before = _counter("overlay.passc.propose.skipped_active")
    propose_join_candidates(passc_conn, "src", [ev], actor=service_actor)   # re-ingest dedupe
    assert _counter("overlay.passc.propose.skipped_active") == before + 1
    key = fact_key(build_join_ref(ev, "src"), "approved_join")
    assert len(load_fact(passc_conn, key)) == 1            # ONE draft event, no duplicate
    assert _count(passc_conn, "overlay_evidence") == 1     # no second evidence row minted


def test_conflicting_candidate_is_not_proposed(passc_conn, service_actor):
    ev = _strong_evidence()
    ref = build_join_ref(ev, "src")
    _propose_join(passc_conn, ref, ev)                     # ACTIVE (DRAFT) rival claims the pair
    _ledger_insert(passc_conn, ev, key=fact_key(ref, "approved_join"))
    rival = replace(ev, proposed_cardinality="1:1")        # same pair, DIFFERENT fact_key
    before = _counter("overlay.passc.propose.conflict")
    tasks_before = _count(passc_conn, "human_tasks")
    propose_join_candidates(passc_conn, "src", [rival], actor=service_actor)
    assert _counter("overlay.passc.propose.conflict") == before + 1
    assert _count(passc_conn, "human_tasks") == tasks_before    # no second governed proposal
    assert _count(passc_conn, "overlay_evidence") == 0          # no evidence minted for a conflict


# ── Fail-soft ────────────────────────────────────────────────────────────────────────────────────


def test_propose_error_is_swallowed(passc_conn, service_actor, monkeypatch):
    def boom(conn, cmd):
        raise RuntimeError("propose exploded")
    monkeypatch.setattr("featuregen.overlay.upload.passc.propose.propose_fact", boom)
    before = _counter("overlay.passc.propose.error")
    propose_join_candidates(passc_conn, "src", [_strong_evidence()], actor=service_actor)  # no raise
    assert _counter("overlay.passc.propose.error") == before + 1
    assert _count(passc_conn, "human_tasks") == 0
    # The per-candidate savepoint rolled the pre-minted evidence row back — no orphan on error.
    assert _count(passc_conn, "overlay_evidence") == 0


def test_error_on_one_candidate_does_not_stop_the_next(passc_conn, service_actor):
    bad = replace(_strong_evidence(), from_ref="src::unparseable")   # build_join_ref raises
    good = _strong_evidence(from_table="loans")
    before = _counter("overlay.passc.propose.error")
    propose_join_candidates(passc_conn, "src", [bad, good], actor=service_actor)
    assert _counter("overlay.passc.propose.error") == before + 1
    key = fact_key(build_join_ref(good, "src"), "approved_join")
    assert fold_overlay_state(load_fact(passc_conn, key)).status == "DRAFT"
