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
* ``join`` models ``left`` and ``inner`` and REFUSES every other how. Both are modelled deliberately:
  §8 rule 3's LEFT join is only a claim if the stand-in can tell it from an INNER one, and a fake
  that refused ``inner`` would kill that mutant with "not modelled" instead of with the missing
  entity, which is the thing the rule is about.
* ``groupBy(...).agg(...)`` models SUM / COUNT / COUNT DISTINCT with Spark's null handling: they all
  skip nulls, and a SUM over a group with no non-null value is NULL rather than 0. An aggregate over
  a group is evaluated with the group's ROW LIST, so a non-aggregate expression handed to ``agg``
  is refused rather than quietly evaluated against a grouping that never happened.
* ``round`` is HALF_UP and ``bround`` is HALF_EVEN, and a ``decimal(p,s)`` cast that does not fit
  returns NULL — Spark's non-ANSI overflow behaviour, and the reason §9's ``OVERFLOW_VIOLATION``
  check exists at all. If that reading of Spark is wrong, the check and this model are wrong
  together; only L0 can tell.
* Nulls in FILTERS, types, coercion, partitioning and every optimisation are OUT of scope. A test
  that needs any of those needs real Spark, which is L0. Null handling is modelled ONLY where an
  aggregate, a coalesce or a cast reads it.

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
import decimal as _decimal
import hashlib as _hashlib
import json as _json
import pathlib as _pathlib
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


def _is_aggregate(value: Any) -> bool:
    return isinstance(value, Column) and value._aggregate


class Column:
    """One expression. ``_eval`` answers it for a row; ``_window`` marks the windowed ones.

    ``_aggregate`` marks an expression whose ``_eval`` is answered against a GROUP — the list of
    rows in one ``groupBy`` bucket — rather than against a single row. It propagates through every
    composition, so ``F.when(F.count(...) > 0, F.sum(...))`` is still recognizably an aggregate and
    a bare ``F.col(...)`` handed to ``agg`` is still recognizably not one.
    """

    def __init__(self, name: str, evaluate: Callable[[Any], Any],
                 *, window: _WindowExpr | None = None, aggregate: bool = False) -> None:
        self._name = name
        self._evaluate = evaluate
        self._window = window
        self._aggregate = aggregate

    # ── naming ───────────────────────────────────────────────────────────────────────────────────

    @property
    def name(self) -> str:
        return self._name

    def alias(self, name: str) -> Column:
        return Column(name, self._evaluate, window=self._window, aggregate=self._aggregate)

    # ── evaluation ───────────────────────────────────────────────────────────────────────────────

    def _eval(self, row: Any) -> Any:
        return self._evaluate(row)

    # ── comparison and ordering ──────────────────────────────────────────────────────────────────

    def _compare(self, other: Any, op: Callable[[Any, Any], Any], symbol: str) -> Column:
        return Column(f"({self._name} {symbol} {getattr(other, 'name', other)})",
                      lambda row: op(self._evaluate(row), _value_of(other, row)),
                      aggregate=self._aggregate or _is_aggregate(other))

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
                      lambda row: bool(self._evaluate(row)) and bool(other._evaluate(row)),
                      aggregate=self._aggregate or _is_aggregate(other))

    def __or__(self, other: Column) -> Column:
        return Column(f"({self._name} OR {other._name})",
                      lambda row: bool(self._evaluate(row)) or bool(other._evaluate(row)),
                      aggregate=self._aggregate or _is_aggregate(other))

    def __invert__(self) -> Column:
        return Column(f"(NOT {self._name})", lambda row: not bool(self._evaluate(row)),
                      aggregate=self._aggregate)

    def __hash__(self) -> int:
        return id(self)

    # ── null tests: the ONE place this stand-in models a null ─────────────────────────────────────

    def isNull(self) -> Column:  # noqa: N802 — Spark's name
        return Column(f"({self._name} IS NULL)", lambda row: self._evaluate(row) is None,
                      aggregate=self._aggregate)

    def isNotNull(self) -> Column:  # noqa: N802 — Spark's name
        return Column(f"({self._name} IS NOT NULL)", lambda row: self._evaluate(row) is not None,
                      aggregate=self._aggregate)

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
        """``date -> timestamp`` (midnight), ``timestamp -> date``, ``decimal(p,s)``, ``bigint``.

        The decimal cast is the one §9's ``OVERFLOW_VIOLATION`` gate depends on: a value that does
        not fit ``DECIMAL(p, s)`` becomes **NULL** rather than raising, which is Spark's behaviour
        with ANSI mode off and precisely the quiet wrongness the rendered check exists to convert
        into a refusal. Modelling it any other way would make that check untestable.

        ``bigint`` truncates toward zero and passes NULL through — what a node casting a declared
        literal to an integral published type asks for. Nothing here models the 64-bit range: a
        stand-in over Python ints cannot overflow, and pretending otherwise would be a claim about
        Spark this file has no way to check.
        """
        wanted = sql_type.strip().lower()
        decimal_type = _DECIMAL_TYPE.fullmatch(wanted)
        if decimal_type is None and wanted not in {"timestamp", "date", "bigint"}:
            raise NotImplementedError(f"fake_spark models no cast to {sql_type!r}")
        if decimal_type is not None:
            precision, scale = int(decimal_type.group(1)), int(decimal_type.group(2))
            return Column(f"CAST({self._name} AS {wanted.upper()})",
                          lambda row: _fit_decimal(self._evaluate(row), precision, scale),
                          aggregate=self._aggregate)

        def convert(row: Any) -> Any:
            value = self._evaluate(row)
            if wanted == "bigint":
                return None if value is None else int(value)
            return _as_datetime(value) if wanted == "timestamp" else _as_date(value)
        return Column(f"CAST({self._name} AS {wanted.upper()})", convert,
                      aggregate=self._aggregate)

    def isin(self, values: Iterable[Any]) -> Column:
        allowed = list(values)
        return Column(f"({self._name} IN {allowed})", lambda row: self._evaluate(row) in allowed,
                      aggregate=self._aggregate)

    def desc(self) -> _Ordering:
        return _Ordering(self, descending=True)

    def asc(self) -> _Ordering:
        return _Ordering(self, descending=False)


