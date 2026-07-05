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
