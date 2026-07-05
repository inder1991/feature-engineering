from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash, enrich_concepts

_TASK = "overlay.enrich.concept"


class _NeverCalledLLM:
    def call(self, request):
        raise AssertionError("LLM must not be called on a cache hit")


def test_classifies_and_caches(db):
    rows = [CanonicalRow("deposits", "accounts", "balance", "numeric", definition="ledger balance")]
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "monetary_amount"})})
    out = enrich_concepts(db, rows, client)
    assert out[content_hash(rows[0])] == "monetary_amount"
    # Cached: a second run with a client that would raise is never called.
    cached = enrich_concepts(db, rows, _NeverCalledLLM())
    assert cached[content_hash(rows[0])] == "monetary_amount"


def test_unknown_concept_falls_back_to_unclassified(db):
    rows = [CanonicalRow("deposits", "accounts", "weird", "text")]
    client = FakeLLM(script={_TASK: FakeResponse(output={"concept": "totally_made_up"})})
    out = enrich_concepts(db, rows, client)
    assert out[content_hash(rows[0])] == "unclassified"


def test_drafts_definition_only_when_blank(db):
    from featuregen.overlay.upload.enrich import draft_definitions
    rows = [
        CanonicalRow("deposits", "accounts", "bal", "numeric"),                        # blank -> drafted
        CanonicalRow("deposits", "accounts", "id", "integer", definition="account id"),  # declared -> skipped
    ]
    client = FakeLLM(script={"overlay.enrich.definition":
                             FakeResponse(output={"definition": "the account ledger balance"})})
    out = draft_definitions(db, rows, client)
    assert out[content_hash(rows[0])] == "the account ledger balance"
    assert content_hash(rows[1]) not in out   # declared definition is never overwritten (R3)


def test_classifies_domain_per_table(db):
    from featuregen.overlay.upload.enrich import classify_domains
    rows = [
        CanonicalRow("deposits", "accounts", "id", "integer"),
        CanonicalRow("deposits", "accounts", "balance", "numeric"),
    ]
    client = FakeLLM(script={"overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"})})
    out = classify_domains(db, rows, client)
    assert out["accounts"] == "Deposits"
