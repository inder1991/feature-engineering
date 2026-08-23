"""S1B-3 — the telemetry outbox's FROZEN INPUTS: what the request path hands the worker.

The request path must not plan. It also must not hand the worker a pointer to "the catalog", because
by the time the worker runs, the catalog has moved: a telemetry replan against a fresher catalog
answers a question nobody asked (*what would this run have produced tomorrow?*) instead of the one
the programme exists to answer (*what did this run's governed planning actually do?*).

So the enqueue freezes the run's INPUTS — the request manifest, the scope's stable shape, the C0
metadata snapshot id, the roles, the target grain and the anchor catalog — and the worker replans
from those and the DB alone. Two properties are structural rather than conventional:

* **The scope material is the AUTHORIZATION shape, never the watermark.**
  :func:`governed_scope_material` deliberately reuses ``planner.scope.resolve_catalog_scope``'s
  inputs vocabulary (the same readable-catalog query, the same two policy versions) and deliberately
  does NOT carry its ``scope_id`` or its ``CatalogStateStampV1`` s. Those re-mint on every ingest, so
  grouping a demand queue on them would shatter one standing gap into one group per ingest. What is
  carried is stable across catalog refreshes and moves only when somebody's ACCESS moves.
* **A recipe-origin request is frozen as a REFERENCE, an intent-origin request as a PAYLOAD.**
  A V2 recipe is in-code and re-derivable, so the manifest freezes its canonical id and the
  ``planning_request_hash`` it had at enqueue; the worker re-derives from the registry and refuses
  to plan when the two disagree (the registry moved under the work item — see
  ``governed_telemetry_worker``). An LLM intent exists only in the run that proposed it: there is no
  registry to re-derive it from, so its whole request rides along as a payload and
  :func:`planning_request_from_frozen` reconstructs it verbatim. Round-tripping is proven by hash:
  the payload's own ``planning_request_hash`` is frozen beside it and re-checked on restore.

Everything here is pure except :func:`governed_scope_material`, which issues exactly ONE read.
"""
from __future__ import annotations

import logging
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from functools import cache
from typing import Any

