"""Phase 3C.2b-i-B · Task 9 — the bounded admin service entrypoint (``govern_llm_idea``).

The KEYSTONE: a CLI/admin service function that governs ONE untrusted LLM cross-catalog feature idea
end-to-end. It is deliberately NOT an HTTP considered-set route — it touches nothing in
``build_considered_set`` / ``_reject_cross_catalog_llm`` / ``is_live``; it composes the T2–T8
normalization (``b_adapter.normalize_feature_idea``), drives A's REAL ``plan_multi_source`` inside a
savepoint under a finite compile budget, and returns a :class:`GovernedResult` ONLY on the two-axis
pass.

THE TWO-AXIS GATE (load-bearing). A's run-level result carries two independent resolution axes — the
assembly axis and the contract axis. :func:`map_a_outcome` yields ``governed`` ONLY when BOTH are
resolved with both winning ids set. An assembly-resolved-but-contract-incomplete result maps to
``contract_unresolved`` and MUST NEVER become a :class:`GovernedResult` — greenlighting an
operationally-unresolved feature is exactly the failure this gate prevents.

Preconditions RAISE (shadow flag / auth / payload cap / trust derivation are caller contract, not a
governance verdict); governance outcomes RETURN a :class:`BDisposition`. Shadow-only, off by default,
``os.environ`` flag only (no ``Settings`` field); fail-closed; A is UNCHANGED.
"""
from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass
from datetime import datetime, timedelta

from featuregen.contracts import DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.identity.permissions import FEATURE_GENERATE, has_permission
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.upload.feature_assist import Requirement
from featuregen.overlay.upload.planner.b_adapter import normalize_feature_idea
from featuregen.overlay.upload.planner.b_dispositions import BDisposition, map_a_outcome
from featuregen.overlay.upload.planner.b_proposal import RawFeatureProposalV1
from featuregen.overlay.upload.planner.b_scope import derive_request_context
from featuregen.overlay.upload.planner.declarations import CompileBudget
from featuregen.overlay.upload.planner.multisource_contracts import (
    MultiSourcePlannerIntentV1,
    MultiSourcePlanningResultV1,
)
from featuregen.overlay.upload.planner.multisource_plan import plan_multi_source

logger = logging.getLogger(__name__)

# The off-by-default shadow flag (env only — never a Settings field: neutrality means flag-off does
# nothing, not even read config).
FEATUREGEN_LLM_XCAT_SHADOW = "FEATUREGEN_LLM_XCAT_SHADOW"

MAX_RAW_OPERANDS = 8      # gross payload cap (DoS guard, applied to the RAW operand list)
BOUNDED_PLANS = 64        # compile-plan budget passed to A (soft: A only DECREMENTS it per compile)


class XCatShadowDisabledError(Exception):
    """The governed-LLM cross-catalog shadow flag is OFF. Raised BEFORE any DB work so flag-off is
    provably inert (does nothing, touches nothing)."""


def _xcat_shadow_enabled() -> bool:
    """Whether the off-by-default ``FEATUREGEN_LLM_XCAT_SHADOW`` shadow flag is on (``"1"``)."""
    return os.environ.get(FEATUREGEN_LLM_XCAT_SHADOW, "0") == "1"


@dataclass(frozen=True, slots=True)
class GovernedResult:
    """The sole success carrier: a governed cross-catalog plan on the two-axis pass. ``disposition``
    is ALWAYS ``governed`` here — a non-governed A outcome never produces a ``GovernedResult`` (the
    two-axis gate). The Slice-3 tri-state (``validation_status`` + ``requirements``) rides from the
    normalized idea, so an honest NEEDS_EXTERNAL_VALIDATION is preserved through governance."""

    intent: MultiSourcePlannerIntentV1
    planning_result: MultiSourcePlanningResultV1
    disposition: BDisposition
    validation_status: str
    requirements: tuple[Requirement, ...]


