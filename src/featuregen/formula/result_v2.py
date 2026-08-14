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

__all__ = [
    "DISPOSITION_POLICY_VERSION_V2",
    "AuthoringResultV2",
    "derive_disposition_v2",
]

#: Version of the v2 fold precedence + coherence rules stamped on every v2 result. Its own pin:
#: v1's :data:`~featuregen.formula.result.DISPOSITION_POLICY_VERSION` must be free to move without
#: restamping v2 runs, and vice versa.
DISPOSITION_POLICY_VERSION_V2 = 1

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
    critic_status: CriticStatus
    technical_status: TechnicalStatus
    authoring_disposition: AuthoringDisposition
    disposition_policy_version: int
    authoring_run_id: str
    #: The validated proposal — the structural half of the v2 artifact. Present on every outcome
    #: that produced one; ``None`` when nothing parsed.
    candidate_proposal: TypedFormulaProposalV2 | None
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


def derive_disposition_v2(
    axes: AuthoringAxes,
    *,
    authoring_run_id: str,
    candidate_proposal: TypedFormulaProposalV2 | None = None,
    candidate_output: FormulaOutputPolicyV2 | None = None,
    output_requirements: tuple[str, ...] = (),
    authority_failures: tuple[AuthorityFailure, ...] = (),
    capability_reason: str | None = None,
    critic_findings_hash: str | None = None,
) -> AuthoringResultV2:
    """Fold the six axes into ONE :class:`AuthoringResultV2` (pure — no I/O).

    Artifact coherence is ENFORCED, not documented: an artifact set that contradicts the folded
    disposition raises :class:`~featuregen.formula.result.IncoherentResultError`."""
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
        critic_status=axes.critic_status,
        technical_status=axes.technical_status,
        authoring_disposition=disposition,
        disposition_policy_version=DISPOSITION_POLICY_VERSION_V2,
        authoring_run_id=authoring_run_id,
        candidate_proposal=candidate_proposal,
        candidate_proposal_hash=(
            None if candidate_proposal is None else proposal_content_hash_v2(candidate_proposal)),
        candidate_output=candidate_output,
        output_requirements=tuple(output_requirements),
        authority_failures=tuple(authority_failures),
        capability_reason=capability_reason,
        critic_findings_hash=critic_findings_hash,
    )
