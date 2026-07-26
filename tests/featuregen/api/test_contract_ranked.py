"""Phase-2A Task A3 — POST /contract/considered-set ranks the ELIGIBLE set over a precomputed rankable
set, behind ``FEATUREGEN_INTENT_RANKING`` (default off).

Proves the wiring end to end on the same two-family catalog as ``test_contract_scoped``:

* flag OFF → the scoped response is byte-identical to Task-7 (NO ``ranking`` / ``ranking_version`` keys);
* ``rankable_recipe_ids`` — the ONE place ``FinalDisposition`` is read — returns only ``ELIGIBLE`` ids;
* flag ON → ``ranking`` is present, ordered by ``canonical_rank``, the initial view respects the family
  cap, ONLY eligible recipes are ranked (out-of-scope / unbuildable / rejected are absent), and
  ``ranking_version`` is stamped;
* the three presentation layers stay SEPARATE — the deterministic ``ranking`` is present alongside the
  LLM ``recommendation`` and never merged with it.
"""
from tests.featuregen.api._helpers import AUTH

from featuregen.api.routes.contract import rankable_recipe_ids
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import field_input_hash, record_field_evidence
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.taxonomy.disposition import (
    FinalDisposition,
    RecipeEvaluation,
    StageEvaluation,
    StageStatus,
)
from featuregen.overlay.upload.taxonomy.recognition import APPLICABILITY_MAPPING_VERSION
from featuregen.overlay.upload.templates import ALL_TEMPLATES

RANK_FLAG = "FEATUREGEN_INTENT_RANKING"
SCOPE_FLAG = "FEATUREGEN_INTENT_SCOPED_APPLICABILITY"
FORMULA_SHADOW_FLAG = "FEATUREGEN_RECIPE_FORMULA_SHADOW"
CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops"
TARGET = "public.accounts.churned"
CHURN_RECIPE = "balance_trend"
CREDIT_RECIPE = "credit_utilisation"   # a non-churn family → out_of_scope under a churn narrowing
FRAUD_RECIPE = "txn_velocity_spike"

_FAMILY_BY_ID = {t.id: t.family for t in ALL_TEMPLATES}
_PER_FAMILY_CAP = 3   # rank_eligible's default; the initial view holds at most this many per family


def _fake() -> FakeLLM:
    return FakeLLM(script={
        "overlay.feature.recommend": FakeResponse(output={"features": [
            {"name": "avg_balance_90d", "derives_from": ["public.accounts.balance"],
             "aggregation": "avg_90d"}]}),
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    })


def _bank_multi(conn) -> None:
    """A TWO-family catalog: an ``accounts`` table the retail_churn recipes ground on, PLUS a
    ``facilities`` table the credit recipes ground on — so a churn narrowing leaves the credit/fraud
    families out of scope. Mirrors test_contract_scoped's catalog."""
    from datetime import UTC, datetime
    # Fresh as of the test run — the route grounds against the real wall clock, so a hardcoded past
    # date rots the freshness gate once that date passes.
    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "amount", "numeric", additivity="additive", currency="USD"),
         "monetary_flow"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
        (CanonicalRow("bank", "facilities", "facility_id", "integer", is_grain=True, entity="Facility"),
         "facility_id"),
        (CanonicalRow("bank", "facilities", "drawn", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "facilities", "credit_limit", "numeric", currency="USD"), "limit"),
        (CanonicalRow("bank", "facilities", "asof2", "timestamp", as_of=True), "as_of_date"),
    ]
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog}
    build_graph(conn, "bank", rows, concepts=concepts)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (now, now))