def govern_llm_idea(
    conn: DbConn,
    adapter: CatalogAdapter,
    *,
    actor: IdentityEnvelope,
    proposal: RawFeatureProposalV1,
    generation_run_id: str,
    now: datetime,
    fresh_within: timedelta,
    budget: CompileBudget | None = None,
) -> GovernedResult | BDisposition:
    """Govern ONE untrusted LLM cross-catalog idea, returning a :class:`GovernedResult` ONLY on the
    two-axis pass, else the governing :class:`BDisposition`.

    Preconditions RAISE (not a governance verdict):

    1. Shadow flag off -> :class:`XCatShadowDisabledError` (BEFORE any DB work — flag-off is inert).
    2. Auth: require ``authenticated`` AND the ``feature:generate`` permission (mirrors
       ``require_admin`` — never roles alone) -> :class:`PermissionError` otherwise.
    3. Gross payload cap on the RAW operand list -> :class:`ValueError` (DoS guard).
    4. Server-side trust derivation (T3): scope + target_entity are SERVER-derived from the
       authenticated roles + the durable confirmed scope; a bogus/absent run raises
       ``TrustDerivationError`` (a valid confirmed-scope run is a caller precondition).

    Governance outcomes RETURN a :class:`BDisposition`:

    5. Normalize (T2–T8) -> the first failing step's disposition, else a ``NormalizedIntentV1``.
    6. Drive A's real ``plan_multi_source`` inside a ``conn.transaction()`` SAVEPOINT under a finite
       budget; a plan failure is contained (savepoint rolled back) -> ``technical_failure``, never
       poisoning the outer transaction.
    7. The two-axis gate: ``governed`` -> a :class:`GovernedResult`; every other disposition (incl.
       assembly-resolved-but-contract-incomplete -> ``contract_unresolved``) returns the disposition
       and NEVER a ``GovernedResult``.
    """
    # 1. Shadow flag — BEFORE any DB work (neutrality: flag-off does nothing).
    if not _xcat_shadow_enabled():
        raise XCatShadowDisabledError(
            f"{FEATUREGEN_LLM_XCAT_SHADOW} is off; the governed-LLM cross-catalog entrypoint is inert")

    # 2. Auth — authenticated AND the feature:generate permission (never roles alone).
    if not (actor.authenticated and has_permission(actor.role_claims, FEATURE_GENERATE)):
        raise PermissionError(f"{FEATURE_GENERATE} permission required")

    # 3. Gross payload cap (DoS guard over the RAW operand list, before any resolution).
    if len(proposal.operands) > MAX_RAW_OPERANDS:
        raise ValueError(
            f"proposal carries {len(proposal.operands)} operands; the cap is {MAX_RAW_OPERANDS}")

    # 4. Server-derived trust context (T3). The caller supplies neither scope nor target_entity — a
    #    TrustDerivationError (no confirmed scope for the run) propagates as a caller precondition.
    ctx = derive_request_context(
        conn, roles=actor.role_claims, generation_run_id=generation_run_id, now=now)

    # 5. Normalize the raw proposal (T2–T8). A governance reject short-circuits with its disposition.
    normalized = normalize_feature_idea(
        conn, adapter, proposal=proposal, ctx=ctx, roles=actor.role_claims, now=now,
        fresh_within=fresh_within)
    if isinstance(normalized, BDisposition):
        return normalized

    # 6. Bounded + savepoint-isolated plan. A plan failure must not poison the outer transaction.
    #    Per-request bounding is STRUCTURAL — the single-operand shape (one compile), the operand
    #    cap, the savepoint, and A's internal enumeration caps — NOT budget-enforced: A only
    #    DECREMENTS ``remaining`` (never checks it) and never reads the deadline on a direct
    #    ``plan_multi_source`` call, so a wall-clock deadline here would be decorative (a synchronous
    #    compile can't be interrupted mid-call). The budget is passed for A-contract completeness.
    budget = budget or CompileBudget(
        remaining=BOUNDED_PLANS, deadline_monotonic=float("inf"), clock=time.monotonic)
    try:
        with conn.transaction():  # savepoint
            result = plan_multi_source(
                conn, adapter, intent=normalized.intent, scope=ctx.scope,
                roles=actor.role_claims, now=now, budget=budget)
    except Exception:
        logger.exception("multi-source planning failed")
        return BDisposition.technical_failure

    # 7. The two-axis gate (load-bearing). A GovernedResult ONLY when A returns ``governed``; every
    #    other disposition — incl. assembly-resolved-but-contract-incomplete (``contract_unresolved``)
    #    — returns the disposition, NEVER a GovernedResult.
    disp = map_a_outcome(result)
    if disp is BDisposition.governed:
        return GovernedResult(
            intent=normalized.intent, planning_result=result, disposition=disp,
            validation_status=normalized.validation_status, requirements=normalized.requirements)
    return disp
