"""Spec A Task 13a — the rendered SPINE node: §4.2's four policies, and the PIT rules with teeth.

**Text assertions are the weak half of this file.** A renderer that resolves every tie with
``row_number()`` and one that refuses with ``SPINE_NON_DETERMINISTIC`` differ by one identifier, and
a test that greps for that identifier is satisfied by a comment. So the rules that matter are
asserted by RUNNING the rendered source against ``fake_spark`` — a stand-in that implements real
``RANK`` semantics — and reading the rows that come out. Two mutants each of those tests must kill:
an implementation that drops the availability half of rule 1, and one that resolves ties by taking
the first row.

**Running against a stand-in is not running against Spark.** ``pyspark`` is not installed here and
no JVM is available; proving the generated project actually executes is L0's job (§11.2, Task 15),
and nothing in this file may be read as that proof. What the stand-in does establish is that the
rendered LOGIC selects the rows §4.2 says it must — which is the property a parse check and a string
match both miss entirely.

Fixtures are the real compilation's (``test_ir`` → ``test_render_project``): a spine rendered from a
declaration nobody compiles is a spine of nothing. The three policies the worked fixture does not
declare are substituted onto that real spine with ``dataclasses.replace``, so everything except the
policy stays exactly what was compiled.
"""
from __future__ import annotations

import ast
import dataclasses
import datetime as dt
import decimal
import inspect
import json
import pathlib

import pytest
import yaml
from tests.featuregen.materialize import fake_spark, fixtures
from tests.featuregen.materialize.test_group_plan import (  # noqa: F401 — `catalog` is a fixture
    BUSINESS_DT,
    CADENCE,
    GENERATION,
    SUM_30D,
    catalog,
)
from tests.featuregen.materialize.test_ir import (
    CUSTOMERS,
    CUSTOMERS_ASOF,
    CUSTOMERS_STATUS,
)
from tests.featuregen.materialize.test_render_project import ENVIRONMENT, _compiled

from featuregen.formula.schema import (
    AggregateFunction,
    EmptyWindowResult,
    FinalOperation,
    NullInput,
    OverflowBehavior,
    RoundingMode,
    ZeroDenominator,
)
from featuregen.materialize.codes import ValidationGateCode
from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    ContractGroup,
    derive_group_contract,
)
from featuregen.materialize.expression_ir import BODY_PATHS, RefRole
from featuregen.materialize.group_plan import (
    StagingManifestV1,
    StagingStatus,
    check_completeness,
)
from featuregen.materialize.inventory import (
    EventTimePartition,
    FullScan,
    PartitionTransform,
    StaticSnapshot,
)
from featuregen.materialize.ir import ir_hash
from featuregen.materialize.render import nodes_compute
from featuregen.materialize.render.nodes_compute import SPINE_FUNC_NAME, render_spine_node
from featuregen.materialize.render.project import (
    RenderedNode,
    feature_staging_path,
    materialize_to,
    project_datasets,
    render_project,
)
from featuregen.materialize.spine import (
    ActivePopulation,
    CurrentSnapshot,
    LatestAvailableAsOf,
    PartitionMappedSnapshot,
    PopulationSemantics,
)
from featuregen.overlay.upload.join_path import JoinStep

GOLDENS = pathlib.Path(__file__).parent / "goldens" / "spine_nodes"

#: The worked declaration's own vintage — `test_ir._declaration` observes the customer file here.
OBSERVED = "2026-07-27"

#: The cadence is `00:00:00` in `Asia/Kolkata`, so the cutoff for 2026-07-27 is **2026-07-26
#: 18:30 UTC**. Every timestamp below is UTC, and the 90 minutes between 18:30 and 20:00 are what
#: separates the governed zone from the session's.
CUTOFF_UTC = dt.datetime(2026, 7, 26, 18, 30)
BEFORE = dt.datetime(2026, 7, 26, 12, 0)
ALSO_BEFORE = dt.datetime(2026, 7, 25, 12, 0)
AFTER_IN_UTC_ONLY = dt.datetime(2026, 7, 26, 20, 0)   # after the IST cutoff, before UTC midnight
AFTER = dt.datetime(2026, 7, 28, 12, 0)


# ── the real compilation, and the three policies it does not declare ─────────────────────────────


@pytest.fixture
def compiled(catalog, db):  # noqa: F811 — `catalog` seeds the governed facts `db` is read through
    """The real spine, plan, contract and physical requirement for the worked group."""
    authorized, plan, spine_input = _compiled(db, SUM_30D)
    group = derive_group_contract(db, authorized, cadence=CADENCE,
                                  availability_promise=AvailabilityPromiseV1(calendar_days=1))
    assert isinstance(group, ContractGroup), group
    return authorized, plan, group.contract, spine_input


def _with_policy(spine, policy):
    """The compiled spine, re-declared under another §4.2 policy and nothing else."""
    return dataclasses.replace(
        spine, snapshot_policy=policy,
        declaration=dataclasses.replace(spine.declaration, snapshot_policy=policy))


LATEST = LatestAvailableAsOf(
    effective_time_ref=f"{CUSTOMERS}.effective_from", availability_ref=CUSTOMERS_ASOF,
    deterministic_tie_break_refs=(f"{CUSTOMERS}.version_seq",))
UNTIEBREAKABLE = LatestAvailableAsOf(
    effective_time_ref=f"{CUSTOMERS}.effective_from", availability_ref=CUSTOMERS_ASOF)
ACTIVE = ActivePopulation(status_ref=CUSTOMERS_STATUS, allowed_status_values=("ACTIVE", "DORMANT"))
PARTITIONED = PartitionMappedSnapshot(ordered_partition_refs=(f"{CUSTOMERS}.snapshot_dt",))
SNAPSHOT = CurrentSnapshot(observed_snapshot_ref=OBSERVED)

EVENT_TIME_PARTITION = EventTimePartition(
    time_ref=CUSTOMERS_ASOF, partition_column="snapshot_dt",
    transform=PartitionTransform.DATE_ISO, timezone=CADENCE.timezone)


def _render(compiled, policy, *, spine_input=None, **overrides):
    spine, plan, contract, resolved = compiled[0].spine, compiled[1], compiled[2], compiled[3]
    return render_spine_node(
        _with_policy(spine, policy), plan, contract,
        spine_input=spine_input if spine_input is not None else resolved,
        source_dataset="raw_public__customers", spine_dataset="primary_spine", **overrides)


def _partition_input(compiled, mapping=EVENT_TIME_PARTITION):
    return dataclasses.replace(
        compiled[3], partition_columns=(("snapshot_dt", "string"),), partition_mapping=mapping)


def _run(node: RenderedNode, rows, business_dt: str = BUSINESS_DT):
    """Execute a rendered spine against the stand-in, and return the rows it selected."""
    build = fake_spark.run_rendered(node.source, node.func_name)
    return build(fake_spark.DataFrame(rows), business_dt)


def _customer(cif, **overrides):
    row = {"cif_id": cif, "load_ts": BEFORE, "effective_from": BEFORE, "version_seq": 1,
           "status_cd": "ACTIVE", "snapshot_dt": BUSINESS_DT}
    row.update(overrides)
    return row


ALL_POLICIES = (SNAPSHOT, LATEST, ACTIVE, PARTITIONED)


def _render_all(compiled):
    return {
        policy.kind.value: _render(
            compiled, policy,
            spine_input=_partition_input(compiled) if policy is PARTITIONED else None)
        for policy in ALL_POLICIES}


# ── the node's shape and its wiring ──────────────────────────────────────────────────────────────


def test_the_node_wires_the_datasets_it_was_given_and_the_business_date(compiled):
    """Storage locations are catalog entries; the node carries NAMES, and the business date is a
    run parameter (§11.1) rather than anything rendered into the source."""
    node = _render(compiled, LATEST)
    assert node.inputs == ("raw_public__customers", "params:business_dt")
    assert node.outputs == ("primary_spine",)
    assert node.func_name == SPINE_FUNC_NAME
    assert "public" not in node.source and "customers" not in node.source


def test_every_policy_renders_a_node_whose_source_parses(compiled):
    """`RenderedNode` parses its own source, so construction succeeding IS the parse — and a file
    that parses is not a file that runs (that is L0's, §11.2)."""
    for kind, node in _render_all(compiled).items():
        assert ast.parse(node.source), kind


def test_rendering_is_byte_identical_for_the_same_input(compiled):
    """Determinism: `generated_project_hash` must identify WHAT was built, not the day it was."""
    first, second = _render_all(compiled), _render_all(compiled)
    assert {k: v.source for k, v in first.items()} == {k: v.source for k, v in second.items()}
    assert {k: v.imports for k, v in first.items()} == {k: v.imports for k, v in second.items()}


def test_each_policy_renders_ITS_OWN_selection(compiled):
    """Four policies, four different selections. A renderer that read the whole customer table under
    every policy — the exact defect §4.2 was written to close — renders one."""
    sources = {kind: node.source for kind, node in _render_all(compiled).items()}
    assert len(set(sources.values())) == 4, sources.keys()

    assert "rank()" in sources["latest_available_as_of"]
    assert "SPINE_NON_DETERMINISTIC" in sources["latest_available_as_of"]
    assert "isin(['ACTIVE', 'DORMANT'])" in sources["active_population"]
    assert "snapshot_dt" in sources["partition_mapped"]
    assert OBSERVED in sources["current_snapshot"]

    # And no policy's marker leaks into another's.
    for kind, source in sources.items():
        if kind != "latest_available_as_of":
            assert "rank()" not in source and "Window" not in source, kind
        if kind != "active_population":
            assert "isin" not in source, kind
        if kind != "partition_mapped":
            assert "snapshot_dt" not in source, kind


def test_no_policy_emits_a_row_collapsing_repair(compiled):
    """§4.2 rule 5: duplicate spine keys are a BLOCKING GATE, not a de-duplication step. A `distinct`
    that made them go away would turn a population the declaration got wrong into a smaller one that
    looks right, and fan-out is already refused upstream (`joins.py`) so there is nothing to repair."""
    for kind, node in _render_all(compiled).items():
        for banned in ("distinct", "dropDuplicates", "drop_duplicates", "row_number"):
            assert banned not in node.source, f"{kind} renders {banned}"


def test_the_renderer_never_imports_pyspark():
    """PySpark exists only INSIDE rendered text — the compiler runs wherever the compiler runs."""
    module = ast.parse(inspect.getsource(nodes_compute))
    imported = {alias.name.split(".")[0]
                for statement in ast.walk(module) if isinstance(statement, ast.Import)
                for alias in statement.names}
    imported |= {(statement.module or "").split(".")[0]
                 for statement in ast.walk(module) if isinstance(statement, ast.ImportFrom)}
    assert "pyspark" not in imported and "kedro" not in imported


def test_the_rendered_node_declares_the_imports_its_source_needs(compiled):
    """`RenderedNode` forbids imports inside `source` so the module can de-duplicate them, which
    means an import the node forgets to DECLARE is a NameError on the cluster."""
    latest = _render(compiled, LATEST)
    assert "from pyspark.sql import Window" in latest.imports
    assert "from pyspark.sql import functions as F" in latest.imports
    assert "from pyspark.sql import DataFrame" in latest.imports
    # Only the policy that ranks needs a Window, and a project that imported one it never uses
    # would be stating a dependency it does not have.
    assert "from pyspark.sql import Window" not in _render(compiled, ACTIVE).imports


def test_the_node_renders_into_a_project_whose_wiring_closes(compiled, catalog, db):  # noqa: F811
    """The end-to-end seam: Task 12 refuses a project whose wiring does not close, and the spine
    node must be the thing that writes the primary dataset."""
    authorized, plan, _contract, spine_input = compiled
    datasets = project_datasets(authorized, plan, spine_input=spine_input)
    spine_node = render_spine_node(
        authorized.spine, plan, _contract, spine_input=spine_input,
        source_dataset=datasets.raw["banking.customers"], spine_dataset=datasets.spine)
    assert spine_node.outputs == (datasets.spine,)

    from tests.featuregen.materialize.test_render_project import _nodes
    others = tuple(node for node in _nodes(datasets) if node.name != "spine")
    project = render_project(
        authorized, plan, environment_id=ENVIRONMENT, engine_versions=fixtures.ENGINE_VERSIONS,
        spine_input=spine_input, nodes=(spine_node, *others))
    rendered = next(text for path, text in project.files.items() if path.endswith("nodes.py"))
    assert f"def {SPINE_FUNC_NAME}(" in rendered


# ── §4.2 rule 1 — future versions are excluded, on BOTH halves ───────────────────────────────────


def test_rule_1_excludes_a_version_effective_AFTER_the_cutoff(compiled):
    """A customer record created after `business_dt` must not appear."""
    node = _render(compiled, LATEST)
    rows = _run(node, [
        _customer("c1", effective_from=BEFORE, version_seq=1),
        _customer("c1", effective_from=AFTER, version_seq=2)]).rows
    assert [row["cif_id"] for row in rows] == ["c1"]
    assert len(rows) == 1


def test_rule_1_excludes_a_version_that_had_not_yet_ARRIVED_at_the_cutoff(compiled):
    """The OTHER half of rule 1, and the one a single `row_number()` appears to satisfy.

    Both versions are effective before the cutoff, so an implementation that filters only on
    effective time picks the LATER one — and the later one had not arrived yet. The spine must
    return the earlier version, not the unavailable one.
    """
    node = _render(compiled, LATEST)
    rows = _run(node, [
        _customer("c1", effective_from=ALSO_BEFORE, load_ts=ALSO_BEFORE, version_seq=1),
        _customer("c1", effective_from=BEFORE, load_ts=AFTER, version_seq=2)]).rows
    assert len(rows) == 1
    # The row that survived is the one that had arrived: the unavailable version is gone entirely.
    assert rows[0]["cif_id"] == "c1"


def test_rule_1_drops_an_entity_whose_ONLY_version_had_not_arrived(compiled):
    """Sharper than the last: if the unavailable row were kept, the entity would still be present,
    so the population would silently include a customer nobody could have known about."""
    node = _render(compiled, LATEST)
    rows = _run(node, [_customer("c1", effective_from=BEFORE, load_ts=AFTER)]).rows
    assert rows == []


def test_the_cutoff_is_the_GOVERNED_zone_not_the_sessions(compiled):
    """The rendered session timezone is pinned to UTC, so the cadence's zone must be stated.

    The cadence is `00:00:00 Asia/Kolkata`, so 2026-07-27's cutoff is 2026-07-26 18:30 UTC. A row at
    2026-07-26 20:00 UTC is AFTER it — but before UTC midnight, so a renderer that inherited the
    session zone would include it and this test would fail.
    """
    node = _render(compiled, LATEST)
    assert CADENCE.timezone in node.source
    rows = _run(node, [_customer("c1", effective_from=AFTER_IN_UTC_ONLY,
                                 load_ts=AFTER_IN_UTC_ONLY)]).rows
    assert rows == []
    # ...and the same row one business date later IS inside the cutoff, so the exclusion above is
    # the zone doing its job rather than the filter refusing everything.
    later = _run(node, [_customer("c1", effective_from=AFTER_IN_UTC_ONLY,
                                  load_ts=AFTER_IN_UTC_ONLY)], business_dt="2026-07-28").rows
    assert [row["cif_id"] for row in later] == ["c1"]


# ── §4.2 rules 2 and 3 — one row per key, and ties that refuse ───────────────────────────────────


def test_rule_2_keeps_the_greatest_eligible_effective_time_per_key(compiled):
    """One row per key, and it is the LATEST eligible version — for every key independently."""
    node = _render(compiled, LATEST)
    rows = _run(node, [
        _customer("c1", effective_from=ALSO_BEFORE, version_seq=1),
        _customer("c1", effective_from=BEFORE, version_seq=2),
        _customer("c2", effective_from=ALSO_BEFORE, version_seq=1)]).rows
    assert sorted(row["cif_id"] for row in rows) == ["c1", "c2"]
    assert len(rows) == 2


def test_rule_3_a_tie_the_declared_refs_CAN_break_resolves_to_one_row(compiled):
    """Two rows share an effective time; `deterministic_tie_break_refs` orders them, so the spine
    answers rather than refusing."""
    node = _render(compiled, LATEST)
    rows = _run(node, [
        _customer("c1", effective_from=BEFORE, version_seq=1),
        _customer("c1", effective_from=BEFORE, version_seq=2)]).rows
    assert len(rows) == 1


def test_rule_3_an_UNRESOLVED_tie_refuses_with_SPINE_NON_DETERMINISTIC(compiled):
    """The sharp one. Two eligible rows agree on the effective time AND on every declared tie-break,
    so no declared ordering separates them.

    `row_number()` would hand back one of them and the run would succeed — with a population that
    changes between runs and every downstream number moving with it. The spine must REFUSE.
    """
    node = _render(compiled, LATEST)
    with pytest.raises(RuntimeError) as refusal:
        _run(node, [
            _customer("c1", effective_from=BEFORE, version_seq=7),
            _customer("c1", effective_from=BEFORE, version_seq=7)])
    assert "SPINE_NON_DETERMINISTIC" in str(refusal.value)


def test_rule_3_refuses_when_NO_tie_break_is_declared_and_rows_tie(compiled):
    """`deterministic_tie_break_refs` may be empty only when the governed grain proves a tie cannot
    happen. When one happens anyway it is a gate on real rows, not a compilation refusal."""
    node = _render(compiled, UNTIEBREAKABLE)
    with pytest.raises(RuntimeError) as refusal:
        _run(node, [_customer("c1", effective_from=BEFORE), _customer("c1", effective_from=BEFORE)])
    assert "SPINE_NON_DETERMINISTIC" in str(refusal.value)


def test_an_unresolved_tie_on_ONE_key_refuses_the_whole_spine(compiled):
    """A refusal that only dropped the ambiguous key would publish a population missing a member and
    say nothing — which is the failure `SPINE_INCOMPLETE` exists to name, arrived at silently."""
    node = _render(compiled, LATEST)
    with pytest.raises(RuntimeError):
        _run(node, [
            _customer("c1", effective_from=BEFORE, version_seq=7),
            _customer("c1", effective_from=BEFORE, version_seq=7),
            _customer("c2", effective_from=BEFORE, version_seq=1)])


# ── §4.2 rules 4, 5, 6 and the other three policies ──────────────────────────────────────────────


def test_active_population_keeps_only_the_DECLARED_status_values(compiled):
    """A closed set, declared — there is no implicit notion of "active" and no free-text predicate."""
    node = _render(compiled, ACTIVE)
    rows = _run(node, [
        _customer("c1", status_cd="ACTIVE"), _customer("c2", status_cd="DORMANT"),
        _customer("c3", status_cd="CLOSED")]).rows
    assert sorted(row["cif_id"] for row in rows) == ["c1", "c2"]


def test_current_snapshot_refuses_a_business_date_it_was_not_OBSERVED_at(compiled):
    """A present-day table cannot honestly answer an arbitrary historical business date, so a
    mismatch refuses rather than answering with today's rows."""
    node = _render(compiled, SNAPSHOT)
    with pytest.raises(RuntimeError) as refusal:
        _run(node, [_customer("c1")], business_dt="2020-01-01")
    assert OBSERVED in str(refusal.value)
    assert "2020-01-01" in str(refusal.value)


def test_current_snapshot_answers_the_business_date_it_WAS_observed_at(compiled):
    """The guard must not refuse everything: the vintage it was observed at is answerable."""
    rows = _run(_render(compiled, SNAPSHOT), [_customer("c1")]).rows
    assert [row["cif_id"] for row in rows] == ["c1"]


