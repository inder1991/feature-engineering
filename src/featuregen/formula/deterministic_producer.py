"""C-A3b/C-A5 — the DETERMINISTIC authoring producer: a reviewed blueprint becomes a v3 proposal.

**Why this exists.** ``recipe_formula_worker`` sends every v2 recipe run through
``run_authoring_v2_replay``, which drives a provider. But a reviewed recipe blueprint already
determines the formula completely: the aggregation, the operand, the window, the policy references
and — since C-A3b — the semantic row selection are all bound facts. Asking a model to restate
arithmetic the registry already holds spends a provider call to re-derive a known answer, and
invites it to drift from the very expectation the run is then validated against.

**What this is NOT.** It is not a second admission path. The proposal it builds goes through the
SAME ``parse_proposal_v3`` gate as any authored one — shape, then construction, then semantics —
so a blueprint that cannot produce a legal formula fails here exactly as a model's would. Nothing
downstream can tell a deterministic proposal from an authored one by its shape, only by its
recorded review.

**Review, stated honestly.** No critic runs, so no ``critic_status`` is recorded: a
:class:`~featuregen.formula.result_v2.ReviewedBlueprintBypassV2` names the blueprint revision and
expectation hash this stood on. ``"clean"`` would say the critic ran and found nothing, which is
false, and ``derive_disposition_v2`` will refuse the bypass unless the caller can name the same
expectation the run was opened for — so neutrality is checked, not asserted.

**The window's LENGTH comes from the BOUND expectation, not the blueprint's parameter name.**
``WindowPolicyExpectationV2.length_parameter`` is a parameter REFERENCE (``"window_days"``); the
bound expectation carries the resolved ``window_length``. Reading the reference where the value
belongs would put a parameter name into a formula's identity.
"""
from __future__ import annotations

from typing import Any

from featuregen.formula.parse_v3 import parse_proposal_v3
from featuregen.formula.schema_v3 import (
    CANONICALIZATION_VERSION_V3,
    FORMULA_SCHEMA_VERSION_V3,
    OPERATION_GRAMMAR_VERSION_V3,
    TypedFormulaProposalV3,
)

__all__ = [
    "DeterministicAuthoringRefused",
    "bypass_for",
    "proposal_from_bound_expectation",
]


class DeterministicAuthoringRefused(Exception):
    """The blueprint cannot produce a legal v3 proposal — named, never silently degraded.

    A refusal here is a REGISTRY defect (a blueprint that does not describe a formula), which is
    exactly the class of problem the deterministic path exists to surface early instead of paying a
    provider call to discover.
    """


def _expression(expectation: Any) -> dict:
    """One ``BoundExpressionExpectationV2`` as the raw v3 expression dict.

    Built as a DICT rather than the dataclass directly so it goes through ``parse_proposal_v3`` —
    the same JSON-Schema-then-semantics gate an authored proposal passes. Constructing the typed
    object here would skip the shape gate and let a malformed blueprint through a door a model
    could not use.
    """
    window = expectation.window
    return {
        "aggregation": str(expectation.aggregation),
        "operand": expectation.operand_ref,
        "second_operand": expectation.second_operand_ref,
        "source_relation": {"table_ref": expectation.source_relation_ref},
        # A reviewed blueprint declares row selection STRUCTURALLY (C-A3b); it never carries a
        # physical filter, and `SELECTION_FILTER_CONFLICT` refuses one beside a selection anyway.
        "filter": None,
        "window": {
            "event_time_ref": expectation.event_time_ref,
            "basis": str(window.basis),
            # the RESOLVED length, not `window.length_parameter` — see the module docstring
            "length": expectation.window_length,
            "unit": str(window.unit),
            "start_inclusive": str(window.start_inclusive),
            "end_inclusive": str(window.end_inclusive),
            "timezone": window.timezone,
            "empty_window": str(window.empty_window),
            "null_input": str(window.null_input),
            "offset_periods": window.offset_periods,
        },
        "aggregation_argument": expectation.aggregation_argument,
        "authority_refs": (None if expectation.authority_refs is None else {
            "status_policy_ref": expectation.authority_refs.status_policy_ref,
            "direction_policy_ref": expectation.authority_refs.direction_policy_ref,
            "reversal_policy_ref": expectation.authority_refs.reversal_policy_ref,
            "currency_conversion_ref": expectation.authority_refs.currency_conversion_ref,
        }),
        "row_selections": [
            {"kind": str(sel.kind), "role": sel.role, "semantic_value": sel.semantic_value}
            for sel in expectation.row_selections
        ],
    }


