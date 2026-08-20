"""The authoring RESULT vocabulary — shared by every authoring generation.

**Extracted for §8.1's reason, one level down.** `formula/schema.py` turned out to be the shared
structural-LEAF library wearing a V1 name; `formula/result.py` is the same thing for the authoring
outcome. The status axes, the fold's disposition vocabulary and the coherence error say nothing
about which formula language was authored — `formula.result_v2` imports every one of them — and
leaving them in a module named for V1 forced three V2 modules to import from V1 to describe a V2
result.

What stays behind in `result.py` is what is genuinely V1: :class:`~featuregen.formula.result.
AuthoringResult` carries a ``TypedFormulaV1`` and a ``TypedFormulaProposalV1``, so it is a V1 object
and belongs with them.

Literals rather than enums, carried verbatim from where they were defined: they are compared as
strings across a JSON boundary, and turning them into enums here would be a behaviour change
smuggled inside a move.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

__all__ = [
    "DISPOSITION_POLICY_VERSION",
    "AuthoringAxes",
    "AuthoringDisposition",
    "AuthorityFailure",
    "CapabilityStatus",
    "CriticStatus",
    "ExpectationStatus",
    "IncoherentResultError",
    "OutputStatus",
    "StructuralStatus",
    "TechnicalStatus",
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
