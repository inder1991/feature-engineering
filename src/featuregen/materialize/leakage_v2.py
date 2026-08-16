"""C-C3/C-C5 — ``FullReadSetLeakageGateV2``, over the COMPLETE read set.

**Why "full read set".** A leakage check that looks only at formula operands and filters is checking
the part of the plan a human already reads. The target arrives through the parts nobody re-reads: a
policy whose status column happens to BE the target, a join key that carries it, an FX rate joined
at current state, a spine column that leaks tomorrow's answer. Since C-C4 the planned read set is
one derived union of all of those, so this gate has one thing to walk and cannot be handed a
narrower list than the run actually reads.

**The claim is NARROW, and the verdict says so (invariant 18).** A pass proves exactly two things:
the target column is not read directly under its canonical ref or a resolved alias, and no read
takes its knowledge from after the cutoff. It does **not** prove that semantic proxies are absent,
and it does **not** prove target descendants harmless — a column that is a deterministic function of
the target passes this gate and still leaks. :data:`LEAKAGE_CLAIM_V2` is carried on the verdict so a
caller reporting "leakage gate passed" has the disclaimer in hand rather than in a docstring nobody
opened.

**Post-cutoff is a policy-read property, not a formula property (C-C5).** ``TemporalReadV2`` keeps
the read's BASIS separate from the T+N promise declared over it, because those answer different
questions: a reversal flag read at ``LATEST_AVAILABLE`` tells you a transaction was reversed after
the cutoff no matter how generous the promise is, and a plan that stored only the promise would call
that fine.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from featuregen.materialize.boundary_v2 import (
    KnowledgeTimeBasisV2,
    PlannedFormulaExecutionIRV2,
)

__all__ = [
    "DIRECT_TARGET_READ",
    "LEAKAGE_CLAIM_V2",
    "POST_CUTOFF_POLICY_READ",
    "LeakageFindingV2",
    "LeakageVerdictV2",
    "full_read_set_leakage_gate_v2",
]

#: What a PASS proves, stated narrowly (invariant 18). Deliberately phrased as what is proved plus
#: what is NOT, because the failure mode of a leakage gate is a caller reading "admitted" as "safe".
LEAKAGE_CLAIM_V2 = (
    "Proves: (1) the target column is not read directly, under its canonical ref or a resolved "
    "alias; (2) no read in the union takes its knowledge from after the cutoff. "
    "Does NOT prove: that semantic proxies of the target are absent, or that target descendants "
    "are harmless — a deterministic function of the target passes this gate and still leaks."
)

DIRECT_TARGET_READ = "DIRECT_TARGET_READ"
POST_CUTOFF_POLICY_READ = "POST_CUTOFF_POLICY_READ"


@dataclass(frozen=True, slots=True)
class LeakageFindingV2:
    """One reason the gate refused, attributed to WHERE the read came from.

    ``source`` matters as much as ``logical_ref``: "the target is read" and "the target is read as a
    reversal policy's status column" send an author to different places, and the second is the one
    a formula-only check never surfaces.
    """

    code: str
    logical_ref: str
    source: str
    feature_name: str
    detail: str


@dataclass(frozen=True, slots=True)
class LeakageVerdictV2:
    """Admitted or refused, plus the NARROW claim a pass makes (invariant 18)."""

    admitted: bool
    findings: tuple[LeakageFindingV2, ...]
    claim: str = field(default=LEAKAGE_CLAIM_V2)

    def __post_init__(self) -> None:
        if self.admitted and self.findings:
            raise ValueError(
                f"a verdict cannot be admitted while carrying {len(self.findings)} finding(s): the "
                f"two fields would then disagree, and a caller reading only `admitted` would ship a "
                f"compilation the gate refused")
        if not self.admitted and not self.findings:
            raise ValueError(
                "a refusal with no findings tells an author nothing to fix, and is indistinguishable "
                "from a gate that failed for its own reasons")


def _normalised_targets(target_ref: str, aliases: Sequence[str]) -> frozenset[str]:
    if not target_ref.strip():
        raise ValueError(
            "the leakage gate needs a canonical target ref: with no target there is nothing to "
            "exclude, and returning `admitted` would be a pass nobody earned")
    return frozenset({target_ref, *(alias for alias in aliases if alias.strip())})


def full_read_set_leakage_gate_v2(
    planned: Sequence[PlannedFormulaExecutionIRV2],
    *,
    target_ref: str,
    target_aliases: Sequence[str] = (),
) -> LeakageVerdictV2:
    """Walk every planned feature's COMPLETE read set and refuse direct target reads and post-cutoff
    knowledge.

    Args:
        planned: the planned IRs — each already carries the derived union of its expression, spine
            and policy reads, so this gate cannot be given a narrower list than the run will read.
        target_ref: the canonical governed ref of the column being predicted.
        target_aliases: refs that RESOLVE to the same column. Passed in rather than derived here,
            because alias resolution is the catalog's job and a gate that guessed would be inventing
            the very fact it is checking against.

    Returns:
        A :class:`LeakageVerdictV2`. Read its ``claim`` before reporting a pass as safety.
    """
    if not planned:
        raise ValueError(
            "the leakage gate was given no planned features: an empty walk finds nothing and would "
            "return an admitted verdict that proves nothing about any compilation")
    targets = _normalised_targets(target_ref, target_aliases)
    findings: list[LeakageFindingV2] = []

    for member in planned:
        name = member.ir.feature_name
        structural = set(member.structural_read_set)

        # ONE FINDING PER PATH, not per ref. A column can be read both as an operand and as a
        # policy's status column, and reporting only one would send an author to close one door
        # while the other still admits the target.
        for logical_ref in sorted(structural & targets):
            findings.append(LeakageFindingV2(
                code=DIRECT_TARGET_READ, logical_ref=logical_ref, source="structural_read",
                feature_name=name,
                detail=(f"{name} reads {logical_ref} as an operand, filter, join key, temporal "
                        f"column or spine read, and that column IS the prediction target")))

        for read in member.policy_reads:
            if read.logical_ref not in targets:
                continue
            findings.append(LeakageFindingV2(
                code=DIRECT_TARGET_READ, logical_ref=read.logical_ref,
                source=f"policy_read:{read.role}", feature_name=name,
                detail=(f"{name} reads {read.logical_ref} as the {read.role} column of "
                        f"{read.policy_ref}, and that column IS the prediction target — a policy is "
                        f"applied by reading its columns, so the target enters the feature through "
                        f"the policy rather than through the formula")))

        for read in member.policy_reads:
            if read.temporal.basis is KnowledgeTimeBasisV2.LATEST_AVAILABLE:
                findings.append(LeakageFindingV2(
                    code=POST_CUTOFF_POLICY_READ, logical_ref=read.logical_ref,
                    source=f"policy_read:{read.role}", feature_name=name,
                    detail=(
                        f"{name} reads {read.logical_ref} for {read.policy_ref} at "
                        f"{KnowledgeTimeBasisV2.LATEST_AVAILABLE.value}, which sees state as it is "
                        f"NOW rather than as it was at the cutoff. The declared promise does not "
                        f"change that: a promise says when data should arrive, not which instant "
                        f"this read observes")))

    ordered = tuple(sorted(findings, key=lambda f: (f.feature_name, f.code, f.logical_ref)))
    return LeakageVerdictV2(admitted=not ordered, findings=ordered)
