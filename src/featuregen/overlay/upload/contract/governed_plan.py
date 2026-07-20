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
) -> tuple[BindingPlanV1, PhysicalReadSetV1] | None:
    """REBUILD the governed plan the envelope pins, against the CURRENT catalog state, and require it
    to reproduce the SAME ids + a ``current`` freshness verdict. Returns ``(plan, physical_read_set)``
    for the caller to persist as role-labelled lineage, ``None`` when the plan is NOT rebuildable, or
    raises :class:`GovernedPlanDrift` on genuine drift.

    The rebuild replays the planner EXACTLY as ``build_considered_set`` did: resolve the recipe's
    ``Template``, resolve the catalog scope at the envelope's ``target_entity``, build the batched
    compiler context, and ``plan_bindings`` with compile ON. The best COMPILED source→target plan is
    the governed plan; its ``physical_plan_id`` (physical identity) and ``contract_id`` (declaration
    identity / ``planner_declaration_id``) MUST equal the envelope's pinned ids, and
    ``recheck_plan_freshness`` MUST be ``current`` — else the plan drifted (a column dropped/retyped, a
    bridge revoked, a governance declaration changed). No compile_ctx column snapshot is captured: the
    read is CURRENT-state under the confirm's own transaction (repeatable-read gives it a consistent
    view), so a snapshot that changed since generation genuinely rebuilds to a different id → drift.

    NOT REBUILDABLE → ``None`` (defense-in-depth, not the sole gate): a legacy envelope with no
    ``target_entity`` (pre-H3c), or a ``recipe_id`` absent from THIS deployment's registry. Neither can
    reach a live governed confirm in production — the planner only emits registry recipes at a known
    grain, and a registry rollback that removed the recipe is a CODE change the live-activation VERSION
    VECTOR already fails closed on (RECIPE_REGISTRY_VERSION). The route's own ``recheck_plan_freshness``
    + cross-catalog interlock still gate catalog/activation drift; the stable-id rebuild is additive."""
    now = now or datetime.now(UTC)
    roles = tuple(roles)
    pinned = envelope.physical_plan_id
    if envelope.target_entity is None:
        logger.info("governed plan %s has no target_entity (legacy envelope) — skipping the confirm-time "
                    "rebuild; the route freshness recheck + interlock still gate", pinned)
        return None
    template = _template_for(envelope.recipe_id, templates)
    if template is None:
        logger.info("governed plan %s recipe %r is not in the running registry — skipping the "
                    "confirm-time rebuild (registry drift is gated by the version-vector interlock)",
                    pinned, envelope.recipe_id)
        return None

    # Freshness FIRST (cheap, fingerprint-based): a drifted / unverifiable / incompatible plan is
    # rejected before the (heavier) rebuild — mirrors the draft-time StalePlan gate.
    fresh = recheck_plan_freshness(conn, envelope, roles)
    if fresh is not ReplayFreshness.current:
        raise GovernedPlanDrift(f"plan freshness is {fresh.value}", pinned)

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

    # RISK-4 assertion: an UNCHANGED snapshot MUST reproduce the same physical id — this reproduction
    # is the revalidation. (Reaching here already proves equality; the assert documents the invariant
    # and fails loud if a future refactor ever lets the rebuild mint a different id for the same input.)
    assert plan.physical_plan_id == envelope.physical_plan_id, "rebuild must reproduce the physical id"

    read_set = build_physical_read_set(compile_ctx, plan)
    return plan, read_set
