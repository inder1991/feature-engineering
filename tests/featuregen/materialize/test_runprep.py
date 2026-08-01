"""Spec A Task 15a — §3.3 run preparation: physical snapshots and execution identity.

**The two hash rules are the task, and they pull in opposite directions.** Snapshots must NOT move
`ir_hash` — a generated project that changed every business date would not be a hash-identified
artifact — while `sandbox_execution_hash` MUST move with `business_dt`, or two runs over two dates
share one execution identity and the staging manifests cannot tell them apart (§9). Each is proved
on its own below, and the execution-hash proof is deliberately run over a VERIFIED UNPARTITIONED
input as well, where the resolved snapshots are byte-identical across the two dates: if the date
only reached the hash through the partitions it selects, that test fails.

**Snapshots resolve per EXPRESSION, not per feature.** A ratio's numerator and denominator are two
reads with two `PitSpec`s, and a 30-day feature and a 90-day feature over the same table select two
different partition sets. Every de-duplication here therefore compares SEMANTICS first: the id a
read is collapsed on covers the requirement, the resolved partitions and the business date, so two
reads that merely look alike (same table, different window) can never collapse into one.

**`business_dt` is never assumed to BE the partition column.** A table partitioned on `load_dt` is
read through its DECLARED mapping (§3.4), and the resolved values are the window's dates rather than
the business date — a 1-day trailing window ending at 2026-07-27 reads 2026-07-26.
"""
from __future__ import annotations

import dataclasses
import datetime as dt
import inspect

import pytest
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize.test_group_plan import (  # noqa: F401 — `catalog` is a fixture
    SUM_30D,
    catalog,
)
from tests.featuregen.materialize.test_ir import INVENTORY, _compile
from tests.featuregen.materialize.test_ir import _ok as _compiled

from featuregen.materialize import compile, identity, render, runprep
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.expression_ir import AvailabilityBasis, PitSpec
from featuregen.materialize.identity import CompilationIdentity, seal_project
from featuregen.materialize.inputs import PhysicalInputRequirement
from featuregen.materialize.inventory import (
    AvailabilityPartition,
    ClusterInventoryV1,
    EventTimePartition,
    FullScan,
    PartitionTransform,
    StaticSnapshot,
    TableLayout,
    VerifiedUnpartitioned,
)
from featuregen.materialize.ir import BridgeExecutionAuthorization, ir_hash
from featuregen.materialize.render import nodes_compute
from featuregen.materialize.render.project import REQUIRED_RUN_PARAMETERS
from featuregen.materialize.runprep import (
    PhysicalInputSnapshot,
    RunInputRequest,
    input_snapshot_ids,
    prepare_run,
    resolve_snapshots,
    run_input_requests,
    staging_root_for,
)

BUSINESS_DT = "2026-07-27"
NEXT_DT = "2026-07-28"
SOURCE = "hdfc"
TXN_DT = f"{SOURCE}::public.transactions.txn_dt"
ZONE = "Asia/Kolkata"


# ── the declared environment: one table, one mapping at a time ───────────────────────────────────

def _layout(mapping, *, partition_columns=(("load_dt", "string"),), table="transactions",
            columns=(("txn_amt", "decimal(18,2)"), ("txn_dt", "timestamp"))):
    # `txn_dt` is TIMESTAMP-typed by default: the default `_pit()` zone is Asia/Kolkata, and a
    # DATE-typed clock under a non-UTC zone now refuses at `prepare_run` (codegen-review Task 9) —
    # the date-typed variant is built per test via `columns=_DATE_CLOCK_COLUMNS`.
    return TableLayout(
        schema="banking", table=table, partition_columns=partition_columns,
        partition_mapping=mapping, columns=columns,
        location=f"hdfs://nn/warehouse/banking.db/{table}", rewritten_in_place=False)


def _inventory(*layouts, environment_id="hdfc-local") -> ClusterInventoryV1:
    return ClusterInventoryV1(
        environment_id=environment_id,
        tables={f"{layout.schema}.{layout.table}": layout for layout in layouts},
        logical_schema_map={f"{SOURCE}::public.{layout.table}": layout.schema
                            for layout in layouts},
        engine_versions=fixtures.ENGINE_VERSIONS, captured_at="2026-07-27T09:00:00+05:30")


def _requirement(layout: TableLayout) -> PhysicalInputRequirement:
    """A requirement DERIVED from the same layout the inventory declares, so the two agree.

    Built here rather than through `derive_requirement` because that needs a seeded catalog, and
    every property below is about what run preparation does with a requirement, not about how one
    is derived.
    """
    return PhysicalInputRequirement(
        catalog_source=SOURCE, schema=layout.schema, table=layout.table,
        partition_columns=layout.partition_columns, partition_mapping=layout.partition_mapping,
        layout_fingerprint=materialize_hash(layout.semantic_payload()),
        catalog_state_stamp=(("stamp_kind", "drift_watermark"), ("head_seq", "42")))


def _event_mapping(*, column="load_dt", transform=PartitionTransform.DATE_ISO, timezone=ZONE):
    return EventTimePartition(time_ref=TXN_DT, partition_column=column, transform=transform,
                              timezone=timezone)


def _availability_mapping(*, late_arrival_days=5):
    return AvailabilityPartition(time_ref=TXN_DT, partition_column="load_dt",
                                 transform=PartitionTransform.DATE_ISO, timezone=ZONE,
                                 late_arrival_days=late_arrival_days)


def _pit(*, length=90, unit="day", basis="trailing", start="inclusive", end="exclusive",
         timezone=ZONE) -> PitSpec:
    return PitSpec(
        event_time_ref=TXN_DT, availability_ref=TXN_DT,
        availability_basis=AvailabilityBasis.POSTED_AT, availability_lag_hours=None,
        window_basis=basis, window_length=length, window_unit=unit,
        window_start_inclusive=start, window_end_inclusive=end, window_timezone=timezone)


def _request(requirement, *, feature="total_debit_amount_30d", expr_path="body.expr", pit=None):
    return RunInputRequest(feature_name=feature, expr_path=expr_path,
                           physical_requirement=requirement,
                           pit_spec=_pit() if pit is None else pit)


