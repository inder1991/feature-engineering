"""The rule as runnable SQL — the derivation logic the registry exists to produce.

Option B is unchanged: the platform SPECIFIES, it does not EXECUTE. This module emits text and
touches neither a database nor a model, exactly as `describe_target` does. What it buys is the
thing a registry of specifications otherwise withholds — the person who registered a label can
read the logic that will build their training data, and hand it to whatever runs it.

Deterministic and model-free by construction, so the SQL can never drift from the rule it renders.

WHAT THIS DELIBERATELY DOES NOT KNOW. The registry records a CATALOG (`cib`, `ftr`), never a
physical database, and `object_ref` is only `public.{table}.{column}` (M3). Emitting a confident
three-part name would invent a location and could silently read the wrong table, so the catalogs
are named in a binding header for the consumer to resolve — the one thing only they know.
"""
from __future__ import annotations

import re

from featuregen.overlay.upload.target_contract import (
    EventWindowRuleV1,
    StateChangeRuleV1,
    TargetHeaderV1,
    TargetRuleV1,
    describe_target,
)

#: A plain SQL name. Refs come from the catalog and are checked against it before registration, so
#: a ref reaching here that is not one of these should be unreachable — which is exactly why it
#: raises rather than being quoted around. Quoting a hostile identifier produces something that
#: PARSES, and the failure would then be silent.
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

#: Contract op → SQL op. The contract's set is closed (`FILTER_OPS`), so this mapping is total and
#: a missing key is a programming error rather than user input.
_SQL_OPS = {"==": "=", "!=": "<>", ">": ">", ">=": ">=", "<": "<", "<=": "<=",
            "in": "IN", "not_in": "NOT IN"}

#: `single` is handled separately — it is not a period to bucket by.
_TRUNC = {"weekly": "week", "monthly": "month", "quarterly": "quarter"}


class SqlRenderError(ValueError):
    """A rule that cannot be rendered safely — refused, never rendered approximately."""


def _fail(message: str) -> None:
    raise SqlRenderError(message)


def _ident(name: str) -> str:
    if not _IDENT_RE.match(name):
        _fail(f"{name!r} is not a plain SQL identifier — refusing to render it")
    return f'"{name}"'


def _split(ref: str) -> tuple[str, str, str]:
    """`public.{table}.{column}` → its three validated parts."""
    parts = ref.split(".")
    if len(parts) != 3:
        _fail(f"ref {ref!r} is not schema.table.column — refusing to render it")
    for part in parts:
        _ident(part)
    return parts[0], parts[1], parts[2]


def _table_of(ref: str) -> str:
    schema, table, _ = _split(ref)
    return f"{_ident(schema)}.{_ident(table)}"


def _col(ref: str) -> str:
    return _ident(_split(ref)[2])


def _literal(value: str) -> str:
    """A string literal. Doubling the quote IS the defence, so it is tested rather than assumed.

    A NUL byte cannot appear in a Postgres text literal at all — it would be truncated or rejected
    downstream, so it is refused here where the reason is still legible.
    """
    text = str(value)
    if "\x00" in text:
        _fail("a NUL byte cannot be rendered as a SQL literal")
    return "'" + text.replace("'", "''") + "'"


def _values(values) -> str:
    return ", ".join(_literal(v) for v in values)


def _banner(rule: TargetRuleV1) -> list[str]:
    """The sentence and the catalog bindings, as comments.

    The sentence travels WITH the SQL because whoever runs it was not necessarily in the room when
    it was approved — a bare query is checked for syntax, a query with its meaning attached is
    checked for meaning.
    """
    h = rule.header
    lines = [f"-- {h.name} — generated from the registered target definition.",
             "-- Deterministic render of the rule; it does not depend on a model.",
             "--"]
    lines += [f"-- {chunk}" for chunk in _wrap(describe_target(rule))]
    lines += ["--",
              "-- Source bindings — the registry records the CATALOG, not the physical database.",
              f"--   {h.anchor_catalog}  ->  {_table_of(h.grain_ref)}   (anchor)"]
    if isinstance(rule, EventWindowRuleV1):
        lines.append(f"--   {rule.event_catalog}  ->  {_table_of(rule.event_date_ref)}   (events)")
    lines += ["-- Point each at the right connection before running.", ""]
    return lines


def _wrap(text: str, width: int = 96) -> list[str]:
    out, line = [], ""
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            out.append(line)
            line = word
        else:
            line = f"{line} {word}".strip()
    if line:
        out.append(line)
    return out


