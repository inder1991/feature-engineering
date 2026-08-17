"""S6 — Gate 2 for a V2 group: the SAME decision, over a read set that includes policy columns.

**Not a second gate.** :func:`~featuregen.materialize.ir.decide_read_scope` is the shipped verdict —
existence before read scope, group-wide, the same two messages byte for byte — and this module hands
it a union built by the shipped walk. Nothing about the ordering, the refusal codes or the wording is
restated here, because a second Gate 2 would eventually decide something differently from the first
and the difference would show up as a compilation that read a column nobody authorized.

**What V2 adds is one element class.** A governed policy is applied by READING ITS COLUMNS — a status
flag, a direction indicator, an FX rate — and those columns are physical reads that Gate 2 must
authorize like any other. They enter the union as ``ReadElementKind.POLICY_READ``, so an FX rate
column the caller's roles cannot read refuses ``READ_SCOPE_INSUFFICIENT`` through the ordinary path
rather than through a rule written specially for FX. The kind is separate from ``EXPRESSION_READ``
because the remedy is: an operator told only "you may not read this column" cannot tell whether to
change the formula or to be granted a rate table the formula never mentions.

**Nothing is rebuilt after the gate.** The token holds the very
:class:`~featuregen.materialize.boundary_v2.PlannedFormulaExecutionIRV2` objects it was given — not
copies, not re-planned equivalents — so the read set the gate decided over and the read set the
renderer executes are the same tuple. C-C2 put that in the type; this is the function that must not
undo it, and a test asserts object identity rather than equality, because two equal IRs planned
against different read sets is exactly the failure that would still pass an equality check.
"""
from __future__ import annotations

from collections.abc import Sequence

from featuregen.contracts.db import DbConn
from featuregen.materialize.boundary_v2 import (
    AuthorizedCompilationV2,
    PlannedFormulaExecutionIRV2,
)
from featuregen.materialize.codes import MaterializationRefused
from featuregen.materialize.ir import (
    authorized_refs_of,
    decide_read_scope,
    union_read_elements,
)
from featuregen.materialize.spine import SpineSpec

__all__ = ["authorize_compilation_v2"]


def authorize_compilation_v2(
    conn: DbConn,
    planned: Sequence[PlannedFormulaExecutionIRV2],
    spine: SpineSpec,
    *,
    roles: Sequence[str] = (),
) -> AuthorizedCompilationV2 | MaterializationRefused:
    """Gate 2 over a V2 group's complete read set — policy columns included.

    Args:
        planned: planned IRs, each already carrying the derived union of its expression, spine and
            policy reads. Taking PLANNED IRs rather than bare ones is C-C2's ordering: a group
            authorized from unplanned IRs could be authorized for one read set and rendered from
            another, and here it is not merely wrong but unconstructible.
        spine: the population declaration this group was compiled against.
        roles: the caller's roles, passed to the shipped read-scope predicate unchanged.

    Returns:
        The token, holding the SAME planned objects, or a
        :class:`~featuregen.materialize.codes.MaterializationRefused`. Nothing partial either way —
        a group is published as one row per key, so one unreadable element refuses the whole
        compilation rather than dropping a feature.

    Raises:
        ValueError: the group is empty, or a member was planned against a different spine than the
            one supplied. Both are calls assembled wrongly rather than governed verdicts: a token
            over no features is a permit for nothing, and authorizing one population while the
            features read another authorizes reads nobody performs.
    """
    group = tuple(planned)
    if not group:
        raise ValueError(
            "authorize_compilation_v2 was called with no features: an authorization token over an "
            "empty group is a permit for nothing, and the next stage cannot tell it apart from a "
            "group that was genuinely authorized")

    declared = spine.identity_payload()
    mismatched = [member.ir.feature_name for member in group
                  if member.ir.spine.identity_payload() != declared]
    if mismatched:
        raise ValueError(
            f"{len(mismatched)} of {len(group)} planned IRs were compiled against a different spine "
            f"declaration than the one supplied ({', '.join(sorted(mismatched))}): §4 declares the "
            f"population once per materialization contract, and authorizing one population while "
            f"the features read another authorizes reads nobody performs")

    # ONE union, through the shipped walk, with the policy reads folded in as their own kind.
    elements = union_read_elements(
        [(member.ir.feature_name, member.ir.expressions) for member in group],
        spine,
        policy_reads=[read.logical_ref for member in group for read in member.policy_reads],
    )
    refusal = decide_read_scope(conn, elements, roles=roles, feature_count=len(group))
    if refusal is not None:
        return refusal

    return AuthorizedCompilationV2(
        planned=group, spine=spine, authorized_refs=authorized_refs_of(elements),
        roles_used=tuple(roles))