class _Metastore:
    """The seam Task 0's `MetastoreInventoryAdapter` must satisfy — metadata only, no rows.

    It answers ONE question: which partitions does this table actually have? Nothing here reads a
    value out of a partition, which is what keeps the control plane away from feature data.
    """

    def __init__(self, partitions=None):
        self._partitions = dict(partitions or {})
        self.calls: list[tuple[str, str]] = []

    def list_partitions(self, *, schema: str, table: str):
        self.calls.append((schema, table))
        return tuple(self._partitions.get(f"{schema}.{table}", ()))


def _ok(value):
    """Assert a resolution succeeded, so a refusal fails the test HERE rather than three lines on."""
    assert not isinstance(value, MaterializationRefused), getattr(value, "detail", value)
    return value


def _dates(snapshot: PhysicalInputSnapshot) -> list[str]:
    assert snapshot.partition_specs is not None
    return [value for spec in snapshot.partition_specs for _column, value in spec.columns]


# ══ the Produces signature ═══════════════════════════════════════════════════════════════════════


def test_resolve_snapshots_takes_REQUESTS_and_has_no_window_parameter() -> None:
    """The plan's Produces line is authoritative; two of its own snippets are stale.

    They call `requirements=(req,)` and pass a `window=_window(30)` the declared signature does not
    have — a leftover from a draft where one window described a whole group. §3.3 settles it in
    prose: *"A single `window` argument cannot describe those reads"*, because a ratio's two
    expressions may read different tables with different windows. The window arrives PER REQUEST, on
    `RunInputRequest.pit_spec`, which is the same reason the requests are plural.
    """
    params = inspect.signature(resolve_snapshots).parameters
    assert list(params) == ["inventory", "metastore", "requests", "business_dt"]
    assert "window" not in params and "requirements" not in params
    assert [name for name, p in params.items() if p.kind is p.KEYWORD_ONLY] == \
        ["requests", "business_dt"]


def test_a_request_carries_ONE_expression_s_read_and_its_OWN_pit() -> None:
    names = [field.name for field in dataclasses.fields(RunInputRequest)]
    assert names == ["feature_name", "expr_path", "physical_requirement", "pit_spec"]


# ══ per-expression resolution ════════════════════════════════════════════════════════════════════


def test_each_expression_resolves_its_OWN_snapshots() -> None:
    """A 30d feature, two 90d features and a ratio's two expressions cannot share one window."""
    layout = _layout(_event_mapping())
    requirement = _requirement(layout)
    requests = (
        _request(requirement, feature="total_debit_amount_30d", pit=_pit(length=30)),
        _request(requirement, feature="distinct_merchant_count_90d", pit=_pit(length=90)),
        _request(requirement, feature="cross_border_value_ratio_90d", expr_path="body.numerator",
                 pit=_pit(length=90)),
        _request(requirement, feature="cross_border_value_ratio_90d", expr_path="body.denominator",
                 pit=_pit(length=30)),
    )
    snaps = _ok(resolve_snapshots(_inventory(layout), _Metastore(), requests=requests,
                                  business_dt=BUSINESS_DT))
    assert {(s.feature_name, s.expr_path) for s in snaps} == {
        ("total_debit_amount_30d", "body.expr"),
        ("distinct_merchant_count_90d", "body.expr"),
        ("cross_border_value_ratio_90d", "body.numerator"),
        ("cross_border_value_ratio_90d", "body.denominator")}
    by_key = {(s.feature_name, s.expr_path): s for s in snaps}
    assert len(_dates(by_key[("cross_border_value_ratio_90d", "body.numerator")])) == 90
    assert len(_dates(by_key[("cross_border_value_ratio_90d", "body.denominator")])) == 30


def test_a_ratio_s_two_expressions_over_ONE_table_keep_their_OWN_windows() -> None:
    """The failure this forecloses: one expression silently inheriting the other's window."""
    layout = _layout(_event_mapping())
    requirement = _requirement(layout)
    requests = (
        _request(requirement, feature="ratio", expr_path="body.numerator", pit=_pit(length=90)),
        _request(requirement, feature="ratio", expr_path="body.denominator", pit=_pit(length=30)),
    )
    numerator, denominator = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(), requests=requests, business_dt=BUSINESS_DT))
    assert len(_dates(numerator)) == 90 and len(_dates(denominator)) == 30
    assert set(_dates(denominator)) < set(_dates(numerator))


def test_one_request_per_EXPRESSION_and_requirement_comes_off_the_IRs(catalog, db) -> None:  # noqa: F811
    """`run_input_requests` is the bridge from compiled IRs, and it is per expression BY SHAPE."""
    ir = _compiled(_compile(db, SUM_30D))
    requests = run_input_requests((ir,))
    assert {(r.feature_name, r.expr_path) for r in requests} == \
        {(ir.feature_name, expr.expr_path) for expr in ir.expressions}
    assert len(requests) == sum(len(expr.input_requirements) for expr in ir.expressions)
    for request in requests:
        expression = next(e for e in ir.expressions if e.expr_path == request.expr_path)
        assert request.pit_spec is expression.pit


# ══ the DECLARED mapping decides — never the business date ═══════════════════════════════════════


def test_business_dt_is_never_assumed_to_BE_the_partition_column() -> None:
    """A table partitioned on `load_dt` is read through its declared mapping — column AND value.

    The value assertion is the sharper half: a 1-day trailing window ending at 2026-07-27 reads
    2026-07-26, so an implementation that partitioned on the business date would fail here even if
    it happened to name the column correctly.
    """
    layout = _layout(_event_mapping())
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(_request(_requirement(layout), pit=_pit(length=1)),), business_dt=BUSINESS_DT))
    cols = {column for spec in snaps[0].partition_specs for column, _value in spec.columns}
    assert cols == {"load_dt"}
    assert _dates(snaps[0]) == ["2026-07-26"]
    assert BUSINESS_DT not in _dates(snaps[0])


def test_a_DECLARED_one_day_event_mapping_resolves_90_partitions() -> None:
    """Valid ONLY for a declared one-day EVENT_TIME_PARTITION mapping — not a general rule."""
    layout = _layout(_event_mapping())
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(_request(_requirement(layout), pit=_pit(length=90)),), business_dt=BUSINESS_DT))
    assert len(snaps[0].partition_specs) == 90
    assert _dates(snaps[0])[0] == "2026-04-28" and _dates(snaps[0])[-1] == "2026-07-26"


