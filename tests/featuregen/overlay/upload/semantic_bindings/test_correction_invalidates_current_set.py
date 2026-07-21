"""Program-audit G3 / F11 — a four-eyes field correction must be able to invalidate a
semantic-binding CURRENT set.

The D1 invariant is "a set whose premise no longer holds never silently stays current", but the only
invalidation driver was ingest, and the ``sbf-v1`` fingerprint hashes INGESTION-STAGE inputs (Pass-A
maps, upload rows) — a governed correction of a field the D2 shortlist consumed (``concept``) changes
the premise in a way the fingerprint can NEVER observe (an unchanged re-upload cache-replays the same
LLM concept). The correction command itself must therefore flip the affected table's current set to
``unverifiable`` (the immutable set stays as WORM history — the same CAS the ingest I-B wire uses).
"""
from __future__ import annotations

from tests.featuregen._helpers import mint_test_identity

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.field_correction import apply_field_correction, read_field_cas
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.semantic_bindings.store_projection import (
    DETERMINISTIC_TASK_VERSION,
    CandidateInput,
    persist_candidate_set,
    project_current_set,
)

ADMIN_A = mint_test_identity(subject="user:priya", role_claims=("platform-admin",))
ADMIN_B = mint_test_identity(subject="user:sam", role_claims=("platform-admin",))


def _seed_graph(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "balance", "numeric"),
        CanonicalRow("bank", "accounts", "currency", "text")])


def _seed_current_set(db):
    res = persist_candidate_set(
        db, catalog_source="bank", table_graph_ref="public.accounts", ingestion_run_id="run-1",
        attempt_no=1, metadata_input_fingerprint="fp-live",
        task_version=DETERMINISTIC_TASK_VERSION, prompt_version="pv1", schema_version="sv1",
        config_version="cv1", completion_status="complete",
        candidates=[CandidateInput(
            binding_kind="currency_binding", subject_graph_ref="public.accounts.balance",
            subject_logical_ref="bank::public.accounts.balance",
            target_graph_ref="public.accounts.currency",
            target_logical_ref="bank::public.accounts.currency", input_hash="ih-1",
            disposition="strong", model_version="m1", prompt_version="pv1", schema_version="sv1",
            config_version="cv1")])
    outcome = project_current_set(
        db, catalog_source="bank", table_graph_ref="public.accounts",
        candidate_set_id=res.candidate_set_id, table_fingerprint_now="fp-live")
    assert outcome.status == "current"
    return res.candidate_set_id


def _current(db):
    return db.execute(
        "SELECT candidate_set_id, status FROM current_semantic_binding_candidate_set "
        "WHERE catalog_source = 'bank' AND table_graph_ref = 'public.accounts'").fetchone()


def _correct(db, field, action, actor, idem, **kw):
    cas = read_field_cas(db, source="bank", object_ref="public.accounts.balance", field=field)
    res = apply_field_correction(
        db, source="bank", object_ref="public.accounts.balance", field=field, action=action,
        actor=actor, idempotency_key=idem,
        expected_latest_decision_id=cas["latest_decision_id"],
        expected_evidence_set_hash=cas["evidence_set_hash"],
        expected_policy_version=cas["policy_version"], **kw)
    assert res["accepted"] is True, res
    return res


def test_confirmed_concept_correction_flips_the_current_set_unverifiable(db):
    _seed_graph(db)
    set_id = _seed_current_set(db)
    assert _current(db) == (set_id, "current")

    # Four-eyes correction of the shortlist-consumed `concept` on a column of the set's table.
    _correct(db, "concept", "propose_override", ADMIN_A, "c1-p", replacement_value="monetary_stock")
    _correct(db, "concept", "confirm_override", ADMIN_B, "c1-c", replacement_value="monetary_stock")

    current = _current(db)
    assert current is not None and current[1] == "unverifiable" and current[0] is None, (
        "a governed concept correction left the table's semantic-binding candidate set 'current' — "
        "the asset UI keeps serving stale-premise candidates and no later upload can ever flip it "
        f"(current row: {current})")
    # The immutable set itself is HISTORY, never deleted (the WORM store keeps it).
    assert db.execute("SELECT 1 FROM semantic_binding_candidate_set WHERE candidate_set_id = %s",
                      (set_id,)).fetchone() is not None


def test_correction_of_a_field_the_shortlist_never_consumed_keeps_the_set_current(db):
    """Scope pin: a `definition` correction (not a shortlist input) must NOT flip the set."""
    _seed_graph(db)
    set_id = _seed_current_set(db)

    _correct(db, "definition", "propose_override", ADMIN_A, "d1-p", replacement_value="a meaning")
    _correct(db, "definition", "confirm_override", ADMIN_B, "d1-c", replacement_value="a meaning")

    assert _current(db) == (set_id, "current")
