"""Ground an :class:`AnalysisPlanV1` against the catalog.

This is the half of the data agent that needs no data. Given a plan and the catalog's own metadata,
it decides whether the question is answerable and what the answer would rest on. Every check here
corresponds to a specific way a plausible-looking analysis is wrong:

* a "last month" window measured on an event timestamp whose data does not land for two days
  includes rows nobody could have known about — the classic look-ahead leak;
* a sum over a monetary column whose currency varies adds dirhams to dollars;
* a per-customer count on a table whose grain is not established counts rows, not customers;
* a cross-catalog hop on an unconfirmed identifier link merges two different people;
* a group-by with a cell of three names those three people.

None of these is visible in the output. All of them are answerable from metadata the catalog already
holds, which is why this half is worth building before there is anything to execute against.

**Read scope first, and as absence.** A column the caller may not see is reported as
``COLUMN_ABSENT``, never as "hidden" — otherwise the refusal itself becomes an existence oracle for
sensitive columns, the same rule ``formula/tools`` and ``asset_detail`` already follow.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence

from featuregen.analysis.plan import (
    AnalysisPlanV1,
    Finding,
    GroundedPlan,
    Window,
)
from featuregen.overlay.upload.bridge_assessment import (
    LinkAvailability,
    LinkReviewStatus,
    LinkUnavailableReason,
    read_overlay_identifier_link_state,
)
from featuregen.overlay.upload.read_scope import allowed_sensitivities

#: Below this many rows in a group, a result can isolate individuals. A default, not a policy: a
#: deployment sets its own, and the finding says which value was applied.
DEFAULT_MIN_CELL_SIZE = 5

_NUMERIC_TYPES = frozenset({
    "numeric", "decimal", "double", "double precision", "float", "float4", "float8", "real",
    "int", "int2", "int4", "int8", "integer", "bigint", "smallint", "money",
})

_COUNTING_OPS = frozenset({"count", "count_distinct"})


def _parse(logical_ref: str) -> tuple[str, str, str]:
    """``source::schema.table.column`` (or ``source::table.column``) -> (source, table, column)."""
    source, _, rest = logical_ref.partition("::")
    parts = [p for p in rest.split(".") if p]
    if len(parts) >= 3:
        return source.strip().lower(), parts[-2], parts[-1]
    if len(parts) == 2:
        return source.strip().lower(), parts[0], parts[1]
    return source.strip().lower(), rest, ""


class _Catalog:
    """One read-scoped pass over every column the plan mentions.

    Loaded once rather than per-check: a plan touches a handful of columns and each check wants a
    different field of the same row, so re-querying per check would multiply reads for no gain —
    the same lesson the grounding engine's ``_load_columns`` hoist recorded.
    """

    def __init__(self, conn, refs: Iterable[str], roles: Sequence[str]) -> None:
        self.rows: dict[str, dict] = {}
        wanted = {r for r in refs if r}
        by_source: dict[str, set[str]] = {}
        for ref in wanted:
            source, table, column = _parse(ref)
            if column:
                by_source.setdefault(source, set()).add(f"public.{table}.{column}".lower())
        for source, object_refs in by_source.items():
            for object_ref, table_name, column_name, data_type, concept, currency, unit, is_grain, \
                    is_as_of, availability_fact_event_id, grain_fact_event_id in conn.execute(
                    "SELECT lower(object_ref), table_name, column_name, data_type, concept, "
                    "       currency, unit, is_grain, is_as_of, availability_fact_event_id, "
                    "       grain_fact_event_id "
                    "FROM graph_node "
                    "WHERE catalog_source = %s AND kind = 'column' AND lower(object_ref) = ANY(%s) "
                    "  AND visible_requires <@ %s",
                    (source, sorted(object_refs), allowed_sensitivities(roles))).fetchall():
                self.rows[f"{source}|{object_ref}"] = {
                    "table": table_name, "column": column_name, "data_type": (data_type or "").lower(),
                    "concept": concept, "currency": currency, "unit": unit,
                    "is_grain": bool(is_grain), "is_as_of": bool(is_as_of),
                    "availability_governed": availability_fact_event_id is not None,
                    "grain_governed": grain_fact_event_id is not None,
                }

    def get(self, logical_ref: str) -> dict | None:
        source, table, column = _parse(logical_ref)
        return self.rows.get(f"{source}|public.{table}.{column}".lower())


def _currency_varies(conn, plan: AnalysisPlanV1, roles: Sequence[str]) -> bool:
    """True when the base table's monetary columns do not all agree on one currency.

    Checked at the TABLE, not the measure column: a transaction table with a `tran_crncy` dimension
    is telling you the amount's currency varies per row, which no property of the amount column
    itself reveals.
    """
    source, table, _ = _parse(plan.base_table_ref + ".x")
    distinct = conn.execute(
        "SELECT count(DISTINCT coalesce(currency, '')) FROM graph_node "
        "WHERE catalog_source = %s AND kind = 'column' AND table_name = %s "
        "  AND currency IS NOT NULL AND visible_requires <@ %s",
        (source, table, allowed_sensitivities(roles))).fetchone()[0]
    return distinct > 1


def _window_findings(window: Window, anchor: dict | None) -> list[Finding]:
    """The leakage checks. This is the single highest-value thing this module does."""
    out: list[Finding] = []
    if anchor is None:
        return out
    if not anchor["availability_governed"]:
        out.append(Finding(
            code="AVAILABILITY_BASIS_UNKNOWN", subject=window.anchor_ref,
            detail=("no governed availability fact: a window on this column may include rows that "
                    "had not landed when the window closed"),
            clears_when="a human confirms the table's availability basis"))
    elif window.availability_basis == "event_time_plus_lag" and not window.availability_lag_hours:
        out.append(Finding(
            code="AVAILABILITY_LAG_UNAPPLIED", subject=window.anchor_ref,
            detail="basis is event_time_plus_lag but the plan applies no lag to the cutoff"))
    if not anchor["is_as_of"]:
        out.append(Finding(
            code="TIME_ANCHOR_UNGOVERNED", subject=window.anchor_ref,
            detail="the window anchor is not a declared as-of column for its table"))
    return out


def ground_analysis_plan(conn, plan: AnalysisPlanV1, *, roles: Sequence[str] = (),
                         min_cell_size: int = DEFAULT_MIN_CELL_SIZE) -> GroundedPlan:
    """Check one plan against the catalog. Read-only; no data is touched.

    Refusals mean the plan cannot be expressed. Findings mean it can be answered but the answer
    rests on something unconfirmed — they travel with the result rather than stopping it, which is
    the same contract the feature gauntlet uses.
    """
    refs = [plan.entity_ref, plan.measure.logical_ref,
            *(w.anchor_ref for w in plan.windows),
            *(d.logical_ref for d in plan.dimensions)]
    cat = _Catalog(conn, refs, roles)
    findings: list[Finding] = []
    refusals: list[tuple[str, str]] = []

    def require(ref: str) -> dict | None:
        if not ref:
            return None
        row = cat.get(ref)
        if row is None:
            # Read-scope-hidden and genuinely absent are reported identically, on purpose.
            refusals.append(("COLUMN_ABSENT", ref))
        return row

    entity = require(plan.entity_ref)
    measure_col = require(plan.measure.logical_ref) if plan.measure.logical_ref else None
    for w in plan.windows:
        findings.extend(_window_findings(w, require(w.anchor_ref)))

    # ── the measure ───────────────────────────────────────────────────────────────────────────────
    if measure_col is not None and plan.measure.op not in _COUNTING_OPS:
        if measure_col["data_type"] not in _NUMERIC_TYPES:
            findings.append(Finding(
                code="MEASURE_NOT_NUMERIC", subject=plan.measure.logical_ref,
                detail=f"{plan.measure.op} over a {measure_col['data_type'] or 'unknown'} column"))
        if measure_col["concept"] in {"monetary_amount", "balance", "transaction_amount"} or \
                measure_col["currency"]:
            if _currency_varies(conn, plan, roles):
                findings.append(Finding(
                    code="CURRENCY_MIXED", subject=plan.measure.logical_ref,
                    detail="the table's monetary columns do not agree on one currency",
                    clears_when="a human confirms the currency binding for this column"))

    # ── the entity grain ──────────────────────────────────────────────────────────────────────────
    # A per-entity count is only a count OF ENTITIES when the base table's grain is established.
    # Otherwise it counts rows and calls them customers.
    if entity is not None and not entity["grain_governed"] and not entity["is_grain"]:
        findings.append(Finding(
            code="GRAIN_NOT_ESTABLISHED", subject=plan.base_table_ref,
            detail=("no governed grain on the base table: a per-entity count may count rows rather "
                    "than entities"),
            clears_when="a human confirms the table's grain"))

    # ── dimensions ────────────────────────────────────────────────────────────────────────────────
    for dim in plan.dimensions:
        row = require(dim.logical_ref)
        if row is None:
            continue
        if not row["concept"]:
            findings.append(Finding(
                code="DIMENSION_UNGOVERNED", subject=dim.logical_ref,
                detail="grouped by a column that carries no business concept"))
        if dim.slice_values:
            findings.append(Finding(
                code="CODE_SET_INCOMPLETE", subject=dim.logical_ref,
                detail=(f"{len(dim.slice_values)} value(s) came from observed samples, not a known "
                        "domain: values never sampled are absent from the result, not zero"),
                clears_when="profiling the column establishes its full value domain"))
    if plan.dimensions:
        findings.append(Finding(
            code="SMALL_CELL_RISK", subject=plan.base_table_ref,
            detail=(f"grouped output can isolate individuals; groups below {min_cell_size} rows must "
                    "be suppressed before the result is shown")))

    # ── cross-catalog hops ────────────────────────────────────────────────────────────────────────
    # Read the link's GOVERNED LIFECYCLE, not `entity_bridge_edge`. That table is the VERIFIED-only,
    # lagging projection of HUMAN REVIEW, and it was being consulted for two things it cannot answer:
    #
    #   * as availability authority — absence from it meant "unconfirmed", which flattened "nobody
    #     has looked at this yet" together with "a human REJECTED it" and "the confirmation EXPIRED".
    #     Only the first is something an approver can act on; the other two send the reader to find
    #     an approver for a link no approval can bring back;
    #   * as current review state — a bridge a human had just confirmed kept carrying "no human has
    #     confirmed this" until a projector drained.
    #
    # Both findings are NON-BLOCKING and stay that way: grounding DISCLOSES what an answer rests on.
    # Availability is enforced where links are actually traversed (`cross_catalog_links`), so this
    # surface can report an unavailable link without becoming a second, divergent gate on it.
    for fact_key in plan.join_refs:
        lifecycle = read_overlay_identifier_link_state(conn, fact_key)
        if (
            lifecycle.availability is LinkAvailability.AVAILABLE
            and lifecycle.review_status is LinkReviewStatus.HUMAN_VERIFIED
        ):
            continue
        if lifecycle.availability is LinkAvailability.AVAILABLE:
            findings.append(Finding(
                code="JOIN_IDENTITY_UNCONFIRMED", subject=fact_key,
                detail=("this answer joins two catalogs on an identifier link no human has "
                        "confirmed"),
                clears_when="a human confirms the identifier link"))
        else:
            state = (
                lifecycle.folded_status.value.lower()
                if lifecycle.folded_status is not None
                else LinkUnavailableReason.UNREADABLE.value
            )
            findings.append(Finding(
                code="JOIN_IDENTITY_UNAVAILABLE", subject=fact_key,
                detail=("this answer joins two catalogs on an identifier link the platform will not "
                        f"consider (governance state: {state})"),
                clears_when="the identifier link is re-derived and re-established"))

    # ── period-over-period coherence ──────────────────────────────────────────────────────────────
    # Two windows must share an anchor and a basis, or "fewer than last month" compares two
    # differently-shaped months and the trend is an artefact of the metadata.
    if plan.comparison and len({w.anchor_ref for w in plan.windows}) > 1:
        refusals.append(("NO_PATH_TO_DIMENSION", plan.base_table_ref))

    return GroundedPlan(plan=plan, answerable=not refusals,
                        findings=tuple(findings), refusals=tuple(refusals))