def _as_of_dates(h: TargetHeaderV1, anchor: str, as_of: str) -> str:
    """WHICH as-of dates the label is evaluated on. A different frequency is a different dataset,
    so this is in the rule and must therefore be in the SQL."""
    if h.as_of_frequency == "single":
        if h.require_full_window:
            # The LATEST date whose window is fully observable. Taking the plain maximum would
            # render a query that is correct and always empty, because the censoring bound below
            # would then remove the only row.
            return (f"    -- sampled once: the latest as-of date whose full "
                    f"{h.window_days}-day window can be observed\n"
                    f"    SELECT MAX({as_of}) AS as_of_date\n"
                    f"    FROM {anchor}\n"
                    f"    WHERE {as_of} + INTERVAL '{h.window_days} days' <= "
                    f"(SELECT MAX({as_of}) FROM {anchor})")
        return (f"    -- sampled once: the latest as-of date\n"
                f"    SELECT MAX({as_of}) AS as_of_date FROM {anchor}")
    if h.as_of_frequency == "daily":
        return (f"    -- sampled daily: every as-of date present in the source\n"
                f"    SELECT DISTINCT {as_of} AS as_of_date FROM {anchor}")
    period = _TRUNC[h.as_of_frequency]
    return (f"    -- sampled {h.as_of_frequency}: the LAST as-of date available in each {period}\n"
            f"    SELECT as_of_date FROM (\n"
            f"        SELECT DISTINCT {as_of} AS as_of_date,\n"
            f"               ROW_NUMBER() OVER (PARTITION BY date_trunc('{period}', {as_of})\n"
            f"                                  ORDER BY {as_of} DESC) AS rn\n"
            f"        FROM {anchor}\n"
            f"    ) periods WHERE rn = 1")


def _filter_sql(condition, alias: str) -> str:
    op = _SQL_OPS[condition.op]
    left = f"{alias}.{_col(condition.column_ref)}"
    if condition.values:
        return f"{left} {op} ({_values(condition.values)})"
    if condition.value_ref is not None:
        # ANOTHER COLUMN — rendered as an identifier, never as a literal. Quoting it as a string
        # would compare the column against its own NAME and match nothing, silently.
        return f"{left} {op} {alias}.{_col(condition.value_ref)}"
    return f"{left} {op} {_literal(condition.value)}"


def _label_column(h: TargetHeaderV1, measured: str) -> str:
    """The label itself. A binary label thresholds; a count or amount reports what it measured —
    thresholding one would silently turn a magnitude into a flag."""
    name = _ident(h.name)
    if h.label_type == "binary":
        return (f"CASE WHEN COALESCE({measured}, 0) {_SQL_OPS[h.operator]} "
                f"{_number(h.threshold)} THEN 1 ELSE 0 END AS {name}")
    return f"COALESCE({measured}, 0) AS {name}"


def _number(value) -> str:
    number = float(value)
    return str(int(number)) if number.is_integer() else repr(number)


def compile_target_sql(rule: TargetRuleV1) -> str:
    """The rule as one SELECT statement. Pure: no database, no model, no I/O."""
    h = rule.header
    anchor = _table_of(h.grain_ref)
    if _table_of(h.as_of_ref) != anchor:
        _fail("grain_ref and as_of_ref are in different tables — the anchor must be one table")
    grain, as_of = _col(h.grain_ref), _col(h.as_of_ref)

    ctes: list[str] = [f"as_of_dates AS (\n{_as_of_dates(h, anchor, as_of)}\n)"]
    where: list[str] = []
    joins: list[str] = []
    if h.require_full_window:
        ctes.append(f"observable AS (\n    -- censoring: the whole window must be visible\n"
                    f"    SELECT MAX({as_of}) AS max_as_of FROM {anchor}\n)")
        joins.append("    CROSS JOIN observable o")
        where.append(f"a.{as_of} + INTERVAL '{h.window_days} days' <= o.max_as_of")

    if isinstance(rule, StateChangeRuleV1):
        body = _state_change(rule, anchor, grain, as_of, ctes, joins, where)
    else:
        body = _event_window(rule, anchor, grain, as_of, ctes, joins, where)

    head = _banner(rule)
    if not h.require_full_window:
        head.insert(3, "-- CENSORING OFF: rows whose window runs past the end of history are "
                       "INCLUDED.")
    return "\n".join(head) + "WITH " + ",\n".join(ctes) + "\n" + body + "\n"


def _population(anchor: str, grain: str, as_of: str, extra_select: list[str],
                joins: list[str], where: list[str]) -> str:
    selected = [f"    SELECT a.{grain} AS entity_id", *extra_select,
                f"           a.{as_of} AS as_of_date"]
    lines = [",\n".join(selected), f"    FROM {anchor} a",
             "    JOIN as_of_dates s ON a." + as_of + " = s.as_of_date", *joins]
    if where:
        lines.append("    WHERE " + "\n      AND ".join(where))
    return "population AS (\n" + "\n".join(lines) + "\n)"


