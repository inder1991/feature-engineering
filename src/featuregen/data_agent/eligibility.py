"""Which transactions count — status and reversal.

Functional correctness, not a filter: a reversed transaction that counts inflates a customer's
activity, and a customer whose only current transaction was reversed reads as active when they are
not. It changes the answer, so it belongs in the plan rather than in whoever writes the SQL.

**Nothing is hardcoded.** `tran_status = 'POSTED'` and `reversal_flag = 'N'` are one source's
vocabulary, not a universal truth, so the meaning is declared per source and carried in the IR. A
renderer that invented a column name or a status code would be guessing at bank semantics.

**Unsupported reversal shapes are REFUSED, never approximated.** Reversals are represented in at
least four ways in the wild — a flag, a compensating row, a link to a reversing transaction id, a
status history, or a negative entry. Only the flag/code shape is implemented. Treating a
compensating-row source as a flag source would silently double-count the original AND its reversal.

**An unresolved pilot fact, recorded rather than decided:** "reversed" may mean *reversed by the
report cutoff* or *reversed at any later time*. Those give different answers for the same month, and
which is right depends on how the source stores reversal history and when it becomes visible. The
renderer must not choose; see :data:`REVERSAL_AS_OF_UNRESOLVED`.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.data_agent.observation import ObservationPlanError, require_identifier

#: See the module docstring. Kept as a named constant so it appears in a search rather than living
#: only in prose, and so a later decision has one place to land.
REVERSAL_AS_OF_UNRESOLVED = (
    "whether 'reversed' means reversed BY THE REPORT CUTOFF or reversed AT ANY LATER TIME is an "
    "unresolved pilot fact; it depends on how the source stores reversal history and availability")


class EligibilityError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class ReversalMode(StrEnum):
    """How a source represents a reversal."""

    BOOLEAN_OR_CODE_COLUMN = "boolean_or_code_column"   # implemented
    COMPENSATING_ROW = "compensating_row"               # refused in v1
    LINKED_REVERSAL_ID = "linked_reversal_id"           # refused in v1
    STATUS_HISTORY = "status_history"                   # refused in v1
    NEGATIVE_ENTRY = "negative_entry"                   # refused in v1


class NullBehavior(StrEnum):
    EXCLUDE = "exclude"
    INCLUDE = "include"


_SUPPORTED_MODES = (ReversalMode.BOOLEAN_OR_CODE_COLUMN,)


@dataclass(frozen=True, slots=True)
class TransactionEligibilityPolicyV1:
    """One source's definition of a transaction that counts."""

    status_column: str
    included_status_values: tuple[str, ...]
    reversal_mode: ReversalMode
    reversal_column: str
    non_reversed_values: tuple[str, ...]
    #: What an unknown status or unknown reversal state means. `exclude` by default: a transaction
    #: whose status nobody recorded is not evidence that it posted.
    null_behavior: NullBehavior = NullBehavior.EXCLUDE

    def __post_init__(self) -> None:
        if self.reversal_mode not in _SUPPORTED_MODES:
            raise EligibilityError(
                "ELIGIBILITY_UNSUPPORTED_REVERSAL_MODE",
                f"reversal mode {self.reversal_mode!r} is not implemented. Treating it as a flag "
                "would silently count both a transaction and its reversal")
        try:
            require_identifier(self.status_column, what="status column")
            require_identifier(self.reversal_column, what="reversal column")
        except ObservationPlanError as exc:
            raise EligibilityError(exc.code, str(exc).split(": ", 1)[-1]) from None
        if not self.included_status_values:
            raise EligibilityError(
                "ELIGIBILITY_NO_STATUS_VALUES",
                "no status counts as eligible; an empty list excludes every transaction")
        if not self.non_reversed_values:
            raise EligibilityError(
                "ELIGIBILITY_NO_NON_REVERSED_VALUES",
                "no reversal value counts as non-reversed; an empty list excludes everything")

    def predicates(self, quote) -> tuple[str, ...]:
        """SQL predicates, for placement INSIDE each period aggregation.

        `quote` is the dialect's identifier quoter, so this never hardcodes backticks or
        double-quotes for one engine.
        """
        status, reversal = quote(self.status_column), quote(self.reversal_column)
        statuses = ", ".join("'" + v.replace("'", "''") + "'"
                             for v in self.included_status_values)
        non_reversed = ", ".join("'" + v.replace("'", "''") + "'"
                                 for v in self.non_reversed_values)
        out = [f"{status} IN ({statuses})", f"{reversal} IN ({non_reversed})"]
        if self.null_behavior is NullBehavior.INCLUDE:
            out = [f"({status} IS NULL OR {out[0]})", f"({reversal} IS NULL OR {out[1]})"]
        # With EXCLUDE, `IN` already drops NULL — SQL's three-valued logic makes `NULL IN (...)`
        # UNKNOWN, which a WHERE treats as false. That is the desired behaviour, stated here so a
        # later reader does not "fix" it by adding an IS NOT NULL that changes nothing.
        return tuple(out)
