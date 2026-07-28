"""Hive dialect — renders an :class:`ObservationPlanV1` as bounded profiling SQL.

Renders only; it executes nothing. That split is what lets every safety property be asserted on the
SQL text with no cluster and no fixture — and it keeps the dialect swappable, since an ODS engine
needs different functions for the same plan.

**The dialect does not re-validate.** Bounds, partition selection and identifier safety are decided
in `observation.py`, so a second executor cannot forget them. By the time a plan reaches here it is
already safe to render.
"""
from __future__ import annotations

from featuregen.data_agent.observation import ObservationPlanV1

#: Hive's HLL sketch. Approximate by default because exact DISTINCT is a shuffle on a large table.
_APPROX_DISTINCT = "APPROX_COUNT_DISTINCT"


def _ident(name: str) -> str:
    """Backtick-quote an already-validated identifier."""
    return f"`{name.strip()}`"


def _literal(value: str) -> str:
    """A partition literal. Values reach here validated as plain strings; the doubling is belt to
    the plan's braces."""
    return "'" + str(value).replace("'", "''") + "'"


class HiveDialect:
    """SQL text for HiveServer2 / Spark SQL."""

    name = "hive"

    def table_ref(self, plan: ObservationPlanV1) -> str:
        identity = plan.binding.identity
        return ".".join(_ident(p) for p in (identity.database, identity.schema, identity.table))

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
            if plan.policy.exact_distinct:
                projections.append(
                    f"COUNT(DISTINCT {quoted}) AS {_ident(safe + '__distinct')}")
            else:
                projections.append(
                    f"{_APPROX_DISTINCT}({quoted}) AS {_ident(safe + '__distinct')}")
            if safe in bounds:
                projections.append(f"MIN({quoted}) AS {_ident(safe + '__min')}")
                projections.append(f"MAX({quoted}) AS {_ident(safe + '__max')}")

        select = ",\n       ".join(projections)
        where = self.where(plan)
        statement = f"SELECT {select}\nFROM {self.table_ref(plan)}"
        return f"{statement}\n{where}" if where else statement

    def render_row_count(self, plan: ObservationPlanV1) -> str:
        where = self.where(plan)
        stmt = f"SELECT COUNT(*) AS row_count\nFROM {self.table_ref(plan)}"
        return f"{stmt}\n{where}" if where else stmt