def test_availability_mapping_widens_beyond_the_event_window() -> None:
    """Late arrivals sit in load partitions OUTSIDE the event range (§3.4)."""
    layout = _layout(_availability_mapping(late_arrival_days=5))
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(_request(_requirement(layout), pit=_pit(length=90)),), business_dt=BUSINESS_DT))
    assert len(snaps[0].partition_specs) > 90
    assert len(snaps[0].partition_specs) == 95
    assert _dates(snaps[0])[-1] == "2026-07-31"


def test_the_widening_is_by_the_DECLARED_late_arrival_days_and_nothing_else() -> None:
    layout = _layout(_availability_mapping(late_arrival_days=30))
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(_request(_requirement(layout), pit=_pit(length=90)),), business_dt=BUSINESS_DT))
    assert len(snaps[0].partition_specs) == 120


def test_a_declared_transform_renders_the_partition_VALUE() -> None:
    """`PartitionTransform` is closed, and the rendered spine applies the same two forms."""
    layout = _layout(_event_mapping(transform=PartitionTransform.DATE_COMPACT))
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(_request(_requirement(layout), pit=_pit(length=1)),), business_dt=BUSINESS_DT))
    assert _dates(snaps[0]) == ["20260726"]
    assert set(PartitionTransform) == set(runprep.PARTITION_VALUE_FORMS), \
        "a transform with no form here would be defaulted to the date as written"


def test_a_static_snapshot_reads_the_DECLARED_values_and_never_moves_with_the_date() -> None:
    layout = _layout(StaticSnapshot(partition_values=(("vintage", "2026Q2"),)),
                     partition_columns=(("vintage", "string"),))
    inventory, requirement = _inventory(layout), _requirement(layout)
    first = _ok(resolve_snapshots(inventory, _Metastore(), requests=(_request(requirement),),
                                  business_dt=BUSINESS_DT))
    later = _ok(resolve_snapshots(inventory, _Metastore(), requests=(_request(requirement),),
                                  business_dt=NEXT_DT))
    assert first[0].partition_specs == later[0].partition_specs
    assert _dates(first[0]) == ["2026Q2"]


def test_a_full_scan_reads_the_partitions_the_METASTORE_lists() -> None:
    layout = _layout(FullScan())
    metastore = _Metastore({"banking.transactions": ((("load_dt", "2026-07-25"),),
                                                     (("load_dt", "2026-07-26"),))})
    snaps = _ok(resolve_snapshots(_inventory(layout), metastore,
                                  requests=(_request(_requirement(layout)),),
                                  business_dt=BUSINESS_DT))
    assert _dates(snaps[0]) == ["2026-07-25", "2026-07-26"]
    assert metastore.calls == [("banking", "transactions")]


def test_a_full_scan_over_a_table_the_metastore_lists_NO_partitions_for_refuses() -> None:
    """Fail closed: "no partitions" and "the metastore did not answer" are the same empty list, and
    reading nothing would return a smaller number with no error."""
    layout = _layout(FullScan())
    refusal = resolve_snapshots(_inventory(layout), _Metastore(),
                                requests=(_request(_requirement(layout)),),
                                business_dt=BUSINESS_DT)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN


def test_verified_unpartitioned_yields_None() -> None:
    layout = _layout(VerifiedUnpartitioned(), partition_columns=None)
    snaps = _ok(resolve_snapshots(_inventory(layout), _Metastore(),
                                  requests=(_request(_requirement(layout)),),
                                  business_dt=BUSINESS_DT))
    assert snaps[0].partition_specs is None


def test_a_MISSING_partition_does_not_shrink_the_resolved_set() -> None:
    """The metastore is not consulted for a windowed read, and deliberately so.

    Intersecting the window's partitions with the ones that exist would turn a missing partition
    into a smaller number with no error. L1 (§11.2) reports it as `PARTITION_ABSENT` instead.
    """
    layout = _layout(_event_mapping())
    metastore = _Metastore({"banking.transactions": ()})
    snaps = _ok(resolve_snapshots(_inventory(layout), metastore,
                                  requests=(_request(_requirement(layout), pit=_pit(length=30)),),
                                  business_dt=BUSINESS_DT))
    assert len(snaps[0].partition_specs) == 30
    assert metastore.calls == []


def test_an_unwindowed_read_lets_the_MAPPING_alone_select_the_set() -> None:
    """A spine's PARTITION_MAPPED policy has no window: the mapping selects the business date's own
    partition, which is what the rendered spine node does (§4.2)."""
    layout = _layout(_event_mapping())
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(RunInputRequest(feature_name="cif_daily", expr_path="spine",
                                  physical_requirement=_requirement(layout), pit_spec=None),),
        business_dt=BUSINESS_DT))
    assert _dates(snaps[0]) == [BUSINESS_DT]


# ══ the window arithmetic is the RENDERER's, restated and pinned ═════════════════════════════════


def test_the_unit_tables_are_the_ONES_THE_RENDERER_RENDERS() -> None:
    """Run preparation selects partitions; the rendered predicate selects rows. If the two disagree
    about how long a month is, the run reads partitions whose rows the predicate then drops."""
    assert runprep.DAYS_PER_UNIT == nodes_compute._DAYS_PER_UNIT
    assert runprep.MONTHS_PER_UNIT == nodes_compute._MONTHS_PER_UNIT
    assert set(runprep.PERIOD_STARTS) == set(nodes_compute._PERIOD_START)


@pytest.mark.parametrize("start, end, expected", [
    ("inclusive", "exclusive", 30),     # the default half-open window: [T-30, T)
    ("inclusive", "inclusive", 31),     # the end instant is midnight of T, which is inside day T
    ("exclusive", "exclusive", 30),     # excluding the start INSTANT leaves the rest of its day
])
def test_BOTH_inclusivity_flags_are_honoured(start: str, end: str, expected: int) -> None:
    layout = _layout(_event_mapping())
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(_request(_requirement(layout), pit=_pit(length=30, start=start, end=end)),),
        business_dt=BUSINESS_DT))
    assert len(snaps[0].partition_specs) == expected


def test_a_MONTH_is_calendar_arithmetic_and_not_thirty_days() -> None:
    layout = _layout(_event_mapping())
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(_request(_requirement(layout), pit=_pit(length=1, unit="month")),),
        business_dt="2026-03-31"))
    assert _dates(snaps[0])[0] == "2026-02-28", "add_months clamps to the end of the month"
    assert len(snaps[0].partition_specs) == 31


