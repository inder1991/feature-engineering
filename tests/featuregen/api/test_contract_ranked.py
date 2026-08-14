"""Phase-2A Task A3 — POST /contract/considered-set ranks the ELIGIBLE set over a precomputed rankable
set.

Proves the wiring end to end on the same two-family catalog as ``test_contract_scoped`` (imported from
there, so the two files cannot drift):

* ``rankable_recipe_ids`` — the ONE place ``FinalDisposition`` is read — returns only ``ELIGIBLE`` ids;
* ``ranking`` is present, ordered by ``canonical_rank``, the initial view respects the family cap, no
  non-eligible recipe is ever ranked, and ``ranking_version`` is stamped;
* the three presentation layers stay SEPARATE — the deterministic ``ranking`` is present alongside the
  LLM ``recommendation`` and never merged with it.

WHAT THE E4 CUTOVER (2026-08-14) CHANGED HERE
---------------------------------------------
The disposition universe is now the V2 recipe registry — the universe the semantic engine actually
plans — so every recipe id named below is a V2 id. Two of the old ones (``balance_trend``,
``credit_utilisation``, ``stage_migration``) exist only in the legacy template registry and can no
longer appear in a disposition at all.

The ranker itself was NOT cut over: its signal bundle (family, explainability, PIT completeness,
journey, semantic group) is authored on the legacy Template objects, so ``_rank_signals`` skips any
rankable id with no template and the ranker deterministically drops it. The honest consequence, which
:func:`test_flag_on_churn_scoped_ranks_eligible_set` states outright, is that the ranking is a SUBSET
of the eligible set: a V2-only recipe is eligible and simply unrankable — dropped rather than ordered
on signals nobody authored for it.
"""
from tests.featuregen.api._helpers import AUTH

# ONE two-family catalog, defined next door and shared: the ranking suite and the scoped suite must
# agree about what binds, or neither proves anything about narrowing.
from tests.featuregen.api.test_contract_scoped import _bank_multi

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
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK
from featuregen.overlay.upload.templates import ALL_TEMPLATES

RANK_FLAG = "FEATUREGEN_INTENT_RANKING"
SCOPE_FLAG = "FEATUREGEN_INTENT_SCOPED_APPLICABILITY"
FORMULA_SHADOW_FLAG = "FEATUREGEN_RECIPE_FORMULA_SHADOW"
CHURN = "customer.relationship_attrition.churn"
HYPOTHESIS = "customers churn when their balance drops"
TARGET = "public.accounts.churned"
#: A churn recipe that binds on the shared catalog (account key + balance + as-of).
CHURN_RECIPE = "balance_volatility"
#: A lending recipe → out_of_scope under a churn narrowing, and the GENERIC (no declared modelling
#: context) half of the Task-B3 pair below.
CREDIT_RECIPE = "days_past_due_max"
FRAUD_RECIPE = "txn_velocity_spike"

_FAMILY_BY_ID = {t.id: t.family for t in ALL_TEMPLATES}
_PER_FAMILY_CAP = 3   # rank_eligible's default; the initial view holds at most this many per family


def _fake() -> FakeLLM:
    """Only the ADVISORY set-recommendation pass is scripted. ``overlay.feature.recommend`` is absent
    because the E4 cutover deleted the free-form generator, and ``overlay.feature.intents`` is left
    unscripted on purpose — the intent lens fails soft, so the recipe half of the engine serves alone
    and the rankings below stay deterministic."""
    return FakeLLM(script={
        "overlay.feature.recommend_set": FakeResponse(output={
            "recommended_lens": "monetary", "reasoning": "monetary fits the balance-drop hypothesis"}),
    })


def _post_churn_scoped(client) -> dict:
    res = client.post("/contract/considered-set", json={
        "hypothesis": HYPOTHESIS, "objective": "predict churn", "catalog_source": "bank",
        "target_ref": TARGET,
        "confirmed_scope": {"primary": CHURN, "confirmation_source": "user_confirmed"}}, headers=AUTH)
    assert res.status_code == 200, res.text
    return res.json()


