"""The authoring route: propose a filled form, then register what the person submits.

Search runs BESIDE the proposal in a separate key — an existing label is a decision the
organisation already made, a draft is a draft — and the SENTENCE is renderable before submitting,
because a person approving twelve JSON fields is rubber-stamping.
"""
from tests.featuregen.api._helpers import AUTH, DEPOSITS_CSV, upload_csv

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.target_draft import TARGET_DRAFT_TASK

_GRAIN = "public.accounts.id"
_ASOF = "public.accounts.posted_at"
_FLAG = "public.accounts.balance"


def _enrichment() -> dict:
    return {
        "overlay.enrich.concept": FakeResponse(output={"concept": "monetary"}),
        "overlay.enrich.definition": FakeResponse(output={"definition": "a column"}),
        "overlay.enrich.domain": FakeResponse(output={"domain": "Deposits"}),
    }


def _draft_fake() -> FakeLLM:
    return FakeLLM(script={**_enrichment(), TARGET_DRAFT_TASK: FakeResponse(output={
        "shape": "state_change",
        "fields": {"name": "tgt_npe_90d", "column_ref": _FLAG, "window_days": 90,
                   "as_of_frequency": "monthly", "label_type": "binary",
                   "operator": ">=", "threshold": 1},
        "needs_input": ["from_values", "to_values"],
        "notes": {"from_values": "no_value_profile", "to_values": "no_value_profile"}})})


def _no_draft_fake() -> FakeLLM:
    """Enrichment scripted, the draft task NOT — the proposer's technical-failure path."""
    return FakeLLM(script=_enrichment())


def _entity_of(client) -> str:
    """Whatever this uploaded catalog can actually anchor a label on."""
    listed = client.get("/targets/entities?catalog_source=deposits", headers=AUTH).json()
    return listed[0]["entity"] if listed else ""


def _valid_rule(client) -> dict:
    entities = client.get("/targets/entities?catalog_source=deposits", headers=AUTH).json()
    spine = entities[0]
    return {"shape": "state_change", "name": "tgt_npe_90d", "entity": spine["entity"],
            "anchor_catalog": "deposits", "grain_ref": spine["spine_ref"],
            "as_of_ref": _ASOF, "window_days": 90, "as_of_frequency": "monthly",
            "label_type": "binary", "operator": ">=", "threshold": 1,
            "column_ref": _FLAG, "from_values": ["P"], "to_values": ["N"]}


def test_the_catalog_says_what_it_can_ANCHOR_a_label_on(make_client):
    """The person picks from this — not from the 38-name vocabulary and not from a model's guess."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    listed = client.get("/targets/entities?catalog_source=deposits", headers=AUTH).json()
    assert listed, "this catalog has a keyed spine table, so it must offer at least one entity"
    assert {"entity", "spine_table", "spine_ref"} <= set(listed[0])


def test_propose_returns_the_registry_hits_BESIDE_the_draft(make_client):
    """Spec §7.5 Step 1 — search runs with the proposal, in a SEPARATE key. Merging them into one
    list would hide which is a decision already made and which is a draft."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/targets/propose", json={
        "hypothesis": "which customers go non-performing",
        "entity": _entity_of(client), "catalog_source": "deposits"}, headers=AUTH).json()
    assert "existing" in body and "draft" in body
    assert body["draft"]["shape"] == "state_change"
    assert "from_values" in body["draft"]["needs_input"]


def test_an_entity_this_catalog_cannot_ANCHOR_is_refused(make_client):
    """The person picks from `selectable_entities`, but the route must not trust the client to have
    picked from it — and "no keyed spine table" is a better answer than a draft anchored on
    nothing."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.post("/targets/propose", json={
        "hypothesis": "h", "entity": "spacecraft", "catalog_source": "deposits"}, headers=AUTH)
    assert res.status_code == 422
    assert res.json()["detail"]["code"] == "ENTITY_NOT_ANCHORABLE"


def test_a_proposal_that_fails_technically_is_reported_not_faked(make_client):
    client = make_client(_no_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/targets/propose", json={
        "hypothesis": "h", "entity": _entity_of(client), "catalog_source": "deposits"},
        headers=AUTH).json()
    assert body["draft"] is None


def test_the_sentence_is_available_BEFORE_submitting(make_client):
    """The person approves a statement of meaning, not twelve fields — so it must be renderable
    while the form is being edited, not returned once they have committed."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    said = client.post("/targets/describe", json={"rule": _valid_rule(client)},
                       headers=AUTH).json()["reads_as"]
    assert "one row per" in said
    assert "sampled monthly" in said and "observed" in said


def test_an_incomplete_rule_describes_as_NOTHING_rather_than_erroring(make_client):
    """A half-filled form is the normal state while someone is typing, not a failure."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/targets/describe", json={"rule": {"shape": "state_change"}},
                       headers=AUTH).json()
    assert body["reads_as"] is None and body["incomplete"]


def test_registering_an_INVALID_rule_is_a_typed_422(make_client):
    """The contract's refusals reach the caller intact — a backward rule is a feature, and saying
    so is more useful than a 500."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    bad = _valid_rule(client) | {"direction": "backward"}
    res = client.post("/targets", json={"rule": bad}, headers=AUTH)
    assert res.status_code == 422
    assert "forward" in str(res.json()["detail"])


def test_registering_stores_the_rule_its_proposal_and_the_comment(make_client):
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    rule = _valid_rule(client)
    res = client.post("/targets", json={
        "rule": rule, "description": "credit deterioration",
        "proposed_draft": {"fields": {"window_days": 90}},
        "author_comment": "180 because the desk reviews quarterly"}, headers=AUTH)
    assert res.status_code == 200, res.text
    assert "one row per" in res.json()["reads_as"]

    listed = client.get(f"/targets?entity={rule['entity']}", headers=AUTH).json()
    assert listed[0]["name"] == "tgt_npe_90d"
    assert "quarterly" in listed[0]["author_comment"]
    assert listed[0]["proposed_draft"]["fields"]["window_days"] == 90