def test_a_calendar_period_ends_where_the_CURRENT_period_begins() -> None:
    layout = _layout(_event_mapping())
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(_request(_requirement(layout),
                           pit=_pit(length=1, unit="month", basis="calendar_period")),),
        business_dt=BUSINESS_DT))
    assert _dates(snaps[0])[0] == "2026-06-01" and _dates(snaps[0])[-1] == "2026-06-30"


def test_a_window_zone_that_differs_from_the_PARTITION_zone_widens_by_a_day_each_side() -> None:
    """An event at 02:00 in the window's zone is the PREVIOUS day in a UTC-partitioned table, so
    the naive date range drops it. The widening changes what is READ, never what is COUNTED."""
    layout = _layout(_event_mapping(timezone="UTC"))
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(_request(_requirement(layout), pit=_pit(length=30, timezone=ZONE)),),
        business_dt=BUSINESS_DT))
    assert len(snaps[0].partition_specs) == 32
    assert _dates(snaps[0])[0] == "2026-06-26" and _dates(snaps[0])[-1] == "2026-07-27"


def test_a_malformed_business_date_is_refused_before_anything_resolves() -> None:
    """A.13 left the date FORMAT to this task. `2026-7-27` parses as a different day in some
    readers and as nothing in others; a partition value built from it selects an empty partition."""
    layout = _layout(_event_mapping())
    for value in ("2026-7-27", "27-07-2026", "", "   ", "2026-07-27T00:00:00"):
        with pytest.raises(ValueError):
            resolve_snapshots(_inventory(layout), _Metastore(),
                              requests=(_request(_requirement(layout)),), business_dt=value)


# ══ de-duplication AFTER comparing semantics ═════════════════════════════════════════════════════


def test_identical_reads_share_ONE_snapshot_id() -> None:
    layout = _layout(_event_mapping())
    requirement = _requirement(layout)
    requests = (_request(requirement, feature="a", pit=_pit(length=30)),
                _request(requirement, feature="b", pit=_pit(length=30)))
    snaps = _ok(resolve_snapshots(_inventory(layout), _Metastore(), requests=requests,
                                  business_dt=BUSINESS_DT))
    assert len({s.snapshot_id() for s in snaps}) == 1
    assert input_snapshot_ids(snaps) == (snaps[0].snapshot_id(),)


def test_reads_that_merely_LOOK_alike_are_never_collapsed() -> None:
    """Same catalog, same schema, same table, different window: two reads, two ids.

    This is the mutant the plan warns about — collapsing on `(schema, table)` is how one expression
    silently inherits another's window.
    """
    layout = _layout(_event_mapping())
    requirement = _requirement(layout)
    requests = (_request(requirement, feature="a", pit=_pit(length=30)),
                _request(requirement, feature="b", pit=_pit(length=90)))
    snaps = _ok(resolve_snapshots(_inventory(layout), _Metastore(), requests=requests,
                                  business_dt=BUSINESS_DT))
    assert len({s.snapshot_id() for s in snaps}) == 2
    assert len(input_snapshot_ids(snaps)) == 2


def test_a_snapshot_id_covers_the_READ_and_not_who_asked_for_it() -> None:
    """Keyed by `(feature_name, expr_path, requirement_id)`; identified by what it READS."""
    layout = _layout(_event_mapping())
    requirement = _requirement(layout)
    snaps = _ok(resolve_snapshots(
        _inventory(layout), _Metastore(),
        requests=(_request(requirement, feature="a", expr_path="body.numerator"),
                  _request(requirement, feature="b", expr_path="body.expr")),
        business_dt=BUSINESS_DT))
    assert {s.key() for s in snaps} == {
        ("a", "body.numerator", requirement.requirement_id()),
        ("b", "body.expr", requirement.requirement_id())}
    assert snaps[0].snapshot_id() == snaps[1].snapshot_id()


def test_the_snapshot_id_ORDER_is_first_seen_and_not_sorted() -> None:
    """§3.4 calls the ids *the exact ordered partition set read*, and `sandbox_execution_hash` keeps
    the caller's order — so the de-duplication must not quietly re-order them."""
    layout = _layout(_event_mapping())
    requirement = _requirement(layout)
    requests = (_request(requirement, feature="b", pit=_pit(length=90)),
                _request(requirement, feature="a", pit=_pit(length=30)),
                _request(requirement, feature="c", pit=_pit(length=90)))
    snaps = _ok(resolve_snapshots(_inventory(layout), _Metastore(), requests=requests,
                                  business_dt=BUSINESS_DT))
    assert input_snapshot_ids(snaps) == (snaps[0].snapshot_id(), snaps[1].snapshot_id())


def test_two_requests_with_the_SAME_key_are_refused() -> None:
    """One expression reads one table once. Two rows under one key would silently drop one."""
    layout = _layout(_event_mapping())
    requirement = _requirement(layout)
    with pytest.raises(ValueError):
        resolve_snapshots(_inventory(layout), _Metastore(),
                          requests=(_request(requirement), _request(requirement)),
                          business_dt=BUSINESS_DT)


# ══ HASH RULE 1 — snapshots do NOT move `ir_hash` ════════════════════════════════════════════════


def test_snapshots_do_NOT_change_the_ir_hash(catalog, db) -> None:  # noqa: F811
    """The generated project must be identical across business dates (§3.3)."""
    ir = _compiled(_compile(db, SUM_30D))
    before = ir_hash(ir)
    requests = run_input_requests((ir,))
    resolved = []
    for business_dt in (BUSINESS_DT, NEXT_DT, "2027-01-01"):
        resolved.append(_ok(resolve_snapshots(INVENTORY, _Metastore(), requests=requests,
                                              business_dt=business_dt)))
    assert ir_hash(ir) == before
    assert len({tuple(s.snapshot_id() for s in snaps) for snaps in resolved}) == 3, \
        "the positive control: the three dates really did resolve three different reads"