from featuregen.contracts import DbConn
from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    RequiredOperandV1,
    planning_request_from_recipe,
    planning_request_hash,
)
from featuregen.overlay.upload.planner.contracts import (
    READ_SCOPE_POLICY_VERSION,
    ROLE_RESOLUTION_VERSION,
)
from featuregen.overlay.upload.read_scope import allowed_sensitivities
from featuregen.overlay.upload.recipe_contract_v2 import (
    EligibilitySpecV2,
    FormulaReferenceV2,
    LeakageSpecV2,
    OutputSpecV2,
    TemporalSpecV2,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

logger = logging.getLogger(__name__)

#: Bumped when the frozen-inputs SHAPE changes. A worker that meets a version it does not know
#: fails the item rather than guessing at a payload it cannot read.
FROZEN_INPUTS_VERSION = "1"

#: The nested request specs, by the key they occupy on ``FeaturePlanningRequestV1``. Every one of
#: them is FLAT (str / bool / tuple[str, ...]), which is why the reconstruction below is a closed
#: list rather than a reflective walk: a nested dataclass appearing here later must be added
#: deliberately, and ``test_the_frozen_payload_round_trips_by_hash`` fails until it is.
_SPEC_FIELDS: dict[str, type] = {
    "output": OutputSpecV2,
    "temporal": TemporalSpecV2,
    "eligibility": EligibilitySpecV2,
    "leakage": LeakageSpecV2,
}


# ── the scope's stable shape ───────────────────────────────────────────────────────────────────


def governed_scope_material(conn: DbConn, *, roles: Iterable[str] = (),
                            target_entity: str) -> dict:
    """The scope material 1120's ``catalog_scope_material`` column holds — ONE read.

    ``authorized_catalog_sources`` is the READABLE set under these roles: the identical predicate
    ``resolve_catalog_scope`` opens with (``graph_node`` columns whose ``visible_requires`` is
    contained by the roles' allowed classes). It is deliberately NOT that function's post-watermark
    ``authorized_catalog_sources``, which additionally drops a catalog with no usable state stamp:
    that answer changes with ingest weather, and this column exists to be GROUPED ON across months.
    A catalog that is readable-but-unstamped is in scope for the question "what may this role
    reach?" and its absence from a given run's plan is the run's business, not the scope's.

    The two policy versions are the planner's own constants, so a policy change moves this material
    and a demand queue can tell "nobody could reach it" from "the rules changed".
    """
    rows = conn.execute(
        "SELECT DISTINCT catalog_source FROM graph_node WHERE kind = 'column' "
        "AND visible_requires <@ %s ORDER BY catalog_source",
        (allowed_sensitivities(tuple(roles)),)).fetchall()
    return {
        "authorized_catalog_sources": sorted(row[0] for row in rows),
        "read_scope_policy_version": READ_SCOPE_POLICY_VERSION,
        "role_resolution_version": ROLE_RESOLUTION_VERSION,
        "target_entity": target_entity,
    }


# ── the request manifest ───────────────────────────────────────────────────────────────────────


@cache
def _recipe_request_identity(recipe_id: str) -> tuple[str, str, str] | None:
    """``(origin, planning_request_hash, source_content_hash)`` for one eligible V2 recipe.

    **Cached, because the V2 registry is IN-CODE and immutable within a process.** Projecting and
    field-exhaustively hashing all 317 recipes measured **131 ms of pure CPU on the request path**
    — every generation, for an answer that cannot change until the process restarts. The cache
    makes it a once-per-process cost (measured after: ~0.2 ms warm). It returns an immutable tuple
    rather than the ref dict so a caller cannot mutate a cached value.

    ▲ The WORKER deliberately does NOT use this. Its whole staleness rule is that the registry may
    have moved since the enqueue, so ``_restore_recipe`` re-derives live, uncached, every time.
    """
    definition = v2_recipe_by_id(recipe_id)
    if definition is None:
        return None
    request = planning_request_from_recipe(definition)
    return (request.origin, planning_request_hash(request), request.source_content_hash)


def _recipe_ref(recipe_id: str) -> dict | None:
    """One eligible V2 recipe as a REFERENCE — id, origin and the request hash it had NOW.

    No payload: the definition is in-code, and re-deriving it is what makes the staleness check
    possible at all. An id this build does not carry is skipped with a log line naming it (the same
    honesty ``governed_requests_for_scope`` applies), and a definition that cannot project into a
    planning request is skipped the same way rather than poisoning the whole manifest.
    """
    try:
        identity = _recipe_request_identity(recipe_id)
    except Exception:
        logger.exception("governed telemetry: recipe %s does not project into a planning request",
                         recipe_id)
        return None
    if identity is None:
        logger.info("governed telemetry: eligible id %s is not a V2 recipe; not frozen", recipe_id)
        return None
    origin, request_hash, content_hash = identity
    return {"canonical_definition_id": recipe_id,
            "origin": origin,
            "planning_request_hash": request_hash,
            "source_content_hash": content_hash}


def _intent_ref(request: FeaturePlanningRequestV1) -> dict:
    """One intent-origin request as a REFERENCE **plus** its whole payload.

    The payload is what makes the worker LLM-free: an intent that a provider proposed during the
    request is replanned from the bytes this run saw, never from a second dispatch.
    """
    return {"canonical_definition_id": request.source_definition_id,
            "origin": request.origin,
            "planning_request_hash": planning_request_hash(request),
            "source_content_hash": request.source_content_hash,
            "payload": asdict(request)}


def frozen_telemetry_inputs(*, v2_eligible_ids: Iterable[str],
                            intent_requests: Sequence[FeaturePlanningRequestV1] = (),
                            roles: Iterable[str] = (), target_entity: str,
                            anchor_catalog_source: str | None,
                            scope_material: dict,
                            metadata_snapshot_id: str | None,
                            redacted_hypothesis: str = "") -> dict:
    """The whole frozen input set, JSON-native, deterministic in order.

    ``redacted_hypothesis`` rides along because R21's parameter-divergence check is a pure function
    of it and the request's own window parameter: the worker must be able to ask "did the person
    say 30 days while the primary variant binds 90?" without re-reading an intent row that may have
    been superseded, and without ever calling a model to do the reading.
    """
    intent_refs = [_intent_ref(request) for request in intent_requests]
    recipe_refs = [ref for ref in
                   (_recipe_ref(recipe_id) for recipe_id in sorted(set(v2_eligible_ids)))
                   if ref is not None]
    return {
        "frozen_inputs_version": FROZEN_INPUTS_VERSION,
        "target_entity": target_entity,
        "roles": sorted(str(role) for role in roles),
        "anchor_catalog_source": anchor_catalog_source or "",
        "metadata_snapshot_id": metadata_snapshot_id,
        "redacted_hypothesis": redacted_hypothesis,
        "catalog_scope_material": dict(scope_material),
        # Intents FIRST, then recipes by canonical id: the worker's cap bites at a deterministic
        # place, and the run's own proposals are never the half that gets dropped.
        "requests": [*intent_refs, *recipe_refs],
    }


# ── restoring a request from its frozen payload ────────────────────────────────────────────────


def _tuples(value: Any) -> Any:
    return tuple(value) if isinstance(value, list) else value


def _spec(spec_type: type, payload: Any):
    """One flat nested spec, rebuilt. JSON has no tuples, so every list becomes one back."""
    if payload is None:
        return None
    return spec_type(**{key: _tuples(value) for key, value in payload.items()})


def planning_request_from_frozen(payload: dict) -> FeaturePlanningRequestV1:
    """Reconstruct a request from the payload :func:`_intent_ref` froze — no registry, no clock.

    Construction runs the SAME ``__post_init__`` validation the original passed, so a payload that
    has been tampered with (an origin outside the closed set, a role duplicated, a physical
    binding hint on a non-user origin) is refused HERE rather than planned. The worker then
    re-checks the rebuilt request's ``planning_request_hash`` against the frozen one, so a round
    trip that is merely LOSSY (rather than invalid) is recorded, not planned.

    ``parameter_values`` are rebuilt as ``(name, value)`` with the value passed through unchanged —
    JSON has no tuples, so a NON-SCALAR value would come back as a list and move the hash. That is
    structurally impossible for both origins this freeze carries, and
    ``test_every_v2_parameter_value_is_a_scalar`` pins the half that could drift: every authored
    ``allowed_values`` entry in the V2 registry is a scalar, and an ``llm_intent`` request built by
    ``planning_request_from_feature_intent`` carries no parameters at all.
    """
    fields = dict(payload)
    operands = tuple(RequiredOperandV1(**{key: _tuples(value) for key, value in operand.items()})
                     for operand in fields.pop("operands", ()))
    parameter_values = tuple((str(name), value)
                             for name, value in fields.pop("parameter_values", ()))
    specs = {key: _spec(spec_type, fields.pop(key, None))
             for key, spec_type in _SPEC_FIELDS.items()}
    specs = {key: value for key, value in specs.items() if value is not None}
    formula_payload = fields.pop("formula", None)
    rest = {key: _tuples(value) for key, value in fields.items()}
    return FeaturePlanningRequestV1(
        operands=operands, parameter_values=parameter_values,
        formula=_spec(FormulaReferenceV2, formula_payload), **specs, **rest)


__all__ = [
    "FROZEN_INPUTS_VERSION",
    "frozen_telemetry_inputs",
    "governed_scope_material",
    "planning_request_from_frozen",
]