def _post_churn_scoped(client) -> dict:
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "target_ref": TARGET,
        "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed"}}, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def _merchant_catalog(conn) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow(
            "merchant_bank", "tx", "merchant_id", "string",
            is_grain=True, entity="Merchant"), "merchant_id"),
        (CanonicalRow(
            "merchant_bank", "tx", "mcc", "string"), "mcc"),
        (CanonicalRow(
            "merchant_bank", "tx", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(
            "merchant_bank", "tx", "ingested_at", "timestamp",
            as_of=True, as_of_basis="ingested_at"), "as_of_date"),
        (CanonicalRow(
            "merchant_bank", "tx", "fraud_flag", "boolean"), "outcome_label"),
    ]
    build_graph(
        conn,
        "merchant_bank",
        [row for row, _concept in catalog],
        concepts={content_hash(row): concept for row, concept in catalog},
    )
    for row, concept in catalog[:3]:
        ref = normalize_ref("merchant_bank", None, row.table, row.column)
        record_field_evidence(
            conn,
            logical_ref=ref,
            field_name="concept",
            proposed_value=concept,
            producer=EvidenceProducer.HUMAN,
            strength=AssertionStrength.CONFIRMED,
            producer_ref="human:test",
            source_snapshot_id="snapshot:test",
            input_hash=field_input_hash(
                logical_ref=ref, field_name="concept", material=concept),
        )
    event_ref = normalize_ref("merchant_bank", None, "tx", "event_ts")
    record_field_evidence(
        conn,
        logical_ref=event_ref,
        field_name="temporal_role",
        proposed_value="event",
        producer=EvidenceProducer.HUMAN,
        strength=AssertionStrength.CONFIRMED,
        producer_ref="human:test",
        source_snapshot_id="snapshot:test",
        input_hash=field_input_hash(
            logical_ref=event_ref, field_name="temporal_role", material="event"),
    )
    resolve_and_project(
        conn,
        source="merchant_bank",
        logical_refs=[event_ref],
        fields=["temporal_role"],
    )
    conn.execute(
        "UPDATE graph_node SET grain_fact_event_id='grain-merchant-test' "
        "WHERE catalog_source='merchant_bank' "
        "AND object_ref='public.tx.merchant_id'",
    )
    conn.execute(
        "INSERT INTO overlay_drift_watermark "
        "(catalog_source,last_completed_at,last_run_id,head_seq) "
        "VALUES ('merchant_bank',%s,'r',0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at=%s",
        (now, now),
    )


def _stage(status: StageStatus) -> StageEvaluation:
    return StageEvaluation(status, (), "v", None)


def _ev(recipe_id: str, disposition: FinalDisposition, tier: str | None = "primary") -> RecipeEvaluation:
    st = _stage(StageStatus.COMPLETED)
    return RecipeEvaluation(recipe_id, st, st, st, disposition, tier)


# ── rankable_recipe_ids: the ONE FinalDisposition read → only ELIGIBLE ids, order preserved ────────────
def test_rankable_recipe_ids_returns_only_eligible():
    evs = [
        _ev("a", FinalDisposition.ELIGIBLE),
        _ev("b", FinalDisposition.OUT_OF_SCOPE, tier=None),
        _ev("c", FinalDisposition.UNBUILDABLE),
        _ev("d", FinalDisposition.SAFETY_REJECTED),
        _ev("e", FinalDisposition.ELIGIBLE),
    ]
    assert rankable_recipe_ids(evs) == ["a", "e"]
    assert rankable_recipe_ids([]) == []


# ── flag OFF: a scoped call is byte-identical to Task-7 (no ranking keys) ──────────────────────────────
def test_flag_off_scoped_call_has_no_ranking(make_client, conn, monkeypatch):
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.delenv(RANK_FLAG, raising=False)   # ranking OFF (default)
    _bank_multi(conn)

    body = _post_churn_scoped(make_client(_fake()))

    assert "ranking" not in body and "ranking_version" not in body
    # Exactly the Task-7 scoped key set — nothing added, nothing removed.
    assert set(body) == {"intent_id", "anchor", "alternatives", "recommendation", "rejections",
                         "generation_run_id", "scope_id", "dispositions", "in_scope_count"}