def test_a_snapshot_carries_no_field_the_IR_could_have_carried(catalog, db) -> None:  # noqa: F811
    """The separation is structural: nothing a snapshot knows can travel back into an identity.

    `PhysicalInputSnapshot` holds the requirement it was resolved FROM, and that requirement's
    payload is what `ir_hash` already covers — so the only fields the snapshot adds are the run's,
    and no KEY of a compiled payload names one of them.

    Searching the payload's BYTES for the business date would be the stronger-looking assertion and
    it is wrong: a spine declaration legitimately carries a DECLARED date (a static snapshot ref),
    and a declared constant is part of what the artifact IS. What may not appear is a value RESOLVED
    from a run's date, which is what the key check states.
    """
    ir = _compiled(_compile(db, SUM_30D))
    names = {field.name for field in dataclasses.fields(PhysicalInputSnapshot)}
    assert names == {"feature_name", "expr_path", "requirement", "partition_specs",
                     "resolved_at_business_dt"}
    run_side = {"partition_specs", "resolved_at_business_dt", "snapshot_id", "input_snapshots",
                "business_dt"}

    def _keys(node) -> set[str]:
        if isinstance(node, dict):
            return set(node) | {k for value in node.values() for k in _keys(value)}
        if isinstance(node, list | tuple):
            return {k for value in node for k in _keys(value)}
        return set()

    assert _keys(ir.identity_payload()) & run_side == set()


# ══ HASH RULE 2 — the execution hash DOES move with `business_dt` ════════════════════════════════

FILES = {
    "pyproject.toml": '[project]\nname = "generated_cif_daily"\n',
    "src/generated_cif_daily/settings.py": "HOOKS = ()\n",
}


def _rendered():
    return seal_project(CompilationIdentity(
        formula_content_hashes=("formula-sum",), ir_hashes=("ir-sum",),
        materialization_contract_hash="contract-0001",
        group_plan_hash="plan-0001"), FILES).identity


def _cross_rendered():
    return seal_project(CompilationIdentity(
        formula_content_hashes=("formula-sum",),
        ir_hashes=("ir-sum",),
        materialization_contract_hash="contract-0001",
        group_plan_hash="plan-0001",
        bridge_realization_dependencies=(("brr-0001", "brds-0001"),),
    ), FILES).identity


def _prepared(*, business_dt=BUSINESS_DT, generation_id="gen-0001", run_id="run-0001",
              layout=None, staging_base="hdfs://nn/staging", requests=None, rendered=None, **kwargs):
    layout = _layout(_event_mapping()) if layout is None else layout
    return prepare_run(
        _rendered() if rendered is None else rendered, _inventory(layout), _Metastore(),
        generation_id=generation_id, run_id=run_id, business_dt=business_dt,
        requests=(_request(_requirement(layout), pit=_pit(length=30)),) if requests is None
        else requests,
        staging_base=staging_base, capability_attestation_id="att-0001", **kwargs)


def test_cross_catalog_run_requires_exact_final_bridge_authorization() -> None:
    rendered = _cross_rendered()
    refused = _prepared(rendered=rendered)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN

    mismatched = BridgeExecutionAuthorization(
        ir_hashes=("ir-sum",),
        environment_id="hdfc-local",
        realization_dependencies=(("brr-other", "brds-0001"),),
    )
    refused = _prepared(rendered=rendered, bridge_authorization=mismatched)
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.JOIN_CARDINALITY_UNKNOWN

    exact = BridgeExecutionAuthorization(
        ir_hashes=("ir-sum",),
        environment_id="hdfc-local",
        realization_dependencies=(("brr-0001", "brds-0001"),),
    )
    assert isinstance(
        _prepared(rendered=rendered, bridge_authorization=exact),
        runprep.RunPreparation,
    )


def test_additional_rendered_parameters_enter_the_execution_hash() -> None:
    required = (*REQUIRED_RUN_PARAMETERS, "bridge_predicate_values")
    first = _ok(_prepared(
        additional_parameters={"bridge_predicate_values": {"tenant_id": "A"}},
        required_parameters=required,
    ))
    second = _ok(_prepared(
        additional_parameters={"bridge_predicate_values": {"tenant_id": "B"}},
        required_parameters=required,
    ))
    assert first.sandbox_execution_hash != second.sandbox_execution_hash


def test_execution_hash_DOES_change_with_business_dt() -> None:
    first, second = _ok(_prepared()), _ok(_prepared(business_dt=NEXT_DT))
    assert first.sandbox_execution_hash != second.sandbox_execution_hash


def test_the_execution_hash_moves_with_the_DATE_and_NOTHING_ELSE_CHANGING() -> None:
    """The sharp half, and the one that caught a hole: EVERYTHING else is held equal.

    A prepared run over two dates differs in several covered values at once — the parameters, the
    snapshot ids, the partitions — so it cannot show that the date itself is inside the identity.
    Removing `business_dt` from the hashed payload passes that test and fails this one. Two runs
    that shared an execution identity would be indistinguishable to §9's staging manifests, which
    bind an output to a generation, a run AND a date.
    """
    rendered = _rendered()
    held = dict(environment_id="hdfc-local", parameters={"generation_id": "gen-0001"},
                input_snapshot_ids=("snap-0001",), capability_attestation_id="att-0001")
    assert identity.sandbox_execution_hash(rendered, business_dt=BUSINESS_DT, **held) != \
        identity.sandbox_execution_hash(rendered, business_dt=NEXT_DT, **held)


def test_an_UNPARTITIONED_read_is_still_two_runs_on_two_dates() -> None:
    """§3.4 records an unpartitioned mutable table honestly: the snapshot is NOT content-addressed,
    so the same `None` partition set on two dates is two different reads of two different tables'
    worth of rows. The business date is therefore part of the snapshot's own id, and a run over it
    is a distinct execution."""
    layout = _layout(VerifiedUnpartitioned(), partition_columns=None)
    first = _ok(_prepared(layout=layout))
    second = _ok(_prepared(layout=layout, business_dt=NEXT_DT))
    assert first.snapshots[0].partition_specs is second.snapshots[0].partition_specs is None
    assert first.snapshots[0].snapshot_id() != second.snapshots[0].snapshot_id()
    assert first.sandbox_execution_hash != second.sandbox_execution_hash


def test_the_execution_hash_is_reproducible_for_one_prepared_run() -> None:
    assert _ok(_prepared()).sandbox_execution_hash == _ok(_prepared()).sandbox_execution_hash


def test_business_dt_whitespace_does_not_fork_execution_identity() -> None:
    """One day, one identity: `_business_date` canonicalizes the date everywhere ELSE (the covered
    parameters, every snapshot id), so the raw caller spelling reaching the hash would make two
    spellings of one run two execution identities while every covered value agrees they are one."""
    a = _ok(_prepared(business_dt=BUSINESS_DT))
    b = _ok(_prepared(business_dt=f" {BUSINESS_DT} "))
    assert a.parameters["sandbox_execution_hash"] == b.parameters["sandbox_execution_hash"]


