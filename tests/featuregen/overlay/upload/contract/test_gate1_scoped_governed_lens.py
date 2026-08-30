"""C2a — the serving wire-up: a CATALOG-SCOPED request reaches the governed cross-catalog lens.

Before this task the governed lens had exactly one door and it was nailed shut. ``build_considered_
set``'s third arm (``elif is_live:``) ran only when ``catalog_source is None``, and the route
refuses an entity-only request with 422 ``SEMANTIC_REQUIRES_CATALOG_SOURCE`` before the builder is
reached — so every piece of governed cross-catalog machinery on the platform was complete and
unreachable from a real request. This suite is about the door: a request that NAMES a catalog now
gets the governed lens ALONGSIDE the engine's answer, never instead of it.

THREE PROPERTIES, and each one is a refusal rather than a preference:

* **the ANCHOR RULE.** A request scoped to catalog X may only be served governed options whose plan
  READS from X. Being authorized for Y and Z does not make a Y↔Z feature an answer to a question
  about X. Pinned below with a third, authorized-but-unrelated catalog.
* **the LOGICAL IDENTITY GATE.** A served governed option's decision row carries migration 1135's
  ``requires_logical_plan_binding``, and 1135's deferred trigger then REQUIRES a
  ``considered_option_plan_binding`` for it at COMMIT. The marker is INSERT-only (1063 refuses
  UPDATE and the writer ends ``ON CONFLICT DO NOTHING``), so an option served without it can never
  be armed afterwards — which is why an option whose logical plan cannot be minted is not served.
* **inactivity changes nothing.** With the resolved activation verdict false the lens does not run,
  is not consulted, and adds neither an option nor a rejection.

WHAT THIS SUITE INJECTS, AND WHAT IT REFUSES TO FAKE. The recipe REGISTRY is the real one and the
planner is the real one. What the tests state explicitly is the planning REQUEST — through
``build_considered_set``'s ``governed_requests`` seam, which is the catalog-scoped twin of the
``templates`` seam the entity-scoped lane's suite already uses. That is not a convenience: as
shipped, NO V2 recipe reaches a resolved governed contract, because the operands the registry
declares stage a measure over a hop whose cardinality the platform cannot supply (G3 cross-catalog,
G2 intra-catalog — both named in ``contract/governed_lens.py``'s header and both open). A suite
that only ran the shipped requests could assert that the lens REFUSED and nothing else, and would
go on passing if the wire-up were deleted. ``_plannable_request`` — the operand trimming
``test_governed_lens_requests`` already owns — is what lets these tests state what happens when a
plan DOES resolve. Everything identity-, governance- and display-bearing about it is the registry's.

▲ UPDATED BY C2b. "No V2 recipe reaches a resolved CONTRACT" is still true and still the reason for
the injected request — but a recipe refused for a PHYSICAL reason is no longer refused as an option:
it is served as a card at the ``CARD_AVAILABLE`` rung. §5 below states both shipped-registry
outcomes measured against the real registry, and the C2b suite
(``test_governed_card_without_execution_proof``) owns the split itself.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload._bridge_fixtures import seed_verified_bridge
from tests.featuregen.overlay.upload.contract.test_governed_lens_requests import (
    RECIPE_ID,
    _plannable_request,
)
from tests.featuregen.overlay.upload.planner._binding_seeds import commit_checks
from tests.featuregen.overlay.upload.planner.test_plan import _freshness, _seed

import featuregen.overlay.upload.contract.gate1 as gate1
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_evidence import record_field_evidence
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.capability import CARD_AVAILABLE
from featuregen.overlay.upload.contract.gate1 import (
    GOVERNED_CROSS_CATALOG_LENS,
    GOVERNED_OPTION_LOGICAL_IDENTITY_UNAVAILABLE,
    GOVERNED_OPTION_MISSES_REQUESTED_CATALOG,
    build_considered_set,
)
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.object_ref import qualify_object_ref
from featuregen.overlay.upload.planner.binding_chain import (
    load_considered_option_plan_binding,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

_NOW = datetime(2026, 7, 14, tzinfo=UTC)

#: The columns the pinned recipe's trimmed request binds, and the concept each one MEANS. Seeded as
#: SOURCE/ATTESTED because that is the strength `field_policies._CONCEPT` treats as a governed
#: reading; a profiler-proposed concept is display-only and would leave the operand with no
#: governed semantic revision — which is exactly the absence the logical identity gate refuses on
#: (`test_an_option_whose_meaning_is_ungoverned_is_refused_not_served` runs without these).
_GOVERNED_CONCEPTS = (
    ("ops", "public.transactions.account_id", "account_id"),
    ("ops", "public.transactions.event_ts", "event_timestamp"),
    ("rev", "public.accounts.account_id", "account_id"),
)


def _seed_two_catalogs(db) -> None:
    """``ops`` holds the transaction grain, ``rev`` the account-grain landing table; the roll-up
    completes only over the VERIFIED bridge between them, so a resolved plan reads BOTH."""
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
    seed_verified_bridge(db, "bfk_c2a", entity="account", left_source="ops",
                         left_ref="public.transactions.account_id", right_source="rev",
                         right_ref="public.accounts.account_id")
    _freshness(db, "ops", "rev")


def _seed_unrelated_catalog(db) -> None:
    """A THIRD catalog this caller is fully authorized for and that the resolved plan never
    touches. Nothing about authorization makes an ops↔rev feature an answer to a question about it.
    """
    _seed(db, "hr", [
        (CanonicalRow("hr", "employees", "employee_id", "integer", is_grain=True), "employee_id"),
        (CanonicalRow("hr", "employees", "hired_on", "timestamp"), "event_timestamp"),
    ])
    _freshness(db, "hr")


#: The two the TRIMMED request does not bind and the SHIPPED one does. Kept separate so the suite
#: can state both deployment conditions — every bound operand's meaning governed, and not.
_SHIPPED_ONLY_CONCEPTS = (
    ("ops", "public.transactions.amount", "monetary_flow"),
    ("ops", "public.transactions.status", "booking_status"),
)


def _record_concepts(db, rows) -> None:
    for catalog_source, object_ref, concept_name in rows:
        record_field_evidence(
            db, logical_ref=qualify_object_ref(catalog_source, object_ref),
            field_name="concept", proposed_value=concept_name,
            producer=EvidenceProducer.SOURCE, strength=AssertionStrength.ATTESTED,
            producer_ref="c2a-suite", source_snapshot_id="snap_c2a", input_hash="ih_c2a")


def _govern_the_concepts(db) -> None:
    _record_concepts(db, _GOVERNED_CONCEPTS)


def _govern_the_shipped_requests_remaining_concepts(db) -> None:
    _record_concepts(db, _SHIPPED_ONLY_CONCEPTS)


def _client() -> FakeLLM:
    return FakeLLM(script={"overlay.feature.recommend_set": FakeResponse(output={
        "recommended_lens": "engine", "reasoning": "advisory"})})


def _request():
    return _plannable_request(v2_recipe_by_id(RECIPE_ID))


def _build(db, *, catalog_source: str, is_live: bool = True, generation_run_id=None,
           governed_requests=None, hypothesis="roll transactions up to the account"):
    """One catalog-scoped run. ``scope=None`` keeps the engine arm out of the way — it needs a
    confirmed scope and a frozen semantic context, and none of the properties here are about it
    (the route suite covers the engine's byte-identity). The governed arm's own gate is
    ``is_live and catalog_source and target_entity``, all three of which hold."""
    intent = submit_intent(hypothesis=hypothesis, actor="ds1")
    if generation_run_id is not None:
        db.execute(
            "INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
            "VALUES (%s, %s, '{\"subject\": \"user:ds1\"}'::jsonb, '{}') ON CONFLICT DO NOTHING",
            (generation_run_id, intent.intent_id))
    return build_considered_set(
        db, intent, _client(), catalog_source=catalog_source, roles=(),
        is_live=is_live, target_entity="account", now=_NOW,
        generation_run_id=generation_run_id,
        governed_requests=(_request(),) if governed_requests is None else governed_requests)


def _governed(cs):
    return [feature_set for feature_set in cs.alternatives
            if feature_set.lens == GOVERNED_CROSS_CATALOG_LENS]


def _reasons(cs) -> set[str]:
    return {rejection.get("reason") for rejection in cs.rejections}


# ── 1. the door itself ────────────────────────────────────────────────────────────────────────
def test_a_catalog_scoped_request_now_reaches_the_governed_cross_catalog_lens(db):
    """THE bug this task exists to close: a request that NAMES a catalog is served governed
    cross-catalog options, in their own lens, alongside whatever else the run produced."""
    _seed_two_catalogs(db)
    _govern_the_concepts(db)
    cs = _build(db, catalog_source="ops")

    (governed_set,) = _governed(cs)
    (option,) = governed_set.features
    assert option.origin == "governed_planner"
    assert option.path_authority == "governed_cross_catalog"
    assert option.planner_applicability == "applicable_cross_catalog"
    # the provenance a card needs rides on the option: which catalogs participate, and the exact
    # compiled plan it was projected from — never re-derived downstream.
    assert option.plan_envelope is not None
    assert sorted(option.plan_envelope.catalog_sources) == ["ops", "rev"]
    assert {source for source, _ref in option.derives_pairs} == {"ops", "rev"}


def test_the_governed_set_is_additive_and_never_displaces_the_run_it_joins(db):
    """ADDITIVE, structurally: the governed lens is appended, so every other feature set — and
    therefore every other option's POSITION, which is what its option id is minted over — is
    exactly where it was on the same run with the lens inactive."""
    _seed_two_catalogs(db)
    _govern_the_concepts(db)
    inactive = _build(db, catalog_source="ops", is_live=False, hypothesis="a stable hypothesis")
    active = _build(db, catalog_source="ops", is_live=True, hypothesis="a stable hypothesis")

    assert not _governed(inactive)
    assert len(_governed(active)) == 1
    other_sets = [s for s in active.alternatives if s.lens != GOVERNED_CROSS_CATALOG_LENS]
    assert [s.lens for s in other_sets] == [s.lens for s in inactive.alternatives]
    assert [[f.name for f in s.features] for s in other_sets] == [
        [f.name for f in s.features] for s in inactive.alternatives]
    # the governed set is LAST, so it can only ever add positions, never renumber existing ones
    assert active.alternatives[-1].lens == GOVERNED_CROSS_CATALOG_LENS


# ── 2. inactive → the lens does not run at all ────────────────────────────────────────────────
def test_an_inactive_verdict_never_consults_the_governed_lens(db, monkeypatch):
    """Not "produces nothing" — is never CALLED. A lens that ran and filtered would still pay the
    planner's cost and could still move a log line, a counter or a row on a flag-off deployment."""
    _seed_two_catalogs(db)
    _govern_the_concepts(db)

    def _boom(*args, **kwargs):
        raise AssertionError("the governed lens must not run when the verdict is inactive")

    monkeypatch.setattr(gate1, "_scoped_governed_cross_catalog_lens", _boom)
    cs = _build(db, catalog_source="ops", is_live=False)
    assert not _governed(cs)
    assert not any(rejection.get("lens") == "governed" for rejection in cs.rejections)


# ── 3. THE ANCHOR RULE ────────────────────────────────────────────────────────────────────────
def test_the_anchor_rule_refuses_a_plan_that_never_reads_the_requested_catalog(db):
    """A request scoped to ``hr`` — a catalog this caller is fully authorized for — must NOT be
    served the ops↔rev feature, even though the planner resolves it and the caller could read
    every column in it. Authorization is not relevance."""
    _seed_two_catalogs(db)
    _seed_unrelated_catalog(db)
    _govern_the_concepts(db)

    anchored = _build(db, catalog_source="ops", hypothesis="anchored at ops")
    assert len(_governed(anchored)[0].features) == 1        # the same plan, from the same seed …

    unrelated = _build(db, catalog_source="hr", hypothesis="anchored at hr")
    assert not _governed(unrelated)                          # … is not served here
    assert GOVERNED_OPTION_MISSES_REQUESTED_CATALOG in _reasons(unrelated)
    refusal = next(r for r in unrelated.rejections
                   if r.get("reason") == GOVERNED_OPTION_MISSES_REQUESTED_CATALOG)
    assert refusal["recipe_id"] == RECIPE_ID
    # a refusal in the considered set carries THREE keys and no more: this list is served to every
    # caller and hashed into the immutable revision, so the planner's evidence (bridge fact keys,
    # physical object refs) must never be widened into it.
    assert set(refusal) == {"lens", "reason", "recipe_id"}


# ── 4. the logical identity gate ──────────────────────────────────────────────────────────────
def test_an_option_whose_meaning_is_ungoverned_is_refused_not_served(db):
    """Same seed, same resolving plan, but NO governed concept evidence on the bound columns — so
    the platform cannot say what the option MEANS. It is refused rather than served, because a
    served option must carry 1135's marker and a marker can never be added afterwards."""
    _seed_two_catalogs(db)                                   # deliberately no _govern_the_concepts
    cs = _build(db, catalog_source="ops")

    assert not _governed(cs)
    assert GOVERNED_OPTION_LOGICAL_IDENTITY_UNAVAILABLE in _reasons(cs)


def test_a_served_governed_option_is_written_with_the_planned_marker_and_its_plan_binding(db):
    """THE arming pin, both halves and the law that ties them.

    ``requires_logical_plan_binding`` is set in the option's own INSERT — the only moment it can
    be — and the ``considered_option_plan_binding`` beneath it lands in the same transaction.
    ``commit_checks`` then runs migration 1135's DEFERRED totality trigger, which is what a real
    COMMIT would do: this suite's connection never commits, so without that call the trigger the
    marker arms would never fire and the test would prove nothing about production."""
    _seed_two_catalogs(db)
    _govern_the_concepts(db)
    cs = _build(db, catalog_source="ops", generation_run_id="grun_c2a")

    (governed_set,) = _governed(cs)
    (path,) = [p for p, _o in cs.option_ids_by_path.items()
               if p.startswith(f"alternative:{cs.alternatives.index(governed_set)}:")]
    option_id = cs.option_ids_by_path[path]
    marker = db.execute(
        "SELECT requires_logical_plan_binding, source_definition_id FROM semantic_option_decision "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (cs.considered_revision_id, option_id)).fetchone()
    assert marker is not None, "a served governed option must leave a decision row"
    assert marker[0] is True
    # the CANONICAL definition id, never a variant id: the column feeds `v2_recipe_by_id` at every
    # durable write downstream.
    assert marker[1] == RECIPE_ID

    binding = load_considered_option_plan_binding(
        db, considered_revision_id=cs.considered_revision_id, option_id=option_id)
    assert binding is not None
    assert binding.logical_plan_revision_id.endswith(binding.logical_digest)
    commit_checks(db)   # 1135's deferred totality trigger, run where a test can observe it


def test_an_engine_lane_option_is_written_pre_plan(db):
    """The marker's other side, and the reason governed facts are keyed by POSITION. An option
    that is NOT a governed cross-catalog plan is honestly PRE-PLAN: its row must carry a false
    marker, or 1135's trigger would demand a plan binding for an option that has no plan and abort
    the whole request at COMMIT."""
    from featuregen.overlay.upload.semantic_option_decision import persist_option_decisions

    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
               "VALUES ('int_c2a', 'h', 'hypothesis')")
    db.execute("INSERT INTO feature_generation_run (generation_run_id, intent_id, actor, flags) "
               "VALUES ('grun_pre', 'int_c2a', '{\"subject\": \"u\"}'::jsonb, '{}')")
    db.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "  generation_run_id, considered_json, considered_content_hash, canonicalization_version) "
        "VALUES ('crv_pre', 'int_c2a', 'grun_pre', '{}'::jsonb, 'cch', 'v1')")
    facts = {"source_definition_id": "sd", "generation_source": "recipe",
             "computation_kind": "window_aggregate", "planning_request_hash": "prh",
             "parameter_values": [], "binding_state": "bound", "confirmation_required_roles": [],
             "readiness": "ready", "review_current": True, "recipe_revision_hash": "rrh",
             "validation_status": "DESIGN_CHECKED", "outstanding_requirement_codes": [],
             "has_reviewed_formula_expectation": False, "formula_expectation_revision": "",
             "plan_envelope_present": False, "dataset_story": {}, "policy_revision_pins": {},
             "context_hash": "ctx"}
    persist_option_decisions(db, considered_revision_id="crv_pre", generation_run_id="grun_pre",
                             metadata_snapshot_id=None, facts_by_option_id={"opt_pre": facts})
    stored = db.execute(
        "SELECT requires_logical_plan_binding FROM semantic_option_decision "
        "WHERE considered_revision_id = 'crv_pre' AND option_id = 'opt_pre'").fetchone()
    assert stored[0] is False
    commit_checks(db)   # nothing to check: an unmarked row never queues the totality event


