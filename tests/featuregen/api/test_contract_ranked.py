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
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
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
    now = datetime(2026, 7, 10, tzinfo=UTC)
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
    now = datetime(2026, 7, 10, tzinfo=UTC)
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
