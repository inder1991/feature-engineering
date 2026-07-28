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
    "JOIN_IDENTITY_UNCONFIRMED",   # the cross-catalog hop rests on an unconfirmed identifier link
    # ── the answer may disclose more than intended ────────────────────────────────────────────────
    "SMALL_CELL_RISK",             # a grouped result can isolate individuals; needs a minimum cell size
    # ── the answer is less grounded than it looks ─────────────────────────────────────────────────
    "DIMENSION_UNGOVERNED",        # a group-by column carries no business concept
    "CODE_SET_INCOMPLETE",         # a slice value came from observed samples, not a known domain
    "MEASURE_NOT_NUMERIC",         # the measure is not a numeric column
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
    #: Cross-catalog hops this plan needs, as identifier-link fact keys. Grounding checks each one's
    #: confirmation state and carries JOIN_IDENTITY_UNCONFIRMED rather than refusing.
    join_refs: tuple[str, ...] = ()


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

    @property
    def trustworthy(self) -> bool:
        """No findings at all — every fact the answer rests on is governed. Deliberately strict, and
        expected to be False on a young catalog; the point is to say so, not to hide it."""
        return self.answerable and not self.findings
