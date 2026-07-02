"""The ONE shared intake test harness (R18) — CREATED by P1; P2/P4/P5/P7 MODIFY/merge (never
`Create`). Holds the autouse schema/command registration + the four canonical collaborator-seam
fixtures (`llm_client`, `intent_redactor`, `candidate_generator`, `intake_catalog`) plus the Phase-4
`intake_env` handle + `_ScriptLLM`. The autouse fixture re-registers SP-0 Phase-06 + SP-2 schemas per
test (the root harness resets the event registry per test), registers the idempotent SP-2 command
catalog, and clears the Phase-4 classifier override + the R10 catalog module-global between tests so
neither leaks. The seams are the R10 canonical module-globals owned by P3 (`current_llm_client`,
`current_intent_redactor`) / P2 (`current_intake_catalog`); each seam fixture imports its module +
double LAZILY so P1's own suite does not depend on the not-yet-built P2/P3/P6 modules."""
import json

import pytest

from featuregen.aggregates._append import append, provenance_for
from featuregen.aggregates.bootstrap import register_phase06_event_schemas
from featuregen.contracts import NewDocument
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.documents.store import append_document, compute_content_hash
from featuregen.events.registry import event_registry
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.idgen import mint_id
from featuregen.intake.banking_catalog import IntakeClassification, IntakeOutcome
from featuregen.intake.catalog import (  # R8/R10 seam (P2, catalog.py)
    _clear_intake_catalog,
    load_banking_catalog_from_seed,
    register_intake_catalog,
)
from featuregen.intake.commands import (
    register_intake_classifier,  # Phase-4-local override of P2's classify_intent (NOT a shared seam)
    register_intake_deps,
    register_sp2_commands,
    reset_intake_seams,
)
from featuregen.intake.contract import register_contract_schemas
from featuregen.intake.events import register_sp2_event_types
from featuregen.intake.llm import LLMResult, register_llm_client  # R10 seam (P3, llm.py)
from featuregen.intake.redaction import (  # R10 seam (P3, redaction.py)
    DefaultIntentRedactor,
    register_intent_redactor,
)
from featuregen.intake.store import append_feature_contract_event as append_fc_event  # R1 seam


@pytest.fixture(autouse=True)
def _register_intake_schemas(db):
    # The event registry is reset per test by the root harness; re-register SP-0 Phase-06 +
    # SP-2 feature_contract event schemas so append_event validation passes. Contract content-
    # schemas go into SP-0's per-connection document registry (Phase 2).
    register_phase06_event_schemas()
    register_sp2_event_types(event_registry())
    register_contract_schemas(DocumentSchemaRegistry(db))
    register_sp2_commands()  # idempotent
    reset_intake_seams()  # clear the Phase-4 classifier override between tests
    register_intake_deps(client=None, redactor=None, catalog=None)  # no Refinement-Loop deps leak in
    yield
    reset_intake_seams()
    register_intake_deps(client=None, redactor=None, catalog=None)  # …nor out (advance_intake/answer)
    _clear_intake_catalog()  # R10 catalog module-global must not leak across tests (Task 2.8 review)


class _ScriptLLM:
    """Minimal in-test LLMClient (Protocol). Returns a scripted LLMResult; can be told to raise
    if .call is ever reached (to prove no payload was dispatched on a fail-closed path)."""

    def __init__(self, output=None, *, scores=None, status="ok", explode=False):
        self._output = output or {}
        self._scores = scores or {}
        self._status = status
        self._explode = explode

    def call(self, request):
        if self._explode:
            raise AssertionError("LLMClient.call must not be reached on a fail-closed path")
        return LLMResult(
            output=self._output, self_reported_scores=self._scores, call_ref="", status=self._status
        )


@pytest.fixture
def intake_env():
    """Wire the R10 canonical collaborator seams (`register_intake_catalog`/`register_intent_redactor`/
    `register_llm_client`) + a stub classifier. Returns a handle a test uses to pin the classification
    outcome, script the LLM, or mark the catalog unavailable (fail-closed park)."""

    class _CatalogSentinel:  # a non-None, versioned catalog; the stub classifier ignores its contents
        version = "bdc-2026.1"

    register_intake_catalog(_CatalogSentinel())        # R8/R10 (P2, catalog.py)
    register_intent_redactor(DefaultIntentRedactor())  # R10 (P3, redaction.py)
    register_intake_classifier(
        lambda intent, *, product=None, region=None, catalog=None: IntakeClassification(
            outcome=IntakeOutcome.CLEAR, catalog_version="bdc-2026.1", reason="intake_env default stub"
        )
    )

    class Handle:
        def pin(self, outcome, *, catalog_version="bdc-2026.1", reason=None, matched_class=None):
            register_intake_classifier(
                lambda intent, *, product=None, region=None, catalog=None: IntakeClassification(
                    outcome=outcome,
                    catalog_version=catalog_version,
                    reason=reason,
                    matched_class=matched_class,
                )
            )

        def script_llm(self, output, **kw):
            register_llm_client(_ScriptLLM(output, **kw))   # R10 (P3, llm.py)

        def drop_catalog(self):
            # unavailable / unversioned reference data → the handler fail-closed parks (§4.5(b));
            # the R10 catalog seam is fail-closed-if-*unset*, so simulate unavailability with an
            # UNVERSIONED catalog rather than unregistering the seam.
            class _Unversioned:
                version = None

            register_intake_catalog(_Unversioned())

    return Handle()


