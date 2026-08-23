"""S1B-3 — the telemetry worker: claim one outbox item, replan its FROZEN inputs, observe, complete.

The request path persists a work item and plans nothing (``contract/gate1.py``). This is the other
half. Everything it consumes is the work item plus the DB: it reads NO environment variable, calls
NO provider, and never re-derives an LLM intent — the intent's whole request rode along in the
frozen payload precisely so this side is provider-free and replayable.

Four rules shape the whole module:

**Every outcome leaves a row.** Resolved, refused, bounded out, or stale — one
``governed_planning_observation`` per request. A telemetry programme that records only successes can
report a resolution rate and can never report a DEMAND, and the demand is what funds bridge work.

**Demand has TWO sources, and the second one is the one that fires today.** The first is S1B-2's
typed unmet hop: a frontier that dead-ended, keyed off each HOP's own verdict (never off the
rejection's headline reason — a rejection can carry a contract refusal at the top and demand-bearing
hops underneath). The second is the CONTRACT-level realization gap: the platform's dominant real
refusal, ``physical_cardinality_unavailable``, rides a path that RESOLVED source→target, so it mints
no rejected candidate and carries no unmet hop at all. Keyed only on hops, the single most common
gap in the platform would file zero demand. It is filed from the best plan's own bridge segments
instead — see :func:`contract_level_demands`.

**A moved registry is not a plan.** A recipe-origin request is frozen as an id plus the
``planning_request_hash`` it had at enqueue. The worker re-derives from the registry and compares.
A mismatch means the definition moved under the work item, and planning the NEW definition would
silently answer a different question under the old run's lineage — so the request is recorded
``stale_registry`` and skipped.

**The fence is load-bearing.** ``complete_telemetry_work`` returns False when this worker no longer
holds the lease. That is not a nuisance return value: it means another worker reclaimed the item and
is authoritative for it. Everything this worker computed is therefore written inside ONE savepoint
that is rolled back when the completion is refused — a zombie's observations never land beside the
live worker's.
"""
from __future__ import annotations

import logging
import re
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from featuregen.contracts import DbConn
from featuregen.overlay.upload.catalog_realizations import table_of
from featuregen.overlay.upload.contract.governed_identity import (
    DEFINITION_ORIGIN_LLM_INTENT,
    DEFINITION_ORIGIN_RECIPE_V2,
    GovernedVariantIdentityV1,
)

# `_parameter_binding_hash` is the governed lens's OWN derivation of the semantic parameter hash
# that enters a variant identity. Imported rather than re-implemented: a second derivation could
# drift, and then one request would have two identities depending on whether it resolved.
from featuregen.overlay.upload.contract.governed_lens import (
    GovernedOptionV1,
    _parameter_binding_hash,
    governed_options_from_requests,
)
from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.field_resolution import canonical_hash
from featuregen.overlay.upload.governed_observation_store import (
    claim_telemetry_work,
    complete_telemetry_work,
    record_bridge_demand,
    record_planning_observations,
)
from featuregen.overlay.upload.governed_scope_material import (
    FROZEN_INPUTS_VERSION,
    governed_scope_material,
    planning_request_from_frozen,
)
from featuregen.overlay.upload.planner.contracts import ReasonCode
from featuregen.overlay.upload.planner.declarations import CompileBudget
from featuregen.overlay.upload.planner.shadow import COMPILE_BUDGET, MAX_COMPILES_PER_RUN
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.semantic_eligibility import clears

logger = logging.getLogger(__name__)

#: One work item is one bounded planning job. A run may be eligible for hundreds of recipes; an
#: unbounded item would make a worker's runtime a function of the registry's size, and a lease
#: cannot be sized against something that grows.
MAX_REQUESTS_PER_ITEM = 60

#: THE identity decision for a row with no plan (S1B-3). ``GovernedVariantIdentityV1`` refuses a
#: blank ``physical_plan_content_hash`` — correctly: blank would collapse every unresolved variant
#: of one definition onto one id. A second 64-hex digest would be worse than blank: it is exactly
#: the shape a REAL plan hash has, so a reader (and the lens's own validator, which checks for 64
#: hex chars) could not tell a plan that compiled from one that never existed. So the sentinel is
#: deliberately NOT hash-shaped, it is stored in the row's own ``physical_plan_content_hash``
#: column as well as hashed into ``governed_variant_id``, and a consumer can therefore recompute
#: the id from the row's values alone.
UNRESOLVED_PLAN_CONTENT_HASH = "unresolved"

#: The resolution status of a request the worker refused to plan because the registry moved under
#: the work item. Not a planner verdict — 1120 leaves ``resolution_status`` free text precisely so
#: an ADAPTER-level outcome can be recorded honestly instead of dressed as one.
STALE_REGISTRY = "stale_registry"

