"""Spec §F — the multi-axis :class:`AuthoringResult` + the pure disposition fold.

Every upstream verdict converges here into ONE outcome: the structural parse (Task 3),
the capability classification (Task 7), the output-authority resolution (Task 6), the
advisory expectation comparison, the critic review, and the technical outcome each
contribute one status axis; :func:`derive_disposition` folds the six axes into a single
``authoring_disposition`` under the strict §F precedence
(technical > rejected > unsupported > needs_review > resolved) and validates that the
CARRIED artifacts are coherent with that disposition.

The honesty invariant (review#4): when output authority is unresolved
(``output_status`` in ``{"needs_authority", "external_requirement"}``) NO authoritative
formula exists — the result carries the validated ``candidate_proposal`` plus the typed
reasons (:class:`AuthorityFailure` / Task-6
:class:`~featuregen.formula.output_authority.ExternalRequirement`), NEVER a
``TypedFormulaV1`` and never a fabricated
:class:`~featuregen.formula.schema.FormulaOutputPolicyV1` (a
``TypedFormulaProposalV1`` structurally has no ``output`` slot). A fold that
manufactures a resolved policy out of an unresolved one would launder a guess into
authority; this module raises :class:`IncoherentResultError` instead.

Pure in-memory: no DB, no execution, no durable artifact. ``unsupported != invalid``.
:func:`derive_disposition` is the SINGLE constructor signature (review#12): one
``candidate_formula`` slot and one ``candidate_proposal`` slot, disposition-checked —
no separate candidate-shaped overloads.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, get_args

from featuregen.formula.canonical import formula_content_hash
from featuregen.formula.output_authority import ExternalRequirement
from featuregen.formula.schema import TypedFormulaProposalV1, TypedFormulaV1

__all__ = [
    "DISPOSITION_POLICY_VERSION",
    "AuthoringAxes",
    "AuthoringResult",
    "AuthorityFailure",
    "IncoherentResultError",
    "derive_disposition",
]

#: Version of the §F fold precedence + coherence rules stamped on every result.
DISPOSITION_POLICY_VERSION = 1

StructuralStatus = Literal["ok", "invalid_formula", "unsupported_operation"]
CapabilityStatus = Literal["ok", "unsupported_capability"]
OutputStatus = Literal["resolved", "needs_authority", "invalid_output", "external_requirement"]
ExpectationStatus = Literal["match", "mismatch", "not_provided"]
CriticStatus = Literal["clean", "advisory", "blocking"]
TechnicalStatus = Literal["ok", "technical_failure"]
AuthoringDisposition = Literal[
    "RESOLVED", "NEEDS_REVIEW", "UNSUPPORTED", "REJECTED", "TECHNICAL_FAILURE"
]

#: The output statuses under which NO authoritative formula can exist (§F honesty core).
_UNRESOLVED_OUTPUT: frozenset[str] = frozenset({"needs_authority", "external_requirement"})


class IncoherentResultError(ValueError):
    """A result whose carried artifacts contradict its folded disposition (or an axis
    value outside the §F vocabulary, which would otherwise fall open to RESOLVED)."""


@dataclass(frozen=True, slots=True)
class AuthorityFailure:
    """WHICH operand/field failed output authority and WHY.

    ``reason`` is the machine reason (e.g. the C1 conflict status a Task-6
    ``NeedsAuthority`` carried: ``fork`` / ``hash_mismatch`` / ``projection_unavailable``);
    ``operand`` is the affected operand — the body path (``body.numerator``) or its
    ``logical_ref``; ``field`` is the C1 field that failed (``additivity`` /
    ``output_type`` / a grain-key read). Either locator may be ``None`` when the failure
    is not attributable that precisely; the reason is always required."""

    reason: str
    operand: str | None = None
    field: str | None = None


@dataclass(frozen=True, slots=True)
class AuthoringAxes:
    """The six upstream status axes :func:`derive_disposition` folds. No defaults —
    every axis is an explicit upstream verdict, never an assumed all-clear."""

    structural_status: StructuralStatus
    capability_status: CapabilityStatus
    output_status: OutputStatus
    expectation_status: ExpectationStatus
    critic_status: CriticStatus
    technical_status: TechnicalStatus


@dataclass(frozen=True, slots=True)
class AuthoringResult:
    """The folded §F outcome. Built ONLY by :func:`derive_disposition` (which enforces
    the artifact-coherence invariants); direct construction bypasses those guards."""

    structural_status: StructuralStatus
    capability_status: CapabilityStatus
    output_status: OutputStatus
    expectation_status: ExpectationStatus
    critic_status: CriticStatus
    technical_status: TechnicalStatus
    authoring_disposition: AuthoringDisposition
    disposition_policy_version: int
    authoring_run_id: str
    candidate_formula: TypedFormulaV1 | None
    candidate_formula_hash: str | None
    candidate_proposal: TypedFormulaProposalV1 | None
    output_requirements: tuple[ExternalRequirement, ...]
    authority_failures: tuple[AuthorityFailure, ...]
    capability_reason: str | None
    critic_findings_hash: str | None


_AXIS_VOCABULARY: tuple[tuple[str, frozenset[str]], ...] = (
    ("structural_status", frozenset(get_args(StructuralStatus))),
    ("capability_status", frozenset(get_args(CapabilityStatus))),
    ("output_status", frozenset(get_args(OutputStatus))),
    ("expectation_status", frozenset(get_args(ExpectationStatus))),
    ("critic_status", frozenset(get_args(CriticStatus))),
    ("technical_status", frozenset(get_args(TechnicalStatus))),
)


def _validate_axes(axes: AuthoringAxes) -> None:
    """Fail CLOSED on an unknown axis value: the precedence chain matches known-bad
    values, so an unrecognized status would fall through every arm and reach RESOLVED."""
    for name, allowed in _AXIS_VOCABULARY:
        value = getattr(axes, name)
        if value not in allowed:
            raise IncoherentResultError(
                f"axes.{name}={value!r} is not in the §F vocabulary {sorted(allowed)}"
            )


def _fold(axes: AuthoringAxes) -> AuthoringDisposition:
    """The §F precedence — STRICT order, first match wins."""
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


def derive_disposition(
    axes: AuthoringAxes,
    *,
    authoring_run_id: str,
    candidate_formula: TypedFormulaV1 | None = None,
    candidate_proposal: TypedFormulaProposalV1 | None = None,
    output_requirements: tuple[ExternalRequirement, ...] = (),
    authority_failures: tuple[AuthorityFailure, ...] = (),
    capability_reason: str | None = None,
    critic_findings_hash: str | None = None,
) -> AuthoringResult:
    """Fold the six axes into ONE :class:`AuthoringResult` (pure — no I/O).

    Artifact coherence is ENFORCED, not documented: an artifact set that contradicts the
    folded disposition raises :class:`IncoherentResultError`. ``candidate_formula_hash``
    is always computed here (``formula_content_hash``), never caller-supplied."""
    _validate_axes(axes)
    disposition = _fold(axes)

    if disposition in ("UNSUPPORTED", "REJECTED", "TECHNICAL_FAILURE"):
        if candidate_formula is not None:
            raise IncoherentResultError(
                f"{disposition} carries no candidate_formula — an authoritative "
                "TypedFormulaV1 cannot accompany a non-authored outcome"
            )
    elif disposition == "RESOLVED" or axes.output_status not in _UNRESOLVED_OUTPUT:
        # RESOLVED, or a reviewable NEEDS_REVIEW whose output DID resolve (blocking
        # critic / expectation mismatch): a real TypedFormulaV1 is the ONLY artifact.
        if candidate_formula is None:
            raise IncoherentResultError(
                f"{disposition} with a resolved output requires the authored "
                "candidate_formula — a result claiming resolution without a "
                "TypedFormulaV1 is incoherent"
            )
        if candidate_proposal is not None:
            raise IncoherentResultError(
                f"{disposition} with a resolved output carries the TypedFormulaV1 "
                "only — candidate_proposal must be None"
            )
    else:
        # NEEDS_REVIEW with UNRESOLVED output authority (§F honesty core): NO
        # authoritative formula exists — carrying one would launder a guess into
        # authority. The validated proposal is the ONLY reviewable artifact.
        if candidate_formula is not None:
            raise IncoherentResultError(
                f"output authority is unresolved (output_status={axes.output_status!r})"
                " — no authoritative TypedFormulaV1 exists, so a candidate_formula "
                "here would launder a guess into authority"
            )
        if candidate_proposal is None:
            raise IncoherentResultError(
                "unresolved-output NEEDS_REVIEW requires the validated "
                "candidate_proposal — there is nothing to review without it"
            )

    return AuthoringResult(
        structural_status=axes.structural_status,
        capability_status=axes.capability_status,
        output_status=axes.output_status,
        expectation_status=axes.expectation_status,
        critic_status=axes.critic_status,
        technical_status=axes.technical_status,
        authoring_disposition=disposition,
        disposition_policy_version=DISPOSITION_POLICY_VERSION,
        authoring_run_id=authoring_run_id,
        candidate_formula=candidate_formula,
        candidate_formula_hash=(
            None if candidate_formula is None else formula_content_hash(candidate_formula)
        ),
        candidate_proposal=candidate_proposal,
        output_requirements=tuple(output_requirements),
        authority_failures=tuple(authority_failures),
        capability_reason=capability_reason,
        critic_findings_hash=critic_findings_hash,
    )
