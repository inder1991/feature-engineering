"""S5 — resolving the authored intent into an EXECUTABLE output policy, or refusing by name.

C-A7 deliberately stopped short of this. A V3 authoring run is terminal at ``OUTPUT_INTENT_CAPTURED``
with **no** ``OUTPUT_POLICY_RESOLVED``, because "a stage that has not run must not be represented as
having run and agreed". This module is that stage: it takes what the author MEANT
(:class:`~featuregen.formula.output_intent_v2.AuthoredOutputIntentV2`, provisional by its own
docstring) and what the platform COMPUTED from governed facts
(:class:`~featuregen.formula.output_authority_v2.FormulaOutputPolicyV2`) and either produces the
executable policy or says exactly which of the two disagreed.

**The refusal compares ONLY fields the intent records — S5's acceptance, and it is a real
discriminator rather than a caution.** The intent keeps advisory and structural apart on purpose:
``unit``, ``additivity`` and ``target_currency`` come from the model's ``expected_output`` and are
``None`` when there was none, while ``conversion_required``, ``declared_conversion_ref`` and
``numeric_shape`` are read from the formula's own structure and are always present. A deterministic
run authors no expectation at all, so comparing ``unit`` there would refuse a formula against a value
**nobody authored** — the platform's own computation would be judged against a default. Worse, it
would do so invisibly: ``authored_expectation_present`` exists precisely because ``""`` collapses
"expected nothing" into "expected an empty string".

``additivity`` is the sharpest case and it is not hypothetical. ``derive_output_intent_v2`` never
populates it — ``ExpectedOutput`` carries none and inventing a default "would put a value into the
intent that nobody authored". So an additivity comparison would be **vacuous today and wrong the day
it stopped being vacuous**, which is why :data:`ADVISORY_FIELDS` names it alongside the other two
rather than special-casing it.

**The intent must name the formula it describes.** ``derived_from_proposal_hash`` exists so "an
intent that travelled away from its formula can be caught instead of trusted" — and nothing checked
it until here. An intent describing another proposal is refused before any comparison runs, because
every later disagreement would then be attributed to the wrong author.

**What is resolved, and what is merely carried.** The executable policy holds a currency CODE and,
separately, the ref that converted it. The declared policy's ``currency`` field holds
``"converted:<ref>"`` — a declaration — and lifting that string into ``currency_code`` is the exact
confusion :class:`~featuregen.materialize.bound_formula_v2.ExecutableOutputPolicyV2` refuses.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2
from featuregen.formula.output_intent_v2 import AuthoredOutputIntentV2
from featuregen.materialize.bound_formula_v2 import ExecutableOutputPolicyV2
from featuregen.materialize.physical_types_v2 import DecimalTypeV2

__all__ = [
    "ADVISORY_FIELDS",
    "INTENT_ADDITIVITY_MISMATCH",
    "INTENT_CONVERSION_MISMATCH",
    "INTENT_CURRENCY_MISMATCH",
    "INTENT_DESCRIBES_ANOTHER_FORMULA",
    "INTENT_NUMERIC_SHAPE_MISMATCH",
    "INTENT_UNIT_MISMATCH",
    "STRUCTURAL_FIELDS",
    "IntentMismatchV1",
    "compared_fields",
    "resolve_executable_output_v2",
]

INTENT_DESCRIBES_ANOTHER_FORMULA = "INTENT_DESCRIBES_ANOTHER_FORMULA"
INTENT_UNIT_MISMATCH = "INTENT_UNIT_MISMATCH"
INTENT_ADDITIVITY_MISMATCH = "INTENT_ADDITIVITY_MISMATCH"
INTENT_CURRENCY_MISMATCH = "INTENT_CURRENCY_MISMATCH"
INTENT_CONVERSION_MISMATCH = "INTENT_CONVERSION_MISMATCH"
INTENT_NUMERIC_SHAPE_MISMATCH = "INTENT_NUMERIC_SHAPE_MISMATCH"

#: Intent fields that exist only when an author expected something. Compared when recorded and
#: SKIPPED when not — never defaulted, because a default is a value nobody authored.
ADVISORY_FIELDS: tuple[str, ...] = ("unit", "additivity", "target_currency")

#: Intent fields read from the formula's own structure. Always present, so always compared.
STRUCTURAL_FIELDS: tuple[str, ...] = (
    "conversion_required", "declared_conversion_ref", "numeric_shape")


@dataclass(frozen=True, slots=True)
class IntentMismatchV1:
    """The author meant one thing and the governed facts say another — named, with both sides.

    RETURNED rather than raised, because a compilation collects verdicts and one disagreement about
    the output is not a reason to lose the rest of them.
    """

    code: str
    field: str
    intended: str
    resolved: str
    detail: str


def compared_fields(intent: AuthoredOutputIntentV2) -> tuple[str, ...]:
    """Exactly which intent fields this intent can be judged on.

    Exposed rather than kept private because S5's acceptance is a claim ABOUT this set, and a claim
    a caller cannot inspect is one a test can only approximate.
    """
    advisory = tuple(
        name for name in ADVISORY_FIELDS if getattr(intent, name) is not None)
    return advisory + STRUCTURAL_FIELDS


def _declared_conversion_ref(declared: FormulaOutputPolicyV2) -> str:
    """The ref inside a ``"converted:<ref>"`` declaration, or ``""``.

    The declared policy stores conversion as a prefixed string; the intent stores the ref on its
    own. Comparing the two forms directly would report a mismatch between two spellings of one fact.
    """
    currency = declared.currency
    return currency[len("converted:"):] if currency.startswith("converted:") else ""


def _fixed_currency_code(declared: FormulaOutputPolicyV2) -> str:
    """The code inside a ``"fixed:<CCY>"`` declaration, or ``""``."""
    currency = declared.currency
    return currency[len("fixed:"):] if currency.startswith("fixed:") else ""


def resolve_executable_output_v2(
    intent: AuthoredOutputIntentV2,
    declared: FormulaOutputPolicyV2,
    *,
    formula_content_hash: str,
    physical_type: DecimalTypeV2,
    currency_code: str,
    nullable: bool,
) -> ExecutableOutputPolicyV2 | IntentMismatchV1:
    """Resolve the intent against the governed policy, or refuse with the field that disagreed.

    Args:
        intent: what the author meant. Provisional until this call.
        declared: what ``resolve_output_v2`` computed from C1's governed operand facts.
        formula_content_hash: the formula being bound. Checked against the intent's own
            ``derived_from_proposal_hash`` FIRST — an intent describing another formula would make
            every later disagreement attributable to the wrong author.
        physical_type: the type the arithmetic actually produced (C-C6), which may legitimately
            differ from the declared decimal policy and is exactly what the numeric-shape comparison
            is for.
        currency_code: the resolved three-letter code. Passed in rather than parsed out of
            ``declared.currency``, because a CONVERTED result's code comes from the realization that
            performed the conversion (S4) and is not recoverable from the declaration.
        nullable: whether the produced column may be null.

    Returns:
        The executable policy, or the first :class:`IntentMismatchV1` in a fixed order — identity
        first, then structure, then the advisory fields the intent actually records.
    """
    if intent.derived_from_proposal_hash != formula_content_hash:
        return IntentMismatchV1(
            code=INTENT_DESCRIBES_ANOTHER_FORMULA, field="derived_from_proposal_hash",
            intended=intent.derived_from_proposal_hash, resolved=formula_content_hash,
            detail=("the intent was derived from a different formula than the one being bound. It "
                    "names the proposal it came from precisely so an intent that travelled away "
                    "from its formula is caught rather than trusted — every disagreement below it "
                    "would otherwise be attributed to an author who never saw this formula"))

    # ── structural: read from the formula, always present, always compared ───────────────────────
    resolved_ref = _declared_conversion_ref(declared)
    if intent.conversion_required != bool(resolved_ref):
        return IntentMismatchV1(
            code=INTENT_CONVERSION_MISMATCH, field="conversion_required",
            intended=str(intent.conversion_required), resolved=str(bool(resolved_ref)),
            detail=("the formula's declared conversion and the resolved output disagree about "
                    "whether a conversion happened at all. One of them is describing a different "
                    "computation, and publishing either would put a number in a currency nobody "
                    "can name"))
    if intent.declared_conversion_ref != resolved_ref:
        return IntentMismatchV1(
            code=INTENT_CONVERSION_MISMATCH, field="declared_conversion_ref",
            intended=intent.declared_conversion_ref, resolved=resolved_ref,
            detail=("the conversion policy the formula declared is not the one the resolved output "
                    "used. Two rate policies are two different numbers, and the difference is not "
                    "visible in the result"))

    intended_shape = DecimalTypeV2(precision=intent.numeric_shape.precision,
                                   scale=intent.numeric_shape.scale)
    if intended_shape != physical_type:
        return IntentMismatchV1(
            code=INTENT_NUMERIC_SHAPE_MISMATCH, field="numeric_shape",
            intended=str(intended_shape), resolved=str(physical_type),
            detail=("the decimal policy the formula declared is not the type its arithmetic "
                    "produces. Silently taking the produced type is how a declared DECIMAL(38,2) "
                    "becomes something that rounds differently in its last places, and taking the "
                    "declared one is how a value that does not fit is truncated"))

    # ── advisory: compared ONLY where the intent records something ───────────────────────────────
    if intent.unit is not None and intent.unit != declared.unit:
        return IntentMismatchV1(
            code=INTENT_UNIT_MISMATCH, field="unit",
            intended=intent.unit, resolved=declared.unit,
            detail=("the author expected a different unit than the governed operand facts produce. "
                    "This is compared because the author stated it — a run that stated none is not "
                    "judged against a default"))
    if (intent.additivity is not None
            and intent.additivity is not declared.output_additivity):
        return IntentMismatchV1(
            code=INTENT_ADDITIVITY_MISMATCH, field="additivity",
            intended=intent.additivity.value, resolved=declared.output_additivity.value,
            detail=("the author expected a different additivity class than the aggregation rules "
                    "produce, so a total built from this column would not mean what they thought"))
    if intent.target_currency is not None:
        resolved_currency = currency_code or _fixed_currency_code(declared)
        if intent.target_currency != resolved_currency:
            return IntentMismatchV1(
                code=INTENT_CURRENCY_MISMATCH, field="target_currency",
                intended=intent.target_currency, resolved=resolved_currency,
                detail=("the author asked for a target currency the resolved output is not in. "
                        "Publishing anyway would put one currency under another's name"))

    return ExecutableOutputPolicyV2(
        physical_type=str(physical_type),
        unit=declared.unit,
        currency_code=currency_code,
        conversion_policy_ref=resolved_ref,
        output_additivity=declared.output_additivity,
        nullable=nullable,
    )


def mismatch_payload(mismatch: IntentMismatchV1) -> Mapping[str, str]:
    """The mismatch as plain fields, for a caller that persists or reports it."""
    return {"code": mismatch.code, "field": mismatch.field, "intended": mismatch.intended,
            "resolved": mismatch.resolved, "detail": mismatch.detail}