#: The same refusal for a PAYLOAD-carried request (an LLM intent): its bytes did not reconstruct
#: into a request that hashes to what was frozen. Spelled differently from ``stale_registry``
#: because the remedy is different — nobody's registry moved, this run's own frozen bytes and the
#: reconstruction disagree, which is a defect in the freeze/thaw pair and not a fact about the
#: catalog.
PAYLOAD_UNREADABLE = "payload_unreadable"

_MODE = "telemetry"
_MAX_ERROR = 2000

#: The capacity verdict that can NEVER arrive through ``unmet_hops``: its reject is minted from the
#: START state, which realized no hop. Filed at the rejection level or not at all.
_REJECTION_LEVEL_CAPACITY = (ReasonCode.bounded_out_max_frontier_states.value,)

#: R21: the closed window vocabulary a hypothesis may spell. Nothing here parses free text for
#: meaning — it finds a number followed by a unit, and refuses to guess at anything else.
#:
#: A bare ``m`` is deliberately NOT a month: "a $30m book" would otherwise imply a 900-day window
#: and file a divergence against a hypothesis that named no window at all. A bare ``d`` is kept
#: (``30d`` is how this platform's own parameter values are spelled), and ``\b`` on BOTH sides
#: stops ``30days`` inside a longer token from matching.
_WINDOW_MENTION = re.compile(r"\b(\d+)\s*[-\s]?\s*(days?|d|months?)\b", re.IGNORECASE)
_UNIT_DAYS = {"d": 1, "day": 1, "days": 1, "month": 30, "months": 30}


class _LeaseLost(Exception):
    """Raised INSIDE the item's savepoint so everything this worker wrote is rolled back."""


@dataclass(frozen=True, slots=True)
class _Restored:
    """One frozen request reference, after restoration. ``request`` is None when it may not be
    planned, and ``status`` then says why in the ledger's own vocabulary."""

    ref: dict
    request: FeaturePlanningRequestV1 | None
    status: str = ""


# ── the entry point ───────────────────────────────────────────────────────────────────────────


def run_governed_telemetry_once(conn: DbConn, *, owner: str,
                                now: datetime | None = None) -> dict | None:
    """Claim one outbox item, restore its frozen inputs, plan, record observations + demand,
    complete. Returns a summary dict, or None when nothing was claimable.

    Never raises for an item-scoped error: a planner explosion, an unreadable payload or an
    integrity error is THIS ITEM's failure (``complete_telemetry_work(ok=False)``, which is
    terminal by S1B-1's ruling — a failure worth re-running is re-enqueued deliberately, with its
    own frozen inputs, never silently re-attempted under inputs nobody re-checked).
    """
    item = claim_telemetry_work(conn, owner=owner)
    if item is None:
        return None
    work_item_id = item["work_item_id"]
    fence = item["lease_fence"]
    base = {"work_item_id": work_item_id, "generation_run_id": item["generation_run_id"],
            "intent_id": item["intent_id"], "observations": 0, "demands": 0, "planned": 0,
            "resolved": 0, "rejected": 0, "stale": 0, "dropped": 0, "error": ""}
    discarded = 0
    try:
        with conn.transaction():
            summary = {**base, **_process_item(conn, item, now=now), "status": "done"}
            discarded = summary["observations"]
            if not complete_telemetry_work(conn, work_item_id=work_item_id, owner=owner,
                                           fence=fence, ok=True):
                raise _LeaseLost(work_item_id)
    except _LeaseLost:
        # Not an error in this worker: the item was reclaimed while it was planning, and the
        # reclaiming worker is authoritative. Its results are already rolled back by the savepoint.
        logger.warning("governed telemetry: lease lost on %s (fence %s, owner %s) — %d "
                       "observation(s) discarded", work_item_id, fence, owner, discarded)
        return {**base, "status": "lease_lost"}
    except Exception as exc:                                          # noqa: BLE001 — item-scoped
        logger.exception("governed telemetry: item %s failed", work_item_id)
        error = f"{type(exc).__name__}: {exc}"[:_MAX_ERROR]
        if not complete_telemetry_work(conn, work_item_id=work_item_id, owner=owner, fence=fence,
                                       ok=False, error=error):
            logger.warning("governed telemetry: could not mark %s failed — lease no longer held",
                           work_item_id)
        return {**base, "status": "failed", "error": error}
    return summary


# ── one item ──────────────────────────────────────────────────────────────────────────────────


