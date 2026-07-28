"""The Hive dialect — Release 1 step 2.

Renders bounded profiling SQL. It executes nothing; step 3's executor does that. Splitting them is
what lets every safety property below be asserted on the SQL TEXT, with no cluster and no fixture.

The properties are executor-independent by design (roadmap §3c): partition pruning, aggregate-only
egress, approximate-by-default, and the caps. If the Spark/Kedro executor arrives later it must
satisfy the same list, because they are plan properties, not packaging.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.observation import (
    ObservationPlanError,
    ObservationPlanV1,
    PartitionSelector,
)
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.sql_hive import HiveDialect


def _table(**over):
    kw = dict(catalog_source="ftr", database="banking", schema="dpl_eib",
              table="tran_repos", object_kind="table")
    kw.update(over)
    return PhysicalObjectIdentityV1(**kw)


def _binding(partitions=("tran_date",), **over):
    kw = dict(binding_id="b-1", catalog_logical_ref="ftr::dpl_eib.tran_repos",
              connection_id="hive-pilot", identity=_table(),
              partition_columns=partitions,
              business_time_column="tran_date" if partitions else None)
    kw.update(over)
    return PhysicalDatasetBindingV1(**kw)


def _plan(**over):
    kw = dict(binding=_binding(), columns=("cif_id", "tran_amt", "tran_type"),
              partitions=PartitionSelector(column="tran_date", values=("2026-06-01", "2026-06-02")),
              policy=ProfilePolicyV1())
    kw.update(over)
    return ObservationPlanV1(**kw)


def _sql(plan=None) -> str:
    return HiveDialect().render_column_profile(plan or _plan())


# ── partition pruning: correctness and cost, not an optimisation ─────────────────────────────────

def test_the_partition_predicate_is_always_present():
    sql = _sql()
    assert "`tran_date` IN (" in sql
    assert "'2026-06-01'" in sql and "'2026-06-02'" in sql


def test_a_partitioned_table_with_no_selector_is_REFUSED_not_scanned():
    """The single most expensive mistake available here. `SELECT COUNT(*) FROM transactions` with no
    predicate is a full scan of a partitioned bank table — the first profiling run being the one the
    DBA remembers. Refuse rather than emit it."""
    with pytest.raises(ObservationPlanError, match="partition"):
        _plan(partitions=None)


def test_an_unpartitioned_table_needs_no_selector():
    """Small dimension tables are legitimately unpartitioned; the rule is about tables that CAN be
    pruned, not about every table."""
    sql = _sql(_plan(binding=_binding(partitions=()), partitions=None))
    assert "WHERE" not in sql.upper() or "1=1" in sql


def test_the_selector_must_name_a_declared_partition_column():
    with pytest.raises(ObservationPlanError, match="partition"):
        _plan(partitions=PartitionSelector(column="posted_ts", values=("x",)))


def test_an_empty_partition_selection_is_refused():
    """Selecting no partitions is either a mistake or a full scan waiting for a fallback."""
    with pytest.raises(ObservationPlanError):
        _plan(partitions=PartitionSelector(column="tran_date", values=()))


# ── egress: aggregates only, never rows ──────────────────────────────────────────────────────────

def test_the_projection_contains_only_aggregates():
    """The boundary property. Every selected expression must be an aggregate, or a row could cross
    into the control plane — which no downstream suppression can undo."""
    sql = _sql()
    select = sql[sql.upper().index("SELECT") + 6: sql.upper().index("FROM")]
    for expression in [e.strip() for e in select.split(",") if e.strip()]:
        assert any(expression.upper().startswith(fn)
                   for fn in ("COUNT(", "APPROX_COUNT_DISTINCT(", "MIN(", "MAX(", "SUM(", "AVG(")), \
            f"non-aggregate in projection: {expression}"


def test_no_star_projection_is_ever_emitted():
    assert "*" not in _sql().replace("COUNT(*)", "")


def test_top_values_are_not_requested_by_default():
    """Categorical top values are real VALUES leaving the data plane. They need an explicit policy
    grant, because a top value on a name column is a customer name."""
    assert "tran_type" not in _sql().lower() or "top" not in _sql().lower()


# ── approximate by default, and say which ────────────────────────────────────────────────────────

def test_distinct_counts_are_approximate_where_the_ENGINE_supports_it():
    """Exact DISTINCT on a large table is a shuffle, so approximate is preferred WHERE AVAILABLE.

    An earlier version of this test asserted approximate unconditionally and so encoded a bug: it
    was satisfied by emitting `APPROX_COUNT_DISTINCT` against plain HiveQL, which has no such
    function and would have failed on the first real cluster run."""
    sql = HiveDialect(flavour="spark").render_column_profile(_plan())
    assert "APPROX_COUNT_DISTINCT(" in sql
    assert "COUNT(DISTINCT" not in sql.upper()


def test_exact_distinct_requires_an_explicit_policy():
    sql = _sql(_plan(policy=ProfilePolicyV1(exact_distinct=True)))
    assert "COUNT(DISTINCT" in sql.upper()
    assert "APPROX_COUNT_DISTINCT(" not in sql


def test_the_plan_reports_the_method_it_used():
    assert _plan().method == "approximate"
    assert _plan(policy=ProfilePolicyV1(exact_distinct=True)).method == "exact"


# ── bounds ───────────────────────────────────────────────────────────────────────────────────────

def test_the_column_cap_is_enforced():
    with pytest.raises(ObservationPlanError, match="max_columns"):
        _plan(columns=tuple(f"c{i}" for i in range(100)),
              policy=ProfilePolicyV1(max_columns=8))


def test_the_partition_cap_is_enforced():
    with pytest.raises(ObservationPlanError, match="max_partitions"):
        _plan(partitions=PartitionSelector(column="tran_date",
                                           values=tuple(f"2026-06-{d:02d}" for d in range(1, 30))),
              policy=ProfilePolicyV1(max_partitions=7))


# ── identifiers ──────────────────────────────────────────────────────────────────────────────────

def test_identifiers_are_backtick_quoted_and_validated():
    sql = _sql()
    assert "`banking`.`dpl_eib`.`tran_repos`" in sql
    assert "`cif_id`" in sql


@pytest.mark.parametrize("bad", ["a`b", "a b", "a;drop", "a'b", ""])
def test_an_unsafe_identifier_is_refused_rather_than_escaped(bad):
    """Quoting is not the defence — refusal is. A Hive identifier is [A-Za-z0-9_], so anything else
    is a mistake or an attack, and neither should be rendered."""
    with pytest.raises(ObservationPlanError):
        _plan(columns=(bad,))


# ── min/max is a VALUE, not a statistic ──────────────────────────────────────────────────────────
# Found by reading the rendered SQL rather than by a test: MIN(`cif_id`) returns an actual customer
# identifier, and MIN on a name column returns an actual name. "Aggregate" is not the same as
# "safe" — an aggregate over one column of one row is that row. So value bounds are opt-in per
# column, for the columns where a bound is a statistic (amounts, dates) rather than a disclosure.

def test_no_value_bounds_are_emitted_by_default():
    """The default must not return values. `cif_id` is the case that matters: a min/max over an
    identifier column is two real customer identifiers."""
    sql = _sql()
    assert "MIN(" not in sql and "MAX(" not in sql


def test_value_bounds_are_emitted_only_for_opted_in_columns():
    sql = _sql(_plan(bounds_columns=("tran_amt",)))
    assert "MIN(`tran_amt`)" in sql and "MAX(`tran_amt`)" in sql
    assert "MIN(`cif_id`)" not in sql


def test_a_bounds_column_must_be_one_of_the_profiled_columns():
    with pytest.raises(ObservationPlanError, match="bounds"):
        _plan(bounds_columns=("not_profiled",))


# ── engine capability: approximate distinct is not universal ─────────────────────────────────────
# `APPROX_COUNT_DISTINCT` is SPARK SQL. Plain HiveQL on Tez/MR has no built-in approximate distinct,
# so emitting it against HiveServer2 fails at runtime — and reporting "approximate" when the engine
# actually ran an exact COUNT(DISTINCT) misstates what the evidence can support in BOTH directions.

def test_spark_flavour_uses_the_approximate_function():
    sql = HiveDialect(flavour="spark").render_column_profile(_plan())
    assert "APPROX_COUNT_DISTINCT(" in sql
    assert HiveDialect(flavour="spark").effective_method(_plan()) == "approximate"


def test_hive_flavour_falls_back_to_exact_and_SAYS_SO():
    """The honest fallback. HiveQL has no approximate distinct, so the request degrades to exact —
    and the reported method must degrade with it, or a consumer believes it holds a sample when it
    holds a census (and refuses a uniqueness proof it could actually make)."""
    dialect = HiveDialect(flavour="hive")
    sql = dialect.render_column_profile(_plan())
    assert "COUNT(DISTINCT" in sql.upper()
    assert "APPROX_COUNT_DISTINCT(" not in sql
    assert dialect.effective_method(_plan()) == "exact"


def test_an_exact_request_is_exact_on_every_flavour():
    exact = _plan(policy=ProfilePolicyV1(exact_distinct=True))
    for flavour in ("hive", "spark"):
        dialect = HiveDialect(flavour=flavour)
        assert "COUNT(DISTINCT" in dialect.render_column_profile(exact).upper()
        assert dialect.effective_method(exact) == "exact"


def test_the_default_flavour_is_the_conservative_one():
    """Defaulting to Spark would emit a function that does not exist on a plain HiveServer2 — a
    runtime failure on the first real run. Default to what always works."""
    assert HiveDialect().flavour == "hive"


def test_an_unknown_flavour_is_refused():
    with pytest.raises(ObservationPlanError, match="flavour"):
        HiveDialect(flavour="impala")
