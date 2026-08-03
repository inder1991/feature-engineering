"""Analysis planning — the metadata half of the natural-language data agent.

An analysis question ("how many customers have fewer transactions this month, split by segment and
sector") becomes an :class:`AnalysisPlanV1`: a typed statement of WHAT to compute, expressed in the
catalog's own identities. This module owns the contract; :mod:`featuregen.analysis.grounding` decides
whether a plan is answerable and what is doubtful about it.

**Why a plan and not SQL.** Handing a model the column names and letting it write SQL produces
confident, wrong answers on a bank catalog: it picks the raw event timestamp when the data is not
available until two days later, sums amounts across currencies, counts rows per customer without
knowing the table's grain, or joins two customer ids that were never sanctioned as the same
namespace. Every one of those is invisible in the output — the number simply looks reasonable.

The catalog already knows enough to catch all four. Grounding a plan against it, and reporting each
doubt as a typed finding, is what separates this from text-to-SQL. It is also why the planning half
is worth building before there is any data to execute against: the checks are metadata reasoning,
and they are the part that makes the answer trustworthy.

**Findings do not block.** A finding is carried, never a refusal — the same shape the feature
gauntlet uses (``NEEDS_EXTERNAL_VALIDATION`` plus a requirement naming the operand). The agent
answers the question and says what the answer rests on. A human confirming the underlying fact
clears the finding. Only a plan that cannot be expressed at all is refused.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:      # import weight: `source_selection` reaches the concept registry (~1s)
    from featuregen.overlay.upload.source_selection import DatasetSourceSelectionV1
    from featuregen.overlay.upload.source_selector import SelectionRefusalV1
    from featuregen.overlay.upload.temporal_policy import DatasetRowSelectionV1

#: Findings a grounded plan can carry. A CLOSED vocabulary: a new doubt gets a code here, so it can
#: never be smuggled into prose that the caller has to read to notice.
FINDING_CODES: frozenset[str] = frozenset({
    # ── the answer may be silently wrong ──────────────────────────────────────────────────────────
    "TIME_ANCHOR_UNGOVERNED",      # the as-of column is file-declared, not a governed fact
    "AVAILABILITY_BASIS_UNKNOWN",  # no availability fact: a "last month" window may include rows
                                   # that were not knowable a month ago (look-ahead leakage)
    "AVAILABILITY_LAG_UNAPPLIED",  # basis is event_time_plus_lag; the cutoff must be shifted back
    "CURRENCY_MIXED",              # aggregating a monetary measure whose currency varies or is unknown
    "GRAIN_NOT_ESTABLISHED",       # counting per entity without a governed unique grain
    "JOIN_IDENTITY_UNCONFIRMED",   # the cross-catalog hop rests on an identifier link no human has
                                   # reviewed — usable, and disclosed
    "JOIN_IDENTITY_UNAVAILABLE",   # the identifier link is REJECTED / drift-STALEd / expired, or its
                                   # governance state cannot be read: the platform will not consider
                                   # it, and no approval brings it back
    # ── the answer may disclose more than intended ────────────────────────────────────────────────
    "SMALL_CELL_RISK",             # a grouped result can isolate individuals; needs a minimum cell size
    # ── the answer is less grounded than it looks ─────────────────────────────────────────────────
    "DIMENSION_UNGOVERNED",        # a group-by column carries no business concept
    "CODE_SET_INCOMPLETE",         # a slice value came from observed samples, not a known domain
    "MEASURE_NOT_NUMERIC",         # the measure is not a numeric column
    "ELIGIBILITY_UNCONFIRMED",     # which rows COUNT rests on a definition no human has
                                   # agreed to — usable, and disclosed
})

#: A plan that cannot be expressed at all is refused rather than carried. These are not findings.
REFUSAL_CODES: frozenset[str] = frozenset({
    "COLUMN_NOT_VISIBLE",   # read scope hides it — reported as absent, never as "hidden"
    "COLUMN_ABSENT",        # the catalog does not describe it
    "TABLE_ABSENT",
    "NO_PATH_TO_DIMENSION",  # a group-by lives in a table nothing connects to the measure
})


@dataclass(frozen=True, slots=True)
class Finding:
    """One typed doubt about a plan, naming the catalog object it concerns.

    Mirrors the feature gauntlet's ``Requirement``: a code from a closed vocabulary, the operand it
    is about, and human-readable detail that carries NO sample values and no PII.
    """

    code: str                       # in FINDING_CODES
    subject: str                    # the logical_ref / table_ref the finding concerns
    detail: str = ""
    #: Set when a human action would clear this finding — e.g. confirming an identifier link or an
    #: availability basis. Absent when the finding is inherent to the question.
    clears_when: str = ""


@dataclass(frozen=True, slots=True)
class Measure:
    """What is being counted or aggregated."""

    op: str                         # count | count_distinct | sum | avg | min | max
    logical_ref: str = ""           # empty for count(*)
    label: str = ""


@dataclass(frozen=True, slots=True)
class Window:
    """A time window over one anchor column.

    ``anchor_ref`` is the column the window is measured on. ``availability_basis`` and
    ``availability_lag_hours`` come from the governed ``availability_time`` fact and are what make
    the window honest: with basis ``event_time_plus_lag`` the cutoff must be moved back by the lag,
    or the window silently includes rows that had not landed yet.
    """

    anchor_ref: str
    length_days: int
    offset_days: int = 0            # 0 = the most recent window; 30 = the one before it
    availability_basis: str = ""
    availability_lag_hours: float | None = None
    label: str = ""
    #: Set when the window is whole CALENDAR periods rather than a span of days — "month" or "day".
    #: A day span cannot express a calendar month, and pretending it can changes the answer: 30 days
    #: ending 30 June overlaps both the 2026-05 and 2026-06 partitions, so reading the partitions it
    #: touches would fold late-May activity into "this month". With `calendar_unit` set,
    #: `calendar_length` and `calendar_offset` count whole units — offset 0 is the period containing
    #: the cutoff, offset 1 the one before it — which is what a monthly-partitioned table can answer
    #: exactly. Defaults keep the day-span meaning for every existing plan.
    calendar_unit: str = ""
    calendar_length: int = 0
    calendar_offset: int = 0


@dataclass(frozen=True, slots=True)
class Dimension:
    """One group-by. ``slice_values`` is set only when the question asked for specific codes."""

    logical_ref: str
    label: str = ""
    slice_values: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class AnalysisPlanV1:
    """WHAT to compute, in the catalog's identities. Contains no SQL and no data.

    A period-over-period question ("fewer transactions this month") is two windows over one measure
    at one entity grain, compared. Keeping the comparison declarative — rather than letting a model
    write the arithmetic — is what lets grounding check both windows use the same anchor and the
    same availability basis.
    """

    question: str
    entity: str                     # the grain the answer is per — e.g. "customer"
    entity_ref: str                 # the column identifying that entity in the base table
    base_table_ref: str
    measure: Measure
    windows: tuple[Window, ...] = ()
    dimensions: tuple[Dimension, ...] = ()
    comparison: str = ""            # "" | decrease | increase | change
    #: WHICH TABLE IS THE POPULATION, declared — never inferred. `spine.py` states the rule and the
    #: reason: `kyc_customers`, `card_customers` and `customers` can look identical to the catalog —
    #: same entity, same unique key, same availability column — so choosing among them from governed
    #: facts is inference, and when it is wrong the spine simply has fewer rows and every number that
    #: follows is plausible. Governed facts may VALIDATE this declaration; they may not make it.
    #:
    #: Empty means undeclared, which is why a period-over-period plan always raises a `population`
    #: clarification: the model is not asked, because a model choosing here is inference by something
    #: with less standing than the catalog.
    population_table_ref: str = ""
    population_key_ref: str = ""

    #: Cross-catalog hops this plan needs, as identifier-link fact keys. Grounding reads each one's
    #: governed lifecycle and DISCLOSES what it finds — JOIN_IDENTITY_UNCONFIRMED for a link no human
    #: has reviewed, JOIN_IDENTITY_UNAVAILABLE for one the platform will not consider — rather than
    #: refusing. Neither finding is a gate: availability is enforced where links are traversed.
    join_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class SelectionPreviewV1:
    """The Release-B source/row decisions this plan rests on — BOUNDED, for preview.

    **This is a SEAM, not the record.** Task 9 seals the exact decision refs into
    ``AnalysisExecutionIRV2`` and the six snapshot kinds, and revalidates the current pointers
    immediately before execution. Until it does, these decisions travel with the plan so a preview
    can SHOW which copy was chosen, which rows, and what else was considered — which is the half of
    "separately explainable" (§5.7) that a person actually reads. Nothing here is persisted, and
    nothing downstream may treat it as a pin.

    Empty by construction while ``FEATUREGEN_SOURCE_TEMPORAL_SELECTION`` is off, so a flag-off
    ``GroundedPlan`` is byte-identical to the one this release found.
    """

    source_selections: tuple[DatasetSourceSelectionV1, ...] = field(default_factory=tuple)
    row_selections: tuple[DatasetRowSelectionV1, ...] = field(default_factory=tuple)
    refusals: tuple[SelectionRefusalV1, ...] = field(default_factory=tuple)
    #: Warnings riding a RESOLVED decision — e.g. `PROPOSED_AUTHORITY_USED`. Visible, never a log
    #: line: the point of letting an unconfirmed classification be useful is that its authority
    #: travels with it.
    warnings: tuple[str, ...] = field(default_factory=tuple)
    #: How many dataset NEEDS the resolver set out to answer, and how many ROW decisions it owed
    #: once the sources were chosen. Carried so :attr:`resolved` can mean "every need got its
    #: decision" rather than "nothing complained" — see that property.
    needs_considered: int = 0
    row_decisions_expected: int = 0

    @property
    def refusal_codes(self) -> tuple[str, ...]:
        """The closed codes, deduped and ordered — what clarify renders and learning records."""
        return tuple(sorted({refusal.code for refusal in self.refusals}))

    @property
    def resolved(self) -> bool:
        """Every need that was asked about got its decision, and nothing refused.

        NOT merely ``not refusals``. That definition called an EMPTY preview resolved, which is
        exactly the shape a swallowed exception leaves behind — no decisions, no refusals, and a
        caller told the selection resolved. The review found precisely that: a historical plan
        whose row rule raised, was caught by a blanket ``except`` and vanished, leaving
        ``row_selections=()``, ``refusals=()`` and ``resolved`` True. An empty preview is never
        resolved now, and a need whose decision is missing for any reason keeps it False.
        """
        if self.refusals or not self.source_selections:
            return False
        return (len(self.source_selections) == self.needs_considered
                and len(self.row_selections) == self.row_decisions_expected)


@dataclass(frozen=True, slots=True)
class GroundedPlan:
    """A plan checked against the catalog.

    ``answerable`` is False only when a refusal fired — the plan could not be expressed. Findings do
    not make a plan unanswerable; they travel with the answer.
    """

    plan: AnalysisPlanV1
    answerable: bool
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    refusals: tuple[tuple[str, str], ...] = field(default_factory=tuple)   # (code, subject)
    #: The Release-B decisions, when the flag is on. Deliberately NOT folded into ``refusals``: that
    #: tuple sets ``answerable``, and a source that is merely undeclared is a question for a person,
    #: not a plan that could not be expressed. The HARD gate is the execution bridge, which is where
    #: the review found the population hole.
    selections: SelectionPreviewV1 | None = None

    @property
    def trustworthy(self) -> bool:
        """No findings at all — every fact the answer rests on is governed. Deliberately strict, and
        expected to be False on a young catalog; the point is to say so, not to hide it."""
        return self.answerable and not self.findings
