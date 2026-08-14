"""E1 — the banking acceptance corpus: the review's §10 table, all 14 cases, END TO END.

Every case drives the REAL serving path (route in, wire out — never a unit fold) over a
named, versioned fixture, and asserts the exact refusal code / named action / served variant
its review row requires. Each docstring cites its row verbatim.

Versioning: the fixture builders are frozen here (v1); a case whose expected wire behavior
changes is a REVIEWED change to this file, never a silent drift.
"""
from __future__ import annotations

import json

import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph

pytestmark = pytest.mark.eval

AUTH = {"X-User": "tester", "X-Roles": "platform_admin"}
GOV = {"X-User": "tester", "X-Roles": "platform_admin,platform-admin"}
CHURN = "customer.relationship_attrition.churn"


# ── fixture builders (v1 — versioned with the corpus) ──────────────────────────────────────────

def _catalog(conn, source: str, rows) -> None:
    from datetime import UTC, datetime

    build_graph(conn, source, [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    now = datetime.now(UTC)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, "
        "last_run_id, head_seq) VALUES (%s, %s, 'r', 0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (source, now, now))


def _propose_concepts(conn, source: str, rows) -> None:
    """Every concept as llm/proposed field evidence — the measured freshly-ingested shape."""
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.column_authority import logical_ref_of

    for row, concept in rows:
        logical = logical_ref_of(conn, source, f"public.{row.table}.{row.column}")
        record_field_evidence(
            conn, logical_ref=logical, field_name="concept", proposed_value=concept,
            producer="llm", strength="proposed", producer_ref="svc:enrichment",
            source_snapshot_id="gold-v1",
            input_hash=field_input_hash(logical_ref=logical, field_name="concept",
                                        material=concept))


def _confirm_concepts(client, source: str) -> None:
    queue = client.get(f"/governance/concept-confirmations?source={source}",
                       headers=GOV).json()
    items = [{
        "object_ref": col["object_ref"], "action": "confirm_existing",
        "evidence_id": col["evidence_id"],
        "expected_latest_decision_id": col["latest_decision_id"],
        "expected_evidence_set_hash": col["evidence_set_hash"],
        "expected_policy_version": col["policy_version"],
    } for group in queue["groups"] for col in group["columns"]]
    assert items
    res = client.post("/governance/concept-confirmations", json={
        "source": source, "reason": "gold corpus", "items": items}, headers=GOV).json()
    assert res["accepted_count"] == len(items)


def _cib_rows(source: str):
    return [
        (CanonicalRow(source, "bo_cib_customer", "cust_num", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(source, "bo_cib_customer", "complaint_flag", "boolean"),
         "complaint_event"),
        (CanonicalRow(source, "bo_cib_customer", "as_of_date", "timestamp", as_of=True),
         "as_of_date"),
        (CanonicalRow(source, "bo_cib_customer", "balance", "numeric",
                      additivity="semi_additive", currency="USD"), "monetary_stock"),
        (CanonicalRow(source, "bo_cib_customer", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow(source, "bo_cib_customer", "event_ts", "timestamp"),
         "event_timestamp"),
        (CanonicalRow(source, "bo_cib_customer", "churned", "boolean"), "outcome_label"),
    ]


def _intent(operands, *, output=None, temporal=None, name="gold_probe",
            reason="gold-corpus conceptual probe"):
    """A structurally-valid conceptual intent (the serving schema's exact field names)."""
    body = {
        "display_name": name,
        "business_definition": f"gold corpus probe: {name}",
        "primary_objective": CHURN,
        "computation_kind": "conceptual_pattern",
        "output_grain_entity": "customer",
        "source_grain": "transaction",
        "output": {
            "output_id": f"gold_{name}", "display_label": name,
            "output_type": "numeric", "additivity": "non_additive", "unit_kind": "count",
            "null_input_policy": "nulls are excluded and counted",
            "empty_population_policy": "null with populated flag",
            **(output or {}),
        },
        "operands": operands,
        "temporal": {"anchor_kind": "event", "window_basis": "event time",
                     "window_unit": "days", "cutoff_inclusivity": "inclusive",
                     **(temporal or {})},
        "conceptual_reason": reason,
        "rationale": "gold corpus probe",
    }
    return {"intents": [body]}


def _fake(intent_payload=None) -> FakeLLM:
    script = {
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "fits"}),
        "overlay.contract.draft": FakeResponse(output={"definition": "gold draft"}),
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
    }
    if intent_payload is not None:
        script["overlay.feature.intents"] = FakeResponse(output=intent_payload)
    else:
        script["overlay.feature.intents"] = FakeResponse(output={"intents": []})
    return FakeLLM(script=script)


def _considered(client, source: str, *, hypothesis="customers churn when balances drop",
                target=None, contract_version=2, intent_id=None):
    payload = {
        "hypothesis": hypothesis, "objective": "predict churn",
        "catalog_source": source, "contract_version": contract_version,
        "confirmed_scope": {"primary": CHURN, "secondary": [], "expansion": "exact"},
    }
    if target:
        payload["target_ref"] = target
    if intent_id:
        payload["intent_id"] = intent_id
    res = client.post("/contract/considered-set", json=payload, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def _blockers(body, option_id, action):
    for section in ("recommended_options", "actionable_options"):
        for entry in body.get(section, ()):
            if entry["option_id"] == option_id:
                return ({b["code"] for b in entry["blocked_actions"].get(action, ())},
                        set(entry["allowed_actions"]))
    raise AssertionError(f"{option_id} in neither section")


def _cards(body):
    return [f for s in body["alternatives"] for f in s["features"]]


def _evidence_codes(client, body, name_prefix: str) -> set:
    """Every code the STORED decision record serves for the named candidate — the D1 wire
    evidence surface (route out): verdict reason codes, validation refusals + requirements."""
    card = next(c for c in _cards(body)
                if (c.get("name") or "").startswith(name_prefix)
                or (c.get("source_definition_id") or "").startswith(name_prefix)
                or name_prefix in (c.get("description") or ""))
    revision = body["considered_revision_id"]
    detail = client.get(
        f"/contract/considered-revisions/{revision}/options/{card['option_id']}",
        headers=AUTH)
    assert detail.status_code == 200, detail.text
    record = detail.json().get("decision_record")
    assert record, "the served option froze a decision record"
    evidence = record["evidence"]
    codes: set = set()
    for verdict in evidence.get("verdicts", []):
        codes.update(verdict.get("reason_codes", []))
    validation = evidence.get("validation", {})
    for refusal in validation.get("refusals", []):
        codes.add(refusal.get("code"))
    for req in validation.get("requirements", []):
        codes.add(req.get("code"))
    return codes


def _wire_codes(body):
    """Every refusal/requirement code visible anywhere on the wire response."""
    codes = set()
    for r in body.get("rejected_outputs", []) + body.get("rejections", []):
        codes.add(r.get("code"))
    for card in _cards(body):
        for req in card.get("requirements", []):
            codes.add(req.get("code"))
    for section in ("recommended_options", "actionable_options"):
        for entry in body.get(section, ()):
            for blocked in entry.get("blocked_actions", {}).values():
                for b in blocked:
                    codes.add(b["code"])
    return codes


# ── the 14 cases ───────────────────────────────────────────────────────────────────────────────

def test_01_an_identifier_is_never_a_generic_measure(make_client, conn, monkeypatch):
    """§10 row 1: "`cust_num` as a generic numeric measure — hard refusal: identifier is not
    a measure." The model PROPOSES the identifier as the quantity; the serving path refuses
    it structurally — no confirmation can promote it."""
    source = "gold01"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = _cib_rows(source)
    _catalog(conn, source, rows)
    client = make_client(llm_client=_fake(_intent(
        [{"role": "who", "concept": "customer_id", "operand_class": "entity_key"},
         {"role": "amount", "concept": "customer_id", "operand_class": "measure"},
         {"role": "when", "concept": "event_timestamp",
          "operand_class": "event_timestamp"}], name="identifier_as_measure")))
    body = _considered(client, source)
    codes = _evidence_codes(client, body, "identifier_as_measure")
    assert "IDENTIFIER_NOT_A_MEASURE" in codes
    card = next(c for c in _cards(body)
                if "identifier_as_measure" in (c.get("description") or ""))
    assert card.get("candidate_status") not in ("", None), \
        "never served as a clean bindable card"


def test_02_a_proposed_key_is_visible_but_cannot_govern(make_client, conn, monkeypatch):
    """§10 row 2: "`cust_num` as entity key with only llm/proposed concept — visible as
    provisional; cannot govern." The card serves; create_contract is blocked with the funnel
    step; save_idea stays open."""
    source = "gold02"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = _cib_rows(source)
    _catalog(conn, source, rows)
    _propose_concepts(conn, source, rows)
    client = make_client(llm_client=_fake())
    body = _considered(client, source)
    card = next(c for c in _cards(body)
                if (c.get("source_definition_id") or "").startswith("complaint_count"))
    blocked, allowed = _blockers(body, card["option_id"], "create_contract")
    assert "PROPOSED_METADATA_ONLY" in blocked
    assert "save_idea" in allowed


def test_03_confirmation_makes_the_key_eligible_with_uniqueness_still_owed(
        make_client, conn, monkeypatch):
    """§10 row 3: "Same key after human concept confirmation — eligible for key/grouping
    roles; still subject to uniqueness check." The funnel clears the floor; the runtime
    grain-uniqueness data check stays NAMED on the card."""
    source = "gold03"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = _cib_rows(source)
    _catalog(conn, source, rows)
    _propose_concepts(conn, source, rows)
    client = make_client(llm_client=_fake())
    _confirm_concepts(client, source)
    body = _considered(client, source)
    card = next(c for c in _cards(body)
                if (c.get("source_definition_id") or "").startswith("complaint_count"))
    blocked, _allowed = _blockers(body, card["option_id"], "create_contract")
    assert "PROPOSED_METADATA_ONLY" not in blocked
    codes = {r["code"] for r in card.get("requirements", [])}
    assert "GRAIN_IS_UNIQUE" in codes, \
        "the identifier-uniqueness data check, in the card's closed legacy vocabulary"


def test_04_an_event_window_over_a_declared_snapshot_refuses(make_client, conn, monkeypatch):
    """§10 row 4: "Event-window transaction feature over a current snapshot — hard refusal or
    named event-history setup requirement." The table is DECLARED a snapshot; the event-
    anchored candidate refuses with the named code."""
    from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
    from featuregen.overlay.upload.object_ref import normalize_ref

    source = "gold04"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = _cib_rows(source)
    _catalog(conn, source, rows)
    logical = normalize_ref(source, "public", "bo_cib_customer", None)
    record_field_evidence(
        conn, logical_ref=logical, field_name="event_or_snapshot", proposed_value="snapshot",
        producer="source", strength="attested", producer_ref="src:manifest",
        source_snapshot_id="gold-v1",
        input_hash=field_input_hash(logical_ref=logical, field_name="event_or_snapshot",
                                    material="snapshot"))
    conn.execute(
        "UPDATE graph_node SET event_or_snapshot = 'snapshot' "
        "WHERE catalog_source = %s AND kind = 'table'", (source,))
    client = make_client(llm_client=_fake())
    body = _considered(client, source)
    codes = _evidence_codes(client, body, "complaint_count")
    assert "SNAPSHOT_CANNOT_SUPPORT_EVENT_WINDOW" in codes


def test_05_a_balance_never_sums_across_time(make_client, conn, monkeypatch):
    """§10 row 5: "Account balance summed across time — refuse unless the declared stock/flow
    and aggregation policy permits it." The DECLARED additivity is the fact: a column whose
    catalog declares semi_additive (a stock) bound into a SUMMING recipe under an event
    anchor refuses by name — the declared stock/flow law, end to end."""
    source = "gold05"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = [
        (CanonicalRow(source, "bo_cib_customer", "cust_num", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(source, "bo_cib_customer", "acct_ref", "integer"), "account_id"),
        (CanonicalRow(source, "bo_cib_customer", "book_status", "text"), "booking_status"),
        # MISDECLARED: enriched as a flow, DECLARED semi_additive — a stock in flow's clothes.
        (CanonicalRow(source, "bo_cib_customer", "amount", "numeric",
                      additivity="semi_additive", currency="USD"), "monetary_flow"),
        (CanonicalRow(source, "bo_cib_customer", "event_ts", "timestamp"),
         "event_timestamp"),
        (CanonicalRow(source, "bo_cib_customer", "complaint_flag", "boolean"),
         "complaint_event"),
        (CanonicalRow(source, "bo_cib_customer", "as_of_date", "timestamp", as_of=True),
         "as_of_date"),
    ]
    _catalog(conn, source, rows)
    client = make_client(llm_client=_fake())
    body = _considered(client, source)
    codes = _evidence_codes(client, body, "rfm_monetary_amount")
    assert "ADDITIVITY_INCOMPATIBLE" in codes


def test_06_mixed_currencies_require_the_conversion_policy(make_client, conn, monkeypatch):
    """§10 row 6: "Transaction amount with mixed currencies — require exact conversion policy
    and currency source." rfm_monetary_amount expects per-row currency; an amount column with
    NO currency fact carries the named policy requirement — never a silent mixed-unit sum."""
    source = "gold06"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = [
        (CanonicalRow(source, "bo_cib_customer", "cust_num", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(source, "bo_cib_customer", "acct_ref", "integer"), "account_id"),
        (CanonicalRow(source, "bo_cib_customer", "book_status", "text"), "booking_status"),
        (CanonicalRow(source, "bo_cib_customer", "amount", "numeric",
                      additivity="additive"), "monetary_flow"),        # NO currency fact
        (CanonicalRow(source, "bo_cib_customer", "event_ts", "timestamp"),
         "event_timestamp"),
        (CanonicalRow(source, "bo_cib_customer", "complaint_flag", "boolean"),
         "complaint_event"),
        (CanonicalRow(source, "bo_cib_customer", "as_of_date", "timestamp", as_of=True),
         "as_of_date"),
    ]
    _catalog(conn, source, rows)
    client = make_client(llm_client=_fake())
    body = _considered(client, source)
    codes = _evidence_codes(client, body, "rfm_monetary_amount")
    assert "CURRENCY_POLICY_MISSING" in codes


def test_07_opposing_legs_on_one_column_refuse_without_a_sign_representation(
        make_client, conn, monkeypatch):
    """§10 row 7: "Debit and credit legs bound to one column — refuse unless a governed
    sign/direction policy separates them." fan_in_fan_out's payer/payee legs (one distinct
    group) land on the catalog's ONE identity column — blocked, with the resolution naming
    the governed representations. (The model cannot even EXPRESS distinct groups — the wire
    schema whitelists role/concept/class — so the law binds at the recipe surface.)"""
    source = "gold07"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = [
        (CanonicalRow(source, "transactions", "cust_num", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(source, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow(source, "transactions", "event_ts", "timestamp"), "event_timestamp"),
    ]
    _catalog(conn, source, rows)
    client = make_client(llm_client=_fake())
    body = _considered(client, source,
                       hypothesis="rapid pass-through flows signal layering")
    def _post_aml():
        res = client.post("/contract/considered-set", json={
            "hypothesis": "rapid pass-through flows signal layering",
            "objective": "detect suspicious flows", "catalog_source": source,
            "contract_version": 2,
            "confirmed_scope": {"primary": "aml_cft.suspicious_transaction_monitoring",
                                "secondary": [], "expansion": "exact"},
        }, headers=AUTH)
        assert res.status_code == 200, res.text
        return res.json()
    body = _post_aml()
    codes = _evidence_codes(client, body, "fan_in_fan_out")
    assert "DISTINCT_BINDING_VIOLATED" in codes


def test_08_status_policies_are_exact_never_inferred_from_prose(make_client, conn, monkeypatch):
    """§10 row 8: "Posted transactions containing reversals — apply exact eligible-status/
    reversal policy; do not infer from prose." Recipes referencing governed status policies
    carry STATUS_POLICY_UNRESOLVED as named setup work until a resolver serves them."""
    source = "gold08"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = [
        *_cib_rows(source),
        # A status-bearing transaction shape, so a recipe that READS a governed status
        # policy (rfm_recency_days: account + booking_status + event time) actually binds.
        (CanonicalRow(source, "bo_cib_customer", "acct_ref", "integer"), "account_id"),
        (CanonicalRow(source, "bo_cib_customer", "book_status", "text"), "booking_status"),
    ]
    _catalog(conn, source, rows)
    client = make_client(llm_client=_fake())
    body = _considered(client, source)
    # Any recipe candidate whose operands reference a governed status policy carries the
    # named setup work in its FROZEN record (raw gauntlet vocabulary, never inferred prose).
    revision = body["considered_revision_id"]
    rows = conn.execute(
        "SELECT outstanding_requirement_codes FROM semantic_option_decision "
        "WHERE considered_revision_id = %s", (revision,)).fetchall()
    all_codes = {code for (codes,) in rows for code in codes}
    assert "STATUS_POLICY_UNRESOLVED" in all_codes


def test_09_a_cross_table_feature_requires_the_verified_relationship(
        make_client, conn, monkeypatch):
    """§10 row 9: "Customer feature joining transaction and customer master — require
    verified relationship, cardinality and PIT-safe path." Operands spanning two tables
    refuse as ONE feature until the relationship is governed."""
    source = "gold09"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = [
        (CanonicalRow(source, "customer_master", "cust_num", "integer", is_grain=True,
                      entity="Customer"), "customer_id"),
        (CanonicalRow(source, "transactions", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow(source, "transactions", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(source, "customer_master", "complaint_flag", "boolean"),
         "complaint_event"),
        (CanonicalRow(source, "customer_master", "as_of_date", "timestamp", as_of=True),
         "as_of_date"),
    ]
    _catalog(conn, source, rows)
    client = make_client(llm_client=_fake(_intent(
        [{"role": "who", "concept": "customer_id", "operand_class": "entity_key"},
         {"role": "amount", "concept": "monetary_flow", "operand_class": "measure"},
         {"role": "when", "concept": "event_timestamp",
          "operand_class": "event_timestamp"}], name="cross_table_flow")))
    body = _considered(client, source)
    codes = _evidence_codes(client, body, "cross_table_flow")
    assert "RELATIONSHIP_REQUIRED" in codes


def test_10_read_allowed_is_not_use_allowed(make_client, conn, monkeypatch):
    """§10 row 10: "Sensitive customer attribute allowed to read but not use — refuse feature
    activation under purpose/use policy." A readable pep_flag with no active use policy is
    visible but PERSONAL_DATA_POLICY_REQUIRED blocks activation."""
    source = "gold10"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = [
        *_cib_rows(source),
        (CanonicalRow(source, "bo_cib_customer", "pep_ind", "text"), "pep_flag"),
    ]
    _catalog(conn, source, rows)
    client = make_client(llm_client=_fake(_intent(
        [{"role": "who", "concept": "customer_id", "operand_class": "entity_key"},
         {"role": "risk", "concept": "pep_flag", "operand_class": "dimension"},
         {"role": "when", "concept": "event_timestamp",
          "operand_class": "event_timestamp"}], name="pep_gated")))
    body = _considered(client, source)
    codes = _evidence_codes(client, body, "pep_gated")
    assert "PERSONAL_DATA_POLICY_REQUIRED" in codes


def test_11_variants_are_explicit_and_the_hypothesis_selects(make_client, conn, monkeypatch):
    """§10 row 11: "30/90/180-day recipe — produce explicit bounded variants or select the
    hypothesis-compatible variant." A "90 day" hypothesis leads with @window=90 and the card
    names the untaken parameterisations."""
    source = "gold11"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = _cib_rows(source)
    _catalog(conn, source, rows)
    client = make_client(llm_client=_fake())
    body = _considered(client, source,
                       hypothesis="complaints in the last 90 days precede churn")
    card = next(c for c in _cards(body)
                if (c.get("source_definition_id") or "").startswith("complaint_count"))
    assert card["source_definition_id"].endswith("@window=90"), \
        "the hypothesis-compatible variant leads"
    assert "[90]" in (card.get("param_alternatives") or ""), \
        "the untaken parameterisations are named with the chosen value marked"


def test_12_metadata_drift_after_consideration_never_silently_rebinds(
        make_client, conn, monkeypatch):
    """§10 row 12: "Metadata change after consideration — draft returns stale/regenerate;
    never silently rebinds." The sealed snapshot is re-verified at the durable write."""
    source = "gold12"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    monkeypatch.setattr(
        "featuregen.overlay.upload.contract.gate1._on_repeatable_read", lambda conn: True)
    monkeypatch.setattr(
        "featuregen.overlay.upload.feature_metadata_snapshot._assert_repeatable_read",
        lambda conn: "repeatable read")
    rows = _cib_rows(source)
    _catalog(conn, source, rows)
    _propose_concepts(conn, source, rows)
    client = make_client(llm_client=_fake())
    _confirm_concepts(client, source)
    body = _considered(client, source)
    card = next(c for c in _cards(body) if c.get("source_definition_id"))

    conn.execute(
        "UPDATE graph_node SET additivity = 'non_additive' "
        "WHERE catalog_source = %s AND column_name = 'balance'", (source,))
    res = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_option_id": card["name"],
        "expected_generation_run_id": body["generation_run_id"],
    }, headers=AUTH)
    assert res.status_code == 409, res.text
    detail = json.dumps(res.json())
    assert "SNAPSHOT" in detail or "ACTIVATION_BLOCKED" in detail


def test_13_a_revoked_review_refuses_activation_at_the_governing_write(
        make_client, conn, monkeypatch):
    """§10 row 13: "Recipe review superseded by revision change — activation refused until
    the new revision is reviewed." The current-layer re-read: a review REVOKED between
    serving and drafting blocks with RECIPE_REVIEW_NOT_CURRENT — the frozen yes is never
    enough."""
    from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
    from featuregen.overlay.upload.recipe_review_validity import required_reviewer_roles

    source = "gold13"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = _cib_rows(source)
    _catalog(conn, source, rows)
    _propose_concepts(conn, source, rows)
    client = make_client(llm_client=_fake())
    _confirm_concepts(client, source)

    hero = v2_recipe_by_id("complaint_count")
    live_hash = canonical_recipe_v2_hash(hero)
    identities = (AUTH, {"X-User": "ravi", "X-Roles": "platform_admin"})
    for i, role in enumerate(required_reviewer_roles(hero)):
        rr = client.post("/recipes/complaint_count/reviews", headers=identities[i % 2],
                         json={"decision": "approved", "reviewer_role": role,
                               "reviewed_revision_hash": live_hash,
                               "rationale": "gold corpus review"})
        assert rr.status_code == 201, rr.text

    body = _considered(client, source)
    card = next(c for c in _cards(body)
                if (c.get("source_definition_id") or "").startswith("complaint_count"))
    _blocked, allowed = _blockers(body, card["option_id"], "create_contract")

    # The supersession: a blocking re-decision lands AFTER serving — the re-read refuses.
    rr = client.post("/recipes/complaint_count/reviews", headers=AUTH, json={
        "decision": "changes_required", "reviewer_role": "banking_sme",
        "reviewed_revision_hash": live_hash,
        "rationale": "definition under re-review"})
    assert rr.status_code == 201, rr.text
    res = client.post("/contract/draft", json={
        "intent_id": body["intent_id"], "chosen_option_id": card["name"],
        "expected_generation_run_id": body["generation_run_id"],
    }, headers=AUTH)
    assert res.status_code == 409, res.text
    codes = {b["code"] for b in res.json()["detail"]["blockers"]}
    assert "RECIPE_REVIEW_NOT_CURRENT" in codes


def test_14_a_v2_formula_never_downgrades_into_a_v1_materializer(
        make_client, conn, monkeypatch):
    """§10 row 14: "Formula V2 presented to V1-only materializer — honest unsupported state;
    never automatic downgrade." Every served option's materialization rung carries
    FORMULA_SCHEMA_UNSUPPORTED (the engine does not advertise the schema) and the action is
    never allowed."""
    source = "gold14"
    monkeypatch.setenv("FEATUREGEN_SEMANTIC_PLANNING", "semantic_v1")
    rows = _cib_rows(source)
    _catalog(conn, source, rows)
    client = make_client(llm_client=_fake())
    body = _considered(client, source)
    checked = 0
    for section in ("recommended_options", "actionable_options"):
        for entry in body.get(section, ()):
            assert "request_materialization" not in entry["allowed_actions"]
            codes = {b["code"]
                     for b in entry["blocked_actions"].get("request_materialization", ())}
            assert "FORMULA_SCHEMA_UNSUPPORTED" in codes, entry["option_id"]
            checked += 1
    assert checked > 0