def test_partition_mapped_reads_only_the_partition_the_business_date_maps_to(compiled):
    """§3.4's declared mapping decides the partition value; the node applies it, never infers one."""
    node = _render(compiled, PARTITIONED, spine_input=_partition_input(compiled))
    rows = _run(node, [
        _customer("c1", snapshot_dt="2026-07-27"), _customer("c2", snapshot_dt="2026-07-26")]).rows
    assert [row["cif_id"] for row in rows] == ["c1"]


def test_partition_mapped_honours_a_COMPACT_transform(compiled):
    """The transform is declared and closed, and the two members render two different values."""
    mapping = dataclasses.replace(EVENT_TIME_PARTITION, transform=PartitionTransform.DATE_COMPACT)
    node = _render(compiled, PARTITIONED, spine_input=_partition_input(compiled, mapping))
    rows = _run(node, [
        _customer("c1", snapshot_dt="20260727"), _customer("c2", snapshot_dt="2026-07-27")]).rows
    assert [row["cif_id"] for row in rows] == ["c1"]


def test_partition_mapped_honours_a_STATIC_snapshot_selection(compiled):
    """A declared constant vintage does not move with the business date — and is still declared."""
    mapping = StaticSnapshot(partition_values=(("snapshot_dt", "2026-01-01"),))
    node = _render(compiled, PARTITIONED, spine_input=_partition_input(compiled, mapping))
    rows = _run(node, [
        _customer("c1", snapshot_dt="2026-01-01"), _customer("c2", snapshot_dt=BUSINESS_DT)]).rows
    assert [row["cif_id"] for row in rows] == ["c1"]


def test_rule_5_duplicate_spine_keys_are_a_BLOCKING_gate(compiled):
    """Not a de-duplication step. Two rows for one key under a policy that collapses nothing is a
    declaration that does not hold, and the run must stop rather than shrink the population."""
    node = _render(compiled, ACTIVE)
    with pytest.raises(RuntimeError) as refusal:
        _run(node, [_customer("c1"), _customer("c1")])
    assert "SPINE_DUPLICATE_KEY" in str(refusal.value)


def test_rule_6_the_spines_own_availability_ref_filters_every_policy(compiled):
    """It participates in PIT filtering exactly as an expression's does — under EVERY policy, not
    only the one whose variant happens to name an availability column of its own."""
    for policy in (ACTIVE, SNAPSHOT, PARTITIONED):
        node = _render(
            compiled, policy,
            spine_input=_partition_input(compiled) if policy is PARTITIONED else None)
        rows = _run(node, [_customer("c1", load_ts=AFTER)]).rows
        assert rows == [], policy.kind


def test_the_spine_emits_the_PLANNED_key_columns_and_the_business_date(compiled):
    """Exactly one row per `(keys…, business_dt)` (§8 rule 3), under the names the plan publishes."""
    _authorized, plan, _contract, _input = compiled
    frame = _run(_render(compiled, ACTIVE), [_customer("c1")])
    assert frame.columns == [*plan.entity_key_columns, plan.business_dt_column]
    assert frame.rows[0][plan.business_dt_column] == dt.date.fromisoformat(BUSINESS_DT)


# ── refusals decidable at RENDER time ────────────────────────────────────────────────────────────


def test_it_refuses_a_policy_outside_the_closed_union(compiled):
    """The default a missing variant falls into is "read the whole table" — §4.2's own words for the
    behaviour it exists to eliminate."""
    with pytest.raises(ValueError, match="snapshot policy"):
        _render(compiled, object())


def test_it_refuses_an_ACTIVE_population_with_an_EMPTY_status_set(compiled):
    """An empty closed set selects nothing, so the spine would be empty and every landing key would
    disappear with no error — the shape of a population claim nobody could check."""
    with pytest.raises(ValueError, match="no allowed status"):
        _render(compiled, ActivePopulation(status_ref=CUSTOMERS_STATUS, allowed_status_values=()))


def test_it_refuses_when_the_contracts_keys_are_not_the_spines(compiled):
    """The published row's key columns come from the plan and its rows from the spine; if the two
    describe different keys the node aliases one table's columns into another's names."""
    authorized, plan, contract, spine_input = compiled
    other = dataclasses.replace(contract, ordered_keys=(f"{CUSTOMERS}.other_id",))
    with pytest.raises(ValueError, match="landing key"):
        render_spine_node(authorized.spine, plan, other, spine_input=spine_input,
                          source_dataset="raw_public__customers", spine_dataset="primary_spine")


def test_it_refuses_a_PARTITION_MAPPED_policy_with_no_declared_mapping(compiled):
    """§3.4 puts the mapping in the environment inventory, declared and never inferred: a policy
    that says a business date selects partitions, over a table that declares no mapping, is a
    partition set this renderer would have to guess."""
    naked = dataclasses.replace(compiled[3], partition_columns=None, partition_mapping=None)
    with pytest.raises(ValueError, match="partition mapping"):
        _render(compiled, PARTITIONED, spine_input=naked)


def test_it_refuses_a_PARTITION_MAPPED_policy_over_a_FULL_SCAN_mapping(compiled):
    """`FullScan` reads every partition, which is the opposite of "a business date selects partition
    values" — accepting it would render a whole-table read under a policy that forbids one."""
    with pytest.raises(ValueError, match="partition mapping"):
        _render(compiled, PARTITIONED, spine_input=_partition_input(compiled, FullScan()))


def test_it_refuses_a_PARTITION_MAPPED_policy_whose_columns_are_not_the_mappings(compiled):
    """Two copies of one mapping is two mappings, and the second one is the one nobody governs."""
    policy = PartitionMappedSnapshot(ordered_partition_refs=(f"{CUSTOMERS}.load_dt",))
    with pytest.raises(ValueError, match="partition"):
        _render(compiled, policy, spine_input=_partition_input(compiled))


def test_it_refuses_something_that_is_not_a_spine_spec(compiled):
    _authorized, plan, contract, spine_input = compiled
    with pytest.raises(TypeError, match="SpineSpec"):
        render_spine_node(object(), plan, contract, spine_input=spine_input,
                          source_dataset="raw", spine_dataset="primary_spine")


def test_it_refuses_a_CURRENT_ACTIVE_ONLY_claim_under_a_policy_that_is_not_ActivePopulation(
        compiled):
    """§4.2 rule 4. The declaration validator enforces it too; the renderer re-checks because it is
    the last place that can, and a spine rendered around a claim its policy cannot support publishes
    an active-only population that filters nothing."""
    spine = _with_policy(compiled[0].spine, LATEST)
    spine = dataclasses.replace(
        spine, population_semantics=PopulationSemantics.CURRENT_ACTIVE_ONLY,
        declaration=dataclasses.replace(
            spine.declaration, population_semantics=PopulationSemantics.CURRENT_ACTIVE_ONLY))
    with pytest.raises(ValueError, match="CURRENT_ACTIVE_ONLY"):
        render_spine_node(spine, compiled[1], compiled[2], spine_input=compiled[3],
                          source_dataset="raw", spine_dataset="primary_spine")


# ── goldens: a change detector, and the weakest test here ────────────────────────────────────────


@pytest.mark.parametrize("kind", [policy.kind.value for policy in ALL_POLICIES])
def test_the_rendered_spine_matches_its_golden(compiled, kind):
    """Goldens prove STABILITY, never correctness — every property above is asserted on its own, and
    these sit on top so an unintended byte cannot move silently."""
    golden = GOLDENS / f"{kind}.py"
    rendered = _render_all(compiled)[kind].source
    if not golden.exists():  # pragma: no cover — first run only
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered, encoding="utf-8")
    assert rendered == golden.read_text(encoding="utf-8")


# ══ Task 13b — the PIT projection (§8 rules 1 and 2) ═════════════════════════════════════════════
#
# The rules here are the ones whose violation raises NOTHING. A gate that admits a row the bank
# could not yet read, and a three-month window silently computed as ninety days, both produce a
# feature that backtests beautifully and is wrong in production. So every rule below is asserted by
# RUNNING the rendered source and reading the rows that survive — the spec's own instruction for the
# worked example ("asserted by executing the generated code, never by inspecting rendered text").
#
# Still not Spark. `fake_spark` is a stand-in; L0 (§11.2, Task 15) is what proves the project runs.

PROJECTION_GOLDENS = pathlib.Path(__file__).parent / "goldens" / "projection_nodes"

#: The window's own governed zone, and the cadence's: both `Asia/Kolkata` in the worked fixture, and
#: separate FIELDS — one test re-declares the window's to prove they are rendered separately.
WINDOW_ZONE = "Asia/Kolkata"

#: §8's worked example, verbatim. `posted_at` is five days after the transaction, so the row is
#: invisible on the 3rd and visible on the 6th — and the window contains 2026-07-01 on both dates,
#: which is what makes this a test of the AVAILABILITY gate and not of the window.
WORKED_EVENT = dt.datetime(2026, 7, 1)
WORKED_POSTED = dt.datetime(2026, 7, 5)
WORKED_INVISIBLE = "2026-07-03"
WORKED_VISIBLE = "2026-07-06"


@pytest.fixture
def expression(compiled):
    """The real compiled expression for `total_debit_amount_30d` — trailing 30 days, posted_at."""
    return compiled[0].irs[0].expressions[0]


def _with_pit(expression, **overrides):
    return dataclasses.replace(expression, pit=dataclasses.replace(expression.pit, **overrides))


def _projection(compiled, expression, **overrides):
    return nodes_compute.render_projection_node(
        _with_pit(expression, **overrides) if overrides else expression,
        compiled[2], feature_column=SUM_30D,
        source_dataset="raw_banking__transactions",
        projection_dataset="intermediate_total_debit_amount_30d__body_expr")


#: Long before any business date these tests use, so a row carrying it always passes rule 1. The
#: window tests set it deliberately: a rule-2 test that a rule-1 filter happened to also satisfy
#: would keep passing after rule 2 was deleted.
LONG_POSTED = dt.datetime(2000, 1, 1)


def _txn(**overrides):
    """One transaction row. Every read-set column present, plus one that is NOT in the read set."""
    row = {"cif_id": "c1", "dr_cr_flag": "D", "posted_ts": BEFORE, "status_cd": "posted",
           "txn_amt": 10, "txn_dt": BEFORE, "pan": "4111111111111111"}
    row.update(overrides)
    return row


def _windowed(txn_dt, amount):
    """A row that rule 1 always admits, so what survives is rule 2's answer alone."""
    return _txn(txn_dt=txn_dt, posted_ts=LONG_POSTED, txn_amt=amount)


def _project(node: RenderedNode, rows, business_dt: str = BUSINESS_DT):
    """Execute a rendered projection against the stand-in and return the frame it produced."""
    run = fake_spark.run_rendered(node.source, node.func_name)
    return run(fake_spark.DataFrame(rows), business_dt)


def _kept(node: RenderedNode, rows, business_dt: str = BUSINESS_DT):
    return [row["txn_amt"] for row in _project(node, rows, business_dt).rows]


# ── the stand-in itself: a fake that models a rule LOOSELY is a test that lies ────────────────────


def test_the_stand_ins_add_months_is_the_CALENDAR_operation_not_a_day_count():
    """If `add_months` were thirty days, every calendar-window test below would pass against a
    renderer that converted months to days — which is the one thing they exist to catch."""
    frame = fake_spark.DataFrame([{"d": dt.date(2026, 7, 6)}, {"d": dt.date(2026, 3, 31)}])
    stepped = frame.select(fake_spark.functions.add_months(
        fake_spark.functions.col("d"), -3).alias("back"))
    assert stepped.rows[0]["back"] == dt.date(2026, 4, 6)      # NOT 2026-04-07, which is 90 days
    assert stepped.rows[1]["back"] == dt.date(2025, 12, 31)

    clamped = frame.select(fake_spark.functions.add_months(
        fake_spark.functions.col("d"), -1).alias("back"))
    assert clamped.rows[1]["back"] == dt.date(2026, 2, 28)     # Spark clamps; 30 days is 2026-03-01


def test_the_stand_ins_trunc_is_the_start_of_the_named_calendar_period():
    frame = fake_spark.DataFrame([{"d": dt.date(2026, 7, 6)}])
    for unit, expected in (("week", dt.date(2026, 7, 6)), ("month", dt.date(2026, 7, 1)),
                           ("quarter", dt.date(2026, 7, 1)), ("year", dt.date(2026, 1, 1))):
        got = frame.select(fake_spark.functions.trunc(
            fake_spark.functions.col("d"), unit).alias("t")).rows[0]["t"]
        assert got == expected, unit


def test_the_stand_in_REFUSES_what_it_does_not_model():
    """A fake's failure mode is not being wrong, it is being permissive. An `expr` that swallowed
    arbitrary SQL would make an `event_time_plus_lag` test pass whether or not the lag rendered."""
    F = fake_spark.functions
    with pytest.raises(NotImplementedError):
        F.expr("current_timestamp()")
    with pytest.raises(NotImplementedError):
        F.trunc(F.col("d"), "fortnight")
    with pytest.raises(NotImplementedError):
        F.col("d").cast("string")

    # Task 13c's additions. Each one is a way a permissive fake would launder a wrong renderer.
    one = fake_spark.DataFrame([{"k": "a", "v": 1}])
    with pytest.raises(NotImplementedError, match="left"):
        one.join(one, on=["k"], how="right")          # an outer join changes the population
    with pytest.raises(NotImplementedError, match="SEQUENCE"):
        one.join(one, on=F.col("k"), how="left")      # the expression form duplicates key columns
    with pytest.raises(NotImplementedError, match="not an aggregate"):
        one.groupBy(F.col("k")).agg(F.col("v"))       # a bare column against a GROUP
    with pytest.raises(AttributeError):
        F.col("v").otherwise(0)                       # `otherwise` without a `when`

    # Task 13d's additions.
    with pytest.raises(NotImplementedError, match="divided by zero"):
        # Non-ANSI Spark answers this with a NULL. Modelling that would make the rendered
        # zero_denominator guard invisible: a renderer that never applied the policy at all would
        # produce the very same NULL, and the test written for the policy would agree with it.
        one.withColumn("q", F.col("v") / F.lit(0))
    with pytest.raises(NotImplementedError, match="subtraction"):
        one.withColumn("d", F.col("k") - F.lit(1))    # arithmetic over a non-numeric


# ── the stand-in's Task 13c surface: aggregation, joining, rounding, overflow ─────────────────────


def test_the_stand_ins_LEFT_join_keeps_the_unmatched_row_and_INNER_drops_it():
    """§8 rule 3 is only a claim if the stand-in can tell the two apart. If `join` ignored `how`,
    the LEFT-join test would pass against a renderer that emitted an INNER one."""
    F = fake_spark.functions
    spine = fake_spark.DataFrame([{"cif_id": "c1"}, {"cif_id": "c2"}])
    aggregate = fake_spark.DataFrame([{"cif_id": "c1", "v": 7}], columns=["cif_id", "v"])

    left = spine.join(aggregate, on=["cif_id"], how="left")
    assert [row["cif_id"] for row in left.rows] == ["c1", "c2"]
    assert left.rows[1]["v"] is None                  # present, and carrying nothing
    assert left.columns == ["cif_id", "v"]            # ONE copy of the key column

    inner = spine.join(aggregate, on=["cif_id"], how="inner")
    assert [row["cif_id"] for row in inner.rows] == ["c1"]

    # The case rule 3 is really about: NOTHING matched. An empty right side still has its column,
    # because a frame's schema does not depend on whether it has rows.
    empty = fake_spark.DataFrame([], columns=["cif_id", "v"])
    nothing = spine.join(empty, on=["cif_id"], how="left")
    assert len(nothing.rows) == 2 and all(row["v"] is None for row in nothing.rows)
    assert F.col("v")._eval(nothing.rows[0]) is None


def test_the_stand_ins_aggregates_SKIP_nulls_and_an_all_null_SUM_is_NULL_not_zero():
    """"Zero" is a value a formula must DECLARE (`EmptyWindowResult.ZERO`). A stand-in whose SUM
    produced 0 on its own would make the empty-window policy untestable — every path would look
    like the declared one."""
    F = fake_spark.functions
    rows = [{"k": "a", "v": 1}, {"k": "a", "v": None}, {"k": "a", "v": 1},
            {"k": "b", "v": None}]
    grouped = fake_spark.DataFrame(rows).groupBy(F.col("k")).agg(
        F.sum(F.col("v")).alias("total"),
        F.count(F.col("v")).alias("non_null"),
        F.count(F.lit(1)).alias("rows"),
        F.countDistinct(F.col("v")).alias("distinct"))
    by_key = {row["k"]: row for row in grouped.rows}
    assert by_key["a"] == {"k": "a", "total": 2, "non_null": 2, "rows": 3, "distinct": 1}
    assert by_key["b"] == {"k": "b", "total": None, "non_null": 0, "rows": 1, "distinct": 0}
    assert grouped.columns == ["k", "total", "non_null", "rows", "distinct"]


def test_the_stand_ins_round_is_HALF_UP_and_bround_is_HALF_EVEN():
    """The two modes disagree on exactly the ties, which is the whole of the distinction. A fake
    that implemented one for both would pass a renderer that emitted the wrong function."""
    F = fake_spark.functions
    frame = fake_spark.DataFrame([{"v": "2.5"}, {"v": "3.5"}, {"v": None}])
    half_up = frame.select(F.round(F.col("v"), 0).alias("r")).rows
    half_even = frame.select(F.bround(F.col("v"), 0).alias("r")).rows
    assert [row["r"] for row in half_up] == [3, 4, None]
    assert [row["r"] for row in half_even] == [2, 4, None]


def test_the_stand_ins_decimal_cast_OVERFLOWS_to_NULL_exactly_as_spark_does():
    """This is the behaviour §9's OVERFLOW_VIOLATION check exists to convert into a refusal. If the
    cast raised instead, the rendered check would be untestable and the gate would be decoration."""
    F = fake_spark.functions
    frame = fake_spark.DataFrame([{"v": "99.994"}, {"v": "1000"}, {"v": None}])
    fitted = frame.select(F.col("v").cast("decimal(5,2)").alias("d")).rows
    assert fitted[0]["d"] == decimal.Decimal("99.99")   # quantized, not lost
    assert fitted[1]["d"] is None                       # 1000.00 needs 6 digits, p=5 — NULL
    assert fitted[2]["d"] is None                       # NULL in, NULL out


# ── the stand-in's Task 13d surface: the two final operations ─────────────────────────────────────


def test_the_stand_ins_division_PROPAGATES_nulls_and_REFUSES_a_zero_divisor():
    """A ratio's two failure modes, told apart.

    A NULL on either side is a NULL — that is how an absent operand (an empty window under a `null`
    policy) reaches the feature column without anything inventing a number for it. A ZERO divisor
    raises instead, and deliberately does not model non-ANSI Spark's silent NULL: §8 rule 4 assigns
    a zero denominator to the formula's own `zero_denominator` policy, and a stand-in that answered
    it with a NULL would produce exactly what a renderer that never applied the policy produces.
    """
    F = fake_spark.functions
    frame = fake_spark.DataFrame([
        {"n": decimal.Decimal("30"), "d": decimal.Decimal("100")},
        {"n": None, "d": decimal.Decimal("100")},
        {"n": decimal.Decimal("30"), "d": None},
    ])
    quotients = [row["q"] for row in frame.withColumn("q", F.col("n") / F.col("d")).rows]
    assert quotients == [decimal.Decimal("0.3"), None, None]

    with pytest.raises(NotImplementedError, match="divided by zero"):
        fake_spark.DataFrame([{"n": 1, "d": decimal.Decimal("0.000000")}]).withColumn(
            "q", F.col("n") / F.col("d"))


