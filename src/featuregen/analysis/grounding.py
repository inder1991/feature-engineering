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

**Release-B (Task 8): grounding also resolves WHICH COPY and WHICH ROWS.** The review recorded that
population enforcement was spread across intent/assembly/execution/clarify and that this module had
zero ``population`` hits at all. It now resolves the plan's dataset NEEDS through the source
selector and the temporal resolver — behind ``FEATUREGEN_SOURCE_TEMPORAL_SELECTION``, so with the
flag off every ``GroundedPlan`` is byte-identical to before.

Those decisions ride the plan as a :class:`SelectionPreviewV1` and their refusals do NOT flip
``answerable``. That is deliberate and it is this module's own doctrine: a refusal here means the
plan could not be EXPRESSED against the catalog, and "nobody has declared which copy serves this"
is a question for a person, not an inexpressible plan. The HARD gate is the execution bridge —
which is exactly where the review found the hole, and where Task 8 closes it.

**ONE WRITE, and it is deliberate.** "No data is touched" still holds — this module reads no
warehouse row. But a SELECTION persists the physical binding it pinned
(``binding_store.select_table_binding``), because selecting a source is the moment a derived
binding becomes a thing decisions, observations and snapshots reference, and a decision naming a
revision that was never recorded is unreplayable. It is idempotent, it happens only for the
SELECTED dataset, and only with the flag on — a flag-off ground writes nothing at all, pinned by
test.

That write rides the CALLER'S TRANSACTION, and this module neither commits nor rolls back: a route
whose request later fails takes the pin with it. That is the honest trade rather than a wart to fix
with a second connection — the id is content-addressed, so the next call re-derives the identical
revision and the only cost of a rollback is doing the work twice. It is worth saying out loud
because the neighbouring learning write has the OPPOSITE property: `routes/analysis.py` returns its
refusal rather than raising it precisely so that write commits.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import datetime