# ── flag ON: eligible set ranked, ordered, cap-respecting, non-eligible absent, versioned ─────────────
def test_flag_on_churn_scoped_ranks_eligible_set(make_client, conn, monkeypatch):
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    _bank_multi(conn)

    body = _post_churn_scoped(make_client(_fake()))

    assert "ranking" in body and body["ranking_version"] == APPLICABILITY_MAPPING_VERSION
    ranking = body["ranking"]
    assert ranking, "a churn-scoped run must rank at least one eligible recipe"
    ranked_ids = {r["recipe_id"] for r in ranking}
    assert CHURN_RECIPE in ranked_ids

    # ONLY eligible recipes are ranked — the rankable set (the one FinalDisposition read) exactly.
    eligible = {d["recipe_id"] for d in body["dispositions"]
                if d["final_disposition"] == "eligible"}
    assert ranked_ids == eligible
    # Out-of-scope / unbuildable / rejected recipes never appear in the ranking.
    non_eligible = {d["recipe_id"] for d in body["dispositions"]
                    if d["final_disposition"] != "eligible"}
    assert non_eligible, "the two-family catalog must leave some recipe non-eligible"
    assert ranked_ids.isdisjoint(non_eligible)
    assert CREDIT_RECIPE not in ranked_ids and FRAUD_RECIPE not in ranked_ids

    # Ordered by a dense, 1-based canonical_rank (stable total order).
    assert [r["canonical_rank"] for r in ranking] == list(range(1, len(ranking) + 1))

    # Initial view RESPECTS the family cap: no family contributes more than per_family_cap recipes.
    selected = [r for r in ranking if r["selected_for_initial_view"]]
    per_family: dict[str, int] = {}
    for r in selected:
        fam = _FAMILY_BY_ID[r["recipe_id"]]
        per_family[fam] = per_family.get(fam, 0) + 1
    assert all(count <= _PER_FAMILY_CAP for count in per_family.values()), per_family
    # Fewer eligible than the initial-view size here, so every ranked recipe fits the initial view, and
    # each carries its OWN initial-view reason stream (separate from rank_reasons).
    assert selected == ranking
    for r in selected:
        assert "selected_initial_view" in r["initial_view_reasons"]
        assert isinstance(r["rank_reasons"], list)


# ── three layers separate: the LLM recommendation is present AND distinct from the ranking ────────────
def test_recommendation_is_present_and_distinct_from_ranking(make_client, conn, monkeypatch):
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    _bank_multi(conn)

    body = _post_churn_scoped(make_client(_fake()))

    # Layer 2 — the advisory LLM recommendation — is still present and unchanged.
    assert body["recommendation"] is not None
    assert body["recommendation"]["recommended_lens"] == "monetary"
    # Layer 1 — the deterministic ranking — is a DISTINCT structure (a list of per-recipe projections),
    # never merged into the recommendation.
    assert isinstance(body["ranking"], list)
    assert isinstance(body["recommendation"], dict)
    assert body["ranking"] != body["recommendation"]
    # The recommendation carries no ranking fields and the ranking carries no lens/reasoning — separate.
    assert "canonical_rank" not in body["recommendation"]
    assert all("recommended_lens" not in r for r in body["ranking"])


def test_formula_shadow_ranking_disabled_records_complete_zero_manifest(
    make_client, conn, monkeypatch
):
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.delenv(RANK_FLAG, raising=False)
    monkeypatch.setenv(FORMULA_SHADOW_FLAG, "1")
    _bank_multi(conn)

    body = _post_churn_scoped(make_client(_fake()))

    assert "ranking" not in body
    expected = conn.execute(
        "SELECT ranking_flag, expected_manifest_id "
        "FROM recipe_formula_shadow_expected_run "
        "WHERE generation_run_id=%s",
        (body["generation_run_id"],),
    ).fetchone()
    assert expected and expected[0] is False
    manifest = conn.execute(
        "SELECT status, expected_observation_count, actual_observation_count, capture_axis "
        "FROM recipe_formula_shadow_run_manifest WHERE manifest_id=%s",
        (expected[1],),
    ).fetchone()
    assert manifest == ("COMPLETE", 0, 0, "SKIPPED_RANKING_DISABLED")