def test_the_stand_ins_subtraction_PROPAGATES_nulls_rather_than_treating_one_side_as_zero():
    """A DIFFERENCE over an empty window. If subtraction coerced a NULL to 0 the declared `null`
    empty-window policy would silently become `zero`, and `0 - x` is a real, plausible number."""
    F = fake_spark.functions
    frame = fake_spark.DataFrame([
        {"a": decimal.Decimal("7"), "b": decimal.Decimal("4")},
        {"a": None, "b": decimal.Decimal("4")},
        {"a": decimal.Decimal("7"), "b": None},
    ])
    differences = [row["d"] for row in frame.withColumn("d", F.col("a") - F.col("b")).rows]
    assert differences == [decimal.Decimal("3"), None, None]


# ── the node's shape and its wiring ──────────────────────────────────────────────────────────────


def test_the_projection_wires_the_datasets_it_was_given_and_the_business_date(compiled, expression):
    node = _projection(compiled, expression)
    assert node.inputs == ("raw_banking__transactions", "params:business_dt")
    assert node.outputs == ("intermediate_total_debit_amount_30d__body_expr",)
    assert node.name == node.func_name == "project_total_debit_amount_30d__body_expr"
    assert node.tags == ("intermediate", "projection")
    assert "banking" not in node.source and "transactions" not in node.source


def test_the_projection_source_parses_and_renders_identically_every_time(compiled, expression):
    """Determinism: `generated_project_hash` must identify WHAT was built, not the day it was."""
    first, second = _projection(compiled, expression), _projection(compiled, expression)
    assert ast.parse(first.source)
    assert first.source == second.source and first.imports == second.imports


# ── §8 rule 1: the availability gate ─────────────────────────────────────────────────────────────


def test_rule_1_the_WORKED_EXAMPLE_excludes_then_includes_the_same_row(compiled, expression):
    """The spec's worked example, EXECUTED: `transaction_date = 2026-07-01`, `posted_at =
    2026-07-05` — excluded at `business_dt = 2026-07-03`, included at `2026-07-06`.

    Nothing about the row changes between the two runs. What changes is what the bank knew.
    """
    node = _projection(compiled, expression)
    row = _txn(txn_dt=WORKED_EVENT, posted_ts=WORKED_POSTED, txn_amt=100)
    assert _kept(node, [row], WORKED_INVISIBLE) == []
    assert _kept(node, [row], WORKED_VISIBLE) == [100]


def test_rule_1_gates_on_the_GOVERNED_availability_column_not_the_event_time(compiled, expression):
    """`posted_ts` is the governed availability column and `txn_dt` is the clock. A row can happen
    long before anyone can see it — a renderer that gated on the event time would keep this row."""
    node = _projection(compiled, expression)
    unposted = _txn(txn_dt=WORKED_EVENT, posted_ts=AFTER, txn_amt=7)
    assert _kept(node, [unposted], WORKED_VISIBLE) == []


def test_rule_1_uses_the_GOVERNED_zone_for_the_cutoff_not_the_sessions(compiled, expression):
    """The cadence is `00:00:00 Asia/Kolkata`, so 2026-07-27's cutoff is 2026-07-26 **18:30 UTC**.
    A row posted at 20:00 UTC is after it and before UTC midnight, so a renderer that inherited the
    pinned-UTC session zone keeps it."""
    node = _projection(compiled, expression)
    late = _txn(txn_dt=ALSO_BEFORE, posted_ts=AFTER_IN_UTC_ONLY, txn_amt=3)
    assert _kept(node, [late]) == []
    assert _kept(node, [_txn(txn_dt=ALSO_BEFORE, posted_ts=CUTOFF_UTC, txn_amt=3)]) == [3]


def test_rule_1_renders_the_DECLARED_LAG_for_event_time_plus_lag(compiled, expression):
    """Under `event_time_plus_lag` the availability column holds the EVENT time, and the row is not
    readable until `lag_hours` later. A renderer that dropped the lag admits every row six hours
    early — six hours of rows the bank had not yet received."""
    node = _projection(
        compiled, expression,
        availability_basis=nodes_compute.AvailabilityBasis.EVENT_TIME_PLUS_LAG,
        availability_lag_hours="6")
    # Exactly at the cutoff: readable without the lag, six hours short of readable with it.
    at_cutoff = _txn(txn_dt=ALSO_BEFORE, posted_ts=CUTOFF_UTC, txn_amt=1)
    six_hours_earlier = _txn(txn_dt=ALSO_BEFORE, posted_ts=CUTOFF_UTC - dt.timedelta(hours=6),
                             txn_amt=2)
    assert _kept(node, [at_cutoff, six_hours_earlier]) == [2]
    assert "INTERVAL 6 HOURS" in node.source


def test_rule_1_renders_a_FRACTIONAL_lag_exactly_rather_than_rounding_it(compiled, expression):
    """`lag_hours` is a canonical string because a float reaching an identity hash is a rounding
    decision nobody made. Rendering it must not make that decision either."""
    node = _projection(
        compiled, expression,
        availability_basis=nodes_compute.AvailabilityBasis.EVENT_TIME_PLUS_LAG,
        availability_lag_hours="1.5")
    assert "INTERVAL 90 MINUTES" in node.source
    just_inside = _txn(txn_dt=ALSO_BEFORE, posted_ts=CUTOFF_UTC - dt.timedelta(minutes=90),
                       txn_amt=5)
    just_outside = _txn(txn_dt=ALSO_BEFORE, posted_ts=CUTOFF_UTC - dt.timedelta(minutes=89),
                        txn_amt=6)
    assert _kept(node, [just_inside, just_outside]) == [5]


def test_rule_1_gates_INGESTED_AT_on_the_governed_column_too(compiled, expression):
    node = _projection(compiled, expression,
                       availability_basis=nodes_compute.AvailabilityBasis.INGESTED_AT)
    assert _kept(node, [_txn(txn_dt=ALSO_BEFORE, posted_ts=AFTER, txn_amt=9)]) == []
    assert "ingested_at" in node.source


# ── §8 rule 2: window boundaries ─────────────────────────────────────────────────────────────────


def test_rule_2_a_trailing_day_window_ends_at_the_business_date_and_spans_its_length(
        compiled, expression):
    """30 trailing days from 2026-07-27, in Asia/Kolkata: `[2026-06-27 00:00 IST, 2026-07-27 00:00
    IST)` — which is `[2026-06-26 18:30 UTC, 2026-07-26 18:30 UTC)`."""
    node = _projection(compiled, expression)
    rows = [
        _windowed(dt.datetime(2026, 6, 26, 18, 29), 1),   # a minute before the start
        _windowed(dt.datetime(2026, 6, 26, 18, 30), 2),   # the start itself — inclusive
        _windowed(dt.datetime(2026, 7, 26, 18, 29), 3),   # a minute before the end
        _windowed(dt.datetime(2026, 7, 26, 18, 30), 4),   # the end itself — exclusive
    ]
    assert _kept(node, rows) == [2, 3]


def test_rule_2_BOTH_inclusivity_flags_are_read_as_DATA_not_assumed(compiled, expression):
    """`start_inclusive` and `end_inclusive` are carried per expression because they vary. Flipping
    both must move exactly the two boundary rows — a renderer that assumed `[start, end)` cannot."""
    flipped = _projection(compiled, expression,
                          window_start_inclusive="exclusive", window_end_inclusive="inclusive")
    rows = [
        _windowed(dt.datetime(2026, 6, 26, 18, 30), 2),   # the start — now EXCLUDED
        _windowed(dt.datetime(2026, 7, 26, 18, 29), 3),
        _windowed(dt.datetime(2026, 7, 26, 18, 30), 4),   # the end — now INCLUDED
    ]
    assert _kept(flipped, rows) == [3, 4]


def test_rule_2_a_MONTH_window_is_a_CALENDAR_month_and_NOT_thirty_days(compiled, expression):
    """The whole of §8 rule 2. Three months before 2026-07-06 is **2026-04-06**; ninety days before
    it is **2026-04-07**. A row on 2026-04-06 is inside the calendar window and outside the day-count
    one, so this test fails against any renderer that converts months to days."""
    node = _projection(compiled, expression, window_length=3, window_unit="month")
    on_the_calendar_boundary = _windowed(dt.datetime(2026, 4, 6, 12, 0), 1)
    assert _kept(node, [on_the_calendar_boundary], "2026-07-06") == [1]
    # And the arithmetic itself: a month window renders `add_months` and never `date_sub`, which
    # is the only shape a day-count conversion could take.
    assert "F.add_months(window_end, -3)" in node.source
    assert "F.date_sub" not in node.source


def test_rule_2_a_month_window_CLAMPS_to_the_end_of_the_month(compiled, expression):
    """One month before 2026-03-31 is 2026-02-28 — `add_months`'s clamping. Thirty days before it
    is 2026-03-01, three days later and a whole month of rows lighter."""
    node = _projection(compiled, expression, window_length=1, window_unit="month")
    late_february = _windowed(dt.datetime(2026, 2, 28, 12, 0), 1)
    assert _kept(node, [late_february], "2026-03-31") == [1]


def test_rule_2_a_CALENDAR_PERIOD_window_excludes_the_INCOMPLETE_current_period(
        compiled, expression):
    """A calendar period ends where the current period BEGINS: as of 2026-07-06, three calendar
    months are `[2026-04-01, 2026-07-01)`. The trailing window of the same length and unit would
    include 2026-07-03, so the two bases select different rows and the flag is not decoration."""
    period = _projection(compiled, expression, window_basis="calendar_period",
                         window_length=3, window_unit="month")
    trailing = _projection(compiled, expression, window_length=3, window_unit="month")
    this_month = _windowed(dt.datetime(2026, 7, 3, 12, 0), 1)
    first_of_the_period = _windowed(dt.datetime(2026, 4, 1, 12, 0), 2)
    just_before_it = _windowed(dt.datetime(2026, 3, 31, 12, 0), 3)
    rows = [this_month, first_of_the_period, just_before_it]
    # Disjoint. The trailing window starts on 2026-04-06 and so drops the 1st of April, and it
    # ends on the business date and so keeps the 3rd of July. Neither row is in both.
    assert _kept(period, rows, "2026-07-06") == [2]
    assert _kept(trailing, rows, "2026-07-06") == [1]


def test_rule_2_every_window_unit_renders_its_OWN_calendar_arithmetic(compiled, expression):
    """Five units, and none of them collapses into another's step. A quarter is three months and a
    year is twelve, so `add_months` carries the multiplier rather than a separate day count."""
    starts = {unit: _projection(compiled, expression, window_length=2, window_unit=unit).source
              for unit in ("day", "week", "month", "quarter", "year")}
    assert "F.date_sub(window_end, 2)" in starts["day"]
    assert "F.date_sub(window_end, 14)" in starts["week"]
    assert "F.add_months(window_end, -2)" in starts["month"]
    assert "F.add_months(window_end, -6)" in starts["quarter"]
    assert "F.add_months(window_end, -24)" in starts["year"]


def test_rule_2_a_year_window_covers_a_LEAP_day_by_being_a_year(compiled, expression):
    """One year before 2027-01-01 is 2026-01-01 — 365 days. One year before 2025-01-01 is
    2024-01-01, which is 366. A renderer using either constant is wrong on the other date."""
    node = _projection(compiled, expression, window_length=1, window_unit="year")
    first_day = _windowed(dt.datetime(2024, 1, 1, 12, 0), 1)
    assert _kept(node, [first_day], "2025-01-01") == [1]


def test_rule_2_the_window_uses_ITS_OWN_governed_zone_not_the_cadences(compiled, expression):
    """`window_timezone` and the cadence's `cutoff_timezone` are separate governed fields. Declared
    in `America/New_York`, 2026-07-27's midnight is 04:00 UTC rather than the previous 18:30, so a
    renderer that reused the cutoff's zone — or inherited the session's UTC — moves the boundary."""
    node = _projection(compiled, expression, window_timezone="America/New_York")
    assert "'America/New_York'" in node.source and WINDOW_ZONE in node.source  # both, separately
    rows = [
        _windowed(dt.datetime(2026, 7, 26, 20, 0), 1),   # 16:00 NY on the 26th — inside
        _windowed(dt.datetime(2026, 7, 27, 4, 1), 2),    # 00:01 NY on the 27th — outside
    ]
    assert _kept(node, rows) == [1]


# ── the read set ─────────────────────────────────────────────────────────────────────────────────


def test_the_projection_selects_ONLY_the_read_set_columns_and_never_a_star(compiled, expression):
    """`*` reads whatever the table holds today, including a column added after Gate 2 authorized
    the read. The `pan` on every fixture row is exactly that column, and it must not come out."""
    node = _projection(compiled, expression)
    authorized = sorted({ref.column for ref in expression.physical_read_set if ref.column})
    assert _project(node, [_txn()]).columns == authorized
    assert "pan" not in node.source
    assert "*" not in node.source


# ── refusals decidable at RENDER time ────────────────────────────────────────────────────────────


def test_it_refuses_an_availability_BASIS_outside_the_closed_vocabulary(compiled, expression):
    """The default an unmodelled member falls into is no gate at all — every row visible."""
    with pytest.raises(ValueError, match="availability basis"):
        _projection(compiled, expression, availability_basis="whenever")


def test_it_refuses_a_LAG_that_contradicts_the_declared_basis(compiled, expression):
    with pytest.raises(ValueError, match="lag"):
        _projection(compiled, expression, availability_lag_hours="6")
    with pytest.raises(ValueError, match="lag"):
        _projection(compiled, expression,
                    availability_basis=nodes_compute.AvailabilityBasis.EVENT_TIME_PLUS_LAG)


def test_it_refuses_a_NEGATIVE_or_unreadable_lag(compiled, expression):
    for bad in ("-1", "later", "0.0001"):
        with pytest.raises(ValueError, match="lag"):
            _projection(compiled, expression,
                        availability_basis=nodes_compute.AvailabilityBasis.EVENT_TIME_PLUS_LAG,
                        availability_lag_hours=bad)


def test_it_refuses_a_window_UNIT_or_BASIS_or_INCLUSIVITY_outside_the_closed_vocabulary(
        compiled, expression):
    with pytest.raises(ValueError, match="unit"):
        _projection(compiled, expression, window_unit="fortnight")
    with pytest.raises(ValueError, match="basis"):
        _projection(compiled, expression, window_basis="rolling")
    with pytest.raises(ValueError, match="boundaries are declared"):
        _projection(compiled, expression, window_start_inclusive="maybe")


def test_it_refuses_a_window_of_no_length(compiled, expression):
    """A window spanning no time is an empty projection for every entity, and every feature falls
    to its empty-window policy with no error anywhere."""
    with pytest.raises(ValueError, match="window length"):
        _projection(compiled, expression, window_length=0)


def test_it_refuses_an_expression_whose_READ_SET_SPANS_TABLES(compiled, expression):
    """A projection reads ONE relation; a node handed one dataset cannot name another table's
    columns. The traversal that would reach it is the join plan, and this renderer emits no join."""
    from featuregen.materialize.expression_ir import PhysicalRef
    elsewhere = dataclasses.replace(
        expression, physical_read_set=(*expression.physical_read_set, PhysicalRef(
            logical_ref="hdfc::public.accounts.acct_id", catalog_source="hdfc", schema="banking",
            table="accounts", column="acct_id", roles=())))
    with pytest.raises(ValueError, match="spans"):
        nodes_compute.render_projection_node(
            elsewhere, compiled[2], feature_column=SUM_30D,
            source_dataset="raw", projection_dataset="intermediate")


def test_it_refuses_a_PIT_column_outside_the_authorized_read_set(compiled, expression):
    """Filtering on a column Gate 2 did not authorize is a read nobody granted — and it is not in
    the select either, so the node would fail on the cluster instead of here."""
    with pytest.raises(ValueError, match="read set"):
        _projection(compiled, expression, availability_ref="hdfc::public.transactions.settled_ts")


def test_it_refuses_something_that_is_not_a_compiled_expression(compiled):
    with pytest.raises(TypeError, match="ExpressionExecutionIR"):
        nodes_compute.render_projection_node(
            object(), compiled[2], feature_column=SUM_30D,
            source_dataset="raw", projection_dataset="intermediate")


# ── goldens: a change detector, and the weakest test here ────────────────────────────────────────


PROJECTION_VARIANTS = {
    "trailing_days_posted_at": {},
    "calendar_months": {"window_basis": "calendar_period", "window_length": 3,
                        "window_unit": "month"},
    "event_time_plus_lag": {"availability_basis": "event_time_plus_lag",
                            "availability_lag_hours": "6"},
}


@pytest.mark.parametrize("variant", sorted(PROJECTION_VARIANTS))
def test_the_rendered_projection_matches_its_golden(compiled, expression, variant):
    """Goldens prove STABILITY, never correctness — every property above is asserted on its own."""
    golden = PROJECTION_GOLDENS / f"{variant}.py"
    rendered = _projection(compiled, expression, **PROJECTION_VARIANTS[variant]).source
    if not golden.exists():  # pragma: no cover — first run only
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered, encoding="utf-8")
    assert rendered == golden.read_text(encoding="utf-8")


# ══ Task 13c — the per-feature calculation (§8 rules 3 and 4, §6, §9) ════════════════════════════
#
# The rules here are the ones that produce a plausible number rather than an error. An INNER join
# instead of a LEFT one shrinks the population and every entity that survives still looks right; a
# decimal overflow under Spark's default returns NULL, which reads as "no activity". So both are
# asserted by RUNNING the rendered source and reading the rows — a string assertion cannot tell a
# `how='left'` from a comment about one, and cannot see a NULL at all.
#
# Still not Spark. `fake_spark` models joins, aggregates, rounding and the decimal cast to the best
# of my reading of them; L0 (§11.2, Task 15) is what proves the project runs.

STAGING = "feature_staging_total_debit_amount_30d"
MANIFEST = "feature_staging_manifest_total_debit_amount_30d"
PROJECTION = "intermediate_total_debit_amount_30d__body_expr"


@pytest.fixture
def feature(compiled):
    """The real PlannedFeature for `total_debit_amount_30d` — DECIMAL(38,6), HALF_EVEN, ERROR."""
    return next(planned for planned in compiled[1].features
                if planned.column_name == SUM_30D)


@pytest.fixture
def lock_tree(tmp_path):
    """The minimal project shape a rendered node's `GENERATED.lock` read resolves against.

    A real sealed project is used by one test below; this stand-in tree exists so the other twenty
    do not each have to render, seal and materialize one to assert something about a join.
    """
    nodes = tmp_path / "src" / "pkg" / "pipelines" / "materialize" / "nodes.py"
    nodes.parent.mkdir(parents=True)
    nodes.write_text("", encoding="utf-8")
    (tmp_path / "GENERATED.lock").write_text(
        json.dumps({"compilation": {}, "generated_project_hash": "project-hash-0001"}),
        encoding="utf-8")
    return str(nodes)


def _per_path(value, ir):
    """A scalar policy fanned out over the body's paths — the shape the renderer requires.

    Tests that care about ONE expression say `EmptyWindowResult.ZERO`; tests that care about the two
    halves declaring different things pass the mapping straight through.
    """
    if isinstance(value, dict):
        return value
    return dict.fromkeys(_paths(ir), value)


