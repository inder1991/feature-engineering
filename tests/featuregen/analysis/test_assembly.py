"""What still stands between a plan and an executable one.

Every route test so far stopped at "blocked". None asserted the WHOLE list, in order, or that the
list is complete — and it turned out not to be: the four gaps the route knew about were missing
attribution and join evidence, and the second of those cannot be configured at all.

The ordering is the contract: cheapest first, so a caller is asked for an address before a decision
and a decision before a data probe.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.analysis.assembly import GAP_ORDER, first_unmet_requirement
from featuregen.analysis.plan import AnalysisPlanV1, Dimension, Measure, Window
from featuregen.analysis.windows import PartitionGranularity
from featuregen.data_agent.binding_store import record_binding, record_connection
from featuregen.data_agent.connection import DataSourceConnectionV1
from featuregen.data_agent.eligibility import (
    NullBehavior,
    ReversalMode,
    TransactionEligibilityPolicyV1,
)
from featuregen.data_agent.eligibility_store import record_eligibility
from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1

_REF = datetime(2026, 6, 30, tzinfo=UTC)
_MONTH = PartitionGranularity.MONTH


def _plan(**over) -> AnalysisPlanV1:
    kw = dict(
        question="q", entity="customer", entity_ref="ftr::tran_repos.cif_id",
        base_table_ref="ftr::tran_repos", measure=Measure(op="count"), comparison="decrease",
        windows=(Window(anchor_ref="ftr::tran_repos.tran_month", length_days=0, label="current",
                        calendar_unit="month", calendar_length=1, calendar_offset=0),
                 Window(anchor_ref="ftr::tran_repos.tran_month", length_days=0, label="previous",
                        calendar_unit="month", calendar_length=1, calendar_offset=1)),
        dimensions=(),
        population_table_ref="ftr::cust_master",
        population_key_ref="ftr::cust_master.cif_id",
    )
    kw.update(over)
    return AnalysisPlanV1(**kw)


def _connection(**over) -> DataSourceConnectionV1:
    kw = dict(connection_id="c1", environment_id="uat", kind="hive", host="h", port=10000,
              auth_mechanism="kerberos", secret_ref="vault://x", execution_principal="svc_ro",
              allowed_schemas=frozenset({"dpl_eib"}), active=True)
    kw.update(over)
    return DataSourceConnectionV1(**kw)


def _bind(conn, table: str, binding_id: str) -> None:
    record_binding(conn, PhysicalDatasetBindingV1(
        binding_id=binding_id, catalog_logical_ref=f"ftr::dpl_eib.{table}", connection_id="c1",
        identity=PhysicalObjectIdentityV1(catalog_source="ftr", database="wh", schema="dpl_eib",
                                          table=table, object_kind="table")))


def _policy(conn) -> None:
    record_eligibility(
        conn, catalog_source="ftr", table="tran_repos", proposed_by="llm",
        policy=TransactionEligibilityPolicyV1(
            status_column="tran_status", included_status_values=("POSTED",),
            reversal_mode=ReversalMode.BOOLEAN_OR_CODE_COLUMN, reversal_column="reversal_flag",
            non_reversed_values=("N",), null_behavior=NullBehavior.EXCLUDE))


def _gap(db, plan=None, **kw):
    kw.setdefault("reference", _REF)
    kw.setdefault("granularity", _MONTH)
    return first_unmet_requirement(db, plan or _plan(), **kw)


# ── the list, in order ───────────────────────────────────────────────────────────────────────────

def test_an_unbound_event_table_comes_first(db):
    assert _gap(db)[0] == "PHYSICAL_BINDING_ABSENT"


def test_then_the_undeclared_population(db):
    record_connection(db, _connection())
    _bind(db, "tran_repos", "b1")
    code, subject = _gap(db, _plan(population_table_ref="", population_key_ref=""))
    assert code == "POPULATION_UNDECLARED"
    assert subject == "ftr::tran_repos"


def test_then_the_populations_own_binding(db):
    record_connection(db, _connection())
    _bind(db, "tran_repos", "b1")
    assert _gap(db) == ("PHYSICAL_BINDING_ABSENT", "ftr::cust_master")


def test_then_the_eligibility_policy(db):
    record_connection(db, _connection())
    _bind(db, "tran_repos", "b1")
    _bind(db, "cust_master", "b2")
    assert _gap(db)[0] == "ELIGIBILITY_ABSENT"


def test_then_the_partition_calendar(db):
    """Nothing records whether `tran_month` names months or days. Guessing from a value like
    `2026-05` is inference that fails silently on a table partitioned by something else."""
    record_connection(db, _connection())
    _bind(db, "tran_repos", "b1")
    _bind(db, "cust_master", "b2")
    _policy(db)
    code, subject = _gap(db, granularity=None)
    assert code == "PARTITION_CALENDAR_UNKNOWN"
    assert subject == "ftr::tran_repos.tran_month"


def test_then_attribution_when_the_question_has_dimensions(db):
    """Nothing records whether a customer is classified as of the cutoff, per period, or as they are
    today — and those give different answers."""
    record_connection(db, _connection())
    for table, bid in (("tran_repos", "b1"), ("cust_master", "b2"), ("cust_dim", "b3")):
        _bind(db, table, bid)
    _policy(db)
    plan = _plan(dimensions=(Dimension(logical_ref="ftr::cust_dim.segment"),))
    assert _gap(db, plan) == ("ATTRIBUTION_ABSENT", "ftr::cust_dim.segment")


# ── the gap no configuration can close ───────────────────────────────────────────────────────────

def test_the_LAST_gap_is_always_the_probe(db):
    """THE finding. With every store satisfied, what remains is not configuration: a verified join
    rests on OBSERVED evidence, and `observe_relationship` measures it against live data. No amount
    of setup produces it — which is the design working, because a relationship someone merely
    declared is exactly what the analysis refuses to trust."""
    record_connection(db, _connection())
    _bind(db, "tran_repos", "b1")
    _bind(db, "cust_master", "b2")
    _policy(db)
    code, subject = _gap(db)
    assert code == "JOIN_EVIDENCE_ABSENT"
    assert "cust_master.cif_id" in subject and "tran_repos.cif_id" in subject


def test_nothing_is_ever_fully_assembled_today(db):
    """Stated as a test so it cannot quietly stop being true, in either direction: if a later change
    makes this return None, the assembly completed and this test asks whether that was intended."""
    record_connection(db, _connection())
    _bind(db, "tran_repos", "b1")
    _bind(db, "cust_master", "b2")
    _policy(db)
    assert _gap(db) is not None


# ── a revoked grant is not a missing one ─────────────────────────────────────────────────────────

def test_a_withdrawn_schema_grant_does_not_read_as_unconfigured(db):
    """"Nobody bound this" and "somebody revoked it" go to different people."""
    record_connection(db, _connection(allowed_schemas=frozenset({"somewhere_else"})))
    _bind(db, "tran_repos", "b1")
    assert _gap(db)[0] not in ("PHYSICAL_BINDING_ABSENT", "POPULATION_UNDECLARED")


# ── window refusals surface with their own code ──────────────────────────────────────────────────

def test_a_day_span_window_surfaces_the_window_refusal_not_a_generic_gap(db):
    """The window resolver's own complaint is more useful than "something is missing": it says the
    plan means a span of days and the table names months."""
    record_connection(db, _connection())
    _bind(db, "tran_repos", "b1")
    _bind(db, "cust_master", "b2")
    _policy(db)
    plan = _plan(windows=(Window(anchor_ref="ftr::tran_repos.tran_month", length_days=30,
                                 label="current"),))
    assert _gap(db, plan)[0] == "WINDOW_NOT_CALENDAR_ALIGNED"


# ── the enumeration is honest about itself ───────────────────────────────────────────────────────

def test_every_gap_this_can_report_is_declared_in_GAP_ORDER():
    """A code returned but absent from the published order is a gap nobody can prepare for."""
    import inspect

    from featuregen.analysis import assembly

    src = inspect.getsource(assembly.first_unmet_requirement)
    emitted = {line.split('("')[1].split('"')[0]
               for line in src.splitlines() if 'return ("' in line}
    assert emitted <= set(GAP_ORDER), emitted - set(GAP_ORDER)


@pytest.mark.parametrize("code", GAP_ORDER)
def test_the_order_has_no_duplicates_and_each_code_is_reachable(code):
    assert GAP_ORDER.count(code) == 1