# ── 5. the shipped registry, stated honestly ──────────────────────────────────────────────────
def _shipped_registry_run(db, hypothesis: str):
    """The registry's OWN primary for the pinned recipe — no injected request, no trimming."""
    return build_considered_set(
        db, submit_intent(hypothesis=hypothesis, actor="ds1"), _client(),
        catalog_source="ops", roles=(), is_live=True, target_entity="account", now=_NOW,
        v2_eligible_ids=frozenset({RECIPE_ID}))


def test_the_shipped_registry_reaches_the_lens_and_is_served_as_a_card(db):
    """The wire-up is REAL for the registry as shipped — and since C2b what it produces is a CARD.

    No V2 recipe reaches a resolved governed CONTRACT yet: the cross-catalog frontier (G3) is open,
    so the governed bridge hop carries no executable directional realization and the contract
    refuses ``physical_cardinality_unavailable``. That refusal is about EXECUTION PROOF, not about
    meaning — the roll-up is expressible, the operands are bound, the path resolves — so the option
    is now served at the ``CARD_AVAILABLE`` rung carrying ``DIRECTIONAL_REALIZATION_MISSING``, and
    the day G3 closes this test is where the promotion to ``CONTRACT_RESOLVED`` announces itself.

    The full concept set is governed here rather than the three the rest of this suite needs: the
    shipped request binds every operand the recipe declares, and A5 refuses a logical identity over
    a column no governed semantic revision covers. That is a real deployment condition, and the
    test below it pins the other side of it."""
    _seed_two_catalogs(db)
    _govern_the_concepts(db)
    _govern_the_shipped_requests_remaining_concepts(db)
    cs = _shipped_registry_run(db, "the shipped registry, unmodified")

    (governed_set,) = _governed(cs)
    (card,) = governed_set.features
    assert card.source_definition_id == RECIPE_ID
    assert card.capability_rung == CARD_AVAILABLE
    assert R.DIRECTIONAL_REALIZATION_MISSING in card.serving_blockers
    # the refusal the card replaced is gone: one outcome per request, never both
    assert "physical_cardinality_unavailable" not in _reasons(cs)


