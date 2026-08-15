"""Phase-1B Task 6 — POST /contract/recognitions.

The recognition endpoint runs the fail-open use-case recognizer over the REDACTED hypothesis/goal
and persists an append-only recognition attempt BEFORE any generation run exists. It is decoupled
from generation: no ``generation_run_id`` is minted and no recipe/applicability count is returned
(applicability owns that, later, after the human commits to generate). A recognizer failure folds to
``status='technical_failure'`` with HTTP 200 — recognition never blocks generation and never 5xxs.
"""
from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import PROVIDER_REFUSAL, FakeLLM, FakeResponse
from featuregen.intake.redaction import REDACTION_VERSION
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
    sealed = conn.execute(
        "SELECT input_json, input_content_hash, redaction_policy_version "
        "FROM intent_recognition_attempt WHERE recognition_id = %s",
        (body["recognition_id"],),
    ).fetchone()
    assert sealed[0] == {
        "redacted_hypothesis": "customers churn when their balance drops",
        "redacted_prediction_goal": "predict churn",
        "redaction_policy_version": REDACTION_VERSION,
    }
    assert sealed[1]
    assert sealed[2] == REDACTION_VERSION


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


# ── Task 0 (2026-08-15): request identity — B2, the id that disagreed with its payload ──────────

class _CountingLLM:
    """A FakeLLM that reports how many PHYSICAL provider requests it served.

    Counting rows in ``llm_call`` would not do: the audit write is best-effort and may land on a
    separate connection, so an absent row proves nothing about whether the provider was called."""

    def __init__(self, *responses: FakeResponse) -> None:
        self._inner = FakeLLM(script={RECOGNIZER_TASK: list(responses)})
        self.calls = 0

    def call(self, request):
        self.calls += 1
        return self._inner.call(request)


def test_the_returned_recognition_id_is_the_row_the_response_was_built_from(make_client, conn):
    """B2, pinned. Recognition persistence was idempotent on ``(intent_id, input_hash)`` where
    ``input_hash`` covered ONLY the redacted user input — so a re-run called the provider AGAIN, got
    a DIFFERENT answer, and ``ON CONFLICT DO NOTHING`` handed back the FIRST row's id. The response
    was built from the new in-memory result while confirmation and provenance read the old row.

    On the live incident that meant a screen could say "Churn" over an id whose stored status is
    ``technical_failure``. The invariant, stated so it cannot regress: the id in the response names
    the row the response was built from."""
    llm = _CountingLLM(_REFUSAL, _CLASSIFIED)
    client = make_client(llm)
    payload = {"hypothesis": "customers churn when their balance drops",
               "objective": "predict churn in the next 90 days"}
    first = client.post("/contract/recognitions", json=payload, headers=AUTH)
    second = client.post("/contract/recognitions", json=payload, headers=AUTH)
    assert first.status_code == 200 and second.status_code == 200, (first.text, second.text)
    body = second.json()
    stored = conn.execute(
        "SELECT status FROM intent_recognition_attempt WHERE recognition_id = %s",
        (body["recognition_id"],)).fetchone()
    assert stored is not None, "the returned recognition_id names no row at all"
    assert stored[0] == body["status"], (
        "the returned recognition_id points at a row whose stored status disagrees with the "
        f"payload: stored={stored[0]!r}, served={body['status']!r}")


def test_an_identical_request_returns_the_stored_result_without_calling_the_provider(make_client):
    """The same objective, recognised twice under the same contract, is ONE provider call. Today's
    second call is a silent double-spend whose answer is then thrown away."""
    llm = _CountingLLM(_CLASSIFIED)
    client = make_client(llm)
    payload = {"hypothesis": "customers churn when their balance drops",
               "objective": "predict churn in the next 90 days"}
    first = client.post("/contract/recognitions", json=payload, headers=AUTH)
    assert first.status_code == 200, first.text
    assert llm.calls == 1
    second = client.post("/contract/recognitions", json=payload, headers=AUTH)
    assert second.status_code == 200, second.text
    assert llm.calls == 1, "an identical request re-called the provider"
    assert second.json() == first.json()


def test_a_changed_prompt_or_model_is_a_new_request(make_client, conn, monkeypatch):
    """Same user text, different recognition contract → a NEW attempt. The model is one leg of the
    request identity: the same words classified by a different model are a different question, and
    the answer to one may not be served as the answer to the other."""
    llm = _CountingLLM(_CLASSIFIED)
    client = make_client(llm)
    payload = {"hypothesis": "customers churn when their balance drops",
               "objective": "predict churn in the next 90 days"}
    first = client.post("/contract/recognitions", json=payload, headers=AUTH)
    assert first.status_code == 200, first.text
    monkeypatch.setenv("FEATUREGEN_LLM_MODEL", "claude-opus-4-8")
    second = client.post("/contract/recognitions", json=payload, headers=AUTH)
    assert second.status_code == 200, second.text
    assert second.json()["intent_id"] == first.json()["intent_id"]   # same immutable intent
    assert second.json()["recognition_id"] != first.json()["recognition_id"]
    assert llm.calls == 2
    rows = conn.execute(
        "SELECT recognizer_model_id FROM intent_recognition_attempt WHERE intent_id = %s "
        "ORDER BY created_at",
        (first.json()["intent_id"],)).fetchall()
    assert [r[0] for r in rows] == ["claude-sonnet-5", "claude-opus-4-8"]


def test_the_served_result_and_its_audit_row_are_one_fact(make_client, conn):
    """``llm_call_ref`` on the attempt itself. Before this, the answer lived in one table and the
    evidence for it in another, joined by nothing — so "which call produced the row this scope was
    confirmed from?" had no answer at all."""
    client = make_client(_llm(_CLASSIFIED))
    res = client.post("/contract/recognitions", json={
        "hypothesis": "customers churn when their balance drops",
        "objective": "predict churn in the next 90 days"}, headers=AUTH)
    assert res.status_code == 200, res.text
    row = conn.execute(
        "SELECT recognition_request_hash, input_hash, input_content_hash, llm_call_ref "
        "FROM intent_recognition_attempt WHERE recognition_id = %s",
        (res.json()["recognition_id"],)).fetchone()
    assert row[0] and row[0] == row[1]          # the request identity IS the idempotency key
    assert row[2] and row[2] != row[0]          # the sealed user input keeps its own, narrower hash
    assert row[3], "the attempt does not name the audited call that produced it"
    task = conn.execute(
        "SELECT task FROM llm_call WHERE llm_call_ref = %s", (row[3],)).fetchone()
    assert task is not None and task[0] == RECOGNIZER_TASK
