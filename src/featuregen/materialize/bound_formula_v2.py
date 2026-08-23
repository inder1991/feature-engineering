"""C-C6a — ``ExecutableOutputPolicyV2`` and ``BoundFormulaRevisionV2``, frozen as TYPES.

**Declared is not executable.** ``FormulaOutputPolicyV2.currency`` carries a DECLARATION —
``""``, ``"fixed:AED"`` or ``"converted:<policy ref>"``. That is what the formula asked for.
``ExecutableOutputPolicyV2`` carries what the number actually IS: a currency CODE, plus the policy
ref that converted it when one did. Keeping the declaration in the executable type would leave
``"converted:currency_conversion:foundation-base-currency"`` sitting in a field a consumer reads as
"the currency", and the answer to "what currency is this column in" would be a policy reference.

**A compiler bump must not move the bound-formula hash (S5's acceptance).** ``compiler_version`` is
therefore PROVENANCE and sits outside :meth:`identity_payload`, exactly as ``authoring_run_id`` does
on the execution IR: recompiling the same formula against the same inputs is one bound revision, and
letting the toolchain in would invalidate every downstream pin on a version bump that changed
nothing about the computation.

**Composed by HASH, so C-B3 can be constructible from hashes alone.**
``BoundFormulaRevisionV2`` names its executable output policy by content hash rather than holding
the object, which is what lets ``ExecutableFeatureRevisionV2`` be defined over opaque hashes here
while S5 produces the instances.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.formula.schema_leaves import AdditivityClass
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.physical_types_v2 import PHYSICAL_TYPE_POLICY_V2

__all__ = [
    "BoundFormulaRevisionV2",
    "ExecutableOutputPolicyV2",
    "bound_formula_hash_v2",
    "executable_output_hash_v2",
]

#: ISO-4217-shaped: three upper-case letters. Checked as a SHAPE, because the executable policy's
#: whole job is to hold a currency rather than a reference to one, and `"converted:..."` slipping
#: into this field is precisely the confusion the type exists to prevent.
_CURRENCY_CODE_LENGTH = 3


@dataclass(frozen=True, slots=True)
class ExecutableOutputPolicyV2:
    """What the published column IS — resolved, not declared.

    ``conversion_policy_ref`` is empty when no conversion happened and names the governing policy
    when one did. It is a separate field from ``currency_code`` on purpose: "this column is in AED"
    and "this column was converted to AED by policy P" are different facts, and a consumer
    reconciling a total needs both.
    """

    physical_type: str
    unit: str
    currency_code: str
    conversion_policy_ref: str
    output_additivity: AdditivityClass
    nullable: bool
    physical_type_policy: str = PHYSICAL_TYPE_POLICY_V2

    def __post_init__(self) -> None:
        if not self.physical_type.strip():
            raise ValueError(
                "an executable output policy with no physical type is not executable: the column "
                "it describes has no type to create")
        if self.currency_code:
            if (len(self.currency_code) != _CURRENCY_CODE_LENGTH
                    or not self.currency_code.isalpha()
                    or self.currency_code != self.currency_code.upper()):
                raise ValueError(
                    f"currency_code={self.currency_code!r} is not a three-letter currency code. "
                    f"This field holds what the number IS in; a declaration such as "
                    f"'converted:<ref>' belongs in `conversion_policy_ref`, and letting one through "
                    f"here would make the answer to 'what currency is this column in' a policy "
                    f"reference")
        elif self.conversion_policy_ref.strip():
            raise ValueError(
                f"a conversion by {self.conversion_policy_ref!r} produced no currency code: a "
                f"conversion whose result currency is unknown converted to nothing nameable")
        if self.unit == "monetary" and not self.currency_code:
            raise ValueError(
                "a monetary column with no currency code cannot be summed with anything or "
                "compared to anything — the unit says it is money and nothing says which money")

    @property
    def was_converted(self) -> bool:
        """Whether a conversion policy produced this currency — not whether one was DECLARED."""
        return bool(self.conversion_policy_ref.strip())

    def identity_payload(self) -> dict[str, Any]:
        return {
            "physical_type": self.physical_type,
            "unit": self.unit,
            "currency_code": self.currency_code,
            "conversion_policy_ref": self.conversion_policy_ref,
            "output_additivity": self.output_additivity.value,
            "nullable": self.nullable,
            "physical_type_policy": self.physical_type_policy,
        }


def executable_output_hash_v2(policy: ExecutableOutputPolicyV2) -> str:
    """The executable output policy's content identity."""
    return materialize_hash(policy.identity_payload())


@dataclass(frozen=True, slots=True)
class BoundFormulaRevisionV2:
    """One formula BOUND to a specific input set in a specific environment.

    ``compiler_version`` is deliberately outside :meth:`identity_payload` — see the module
    docstring. Everything else is identity: the same formula bound to different inputs, or to the
    same inputs in a different environment, is a different bound revision, because those are the
    facts that decide what the computation reads.
    """

    formula_content_hash: str
    bound_input_set_hash: str
    environment_id: str
    executable_output_hash: str
    compiler_version: str

    def __post_init__(self) -> None:
        for value, what in (
            (self.formula_content_hash, "formula_content_hash"),
            (self.bound_input_set_hash, "bound_input_set_hash"),
            (self.environment_id, "environment_id"),
            (self.executable_output_hash, "executable_output_hash"),
        ):
            if not value.strip():
                raise ValueError(
                    f"a bound formula revision with a blank {what} is not bound to anything: it "
                    f"would hash the same as every other revision missing that fact")

    def identity_payload(self) -> dict[str, Any]:
        """What this bound revision IS. NO compiler version — S5's acceptance."""
        return {
            "formula_content_hash": self.formula_content_hash,
            "bound_input_set_hash": self.bound_input_set_hash,
            "environment_id": self.environment_id,
            "executable_output_hash": self.executable_output_hash,
        }


def bound_formula_hash_v2(revision: BoundFormulaRevisionV2) -> str:
    """The bound revision's content identity — stable across compiler versions."""
    return materialize_hash(revision.identity_payload())