def _paths(ir):
    """The body paths of `ir` — empty for the deliberately-wrong arguments the type checks reject."""
    return [expression.expr_path for expression in getattr(ir, "expressions", ())]


def _calculate(compiled, feature, *, ir=None, plan=None, projections=None,
               empty_window=EmptyWindowResult.NULL, null_input=NullInput.IGNORE):
    ir = ir if ir is not None else compiled[0].irs[0]
    if projections is None:
        projections = dict.fromkeys(_paths(ir), PROJECTION)
    return nodes_compute.render_calculation_node(
        ir, feature, plan if plan is not None else compiled[1],
        empty_window=_per_path(empty_window, ir), null_input=_per_path(null_input, ir),
        projection_datasets=projections, spine_dataset="primary_spine",
        staging_dataset=STAGING, manifest_dataset=MANIFEST)


def _run_calculation(node, lock_tree, *, projection, spine, business_dt=BUSINESS_DT,
                     generation="gen-0001", run="run-0001", execution="exec-0001",
                     staging_root="hdfs://nn/staging/gen-0001"):
    run_node = fake_spark.run_rendered(node.source, node.func_name, module_file=lock_tree)
    return run_node(fake_spark.DataFrame(projection), fake_spark.DataFrame(spine, columns=[
        "cif_id", "business_dt"]), business_dt, generation, run, execution, staging_root)


def _spine_rows(*cifs, business_dt=BUSINESS_DT):
    return [{"cif_id": cif, "business_dt": dt.date.fromisoformat(business_dt)} for cif in cifs]


def _debit(cif, amount, **overrides):
    """One row as the PROJECTION emits it: the read set, already point-in-time filtered."""
    row = {"cif_id": cif, "dr_cr_flag": "D", "status_cd": "posted", "txn_amt": amount,
           "posted_ts": LONG_POSTED, "txn_dt": BEFORE}
    row.update(overrides)
    return row


def _staged(node, lock_tree, *, projection, spine, **kwargs):
    frame, _manifest = _run_calculation(node, lock_tree, projection=projection, spine=spine,
                                        **kwargs)
    return {row["cif_id"]: row[SUM_30D] for row in frame.rows}


# ── the node's shape and its wiring ──────────────────────────────────────────────────────────────


def test_the_calculation_wires_both_projections_inputs_and_BOTH_outputs(compiled, feature):
    node = _calculate(compiled, feature)
    assert node.inputs == (PROJECTION, "primary_spine", "params:business_dt",
                           "params:generation_id", "params:run_id",
                           "params:sandbox_execution_hash", "params:staging_root")
    assert node.outputs == (STAGING, MANIFEST)
    assert node.name == node.func_name == "calculate_total_debit_amount_30d"
    assert node.tags == ("feature_staging", "calculation")


def test_the_calculation_source_parses_and_renders_identically_every_time(compiled, feature):
    first, second = _calculate(compiled, feature), _calculate(compiled, feature)
    assert ast.parse(first.source)
    assert first.source == second.source and first.imports == second.imports


def test_the_calculations_signature_is_positionally_the_wiring(compiled, feature):
    """Kedro binds inputs POSITIONALLY. A signature and a wiring that disagree produce a pipeline
    that runs and computes the wrong thing — the spine's frame arriving as the projection."""
    node = _calculate(compiled, feature)
    tree = ast.parse(node.source)
    arguments = [arg.arg for arg in tree.body[0].args.args]
    expected = [name.removeprefix("params:") for name in node.inputs]
    assert arguments == ["projection", "spine", *expected[2:]]



# ── §8 rule 3: the spine reduction ───────────────────────────────────────────────────────────────


def test_rule_3_an_entity_with_NO_source_rows_is_STILL_PRESENT(compiled, feature, lock_tree):
    """The mutant this is written against is `how='inner'`. It raises nothing, computes numbers
    that are all individually right, and silently publishes a smaller population."""
    node = _calculate(compiled, feature)
    values = _staged(node, lock_tree,
                     projection=[_debit("c1", 10), _debit("c1", 5)],
                     spine=_spine_rows("c1", "c2", "c3"))
    assert sorted(values) == ["c1", "c2", "c3"]
    assert values["c1"] == 15
    assert values["c2"] is None and values["c3"] is None


def test_rule_3_EVERY_entity_is_present_when_NOTHING_matched_at_all(compiled, feature, lock_tree):
    """The degenerate case, and the one an inner join makes look like a successful empty run."""
    node = _calculate(compiled, feature)
    values = _staged(node, lock_tree, projection=[], spine=_spine_rows("c1", "c2"))
    assert values == {"c1": None, "c2": None}


def test_rule_3_produces_EXACTLY_ONE_row_per_key_and_business_date(compiled, feature, lock_tree):
    node = _calculate(compiled, feature)
    frame, _manifest = _run_calculation(
        node, lock_tree, projection=[_debit("c1", 1), _debit("c1", 2), _debit("c2", 3)],
        spine=_spine_rows("c1", "c2"))
    keyed = [(row["cif_id"], row["business_dt"]) for row in frame.rows]
    assert len(keyed) == len(set(keyed)) == 2


def test_rule_3_an_entity_NOT_in_the_spine_is_not_invented_by_the_join(compiled, feature,
                                                                      lock_tree):
    """The population is the SPINE's. A left join from the spine cannot add a member, and a
    right or outer one would — which is why the stand-in refuses both."""
    node = _calculate(compiled, feature)
    values = _staged(node, lock_tree, projection=[_debit("ghost", 99), _debit("c1", 1)],
                     spine=_spine_rows("c1"))
    assert values == {"c1": 1}


def test_the_staged_output_carries_ONLY_keys_business_dt_and_the_ONE_feature(compiled, feature,
                                                                            lock_tree):
    """§10.2. The three system columns are added once, at assembly; one per staging output would
    collide there. The marker column must be gone too — it is this node's invention."""
    node = _calculate(compiled, feature, empty_window=EmptyWindowResult.ZERO)
    frame, _manifest = _run_calculation(node, lock_tree, projection=[_debit("c1", 4)],
                                        spine=_spine_rows("c1"))
    assert frame.columns == ["cif_id", "business_dt", SUM_30D]
    assert "__source_rows" not in frame.columns


def test_the_business_date_on_the_staged_row_is_the_SPINES(compiled, feature, lock_tree):
    """Not the run parameter re-derived. The spine decides which population the row belongs to."""
    node = _calculate(compiled, feature)
    frame, _manifest = _run_calculation(node, lock_tree, projection=[_debit("c1", 4)],
                                        spine=_spine_rows("c1", business_dt="2026-07-27"))
    assert frame.rows[0]["business_dt"] == dt.date(2026, 7, 27)


def test_no_row_collapsing_repair_is_ever_emitted(compiled, feature, lock_tree):
    """Fan-out is refused upstream (`joins.py`), so a `dropDuplicates` here would be masking a
    `1:N` that got through — the way row inflation becomes invisible.

    Asserted on the CALL, not the word: the rendered prose argues the case for not collapsing, so
    a search for "distinct" matches this node's own explanation of why it does not use one. The
    stand-in raises on both, so their USE is detectable and not merely their spelling — the
    execution below is the load-bearing half."""
    for empty in EmptyWindowResult:
        node = _calculate(compiled, feature, empty_window=empty)
        assert ".dropDuplicates(" not in node.source and ".distinct(" not in node.source
        assert "how='left'" in node.source and "how='inner'" not in node.source
        _run_calculation(node, lock_tree, projection=[_debit("c1", 1)], spine=_spine_rows("c1"))


# ── §8 rule 4: the empty-window value, and null_input ─────────────────────────────────────────────


def test_rule_4_an_empty_window_carries_the_DECLARED_zero_not_a_null(compiled, feature, lock_tree):
    node = _calculate(compiled, feature, empty_window=EmptyWindowResult.ZERO)
    values = _staged(node, lock_tree, projection=[_debit("c1", 7)], spine=_spine_rows("c1", "c2"))
    assert values["c1"] == 7
    assert values["c2"] == 0 and values["c2"] is not None


def test_rule_4_an_empty_window_carries_NULL_when_that_is_what_is_declared(compiled, feature,
                                                                          lock_tree):
    """The pair with the test above: the same rows and the same spine produce different values,
    so a renderer that ignored the policy and always did one of them fails one of the two."""
    node = _calculate(compiled, feature, empty_window=EmptyWindowResult.NULL)
    values = _staged(node, lock_tree, projection=[_debit("c1", 7)], spine=_spine_rows("c1", "c2"))
    assert values == {"c1": 7, "c2": None}


def test_rule_4_an_empty_window_STOPS_THE_RUN_when_that_is_what_is_declared(compiled, feature,
                                                                           lock_tree):
    node = _calculate(compiled, feature, empty_window=EmptyWindowResult.ERROR)
    with pytest.raises(RuntimeError, match="empty_window=error"):
        _run_calculation(node, lock_tree, projection=[_debit("c1", 7)],
                         spine=_spine_rows("c1", "c2"))
    # …and does NOT stop when every entity has rows: the gate fires on the declared condition and
    # not on the policy being declared at all.
    frame, _manifest = _run_calculation(node, lock_tree,
                                        projection=[_debit("c1", 7), _debit("c2", 1)],
                                        spine=_spine_rows("c1", "c2"))
    assert len(frame.rows) == 2


def test_rule_4_the_empty_window_value_is_told_apart_from_a_NULL_AGGREGATE(compiled, feature,
                                                                          lock_tree):
    """The distinction the marker column exists for. `c2` HAS a row in the window; its operand is
    null, so `ignore` sums nothing and Spark returns NULL. That is not an empty window, and under
    `zero` it must NOT be filled — a coalesce would answer this entity's policy question with the
    other one's answer."""
    node = _calculate(compiled, feature, empty_window=EmptyWindowResult.ZERO)
    values = _staged(node, lock_tree,
                     projection=[_debit("c1", 3), _debit("c2", None)],
                     spine=_spine_rows("c1", "c2", "c3"))
    assert values["c1"] == 3
    assert values["c2"] is None      # a row EXISTED; its aggregate is null. Not an empty window.
    assert values["c3"] == 0         # no row at all. THIS is the empty window.


def test_rule_4_null_input_IGNORE_skips_a_null_operand(compiled, feature, lock_tree):
    node = _calculate(compiled, feature, null_input=NullInput.IGNORE)
    values = _staged(node, lock_tree,
                     projection=[_debit("c1", 5), _debit("c1", None), _debit("c1", 5)],
                     spine=_spine_rows("c1"))
    assert values["c1"] == 10


def test_rule_4_null_input_ZERO_counts_a_null_operand_as_zero(compiled, feature, lock_tree):
    """For a SUM the total is the same as `ignore`; the policy is still read, and a COUNT over the
    same rows would differ. Asserted alongside `propagate`, which the same rows must not equal."""
    node = _calculate(compiled, feature, null_input=NullInput.ZERO)
    values = _staged(node, lock_tree,
                     projection=[_debit("c1", 5), _debit("c1", None)], spine=_spine_rows("c1"))
    assert values["c1"] == 5
    assert "F.coalesce" in _calculate(compiled, feature, null_input=NullInput.ZERO).source


def test_rule_4_null_input_PROPAGATE_makes_ONE_null_operand_null_the_whole_aggregate(
        compiled, feature, lock_tree):
    """Spark's aggregates all skip nulls, so this is the member that has to be RENDERED. A
    renderer that ignored `null_input` entirely would return 10 here and pass every test above."""
    node = _calculate(compiled, feature, null_input=NullInput.PROPAGATE)
    values = _staged(node, lock_tree,
                     projection=[_debit("c1", 5), _debit("c1", None), _debit("c1", 5),
                                 _debit("c2", 4)],
                     spine=_spine_rows("c1", "c2"))
    assert values["c1"] is None      # one null anywhere in the group
    assert values["c2"] == 4         # and a group with none is unaffected


# ── §6 / §9: rounding is explicit, and overflow RAISES ───────────────────────────────────────────


def test_overflow_RAISES_rather_than_publishing_the_NULL_spark_would_substitute(
        compiled, feature, lock_tree):
    """The whole point of OVERFLOW_VIOLATION. Spark's default on decimal overflow is a NULL, which
    reads downstream as "no activity" — a wrong number that raises nothing anywhere."""
    node = _calculate(compiled, feature)
    huge = decimal.Decimal(10) ** 33          # 10^33 needs 34 integer digits; DECIMAL(38,6) has 32
    with pytest.raises(RuntimeError, match="OVERFLOW_VIOLATION"):
        _run_calculation(node, lock_tree, projection=[_debit("c1", huge)],
                         spine=_spine_rows("c1"))


def test_a_value_that_FITS_is_published_and_does_not_trip_the_overflow_gate(compiled, feature,
                                                                            lock_tree):
    """The pair with the test above: a gate that fired on every row would also pass it."""
    node = _calculate(compiled, feature)
    fits = decimal.Decimal(10) ** 30
    values = _staged(node, lock_tree, projection=[_debit("c1", fits)], spine=_spine_rows("c1"))
    assert values["c1"] == fits


def test_a_null_that_was_ALREADY_null_is_not_reported_as_an_overflow(compiled, feature, lock_tree):
    """An entity with no rows is NULL by the declared empty-window policy. Reporting it as an
    overflow would fire the wrong gate and stop a run that is behaving exactly as declared."""
    node = _calculate(compiled, feature, empty_window=EmptyWindowResult.NULL)
    values = _staged(node, lock_tree, projection=[_debit("c1", 1)], spine=_spine_rows("c1", "c2"))
    assert values["c2"] is None


def test_the_overflow_check_reads_the_DECLARED_precision_and_scale(compiled, feature, lock_tree):
    """Not a constant. A narrower declared type must refuse a value the wider one accepts."""
    narrow = dataclasses.replace(
        feature, physical_type=dataclasses.replace(
            feature.physical_type, sql_type="DECIMAL(9,2)"))
    plan = dataclasses.replace(compiled[1], features=(narrow,))
    node = _calculate(compiled, narrow, plan=plan)
    assert "decimal(9,2)" in node.source
    with pytest.raises(RuntimeError, match="OVERFLOW_VIOLATION"):
        _run_calculation(node, lock_tree, projection=[_debit("c1", 10_000_000)],
                         spine=_spine_rows("c1"))


def test_rounding_is_the_DECLARED_mode_and_the_two_modes_disagree_on_ties(compiled, feature,
                                                                          lock_tree):
    """HALF_UP and HALF_EVEN differ on exactly the ties. A renderer that emitted one for both, or
    that left rounding to an engine default, gives the same answer for one of these and not both."""
    def value(mode):
        typed = dataclasses.replace(feature.physical_type, rounding=mode,
                                    sql_type="DECIMAL(18,0)")
        planned = dataclasses.replace(feature, physical_type=typed)
        node = _calculate(compiled, planned,
                          plan=dataclasses.replace(compiled[1], features=(planned,)))
        return _staged(node, lock_tree, projection=[_debit("c1", decimal.Decimal("2.5"))],
                       spine=_spine_rows("c1"))["c1"]

    assert value(RoundingMode.HALF_UP) == 3
    assert value(RoundingMode.HALF_EVEN) == 2


def test_rounding_is_rendered_at_the_DECLARED_scale(compiled, feature, lock_tree):
    node = _calculate(compiled, feature)
    assert "F.bround(F.col('total_debit_amount_30d'), 6)" in node.source
    values = _staged(node, lock_tree,
                     projection=[_debit("c1", decimal.Decimal("1.00000049"))],
                     spine=_spine_rows("c1"))
    assert values["c1"] == decimal.Decimal("1.000000")


def test_an_INTEGRAL_published_type_renders_no_rounding_and_no_overflow_check(compiled, feature):
    """§6 attaches both obligations to decimal arithmetic only. A rounding mode recorded against a
    BIGINT would be an instruction this node could act on wrongly."""
    counted = dataclasses.replace(
        feature, physical_type=dataclasses.replace(
            feature.physical_type, sql_type="BIGINT", rounding=None, overflow=None))
    node = _calculate(compiled, counted,
                      plan=dataclasses.replace(compiled[1], features=(counted,)))
    assert "F.bround" not in node.source and "F.round" not in node.source
    assert "OVERFLOW_VIOLATION" not in node.source


def test_it_refuses_a_ROUNDING_MODE_no_spark_function_implements_exactly(compiled, feature):
    for mode in (RoundingMode.DOWN, RoundingMode.UP, RoundingMode.FLOOR, RoundingMode.CEILING):
        planned = dataclasses.replace(
            feature, physical_type=dataclasses.replace(feature.physical_type, rounding=mode))
        with pytest.raises(ValueError, match="no Spark function implements it exactly"):
            _calculate(compiled, planned,
                       plan=dataclasses.replace(compiled[1], features=(planned,)))


def test_it_refuses_SATURATE_overflow_rather_than_quietly_not_clamping(compiled, feature):
    planned = dataclasses.replace(
        feature, physical_type=dataclasses.replace(
            feature.physical_type, overflow=OverflowBehavior.SATURATE))
    with pytest.raises(ValueError, match="nothing in this slice clamps"):
        _calculate(compiled, planned, plan=dataclasses.replace(compiled[1], features=(planned,)))


# ── §6: the governed filter ──────────────────────────────────────────────────────────────────────


def test_the_GOVERNED_FILTER_is_applied_before_the_aggregate(compiled, feature, lock_tree):
    """`total_debit_amount_30d` filters `dr_cr_flag = 'D' AND status_cd = 'posted'`. Neither the
    projection nor anything else applies it, so a renderer that dropped it sums the credits too —
    a bigger, plausible number with nothing to report it."""
    node = _calculate(compiled, feature)
    values = _staged(node, lock_tree, spine=_spine_rows("c1"), projection=[
        _debit("c1", 10),
        _debit("c1", 500, dr_cr_flag="C"),               # a credit
        _debit("c1", 700, status_cd="pending"),          # not posted
        _debit("c1", 900, dr_cr_flag="C", status_cd="pending"),
    ])
    assert values["c1"] == 10


def test_an_expression_with_NO_filter_aggregates_every_projected_row(compiled, feature, lock_tree):
    unfiltered = dataclasses.replace(compiled[0].irs[0].expressions[0], filter_tree=None)
    ir = dataclasses.replace(compiled[0].irs[0], expressions=(unfiltered,))
    node = _calculate(compiled, feature, ir=ir)
    assert "rows = projection\n" in node.source
    values = _staged(node, lock_tree, spine=_spine_rows("c1"),
                     projection=[_debit("c1", 10), _debit("c1", 500, dr_cr_flag="C")])
    assert values["c1"] == 510


