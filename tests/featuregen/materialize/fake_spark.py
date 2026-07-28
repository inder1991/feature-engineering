"""A tiny, honest stand-in for the slice of ``pyspark.sql`` the rendered nodes use.

**Why this exists.** ``pyspark`` is not installed and no JVM is available, so the rendered nodes
cannot be executed against real Spark here — that is L0's job (§11.2, Task 15). But a renderer test
that only asserts on the rendered *text* is satisfied by text: an implementation that resolved every
tie with ``row_number()`` and one that refuses with ``SPINE_NON_DETERMINISTIC`` differ by one
identifier, and a string assertion that happens to name the right identifier proves nothing about
what the code DOES. This module lets the rendered source be ``exec``'d and its rows inspected, so
"the availability filter is missing" and "the tie is silently resolved" fail as behaviour.

**It is a stand-in, not an implementation.** It models exactly what the spine node needs, over plain
Python lists:

* ``RANK`` semantics are the real ones — tied rows all take the rank of the first of them, which is
  what makes an unresolved tie observable at all. ``ROW_NUMBER`` is deliberately provided too, so a
  test can prove the rendered code does not use it.
* ``to_utc_timestamp(ts, zone)`` reads ``ts``'s wall-clock fields AS ``zone`` and returns the UTC
  instant, matching Spark under a UTC session timezone.
* Nulls, types, coercion, partitioning and every optimisation are OUT of scope. A test that needs
  any of those needs real Spark, which is L0.

Nothing here may be imported from ``src`` — it is test support, and the package-wide rule is that
``pyspark`` (real or fake) never appears outside rendered text.
"""
from __future__ import annotations

import datetime as _dt
from collections.abc import Callable, Iterable, Sequence
from typing import Any
from zoneinfo import ZoneInfo

__all__ = ["DataFrame", "Window", "functions", "run_rendered"]

_MISSING = object()


class Column:
    """One expression. ``_eval`` answers it for a row; ``_window`` marks the windowed ones."""

    def __init__(self, name: str, evaluate: Callable[[dict[str, Any]], Any],
                 *, window: _WindowExpr | None = None) -> None:
        self._name = name
        self._evaluate = evaluate
        self._window = window

    # ── naming ───────────────────────────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    def alias(self, name: str) -> Column:
        return Column(name, self._evaluate, window=self._window)

    # ── evaluation ───────────────────────────────────────────────────────────────────────────────

    def _eval(self, row: dict[str, Any]) -> Any:
        return self._evaluate(row)

    # ── comparison and ordering ──────────────────────────────────────────────────────────────────

    def _compare(self, other: Any, op: Callable[[Any, Any], Any], symbol: str) -> Column:
        right = other._evaluate if isinstance(other, Column) else (lambda _row, v=other: v)
        return Column(f"({self._name} {symbol} {getattr(other, 'name', other)})",
                      lambda row: op(self._evaluate(row), right(row)))

    def __le__(self, other: Any) -> Column:
        return self._compare(other, lambda a, b: a <= b, "<=")

    def __lt__(self, other: Any) -> Column:
        return self._compare(other, lambda a, b: a < b, "<")

    def __ge__(self, other: Any) -> Column:
        return self._compare(other, lambda a, b: a >= b, ">=")

    def __gt__(self, other: Any) -> Column:
        return self._compare(other, lambda a, b: a > b, ">")

    def __eq__(self, other: Any) -> Column:  # type: ignore[override]
        return self._compare(other, lambda a, b: a == b, "==")

    def __ne__(self, other: Any) -> Column:  # type: ignore[override]
        return self._compare(other, lambda a, b: a != b, "!=")

    def __and__(self, other: Column) -> Column:
        return Column(f"({self._name} AND {other._name})",
                      lambda row: bool(self._evaluate(row)) and bool(other._evaluate(row)))

    def __or__(self, other: Column) -> Column:
        return Column(f"({self._name} OR {other._name})",
                      lambda row: bool(self._evaluate(row)) or bool(other._evaluate(row)))

    def __hash__(self) -> int:
        return id(self)

    def isin(self, values: Iterable[Any]) -> Column:
        allowed = list(values)
        return Column(f"({self._name} IN {allowed})", lambda row: self._evaluate(row) in allowed)

    def desc(self) -> _Ordering:
        return _Ordering(self, descending=True)

    def asc(self) -> _Ordering:
        return _Ordering(self, descending=False)


