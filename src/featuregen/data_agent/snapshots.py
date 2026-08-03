"""ENGINE B — latest snapshot at or before a cutoff. NEW analysis-path work (D12.6).

The profile plan's §6.5 lists three selection kinds and describes all three as adapters. Two were
not: ``current_record`` mapped to a basis :mod:`featuregen.data_agent.dimensions` declared and
REFUSED, and ``latest_snapshot_as_of`` had no analysis-path implementation at all. The only
latest-as-of logic on the tree is ``materialize/spine.py``'s ``LatestAvailableAsOf``, which the
analysis path does not import and must not: that module is the FEATURE stack's point-in-time
contract, it is owned by a separate session, and reaching across for it would make one change to a
Spark spine silently re-answer a data-agent question. It is the SEMANTIC reference for what
"latest at or before" means, and nothing more.

**The row rule.** For each entity (or once for the whole table), keep the row carrying the GREATEST
``snapshot_column`` value that is ``<= cutoff``. A snapshot table answers "as it stood then", so a
row stamped after the cutoff is a fact nobody could have known — the same look-ahead defect the
availability-basis findings exist for, one layer down.

**A tie REFUSES.** Two rows sharing the greatest snapshot value for one entity mean the answer
depends on read order. Declaring ``tie_break_refs`` on the temporal policy resolves it — the
columns join the ordering, and a distinct tie-break tuple makes the winner deterministic. With no
tie-breaker, or with tie-breakers that still leave two identical rows, this refuses
:data:`~...source_selection.TEMPORAL_SNAPSHOT_TIE` rather than picking. That refusal is a
DECLARATION gap, not a data defect (unlike SCD overlap): naming a tie-break column closes it, which
is exactly why ``data_agent.learning`` routes it to ``SNAPSHOT_TIE_BREAK_UNDECLARED`` while it
deliberately routes overlap nowhere.

**Ordering is DESC, and the tie-breakers are too.** "Latest" is the greatest snapshot value; a
tie-break column is read the same way, so a declared ``load_seq``/``version`` picks the highest.
Any other reading would have to be declared, and no frozen contract carries that declaration.

**Rendered through the caller's dialect**, exactly like every other predicate in this package:
``quote`` is the dialect's ``ident`` and the table reference is the dialect's ``table_ref``. A
hardcoded ``"name"`` is an identifier in PostgreSQL and a string LITERAL in HiveQL — the defect
``compile_analysis`` records at its own quoting seam.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.data_agent.dimensions import MissingValueBehavior
from featuregen.data_agent.observation import ObservationPlanError, require_identifier


class SnapshotSelectionError(ValueError):
    """A typed snapshot row-selection refusal. ``code`` is closed."""

    def __init__(self, code: str, message: str, *, subjects: tuple[str, ...] = ()) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.subjects = subjects


class SnapshotScope(StrEnum):
    """WHOSE latest snapshot. Per-entity is the usual shape; per-table is a reference/lookup
    dataset republished whole, where "the latest snapshot" is one instant for every row."""

    PER_ENTITY = "per_entity"
    PER_TABLE = "per_table"


@dataclass(frozen=True, slots=True)
class LatestSnapshotPolicyV1:
    """The compiled form of a ``latest_snapshot_as_of`` row selection.

    The CUTOFF is a runtime VALUE here, deliberately: the immutable
    :class:`DatasetRowSelectionV1` carries the cutoff's REF, and the value is a parameter that must
    never enter a decision identity or the same governed decision would re-key every day it is
    replayed (``temporal_policy._reject_literal_cutoff``). This dataclass is the engine input, not
    the record.
    """

    snapshot_column: str
    cutoff: str
    scope: SnapshotScope = SnapshotScope.PER_ENTITY
    key_column: str = ""
    tie_break_columns: tuple[str, ...] = ()
    #: The SAME population guarantee the SCD path keeps: an entity with no snapshot at or before the
    #: cutoff is a bucket, not a disappearance. Shared with `dimensions` rather than re-declared —
    #: two vocabularies for "what happens to the unclassified" is how the two row rules end up
    #: quietly answering different questions about the same population.
    missing_value_behavior: MissingValueBehavior = MissingValueBehavior.UNKNOWN_BUCKET

    def __post_init__(self) -> None:
        try:
            require_identifier(self.snapshot_column, what="snapshot column")
            if self.scope is SnapshotScope.PER_ENTITY:
                require_identifier(self.key_column, what="snapshot key column")
            for column in self.tie_break_columns:
                require_identifier(column, what="tie-break column")
        except ObservationPlanError as exc:
            raise SnapshotSelectionError(exc.code, str(exc).split(": ", 1)[-1]) from None
        if not self.cutoff.strip():
            raise SnapshotSelectionError(
                "SNAPSHOT_NO_CUTOFF",
                "latest-snapshot selection needs the instant to read as of; defaulting to 'now' "
                "would answer a historical question with today's snapshot")
        if self.scope is SnapshotScope.PER_TABLE and self.key_column:
            raise SnapshotSelectionError(
                "SNAPSHOT_SCOPE_CONTRADICTION",
                "a per-table snapshot selection names no entity key: declaring one says the latest "
                "snapshot is per entity, which is the other scope")
        if len(set(self.tie_break_columns)) != len(self.tie_break_columns):
            raise SnapshotSelectionError(
                "SNAPSHOT_TIE_BREAK_REPEATED",
                "tie-break columns must be distinct; a repeated column adds no ordering")

    # ── the compiled shape ───────────────────────────────────────────────────────────────────────

    def cutoff_predicate(self, quote) -> str:
        """Rows that had been published by the cutoff. A row stamped later is a look-ahead leak."""
        literal = "'" + self.cutoff.replace("'", "''") + "'"
        return f"{quote(self.snapshot_column)} <= {literal}"

    def _ordering(self, quote) -> str:
        parts = [f"{quote(self.snapshot_column)} DESC"]
        parts += [f"{quote(column)} DESC" for column in self.tie_break_columns]
        return ", ".join(parts)

    def _partition(self, quote) -> str:
        if self.scope is SnapshotScope.PER_TABLE:
            return ""
        return f"PARTITION BY {quote(self.key_column)} "

    def ranked_selection(self, quote, *, table_ref: str, columns: tuple[str, ...],
                         key_alias: str = "k") -> str:
        """The CTE BODY selecting exactly the latest row per scope.

        ``ROW_NUMBER`` rather than ``MAX(snapshot)`` + rejoin: the rejoin re-duplicates every tied
        row, which is the state :func:`assert_no_snapshot_tie` exists to refuse rather than absorb.
        Both dialects in this package support the window function.
        """
        projected = ", ".join(quote(column) for column in columns)
        key_select = (f"{quote(self.key_column)} AS {quote(key_alias)}, "
                      if self.scope is SnapshotScope.PER_ENTITY else "")
        inner_key = f"{quote(key_alias)}, " if self.scope is SnapshotScope.PER_ENTITY else ""
        return (
            f"SELECT {inner_key}{projected} FROM ("
            f"SELECT {key_select}{projected}, "
            f"ROW_NUMBER() OVER ({self._partition(quote)}ORDER BY {self._ordering(quote)}) AS rn "
            f"FROM {table_ref} WHERE {self.cutoff_predicate(quote)}) ranked WHERE rn = 1")

    def tie_probe(self, quote, *, table_ref: str) -> str:
        """The DATA question: does any scope have two rows the ordering cannot separate?

        ``RANK`` (not ``ROW_NUMBER``) is the point — it gives 1 to EVERY row tied on the whole
        ordering tuple, so a declared tie-breaker that genuinely separates them collapses the count
        to one and the probe passes. That is what makes "the policy's tie_break_refs resolve it
        deterministically" a testable statement rather than a hope.
        """
        ordering = self._ordering(quote)
        if self.scope is SnapshotScope.PER_TABLE:
            return (f"SELECT COUNT(*) FROM (SELECT RANK() OVER (ORDER BY {ordering}) AS r "
                    f"FROM {table_ref} WHERE {self.cutoff_predicate(quote)}) ranked WHERE r = 1")
        key = quote(self.key_column)
        return (
            f"SELECT COUNT(*) FROM (SELECT {key} AS k FROM ("
            f"SELECT {key}, RANK() OVER (PARTITION BY {key} ORDER BY {ordering}) AS r "
            f"FROM {table_ref} WHERE {self.cutoff_predicate(quote)}) ranked "
            f"WHERE r = 1 GROUP BY {key} HAVING COUNT(*) > 1) tied")


def assert_no_snapshot_tie(conn, policy: LatestSnapshotPolicyV1, *, table_ref: str,
                           dialect) -> None:
    """Refuse when the greatest snapshot at or before the cutoff is not a single row.

    The refusal carries the CLOSED selection code so the clarification and the learning gap already
    wired for it fire without a translation table. Imported lazily: at module scope the selection
    vocabulary drags the contract package into every importer of this engine, which is the same
    trade ``binding_store`` records for the same constant.
    """
    from featuregen.overlay.upload.source_selection import TEMPORAL_SNAPSHOT_TIE

    cursor = conn.cursor()
    try:
        cursor.execute(policy.tie_probe(dialect.ident, table_ref=table_ref))
        tied = int(cursor.fetchone()[0])
    finally:
        close = getattr(cursor, "close", None)
        if callable(close):
            close()
    if policy.scope is SnapshotScope.PER_TABLE:
        if tied <= 1:
            return
        raise SnapshotSelectionError(
            TEMPORAL_SNAPSHOT_TIE,
            f"{tied} rows share the greatest snapshot at or before {policy.cutoff!r} and nothing "
            "declared separates them, so which one answers the question is read order. Declare a "
            "tie-break column on the dataset's temporal policy",
            subjects=(policy.snapshot_column,))
    if not tied:
        return
    raise SnapshotSelectionError(
        TEMPORAL_SNAPSHOT_TIE,
        f"{tied} entit(ies) have more than one row at the greatest snapshot on or before "
        f"{policy.cutoff!r}, and the declared tie-breakers "
        f"({', '.join(policy.tie_break_columns) or 'none'}) do not separate them. Choosing one "
        "would make the answer depend on read order",
        subjects=(policy.key_column, policy.snapshot_column))