def test_the_shipped_registry_is_refused_when_an_operands_meaning_is_ungoverned(db):
    """The other side of the condition above, and the one an operator is most likely to meet: the
    SAME shipped request over the SAME catalogs, with concept evidence on only some of the columns
    it binds. A card IS its logical identity, so the honest answer is a refusal that names exactly
    that — never a card built over an operand whose meaning nobody governs."""
    _seed_two_catalogs(db)
    _govern_the_concepts(db)        # deliberately NOT the amount/status concepts
    cs = _shipped_registry_run(db, "shipped registry, partly governed")

    assert not _governed(cs)
    governed_rejections = [r for r in cs.rejections if r.get("lens") == "governed"]
    assert [r["recipe_id"] for r in governed_rejections] == [RECIPE_ID]
    assert governed_rejections[0]["reason"] == GOVERNED_OPTION_LOGICAL_IDENTITY_UNAVAILABLE


@pytest.mark.parametrize("catalog_source", [None])
def test_the_entity_scoped_lane_is_untouched(db, catalog_source):
    """The pre-existing ``elif is_live:`` arm still answers for an entity-only run: the C2a arm is
    gated on ``catalog_source is not None`` and adds nothing here."""
    _seed_two_catalogs(db)
    _govern_the_concepts(db)
    intent = submit_intent(hypothesis="an entity-scoped hypothesis", actor="ds1")
    cs = build_considered_set(
        db, intent, _client(), catalog_source=catalog_source, is_live=True,
        target_entity="account", templates=(), now=_NOW)
    assert not _governed(cs)
    assert cs.governed_decision_facts_by_path == {}