def _process_item(conn: DbConn, item: dict, *, now: datetime | None) -> dict:
    frozen = dict(item["frozen_inputs"] or {})
    version = str(frozen.get("frozen_inputs_version") or "")
    if version != FROZEN_INPUTS_VERSION:
        raise ValueError(
            f"frozen inputs are version {version!r}, this worker reads "
            f"{FROZEN_INPUTS_VERSION!r} — a payload whose shape is unknown is failed, never guessed")
    target_entity = str(frozen.get("target_entity") or "")
    roles = tuple(frozen.get("roles") or ())
    resolved_now = now or datetime.now(UTC)

    restored, dropped = _restore(frozen)
    plannable = [entry.request for entry in restored if entry.request is not None]
    options: list[GovernedOptionV1] = []
    rejections: list[dict] = []
    if plannable:
        options, rejections = governed_options_from_requests(
            conn, requests=tuple(plannable), target_entity=target_entity, roles=roles,
            now=resolved_now,
            budget=CompileBudget(remaining=MAX_COMPILES_PER_RUN,
                                 deadline_monotonic=(time.monotonic()
                                                     + COMPILE_BUDGET.total_seconds()),
                                 clock=time.monotonic))

    option_by_hash = {planning_request_hash(option.request): option for option in options}
    rejection_by_hash = {str(rejection.get("planning_request_hash") or ""): rejection
                         for rejection in rejections}
    common = {
        "target_entity": target_entity,
        "catalog_scope_material": _scope_material_record(
            conn, frozen=frozen, roles=roles, target_entity=target_entity),
        "metadata_snapshot_id": frozen.get("metadata_snapshot_id"),
        "compiler_input_fingerprint": _compiler_input_fingerprint(frozen),
    }
    corroborating = _corroborating_signatures(restored)
    hypothesis = str(frozen.get("redacted_hypothesis") or "")

    rows: list[dict] = []
    demands_per_row: list[list[dict]] = []
    counts = {"resolved": 0, "rejected": 0, "stale": 0}
    # One realization lookup per DISTINCT bridge fact key per ITEM, shared across every rejection.
    realization_cache: dict[str, str] = {}
    frozen_anchor = str(frozen.get("anchor_catalog_source") or "")
    for entry in restored:
        if entry.request is None:
            rows.append(_unplanned_row(entry, common=common, anchor_catalog_source=frozen_anchor))
            demands_per_row.append([])
            counts["stale"] += 1
            continue
        request_hash = planning_request_hash(entry.request)
        option = option_by_hash.get(request_hash)
        corroborated = (entry.request.origin == DEFINITION_ORIGIN_RECIPE_V2
                        and _signature(entry.request) in corroborating)
        divergence = (_param_divergence(entry.request, hypothesis)
                      if entry.request.origin == DEFINITION_ORIGIN_RECIPE_V2 else [])
        if option is not None:
            rows.append(_resolved_row(option, common=common, corroborated=corroborated,
                                      divergence=divergence))
            demands_per_row.append([])
            counts["resolved"] += 1
            continue
        rejection = rejection_by_hash.get(request_hash)
        if rejection is None:
            # Structurally impossible: the lens returns exactly one option or one rejection per
            # request. Recorded rather than dropped, so an upstream change that breaks the
            # invariant shows up as a countable row instead of a missing one.
            logger.error("governed telemetry: request %s produced neither an option nor a "
                         "rejection", entry.request.source_definition_id)
            rejection = {"reason": ReasonCode.planner_internal_error.value}
        rows.append(_rejected_row(entry.request, rejection, common=common,
                                  corroborated=corroborated, divergence=divergence,
                                  fallback_anchor=frozen_anchor))
        demands_per_row.append(demands_for_rejection(
            conn, rejection, recipe_revision_hash=entry.request.source_content_hash,
            realization_cache=realization_cache))
        counts["rejected"] += 1

    observation_ids = record_planning_observations(
        conn, generation_run_id=item["generation_run_id"], intent_id=item["intent_id"],
        observation_mode=_MODE, rows=rows)
    filed = 0
    for observation_id, demands in zip(observation_ids, demands_per_row, strict=True):
        if demands:
            filed += len(record_bridge_demand(conn, observation_id=observation_id,
                                              rejections=demands))
    return {"observations": len(rows), "demands": filed, "planned": len(plannable),
            "dropped": dropped, **counts}


# ── restoring the request set ─────────────────────────────────────────────────────────────────


def _order_key(ref: dict) -> tuple[int, str]:
    """Intents FIRST, then canonical id. The run's OWN proposals are never the half a cap drops —
    a recipe the cap skipped is still in the registry tomorrow; the intent that this run's provider
    proposed exists nowhere else."""
    origin = str(ref.get("origin") or "")
    return (0 if origin == DEFINITION_ORIGIN_LLM_INTENT else 1,
            str(ref.get("canonical_definition_id") or ""))


def _restore(frozen: dict) -> tuple[list[_Restored], int]:
    """Rebuild every frozen reference, in a deterministic order, capped.

    The cap's drop is COUNTED and logged. Silent truncation is how a resolution rate quietly
    becomes a rate over whatever happened to fit."""
    refs = sorted(frozen.get("requests") or [], key=_order_key)
    cap = MAX_REQUESTS_PER_ITEM
    kept, dropped = refs[:cap], refs[cap:]
    if dropped:
        logger.warning("governed telemetry: %d request(s) dropped by the per-item cap of %d "
                       "(first dropped: %s)", len(dropped), cap,
                       dropped[0].get("canonical_definition_id"))
    return [_restore_one(ref) for ref in kept], len(dropped)


