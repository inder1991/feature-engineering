"""The ONE shared intake test harness (R18) — CREATED by P1; P2/P4/P5/P7 MODIFY/merge (never
`Create`). Holds the autouse schema/command registration + the four canonical collaborator-seam
fixtures (`llm_client`, `intent_redactor`, `candidate_generator`, `intake_catalog`) plus the Phase-4
`intake_env` handle + `_ScriptLLM`. The autouse fixture re-registers SP-0 Phase-06 + SP-2 schemas per
test (the root harness resets the event registry per test), registers the idempotent SP-2 command
catalog, and clears the Phase-4 classifier override + the R10 catalog module-global between tests so
neither leaks. The seams are the R10 canonical module-globals owned by P3 (`current_llm_client`,
`current_intent_redactor`) / P2 (`current_intake_catalog`); each seam fixture imports its module +
double LAZILY so P1's own suite does not depend on the not-yet-built P2/P3/P6 modules."""
import pytest

from featuregen.aggregates.bootstrap import register_phase06_event_schemas
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.events.registry import event_registry
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.intake.banking_catalog import IntakeClassification, IntakeOutcome
from featuregen.intake.catalog import (  # R8/R10 seam (P2, catalog.py)
    _clear_intake_catalog,
    register_intake_catalog,
)
from featuregen.intake.commands import (
    register_intake_classifier,  # Phase-4-local override of P2's classify_intent (NOT a shared seam)
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
    yield
    reset_intake_seams()
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
    """R10 candidate seam — register the StubCandidateGenerator (P6 owns it)."""
    from featuregen.intake.candidates import StubCandidateGenerator, register_candidate_generator

    generator = StubCandidateGenerator()
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
    from featuregen.intake.critique import register_critique_schemas

    registry = DocumentSchemaRegistry(db)
    register_contract_schemas(registry)
    register_critique_schemas(registry)
    return db


@pytest.fixture
def owner():
    return build_human_identity(subject="user:raj", role_claims=("data_scientist",))


@pytest.fixture
def agent():
    return build_service_identity(
        subject="service:intake-agent", role_claims=("intake-agent",), attestation="sig"
    )
