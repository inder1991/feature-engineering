"""Point-in-time dimension attribution — which segment classifies a customer.

Joining *today's* segment to *last month's* transactions silently reattributes activity whenever a
customer changes segment, and the answer looks entirely reasonable. It is the same class of defect
as counting reversed transactions: it changes the number, not the presentation.

**The business decision is declared, never chosen by the renderer.** At least four attribution bases
are defensible and give different answers for the same question:

* ``report_cutoff`` — one classification per customer, valid at the report cutoff (implemented);
* ``period_end_per_period`` — May's segment for May, June's for June;
* ``transaction_event_time`` — the segment at each individual transaction;
* ``current_value`` — whatever the customer is today.

Only ``report_cutoff`` is implemented, and it is bound to the fixture rather than to a real source
until an owner confirms both the SCD storage shape and the intended rule — see
:data:`DIMENSION_ATTRIBUTION_AS_OF_UNRESOLVED`.

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
    PERIOD_END_PER_PERIOD = "period_end_per_period"     # refused in v1
    TRANSACTION_EVENT_TIME = "transaction_event_time"   # refused in v1
    CURRENT_VALUE = "current_value"                     # refused in v1


class MissingValueBehavior(StrEnum):
    UNKNOWN_BUCKET = "unknown_bucket"   # keep the customer, classify as "Unknown"
    RETAIN_NULL = "retain_null"         # keep the customer, dimension is NULL
    REFUSE = "refuse"                   # no classification, no analysis


class OverlapBehavior(StrEnum):
    REFUSE = "refuse"


_SUPPORTED_BASES = (AttributionBasis.REPORT_CUTOFF,)
UNKNOWN = "Unknown"


@dataclass(frozen=True, slots=True)
class DimensionAttributionPolicyV1:
    """How and when a customer is classified."""

    attribution_basis: AttributionBasis
    effective_from_column: str
    effective_to_column: str
    #: The instant to classify at. Required for `report_cutoff`.
    report_cutoff: str = ""
    interval_semantics: str = "start_inclusive_end_exclusive"
    missing_value_behavior: MissingValueBehavior = MissingValueBehavior.UNKNOWN_BUCKET
    overlap_behavior: OverlapBehavior = OverlapBehavior.REFUSE

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

    def validity_predicate(self, quote) -> str:
        """The half-open interval test at the cutoff.

        `effective_from <= cutoff` INCLUDES a row starting exactly on it; `effective_to > cutoff`
        EXCLUDES one ending exactly on it. The two rules are complementary on purpose: for adjacent
        rows they select exactly one, whereas `>=`/`<=` on both sides would select both.

        An open-ended row (`effective_to IS NULL`) is current and therefore valid.
        """
        cutoff = "'" + self.report_cutoff.replace("'", "''") + "'"
        start, end = quote(self.effective_from_column), quote(self.effective_to_column)
        return f"{start} <= {cutoff} AND ({end} > {cutoff} OR {end} IS NULL)"
