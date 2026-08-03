"""Point-in-time dimension attribution — which segment classifies a customer.

Joining *today's* segment to *last month's* transactions silently reattributes activity whenever a
customer changes segment, and the answer looks entirely reasonable. It is the same class of defect
as counting reversed transactions: it changes the number, not the presentation.

**The business decision is declared, never chosen by the renderer.** At least four attribution bases
are defensible and give different answers for the same question:

* ``report_cutoff`` — one classification per customer, valid at the report cutoff (implemented);
* ``period_end_per_period`` — May's segment for May, June's for June;
* ``transaction_event_time`` — the segment at each individual transaction;
* ``current_value`` — whatever the customer is today (implemented in Release-B Task 8).

**Why ``current_value`` was refused, and what changed (Release-B Task 8 / D12.6).** The original
refusal was not that "today's row" is hard to express — it is one predicate. It was that NOTHING
DECLARED WHICH ROW IS TODAY'S. On an SCD2 table the current row is marked either by an open-ended
``effective_to`` or by a dedicated current flag, and the two conventions coexist in one bank; with
neither declared, an engine picking "the row with the greatest ``effective_from``" silently answers
a different question on every table that keeps a future-dated row. The Release-B temporal policy
(``overlay/upload/temporal_policy.py``) is that missing declaration: it names ``current_flag_ref``,
and its own validation REFUSES ``current_record`` on a history-keeping dataset that declares no
flag. So the basis is now supported — the declaration exists — and the predicate shape follows the
policy's refs rather than a convention chosen here. ``period_end_per_period`` and
``transaction_event_time`` remain refused, and remain business decisions nobody has made: see
:data:`DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED`.

``report_cutoff`` remains bound to the fixture rather than to a real source until an owner confirms
the SCD storage shape.

**Half-open intervals.** ``[effective_from, effective_to)`` — a row starting exactly on the cutoff is
INCLUDED and a row ending exactly on it is EXCLUDED. Any other convention makes adjacent history rows
either both match (duplicating the customer) or both miss (losing them).

**Overlap is refused, never resolved.** Picking ``MAX(effective_from)`` when two records are
simultaneously valid would silently choose one classification and hide a data-quality problem in the
dimension table.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.data_agent.observation import ObservationPlanError, require_identifier

#: Named so a later decision has one place to land, and so it surfaces in a search rather than
#: living only in prose.
DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED = (
    "the attribution basis is a BUSINESS decision and is unconfirmed for the real source: "
    "report_cutoff | period_end_per_period | transaction_event_time | current_value all give "
    "different answers for the same question")


class AttributionError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class AttributionBasis(StrEnum):
    REPORT_CUTOFF = "report_cutoff"                     # implemented
    PERIOD_END_PER_PERIOD = "period_end_per_period"     # refused — undeclared business decision
    TRANSACTION_EVENT_TIME = "transaction_event_time"   # refused — undeclared business decision
    CURRENT_VALUE = "current_value"                     # implemented (Release-B Task 8)


class MissingValueBehavior(StrEnum):
    UNKNOWN_BUCKET = "unknown_bucket"   # keep the customer, classify as "Unknown"
    RETAIN_NULL = "retain_null"         # keep the customer, dimension is NULL
    REFUSE = "refuse"                   # no classification, no analysis


class OverlapBehavior(StrEnum):
    REFUSE = "refuse"


#: The bases with an ENGINE. `current_value` joined it in Release-B Task 8 (module docstring); the
#: other two are still undeclared business decisions, and an engine for them would be a renderer
#: choosing between defensible answers.
_SUPPORTED_BASES = (AttributionBasis.REPORT_CUTOFF, AttributionBasis.CURRENT_VALUE)
UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True)
class DimensionAttributionPolicyV1:
    """How and when a customer is classified."""

    attribution_basis: AttributionBasis
    effective_from_column: str
    effective_to_column: str
    #: The instant to classify at. Required for `report_cutoff`, and REFUSED for `current_value` —
    #: "the row that is current today" and "the row valid at 2026-06-30" are different questions,
    #: and a cutoff carried on the first one is a contradiction, not an unused field.
    report_cutoff: str = ""
    interval_semantics: str = "start_inclusive_end_exclusive"
    missing_value_behavior: MissingValueBehavior = MissingValueBehavior.UNKNOWN_BUCKET
    overlap_behavior: OverlapBehavior = OverlapBehavior.REFUSE
    #: For `current_value`: the column the SOURCE declares its current row with (the temporal
    #: policy's `current_flag_ref`). Empty falls back to the open-ended-row convention
    #: (`effective_to IS NULL`), which is the only other shape a declaration can take.
    #:
    #: A BOOLEAN column, checked by the caller that reads the catalog — the predicate renders
    #: `flag = TRUE`, and rendering that against a `text` flag holding 'Y' is the exact class of
    #: defect that runs clean and answers a different question.
    current_flag_column: str = ""

    def __post_init__(self) -> None:
        if self.attribution_basis not in _SUPPORTED_BASES:
            raise AttributionError(
                "ATTRIBUTION_UNSUPPORTED_BASIS",
                f"attribution basis {self.attribution_basis!r} is not implemented. It is a business "
                f"decision with a different answer per basis — {DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED}")
        if self.interval_semantics != "start_inclusive_end_exclusive":
            raise AttributionError(
                "ATTRIBUTION_UNSUPPORTED_INTERVAL",
                "only half-open [from, to) intervals are implemented; any other convention makes "
                "adjacent history rows either both match or both miss")
        if self.attribution_basis is AttributionBasis.CURRENT_VALUE:
            self._validate_current_value()
            return
        if not self.report_cutoff.strip():
            raise AttributionError(
                "ATTRIBUTION_NO_CUTOFF",
                "report_cutoff attribution needs the instant to classify at; defaulting to 'today' "
                "would silently reattribute a historical report")
        try:
            require_identifier(self.effective_from_column, what="effective_from column")
            require_identifier(self.effective_to_column, what="effective_to column")
        except ObservationPlanError as exc:
            raise AttributionError(exc.code, str(exc).split(": ", 1)[-1]) from None

    def _validate_current_value(self) -> None:
        """`current_value` needs a DECLARATION of which row is today's, and no cutoff."""
        if self.report_cutoff.strip():
            raise AttributionError(
                "ATTRIBUTION_CUTOFF_ON_CURRENT_VALUE",
                f"current_value attribution carries report_cutoff {self.report_cutoff!r}. "
                "'Whatever the customer is today' and 'the classification valid at a cutoff' are "
                "two different questions with two different answers; a cutoff here would be "
                "silently ignored, and the report would claim a point-in-time it never applied")
        declared = self.current_flag_column or self.effective_to_column
        if not declared.strip():
            raise AttributionError(
                "ATTRIBUTION_NO_CURRENT_RULE",
                "current_value attribution needs the source's own declaration of which row is "
                "current — a current flag, or the open-ended-row convention's effective_to column. "
                "Without one, 'the current row' is whichever row the engine reads first, and on a "
                "table carrying a future-dated row that is not today's value at all")
        try:
            require_identifier(declared, what="current-record column")
        except ObservationPlanError as exc:
            raise AttributionError(exc.code, str(exc).split(": ", 1)[-1]) from None

    def validity_predicate(self, quote) -> str:
        """The row test for this basis. One predicate, rendered through the DIALECT's quoting."""
        if self.attribution_basis is AttributionBasis.CURRENT_VALUE:
            return self._current_predicate(quote)
        return self._cutoff_predicate(quote)

    def _cutoff_predicate(self, quote) -> str:
        """The half-open interval test at the cutoff.

        `effective_from <= cutoff` INCLUDES a row starting exactly on it; `effective_to > cutoff`
        EXCLUDES one ending exactly on it. The two rules are complementary on purpose: for adjacent
        rows they select exactly one, whereas `>=`/`<=` on both sides would select both.

        An open-ended row (`effective_to IS NULL`) is current and therefore valid.
        """
        cutoff = "'" + self.report_cutoff.replace("'", "''") + "'"
        start, end = quote(self.effective_from_column), quote(self.effective_to_column)
        return f"{start} <= {cutoff} AND ({end} > {cutoff} OR {end} IS NULL)"

    def _current_predicate(self, quote) -> str:
        """ENGINE A. The row the SOURCE says is current — flag first, open-ended row otherwise.

        Flag first because it is an explicit declaration and the open-ended convention is an
        inference from a NULL: a table that closes its last row on a scheduled date has no
        open-ended row at all, and the two shapes disagree exactly there.
        """
        if self.current_flag_column:
            return f"{quote(self.current_flag_column)} = TRUE"
        return f"{quote(self.effective_to_column)} IS NULL"