@pytest.mark.parametrize(("tree", "expected"), [
    ({"kind": "predicate", "op": "greater_than", "left": fixtures.REF_AMT,
      "right_literal": {"type": "decimal", "value": "7.5"}, "right_param": None,
      "right_set": None},
     "F.col('txn_amt') > Decimal('7.5')"),
    ({"kind": "predicate", "op": "in", "left": fixtures.REF_DR_CR, "right_literal": None,
      "right_param": None,
      "right_set": [{"type": "string", "value": "D"}, {"type": "string", "value": "C"}]},
     "F.col('dr_cr_flag').isin(['D', 'C'])"),
    ({"kind": "predicate", "op": "not_in", "left": fixtures.REF_DR_CR, "right_literal": None,
      "right_param": None, "right_set": [{"type": "string", "value": "C"}]},
     "~F.col('dr_cr_flag').isin(['C'])"),
    ({"kind": "predicate", "op": "is_null", "left": fixtures.REF_AMT, "right_literal": None,
      "right_param": None, "right_set": None},
     "F.col('txn_amt').isNull()"),
    ({"kind": "bool", "op": "not", "children": [
        {"kind": "predicate", "op": "equal", "left": fixtures.REF_DR_CR,
         "right_literal": {"type": "string", "value": "C"}, "right_param": None,
         "right_set": None}]},
     "~(F.col('dr_cr_flag') == 'C')"),
    ({"kind": "bool", "op": "or", "children": [
        {"kind": "predicate", "op": "equal", "left": fixtures.REF_DR_CR,
         "right_literal": {"type": "string", "value": "C"}, "right_param": None,
         "right_set": None},
        {"kind": "predicate", "op": "less_or_equal", "left": fixtures.REF_AMT,
         "right_literal": {"type": "integer", "value": "3"}, "right_param": None,
         "right_set": None}]},
     "(F.col('dr_cr_flag') == 'C') | (F.col('txn_amt') <= 3)"),
])
def test_every_filter_op_renders_a_TYPED_comparison_not_the_canonical_string(
        compiled, feature, tree, expected):
    """Child-1 canonicalizes every literal value to a string so an identity hash never sees a
    float. Rendering that string straight through would compare a governed decimal against text,
    which Spark resolves by coercing one side — silently, and not always the same way."""
    expression = dataclasses.replace(compiled[0].irs[0].expressions[0], filter_tree=tree)
    ir = dataclasses.replace(compiled[0].irs[0], expressions=(expression,))
    source = _calculate(compiled, feature, ir=ir).source
    assert f"rows = projection.where({expected})" in source


def test_it_refuses_a_filter_that_compares_against_a_RUN_PARAMETER(compiled, feature):
    tree = {"kind": "predicate", "op": "equal", "left": fixtures.REF_DR_CR, "right_literal": None,
            "right_param": {"name": "min_amount"}, "right_set": None}
    expression = dataclasses.replace(compiled[0].irs[0].expressions[0], filter_tree=tree)
    ir = dataclasses.replace(compiled[0].irs[0], expressions=(expression,))
    with pytest.raises(ValueError, match="run parameter"):
        _calculate(compiled, feature, ir=ir)


def test_it_refuses_a_filter_on_a_column_OUTSIDE_the_authorized_read_set(compiled, feature):
    tree = {"kind": "predicate", "op": "equal", "left": fixtures.TABLE_REF + ".pan",
            "right_literal": {"type": "string", "value": "4111"}, "right_param": None,
            "right_set": None}
    expression = dataclasses.replace(compiled[0].irs[0].expressions[0], filter_tree=tree)
    ir = dataclasses.replace(compiled[0].irs[0], expressions=(expression,))
    with pytest.raises(ValueError, match="authorized read set"):
        _calculate(compiled, feature, ir=ir)


# ── §9: the staging manifest this node writes about itself ───────────────────────────────────────


def test_the_manifest_binds_the_output_to_THIS_generation_run_and_business_date(
        compiled, feature, lock_tree):
    """§9's stale-manifest hazard: a reused staging path can leave an older SUCCESSFUL manifest
    whose ir_hash still matches, and assembly would publish stale output past every other check."""
    node = _calculate(compiled, feature)
    _frame, manifest = _run_calculation(
        node, lock_tree, projection=[_debit("c1", 3)], spine=_spine_rows("c1"),
        generation="gen-0007", run="run-0009", business_dt=BUSINESS_DT)
    assert manifest["generation_id"] == "gen-0007"
    assert manifest["run_id"] == "run-0009"
    assert manifest["business_dt"] == BUSINESS_DT
    assert manifest["intent_feature_name"] == SUM_30D
    assert manifest["ir_hash"] == feature.ir_hash
    assert manifest["status"] == StagingStatus.COMPLETED.value
    assert manifest["row_count"] == 1


def test_the_manifest_is_a_REAL_StagingManifestV1_that_passes_completeness(compiled, feature,
                                                                          lock_tree):
    """Constructed from the rendered node's own JSON, then handed to §9's real gate. A manifest
    this renderer shaped differently from the dataclass would be a run's evidence nothing reads."""
    node = _calculate(compiled, feature)
    _frame, manifest = _run_calculation(
        node, lock_tree, projection=[_debit("c1", 3)], spine=_spine_rows("c1"),
        generation=GENERATION, run="run-0001")
    parsed = StagingManifestV1(**json.loads(json.dumps(manifest)))
    plan = dataclasses.replace(compiled[1], features=(feature,))
    assert check_completeness(plan, [parsed], generation_id=GENERATION, run_id="run-0001",
                              business_dt=BUSINESS_DT) == ()


def test_the_manifest_carries_NO_data_value_and_the_row_count_MOVES(compiled, feature, lock_tree):
    """§14: counts, types, hashes and locations only. The count is asserted to move so a hard-coded
    one — which would satisfy every other assertion here — fails."""
    node = _calculate(compiled, feature)
    _one, manifest_one = _run_calculation(node, lock_tree, projection=[_debit("c1", 3)],
                                          spine=_spine_rows("c1"))
    _two, manifest_two = _run_calculation(node, lock_tree, projection=[_debit("c1", 3)],
                                          spine=_spine_rows("c1", "c2", "c3"))
    assert manifest_one["row_count"] == 1 and manifest_two["row_count"] == 3
    assert set(manifest_one) == {field.name for field in dataclasses.fields(StagingManifestV1)}
    # And nothing beyond that field set: a manifest is counts, types, hashes and locations, so a
    # rendered node that helpfully added a sample value would fail here rather than at review.


def test_the_manifests_schema_hash_MOVES_with_the_declared_type(compiled, feature, lock_tree):
    """A hash that did not move with the type would be a field that proves nothing."""
    def hashed(sql_type):
        planned = dataclasses.replace(
            feature, physical_type=dataclasses.replace(feature.physical_type, sql_type=sql_type))
        node = _calculate(compiled, planned,
                          plan=dataclasses.replace(compiled[1], features=(planned,)))
        _frame, manifest = _run_calculation(node, lock_tree, projection=[_debit("c1", 3)],
                                            spine=_spine_rows("c1"))
        return manifest["schema_hash"]

    assert hashed("DECIMAL(38,6)") != hashed("DECIMAL(18,2)")


def test_the_manifests_output_location_is_composed_from_the_staging_root_PARAMETER(
        compiled, feature, lock_tree):
    """No location is written into node source. The root is a run parameter because §9's staging
    area is generation-scoped: one fixed at render time would be shared by every run."""
    node = _calculate(compiled, feature)
    _frame, manifest = _run_calculation(node, lock_tree, projection=[_debit("c1", 3)],
                                        spine=_spine_rows("c1"),
                                        staging_root="hdfs://nn/staging/gen-0042/")
    assert manifest["output_location"] == \
        "hdfs://nn/staging/gen-0042/" + feature_staging_path(SUM_30D)
    assert "hdfs" not in node.source          # the ROOT is never rendered, only the relative path


def test_the_manifests_path_is_the_CATALOGS_path_and_not_a_second_spelling(compiled, feature):
    """One spelling, or the manifest names a path nothing wrote to."""
    datasets = project_datasets(compiled[0], compiled[1], spine_input=compiled[3])
    project = render_project(
        compiled[0], compiled[1], environment_id=ENVIRONMENT,
        engine_versions=fixtures.ENGINE_VERSIONS, spine_input=compiled[3],
        nodes=_wired_nodes(compiled, datasets))
    entries = yaml.safe_load(project.files["conf/base/catalog.yml"])
    declared = entries[datasets.staging[SUM_30D]]["filepath"]
    assert declared.endswith(feature_staging_path(SUM_30D))
    assert feature_staging_path(SUM_30D) in _calculate(compiled, feature).source


def test_the_generated_project_hash_is_READ_FROM_THE_REAL_LOCK_at_run_time(compiled, feature,
                                                                          tmp_path):
    """§7 keeps `generated_project_hash` out of every other generated file, because the hash is
    taken OVER those files. The node therefore reads the lock — and the depth it walks up from
    `__file__` is proved against a really rendered project, not counted by eye."""
    datasets = project_datasets(compiled[0], compiled[1], spine_input=compiled[3])
    project = render_project(
        compiled[0], compiled[1], environment_id=ENVIRONMENT,
        engine_versions=fixtures.ENGINE_VERSIONS, spine_input=compiled[3],
        nodes=_wired_nodes(compiled, datasets))
    root = materialize_to(project, tmp_path / "generated")
    nodes_py = next(path for path in project.files if path.endswith("pipelines/materialize/nodes.py"))

    node = _calculate(compiled, feature)
    _frame, manifest = _run_calculation(node, str(root / nodes_py),
                                        projection=[_debit("c1", 3)], spine=_spine_rows("c1"))
    assert manifest["generated_project_hash"] == project.identity.generated_project_hash
    assert project.identity.generated_project_hash not in node.source


# ── the node inside a real project ───────────────────────────────────────────────────────────────


def _wired_nodes(compiled, datasets):
    """The real spine, the real projections and the real calculations, plus stubs for Task 14."""
    authorized, plan = compiled[0], compiled[1]
    nodes = [render_spine_node(
        authorized.spine, plan, compiled[2], spine_input=compiled[3],
        source_dataset=datasets.raw["banking.customers"], spine_dataset=datasets.spine)]
    for ir in authorized.irs:
        for expression in ir.expressions:
            physical = next(ref for ref in expression.physical_read_set
                            if RefRole.SOURCE_TABLE in ref.roles)
            nodes.append(nodes_compute.render_projection_node(
                expression, compiled[2], feature_column=ir.feature_name,
                source_dataset=datasets.raw[f"{physical.schema}.{physical.table}"],
                projection_dataset=datasets.projections[(ir.feature_name,
                                                         expression.expr_path)]))
        planned = next(item for item in plan.features if item.column_name == ir.feature_name)
        paths = [expression.expr_path for expression in ir.expressions]
        nodes.append(nodes_compute.render_calculation_node(
            ir, planned, plan,
            empty_window=dict.fromkeys(paths, EmptyWindowResult.NULL),
            null_input=dict.fromkeys(paths, NullInput.IGNORE),
            projection_datasets={path: datasets.projections[(ir.feature_name, path)]
                                 for path in paths},
            spine_dataset=datasets.spine, staging_dataset=datasets.staging[ir.feature_name],
            manifest_dataset=datasets.manifests[ir.feature_name]))
    nodes.append(_assembly_stub(datasets))
    return tuple(nodes)


def _assembly_stub(datasets):
    """Task 14's two nodes, as WIRING only — this file owns no line of their compute."""
    body = ("def assemble_and_publish(*frames):\n"
            '    """Task 14 renders this body."""\n    return None, None\n')
    return RenderedNode(
        name="assemble_and_publish", func_name="assemble_and_publish", source=body,
        inputs=(datasets.spine, *sorted(datasets.staging.values()),
                *sorted(datasets.manifests.values())),
        outputs=(datasets.assembled, datasets.published), tags=("feature",))


def test_the_calculation_closes_the_wiring_of_a_REAL_rendered_project(compiled):
    """Task 12 checks six ways a pipeline can be wrong while parsing perfectly. The calculation's
    two outputs and seven inputs are checked by THAT, not by a claim here."""
    datasets = project_datasets(compiled[0], compiled[1], spine_input=compiled[3])
    project = render_project(
        compiled[0], compiled[1], environment_id=ENVIRONMENT,
        engine_versions=fixtures.ENGINE_VERSIONS, spine_input=compiled[3],
        nodes=_wired_nodes(compiled, datasets))
    nodes_py = next(text for path, text in project.files.items()
                    if path.endswith("pipelines/materialize/nodes.py"))
    assert f"def {nodes_compute.calculation_func_name(SUM_30D)}(" in nodes_py
    assert ast.parse(nodes_py)


def test_the_renderer_still_imports_no_pyspark_and_writes_no_location(compiled, feature):
    """The package-wide rule, re-asserted over 13c's additions: PySpark exists only inside the
    rendered strings, and every location is a catalog entry."""
    source = inspect.getsource(nodes_compute)
    tree = ast.parse(source)
    imported = {getattr(node, "module", None) for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(tree) if isinstance(node, ast.Import)
                 for alias in node.names}
    assert not any((name or "").startswith(("pyspark", "kedro")) for name in imported)
    rendered = _calculate(compiled, feature).source
    assert "hdfs://" not in rendered and "s3://" not in rendered


# ── refusals decidable at RENDER time ────────────────────────────────────────────────────────────


def test_it_refuses_a_BODY_whose_compiled_PATHS_are_not_the_operations_own(compiled, feature):
    """The body path is the only thing that says WHICH operand an expression is. A ratio compiled
    to `body.expr` would be rendered as its own numerator and its own denominator — the number is 1
    for every entity that has any rows at all, and nothing anywhere would call that an error."""
    identity = compiled[0].irs[0]
    for operation in (FinalOperation.RATIO, FinalOperation.DIFFERENCE):
        ir = dataclasses.replace(identity, final_operation=operation,
                                 zero_denominator=None)
        with pytest.raises(ValueError, match="body path is what says WHICH operand"):
            _calculate(compiled, feature, ir=ir)


def test_it_refuses_an_identity_body_that_compiled_to_more_than_ONE_expression(compiled, feature):
    expression = compiled[0].irs[0].expressions[0]
    ir = dataclasses.replace(compiled[0].irs[0], expressions=(expression, expression))
    with pytest.raises(ValueError, match="body path is what says WHICH operand"):
        _calculate(compiled, feature, ir=ir)


def test_it_refuses_when_the_GRAIN_columns_are_not_the_LANDING_key_columns(compiled, feature):
    """`derive_contract` allows a feature's grain columns to be "a different table's spelling of
    the same entity", and nothing declares the mapping between the two spellings. Pairing them
    positionally would land every entity's value under another entity's key."""
    plan = dataclasses.replace(compiled[1], entity_key_columns=("customer_number",))
    with pytest.raises(ValueError, match="nothing states how one spelling maps"):
        _calculate(compiled, feature, plan=plan)


def test_it_refuses_when_the_PLAN_and_the_IR_describe_different_columns(compiled, feature):
    other = dataclasses.replace(feature, column_name="something_else")
    with pytest.raises(ValueError, match="two different features"):
        _calculate(compiled, feature, ir=dataclasses.replace(
            compiled[0].irs[0], feature_name="something_else"))
    with pytest.raises(ValueError, match="does not publish"):
        _calculate(compiled, other, ir=dataclasses.replace(
            compiled[0].irs[0], feature_name="something_else"))


def test_it_refuses_an_expression_whose_JOIN_PLAN_IS_NOT_RENDERABLE(compiled, feature):
    """13e made the calculation read the projection's OUTPUT rather than the whole read set, so a
    well-formed traversal now renders (see the 13e section). A malformed one still refuses HERE, at
    the calculation, because the grain it groups by is the one the traversal was supposed to reach.
    """
    expression = compiled[0].irs[0].expressions[0]
    hopped = dataclasses.replace(
        expression, join_plan=dataclasses.replace(
            expression.join_plan, steps=(JoinStep(
                from_ref=fixtures.TABLE_REF, to_ref=CUSTOMERS, cardinality='N:1'),)))
    ir = dataclasses.replace(compiled[0].irs[0], expressions=(hopped,))
    with pytest.raises(ValueError, match="hop"):
        _calculate(compiled, feature, ir=ir)


def test_it_refuses_an_aggregate_whose_OPERAND_does_not_match_its_kind(compiled, feature):
    """`operand is None` IFF COUNT_ROWS is Child-1's own grammar rule, checked in both directions:
    an operand under a row count describes a different expression from the one it says it is."""
    expression = compiled[0].irs[0].expressions[0]
    counted = dataclasses.replace(expression, aggregation=AggregateFunction.COUNT_ROWS)
    ir = dataclasses.replace(compiled[0].irs[0], expressions=(counted,))
    with pytest.raises(ValueError, match="count_rows aggregate carries operand"):
        _calculate(compiled, feature, ir=ir)

    without = dataclasses.replace(expression, physical_read_set=tuple(
        ref for ref in expression.physical_read_set if RefRole.OPERAND not in ref.roles))
    with pytest.raises(ValueError, match="0 operand column"):
        _calculate(compiled, feature,
                   ir=dataclasses.replace(compiled[0].irs[0], expressions=(without,)))


def test_it_refuses_a_filter_KIND_or_OP_outside_the_closed_vocabulary(compiled, feature):
    for tree, pattern in (
            ({"kind": "predicate", "op": "matches", "left": fixtures.REF_DR_CR,
              "right_literal": None, "right_param": None, "right_set": None}, "matches"),
            ({"kind": "regex", "op": "and", "children": []}, "closed vocabulary"),
            ({"kind": "bool", "op": "and", "children": []}, "no children"),
            ({"kind": "bool", "op": "not", "children": [
                {"kind": "predicate", "op": "is_null", "left": fixtures.REF_AMT,
                 "right_literal": None, "right_param": None, "right_set": None},
                {"kind": "predicate", "op": "is_null", "left": fixtures.REF_AMT,
                 "right_literal": None, "right_param": None, "right_set": None}]},
             "negates 2 children")):
        expression = dataclasses.replace(compiled[0].irs[0].expressions[0], filter_tree=tree)
        ir = dataclasses.replace(compiled[0].irs[0], expressions=(expression,))
        with pytest.raises(ValueError, match=pattern):
            _calculate(compiled, feature, ir=ir)


def test_it_refuses_a_LITERAL_that_is_not_the_type_it_declares(compiled, feature):
    for literal in ({"type": "integer", "value": "one"},
                    {"type": "decimal", "value": "1.2.3"},
                    {"type": "boolean", "value": "TRUE"},
                    {"type": "date", "value": "07/01/2026"}):
        tree = {"kind": "predicate", "op": "equal", "left": fixtures.REF_AMT,
                "right_literal": literal, "right_param": None, "right_set": None}
        expression = dataclasses.replace(compiled[0].irs[0].expressions[0], filter_tree=tree)
        ir = dataclasses.replace(compiled[0].irs[0], expressions=(expression,))
        with pytest.raises(ValueError, match="literal"):
            _calculate(compiled, feature, ir=ir)


def test_it_refuses_arguments_that_are_not_what_they_must_be(compiled, feature):
    with pytest.raises(TypeError, match="FormulaExecutionIRV1"):
        _calculate(compiled, feature, ir="total_debit_amount_30d")
    with pytest.raises(TypeError, match="PlannedFeature"):
        _calculate(compiled, "total_debit_amount_30d")
    with pytest.raises(TypeError, match="FeatureGroupPlanV1"):
        _calculate(compiled, feature, plan=object())


def test_the_two_POLICY_arguments_have_no_defaults_and_reject_a_word_outside_the_vocabulary(
        compiled, feature):
    """§8 rule 4's policies are the formula's. A default would be this renderer answering a policy
    question — `ignore` and `propagate` give different numbers and neither is the safer guess."""
    signature = inspect.signature(nodes_compute.render_calculation_node)
    for name in ("empty_window", "null_input"):
        assert signature.parameters[name].default is inspect.Parameter.empty
    with pytest.raises(ValueError):
        _calculate(compiled, feature, empty_window="nothing")
    with pytest.raises(ValueError):
        _calculate(compiled, feature, null_input="skip")


# ── goldens: a change detector, and the weakest test here ────────────────────────────────────────


CALCULATION_GOLDENS = pathlib.Path(__file__).parent / "goldens" / "calculation_nodes"

