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


def test_provider_failure_is_not_cached(db):
    """M3: a non-OK provider outcome must not poison the cache — retried next time."""
    from featuregen.intake.llm import PROVIDER_REFUSAL
    rows = [CanonicalRow("deposits", "accounts", "x", "text")]
    fail = FakeLLM(script={_TASK: FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)})
    out = enrich_concepts(db, rows, fail)
    assert out == {}                                      # nothing cached
    assert db.execute("SELECT count(*) FROM enrichment_concept").fetchone()[0] == 0
    # A later OK call succeeds (the failure did not stick).
    ok = FakeLLM(script={_TASK: FakeResponse(output={"concept": "account_identifier"})})
    out2 = enrich_concepts(db, rows, ok)
    assert out2[content_hash(rows[0])] == "account_identifier"


def test_garbage_domain_and_definition_are_rejected(db):
    from featuregen.overlay.upload.enrich import classify_domains, draft_definitions
    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric")]
    listish = FakeLLM(script={
        "overlay.enrich.definition": FakeResponse(output={"definition": "['a', 'b']"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "['Deposits','Payments']"}),
    })
    assert draft_definitions(db, rows, listish) == {}     # list-stringified -> rejected
    assert classify_domains(db, rows, listish) == {}


def test_concept_inputs_exclude_free_text_definition(db):
    """M4: the uploader's free-text definition must not be sent to the LLM."""
    captured = {}

    class _Capture:
        def call(self, request):
            captured["inputs"] = dict(request.inputs)
            from featuregen.intake.llm import LLMResult
            return LLMResult(output={"concept": "monetary_amount"}, self_reported_scores={},
                             call_ref="", status="ok")

    rows = [CanonicalRow("deposits", "accounts", "bal", "numeric",
                         definition="holder SSN 123-45-6789")]   # PII in free text
    enrich_concepts(db, rows, _Capture())
    from featuregen.intake.redaction import INPUT_KEY_CATALOG
    # Inputs are reserved-keyed; the LLM-visible catalog metadata is names/types only — the
    # uploader's free-text definition (and its PII) is nowhere in the outbound payload.
    assert captured["inputs"][INPUT_KEY_CATALOG] == {"table": "accounts", "column": "bal",
                                                     "type": "numeric"}
    assert "123-45-6789" not in str(captured["inputs"])