def _restore_one(ref: dict) -> _Restored:
    """Rebuild one reference, and PROVE the rebuild by hash before letting it near the planner.

    A recipe is re-derived from the registry and refused on drift (:func:`_restore_recipe`). A
    payload-carried request is reconstructed and its ``planning_request_hash`` re-checked against
    the frozen one — the check the freeze's contract promises. A lossy round trip is not a
    theoretical worry: JSON has no tuples, so any value the reconstruction failed to re-tuple
    would produce a request that hashes differently and would then be planned, observed and
    reported as though it were the request this run actually proposed. Recorded, never planned.
    """
    origin = str(ref.get("origin") or "")
    if origin == DEFINITION_ORIGIN_RECIPE_V2:
        return _restore_recipe(ref)
    payload = ref.get("payload")
    definition_id = ref.get("canonical_definition_id")
    if not payload:
        logger.warning("governed telemetry: %s-origin ref %s carries no payload and cannot be "
                       "reconstructed", origin, definition_id)
        return _Restored(ref=ref, request=None, status=PAYLOAD_UNREADABLE)
    try:
        request = planning_request_from_frozen(payload)
    except Exception:
        logger.warning("governed telemetry: %s-origin payload %s does not reconstruct into a "
                       "valid planning request", origin, definition_id, exc_info=True)
        return _Restored(ref=ref, request=None, status=PAYLOAD_UNREADABLE)
    if planning_request_hash(request) != str(ref.get("planning_request_hash") or ""):
        logger.warning("governed telemetry: %s-origin payload %s did not round-trip (rebuilt hash "
                       "differs from the frozen one); not planned", origin, definition_id)
        return _Restored(ref=ref, request=None, status=PAYLOAD_UNREADABLE)
    return _Restored(ref=ref, request=request)


def _restore_recipe(ref: dict) -> _Restored:
    """Re-derive from the registry and REFUSE on drift.

    Both failure modes mean the same thing to a reader — the definition this run planned is not the
    definition this build carries — so both record ``stale_registry`` rather than one of them
    vanishing as a skipped id."""
    recipe_id = str(ref.get("canonical_definition_id") or "")
    definition = v2_recipe_by_id(recipe_id)
    if definition is None:
        logger.info("governed telemetry: recipe %s left the registry since the enqueue", recipe_id)
        return _Restored(ref=ref, request=None, status=STALE_REGISTRY)
    request = planning_request_from_recipe(definition)
    if planning_request_hash(request) != str(ref.get("planning_request_hash") or ""):
        logger.info("governed telemetry: recipe %s moved since the enqueue; not planned", recipe_id)
        return _Restored(ref=ref, request=None, status=STALE_REGISTRY)
    return _Restored(ref=ref, request=request)


# ── the observation rows ──────────────────────────────────────────────────────────────────────


def _identity_for(request: FeaturePlanningRequestV1, *,
                  physical_plan_content_hash: str) -> GovernedVariantIdentityV1:
    return GovernedVariantIdentityV1(
        canonical_definition_id=request.source_definition_id,
        definition_origin=request.origin,
        planning_request_hash=planning_request_hash(request),
        physical_plan_content_hash=physical_plan_content_hash,
        parameter_binding_hash=_safe_parameter_binding_hash(request))


def _safe_parameter_binding_hash(request: FeaturePlanningRequestV1) -> str:
    """The lens's own derivation, which RAISES on a parameter type its sibling producer refuses.

    A refusal must not cost the observation: telemetry that disappears when a parameter is odd
    measures only the easy cases. The absence is logged and the hash is the honest ``""``."""
    try:
        return _parameter_binding_hash(request)
    except Exception:
        logger.warning("governed telemetry: %s has an unhashable parameter binding; recorded as "
                       "absent", request.source_definition_id, exc_info=True)
        return ""


def _resolved_row(option: GovernedOptionV1, *, common: dict, corroborated: bool,
                  divergence: list[dict]) -> dict:
    """A resolved request's row.

    Every plan-shaped column comes from ``option.plan_facts`` — the SAME ``_plan_facts``
    derivation a rejection carries — so ``anchor_catalog_source`` and ``hop_count`` mean exactly
    one thing across the whole table. They used to be re-derived here from the idea and its
    envelope, which read the anchor off a SORTED read set (right only while the anchor happened to
    sort first) and counted path segments instead of fan-in hops."""
    facts = option.plan_facts
    envelope = option.idea.plan_envelope
    return {
        **common,
        "definition_origin": option.identity.definition_origin,
        "canonical_definition_id": option.identity.canonical_definition_id,
        "recipe_id": option.idea.recipe_id,
        "governed_variant_id": option.identity.governed_variant_id,
        "planning_request_hash": option.identity.planning_request_hash,
        "parameter_binding_hash": option.identity.parameter_binding_hash,
        "physical_plan_content_hash": option.identity.physical_plan_content_hash,
        "selected_physical_plan_id": (facts.get("physical_plan_id")
                                      or (envelope.physical_plan_id if envelope is not None
                                          else "")),
        "contract_id": str(facts.get("contract_id") or "") or None,
        "primary_objective": option.request.primary_objective,
        "anchor_catalog_source": str(facts.get("anchor_catalog_source") or ""),
        "resolution_status": "resolved",
        # A resolved row records no refusal. The plan's own observed codes stay on the plan; a
        # reason code beside `resolution_status = resolved` would read as a refusal that resolved.
        "reason_codes": [],
        "participating_catalogs": list(facts.get("participating_catalogs") or ()),
        "hop_count": int(facts.get("hop_count") or 0),
        "bridge_count": int(facts.get("bridge_count") or 0),
        "authority_floor_status": _authority_floor_status(option),
        "readiness": option.readiness.state,
        "intent_corroborated": corroborated,
        "param_divergence": divergence,
    }