def _state_change(rule: StateChangeRuleV1, anchor: str, grain: str, as_of: str,
                  ctes: list[str], joins: list[str], where: list[str]) -> str:
    h = rule.header
    flag = _col(rule.column_ref)
    if rule.exclude_null_at_as_of:
        # A NULL means eligibility cannot be READ, not that the row is ineligible. Including it
        # invents an answer the source does not carry.
        where.append(f"a.{flag} IS NOT NULL")
    if rule.population_filter == "from_values":
        # Rows that ALREADY have the outcome are not candidates for acquiring it. Omitting this is
        # the most common way to build a silently broken label.
        where.append(f"a.{flag} IN ({_values(rule.from_values)})")
    ctes.append(_population(anchor, grain, as_of, [], joins, where))

    window = (f"      ON f.{grain} = p.entity_id\n"
              f"     AND f.{as_of} >  p.as_of_date\n"
              f"     AND f.{as_of} <= p.as_of_date + INTERVAL '{h.window_days} days'")
    if rule.at_least_once:
        ctes.append(
            "outcome AS (\n"
            "    -- 'at any point in the window': the states are counted, not just the last one\n"
            "    SELECT p.entity_id, p.as_of_date,\n"
            f"           SUM(CASE WHEN f.{flag} IN ({_values(rule.to_values)})\n"
            "                    THEN 1 ELSE 0 END) AS matches\n"
            "    FROM population p\n"
            f"    LEFT JOIN {anchor} f\n{window}\n"
            "    GROUP BY p.entity_id, p.as_of_date\n)")
    else:
        # "ended non-performing" is a different label from "was ever non-performing"; rendering
        # both the same way would make the flag decorative.
        #
        # A PLAIN JOIN, deliberately — and this is where the two modes diverge for a row with no
        # observations inside its window (an account that closed). "Was it ever X" counts observed
        # transitions and honestly reports 0; "what was it at the END" has no end state to read, so
        # the row is dropped rather than labelled 0, which would assert a state nobody recorded.
        # A test executes both and pins the difference.
        ctes.append(
            "last_in_window AS (\n"
            "    -- 'the state at the END of the window'\n"
            f"    SELECT p.entity_id, p.as_of_date, f.{flag} AS final_value,\n"
            "           ROW_NUMBER() OVER (PARTITION BY p.entity_id, p.as_of_date\n"
            f"                              ORDER BY f.{as_of} DESC) AS rn\n"
            "    FROM population p\n"
            f"    JOIN {anchor} f\n{window}\n)")
        ctes.append(
            "outcome AS (\n"
            "    SELECT entity_id, as_of_date,\n"
            f"           CASE WHEN final_value IN ({_values(rule.to_values)})\n"
            "                THEN 1 ELSE 0 END AS matches\n"
            "    FROM last_in_window WHERE rn = 1\n)")
    return ("SELECT entity_id AS " + _ident(h.entity) + ", as_of_date,\n"
            "       " + _label_column(h, "matches") + "\nFROM outcome;")


def _event_window(rule: EventWindowRuleV1, anchor: str, grain: str, as_of: str,
                  ctes: list[str], joins: list[str], where: list[str]) -> str:
    h = rule.header
    events = _table_of(rule.event_date_ref)
    if _split(rule.event_date_ref)[1] != rule.event_table:
        _fail(f"event_table {rule.event_table!r} disagrees with the table in event_date_ref")
    left, right = _col(rule.join_left), _col(rule.join_right)
    date = _col(rule.event_date_ref)
    conditions = [_filter_sql(f, "e") for f in rule.event_filters]

    ctes.append(_population(anchor, grain, as_of, [f"           a.{left} AS join_key"],
                            joins, where))
    source = "population"
    if rule.population_having == "none":
        # The lookback must carry the SAME filters as the outcome. One that ignores them excludes
        # people for activity the label never counts — "has not traded FX" would then drop anyone
        # who had made any payment at all.
        lookback = "\n".join(
            [f"     AND e.{date} >  p.as_of_date - "
             f"INTERVAL '{rule.population_lookback_days} days'",
             "     AND e." + date + " <= p.as_of_date"]
            + [f"     AND {c}" for c in conditions])
        ctes.append(
            "prior_activity AS (\n"
            f"    -- 'who will START': {h.entity}s already doing this in the prior "
            f"{rule.population_lookback_days} days are not candidates\n"
            "    SELECT DISTINCT p.entity_id, p.as_of_date\n"
            "    FROM population p\n"
            f"    JOIN {events} e\n"
            f"      ON e.{right} = p.join_key\n{lookback}\n)")
        ctes.append(
            "eligible AS (\n"
            "    SELECT p.* FROM population p\n"
            "    LEFT JOIN prior_activity x\n"
            "      ON x.entity_id = p.entity_id AND x.as_of_date = p.as_of_date\n"
            "    WHERE x.entity_id IS NULL\n)")
        source = "eligible"

    measured = (f"SUM(ev.{_col(rule.measure_ref)})" if rule.aggregate == "sum"
                else f"COUNT(ev.{right})")
    outcome_filters = "".join(f"\n     AND {c.replace('e.', 'ev.')}" for c in conditions)
    ctes.append(
        "outcome AS (\n"
        f"    SELECT p.entity_id, p.as_of_date, {measured} AS measured\n"
        f"    FROM {source} p\n"
        f"    LEFT JOIN {events} ev\n"
        f"      ON ev.{right} = p.join_key\n"
        f"     AND ev.{date} >  p.as_of_date\n"
        f"     AND ev.{date} <= p.as_of_date + INTERVAL '{h.window_days} days'"
        f"{outcome_filters}\n"
        "    GROUP BY p.entity_id, p.as_of_date\n)")
    return ("SELECT entity_id AS " + _ident(h.entity) + ", as_of_date,\n"
            "       " + _label_column(h, "measured") + "\nFROM outcome;")
