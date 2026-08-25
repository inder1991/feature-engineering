"""R1 — ONE authority, six TYPED fact loaders. A loader assembles FACTS, never verdicts.

**The problem this closes** (2026-08-24 plan, ruling R1; T0 §2): every ``ActionRequestV1`` today
is hand-assembled at its call site, and hand-assembled facts diverge — the serve-time contract
route omits ten frozen-option fields the complete assembler carries, and the run rail folds
availability in four separate places. One typed loader per action, dispatched on a TYPED subject,
makes the facts a decision is taken over come from one composition per action:

=======================  ==================================================================
action                   subject (the R1 table)
=======================  ==================================================================
AUTHOR_FORMULA           the 1103 authoring subject — :class:`AuthoringSubjectKeyV1`
GENERATE_PREVIEW         build-set revision                       (loader lands at step 8/B3)
EXECUTE_SANDBOX          sealed artifact                          (step 8/B3)
PUBLISH_SANDBOX          verified output revision                 (step 8/B3)
MATERIALIZE_PRODUCTION   sealed artifact                          (step 8/B3)
PUBLISH_PRODUCTION       exact production output revision         (step 8/B3)
=======================  ==================================================================

Only the AUTHOR_FORMULA loader exists yet — the other five actions' facts do not exist until the
B1/B2 persistence lands, and a loader over facts that do not exist would be an invented answer.
The registry makes adding each one a REGISTRATION, not a modification of this module: a new
loader names its action and its subject type and registers; the dispatch, the refusals and the
``ActionFactsV1`` shape are already here.

**Facts, never verdicts.** A loader returns member names, member blocker/warning FACT codes and
evidence pins. The verdict — which facts refuse THIS action, which proceed-with-warning, which
this action is not the gate for — belongs to ``action_decision.ask()/decide()`` via the §5
disposition table, and nowhere else. A loader that pre-filtered its codes would be a second
authority wearing a loader's name.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

from featuregen.materialize.action_authorization import ActionV1
from featuregen.materialize.action_decision import ActionRequestV1

__all__ = [
    "ActionFactsV1",
    "AuthoringSubjectDiverged",
    "AuthoringSubjectKeyV1",
    "FactLoaderCollision",
    "FactLoaderMissing",
    "FactSubjectMismatch",
    "facts_for_author_formula",
    "load_action_facts",
    "register_fact_loader",
    "registered_subject_type",
    "unregister_fact_loader",
]


class FactLoaderMissing(LookupError):
    """No fact loader is registered for this action — its facts do not exist yet (the R1 table
    says which step builds each). Asking anyway is a PROGRAMMER error, refused by name; the
    remedy is the loader registration, never an inline assembly at the call site."""


class FactSubjectMismatch(TypeError):
    """The subject is not the TYPE this action's loader is registered for. Typed dispatch is the
    whole point: a subject of the wrong shape must refuse loudly, never coerce — a string that
    happens to look like a key is exactly how the wrong resource gets decided."""


class FactLoaderCollision(ValueError):
    """A second loader for an action that already has one. One authority per action — replacing
    a loader is an explicit act (``replace=True``), never a silent import-order accident."""


class AuthoringSubjectDiverged(RuntimeError):
    """The typed subject's pinned hashes disagree with what the store holds for that candidate.

    The subject CLAIMS the five 1103 identity fields; three of them are derivable from the
    frozen candidate, and a claim the store contradicts must refuse — loading facts under it
    would decide about a candidate that does not exist as described.
    """


@dataclass(frozen=True, slots=True)
class ActionFactsV1:
    """One action's assembled facts — exactly the member/evidence halves of ``ActionRequestV1``.

    ``member_names`` lists EVERY member, including the clean ones (owner ruling 2026-08-23
    item 4 — a clean member must not vanish from the record). ``evidence_pins`` are the revision
    ids and content hashes whose movement would change the answer — what the worker re-reads at
    recheck.
    """

    member_names: tuple[str, ...]
    member_blockers: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    member_warnings: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    evidence_pins: Mapping[str, str] = field(default_factory=dict)

    def request(self, *, action: ActionV1, resource_identity_hash: str) -> ActionRequestV1:
        """These facts as the canonical service's question. The caller still chooses ask vs
        decide — a loader never records anything."""
        return ActionRequestV1(
            action=action,
            resource_identity_hash=resource_identity_hash,
            member_names=self.member_names,
            member_blockers=dict(self.member_blockers),
            member_warnings=dict(self.member_warnings),
            evidence_pins=dict(self.evidence_pins))


@dataclass(frozen=True, slots=True)
class AuthoringSubjectKeyV1:
    """The 1103 authoring subject, TYPED — the five identity fields whose JCS hash is the
    retirement scope key (``retirement_scope.retirement_scope_key``: one tuple, one hash, three
    uses — retirement withdraws it, authorization authorizes it, the money guard's
    non-configuration half IS it). ``subject_key`` is the ``resource_identity_hash`` an
    AUTHOR_FORMULA authorization and decision are keyed on."""

    considered_revision_id: str
    option_id: str
    planning_request_hash: str
    catalog_snapshot_hash: str
    definition_revision: str

    @property
    def subject_key(self) -> str:
        from featuregen.overlay.upload.retirement_scope import retirement_scope_key

        return retirement_scope_key(
            considered_revision_id=self.considered_revision_id,
            option_id=self.option_id,
            planning_request_hash=self.planning_request_hash,
            catalog_snapshot_hash=self.catalog_snapshot_hash,
            definition_revision=self.definition_revision)


#: action -> (subject type, loader). Mutated ONLY through the two registration functions below.
_LOADERS: dict[ActionV1, tuple[type, Callable[..., ActionFactsV1]]] = {}


def register_fact_loader(
    action: ActionV1, subject_type: type, loader: Callable[..., ActionFactsV1],
    *, replace: bool = False,
) -> None:
    """Adding a loader is REGISTRATION, not modification of this module — the R1 completion
    path for the five remaining actions (and the test seam for doubles)."""
    if action in _LOADERS and not replace:
        registered, _ = _LOADERS[action]
        raise FactLoaderCollision(
            f"{action} already has a fact loader (subject type {registered.__name__}); one "
            f"authority per action — pass replace=True only for a deliberate substitution")
    _LOADERS[action] = (subject_type, loader)


def unregister_fact_loader(action: ActionV1) -> None:
    """Remove a registration (the test-double cleanup seam)."""
    _LOADERS.pop(action, None)


def registered_subject_type(action: ActionV1) -> type | None:
    """The subject type this action's loader dispatches on, or ``None`` while unbuilt."""
    entry = _LOADERS.get(action)
    return entry[0] if entry is not None else None