def _rejected_row(request: FeaturePlanningRequestV1, rejection: dict, *, common: dict,
                  corroborated: bool, divergence: list[dict],
                  fallback_anchor: str = "") -> dict:
    """A refused request's row. ``fallback_anchor`` is the run's own anchor catalog from the frozen
    inputs, used when the rejection has no plan to name one (a planner failure): the run's anchor
    is a fact this item KNOWS, and leaving the column blank would drop that row out of every
    per-anchor rollup for no reason."""
    reason = str(rejection.get("reason") or "")
    return {
        **common,
        "definition_origin": request.origin,
        "canonical_definition_id": request.source_definition_id,
        "recipe_id": (request.source_definition_id
                      if request.origin == DEFINITION_ORIGIN_RECIPE_V2 else None),
        "governed_variant_id": _identity_for(
            request,
            physical_plan_content_hash=UNRESOLVED_PLAN_CONTENT_HASH).governed_variant_id,
        "planning_request_hash": planning_request_hash(request),
        "parameter_binding_hash": _safe_parameter_binding_hash(request),
        "physical_plan_content_hash": UNRESOLVED_PLAN_CONTENT_HASH,
        "selected_physical_plan_id": str(rejection.get("physical_plan_id") or ""),
        "contract_id": str(rejection.get("contract_id") or "") or None,
        "primary_objective": request.primary_objective,
        "anchor_catalog_source": (str(rejection.get("anchor_catalog_source") or "")
                                  or fallback_anchor),
        "resolution_status": reason,
        "reason_codes": list(rejection.get("reason_codes") or ([reason] if reason else [])),
        "participating_catalogs": list(rejection.get("participating_catalogs") or ()),
        "hop_count": int(rejection.get("hop_count") or 0),
        "bridge_count": int(rejection.get("bridge_count") or 0),
        # Nothing was bound, so nothing was measured. "unmet" would claim a measurement that never
        # happened; "" would be indistinguishable from a column nobody filled.
        "authority_floor_status": "unevaluated",
        "readiness": "",
        "intent_corroborated": corroborated,
        "param_divergence": divergence,
    }


def _unplanned_row(entry: _Restored, *, common: dict, anchor_catalog_source: str = "") -> dict:
    """A row for a request that was never planned. It names what the RUN froze — the id, the
    request hash and the anchor catalog as they were at enqueue — because that is the fact worth
    keeping: this run wanted THIS definition and the build no longer has it."""
    ref = entry.ref
    origin = str(ref.get("origin") or DEFINITION_ORIGIN_RECIPE_V2)
    identity = GovernedVariantIdentityV1(
        canonical_definition_id=str(ref.get("canonical_definition_id") or ""),
        definition_origin=origin,
        planning_request_hash=str(ref.get("planning_request_hash") or ""),
        physical_plan_content_hash=UNRESOLVED_PLAN_CONTENT_HASH)
    return {
        **common,
        "definition_origin": origin,
        "canonical_definition_id": identity.canonical_definition_id,
        "recipe_id": (identity.canonical_definition_id
                      if origin == DEFINITION_ORIGIN_RECIPE_V2 else None),
        "governed_variant_id": identity.governed_variant_id,
        "planning_request_hash": identity.planning_request_hash,
        "physical_plan_content_hash": UNRESOLVED_PLAN_CONTENT_HASH,
        "anchor_catalog_source": anchor_catalog_source,
        "resolution_status": entry.status or STALE_REGISTRY,
        "authority_floor_status": "unevaluated",
    }


def _authority_floor_status(option: GovernedOptionV1) -> str:
    """``met`` when every REQUIRED operand's binding carries evidence that clears the AUTHORING
    rung of the shared authority matrix, ``unmet`` otherwise, ``unevaluated`` when the option
    bound none of them. The matrix is the platform's one answer to "what may this evidence
    authorize?" — re-deciding it here would give governed contracts two floors."""
    required = {operand.role for operand in option.request.operands if operand.required}
    bindings = [b for b in option.idea.input_role_bindings if b.role in required]
    if not bindings:
        return "unevaluated"
    return "met" if all(clears(b.authority, "authoring") for b in bindings) else "unmet"


