"""CapabilityAssessmentV1 — a PROJECTION of the one authority's verdicts into display states.

**This module never computes a verdict.** It consumes the canonical six-action service's answers
(``ask``/``decide`` results — ``ActionDecisionV1``) plus server-derived DISCOVERY facts (which R1
subject resources exist yet), and maps them onto what a card renders. It imports neither the
disposition table nor the fold; a projected capability that contradicted ``ask()`` would be a
second decision authority wearing a renderer's name — the property test proves projection equals
service over the whole reason vocabulary.

Three render modes, plus the two pinned copy lines the 2026-08-24 plan requires:

* ``EXACT`` — the action is allowed and nothing provisional rides it.
* ``PROVISIONAL`` — allowed, with guard-compiled warnings (unknown cardinality, unverified
  transaction identity …): the owner's matrix's "Provisional (guards compiled in)" row. The
  artifact renders; its results carry guards that may refuse at run time.
* ``UNAVAILABLE`` — the service refused (the decision's blockers are served verbatim), OR the
  action's subject resource does not exist yet (pre-resource, below).

**Pre-resource actions** (R1): an action whose subject does not exist yet — no build-set revision
minted, no artifact sealed — has NOTHING to ask the service about, and inventing a verdict is
exactly what this module must never do. Such a card renders ``UNAVAILABLE`` with
``pre_resource=True`` and the pinned copy :data:`PRE_RESOURCE_COPY`, and NO blocker codes (an
absent resource is not a refusal).

**Production actions** render the governance-not-released copy while the service reports them
unavailable (``ACTION_UNAVAILABLE`` / the ``*_NOT_RELEASED`` codes): production is a release
gate, not a missing certificate, and the copy says so in the platform's own words.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from featuregen.materialize.action_authorization import ActionV1
from featuregen.materialize.action_decision import ActionDecisionV1
from featuregen.overlay.upload import semantic_eligibility_reasons as R

__all__ = [
    "ActionCapabilityV1",
    "CapabilityAssessmentV1",
    "CapabilityProjectionDefect",
    "CapabilityRenderMode",
    "CapabilityResourceFactsV1",
    "GUARD_RENDERED_WARNINGS",
    "LADDER_RUNG_VIEW",
    "PRE_RESOURCE_COPY",
    "PRODUCTION_NOT_RELEASED_COPY",
    "project_capabilities",
]


class CapabilityRenderMode(StrEnum):
    EXACT = "exact"
    PROVISIONAL = "provisional"
    UNAVAILABLE = "unavailable"


#: R1's pre-resource card copy, verbatim from the plan. Pinned by test — UI copy that drifts
#: silently is a product decision nobody made.
PRE_RESOURCE_COPY = "potentially available after selection"

#: The governance-not-released copy for the production actions (§21.0's posture: a release gate,
#: not a switch). Pinned by test.
PRODUCTION_NOT_RELEASED_COPY = (
    "Not released: production materialization and publication open when production governance "
    "exists — a release gate, not a switch")

#: Warnings whose presence means the artifact renders WITH GUARDS COMPILED IN — the owner's
#: matrix's "Provisional" rows (unknown cardinality with a pinned guard policy; unverified or
#: violated data checks whose sandbox story is "the guard is what the run produces evidence
#: against"). A warning outside this set surfaces on the card but the render stays EXACT.
GUARD_RENDERED_WARNINGS: frozenset[str] = frozenset({
    R.DIRECTIONAL_CARDINALITY_UNPROVEN,
    R.TRANSACTION_IDENTITY_NOT_UNIQUE,
    R.IDENTIFIER_UNIQUENESS,
    R.EVENT_HISTORY_VERIFICATION,
})

#: The blocker codes that mean "the platform has not released this capability" — the copy
#: selector for the production rows (and the honest sandbox stand-in ACTION_UNAVAILABLE, which
#: `ask` emits for the two production acts today).
_NOT_RELEASED_CODES: frozenset[str] = frozenset({
    "ACTION_UNAVAILABLE",
    R.SANDBOX_EXECUTION_NOT_RELEASED,
    R.SANDBOX_PUBLICATION_NOT_RELEASED,
    R.PRODUCTION_MATERIALIZATION_NOT_RELEASED,
    R.PRODUCTION_PUBLICATION_NOT_RELEASED,
})

_PRODUCTION_ACTIONS = frozenset({ActionV1.MATERIALIZE_PRODUCTION, ActionV1.PUBLISH_PRODUCTION})


class CapabilityProjectionDefect(ValueError):
    """The projection's inputs contradict each other or leave it nothing to project from — a
    PROGRAMMER error, never a card state. A resource that exists with no decision supplied means
    somebody skipped ``ask``; a decision over a resource the discovery facts say does not exist
    means the two inputs describe different worlds."""


@dataclass(frozen=True, slots=True)
class CapabilityResourceFactsV1:
    """Server-derived DISCOVERY facts: does the R1 subject each action is decided over exist
    yet? Assembled server-side from the stores — never from a client claim. Every default is
    the pre-resource side, so an unassembled fact renders "potentially available", never a
    fabricated verdict."""

    authoring_subject_exists: bool = False       # AUTHOR_FORMULA — the 1103 subject
    build_set_revision_exists: bool = False      # GENERATE_PREVIEW
    sealed_artifact_exists: bool = False         # EXECUTE_SANDBOX + MATERIALIZE_PRODUCTION
    verified_output_exists: bool = False         # PUBLISH_SANDBOX
    production_output_exists: bool = False       # PUBLISH_PRODUCTION

    def exists_for(self, action: ActionV1) -> bool:
        return getattr(self, _SUBJECT_FACT_BY_ACTION[action])


#: action -> the discovery fact naming its R1 subject. EXECUTE_SANDBOX and MATERIALIZE_PRODUCTION
#: deliberately share the sealed artifact — R1's table says so.
_SUBJECT_FACT_BY_ACTION: dict[ActionV1, str] = {
    ActionV1.AUTHOR_FORMULA: "authoring_subject_exists",
    ActionV1.GENERATE_PREVIEW: "build_set_revision_exists",
    ActionV1.EXECUTE_SANDBOX: "sealed_artifact_exists",
    ActionV1.PUBLISH_SANDBOX: "verified_output_exists",
    ActionV1.MATERIALIZE_PRODUCTION: "sealed_artifact_exists",
    ActionV1.PUBLISH_PRODUCTION: "production_output_exists",
}

#: The legacy five-rung wire names, projected onto the one authority. ``None`` marks the rungs
#: the ladder KEEPS (R1: save_idea + create_contract stay legacy) and the two retired-in-place
#: V1-lane rungs whose canonical successor (EXECUTE_SANDBOX over the sealed artifact) takes over
#: at step 8/B3 — until then their answers come from the legacy fold, not from this projection.
LADDER_RUNG_VIEW: Mapping[str, ActionV1 | None] = {
    "save_idea": None,
    "create_contract": None,
    "author_formula": ActionV1.AUTHOR_FORMULA,
    "request_materialization": None,
    "execute_materialization": None,
}


@dataclass(frozen=True, slots=True)
class ActionCapabilityV1:
    """One action's display state. ``blockers``/``warnings`` are the SERVICE's codes verbatim —
    the projection adds copy and a mode, never a code."""

    action: ActionV1
    render_mode: CapabilityRenderMode
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    copy: str
    pre_resource: bool


@dataclass(frozen=True, slots=True)
class CapabilityAssessmentV1:
    """All six actions, in ladder order — the ONE card-state source (a route never enqueues what
    this calls unavailable, because both read the same decisions)."""

    per_action: tuple[ActionCapabilityV1, ...]

    def for_action(self, action: ActionV1) -> ActionCapabilityV1:
        for entry in self.per_action:
            if entry.action is action:
                return entry
        raise KeyError(action)


def _project_one(action: ActionV1, decision: ActionDecisionV1) -> ActionCapabilityV1:
    blockers = tuple(sorted(
        set(decision.blockers)
        | {code for verdict in decision.per_member for code in verdict.blockers}))
    warnings = tuple(decision.warnings)
    if not decision.allowed:
        copy = (PRODUCTION_NOT_RELEASED_COPY
                if action in _PRODUCTION_ACTIONS and set(blockers) & _NOT_RELEASED_CODES
                else "")
        return ActionCapabilityV1(
            action=action, render_mode=CapabilityRenderMode.UNAVAILABLE,
            blockers=blockers, warnings=warnings, copy=copy, pre_resource=False)
    mode = (CapabilityRenderMode.PROVISIONAL
            if set(warnings) & GUARD_RENDERED_WARNINGS else CapabilityRenderMode.EXACT)
    return ActionCapabilityV1(
        action=action, render_mode=mode, blockers=(), warnings=warnings, copy="",
        pre_resource=False)


def project_capabilities(
    decisions: Mapping[ActionV1, ActionDecisionV1],
    *, resources: CapabilityResourceFactsV1,
) -> CapabilityAssessmentV1:
    """Project the service's answers (plus discovery facts) into the six card states.

    For every action: a decision when its subject exists (the projection serves it verbatim),
    or the pre-resource state when it does not. Supplying neither for an existing resource, or a
    decision for a resource the discovery facts deny, refuses — the projection may never fill a
    gap with an invented verdict.
    """
    entries: list[ActionCapabilityV1] = []
    for action in ActionV1:
        decision = decisions.get(action)
        exists = resources.exists_for(action)
        if decision is not None and not exists:
            raise CapabilityProjectionDefect(
                f"a decision was supplied for {action} but the discovery facts say its subject "
                f"({_SUBJECT_FACT_BY_ACTION[action]}) does not exist: the two inputs describe "
                f"different worlds, and the projection will not pick one")
        if decision is None and exists:
            raise CapabilityProjectionDefect(
                f"{action}'s subject exists but no decision was supplied: ask the service — "
                f"the projection never computes a verdict of its own")
        if decision is None:
            # Pre-resource. A production action is honest about WHY it is not "potentially
            # available": the capability itself is not released, whatever gets selected.
            production = action in _PRODUCTION_ACTIONS
            entries.append(ActionCapabilityV1(
                action=action, render_mode=CapabilityRenderMode.UNAVAILABLE,
                blockers=(), warnings=(),
                copy=PRODUCTION_NOT_RELEASED_COPY if production else PRE_RESOURCE_COPY,
                pre_resource=True))
            continue
        entries.append(_project_one(action, decision))
    return CapabilityAssessmentV1(per_action=tuple(entries))