# ══ the derivation logic ═════════════════════════════════════════════════════════════════════════

def test_the_SQL_is_previewable_before_the_label_is_registered(make_client):
    """The person approving a label should see the logic that will build their training data while
    they can still change it — the same argument as the sentence, one level deeper."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/targets/sql", json={"rule": _valid_rule(client)}, headers=AUTH).json()
    assert "WITH as_of_dates AS" in body["sql"]
    assert body["incomplete"] is None


def test_an_incomplete_rule_previews_as_NOTHING_rather_than_erroring(make_client):
    """A half-filled form is the normal state while someone is typing."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    body = client.post("/targets/sql", json={"rule": {"shape": "state_change"}},
                       headers=AUTH).json()
    assert body["sql"] is None and body["incomplete"]


def test_a_REGISTERED_label_can_be_asked_for_its_sql(make_client):
    """The consumer who runs it was not necessarily the person who approved it."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    rule = _valid_rule(client)
    client.post("/targets", json={"rule": rule, "description": "d"}, headers=AUTH)
    res = client.get(f"/targets/{rule['entity']}/tgt_npe_90d/sql", headers=AUTH)
    assert res.status_code == 200, res.text
    assert "WITH as_of_dates AS" in res.json()["sql"]
    assert "one row per" in res.json()["reads_as"]


def test_asking_for_the_sql_of_a_label_that_does_not_exist_is_a_404(make_client):
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    res = client.get("/targets/customer/tgt_never_registered/sql", headers=AUTH)
    assert res.status_code == 404


# ══ the seam into generation ═════════════════════════════════════════════════════════════════════

def test_a_registered_label_can_be_ATTACHED_to_an_intent(make_client, db):
    """Until this existed the registry was an island: a person could author a label and nothing in
    the platform could then be trained against it."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    rule = _valid_rule(client)
    definition_id = client.post("/targets", json={"rule": rule, "description": "d"},
                                headers=AUTH).json()["definition_id"]
    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, "
               "redacted_hypothesis) VALUES ('int-9','h','hypothesis','h')")
    res = client.post(f"/targets/{rule['entity']}/tgt_npe_90d/attach",
                      json={"intent_id": "int-9"}, headers=AUTH)
    assert res.status_code == 200, res.text
    assert res.json()["definition_id"] == definition_id


def test_attaching_a_label_to_an_intent_that_ALREADY_has_a_column_target_is_refused(
        make_client, db):
    """Two kinds of target on one intent would name two different things to predict."""
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    rule = _valid_rule(client)
    client.post("/targets", json={"rule": rule, "description": "d"}, headers=AUTH)
    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, "
               "redacted_hypothesis, target_ref) "
               "VALUES ('int-8','h','hypothesis','h','public.accounts.churned')")
    res = client.post(f"/targets/{rule['entity']}/tgt_npe_90d/attach",
                      json={"intent_id": "int-8"}, headers=AUTH)
    assert res.status_code == 409


# ══ end to end: a registered label becomes a governed generation target ══════════════════════════

def _build_set(db) -> None:
    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, "
               "redacted_hypothesis) VALUES ('int-e2e','h','hypothesis','h') "
               "ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
               "VALUES ('trr-e2e','int-e2e','exploration','h') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO build_set_revision (revision_id, target_reading_revision_id, "
               "declaration_hash, declaration_json, content_hash, declared_by, declared_at) "
               "VALUES ('bs-e2e','trr-e2e','dh','{}'::jsonb,'bs-e2e','user:ops','2026-09-03') "
               "ON CONFLICT DO NOTHING")


def test_a_generation_can_be_AUTHORIZED_for_a_registered_label(make_client, db, monkeypatch):
    """The whole seam in one test: author a label, register it, and authorize a generation to
    predict it. Before this the registry was complete and disconnected — nothing in the platform
    could be trained against anything it held."""
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_ENABLED", "1")
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    _build_set(db)
    definition_id = client.post("/targets", json={"rule": _valid_rule(client), "description": "d"},
                                headers=AUTH).json()["definition_id"]

    res = client.post("/feature-execution/generations", json={
        "environment_id": "sandbox", "logical_group_name": "grp",
        "build_set_revision_id": "bs-e2e", "target_mode": "prediction",
        "target_definition_id": definition_id}, headers=AUTH)
    assert res.status_code == 201, res.text
    assert res.json()["target_definition_id"] == definition_id
    assert res.json()["target_ref"] is None

    consumers = db.execute("SELECT consumer_ref FROM target_consumer WHERE definition_id = %s",
                           (definition_id,)).fetchall()
    assert consumers, "§9: authorizing a generation is how a label acquires a consumer"


def test_authorizing_with_BOTH_kinds_of_target_is_a_422(make_client, db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_ENABLED", "1")
    client = make_client(_draft_fake())
    upload_csv(client, "deposits", DEPOSITS_CSV)
    _build_set(db)
    definition_id = client.post("/targets", json={"rule": _valid_rule(client), "description": "d"},
                                headers=AUTH).json()["definition_id"]
    res = client.post("/feature-execution/generations", json={
        "environment_id": "sandbox", "logical_group_name": "grp",
        "build_set_revision_id": "bs-e2e", "target_mode": "prediction",
        "target_ref": "public.accounts.churned",
        "target_definition_id": definition_id}, headers=AUTH)
    assert res.status_code == 422
    assert "one kind of target" in str(res.json()["detail"])
