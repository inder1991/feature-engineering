"""SE-8 (part 1) — every origin plans through ONE entry, with the planner pipeline unchanged."""
from __future__ import annotations

from datetime import UTC, datetime

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_planning_contracts import (
    RequiredOperandV1,
    planning_request_from_recipe,
    planning_request_from_user_definition,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import PlanResolutionStatus
from featuregen.overlay.upload.planner.requests import plan_planning_request, planning_probe
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

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
