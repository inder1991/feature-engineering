"""C2b — a sound plan without EXECUTION PROOF is a card, not a refusal.

C2a made the governed cross-catalog lens reachable from a catalog-scoped request, and measured the
honest consequence: every shipped recipe refused, so a live deployment saw governed rejections and
zero cards. That was not merely an unfinished frontier — it was a design gap. A5
(``planner/logical_resolution``) deliberately resolves a feature's MEANING without reading one
physical fact, and the capability ladder says a card needs a logical plan and nothing more; but the
serving arm gated cards on a fully RESOLVED CONTRACT, which is a physical verdict. This suite pins
the correction and, just as hard, its limits.

**THE SPLIT, and it is the whole test.**

* a PHYSICAL refusal — the meaning is sound, the platform cannot prove how to EXECUTE the crossing
  (``physical_cardinality_unavailable``, G3's live boundary) — now yields a ``CARD_AVAILABLE``
  option naming ``DIRECTIONAL_REALIZATION_MISSING``;
* a LOGICAL refusal — the roll-up cannot be EXPRESSED (``aggregation_axis_unsupported``, G2), an
  operand is unbound, the concept is ungoverned — still yields NO card, because serving one would
  offer a person a feature the platform can never compute.

The classification is an ALLOW-LIST (``governed_lens.EXECUTION_PROOF_REFUSALS``), pinned below in
both directions, because the permissive direction is the one that produces a lie.

The seeds are the two the lens suite already owns and measured — ``_two_catalogs`` reaches G3 over
a governed bridge, and a single ``solo`` catalog with a declared ``N:1`` join reaches G2 — so this
suite never has to assert which frontier a request hits: ``test_the_measured_refusal_sequence_is_
g3_before_g2`` and ``test_g2_surfaces_only_once_a_cardinality_is_available`` already do, against the
real planner, and this one builds on their answer.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.overlay.upload.contract.test_governed_lens_requests import (
    RECIPE_ID,
    _fully_annotated_request,
    _options,
    _two_catalogs,
)
from tests.featuregen.overlay.upload.planner._binding_seeds import commit_checks
from tests.featuregen.overlay.upload.planner.test_plan import _freshness, _seed

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.materialize.action_authorization import ActionV1
from featuregen.materialize.action_decision import ask
from featuregen.materialize.action_facts import ActionFactsV1
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.capability import (
    CARD_AVAILABLE,
    CONTRACT_RESOLVED,
    SERVING_RUNGS,
)
from featuregen.overlay.upload.contract.gate1 import (
    GOVERNED_CROSS_CATALOG_LENS,
    GOVERNED_OPTION_MISSES_REQUESTED_CATALOG,
    build_considered_set,
)
from featuregen.overlay.upload.contract.governed_lens import EXECUTION_PROOF_REFUSALS
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.object_ref import qualify_object_ref
from featuregen.overlay.upload.planner.binding_chain import (
    load_considered_option_plan_binding,
)
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

_NOW = datetime(2026, 7, 14, tzinfo=UTC)

#: EVERY column the fully-annotated request binds, and the concept each one MEANS. Seeded at
#: SOURCE/ATTESTED, the strength `field_policies._CONCEPT` treats as a governed reading — a card
#: still needs its operands' meanings governed, because a card IS its logical identity and A5
#: refuses to build one over a column nobody governs. This is a superset of the C2a suite's list:
#: the trimmed request it uses binds two operands, this one binds four.
_GOVERNED_CONCEPTS = (
    ("ops", "public.transactions.account_id", "account_id"),
    ("ops", "public.transactions.event_ts", "event_timestamp"),
    ("ops", "public.transactions.amount", "monetary_flow"),
    ("ops", "public.transactions.status", "booking_status"),
    ("rev", "public.accounts.account_id", "account_id"),
)


def _govern_the_concepts(db, *catalogs: str) -> None:
    for catalog_source, object_ref, concept_name in _GOVERNED_CONCEPTS:
        if catalogs and catalog_source not in catalogs:
            continue
        record_field_evidence(
            db, logical_ref=qualify_object_ref(catalog_source, object_ref),
            field_name="concept", proposed_value=concept_name,
            producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
            producer_ref="c2b-suite", source_snapshot_id="snap_c2b", input_hash="ih_c2b")


def _g2_catalog(db) -> None:
    """ONE catalog whose declared ``N:1`` join supplies the cardinality the governed bridge hop
    withholds. The additivity matrix therefore runs and G2 surfaces —
    ``aggregation_axis_unsupported`` — for an operand nobody intended to aggregate. The lens suite
    measures exactly this."""
    _seed(db, "solo", [
        (CanonicalRow("solo", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("solo", "transactions", "account_id", "integer",
                      joins_to="accounts.account_id", cardinality="N:1"), "account_id"),
        (CanonicalRow("solo", "transactions", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow("solo", "transactions", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("solo", "transactions", "status", "text"), "booking_status"),
        (CanonicalRow("solo", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    _freshness(db, "solo")
    for object_ref, concept_name in (
            ("public.transactions.account_id", "account_id"),
            ("public.transactions.event_ts", "event_timestamp"),
            ("public.transactions.amount", "monetary_flow"),
            ("public.transactions.status", "booking_status")):
        record_field_evidence(
            db, logical_ref=qualify_object_ref("solo", object_ref), field_name="concept",
            proposed_value=concept_name, producer=EvidenceProducer.SOURCE,
            strength=AssertionStrength.ATTESTED, producer_ref="c2b-suite",
            source_snapshot_id="snap_c2b_g2", input_hash="ih_c2b_g2")


def _request():
    """The registry's own primary for the pinned recipe, carrying the roles the platform's OWN
    derivation resolves — i.e. what the planner sees for the recipe AS SHIPPED. Nothing is
    trimmed: this is the request that reaches G3, which is the point."""
    return _fully_annotated_request(v2_recipe_by_id(RECIPE_ID))


def _client() -> FakeLLM:
    return FakeLLM(script={"overlay.feature.recommend_set": FakeResponse(output={
        "recommended_lens": "engine", "reasoning": "advisory"})})


def _build(db, *, catalog_source: str, hypothesis: str, generation_run_id=None):
    intent = submit_intent(hypothesis=hypothesis, actor="ds1")
    if generation_run_id is not None:
        db.execute(
            "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
            "VALUES (%s, %s, '{\"subject\": \"user:ds1\"}'::jsonb, '{}') ON CONFLICT DO NOTHING",
            (generation_run_id, intent.intent_id))
    return build_considered_set(
        db, intent, _client(), catalog_source=catalog_source, roles=(), is_live=True,
        target_entity="account", now=_NOW, generation_run_id=generation_run_id,
        governed_requests=(_request(),))


def _governed(cs):
    return [s for s in cs.alternatives if s.lens == GOVERNED_CROSS_CATALOG_LENS]


def _reasons(cs) -> set[str]:
    return {rejection.get("reason") for rejection in cs.rejections}


# ── 1. the classification, declared in both directions ────────────────────────────────────────
def test_only_execution_proof_refusals_fall_back_to_a_card():
    """The allow-list VERBATIM, and every logical refusal named as excluded.

    Written as an equality rather than as containment on purpose: adding a code here must be a
    deliberate act with a test to change, because the permissive direction of this table serves a
    card for a feature that cannot be computed at all."""
    assert EXECUTION_PROOF_REFUSALS == {
        ReasonCode.physical_cardinality_unavailable.value: R.DIRECTIONAL_REALIZATION_MISSING,
        ReasonCode.missing_realization.value: R.DIRECTIONAL_REALIZATION_MISSING,
    }
    # the LOGICAL refusals, each excluded for its own reason: the roll-up cannot be expressed; a
    # required operand found no column; the meaning is ungoverned; the operand's governed join role
    # is unruled. None of them is "the execution proof is missing".
    for logical in (ReasonCode.aggregation_axis_unsupported,
                    ReasonCode.missing_required_need,
                    ReasonCode.aggregation_strategy_missing,
                    ReasonCode.aggregation_incompatible_with_additivity,
                    ReasonCode.ingredient_not_connected_to_path,
                    ReasonCode.concept_mismatch,
                    ReasonCode.grain_incompatible):
        assert logical.value not in EXECUTION_PROOF_REFUSALS, logical
    # …and so are the SAFETY refusals, which must never be softened into a card
    for unsafe in (ReasonCode.leakage_anchor_read, ReasonCode.protected_attribute_read,
                   ReasonCode.binding_safety_rejected, ReasonCode.safety_evaluation_incomplete):
        assert unsafe.value not in EXECUTION_PROOF_REFUSALS, unsafe
    # …and the RESOURCE refusals: the planner stopped looking, so nothing is known about the meaning
    for bounded in (ReasonCode.bounded_out_max_bridges, ReasonCode.bounded_out_max_frontier_states,
                    ReasonCode.compile_budget_exhausted, ReasonCode.planner_internal_error):
        assert bounded.value not in EXECUTION_PROOF_REFUSALS, bounded
    # every value is a REGISTERED serving code — a card may only carry a code the decision service
    # can answer with
    assert set(EXECUTION_PROOF_REFUSALS.values()) <= set(R.REASON_FAMILIES)


# ── 2. the lens: a physical refusal becomes a card ────────────────────────────────────────────
def test_a_physical_refusal_becomes_a_card_that_names_its_blocker(db):
    """G3, served. The same request that produced ``physical_cardinality_unavailable`` and nothing
    else now produces ONE option at the ``CARD_AVAILABLE`` rung, carrying the registered code for
    what is missing — and it is no longer ALSO a rejection: an outcome is one thing or the other."""
    _two_catalogs(db)
    options, rejections = _options(db, [_request()], include_execution_blocked_cards=True)

    assert rejections == []
    (card,) = options
    assert card.capability_rung == CARD_AVAILABLE
    assert card.serving_blockers == (R.DIRECTIONAL_REALIZATION_MISSING,)
    assert card.idea.capability_rung == CARD_AVAILABLE
    # it is a REAL governed option, projected the one way — the provenance a cross-catalog card
    # must show is on it, not a stub's worth of it
    assert card.idea.origin == "governed_planner"
    assert card.idea.path_authority == "governed_cross_catalog"
    assert sorted(card.idea.plan_envelope.catalog_sources) == ["ops", "rev"]
    assert {source for source, _ref in card.idea.derives_pairs} == {"ops", "rev"}
    # …and the envelope tells the truth about the contract rather than withholding it
    assert card.idea.plan_envelope.contract_resolution_status != "resolved"
    assert (ReasonCode.physical_cardinality_unavailable.value
            in card.idea.plan_envelope.contract_reason_codes)


def test_the_lane_that_did_not_ask_for_cards_is_unchanged(db):
    """DEFAULT OFF, and the default is a contract. The telemetry lane measures the governed
    planner's RESOLUTION RATE; a lane that counted cards as resolutions would report a frontier
    closing that had not moved. Same request, same seed, no keyword: still a refusal."""
    _two_catalogs(db)
    options, rejections = _options(db, [_request()])

    assert options == []
    (rejection,) = rejections
    assert rejection["reason"] == ReasonCode.physical_cardinality_unavailable.value


def test_a_logical_refusal_is_never_served_as_a_card(db):
    """G2 — the roll-up cannot be EXPRESSED. An intra-catalog realization supplies the cardinality
    the bridge hop withheld, the additivity matrix runs, and it refuses
    ``aggregation_axis_unsupported`` for an operand nobody intended to aggregate. No card, WITH the
    fallback asked for: this is the permissive-direction failure the allow-list exists to stop."""
    _g2_catalog(db)
    options, rejections = _options(db, [_request()], include_execution_blocked_cards=True)

    assert options == []
    (rejection,) = rejections
    assert rejection["reason"] == ReasonCode.aggregation_axis_unsupported.value


def test_a_resolved_contract_still_reports_the_higher_rung(db):
    """The other end of the ladder, so the rung is a real distinction rather than a constant. The
    trimmed request the lens suite owns RESOLVES its contract; its option says so."""
    from tests.featuregen.overlay.upload.contract.test_governed_lens_requests import (
        _plannable_request,
    )

    _two_catalogs(db)
    options, rejections = _options(
        db, [_plannable_request(v2_recipe_by_id(RECIPE_ID))],
        include_execution_blocked_cards=True)
    assert rejections == []
    (option,) = options
    assert option.capability_rung == CONTRACT_RESOLVED
    assert option.serving_blockers == ()
    assert {CARD_AVAILABLE, CONTRACT_RESOLVED} == SERVING_RUNGS


# ── 3. serving: the card reaches a catalog-scoped request ─────────────────────────────────────
def test_the_shipped_registry_now_serves_a_card_at_the_open_frontier(db):
    """THE point of the whole ladder, end to end through ``build_considered_set``.

    The request is the registry's own primary for the pinned recipe. Before C2b this run served
    zero governed options and one ``physical_cardinality_unavailable`` rejection; it now serves a
    card that says what it is and what is missing."""
    _two_catalogs(db)
    _govern_the_concepts(db)
    cs = _build(db, catalog_source="ops", hypothesis="roll transactions up to the account")

    (governed_set,) = _governed(cs)
    (card,) = governed_set.features
    assert card.capability_rung == CARD_AVAILABLE
    assert R.DIRECTIONAL_REALIZATION_MISSING in card.serving_blockers
    # A5's own absences ride ALONGSIDE the physical blocker — one list answers "why can't I
    # generate this yet", rather than two the consumer has to know to merge. The crossing carries
    # no declared temporal semantics on this seed, and R14 never defaults one.
    assert R.TEMPORAL_JOIN_POLICY_MISSING in card.serving_blockers
    # …and the refusal it replaced is GONE from the rejections: one outcome per request
    assert ReasonCode.physical_cardinality_unavailable.value not in _reasons(cs)


def test_a_card_is_marked_and_plan_bound_like_any_other_governed_option(db):
    """A card is a card BECAUSE it has a logical plan, so migration 1135's planned-lane marker and
    its ``considered_option_plan_binding`` are not optional extras here — they are the evidence the
    rung is claiming. ``commit_checks`` runs 1135's DEFERRED totality trigger where this
    never-committing connection can observe it."""
    _two_catalogs(db)
    _govern_the_concepts(db)
    cs = _build(db, catalog_source="ops", hypothesis="a card with a plan behind it",
                generation_run_id="grun_c2b")

    (governed_set,) = _governed(cs)
    (path,) = [p for p in cs.option_ids_by_path
               if p.startswith(f"alternative:{cs.alternatives.index(governed_set)}:")]
    option_id = cs.option_ids_by_path[path]
    marker = db.execute(
        "SELECT requires_logical_plan_binding FROM semantic_option_decision "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (cs.considered_revision_id, option_id)).fetchone()
    assert marker is not None and marker[0] is True
    binding = load_considered_option_plan_binding(
        db, considered_revision_id=cs.considered_revision_id, option_id=option_id)
    assert binding is not None
    commit_checks(db)


def test_the_anchor_rule_holds_on_the_card_path(db):
    """A card is served for a request scoped to X only if its plan READS from X. The card path must
    not be a way around the rule: it runs through the SAME loop, and this proves it does — the
    identical ops↔rev card, asked for from a third fully authorized catalog, is refused by name."""
    _two_catalogs(db)
    _seed(db, "hr", [
        (CanonicalRow("hr", "employees", "employee_id", "integer", is_grain=True), "employee_id"),
        (CanonicalRow("hr", "employees", "hired_on", "timestamp"), "event_timestamp"),
    ])
    _freshness(db, "hr")
    _govern_the_concepts(db)

    anchored = _build(db, catalog_source="ops", hypothesis="anchored at ops")
    assert len(_governed(anchored)[0].features) == 1

    unrelated = _build(db, catalog_source="hr", hypothesis="anchored at hr")
    assert not _governed(unrelated)
    assert GOVERNED_OPTION_MISSES_REQUESTED_CATALOG in _reasons(unrelated)


def test_a_card_carries_its_rung_onto_the_wire_and_back(db):
    """The rung and its blockers are part of the REVIEWED ARTIFACT: they are serialized with the
    considered set and restored by the Gate-1 reload, so the human who comes back to the decision
    sees the same "why can't I generate this yet" the card showed. An option that computes no rung
    emits neither key, which is what keeps every engine option's bytes — and therefore its option
    id — exactly where they were."""
    from featuregen.overlay.upload.contract.gate1 import _idea_from_json, _idea_json

    _two_catalogs(db)
    _govern_the_concepts(db)
    cs = _build(db, catalog_source="ops", hypothesis="round trip the rung")

    (card,) = _governed(cs)[0].features
    body = _idea_json(card)
    assert body["capability_rung"] == CARD_AVAILABLE
    assert R.DIRECTIONAL_REALIZATION_MISSING in body["serving_blockers"]
    restored = _idea_from_json(body)
    assert restored.capability_rung == card.capability_rung
    assert restored.serving_blockers == card.serving_blockers


def test_an_option_that_computes_no_rung_emits_neither_key():
    """BYTE IDENTITY, asserted where it cannot go vacuous. Every engine and LLM idea — and every
    persisted pre-C2b snapshot — computes no rung, and `_idea_json` must emit neither key for one,
    or every such option's bytes, its ``considered_content_hash`` and its ``option_id`` would all
    move.

    Asserted over a bare ``FeatureIdea`` rather than over a run's engine options: whether a given
    seed produces any engine option is incidental, and a test whose subject can be empty proves
    nothing on the day it is empty."""
    from featuregen.overlay.upload.contract.gate1 import _idea_from_json, _idea_json
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    plain = FeatureIdea(name="n", description="d", derives_from=[], aggregation="sum",
                        grain_table="public.accounts")
    assert plain.capability_rung == "" and plain.serving_blockers == ()
    emitted = _idea_json(plain)
    assert "capability_rung" not in emitted and "serving_blockers" not in emitted
    # …and the reader restores the same honest absence from a payload carrying neither key
    restored = _idea_from_json(emitted)
    assert restored.capability_rung == "" and restored.serving_blockers == ()


# ── 4. the card is honestly limited — asked of the ONE authority, not asserted here ────────────
def test_a_served_card_cannot_pass_the_decision_service_for_any_computing_act(db):
    """VERIFIED, not assumed. The card's blockers are handed to the canonical six-action service
    exactly as a consumer would hand them over, and the service's own answers are read back.

    The shape that must hold is the owner's capability matrix, whose Formula column reads Allow
    under EVERY link condition: authoring PROCEEDS with the caller told, and every act that would
    COMPUTE over the unproven crossing refuses. A card that could be previewed, executed or
    published would be this task's central defect."""
    _two_catalogs(db)
    _govern_the_concepts(db)
    cs = _build(db, catalog_source="ops", hypothesis="ask the one authority")
    (card,) = _governed(cs)[0].features
    assert card.serving_blockers

    facts = ActionFactsV1(member_names=(card.name,),
                          member_blockers={card.name: card.serving_blockers})
    answers = {action: ask(db, facts.request(action=action,
                                             resource_identity_hash="0" * 64))
               for action in ActionV1}

    # AUTHOR_FORMULA proceeds — and says why, so nobody is surprised later
    assert answers[ActionV1.AUTHOR_FORMULA].allowed
    assert R.DIRECTIONAL_REALIZATION_MISSING in answers[ActionV1.AUTHOR_FORMULA].warnings
    # every computing act refuses, and names the missing realization while doing it
    for action in (ActionV1.GENERATE_PREVIEW, ActionV1.EXECUTE_SANDBOX, ActionV1.PUBLISH_SANDBOX,
                   ActionV1.MATERIALIZE_PRODUCTION, ActionV1.PUBLISH_PRODUCTION):
        decision = answers[action]
        assert not decision.allowed, action
        refused = set(decision.blockers) | {
            code for verdict in decision.per_member for code in verdict.blockers}
        assert R.DIRECTIONAL_REALIZATION_MISSING in refused, action


