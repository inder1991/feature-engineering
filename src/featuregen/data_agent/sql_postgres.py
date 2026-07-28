"""PostgreSQL dialect — the same observation plan, a different engine.

It exists for two reasons, both real:

* **Validation without a cluster.** Release 1 step 4 requires hand-computed answers, and a local
  PostgreSQL proves the plan -> dialect -> executor -> typed-result path end to end. When Hive
  access arrives, the only unproven piece is Hive's SQL text.
* **ODS.** An operational data store is frequently PostgreSQL or Oracle, and the roadmap's answer
  for ODS is bounded native SQL. This is that path, starting where it can be tested.

The differences from Hive are confined to identifier quoting and the approximate-distinct function
-- which is precisely the claim the dialect abstraction makes.
"""
from __future__ import annotations

from featuregen.data_agent.observation import ObservationPlanV1


def _ident(name: str) -> str:
    return '"' + name.strip().replace('"', '""') + '"'


def _literal(value: str) -> str:
    return "'" + str(value).replace("'", "''") + "'"


class PostgresDialect:
    """SQL text for PostgreSQL (and the starting point for an ODS adapter)."""

    name = "postgres"

    def timeout_statements(self, plan: ObservationPlanV1) -> tuple[str, ...]:
        """`SET LOCAL` is transaction-scoped, so the bound dies with the caller's transaction rather
        than leaking onto a pooled connection."""
        return (f"SET LOCAL statement_timeout = {int(plan.policy.statement_timeout_ms)}",)

    def effective_method(self, plan: ObservationPlanV1) -> str:
        """Always `exact`. PostgreSQL has no built-in approximate distinct, so an approximate
        REQUEST runs a census here — and the result must report the census, not the request. An
        earlier version reported the plan's method and so overstated its own imprecision."""
        return "exact"

    def table_ref(self, plan: ObservationPlanV1) -> str:
        identity = plan.binding.identity
        # The database is selected by the CONNECTION, not named in the statement — the one place
        # Postgres genuinely differs from Hive's three-part name.
        return f"{_ident(identity.schema)}.{_ident(identity.table)}"

    def where(self, plan: ObservationPlanV1) -> str:
        if plan.partitions is None:
            return ""
        values = ", ".join(_literal(v) for v in plan.partitions.values)
        return f"WHERE {_ident(plan.partitions.column)} IN ({values})"

    def render_column_profile(self, plan: ObservationPlanV1) -> str:
        """Same shape as Hive: one scan, aggregates only, bounds only where opted in.

        PostgreSQL has no APPROX_COUNT_DISTINCT, so an approximate request falls back to exact here
        and the RESULT still reports the plan's declared method. That is honest for a local fixture
        and for a small ODS table; a large ODS table wanting a genuine sketch needs an extension,
        which is a Release-6 concern rather than a first-slice one.
        """
        bounds = {c.strip().lower() for c in plan.bounds_columns}
        projections: list[str] = ["COUNT(*) AS row_count"]
        for column in plan.columns:
            quoted, safe = _ident(column), column.strip().lower()
            projections.append(f"COUNT({quoted}) AS {_ident(safe + '__non_null')}")
            projections.append(f"COUNT(DISTINCT {quoted}) AS {_ident(safe + '__distinct')}")
            if safe in bounds:
                projections.append(f"MIN({quoted}::text) AS {_ident(safe + '__min')}")
                projections.append(f"MAX({quoted}::text) AS {_ident(safe + '__max')}")
        select = ",\n       ".join(projections)
        where = self.where(plan)
        stmt = f"SELECT {select}\nFROM {self.table_ref(plan)}"
        return f"{stmt}\n{where}" if where else stmt
