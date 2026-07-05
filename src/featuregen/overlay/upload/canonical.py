from __future__ import annotations

from dataclasses import dataclass, field

_REQUIRED = ("source", "table", "column", "type")


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
    row: "CanonicalRow | None" = None


@dataclass(frozen=True, slots=True)
class ValidationResult:
    good: list[CanonicalRow] = field(default_factory=list)
    quarantined: list[RowError] = field(default_factory=list)
    structural_error: str | None = None


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
    seen: dict[tuple[str, str, str], str] = {}  # (source,table,column) -> type

    for i, r in enumerate(rows):
        missing = [f for f in _REQUIRED if not getattr(r, f)]
        if missing:
            quarantined.append(RowError(i, f"missing required field(s): {', '.join(missing)}", r))
            continue
        if catalog_source is not None and r.source != catalog_source:
            quarantined.append(RowError(
                i, f"row source '{r.source}' does not match upload source '{catalog_source}'", r))
            continue
        key = (r.source, r.table, r.column)
        if key in seen:
            if seen[key] == r.type:
                continue  # identical duplicate -> dedup
            quarantined.append(
                RowError(i, f"conflicting type for {key}: {seen[key]} vs {r.type}", r))
            continue
        seen[key] = r.type
        good.append(r)

    return ValidationResult(good=good, quarantined=quarantined, structural_error=None)