def load_action_facts(conn, action: ActionV1, subject: object) -> ActionFactsV1:
    """Typed dispatch: the registered loader for ``action``, refusing an unknown subject type.

    ``type(subject) is subject_type`` deliberately, not ``isinstance``: a subclass smuggling
    extra state past a loader written for the base shape is the same forgery-by-shape problem
    the decision module's facts-never-verdicts rule exists for.
    """
    entry = _LOADERS.get(action)
    if entry is None:
        raise FactLoaderMissing(
            f"no fact loader is registered for {action}: its facts are not built yet (the R1 "
            f"table names the owning step). Register the loader; never assemble inline")
    subject_type, loader = entry
    if type(subject) is not subject_type:
        raise FactSubjectMismatch(
            f"{action} facts load from a {subject_type.__name__} subject, got "
            f"{type(subject).__name__}: typed-subject dispatch refuses rather than coerces")
    return loader(conn, subject)


def facts_for_author_formula(
    conn, authoring_subject_key: AuthoringSubjectKeyV1,
) -> ActionFactsV1:
    """The AUTHOR_FORMULA fact loader — the ONE assembly of the authoring decision's facts.

    Reuses the shipped compositions verbatim rather than re-deriving any of them (§8.3: a second
    composition of one act is how two drift): the server-resolved frozen candidate, the strategy
    resolution, the identity-V2 configuration, the governance blockers (the same covering-set +
    coupon read the store gates on), and the ONE pin builder the service's decide and the
    worker's recheck already share.

    Facts only: strategy refusals and governance withdrawals come back as member blocker CODES
    for the §5 fold to route — this loader never decides which of them refuse.

    Raises:
        CandidateUnavailable: the subject names a revision/option the store cannot resolve —
            there is no candidate to load facts about (typed; the caller maps it).
        AuthoringSubjectDiverged: the subject's pinned hashes contradict the frozen candidate.
    """
    from featuregen.overlay.upload.formula_draft_service import (
        authoring_evidence_pins,
        candidate_governance_blockers,
        current_authoring_config,
        frozen_candidate,
    )
    from featuregen.overlay.upload.formula_strategy import resolve_formula_strategy
    from featuregen.overlay.upload.formula_strategy_facts import assemble_strategy_facts

    subject = authoring_subject_key
    candidate = frozen_candidate(conn, subject.considered_revision_id, subject.option_id)
    stored = (candidate.planning_request_hash, candidate.catalog_snapshot_hash,
              candidate.definition_revision)
    claimed = (subject.planning_request_hash, subject.catalog_snapshot_hash,
               subject.definition_revision)
    if stored != claimed:
        moved = [name for name, s, c in zip(
            ("planning_request_hash", "catalog_snapshot_hash", "definition_revision"),
            stored, claimed) if s != c]
        raise AuthoringSubjectDiverged(
            f"authoring subject for option {subject.option_id!r} on revision "
            f"{subject.considered_revision_id!r} disagrees with the frozen candidate on "
            f"{moved!r}: the subject describes a candidate the store does not hold")

    assembled = assemble_strategy_facts(
        conn, considered_revision_id=candidate.considered_revision_id,
        option_id=subject.option_id, idea=candidate.idea,
        catalog_snapshot_hash=candidate.catalog_snapshot_hash)
    decision = resolve_formula_strategy(assembled.facts)
    provider_contract, _config_payload, config_hash = current_authoring_config(decision)
    governance_blockers, governance_warnings = candidate_governance_blockers(
        conn, candidate=candidate, option_id=subject.option_id, strategy=decision.strategy,
        strategy_identity_hash=decision.strategy_identity_hash,
        provider_contract_hash=provider_contract, config_hash=config_hash,
        scope_key=subject.subject_key)

    blockers = tuple(dict.fromkeys(tuple(decision.blockers) + tuple(governance_blockers)))
    warnings = tuple(dict.fromkeys(tuple(decision.warnings) + tuple(governance_warnings)))
    return ActionFactsV1(
        member_names=(subject.option_id,),
        member_blockers={subject.option_id: blockers} if blockers else {},
        member_warnings={subject.option_id: warnings} if warnings else {},
        evidence_pins=authoring_evidence_pins(
            retirement_scope_key=subject.subject_key,
            catalog_snapshot_hash=candidate.catalog_snapshot_hash,
            strategy_identity_hash=decision.strategy_identity_hash))


register_fact_loader(ActionV1.AUTHOR_FORMULA, AuthoringSubjectKeyV1, facts_for_author_formula)
