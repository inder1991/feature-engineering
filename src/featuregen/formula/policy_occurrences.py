"""C-C7 — ``derive_policy_occurrences``: which policies a formula needs, and WHERE.

**Why not ``required_policy_kinds``.** That function answers from a formula's SHAPE: *filtered ⇒
needs eligible_status AND reversal_correction*. It is a reasonable approximation and it is wrong in
both directions. A formula filtered on ``country_code`` gets told it needs a reversal policy, which
has nothing to do with countries; a formula with no filter but a per-row-currency operand gets told
it needs nothing. Shape cannot tell those apart because shape does not know what the columns MEAN.
(It is also, today, unwired — nothing in ``src/`` calls it.)

An OCCURRENCE is the opposite: it exists because a specific thing in a specific expression needs a
specific policy applied to a specific bound column. That is what makes it durable and checkable —
"this formula needs a reversal policy" cannot be verified against anything, while "expression
``body.expr`` needs ``reversal_correction`` applied to ``hdfc::public.txns.txn_amt`` in
``hdfc-local``" names every part of its own proof.

**Two questions, kept apart.** :func:`required_policy_kinds_v2` asks what an expression NEEDS, from
bound facts. :func:`derive_policy_occurrences` records what it DECLARED, bound to physical columns.
Comparing them is a caller's job (and C-C4 already refuses a declaration with no read) — folding
them here would make an unmet need indistinguishable from an undeclared one.

**Occurrences carry no realization.** Which physical value ``debit`` has, and which rate table
serves a conversion, is C-C8's. An occurrence says a policy is needed HERE; a realization says what
applying it does.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.canonical import jcs_sha256
from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.schema_v3 import (
    _REQUIRED_POLICY_REF,
    AggregateExpressionV3,
)
from featuregen.overlay.upload.banking_policies import AUTHORITY_REF_KINDS, parse_policy_ref

__all__ = [
    "PolicyOccurrenceSetV1",
    "PolicyOccurrenceV1",
    "derive_policy_occurrences",
    "occurrence_set_hash",
    "required_policy_kinds_v2",
]

#: ``AuthorityRefsV2`` field → the SEMANTIC ROLE the policy plays at an occurrence. Separate from
#: the field name because the field is where the reference was written and the role is what the
#: policy DOES — C-C8 keys realization families on the role, so a rename of one must not silently
#: become a change to the other.
_SEMANTIC_ROLE: Mapping[str, str] = {
    "status_policy_ref": "status",
    "direction_policy_ref": "direction",
    "reversal_policy_ref": "reversal",
    "currency_conversion_ref": "currency_conversion",
    "allocation_policy_ref": "allocation",
}


def required_policy_kinds_v2(
    expression: AggregateExpressionV3, operand_facts: OperandFactsV2,
) -> frozenset[str]:
    """Which policy kinds THIS expression genuinely needs, from bound facts and declarations.

    Deliberately narrow, and the two clauses C-C7's gate names are both consequences of that:

    * **A filter requires nothing.** A predicate on ``country_code`` is a filter and needs no
      reversal policy; the schema cannot see which column a filter touches (which is why
      ``SELECTION_FILTER_CONFLICT`` exists), so inferring a policy need from a filter's mere
      presence would be inventing a requirement from something unreadable.
    * **Currency conversion is required only by the OPERAND'S FACTS.** An operand whose C1 facts
      are not ``monetary``/``per_row`` needs no currency occurrence — a count of rows, or an amount
      in a single fixed currency, has nothing to convert.

    A row selection requires the kind that resolves it, because a selection without its policy is
    an intent nothing can execute — the same pairing ``_REQUIRED_POLICY_REF`` enforces at the
    schema layer, restated here over bound facts rather than over the wire shape.
    """
    kinds: set[str] = set()
    for selection in expression.row_selections:
        field = _REQUIRED_POLICY_REF[selection.kind]
        kinds.add(AUTHORITY_REF_KINDS[field])
    if operand_facts.unit == "monetary" and operand_facts.currency == "per_row":
        kinds.add(AUTHORITY_REF_KINDS["currency_conversion_ref"])
    return frozenset(kinds)


@dataclass(frozen=True, slots=True)
class PolicyOccurrenceV1:
    """ONE place a governed policy applies, named completely enough to be proved or refuted."""

    expr_path: str
    policy_ref_field: str
    policy_kind: str
    policy_ref: str
    semantic_role: str
    bound_dataset: str
    bound_column: str
    environment_id: str

    def __post_init__(self) -> None:
        for value, what in (
            (self.expr_path, "expr_path"), (self.policy_ref_field, "policy_ref_field"),
            (self.policy_kind, "policy_kind"), (self.policy_ref, "policy_ref"),
            (self.semantic_role, "semantic_role"), (self.bound_dataset, "bound_dataset"),
            (self.environment_id, "environment_id"),
        ):
            if not value.strip():
                raise ValueError(
                    f"a policy occurrence with a blank {what} names no place: an occurrence whose "
                    f"own location is unknown can be neither realized nor refuted")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "expr_path": self.expr_path,
            "policy_ref_field": self.policy_ref_field,
            "policy_kind": self.policy_kind,
            "policy_ref": self.policy_ref,
            "semantic_role": self.semantic_role,
            "bound_dataset": self.bound_dataset,
            "bound_column": self.bound_column,
            "environment_id": self.environment_id,
        }

    @property
    def occurrence_hash(self) -> str:
        """Content identity. Two occurrences of the same policy on the same bound column in the
        same environment ARE one occurrence — that is what makes the set deduplicable."""
        return jcs_sha256(self.identity_payload())


@dataclass(frozen=True, slots=True)
class PolicyOccurrenceSetV1:
    """Every occurrence one formula produces, ordered by occurrence hash.

    Ordered by HASH rather than by the sequence expressions were walked in, for the reason Task 6
    found one level down: ordering by discovery order makes identity depend on a walk, and a walk
    changes for reasons that are not about the formula.
    """

    occurrences: tuple[PolicyOccurrenceV1, ...]

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.occurrences, key=lambda o: o.occurrence_hash))
        seen = [o.occurrence_hash for o in ordered]
        if len(set(seen)) != len(seen):
            raise ValueError(
                "the occurrence set carries the same occurrence twice: an occurrence is identified "
                "by policy, bound column and environment, so a duplicate is one place counted "
                "twice and would make a realization look doubly required")
        object.__setattr__(self, "occurrences", ordered)

    def kinds(self) -> frozenset[str]:
        return frozenset(occurrence.policy_kind for occurrence in self.occurrences)

    def for_expression(self, expr_path: str) -> tuple[PolicyOccurrenceV1, ...]:
        return tuple(o for o in self.occurrences if o.expr_path == expr_path)

    def identity_payload(self) -> dict[str, Any]:
        return {"occurrences": [o.identity_payload() for o in self.occurrences]}


def occurrence_set_hash(occurrences: PolicyOccurrenceSetV1) -> str:
    """The set's durable identity."""
    return jcs_sha256(occurrences.identity_payload())