def test_a_resolved_option_is_not_gated_by_a_blocker_it_does_not_carry(db):
    """The control for the test above: the SAME service, over an option whose contract resolved and
    whose crossing IS realized, allows the preview. Without this, the refusals above would be
    consistent with a service that refuses everything."""
    from tests.featuregen.overlay.upload.contract.test_governed_lens_requests import (
        _plannable_request,
    )

    _two_catalogs(db)
    options, _rejections = _options(
        db, [_plannable_request(v2_recipe_by_id(RECIPE_ID))],
        include_execution_blocked_cards=True)
    (option,) = options
    facts = ActionFactsV1(member_names=(option.idea.name,),
                          member_blockers={option.idea.name: option.serving_blockers})
    decision = ask(db, facts.request(action=ActionV1.GENERATE_PREVIEW,
                                     resource_identity_hash="0" * 64))
    assert decision.allowed


# ── 5. the seam a card does NOT open ──────────────────────────────────────────────────────────
def test_a_card_whose_meaning_is_ungoverned_is_still_refused(db):
    """The logical identity gate is not weakened by the card path. Same seed, same physical
    refusal, but NO governed concept evidence on the bound columns: the platform cannot say what
    the option MEANS, and a card IS its meaning, so nothing is served."""
    _two_catalogs(db)                       # deliberately no _govern_the_concepts
    cs = _build(db, catalog_source="ops", hypothesis="ungoverned meaning")
    assert not _governed(cs)


def test_a_bridgeless_catalog_pair_yields_no_card(db):
    """``missing_realization`` is ON the allow-list, and it still serves nothing here — the gate
    that decides is the one that asks whether a PATH resolved, not the table. With no link between
    the catalogs there is no relationship to give a meaning to, and the request keeps its own
    informative refusal rather than acquiring a second one about logical identity."""
    _seed(db, "ops", [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "account_id"),
        (CanonicalRow("ops", "transactions", "amount", "numeric", additivity="additive",
                      currency="USD"), "monetary_flow"),
        (CanonicalRow("ops", "transactions", "event_ts", "timestamp"), "event_timestamp"),
        (CanonicalRow("ops", "transactions", "status", "text"), "booking_status"),
    ])
    _seed(db, "rev", [
        (CanonicalRow("rev", "accounts", "account_id", "integer", is_grain=True), "account_id"),
    ])
    _freshness(db, "ops", "rev")
    _govern_the_concepts(db)

    options, rejections = _options(db, [_request()], include_execution_blocked_cards=True)
    assert options == []
    (rejection,) = rejections
    assert rejection["reason"] == ReasonCode.missing_realization.value
