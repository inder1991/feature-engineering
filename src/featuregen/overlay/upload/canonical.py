from __future__ import annotations

from dataclasses import dataclass, field

from featuregen.overlay.upload.read_scope import SENSITIVITY_ROLES

_REQUIRED = ("source", "table", "column", "type")
_VALID_SENSITIVITY = frozenset({"", *SENSITIVITY_ROLES})   # "" (none) + the recognized tags


@dataclass(frozen=True, slots=True)
class CanonicalRow:
    source: str
    table: str
    column: str
    type: str
    is_grain: bool = False
    as_of: bool = False
    as_of_basis: str = ""     # posted_at | ingested_at (how availability is derived)
    definition: str = ""
    sensitivity: str = ""
    joins_to: str = ""        # target "table.column" (single-column join)
    cardinality: str = ""     # N:1 | 1:1 | 1:N
    additivity: str = ""      # additive | semi_additive | non_additive (safe-aggregation)
    unit: str = ""            # e.g. dollars, cents
    currency: str = ""        # e.g. USD
    entity: str = ""          # the business entity this column denotes (Customer, Account)


@dataclass(frozen=True, slots=True)
class RowError:
    row_index: int
    message: str
    row: CanonicalRow | None = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    good: list[CanonicalRow] = field(default_factory=list)
    quarantined: list[RowError] = field(default_factory=list)
    structural_error: str | None = None


def _material(r: CanonicalRow) -> tuple:
    """The load-bearing fields of a row (everything but the advisory `definition`). Two rows for the
    same column that agree here are true duplicates; disagreement is a conflict worth quarantining."""
    return (r.type, r.is_grain, r.as_of, r.as_of_basis, r.sensitivity, r.joins_to,
            r.cardinality, r.additivity, r.unit, r.currency, r.entity)


def validate_rows(rows: list[CanonicalRow],
                  catalog_source: str | None = None) -> ValidationResult:
    """Validate rows for one upload. When `catalog_source` is given (the upload's source), any row
    declaring a DIFFERENT source is quarantined — the upload is single-source (T3), and downstream
    (facts, graph) key object identity on this one source, so a foreign-source row would collide."""
    if not rows:
        return ValidationResult(structural_error="empty upload: no rows")
    if all(not r.source for r in rows):
        return ValidationResult(structural_error="no row has a source")

    good: list[CanonicalRow] = []
    quarantined: list[RowError] = []
    seen: dict[tuple[str, str, str], tuple[CanonicalRow, int]] = {}  # key -> (first row, its index)
    conflicted: set[tuple[str, str, str]] = set()

    for i, r in enumerate(rows):
        missing = [f for f in _REQUIRED if not getattr(r, f)]
        if missing:
            quarantined.append(RowError(i, f"missing required field(s): {', '.join(missing)}", r))
            continue
        if catalog_source is not None and r.source != catalog_source:
            quarantined.append(RowError(
                i, f"row source '{r.source}' does not match upload source '{catalog_source}'", r))
            continue
        if r.sensitivity not in _VALID_SENSITIVITY:
            # An unrecognized sensitivity would make the node invisible to EVERY role (fail-closed
            # but silent). Quarantine it instead so the reviewer fixes/normalizes the value.
            quarantined.append(RowError(
                i, f"unrecognized sensitivity '{r.sensitivity}' "
                f"(expected one of: {', '.join(sorted(_VALID_SENSITIVITY - {''}))})", r))
            continue
        key = (r.source, r.table, r.column)
        if key in seen:
            first_row, first_i = seen[key]
            if _material(first_row) == _material(r):
                continue  # same load-bearing metadata (advisory `definition` may differ) -> dedup
            # Same column, differing metadata. Dedup used to keep the FIRST row and silently drop the
            # later one's fields — including `sensitivity`, so a later `pii` tag could be lost and the
            # column left world-readable (fail-OPEN). Fail CLOSED instead: quarantine ALL rows for the
            # column (pull the already-accepted first one back out) so it is NOT graphed until resolved.
            msg = f"conflicting metadata for {key} (rows for the same column disagree)"
            if key not in conflicted:
                conflicted.add(key)
                good.remove(first_row)
                quarantined.append(RowError(first_i, msg, first_row))
            quarantined.append(RowError(i, msg, r))
            continue
        seen[key] = (r, i)
        good.append(r)

    return ValidationResult(good=good, quarantined=quarantined, structural_error=None)