def derive_policy_occurrences(
    expressions: Mapping[str, AggregateExpressionV3],
    *,
    bound_datasets: Mapping[str, str],
    environment_id: str,
) -> PolicyOccurrenceSetV1:
    """Every policy occurrence the given expressions DECLARE, bound to physical datasets.

    Args:
        expressions: body path → expression, so every occurrence can name where it came from.
        bound_datasets: body path → the physical dataset that expression reads. Passed in rather
            than read off the expression, because binding a logical relation to a physical dataset
            is the generation inventory's job (S3) and an occurrence that guessed would name a
            dataset nobody bound.
        environment_id: occurrences are per-environment — the same policy over the same column in
            two environments is two occurrences, because the physical dataset behind them differs.

    Returns:
        A :class:`PolicyOccurrenceSetV1`, deduplicated and ordered by occurrence hash.
    """
    if not environment_id.strip():
        raise ValueError(
            "policy occurrences are per-environment: without one, an occurrence would claim to "
            "hold everywhere, and the realization it demands is bound to a specific cluster")

    found: dict[str, PolicyOccurrenceV1] = {}
    for expr_path, expression in expressions.items():
        dataset = bound_datasets.get(expr_path, "")
        if not dataset.strip():
            raise ValueError(
                f"{expr_path} has no bound dataset: an occurrence names the physical dataset its "
                f"policy applies to, and one that could not would be a requirement with nowhere "
                f"to be met")
        refs = expression.authority_refs
        if refs is None:
            continue
        for field, role in _SEMANTIC_ROLE.items():
            ref = getattr(refs, field, "")
            if not ref.strip():
                continue
            kind, _name = parse_policy_ref(ref)
            occurrence = PolicyOccurrenceV1(
                expr_path=expr_path, policy_ref_field=field, policy_kind=kind, policy_ref=ref,
                semantic_role=role, bound_dataset=dataset,
                bound_column=expression.operand or "", environment_id=environment_id)
            found[occurrence.occurrence_hash] = occurrence

    return PolicyOccurrenceSetV1(occurrences=tuple(found.values()))