def _body(bound: Any) -> dict:
    """The v3 body for the bound expectation's ``final_operation``.

    The arity check is the honest one: a ratio needs exactly two expressions and a unary exactly
    one, so a blueprint whose expression count disagrees with its own final operation is a registry
    defect, not something to paper over by taking the first N.
    """
    final = str(bound.final_operation)
    expressions = bound.expressions

    def need(count: int) -> None:
        if len(expressions) != count:
            raise DeterministicAuthoringRefused(
                f"final_operation {final!r} needs exactly {count} expression(s), "
                f"the bound blueprint carries {len(expressions)}")

    if final == "signed_sum":
        if not expressions:
            raise DeterministicAuthoringRefused("a signed sum needs at least one term")
        return {"final_operation": final, "terms": [
            {"name": e.term_name, "sign": e.term_sign, "expr": _expression(e)}
            for e in expressions]}
    if final == "identity":
        need(1)
        return {"final_operation": final, "expr": _expression(expressions[0])}
    if final == "ratio":
        need(2)
        # `zero_denominator` is NOT a blueprint field today; a ratio recipe must declare it before
        # this path can author one, and saying so is better than inventing a division policy.
        raise DeterministicAuthoringRefused(
            "ratio bodies need a declared zero_denominator, which the bound expectation does not "
            "carry — declare it on the blueprint before authoring a ratio deterministically")
    if final == "difference":
        need(2)
        return {"final_operation": final, "minuend": _expression(expressions[0]),
                "subtrahend": _expression(expressions[1])}
    raise DeterministicAuthoringRefused(f"unsupported final_operation {final!r}")


def proposal_from_bound_expectation(bound: Any) -> TypedFormulaProposalV3:
    """Build the v3 proposal a reviewed, BOUND blueprint determines — with no provider call.

    Raises:
        DeterministicAuthoringRefused: the blueprint does not determine a legal formula.
    """
    raw = {
        "formula_schema_version": FORMULA_SCHEMA_VERSION_V3,
        "operation_grammar_version": OPERATION_GRAMMAR_VERSION_V3,
        "canonicalization_version": CANONICALIZATION_VERSION_V3,
        "grain": {"entity": bound.grain_entity, "keys": list(bound.grain_key_refs)},
        "body": _body(bound),
        # A deterministic proposal declares NO parameters: every parameter the recipe had is
        # already RESOLVED into the bound expectation (that is what "bound" means), and re-declaring
        # them would put an unbound name into the formula's identity.
        "parameters": [],
        "decimal": {
            "precision": bound.decimal.precision,
            "scale": bound.decimal.scale,
            "rounding": str(bound.decimal.rounding),
            "overflow": str(bound.decimal.overflow),
        },
        # The model's ADVISORY guess, which a deterministic run has no opinion to offer. Authority
        # comes from the C1 output stage either way; a fabricated expectation here would be compared
        # against that authority and could only ever manufacture a spurious mismatch.
        "expected_output": None,
        "allocation_policy_ref": bound.allocation_policy_ref,
    }
    try:
        return parse_proposal_v3(raw)
    except Exception as exc:                      # SchemaError and anything the builders raise
        raise DeterministicAuthoringRefused(
            f"the bound blueprint does not determine a legal v3 formula: {exc}") from exc


def bypass_for(bound: Any):
    """The review outcome for a deterministic run over ``bound``.

    Both fields come from the bound expectation itself, so the bypass names the exact artifact it
    stood on rather than a value a caller chose — which is what lets ``derive_disposition_v2``
    check coverage instead of trusting it.
    """
    from featuregen.formula.result_v2 import ReviewedBlueprintBypassV2

    return ReviewedBlueprintBypassV2(
        blueprint_revision=bound.recipe_candidate_key,
        expectation_hash=bound.blueprint_content_hash,
    )