from featuregen.analysis.plan import (
    AnalysisPlanV1,
    Finding,
    GroundedPlan,
    SelectionPreviewV1,
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

#: The DEFAULT execution tier for a selection, and it is PRODUCTION.
#:
#: It was ``"sandbox"``, and nothing passed a tier, so every real selection ran under the tier that
#: is allowed to rank on a merely PROPOSED (LLM) authority value — the governance default moved
#: production -> sandbox on the production path, silently, for every caller. The tier decides
#: whether an unconfirmed classification may choose which copy of a dataset answers a question, so
#: the safe value has to be the one you get by saying nothing. Sandbox is now reachable only by
#: asking for it, which is what "sandbox is opt-in" means.
PRODUCTION_TIER = "production"

#: The parameter name a plan's runtime report cutoff binds to. A NAME, never a date: the decision
#: carries the ref and the engines take the value, so two replays of one governed decision hash
#: identically however many days apart they run (`temporal_policy._reject_literal_cutoff`).
REPORT_CUTOFF_VALUE_REF = "report_cutoff"

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
                         min_cell_size: int = DEFAULT_MIN_CELL_SIZE,
                         execution_tier: str = PRODUCTION_TIER,
                         cutoff_value_ref: str | None = None,
                         candidate_dataset_refs: Iterable[str] = (),
                         recorded_by: str | None = None) -> GroundedPlan:
    """Check one plan against the catalog. Read-only apart from the binding a SELECTION pins.

    Refusals mean the plan cannot be expressed. Findings mean it can be answered but the answer
    rests on something unconfirmed — they travel with the result rather than stopping it, which is
    the same contract the feature gauntlet uses.

    The four Release-B keyword arguments are inert while the flag is off; every existing caller
    keeps its exact behaviour without passing any of them. ``execution_tier`` defaults to
    :data:`PRODUCTION_TIER` — the tier you get by saying nothing is the one that may NOT rank on an
    unconfirmed authority value. ``cutoff_value_ref`` is the caller's: the route derives it from
    the plan's own time context with :func:`plan_cutoff_value_ref`, and a plan that needs one and
    has none gets a typed refusal on the preview rather than an unnoticed absence.
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

    return GroundedPlan(
        plan=plan, answerable=not refusals,
        findings=tuple(findings), refusals=tuple(refusals),
        selections=resolve_plan_selections(
            conn, plan, roles=roles, execution_tier=execution_tier,
            cutoff_value_ref=cutoff_value_ref, candidate_dataset_refs=candidate_dataset_refs,
            recorded_by=recorded_by))


# ── Release-B: WHICH COPY, and WHICH ROWS (Task 8) ───────────────────────────────────────────────


def _table_ref_of(logical_ref: str) -> str:
    """The TABLE a column ref belongs to, by string. Deriving is safe; inferring is the thing the
    population doctrine forbids, and this derives."""
    source, _, rest = logical_ref.partition("::")
    parts = [p for p in rest.split(".") if p]
    return f"{source}::{'.'.join(parts[:-1])}" if len(parts) >= 2 else logical_ref


def plan_cutoff_value_ref(plan: AnalysisPlanV1) -> str | None:
    """The REF this plan's runtime report cutoff binds to, from the plan's OWN time context.

    A plan expresses time through its ``windows``: each is measured back from the instant the
    question is asked as of, and that instant IS the report cutoff — the same one the dimension row
    rule reads. So a plan with a window has a cutoff to name, and a plan with none has no time
    context at all: there is no instant to be "as of", and a row rule that needs one must refuse
    rather than quietly read whatever is there now.

    A NAME is returned, never a date. The value is a runtime parameter (§6.5) and a literal here
    would re-key the same governed decision on every day it was replayed.
    """
    return REPORT_CUTOFF_VALUE_REF if plan.windows else None


def resolve_plan_selections(
    conn, plan: AnalysisPlanV1, *, roles: Sequence[str] = (),
    execution_tier: str = PRODUCTION_TIER,
    cutoff_value_ref: str | None = None, candidate_dataset_refs: Iterable[str] = (),
    recorded_by: str | None = None,
) -> SelectionPreviewV1 | None:
    """Resolve this plan's dataset NEEDS, or ``None`` while the flag is off.

    THREE need roles, and no more. The POPULATION (who is in the answer), the EVENT SOURCE (what is
    counted) and one DIMENSION SOURCE per distinct group-by table.

    **Only the POPULATION is an explicit declaration.** ``population_table_ref`` is answered by a
    PERSON through the population clarification — ``spine.py``'s doctrine is that the declaration
    chooses the source and governed facts may only validate it. ``base_table_ref`` and the group-by
    tables are NOT declarations: they are what the model picked out of a bounded retrieval set, and
    treating a model's pick as an explicit request would hand it the source decision that §5.3
    reserves for a serving policy or a load-bearing authority value. So they enter as CANDIDATES,
    and the policy/authority precedence decides among them. A single candidate still resolves — as
    ``single_eligible_candidate``, which is what it honestly is.

    **Only the dimension sources get a ROW decision**, deliberately. "Which row classifies this
    customer" is the question the renderer used to answer by itself, and it is the one §6.5 exists
    for. The population's and the event table's row rules are the window/eligibility machinery that
    already exists and is already declared; demanding a temporal policy for them would refuse every
    plan that works today in order to re-declare something that is not in doubt.
    """
    from featuregen.overlay.upload.source_selection import (
        SELECTION_BINDING_MISSING,
        DatasetNeedRole,
        DatasetNeedV1,
        SelectionError,
        ServingPurpose,
        normalize_dataset_ref,
        source_temporal_selection_enabled,
    )
    from featuregen.overlay.upload.source_selector import SelectionRefusalV1, select_dataset_source
    from featuregen.overlay.upload.temporal_resolver import (
        RequestTemporality,
        resolve_row_selection,
    )

    if not source_temporal_selection_enabled():
        return None

    def _unaddressable(refs: Iterable[str]) -> tuple[str, ...]:
        """The refs that cannot name a DATASET at all — a ref with no schema, or a column ref.

        A ref the selection contracts cannot address is not a caller bug and not an outage: it is
        a catalog that has not supplied the schema half of a physical address, which is what
        `SELECTION_BINDING_MISSING` already names. It is reported as a REFUSAL rather than skipped,
        because the alternative is the defect this whole review item is about — a need with no
        decision and nothing saying so."""
        bad = []
        for ref in refs:
            try:
                normalize_dataset_ref(ref)
            except SelectionError:
                bad.append(ref)
        return tuple(bad)

    selections, rows, refusals, warnings = [], [], [], []
    entity = plan.entity or "entity"
    dimension_tables = tuple(dict.fromkeys(
        _table_ref_of(dim.logical_ref) for dim in plan.dimensions))

    offered = tuple(ref for ref in candidate_dataset_refs if ref)
    # (need role, the one EXPLICIT declaration if there is one, the candidates it may choose from)
    wanted: list[tuple[DatasetNeedRole, str | None, tuple[str, ...]]] = [
        (DatasetNeedRole.POPULATION, plan.population_table_ref or None, ()),
        (DatasetNeedRole.EVENT_SOURCE, None,
         tuple(dict.fromkeys(([plan.base_table_ref] if plan.base_table_ref else []) + list(offered)))),
    ]
    wanted += [(DatasetNeedRole.DIMENSION_SOURCE, None, (table,)) for table in dimension_tables]

    # NO BLANKET `except SelectionError`. It used to wrap both calls below, and it erased the ONE
    # refusal the resolver raised — a historical plan with no cutoff came back with no row rule, no
    # refusal, and `resolved` True. A genuine bug must crash and a refusal must ride the payload;
    # the only thing that legitimately raised here was an unaddressable REF, which is checked up
    # front and reported as the typed refusal it is.
    row_decisions_expected = 0
    for need_role, explicit, candidates in wanted:
        bad_refs = _unaddressable(([explicit] if explicit else []) + list(candidates))
        if bad_refs:
            refusals.append(SelectionRefusalV1(
                code=SELECTION_BINDING_MISSING, subject_refs=bad_refs,
                detail=(f"{', '.join(repr(r) for r in bad_refs)} cannot address a dataset: a "
                        "source decision needs source, schema and table, and the catalog has not "
                        "supplied all three for this table. Bind the table, or ask about one that "
                        "is addressable")))
            continue
        need = DatasetNeedV1(
            entity_id=entity, need_role=need_role,
            serving_purpose=ServingPurpose.ANALYTICAL, execution_tier=execution_tier,
            explicit_dataset_ref=explicit)
        outcome = select_dataset_source(
            conn, need=need, candidate_dataset_refs=candidates, roles=roles,
            recorded_by=recorded_by)
        if outcome.refusal is not None:
            refusals.append(outcome.refusal)
            continue
        selections.append(outcome.selection)
        warnings.extend(outcome.warnings)
        if need_role is not DatasetNeedRole.DIMENSION_SOURCE:
            continue
        row_decisions_expected += 1
        row = resolve_row_selection(
            conn, dataset_logical_ref=outcome.selection.selected_dataset_ref,
            dataset_profile_hash=outcome.selection.selected_dataset_profile_hash,
            # A period-over-period question is HISTORICAL by construction: it is asked about a
            # window that has closed, so "today's segment" would reattribute it.
            temporality=(RequestTemporality.HISTORICAL if plan.comparison
                         else RequestTemporality.CURRENT),
            cutoff_value_ref=cutoff_value_ref, roles=roles)
        if row.refusal is not None:
            refusals.append(row.refusal)
        else:
            rows.append(row.row_selection)

    return SelectionPreviewV1(
        source_selections=tuple(selections), row_selections=tuple(rows),
        refusals=tuple(refusals), warnings=tuple(dict.fromkeys(warnings)),
        needs_considered=len(wanted), row_decisions_expected=row_decisions_expected)


def record_selection_gaps(conn, selections: SelectionPreviewV1 | None, *, question: str,
                          dependency_snapshot_id: str, now: datetime) -> tuple[str, ...]:
    """Record each selection REFUSAL as a learning gap. Returns the event ids that were written.

    **The request id is DERIVED, never minted** — ``stable_analysis_request_id``, the precedent the
    retrieval producer set. ``record_gap`` dedupes on (request, gap, snapshot), so a per-call
    ``areq-{uuid4}`` would make every row unique by construction and three retries of one blocked
    question would read as demand 3 for one thing to decide. With the derived id the retry is
    idempotent, which is pinned by test.

    ``REFUSAL_TO_GAP`` decides which refusals are evidence at all: seven of the eight closed codes
    map, and ``TEMPORAL_SCD_OVERLAP`` deliberately does not, because a data defect is not a decision
    anyone is waiting to make. ``record_refusal`` returns ``None`` for it and this records nothing.
    """
    from featuregen.analysis.retrieval import stable_analysis_request_id
    from featuregen.data_agent.learning import LearningStage, record_refusal

    if selections is None or not selections.refusals:
        return ()
    request_id = stable_analysis_request_id(
        question, dependency_snapshot_id=dependency_snapshot_id)
    written: list[str] = []
    for refusal in selections.refusals:
        event_id = record_refusal(
            conn, analysis_request_id=request_id, refusal_code=refusal.code,
            subject_refs=refusal.subject_refs, dependency_snapshot_id=dependency_snapshot_id,
            now=now, stage=LearningStage.GROUNDING)
        if event_id:
            written.append(event_id)
    return tuple(written)
