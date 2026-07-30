"""What will run, before it runs.

The last thing between a question and a number. It answers three things a user needs and currently
cannot get: what will be computed, which rows it will read, and what the answer rests on.

**Assembled from the artifacts that execute, never a paraphrase of them.** The SQL shown is the
statement :func:`compile_analysis` produces from the same IR that will be handed to the executor, and
the partitions listed are the ones in that IR. A preview written independently drifts from what runs
— and a drifted preview is worse than none, because it converts "I checked" into false confidence.
It is the same reason the plan hash is shown: two previews with one hash are the same computation.

**Findings are shown, not hidden.** `plan.py`'s rule is that a finding travels with the answer rather
than blocking it, and this is where a user finally sees them. A currency that varies, a join identity
no human confirmed, a time anchor that is only file-declared — each is a specific reason a plausible
number might be wrong, and each names the object it concerns and what would clear it.

**A plan that cannot run says why, in the same shape.** Without :class:`ExecutionInputs` there is no
SQL to show, and the honest preview reports the refusal — a missing eligibility policy, a spine that
is the event table — rather than rendering a confident summary of something that will not execute.
"""

from __future__ import annotations

from dataclasses import dataclass

from featuregen.analysis.execution import BridgeRefusal, ExecutionInputs, plan_to_execution_ir
from featuregen.analysis.plan import GroundedPlan
from featuregen.data_agent.analysis import compile_analysis


@dataclass(frozen=True, slots=True)
class PeriodPreview:
    """One period and the exact partitions it will read.

    Listed because partition pruning is part of the plan rather than an optimisation: a user who can
    see `2026-05` and `2026-06` can tell at a glance that a question about two months is not about to
    scan four years.
    """

    label: str
    partitions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class FindingPreview:
    code: str
    subject: str
    detail: str = ""
    clears_when: str = ""


@dataclass(frozen=True, slots=True)
class PlanPreview:
    """Everything a person needs to decide whether to run this."""

    question: str
    entity: str
    measure: str
    comparison: str
    dimensions: tuple[str, ...] = ()
    periods: tuple[PeriodPreview, ...] = ()
    findings: tuple[FindingPreview, ...] = ()
    unresolved: tuple[str, ...] = ()
    #: The statement that will execute. Empty when the plan cannot be compiled — see `blocked_by`.
    sql: str = ""
    #: Identity of WHAT is computed, excluding provenance. Two previews sharing it are the same
    #: computation even if the questions were worded differently.
    plan_hash: str = ""
    #: (code, subject) for the one thing stopping this from running, if anything.
    blocked_by: tuple[str, str] | None = None

    @property
    def runnable(self) -> bool:
        return self.blocked_by is None

    @property
    def rests_on_unconfirmed_facts(self) -> bool:
        """True when the answer would carry at least one disclosed doubt.

        Deliberately not called "unreliable": a finding is a statement about what is GOVERNED, and on
        a young catalog most true answers carry several.
        """
        return bool(self.findings)


def _measure_phrase(grounded: GroundedPlan) -> str:
    measure = grounded.plan.measure
    if measure.op == "count" and not measure.logical_ref:
        return "count of rows"
    return f"{measure.op} of {measure.logical_ref}" if measure.logical_ref else measure.op


def preview(grounded: GroundedPlan, inputs: ExecutionInputs | None = None, *,
            dialect=None) -> PlanPreview:
    """Build the preview. With `inputs` it shows the real SQL; without, it shows what is missing.

    `dialect` decides the SQL flavour shown. It must be the dialect that will EXECUTE — showing a
    user PostgreSQL and running HiveQL would defeat the point, since the two disagree about things
    that change the answer rather than raising.
    """
    plan = grounded.plan
    base = {
        "question": plan.question,
        "entity": plan.entity,
        "measure": _measure_phrase(grounded),
        "comparison": plan.comparison,
        "dimensions": tuple(d.logical_ref for d in plan.dimensions),
        "findings": tuple(FindingPreview(code=f.code, subject=f.subject, detail=f.detail,
                                         clears_when=f.clears_when) for f in grounded.findings),
    }

    if not grounded.answerable:
        code, subject = grounded.refusals[0] if grounded.refusals else ("PLAN_NOT_ANSWERABLE", "")
        return PlanPreview(**base, blocked_by=(code, subject))

    if inputs is None:
        return PlanPreview(**base, blocked_by=("EXECUTION_INPUTS_ABSENT", plan.base_table_ref))

    try:
        ir = plan_to_execution_ir(grounded, inputs)
    except BridgeRefusal as exc:
        # The refusal is the useful content: it names the decision a human still owes — an
        # eligibility policy, a population spine — rather than a generic "cannot run".
        return PlanPreview(**base, blocked_by=(exc.code, exc.subject))

    if dialect is None:
        from featuregen.data_agent.sql_postgres import PostgresDialect
        dialect = PostgresDialect()

    return PlanPreview(
        **base,
        periods=(PeriodPreview(label="previous", partitions=ir.previous.values),
                 PeriodPreview(label="current", partitions=ir.current.values)),
        sql=compile_analysis(ir, dialect=dialect),
        plan_hash=ir.plan_hash)