class _When(Column):
    """``F.when(cond, value)`` — NULL where the condition is false until ``otherwise`` says else.

    A subclass rather than a flag so ``otherwise`` exists only on a column that HAS a ``when``:
    Spark rejects it anywhere else, and a stand-in that accepted it would model a shape the
    rendered code could then use without ever being told it is invalid.
    """

    def __init__(self, condition: Column, value: Any) -> None:
        self._condition = condition
        self._value = value
        super().__init__(
            f"CASE WHEN {condition.name} THEN {getattr(value, 'name', value)} END",
            lambda row: (_value_of(value, row) if bool(condition._eval(row)) else None),
            aggregate=condition._aggregate or _is_aggregate(value))

    def otherwise(self, other: Any) -> Column:
        condition, value = self._condition, self._value
        return Column(
            f"CASE WHEN {condition.name} THEN {getattr(value, 'name', value)} "
            f"ELSE {getattr(other, 'name', other)} END",
            lambda row: (_value_of(value, row) if bool(condition._eval(row))
                         else _value_of(other, row)),
            aggregate=condition._aggregate or _is_aggregate(value) or _is_aggregate(other))


def _value_of(value: Any, row: Any) -> Any:
    return value._eval(row) if isinstance(value, Column) else value


def _quantize(value: Any, scale: int, rounding: str) -> _decimal.Decimal:
    """``value`` at ``scale``, under a context WIDE enough that quantizing never itself overflows.

    Python's default decimal context is 28 digits and raises ``InvalidOperation`` above it. Letting
    that stand would put a second, undeclared precision limit into the stand-in — and it would fire
    before the ``decimal(p,s)`` cast, so the rendered OVERFLOW_VIOLATION check would never be
    reached and the test written for it would pass for the wrong reason. Rounding decides the SCALE
    here; only the cast decides whether a value fits.
    """
    with _decimal.localcontext() as context:
        context.prec = _WIDE_PRECISION
        return _decimal.Decimal(str(value)).quantize(
            _decimal.Decimal(1).scaleb(-scale), rounding=rounding)


