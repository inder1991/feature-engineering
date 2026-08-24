"""SE-11 step 1 — the EXPLICIT response contract: v2 is asked for, v1 never leaks it.

The rollout rule the plan pins: an omitted version preserves the current response byte-shape,
and an old client never infers the version from optional fields — the marker is top-level and
present ONLY when the client asked. The v2 additions are the version marker itself plus the
resolved semantic-planning mode (the step-7 diagnostic); the per-card semantic fields already
ride the v2 card serializer on every path.
"""
from __future__ import annotations

from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

CHURN = "customer.relationship_attrition.churn"


def _fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits"}),
    })


def _bank(conn) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        # T2: the account key the balance recipes bind their population on. Before T2 this
        # fixture served OPTIONS anyway — every eligible recipe became an actionable card with
        # its measure unbound — so these tests could address options over a catalog that could
        # not compute one. A card is an offer to compute something, so the catalog now carries
        # what the offer needs: with this column 12 candidates bind completely.
        (CanonicalRow("bank", "accounts", "acct_id", "integer", entity="Account"),
         "account_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
    ]
    build_graph(conn, "bank", [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s", (now, now))


def _post(client, **extra) -> dict:
    res = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops",
        "objective": "predict churn", "catalog_source": "bank",
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
        **extra,
    }, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def test_v2_is_asked_for_and_carries_the_marker(make_client, conn):
    """E4: the marker survives the cutover; `semantic_planning_mode` does NOT. It named which
    of three pipelines answered, and there is one pipeline now — a constant that looks like a
    reading is worse than no reading, so the key is gone rather than frozen to a value."""
    _bank(conn)
    body = _post(make_client(llm_client=_fake()), contract_version=2)
    assert body["contract_version"] == 2
    assert "semantic_planning_mode" not in body


def test_v1_default_never_carries_the_new_keys(make_client, conn):
    """The no-silent-leak pin: an omitted version is TODAY'S response — a frozen old client
    must never see a semantic field appear and be tempted to infer."""
    _bank(conn)
    body = _post(make_client(llm_client=_fake()))
    assert "contract_version" not in body
    assert "semantic_planning_mode" not in body


def test_v2_carries_the_revision_address_and_the_detail_endpoint_serves_it(
        make_client, conn, monkeypatch):
    """SE-11 steps 3+4 round trip: the v2 response names the immutable revision it was minted
    from; the detail endpoint serves any of its options FROM THAT STORED REVISION, with the
    run's semantic evidence attached for recipe-sourced options when observations exist."""
    _bank(conn)
    client = make_client(llm_client=_fake())
    body = _post(client, contract_version=2)
    revision_id = body["considered_revision_id"]
    assert revision_id and body["considered_content_hash"]

    option_ids = [f["option_id"] for s in body["alternatives"] for f in s["features"]]
    assert option_ids, "the considered set minted addressable options"
    res = client.get(
        f"/contract/considered-revisions/{revision_id}/options/{option_ids[0]}",
        headers=AUTH)
    assert res.status_code == 200, res.text
    detail = res.json()
    assert detail["considered_revision_id"] == revision_id
    assert detail["considered_content_hash"] == body["considered_content_hash"]
    assert detail["option"]["canonical_candidate_identity_hash"]
    identity = detail["option"]["canonical_candidate_identity"]
    assert identity["feature"]["name"]                        # the exact card the human saw
    if (identity["feature"].get("recipe_id")
            and "semantic_evidence" in detail):
        evidence = detail["semantic_evidence"]
        assert evidence["binding_state"] in ("bound", "ambiguous", "missing", "blocked")
        assert evidence["context_hash"]


def test_detail_endpoint_404s_unknown_revision_and_option(make_client, conn, monkeypatch):
    _bank(conn)
    client = make_client(llm_client=_fake())
    res = client.get(
        "/contract/considered-revisions/ccr_nope/options/opt_nope", headers=AUTH)
    assert res.status_code == 404

    body = _post(client, contract_version=2)
    res = client.get(
        f"/contract/considered-revisions/{body['considered_revision_id']}/options/opt_nope",
        headers=AUTH)
    assert res.status_code == 404
    assert "UNKNOWN_CONSIDERED_OPTION" in res.text


def test_v2_on_the_emergency_unscoped_path_is_refused(make_client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_CONFIRMATION_REQUIRED", "0")
    _bank(conn)
    res = make_client(llm_client=_fake()).post("/contract/considered-set", json={
        "hypothesis": "h", "objective": "o", "catalog_source": "bank",
        "contract_version": 2,
    }, headers=AUTH)
    assert res.status_code == 422
    assert "confirmed_scope" in res.text


def test_draft_refuses_on_a_stale_semantic_snapshot(make_client, conn, monkeypatch):
    """SE-11 step 6: catalog drift between generation and the human's choice is a typed 409 —
    the draft never proceeds over a world that no longer exists. The drifted field is OUTSIDE
    the chosen option's own refs: only the sealed context pin can see it, which is the point.
    Seeding idiom: the C0 lineage tests' — seal a REAL snapshot and attach it to the mutable
    considered-set pointer, as a production REPEATABLE READ run would have."""
    from featuregen.overlay.upload.feature_metadata_snapshot import build_metadata_snapshot
    from featuregen.overlay.upload.generation_semantic_context import (
        build_generation_semantic_context,
        context_snapshot_item,
    )

    _bank(conn)
    client = make_client(llm_client=_fake())
    res = client.post("/contract/considered-set", json={
        "hypothesis": "customers churn when their balance drops",
        "objective": "predict churn", "catalog_source": "bank"}, headers=AUTH)
    assert res.status_code == 200, res.text
    body = res.json()
    names = [f["name"] for s_ in body["alternatives"] for f in s_["features"]]
    assert names

    # The shared test transaction cannot switch to REPEATABLE READ mid-flight; the seal's
    # isolation pinning has its own suite (test_feature_gen_isolation) — THIS test proves the
    # draft route consumes the freshness verdict, so the assertion is stubbed here only.
    monkeypatch.setattr(
        "featuregen.overlay.upload.feature_metadata_snapshot._assert_repeatable_read",
        lambda conn: "repeatable read")
    context = build_generation_semantic_context(conn, catalog_source="bank")
    sealed = build_metadata_snapshot(
        conn, generation_run_id="fgr_stale_probe",
        refs=[("bank", "public.accounts.balance")],
        read_scope_hash="sha256:scope", fields=["additivity"],
        extra_items=[context_snapshot_item(context)])
    conn.execute(
        "UPDATE contract_considered SET generation_run_id = %s, snapshot_id = %s, "
        "snapshot_content_hash = %s WHERE intent_id = %s",
        ("fgr_stale_probe", sealed.snapshot_id, sealed.content_hash, body["intent_id"]))

    conn.execute("UPDATE graph_node SET additivity = 'non_additive' "
                 "WHERE object_ref = 'public.accounts.balance'")
    res = client.post("/contract/draft", json={
        "intent_id": body["intent_id"],
        "chosen_option_id": names[0],
    }, headers=AUTH)
    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "SEMANTIC_SNAPSHOT_STALE"
    assert detail["reason"] == "SNAPSHOT_ITEM_DRIFT"


def test_v2_serves_three_sections_with_actions_from_the_fold(make_client, conn, monkeypatch):
    """A3: recommended / actionable / rejected — actionable candidates are OPTIONS with ids
    and per-action verdicts from the SAME activation fold the durable writes consult;
    save_idea is always allowed, create_contract blocked with named next steps."""
    _bank(conn)
    body = _post(make_client(llm_client=_fake()), contract_version=2)

    assert "recommended_options" in body and "actionable_options" in body
    assert "rejected_outputs" in body
    sections = body["recommended_options"] + body["actionable_options"]
    assert sections, "the engine served at least one sectioned option"
    for entry in sections:
        assert entry["option_id"] and entry["recipe_id"]      # sections key stays recipe_id
        # (the section builder falls back to source_definition_id server-side)
        assert "save_idea" in entry["allowed_actions"]          # an idea is an idea, always
        assert "create_contract" in entry["blocked_actions"]    # nothing authorable yet here
        blockers = entry["blocked_actions"]["create_contract"]
        assert all(b["code"] and b["next_step"] for b in blockers)
    # T2: an OPTION is now always a candidate whose REQUIRED operands all bound — an unbound
    # one is setup work and mints no option id, so it can appear in neither section. That
    # narrows `actionable` to what the section builder has always actually decided it on:
    # BOUND-BUT-PLANLESS (B10 — no declared population, a cross-dataset hop, a UOA mismatch).
    # The old `in ("ambiguous", "missing", "blocked")` held only because nothing on this
    # fixture bound at all, which is the defect this test now sits downstream of.
    assert body["needs_setup"], "the held-out candidates are named, not dropped"
    for entry in body["actionable_options"]:
        assert entry["binding_state"] == "bound"
        # every actionable option is a REAL option: it has a decision row at its exact key
        row = conn.execute(
            "SELECT 1 FROM semantic_option_decision WHERE option_id = %s",
            (entry["option_id"],)).fetchone()
        assert row is not None


def test_v1_never_carries_the_section_keys(make_client, conn, monkeypatch):
    _bank(conn)
    body = _post(make_client(llm_client=_fake()))
    # `needs_setup` is T2's addition and obeys the same no-silent-leak rule as every key
    # above it: a frozen v1 client must never see a new field appear and infer a version.
    for key in ("recommended_options", "actionable_options", "rejected_outputs",
                "needs_setup", "contract_version", "semantic_planning_mode"):
        assert key not in body


# ══ C-D11 — the 409 gate, made real and then FIRED through the production route ══════════════════
def test_A_TAMPERED_TYPED_PLANNING_REQUEST_RETURNS_409(make_client, conn, monkeypatch):
    """The gate had exactly one occurrence in the repo, no test, and no way to fire: the two values
    it compares are written from ONE in-memory object in one statement.

    C-D11 adds a SECOND, INDEPENDENT source whose hash is recomputed from the stored payload's own
    bytes. This inserts a deliberately inconsistent row and drives the REAL route — a legitimate
    synthetic corruption test, because it exercises the production reader and handler rather than
    asserting that `!=` works.
    """
    import json

    from featuregen.overlay.upload.feature_planning_contracts import (
        planning_request_from_recipe,
    )
    from featuregen.overlay.upload.planning_request_store import (
        canonical_planning_request_payload,
    )
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    _bank(conn)
    client = make_client(llm_client=_fake())
    body = _post(client, contract_version=2)
    revision_id = body["considered_revision_id"]
    option_ids = [f["option_id"] for s in body["alternatives"] for f in s["features"]]
    assert option_ids

    # serves cleanly first — a legacy row is NOT tampering, so the route still answers 200
    clean = client.get(
        f"/contract/considered-revisions/{revision_id}/options/{option_ids[0]}", headers=AUTH)
    assert clean.status_code == 200, clean.text
    assert clean.json().get("planning_request_verified") is False, "no typed row stored yet"

    # now store a payload whose bytes do not hash to the identity stored beside them
    request = planning_request_from_recipe(v2_recipe_by_id("posted_debit_amount"))
    payload = canonical_planning_request_payload(request)
    payload["source_content_hash"] = "sha256:not-what-was-hashed"
    conn.execute(
        "INSERT INTO typed_planning_request (considered_revision_id, option_id, request_payload, "
        "planning_request_hash) VALUES (%s, %s, %s::jsonb, %s)",
        (revision_id, option_ids[0], json.dumps(payload), "sha256:claimed-but-wrong"))

    res = client.get(
        f"/contract/considered-revisions/{revision_id}/options/{option_ids[0]}", headers=AUTH)
    assert res.status_code == 409, res.text
    assert res.json()["detail"]["code"] == "DECISION_RECORD_TAMPERED"
