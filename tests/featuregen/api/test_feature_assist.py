from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv

from featuregen.intake.llm import FakeLLM, FakeResponse


def _fake() -> FakeLLM:
    return FakeLLM(script={
        # Uploading via a configured client runs ingest enrichment first (ingest_upload ->
        # enrich_concepts/draft_definitions/classify_domains). FakeLLM raises KeyError on an
        # unscripted task, so these must be present or upload_csv 500s before we ever hit assist.
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a business column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance", "description": "average balance per customer",
            "derives_from": ["public.accounts.balance", "public.ghost.col"],
            "aggregation": "avg", "grain_table": "customers"}]}),
        "overlay.feature.recipe": FakeResponse(output={
            "grain_table": "customers", "derives_from": ["public.transactions.amount"],
            "aggregation": "sum", "as_of_column": None, "join_table": "transactions"}),
        "overlay.feature.leakage": FakeResponse(output={"leaks": [
            {"object_ref": "public.accounts.balance", "reason": "target-adjacent"},
            {"object_ref": "public.other.col", "reason": "not in derives_from"}]}),
    })


def _leaky() -> FakeLLM:
    # Same enrichment tasks as _fake (upload runs them first), plus a recommend response whose sole
    # grounded derives-from IS the target column, so the leakage gate must reject it every pass.
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a business column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance", "description": "average balance per customer",
            "derives_from": ["public.accounts.balance"],
            "aggregation": "avg", "grain_table": "customers"}]}),
    })


def test_assist_unconfigured_is_503_not_broken(client):
    for path, body in [
        ("/features/recommend", {"objective": "churn"}),
        ("/features/recipe", {"query": "spend", "catalog_source": "deposits"}),
        ("/features/leakage-check", {"derives_from": [], "target_ref": "x"}),
        ("/features/refine", {"candidate": {"name": "avg_balance"}, "instruction": "tighten it"}),
        ("/features/recommend-sets", {"objective": "churn"}),
    ]:
        res = client.post(path, json=body, headers=AUTH)
        assert res.status_code == 503, path


class _Recording:
    """Wraps a scripted FakeLLM and records every request so tests can assert on the wire inputs."""

    def __init__(self, inner):
        self._inner = inner
        self.requests = []

    def call(self, request):
        self.requests.append(request)
        return self._inner.call(request)


def _refiner() -> FakeLLM:
    return FakeLLM(script={
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary_amount"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a business column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
        "overlay.feature.recommend": FakeResponse(output={"features": [{
            "name": "avg_balance_30d", "description": "30 day average balance",
            "derives_from": ["public.accounts.balance"], "aggregation": "avg_30d",
            "grain_table": "customers", "rationale": "a shorter window reacts faster"}]}),
    })


def test_leakage_check_filters_to_used_refs(make_client):
    client = make_client(llm_client=_fake())
    warnings = client.post("/features/leakage-check",
                           json={"derives_from": ["public.accounts.balance"],
                                 "target_ref": "public.labels.churned"},
                           headers=AUTH).json()["warnings"]
    assert warnings == [{"object_ref": "public.accounts.balance", "reason": "target-adjacent"}]


# ── SE-11 step 5, made unconditional by the E4 cutover: the bypasses are closed ─────────────────

def test_direct_feature_routes_always_refuse(make_client):
    """No public endpoint remains a bypass around typed intent, confirmed scope and semantic
    eligibility — and after E4 there is no mode, flag or catalog shape in which one serves. The
    refusal is typed and names the governed route, and it happens BEFORE any model dispatch, so
    a refused request costs nothing. The ROUTES stay: a 404 would tell a client it had the wrong
    address, when the address is right and the answer is "not this way any more".

    /features/refine is deliberately absent from this list: it plans through the SAME engine
    over one frozen catalog context (its own test below), so it is not a bypass."""
    client = make_client(llm_client=_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    for path, body in (
        ("/features/recommend", {"objective": "o", "catalog_source": "deposits"}),
        ("/features/recommend-sets", {"objective": "o", "catalog_source": "deposits"}),
        ("/features/recipe", {"query": "q", "catalog_source": "deposits"}),
    ):
        res = client.post(path, json=body, headers=AUTH)
        assert res.status_code == 409, (path, res.text)
        assert res.json()["detail"]["code"] == "SEMANTIC_ENFORCED_USE_CONTRACT_PIPELINE", path


def test_refine_without_a_catalog_is_a_typed_422_not_an_empty_answer(make_client):
    """B1's refusal, re-verified after the deletion: the engine plans over ONE frozen catalog
    context, so a refine with no catalog_source is refused BY NAME rather than served an answer
    the binder could not have grounded."""
    res = make_client(llm_client=_fake()).post("/features/refine", json={
        "candidate": {"name": "avg_balance"}, "instruction": "tighten it"}, headers=AUTH)
    assert res.status_code == 422, res.text
    assert res.json()["detail"]["code"] == "SEMANTIC_REQUIRES_CATALOG_SOURCE"


def test_refine_answers_200_when_the_model_invents_a_concept_and_a_class(make_client, conn):
    """T1 regression: this route's contract is that BOTH outcomes are 200 — a revision or a typed
    `rejected`. A model that writes an off-vocabulary `operand_class` is the same model that
    writes an invented concept name, and the vocabulary seam used to dereference the registry's
    None for that pair — an unhandled 500 here, and a silently killed intent lane in gate1."""
    import sys
    sys.path.insert(0, ".")
    from tests.featuregen.api.test_semantic_v1_serving import _bank, _intent_payload

    payload = _intent_payload()
    payload["intents"][0]["operands"] = [
        {"role": "who", "concept": "not_a_registered_concept", "operand_class": "attribute"}]
    _bank(conn)
    res = make_client(llm_client=FakeLLM(script={
        "overlay.feature.intents": FakeResponse(output=payload)})).post(
        "/features/refine", json={
            "candidate": {"name": "activity_recency", "description": "days since last event",
                          "derives_from": [], "aggregation": None, "grain_table": None},
            "instruction": "make it deposits only", "catalog_source": "bank"}, headers=AUTH)
    assert res.status_code == 200, res.text
    rejected = res.json()["rejected"]
    assert rejected["code"] == "INTENT_VOCABULARY_GAP"
    assert "vocabulary gap" in rejected["reason"]


def test_refine_revises_the_meaning_through_the_engine(
        make_client, conn, monkeypatch):
    """B9: refine round-trips under the mode — instruction in, ENGINE-bound revision out with
    typed role bindings; the free-form refine path never dispatches (unscripted => would 500).
    The revised card is a preview: governing requires a whole-round regenerate."""
    import sys
    sys.path.insert(0, ".")
    from tests.featuregen.api.test_semantic_v1_serving import (
        _bank,
        _intent_payload,
    )

    fake = FakeLLM(script={
        "overlay.feature.intents": FakeResponse(output=_intent_payload()),
    })
    _bank(conn)
    res = make_client(llm_client=fake).post("/features/refine", json={
        "candidate": {"name": "activity_recency", "description": "days since last event",
                      "derives_from": [], "aggregation": None, "grain_table": None},
        "instruction": "make it deposits only",
        "catalog_source": "bank",
    }, headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    assert "revised" in body, body
    revised = body["revised"]
    assert revised["generation_source"] == "llm_intent"        # honest origin
    assert revised.get("input_role_bindings"), "the BINDER chose the revised columns"
    assert body["regenerate_to_govern"] is True
