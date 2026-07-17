"""Phase-3B.4 D7 — the curated GOLD SET: a versioned, content-hashed corpus of adversarial contract
shapes with IMMUTABLE expert-authored expectations. The durable population proves representativeness
and replay stability, but only expert labels can prove "zero classifier defects / zero false
resolves" — that is this corpus's job, and its ``GOLD_SET_HASH`` is signed into the 3C gate artifact
so the exact cases a gate passed over are pinned.

Each case seeds a controlled single-catalog fixture, plans one deterministic roll-up, and runs the
REAL ``compile_contract`` (the production declaration pipeline). ``run_gold_case`` returns the
``(case_id, expected, actual)`` triple ``contract_eval.evaluate`` consumes. The corpus deliberately
includes the ``take_latest``-without-ordering shape (F14) and other rejections whose ``resolved``
would be a defect, so the strict false-resolve check has teeth.
"""
from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner import contracts as c
from featuregen.overlay.upload.planner.cause import ResolutionCause, contextual_cause
from featuregen.overlay.upload.planner.contract_eval import ActualVerdict, ExpectedVerdict
from featuregen.overlay.upload.planner.contracts import AggregationFunction, ReasonCode
from featuregen.overlay.upload.planner.declarations import build_compiler_context, compile_contract
from featuregen.overlay.upload.planner.plan import _envelope
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.templates import Need, Template

GOLD_SET_VERSION = "1.0.0"
_GOLD_NOW = datetime(2026, 7, 16, 12, 0, tzinfo=UTC)
_TEMPLATE = Template(
    id="gold_probe", family="balance_stock", intent="gold-set probe",
    needs=(Need("m", "monetary_flow"), Need("entity", "customer_id")), params={},
    aggregation="probe", additivity="additive", explain="H", use_cases=(), pit="single point-in-time")


@dataclass(frozen=True, slots=True)
class GoldCase:
    """One adversarial shape. ``measures`` are ``(need_role, object_ref, concept)`` triples; ``agg``
    is the declared-aggregation registry injected for this case (keyed ``(case_id, need_role)``)."""

    case_id: str
    description: str
    measures: tuple[tuple[str, str, str], ...]
    agg: dict[tuple[str, str], AggregationFunction]
    expert_cause: ResolutionCause
    expected_declaration_status: str
    expected_primary_reason_code: str | None
    resolved_is_valid: bool


def _seed(conn) -> None:
    """A single-catalog accounts→customers roll-up (accounts N:1 customers) covering every additivity
    class, plus fresh watermarks so a clean contract's freshness axis resolves."""
    rows = [
        (CanonicalRow("core", "accounts", "account_id", "integer", is_grain=True), "account_id"),
        (CanonicalRow("core", "accounts", "customer_id", "integer",
                      joins_to="customers.customer_id", cardinality="N:1"), "customer_id"),
        (CanonicalRow("core", "accounts", "amount", "numeric"), "monetary_flow"),
        (CanonicalRow("core", "accounts", "balance", "numeric"), "monetary_stock"),
        (CanonicalRow("core", "accounts", "rate", "numeric"), "monetary_rate"),
        (CanonicalRow("core", "customers", "customer_id", "integer", is_grain=True), "customer_id"),
    ]
    build_graph(conn, "core", [r for r, _ in rows], concepts={content_hash(r): cn for r, cn in rows})
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq)"
        " VALUES ('core', %s, 'gold', 1) ON CONFLICT (catalog_source) DO UPDATE SET"
        " last_completed_at = EXCLUDED.last_completed_at, head_seq = EXCLUDED.head_seq", (_GOLD_NOW,))
    conn.execute(
        "INSERT INTO projection_checkpoints (projection_name, checkpoint_seq) VALUES ('overlay', 1)"
        " ON CONFLICT (projection_name) DO UPDATE SET checkpoint_seq = EXCLUDED.checkpoint_seq")


def _binding(recipe_id: str, need_role: str, obj_ref: str, concept: str) -> c.IngredientBindingV1:
    return c.IngredientBindingV1(
        recipe_id=recipe_id, need_role=need_role, concept=concept, required_grains=(),
        join_role=str(JoinRole.MEASURE), temporal_role=str(TemporalRole.NONE),
        bound_catalog_source="core", bound_object_ref=obj_ref, actual_source_grain=None,
        binding_quality=c.BindingQuality.exact_concept, safety=c.BindingSafety.safe, reason_codes=())


def _build_plan(ctx, case: GoldCase) -> c.BindingPlanV1:
    (realization,) = ctx.realizations_by_catalog["core"]
    source_key = c.IngredientBindingV1(
        recipe_id=case.case_id, need_role="source_key", concept="customer_id", required_grains=(),
        join_role=str(JoinRole.SOURCE_ENTITY_KEY), temporal_role=str(TemporalRole.NONE),
        bound_catalog_source="core", bound_object_ref="public.accounts.customer_id",
        actual_source_grain=None, binding_quality=c.BindingQuality.exact_concept,
        safety=c.BindingSafety.safe, reason_codes=())
    measures = tuple(_binding(case.case_id, role, ref, concept) for role, ref, concept in case.measures)
    segments = (
        c.BindingPathSegmentV1(c.SegmentKind.semantic_rollup, "core",
                               from_entity="account", to_entity="customer", cardinality="many_to_one"),
        c.BindingPathSegmentV1(c.SegmentKind.intra_catalog_realization, "core",
                               realization_ref=realization.realization_id))
    return c.make_binding_plan(
        recipe_id=case.case_id, target_entity="customer", catalog_source="core",
        ingredient_bindings=(source_key, *measures), path_segments=segments,
        resolution_status=c.PlanResolutionStatus.resolved,
        path_resolution_status=c.PathResolutionStatus.source_to_target_resolved,
        primary_reason_code=None, reason_codes=(), safety=c.BindingSafety.safe,
        preference_rank=0, preference_reasons=(), candidate_role=c.CandidateRole.unranked)