class _Ordering:
    def __init__(self, column: Column, *, descending: bool) -> None:
        self.column = column
        self.descending = descending


def _as_ordering(value: Column | _Ordering) -> _Ordering:
    return value if isinstance(value, _Ordering) else _Ordering(value, descending=False)


class _WindowSpec:
    def __init__(self, partition: Sequence[Column], order: Sequence[_Ordering]) -> None:
        self.partition = tuple(partition)
        self.order = tuple(order)

    def orderBy(self, *columns: Column | _Ordering) -> _WindowSpec:  # noqa: N802 — Spark's name
        return _WindowSpec(self.partition, [_as_ordering(column) for column in columns])

    def partitionBy(self, *columns: Column) -> _WindowSpec:  # noqa: N802 — Spark's name
        return _WindowSpec(columns, self.order)


class Window:
    """Spark's ``Window`` entry points, only the two the spine needs."""

    @staticmethod
    def partitionBy(*columns: Column) -> _WindowSpec:  # noqa: N802 — Spark's name
        return _WindowSpec(columns, ())

    @staticmethod
    def orderBy(*columns: Column | _Ordering) -> _WindowSpec:  # noqa: N802 — Spark's name
        return _WindowSpec((), [_as_ordering(column) for column in columns])


class _WindowExpr:
    """A ranking function awaiting a window. ``over`` binds it; the DataFrame applies it."""

    def __init__(self, kind: str) -> None:
        self.kind = kind
        self.spec: _WindowSpec | None = None

    def over(self, spec: _WindowSpec) -> Column:
        bound = _WindowExpr(self.kind)
        bound.spec = spec
        return Column(self.kind, lambda _row: _MISSING, window=bound)


def _sort_key(row: dict[str, Any], order: Sequence[_Ordering]) -> tuple[Any, ...]:
    return tuple(ordering.column._eval(row) for ordering in order)


def _sorted_partition(rows: list[dict[str, Any]],
                      order: Sequence[_Ordering]) -> list[dict[str, Any]]:
    """Stable multi-key sort, applied least-significant key first (Python's sorts are stable)."""
    ordered = list(rows)
    for ordering in reversed(order):
        ordered.sort(key=lambda row, o=ordering: o.column._eval(row), reverse=ordering.descending)
    return ordered


class _GroupedData:
    def __init__(self, rows: list[dict[str, Any]], keys: Sequence[Column]) -> None:
        self._rows = rows
        self._keys = tuple(keys)

    def count(self) -> DataFrame:
        """``groupBy(...).count()``, whose count column is named ``count`` — as in Spark."""
        counts: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in self._rows:
            key = tuple(column._eval(row) for column in self._keys)
            bucket = counts.get(key)
            if bucket is None:
                bucket = {column.name: value for column, value in zip(self._keys, key, strict=True)}
                bucket["count"] = 0
                counts[key] = bucket
            bucket["count"] += 1
        return DataFrame(list(counts.values()))


