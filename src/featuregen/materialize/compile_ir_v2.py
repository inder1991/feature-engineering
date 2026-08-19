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

**The output is a PLANNED IR, not a bare one.** Which physical columns a policy reads is knowable
only once the policy is resolved, and that happens here — so this is the one place that can derive
the complete read set. Returning a bare IR would leave the next stage to re-derive reads from
content it does not hold, and Gate 2 would authorize a compilation narrower than the run performs.

**The DECLARATION is identity, the RESOLVED CONTENT is not.** The IR carries what each expression
declared, verbatim: a formula with a reversal policy is a different formula from one without.
Resolved payloads never enter it — baking one environment's realization into a feature's identity
would make the same governed formula two different features in two environments, and re-pointing a
realization would silently re-identify every feature that used it.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping

from featuregen.contracts.db import DbConn
from featuregen.formula.policy_payloads import (
    CurrencyConversionPayloadV1,
    DirectionPayloadV1,
    EligibleStatusPayloadV1,
    PolicyPayloadUnavailable,
    ReversalPayloadV1,
    resolve_executable_policy,
)
from featuregen.formula.schema_v2 import FinalOperationV2
from featuregen.materialize.admission_v2 import AdmittedFeatureV2
from featuregen.materialize.boundary_v2 import (
    DeclaredPoliciesV2,
    FormulaExecutionIRV2,
    KnowledgeTimeBasisV2,
    PlannedFormulaExecutionIRV2,
    PolicyReadV2,
    SelectedRowsV2,
    TemporalReadV2,
)
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
    output_policy,
    roles: Iterable[str] = (),
    policy_realization_ids: Mapping[str, str] = (),
) -> PlannedFormulaExecutionIRV2 | MaterializationRefused:
    """Compile ONE admitted V3 feature into its PLANNED executable plan, or refuse it.

    Returns a *planned* IR rather than a bare one, because C-C2's ordering says a read set is
    derived once and paired with the IR it belongs to — and because the physical columns a policy
    reads are only knowable once the policy is resolved, which happens here. Handing back a bare IR
    would leave the next stage to re-derive the reads from content it does not have.

    Args:
        output_policy: the RESOLVED output policy, from `resolve_output_v2`. Required, and
            deliberately not read off ``proposal.expected_output``: that field is what the author
            EXPECTED, and output authority is what the operands' governed units and declared
            conversions actually permit. Substituting the expectation for the authority is how a
            feature comes to claim a unit nobody established.
        policy_realization_ids: declared policy ref → the realization revision that decides it.
            Every policy the formula declares must appear here and resolve to stored executable
            content. A missing entry is a refusal, not a policy that does not apply: the formula
            SAID it applies, and rendering without it would produce a number computed under rules
            nobody wrote.

    Returns:
        The planned IR, or a ``MaterializationRefused`` carrying the first failing check's code.
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
    #
    # The DECLARATION is what the IR carries (it is identity-bearing: a formula with a reversal
    # policy is a different formula from one without). The RESOLVED CONTENT never enters the IR —
    # it is how the physical reads are derived, and baking one environment's realization into a
    # feature's identity would make the same governed formula two different features in two
    # environments.
    declared = _declared_policies(paths, expressions_in)
    policy_reads = _policy_reads(conn, declared, dict(policy_realization_ids or {}))
    if isinstance(policy_reads, MaterializationRefused):
        return policy_reads

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

    # ── 5. THE OUTPUT POLICY, RESOLVED BY V2's OWN AUTHORITY AND PASSED IN ──────────────────────
    if output_policy is None:
        return MaterializationRefused(
            CompilationRefusalCode.OUTPUT_TYPE_NOT_GOVERNED,
            f"feature {admitted.feature_name!r} has no resolved output policy: without one the "
            f"published column has no established unit, currency or additivity, and a consumer "
            f"would have to guess what the number means")

    ir = FormulaExecutionIRV2(
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
        row_selections=_row_selections(paths, expressions_in),
        policies=declared,
        spine=spine,
        output_policy=output_policy,
        authoring_run_id=admitted.authoring_run_id)

    # Derives the read set from the IR and pairs the two — never a caller's assertion about what an
    # IR reads. Gate 2 then authorizes exactly this, and the renderer executes exactly this.
    return PlannedFormulaExecutionIRV2.plan(ir, policy_reads=policy_reads)


def _row_selections(
    paths: tuple[str, ...], expressions: tuple,
) -> tuple[SelectedRowsV2, ...]:
    """The row selections each expression carries, keyed by the body path it sits at.

    Read off the EXPRESSIONS, because that is where V3 puts them. An earlier draft of this read
    ``proposal.row_selections`` — a field that does not exist on the proposal — so it silently
    produced an empty tuple for every formula, and a semantically filtered feature compiled to an
    unfiltered one that looked correct everywhere except in its numbers.

    An expression with no selections gets NO entry: ``SelectedRowsV2`` refuses an empty tuple,
    precisely so "selects nothing" and "the selection was dropped on the way here" cannot look
    alike.
    """
    return tuple(
        SelectedRowsV2(expr_path=path, selections=tuple(selections))
        for path, expression in zip(paths, expressions, strict=True)
        if (selections := tuple(getattr(expression, "row_selections", ()) or ()))
    )


def _declared_policies(
    paths: tuple[str, ...], expressions: tuple,
) -> tuple[DeclaredPoliciesV2, ...]:
    """Each expression's declared authority refs, carried verbatim and keyed by body path.

    A carry, not a derivation: the refs are the formula's own declaration, and re-deriving them
    would create a second answer to a question the formula already answers.
    """
    return tuple(
        DeclaredPoliciesV2(expr_path=path, refs=refs)
        for path, expression in zip(paths, expressions, strict=True)
        if (refs := getattr(expression, "authority_refs", None)) is not None
    )


#: Which declared role each resolved payload shape is allowed to answer for. A status ref that
#: resolves to a direction payload is not a near-miss — it would read the wrong column and filter on
#: values that mean something else — so the shape is checked against the role that asked for it.
_SHAPE_FOR_ROLE: Mapping[str, type] = {
    "status": EligibleStatusPayloadV1,
    "direction": DirectionPayloadV1,
    "reversal": ReversalPayloadV1,
    "currency_conversion": CurrencyConversionPayloadV1,
}


def _policy_reads(
    conn: DbConn,
    declared: tuple[DeclaredPoliciesV2, ...],
    realization_ids: Mapping[str, str],
) -> tuple[PolicyReadV2, ...] | MaterializationRefused:
    """The physical columns every DECLARED policy reads, derived from its resolved content.

    This is the reason resolution belongs at compile time. A policy is applied by reading its
    columns, and which columns those are lives in the payload — so until it is resolved, the read
    set is incomplete and Gate 2 would authorize a compilation narrower than the run performs.

    A hash names a decision; it is not the decision. A realization pointing at content nobody stored
    cannot be rendered, and rendering it anyway would apply a policy nobody wrote — so an
    unresolvable policy refuses here rather than becoming a default at render time.
    """
    reads: list[PolicyReadV2] = []
    seen: set[tuple[str, str, str]] = set()
    for expr_path, role, ref in sorted(
        (policies.expr_path, role, ref)
        for policies in declared for role, ref in policies.declared_refs()
    ):
        realization_id = realization_ids.get(ref)
        if not realization_id:
            return MaterializationRefused(
                CompilationRefusalCode.POLICY_REFERENCE_UNRESOLVABLE,
                f"{expr_path} declares {role} policy {ref!r} and nothing says which realization "
                f"decides it: the feature claims a governed rule that has not been bound to a "
                f"decision")
        try:
            payload = resolve_executable_policy(conn, realization_revision_id=realization_id)
        except PolicyPayloadUnavailable as exc:
            return MaterializationRefused(
                CompilationRefusalCode.FORMULA_SCHEMA_UNSUPPORTED,
                f"policy {ref!r} cannot be rendered: {exc}")

        expected = _SHAPE_FOR_ROLE[role]
        if not isinstance(payload, expected):
            return MaterializationRefused(
                CompilationRefusalCode.POLICY_REFERENCE_UNRESOLVABLE,
                f"{expr_path} declares {ref!r} as its {role} policy and that realization holds "
                f"{type(payload).__name__}: applying it would read a column that answers a "
                f"different question, and produce a filter nobody wrote")

        # The basis travels from the payload to the read UNCHANGED. It is the one fact that decides
        # whether this policy leaks, and the leakage gate is the thing that reads it.
        temporal = TemporalReadV2(
            basis=KnowledgeTimeBasisV2(payload.read_basis.value), declared_promise=None)
        for sub_role, logical_ref in _columns_of(payload, role):
            key = (ref, sub_role, logical_ref)
            if key in seen:
                continue
            seen.add(key)
            reads.append(PolicyReadV2(
                policy_ref=ref, role=sub_role, logical_ref=logical_ref, temporal=temporal))
    return tuple(reads)


def _columns_of(payload, role: str) -> tuple[tuple[str, str], ...]:
    """``(role, logical_ref)`` for every physical column this payload causes to be read.

    FX reports SUB-ROLES rather than one flat ``currency_conversion``, because the leakage gate
    prints the role and "the target is read as the rate column" and "as a rate join key" send an
    author to different places. The rate TABLE is not a read: the read set is columns, and the table
    is already implied by every column named on it.
    """
    if isinstance(payload, EligibleStatusPayloadV1):
        return ((role, payload.status_column_ref),)
    if isinstance(payload, DirectionPayloadV1):
        return ((role, payload.direction_column_ref),)
    if isinstance(payload, ReversalPayloadV1):
        return ((role, payload.link_column_ref),)
    fx: CurrencyConversionPayloadV1 = payload
    return (
        (f"{role}:rate", fx.rate_column_ref),
        (f"{role}:as_of", fx.as_of_column_ref),
        *((f"{role}:key", key) for key in fx.rate_key_refs),
    )


def _final_operation(body) -> FinalOperationV2:
    raw = getattr(body, "final_operation", FinalOperationV2.IDENTITY)
    return raw if isinstance(raw, FinalOperationV2) else FinalOperationV2(str(raw))


def _expressions(body) -> tuple:
    """The body's expressions, through the v2 vocabulary's own walker — never a second traversal."""
    from featuregen.formula.schema_v2 import body_expressions_v2

    return tuple(body_expressions_v2(body))