CALCULATION_VARIANTS = {
    "sum_decimal_null_window": {},
    "sum_decimal_zero_window": {"empty_window": EmptyWindowResult.ZERO},
    "sum_decimal_error_window": {"empty_window": EmptyWindowResult.ERROR},
    "sum_decimal_propagate_nulls": {"null_input": NullInput.PROPAGATE},
    "sum_decimal_zero_nulls": {"null_input": NullInput.ZERO},
}


@pytest.mark.parametrize("variant", sorted(CALCULATION_VARIANTS))
def test_the_rendered_calculation_matches_its_golden(compiled, feature, variant):
    """Goldens prove STABILITY, never correctness — every property above is asserted on its own."""
    golden = CALCULATION_GOLDENS / f"{variant}.py"
    rendered = _calculate(compiled, feature, **CALCULATION_VARIANTS[variant]).source
    if not golden.exists():  # pragma: no cover — first run only
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered, encoding="utf-8")
    assert rendered == golden.read_text(encoding="utf-8")


def test_the_count_variant_renders_its_own_golden(compiled, feature):
    """A BIGINT feature: no rounding, no overflow check, a COUNT DISTINCT and no operand policy to
    apply. Rendered from the SAME code path, so the golden is evidence the branches are branches."""
    expression = dataclasses.replace(compiled[0].irs[0].expressions[0],
                                     aggregation=AggregateFunction.COUNT_DISTINCT)
    ir = dataclasses.replace(compiled[0].irs[0], expressions=(expression,))
    counted = dataclasses.replace(feature, physical_type=dataclasses.replace(
        feature.physical_type, sql_type="BIGINT", nullable=False, rounding=None, overflow=None))
    golden = CALCULATION_GOLDENS / "count_distinct_bigint.py"
    rendered = _calculate(compiled, counted, ir=ir,
                          plan=dataclasses.replace(compiled[1], features=(counted,))).source
    if not golden.exists():  # pragma: no cover — first run only
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered, encoding="utf-8")
    assert rendered == golden.read_text(encoding="utf-8")


def test_every_rendered_calculation_line_fits_the_width_it_states(compiled, feature):
    """`_RENDERED_WIDTH` is a claim about the emitted bytes. Task 13a and 13b each shipped a line
    past it that only reading the golden caught; this makes the next one fail instead."""
    for variant in CALCULATION_VARIANTS.values():
        for line in _calculate(compiled, feature, **variant).source.splitlines():
            assert len(line) <= 100, line


# ══ Task 13d — the two FINAL OPERATIONS (§8 rule 4's ÷0 half) ════════════════════════════════════
#
# The rule that produces a plausible number rather than an error here is the ÷0 policy. Spark's
# division returns NULL for a zero denominator and NULL for an absent one, so a renderer that read
# the QUOTIENT back — or that coalesced — would answer one declaration's question with another's
# answer and publish a column nobody declared. Every assertion below therefore RUNS the rendered
# source and reads the rows: `zero_denominator=error` is a raise or it is nothing, and the
# difference between "no rows at all" and "rows summing to zero" is invisible in the text.

RATIO_90D = "cross_border_value_ratio_90d"
NUMERATOR_PATH = "body.numerator"
DENOMINATOR_PATH = "body.denominator"
RATIO_STAGING = "feature_staging_cross_border_value_ratio_90d"
RATIO_MANIFEST = "feature_staging_manifest_cross_border_value_ratio_90d"
RATIO_PROJECTIONS = {NUMERATOR_PATH: "intermediate_ratio__body_numerator",
                     DENOMINATOR_PATH: "intermediate_ratio__body_denominator"}


@pytest.fixture
def ratio(catalog, db):  # noqa: F811 — `catalog` seeds the governed facts `db` is read through
    """The REAL compilation of the worked ratio — two expressions, two windows, one filter."""
    authorized, plan, spine_input = _compiled(db, RATIO_90D)
    group = derive_group_contract(db, authorized, cadence=CADENCE,
                                  availability_promise=AvailabilityPromiseV1(calendar_days=1))
    assert isinstance(group, ContractGroup), group
    return authorized, plan, group.contract, spine_input


@pytest.fixture
def ratio_feature(ratio):
    """The real PlannedFeature for `cross_border_value_ratio_90d` — DECIMAL(38,6), HALF_EVEN."""
    return next(planned for planned in ratio[1].features if planned.column_name == RATIO_90D)


def _render_ratio(ratio, ratio_feature, *, zero_denominator=None, ir=None, plan=None,
                  empty_window=EmptyWindowResult.NULL, null_input=NullInput.IGNORE,
                  projections=None):
    ir = ir if ir is not None else ratio[0].irs[0]
    if zero_denominator is not None:
        ir = dataclasses.replace(ir, zero_denominator=zero_denominator)
    return nodes_compute.render_calculation_node(
        ir, ratio_feature, plan if plan is not None else ratio[1],
        empty_window=_per_path(empty_window, ir), null_input=_per_path(null_input, ir),
        projection_datasets=projections if projections is not None else RATIO_PROJECTIONS,
        spine_dataset="primary_spine", staging_dataset=RATIO_STAGING,
        manifest_dataset=RATIO_MANIFEST)


def _ratio_txn(cif, amount, *, cross_border=True):
    """One row as the PROJECTION emits it — already point-in-time filtered, no governed filter."""
    return {"cif_id": cif, "txn_amt": decimal.Decimal(str(amount)),
            "cross_border_flag": cross_border, "posted_ts": LONG_POSTED, "txn_dt": BEFORE}


def _run_ratio(node, lock_tree, *, numerator, denominator, spine, business_dt=BUSINESS_DT,
               generation="gen-0001", run="run-0001"):
    run_node = fake_spark.run_rendered(node.source, node.func_name, module_file=lock_tree)
    return run_node(fake_spark.DataFrame(numerator), fake_spark.DataFrame(denominator),
                    fake_spark.DataFrame(spine, columns=["cif_id", "business_dt"]),
                    business_dt, generation, run, "exec-0001", "hdfs://nn/staging/gen-0001")


def _ratio_values(node, lock_tree, *, rows=None, numerator=None, denominator=None, spine):
    """The feature column per entity. `rows` feeds BOTH projections, which is the real shape: the
    two PIT projections of this formula select the same window over the same table, and it is the
    GOVERNED filter inside the node that separates cross-border value from total value."""
    frame, _manifest = _run_ratio(
        node, lock_tree,
        numerator=rows if numerator is None else numerator,
        denominator=rows if denominator is None else denominator, spine=spine)
    return {row["cif_id"]: row[RATIO_90D] for row in frame.rows}


# ── the node's shape and its wiring ──────────────────────────────────────────────────────────────


def test_the_ratio_wires_BOTH_projections_IN_SLOT_ORDER_and_both_outputs(ratio, ratio_feature):
    """Kedro binds positionally. The numerator's dataset must be the FIRST input, or the generated
    pipeline runs, computes a number, and computes the reciprocal of the one that was declared."""
    node = _render_ratio(ratio, ratio_feature)
    assert node.inputs == (RATIO_PROJECTIONS[NUMERATOR_PATH], RATIO_PROJECTIONS[DENOMINATOR_PATH],
                           "primary_spine", "params:business_dt", "params:generation_id",
                           "params:run_id", "params:sandbox_execution_hash", "params:staging_root")
    assert node.outputs == (RATIO_STAGING, RATIO_MANIFEST)
    assert node.name == node.func_name == "calculate_cross_border_value_ratio_90d"
    signature = node.source.split(") -> ")[0]
    assert signature.index("numerator_projection") < signature.index("denominator_projection")


def test_the_ratio_source_parses_and_renders_identically_every_time(ratio, ratio_feature):
    first, second = _render_ratio(ratio, ratio_feature), _render_ratio(ratio, ratio_feature)
    assert ast.parse(first.source)
    assert first.source == second.source and first.imports == second.imports


def test_the_body_slots_are_CHILD_ONES_own_body_paths(ratio, ratio_feature):
    """`_BODY_SLOTS` says which operation owns which path; `BODY_PATHS` says which paths exist. If
    they drift, this renderer names an operand Child-1 does not compile and refuses every feature."""
    declared = {path for paths in nodes_compute._BODY_SLOTS.values() for path in paths}
    assert declared == set(BODY_PATHS)


# ── the two operands are two EXPRESSIONS ─────────────────────────────────────────────────────────


def test_the_WORKED_RATIO_divides_the_numerator_by_the_denominator(ratio, ratio_feature, lock_tree):
    """`cross_border_value_ratio_90d` — the feature that compiled, passed governance and then had
    no calculation node at all. 30 of 100 is 0.3, and the governed filter is what makes it 30."""
    node = _render_ratio(ratio, ratio_feature)
    values = _ratio_values(node, lock_tree, spine=_spine_rows("c1"), rows=[
        _ratio_txn("c1", 30, cross_border=True), _ratio_txn("c1", 70, cross_border=False)])
    assert values == {"c1": decimal.Decimal("0.300000")}


def test_each_operand_applies_its_OWN_governed_filter(ratio, ratio_feature, lock_tree):
    """The numerator declares `cross_border_flag = true` and the denominator declares nothing. The
    same rows reach both, so a renderer applying ONE filter to both would answer 1.0 for everyone —
    a number with no error anywhere, and exactly the shape a share-of-total feature would take."""
    node = _render_ratio(ratio, ratio_feature)
    values = _ratio_values(node, lock_tree, spine=_spine_rows("c1", "c2"), rows=[
        _ratio_txn("c1", 25, cross_border=True), _ratio_txn("c1", 75, cross_border=False),
        _ratio_txn("c2", 40, cross_border=True), _ratio_txn("c2", 40, cross_border=False)])
    assert values == {"c1": decimal.Decimal("0.250000"), "c2": decimal.Decimal("0.500000")}


def test_the_two_projections_are_read_INDEPENDENTLY_and_NOT_swapped(ratio, ratio_feature,
                                                                    lock_tree):
    """Two frames, deliberately different. A node reading one input twice, or reading them the
    other way round, produces 4 or 0.25 — both plausible, neither declared."""
    node = _render_ratio(ratio, ratio_feature)
    values = _ratio_values(
        node, lock_tree, spine=_spine_rows("c1"),
        numerator=[_ratio_txn("c1", 20)], denominator=[_ratio_txn("c1", 80)])
    assert values == {"c1": decimal.Decimal("0.250000")}


def test_each_operand_resolves_ITS_OWN_empty_window_policy(ratio, ratio_feature, lock_tree):
    """§8 rule 4's policies live on each expression's own WindowPolicy, not on the feature. The
    same rows and the same ÷0 policy give different columns when ONE operand's declaration moves —
    which is only true if the renderer read two declarations rather than one."""
    spine, rows = _spine_rows("c1"), []
    both_null = _render_ratio(ratio, ratio_feature, zero_denominator=ZeroDenominator.ZERO)
    assert _ratio_values(both_null, lock_tree, spine=spine, rows=rows) == {"c1": None}

    denominator_zero = _render_ratio(
        ratio, ratio_feature, zero_denominator=ZeroDenominator.ZERO,
        empty_window={NUMERATOR_PATH: EmptyWindowResult.NULL,
                      DENOMINATOR_PATH: EmptyWindowResult.ZERO})
    # The denominator's OWN declaration turned the empty window into a real zero, and the ÷0 policy
    # then answered it. Nothing about the numerator's declaration changed.
    assert _ratio_values(denominator_zero, lock_tree, spine=spine, rows=rows) == {"c1": 0}


def test_a_NULL_numerator_over_a_present_denominator_is_NULL_and_not_zero(ratio, ratio_feature,
                                                                          lock_tree):
    """An entity with total value but no cross-border value at all. Its numerator is an empty
    window under a `null` declaration, and `NULL / 100` is NULL — not the 0 a coalesce would give,
    which reads downstream as "this customer sends nothing abroad" rather than "not known"."""
    node = _render_ratio(ratio, ratio_feature)
    values = _ratio_values(node, lock_tree, spine=_spine_rows("c1"),
                           rows=[_ratio_txn("c1", 100, cross_border=False)])
    assert values == {"c1": None}


# ── §8 rule 4's ÷0 half: three members, three different columns ──────────────────────────────────


def _zero_denominator_rows():
    """c1 has a ratio; c2's denominator is exactly ZERO; c3 has no rows at all — ABSENT.

    c2 and c3 are the pair the whole policy turns on. Both end as "no value" under a naive
    implementation, and §8 rule 4 gives them different declared answers.
    """
    return [_ratio_txn("c1", 30, cross_border=True), _ratio_txn("c1", 70, cross_border=False),
            _ratio_txn("c2", 50, cross_border=True), _ratio_txn("c2", -50, cross_border=False)]


def test_rule_4_zero_denominator_NULL_leaves_a_null_and_stops_nothing(ratio, ratio_feature,
                                                                      lock_tree):
    node = _render_ratio(ratio, ratio_feature, zero_denominator=ZeroDenominator.NULL)
    values = _ratio_values(node, lock_tree, spine=_spine_rows("c1", "c2", "c3"),
                           rows=_zero_denominator_rows())
    assert values == {"c1": decimal.Decimal("0.300000"), "c2": None, "c3": None}


def test_rule_4_zero_denominator_ZERO_tells_a_ZERO_denominator_from_an_ABSENT_one(
        ratio, ratio_feature, lock_tree):
    """The test this whole task turns on. c2 has rows and its denominator is exactly zero, so the
    ÷0 policy answers it with 0. c3 has NO rows, so its own empty_window policy answers it with
    NULL. An implementation that coalesced the quotient, or that read "the result is null" as "the
    denominator was zero", gives c3 = 0 — a customer with no transactions reported as having a
    cross-border share of nought, which is a claim about them nobody made."""
    node = _render_ratio(ratio, ratio_feature, zero_denominator=ZeroDenominator.ZERO)
    values = _ratio_values(node, lock_tree, spine=_spine_rows("c1", "c2", "c3"),
                           rows=_zero_denominator_rows())
    assert values == {"c1": decimal.Decimal("0.300000"), "c2": 0, "c3": None}


def test_rule_4_zero_denominator_ERROR_genuinely_RAISES(ratio, ratio_feature, lock_tree):
    """`error` is not a mode Spark has: division by zero returns the same NULL the `null` policy
    asks for, so a formula declaring that the run should STOP would publish a column of nulls and
    report nothing. The rendered node makes it a real refusal, naming the count and no value."""
    node = _render_ratio(ratio, ratio_feature, zero_denominator=ZeroDenominator.ERROR)
    with pytest.raises(RuntimeError, match="zero_denominator=error") as raised:
        _ratio_values(node, lock_tree, spine=_spine_rows("c1", "c2", "c3"),
                      rows=_zero_denominator_rows())
    assert "Entities affected: 1" in str(raised.value)
    assert "50" not in str(raised.value)      # a count, never a data value (§14)


def test_rule_4_zero_denominator_ERROR_does_NOT_fire_for_an_ABSENT_denominator(
        ratio, ratio_feature, lock_tree):
    """The other half of the same distinction, and the one a naive implementation gets wrong in the
    louder direction: every entity with no rows in its window would stop the run."""
    node = _render_ratio(ratio, ratio_feature, zero_denominator=ZeroDenominator.ERROR)
    values = _ratio_values(node, lock_tree, spine=_spine_rows("c1", "c3"), rows=[
        _ratio_txn("c1", 30, cross_border=True), _ratio_txn("c1", 70, cross_border=False)])
    assert values == {"c1": decimal.Decimal("0.300000"), "c3": None}


def test_the_three_zero_denominator_members_render_DIFFERENTLY(ratio, ratio_feature):
    """Three declarations, three sources. If two of them rendered the same bytes, one formula's
    declared behaviour would be another's and no execution test could tell which."""
    rendered = {policy: _render_ratio(ratio, ratio_feature, zero_denominator=policy).source
                for policy in ZeroDenominator}
    assert len(set(rendered.values())) == 3
    for policy, source in rendered.items():
        assert f"zero_denominator is `{policy.value}`" in source


# ── §8 rule 3 and §10.2, over two aggregates ─────────────────────────────────────────────────────


def test_rule_3_an_entity_with_no_rows_in_EITHER_window_is_still_present(ratio, ratio_feature,
                                                                         lock_tree):
    node = _render_ratio(ratio, ratio_feature)
    frame, _manifest = _run_ratio(node, lock_tree, numerator=[], denominator=[],
                                  spine=_spine_rows("c1", "c2"))
    assert [row["cif_id"] for row in frame.rows] == ["c1", "c2"]
    assert all(row[RATIO_90D] is None for row in frame.rows)


def test_the_staged_frame_carries_only_the_keys_the_date_and_the_FEATURE(ratio, ratio_feature,
                                                                         lock_tree):
    """§10.2. The two operand columns and their markers are this node's own working state; either
    one surviving is an extra column §9 reports against the whole group at assembly."""
    node = _render_ratio(
        ratio, ratio_feature,
        empty_window={NUMERATOR_PATH: EmptyWindowResult.ZERO,
                      DENOMINATOR_PATH: EmptyWindowResult.ZERO},
        zero_denominator=ZeroDenominator.ZERO)
    frame, _manifest = _run_ratio(node, lock_tree, numerator=[_ratio_txn("c1", 3)],
                                  denominator=[_ratio_txn("c1", 6)], spine=_spine_rows("c1"))
    assert frame.columns == ["cif_id", "business_dt", RATIO_90D]


def test_the_operand_columns_are_dropped_BEFORE_the_staging_select(ratio, ratio_feature):
    """§10.2 is enforced TWICE, deliberately, and this asserts the second belt.

    The staging select carries the keys, the business date and one feature column, so it already
    removes the two operand columns whether or not they are dropped — the drop is not observable in
    the output, and a mutation that removes it survives every execution test in this file. It is
    rendered anyway because the node's own working state should have a stated lifetime in the
    generated project a bank reads, and because §10.2 then does not depend on one line: a change to
    the select alone cannot leak `__numerator` into an assembled group.
    """
    source = _render_ratio(ratio, ratio_feature).source
    dropped = source.index("staged = staged.drop('__numerator', '__denominator')")
    assert dropped < source.index("staged = staged.select(")


def test_the_ratio_emits_no_row_collapsing_repair(ratio, ratio_feature, lock_tree):
    """Two joins instead of one is two more chances to "fix" a fan-out that is refused upstream."""
    node = _render_ratio(ratio, ratio_feature)
    assert ".dropDuplicates(" not in node.source and ".distinct(" not in node.source
    _ratio_values(node, lock_tree, spine=_spine_rows("c1"), rows=[_ratio_txn("c1", 1)])


def test_the_ratios_manifest_is_read_by_SECTION_NINES_own_completeness_check(ratio, ratio_feature,
                                                                             lock_tree):
    node = _render_ratio(ratio, ratio_feature)
    _frame, manifest = _run_ratio(node, lock_tree, numerator=[_ratio_txn("c1", 3)],
                                  denominator=[_ratio_txn("c1", 6)], spine=_spine_rows("c1"),
                                  generation=GENERATION, run="run-0001")
    assert set(manifest) == {field.name for field in dataclasses.fields(StagingManifestV1)}
    parsed = StagingManifestV1(**json.loads(json.dumps(manifest)))
    assert check_completeness(ratio[1], [parsed], generation_id=GENERATION, run_id="run-0001",
                              business_dt=BUSINESS_DT) == ()


# ── the DIFFERENCE body ──────────────────────────────────────────────────────────────────────────