class DataFrame:
    """Rows as dicts. Column order follows first-seen order, which is all any test here reads."""

    def __init__(self, rows: Sequence[dict[str, Any]]) -> None:
        self._rows = [dict(row) for row in rows]

    # ── inspection, for tests ────────────────────────────────────────────────────────────────────

    @property
    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]

    @property
    def columns(self) -> list[str]:
        seen: list[str] = []
        for row in self._rows:
            for name in row:
                if name not in seen:
                    seen.append(name)
        return seen

    # ── the operations the rendered spine uses ───────────────────────────────────────────────────

    def where(self, condition: Column) -> DataFrame:
        return DataFrame([row for row in self._rows if bool(condition._eval(row))])

    filter = where

    def withColumn(self, name: str, expression: Column) -> DataFrame:  # noqa: N802 — Spark's name
        if expression._window is not None:
            return DataFrame(self._windowed(name, expression._window))
        return DataFrame([{**row, name: expression._eval(row)} for row in self._rows])

    def _windowed(self, name: str, window: _WindowExpr) -> list[dict[str, Any]]:
        spec = window.spec
        assert spec is not None, "a window expression reached a DataFrame without `.over(...)`"
        partitions: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in self._rows:
            key = tuple(column._eval(row) for column in spec.partition)
            partitions.setdefault(key, []).append(row)
        produced: list[dict[str, Any]] = []
        for members in partitions.values():
            ordered = _sorted_partition(members, spec.order)
            keys = [_sort_key(row, spec.order) for row in ordered]
            for index, row in enumerate(ordered):
                if window.kind == "rank":
                    # REAL rank: every row tied on the complete ordering takes the rank of the
                    # first of them. That is what makes an unresolved tie observable.
                    value = keys.index(keys[index]) + 1
                elif window.kind == "row_number":
                    value = index + 1
                else:  # pragma: no cover — a kind this stand-in was never asked for
                    raise NotImplementedError(f"fake_spark models no window function {window.kind!r}")
                produced.append({**row, name: value})
        return produced

    def select(self, *expressions: Column) -> DataFrame:
        return DataFrame([{column.name: column._eval(row) for column in expressions}
                          for row in self._rows])

    def drop(self, *names: str) -> DataFrame:
        dropped = set(names)
        return DataFrame([{k: v for k, v in row.items() if k not in dropped} for row in self._rows])

    def groupBy(self, *columns: Column) -> _GroupedData:  # noqa: N802 — Spark's name
        return _GroupedData(self._rows, columns)

    def limit(self, n: int) -> DataFrame:
        return DataFrame(self._rows[:n])

    def count(self) -> int:
        return len(self._rows)

    def distinct(self) -> DataFrame:  # pragma: no cover — present so its USE is detectable
        raise AssertionError(
            "the rendered node called distinct(): §4.2 rule 5 makes duplicate spine keys a "
            "BLOCKING gate, never a de-duplication step")

    def dropDuplicates(self, *args: Any) -> DataFrame:  # noqa: N802 # pragma: no cover
        raise AssertionError(
            "the rendered node called dropDuplicates(): §4.2 rule 5 makes duplicate spine keys a "
            "BLOCKING gate, never a de-duplication step")


class _Functions:
    """``pyspark.sql.functions``, only the members the spine renders."""

    @staticmethod
    def col(name: str) -> Column:
        return Column(name, lambda row, n=name: row[n])

    @staticmethod
    def lit(value: Any) -> Column:
        return Column(repr(value), lambda _row, v=value: v)

    @staticmethod
    def rank() -> _WindowExpr:
        return _WindowExpr("rank")

    @staticmethod
    def row_number() -> _WindowExpr:  # noqa: N802 — Spark's name
        return _WindowExpr("row_number")

    @staticmethod
    def to_timestamp(column: Column) -> Column:
        def parse(row: dict[str, Any]) -> Any:
            value = column._eval(row)
            return value if isinstance(value, _dt.datetime) else _dt.datetime.fromisoformat(value)
        return Column(f"to_timestamp({column.name})", parse)

    @staticmethod
    def to_utc_timestamp(column: Column, zone: str) -> Column:
        def convert(row: dict[str, Any]) -> Any:
            value = column._eval(row)
            naive = value if isinstance(value, _dt.datetime) else _dt.datetime.fromisoformat(value)
            # Spark reads the wall-clock FIELDS as `zone` and returns the UTC instant. Under a UTC
            # session timezone that is exactly "this local time, expressed in UTC".
            local = naive.replace(tzinfo=ZoneInfo(zone))
            return local.astimezone(_dt.UTC).replace(tzinfo=None)
        return Column(f"to_utc_timestamp({column.name}, {zone})", convert)

    @staticmethod
    def to_date(column: Column) -> Column:
        def parse(row: dict[str, Any]) -> Any:
            value = column._eval(row)
            if isinstance(value, _dt.datetime):
                return value.date()
            return value if isinstance(value, _dt.date) else _dt.date.fromisoformat(value)
        return Column(f"to_date({column.name})", parse)


functions = _Functions()


def run_rendered(source: str, func_name: str) -> Callable[..., Any]:
    """``exec`` a rendered node's source against this stand-in and return its function.

    The node's imports are supplied here rather than executed from the source, because a
    :class:`~featuregen.materialize.render.project.RenderedNode` forbids imports inside ``source``
    (they are declared on the node so the rendered module can de-duplicate them).
    """
    namespace: dict[str, Any] = {
        "DataFrame": DataFrame, "F": functions, "Window": Window}
    exec(compile(source, f"<rendered {func_name}>", "exec"), namespace)  # noqa: S102 — the point
    return namespace[func_name]  # type: ignore[no-any-return]
