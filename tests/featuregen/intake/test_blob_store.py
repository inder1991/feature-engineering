"""F1 (P1-b / P2-c) — the write-once blob store makes minted refs durably resolvable:
  * a candidate document's `body_ref` round-trips its exact body (candidate bodies are NOT
    event-inlined, so this is the ONLY resolver);
  * submit_intent's `raw_input_ref` resolves to the raw intent (audit-of-record / replay);
  * the store is write-once (differing re-write rejected; UPDATE/DELETE physically blocked)."""

import json

import pytest
from psycopg.types.json import Jsonb
from tests.featuregen._helpers import mint_test_identity

from featuregen.aggregates._append import provenance_for
from featuregen.contracts import Command
from featuregen.contracts.documents import NewDocument, Stage
from featuregen.documents.store import append_document, get_document
from featuregen.events.store import load_stream
from featuregen.idgen import mint_id
from featuregen.intake.blobs import BlobConflictError, read_blob, write_blob
from featuregen.intake.candidates import Candidate, write_candidate_docs
from featuregen.intake.commands import submit_intent
from featuregen.intake.events import INTENT_SUBMITTED

OWNER = mint_test_identity(subject="user:raj", role_claims=("data_scientist",))


# ── the store itself ────────────────────────────────────────────────────────────────────────────
def test_write_blob_round_trips_exact_content(db):
    write_blob(db, "blob_rt", {"definition_text": "x", "nested": {"n": [1, 2, 3]}})
    assert read_blob(db, "blob_rt") == {"definition_text": "x", "nested": {"n": [1, 2, 3]}}


def test_read_blob_unknown_returns_none(db):
    assert read_blob(db, "blob_nope") is None


def test_write_blob_idempotent_on_identical_content(db):
    write_blob(db, "blob_idem", {"a": 1})
    write_blob(db, "blob_idem", {"a": 1})  # no-op, no error
    assert read_blob(db, "blob_idem") == {"a": 1}


def test_write_blob_rejects_differing_rewrite(db):
    write_blob(db, "blob_wo", {"a": 1})
    with pytest.raises(BlobConflictError):
        write_blob(db, "blob_wo", {"a": 2})  # different content under the same ref → rejected
    assert read_blob(db, "blob_wo") == {"a": 1}  # original preserved


def test_blob_store_rejects_update(db):
    write_blob(db, "blob_u", {"a": 1})
    with pytest.raises(Exception):  # plpgsql RAISE EXCEPTION from blob_no_mutation
        db.execute("UPDATE blob SET content = %s WHERE blob_ref = 'blob_u'", (Jsonb({"a": 9}),))


def test_blob_store_rejects_delete(db):
    write_blob(db, "blob_d", {"a": 1})
    with pytest.raises(Exception):  # plpgsql RAISE EXCEPTION from blob_no_mutation
        db.execute("DELETE FROM blob WHERE blob_ref = 'blob_d'")


# ── P1-b: candidate document body is durably resolvable by body_ref ───────────────────────────────
def _draft_doc(db, run_id, request_id):
    doc_id = mint_id("doc")
    append_document(
        db,
        NewDocument(
            doc_id=doc_id,
            stage=Stage.DRAFT_CONTRACT.value,
            schema_version=1,
            branch_role="primary",
            content_hash="sha256:draft",
            body_classification="governance-retained",
            provenance=provenance_for(artifact_type="DRAFT_CONTRACT"),
            body_ref="blob_draft_seed",
        ),
        run_id=run_id,
        request_id=request_id,
        actor=OWNER,
    )
    return doc_id


