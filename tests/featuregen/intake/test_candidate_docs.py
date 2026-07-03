from featuregen.aggregates._append import provenance_for
from featuregen.contracts.documents import NewDocument, Stage
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.documents.store import append_document, get_document
from featuregen.identity.build import build_human_identity
from featuregen.idgen import mint_id
from featuregen.intake.candidates import (
    CANDIDATES_OUTPUT_SCHEMA_ID,
    CANDIDATES_OUTPUT_SCHEMA_VERSION,
    StubCandidateGenerator,
    generate_candidate_docs,
    register_candidate_schemas,
    write_candidate_docs,
)
from featuregen.intake.llm import LLMResult

OWNER = build_human_identity(subject="user:raj", role_claims=("data_scientist",))

_OUT = {"candidates": [
    {"definition_text": "distinct MCC delta 30d", "rationale": "churn",
     "calculation_method": {"kind": "rolling_aggregate", "aggregation": "distinct_count",
                            "window": "30d", "filter": {"concept": "mcc"}}},
    {"definition_text": "top-category share drift", "rationale": "concentration",
     "calculation_method": {"kind": "ratio", "numerator": "top", "denominator": "total", "window": "30d"}},
    {"definition_text": "JS divergence", "rationale": "distribution shift",
     "calculation_method": {"kind": "distribution_divergence", "measure": "jensen_shannon",
                            "window": "30d", "baseline_window": "180d"}},
]}


class _ScriptedLLM:
    def __init__(self, output, *, status="ok"):
        self.output = output
        self.status = status

    def call(self, request):
        return LLMResult(output=self.output, self_reported_scores={}, call_ref="llmc_1",
                         status=self.status)


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
            body_ref="blob_draft",
        ),
        run_id=run_id,
        request_id=request_id,
        actor=OWNER,
    )
    return doc_id


def test_write_candidate_docs_freezes_candidate_role_draft_docs(db, intent_redactor):
    run_id, request_id = "run_h1", "req_h1"
    draft = _draft_doc(db, run_id, request_id)
    cands = StubCandidateGenerator(_ScriptedLLM(_OUT)).generate(
        {"intake_mode": "hypothesis", "raw_input_classification": "clean"},
        {"concepts": ["mcc", "total", "top"]}, None
    )
    doc_ids = write_candidate_docs(
        db, candidates=cands, draft_doc_id=draft, run_id=run_id, request_id=request_id, actor=OWNER
    )
    assert len(doc_ids) == 3
    rows = db.execute(
        "SELECT branch_role, stage, derived_from, body_classification, run_id "
        "FROM documents WHERE doc_id = ANY(%s)",
        (list(doc_ids),),
    ).fetchall()
    assert {r[0] for r in rows} == {"candidate"}             # candidate branch role (§7.1)
    assert {r[1] for r in rows} == {"DRAFT_CONTRACT"}        # under the run's Draft stage
    assert all(r[2] == [draft] for r in rows)                # DAG-linked derived_from the Draft
    assert {r[3] for r in rows} == {"governance-retained"}   # contract bodies are governance-retained (§4.3)
    assert {r[4] for r in rows} == {run_id}


def test_generate_candidate_docs_orchestrates_generate_then_freeze(db, intent_redactor):
    run_id, request_id = "run_h2", "req_h2"
    draft = _draft_doc(db, run_id, request_id)
    gen = StubCandidateGenerator(_ScriptedLLM(_OUT))
    doc_ids = generate_candidate_docs(
        db, gen, draft={"intake_mode": "hypothesis", "raw_input_classification": "clean"},
        catalog_metadata={"concepts": ["mcc"]},
        domain_context=None, draft_doc_id=draft, run_id=run_id, request_id=request_id, actor=OWNER
    )
    assert len(doc_ids) == 3
    # each candidate body is opaque-by-reference (body_ref + content_hash), never inline (§3.4)
    d = get_document(db, doc_ids[0])
    assert d["body_ref"].startswith("blob_")
    assert d["content_hash"].startswith("sha256:")
    # the frozen blob is a live, governance-retained object-store row
    row = db.execute(
        "SELECT classification, status FROM blob_index WHERE blob_id = %s", (d["body_ref"],)
    ).fetchone()
    assert row == ("governance-retained", "live")


def test_generation_failed_into_clarification_writes_no_docs(db, intent_redactor):
    run_id, request_id = "run_h3", "req_h3"
    draft = _draft_doc(db, run_id, request_id)
    gen = StubCandidateGenerator(_ScriptedLLM({}, status="failed_into_clarification"))
    doc_ids = generate_candidate_docs(
        db, gen, draft={"raw_input_classification": "clean"}, catalog_metadata={}, domain_context=None,
        draft_doc_id=draft, run_id=run_id, request_id=request_id, actor=OWNER
    )
    assert doc_ids == ()  # fail closed → no candidate docs; the run stays in clarification (§7.2)


def test_register_candidate_schemas_makes_output_schema_resolvable(db):
    # Carry-forward (Task-6.3 review): CANDIDATES_OUTPUT_SCHEMA must be registered DURABLY so the real
    # hypothesis path (RecordingLLMClient → call_llm) validates the generation pass output (§9.1).
    reg = DocumentSchemaRegistry(db)
    register_candidate_schemas(reg)
    reg.assert_writable(CANDIDATES_OUTPUT_SCHEMA_ID, CANDIDATES_OUTPUT_SCHEMA_VERSION)
    reg.validate(CANDIDATES_OUTPUT_SCHEMA_ID, CANDIDATES_OUTPUT_SCHEMA_VERSION, _OUT)
    register_candidate_schemas(reg)  # idempotent (ON CONFLICT DO UPDATE)
