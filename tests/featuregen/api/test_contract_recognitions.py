"""Phase-1B Task 6 — POST /contract/recognitions.

The recognition endpoint runs the fail-open use-case recognizer over the REDACTED hypothesis/goal
and persists an append-only recognition attempt BEFORE any generation run exists. It is decoupled
from generation: no ``generation_run_id`` is minted and no recipe/applicability count is returned
(applicability owns that, later, after the human commits to generate). A recognizer failure folds to
``status='technical_failure'`` with HTTP 200 — recognition never blocks generation and never 5xxs.
"""
from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import PROVIDER_REFUSAL, FakeLLM, FakeResponse
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK

# A real, selectable LEAF objective — a valid primary the closed-taxonomy validator accepts.
CHURN = "customer.relationship_attrition.churn"

_CLASSIFIED = FakeResponse(output={
    "status": "classified",
    "candidates": [{
        "use_case_id": CHURN, "relationship": "primary", "confidence": "high",
        "evidence_spans": ["churn"], "rationale": "the hypothesis is about customers leaving"}],
    # Phase-2B SOFT dimensions: a governed modelling context + prediction grain the recognizer proposed.
    "modelling_contexts": ["ifrs9"], "target_entity": "customer",
    "ambiguity_note": None})

_UNSCOPED = FakeResponse(output={
    "status": "unscoped", "candidates": [],
    "ambiguity_note": "nothing in the closed taxonomy applies"})

# A provider refusal drives drive_structured_call to fail-into-clarification; recognize folds it to a
# candidate-free TECHNICAL_FAILURE (fail-open) — the endpoint must return 200, never a 5xx.
_REFUSAL = FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)


def _llm(response: FakeResponse) -> FakeLLM:
    # Recognition makes exactly one LLM call, on the recognizer task key — nothing else to script.
    return FakeLLM(script={RECOGNIZER_TASK: response})


def test_recognitions_classified_returns_candidate_and_writes_attempt(make_client, conn):
    client = make_client(_llm(_CLASSIFIED))
    res = client.post("/contract/recognitions", json={
        "hypothesis": "customers churn when their balance drops",
        "objective": "predict churn"}, headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["intent_id"]
    assert body["recognition_id"]
    assert body["status"] == "classified"
    assert body["unscoped"] is False
    assert len(body["candidates"]) == 1
    cand = body["candidates"][0]
    assert cand["use_case_id"] == CHURN
    assert cand["display_name"] == "Churn"      # resolved from the taxonomy display_name
    assert cand["relationship"] == "primary"
    assert cand["confidence"] == "high"
    assert cand["evidence_spans"] == ["churn"]
    # Phase-2B SOFT dimensions surface on the response (proposed to the human at Gate #1, never rejected).
    assert body["modelling_contexts"] == ["ifrs9"]
    assert body["target_entity"] == "customer"
    assert body["warnings"] == []
    # Recognition is decoupled from generation: NO run id, NO recipe/applicability count in the response.
    assert "generation_run_id" not in body
    assert not any(("count" in k) or ("recipe" in k) for k in body)
    # An append-only attempt row was written for this intent (no generation run row is created here).
    n = conn.execute(
        "SELECT count(*) FROM intent_recognition_attempt WHERE intent_id = %s",
        (body["intent_id"],)).fetchone()[0]
    assert n == 1


def test_recognitions_unscoped(make_client):
    client = make_client(_llm(_UNSCOPED))
    res = client.post("/contract/recognitions", json={
        "hypothesis": "forecast quarterly rainfall for the northern region"}, headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "unscoped"
    assert body["unscoped"] is True
    assert body["candidates"] == []


def test_recognitions_recognizer_failure_is_fail_open_200(make_client):
    client = make_client(_llm(_REFUSAL))
    res = client.post("/contract/recognitions", json={
        "hypothesis": "customers churn when their balance drops"}, headers=AUTH)
    # Fail-open: a provider refusal is NOT a 5xx — it folds to a technical_failure result at 200.
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "technical_failure"
    assert body["unscoped"] is True
    assert body["candidates"] == []
    # Fail-open surfaces empty dimensions: no context, no proposed grain, no dimension warnings.
    assert body["modelling_contexts"] == []
    assert body["target_entity"] is None
    assert body["warnings"] == []


def test_recognitions_idempotent_intent_and_single_attempt(make_client, conn):
    client = make_client(_llm(_CLASSIFIED))
    payload = {"hypothesis": "customers churn when their balance drops"}
    a = client.post("/contract/recognitions", json=payload, headers=AUTH)
    b = client.post("/contract/recognitions", json=payload, headers=AUTH)
    assert a.status_code == 200 and b.status_code == 200
    # Re-recognising the same objective reuses the same immutable intent and the same attempt row.
    assert a.json()["intent_id"] == b.json()["intent_id"]
    assert a.json()["recognition_id"] == b.json()["recognition_id"]
    n = conn.execute(
        "SELECT count(*) FROM intent_recognition_attempt WHERE intent_id = %s",
        (a.json()["intent_id"],)).fetchone()[0]
    assert n == 1


def test_recognitions_dedup_is_per_actor(make_client, conn):
    # The intent-dedup is scoped to the REQUESTING actor: two different identities typing the SAME
    # hypothesis must get DIFFERENT immutable intents — actor A's intent is never reused for actor B
    # (which would merge attribution + clobber the considered set + inherit A's target_ref leakage gate).
    client = make_client(_llm(_CLASSIFIED))
    payload = {"hypothesis": "customers churn when their balance drops"}
    alice = {"X-User": "alice", "X-Roles": "platform_admin"}
    bob = {"X-User": "bob", "X-Roles": "platform_admin"}
    a = client.post("/contract/recognitions", json=payload, headers=alice)
    b = client.post("/contract/recognitions", json=payload, headers=bob)
    assert a.status_code == 200 and b.status_code == 200, (a.text, b.text)
    assert a.json()["intent_id"] != b.json()["intent_id"]   # cross-actor: separate intents
    # Each actor's OWN re-recognition still reuses its own intent (idempotent per-actor, unchanged).
    a2 = client.post("/contract/recognitions", json=payload, headers=alice)
    assert a2.json()["intent_id"] == a.json()["intent_id"]
    # Exactly two intent rows for this hypothesis — one per actor.
    n = conn.execute(
        "SELECT count(*) FROM contract_intent WHERE hypothesis = %s",
        (payload["hypothesis"],)).fetchone()[0]
    assert n == 2
