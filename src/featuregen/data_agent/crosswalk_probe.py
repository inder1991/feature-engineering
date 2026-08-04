"""The THREE-table crosswalk probe — one bounded aggregate statement, no free-form SQL.

**Why this is not the pair probe run twice.** ``render_relationship_probe_sql`` answers a question
about two tuples. A crosswalk asks a question about three, and two of the numbers it needs exist in
no pair probe:

* the COMPOSED fan-out — how many target rows one source row reaches THROUGH the mapping. The
  product of the two leg maxima is an upper bound, not a measurement (a source tuple that fans out
  in leg 1 and a different target tuple that fans out in leg 2 never multiply if they are not on the
  same path);
* the mapping's own two tuples, measured AFTER its governed row filter.

**The mapping row filter is applied FIRST, inside the statement.** Every count downstream of the
``mapping_rows`` CTE — mapping uniqueness, both legs' coverage, the composed fan-out — is measured
over the filtered rows. Measuring uniqueness over full SCD history and reporting it as a statement
about the current mapping is the "measure uniqueness before time filtering" mutation Task 13 must
kill, and the CTE is what makes it structurally impossible rather than a convention.

**Recorded, verified limitation of the shipped pair probe.** ``RelationshipFixedPredicateV1`` renders
equality-to-a-literal-set and nothing else, so the SCD predicates a governed temporal policy resolves
(``from <= cutoff AND to > cutoff``) are INEXPRESSIBLE in the V2 pair plan. That is why the composed
probe carries its own closed predicate vocabulary rather than delegating, and why a leg's V2 row id
is only claimed when its mapping-side filter is empty (``crosswalk_measurement``).

Values are quoted literals and columns are quoted identifiers; no caller-supplied SQL reaches the
engine. The result is aggregates only — no identifier value can cross the boundary.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.data_agent.observation import (
    ObservationPlanError,
    ObservationPlanV1,
    PartitionSelector,
    require_identifier,
)
from featuregen.data_agent.physical import PhysicalDatasetBindingV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.materialize.canonical import materialize_hash

CROSSWALK_PROBE_CONTRACT_VERSION = "1.0.0"

CROSSWALK_PROBE_TUPLE_MISMATCH = "CROSSWALK_PROBE_TUPLE_MISMATCH"
CROSSWALK_PROBE_UNKNOWN_PREDICATE = "CROSSWALK_PROBE_UNKNOWN_PREDICATE"
CROSSWALK_PROBE_SCOPE_MISMATCH = "CROSSWALK_PROBE_SCOPE_MISMATCH"

#: How many metric columns :func:`render_crosswalk_probe_sql` returns. Pinned so a renderer change
#: that adds a column without teaching the shaper about it fails loudly rather than silently
#: shifting every number one position to the left.
CROSSWALK_PROBE_METRIC_COUNT = 27


class ProbeSide(StrEnum):
    SOURCE = "source"
    MAPPING = "mapping"
    TARGET = "target"


class ProbePredicateKind(StrEnum):
    """CLOSED. Mirrors the resolved row-selection kinds (``temporal_resolver._predicates_for``)
    plus the partition/equality shapes the observation family already speaks."""

    EQUALITY = "equality"                 # column IN (values)
    EFFECTIVE_TIME = "effective_time"     # column <=/> a bound cutoff VALUE
    SNAPSHOT_TIME = "snapshot_time"
    AVAILABILITY_TIME = "availability_time"
    CURRENT_FLAG = "current_flag"         # column IS TRUE


_TIME_KINDS = {ProbePredicateKind.EFFECTIVE_TIME, ProbePredicateKind.SNAPSHOT_TIME,
               ProbePredicateKind.AVAILABILITY_TIME}
_TIME_OPERATORS = ("<=", "<", ">", ">=")


@dataclass(frozen=True, slots=True)
class ProbePredicateV1:
    """One governed restriction on one side. Never SQL — a kind, a column, and bound values."""

    predicate_id: str
    side: ProbeSide
    kind: ProbePredicateKind
    column: str
    operator: str = "in"
    values: tuple[str, ...] = ()
    #: The REF the runtime cutoff bound to, kept for the record's pin. The VALUE is in ``values``.
    parameter_ref: str | None = None

    def __post_init__(self) -> None:
        if not self.predicate_id.strip():
            raise ObservationPlanError(
                CROSSWALK_PROBE_UNKNOWN_PREDICATE, "predicate_id must not be blank")
        require_identifier(self.column, what="crosswalk predicate column")
        if self.kind is ProbePredicateKind.CURRENT_FLAG:
            if self.values:
                raise ObservationPlanError(
                    CROSSWALK_PROBE_UNKNOWN_PREDICATE,
                    "a current-flag predicate tests the flag itself and binds no value")
            return
        if self.kind in _TIME_KINDS:
            if self.operator not in _TIME_OPERATORS:
                raise ObservationPlanError(
                    CROSSWALK_PROBE_UNKNOWN_PREDICATE,
                    f"temporal operator {self.operator!r} is not one of {_TIME_OPERATORS}")
            if len(self.values) != 1:
                raise ObservationPlanError(
                    CROSSWALK_PROBE_UNKNOWN_PREDICATE,
                    "a temporal predicate binds exactly one cutoff value; the decision keeps the "
                    "REF and only the engine adapter takes the VALUE")
            return
        if self.operator != "in" or not self.values:
            raise ObservationPlanError(
                CROSSWALK_PROBE_UNKNOWN_PREDICATE,
                f"predicate {self.predicate_id!r} selects nothing")


@dataclass(frozen=True, slots=True)
class CrosswalkProbePlanV1:
    """One executable three-table measurement. Validated at construction, like every other plan."""

    source_binding: PhysicalDatasetBindingV1
    mapping_binding: PhysicalDatasetBindingV1
    target_binding: PhysicalDatasetBindingV1
    #: Ordered tuples. ``source_columns[i]`` joins ``mapping_source_columns[i]``, and
    #: ``mapping_target_columns[i]`` joins ``target_columns[i]``.
    source_columns: tuple[str, ...]
    mapping_source_columns: tuple[str, ...]
    mapping_target_columns: tuple[str, ...]
    target_columns: tuple[str, ...]
    policy: ProfilePolicyV1
    scope_id: str
    execution_principal: str
    predicates: tuple[ProbePredicateV1, ...] = ()
    source_partitions: PartitionSelector | None = None
    mapping_partitions: PartitionSelector | None = None
    target_partitions: PartitionSelector | None = None
    snapshot_or_as_of: str | None = None

    def __post_init__(self) -> None:
        for name in ("source_columns", "mapping_source_columns", "mapping_target_columns",
                     "target_columns"):
            columns = tuple(str(v).strip().lower() for v in getattr(self, name))
            if not columns:
                raise ObservationPlanError(
                    CROSSWALK_PROBE_TUPLE_MISMATCH, f"{name} must not be empty")
            for column in columns:
                require_identifier(column, what=f"crosswalk {name} member")
            if len(set(columns)) != len(columns):
                raise ObservationPlanError(
                    CROSSWALK_PROBE_TUPLE_MISMATCH, f"{name} must not repeat a member")
            object.__setattr__(self, name, columns)
        if len(self.source_columns) != len(self.mapping_source_columns):
            raise ObservationPlanError(
                CROSSWALK_PROBE_TUPLE_MISMATCH,
                "the source leg joins two tuples of equal arity")
        if len(self.target_columns) != len(self.mapping_target_columns):
            raise ObservationPlanError(
                CROSSWALK_PROBE_TUPLE_MISMATCH,
                "the target leg joins two tuples of equal arity")
        if set(self.mapping_source_columns) & set(self.mapping_target_columns):
            raise ObservationPlanError(
                CROSSWALK_PROBE_TUPLE_MISMATCH,
                "a mapping row's two tuples must not share a column; one column serving both sides "
                "is a direct-equality bridge, not a crosswalk")
        for name in ("scope_id", "execution_principal"):
            if not str(getattr(self, name)).strip():
                raise ObservationPlanError(
                    CROSSWALK_PROBE_SCOPE_MISMATCH, f"{name} must not be blank")
        if len({p.predicate_id for p in self.predicates}) != len(self.predicates):
            raise ObservationPlanError(
                CROSSWALK_PROBE_SCOPE_MISMATCH, "predicate_id values must be unique")
        # Reuse the single-table admission seam on ALL THREE bindings: identifier safety, caps and
        # the load-bearing "a partitioned table requires a selector" rule. A crosswalk that skipped
        # it on the mapping table would be the one unscanned full read in an otherwise bounded plan.
        for binding, columns, partitions, side in (
            (self.source_binding, self.source_columns, self.source_partitions, ProbeSide.SOURCE),
            (self.mapping_binding, self.mapping_source_columns + self.mapping_target_columns,
             self.mapping_partitions, ProbeSide.MAPPING),
            (self.target_binding, self.target_columns, self.target_partitions, ProbeSide.TARGET),
        ):
            predicate_columns = tuple(p.column for p in self.predicates if p.side is side)
            ObservationPlanV1(
                binding=binding,
                columns=tuple(dict.fromkeys((*columns, *predicate_columns))),
                policy=self.policy,
                partitions=partitions)

    @property
    def plan_hash(self) -> str:
        return materialize_hash({
            "contract_version": CROSSWALK_PROBE_CONTRACT_VERSION,
            "source_binding_revision_id": self.source_binding.binding_revision_id,
            "mapping_binding_revision_id": self.mapping_binding.binding_revision_id,
            "target_binding_revision_id": self.target_binding.binding_revision_id,
            "source_columns": list(self.source_columns),
            "mapping_source_columns": list(self.mapping_source_columns),
            "mapping_target_columns": list(self.mapping_target_columns),
            "target_columns": list(self.target_columns),
            "predicates": [
                {"predicate_id": p.predicate_id, "side": p.side.value, "kind": p.kind.value,
                 "column": p.column, "operator": p.operator, "values": list(p.values),
                 "parameter_ref": p.parameter_ref}
                for p in self.predicates],
            "source_partitions": _selector_payload(self.source_partitions),
            "mapping_partitions": _selector_payload(self.mapping_partitions),
            "target_partitions": _selector_payload(self.target_partitions),
            "scope_id": self.scope_id,
            "execution_principal": self.execution_principal,
            "snapshot_or_as_of": self.snapshot_or_as_of,
        })


def _selector_payload(selector: PartitionSelector | None) -> dict[str, object] | None:
    if selector is None:
        return None
    return {"column": selector.column, "values": list(selector.values)}


def _literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def render_crosswalk_probe_sql(plan: CrosswalkProbePlanV1, *, dialect) -> str:
    """Render ONE portable aggregate statement. No PostgreSQL-only syntax, no window functions."""

    class _Shim:
        def __init__(self, binding):
            self.binding = binding

    q = dialect.ident
    source_table = dialect.table_ref(_Shim(plan.source_binding))
    mapping_table = dialect.table_ref(_Shim(plan.mapping_binding))
    target_table = dialect.table_ref(_Shim(plan.target_binding))

    s_alias = tuple(f"s{i}" for i in range(len(plan.source_columns)))
    ms_alias = tuple(f"ms{i}" for i in range(len(plan.mapping_source_columns)))
    mt_alias = tuple(f"mt{i}" for i in range(len(plan.mapping_target_columns)))
    t_alias = tuple(f"t{i}" for i in range(len(plan.target_columns)))

    def select(columns: tuple[str, ...], aliases: tuple[str, ...]) -> str:
        return ", ".join(f"{q(column)} AS {q(alias)}"
                         for column, alias in zip(columns, aliases, strict=True))

    def where(side: ProbeSide, selector: PartitionSelector | None) -> str:
        terms: list[str] = []
        if selector is not None:
            values = ", ".join(_literal(value) for value in selector.values)
            terms.append(f"{q(selector.column)} IN ({values})")
        for predicate in plan.predicates:
            if predicate.side is not side:
                continue
            column = q(predicate.column)
            if predicate.kind is ProbePredicateKind.CURRENT_FLAG:
                terms.append(f"{column} IS TRUE")
            elif predicate.kind in _TIME_KINDS:
                terms.append(f"{column} {predicate.operator} {_literal(predicate.values[0])}")
            else:
                values = ", ".join(_literal(value) for value in predicate.values)
                terms.append(f"{column} IN ({values})")
        return (" WHERE " + " AND ".join(terms)) if terms else ""

    def not_null(aliases: tuple[str, ...]) -> str:
        return " AND ".join(f"{q(alias)} IS NOT NULL" for alias in aliases)

    def keys(aliases: tuple[str, ...]) -> str:
        return ", ".join(q(alias) for alias in aliases)

    def on(left_prefix: str, left: tuple[str, ...],
           right_prefix: str, right: tuple[str, ...]) -> str:
        return " AND ".join(
            f"{left_prefix}.{q(a)} = {right_prefix}.{q(b)}"
            for a, b in zip(left, right, strict=True))

    mapping_not_null = f"{not_null(ms_alias)} AND {not_null(mt_alias)}"
    ctes = [
        # THE FILTER, applied once, at the top. Everything below reads the filtered rows.
        f"mapping_rows AS (SELECT "
        f"{select(plan.mapping_source_columns, ms_alias)}, "
        f"{select(plan.mapping_target_columns, mt_alias)} "
        f"FROM {mapping_table}{where(ProbeSide.MAPPING, plan.mapping_partitions)})",
        f"source_rows AS (SELECT {select(plan.source_columns, s_alias)} "
        f"FROM {source_table}{where(ProbeSide.SOURCE, plan.source_partitions)})",
        f"target_rows AS (SELECT {select(plan.target_columns, t_alias)} "
        f"FROM {target_table}{where(ProbeSide.TARGET, plan.target_partitions)})",

        "mapping_raw_stats AS (SELECT COUNT(*) AS row_count, "
        f"COALESCE(SUM(CASE WHEN {mapping_not_null} THEN 1 ELSE 0 END), 0) AS non_null_count "
        "FROM mapping_rows)",
        f"mapping_src_groups AS (SELECT {keys(ms_alias)}, COUNT(*) AS n FROM mapping_rows "
        f"WHERE {mapping_not_null} GROUP BY {keys(ms_alias)})",
        f"mapping_tgt_groups AS (SELECT {keys(mt_alias)}, COUNT(*) AS n FROM mapping_rows "
        f"WHERE {mapping_not_null} GROUP BY {keys(mt_alias)})",
        f"mapping_pair_groups AS (SELECT {keys(ms_alias)}, {keys(mt_alias)} FROM mapping_rows "
        f"WHERE {mapping_not_null} GROUP BY {keys(ms_alias)}, {keys(mt_alias)})",
        "mapping_src_stats AS (SELECT COUNT(*) AS distinct_count, "
        "COALESCE(SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END), 0) AS duplicate_tuples, "
        "COALESCE(MAX(n), 0) AS max_rows FROM mapping_src_groups)",
        "mapping_tgt_stats AS (SELECT COUNT(*) AS distinct_count, "
        "COALESCE(SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END), 0) AS duplicate_tuples, "
        "COALESCE(MAX(n), 0) AS max_rows FROM mapping_tgt_groups)",
        "mapping_pair_stats AS (SELECT COUNT(*) AS distinct_pairs FROM mapping_pair_groups)",

        f"source_groups AS (SELECT {keys(s_alias)}, COUNT(*) AS n FROM source_rows "
        f"WHERE {not_null(s_alias)} GROUP BY {keys(s_alias)})",
        f"target_groups AS (SELECT {keys(t_alias)}, COUNT(*) AS n FROM target_rows "
        f"WHERE {not_null(t_alias)} GROUP BY {keys(t_alias)})",
        "source_raw_stats AS (SELECT COUNT(*) AS row_count, "
        f"COALESCE(SUM(CASE WHEN {not_null(s_alias)} THEN 1 ELSE 0 END), 0) AS non_null_count "
        "FROM source_rows)",
        "target_raw_stats AS (SELECT COUNT(*) AS row_count, "
        f"COALESCE(SUM(CASE WHEN {not_null(t_alias)} THEN 1 ELSE 0 END), 0) AS non_null_count "
        "FROM target_rows)",
        "source_group_stats AS (SELECT COUNT(*) AS distinct_count, "
        "COALESCE(SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END), 0) AS duplicate_tuples, "
        "COALESCE(MAX(n), 0) AS max_rows FROM source_groups)",
        "target_group_stats AS (SELECT COUNT(*) AS distinct_count, "
        "COALESCE(SUM(CASE WHEN n > 1 THEN 1 ELSE 0 END), 0) AS duplicate_tuples, "
        "COALESCE(MAX(n), 0) AS max_rows FROM target_groups)",

        # LEG 1 — source endpoint against the FILTERED mapping's source tuple.
        "leg1_pairs AS (SELECT sg.n AS endpoint_n, mg.n AS mapping_n FROM source_groups sg "
        f"JOIN mapping_src_groups mg ON {on('sg', s_alias, 'mg', ms_alias)})",
        "leg1_stats AS (SELECT COUNT(*) AS matched_groups, "
        "COALESCE(SUM(endpoint_n * mapping_n), 0) AS joined_rows, "
        "COALESCE(MAX(mapping_n), 0) AS max_mapping, "
        "COALESCE(MAX(endpoint_n), 0) AS max_endpoint FROM leg1_pairs)",
        "leg1_orphans AS (SELECT COUNT(*) AS distinct_count, COALESCE(SUM(sg.n), 0) AS orphan_rows "
        f"FROM source_groups sg LEFT JOIN mapping_src_groups mg "
        f"ON {on('sg', s_alias, 'mg', ms_alias)} WHERE mg.{q(ms_alias[0])} IS NULL)",

        # LEG 2 — target endpoint against the FILTERED mapping's target tuple.
        "leg2_pairs AS (SELECT tg.n AS endpoint_n, mg.n AS mapping_n FROM target_groups tg "
        f"JOIN mapping_tgt_groups mg ON {on('tg', t_alias, 'mg', mt_alias)})",
        "leg2_stats AS (SELECT COUNT(*) AS matched_groups, "
        "COALESCE(SUM(endpoint_n * mapping_n), 0) AS joined_rows, "
        "COALESCE(MAX(mapping_n), 0) AS max_mapping, "
        "COALESCE(MAX(endpoint_n), 0) AS max_endpoint FROM leg2_pairs)",
        "leg2_orphans AS (SELECT COUNT(*) AS distinct_count, COALESCE(SUM(tg.n), 0) AS orphan_rows "
        f"FROM target_groups tg LEFT JOIN mapping_tgt_groups mg "
        f"ON {on('tg', t_alias, 'mg', mt_alias)} WHERE mg.{q(mt_alias[0])} IS NULL)",

        # THE COMPOSITION — one row per (source tuple, target tuple) actually connected, carrying
        # how many mapping ROWS connect them. `path_n` is why this cannot be derived from the legs.
        f"composed AS (SELECT {', '.join('sg.' + q(a) for a in s_alias)}, "
        f"{', '.join('tg.' + q(a) for a in t_alias)}, sg.n AS source_n, tg.n AS target_n, "
        "COUNT(*) AS path_n FROM source_groups sg "
        f"JOIN mapping_rows m ON {on('sg', s_alias, 'm', ms_alias)} "
        f"JOIN target_groups tg ON {on('tg', t_alias, 'm', mt_alias)} "
        f"WHERE {' AND '.join('m.' + q(a) + ' IS NOT NULL' for a in ms_alias + mt_alias)} "
        f"GROUP BY {', '.join('sg.' + q(a) for a in s_alias)}, "
        f"{', '.join('tg.' + q(a) for a in t_alias)}, sg.n, tg.n)",
        "composed_stats AS (SELECT COALESCE(SUM(source_n * target_n * path_n), 0) AS composed_rows "
        "FROM composed)",
        f"source_reach AS (SELECT {keys(s_alias)}, SUM(target_n * path_n) AS reach FROM composed "
        f"GROUP BY {keys(s_alias)})",
        f"target_reach AS (SELECT {keys(t_alias)}, SUM(source_n * path_n) AS reach FROM composed "
        f"GROUP BY {keys(t_alias)})",
        "source_reach_stats AS (SELECT COUNT(*) AS matched_distinct, "
        "COALESCE(MAX(reach), 0) AS max_reach FROM source_reach)",
        "target_reach_stats AS (SELECT COUNT(*) AS matched_distinct, "
        "COALESCE(MAX(reach), 0) AS max_reach FROM target_reach)",
    ]

    return (
        "WITH " + ",\n     ".join(ctes) + "\n"
        "SELECT mrs.row_count, mrs.non_null_count, "
        "mss.distinct_count, mss.duplicate_tuples, mss.max_rows, "
        "mts.distinct_count, mts.duplicate_tuples, mts.max_rows, mps.distinct_pairs, "
        "srs.row_count, srs.non_null_count, sgs.distinct_count, sgs.duplicate_tuples, "
        "sgs.max_rows, "
        "trs.row_count, trs.non_null_count, tgs.distinct_count, tgs.duplicate_tuples, "
        "tgs.max_rows, "
        "l1.joined_rows, l1.max_mapping, l1o.orphan_rows, "
        "l2.joined_rows, l2.max_mapping, l2o.orphan_rows, "
        "cs.composed_rows, srr.max_reach\n"
        "FROM mapping_raw_stats mrs CROSS JOIN mapping_src_stats mss "
        "CROSS JOIN mapping_tgt_stats mts CROSS JOIN mapping_pair_stats mps "
        "CROSS JOIN source_raw_stats srs CROSS JOIN source_group_stats sgs "
        "CROSS JOIN target_raw_stats trs CROSS JOIN target_group_stats tgs "
        "CROSS JOIN leg1_stats l1 CROSS JOIN leg1_orphans l1o "
        "CROSS JOIN leg2_stats l2 CROSS JOIN leg2_orphans l2o "
        "CROSS JOIN composed_stats cs CROSS JOIN source_reach_stats srr"
    )


def render_crosswalk_probe_tail_sql(plan: CrosswalkProbePlanV1, *, dialect) -> str:
    """The four metrics the main statement's single row cannot carry without a second reach join.

    Split deliberately rather than widened: cross-joining both reach stats into the main SELECT is
    correct but makes one already-long statement harder to read than two, and both run against the
    same filtered CTEs in the same transaction and instant.
    """
    main = render_crosswalk_probe_sql(plan, dialect=dialect)
    body = main.split("\nSELECT ", 1)[0]
    return (
        body + "\n"
        "SELECT srr.matched_distinct, trr.matched_distinct, trr.max_reach, l1o.distinct_count, "
        "l2o.distinct_count\n"
        "FROM source_reach_stats srr CROSS JOIN target_reach_stats trr "
        "CROSS JOIN leg1_orphans l1o CROSS JOIN leg2_orphans l2o"
    )
