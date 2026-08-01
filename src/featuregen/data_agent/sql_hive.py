"""Hive dialect — renders an :class:`ObservationPlanV1` as bounded profiling SQL.

Renders only; it executes nothing. That split is what lets every safety property be asserted on the
SQL text with no cluster and no fixture — and it keeps the dialect swappable, since an ODS engine
needs different functions for the same plan.

**The dialect does not re-validate.** Bounds, partition selection and identifier safety are decided
in `observation.py`, so a second executor cannot forget them. By the time a plan reaches here it is
already safe to render.
"""
from __future__ import annotations

from featuregen.data_agent.observation import (
    ObservationPlanError,
    ObservationPlanV1,
    SchemaObservationPlanV1,
)
from featuregen.data_agent.relationship_observation import (
    RelationshipObservationPlanV2,
    render_relationship_probe_sql,
)

#: Which engines behind HiveServer2 offer a built-in approximate distinct.
#:
#: `APPROX_COUNT_DISTINCT` is SPARK SQL. Plain HiveQL on Tez/MR has no built-in equivalent (the
#: DataSketches UDFs are an optional install, not a guarantee), so emitting it against a plain
#: HiveServer2 fails at RUNTIME — on the first real run, against a real cluster.
#:
#: The default is therefore `hive`: the flavour that always works. Being slower on the first profile
#: is recoverable; a failed first run against a bank cluster is a worse way to learn this.
_APPROX_BY_FLAVOUR = {"spark": "APPROX_COUNT_DISTINCT", "hive": None}


def _ident(name: str) -> str:
    """Backtick-quote an already-validated identifier."""
    return f"`{name.strip()}`"


def _literal(value: str) -> str:
    """A partition literal. Values reach here validated as plain strings; the doubling is belt to
    the plan's braces."""
    return "'" + str(value).replace("'", "''") + "'"


