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
import inspect
import pathlib

import pytest
from tests.featuregen.materialize import fake_spark, fixtures
from tests.featuregen.materialize.test_group_plan import (  # noqa: F401 — `catalog` is a fixture
    BUSINESS_DT,
    CADENCE,
    SUM_30D,
    catalog,
)
from tests.featuregen.materialize.test_ir import (
    CUSTOMERS,
    CUSTOMERS_ASOF,
    CUSTOMERS_STATUS,
)
from tests.featuregen.materialize.test_render_project import ENVIRONMENT, _compiled

from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    ContractGroup,
    derive_group_contract,
)
from featuregen.materialize.inventory import (
    EventTimePartition,
    FullScan,
    PartitionTransform,
    StaticSnapshot,
)
from featuregen.materialize.render import nodes_compute
from featuregen.materialize.render.nodes_compute import SPINE_FUNC_NAME, render_spine_node
from featuregen.materialize.render.project import (
    RenderedNode,
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
