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

THE RANKER IS NOW CUT OVER TOO (E4 follow-up, same date). The cutover left ``_rank_signals`` keyed on
the LEGACY ``Template`` registry, so it skipped any rankable id with no template and the ranker
deterministically dropped it — only the ~106 ids in both registries could ever be ranked, and an
eligible V2-only recipe was silently absent from ``ranking``, from the initial view, and from
formula-shadow capture selection. The profiles now come from the V2 registry
(``ranking_signals.v2_rank_profiles``), so the ranking is the WHOLE eligible set, which is what
:func:`test_flag_on_churn_scoped_ranks_eligible_set` asserts as an equality below. The five ordering
axes are unchanged; ``ranking_version`` moved off ``APPLICABILITY_MAPPING_VERSION`` onto the ranker's
own ``RANKING_MAPPING_VERSION``, because applicability did NOT change and one stamp cannot honestly
speak for both mappings.
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
from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES
from featuregen.overlay.upload.taxonomy.disposition import (
    FinalDisposition,
    RecipeEvaluation,
    StageEvaluation,
    StageStatus,
)
from featuregen.overlay.upload.taxonomy.recognizer import RECOGNIZER_TASK
from featuregen.overlay.upload.taxonomy.versions import RANKING_MAPPING_VERSION

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

#: The ranker's family axis — read from the V2 registry, the universe it now ranks.
_FAMILY_BY_ID = {r.recipe_id: r.family for r in V2_RECIPES}
_PER_FAMILY_CAP = 3    # rank_eligible's default; the strict pass holds at most this many per family
_INITIAL_VIEW_SIZE = 15   # rank_eligible's default first-screen capacity


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