def test_a_different_GENERATION_or_RUN_moves_the_execution_hash() -> None:
    baseline = _ok(_prepared()).sandbox_execution_hash
    assert _ok(_prepared(generation_id="gen-0002")).sandbox_execution_hash != baseline
    assert _ok(_prepared(run_id="run-0002")).sandbox_execution_hash != baseline


def test_the_execution_hash_covers_the_parameters_WITHOUT_covering_itself() -> None:
    """The same non-circularity `generated_project_hash` needs: the hash is over the prepared
    parameters, and `sandbox_execution_hash` is one of the parameters execution must be given."""
    prep = _ok(_prepared())
    assert prep.parameters["sandbox_execution_hash"] == prep.sandbox_execution_hash
    assert prep.covered_parameters().keys() == prep.parameters.keys() - {"sandbox_execution_hash"}
    assert identity.sandbox_execution_hash(
        _rendered(), environment_id="hdfc-local", parameters=prep.covered_parameters(),
        business_dt=BUSINESS_DT, input_snapshot_ids=input_snapshot_ids(prep.snapshots),
        capability_attestation_id="att-0001") == prep.sandbox_execution_hash


def test_the_toolchain_versions_reach_the_prepared_run_through_their_modules() -> None:
    baseline = _ok(_prepared()).sandbox_execution_hash
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(compile, "COMPILER_VERSION", "compiler-9.9.9")
        assert _ok(_prepared()).sandbox_execution_hash != baseline
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(render, "RENDERER_VERSION", "renderer-9.9.9")
        assert _ok(_prepared()).sandbox_execution_hash != baseline


# ══ §11.1 — prepared parameters are what execution reads ═════════════════════════════════════════


def test_the_prepared_parameters_are_EXACTLY_what_the_rendered_hook_requires() -> None:
    """The rendered `RunParametersHook` refuses a missing one AND an unexpected one, so equality is
    the only shape that runs at all."""
    prep = _ok(_prepared())
    assert set(prep.parameters) == set(REQUIRED_RUN_PARAMETERS)


def test_the_staging_root_derives_from_the_generation_id() -> None:
    """A.15. A reused staging path can leave an older SUCCESSFUL manifest whose `ir_hash` still
    matches, and assembly would publish stale output while passing every other check (§9)."""
    first, second = _ok(_prepared()), _ok(_prepared(generation_id="gen-0002"))
    assert first.parameters["staging_root"] != second.parameters["staging_root"]
    assert first.parameters["staging_root"].endswith("/gen-0001")
    assert staging_root_for("hdfs://nn/staging/", generation_id="gen-0001") == \
        "hdfs://nn/staging/gen-0001"


def test_a_staging_root_that_does_not_name_its_generation_is_impossible() -> None:
    params = inspect.signature(staging_root_for).parameters
    assert list(params) == ["staging_base", "generation_id"]
    with pytest.raises(ValueError):
        staging_root_for("hdfs://nn/staging", generation_id="  ")


def test_the_input_snapshots_parameter_names_every_resolved_read() -> None:
    """The shape Task 13a declined to invent (A.16). It is the run's ordered read set, per
    expression, and it is what an execution proves it read exactly."""
    layout = _layout(_event_mapping())
    requirement = _requirement(layout)
    prep = _ok(_prepared(layout=layout, requests=(
        _request(requirement, feature="a", pit=_pit(length=1)),
        _request(requirement, feature="b", pit=_pit(length=2)))))
    entries = prep.parameters["input_snapshots"]
    assert [(e["feature_name"], e["expr_path"]) for e in entries] == \
        [("a", "body.expr"), ("b", "body.expr")]
    assert entries[0]["partition_specs"] == [[["load_dt", "2026-07-26"]]]
    assert entries[0]["snapshot_id"] == prep.snapshots[0].snapshot_id()
    assert entries[0]["schema"] == "banking" and entries[0]["table"] == "transactions"


def test_every_prepared_parameter_is_canonicalizable() -> None:
    """They go through RFC 8785 into the execution hash, and a value that cannot be canonicalized
    would surface as a `CanonicalizationError` at the moment the run is identified."""
    prep = _ok(_prepared())
    assert materialize_hash(dict(prep.covered_parameters()))


def test_the_prepared_parameters_are_read_only() -> None:
    prep = _ok(_prepared())
    with pytest.raises(TypeError):
        prep.parameters["business_dt"] = NEXT_DT  # type: ignore[index]


# ══ codegen-review Task 9 — a DATE-typed clock outside UTC refuses at run preparation ════════════
#
# The emitted window comparison casts the boundary DATE to midnight and `to_utc_timestamp` re-reads
# it as the window zone's wall clock, so west of UTC the whole window shifts by a day (report §2.3,
# measured with America/New_York: the first day drops and an extra trailing day is admitted; east
# of UTC is correct by luck). The IR carries no physical type for the clock, but the inventory's
# `TableLayout.columns` does — so run preparation is where the refusal can live, fail-closed, until
# `PitSpec` carries the clock dtype (DEFERRED-WORK A.29).

_DATE_CLOCK_COLUMNS = (("txn_amt", "decimal(18,2)"), ("txn_dt", "date"))


def test_a_DATE_typed_clock_with_a_non_utc_zone_is_refused() -> None:
    layout = _layout(_event_mapping(), columns=_DATE_CLOCK_COLUMNS)
    refused = _prepared(layout=layout, requests=(
        _request(_requirement(layout), pit=_pit(length=30, timezone="America/New_York")),))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED
    assert "txn_dt" in refused.detail, "the refusal names the COLUMN"
    assert "America/New_York" in refused.detail, "the refusal names the ZONE"
    assert "to_utc_timestamp" in refused.detail, "the refusal names the MECHANISM"


def test_a_DATE_typed_clock_under_utc_still_prepares() -> None:
    """The shift is zero under UTC — refusing there would refuse every date-columned daily table."""
    layout = _layout(_event_mapping(), columns=_DATE_CLOCK_COLUMNS)
    prep = _prepared(layout=layout, requests=(
        _request(_requirement(layout), pit=_pit(length=30, timezone="UTC")),))
    assert isinstance(prep, runprep.RunPreparation)