def _scope_material_record(conn: DbConn, *, frozen: dict, roles: tuple[str, ...],
                           target_entity: str) -> dict:
    """The scope the RUN saw, and whether the REPLAN saw the same one.

    The lens resolves its own live catalog scope when it plans (``resolve_catalog_scope`` inside
    ``governed_options_from_requests``), so a replan can silently run against a wider or narrower
    authorization than the run did — and then "same inputs, different answer" would be a claim
    nobody checked. Asking the same question again here costs ONE read and makes the mismatch
    COUNTABLE: ``replan_matched`` is on every row, and the differing material is recorded beside
    the frozen one ONLY when it actually differs, so the common case stays the shape S1B-1
    specified.

    A read failure records ``replan_matched: null`` — "nobody could ask" is not "they matched".
    """
    frozen_material = dict(frozen.get("catalog_scope_material") or {})
    try:
        replan = governed_scope_material(conn, roles=roles, target_entity=target_entity)
    except Exception:
        logger.warning("governed telemetry: the replan scope could not be read; recorded as "
                       "unknown rather than matched", exc_info=True)
        return {"frozen": frozen_material, "replan_matched": None}
    if replan == frozen_material:
        return {"frozen": frozen_material, "replan_matched": True}
    logger.info("governed telemetry: the replan scope differs from the frozen one (%s vs %s)",
                replan.get("authorized_catalog_sources"),
                frozen_material.get("authorized_catalog_sources"))
    return {"frozen": frozen_material, "replan_matched": False, "replan": replan}


def _compiler_input_fingerprint(frozen: dict) -> str:
    """One fingerprint per WORK ITEM: the compiler inputs this replan ran under.

    It fingerprints the frozen INPUTS, not the request — every row of one item shares it, which is
    exactly what makes two runs comparable ("same scope, same snapshot, different answer") and what
    makes a scope change visible as a fingerprint change rather than as unexplained drift."""
    return canonical_hash({
        "version": FROZEN_INPUTS_VERSION,
        "target_entity": frozen.get("target_entity"),
        "roles": list(frozen.get("roles") or ()),
        "anchor_catalog_source": frozen.get("anchor_catalog_source"),
        "metadata_snapshot_id": frozen.get("metadata_snapshot_id"),
        "catalog_scope_material": dict(frozen.get("catalog_scope_material") or {}),
    })


# ── the two demand sources ────────────────────────────────────────────────────────────────────


def demands_for_rejection(conn: DbConn, rejection: dict, *, recipe_revision_hash: str,
                          realization_cache: dict[str, str] | None = None) -> list[dict]:
    """BOTH demand sources for one rejection, merged with the rejection-level fields the store
    reads (``recipe_revision_hash``, ``anchor_catalog_source`` — passed explicitly so the store
    never has to look the anchor up).

    ``recipe_revision_hash`` is a PARAMETER rather than a field read off a request, because the
    two writers hold different things: this worker has a ``FeaturePlanningRequestV1`` and passes
    its ``source_content_hash``, while gate1's live entity-scoped lane plans a ``Template`` — the
    V1 recipe registry, which has no revision hash at all — and passes ``""`` rather than inventing
    one. One function, two lanes, no second copy of the two-source logic.

    **RULING (S1B-3, on review): the two sources are NOT deduplicated against each other, and a
    rejection that carries both files both rows.** They answer different questions and land in
    different queues: an unmet HOP says *the frontier could not cross here at all* (bridge_demand —
    somebody must sanction a crossing), while a contract-level realization gap says *the crossing
    is sanctioned and has no executable realization* (realization_gap — somebody must build or
    attach one). Collapsing them would hide one of two genuinely different pieces of work behind
    whichever happened to be filed first, and the headline ``reason`` cannot decide between them:
    ``_rejection``'s precedence prefers the SELECTED plan's contract refusal, so a run can carry a
    contract headline while other candidates dead-ended carrying real hops. Pinned by
    ``test_a_rejection_carrying_both_sources_files_into_both_queues``.
    """
    anchor = str(rejection.get("anchor_catalog_source") or "")
    hops = [{**hop, "recipe_revision_hash": recipe_revision_hash, "anchor_catalog_source": anchor}
            for hop in (rejection.get("unmet_hops") or ())]
    states = _realization_states(conn, rejection, cache=realization_cache)
    return [*hops, *contract_level_demands(rejection,
                                           recipe_revision_hash=recipe_revision_hash,
                                           realization_states=states)]


