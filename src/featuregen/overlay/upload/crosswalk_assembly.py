"""The ONE ``src/`` path that assembles a STORED crosswalk into :class:`AdmittedCrosswalkV1`.

Release C Task 13, closing DEFERRED-WORK A.42's fourth row. Task 12 proved a governed two-leg
traversal CAN be planned, authorized, rendered and sealed — but every ``AdmittedCrosswalkV1`` in the
repository came out of a fixture bank, so nothing in production assembled a definition + execution +
decision + row-selection + observation + three bindings into one. This module is that assembler, and
it reads the stores Tasks 10-11 wrote rather than inventing a sixth place to keep a crosswalk.

**THERE IS NO EXECUTION STORE AND NO DECISION STORE, AND THERE MUST NOT BE.** Task 11 persists
exactly two things — the definition revision (migration 1050) and the composed observation
(migration 1057) — and the execution revision and the admission decision are DERIVED from them by
pure functions. Persisting a verdict would let a stored answer outlive the evidence it was read
from, which is precisely the failure Definition-of-Done 17 names: *production execution depends on
current deterministic, direction-specific evidence — not review*. So this module re-derives both,
every time, from what is current NOW.

**REVIEW STATUS IS NEVER READ HERE, and that is a functional requirement rather than an omission.**
:func:`~featuregen.overlay.upload.crosswalk_store.current_crosswalk_definition` returns the review
status beside the revision and this module ignores it, because:

* DoD 16 — an LLM-only crosswalk candidate must stay discoverable and SANDBOX-PLANNABLE without any
  human approval, so consulting review here would make a reviewer's absence a capability wall; and
* DoD 17 — production admissibility is the measured, direction-specific verdict, so consulting
  review here would let an approval stand in for a measurement.

Both directions of that rule are tested (``test_crosswalk_assembly``), and the mutation
``review_substitutes_for_crosswalk_safety`` exists to kill an implementation that forgets it.

**Fan-out, duplicates and overlap are REFUSED, never repaired.** ``active_mapping_count`` is counted
from the store here and handed to ``evaluate_crosswalk_admission`` unmodified: two ACTIVE mapping
definitions for one endpoint pair mean the answer depends on which row a compiler happened to read,
and no ``DISTINCT``, ``LIMIT 1`` or newest-wins tie-break is permitted to hide that.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum

from featuregen.contracts import DbConn
from featuregen.data_agent.physical import (
    PhysicalDatasetBindingV1,
    PhysicalObjectIdentityV1,
)
from featuregen.overlay.upload.bridge_realization import (
    ExecutionTier,
    RealizationApplicabilityScopeV1,
    eligible_for_production,
    eligible_for_sandbox,
)
from featuregen.overlay.upload.crosswalk import (
    CrosswalkDefinitionRevisionV1,
    JoinLegPinV1,
)
from featuregen.overlay.upload.crosswalk_admission import (
    AdmittedCrosswalkV1,
    CrosswalkAdmissionDecisionV1,
    CrosswalkAdmissionPolicyV1,
    admitted_crosswalk_execution,
    evaluate_crosswalk_admission,
)
from featuregen.overlay.upload.crosswalk_observation import (
    CrosswalkExecutionObservationV1,
    observation_scope_is_proved,
    observation_scope_matches,
)
from featuregen.overlay.upload.crosswalk_observation_store import (
    current_crosswalk_observations_for_revision,
)
from featuregen.overlay.upload.crosswalk_store import (
    CurrentCrosswalkV1,
    current_crosswalk_definition,
)
from featuregen.overlay.upload.temporal_policy import DatasetRowSelectionV1

__all__ = [
    "CrosswalkAssemblyRefusalV1",
    "CrosswalkAssemblyReason",
    "assemble_admitted_crosswalk",
    "active_mapping_count",
    "load_binding_revision",
    "pinned_mapping_temporal_policy",
    "production_admissible",
    "sandbox_plannable",
    "unreviewed_reason_codes",
]


class CrosswalkAssemblyReason(StrEnum):
    """CLOSED vocabulary. A caller branches on these, never on message text."""

    DEFINITION_NOT_FOUND = "crosswalk_definition_not_found"
    #: A pin names a binding revision no store holds. The pin is then a claim about a table nobody
    #: recorded an address for, and the bundle's own binding cross-checks could not run.
    BINDING_REVISION_NOT_STORED = "crosswalk_binding_revision_not_stored"
    #: The two legs disagree about which table the mapping dataset is. Structural, and caught before
    #: admission so the message names the disagreement rather than a downstream symptom.
    LEG_PINS_DISAGREE_ON_MAPPING = "crosswalk_leg_pins_disagree_on_mapping"
    #: The pinned mapping binding is not the one the composed measurement was taken against.
    MEASUREMENT_MAPPING_BINDING_MISMATCH = "crosswalk_measurement_mapping_binding_mismatch"
    #: Construction of the bundle raised — every cross-check in ``AdmittedCrosswalkV1.__post_init__``
    #: surfaces here rather than as an exception escaping a read path.
    BUNDLE_INCONSISTENT = "crosswalk_bundle_inconsistent"
    #: The execution's pinned mapping row rule is not the one the measurement was taken under. A
    #: DERIVATION guard — see :func:`pinned_mapping_temporal_policy`.
    MEASUREMENT_TEMPORAL_POLICY_MISMATCH = "crosswalk_measurement_temporal_policy_mismatch"
    #: A PRODUCTION-tier scope resolved a measurement that cannot prove which scope it ran under.
    #: Not a statement about the crosswalk: the numbers may be perfect and the tier is unknowable.
    OBSERVATION_SCOPE_IDENTITY_UNPROVED = "crosswalk_observation_scope_identity_unproved"
    #: Two CURRENT measurements answer to one scope. ``refuse_on_multiple``, applied to evidence:
    #: picking the newer one would make the verdict depend on which row a reader happened to sort
    #: first, which is the same defect ``active_mapping_count`` refuses for mapping definitions.
    MULTIPLE_OBSERVATIONS_FOR_SCOPE = "crosswalk_multiple_observations_for_scope"


@dataclass(frozen=True, slots=True)
class CrosswalkAssemblyRefusalV1:
    """Why one stored crosswalk could not be assembled. A VALUE, never an exception.

    An assembly refusal is not a failure of the crosswalk — it is a failure to build the bundle,
    which is a different sentence and belongs in a different place in the UI. "Nobody measured this
    yet" is NOT one of these: an unmeasured crosswalk assembles perfectly well and comes back
    ``UNASSESSED``, which is sandbox-admissible.
    """

    code: CrosswalkAssemblyReason
    detail: str
    definition_id: str


# ── stored reads ────────────────────────────────────────────────────────────────────────────────

def load_binding_revision(
    conn: DbConn, binding_revision_id: str,
) -> PhysicalDatasetBindingV1 | None:
    """One physical binding, by the revision id a pin names, CONTENT-VERIFIED on the way out.

    ``binding_json`` stores ``content_payload()``, which deliberately excludes ``binding_id`` (it
    names the durable stream, not one revision of it), so the id comes from its own column and the
    reconstructed object is then required to reproduce the revision id that was asked for. Without
    that last step a row whose ``binding_id`` column and ``binding_json`` had drifted apart would
    hand back a binding addressing one table under another table's revision id — which is exactly
    the substitution the bundle's binding cross-checks exist to catch, arriving pre-laundered.
    """
    row = conn.execute(
        "SELECT binding_id, binding_json FROM physical_dataset_binding_revision "
        "WHERE binding_revision_id = %s", (binding_revision_id,)).fetchone()
    if row is None:
        return None
    binding_id, payload = row
    binding = PhysicalDatasetBindingV1(
        binding_id=binding_id,
        catalog_logical_ref=payload["catalog_logical_ref"],
        connection_id=payload["connection_id"],
        identity=PhysicalObjectIdentityV1(**payload["physical_identity"]),
        partition_columns=tuple(payload.get("partition_columns") or ()),
        business_time_column=payload.get("business_time_column"),
        purposes=tuple(payload.get("purposes") or ()),
    )
    if binding.binding_revision_id != binding_revision_id:
        return None
    return binding


def active_mapping_count(
    conn: DbConn, definition: CrosswalkDefinitionRevisionV1,
) -> int:
    """How many CURRENT crosswalk definitions relate THIS endpoint pair, however they map it.

    §6.6's ``refuse_on_multiple`` is enforced by counting honestly and refusing downstream — not by
    picking one. Both orderings of the pair are counted because a crosswalk describes a
    RELATIONSHIP: the same two endpoints declared the other way round is the same pair, and
    counting only one direction would make the conflict disappear by re-spelling it.

    A count of 0 is impossible for a definition read through the CURRENT pointer and is therefore
    returned as 1 rather than repaired silently — the caller's own definition is one of the rows,
    and if it is not, the SQL is wrong and admission must not be handed a number that says "no
    conflict" for the wrong reason.
    """
    source_ref = definition.source_endpoint.logical_table_ref
    target_ref = definition.target_endpoint.logical_table_ref
    row = conn.execute(
        "SELECT count(*) FROM crosswalk_definition_current c "
        "JOIN crosswalk_definition_revision r ON r.revision_id = c.revision_id "
        "WHERE (r.source_endpoint_ref = %s AND r.target_endpoint_ref = %s) "
        "   OR (r.source_endpoint_ref = %s AND r.target_endpoint_ref = %s)",
        (source_ref, target_ref, target_ref, source_ref)).fetchone()
    counted = int(row[0]) if row else 0
    return max(counted, 1)


def _observations_for_scope(
    conn: DbConn, revision_id: str, scope: RealizationApplicabilityScopeV1,
) -> tuple[CrosswalkExecutionObservationV1, ...]:
    """EVERY current composed measurement whose recorded scope names this one. Usually 0 or 1.

    **Scope-exact only up to what migration 1057 persists, which is the scope's NAME.** The earlier
    wording here claimed the pick was "scope-exact by construction"; it is not, and the difference
    matters: the stored row carries ``scope_id`` and no tier, no environment and no purposes, so a
    SANDBOX probe and a PRODUCTION run that were given the same ``scope_id`` string resolve to the
    same row. ``crosswalk_observation.scope_binding_id`` is how a measurement makes its scope
    provable, :func:`observation_scope_is_proved` is how this module asks, and
    :meth:`CrosswalkAssemblyReason.OBSERVATION_SCOPE_IDENTITY_UNPROVED` is what production does
    about an answer it cannot prove.

    What IS true is that the newest measurement across DIFFERENT scopes is never picked:
    :func:`current_crosswalk_observations_for_revision` returns one row per measured scope, and this
    filters rather than sorts. ALL matches are returned — the caller refuses on more than one rather
    than taking the first, because two current measurements of one scope make the verdict depend on
    a sort order (§6.6 ``refuse_on_multiple``, the discipline ``active_mapping_count`` already keeps
    for rival mapping definitions).
    """
    return tuple(
        observation
        for observation in current_crosswalk_observations_for_revision(conn, revision_id)
        if observation_scope_matches(observation.scope_id, scope))


def pinned_mapping_temporal_policy(
    definition: CrosswalkDefinitionRevisionV1,
    observation: CrosswalkExecutionObservationV1 | None,
) -> str | None:
    """WHICH mapping row rule an assembled execution pins — from the MEASUREMENT when there is one.

    **Never from the caller's row selection**, which is what shipped and which made the drift
    refusal unreachable. ``AdmittedCrosswalkV1.__post_init__`` compares
    ``mapping_row_selection.temporal_policy_revision_id`` against this pin and refuses when the
    "policy pointer moved after the crosswalk was measured"; deriving the pin FROM the selection
    made the two sides of that comparison one value, so a caller who resolved rows under a policy
    that had since advanced simply carried the pin along with it and the bundle assembled clean.
    Reviewer's probe: measured under ``dtp_999``, assembled with a selection resolved under
    ``dtp_777`` — production-admissible, with no reason code anywhere.

    The measurement is the right source because the pin's whole job is to name the row set the
    uniqueness verdict was proved over. An UNMEASURED crosswalk has no such row set, so it falls
    back to the definition's own declaration — which is what the crosswalk CLAIMS its rows are, and
    the only governed answer available before anybody measures.
    """
    if observation is None:
        return definition.mapping_temporal_policy_revision_id
    return observation.mapping_temporal_policy_revision_id


def _legs_currently_executable(conn: DbConn, pins: Sequence[JoinLegPinV1], *,
                               require_production: bool) -> bool:
    """Does every realization these pins name still stand?

    A leg that resolved through a governed bridge realization pins that realization's revision id;
    a same-catalog leg pins none (``JoinLegPinV1`` refuses the pretence that every leg is an entity
    bridge) and so has nothing to revalidate here — its staleness is caught by the row-selection and
    observation pins the step carries instead.

    Fail-CLOSED on a pinned realization that is absent from the current pointers: a pin naming a
    realization the store no longer serves is a stale pin, and treating "not found" as "fine"
    would carry a withdrawn link's authority into a fresh compilation.
    """
    from featuregen.overlay.upload.bridge_store import load_current_bridge_realizations

    wanted = {pin_id for pin in pins for pin_id in pin.realization_revision_ids}
    if not wanted:
        return True
    predicate = eligible_for_production if require_production else eligible_for_sandbox
    live = {
        item.revision.realization_revision_id: item
        for item in load_current_bridge_realizations(conn)
    }
    for revision_id in wanted:
        item = live.get(revision_id)
        if item is None or not predicate(item.revision, item.current):
            return False
    return True


# ── the assembler ───────────────────────────────────────────────────────────────────────────────

def assemble_admitted_crosswalk(
    conn: DbConn,
    *,
    definition_id: str,
    scope: RealizationApplicabilityScopeV1,
    now: datetime,
    source_leg: JoinLegPinV1,
    target_leg: JoinLegPinV1,
    mapping_row_selection: DatasetRowSelectionV1 | None = None,
    policy: CrosswalkAdmissionPolicyV1 | None = None,
    current: CurrentCrosswalkV1 | None = None,
) -> AdmittedCrosswalkV1 | CrosswalkAssemblyRefusalV1:
    """Assemble ONE stored crosswalk into the bundle a compilation may plan a traversal on.

    Everything that CAN be read is read: the definition through its CURRENT pointer, the composed
    measurement recorded against this scope, all three physical bindings by the revision ids the
    pins name, the number of active mappings for the endpoint pair, and whether each pinned
    realization still stands. The admission decision and the execution revision are then RE-DERIVED
    — see the module docstring for why neither may be stored.

    **What "against this scope" is worth, exactly.** Migration 1057 persists a measurement's
    ``scope_id`` and nothing else about the scope it ran under, so a name match is a NAME match. A
    production assembly therefore additionally requires the recorded id to be scope-BOUND
    (``crosswalk_observation.scope_binding_id``) and refuses an unprovable one outright; a sandbox
    assembly accepts a bare name, because reading a probe's own measurement at the probe tier is the
    question that was asked. Two current measurements answering to one scope refuse rather than
    resolve by recency.

    The mapping row rule this execution pins comes from the MEASUREMENT
    (:func:`pinned_mapping_temporal_policy`) and never from ``mapping_row_selection`` — which is the
    object ``AdmittedCrosswalkV1.__post_init__`` then validates against the pin, and which therefore
    cannot also be its source without making that refusal unreachable.

    The two leg pins are passed IN, and that is the same seam ``crosswalk_admission`` draws for
    ``leg_pin_from_join_plan``: pinning a same-catalog leg requires a real ``JoinPlan``, and planning
    one here would put ``materialize.joins`` — which imports this package's admission module —
    inside a governance read path. What this function guarantees about them instead is that the
    bindings they name are STORED, that they agree with each other about which table the mapping is,
    and that they agree with the measurement.

    ``current`` lets a caller that already resolved the pointer (a listing rendering many crosswalks)
    hand it in rather than re-reading; it is verified to be the same definition, never trusted to
    name a different one.
    """
    if current is None:
        current = current_crosswalk_definition(conn, definition_id)
    if current is None or current.current.definition_id != definition_id:
        return CrosswalkAssemblyRefusalV1(
            code=CrosswalkAssemblyReason.DEFINITION_NOT_FOUND,
            detail=f"no current crosswalk definition {definition_id!r}",
            definition_id=definition_id)
    definition = current.revision

    # The mapping dataset is named by THREE pins, and a crosswalk whose two legs travelled through
    # different mapping tables is not a crosswalk. Checked here — before admission — so the message
    # names the disagreement instead of a downstream binding mismatch.
    if source_leg.to_binding_revision_id != target_leg.from_binding_revision_id:
        return CrosswalkAssemblyRefusalV1(
            code=CrosswalkAssemblyReason.LEG_PINS_DISAGREE_ON_MAPPING,
            detail=(
                f"the source leg arrives at binding {source_leg.to_binding_revision_id} and the "
                f"target leg departs from {target_leg.from_binding_revision_id}: two legs through "
                "two different mapping tables are two relationships, not one crosswalk"),
            definition_id=definition_id)
    mapping_binding_revision_id = source_leg.to_binding_revision_id

    # PRODUCTION eligibility is asked of the pinned realizations whenever the scope is a production
    # one. A sandbox scope asks the sandbox predicate — an UNASSESSED realization is usable as
    # sandbox data and refusing it here would make the sandbox tier stricter than production
    # admission, which is not a safety property, only a broken ladder.
    require_production = scope.execution_tier is ExecutionTier.PRODUCTION

    matched = _observations_for_scope(conn, definition.revision_id, scope)
    if len(matched) > 1:
        return CrosswalkAssemblyRefusalV1(
            code=CrosswalkAssemblyReason.MULTIPLE_OBSERVATIONS_FOR_SCOPE,
            detail=(
                f"{len(matched)} current composed measurements answer to scope "
                f"{scope.scope_id!r} ("
                + ", ".join(sorted(item.observation_revision_id for item in matched))
                + "): which verdict this crosswalk carries would depend on which row was read "
                "first, and §6.6 refuses on multiple rather than picking one"),
            definition_id=definition_id)
    observation = matched[0] if matched else None

    # A PRODUCTION question answered by evidence that cannot say which scope it came from. Migration
    # 1057 persists the scope's NAME and not its tier, so "recorded under a sandbox probe called
    # `probe-scope`" and "recorded under a production scope called `probe-scope`" are the same row —
    # and the review's probe walked straight through it. Refused rather than verdicted, because the
    # crosswalk is not what is wrong: nobody can tell what this measurement measured.
    if (observation is not None and require_production
            and not observation_scope_is_proved(observation.scope_id, scope)):
        return CrosswalkAssemblyRefusalV1(
            code=CrosswalkAssemblyReason.OBSERVATION_SCOPE_IDENTITY_UNPROVED,
            detail=(
                f"the composed measurement {observation.observation_revision_id} records scope "
                f"{observation.scope_id!r}, which names this scope but does not prove it: the "
                "execution tier, the environment and the purposes of the scope it ran under were "
                "never persisted, so a SANDBOX probe of the same name is indistinguishable from a "
                "production run. Re-measure with a scope-bound id "
                "(`crosswalk_observation.scope_binding_id`) before reading this as production "
                "evidence"),
            definition_id=definition_id)

    if (observation is not None
            and observation.mapping_binding_revision_id != mapping_binding_revision_id):
        # Admission would ALSO catch this (MAPPING_BINDING_MISMATCH) and return an UNSAFE verdict.
        # It is caught here as well, and as a refusal rather than a verdict, because the two say
        # different things: a verdict is "this crosswalk is unsafe", and the truth is "these inputs
        # do not describe one crosswalk". Only the second tells an operator what to fix.
        return CrosswalkAssemblyRefusalV1(
            code=CrosswalkAssemblyReason.MEASUREMENT_MAPPING_BINDING_MISMATCH,
            detail=(
                f"the composed measurement {observation.observation_revision_id} was taken against "
                f"mapping binding {observation.mapping_binding_revision_id} and the legs pin "
                f"{mapping_binding_revision_id}: every verdict it carries describes another table"),
            definition_id=definition_id)

    bindings: dict[str, PhysicalDatasetBindingV1] = {}
    for slot, revision_id in (
            ("source", source_leg.from_binding_revision_id),
            ("target", target_leg.to_binding_revision_id),
            ("mapping", mapping_binding_revision_id)):
        binding = load_binding_revision(conn, revision_id)
        if binding is None:
            return CrosswalkAssemblyRefusalV1(
                code=CrosswalkAssemblyReason.BINDING_REVISION_NOT_STORED,
                detail=(
                    f"the {slot} leg pins physical binding revision {revision_id}, which no store "
                    "holds: the address this traversal would read cannot be compared with the "
                    "address the measurement was taken against"),
                definition_id=definition_id)
        bindings[slot] = binding

    decision = evaluate_crosswalk_admission(
        crosswalk_definition_revision_id=definition.revision_id,
        scope=scope,
        policy=policy or CrosswalkAdmissionPolicyV1(),
        now=now,
        observation=observation,
        active_mapping_count=active_mapping_count(conn, definition),
        definition_revision_current=True,
        legs_currently_executable=_legs_currently_executable(
            conn, (source_leg, target_leg), require_production=require_production),
        mapping_binding_revision_id=mapping_binding_revision_id,
    )
    pinned_policy = pinned_mapping_temporal_policy(definition, observation)
    # THE DERIVATION GUARD. Unreachable while the line above is right, and that is exactly what it
    # is for: the shipped bug was a pin derived from `mapping_row_selection`, and the only thing
    # `__post_init__`'s drift refusal needs in order to be real is that this pin comes from
    # somewhere the caller does not control. Anything that re-points the derivation at the caller
    # (the mutation `crosswalk_policy_pin_follows_the_caller`) makes these two disagree and lands
    # here with a message naming the two policies, instead of assembling a clean bundle whose
    # verdict describes another row set.
    if observation is not None and (
            observation.mapping_temporal_policy_revision_id != pinned_policy):
        return CrosswalkAssemblyRefusalV1(
            code=CrosswalkAssemblyReason.MEASUREMENT_TEMPORAL_POLICY_MISMATCH,
            detail=(
                f"the composed measurement {observation.observation_revision_id} was taken under "
                f"mapping row rule {observation.mapping_temporal_policy_revision_id!r} and this "
                f"execution would pin {pinned_policy!r}: uniqueness proved over one row set says "
                "nothing about the other"),
            definition_id=definition_id)
    execution = admitted_crosswalk_execution(
        decision,
        source_leg=source_leg,
        target_leg=target_leg,
        mapping_binding_revision_id=mapping_binding_revision_id,
        scope=scope,
        observation=observation,
        mapping_temporal_policy_revision_id=pinned_policy,
    )
    try:
        return AdmittedCrosswalkV1(
            definition=definition,
            execution=execution,
            decision=decision,
            source_binding=bindings["source"],
            target_binding=bindings["target"],
            mapping_binding=bindings["mapping"],
            mapping_row_selection=mapping_row_selection,
            observation=observation)
    except ValueError as exc:
        # `__post_init__`'s cross-checks now run on REAL rows, which is the whole point of this
        # module — and a read surface must not turn one inconsistent crosswalk into a 500.
        return CrosswalkAssemblyRefusalV1(
            code=CrosswalkAssemblyReason.BUNDLE_INCONSISTENT,
            detail=str(exc),
            definition_id=definition_id)


def sandbox_plannable(
    admitted: AdmittedCrosswalkV1, *, direction: str,
) -> bool:
    """May the SANDBOX tier plan this direction — with no human approval anywhere in the answer?

    DoD 16, as one readable predicate. It is the direction's own ``sandbox_admissible`` and nothing
    else: not the review status, not the display tier, not whether the other direction is safe. An
    UNMEASURED crosswalk answers True, because "discoverable, unmeasured" is a state a sandbox plan
    may proceed under (Task 12's generated project carries an exact runtime gate that catches at
    execution what measurement has not yet answered) — and calling it a failure is the thing the
    no-blocked rule forbids.
    """
    return admitted.verdict_for(direction).sandbox_admissible


def production_admissible(
    admitted: AdmittedCrosswalkV1, *, direction: str,
) -> bool:
    """May PRODUCTION execute this direction, on current deterministic evidence alone?

    DoD 17. The scope's own tier is part of the answer: a measurement taken under a SANDBOX
    applicability scope proves nothing about production data, so a sandbox-scoped execution is not
    production-admissible however clean its numbers are — the same refusal
    ``joins.plan_crosswalk_join`` makes, stated here so a display surface reaches it without
    compiling anything.
    """
    if admitted.execution.applicability_scope.execution_tier is not ExecutionTier.PRODUCTION:
        return False
    return admitted.verdict_for(direction).production_admissible


def unreviewed_reason_codes(decision: CrosswalkAdmissionDecisionV1) -> tuple[str, ...]:
    """Every reason code either direction recorded, deduplicated and sorted.

    For display surfaces that need "why is this not production-admissible" without picking a
    direction — never for a gate, which must ask ONE direction (:meth:`AdmittedCrosswalkV1.
    verdict_for`), because a code earned by the reverse direction says nothing about the forward one.
    """
    return tuple(sorted({
        *decision.reason_codes, *decision.forward.reason_codes, *decision.reverse.reason_codes}))
