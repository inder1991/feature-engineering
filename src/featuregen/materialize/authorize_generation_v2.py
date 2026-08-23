"""Step 6 — from planned V2 IRs to an AUTHORIZED compilation: the two gates, in order.

**Both gates were built and neither was ever called.** ``full_read_set_leakage_gate_v2`` and
``authorize_compilation_v2`` have zero production callers; V1 has no compile-time leakage gate at
all. This is the function that runs them, and the ordering below is the whole content of the module
— everything else is delegation.

**Leakage first, read scope second.** Not for cost, though leakage is the cheap one: the two answer
questions about different things. Leakage is a fact about the FEATURE — it reads its own target, and
it is wrong for everyone. Read scope is a fact about the CALLER — someone else may legitimately be
permitted. Running read scope first would tell one operator "you may not read this column" and
another "this feature leaks" about the same build, and the second is the one that has to be fixed
once rather than per person.

**An EXPLORATION build makes no leakage claim, and says so.** The gate needs a target to exclude and
there is none: that is what the mode means. Returning a passing verdict for it would be a claim
nobody earned, so the verdict is absent and :class:`AuthorizedGenerationV2` refuses to hold a
PREDICTION authorization with no verdict — two fields that can disagree eventually do.

**What a pass means is carried, not documented.** ``LeakageVerdictV2`` ships its own narrow claim —
a pass proves the target is not read DIRECTLY and no read is post-cutoff, and proves nothing about
semantic proxies or deterministic functions of the target. That text travels on the result, so a
caller reporting "leakage gate passed" has the disclaimer in hand rather than in a docstring nobody
opened.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.materialize.boundary_v2 import (
    AuthorizedCompilationV2,
    PlannedFormulaExecutionIRV2,
)
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.gate2_v2 import authorize_compilation_v2
from featuregen.materialize.generation_authorization import GenerationAuthorizationV1
from featuregen.materialize.leakage_v2 import (
    LeakageVerdictV2,
    full_read_set_leakage_gate_v2,
)
from featuregen.materialize.spine import SpineSpec
from featuregen.overlay.upload.selection_revisions import TargetModeV1

__all__ = ["AuthorizedGenerationV2", "authorize_generation_v2"]


@dataclass(frozen=True, slots=True)
class AuthorizedGenerationV2:
    """A group cleared by BOTH gates, carrying what each one actually checked.

    The leakage verdict travels with the token rather than being consumed and discarded, because
    "authorized" and "proved not to leak" are different statements and a caller holding only the
    token cannot tell which it has. For an exploration build ``leakage`` is ``None`` — no target, no
    claim — which is deliberately not the same value as a verdict that passed.
    """

    token: AuthorizedCompilationV2
    authorization: GenerationAuthorizationV1
    leakage: LeakageVerdictV2 | None

    def __post_init__(self) -> None:
        is_prediction = self.authorization.target_mode is TargetModeV1.PREDICTION
        if is_prediction and self.leakage is None:
            raise ValueError(
                "a prediction generation carries no leakage verdict: it was authorized FOR a "
                "target, so 'was the target read' is a question that was asked or the "
                "authorization is not one")
        if not is_prediction and self.leakage is not None:
            raise ValueError(
                "an exploration generation carries a leakage verdict: there is no target to leak — "
                "that is what the mode means — so a verdict here claims a check nobody could have "
                "performed")

    @property
    def leakage_claim(self) -> str:
        """What the leakage result actually proves, for a caller about to report it."""
        if self.leakage is None:
            return ("No leakage claim: this generation is authorized for exploration, which has no "
                    "prediction target, so nothing was checked against one.")
        return self.leakage.claim


def authorize_generation_v2(
    conn: DbConn,
    planned: Sequence[PlannedFormulaExecutionIRV2],
    *,
    spine: SpineSpec,
    authorization: GenerationAuthorizationV1,
    target_aliases: Sequence[str] = (),
    roles: Sequence[str] = (),
) -> AuthorizedGenerationV2 | MaterializationRefused:
    """Run both gates over one build set's planned IRs and return the authorized compilation.

    Args:
        planned: the planned IRs, each already carrying the derived union of its expression, spine
            and policy reads. Planned rather than bare, so neither gate can be handed a narrower
            read set than the run performs.
        authorization: what this generation is authorized FOR (invariant 17). Its MODE decides
            whether there is a target at all, and therefore whether leakage is a question.
        target_aliases: refs that RESOLVE to the target column. Passed in rather than derived,
            because alias resolution is the catalog's job and a gate that guessed would be
            inventing the fact it is checking against.
        roles: the caller's roles, handed to the shipped read-scope predicate unchanged.

    Returns:
        :class:`AuthorizedGenerationV2`, or the FIRST gate's refusal. Nothing partial: a group is
        published as one row per key, so one leaking or unreadable member refuses the whole
        compilation rather than quietly dropping a feature and building the rest.

    Raises:
        ValueError: the group is empty, or a member was planned against a different spine. Both are
            calls assembled wrongly rather than governed verdicts.
    """
    group = tuple(planned)
    if not group:
        raise ValueError(
            "authorize_generation_v2 was called with no planned features: an empty group compiles "
            "nothing, and a token over it would authorize every empty group equally")

    # ── 1. DOES THIS BUILD READ ITS OWN ANSWER? ─────────────────────────────────────────────────
    leakage = _leakage_verdict(group, authorization, target_aliases)
    if isinstance(leakage, MaterializationRefused):
        return leakage

    # ── 2. MAY THIS CALLER READ WHAT IT READS? ──────────────────────────────────────────────────
    # The shipped Gate 2, unchanged: existence before read scope, group-wide, the same two messages
    # byte for byte. The token it returns holds the SAME planned objects it was given, so the read
    # set decided over and the read set executed are one tuple.
    token = authorize_compilation_v2(conn, group, spine, roles=roles)
    if isinstance(token, MaterializationRefused):
        return token

    return AuthorizedGenerationV2(
        token=token, authorization=authorization, leakage=leakage)


def _leakage_verdict(
    group: tuple[PlannedFormulaExecutionIRV2, ...],
    authorization: GenerationAuthorizationV1,
    target_aliases: Sequence[str],
) -> LeakageVerdictV2 | None | MaterializationRefused:
    """The leakage verdict, ``None`` for an exploration build, or the refusal.

    The findings are folded into one refusal rather than reported per feature, because a group is
    authorized or refused as one thing — but every finding's text survives in the detail, since
    "something leaks" without saying WHICH feature reads WHICH column through WHICH path is not
    something an author can act on.
    """
    if authorization.target_mode is not TargetModeV1.PREDICTION:
        return None

    verdict = full_read_set_leakage_gate_v2(
        group, target_ref=authorization.target_ref or "", target_aliases=target_aliases)
    if verdict.admitted:
        return verdict

    return MaterializationRefused(
        CompilationRefusalCode.TARGET_LEAKAGE_DETECTED,
        f"{len(verdict.findings)} leaking read(s) in this build set — the features would be "
        f"trained on their own answer:\n"
        + "\n".join(f"  • [{finding.code}] {finding.detail}" for finding in verdict.findings))