def _difference(ratio):
    """The worked ratio's compilation, re-declared as a DIFFERENCE and nothing else.

    No worked fixture declares a difference, and adding one to `fixtures` would change what every
    other test in the program compiles. So the real compilation's two expressions are re-pathed onto
    the difference's slots — the same aggregates, the same windows, the same governed filter, under
    the other operation. The feature keeps its name because the plan publishes that column and a
    node staged under any other name has no manifest §9 would read.
    """
    ir = ratio[0].irs[0]
    renamed = {NUMERATOR_PATH: "body.minuend", DENOMINATOR_PATH: "body.subtrahend"}
    return dataclasses.replace(
        ir, final_operation=FinalOperation.DIFFERENCE, zero_denominator=None,
        expressions=tuple(dataclasses.replace(expression, expr_path=renamed[expression.expr_path])
                          for expression in ir.expressions))


DIFFERENCE_PROJECTIONS = {"body.minuend": "intermediate_diff__body_minuend",
                          "body.subtrahend": "intermediate_diff__body_subtrahend"}


def _render_difference(ratio, ratio_feature, **overrides):
    return _render_ratio(ratio, ratio_feature, ir=_difference(ratio),
                         projections=DIFFERENCE_PROJECTIONS, **overrides)


def test_the_DIFFERENCE_subtracts_the_subtrahend_from_the_minuend(ratio, ratio_feature, lock_tree):
    node = _render_difference(ratio, ratio_feature)
    values = _ratio_values(node, lock_tree, spine=_spine_rows("c1"),
                           numerator=[_ratio_txn("c1", 90)], denominator=[_ratio_txn("c1", 40)])
    assert values == {"c1": decimal.Decimal("50.000000")}
    assert node.inputs[:2] == ("intermediate_diff__body_minuend",
                               "intermediate_diff__body_subtrahend")


def test_the_DIFFERENCE_answers_an_EMPTY_side_from_that_SIDES_own_policy(ratio, ratio_feature,
                                                                         lock_tree):
    """What a difference means when one side is an empty window is not this renderer's question.
    Under `null` the difference is NULL; under `zero` the same rows give `0 - 40 = -40`. Both are
    the declaration's answer, and picking either as a default would publish it as the formula's."""
    absent_minuend = {"numerator": [], "denominator": [_ratio_txn("c1", 40)]}
    propagated = _render_difference(ratio, ratio_feature)
    assert _ratio_values(propagated, lock_tree, spine=_spine_rows("c1"), **absent_minuend) == {
        "c1": None}

    zeroed = _render_difference(ratio, ratio_feature, empty_window={
        "body.minuend": EmptyWindowResult.ZERO, "body.subtrahend": EmptyWindowResult.NULL})
    assert _ratio_values(zeroed, lock_tree, spine=_spine_rows("c1"), **absent_minuend) == {
        "c1": decimal.Decimal("-40.000000")}


def test_the_DIFFERENCE_does_not_COALESCE_either_side(ratio, ratio_feature):
    """The plausible shortcut, named. `F.coalesce` anywhere in the final operation would turn every
    `null` empty-window declaration into a `zero` one, silently and for both halves at once."""
    source = _render_difference(ratio, ratio_feature).source
    final = source.split("The DIFFERENCE.")[1]
    assert "F.coalesce" not in final


# ── refusals decidable at RENDER time ────────────────────────────────────────────────────────────


def test_it_refuses_a_body_path_with_NO_declared_policy_or_NO_projection(ratio, ratio_feature):
    """A path with nothing declared for it would be rendered under a default, and every default
    here is this renderer answering a question §8 rule 4 assigns to the formula."""
    for missing in ("empty_window", "null_input"):
        policy = {EmptyWindowResult.NULL if missing == "empty_window" else NullInput.IGNORE}
        with pytest.raises(ValueError, match=f"{missing} for"):
            _render_ratio(ratio, ratio_feature,
                          **{missing: {NUMERATOR_PATH: policy.pop()}})
    with pytest.raises(ValueError, match="projection_datasets for"):
        _render_ratio(ratio, ratio_feature,
                      projections={NUMERATOR_PATH: RATIO_PROJECTIONS[NUMERATOR_PATH]})
    with pytest.raises(ValueError, match="projection_datasets for"):
        _render_ratio(ratio, ratio_feature,
                      projections={**RATIO_PROJECTIONS, "body.expr": "intermediate_extra"})


def test_it_refuses_a_policy_that_is_NOT_a_mapping_over_body_paths(ratio, ratio_feature):
    """One value shared between the operands would apply the numerator's declaration to the
    denominator — a ratio may legitimately declare `zero` for one and `null` for the other."""
    with pytest.raises(TypeError, match="mapping from body path"):
        nodes_compute.render_calculation_node(
            ratio[0].irs[0], ratio_feature, ratio[1],
            empty_window=EmptyWindowResult.ZERO,          # the scalar 13c took, deliberately
            null_input=dict.fromkeys(RATIO_PROJECTIONS, NullInput.IGNORE),
            projection_datasets=RATIO_PROJECTIONS, spine_dataset="primary_spine",
            staging_dataset=RATIO_STAGING, manifest_dataset=RATIO_MANIFEST)


def test_it_refuses_a_RATIO_with_no_zero_denominator_and_a_NON_ratio_with_one(ratio,
                                                                             ratio_feature):
    ir = dataclasses.replace(ratio[0].irs[0], zero_denominator=None)
    with pytest.raises(ValueError, match="carries no zero_denominator"):
        _render_ratio(ratio, ratio_feature, ir=ir)
    difference = dataclasses.replace(_difference(ratio), zero_denominator=ZeroDenominator.NULL)
    with pytest.raises(ValueError, match="no division in that body"):
        _render_ratio(ratio, ratio_feature, ir=difference,
                      projections=DIFFERENCE_PROJECTIONS)


def test_it_refuses_a_RATIO_whose_operands_declare_DIFFERENT_grains(ratio, ratio_feature):
    """One groupBy serves both aggregates. An operand grouped by something else would land its
    values against another entity's key, which is a full column of plausible numbers."""
    ir = ratio[0].irs[0]
    elsewhere = dataclasses.replace(ir.expressions[1], physical_read_set=tuple(
        ref for ref in ir.expressions[1].physical_read_set if ref.column != "cif_id"))
    with pytest.raises(ValueError, match="authorized read set"):
        _render_ratio(ratio, ratio_feature,
                      ir=dataclasses.replace(ir, expressions=(ir.expressions[0], elsewhere)))


# ── the node inside a real project ───────────────────────────────────────────────────────────────


def test_the_RATIO_closes_the_wiring_of_a_REAL_rendered_project(ratio):
    """The whole of A.18's first row, end to end: the worked ratio now has a calculation node, and
    Task 12's six wiring checks accept it — two projection inputs and all."""
    datasets = project_datasets(ratio[0], ratio[1], spine_input=ratio[3])
    project = render_project(
        ratio[0], ratio[1], environment_id=ENVIRONMENT,
        engine_versions=fixtures.ENGINE_VERSIONS, spine_input=ratio[3],
        nodes=_wired_nodes(ratio, datasets))
    nodes_py = next(text for path, text in project.files.items()
                    if path.endswith("pipelines/materialize/nodes.py"))
    assert f"def {nodes_compute.calculation_func_name(RATIO_90D)}(" in nodes_py
    assert ast.parse(nodes_py)


# ── goldens: a change detector, and the weakest test here ────────────────────────────────────────


RATIO_VARIANTS = {
    "ratio_zero_denominator_null": {"zero_denominator": ZeroDenominator.NULL},
    "ratio_zero_denominator_zero": {"zero_denominator": ZeroDenominator.ZERO},
    "ratio_zero_denominator_error": {"zero_denominator": ZeroDenominator.ERROR},
    "ratio_per_operand_empty_windows": {
        "zero_denominator": ZeroDenominator.ZERO,
        "empty_window": {NUMERATOR_PATH: EmptyWindowResult.NULL,
                         DENOMINATOR_PATH: EmptyWindowResult.ZERO},
        "null_input": {NUMERATOR_PATH: NullInput.PROPAGATE,
                       DENOMINATOR_PATH: NullInput.IGNORE}},
}


@pytest.mark.parametrize("variant", sorted(RATIO_VARIANTS))
def test_the_rendered_ratio_matches_its_golden(ratio, ratio_feature, variant):
    """Goldens prove STABILITY, never correctness — every property above is asserted on its own."""
    golden = CALCULATION_GOLDENS / f"{variant}.py"
    rendered = _render_ratio(ratio, ratio_feature, **RATIO_VARIANTS[variant]).source
    if not golden.exists():  # pragma: no cover — first run only
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered, encoding="utf-8")
    assert rendered == golden.read_text(encoding="utf-8")


def test_the_rendered_difference_matches_its_golden(ratio, ratio_feature):
    golden = CALCULATION_GOLDENS / "difference_decimal.py"
    rendered = _render_difference(ratio, ratio_feature).source
    if not golden.exists():  # pragma: no cover — first run only
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered, encoding="utf-8")
    assert rendered == golden.read_text(encoding="utf-8")


def test_every_rendered_FINAL_OPERATION_line_fits_the_width_it_states(ratio, ratio_feature):
    for variant in RATIO_VARIANTS.values():
        for line in _render_ratio(ratio, ratio_feature, **variant).source.splitlines():
            assert len(line) <= 100, line
    for line in _render_difference(ratio, ratio_feature).source.splitlines():
        assert len(line) <= 100, line


# ══ Task 13e — the governed multi-hop traversal (§3.1–3.2) ═══════════════════════════════════════
#
# The shape A.17 names, and the normal shape in a bank: a feature grained on the CUSTOMER, computed
# over TRANSACTIONS, whose rows reach their customer through the ACCOUNT. Every hop fans IN toward
# the destination (N:1) because that is the only kind `plan_join` returns — a fanning path is
# refused there, never repaired here.
#
# The failure this section is written against is not an exception. A hop rendered backwards still
# joins and still produces rows; an inner join silently drops the source rows whose dimension row is
# missing. Both leave a full table of plausible numbers. So the traversal is proved by EXECUTION —
# one row per source row, each carrying the entity it actually belongs to.

TRAVERSAL_GOLDENS = pathlib.Path(__file__).parent / "goldens" / "traversal_nodes"

_ACCOUNTS = "accounts"
_CUSTOMERS = "customers"

#: `transactions -> accounts -> customers`. The two hops join on DIFFERENT column names on each
#: side (`acct_num`/`acct_id`, `owner_id`/`cif_id`), so a renderer that read the endpoints in the
#: wrong order names a column that is not there rather than silently joining on the right one.
HOP_1 = JoinStep(from_ref="public.transactions.acct_num", to_ref="public.accounts.acct_id",
                 cardinality="N:1", approved_join_fact_key="ajf-txn-acct",
                 approved_join_status="VERIFIED", authority="operational")
HOP_2 = JoinStep(from_ref="public.accounts.owner_id", to_ref="public.customers.cif_id",
                 cardinality="N:1", approved_join_fact_key=None,
                 approved_join_status=None, authority="operational")

TRAVERSAL_DATASETS = {"banking.accounts": "raw_banking__accounts",
                      "banking.customers": "raw_banking__customers"}


def _pref(table, column, *roles):
    """One PhysicalRef in the shape Task 5 resolves: a schema-flattened logical ref, and the
    physical `banking.<table>` the metastore answers to."""
    from featuregen.materialize.expression_ir import PhysicalRef
    ref = f"{fixtures.SOURCE}::public.{table}" + (f".{column}" if column else "")
    return PhysicalRef(logical_ref=ref, catalog_source=fixtures.SOURCE, schema="banking",
                       table=table, column=column, roles=tuple(roles))


#: Every endpoint the two hops travel on, as §1.3 authorized them: `(table, column, *roles)`.
TWO_HOP_READS = (
    (fixtures.TABLE, "acct_num", RefRole.JOIN_KEY),
    (_ACCOUNTS, "acct_id", RefRole.JOIN_KEY),
    (_ACCOUNTS, "owner_id", RefRole.JOIN_KEY),
    (_CUSTOMERS, "cif_id", RefRole.JOIN_KEY, RefRole.GRAIN_KEY),
)

#: The shorter shape, and the commoner one: the grain sits on the table directly joined.
ONE_HOP = (JoinStep(from_ref="public.transactions.cif_num", to_ref="public.customers.cif_id",
                    cardinality="N:1", approved_join_fact_key="ajf-txn-cust",
                    approved_join_status="VERIFIED", authority="operational"),)
ONE_HOP_READS = (
    (fixtures.TABLE, "cif_num", RefRole.JOIN_KEY),
    (_CUSTOMERS, "cif_id", RefRole.JOIN_KEY, RefRole.GRAIN_KEY),
)


def _traversed(expression, *, steps=(HOP_1, HOP_2), reads=TWO_HOP_READS, **plan_overrides):
    """The worked expression re-grained onto `customers.cif_id`, `steps` hops away.

    Everything else is the real compilation: the same operand, the same governed filter, the same
    window and the same availability column. Only the grain moves off the source relation — which
    is the ONLY reason `_plan_to_grain` ever produces a hop.
    """
    kept = tuple(ref for ref in expression.physical_read_set
                 if not (ref.column == "cif_id" and ref.table == fixtures.TABLE))
    read_set = (*kept, *(_pref(*read) for read in reads))
    return dataclasses.replace(
        expression, physical_read_set=read_set,
        join_plan=dataclasses.replace(expression.join_plan, steps=tuple(steps), **plan_overrides))


def _traversal(compiled, expression, *, joined_datasets=None, **kwargs):
    return nodes_compute.render_projection_node(
        _traversed(expression, **kwargs), compiled[2], feature_column=SUM_30D,
        source_dataset="raw_banking__transactions",
        joined_datasets=TRAVERSAL_DATASETS if joined_datasets is None else joined_datasets,
        projection_dataset="intermediate_total_debit_amount_30d__body_expr")


#: Two customers, two accounts, and a transaction on an account NOBODY owns. The orphan is the whole
#: point: it is what tells a LEFT traversal from an INNER one, and an inner one silently changes
#: which source rows the feature is computed over.
ACCOUNTS = [{"acct_id": "A1", "owner_id": "C1"}, {"acct_id": "A2", "owner_id": "C2"}]
CUSTOMERS_ROWS = [{"cif_id": "C1"}, {"cif_id": "C2"}]


def _traverse(node, txns, *, accounts=None, customers=None, business_dt=BUSINESS_DT):
    run = fake_spark.run_rendered(node.source, node.func_name)
    return run(fake_spark.DataFrame(txns),
               fake_spark.DataFrame(ACCOUNTS if accounts is None else accounts),
               fake_spark.DataFrame(CUSTOMERS_ROWS if customers is None else customers),
               business_dt)


def test_the_traversal_lands_every_source_row_on_the_entity_it_ACTUALLY_belongs_to(
        compiled, expression):
    """The number test. A hop rendered backwards, or joined on the wrong endpoint, still produces
    rows — it produces them under the wrong customer, and nothing downstream would say so."""
    node = _traversal(compiled, expression)
    rows = _traverse(node, [
        _windowed(BEFORE, 10) | {"acct_num": "A1"},
        _windowed(BEFORE, 20) | {"acct_num": "A2"},
        _windowed(BEFORE, 30) | {"acct_num": "A1"},
    ]).rows
    assert [(row["txn_amt"], row["cif_id"]) for row in rows] == [
        (10, "C1"), (20, "C2"), (30, "C1")]


def test_the_traversal_produces_EXACTLY_ONE_ROW_PER_SOURCE_ROW(compiled, expression):
    """`JoinPlan.fans_out` is False by construction, so a rendered traversal that multiplied rows
    would be inflating every SUM over it. Counted, because a wrong SUM raises nothing."""
    node = _traversal(compiled, expression)
    txns = [_windowed(BEFORE, n) | {"acct_num": acct}
            for n, acct in ((10, "A1"), (20, "A2"), (30, "A1"), (40, "A1"))]
    assert _traverse(node, txns).count() == len(txns)
    # The count above passed THROUGH every hop's uniqueness gate: unique keys raise nothing, and
    # each gate sits BEFORE its join so a refusal names the offending table, not the grown rows.
    assert node.source.index("duplicate_1") < node.source.index("rows.join(")
    assert "duplicate_2" in node.source


def test_a_DUPLICATED_DIMENSION_KEY_refuses_loudly_instead_of_doubling_every_SUM(
        compiled, expression):
    """A double-loaded dimension row is the classic silent SUM-doubler. The declared N:1 is
    metadata about the catalog; this gate is the first check against the data itself — and it runs
    BEFORE the join, so the refusal names the table that fanned, not the rows that grew."""
    node = _traversal(compiled, expression)
    with pytest.raises(RuntimeError) as caught:
        _traverse(node, [_windowed(BEFORE, 10) | {"acct_num": "A1"}],
                  accounts=[*ACCOUNTS, dict(ACCOUNTS[0])])          # the same acct_id, twice
    assert str(caught.value).startswith(ValidationGateCode.JOIN_AMPLIFICATION.value)
    assert "banking.accounts" in str(caught.value) and "hop 1" in str(caught.value)


def test_a_duplicated_key_on_the_SECOND_hop_is_caught_at_THAT_hop_and_names_THAT_table(
        compiled, expression):
    """Every hop carries the gate, not just the first: a duplicate two tables away multiplies rows
    exactly as silently, and a gate that only guarded hop 1 would let it through."""
    node = _traversal(compiled, expression)
    with pytest.raises(RuntimeError) as caught:
        _traverse(node, [_windowed(BEFORE, 10) | {"acct_num": "A1"}],
                  customers=[*CUSTOMERS_ROWS, dict(CUSTOMERS_ROWS[0])])   # the same cif_id, twice
    assert str(caught.value).startswith(ValidationGateCode.JOIN_AMPLIFICATION.value)
    assert "banking.customers" in str(caught.value) and "hop 2" in str(caught.value)


def test_a_source_row_whose_DIMENSION_ROW_IS_MISSING_survives_with_a_NULL_key(
        compiled, expression):
    """The join type, made observable. An INNER traversal drops this row — which changes the rows
    the feature is computed over on a referential-integrity fact nobody governed. A LEFT one keeps
    it with a null entity key, and §8 rule 3's spine reduction then never lands it on anybody."""
    node = _traversal(compiled, expression)
    rows = _traverse(node, [
        _windowed(BEFORE, 10) | {"acct_num": "A1"},
        _windowed(BEFORE, 99) | {"acct_num": "A404"},      # an account that does not exist
    ]).rows
    assert len(rows) == 2
    assert [(row["txn_amt"], row["cif_id"]) for row in rows] == [(10, "C1"), (99, None)]


def test_a_hop_rendered_BACKWARDS_refuses_rather_than_joining(compiled, expression):
    """`JoinStep` is oriented to the direction of travel. Replayed in the other order, hop 2 starts
    at a table the frame has not reached — and rendered anyway it would land rows on the wrong
    entities while producing a perfectly plausible column."""
    with pytest.raises(ValueError, match="has not reached it"):
        _traversal(compiled, expression, steps=(HOP_2, HOP_1))


def test_a_traversal_that_REVISITS_A_TABLE_refuses(compiled, expression):
    back = JoinStep(from_ref="public.customers.cif_id", to_ref="public.accounts.acct_id",
                    cardinality="1:1", authority="operational")
    with pytest.raises(ValueError, match="already reached"):
        _traversal(compiled, expression, steps=(HOP_1, HOP_2, back))