def _fit_decimal(value: Any, precision: int, scale: int) -> Any:
    """``value`` as ``DECIMAL(precision, scale)``, or ``None`` if it does not fit.

    NULL in, NULL out. Anything else is quantized HALF_UP to ``scale`` (Spark's cast rounds; the
    rendered code rounds EXPLICITLY first, so this quantization is exact for every value it sees),
    and a result needing more than ``precision`` digits becomes NULL rather than raising.
    """
    if value is None:
        return None
    with _decimal.localcontext() as context:
        context.prec = _WIDE_PRECISION
        quantized = _quantize(value, scale, _decimal.ROUND_HALF_UP)
        unscaled = abs(quantized).scaleb(scale).to_integral_value()
    return None if unscaled >= 10 ** precision else quantized


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

    def _buckets(self) -> dict[tuple[Any, ...], list[dict[str, Any]]]:
        buckets: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in self._rows:
            buckets.setdefault(tuple(column._eval(row) for column in self._keys), []).append(row)
        return buckets

    def count(self) -> DataFrame:
        """``groupBy(...).count()``, whose count column is named ``count`` — as in Spark."""
        produced = []
        for key, members in self._buckets().items():
            bucket = {column.name: value for column, value in zip(self._keys, key, strict=True)}
            bucket["count"] = len(members)
            produced.append(bucket)
        return DataFrame(produced, columns=[*(column.name for column in self._keys), "count"])

    def agg(self, *expressions: Column) -> DataFrame:
        """One row per group, carrying the key columns and each aggregate expression.

        Every expression must BE an aggregate. Spark refuses a bare column here too, and a stand-in
        that evaluated one against the group's row list would answer with whatever the list happened
        to do — the permissive shape that turns a grouping the renderer never wrote into a pass.
        """
        for expression in expressions:
            if not isinstance(expression, Column) or not expression._aggregate:
                raise NotImplementedError(
                    f"fake_spark refuses {getattr(expression, 'name', expression)!r} in agg(): it "
                    f"is not an aggregate expression, and evaluating one per GROUP would model a "
                    f"grouping nobody wrote")
        produced = []
        for key, members in self._buckets().items():
            bucket = {column.name: value for column, value in zip(self._keys, key, strict=True)}
            for expression in expressions:
                bucket[expression.name] = expression._eval(members)
            produced.append(bucket)
        return DataFrame(produced, columns=[*(column.name for column in self._keys),
                                            *(expression.name for expression in expressions)])