#: A governed bridge segment carries no realization revision. Two very different reasons, two very
#: different work items, and ``bridge_realization_revision is None`` alone cannot tell them apart:
#: the planner never calls ``assembly.attach_executable_bridge_realizations`` (it has zero callers),
#: so EVERY segment reads None today — including one whose realization already exists and is
#: executable. Filing both under one state would put "build a realization" and "wire up the
#: realization you already have" in the same queue row, addressed to the wrong person half the time.
REALIZATION_ATTACHED = "attached"
REALIZATION_EXISTS_UNATTACHED = "revision_exists_unattached"
REALIZATION_NONE = "no_revision_exists"
#: Not a state of the bridge — a state of the QUESTION. A revalidation fault must never read as
#: "nothing exists" (that would file a build ticket for work that may already be done).
REALIZATION_UNKNOWN = "realization_lookup_failed"

#: The reader's fixed question: is a CURRENT, fully revalidated realization executable right now?
#: Same purpose/environment the analysis tree asks with (``context_graph``), so telemetry and the
#: dossier cannot disagree about whether a bridge is executable.
_REALIZATION_PURPOSE = "analysis"
_REALIZATION_ENVIRONMENT = "production"


def _realization_states(conn: DbConn, rejection: dict, *,
                        cache: dict[str, str] | None = None) -> dict[str, str]:
    """``bridge_fact_key -> realization state`` for every governed-bridge segment in the evidence.

    One store read per DISTINCT bridge fact key per item (memoized through ``cache``), inside its
    own savepoint: ``executable_bridge_realizations`` revalidates, and a revalidation fault would
    otherwise abort the item's whole transaction — the ``context_graph`` idiom, for the same
    reason."""
    from featuregen.overlay.upload.bridge_store import executable_bridge_realizations

    states = cache if cache is not None else {}
    for segment in rejection.get("evidence") or ():
        if segment.get("segment_kind") != "governed_bridge":
            continue
        key = str(segment.get("bridge_fact_key") or "")
        if segment.get("has_realization_revision"):
            states[key] = REALIZATION_ATTACHED
            continue
        if key in states or not key:
            continue
        try:
            with conn.transaction():
                found = executable_bridge_realizations(
                    conn, purpose=_REALIZATION_PURPOSE, environment=_REALIZATION_ENVIRONMENT,
                    bridge_fact_key=key)
        except Exception:       # noqa: BLE001 — a fault must not read as "nothing exists"
            logger.warning("governed telemetry: realization lookup failed for bridge %s", key,
                           exc_info=True)
            states[key] = REALIZATION_UNKNOWN
            continue
        states[key] = REALIZATION_EXISTS_UNATTACHED if found else REALIZATION_NONE
    return states


def contract_level_demands(rejection: dict, *, recipe_revision_hash: str,
                           realization_states: dict[str, str] | None = None) -> list[dict]:
    """The SECOND demand source: refusals that carry no unmet hop at all.

    Two of them exist, and they are different shapes:

    * ``physical_cardinality_unavailable`` — the G3 boundary. The governed bridge IS sanctioned;
      what is missing is an executable realization revision, without which the hop's cardinality is
      unknown and no measure can be costed over it. That is a REALIZATION gap by the store's own
      vocabulary, so it is filed with ``verdict="missing_realization"`` from the best plan's bridge
      segments — near side as the position (where an engineer would build it), far side as the
      advisory endpoint hint, and the bridge fact key in the evidence.

      One demand per UNREALIZED governed-bridge segment. The measured G3 case is a single-bridge
      plan and therefore files exactly one; a multi-bridge plan has more than one crossing that
      genuinely needs work, and naming only the first would under-report a gap while pretending to
      have attributed the refusal to a specific hop it cannot attribute.

      ``realization_states`` (from :func:`_realization_states`) decides WHICH segments file and
      what the row says the work is: a segment whose realization is already ``attached`` files
      nothing (nothing is missing), while ``no_revision_exists`` and ``revision_exists_unattached``
      both file — the state rides in the row so the queue can tell "build one" from "wire up the
      one you have". An absent key means nobody asked the store; it is recorded as such rather
      than assumed.

    * ``bounded_out_max_frontier_states`` — hop-less by construction (its reject is minted from the
      START state, which realized nothing), so it can NEVER arrive through ``unmet_hops``. Filed
      from rejection-level fields only; the store zeroes a capacity row's hop columns by ruling,
      because the demand is the CAPACITY and not whichever hop the frontier was sitting on.

    Anything else returns nothing: a concept mismatch or an additivity conflict is a modelling
    problem, not somebody's missing crossing, and filing it would rank noise into a queue that
    exists to fund bridge work.
    """
    codes = set(rejection.get("reason_codes") or ())
    anchor = str(rejection.get("anchor_catalog_source") or "")
    states = realization_states or {}
    demands: list[dict] = []
    if ReasonCode.physical_cardinality_unavailable.value in codes:
        for segment in rejection.get("evidence") or ():
            if segment.get("segment_kind") != "governed_bridge":
                continue
            state = states.get(str(segment.get("bridge_fact_key") or ""),
                               REALIZATION_UNKNOWN if not segment.get("has_realization_revision")
                               else REALIZATION_ATTACHED)
            if state == REALIZATION_ATTACHED:
                continue        # nothing is missing here; this segment is not somebody's demand
            demands.append(_realization_gap(segment, recipe_revision_hash=recipe_revision_hash,
                                            anchor=anchor, realization_state=state))
    demands.extend({"verdict": code, "anchor_catalog_source": anchor,
                    "recipe_revision_hash": recipe_revision_hash}
                   for code in _REJECTION_LEVEL_CAPACITY if code in codes)
    return demands


