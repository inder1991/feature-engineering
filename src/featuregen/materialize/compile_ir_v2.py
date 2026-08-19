"""Compile an admitted V3 formula into its executable plan — the missing link.

**Nothing in production has ever built a `FormulaExecutionIRV2`.** Every V2 test constructs one by
wrapping expressions compiled by the *V1* compiler, which proves the downstream types can carry V1
expressions and proves nothing about compiling a V2 formula. This is the function that was absent
between "admitted" and "renderable".

**What is REUSED and what is new.** The expression compiler is V1's and stays V1's: physical
resolution, governed availability, the traversal to the grain, the point-in-time window — none of it
carries formula grammar, and re-implementing it here would be a second, drifting copy of the rules
`compile_expression` already enforces. What is new is everything V2 added: row selections, declared
policies resolved to *executable content*, and an output policy resolved through V2's own authority.

**Refusals are RETURNED, not raised**, and the FIRST failure decides the code. One refused feature is
one governed verdict among the many a compilation collects; raising would let the first bad feature
hide every other verdict in the group.

**Nothing is defaulted.** A declared policy with no stored content refuses by name rather than
rendering without it — the whole reason the payload store exists. A candidate with no declared grain
refuses rather than borrowing its operands. An aggregate the renderer cannot emit refuses rather than
compiling into something that cannot be rendered.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from featuregen.contracts.db import DbConn
from featuregen.formula.policy_payloads import (
    PolicyPayloadUnavailable,
    resolve_executable_policy,
)
from featuregen.formula.schema_v2 import FinalOperationV2
from featuregen.materialize.admission_v2 import AdmittedFeatureV2
from featuregen.materialize.boundary_v2 import FormulaExecutionIRV2
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import BODY_PATHS, compile_expression
from featuregen.materialize.inventory import ClusterInventoryV1
from featuregen.materialize.ir import _fold

__all__ = ["compile_ir_v2"]

#: Which body path each final operation's expressions occupy, in ORDER. The renderer's own
#: `_BODY_SLOTS` is the authority for what it can emit; this is the compiler's side of the same
#: agreement, and the two are checked against each other rather than assumed to match.
_BODY_PATHS_BY_OPERATION: Mapping[FinalOperationV2, tuple[str, ...]] = {
    FinalOperationV2.IDENTITY: ("body.expr",),
    FinalOperationV2.RATIO: ("body.numerator", "body.denominator"),
    FinalOperationV2.DIFFERENCE: ("body.minuend", "body.subtrahend"),
}


def compile_ir_v2(
    conn: DbConn,
    admitted: AdmittedFeatureV2,
    *,
    spine,
    inventory: ClusterInventoryV1,
    roles: Iterable[str] = (),
    policy_realization_ids: Mapping[str, str] = (),
) -> FormulaExecutionIRV2 | MaterializationRefused:
    """Compile ONE admitted V3 feature into its executable plan, or refuse it.

    Args:
        policy_realization_ids: declared policy ref → the realization revision that decides it.
            Every policy the formula declares must appear here and resolve to stored executable
            content. A missing entry is a refusal, not a policy that does not apply: the formula
            SAID it applies, and rendering without it would produce a number computed under rules
            nobody wrote.

    Returns:
        The compiled IR, or a ``MaterializationRefused`` carrying the first failing check's code.
    """
    proposal = admitted.proposal
    body = proposal.body
    final = _final_operation(body)

    # ── 1. THE FINAL OPERATION MUST HAVE SOMEWHERE TO PUT ITS TERMS ─────────────────────────────
    # `signed_sum` has no body-path spelling anywhere in the codebase: BODY_PATHS is the closed
    # five-member set v1 froze, and there is no path for N signed terms. Compiling one would
    # produce an IR whose expressions sit at paths the renderer cannot look up, which surfaces as a
    # KeyError deep in rendering rather than as a verdict about the feature.
    paths = _BODY_PATHS_BY_OPERATION.get(final)
    if paths is None:
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"{final} has no body-path spelling in this build: its terms have nowhere to be "
            f"staged, so there is nothing for the renderer to read. Supporting it is a vocabulary "
            f"change, not a compilation one")

    expressions_in = _expressions(body)
    if len(expressions_in) != len(paths):
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"{final} combines {len(paths)} term(s) and this formula carries "
            f"{len(expressions_in)}: an arity the operation does not have would compile to a "
            f"different calculation from the one the formula declares")

    # ── 2. THE GRAIN, WHICH IS NOT OPTIONAL ─────────────────────────────────────────────────────
    grain = getattr(proposal, "grain", None)
    grain_keys = tuple(getattr(grain, "keys", ()) or ())
    if not grain_keys:
        return MaterializationRefused(
            CompilationRefusalCode.GRAIN_NOT_RESOLVED,
            f"feature {admitted.feature_name!r} declares no grain keys, so what it is computed PER "
            f"is unknown. Compiling it would land rows on a population nobody chose")

    # ── 3. DECLARED POLICIES RESOLVE TO EXECUTABLE CONTENT, OR REFUSE ───────────────────────────
    # BEFORE the expressions, deliberately. This asks only what the formula DECLARES — no catalog,
    # no physical resolution, no cluster — so a formula whose policies cannot be resolved is refused
    # cheaply and for the reason that actually blocks it. Running it after expression compilation
    # would report a physical-resolution code for a feature whose real problem is an unbound policy,
    # and send whoever reads the refusal to the wrong place.
    policies = _resolve_policies(conn, proposal, dict(policy_realization_ids or {}))
    if isinstance(policies, MaterializationRefused):
        return policies

    # ── 4. EVERY EXPRESSION, IN THE BODY'S OWN PATH ORDER ───────────────────────────────────────
    # Order matters and is not sorted: `body.numerator` before `body.denominator` is the difference
    # between a/b and b/a, and the renderer reads these by path.
    compiled = []
    for path, expression in zip(paths, expressions_in, strict=True):
        if path not in BODY_PATHS:
            return MaterializationRefused(
                CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
                f"body path {path!r} is outside the compiler's vocabulary: the path names the "
                f"staging output this expression produces, and one nothing else reads is not a "
                f"path")
        result = compile_expression(
            conn, expr_path=path, expr=expression, grain_keys=grain_keys,
            roles=roles, inventory=inventory)
        if isinstance(result, MaterializationRefused):
            return result                      # the FIRST failure decides the code
        compiled.append(result)

    # ── 5. THE OUTPUT POLICY, THROUGH V2's OWN AUTHORITY ────────────────────────────────────────
    output = getattr(proposal, "expected_output", None) or getattr(proposal, "output", None)
    if output is None:
        return MaterializationRefused(
            CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
            f"feature {admitted.feature_name!r} resolved no output policy: without one the "
            f"published column has no declared unit, currency or additivity, and a consumer would "
            f"have to guess what the number means")

    return FormulaExecutionIRV2(
        feature_name=admitted.feature_name,
        formula_content_hash=admitted.proposal_content_hash,
        final_operation=final,
        zero_denominator=getattr(body, "zero_denominator", None),
        grain_entity=getattr(grain, "entity", ""),
        # FOLDED, for `compile_ir`'s stated reason: `formula_content_hash` folds case, so a raw
        # spelling here would fork one governed formula into two ir_hashes — and the renderer
        # compares these against the folded expression read set and the lowered landing keys, which
        # a raw spelling matches neither of.
        grain_keys=tuple(_fold(key) for key in grain_keys),
        expressions=tuple(compiled),
        row_selections=tuple(getattr(proposal, "row_selections", ()) or ()),
        policies=policies,
        spine=spine,
        output_policy=output,
        authoring_run_id=admitted.authoring_run_id)


def _resolve_policies(
    conn: DbConn, proposal, realization_ids: Mapping[str, str],
) -> tuple | MaterializationRefused:
    """Every policy the formula DECLARES, resolved to stored executable content.

    A hash names a decision; it is not the decision. A realization pointing at content nobody stored
    cannot be rendered, and rendering it anyway would apply a policy nobody wrote — so an
    unresolvable policy refuses here rather than becoming a default at render time.

    A declared policy with no realization at all is the same refusal wearing a different hat: the
    formula said the policy applies, and nothing says what it is.
    """
    declared = _declared_policy_refs(proposal)
    resolved = []
    for ref in declared:
        realization_id = realization_ids.get(ref)
        if not realization_id:
            return MaterializationRefused(
                CompilationRefusalCode.POLICY_REFERENCE_UNRESOLVABLE,
                f"the formula declares policy {ref!r} and nothing says which realization decides "
                f"it: the feature claims a governed rule that has not been bound to a decision")
        try:
            resolved.append(resolve_executable_policy(
                conn, realization_revision_id=realization_id))
        except PolicyPayloadUnavailable as exc:
            return MaterializationRefused(
                CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
                f"policy {ref!r} cannot be rendered: {exc}")
    return tuple(resolved)


def _declared_policy_refs(proposal) -> tuple[str, ...]:
    """Which policies this formula says apply, in a stable order.

    Read off the expressions' authority refs rather than from a separate list, so a formula cannot
    declare a policy in one place and not the other — the two would disagree the first time one was
    edited.
    """
    refs: list[str] = []
    for expression in _expressions(proposal.body):
        authority = getattr(expression, "authority_refs", None)
        if authority is None:
            continue
        for name in ("status_policy_ref", "direction_policy_ref", "reversal_policy_ref",
                     "currency_conversion_ref"):
            value = (getattr(authority, name, "") or "").strip()
            if value and value not in refs:
                refs.append(value)
    return tuple(refs)


def _final_operation(body) -> FinalOperationV2:
    raw = getattr(body, "final_operation", FinalOperationV2.IDENTITY)
    return raw if isinstance(raw, FinalOperationV2) else FinalOperationV2(str(raw))


def _expressions(body) -> tuple:
    """The body's expressions, through the v2 vocabulary's own walker — never a second traversal."""
    from featuregen.formula.schema_v2 import body_expressions_v2

    return tuple(body_expressions_v2(body))