@pytest.fixture
def llm_client():
    """R10 llm seam — register a deterministic FakeLLM (P3 owns FakeLLM + the R19 script form)."""
    from featuregen.intake.llm import FakeLLM, register_llm_client

    client = FakeLLM(script={})
    register_llm_client(client)
    return client


@pytest.fixture
def intent_redactor():
    """R10 redactor seam — register the DefaultIntentRedactor (P3 owns it)."""
    from featuregen.intake.redaction import DefaultIntentRedactor, register_intent_redactor

    redactor = DefaultIntentRedactor()
    register_intent_redactor(redactor)
    return redactor


@pytest.fixture
def candidate_generator():
    """R10 candidate seam — register the StubCandidateGenerator (P6 owns it). The stub now requires an
    injected LLMClient (Task 6.2/6.3): construct it with a scripted _ScriptLLM so the fixture is usable
    (a no-arg StubCandidateGenerator() is broken)."""
    from featuregen.intake.candidates import StubCandidateGenerator, register_candidate_generator

    generator = StubCandidateGenerator(_ScriptLLM({"candidates": []}))
    register_candidate_generator(generator)
    return generator


@pytest.fixture
def intake_catalog():
    """R10 catalog seam — register a BankingDomainCatalog from the seed (P2 owns the loader)."""
    from featuregen.intake.catalog import load_banking_catalog_from_seed, register_intake_catalog

    catalog = load_banking_catalog_from_seed({})
    register_intake_catalog(catalog)
    return catalog


@pytest.fixture
def sp2_schemas(db):
    """The DRAFT/LEDGER/CONFIRMED content-schemas + the CONTRACT_REVIEW structured LLM output-schema
    that call_llm resolves (Task 5.3). register_critique_schemas is imported LAZILY so a critique-module
    import fault confines itself to critique tests rather than the whole intake suite."""
    from featuregen.intake.candidates import register_candidate_schemas
    from featuregen.intake.critique import register_critique_schemas

    registry = DocumentSchemaRegistry(db)
    register_contract_schemas(registry)
    register_critique_schemas(registry)
    register_candidate_schemas(registry)  # durable generate_candidates output-schema (Task-6.3 carry-forward)
    return db


@pytest.fixture
def owner():
    return build_human_identity(subject="user:raj", role_claims=("data_scientist",))


@pytest.fixture
def agent():
    return build_service_identity(
        subject="service:intake-agent", role_claims=("intake-agent",), attestation="sig"
    )


# ── Gate-#1 (Phase-7) test seams: identity constants + contract seed helpers (Task 7.1) ──────────
# Author-self-confirms (§8.2): the eligible Gate-#1 confirmer is the authenticated HUMAN requester
# (the request owner), never a service / the LLM / a second signer. REQUESTER is that owner.
REQUESTER = build_human_identity(
    subject="user:raj", role_claims=("data_scientist",), source_of_authority="oidc:raj"
)
OTHER_DS = build_human_identity(subject="user:mia", role_claims=("data_scientist",))
INTAKE_SVC = build_service_identity(
    subject="service:intake-agent", role_claims=("intake-agent",), attestation="sig"
)

# ── §8.4 confirmation-time prohibited-intent screen (Task 7.4) test seams ─────────────────────────
# The default banking catalog an MCV-validated run was screened under at intake: version bdc-2026.06
# (matching definition_draft's provenance.catalog_version — i.e. NO version drift). Its use-case term
# `card authorization` matches definition_draft's filter concept so the confirmation-time re-screen
# classifies CLEAR; the protected_attribute surface terms it blocks are absent from the draft text.
_DEFAULT_INTAKE_CATALOG_SEED = {
    "catalog_version": "bdc-2026.06",
    "entities": ["customer", "account", "card", "transaction", "application"],
    "data_classes": ["transactions", "balances", "card_authorizations", "protected_attribute"],
    "use_cases": [
        {
            "use_case": "card_authorization",
            "status": "active",
            "target": {"name": "declined_auth"},
            "blocked_data_classes": ["protected_attribute"],
        }
    ],
}


class _StubCatalog:
    """A minimal versioned BankingDomainCatalog stand-in — the §8.4 screen reads only `.version`
    (the classifier itself is monkeypatched in these tests). Used to inject a drifted catalog."""

    def __init__(self, version):
        self.version = version


class _Cls:
    """A minimal IntakeClassification stand-in the monkeypatched classify_intent returns — the §8.4
    screen reads `.outcome`, `.matched_class`, `.catalog_version`."""

    def __init__(self, outcome, catalog_version, *, matched_class=None):
        self.outcome = outcome
        self.catalog_version = catalog_version
        self.matched_class = matched_class