def test_a_TIMESTAMP_typed_clock_in_a_non_utc_zone_still_prepares() -> None:
    """The no-over-refusal control: an instant-typed clock is what the emitted comparison is
    correct for, in every zone."""
    layout = _layout(_event_mapping(),
                     columns=(("txn_amt", "decimal(18,2)"), ("txn_dt", "timestamp")))
    prep = _prepared(layout=layout, requests=(
        _request(_requirement(layout), pit=_pit(length=30, timezone="America/New_York")),))
    assert isinstance(prep, runprep.RunPreparation)


def test_the_availability_ref_is_checked_too() -> None:
    """Rule 1's gate compares under the same zone arithmetic as rule 2's window, so a DATE-typed
    availability column shifts the cutoff the same way a DATE-typed clock shifts the window."""
    layout = _layout(_event_mapping(),
                     columns=(("txn_amt", "decimal(18,2)"), ("txn_dt", "timestamp"),
                              ("posted_dt", "date")))
    pit = dataclasses.replace(
        _pit(length=30, timezone="America/New_York"),
        availability_ref=f"{SOURCE}::public.transactions.posted_dt")
    refused = _prepared(layout=layout, requests=(_request(_requirement(layout), pit=pit),))
    assert isinstance(refused, MaterializationRefused)
    assert refused.code is CompilationRefusalCode.PHYSICAL_TYPE_UNSUPPORTED
    assert "posted_dt" in refused.detail


# ══ the inventory is CONSULTED, and a drifted one fails closed ═══════════════════════════════════


def test_a_table_the_RUN_TIME_inventory_no_longer_declares_refuses() -> None:
    layout = _layout(_event_mapping())
    refusal = resolve_snapshots(_inventory(_layout(_event_mapping(), table="other")), _Metastore(),
                                requests=(_request(_requirement(layout)),),
                                business_dt=BUSINESS_DT)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN


def test_an_inventory_whose_LAYOUT_MOVED_since_compilation_refuses() -> None:
    """The artifact was compiled against a layout; the environment now declares another one. Which
    of the two describes the table this run will read is exactly what is unknown."""
    compiled = _layout(_event_mapping())
    moved = _layout(_event_mapping(column="arrival_dt"),
                    partition_columns=(("arrival_dt", "string"),))
    refusal = resolve_snapshots(_inventory(moved), _Metastore(),
                                requests=(_request(_requirement(compiled)),),
                                business_dt=BUSINESS_DT)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.PARTITION_IDENTITY_UNKNOWN


def test_a_requirement_with_partition_columns_and_NO_mapping_refuses() -> None:
    layout = _layout(None)
    refusal = resolve_snapshots(_inventory(layout), _Metastore(),
                                requests=(_request(_requirement(layout)),),
                                business_dt=BUSINESS_DT)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.PARTITION_MAPPING_NOT_DECLARED


# ══ control-plane hygiene ════════════════════════════════════════════════════════════════════════


def test_run_preparation_reads_METADATA_only() -> None:
    """The control plane never reads feature data. The seam offers exactly one method, and it
    answers with partition VALUES from the metastore — never a row."""
    methods = [name for name in dir(runprep.MetastorePartitions) if not name.startswith("_")]
    assert methods == ["list_partitions"]


def test_the_module_imports_no_pyspark() -> None:
    source = inspect.getsource(runprep)
    assert "pyspark" not in source


def test_a_resolution_is_a_pure_function_of_its_inputs() -> None:
    """The must-survive control's twin: resolving twice must answer identically, or nothing above
    is measuring what it claims to."""
    layout = _layout(_event_mapping())
    args = dict(requests=(_request(_requirement(layout)),), business_dt=BUSINESS_DT)
    first = _ok(resolve_snapshots(_inventory(layout), _Metastore(), **args))
    second = _ok(resolve_snapshots(_inventory(layout), _Metastore(), **args))
    assert first == second


def test_the_business_date_is_recorded_on_every_snapshot() -> None:
    layout = _layout(_event_mapping())
    snaps = _ok(resolve_snapshots(_inventory(layout), _Metastore(),
                                  requests=(_request(_requirement(layout)),),
                                  business_dt=BUSINESS_DT))
    assert all(s.resolved_at_business_dt == BUSINESS_DT for s in snaps)
    assert dt.date.fromisoformat(snaps[0].resolved_at_business_dt) == dt.date(2026, 7, 27)


# ── Task 15b: the SPINE's request, which 15a could not build ─────────────────────────────────────
#
# §11.2's L1 covers *every feature IR, every expression, and the spine*. A spine's population is
# selected by a `SnapshotPolicy` rather than by a `PitSpec`, so `run_input_requests` — which walks
# expressions — cannot produce one. This is the request that closes that gap, and building it is
# what makes §4.2's `CurrentSnapshot` vintage condition reachable for the first time.

from featuregen.materialize.runprep import (  # noqa: E402 - the 15b section's own imports
    SPINE_EXPR_PATH,
    SPINE_FEATURE_NAME,
    spine_input_request,
)
from featuregen.materialize.spine import (  # noqa: E402
    ActivePopulation,
    ClaimAuthority,
    CurrentSnapshot,
    LatestAvailableAsOf,
    PartitionMappedSnapshot,
    SpineSpec,
)


def _spine(policy):
    """A `SpineSpec` carrying one snapshot policy — every other field is beside the point here."""
    from tests.featuregen.materialize.test_ir import DECLARATION

    declaration = dataclasses.replace(DECLARATION, snapshot_policy=policy)
    return SpineSpec(
        declaration=declaration, source_table_ref=declaration.source_table_ref,
        entity=declaration.entity, ordered_key_refs=declaration.ordered_key_refs,
        population_semantics=declaration.population_semantics,
        snapshot_policy=declaration.snapshot_policy,
        availability_ref=declaration.availability_ref,
        read_set=declaration.ordered_key_refs, governed_grain_refs=declaration.ordered_key_refs,
        roles_used=("feature_engineer",),
        completeness_claim_authority=ClaimAuthority.HUMAN_DECLARATION)


_CUSTOMERS = _layout(VerifiedUnpartitioned(), partition_columns=None, table="customers")
_CUSTOMERS_MAPPED = _layout(_event_mapping(column="snapshot_dt"),
                            partition_columns=(("snapshot_dt", "string"),), table="customers")


