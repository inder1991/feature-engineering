"""C-A7 — ``AuthoredOutputIntentV2``: what the author INTENDED, derived and never re-declared.

**Why derived.** ``ExpectedOutput`` is advisory and never identity-bearing; the formula's authority
refs and decimal policy are structural and always are. An intent assembled by hand from both would
be a THIRD statement about the output, free to disagree with either — and the disagreement would
surface at S5 as a mismatch nobody could attribute. :func:`derive_output_intent_v2` reads the
proposal and nothing else, so the intent cannot say something the proposal does not.

**Advisory and structural are kept apart, on purpose.** ``unit``, ``additivity`` and
``target_currency`` come from the model's ``expected_output`` and may be absent — a deterministic
run declares none, which is truthful rather than a gap. ``conversion_required``,
``declared_conversion_ref`` and ``numeric_shape`` come from the formula's own structure and are
always present. ``authored_expectation_present`` records which world this intent came from, because
"the author expected AED" and "the author expected nothing" are different facts and an empty string
collapses them.

**PROVISIONAL, and it says so.** This is what the author meant, not what the output IS. Resolving it
against C1's governed facts is S5's, and the V3 authoring result is deliberately terminal WITHOUT
``OUTPUT_POLICY_RESOLVED`` — a stage that has not run must not be represented as having run and
agreed.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.formula.schema import AdditivityClass, DecimalPolicy
from featuregen.formula.schema_v3 import TypedFormulaProposalV3, body_expressions_v3

__all__ = [
    "OUTPUT_INTENT_CAPTURED",
    "AuthoredOutputIntentV2",
    "NumericShapeV2",
    "derive_output_intent_v2",
]

#: The trace stage a captured intent is recorded under. Named here rather than in the orchestrator
#: so the stage and the artifact it records cannot drift apart.
OUTPUT_INTENT_CAPTURED = "OUTPUT_INTENT_CAPTURED"


@dataclass(frozen=True, slots=True)
class NumericShapeV2:
    """The DESIRED numeric shape — the formula's declared decimal policy, carried verbatim.

    Desired, not resolved: whether this shape survives contact with the operands' real types is
    C-C6's answer, and it can refuse. Recording the intent separately is what lets that refusal
    say which of the two disagreed.
    """

    precision: int
    scale: int
    rounding: str
    overflow: str

    def identity_payload(self) -> dict[str, Any]:
        return {"precision": self.precision, "scale": self.scale,
                "rounding": self.rounding, "overflow": self.overflow}


@dataclass(frozen=True, slots=True)
class AuthoredOutputIntentV2:
    """What the author intended the output to be. Provisional; S5 resolves it.

    ``derived_from_proposal_hash`` is what makes this checkable rather than merely asserted: an
    intent claiming to describe a proposal names the proposal it was derived from, so an intent that
    travelled away from its formula can be caught instead of trusted.
    """

    unit: str | None
    additivity: AdditivityClass | None
    conversion_required: bool
    declared_conversion_ref: str
    target_currency: str | None
    numeric_shape: NumericShapeV2
    authored_expectation_present: bool
    derived_from_proposal_hash: str

    def __post_init__(self) -> None:
        if not self.derived_from_proposal_hash.strip():
            raise ValueError(
                "an output intent that does not name the proposal it was derived from cannot be "
                "checked against one: it would be a second declaration in all but name")
        if self.conversion_required != bool(self.declared_conversion_ref.strip()):
            raise ValueError(
                f"conversion_required={self.conversion_required} disagrees with "
                f"declared_conversion_ref={self.declared_conversion_ref!r}. These are two readings "
                f"of ONE structural fact — whether the formula declared a conversion — and letting "
                f"them differ is exactly the second declaration this type exists to prevent")
        if not self.authored_expectation_present and (
                self.unit is not None or self.additivity is not None
                or self.target_currency is not None):
            raise ValueError(
                "the intent carries authored values while recording that no authored expectation "
                "was present: one of the two is untrue, and a reader cannot tell which")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "unit": self.unit,
            "additivity": None if self.additivity is None else self.additivity.value,
            "conversion_required": self.conversion_required,
            "declared_conversion_ref": self.declared_conversion_ref,
            "target_currency": self.target_currency,
            "numeric_shape": self.numeric_shape.identity_payload(),
            "authored_expectation_present": self.authored_expectation_present,
            "derived_from_proposal_hash": self.derived_from_proposal_hash,
        }


def _shape(decimal: DecimalPolicy) -> NumericShapeV2:
    return NumericShapeV2(precision=decimal.precision, scale=decimal.scale,
                          rounding=str(decimal.rounding), overflow=str(decimal.overflow))


def _declared_conversion_ref(proposal: TypedFormulaProposalV3) -> str:
    """The conversion the FORMULA declared, over every expression.

    Read from the structure rather than from ``expected_output``, because whether a conversion was
    declared is a fact about the formula and not a guess about its result. Expressions are visited
    in body order and the first declared ref wins; a formula whose halves declared different
    conversions is a real defect, so it refuses rather than picking.
    """
    refs = {
        expression.authority_refs.currency_conversion_ref.strip()
        for expression in body_expressions_v3(proposal.body)
        if expression.authority_refs is not None
        and expression.authority_refs.currency_conversion_ref.strip()
    }
    if len(refs) > 1:
        raise ValueError(
            f"the formula declares {len(refs)} different currency conversions ({sorted(refs)}): its "
            f"halves would be converted by different policies and the result would be in neither "
            f"currency cleanly")
    return next(iter(refs), "")


def derive_output_intent_v2(
    proposal: TypedFormulaProposalV3, *, proposal_hash: str,
) -> AuthoredOutputIntentV2:
    """Derive the authored intent from ``proposal`` — the ONLY producer.

    Takes the proposal and nothing else, which is C-A7's gate: an intent derivable from the
    expectation alone cannot carry a value the expectation does not contain.

    Raises:
        ValueError: the formula declares more than one currency conversion.
    """
    expectation = proposal.expected_output
    present = expectation is not None
    conversion_ref = _declared_conversion_ref(proposal)

    additivity: AdditivityClass | None = None
    unit: str | None = None
    currency: str | None = None
    if present:
        unit = getattr(expectation, "unit", None)
        currency = getattr(expectation, "currency", None)
        # `ExpectedOutput` carries no additivity — it is not something a model is asked to guess,
        # and inventing a default here would put a value into the intent that nobody authored.
        additivity = None

    return AuthoredOutputIntentV2(
        unit=unit,
        additivity=additivity,
        conversion_required=bool(conversion_ref),
        declared_conversion_ref=conversion_ref,
        target_currency=currency,
        numeric_shape=_shape(proposal.decimal),
        authored_expectation_present=present,
        derived_from_proposal_hash=proposal_hash,
    )