def definition_draft(request_id="req_def", *, intake_mode="definition", risk_flags=()):
    """A post-clarification Draft body (empty open_fields → MCV has passed)."""
    return {
        "request_id": request_id,
        "intake_mode": intake_mode,
        "raw_input_ref": "blob_raw_def",
        "raw_input_classification": "clean",
        "proposed_feature_name": "declined_card_auth_count_90d",
        "feature_semantics": {
            "entity": "customer",
            "entity_grain": ["customer_id", "as_of_date"],
            "observation_intent": {
                "kind": "point_in_time",
                "as_of_field": "as_of_date",
                "rule": "use only data available strictly before as_of_date",
            },
            "calculation_method": "rolling_count",
            "windows": [{"name": "lookback", "value": "90d"}],
            "filters": [
                {
                    "concept": "declined card authorization",
                    "predicate": "card_authorizations.auth_result = 'D'",
                }
            ],
            "target_definition": "N/A (definition-mode feature, no target)",
        },
        "field_scores": {"entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"}},
        "open_fields": [],
        "assumption_ledger_ref": "doc_ledger_def",
        "provenance": {"schema_version": 1, "catalog_version": "bdc-2026.06"},
        "status": "NEEDS_CLARIFICATION",
        "product": "cards",
        "region": "US",
        "risk_flags": list(risk_flags),
    }


def _freeze_draft_doc(db, *, run_id, request_id, body, branch_role="primary", supersedes=()):
    doc_id = mint_id("doc")
    body_bytes = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    append_document(
        db,
        NewDocument(
            doc_id=doc_id,
            stage="DRAFT_CONTRACT",
            schema_version=1,
            branch_role=branch_role,
            content_hash=compute_content_hash(body_bytes),
            body_classification="governance-retained",
            provenance=provenance_for("DRAFT_CONTRACT"),
            body_ref=None,
            supersedes=tuple(supersedes),
        ),
        run_id=run_id,
        request_id=request_id,
        actor=REQUESTER,
    )
    return doc_id


def seed_needs_clarification(db, *, run_id, request_id, draft_body):
    """Seed a run whose contract folds to NEEDS_CLARIFICATION (Draft produced, MCV NOT run)."""
    append(
        db, aggregate="run", aggregate_id=run_id, type="RUN_CREATED",
        payload={"run_id": run_id, "request_id": request_id}, actor=REQUESTER,
        request_id=request_id, run_id=run_id, expected_version=0,
    )
    draft_doc_id = _freeze_draft_doc(db, run_id=run_id, request_id=request_id, body=draft_body)
    append_fc_event(
        db, run_id=run_id, type="INTENT_SUBMITTED",
        payload={
            "request_id": request_id,
            "intake_mode": draft_body["intake_mode"],
            "raw_input_ref": draft_body["raw_input_ref"],
            "raw_input_classification": draft_body["raw_input_classification"],
            # A genuinely MCV-validated contract was screened in-scope: submit_intent persists
            # classification.as_mapping() (R9) on INTENT_SUBMITTED, and MCV check 5 reads it back. Without
            # it a real MCV re-run (e.g. request_edit's re-validation of an edited body) would spuriously
            # fail `classification_unavailable`, so the seed carries the CLEAR mapping the real flow would.
            "classification": {
                "outcome": "CLEAR",
                "catalog_version": draft_body.get("provenance", {}).get("catalog_version", "bdc-2026.06"),
                "matched_class": None,
            },
        },
        actor=REQUESTER, expected_version=0,
    )
    append_fc_event(
        db, run_id=run_id, type="DRAFT_CONTRACT_PRODUCED",
        payload={
            "draft_doc_id": draft_doc_id,
            "assumption_ledger_ref": draft_body["assumption_ledger_ref"],
            "draft_body": draft_body,
        },
        actor=INTAKE_SVC,
    )
    return draft_doc_id


def seed_validated_contract(db, *, run_id, request_id, draft_body, candidate_docs=0):
    """Seed a run whose contract folds to MINIMUM_CONTRACT_VALIDATED (ready for Gate #1).
    Returns (final_draft_doc_id, [candidate_doc_ids])."""
    draft_doc_id = seed_needs_clarification(
        db, run_id=run_id, request_id=request_id, draft_body=draft_body
    )
    cand_ids = [
        _freeze_draft_doc(
            db, run_id=run_id, request_id=request_id, body=draft_body, branch_role="candidate"
        )
        for _ in range(candidate_docs)
    ]
    append_fc_event(
        db, run_id=run_id, type="MINIMUM_CONTRACT_VALIDATED",
        payload={"run_id": run_id}, actor=INTAKE_SVC,
    )
    # An MCV-validated run was screened against an available intake catalog; register the current
    # one (bdc-2026.06 — no drift vs the draft provenance) so the §8.4 confirmation-time re-screen
    # (Task 7.4) has a catalog to read. Tests override it via monkeypatch (drift / unavailable).
    register_intake_catalog(load_banking_catalog_from_seed(_DEFAULT_INTAKE_CATALOG_SEED))
    return draft_doc_id, cand_ids
