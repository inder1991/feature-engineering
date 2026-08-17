"""S12 — corpus generation: a batch that GENERATES and provably executes nothing.

**The batch triggers no execution, and that is the whole safety property.** Generating a corpus means
compiling and sealing many features at once; if any of that reached a cluster, a batch run would be
an unreviewed mass execution. So this module has no verification and no publication in it — not a
flag defaulting to off, not a parameter nobody passes. It imports neither store, and a test asserts
that from its source. The check is structural because a runtime one can only prove that a particular
batch executed nothing.

**A coverage table names EVERY refusal.** The output is not "42 of 60 generated". Each declaration
that could not generate carries the blocker codes that stopped it, so the table answers "what is
standing between this corpus and complete coverage" rather than "how far did we get". Codes are the
closed activation vocabulary — a corpus report inventing its own words would be a second thing to
learn and would drift from what the product surface says about the same candidate.

**Both target modes, and the difference is recorded rather than assumed.**
:class:`~featuregen.overlay.upload.selection_revisions.TargetModeV1` splits ``PREDICTION`` from
``EXPLORATION``, and a corpus over an exploration build is a legitimate thing to want — it just has
no target to leak. The mode rides on every row, so a reader can tell which population a coverage
number is about instead of averaging two.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from featuregen.contracts.db import DbConn
from featuregen.formula.policy_occurrences import PolicyOccurrenceSetV1
from featuregen.materialize.evaluate_generate import evaluate_generate
from featuregen.materialize.operator_graph_v2 import OperatorGraphV2
from featuregen.overlay.upload.evaluator_contracts import ACTIVATION_BLOCKER_DISPOSITIONS
from featuregen.overlay.upload.selection_revisions import BuildDeclarationV1, TargetModeV1

__all__ = [
    "CorpusCandidateV1",
    "CorpusCoverageV1",
    "CorpusRowV1",
    "generate_corpus",
    "named_refusals",
]


@dataclass(frozen=True, slots=True)
class CorpusCandidateV1:
    """One thing the batch tries to generate, and everything the attempt needs.

    Everything is supplied rather than looked up, because a batch that discovered its own inputs
    would be deciding which features to build — and choosing the corpus is the operator's call, not
    this module's.
    """

    candidate_id: str
    declaration: BuildDeclarationV1
    target_mode: TargetModeV1
    generation_authorization_revision_id: str
    activation_blockers: tuple[str, ...]
    occurrences: PolicyOccurrenceSetV1
    graph: OperatorGraphV2

    def __post_init__(self) -> None:
        if not self.candidate_id.strip():
            raise ValueError(
                "a corpus candidate with no id cannot appear in a coverage table: every row has to "
                "be traceable back to the thing it is about")


@dataclass(frozen=True, slots=True)
class CorpusRowV1:
    """One row of the coverage table: what was tried, and what stopped it."""

    candidate_id: str
    target_mode: TargetModeV1
    environment_id: str
    generated: bool
    blockers: tuple[str, ...]

    def __post_init__(self) -> None:
        if self.generated and self.blockers:
            raise ValueError(
                f"{self.candidate_id} is recorded as generated while carrying "
                f"{len(self.blockers)} blocker(s): a coverage table whose two columns disagree is "
                f"read by whichever one the reader trusts")
        if not self.generated and not self.blockers:
            raise ValueError(
                f"{self.candidate_id} did not generate and names nothing that stopped it, which is "
                f"the one thing a coverage table exists to say")


@dataclass(frozen=True, slots=True)
class CorpusCoverageV1:
    """The whole table, plus the counts a reader actually asks for."""

    rows: tuple[CorpusRowV1, ...]
    #: blocker code → how many candidates it stopped, most-blocking first. Built here rather than by
    #: a reader, so "the top reason coverage is incomplete" has one answer.
    by_blocker: Mapping[str, int] = field(default_factory=dict)

    @property
    def generated(self) -> tuple[str, ...]:
        return tuple(row.candidate_id for row in self.rows if row.generated)

    @property
    def refused(self) -> tuple[str, ...]:
        return tuple(row.candidate_id for row in self.rows if not row.generated)

    def for_mode(self, mode: TargetModeV1) -> tuple[CorpusRowV1, ...]:
        """The rows for one target mode.

        Exposed because a coverage number averaged across ``PREDICTION`` and ``EXPLORATION``
        describes neither population: an exploration build has no target to leak, so its refusals
        are a different set of questions.
        """
        return tuple(row for row in self.rows if row.target_mode is mode)


def generate_corpus(
    conn: DbConn,
    candidates: Sequence[CorpusCandidateV1],
    *,
    engine_id: str,
) -> CorpusCoverageV1:
    """Evaluate every candidate for GENERATION and return the coverage table.

    Executes nothing. Not "does not execute by default" — there is no verification or publication
    path reachable from here at all, which is what makes a batch run safe to point at a whole
    catalog.

    Raises:
        ValueError: two candidates share an id, or a candidate's evaluation raised. The second is
            deliberate: ``evaluate_generate`` raises on a blocker code with no disposition, and
            swallowing that into a row would turn "a code nobody decided about" into "this candidate
            was refused for reasons" — the exact silent-shrinking this vocabulary exists to prevent.
    """
    ids = [candidate.candidate_id for candidate in candidates]
    if len(set(ids)) != len(ids):
        raise ValueError(
            "two corpus candidates share an id: a coverage table with a duplicated row cannot say "
            "which of the two a blocker belongs to")

    rows: list[CorpusRowV1] = []
    counts: dict[str, int] = {}
    for candidate in candidates:
        verdict = evaluate_generate(
            conn,
            generation_authorization_revision_id=candidate.generation_authorization_revision_id,
            activation_blockers=candidate.activation_blockers,
            occurrences=candidate.occurrences,
            graph=candidate.graph,
            engine_id=engine_id)
        rows.append(CorpusRowV1(
            candidate_id=candidate.candidate_id, target_mode=candidate.target_mode,
            environment_id=candidate.declaration.environment_id,
            generated=verdict.allowed, blockers=verdict.blockers))
        for code in verdict.blockers:
            counts[code] = counts.get(code, 0) + 1

    ordered = dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))
    return CorpusCoverageV1(rows=tuple(rows), by_blocker=ordered)


def named_refusals(coverage: CorpusCoverageV1) -> tuple[tuple[str, str, int], ...]:
    """Every refusal as ``(code, why-it-is-carried, count)`` — the coverage table's prose column.

    The reason comes from the evaluator's disposition table rather than from a string here, so a
    corpus report and the product surface explain the same code the same way. A code with no
    disposition raises through the lookup rather than printing as itself: an unexplained code in a
    coverage report is how a blocker stops being understood.
    """
    return tuple(
        (code, ACTIVATION_BLOCKER_DISPOSITIONS[code][1], count)
        for code, count in coverage.by_blocker.items())