class DataFrame:
    """Rows as dicts, plus the column list they are shaped by.

    ``columns`` is carried explicitly rather than derived from the rows, because a frame's SCHEMA
    exists independently of whether it has any rows: an empty grain-level aggregate still has a
    feature column, and §8 rule 3's "entities with no source rows still present" is exactly the case
    where the right-hand side of the join is empty. A stand-in that read the columns off the rows
    would lose the column at the moment the rule under test needs it.
    """

    def __init__(self, rows: Sequence[dict[str, Any]],
                 columns: Sequence[str] | None = None) -> None:
        self._rows = [dict(row) for row in rows]
        if columns is None:
            seen: list[str] = []
            for row in self._rows:
                for name in row:
                    if name not in seen:
                        seen.append(name)
            columns = seen
        self._columns = tuple(columns)

    # ── inspection, for tests ────────────────────────────────────────────────────────────────────

    @property
    def rows(self) -> list[dict[str, Any]]:
        return [dict(row) for row in self._rows]

    @property
    def columns(self) -> list[str]:
        return list(self._columns)

    # ── the operations the rendered spine uses ───────────────────────────────────────────────────

    def where(self, condition: Column) -> DataFrame:
        return DataFrame([row for row in self._rows if bool(condition._eval(row))], self._columns)

    filter = where

    def withColumn(self, name: str, expression: Column) -> DataFrame:  # noqa: N802 — Spark's name
        columns = self._columns if name in self._columns else (*self._columns, name)
        if expression._window is not None:
            return DataFrame(self._windowed(name, expression._window), columns)
        return DataFrame([{**row, name: expression._eval(row)} for row in self._rows], columns)

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
                          for row in self._rows],
                         [column.name for column in expressions])

    def drop(self, *names: str) -> DataFrame:
        dropped = set(names)
        return DataFrame([{k: v for k, v in row.items() if k not in dropped} for row in self._rows],
                         [name for name in self._columns if name not in dropped])

    def groupBy(self, *columns: Column) -> _GroupedData:  # noqa: N802 — Spark's name
        return _GroupedData(self._rows, columns)

    def join(self, other: DataFrame, on: Sequence[str], how: str) -> DataFrame:
        """An equi-join on column NAMES, ``left`` or ``inner``, with one copy of each key column.

        ``how`` has no default. Spark's is ``inner``, and an inner join here silently drops every
        entity with no source rows — the population shrinks, the feature still looks plausible, and
        nothing reports it. A stand-in that defaulted the same way would let a renderer that omitted
        ``how`` pass, which is the mutant §8 rule 3 is written against.
        """
        if how not in {"left", "inner"}:
            raise NotImplementedError(
                f"fake_spark models `left` and `inner` joins only, got {how!r}: an outer or semi "
                f"join changes which entities survive, and guessing at one would model a population")
        if isinstance(on, str) or not isinstance(on, list | tuple) \
                or not all(isinstance(name, str) for name in on):
            raise NotImplementedError(
                f"fake_spark joins on a SEQUENCE of column names, got {on!r}: Spark keeps one copy "
                f"of each key only for that form, and the column-expression form duplicates them")
        keys = list(on)
        index: dict[tuple[Any, ...], list[dict[str, Any]]] = {}
        for row in other._rows:
            index.setdefault(tuple(row[key] for key in keys), []).append(row)
        added = [name for name in other.columns if name not in keys]
        produced: list[dict[str, Any]] = []
        for row in self._rows:
            matches = index.get(tuple(row[key] for key in keys), [])
            if matches:
                produced.extend({**row, **match} for match in matches)
            elif how == "left":
                produced.append({**row, **dict.fromkeys(added)})
        return DataFrame(produced, [*self._columns, *added])

    def limit(self, n: int) -> DataFrame:
        return DataFrame(self._rows[:n], self._columns)

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

    # ── §8 rules 3 and 4: aggregation, null handling and rounding ────────────────────────────────

    @staticmethod
    def sum(column: Column) -> Column:  # noqa: A003 — Spark's name shadows the builtin
        """Spark's SUM: nulls are SKIPPED, and a group with no non-null value sums to NULL — not 0.

        That last part is the one that matters. "Zero" is a value a formula must DECLARE
        (``EmptyWindowResult.ZERO``); a SUM that produced it on its own would answer a policy
        question nobody asked.
        """
        def aggregate(rows: Sequence[dict[str, Any]]) -> Any:
            present = [value for value in (column._eval(row) for row in rows) if value is not None]
            if not present:
                return None
            total = present[0]
            for value in present[1:]:
                total = total + value
            return total
        return Column(f"sum({column.name})", aggregate, aggregate=True)

    @staticmethod
    def count(column: Column) -> Column:
        """Spark's COUNT: the number of NON-NULL values. ``F.count(F.lit(1))`` is the row count."""
        return Column(f"count({column.name})",
                      lambda rows: len([row for row in rows if column._eval(row) is not None]),
                      aggregate=True)

    @staticmethod
    def countDistinct(column: Column) -> Column:  # noqa: N802 — Spark's name
        """Spark's COUNT(DISTINCT x): distinct NON-NULL values."""
        return Column(f"count(DISTINCT {column.name})",
                      lambda rows: len({value for value in (column._eval(row) for row in rows)
                                        if value is not None}),
                      aggregate=True)

    @staticmethod
    def coalesce(*columns: Column) -> Column:
        def first(row: Any) -> Any:
            for column in columns:
                value = column._eval(row)
                if value is not None:
                    return value
            return None
        return Column(f"coalesce({', '.join(column.name for column in columns)})", first,
                      aggregate=any(column._aggregate for column in columns))

    @staticmethod
    def when(condition: Column, value: Any) -> _When:
        return _When(condition, value)

    @staticmethod
    def round(column: Column, scale: int) -> Column:  # noqa: A003 — Spark's name
        """Spark's ``round``: HALF_UP. ``2.5`` at scale 0 is ``3``."""
        return _quantized(column, scale, _decimal.ROUND_HALF_UP, "round")

    @staticmethod
    def bround(column: Column, scale: int) -> Column:
        """Spark's ``bround``: HALF_EVEN (banker's). ``2.5`` at scale 0 is ``2``, ``3.5`` is ``4``."""
        return _quantized(column, scale, _decimal.ROUND_HALF_EVEN, "bround")

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