class HiveDialect:
    """SQL text for HiveServer2 — plain HiveQL by default, Spark SQL on request."""

    name = "hive"

    def __init__(self, flavour: str = "hive") -> None:
        if flavour not in _APPROX_BY_FLAVOUR:
            raise ObservationPlanError(
                "DIALECT_UNKNOWN_FLAVOUR",
                f"flavour {flavour!r} is not one of {sorted(_APPROX_BY_FLAVOUR)}")
        self.flavour = flavour

    def ident(self, name: str) -> str:
        """Quote one identifier. Public because the analysis compiler needs it: a caller that
        hardcodes quoting silently produces STRING LITERALS here, which HiveQL accepts and evaluates
        to something else entirely rather than rejecting."""
        return _ident(name)

    def timeout_statements(
        self, plan: ObservationPlanV1 | RelationshipObservationPlanV2 | SchemaObservationPlanV1
    ) -> tuple[str, ...]:
        """Hive's query timeout is a SESSION setting measured in SECONDS — not PostgreSQL's
        transaction-scoped milliseconds.

        Two traps this closes: sending the millisecond number would set a bound roughly a thousand
        times longer than intended, and rounding a sub-second request DOWN would reach 0, which Hive
        reads as "no limit". So it rounds UP, with a floor of one second.
        """
        seconds = max(1, -(-int(plan.policy.statement_timeout_ms) // 1000))   # ceil, min 1
        return (f"SET hive.query.timeout.seconds={seconds}",)

    @property
    def supports_approx_distinct(self) -> bool:
        return _APPROX_BY_FLAVOUR[self.flavour] is not None

    def effective_method(
        self, plan: ObservationPlanV1 | RelationshipObservationPlanV2
    ) -> str:
        """What this engine will ACTUALLY do — not what the plan asked for.

        A plan requesting `approximate` against an engine without an approximate function runs an
        exact COUNT(DISTINCT). Reporting the REQUEST would misstate the evidence in both directions:
        a consumer would believe it holds a sample when it holds a census, and would refuse a
        uniqueness proof it could legitimately make.
        """
        # The relationship probe groups complete tuples to measure actual join multiplication.
        # That operation is exact even on Spark; labelling it approximate because the request
        # preferred a sketch would understate what the engine actually executed.
        if isinstance(plan, RelationshipObservationPlanV2):
            return "exact"
        if plan.policy.exact_distinct or not self.supports_approx_distinct:
            return "exact"
        return "approximate"

    def table_ref(self, plan: ObservationPlanV1) -> str:
        """`schema.table` — TWO parts.

        This emitted `database.schema.table` until a real HiveServer2 refused it
        (`SemanticException [Error 10001]: Table not found`). HiveQL has one namespace level below
        the connection, not two: `CREATE SCHEMA` is an alias for `CREATE DATABASE`. The identity
        keeps its `database` component because an ADDRESS needs it — two clusters can hold the same
        schema name — but naming it in the statement makes every statement malformed.
        """
        identity = plan.binding.identity
        return ".".join(_ident(p) for p in (identity.schema, identity.table))

    def where(self, plan: ObservationPlanV1) -> str:
        """The partition predicate. Empty only for a table the binding declares unpartitioned —
        `observation.py` refuses a partitioned table with no selector, so this cannot silently
        become a full scan."""
        if plan.partitions is None:
            return ""
        values = ", ".join(_literal(v) for v in plan.partitions.values)
        return f"WHERE {_ident(plan.partitions.column)} IN ({values})"

    def render_column_profile(self, plan: ObservationPlanV1) -> str:
        """One statement profiling every requested column.

        ONE scan for all columns rather than one query per column: the shipped grounding engine's
        `_load_columns` defect — a per-item query inside a loop — cost 157 catalog scans per pass,
        and the same shape against a partitioned Hive table would be far more expensive.

        Every projected expression is an AGGREGATE — but aggregate is not the same as SAFE. MIN/MAX
        return real values (`MIN(cif_id)` is a customer identifier), so value bounds are rendered
        ONLY for columns the plan explicitly opted in.
        """
        bounds = {c.strip().lower() for c in plan.bounds_columns}
        projections: list[str] = ["COUNT(*) AS row_count"]
        for column in plan.columns:
            quoted = _ident(column)
            safe = column.strip().lower()
            projections.append(f"COUNT({quoted}) AS {_ident(safe + '__non_null')}")
            approx = _APPROX_BY_FLAVOUR[self.flavour]
            if plan.policy.exact_distinct or approx is None:
                projections.append(
                    f"COUNT(DISTINCT {quoted}) AS {_ident(safe + '__distinct')}")
            else:
                projections.append(
                    f"{approx}({quoted}) AS {_ident(safe + '__distinct')}")
            if safe in bounds:
                projections.append(f"MIN({quoted}) AS {_ident(safe + '__min')}")
                projections.append(f"MAX({quoted}) AS {_ident(safe + '__max')}")

        select = ",\n       ".join(projections)
        where = self.where(plan)
        statement = f"SELECT {select}\nFROM {self.table_ref(plan)}"
        return f"{statement}\n{where}" if where else statement

    def render_schema_observation(self, plan: SchemaObservationPlanV1) -> str:
        """`DESCRIBE` — the engine's own account of what the table physically holds.

        Two-part backticked name, same as :meth:`table_ref`: a double-quoted identifier is a
        STRING LITERAL in HiveQL, so a "safely quoted" statement would be accepted and evaluated
        to something else entirely. The identity's `database` stays an address, never a name part
        (a real HiveServer2 refuses three parts).

        Plain `DESCRIBE`, not `FORMATTED`/`EXTENDED`: the extended forms interleave table metadata
        prose with the column list, and the executor's shaping should skip a known marker section,
        not parse a report."""
        identity = plan.binding.identity
        return f"DESCRIBE {_ident(identity.schema)}.{_ident(identity.table)}"

    def render_row_count(self, plan: ObservationPlanV1) -> str:
        where = self.where(plan)
        stmt = f"SELECT COUNT(*) AS row_count\nFROM {self.table_ref(plan)}"
        return f"{stmt}\n{where}" if where else stmt

    def render_relationship_probe(self, plan: RelationshipObservationPlanV2) -> str:
        return render_relationship_probe_sql(plan, dialect=self)