def test_formula_shadow_expected_declaration_failure_is_loud_503(
    make_client, conn, monkeypatch
):
    import featuregen.api.routes.contract as route

    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(FORMULA_SHADOW_FLAG, "1")
    _bank_multi(conn)

    def _fail(*args, **kwargs):
        raise RuntimeError("store unavailable")

    monkeypatch.setattr(route, "declare_expected_run", _fail)
    response = make_client(_fake()).post(
        "/contract/considered-set",
        json={
            "hypothesis": HYPOTHESIS,
            "objective": "predict churn",
            "catalog_source": "bank",
            "target_ref": TARGET,
            "confirmed_scope": {
                "primary": CHURN,
                "confirmation_source": "user_confirmed",
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 503
    assert response.json()["detail"] == "SHADOW_EXPECTATION_STORE_UNAVAILABLE"


def test_formula_shadow_positive_route_creates_immutable_work_item(
    make_client, conn, monkeypatch
):
    import psycopg

    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    monkeypatch.setenv(FORMULA_SHADOW_FLAG, "1")
    _merchant_catalog(conn)
    response = make_client(_fake()).post(
        "/contract/considered-set",
        json={
            "hypothesis": "merchant category breadth may indicate merchant fraud",
            "objective": "identify merchant fraud",
            "catalog_source": "merchant_bank",
            "target_ref": "public.tx.fraud_flag",
            "confirmed_scope": {
                "primary": "fraud.merchant_fraud",
                "confirmation_source": "user_confirmed",
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    selected = {
        item["recipe_id"]
        for item in body["ranking"]
        if item["selected_for_initial_view"]
    }
    merchant_disposition = next(
        item for item in body["dispositions"]
        if item["recipe_id"] == "merchant_mcc_diversity"
    )
    import json

    assert "merchant_mcc_diversity" in selected, json.dumps({
        "disposition": merchant_disposition,
        "ranking": body["ranking"],
    }, indent=2)
    work = conn.execute(
        "SELECT recipe_id,metadata_snapshot_id,binding_envelope_json,"
        "provider_input_json FROM recipe_formula_shadow_work_item "
        "WHERE generation_run_id=%s",
        (body["generation_run_id"],),
    ).fetchall()
    observations = conn.execute(
        "SELECT recipe_id,capture_axis,authority_axis,technical_axis "
        "FROM recipe_formula_shadow_observation WHERE generation_run_id=%s",
        (body["generation_run_id"],),
    ).fetchall()
    assert len(work) == 1, observations
    assert work[0][0] == "merchant_mcc_diversity"
    assert work[0][1]
    assert work[0][2]["event_time_facts"][0]["temporal_role"] == "event"
    assert work[0][3]["prediction_goal"] == "identify merchant fraud"
    assert conn.execute(
        "SELECT count(*) FROM outbox "
        "WHERE topic='recipe_formula_shadow.requested.v1'"
    ).fetchone()[0] == 1


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Phase-2B Task B3 — the confirmed DIMENSIONS feed the ranker + surface SOFT warnings, and NEVER reject.
#
# The modelling-context fit and the soft entity-grain signal ride the SAME precomputed rankable set as
# A3. A confirmed ``modelling_contexts`` lifts a framework-specific recipe above an equal-tier generic
# one; a confirmed ``target_entity`` never moves a recipe ``out_of_scope`` — it only nudges the rank and
# surfaces an ``entity_grain_mismatch`` / ``modelling_context_conflict`` warning per recipe.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
IFRS9_RECIPE = "stage_migration"        # tagged ifrs9_staging; grain=facility → REQUIRED_MATCH under ifrs9
GENERIC_CREDIT_RECIPE = "credit_utilisation"   # no framework tag; grain=facility → COMPATIBLE under ifrs9


def _bank_ifrs9(conn) -> None:
    """The two-family catalog of :func:`_bank_multi` PLUS an ``impairment_stage`` column on facilities so
    the ifrs9-tagged ``stage_migration`` recipe grounds — giving an eligible framework-specific recipe
    (REQUIRED_MATCH under confirmed ifrs9) alongside the generic ``credit_utilisation`` (COMPATIBLE),
    both at facility grain."""
    from datetime import UTC, datetime
    # Fresh as of the test run — the route grounds against the real wall clock, so a hardcoded past
    # date rots the freshness gate once that date passes.
    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow("bank", "accounts", "customer_id", "integer", is_grain=True, entity="Customer"),
         "customer_id"),
        (CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "accounts", "as_of_date", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "accounts", "amount", "numeric", additivity="additive", currency="USD"),
         "monetary_flow"),
        (CanonicalRow("bank", "accounts", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("bank", "accounts", "churned", "boolean"), "outcome_label"),
        (CanonicalRow("bank", "facilities", "facility_id", "integer", is_grain=True, entity="Facility"),
         "facility_id"),
        (CanonicalRow("bank", "facilities", "drawn", "numeric", additivity="semi_additive",
                      currency="USD"), "monetary_stock"),
        (CanonicalRow("bank", "facilities", "credit_limit", "numeric", currency="USD"), "limit"),
        (CanonicalRow("bank", "facilities", "asof2", "timestamp", as_of=True), "as_of_date"),
        (CanonicalRow("bank", "facilities", "imp_stage", "integer"), "impairment_stage"),
    ]
    rows = [r for r, _ in catalog]
    concepts = {content_hash(r): c for r, c in catalog}
    build_graph(conn, "bank", rows, concepts=concepts)
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (now, now))


def _post_unscoped(client, *, modelling_contexts=None, target_entity=None) -> dict:
    """An unscoped (fail-open) scoped run — every grounded recipe is a ``primary``-tier eligible, so the
    dimension signals are the ONLY thing separating equal-tier recipes. Optionally carries the two
    confirmed dimensions."""
    scope = {"unscoped": True, "confirmation_source": "user_confirmed"}
    if modelling_contexts is not None:
        scope["modelling_contexts"] = list(modelling_contexts)
    if target_entity is not None:
        scope["target_entity"] = target_entity
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "target_ref": TARGET, "confirmed_scope": scope}, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def _dispositions(body: dict) -> dict[str, str]:
    return {d["recipe_id"]: d["final_disposition"] for d in body["dispositions"]}


def _rank_by_id(body: dict) -> dict[str, int]:
    return {r["recipe_id"]: r["canonical_rank"] for r in body["ranking"]}


# ── a confirmed modelling context lifts a REQUIRED_MATCH recipe above an equal-tier COMPATIBLE one ─────
def test_confirmed_context_ranks_required_match_above_compatible(make_client, conn, monkeypatch):
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    _bank_ifrs9(conn)

    body = _post_unscoped(make_client(_fake()), modelling_contexts=("ifrs9",))

    ranks = _rank_by_id(body)
    # Both recipes ground at facility grain and are eligible (primary tier under an unscoped run).
    assert IFRS9_RECIPE in ranks and GENERIC_CREDIT_RECIPE in ranks
    dispo = _dispositions(body)
    assert dispo[IFRS9_RECIPE] == "eligible" and dispo[GENERIC_CREDIT_RECIPE] == "eligible"
    # The confirmed ifrs9 context (REQUIRED_MATCH) outranks the equal-tier generic recipe (COMPATIBLE):
    # the ranker actually consumed the Task-B3 fit.
    assert ranks[IFRS9_RECIPE] < ranks[GENERIC_CREDIT_RECIPE]


# ── a confirmed target_entity NEVER rejects: dispositions unchanged + a grain-mismatch warning surfaced ─
def test_confirmed_target_entity_warns_but_never_rejects(make_client, conn, monkeypatch):
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    _bank_ifrs9(conn)

    base = _post_unscoped(make_client(_fake()))                       # no target_entity
    scoped = _post_unscoped(make_client(_fake()), target_entity="obligor")

    # Dispositions are BYTE-identical — a soft target_entity moves NOTHING out_of_scope (facility only
    # DERIVES obligor; hard entity rejection is Phase-3).
    assert _dispositions(scoped) == _dispositions(base)
    # No recipe is out_of_scope on entity grounds — the facility-grain credit recipes stay eligible.
    assert _dispositions(scoped)[GENERIC_CREDIT_RECIPE] == "eligible"
    # …but a grain warning IS surfaced: a facility-grain recipe rolls up to obligor -> entity_grain_mismatch.
    warnings = scoped["signal_warnings"]
    assert "entity_grain_mismatch" in warnings.get(GENERIC_CREDIT_RECIPE, [])
    assert "entity_grain_mismatch" in warnings.get(IFRS9_RECIPE, [])
    # The no-dimension run surfaces no such warning (UNKNOWN grain -> silent).
    assert GENERIC_CREDIT_RECIPE not in base.get("signal_warnings", {})


# ── a confirmed context that CONFLICTS is a warning, not a reject ──────────────────────────────────────
def test_confirmed_context_conflict_is_a_warning_not_a_reject(make_client, conn, monkeypatch):
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    _bank_ifrs9(conn)

    body = _post_unscoped(make_client(_fake()), modelling_contexts=("frtb",))

    # The ifrs9-tagged recipe conflicts with a confirmed frtb context — but it is NOT rejected.
    assert _dispositions(body)[IFRS9_RECIPE] == "eligible"
    assert IFRS9_RECIPE in _rank_by_id(body)
    assert "modelling_context_conflict" in body["signal_warnings"].get(IFRS9_RECIPE, [])
    # The generic recipe is COMPATIBLE under frtb — no conflict warning.
    assert "modelling_context_conflict" not in body["signal_warnings"].get(GENERIC_CREDIT_RECIPE, [])


# ── Fix 5: a bogus modelling_context is CLEANED at the route boundary (dropped, never a reject) ────────
def test_bogus_modelling_context_is_dropped_at_the_boundary(make_client, conn, monkeypatch):
    """A hand-crafted bogus modelling_context is non-fatally CLEANED at the route boundary — dropped
    BEFORE ranking / warnings / persistence, so it raises no spurious modelling_context_conflict and
    writes no dimension row; a valid context alongside it is kept."""
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    _bank_ifrs9(conn)

    # A BOGUS-ONLY confirmed set: without cleaning it would clean to (), the ifrs9-tagged recipe's own
    # {ifrs9} would be disjoint from {not_a_framework} -> CONFLICT -> a SPURIOUS modelling_context_conflict.
    bogus = _post_unscoped(make_client(_fake()), modelling_contexts=("not_a_framework",))
    warnings = bogus["signal_warnings"]
    assert all("modelling_context_conflict" not in codes for codes in warnings.values())
    # NOTHING was written to the immutable dimension table for the bogus value.
    n = conn.execute(
        "SELECT count(*) FROM confirmed_scope_dimension WHERE scope_id = %s",
        (bogus["scope_id"],)).fetchone()[0]
    assert n == 0

    # A valid context ALONGSIDE a bogus one: the valid ifrs9 is kept + persisted, the bogus is dropped.
    mixed = _post_unscoped(make_client(_fake()),
                           modelling_contexts=("ifrs9", "not_a_framework"))
    contexts = conn.execute(
        "SELECT value FROM confirmed_scope_dimension "
        "WHERE scope_id = %s AND dimension = 'modelling_context' ORDER BY display_order",
        (mixed["scope_id"],)).fetchall()
    assert [c[0] for c in contexts] == ["ifrs9"]
    # The kept ifrs9 still drives the ifrs9-tagged recipe to REQUIRED_MATCH (no conflict warning).
    assert "modelling_context_conflict" not in mixed["signal_warnings"].get(IFRS9_RECIPE, [])


# ── Phase-3A Task 3A.5: the graph-backed grain resolver leaks NO provenance to the wire ────────────────
def _all_keys(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            yield k
            yield from _all_keys(v)
    elif isinstance(obj, list):
        for item in obj:
            yield from _all_keys(item)


def test_scoped_ranking_response_leaks_no_entity_graph_metadata(make_client, conn, monkeypatch):
    """A REAL scoped run that produces a DERIVABLE grain (facility -> obligor — the B3
    entity_grain_mismatch case above, now resolved by the 3A entity graph) must carry NONE of the graph
    provenance fields anywhere in its serialized response tree. This is the stronger, wire-level neutrality
    guard: the graph resolver DID run (proven by the mismatch warning), yet its graph_version / paths /
    reason-code provenance stays entirely internal to the ranking adapter."""
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    _bank_ifrs9(conn)

    body = _post_unscoped(make_client(_fake()), target_entity="obligor")

    # Non-vacuous: this really is the DERIVABLE path — the graph resolver ran and surfaced the roll-up.
    assert "entity_grain_mismatch" in body["signal_warnings"].get(GENERIC_CREDIT_RECIPE, [])
    # …yet none of the 3A entity-graph provenance appears ANYWHERE in the response JSON.
    keys = set(_all_keys(body))
    assert not (keys & {"graph_version", "paths", "paths_truncated", "relationship_version"})