def test_candidate_body_ref_resolves_to_exact_body(db):
    run_id, request_id = "run_b1", "req_b1"
    draft = _draft_doc(db, run_id, request_id)
    cand = Candidate(
        candidate_id="cand_1",
        definition_text="distinct MCC delta 30d",
        rationale="churn",
        calculation_method={
            "method_version": 1,
            "chosen": {"kind": "rolling_aggregate", "aggregation": "distinct_count",
                       "window": "30d", "filter": {"concept": "mcc"}},
            "considered": [{"kind": "rolling_aggregate"}],
        },
        signals={"heuristic_rank": 0.9},
        provenance={"llm_call_refs": ["llmc_1"], "generator_version": "sp2-stub@1"},
    )
    (doc_id,) = write_candidate_docs(
        db, candidates=[cand], draft_doc_id=draft, run_id=run_id, request_id=request_id, actor=OWNER
    )
    body_ref = get_document(db, doc_id)["body_ref"]
    assert body_ref.startswith("blob_")
    # the candidate body (NOT event-inlined) round-trips EXACTLY via read_blob
    assert read_blob(db, body_ref) == {
        "request_id": request_id,
        "candidate_id": cand.candidate_id,
        "definition_text": cand.definition_text,
        "rationale": cand.rationale,
        "calculation_method": cand.calculation_method,
        "signals": cand.signals,
        "provenance": cand.provenance,
    }


# ── P2-c: submit_intent's raw_input_ref resolves to the raw intent ────────────────────────────────
_INTENT = "90-day rolling count of declined card authorizations per customer"

_DEFINITION_OUTPUT = {
    "request_id": "ECHO", "intake_mode": "definition", "raw_input_ref": "blob_echo",
    "raw_input_classification": "clean", "assumption_ledger_ref": "doc_echo",
    "status": "NEEDS_CLARIFICATION", "provenance": {"schema_version": 1},
    "proposed_feature_name": "declined_card_auth_count_90d",
    "feature_semantics": {
        "entity": "customer", "entity_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date",
                               "rule": "use only data available strictly before as_of_date"},
        "calculation_method": "rolling_count",
        "windows": [{"name": "lookback", "value": "90d"}],
        "filters": [{"concept": "declined card authorization", "predicate": "UNKNOWN"}],
        "target_definition": "N/A (definition-mode feature, no target)",
    },
    "field_scores": {"entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"}},
    "open_fields": ["filters.declined_status_encoding"],
    "open_questions": [{"field": "filters.declined_status_encoding",
                        "question": "Which column/value marks a declined authorization?",
                        "ambiguity": 0.80, "confidence": 0.40, "blocks_progress": True,
                        "routed_to": "human"}],
    "assumptions": [],
}


def test_submit_intent_raw_input_ref_resolves_to_raw_intent(db, intake_env):
    intake_env.script_llm(_DEFINITION_OUTPUT)
    res = submit_intent(
        db,
        Command(
            "submit_intent", "feature_contract", None,
            {"intent_text": _INTENT, "intake_mode": "definition", "raw_input_classification": "clean"},
            OWNER, "k_blob",
        ),
    )
    assert res.accepted is True, res.denied_reason
    submitted = next(e for e in load_stream(db, "feature_contract", res.aggregate_id)
                     if e.type == INTENT_SUBMITTED)
    raw_input_ref = submitted.payload["raw_input_ref"]
    assert raw_input_ref.startswith("blob_")
    # the raw intent (held BY REFERENCE, never inlined) is the audit-of-record — resolvable by ref
    assert read_blob(db, raw_input_ref) == {"raw_input": _INTENT}
    # …and it was NOT inlined into the event payload (§9.4)
    assert _INTENT not in json.dumps(submitted.payload)
    # N12 — the raw intent is tracked in blob_index as the ERASABLE-PII class (crypto-shred / GC eligible),
    # referenced + live — never untracked (which replay would misclassify as 'shredded').
    row = db.execute(
        "SELECT classification, status, referenced FROM blob_index WHERE blob_id=%s", (raw_input_ref,)
    ).fetchone()
    assert row is not None
    assert row[0] == "pii-erasable" and row[1] == "live" and row[2] is True