def test_a_DISPLAY_ONLY_hop_refuses(compiled, expression):
    """`authority` is `operational` (usable for feature construction) vs `display_only` (an
    ungoverned edge kept for lineage display). Computing over the second produces a number whose
    path nobody governed, and it looks exactly like a governed one."""
    ungoverned = dataclasses.replace(HOP_2, authority="display_only")
    with pytest.raises(ValueError, match="display_only"):
        _traversal(compiled, expression, steps=(HOP_1, ungoverned))


def test_a_hop_whose_APPROVED_JOIN_FACT_IS_NOT_VERIFIED_refuses(compiled, expression):
    """A fact that exists and is not VERIFIED is a join somebody proposed. `None` is different and
    is NOT covered — a file-declared edge is a meaningful answer, not a missing one."""
    proposed = dataclasses.replace(HOP_1, approved_join_status="DRAFT")
    with pytest.raises(ValueError, match="not.*VERIFIED|VERIFIED"):
        _traversal(compiled, expression, steps=(proposed, HOP_2))
    assert HOP_2.approved_join_fact_key is None                 # file-declared, and it renders
    assert _traversal(compiled, expression).source


@pytest.mark.parametrize("cardinality", ["1:N", None, "M:N"])
def test_a_hop_that_FANS_OUT_OR_IS_UNATTESTED_refuses(compiled, expression, cardinality):
    """Refusing only the hops that SAY 1:N is a fail-open: an unattested edge may BE 1:N. 'We do
    not know' is not 'it is safe' — and neither is repaired here, because de-duplicating a fanned
    join picks a business allocation rule nobody approved (§3.2)."""
    fanning = dataclasses.replace(HOP_2, cardinality=cardinality)
    with pytest.raises(ValueError, match="cardinality"):
        _traversal(compiled, expression, steps=(HOP_1, fanning))


def test_a_plan_that_REPORTS_FANS_OUT_refuses_even_though_every_hop_fans_in(compiled, expression):
    """`fans_out` is READ, not inferred from the absence of a warning — which is what `JoinPlan`'s
    own docstring asks a consumer to do. Every hop here is N:1, so a renderer that inferred the
    answer from the steps would render this plan and multiply nothing it could see."""
    with pytest.raises(ValueError, match="fans_out"):
        _traversal(compiled, expression, fans_out=True)


def test_a_plan_whose_GOVERNED_OUTCOME_IS_NOT_OPERATIONAL_refuses(compiled, expression):
    with pytest.raises(ValueError, match="outcome"):
        _traversal(compiled, expression, outcome_kind="UNVERIFIED")


def test_a_hop_ENDPOINT_OUTSIDE_THE_AUTHORIZED_READ_SET_refuses(compiled, expression):
    """§1.3 records every join endpoint as its own read. An endpoint missing from the read set is a
    column the group would join on and was never granted."""
    elsewhere = dataclasses.replace(HOP_1, to_ref="public.accounts.branch_cd")
    with pytest.raises(ValueError, match="authorized read set does not carry"):
        _traversal(compiled, expression, steps=(elsewhere, HOP_2))


def test_a_read_set_table_NO_HOP_REACHES_refuses(compiled, expression):
    """The zero-hop case of the same rule, and 13b's original refusal: a node handed no dataset for
    a table cannot name its columns."""
    with pytest.raises(ValueError, match="spans"):
        _traversal(compiled, expression, steps=(HOP_1,))


def test_a_traversal_with_NO_GRAIN_KEY_AT_THE_END_refuses(compiled, expression):
    """A traversal exists to reach the grain. One that emits nothing joins the tables and throws
    the answer away — while costing exactly as much and looking exactly as governed."""
    ungrained = dataclasses.replace(
        _traversed(expression),
        physical_read_set=tuple(
            dataclasses.replace(ref, roles=tuple(r for r in ref.roles if r is not RefRole.GRAIN_KEY))
            for ref in _traversed(expression).physical_read_set))
    with pytest.raises(ValueError, match="no grain key is reached"):
        nodes_compute.render_projection_node(
            ungrained, compiled[2], feature_column=SUM_30D,
            source_dataset="raw_banking__transactions", joined_datasets=TRAVERSAL_DATASETS,
            projection_dataset="intermediate")


def test_a_grain_key_the_SOURCE_ALSO_SPELLS_refuses(compiled, expression):
    """Two spellings of one entity and nothing declaring the mapping between them (A.18's first
    row, reached the other way): choosing one here lands every entity's value under a key this
    compilation picked."""
    both = dataclasses.replace(
        _traversed(expression),
        physical_read_set=(*_traversed(expression).physical_read_set,
                           _pref(fixtures.TABLE, "cif_id", RefRole.FILTER_LEFT)))
    with pytest.raises(ValueError, match="already carries a column of that name"):
        nodes_compute.render_projection_node(
            both, compiled[2], feature_column=SUM_30D,
            source_dataset="raw_banking__transactions", joined_datasets=TRAVERSAL_DATASETS,
            projection_dataset="intermediate")


def test_the_hop_DATASETS_must_be_exactly_the_tables_the_traversal_reaches(compiled, expression):
    """Kedro binds inputs positionally, so a dataset supplied against the wrong hop reads the wrong
    table through a name that parses perfectly."""
    with pytest.raises(ValueError, match="missing"):
        _traversal(compiled, expression, joined_datasets={"banking.accounts": "raw_a"})
    with pytest.raises(ValueError, match="unexpected"):
        _traversal(compiled, expression,
                   joined_datasets={**TRAVERSAL_DATASETS, "banking.branches": "raw_b"})
    with pytest.raises(ValueError, match="missing"):
        _traversal(compiled, expression, joined_datasets={})


# ── the wiring, and what a reviewer counts it against ────────────────────────────────────────────


def test_the_traversal_wires_ONE_INPUT_PER_JOINED_TABLE_in_traversal_order(compiled, expression):
    node = _traversal(compiled, expression)
    assert node.inputs == ("raw_banking__transactions", "raw_banking__accounts",
                           "raw_banking__customers", "params:business_dt")
    assert node.outputs == ("intermediate_total_debit_amount_30d__body_expr",)


def test_the_rendered_SIGNATURE_matches_the_wiring_position_for_position(compiled, expression):
    """The one defect a parse check cannot see: Kedro binds positionally, so a signature and a
    wiring that disagree produce a pipeline that runs and computes the wrong thing."""
    node = _traversal(compiled, expression)
    module = ast.parse(node.source)
    parameters = [arg.arg for arg in module.body[0].args.args]
    assert parameters == ["source", "join_1_accounts", "join_2_customers", "business_dt"]
    assert len(parameters) == len(node.inputs)


def test_the_traversal_emits_NO_ROW_COLLAPSING_REPAIR(compiled, expression):
    """A `dropDuplicates` here would hide a fan-out instead of reporting it, and a hidden fan-out is
    a SUM wrong by a factor nobody can see. Asserted on the text AND by execution: the stand-in
    raises when either is called, so a renderer that emitted one dies in every test above."""
    source = _traversal(compiled, expression).source
    assert "dropDuplicates" not in source
    assert "distinct" not in source


def test_the_traversal_never_JOINS_BEFORE_THE_PIT_GATES(compiled, expression):
    """The gates read the source relation's own columns, so they are rendered first — on the rows
    the feature may see, not on the joined product of them."""
    source = _traversal(compiled, expression)
    assert source.source.index("rows.where(") < source.source.index("rows.join(")


def test_every_rendered_TRAVERSAL_line_fits_the_width_it_states(compiled, expression):
    for line in _traversal(compiled, expression).source.splitlines():
        assert len(line) <= 100, line


def test_the_traversal_renders_identically_every_time(compiled, expression):
    first, second = _traversal(compiled, expression), _traversal(compiled, expression)
    assert ast.parse(first.source)
    assert first.source == second.source and first.inputs == second.inputs


# ── through the calculation, and through Task 12's wiring ────────────────────────────────────────


def _traversed_ir(compiled, expression, **kwargs):
    """The worked feature re-grained onto the customer two hops away — IR and all."""
    return dataclasses.replace(
        compiled[0].irs[0], expressions=(_traversed(expression, **kwargs),),
        grain_keys=(f"{fixtures.SOURCE}::public.customers.cif_id",))


def test_the_CALCULATION_accepts_a_multi_hop_projection_and_sums_per_ENTITY(
        compiled, expression, feature, lock_tree):
    """End to end, by execution: the traversal's own output frame is what the calculation reads.
    A hop landed on the wrong customer produces the same shape and different numbers, so the
    assertion is on the numbers."""
    projection = _traversal(compiled, expression)
    staged_rows = _traverse(projection, [
        _windowed(BEFORE, 10) | {"acct_num": "A1"},
        _windowed(BEFORE, 20) | {"acct_num": "A2"},
        _windowed(BEFORE, 30) | {"acct_num": "A1"},
        _windowed(BEFORE, 99) | {"acct_num": "A404"},          # reaches no customer at all
    ]).rows

    node = _calculate(compiled, feature, ir=_traversed_ir(compiled, expression))
    assert _staged(node, lock_tree, projection=staged_rows,
                   spine=_spine_rows("C1", "C2")) == {"C1": 40, "C2": 20}


def test_the_ORPHANED_ROW_lands_on_no_entity_rather_than_on_the_wrong_one(
        compiled, expression, feature, lock_tree):
    """The other half of the LEFT decision. The row survives the projection with a null key, and
    §8 rule 3's reduction then never joins it to anybody — so it changes no entity's number."""
    projection = _traversal(compiled, expression)
    orphan_only = _traverse(projection, [_windowed(BEFORE, 99) | {"acct_num": "A404"}]).rows
    assert orphan_only[0]["cif_id"] is None

    node = _calculate(compiled, feature, ir=_traversed_ir(compiled, expression))
    assert _staged(node, lock_tree, projection=orphan_only,
                   spine=_spine_rows("C1", "C2")) == {"C1": None, "C2": None}


def _traversed_compilation(compiled, expression):
    """The whole authorized compilation re-grained two hops away, physical requirements included."""
    source_requirement = compiled[0].irs[0].expressions[0].input_requirements[0]
    ir = _traversed_ir(compiled, expression)
    expressions = (dataclasses.replace(
        ir.expressions[0],
        input_requirements=(source_requirement,
                            dataclasses.replace(source_requirement, table=_ACCOUNTS),
                            dataclasses.replace(source_requirement, table=_CUSTOMERS))),)
    authorized = dataclasses.replace(
        compiled[0], irs=(dataclasses.replace(ir, expressions=expressions),))
    plan = dataclasses.replace(compiled[1], features=tuple(
        dataclasses.replace(item, ir_hash=ir_hash(authorized.irs[0]))
        if item.column_name == ir.feature_name else item
        for item in compiled[1].features))
    return authorized, plan


def test_the_TRAVERSAL_CLOSES_TASK_12s_WIRING_with_one_raw_dataset_per_hop(compiled, expression):
    """The evidence that decided WHICH node renders the traversal. `project_datasets` already
    declares one `raw` dataset per table an expression's `input_requirements` cover — the hops
    included — and `_check_wiring` refuses a governed source that no node reads. The projection is
    the node that reads raw datasets, so it is the node that must read them."""
    authorized, plan = _traversed_compilation(compiled, expression)
    datasets = project_datasets(authorized, plan, spine_input=compiled[3])
    assert datasets.raw["banking.accounts"] == "raw_banking__accounts"
    assert datasets.raw["banking.customers"] == "raw_banking__customers"

    node = _traversal(compiled, expression)
    with pytest.raises(ValueError, match="read by no node"):
        render_project(authorized, plan, environment_id=ENVIRONMENT,
                       engine_versions=fixtures.ENGINE_VERSIONS, spine_input=compiled[3],
                       nodes=(*_wired_nodes(compiled, datasets)[:1],
                              dataclasses.replace(node, inputs=("raw_banking__transactions",
                                                                "params:business_dt")),
                              *_wired_nodes(compiled, datasets)[2:]))


def test_a_REAL_PROJECT_renders_with_the_traversal_wired_into_it(compiled, expression, tmp_path):
    """Task 12 checks six ways a pipeline can be wrong while parsing perfectly. The traversal's
    three inputs are checked by THAT, not by a claim here."""
    authorized, plan = _traversed_compilation(compiled, expression)
    datasets = project_datasets(authorized, plan, spine_input=compiled[3])
    ir = authorized.irs[0]
    planned = next(item for item in plan.features if item.column_name == ir.feature_name)
    nodes = [
        render_spine_node(authorized.spine, plan, compiled[2], spine_input=compiled[3],
                          source_dataset=datasets.raw["banking.customers"],
                          spine_dataset=datasets.spine),
        nodes_compute.render_projection_node(
            ir.expressions[0], compiled[2], feature_column=ir.feature_name,
            source_dataset=datasets.raw["banking.transactions"],
            joined_datasets={"banking.accounts": datasets.raw["banking.accounts"],
                             "banking.customers": datasets.raw["banking.customers"]},
            projection_dataset=datasets.projections[(ir.feature_name, "body.expr")]),
        nodes_compute.render_calculation_node(
            ir, planned, plan, empty_window={"body.expr": EmptyWindowResult.NULL},
            null_input={"body.expr": NullInput.IGNORE},
            projection_datasets={"body.expr": datasets.projections[(ir.feature_name, "body.expr")]},
            spine_dataset=datasets.spine, staging_dataset=datasets.staging[ir.feature_name],
            manifest_dataset=datasets.manifests[ir.feature_name]),
        _assembly_stub(datasets),
    ]
    project = render_project(authorized, plan, environment_id=ENVIRONMENT,
                             engine_versions=fixtures.ENGINE_VERSIONS, spine_input=compiled[3],
                             nodes=tuple(nodes))
    nodes_py = next(text for path, text in project.files.items()
                    if path.endswith("pipelines/materialize/nodes.py"))
    assert ast.parse(nodes_py)
    pipeline = next(text for path, text in project.files.items()
                    if path.endswith("pipelines/materialize/pipeline.py"))
    assert "raw_banking__accounts" in pipeline and "raw_banking__customers" in pipeline
    assert materialize_to(project, tmp_path / "generated")


def test_the_stand_ins_join_NEVER_MATCHES_A_NULL_KEY_to_a_null_key():
    """Spark's equi-join never matches NULL to NULL, itself included. A stand-in that did would
    attribute every unreachable source row to whichever dimension row happened to carry a null key
    — and the traversal tests above would agree with that answer."""
    left = fake_spark.DataFrame([{"k": "A1", "v": 1}, {"k": None, "v": 2}])
    right = fake_spark.DataFrame([{"k": "A1", "owner": "C1"}, {"k": None, "owner": "C9"}])
    joined = left.join(right, on=["k"], how="left")
    assert [row["owner"] for row in joined.rows] == ["C1", None]
    assert left.join(right, on=["k"], how="inner").count() == 1


# ── one hop: the grain sits on the table directly joined ─────────────────────────────────────────


def _one_hop(compiled, expression):
    return nodes_compute.render_projection_node(
        _traversed(expression, steps=ONE_HOP, reads=ONE_HOP_READS), compiled[2],
        feature_column=SUM_30D, source_dataset="raw_banking__transactions",
        joined_datasets={"banking.customers": "raw_banking__customers"},
        projection_dataset="intermediate_total_debit_amount_30d__body_expr")


def test_a_SINGLE_HOP_traversal_reaches_the_grain_on_the_table_it_joins(compiled, expression):
    node = _one_hop(compiled, expression)
    assert node.inputs == ("raw_banking__transactions", "raw_banking__customers",
                           "params:business_dt")
    run = fake_spark.run_rendered(node.source, node.func_name)
    rows = run(fake_spark.DataFrame([_windowed(BEFORE, 10) | {"cif_num": "C1"},
                                     _windowed(BEFORE, 20) | {"cif_num": "C2"},
                                     _windowed(BEFORE, 30) | {"cif_num": "C404"}]),
               fake_spark.DataFrame(CUSTOMERS_ROWS), BUSINESS_DT).rows
    assert [(row["txn_amt"], row["cif_id"]) for row in rows] == [
        (10, "C1"), (20, "C2"), (30, None)]
    assert sorted(rows[0]) == ["cif_id", "dr_cr_flag", "cif_num", "posted_ts", "status_cd",
                               "txn_amt", "txn_dt"][:0] + sorted(
        ["cif_id", "cif_num", "dr_cr_flag", "posted_ts", "status_cd", "txn_amt", "txn_dt"])


def test_the_traversal_hands_on_the_SOURCE_COLUMNS_AND_THE_GRAIN_KEY_and_nothing_else(
        compiled, expression):
    """The hops' own join keys stop at the projection: they were read to travel on, and two tables
    on one path spell one entity's key the same way — `accounts.cif_id` and `customers.cif_id`
    would be one ambiguous output column, not two."""
    rows = _traverse(_traversal(compiled, expression),
                     [_windowed(BEFORE, 10) | {"acct_num": "A1", "pan": "4111"}]).rows
    assert sorted(rows[0]) == ["acct_num", "cif_id", "dr_cr_flag", "posted_ts", "status_cd",
                               "txn_amt", "txn_dt"]


# ── goldens: a change detector, and the weakest test here ────────────────────────────────────────


TRAVERSAL_VARIANTS = {"one_hop": _one_hop, "two_hop": _traversal}


@pytest.mark.parametrize("variant", sorted(TRAVERSAL_VARIANTS))
def test_the_rendered_traversal_matches_its_golden(compiled, expression, variant):
    """Goldens prove STABILITY, never correctness — every property above is asserted on its own."""
    golden = TRAVERSAL_GOLDENS / f"{variant}.py"
    rendered = TRAVERSAL_VARIANTS[variant](compiled, expression).source
    if not golden.exists():  # pragma: no cover — first run only
        golden.parent.mkdir(parents=True, exist_ok=True)
        golden.write_text(rendered, encoding="utf-8")
    assert rendered == golden.read_text(encoding="utf-8")


def test_a_row_that_reaches_a_DANGLING_DIMENSION_lands_on_no_entity_not_on_the_dangling_id(
        compiled, expression):
    """Why each hop's key is dropped rather than carried out as the answer. Spark's name-form join
    keeps ONE key column and it holds the LEFT side's value — so an account owned by a customer who
    is not in the customer table would come out claiming that customer, and the feature would land
    a value under an entity the population has never heard of."""
    node = _traversal(compiled, expression)
    rows = _traverse(node, [_windowed(BEFORE, 55) | {"acct_num": "A3"}],
                     accounts=[*ACCOUNTS, {"acct_id": "A3", "owner_id": "C9"}]).rows
    assert [(row["txn_amt"], row["cif_id"]) for row in rows] == [(55, None)]


def test_the_rendered_traversal_states_the_ROLES_the_path_was_planned_under(compiled, expression):
    """`roles_used` is load-bearing, not decoration: roles decide whether a hop is DENIED, so the
    same catalog yields a different path for a different reader — and a rendered artifact that did
    not say which reader's path it computed could not be re-checked against the catalog."""
    node = _traversal(compiled, expression)
    roles = _traversed(expression).join_plan.roles_used
    assert roles, "the worked compilation plans under at least one role"
    for role in roles:
        assert role in node.source
    empty = dataclasses.replace(_traversed(expression),
                                join_plan=dataclasses.replace(
                                    _traversed(expression).join_plan, roles_used=()))
    rendered = nodes_compute.render_projection_node(
        empty, compiled[2], feature_column=SUM_30D,
        source_dataset="raw_banking__transactions", joined_datasets=TRAVERSAL_DATASETS,
        projection_dataset="intermediate").source
    assert "an EMPTY read scope" in rendered