GOLD_CASES: tuple[GoldCase, ...] = (
    GoldCase(
        case_id="additive_clean_resolve",
        description="additive flow, fan-in, undeclared → versioned SUM → a VALID clean resolve",
        measures=(("amount", "public.accounts.amount", "monetary_flow"),), agg={},
        expert_cause=ResolutionCause.expected, expected_declaration_status="resolved",
        expected_primary_reason_code=None, resolved_is_valid=True),
    GoldCase(
        case_id="take_latest_without_ordering",
        description="declared take_latest with NO temporal ordering column (F14) → must NOT resolve",
        measures=(("balance", "public.accounts.balance", "monetary_stock"),),
        agg={("take_latest_without_ordering", "balance"): AggregationFunction.take_latest},
        expert_cause=ResolutionCause.expected,
        expected_declaration_status="unresolved_aggregation_declaration",
        expected_primary_reason_code=str(ReasonCode.aggregation_ordering_column_missing),
        resolved_is_valid=False),
    GoldCase(
        case_id="non_additive_undeclared_strategy_missing",
        description="non_additive rate, undeclared → strategy missing → must NOT resolve",
        measures=(("rate", "public.accounts.rate", "monetary_rate"),), agg={},
        expert_cause=ResolutionCause.expected,
        expected_declaration_status="unresolved_aggregation_declaration",
        expected_primary_reason_code=str(ReasonCode.aggregation_strategy_missing),
        resolved_is_valid=False),
    GoldCase(
        case_id="non_additive_declared_sum_incompatible",
        description="non_additive rate declared SUM → incompatible-with-additivity → must NOT resolve",
        measures=(("rate", "public.accounts.rate", "monetary_rate"),),
        agg={("non_additive_declared_sum_incompatible", "rate"): AggregationFunction.sum},
        expert_cause=ResolutionCause.unsupported_topology,
        expected_declaration_status="unresolved_aggregation_declaration",
        expected_primary_reason_code=str(ReasonCode.aggregation_incompatible_with_additivity),
        resolved_is_valid=False),
    GoldCase(
        case_id="weighted_average_weight_unbound",
        description="declared weighted_average with the weight role unbound → inputs missing",
        measures=(("rate", "public.accounts.rate", "monetary_rate"),),
        agg={("weighted_average_weight_unbound", "rate"): AggregationFunction.weighted_average},
        expert_cause=ResolutionCause.expected,
        expected_declaration_status="unresolved_aggregation_declaration",
        expected_primary_reason_code=str(ReasonCode.aggregation_weight_missing),
        resolved_is_valid=False),
)


def _gold_set_hash() -> str:
    ids = [gc.case_id for gc in GOLD_CASES]
    assert len(ids) == len(set(ids)), "gold case_ids must be unique (hash + eval key on case_id)"
    material = [
        [c.case_id, c.description, sorted(list(m) for m in c.measures),
         sorted(f"{k[0]}|{k[1]}={v.value}" for k, v in c.agg.items()),
         c.expert_cause.value, c.expected_declaration_status,
         c.expected_primary_reason_code, c.resolved_is_valid]
        for c in GOLD_CASES]
    payload = {"version": GOLD_SET_VERSION, "cases": sorted(material)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


GOLD_SET_HASH = _gold_set_hash()


def run_gold_case(conn, case: GoldCase, *, seed: Callable[[object], None] = _seed
                  ) -> tuple[str, ExpectedVerdict, ActualVerdict]:
    """Seed the fixture, run the REAL compile pipeline over the case's plan, and return the
    ``(case_id, expected, actual)`` triple. The Layer-B ACTUAL cause is derived by applying the case's
    expert label to the classifier's ACTUAL primary reason (``contextual_cause``) — so an untaxonomized
    or mismatched reason surfaces as a cause diff, and a clean resolve is the ``expected`` cause."""
    seed(conn)
    scope = resolve_catalog_scope(conn, roles=(), target_entity="customer", now=_GOLD_NOW)
    ctx = build_compiler_context(conn, scope, (), _GOLD_NOW)
    if case.agg:
        ctx = dataclasses.replace(ctx, agg_declarations=dict(case.agg))
    plan = _build_plan(ctx, case)
    compiled = compile_contract(conn, ctx, plan, _TEMPLATE,
                                base_envelope=_envelope(conn, scope, case.case_id, "customer"))
    primary = (str(compiled.contract_primary_reason_code)
               if compiled.contract_primary_reason_code is not None else None)
    if primary is None:
        actual_cause = ResolutionCause.expected                 # a clean resolve
    else:
        actual_cause = contextual_cause(ReasonCode(primary), case.expert_cause)
    expected = ExpectedVerdict(
        declaration_status=case.expected_declaration_status,
        contract_resolution_status=case.expected_declaration_status,   # rejections short-circuit before freshness
        primary_reason_code=case.expected_primary_reason_code,
        cause=case.expert_cause.value, resolved_is_valid=case.resolved_is_valid)
    actual = ActualVerdict(
        declaration_status=str(compiled.declaration_status),
        contract_resolution_status=str(compiled.contract_resolution_status),
        primary_reason_code=primary, cause=actual_cause.value)
    return case.case_id, expected, actual
