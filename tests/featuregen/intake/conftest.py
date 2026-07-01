"""The ONE shared intake test harness (R18) — CREATED by P1; P2/P4/P5/P7 MODIFY/merge (never
`Create`). Holds the autouse event-type registration + the four collaborator-seam fixtures
(`llm_client`, `intent_redactor`, `candidate_generator`, `intake_catalog`). Each seam fixture imports
its module + double LAZILY (inside the body) so P1's own suite — which never requests them — does not
depend on the not-yet-built P2/P3/P6 modules; the owning phase fleshes out the double it registers."""
import pytest

from featuregen.aggregates.events import register_phase06_event_types
from featuregen.events.registry import event_registry
from featuregen.intake.events import register_sp2_event_types


@pytest.fixture(autouse=True)
def _register_intake_event_types():
    # The event registry is reset PER TEST by the root harness (tests/conftest.py::_reset_registry),
    # so — exactly like tests/featuregen/overlay/conftest.py — re-register the SP-2 event schemas AND
    # the phase-06 RUN_*/lifecycle schemas for every intake test so append_event validation passes.
    register_phase06_event_types(event_registry())
    register_sp2_event_types(event_registry())


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
