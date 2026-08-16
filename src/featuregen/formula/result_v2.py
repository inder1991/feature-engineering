"""Task A3 — the §F multi-axis result and disposition fold, restated over the **v2** types.

D-3 of the formula/execution-seam charter: *the v2 orchestrator is a SIBLING of ``run_authoring``,
never an edit of it — ``run_authoring_v2`` restates those invariants over the v2 types.* This module
is the half of that restatement that owns the ARTIFACT, and it exists as its own type for one
structural reason, not for symmetry:

**v2 has no ``TypedFormulaV2``.** v1 fuses the authored structure and its C1-resolved output policy
into one identity object (``TypedFormulaV1``), which is why ``derive_disposition`` can say "a
resolved output carries the TypedFormulaV1 ONLY — ``candidate_proposal`` must be None". BR-6 never
minted that fusing type for v2: ``resolve_output_v2`` returns a ``FormulaOutputPolicyV2`` BESIDE the
``TypedFormulaProposalV2`` it was resolved for. So the v2 authored artifact IS the pair, and the
coherence law is restated accordingly:

* UNSUPPORTED / REJECTED / TECHNICAL_FAILURE carry **no** ``candidate_output`` — an authoritative
  output policy cannot accompany an outcome that authored nothing.
* A RESOLVED (or a reviewable NEEDS_REVIEW whose output DID resolve) requires **both** halves: the
  validated proposal and the policy resolved for it. Half a pair is not an artifact.
* A NEEDS_REVIEW whose output authority is UNRESOLVED requires the proposal and **forbids** the
  policy — the §F honesty core, unchanged in words and in force: carrying a policy nothing resolved
  would launder a guess into authority.

``candidate_proposal_hash`` is always derived here (``proposal_content_hash_v2``), never
caller-supplied, exactly as v1 derives ``candidate_formula_hash``.

THE PRECEDENCE IS NOT RE-DECIDED. :func:`_fold_v2` restates v1's §F order verbatim; a test pins the
two equal over the complete 432-combination axis cross-product, so the restatement can never drift
into a second policy. Pure in-memory: no DB, no execution. ``unsupported != invalid``.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import get_args

from featuregen.formula.canonical_v2 import proposal_content_hash_v2
from featuregen.formula.canonical_v3 import proposal_content_hash_v3
from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2
from featuregen.formula.result import (
    AuthoringAxes,
    AuthoringDisposition,
    AuthorityFailure,
    CapabilityStatus,
    CriticStatus,
    ExpectationStatus,
    IncoherentResultError,
    OutputStatus,
    StructuralStatus,
    TechnicalStatus,
)
from featuregen.formula.schema_v2 import TypedFormulaProposalV2
from featuregen.formula.schema_v3 import TypedFormulaProposalV3

__all__ = [
    "DISPOSITION_POLICY_VERSION_V2",
    "AuthoringResultV2",
    "derive_disposition_v2",
]

#: Version of the v2 fold precedence + coherence rules stamped on every v2 result. Its own pin:
#: v1's :data:`~featuregen.formula.result.DISPOSITION_POLICY_VERSION` must be free to move without
#: restamping v2 runs, and vice versa.
DISPOSITION_POLICY_VERSION_V2 = 2   # C-A6: REVIEW_BYPASSED is a disposition this version knows

#: The output statuses under which NO authoritative output policy can exist (§F honesty core).
_UNRESOLVED_OUTPUT: frozenset[str] = frozenset({"needs_authority", "external_requirement"})

_AXIS_VOCABULARY: tuple[tuple[str, frozenset[str]], ...] = (
    ("structural_status", frozenset(get_args(StructuralStatus))),
    ("capability_status", frozenset(get_args(CapabilityStatus))),
    ("output_status", frozenset(get_args(OutputStatus))),
    ("expectation_status", frozenset(get_args(ExpectationStatus))),
    ("critic_status", frozenset(get_args(CriticStatus))),
    ("technical_status", frozenset(get_args(TechnicalStatus))),
)


@dataclass(frozen=True, slots=True)
class AuthoringResultV2:
    """The folded §F outcome of ONE v2 authoring run. Built ONLY by
    :func:`derive_disposition_v2`, which enforces the artifact-coherence invariants above."""

    structural_status: StructuralStatus
    capability_status: CapabilityStatus
    output_status: OutputStatus
    expectation_status: ExpectationStatus
    #: ``None`` ONLY for a reviewed-blueprint bypass — see :attr:`review`.
    critic_status: CriticStatus | None
    #: HOW review was obtained (C-A5). ``None`` on results folded from v1-shaped axes, which have
    #: no concept of a bypass.
    review: ReviewOutcomeV2 | None
    technical_status: TechnicalStatus
    authoring_disposition: AuthoringDisposition
    disposition_policy_version: int
    authoring_run_id: str
    #: The validated proposal — the structural half of the artifact. Present on every outcome that
    #: produced one; ``None`` when nothing parsed. **v2 OR v3** (C-A2): v3 is the same LANGUAGE at
    #: wire schema 3, so one result type carries either, and the hash below dispatches on which.
    candidate_proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3 | None
    candidate_proposal_hash: str | None
    #: The AUTHORITATIVE output policy, the second half of the pair. Present ONLY when the output
    #: actually resolved (see the module docstring's three coherence rules).
    candidate_output: FormulaOutputPolicyV2 | None
    #: Machine reasons for a refused or externally-gated output (``CURRENCY_CONVERSION_UNDECLARED``,
    #: ``MIXED_UNITS``, ``EXTERNAL_TYPE_VALIDATION_REQUIRED``).
    output_requirements: tuple[str, ...]
    authority_failures: tuple[AuthorityFailure, ...]
    capability_reason: str | None
    critic_findings_hash: str | None


@dataclass(frozen=True, slots=True)
class CriticExecutedV2:
    """The critic RAN. ``status`` is the shared §F vocabulary; the findings hash is its evidence."""

    status: CriticStatus
    findings_hash: str


@dataclass(frozen=True, slots=True)
class ReviewedBlueprintBypassV2:
    """The critic did NOT run, because a REVIEWED blueprint is the authority (C-A5).

    Deterministic recipe authoring instantiates a blueprint a human already reviewed. Asking an LLM
    critic to re-judge it would spend a provider call to re-derive a verdict the review already
    carries — but recording ``critic_status="clean"`` would state that the critic ran and found
    nothing, which is false. This variant says what actually happened, and names the exact blueprint
    revision and expectation it stood on, so "the review covered THIS" is checkable rather than
    asserted.

    Neutrality is earned at CONSTRUCTION, not in the fold: only a producer that has verified the
    blueprint revision and expectation hash may build one.
    """

    blueprint_revision: str
    expectation_hash: str

    def __post_init__(self) -> None:
        # A bypass with nothing to check is strictly WORSE than the `"clean"` lie C-A5 removed: it
        # also carries a false claim of checkability.
        for name in ("blueprint_revision", "expectation_hash"):
            if not str(getattr(self, name)).strip():
                raise IncoherentResultError(
                    f"ReviewedBlueprintBypassV2.{name} is empty — a bypass names the exact "
                    "blueprint it stood on, or it is an assertion of neutrality with no evidence")


#: How review was obtained for one v2/v3 authoring run.
ReviewOutcomeV2 = CriticExecutedV2 | ReviewedBlueprintBypassV2


@dataclass(frozen=True, slots=True)
class AuthoringAxesV2:
    """v2's six axes, with ``review`` where v1 has a bare ``critic_status``.

    V1's :class:`AuthoringAxes` is deliberately untouched: its ``critic_status`` is mandatory and
    three-valued, and widening it would let a v1 run claim a bypass it has no concept of.
    """

    structural_status: StructuralStatus
    capability_status: CapabilityStatus
    output_status: OutputStatus
    expectation_status: ExpectationStatus
    review: ReviewOutcomeV2
    technical_status: TechnicalStatus

    def shared_axes(self) -> AuthoringAxes:
        """The v1-shaped projection the SHARED §F precedence folds.

        A bypass projects to ``"clean"`` **for folding only** — it is neutral, exactly as a clean
        critic is. The projection never reaches the artifact: the result records the real
        :class:`ReviewOutcomeV2` and leaves ``critic_status`` ``None``, so nothing downstream can
        read a bypass as "the critic ran and found nothing".

        **The variant check FAILS CLOSED, and that is load-bearing.** An ``else`` that mapped every
        non-``CriticExecutedV2`` value to ``"clean"`` would make ``review=None`` — or any object at
        all — fold as neutral, which is the exact fail-open :func:`_validate_axes` exists to
        prevent ("an unrecognized status would fall through every arm and reach RESOLVED"), and the
        exact confusion this whole type exists to end: a non-answer reading as "the critic ran and
        found nothing".
        """
        if isinstance(self.review, CriticExecutedV2):
            critic_status: CriticStatus = self.review.status
        elif isinstance(self.review, ReviewedBlueprintBypassV2):
            critic_status = "clean"
        else:
            raise IncoherentResultError(
                f"axes.review={self.review!r} is neither CriticExecutedV2 nor "
                "ReviewedBlueprintBypassV2 — review must say HOW it was obtained, and an "
                "unrecognized value must never fold as neutral")
        return AuthoringAxes(
            structural_status=self.structural_status,
            capability_status=self.capability_status,
            output_status=self.output_status,
            expectation_status=self.expectation_status,
            critic_status=critic_status,
            technical_status=self.technical_status,
        )


def _validate_axes(axes: AuthoringAxes) -> None:
    """Fail CLOSED on an unknown axis value: the precedence chain matches known-bad values, so an
    unrecognized status would fall through every arm and reach RESOLVED."""
    for name, allowed in _AXIS_VOCABULARY:
        value = getattr(axes, name)
        if value not in allowed:
            raise IncoherentResultError(
                f"axes.{name}={value!r} is not in the §F vocabulary {sorted(allowed)}")


def _fold_v2(axes: AuthoringAxes) -> AuthoringDisposition:
    """The §F precedence — STRICT order, first match wins. Verbatim v1; pinned equal to it."""
    if axes.technical_status == "technical_failure":
        return "TECHNICAL_FAILURE"
    if axes.structural_status == "invalid_formula" or axes.output_status == "invalid_output":
        return "REJECTED"
    if (
        axes.structural_status == "unsupported_operation"
        or axes.capability_status == "unsupported_capability"
    ):
        return "UNSUPPORTED"
    if (
        axes.output_status in _UNRESOLVED_OUTPUT
        or axes.critic_status == "blocking"
        or axes.expectation_status == "mismatch"
    ):
        return "NEEDS_REVIEW"
    return "RESOLVED"


def _content_hash(proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3) -> str:
    """The proposal's content identity, by VERSION — never by the caller's say-so.

    Each wire schema owns its own canonical projection and its own hash lineage, so hashing a v3
    proposal with v2's canonicalizer would mint an identity under a projection that never saw
    ``row_selections``. Dispatching on the type is what keeps the two lineages separate while one
    result type carries either.
    """
    if isinstance(proposal, TypedFormulaProposalV3):
        return proposal_content_hash_v3(proposal)
    return proposal_content_hash_v2(proposal)


def derive_disposition_v2(
    axes: AuthoringAxes | AuthoringAxesV2,
    *,
    authoring_run_id: str,
    candidate_proposal: TypedFormulaProposalV2 | TypedFormulaProposalV3 | None = None,
    candidate_output: FormulaOutputPolicyV2 | None = None,
    output_requirements: tuple[str, ...] = (),
    authority_failures: tuple[AuthorityFailure, ...] = (),
    capability_reason: str | None = None,
    critic_findings_hash: str | None = None,
    reviewed_expectation_hash: str | None = None,
) -> AuthoringResultV2:
    """Fold the six axes into ONE :class:`AuthoringResultV2` (pure — no I/O).

    Artifact coherence is ENFORCED, not documented: an artifact set that contradicts the folded
    disposition raises :class:`~featuregen.formula.result.IncoherentResultError`."""
    review = axes.review if isinstance(axes, AuthoringAxesV2) else None
    if isinstance(axes, AuthoringAxesV2):
        axes = axes.shared_axes()

    if isinstance(review, ReviewedBlueprintBypassV2):
        # NEUTRALITY IS EARNED, NOT ASSERTED. Without this the same bypass value folds ANY formula
        # neutral — one value was measured being accepted verbatim on two proposals with different
        # content hashes, both RESOLVED. The caller must name the expectation the run was opened
        # for, and it must be the one the bypass stood on.
        if not str(reviewed_expectation_hash or "").strip():
            raise IncoherentResultError(
                "a ReviewedBlueprintBypassV2 requires reviewed_expectation_hash — a bypass whose "
                "coverage nobody checked is an unreviewed formula folding as neutral")
        if review.expectation_hash != reviewed_expectation_hash:
            raise IncoherentResultError(
                f"bypass expectation_hash {review.expectation_hash!r} does not match the run's "
                f"reviewed expectation {reviewed_expectation_hash!r} — the review covered a "
                "different formula")
        if critic_findings_hash is not None:
            raise IncoherentResultError(
                "a bypass carries no critic_findings_hash — the critic did not run, so evidence "
                "of its findings contradicts the fold")
    if isinstance(review, CriticExecutedV2):
        # DERIVED here, never caller-supplied — the same rule `candidate_proposal_hash` obeys. Two
        # independent copies of one piece of evidence are two things that can disagree.
        critic_findings_hash = review.findings_hash
    _validate_axes(axes)
    disposition = _fold_v2(axes)

    if disposition in ("UNSUPPORTED", "REJECTED", "TECHNICAL_FAILURE"):
        if candidate_output is not None:
            raise IncoherentResultError(
                f"{disposition} carries no candidate_output — an authoritative "
                "FormulaOutputPolicyV2 cannot accompany a non-authored outcome")
    elif disposition == "RESOLVED" or axes.output_status not in _UNRESOLVED_OUTPUT:
        # RESOLVED, or a reviewable NEEDS_REVIEW whose output DID resolve (blocking critic /
        # expectation mismatch): the v2 artifact is the PAIR, and half a pair is not an artifact.
        if candidate_output is None:
            raise IncoherentResultError(
                f"{disposition} with a resolved output requires the authoritative "
                "candidate_output — a result claiming resolution without a "
                "FormulaOutputPolicyV2 is incoherent")
        if candidate_proposal is None:
            raise IncoherentResultError(
                f"{disposition} with a resolved output requires the validated "
                "candidate_proposal — v2 has no type that fuses the two, so the policy alone "
                "names no formula")
    else:
        # NEEDS_REVIEW with UNRESOLVED output authority (§F honesty core): no authoritative output
        # exists — carrying one would launder a guess into authority.
        if candidate_output is not None:
            raise IncoherentResultError(
                f"output authority is unresolved (output_status={axes.output_status!r})"
                " — no authoritative FormulaOutputPolicyV2 exists, so a candidate_output here "
                "would launder a guess into authority")
        if candidate_proposal is None:
            raise IncoherentResultError(
                "unresolved-output NEEDS_REVIEW requires the validated candidate_proposal — "
                "there is nothing to review without it")

    return AuthoringResultV2(
        structural_status=axes.structural_status,
        capability_status=axes.capability_status,
        output_status=axes.output_status,
        expectation_status=axes.expectation_status,
        # C-A5: a BYPASS reports no critic status at all. `"clean"` would say the critic ran and
        # found nothing; `None` says it did not run, and `review` says why.
        critic_status=(None if isinstance(review, ReviewedBlueprintBypassV2)
                       else axes.critic_status),
        review=review,
        technical_status=axes.technical_status,
        authoring_disposition=disposition,
        disposition_policy_version=DISPOSITION_POLICY_VERSION_V2,
        authoring_run_id=authoring_run_id,
        candidate_proposal=candidate_proposal,
        candidate_proposal_hash=(
            None if candidate_proposal is None else _content_hash(candidate_proposal)),
        candidate_output=candidate_output,
        output_requirements=tuple(output_requirements),
        authority_failures=tuple(authority_failures),
        capability_reason=capability_reason,
        critic_findings_hash=critic_findings_hash,
    )