def _obligor_catalog(conn) -> None:
    """A facility-event catalog the V2 ``obligor_facility_count`` recipe binds on.

    The one recipe whose REVIEWED Formula-v1 blueprint and V2 operand contract agree role for role
    (``obligor`` grain / ``facility`` operand / ``event_ts``), which is what makes it the honest
    end-to-end proof that the engine path can now be captured at all. The three formula-bearing
    columns carry HUMAN-CONFIRMED concept evidence (the authority envelope re-resolves each one),
    the obligor key carries a grain fact, and the event timestamp carries a VERIFIED temporal-role
    decision — the three authorities ``build_formula_authority_envelope`` demands.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow(
            "obligor_bank", "facility_events", "obligor_ref", "string",
            is_grain=True, entity="Obligor"), "obligor_id"),
        (CanonicalRow(
            "obligor_bank", "facility_events", "facility_ref", "string",
            entity="Facility"), "facility_id"),
        (CanonicalRow(
            "obligor_bank", "facility_events", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(
            "obligor_bank", "facility_events", "ingested_at", "timestamp",
            as_of=True, as_of_basis="ingested_at"), "as_of_date"),
        (CanonicalRow(
            "obligor_bank", "facility_events", "defaulted", "boolean"), "outcome_label"),
    ]
    build_graph(
        conn,
        "obligor_bank",
        [row for row, _concept in catalog],
        concepts={content_hash(row): concept for row, concept in catalog},
    )
    for row, concept in catalog[:3]:
        ref = normalize_ref("obligor_bank", None, row.table, row.column)
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
    event_ref = normalize_ref("obligor_bank", None, "facility_events", "event_ts")
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
        source="obligor_bank",
        logical_refs=[event_ref],
        fields=["temporal_role"],
    )
    conn.execute(
        "UPDATE graph_node SET grain_fact_event_id='grain-obligor-test' "
        "WHERE catalog_source='obligor_bank' "
        "AND object_ref='public.facility_events.obligor_ref'",
    )
    conn.execute(
        "INSERT INTO overlay_drift_watermark "
        "(catalog_source,last_completed_at,last_run_id,head_seq) "
        "VALUES ('obligor_bank',%s,'r',0) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at=%s",
        (now, now),
    )


def _posted_debit_catalog(conn) -> None:
    """A posted-transaction catalog the V2 ``posted_debit_amount`` recipe binds on — the ONE
    ``formula-v2``-declaring recipe the registry calls FORMULA_AUTHORABLE, and the recipe A2's
    derivation turns into a bindable blueprint.

    All eight required operands are here (account key, event + booking + value timestamps, the
    monetary measure, the direction and status columns the governed policies read, and the
    transaction dimension), because the recipe cannot be served unless every required operand
    grounds. The three FORMULA-bearing roles — ``account`` (grain), ``amount`` (operand and
    source relation) and ``event_ts`` (the window's clock) — carry the three authorities
    ``build_formula_authority_envelope`` demands: human-confirmed concept evidence, a grain fact
    on the key, and a VERIFIED temporal-role decision on the clock.
    """
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    catalog = [
        (CanonicalRow(
            "posting_bank", "txns", "acct_id", "string",
            is_grain=True, entity="Account"), "account_id"),
        (CanonicalRow(
            "posting_bank", "txns", "txn_amt", "numeric"), "monetary_flow"),
        (CanonicalRow(
            "posting_bank", "txns", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow(
            "posting_bank", "txns", "dr_cr_ind", "string"), "debit_credit_indicator"),
        (CanonicalRow(
            "posting_bank", "txns", "posting_status", "string"), "booking_status"),
        (CanonicalRow(
            "posting_bank", "txns", "txn_ref", "string", entity="Transaction"), "transaction_id"),
        (CanonicalRow(
            "posting_bank", "txns", "booking_ts", "timestamp"), "booking_date"),
        (CanonicalRow(
            "posting_bank", "txns", "value_ts", "timestamp"), "value_date"),
        (CanonicalRow(
            "posting_bank", "txns", "ingested_at", "timestamp",
            as_of=True, as_of_basis="ingested_at"), "as_of_date"),
        (CanonicalRow(
            "posting_bank", "txns", "attrited", "boolean"), "outcome_label"),
    ]
    build_graph(
        conn,
        "posting_bank",
        [row for row, _concept in catalog],
        concepts={content_hash(row): concept for row, concept in catalog},
    )
    for row, concept in catalog[:8]:
        ref = normalize_ref("posting_bank", None, row.table, row.column)
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
    event_ref = normalize_ref("posting_bank", None, "txns", "event_ts")
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
        source="posting_bank",
        logical_refs=[event_ref],
        fields=["temporal_role"],
    )
    conn.execute(
        "UPDATE graph_node SET grain_fact_event_id='grain-account-test' "
        "WHERE catalog_source='posting_bank' AND object_ref='public.txns.acct_id'",
    )
    conn.execute(
        "INSERT INTO overlay_drift_watermark "
        "(catalog_source,last_completed_at,last_run_id,head_seq) "
        "VALUES ('posting_bank',%s,'r',0) "
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

    assert "ranking" in body and body["ranking_version"] == RANKING_MAPPING_VERSION
    ranking = body["ranking"]
    assert ranking, "a churn-scoped run must rank at least one eligible recipe"
    ranked_ids = {r["recipe_id"] for r in ranking}
    assert CHURN_RECIPE in ranked_ids

    # EVERY eligible recipe is ranked — an EQUALITY, not the subset this asserted while the ranker
    # was still keyed on the legacy Template registry. The signal profiles are now derived over the
    # V2 registry, which is the same universe the dispositions come from, so an eligible recipe
    # always has a bundle to be ordered on and can never be silently dropped from the ranking.
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

    # Every ranked recipe carries a family the ranker's diversity pass can group on — the axis is
    # read from the V2 registry now, so an eligible recipe always has one.
    selected = [r for r in ranking if r["selected_for_initial_view"]]
    assert all(r["recipe_id"] in _FAMILY_BY_ID for r in ranking)
    # This churn-scoped catalog grounds FEWER recipes than the initial-view size, so pass 3's
    # INCREMENTAL family-cap relaxation runs and every ranked recipe fits the view — one family may
    # therefore exceed `per_family_cap`, which is the ranker's documented relaxation rather than a
    # cap violation (the cap and its round-robin relaxation are pinned unit-side in
    # `taxonomy/test_ranking.py`). Each selected recipe carries its OWN initial-view reason stream,
    # separate from rank_reasons.
    assert len(ranking) < _INITIAL_VIEW_SIZE
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


def _arm_shadow(conn, monkeypatch) -> None:
    """Formula-shadow enrolment, armed BEFORE the catalog is seeded — the capture requires the
    REPEATABLE READ connection, and psycopg refuses to change isolation once a statement has run."""
    import psycopg

    conn.isolation_level = psycopg.IsolationLevel.REPEATABLE_READ
    monkeypatch.setenv(SCOPE_FLAG, "1")
    monkeypatch.setenv(RANK_FLAG, "1")
    monkeypatch.setenv(FORMULA_SHADOW_FLAG, "1")
    monkeypatch.setenv("FEATUREGEN_SCOPE_EXECUTION_MODE", "confirmation_required")


def _shadow_run(make_client, conn, *, use_case, hypothesis, objective,
                catalog_source, target_ref) -> dict:
    """One confirmed-scope, formula-shadow-enrolled generation run over ``catalog_source``."""
    recognition = make_client(FakeLLM(script={
        RECOGNIZER_TASK: FakeResponse(output={
            "status": "classified",
            "candidates": [{
                "use_case_id": use_case,
                "relationship": "primary",
                "confidence": "high",
                "evidence_spans": [objective],
                "rationale": f"the hypothesis concerns {use_case}",
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
            "catalog_source": catalog_source,
            "target_ref": target_ref,
            "intent_id": recognition["intent_id"],
            "recognition_id": recognition["recognition_id"],
            "confirmed_scope": {
                "primary": use_case,
                "confirmation_source": "user_confirmed",
            },
        },
        headers=AUTH,
    )
    assert response.status_code == 200, response.text
    return response.json()


def test_formula_shadow_captures_a_work_item_on_the_engine_path(
    make_client, conn, monkeypatch
):
    """Delivery-B formula shadow captures REAL work off the engine path — the E4 follow-up fix.

    The capture resolves a run's PRIVATE grounding context through
    ``recipe_candidate_keys_by_recipe_id``. Until 2026-08-14 that map was filled only by the legacy
    ``_template_candidates`` pass, which the E4 cutover stopped running on the confirmed-scope path:
    the engine served the candidates and filled nothing, so EVERY capture resolved
    ``CANDIDATE_MISSING`` and wrote zero work items — the whole subsystem was inert on the only path
    there is. The engine now rebuilds both maps from the candidates it actually served, and this test
    is the proof that the rebuilt context is not merely present but USABLE: it survives the
    blueprint preflight, the authority envelope re-resolves every role against raw concept evidence,
    the grain fact and the verified event-time decision, and an immutable work item plus its
    transactional outbox pointer are written.

    ``obligor_facility_count`` is the subject because it is the one recipe whose reviewed Formula-v1
    blueprint and V2 operand contract agree role for role (see the sibling test below for the one
    that does not, and why that is a different defect).
    """
    _arm_shadow(conn, monkeypatch)
    _obligor_catalog(conn)
    body = _shadow_run(
        make_client, conn,
        use_case="credit.monitoring.obligor",
        hypothesis="obligors with more active facilities are harder to monitor",
        objective="monitor obligor complexity",
        catalog_source="obligor_bank",
        target_ref="public.facility_events.defaulted")

    selected = {item["recipe_id"] for item in body["ranking"]
                if item["selected_for_initial_view"]}
    assert "obligor_facility_count" in selected, body["ranking"]
    work = conn.execute(
        "SELECT recipe_id,recipe_candidate_key,metadata_snapshot_id,binding_envelope_json,"
        "provider_input_json,request_read_scope_hash FROM recipe_formula_shadow_work_item "
        "WHERE generation_run_id=%s",
        (body["generation_run_id"],),
    ).fetchall()
    assert [row[0] for row in work] == ["obligor_facility_count"], work
    recipe_id, candidate_key, snapshot_id, envelope, provider_input, read_scope = work[0]
    assert candidate_key and snapshot_id and read_scope
    # The envelope names the exact bound refs, re-resolved — not the recipe's authored roles.
    assert {b["role"] for b in envelope["bindings"]} == {"obligor", "facility", "event_ts"}
    assert envelope["grain_facts"][0]["fact_event_id"] == "grain-obligor-test"
    assert envelope["event_time_facts"][0]["temporal_role"] == "event"
    assert provider_input
    # A captured work item is NOT an observation — the observation is the worker's to write when it
    # finishes. Nothing about this recipe was recorded as an incomplete capture.
    incomplete = conn.execute(
        "SELECT technical_axis FROM recipe_formula_shadow_observation "
        "WHERE generation_run_id=%s AND recipe_id=%s",
        (body["generation_run_id"], recipe_id),
    ).fetchall()
    assert incomplete == []
    # …and the transactional outbox pointer the worker reads was written with it.
    assert conn.execute(
        "SELECT count(*) FROM outbox WHERE topic='recipe_formula_shadow.requested.v1'"
    ).fetchone()[0] == 1


def test_the_v2_exemplar_recipe_reaches_a_work_item(make_client, conn, monkeypatch):
    """A4: the FIRST time the v2 path produces durable authoring input.

    Before this task the capture population was ``frozenset(RECIPE_FORMULA_EXPECTATIONS)`` — the
    two reviewed v1 entries — so ``posted_debit_amount``, the one ``formula-v2`` recipe the
    registry calls FORMULA_AUTHORABLE, was never captured at all. The population is now every
    recipe with a BINDABLE blueprint, and this recipe's blueprint is DERIVED from its own
    definition (A2) and bound by the v2 binder (A1).

    The work item is real: an EXACT candidate, a v2-bound expectation, an authority envelope
    re-resolved against raw evidence, a provider payload that crossed the fail-close whitelist's
    new v2 arm (increment 1), and one transactional outbox pointer. What it is NOT is authored —
    increment 2's worker gate stops it, honestly, because no v2 orchestrator exists yet.
    """
    _arm_shadow(conn, monkeypatch)
    _posted_debit_catalog(conn)
    body = _shadow_run(
        make_client, conn,
        use_case="payments.behaviour",
        hypothesis="accounts posting more debit value are more likely to attrite",
        objective="understand payment behaviour",
        catalog_source="posting_bank",
        target_ref="public.txns.attrited")

    selected = {item["recipe_id"] for item in body["ranking"]
                if item["selected_for_initial_view"]}
    assert "posted_debit_amount" in selected, body["ranking"]
    work = conn.execute(
        "SELECT recipe_candidate_key,provider_input_json,recipe_expectation_json "
        "FROM recipe_formula_shadow_work_item "
        "WHERE generation_run_id=%s AND recipe_id='posted_debit_amount'",
        (body["generation_run_id"],),
    ).fetchall()
    assert len(work) == 1, work
    candidate_key, provider_input, expectation = work[0]
    assert candidate_key
    # The payload DECLARES its generation — which is exactly what the egress gate dispatched on,
    # and what the worker gate will read.
    assert provider_input["formula_expectation"]["formula_schema_version"] == "formula-v2"
    # The v2 expectation is bound to the exact columns, not to authored roles.
    assert expectation["grain_entity"] == "account"
    assert expectation["grain_key_refs"] == ["posting_bank::public.txns.acct_id"]
    assert expectation["expressions"][0]["operand_ref"] == "posting_bank::public.txns.txn_amt"
    assert expectation["expressions"][0]["event_time_ref"] == "posting_bank::public.txns.event_ts"
    assert expectation["expressions"][0]["aggregation"] == "sum"
    # Nothing was recorded as an incomplete capture for it, and the outbox pointer rode along.
    assert conn.execute(
        "SELECT count(*) FROM recipe_formula_shadow_observation "
        "WHERE generation_run_id=%s AND recipe_id='posted_debit_amount'",
        (body["generation_run_id"],)).fetchone()[0] == 0
    assert conn.execute(
        "SELECT count(*) FROM outbox WHERE topic='recipe_formula_shadow.requested.v1'"
    ).fetchone()[0] >= 1


def test_formula_shadow_reaches_the_reviewed_blueprint_and_names_its_disagreement(
    make_client, conn, monkeypatch
):
    """The SECOND authorable recipe gets as far as its reviewed blueprint and is refused BY it —
    a different, still-open defect, recorded rather than hidden.

    Before the E4 follow-up this run recorded ``CANDIDATE_MISSING``: the engine filled no
    candidate-key map, so the capture never reached the blueprint at all. It now resolves an EXACT
    candidate, finds the private context, and fails one step later — because the reviewed Formula-v1
    blueprint for ``merchant_mcc_diversity`` was authored against the LEGACY template, whose grain
    role was ``merchant``, while the V2 recipe computes per CUSTOMER. ``bind_formula_expectation``
    refuses a source-entity role that is not one of the blueprint's grain key roles, which is
    exactly right: silently authoring a merchant-grain formula for a customer-grain recipe is the
    class of error the preflight exists to stop.

    Re-keying a REVIEWED expectation to a different grain entity is a governance act, not a
    follow-up fix, so it is named here and left open. The sibling test above proves the capture
    path itself works end to end.
    """
    _arm_shadow(conn, monkeypatch)
    _merchant_catalog(conn)
    body = _shadow_run(
        make_client, conn,
        use_case="fraud.merchant_fraud",
        hypothesis="merchant category breadth may indicate merchant fraud",
        objective="identify merchant fraud",
        catalog_source="merchant_bank",
        target_ref="public.tx.fraud_flag")

    selected = {item["recipe_id"] for item in body["ranking"]
                if item["selected_for_initial_view"]}
    assert "merchant_mcc_diversity" in selected, body["ranking"]
    observations = conn.execute(
        "SELECT recipe_id,capture_axis,authority_axis,technical_axis "
        "FROM recipe_formula_shadow_observation WHERE generation_run_id=%s",
        (body["generation_run_id"],),
    ).fetchall()
    # The expected-run declaration (the flag-on durability interlock) happened, which is what makes
    # any miss detectable at all.
    assert conn.execute(
        "SELECT count(*) FROM recipe_formula_shadow_expected_run "
        "WHERE generation_run_id=%s", (body["generation_run_id"],)).fetchone()[0] == 1
    # The candidate RESOLVED (the map is filled) and the refusal names the blueprint's own rule —
    # never CANDIDATE_MISSING, which would mean the engine handed the shadow nothing.
    assert ("merchant_mcc_diversity", "CAPTURE_INPUT_INCOMPLETE", "NOT_EVALUATED",
            "FORMULA_SOURCE_ENTITY_ROLE_UNRESOLVED") in observations
    assert all(row[3] != "CANDIDATE_MISSING" for row in observations), observations
    manifest = conn.execute(
        "SELECT capture_entries FROM recipe_formula_shadow_run_manifest "
        "WHERE generation_run_id=%s", (body["generation_run_id"],)).fetchone()[0]
    entry = next(e for e in manifest if e["recipe_id"] == "merchant_mcc_diversity")
    assert entry["candidate_resolution"] == "EXACT" and entry["recipe_candidate_key"]
    # Nothing was enqueued: a refused preflight is an observation, never work a provider would run.
    assert conn.execute(
        "SELECT count(*) FROM recipe_formula_shadow_work_item WHERE generation_run_id=%s",
        (body["generation_run_id"],)).fetchone()[0] == 0


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