def test_the_spine_request_carries_NO_pit_spec() -> None:
    """A `SnapshotPolicy` is not a `PitSpec`, and a spine that borrowed one would read a window."""
    request = spine_input_request(
        _spine(PartitionMappedSnapshot(ordered_partition_refs=(f"{SOURCE}::public.customers.snapshot_dt",))),
        _requirement(_CUSTOMERS_MAPPED), business_dt=BUSINESS_DT)
    assert isinstance(request, RunInputRequest)
    assert request.pit_spec is None
    assert (request.feature_name, request.expr_path) == (SPINE_FEATURE_NAME, SPINE_EXPR_PATH)


def test_a_PARTITION_MAPPED_spine_resolves_the_business_dates_OWN_partition() -> None:
    request = spine_input_request(
        _spine(PartitionMappedSnapshot(ordered_partition_refs=(f"{SOURCE}::public.customers.snapshot_dt",))),
        _requirement(_CUSTOMERS_MAPPED), business_dt=BUSINESS_DT)
    snaps = _ok(resolve_snapshots(_inventory(_CUSTOMERS_MAPPED), _Metastore(),
                                  requests=(request,), business_dt=BUSINESS_DT))
    assert [spec.columns for spec in snaps[0].partition_specs] == \
        [(("snapshot_dt", BUSINESS_DT),)]


def test_a_CURRENT_SNAPSHOT_whose_vintage_IS_the_business_date_resolves() -> None:
    request = spine_input_request(_spine(CurrentSnapshot(observed_snapshot_ref=BUSINESS_DT)),
                                  _requirement(_CUSTOMERS), business_dt=BUSINESS_DT)
    assert isinstance(request, RunInputRequest)


def test_a_CURRENT_SNAPSHOT_asked_for_ANOTHER_date_refuses_rather_than_pretending() -> None:
    """§4.2, reachable for the first time: a present-day table cannot answer another date.

    Everything is held equal to the passing case above except the run's date — the declaration, the
    requirement and the policy are the same objects.
    """
    refusal = spine_input_request(_spine(CurrentSnapshot(observed_snapshot_ref=BUSINESS_DT)),
                                  _requirement(_CUSTOMERS), business_dt=NEXT_DT)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS
    assert BUSINESS_DT in refusal.detail and NEXT_DT in refusal.detail


def test_a_CURRENT_SNAPSHOT_vintage_that_is_not_a_DATE_cannot_be_compared_and_refuses() -> None:
    """An opaque version identifier and a calendar date have no ordering between them.

    Fail closed: "this module cannot check the vintage" is not "the vintage is fine".
    """
    refusal = spine_input_request(_spine(CurrentSnapshot(observed_snapshot_ref="v42")),
                                  _requirement(_CUSTOMERS), business_dt=BUSINESS_DT)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_an_SCD_spine_over_a_TIME_MAPPED_table_refuses_instead_of_reading_one_day() -> None:
    """`LATEST_AVAILABLE_AS_OF` needs history; `pit_spec=None` over a time mapping selects one day.

    Resolving it would give the spine a single day of effective-dated rows and a population missing
    everyone whose record did not change today — a smaller number with no error.
    """
    policy = LatestAvailableAsOf(
        effective_time_ref=f"{SOURCE}::public.customers.effective_dt",
        availability_ref=f"{SOURCE}::public.customers.load_ts",
        deterministic_tie_break_refs=(f"{SOURCE}::public.customers.cif_id",))
    refusal = spine_input_request(_spine(policy), _requirement(_CUSTOMERS_MAPPED),
                                  business_dt=BUSINESS_DT)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS


def test_an_SCD_spine_over_an_UNPARTITIONED_table_is_fine() -> None:
    """The control for the refusal above: only a TIME mapping makes `pit_spec=None` wrong."""
    policy = LatestAvailableAsOf(
        effective_time_ref=f"{SOURCE}::public.customers.effective_dt",
        availability_ref=f"{SOURCE}::public.customers.load_ts",
        deterministic_tie_break_refs=(f"{SOURCE}::public.customers.cif_id",))
    assert isinstance(spine_input_request(_spine(policy), _requirement(_CUSTOMERS),
                                          business_dt=BUSINESS_DT), RunInputRequest)


def _active_population(observed_on: str) -> ActivePopulation:
    """The vintage is stated by EVERY caller: which date the gate answers is the point under test."""
    return ActivePopulation(status_ref=f"{SOURCE}::public.customers.status_cd",
                            allowed_status_values=("ACTIVE",), observed_on=observed_on)


def test_an_ACTIVE_POPULATION_spine_is_refused_for_a_date_its_vintage_cannot_answer() -> None:
    """`status_cd` is CURRENT-valued: the table can only answer "who is active NOW", and the
    declared `observed_on` is the one date on record for when "now" was. Any other business date
    is the population-level as-of leak the review confirmed by execution (report §2.1): a January
    backfill run in July silently publishing July's actives. Rule 6's availability filter cannot
    close it — an entity CLOSED since the business date has no row left to filter."""
    policy = _active_population(observed_on=BUSINESS_DT)
    for not_the_vintage in (NEXT_DT, "2026-01-15"):  # the next day, and the review's backfill
        refused = spine_input_request(_spine(policy), _requirement(_CUSTOMERS),
                                      business_dt=not_the_vintage)
        assert isinstance(refused, MaterializationRefused), not_the_vintage
        assert refused.code is CompilationRefusalCode.SPINE_DECLARATION_REJECTED_BY_FACTS
        assert "ACTIVE_POPULATION" in refused.detail  # the refusal names the policy
        assert not_the_vintage in refused.detail and BUSINESS_DT in refused.detail


def test_an_ACTIVE_POPULATION_spine_resolves_for_its_own_DECLARED_vintage() -> None:
    """The gate reads the policy's DECLARED `observed_on` — nothing else. `NEXT_DT` is chosen
    because it differs from every other date the fixture declaration carries (its `recorded_at`
    sits on `BUSINESS_DT`'s calendar day), so a pass here cannot come from a coincidence."""
    request = spine_input_request(
        _spine(_active_population(observed_on=NEXT_DT)), _requirement(_CUSTOMERS),
        business_dt=NEXT_DT)
    assert isinstance(request, RunInputRequest)


def test_the_spine_request_key_cannot_COLLIDE_with_a_feature_expression() -> None:
    """`run_input_requests` raises on a duplicate key, so the spine's must not look like a feature."""
    assert SPINE_FEATURE_NAME.startswith("__") and SPINE_FEATURE_NAME.endswith("__")
