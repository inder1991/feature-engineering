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


@dataclass(frozen=True, slots=True)
class RowError:
    row_index: int
    message: str


@dataclass(frozen=True, slots=True)
class ValidationResult:
    good: list[CanonicalRow] = field(default_factory=list)
    quarantined: list[RowError] = field(default_factory=list)
    structural_error: str | None = None


def validate_rows(rows: list[CanonicalRow]) -> ValidationResult:
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
            quarantined.append(RowError(i, f"missing required field(s): {', '.join(missing)}"))
            continue
        key = (r.source, r.table, r.column)
        if key in seen:
            if seen[key] == r.type:
                continue  # identical duplicate -> dedup
            quarantined.append(RowError(i, f"conflicting type for {key}: {seen[key]} vs {r.type}"))
            continue
        seen[key] = r.type
        good.append(r)

    return ValidationResult(good=good, quarantined=quarantined, structural_error=None)
