"""D1 persistence contract — deterministic idempotent ids, the complete-set CAS current projection,
the tombstone/unverifiable lifecycle, the reset/rebuild (no LLM), and stale-linked-DRAFT.

Real-DB (the root ``conn`` fixture: a migrated PG connection whose writes roll back on teardown).
Exercises store_projection.py against the 1014 tables directly — no LLM, no fact creation.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.overlay.upload.semantic_bindings.store_projection import (
    DETERMINISTIC_TASK_VERSION,
    CandidateInput,
    SemanticBindingContentConflict,
    mint_candidate_set_id,
    next_attempt_no,
    persist_candidate_set,
    project_current_set,
    rebuild_current_sets,
    stale_orphaned_proposals,
    table_metadata_fingerprint,
)

_T0 = datetime(2026, 7, 20, 12, 0, tzinfo=UTC)
# The D3 LLM producer's task version — a set persisted for audit that must NEVER become current.
_LLM_TASK_VERSION = "d3-select-v1"


def _currency(subject="public.txn.amt", target="public.txn.ccy", disposition="strong",
              input_hash="ih_amt") -> CandidateInput:
    return CandidateInput(
        binding_kind="currency_binding", subject_graph_ref=subject,
        subject_logical_ref=f"src::{subject}", target_graph_ref=target,
        target_logical_ref=f"src::{target}", input_hash=input_hash, disposition=disposition,
        model_version="m1", prompt_version="pv1", schema_version="sv1", config_version="cv1")


def _entity(subject="public.txn.cust", entity_id="customer", input_hash="ih_cust") -> CandidateInput:
    return CandidateInput(
        binding_kind="entity_assignment", subject_graph_ref=subject,
        subject_logical_ref=f"src::{subject}", input_hash=input_hash, disposition="strong",
        proposed_value={"entity_id": entity_id}, model_version="m1", prompt_version="pv1",
        schema_version="sv1", config_version="cv1")


def _persist(conn, *, attempt_no=1, fingerprint="fp1", completion_status="complete",
             candidates=None, run_id="run_1", table="public.txn", created_at=_T0,
             task_version=DETERMINISTIC_TASK_VERSION):
    return persist_candidate_set(
        conn, catalog_source="src", table_graph_ref=table, ingestion_run_id=run_id,
        attempt_no=attempt_no, metadata_input_fingerprint=fingerprint, task_version=task_version,
        prompt_version="pv1", schema_version="sv1", config_version="cv1",
        completion_status=completion_status,
        candidates=[_currency()] if candidates is None else candidates, created_at=created_at)


def _current(conn, table="public.txn"):
    return conn.execute(
        "SELECT candidate_set_id, status FROM current_semantic_binding_candidate_set "
        "WHERE catalog_source = 'src' AND table_graph_ref = %s", (table,)).fetchone()


# ==================================================================================================
# Deterministic ids + idempotent replay
# ==================================================================================================
def test_mint_set_id_is_deterministic() -> None:
    kwargs = dict(ingestion_run_id="run_1", attempt_no=1, catalog_source="src",
                  table_graph_ref="public.txn", metadata_input_fingerprint="fp1", task_version="tv1",
                  prompt_version="pv1", schema_version="sv1", config_version="cv1")
    assert mint_candidate_set_id(**kwargs) == mint_candidate_set_id(**kwargs)
    assert mint_candidate_set_id(**{**kwargs, "attempt_no": 2}) != mint_candidate_set_id(**kwargs)


def test_replaying_same_attempt_is_idempotent(conn) -> None:
    first = _persist(conn)
    assert first.inserted is True
    second = _persist(conn)                              # same attempt, same content
    assert second.inserted is False                      # ON CONFLICT DO NOTHING — no new set
    assert second.candidate_set_id == first.candidate_set_id
    assert conn.execute("SELECT count(*) FROM semantic_binding_candidate_set").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM semantic_binding_candidate").fetchone()[0] == 1


def test_retry_new_attempt_does_not_mutate_prior(conn) -> None:
    first = _persist(conn, attempt_no=1, completion_status="partial")
    assert next_attempt_no(conn, ingestion_run_id="run_1", catalog_source="src",
                           table_graph_ref="public.txn") == 2
    second = _persist(conn, attempt_no=2, completion_status="complete")
    assert second.candidate_set_id != first.candidate_set_id
    # the prior partial attempt is IMMUTABLE — untouched by the retry.
    assert conn.execute("SELECT completion_status FROM semantic_binding_candidate_set "
                        "WHERE candidate_set_id = %s", (first.candidate_set_id,)).fetchone()[0] \
        == "partial"


def test_content_hash_conflict_fails_closed(conn) -> None:
    _persist(conn, candidates=[_currency()])
    # same identity tuple (same attempt) but DIFFERENT candidate content -> fail closed.
    with pytest.raises(SemanticBindingContentConflict):
        _persist(conn, candidates=[_currency(), _entity()])


def _currency_rc(*, reason_codes=(), evidence_json=None, llm_call_ref=None) -> CandidateInput:
    """A currency candidate with a fixed IDENTITY (same candidate_id) but tunable audit columns."""
    return CandidateInput(
        binding_kind="currency_binding", subject_graph_ref="public.txn.amt",
        subject_logical_ref="src::public.txn.amt", target_graph_ref="public.txn.ccy",
        target_logical_ref="src::public.txn.ccy", input_hash="ih_amt", disposition="strong",
        model_version="m1", prompt_version="pv1", schema_version="sv1", config_version="cv1",
        reason_codes=reason_codes, evidence_json=evidence_json or {}, llm_call_ref=llm_call_ref)


def test_content_hash_covers_audit_columns(conn) -> None:
    # M-1: reason_codes / evidence_json / llm_call_ref are now part of the set content_hash, so a
    # same-attempt replay whose audit columns were tampered fails closed (they used to be un-hashed —
    # the candidate_id is identical, so the set would silently keep the original otherwise).
    _persist(conn, candidates=[_currency_rc(reason_codes=("over_bound",))])
    with pytest.raises(SemanticBindingContentConflict):
        _persist(conn, candidates=[_currency_rc(reason_codes=("ambiguous_target",))])   # reason_codes
    with pytest.raises(SemanticBindingContentConflict):
        _persist(conn, candidates=[_currency_rc(reason_codes=("over_bound",),
                                                evidence_json={"signals": ["x"]})])      # evidence
    with pytest.raises(SemanticBindingContentConflict):
        _persist(conn, candidates=[_currency_rc(reason_codes=("over_bound",),
                                                llm_call_ref="llm_1")])                  # llm_call_ref


def test_entity_and_currency_shapes_persist(conn) -> None:
    res = _persist(conn, candidates=[_currency(), _entity()])
    assert len(res.candidate_ids) == 2
    kinds = {r[0] for r in conn.execute(
        "SELECT binding_kind FROM semantic_binding_candidate WHERE candidate_set_id = %s",
        (res.candidate_set_id,)).fetchall()}
    assert kinds == {"currency_binding", "entity_assignment"}


# ==================================================================================================
# CAS current projection + unverifiable lifecycle
# ==================================================================================================
def test_complete_matching_set_becomes_current(conn) -> None:
    res = _persist(conn)
    outcome = project_current_set(conn, catalog_source="src", table_graph_ref="public.txn",
                                  candidate_set_id=res.candidate_set_id, table_fingerprint_now="fp1")
    assert outcome.status == "current"
    assert _current(conn) == (res.candidate_set_id, "current")


def test_partial_set_is_unverifiable(conn) -> None:
    res = _persist(conn, completion_status="partial")
    outcome = project_current_set(conn, catalog_source="src", table_graph_ref="public.txn",
                                  candidate_set_id=res.candidate_set_id, table_fingerprint_now="fp1")
    assert outcome.status == "unverifiable"
    assert _current(conn) == (None, "unverifiable")


def test_changed_metadata_never_keeps_stale_set_current(conn) -> None:
    res = _persist(conn, fingerprint="fp1")
    project_current_set(conn, catalog_source="src", table_graph_ref="public.txn",
                        candidate_set_id=res.candidate_set_id, table_fingerprint_now="fp1")
    assert _current(conn)[1] == "current"
    # the table's metadata has since CHANGED (fp2) — the fp1-authored set is no longer verifiable.
    outcome = project_current_set(conn, catalog_source="src", table_graph_ref="public.txn",
                                  candidate_set_id=res.candidate_set_id, table_fingerprint_now="fp2")
    assert outcome.status == "unverifiable"
    assert _current(conn) == (None, "unverifiable")


def test_project_rejects_foreign_table_set(conn) -> None:
    res = _persist(conn, table="public.txn")
    with pytest.raises(ValueError, match="belongs to"):
        project_current_set(conn, catalog_source="src", table_graph_ref="public.other",
                            candidate_set_id=res.candidate_set_id, table_fingerprint_now="fp1")


# ==================================================================================================
# Tombstone — a complete EMPTY set retires the prior current set
# ==================================================================================================
def test_complete_empty_set_is_a_tombstone(conn) -> None:
    first = _persist(conn, attempt_no=1, candidates=[_currency()])
    project_current_set(conn, catalog_source="src", table_graph_ref="public.txn",
                        candidate_set_id=first.candidate_set_id, table_fingerprint_now="fp1")
    assert _current(conn) == (first.candidate_set_id, "current")
    # a complete EMPTY set (a deliberate "no bindings" outcome) retires the prior set.
    tomb = _persist(conn, attempt_no=2, candidates=[])
    outcome = project_current_set(conn, catalog_source="src", table_graph_ref="public.txn",
                                  candidate_set_id=tomb.candidate_set_id, table_fingerprint_now="fp1")
    assert outcome.status == "current"
    assert _current(conn) == (tomb.candidate_set_id, "current")
    # the current set now has ZERO candidates — the previous candidates are retired.
    assert conn.execute("SELECT count(*) FROM semantic_binding_candidate WHERE candidate_set_id = %s",
                        (tomb.candidate_set_id,)).fetchone()[0] == 0


# ==================================================================================================
# Reset / rebuild — NO LLM
# ==================================================================================================
def _live(*tables, fp="fp1"):
    """The live-fingerprint map ``rebuild_current_sets`` now REQUIRES (I-A) — each table maps to its
    current metadata fingerprint."""
    return {("src", t): fp for t in tables}


def test_rebuild_reconstructs_latest_complete_per_table(conn) -> None:
    old = _persist(conn, attempt_no=1, candidates=[_currency()],
                   created_at=datetime(2026, 7, 20, 10, 0, tzinfo=UTC))
    new = _persist(conn, attempt_no=2, candidates=[_currency(), _entity()],
                   created_at=datetime(2026, 7, 20, 11, 0, tzinfo=UTC))
    # a second table, one complete set.
    other = _persist(conn, table="public.acct", candidates=[_currency(subject="public.acct.bal")],
                     created_at=datetime(2026, 7, 20, 10, 30, tzinfo=UTC))
    # projection loss: the current table is empty. Rebuild from the immutable store alone (no LLM),
    # supplying the live fingerprints that gate each promotion.
    result = rebuild_current_sets(conn, live_fingerprints=_live("public.txn", "public.acct"))
    assert result.tables == 2 and result.projected == 2
    assert _current(conn, "public.txn") == (new.candidate_set_id, "current")   # latest complete wins
    assert _current(conn, "public.acct") == (other.candidate_set_id, "current")
    assert old.candidate_set_id != new.candidate_set_id


def test_rebuild_marks_unverifiable_when_live_fingerprint_moved(conn) -> None:
    res = _persist(conn, fingerprint="fp1")
    result = rebuild_current_sets(conn, live_fingerprints={("src", "public.txn"): "fp2"})
    assert result.unverifiable == 1 and result.projected == 0
    assert _current(conn) == (None, "unverifiable")
    del res


def test_rebuild_requires_live_fingerprint_never_resurrects_unknown(conn) -> None:
    # I-A: a winner whose live fingerprint is UNKNOWN (absent from the map — e.g. a set the re-ingest
    # invalidation had already retired) is projected `unverifiable`, NEVER silently resurrected.
    _persist(conn, fingerprint="fp1")
    result = rebuild_current_sets(conn, live_fingerprints={})   # no live fingerprint for the table
    assert result.tables == 1 and result.projected == 0 and result.unverifiable == 1
    assert _current(conn) == (None, "unverifiable")


def test_rebuild_never_promotes_the_llm_set_to_current(conn) -> None:
    # I-A: the D3 LLM set and the D2 deterministic set share a table + created_at (same ingest tx).
    # Only the deterministic set may become current — the LLM set is filtered out of eligibility
    # entirely, so the same-tx id tie-break can never repoint current at it.
    det = _persist(conn, candidates=[_currency()], task_version=DETERMINISTIC_TASK_VERSION,
                   run_id="run_det", created_at=_T0)
    llm = _persist(conn, candidates=[_currency()], task_version=_LLM_TASK_VERSION,
                   run_id="run_llm", created_at=_T0)
    assert det.candidate_set_id != llm.candidate_set_id
    result = rebuild_current_sets(conn, live_fingerprints=_live("public.txn"))
    assert result.tables == 1 and result.projected == 1     # exactly the deterministic winner
    assert _current(conn) == (det.candidate_set_id, "current")   # NEVER the LLM set


def test_rebuild_fails_closed_on_content_hash_conflict(conn) -> None:
    # a directly-inserted corrupt set (a bogus stored content_hash) simulates tamper/corruption.
    # task_version is the DETERMINISTIC one so the winner is enumerated (else it'd be filtered out).
    set_id = mint_candidate_set_id(
        ingestion_run_id="run_x", attempt_no=1, catalog_source="src", table_graph_ref="public.bad",
        metadata_input_fingerprint="fp1", task_version=DETERMINISTIC_TASK_VERSION,
        prompt_version="pv1", schema_version="sv1", config_version="cv1")
    conn.execute(
        "INSERT INTO semantic_binding_candidate_set (candidate_set_id, catalog_source, "
        "table_graph_ref, ingestion_run_id, attempt_no, metadata_input_fingerprint, task_version, "
        "prompt_version, schema_version, config_version, completion_status, content_hash) "
        "VALUES (%s, 'src', 'public.bad', 'run_x', 1, 'fp1', %s, 'pv1', 'sv1', 'cv1', "
        "'complete', 'bogus-hash')", (set_id, DETERMINISTIC_TASK_VERSION))
    conn.execute(
        "INSERT INTO semantic_binding_candidate (candidate_id, candidate_set_id, catalog_source, "
        "subject_graph_ref, subject_logical_ref, binding_kind, target_graph_ref, "
        "target_logical_ref, disposition, input_hash, model_version, prompt_version, "
        "schema_version, config_version) VALUES ('sbc_x', %s, 'src', 'public.bad.amt', "
        "'src::public.bad.amt', 'currency_binding', 'public.bad.ccy', 'src::public.bad.ccy', "
        "'strong', 'ih', 'm1', 'pv1', 'sv1', 'cv1')", (set_id,))
    # content-hash re-verification runs for every enumerated winner, independent of live fingerprints.
    with pytest.raises(SemanticBindingContentConflict):
        rebuild_current_sets(conn, live_fingerprints=_live("public.bad"))


# ==================================================================================================
# Stale linked DRAFT — VERIFIED untouched, divergence signal survives
# ==================================================================================================
def _fact_state(conn, fact_key, status) -> None:
    conn.execute(
        "INSERT INTO overlay_fact_state (fact_key, object_ref, fact_type, status, updated_seq, "
        "catalog_source) VALUES (%s, 'public.txn.x', 'currency_binding', %s, 1, 'src')",
        (fact_key, status))


def _proposal(conn, candidate_id, fact_key) -> None:
    conn.execute(
        "INSERT INTO semantic_binding_candidate_proposal (candidate_id, fact_key, proposed_event_id) "
        "VALUES (%s, %s, %s)", (candidate_id, fact_key, f"evt_{fact_key}"))


def test_stale_draft_link_removed_verified_untouched(conn) -> None:
    # set1 has two candidates, each later linked to a proposed governed fact.
    res = _persist(conn, attempt_no=1, candidates=[
        _currency(subject="public.txn.amt", input_hash="ih_amt"),
        _currency(subject="public.txn.fee", target="public.txn.ccy", input_hash="ih_fee")])
    draft_cid, verified_cid = res.candidate_ids
    _fact_state(conn, "fk_draft", "DRAFT")
    _fact_state(conn, "fk_verified", "VERIFIED")
    _proposal(conn, draft_cid, "fk_draft")
    _proposal(conn, verified_cid, "fk_verified")
    # set1 becomes current...
    project_current_set(conn, catalog_source="src", table_graph_ref="public.txn",
                        candidate_set_id=res.candidate_set_id, table_fingerprint_now="fp1")
    # ...then a complete EMPTY set supersedes it: both candidates LEAVE the current set.
    tomb = _persist(conn, attempt_no=2, candidates=[])
    project_current_set(conn, catalog_source="src", table_graph_ref="public.txn",
                        candidate_set_id=tomb.candidate_set_id, table_fingerprint_now="fp1")

    result = stale_orphaned_proposals(conn, catalog_source="src", table_graph_ref="public.txn")
    assert result.staled == 1 and result.diverged == 1
    # the DRAFT link is retired; the VERIFIED link SURVIVES (the durable divergence signal).
    assert conn.execute("SELECT 1 FROM semantic_binding_candidate_proposal WHERE candidate_id = %s",
                        (draft_cid,)).fetchone() is None
    assert conn.execute("SELECT 1 FROM semantic_binding_candidate_proposal WHERE candidate_id = %s",
                        (verified_cid,)).fetchone() is not None


def test_stale_is_idempotent(conn) -> None:
    res = _persist(conn, attempt_no=1, candidates=[_currency(input_hash="ih_amt")])
    (cid,) = res.candidate_ids
    _fact_state(conn, "fk_draft", "DRAFT")
    _proposal(conn, cid, "fk_draft")
    tomb = _persist(conn, attempt_no=2, candidates=[])
    project_current_set(conn, catalog_source="src", table_graph_ref="public.txn",
                        candidate_set_id=tomb.candidate_set_id, table_fingerprint_now="fp1")
    assert stale_orphaned_proposals(conn, catalog_source="src",
                                    table_graph_ref="public.txn").staled == 1
    # a second sweep finds nothing to do.
    assert stale_orphaned_proposals(conn, catalog_source="src",
                                    table_graph_ref="public.txn").staled == 0


# ==================================================================================================
# Fingerprint
# ==================================================================================================
def test_fingerprint_is_deterministic_and_versioned() -> None:
    kwargs = dict(table_material={"t": "txn"}, passb_dispositions=[{"grain": "id"}],
                  passc_identifiers=[{"pk": "id"}], shortlist_version="s1", config_version="cv1")
    fp = table_metadata_fingerprint(**kwargs)
    assert fp == table_metadata_fingerprint(**kwargs)          # deterministic
    assert fp.startswith("sbf_")                                # versioned namespace
    assert table_metadata_fingerprint(**{**kwargs, "shortlist_version": "s2"}) != fp
