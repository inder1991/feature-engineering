"""Remediation E0 — the end-to-end walkthrough gate: this plan's own acceptance test.

ONE test walks the whole semantic_v1 feature-generation workflow and clears every blocker
through its REAL surface — the same clicks a human makes, as API calls:

    blocked card → funnel concept confirm → recipe reviews → UOA confirm → regenerate →
    create_contract ALLOWED → draft → confirm (governed, FORMULA_BLOCKED honest) →
    materialization typed refusal → save-idea → refine → suggestions v4 agrees.

Runs in the DEFAULT suite deliberately: from the day it lands, any regression that breaks the
end-to-end path breaks the build. The completion claim it proves is capped exactly as the plan
states: contract-authoring is ready for UAT; materialization is VISIBLY unavailable.

The hero candidate is ``complaint_count`` — churn objective, deterministic formula, customer
output grain (matches the confirmed UOA), and an authored temporal contract that actually
compiles (its windowed variants serve as real cards; the authored-gap recipes like
``tenure_days`` are honestly rejected with TEMPORAL_POLICY_UNRESOLVED — asserted below).
The catalog is a minimal cib-shaped single table whose concepts are ALL
AI-proposed (so the authority floor genuinely blocks until the funnel confirms them) and
whose recipes carry no reviews (so review-currency genuinely blocks until the review route
records them).
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.api._helpers import AUTH

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "long-tenured customers churn when their balance drops"
TARGET = "public.accounts.churned"
SOURCE = "cib"

#: A second attributable governor — review validity requires >=2 distinct identities across
#: the required roles (single-identity approvals are a violation by design).
AUTH_2 = {"X-User": "ravi", "X-Roles": "platform_admin"}
#: The funnel routes gate on the RAW `platform-admin` claim (the overlay's dual-owner confirm
#: claim), deliberately not the permission bundle — the walkthrough carries both.
GOV = {"X-User": "tester", "X-Roles": "platform_admin,platform-admin"}


def _fake() -> FakeLLM:
    from tests.featuregen.api.test_semantic_v1_serving import _intent_payload

    return FakeLLM(script={
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits"}),
        "overlay.feature.intents": FakeResponse(output=_intent_payload()),
        "overlay.contract.draft": FakeResponse(output={
            "definition": "Count of complaint events per customer in the window."}),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    })


def _cib(conn) -> None:
    """The cib-shaped minimal catalog: ONE snapshot-and-events table at customer grain.

    Every concept lands as AI-proposed — stamped on the graph AND recorded as llm/proposed
    field evidence (what a real enrichment pass writes and what the funnel reads) — so the
    authority floor is REAL until the funnel confirms it: exactly the state a freshly
    ingested catalog is in."""
    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow(SOURCE, "accounts", "customer_id", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(SOURCE, "accounts", "open_date", "timestamp"), "origination_date"),
        (CanonicalRow(SOURCE, "accounts", "complaint_flag", "boolean"), "complaint_event"),
        (CanonicalRow(SOURCE, "accounts", "as_of_date", "timestamp", as_of=True),
         "as_of_date"),
        (CanonicalRow(SOURCE, "accounts", "balance", "numeric",
                      additivity="semi_additive", currency="USD"), "monetary_stock"),
        (CanonicalRow(SOURCE, "accounts", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow(SOURCE, "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(SOURCE, "accounts", "churned", "boolean"), "outcome_label"),
    ]
    build_graph(conn, SOURCE, [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    # The AI's concept proposals as EVIDENCE rows — what a real enrichment pass writes and
    # what the confirmation funnel reads. Same producer/strength as the measured live shape.
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.column_authority import logical_ref_of

    for row, concept in catalog:
        logical = logical_ref_of(conn, SOURCE, f"public.{row.table}.{row.column}")
        record_field_evidence(
            conn, logical_ref=logical, field_name="concept", proposed_value=concept,
            producer="llm", strength="proposed", producer_ref="svc:enrichment",
            source_snapshot_id="snap-e0",
            input_hash=field_input_hash(logical_ref=logical, field_name="concept",
                                        material=concept))
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, "
        "last_run_id, head_seq) VALUES (%s, %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (SOURCE, now, now))


def _considered_set(client, *, intent_id=None, uoa=False) -> dict:
    payload = {
        "hypothesis": HYPOTHESIS, "objective": "predict churn",
        "catalog_source": SOURCE, "target_ref": TARGET, "contract_version": 2,
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }
    if intent_id is not None:
        payload["intent_id"] = intent_id
    if uoa:
        # B10 — the human's one-click yes on the DERIVED unit-of-analysis proposal.
        payload["confirmed_scope"]["uoa_entity"] = "customer"
        payload["confirmed_scope"]["spine_ref"] = "public.accounts.customer_id"
    res = client.post("/contract/considered-set", json=payload, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def _card(body: dict, definition_prefix: str) -> dict:
    """The served card whose origin-neutral id matches — exact or a variant of it."""
    cards = [f for s in body["alternatives"] for f in s["features"]
             if (f.get("source_definition_id") or "").split("@")[0] == definition_prefix]
    assert cards, (f"{definition_prefix} not served; served ids: "
                   f"{[f.get('source_definition_id') for s in body['alternatives'] for f in s['features']]}")
    return cards[0]


def _actions_entry(body: dict, option_id: str) -> dict:
    for section in ("recommended_options", "actionable_options"):
        for entry in body.get(section, ()):
            if entry["option_id"] == option_id:
                return entry
    raise AssertionError(f"option {option_id} in neither v2 section")


def _blocker_codes(entry: dict, action: str) -> set:
    return {b["code"] for b in entry.get("blocked_actions", {}).get(action, ())}


def test_the_whole_workflow_walks_end_to_end(make_client, conn, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    _cib(conn)
    # The route harness shares ONE READ COMMITTED test transaction, so the C0 catalog seal's
    # isolation gate would skip snapshotting (production always seals — the REPEATABLE READ
    # pinning has its own suite, test_feature_gen_isolation). Stub the two isolation gates so
    # generation SEALS FOR REAL here: the revision carries its metadata_snapshot_id and the
    # draft/confirm freshness re-reads run exactly as they do live.
    monkeypatch.setattr(
        "featuregen.overlay.upload.contract.gate1._on_repeatable_read", lambda conn: True)
    monkeypatch.setattr(
        "featuregen.overlay.upload.feature_metadata_snapshot._assert_repeatable_read",
        lambda conn: "repeatable read")
    client = make_client(llm_client=_fake())

    # ── Step 1: the first considered set — the hero candidate is SERVED and honestly
    # blocked: AI-proposed concepts (authority floor) + no recipe reviews. ──────────────
    round1 = _considered_set(client)
    card = _card(round1, "complaint_count")

    # The authored-gap recipe is honestly REJECTED, never silently dropped: tenure_days
    # binds but its authored temporal contract has no snapshot policy — setup work, named.
    assert any(r.get("code") == "TEMPORAL_POLICY_UNRESOLVED"
               and r.get("name") == "Tenure days"
               for r in round1.get("rejected_outputs", [])), "the authored gap must be named"
    entry = _actions_entry(round1, card["option_id"])
    blocked = _blocker_codes(entry, "create_contract")
    assert "PROPOSED_METADATA_ONLY" in blocked, blocked
    assert "RECIPE_REVIEW_NOT_CURRENT" in blocked, blocked
    assert "save_idea" in entry["allowed_actions"]          # a blocked card is never hidden

    # ── Step 2a: confirm the AI-proposed concepts through the REAL funnel route —
    # the exact CAS anchors the queue served, one attributable decision per column. ─────
    queue_res = client.get(f"/governance/concept-confirmations?source={SOURCE}",
                           headers=GOV)
    assert queue_res.status_code == 200, queue_res.text
    queue = queue_res.json()
    items = []
    for group in queue["groups"]:
        for col in group["columns"]:
            items.append({
                "object_ref": col["object_ref"], "action": "confirm_existing",
                "evidence_id": col["evidence_id"],
                "expected_latest_decision_id": col["latest_decision_id"],
                "expected_evidence_set_hash": col["evidence_set_hash"],
                "expected_policy_version": col["policy_version"],
            })
    assert items, "the funnel queue must offer the proposed concepts"
    confirmed = client.post("/governance/concept-confirmations", json={
        "source": SOURCE, "reason": "E0 walkthrough — SME confirms the proposals",
        "items": items,
    }, headers=GOV).json()
    assert confirmed["accepted_count"] == len(items), confirmed

    # ── Step 2b: record the recipe reviews through the REAL route — all three required
    # roles, two distinct identities (single-identity approval is a violation). ─────────
    from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
    from featuregen.overlay.upload.recipe_review_validity import required_reviewer_roles

    hero = v2_recipe_by_id("complaint_count")
    live_hash = canonical_recipe_v2_hash(hero)
    identities = (AUTH, AUTH_2)
    for i, role in enumerate(required_reviewer_roles(hero)):
        rr = client.post("/recipes/complaint_count/reviews",
                         headers=identities[i % 2], json={
            "decision": "approved", "reviewer_role": role,
            "reviewed_revision_hash": live_hash,
            "rationale": "E0 walkthrough — definition matches the banking meaning"})
        assert rr.status_code == 201, rr.text

    # ── Step 2c: the UOA proposal derives from the signed target's table grain; the
    # human confirms it with ONE yes; regenerate under the SAME intent. ─────────────────
    proposal = client.get(
        f"/contract/uoa-proposal?catalog_source={SOURCE}&target_ref={TARGET}",
        headers=AUTH).json()
    assert proposal["proposed"] is not None
    assert proposal["proposed"]["entity"] == "Customer"
    assert proposal["proposed"]["spine_ref"] == "public.accounts.customer_id"

    round2 = _considered_set(client, intent_id=round1["intent_id"], uoa=True)
    card2 = _card(round2, "complaint_count")
    entry2 = _actions_entry(round2, card2["option_id"])

    # ── Step 3: every named blocker was cleared through its surface — create_contract is
    # ALLOWED. Draft, then confirm: the feature is GOVERNED, and the readiness truth
    # (FORMULA_BLOCKED — no executable formula exists yet) rides the contract honestly. ─
    assert "create_contract" in entry2["allowed_actions"], (
        entry2.get("blocked_actions", {}).get("create_contract"))

    dr = client.post("/contract/draft", json={
        "intent_id": round2["intent_id"], "chosen_option_id": card2["name"],
        "expected_generation_run_id": round2["generation_run_id"],
        "why": "complaint pressure is a churn precursor",
    }, headers=AUTH)
    assert dr.status_code == 200, dr.text
    draft = dr.json()

    confirm_body = dict(draft["draft"])
    confirm_body["intent_id"] = round2["intent_id"]
    confirm_body["expected_binding_hash"] = draft.get("binding_hash")
    cr = client.post("/contract/confirm", json=confirm_body, headers=AUTH)
    assert cr.status_code == 200, cr.text

    lifecycle = conn.execute(
        "SELECT lifecycle_state FROM feature WHERE name = %s",
        (card2["name"],)).fetchone()
    assert lifecycle == ("governed",), lifecycle
    # The readiness truth rides the FROZEN decision the confirm was made against: no
    # executable formula exists, and the record never pretends otherwise.
    readiness = conn.execute(
        "SELECT DISTINCT readiness FROM semantic_option_decision "
        "WHERE source_definition_id LIKE 'complaint_count%%'").fetchall()
    assert readiness == [("FORMULA_BLOCKED",)], readiness

    # ── Step 4: materialization is VISIBLY unavailable — the typed ladder refusal, never
    # a silent success and never a hidden button. C2 made the execution floor REAL: after
    # the funnel confirmations the floor is measured and MET (neither EXECUTION code
    # appears), and what still refuses is the honest remainder — no executable formula.
    mat_blocked = _blocker_codes(entry2, "request_materialization")
    assert "READINESS_NOT_MATERIALIZATION_READY" in mat_blocked, mat_blocked
    assert "FORMULA_SCHEMA_UNSUPPORTED" in mat_blocked, mat_blocked
    assert "EXECUTION_AUTHORITY_UNEVALUATED" not in mat_blocked, mat_blocked
    assert "EXECUTION_AUTHORITY_UNMET" not in mat_blocked, mat_blocked
    assert "request_materialization" not in entry2["allowed_actions"]

    # ── Step 5: the conceptual LLM intent saves as an IDEA — browsable, labeled, never a
    # governed feature and never a model consumer's input. ──────────────────────────────
    intent_card = next(f for s in round2["alternatives"] for f in s["features"]
                       if f.get("generation_source") == "llm_intent")
    saved = client.post("/features", json={
        "name": intent_card["name"], "description": intent_card["description"],
        "grain_table": intent_card.get("grain_table"),
        "aggregation": intent_card.get("aggregation"),
        "derives_from": [{"catalog_source": SOURCE, "object_ref": ref}
                         for ref in intent_card.get("derives_from", [])],
    }, headers=AUTH)
    assert saved.status_code == 200, saved.text
    assert saved.json()["lifecycle_state"] == "idea"
    assert saved.json()["governed"] is False

    # ── Step 6: refine round-trips through the ENGINE — instruction in, binder-bound
    # revision out, and governing still requires a whole-round regenerate. ──────────────
    refined = client.post("/features/refine", json={
        "candidate": {"name": intent_card["name"],
                      "description": intent_card["description"],
                      "derives_from": intent_card.get("derives_from", []),
                      "aggregation": None, "grain_table": None},
        "instruction": "look at deposit activity only",
        "catalog_source": SOURCE,
    }, headers=AUTH)
    assert refined.status_code == 200, refined.text
    assert refined.json()["regenerate_to_govern"] is True
    assert refined.json()["revised"]["generation_source"] == "llm_intent"

    # ── Step 7: the Suggested Features page consults the SAME engine — its semantic
    # block agrees with the Workbench on the hero's binding. ──────────────────────────
    sugg = client.get(f"/catalog/{SOURCE}/tables/accounts/suggestions?contract_version=4",
                      headers=AUTH)
    assert sugg.status_code == 200, sugg.text
    semantic = sugg.json().get("semantic")
    assert semantic is not None, "v4 must carry the semantic block"
    hero_entries = [c for section in ("ranked", "actionable")
                    for c in semantic.get(section, ())
                    if (c.get("recipe_id") or "").split("@")[0] == "complaint_count"]
    assert hero_entries, "the hero must appear on its anchored table's page"
    assert all(c["binding_state"] == "bound" for c in hero_entries), hero_entries
    assert any(c in semantic["ranked"] for c in hero_entries), (
        "both surfaces consulted ONE engine — the confirmed binding ranks here too")