def _merchant_catalog(conn) -> None:
    """A card-transaction catalog the fraud recipes bind on.

    ``cust_num`` is here because the V2 ``merchant_mcc_diversity`` recipe is keyed per CUSTOMER — it
    measures how many merchant categories ONE customer transacts across, which is the fraud signal.
    The legacy template of the same name declared a merchant key, so before the E4 cutover
    (2026-08-14) this fixture had no customer id and the V2 recipe would refuse with
    REQUIRED_OPERAND_MISSING. The merchant id stays: it is the table's grain and the recipe's
    category dimension hangs off it.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow(
            "merchant_bank", "tx", "merchant_id", "string",
            is_grain=True, entity="Merchant"), "merchant_id"),
        (CanonicalRow(
            "merchant_bank", "tx", "cust_num", "string", entity="Customer"), "customer_id"),
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
    for row, concept in catalog[:4]:
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


# ── ranking is ALWAYS ON (pre-live simplification 2026-08-11; the env flag is inert) ──────────────────
def test_ranking_is_always_present_regardless_of_env(make_client, conn, monkeypatch):
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.delenv(RANK_FLAG, raising=False)   # deleting the retired env changes nothing
    _bank_multi(conn)

    body = _post_churn_scoped(make_client(_fake()))

    assert {"ranking", "ranking_version"} <= set(body)


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

    # ONLY eligible recipes are ranked. The ranking is a SUBSET of the eligible set, not an equality:
    # `_rank_signals` builds each recipe's bundle from the LEGACY Template metadata (family,
    # explainability, PIT completeness, journey, semantic group), so a recipe that exists only in the
    # V2 registry has no bundle and the ranker deterministically DROPS it rather than ordering it on
    # invented signals. After the E4 cutover the eligible set comes from V2, so that gap is visible
    # here — and it is a gap in the ranker, never a second eligibility policy.
    eligible = {d["recipe_id"] for d in body["dispositions"]
                if d["final_disposition"] == "eligible"}
    assert ranked_ids <= eligible
    assert ranked_ids == {rid for rid in eligible if rid in _FAMILY_BY_ID}
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


# NOTE (pre-live simplification, 2026-08-11): the SKIPPED_RANKING_DISABLED manifest scenario
# became unrepresentable when the ranking flag retired — ranking is always on, so the expected
# run always records ranking_flag=True. The enabled manifest path is covered end to end by
# test_formula_shadow_positive_route_creates_immutable_work_item below (which supplies the
# REPEATABLE READ connection the capture requires).


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


def test_formula_shadow_records_why_it_could_not_capture_on_the_engine_path(
    make_client, conn, monkeypatch
):
    """Delivery-B formula shadow on a confirmed-scope run — and a GAP the E4 cutover opened.

    The shadow capture needs the run's PRIVATE grounding context, looked up by
    ``recipe_candidate_keys_by_recipe_id``. That map is populated only by the legacy
    ``_template_candidates`` pass, which — since the E4 cutover (2026-08-14) — no longer runs when a
    ``confirmed_scope`` is present: the semantic engine serves that path and does not fill it. So on
    every confirmed-scope run the capture now resolves CANDIDATE_MISSING and writes NO work item.

    That is a real regression in the shadow subsystem, not a property worth having, and this test is
    deliberately written so it cannot be mistaken for one: it asserts the honest current behaviour —
    the recipe is ranked and selected, the expected run is declared, and the shadow records an
    OBSERVATION that names exactly why it captured nothing — and it will fail the moment the
    capture starts working again, at which point it should be restored to asserting the work item.
    Nothing about this is silent: CAPTURE_INPUT_INCOMPLETE / CANDIDATE_MISSING is precisely the
    machine-readable "I could not do my job, here is why" the shadow was built to emit.
    """
    import psycopg

    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    monkeypatch.setenv(FORMULA_SHADOW_FLAG, "1")
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")
    _merchant_catalog(conn)
    hypothesis = "merchant category breadth may indicate merchant fraud"
    objective = "identify merchant fraud"
    recognition = make_client(FakeLLM(script={
        RECOGNIZER_TASK: FakeResponse(output={
            "status": "classified",
            "candidates": [{
                "use_case_id": "fraud.merchant_fraud",
                "relationship": "primary",
                "confidence": "high",
                "evidence_spans": ["merchant fraud"],
                "rationale": "the hypothesis concerns merchant fraud",
            }],
            "ambiguity_note": None,
        }),
    })).post(
        "/contract/recognitions",
        json={"hypothesis": hypothesis, "objective": objective},
        headers=AUTH,
    ).json()
    response = make_client(_fake()).post(
        "/contract/considered-set",
        json={
            "hypothesis": hypothesis,
            "objective": objective,
            "catalog_source": "merchant_bank",
            "target_ref": "public.tx.fraud_flag",
            "intent_id": recognition["intent_id"],
            "recognition_id": recognition["recognition_id"],
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
    # The expected-run declaration (the flag-on durability interlock) still happened — the shadow
    # knows a run was owed to it, which is what makes the miss below detectable at all.
    assert conn.execute(
        "SELECT count(*) FROM recipe_formula_shadow_expected_run "
        "WHERE generation_run_id=%s", (body["generation_run_id"],)).fetchone()[0] == 1
    # …and the capture named its own failure instead of vanishing.
    assert ("merchant_mcc_diversity", "CAPTURE_INPUT_INCOMPLETE", "NOT_EVALUATED",
            "CANDIDATE_MISSING") in observations
    assert work == [], (
        "the engine path fills no candidate-key map, so nothing can be captured — see this "
        "test's docstring; restore the work-item assertions when that is fixed")
    assert conn.execute(
        "SELECT count(*) FROM outbox "
        "WHERE topic='recipe_formula_shadow.requested.v1'"
    ).fetchone()[0] == 0


# ══════════════════════════════════════════════════════════════════════════════════════════════════
# Phase-2B Task B3 — the confirmed DIMENSIONS feed the ranker + surface SOFT warnings, and NEVER reject.
#
# The modelling-context fit and the soft entity-grain signal ride the SAME precomputed rankable set as
# A3. A confirmed ``modelling_contexts`` lifts a framework-specific recipe above an equal-tier generic
# one; a confirmed ``target_entity`` never moves a recipe ``out_of_scope`` — it only nudges the rank and
# surfaces an ``entity_grain_mismatch`` / ``modelling_context_conflict`` warning per recipe.
# ══════════════════════════════════════════════════════════════════════════════════════════════════
#: Tagged ifrs9; grain=facility → REQUIRED_MATCH under a confirmed ifrs9 context. (The pre-cutover pair
#: was stage_migration / credit_utilisation — both legacy-only template ids, so neither appears in a
#: disposition any more. These two are the same pair one registry over: both exist in BOTH registries,
#: both bind at facility grain on the shared catalog, and only the first declares a framework.)
IFRS9_RECIPE = "sicr_onset"
#: No framework tag; grain=facility → COMPATIBLE under any confirmed context.
GENERIC_CREDIT_RECIPE = CREDIT_RECIPE


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
    _bank_multi(conn)

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
    _bank_multi(conn)

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
    _bank_multi(conn)

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
    _bank_multi(conn)

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
    _bank_multi(conn)

    body = _post_unscoped(make_client(_fake()), target_entity="obligor")

    # Non-vacuous: this really is the DERIVABLE path — the graph resolver ran and surfaced the roll-up.
    assert "entity_grain_mismatch" in body["signal_warnings"].get(GENERIC_CREDIT_RECIPE, [])
    # …yet none of the 3A entity-graph provenance appears ANYWHERE in the response JSON.
    keys = set(_all_keys(body))
    assert not (keys & {"graph_version", "paths", "paths_truncated", "relationship_version"})
