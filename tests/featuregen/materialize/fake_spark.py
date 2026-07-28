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
* ``add_months`` and ``trunc`` are the CALENDAR operations, with Spark's end-of-month clamping.
  They exist so §8 rule 2's "calendar periods, never day counts" is a behaviour a test can see: a
  stand-in that quietly turned three months into ninety days would pass a renderer that did too.
* ``expr`` parses ``INTERVAL <n> <UNIT>`` and NOTHING else. A stand-in that accepted arbitrary SQL
  and returned something inert would make every ``event_time_plus_lag`` test pass whether or not
  the lag was rendered at all — the loosest possible model of the one rule under test.
* Nulls, types, coercion, partitioning and every optimisation are OUT of scope. A test that needs
  any of those needs real Spark, which is L0.

**Every method here raises rather than degrading.** An unmodelled cast, an unmodelled ``trunc``
format and an unparseable ``expr`` all raise :class:`NotImplementedError`. That is the rule that
keeps the stand-in honest: the failure mode of a fake is not being wrong, it is being permissive —
returning something plausible for an operation it does not model, so a test passes for a reason
nobody chose.

Nothing here may be imported from ``src`` — it is test support, and the package-wide rule is that
``pyspark`` (real or fake) never appears outside rendered text.
"""
from __future__ import annotations

import datetime as _dt
import re as _re
from collections.abc import Callable, Iterable, Sequence
from typing import Any
from zoneinfo import ZoneInfo

__all__ = ["DataFrame", "Window", "functions", "run_rendered"]

_MISSING = object()


def _as_datetime(value: Any) -> _dt.datetime:
    """A date or a timestamp as a timestamp — a date becomes MIDNIGHT, as Spark's cast does."""
    if isinstance(value, _dt.datetime):
        return value
    if isinstance(value, _dt.date):
        return _dt.datetime(value.year, value.month, value.day)
    return _dt.datetime.fromisoformat(str(value))


def _as_date(value: Any) -> _dt.date:
    if isinstance(value, _dt.datetime):
        return value.date()
    if isinstance(value, _dt.date):
        return value
    return _dt.date.fromisoformat(str(value))


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

    def __add__(self, other: Any) -> Column:
        """Only ``timestamp + INTERVAL`` — the one addition a rendered PIT node performs."""
        def add(row: dict[str, Any]) -> Any:
            left = self._evaluate(row)
            right = other._evaluate(row) if isinstance(other, Column) else other
            if not isinstance(right, _dt.timedelta):
                raise NotImplementedError(
                    f"fake_spark models Column + INTERVAL only, got {type(right).__name__}")
            return _as_datetime(left) + right
        return Column(f"({self._name} + {getattr(other, 'name', other)})", add)

    def cast(self, sql_type: str) -> Column:
        """``date -> timestamp`` (midnight) and ``timestamp -> date``. Nothing else is modelled."""
        wanted = sql_type.strip().lower()
        if wanted not in {"timestamp", "date"}:
            raise NotImplementedError(f"fake_spark models no cast to {sql_type!r}")

        def convert(row: dict[str, Any]) -> Any:
            value = self._evaluate(row)
            return _as_datetime(value) if wanted == "timestamp" else _as_date(value)
        return Column(f"CAST({self._name} AS {wanted.upper()})", convert)

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
        return Column(f"to_date({column.name})", lambda row: _as_date(column._eval(row)))

    # ── §8 rule 2's CALENDAR arithmetic ──────────────────────────────────────────────────────────

    @staticmethod
    def date_sub(column: Column, days: int) -> Column:
        """Exactly ``days`` days back, on a DATE. A day and a week are whole numbers of days in
        every calendar, which is why these two units may be counted; a month is not."""
        if not isinstance(days, int) or isinstance(days, bool):
            raise NotImplementedError(f"fake_spark models date_sub over whole days, got {days!r}")
        return Column(f"date_sub({column.name}, {days})",
                      lambda row: _as_date(column._eval(row)) - _dt.timedelta(days=days))

    @staticmethod
    def add_months(column: Column, months: int) -> Column:  # noqa: N802 — Spark's name is snake
        """Spark's ``add_months``: real month arithmetic, clamped to the end of the month.

        ``2026-01-31`` plus one month is ``2026-02-28`` — not ``2026-03-03``, which is what adding
        thirty days gives. That difference is the whole of "calendar periods, never day counts", so
        this is modelled properly rather than approximated.
        """
        if not isinstance(months, int) or isinstance(months, bool):
            raise NotImplementedError(
                f"fake_spark models add_months over whole months, got {months!r}")

        def shift(row: dict[str, Any]) -> Any:
            start = _as_date(column._eval(row))
            index = (start.year * 12 + start.month - 1) + months
            year, month = divmod(index, 12)
            last = _MONTH_LENGTHS[month] + (
                1 if month == 1 and (year % 4 == 0 and (year % 100 != 0 or year % 400 == 0)) else 0)
            return _dt.date(year, month + 1, min(start.day, last))
        return Column(f"add_months({column.name}, {months})", shift)

    @staticmethod
    def trunc(column: Column, unit: str) -> Column:
        """The first day of the calendar period ``unit`` names — Spark's ``trunc`` over a date.

        Spark's week starts on MONDAY. Spark returns NULL for an unrecognized format; this raises
        instead, because a NULL boundary silently keeps or drops every row.
        """
        wanted = unit.strip().lower()
        if wanted not in _TRUNC_UNITS:
            raise NotImplementedError(f"fake_spark models no trunc format {unit!r}")

        def truncate(row: dict[str, Any]) -> Any:
            value = _as_date(column._eval(row))
            if wanted == "week":
                return value - _dt.timedelta(days=value.weekday())
            if wanted == "month":
                return value.replace(day=1)
            if wanted == "quarter":
                return value.replace(month=((value.month - 1) // 3) * 3 + 1, day=1)
            return value.replace(month=1, day=1)
        return Column(f"trunc({column.name}, {wanted})", truncate)

    @staticmethod
    def expr(sql: str) -> Column:
        """``INTERVAL <n> <UNIT>`` and nothing else — see this module's docstring."""
        match = _INTERVAL.fullmatch(sql.strip())
        if match is None:
            raise NotImplementedError(
                f"fake_spark parses `INTERVAL <n> <UNIT>` only, got {sql!r}: accepting arbitrary "
                f"SQL and returning something inert is how a lag that was never rendered passes")
        amount, unit = int(match.group(1)), match.group(2).lower().rstrip("s")
        delta = _dt.timedelta(**{f"{unit}s": amount})
        return Column(f"INTERVAL {amount} {unit.upper()}S", lambda _row: delta)


#: Days per month, indexed 0–11. February's leap day is added by ``add_months``.
_MONTH_LENGTHS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

#: The ``trunc`` formats this stand-in models — Spark accepts more spellings of each.
_TRUNC_UNITS = frozenset({"week", "month", "quarter", "year"})

_INTERVAL = _re.compile(r"INTERVAL\s+(\d+)\s+(HOURS?|MINUTES?|SECONDS?)", _re.IGNORECASE)

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