def _quantized(column: Column, scale: int, rounding: str, label: str) -> Column:
    """One rounding mode, applied EXPLICITLY. NULL in, NULL out."""
    if not isinstance(scale, int) or isinstance(scale, bool) or scale < 0:
        raise NotImplementedError(f"fake_spark models {label} to a whole scale >= 0, got {scale!r}")
    def apply(row: Any) -> Any:
        value = column._eval(row)
        return None if value is None else _quantize(value, scale, rounding)
    return Column(f"{label}({column.name}, {scale})", apply, aggregate=column._aggregate)


#: The decimal context this stand-in quantizes under. Python's default is 28 digits and RAISES
#: above it; a `DECIMAL(38,6)` is 38. Left at the default, the stand-in would carry a second,
#: undeclared precision limit that fired BEFORE the cast — and the rendered overflow check would
#: never be reached by the test written for it.
_WIDE_PRECISION = 80

#: Days per month, indexed 0–11. February's leap day is added by ``add_months``.
_MONTH_LENGTHS = (31, 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31)

#: ``decimal(p,s)`` as ``Column.cast`` accepts it. Spark spells more types; this stand-in models the
#: one §6 publishes and refuses the rest rather than guessing at their overflow behaviour.
_DECIMAL_TYPE = _re.compile(r"decimal\(\s*(\d+)\s*,\s*(\d+)\s*\)")

#: The ``trunc`` formats this stand-in models — Spark accepts more spellings of each.
_TRUNC_UNITS = frozenset({"week", "month", "quarter", "year"})

_INTERVAL = _re.compile(r"INTERVAL\s+(\d+)\s+(HOURS?|MINUTES?|SECONDS?)", _re.IGNORECASE)

functions = _Functions()


def run_rendered(source: str, func_name: str, *, module_file: str | None = None) -> Callable[..., Any]:
    """``exec`` a rendered node's source against this stand-in and return its function.

    The node's imports are supplied here rather than executed from the source, because a
    :class:`~featuregen.materialize.render.project.RenderedNode` forbids imports inside ``source``
    (they are declared on the node so the rendered module can de-duplicate them).

    ``module_file`` is the path the rendered ``nodes.py`` would have. A node that reads
    ``GENERATED.lock`` locates it relative to ``__file__``, and an ``exec`` namespace has no
    ``__file__`` at all — so it is supplied here rather than defaulted to something plausible, which
    would let a wrong depth resolve to a file that happened to exist.
    """
    namespace: dict[str, Any] = {
        "DataFrame": DataFrame, "F": functions, "Window": Window,
        "hashlib": _hashlib, "json": _json, "pathlib": _pathlib, "Decimal": _decimal.Decimal}
    if module_file is not None:
        namespace["__file__"] = module_file
    exec(compile(source, f"<rendered {func_name}>", "exec"), namespace)  # noqa: S102 — the point
    return namespace[func_name]  # type: ignore[no-any-return]
