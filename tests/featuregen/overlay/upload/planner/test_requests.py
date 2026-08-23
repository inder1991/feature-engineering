"""SE-8 (part 1) — every origin plans through ONE entry, with the planner pipeline unchanged."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    RequiredOperandV1,
    planning_request_from_recipe,
    planning_request_from_user_definition,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.need_metadata import (
    RESOLVED_NEED_METADATA,
    ResolvedNeedMetadataV1,
)
from featuregen.overlay.upload.planner import candidates as candidates_module
from featuregen.overlay.upload.planner.candidates import discover_ingredient_candidates
from featuregen.overlay.upload.planner.contracts import PlanResolutionStatus
from featuregen.overlay.upload.planner.declarations import recipe_content_hash
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.requests import plan_planning_request, planning_probe
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.templates import ALL_TEMPLATES

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
RECIPE = v2_recipe_by_id("customer_activity_recency")


def _catalog(db, source: str) -> None:
    catalog = [
        # TRANSACTION-grain, as the recipe's allowed_source_grains demand — the projection
        # carries the constraint and the planner enforces it, so the fixture must honor it.
        (CanonicalRow(source, "events", "txn_id", "integer", is_grain=True,
                      entity="Transaction"), "transaction_id"),
        (CanonicalRow(source, "events", "customer_id", "integer",
                      entity="Customer"), "customer_id"),
        (CanonicalRow(source, "events", "account_id", "integer"), "account_id"),
        (CanonicalRow(source, "events", "event_ts", "timestamp"), "event_timestamp"),
    ]
    build_graph(db, source, [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, %s, 'r', 1) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (source, _NOW, _NOW))


def test_the_probe_is_a_faithful_projection_of_the_request():
    request = planning_request_from_recipe(RECIPE)
    probe = planning_probe(request)
    assert probe.id == RECIPE.recipe_id
    assert probe.family == "planning_request:recipe_v2"
    assert [n.role for n in probe.needs] == [op.role for op in RECIPE.operands]
    assert [n.concept for n in probe.needs] == [op.concept for op in RECIPE.operands]
    for need, operand in zip(probe.needs, RECIPE.operands):
        assert need.allowed_source_grains == operand.allowed_source_grains
        assert need.optional == (not operand.required)
    assert probe.additivity == RECIPE.output.additivity
    assert probe.intent == RECIPE.recipe_id               # an id, never prose


def test_probe_carries_the_source_anchor_for_a_multi_entity_recipe():
    from featuregen.overlay.upload.need_metadata import derive_need_metadata
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    recipe = v2_recipe_by_id("own_transfer_outflow_amount")   # account + beneficiary keys
    probe = planning_probe(planning_request_from_recipe(recipe))
    assert probe.source_entity == recipe.source_grain
    assert probe.source_entity_need_role is not None
    derive_need_metadata(probe)                                # must not raise


def test_probe_anchor_derives_for_every_v2_recipe_or_leaves_planner_fallback():
    """The whole registry sweep: 58 probes used to raise inside `derive_need_metadata`; the list
    is now EMPTY, with no id pinned as an expected failure.

    How the residue closed. Grain compatibility alone (the plain rule) settles 314 of the 317
    recipes and leaves 3 whose several entity keys are ALL compatible with the source grain; the
    two declared tie-breaks in `_source_anchor` settle exactly those — `own_transfer_outflow_amount`
    and `first_time_payee_high_value` on the payee's declared `relationship_requirement`,
    `customer_worst_days_in_collection` on the recipe's own `output_grain`. One recipe,
    `device_sharing_velocity`, deliberately keeps the planner FALLBACK: its only `entity_key`
    operand names `device_fingerprint`, a concept the governed registry links to no entity, so
    anchoring it would be a mis-anchor the planner cannot bind — with no anchor its single
    entity-linked need (`account`) is unambiguous and nothing raises."""
    from featuregen.overlay.upload.need_metadata import derive_need_metadata
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    raising = []
    for recipe in V2_RECIPES:
        probe = planning_probe(planning_request_from_recipe(recipe))
        try:
            derive_need_metadata(probe)
        except ValueError:
            raising.append(recipe.recipe_id)
    assert raising == [], f"{len(raising)} probes still raise: {raising[:5]}"


def test_the_declared_tie_breaks_pick_the_source_key_never_the_related_one():
    """WHICH key each tie-break picks — the sweep only proves nothing raises, and a rule that
    anchored the verified payee would satisfy it just as well."""
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    def anchor_role(recipe_id: str) -> str | None:
        return planning_probe(
            planning_request_from_recipe(v2_recipe_by_id(recipe_id))).source_entity_need_role

    assert anchor_role("own_transfer_outflow_amount") == "account"        # not the payee
    assert anchor_role("first_time_payee_high_value") == "customer"       # not the payee
    assert anchor_role("customer_worst_days_in_collection") == "customer" # the recipe's own grain
    # a SOLE compatible key that declares a relationship requirement is still the anchor: the
    # relationship tie-break breaks a tie, it never disqualifies the only candidate.
    assert anchor_role("household_relationship_value") == "household"


def test_incompatible_sole_entity_key_is_never_an_anchor():
    """A sole entity key whose allowed grains EXCLUDE the source grain must yield None/None
    (planner fallback), never a mis-anchor the planner cannot bind."""
    request = FeaturePlanningRequestV1(
        origin="user_definition",
        source_definition_id="user:grain_incompatible_anchor",
        source_revision="1",
        source_content_hash="incompatiblehash",
        primary_objective=RECIPE.primary_objective,
        output=RECIPE.output,
        operands=(
            # the ONLY entity key, and its declared grains do NOT name the request's source grain
            RequiredOperandV1(role="who", concept="customer_id", operand_class="entity_key",
                              allowed_source_grains=("account",)),
            RequiredOperandV1(role="when", concept="event_timestamp",
                              operand_class="event_timestamp",
                              allowed_source_grains=("transaction",)),
        ),
        source_grain="transaction", output_grain="customer",
        temporal=RECIPE.temporal,
        computation_kind="conceptual_pattern",
        conceptual_reason="probe: the sole entity key is grain-incompatible by construction")
    probe = planning_probe(request)
    assert probe.source_entity is None
    assert probe.source_entity_need_role is None


def test_a_recipe_origin_request_plans_end_to_end(db):
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    result = plan_planning_request(
        db, request=planning_request_from_recipe(RECIPE), target_entity="customer",
        scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved
    selected = next(p for p in result.candidate_plans
                    if p.physical_plan_id == result.selected_plan_id)
    bound = {b.bound_object_ref for b in selected.ingredient_bindings}
    assert "public.events.event_ts" in bound
    assert result.replay_envelope.planner_input_hash     # the same envelope machinery


def test_a_user_definition_origin_plans_through_the_same_entry(db):
    """The origin-neutrality proof: a user-defined request (no recipe anywhere) reaches the
    SAME planner, same envelope, same fail-closed rules."""
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    request = planning_request_from_user_definition(
        definition_id="user:recency_probe",
        primary_objective=RECIPE.primary_objective,
        output=RECIPE.output,
        operands=(
            RequiredOperandV1(role="who", concept="customer_id", operand_class="entity_key",
                              allowed_source_grains=("transaction",)),
            RequiredOperandV1(role="when", concept="event_timestamp",
                              operand_class="event_timestamp"),
        ),
        source_grain=RECIPE.source_grain, output_grain=RECIPE.output_grain,
        temporal=RECIPE.temporal, content_hash="userhash")
    result = plan_planning_request(
        db, request=request, target_entity="customer", scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved
    selected = next(p for p in result.candidate_plans
                    if p.physical_plan_id == result.selected_plan_id)
    assert {b.bound_object_ref for b in selected.ingredient_bindings} == {
        "public.events.customer_id", "public.events.event_ts"}


def test_no_authorized_catalog_stays_not_applicable_for_requests_too(db):
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    result = plan_planning_request(
        db, request=planning_request_from_recipe(RECIPE), target_entity="customer",
        scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.not_applicable


# ── S1A-2: the identity-neutral registry bypass ────────────────────────────────────────────────
# `discover_ingredient_candidates` resolves each need's binding metadata out of
# RESOLVED_NEED_METADATA, keyed on `template.id`. 106 ids in the legacy template corpus collide
# with V2 recipe ids, so a probe projected from a V2 recipe silently inherits the LEGACY
# template's resolved metadata (measured: 37 of 317 V2 recipes are shadowed). The discriminator
# has to be a planner ARGUMENT, never a `Template` field — `recipe_grounding_context` enumerates
# Template's fields dynamically, so a new field would move every legacy template's canonical hash.

_COLLIDING_ID = "inflow_outflow_ratio"     # one of the 106 shared ids; `direction` is a shared role

# An entry no honest derivation could ever produce, so a match proves the registry was consulted.
_POISON = ResolvedNeedMetadataV1(
    role="direction", concept="debit_credit_indicator",
    allowed_source_grains=("__sentinel_grain__",),
    join_role=JoinRole.SOURCE_ENTITY_KEY,    # absurd: a debit/credit flag is not an entity key
    temporal_role=TemporalRole.AS_OF_TIME,   # absurd: the flag carries no time at all
    grain_source="explicit_recipe", join_role_source="explicit_recipe",
    temporal_role_source="explicit_recipe")


def _direction_catalog(db, source: str) -> None:
    """The smallest catalog that yields a candidate for `direction`, the ONE need role the legacy
    `inflow_outflow_ratio` template and the V2 recipe of the same id share."""
    catalog = [
        (CanonicalRow(source, "postings", "txn_id", "integer", is_grain=True,
                      entity="Transaction"), "transaction_id"),
        (CanonicalRow(source, "postings", "dr_cr", "text"), "debit_credit_indicator"),
    ]
    build_graph(db, source, [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})


def test_probe_operand_metadata_is_never_shadowed_by_the_legacy_registry(db, monkeypatch):
    """106 V2 ids collide with legacy template ids. In request_contract mode the resolved-need
    registry must not override a probe's own declared operand metadata; in legacy mode the same
    poisoned entry MUST still be consumed (existing behavior preserved)."""
    _direction_catalog(db, "core")
    # RESOLVED_NEED_METADATA is a MappingProxyType (immutable), and `candidates` binds the name at
    # import — so the poison goes on the CONSUMER's module attribute, not via setitem.
    monkeypatch.setattr(candidates_module, "RESOLVED_NEED_METADATA",
                        dict(RESOLVED_NEED_METADATA) | {_COLLIDING_ID: (_POISON,)})

    probe = planning_probe(planning_request_from_recipe(v2_recipe_by_id(_COLLIDING_ID)))
    legacy = next(t for t in ALL_TEMPLATES if t.id == _COLLIDING_ID)
    assert probe.id == legacy.id                      # the collision itself, not a contrivance

    def direction(template, **kw):
        # required_grains / join_role / temporal_role on the candidate ARE the resolved need's
        # three fields verbatim — the narrowest surface that reflects the registry read.
        return discover_ingredient_candidates(
            db, template, "core", roles=(), **kw).candidates["direction"]

    clean = direction(probe, metadata_resolution_mode="request_contract")
    assert clean, "fixture must yield at least one debit_credit_indicator candidate"
    assert {c.required_grains for c in clean} == {("transaction",)}   # the probe's OWN declaration
    assert {c.join_role for c in clean} == {""}                      # V2 declares neither role...
    assert {c.temporal_role for c in clean} == {""}                  # ...so the probe carries none

    # the SAME probe under the default mode still eats the sentinel — the mode is what changed,
    # not the template (so this can never be passing for an unrelated reason).
    assert {c.required_grains for c in direction(probe)} == {("__sentinel_grain__",)}

    poisoned = direction(legacy)                      # legacy template, default mode: UNCHANGED
    assert poisoned
    assert {c.required_grains for c in poisoned} == {("__sentinel_grain__",)}
    assert {c.join_role for c in poisoned} == {"source_entity_key"}
    assert {c.temporal_role for c in poisoned} == {"as_of_time"}


def test_plan_bindings_rejects_an_unknown_resolution_mode():
    """The pair is CLOSED: a typo must fail loudly, never degrade silently to the shadowing
    default. Validation is the first thing `plan_bindings` does, so conn/scope are never touched
    — passing None for both is the proof, and a later move of the check fails this test."""
    with pytest.raises(ValueError) as excinfo:
        plan_bindings(None, template=planning_probe(planning_request_from_recipe(RECIPE)),
                      target_entity=None, scope=None, now=_NOW,
                      metadata_resolution_mode="typo")
    message = str(excinfo.value)
    assert "typo" in message
    assert "legacy_registry" in message and "request_contract" in message


def test_the_registry_bypass_is_identity_neutral():
    """The literals below were computed on the PRE-change checkout (commit 219d8360, clean tree).
    They pin the two canonicalizations a new `Template`/`Need` field would have moved: the legacy
    recipe content hash and the V2 canonical recipe hash. Recomputing both sides with the same
    post-change code would only prove self-agreement — these are the before-values, pasted."""
    assert ALL_TEMPLATES[0].id == "balance_trend"
    assert recipe_content_hash(ALL_TEMPLATES[0]) == "rh_4bc9a3f80f885743"
    assert canonical_recipe_v2_hash(v2_recipe_by_id("posted_debit_amount")) == (
        "37b37069833163063a90c88a77ec2615107392a281bcb3906fbdc47d2c467b34")
