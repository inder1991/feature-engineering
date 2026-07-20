"""Delivery H3c — confirm-time governed-plan rebuild + revalidation (STABLE ids + freshness).

A GOVERNED (planner-authored) contract carries a ``PlanEnvelopeV1`` pinning the exact governed
physical plan the option was compiled to (``physical_plan_id`` = the physical-path identity,
``contract_id`` = the declaration identity / ``planner_declaration_id``). At confirm this module
REBUILDS that plan against the CURRENT catalog state and requires BOTH ids to reproduce EXACTLY and
the plan's freshness to be ``current`` — anything else means the plan drifted between generation and
confirm and must be regenerated, never silently finalized (``GovernedPlanDrift`` → the route 409s).

RISK-4 (ids are never touched): the rebuild MINTS ids through the SAME canonical constructors
(``make_binding_plan`` / ``make_contract_id``) and only COMPARES them — no id material changes here.
An unchanged snapshot therefore reproduces the SAME ids (asserted); this reproduction IS the check.
The rebuilt plan's ``build_physical_read_set`` is returned so the confirm can persist the FULL read
set (join keys / anchors / bridge keys) as role-labelled lineage.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime

from featuregen.overlay.upload.planner.contracts import (
    BindingPlanV1,
    PhysicalReadSetV1,
    ReplayFreshness,
    make_binding_plan,
)
from featuregen.overlay.upload.planner.declarations import (
    build_compiler_context,
    build_physical_read_set,
)
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.plan_envelope import (
    PlanEnvelopeV1,
    recheck_plan_freshness,
)
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.templates import ALL_TEMPLATES, Template

logger = logging.getLogger(__name__)


class GovernedPlanDrift(Exception):
    """H3c fail-closed: at confirm, the governed plan rebuilt against the CURRENT snapshot no longer
    reproduces the pinned ``physical_plan_id`` / ``planner_declaration_id``, or its freshness is not
    ``current`` — the plan drifted between generation and confirm. The confirm must REGENERATE (never
    finalize a drifted plan, never accept a client-supplied physical plan). The route maps this to
    409 / retry, exactly like the existing ``recheck_plan_freshness`` staleness 409."""

    def __init__(self, reason: str, physical_plan_id: str) -> None:
        self.reason = reason
        self.physical_plan_id = physical_plan_id
        super().__init__(f"governed plan {physical_plan_id} drifted at confirm: {reason}")


def _template_for(recipe_id: str, templates: Sequence[Template] | None) -> Template | None:
    for t in (templates if templates is not None else ALL_TEMPLATES):
        if t.id == recipe_id:
            return t
    return None


def revalidate_governed_plan(
        conn, envelope: PlanEnvelopeV1, roles: Iterable[str], now: datetime | None = None, *,
        templates: Sequence[Template] | None = None,
) -> tuple[BindingPlanV1, PhysicalReadSetV1]:
    """REBUILD the governed plan the envelope pins, against the CURRENT catalog state, and require it
    to reproduce the SAME ids + a ``current`` freshness verdict. Returns ``(plan, physical_read_set)``
    for the caller to persist as role-labelled lineage, or raises :class:`GovernedPlanDrift` on genuine
    drift OR when the plan is NOT rebuildable (I-2: FAIL CLOSED, never a silent skip that promotes).

    The rebuild replays the planner EXACTLY as ``build_considered_set`` did: resolve the recipe's
    ``Template``, resolve the catalog scope at the envelope's ``target_entity``, build the batched
    compiler context, and ``plan_bindings`` with compile ON. The best COMPILED source→target plan is
    the governed plan; its ``physical_plan_id`` (physical identity) and ``contract_id`` (declaration
    identity / ``planner_declaration_id``) MUST equal the envelope's pinned ids, and
    ``recheck_plan_freshness`` MUST be ``current`` — else the plan drifted (a column dropped/retyped, a
    bridge revoked, a governance declaration changed).

    M-2b — CONCURRENCY: ``/contract/confirm`` runs on a READ COMMITTED connection (deliberately, MF-2),
    NOT repeatable read. No compile_ctx column snapshot is captured: the read is CURRENT-state, so a
    snapshot that changed since generation genuinely rebuilds to a different id → drift. A commit by a
    concurrent writer BETWEEN this freshness check and the rebuild can therefore surface as a transient
    ``GovernedPlanDrift`` (→ 409) even though the plan is intact; the 409 is RETRYABLE and the regenerate
    resettles on the newly-committed state — acceptable, and strictly fail-closed (never a stale promote).

    I-2 — NOT REBUILDABLE FAILS CLOSED (raise, not skip): a legacy envelope with no ``target_entity``
    (pre-H3c) or a ``recipe_id`` absent from THIS deployment's registry cannot be rebuilt, so its pinned
    ids can neither be re-verified nor its read set recorded — a confirm on it would PROMOTE unverified
    provenance with NO lineage. The freshness gate is applied FIRST (before these guards) so it covers
    ALL governed confirm paths — the route AND any direct ``confirm_contract`` caller — and then a
    not-rebuildable plan is refused (``GovernedPlanDrift`` → 409, regenerate). Neither case reaches a live
    governed confirm in production (the planner only emits registry recipes at a known grain; a registry
    rollback is a CODE change the live-activation version vector already fails closed on), so refusing is
    the strongest fail-closed with no legitimate-flow cost."""
    now = now or datetime.now(UTC)
    roles = tuple(roles)
    pinned = envelope.physical_plan_id

    # Freshness FIRST (cheap, fingerprint-based) — applied on EVERY governed confirm path BEFORE the
    # rebuildability guards, so a drifted plan is rejected even when it is not rebuildable (I-2: the
    # freshness gate is never skipped by an early return). Mirrors the draft-time StalePlan gate.
    fresh = recheck_plan_freshness(conn, envelope, roles)
    if fresh is not ReplayFreshness.current:
        raise GovernedPlanDrift(f"plan freshness is {fresh.value}", pinned)

    # NOT REBUILDABLE → FAIL CLOSED (I-2): cannot re-verify the pinned ids nor record read-set lineage,
    # so REFUSE rather than let the confirm promote unverified provenance.
    if envelope.target_entity is None:
        logger.warning("governed plan %s has no target_entity (legacy envelope) — NOT rebuildable; "
                       "failing closed (no unverified promote)", pinned)
        raise GovernedPlanDrift("legacy envelope has no target_entity — not rebuildable", pinned)
    template = _template_for(envelope.recipe_id, templates)
    if template is None:
        logger.warning("governed plan %s recipe %r is not in the running registry — NOT rebuildable; "
                       "failing closed (registry drift is also gated by the version-vector interlock)",
                       pinned, envelope.recipe_id)
        raise GovernedPlanDrift(
            f"recipe {envelope.recipe_id!r} absent from the running registry — not rebuildable", pinned)

    scope = resolve_catalog_scope(conn, roles=roles, target_entity=envelope.target_entity, now=now)
    compile_ctx = build_compiler_context(conn, scope, roles, now)
    result = plan_bindings(conn, template=template, target_entity=envelope.target_entity, scope=scope,
                           roles=roles, now=now, compile_ctx=compile_ctx)

    pid = result.selected_contract_physical_plan_id
    if pid is None:
        raise GovernedPlanDrift("rebuild produced no compiled source→target plan", pinned)
    plan = next((p for p in result.candidate_plans if p.physical_plan_id == pid), None)
    if plan is None:   # defensive — the selected id must be in the candidate set
        raise GovernedPlanDrift("selected plan absent from the rebuilt candidate set", pinned)

    # STABLE-ID revalidation — the whole point (RISK-4: COMPARE, never mint anew for the store).
    if plan.physical_plan_id != envelope.physical_plan_id:
        raise GovernedPlanDrift(
            f"physical_plan_id drifted (rebuilt {plan.physical_plan_id})", pinned)
    if plan.contract_id != envelope.contract_id:
        raise GovernedPlanDrift(
            f"planner_declaration_id drifted (rebuilt {plan.contract_id})", pinned)

    # RISK-4 assertion (M-2a — a REAL check, not the old tautology): re-MINT the physical_plan_id from the
    # rebuilt plan's OWN material through the canonical constructor and require it to reproduce the same id.
    # The equality above only compares two already-minted ids; this independently proves minting is
    # DETERMINISTIC for identical input (mint twice → same id) and fails loud if a future refactor ever
    # makes ``make_binding_plan`` fold non-deterministic material into the id. COMPARE only — no id material
    # changes (RISK-4 stays clean); ``candidate_role`` is reset post-construction and is NOT hashed.
    reminted = make_binding_plan(
        recipe_id=plan.recipe_id, target_entity=plan.target_entity, catalog_source=plan.catalog_source,
        ingredient_bindings=plan.ingredient_bindings, path_segments=plan.path_segments,
        resolution_status=plan.resolution_status, path_resolution_status=plan.path_resolution_status,
        primary_reason_code=plan.primary_reason_code, reason_codes=plan.reason_codes,
        safety=plan.safety, preference_rank=plan.preference_rank,
        preference_reasons=plan.preference_reasons, candidate_role=plan.candidate_role)
    if reminted.physical_plan_id != plan.physical_plan_id:
        raise GovernedPlanDrift(
            f"physical_plan_id minting is not deterministic (re-minted {reminted.physical_plan_id})", pinned)

    read_set = build_physical_read_set(compile_ctx, plan)
    return plan, read_set