def _realization_gap(segment: dict, *, recipe_revision_hash: str, anchor: str,
                     realization_state: str) -> dict:
    """One unrealized governed bridge, in the flat shape ``record_bridge_demand`` consumes.

    The POSITION is the near side — the catalog and table an engineer would build the realization
    in — matching S1B-2's convention for an unmet hop so the two sources land in one queue with one
    meaning. ``hop_index`` is ``-1``: this demand comes from a path that resolved, so there is no
    frontier position to name, and inventing one would forge a distinct demand identity per run."""
    near_ref = str(segment.get("bridge_from_object_ref") or "")
    far_ref = str(segment.get("bridge_to_object_ref") or "")
    far_catalog = str(segment.get("bridge_to_catalog_source") or "")
    return {
        "verdict": ReasonCode.missing_realization.value,
        "recipe_revision_hash": recipe_revision_hash,
        "anchor_catalog_source": anchor,
        "relationship_id": str(segment.get("relationship_id") or ""),
        "relationship_version": str(segment.get("relationship_version") or ""),
        "from_entity": str(segment.get("from_entity") or ""),
        "to_entity": str(segment.get("to_entity") or ""),
        "position_catalog": str(segment.get("bridge_from_catalog_source") or ""),
        "position_table_ref": table_of(near_ref) if near_ref else "",
        "hop_index": -1,
        "near_side_key_refs": [near_ref] if near_ref else [],
        # NOT a realizer in the "somebody else already does this" sense — that is what makes this
        # a demand at all. The entry names the sanctioned crossing and says, in the row itself,
        # what is missing from it.
        "realizers": [{"bridge_fact_key": str(segment.get("bridge_fact_key") or ""),
                       "catalog_source": far_catalog,
                       "from_catalog_source": str(segment.get("bridge_from_catalog_source") or ""),
                       "from_key_ref": near_ref,
                       "to_key_ref": far_ref,
                       "realization_state": realization_state}],
        "to_endpoint_hint": f"{far_catalog}::{far_ref}" if far_catalog and far_ref else "",
    }


# ── the two pure derivations ──────────────────────────────────────────────────────────────────


def _signature(request: FeaturePlanningRequestV1) -> tuple[str, tuple[str, ...]]:
    """The corroboration key: the output grain and the sorted operand concepts.

    ``candidate_assembly.semantic_signature`` is the key the SERVING path merges twins on, and it
    is deliberately NOT reused here: it hashes the binder's per-role SELECTED COLUMN, and this
    worker holds requests rather than bound candidates. Building it with empty bindings would mint
    a different key wearing the same name — a silent divergence from the merge rule it claims to
    follow. This is the honest subset: what is computed, at what grain, over which concepts."""
    return (request.output_grain, tuple(sorted(op.concept for op in request.operands)))


def _corroborating_signatures(restored: Sequence[_Restored]) -> set:
    return {_signature(entry.request) for entry in restored
            if entry.request is not None and entry.request.origin == DEFINITION_ORIGIN_LLM_INTENT}


def _days(text: str) -> int | None:
    match = _WINDOW_MENTION.search(text)
    if match is None:
        return None
    return int(match.group(1)) * _UNIT_DAYS[match.group(2).lower()]


def _param_divergence(request: FeaturePlanningRequestV1, hypothesis: str) -> list[dict]:
    """R21 — did the person's own words imply a window the primary variant does not bind?

    Zero model calls: a closed regex over the redacted hypothesis, normalized to days, against the
    request's own resolved ``window`` parameter. Silent when the hypothesis names no window or the
    request binds none — an absent statement is not a disagreement."""
    implied = _days(hypothesis)
    if implied is None:
        return []
    bound = next((value for name, value in request.parameter_values if name == "window"), None)
    if bound is None:
        return []
    primary = _days(f"{bound}d") if str(bound).isdigit() else _days(str(bound))
    if primary is None or primary == implied:
        return []
    return [{"parameter": "window", "hypothesis_implied": f"{implied}d",
             "primary_value": f"{primary}d"}]


__all__: list[str] = [
    "MAX_REQUESTS_PER_ITEM",
    "PAYLOAD_UNREADABLE",
    "REALIZATION_ATTACHED",
    "REALIZATION_EXISTS_UNATTACHED",
    "REALIZATION_NONE",
    "REALIZATION_UNKNOWN",
    "STALE_REGISTRY",
    "UNRESOLVED_PLAN_CONTENT_HASH",
    "contract_level_demands",
    "demands_for_rejection",
    "run_governed_telemetry_once",
]
