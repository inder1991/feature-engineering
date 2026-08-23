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
#: ``deferred_to_compiler`` is V3's, and ONLY V3's. A V3 run validates the formula and captures the
#: author's intent; resolving governed output type, unit, currency and additivity is the COMPILER's
#: (C-A7), so the axis records that the question is not yet asked rather than unanswerable.
#:
#: ▲ It is a SEPARATE value from ``needs_authority`` because the two mean opposite things. A V1/V2
#: run reaching ``needs_authority`` FAILED to establish authority — its governed-facts read failed
#: closed — and must be reviewed by a human. A V3 run reaching ``deferred_to_compiler`` succeeded at
#: everything it owns. Collapsing them would make "no human has looked at this" and "the next stage
#: has not run yet" the same recorded fact, and a reader could not tell a working pipeline from a
#: stalled one.
OutputStatus = Literal[
    "resolved", "needs_authority", "invalid_output", "external_requirement",
    "deferred_to_compiler",
]
ExpectationStatus = Literal["match", "mismatch", "not_provided"]
CriticStatus = Literal["clean", "advisory", "blocking"]
TechnicalStatus = Literal["ok", "technical_failure"]
#: ``READY_FOR_OUTPUT_BINDING`` is the terminal state of a V3 run that did everything IT owns:
#: the formula validated, the intent was captured, review evidence was recorded, and the only thing
#: outstanding is the governed output binding the compiler performs. It is admissible; it is not
#: RESOLVED, because nothing has yet consulted governed metadata.
#:
#: A distinct member rather than a special NEEDS_REVIEW, because those are operationally different:
#: a queue, an API or a screen showing "Needs review" while a worker legitimately proceeds without
#: any human review is a status that lies to whoever reads it.
AuthoringDisposition = Literal[
    "RESOLVED", "READY_FOR_OUTPUT_BINDING", "NEEDS_REVIEW", "UNSUPPORTED", "REJECTED",
    "TECHNICAL_FAILURE",
]

#: The output statuses under which NO authoritative formula can exist (§F honesty core).
_UNRESOLVED_OUTPUT: frozenset[str] = frozenset({
    "needs_authority", "external_requirement",
    # V3's deferral belongs here too: no authoritative output exists YET, so a result
    # carrying a `candidate_output` would be laundering a guess into authority exactly as
    # `needs_authority` would. What differs is the DISPOSITION, not the honesty core.
    "deferred_to_compiler",
})


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
