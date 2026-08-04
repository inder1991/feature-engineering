"""Phase G T4 — ``compile/wiring.py``: the Kedro node sequence, join gates included.

**The cross-catalog case is the point of this file.** Nothing in ``src/`` had ever assembled a node
sequence before this task: the only reference wiring was a test fixture
(``test_render_nodes_compute._wired_nodes``) and it omits the join gate entirely, so no full
cross-catalog project had ever been assembled by anything. The fixture below therefore compiles a
REAL cross-catalog group — a real ``CrossCatalogJoinStepV1`` produced by ``compile_ir`` from a real
executable bridge realization — and asserts on what the assembly wires it to.

**Why the spine is ``banking.customers`` and the grain is on ``crm``.** The three tables have to be
distinct for the test to discriminate: the projection's SOURCE (``banking.transactions``), the
bridge TARGET (``crm_banking.customer_master``) and the SPINE (``banking.customers``). If the spine
were the bridge target, its raw dataset would be read by the spine node anyway and
``_check_wiring``'s "every governed source is read" rule would be satisfied whether or not the gate
existed — the assertion would pass for the wrong reason.

The grain key column must also differ from every column of the source relation: ``_projection_plan``
refuses a grain key reached through a traversal when the source already carries that name (nothing
would state which of the two spellings the feature is grained on). Hence ``customer_id`` on both the
crm target and the spine, and ``cif_id`` — the source's own join column — on neither.
"""
from __future__ import annotations

import dataclasses

import pytest
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize.test_cross_catalog_ir import (
    CRM_CIF,
    CRM_EFFECTIVE_FROM,
    CRM_EFFECTIVE_TO,
    CRM_TENANT,
    _inventory,
    _realization_current,
    _seed_crm_catalog,
)
from tests.featuregen.materialize.test_group_plan import (  # noqa: F401 — `catalog` is a fixture
    CADENCE,
    COUNT_90D,
    GROUP,
    RATIO_90D,
    SUM_30D,
    catalog,
)
from tests.featuregen.materialize.test_ir import (
    _DECLARER,
    CUSTOMERS,
    CUSTOMERS_ASOF,
    INVENTORY,
    _admitted,
    _col,
    _govern_entity,
    _govern_grain,
)
from tests.featuregen.materialize.test_render_project import ENVIRONMENT, _compiled

from featuregen.formula.schema import Grain
from featuregen.materialize.compile.chain import NodeAssemblyInputs
from featuregen.materialize.compile.wiring import assemble_nodes
from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    ContractGroup,
    derive_group_contract,
)
from featuregen.materialize.group_plan import PlannedFeature, build_group_plan
from featuregen.materialize.inputs import derive_requirement
from featuregen.materialize.ir import authorize_compilation, compile_ir, ir_hash
from featuregen.materialize.joins import CrossCatalogJoinStepV1
from featuregen.materialize.physical_types import PhysicalType, resolve_physical_type
from featuregen.materialize.render.nodes_compute import (
    SPINE_NODE_NAME,
    projection_node_name,
    render_projection_node,
)
from featuregen.materialize.render.project import (
    SealedProject,
    project_datasets,
    render_project,
)
from featuregen.materialize.spine import (
    CurrentSnapshot,
    PopulationSemantics,
    SpineSourceDeclarationV1,
)
from featuregen.overlay.upload.bridge_realization import (
    AsOfIntervalRequirementV1,
    FixedValueReferencePredicateV1,
)

_ROLES = ("feature_engineer",)
_PROMISE = AvailabilityPromiseV1(calendar_days=1)

#: The bridge's target table, as ``ProjectDatasets.raw`` keys it (§3.5's RESOLVED physical pair).
CRM_PHYSICAL = "crm_banking.customer_master"

#: The population, keyed on a column no source relation of this group carries.
CROSS_CATALOG_DECLARATION = SpineSourceDeclarationV1(
    entity="customer",
    source_table_ref=CUSTOMERS,
    ordered_key_refs=(f"{CUSTOMERS}.customer_id",),
    population_semantics=PopulationSemantics.CURRENT_COMPLETE_POPULATION,
    availability_ref=CUSTOMERS_ASOF,
    snapshot_policy=CurrentSnapshot(observed_snapshot_ref="2026-07-27"),
    declared_by=_DECLARER,
    declaration_reason="the bank's master customer file, keyed for the crm bridge",
    declaration_version=1,
    recorded_at="2026-07-27T09:00:00+00:00",
    declaration_record_id="spine-decl-xcat",
)


# ── the two groups: one that crosses catalogs, one that does not ─────────────────────────────────


def _contract_of(db, authorized, plan):
    """The group's ONE materialization contract, re-derived and proved to be the plan's own.

    ``test_render_project._compiled`` drops the ``ContractGroup`` it derived, and the assembly needs
    the contract itself (§8's cutoff is derived from ITS cadence). Re-deriving is safe precisely
    because the hash is asserted: a contract that differed from the plan's would be a second answer
    to which clock the group runs on, and the assertion is what refuses one.
    """
    group = derive_group_contract(db, authorized, cadence=CADENCE, availability_promise=_PROMISE)
    assert isinstance(group, ContractGroup), group
    assert group.contract_hash == plan.materialization_contract_hash
    return group.contract


def _inputs(db, authorized, plan, spine_input, names) -> NodeAssemblyInputs:
    datasets = project_datasets(authorized, plan, spine_input=spine_input)
    return NodeAssemblyInputs(
        authorized=authorized, plan=plan, contract=_contract_of(db, authorized, plan),
        admitted={name: _admitted(name) for name in names}, spine_input=spine_input,
        datasets=datasets)


@pytest.fixture
def same_catalog(catalog):  # noqa: F811
    """Three real worked features over one catalog — the shape every earlier task rendered."""
    names = (SUM_30D, COUNT_90D, RATIO_90D)
    authorized, plan, spine_input = _compiled(catalog, *names)
    return _inputs(catalog, authorized, plan, spine_input, names)


def _cross_catalog(db, *names, composite=False, predicates=()) -> NodeAssemblyInputs:
    """Compile a group whose every feature traverses ONE real ``CrossCatalogJoinStepV1``.

    Every stage is the real one: the bridge realization is the shipped ``test_cross_catalog_ir``
    fixture, ``compile_ir`` plans the hop, Gate 2 authorizes the union of both catalogs' read sets,
    and §5/§6/§10 derive the contract, the type and the plan. Only the GRAIN is rewritten — onto the
    crm key — because that is what makes the traversal cross a catalog at all.
    """
    _seed_crm_catalog(db)
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, schema_name) "
        "VALUES ('crm','public.customer_master','table','customer_master','crm_banking')")
    # The population's key: governed on `customer_id`, and `cif_id` demoted so the declared key is
    # the WHOLE governed grain (a `current_snapshot` policy over a finer grain is refused).
    _col(db, "customers", "customer_id")
    _govern_grain(db, "customers", "customer_id")
    _govern_entity(db, "customers", "customer_id")
    db.execute("UPDATE graph_node SET is_grain = false WHERE catalog_source = 'hdfc' "
               "AND table_name = 'customers' AND kind = 'column' AND column_name = 'cif_id'")
    if composite:
        _col(db, "transactions", "tenant_id")

    customers = INVENTORY.tables["banking.customers"]
    inventory = _inventory(dataclasses.replace(INVENTORY, tables={
        **INVENTORY.tables,
        "banking.customers": dataclasses.replace(
            customers, columns=(*customers.columns, ("customer_id", "string")))}))
    realization = _realization_current(inventory, composite=composite, predicates=predicates)

    admitted, irs, features = {}, [], []
    for name in names:
        artifact = _admitted(name)
        formula = dataclasses.replace(
            artifact.formula, grain=Grain(entity="customer", keys=(CRM_CIF,)))
        admitted[name] = _admitted(name, formula=formula)
        ir = compile_ir(db, admitted[name], roles=_ROLES, spine_decl=CROSS_CATALOG_DECLARATION,
                        inventory=inventory, bridge_realizations=(realization,))
        assert not isinstance(ir, Exception), ir
        irs.append(ir)

    authorized = authorize_compilation(db, irs, irs[0].spine, roles=_ROLES)
    assert not isinstance(authorized, Exception), authorized
    group = derive_group_contract(db, authorized, cadence=CADENCE, availability_promise=_PROMISE)
    assert isinstance(group, ContractGroup), group
    for ir in irs:
        physical = resolve_physical_type(
            admitted[ir.feature_name].formula,
            operand_types={e.expr_path: e.operand_type for e in ir.expressions})
        assert isinstance(physical, PhysicalType), physical
        features.append(PlannedFeature(column_name=ir.feature_name, ir_hash=ir_hash(ir),
                                       physical_type=physical))
    plan = build_group_plan(group, tuple(features), logical_group_name=GROUP)
    spine_input = derive_requirement(db, inventory, table_ref=CUSTOMERS)
    return NodeAssemblyInputs(
        authorized=authorized, plan=plan, contract=group.contract, admitted=admitted,
        spine_input=spine_input,
        datasets=project_datasets(authorized, plan, spine_input=spine_input))


@pytest.fixture
def cross_catalog(catalog):  # noqa: F811
    """One feature, one expression, one bridge hop — the smallest project that crosses a catalog."""
    return _cross_catalog(catalog, SUM_30D)


def _render(inputs: NodeAssemblyInputs, nodes=None) -> SealedProject:
    return render_project(
        inputs.authorized, inputs.plan, environment_id=ENVIRONMENT,
        engine_versions=fixtures.ENGINE_VERSIONS, spine_input=inputs.spine_input,
        nodes=tuple(assemble_nodes(inputs) if nodes is None else nodes))


def _node(nodes, name):
    return next(node for node in nodes if node.name == name)


def _the_step(inputs: NodeAssemblyInputs) -> CrossCatalogJoinStepV1:
    steps = [step for ir in inputs.authorized.irs for expression in ir.expressions
             for step in expression.join_plan.steps if isinstance(step, CrossCatalogJoinStepV1)]
    assert len(steps) == 1, steps
    return steps[0]


# ── the cross-catalog assembly ───────────────────────────────────────────────────────────────────


def test_the_fixture_really_crosses_catalogs(cross_catalog) -> None:
    """The premise, asserted rather than assumed: a test that silently compiled a same-catalog
    group would pass every assertion below by vacuity."""
    step = _the_step(cross_catalog)
    assert step.to_catalog_source != step.from_catalog_source
    assert cross_catalog.datasets.join_gates == {
        step.realization_revision_id: cross_catalog.datasets.join_gates[
            step.realization_revision_id]}
    assert CRM_PHYSICAL in cross_catalog.datasets.raw


def test_a_join_precondition_node_is_assembled_for_every_realization(cross_catalog) -> None:
    """§3.2's pre-computation gate. `render_join_precondition_node` exists and is unit-tested, and
    until now nothing in the repository ever placed one in a project."""
    step = _the_step(cross_catalog)
    nodes = assemble_nodes(cross_catalog)

    gates = [node for node in nodes if "bridge_precondition" in node.tags]

    assert len(gates) == 1
    assert gates[0].outputs == (cross_catalog.datasets.join_gates[step.realization_revision_id],)
    assert gates[0].inputs[0] == cross_catalog.datasets.raw[CRM_PHYSICAL]


def test_ONE_gate_serves_every_expression_that_shares_a_realization(catalog) -> None:  # noqa: F811
    """`project_datasets` declares ONE validated dataset per ``realization_revision_id``, so the
    assembly must render one gate per REVISION and not one per hop: two nodes writing one dataset is
    a wiring the renderer refuses, and whichever ran last would decide what every projection read.

    The group below reaches the same realization four times — a ratio's numerator and denominator,
    plus two more features — and must still assemble exactly one gate.
    """
    inputs = _cross_catalog(catalog, SUM_30D, COUNT_90D, RATIO_90D)
    hops = [step for ir in inputs.authorized.irs for expression in ir.expressions
            for step in expression.join_plan.steps if isinstance(step, CrossCatalogJoinStepV1)]
    assert len(hops) == 4 and len({step.realization_revision_id for step in hops}) == 1

    nodes = assemble_nodes(inputs)

    gates = [node for node in nodes if "bridge_precondition" in node.tags]
    assert len(gates) == 1
    assert len(inputs.datasets.projections) == 4
    # every one of the four projections reads the ONE validated frame, and none reads the raw target
    for projection in (node for node in nodes if "projection" in node.tags):
        assert gates[0].outputs[0] in projection.inputs
        assert inputs.datasets.raw[CRM_PHYSICAL] not in projection.inputs
    assert _render(inputs).identity.generated_project_hash


def test_a_COMPOSITE_predicated_realization_wires_the_whole_tuple_to_the_gate(
        catalog) -> None:  # noqa: F811
    """A composite bridge tuple is where "which frame did we validate" gets sharp: the gate proves
    uniqueness of the WHOLE target tuple under the realization's closed predicates, so a projection
    that read the raw table would join on two columns nothing checked. The prepared predicate values
    also become a run parameter, which is how §11.1 refuses a run that never resolved them."""
    inputs = _cross_catalog(catalog, SUM_30D, composite=True, predicates=(
        FixedValueReferencePredicateV1("tenant-scope", CRM_TENANT, "tenant_id"),
        AsOfIntervalRequirementV1("customer-as-of", CRM_EFFECTIVE_FROM, CRM_EFFECTIVE_TO,
                                  "dimension_as_of")))
    step = _the_step(inputs)
    assert len(step.column_pairs) == 2

    nodes = assemble_nodes(inputs)

    (gate,) = [node for node in nodes if "bridge_precondition" in node.tags]
    (projection,) = [node for node in nodes if "projection" in node.tags]
    assert gate.inputs == (inputs.datasets.raw[CRM_PHYSICAL], "params:bridge_predicate_values")
    assert gate.outputs[0] in projection.inputs
    assert inputs.datasets.raw[CRM_PHYSICAL] not in projection.inputs
    sealed = _render(inputs)
    assert "bridge_predicate_values" in next(
        text for path, text in sealed.files.items() if path.endswith("hooks.py"))


def test_the_bridged_projection_reads_the_VALIDATED_frame_and_not_the_raw_target(
        cross_catalog) -> None:
    """THE property of this task. The gate returns the predicate-scoped target it checked, so the
    frame whose uniqueness was proved is the frame the join consumes — but only if the projection
    is wired to the gate's OUTPUT. Wired to the raw table instead, the gate would fail beside the
    computation rather than before it, and the join would still amplify."""
    step = _the_step(cross_catalog)
    validated = cross_catalog.datasets.join_gates[step.realization_revision_id]
    raw_target = cross_catalog.datasets.raw[CRM_PHYSICAL]

    nodes = assemble_nodes(cross_catalog)

    (projection,) = [node for node in nodes if "projection" in node.tags]
    assert validated in projection.inputs
    assert raw_target not in projection.inputs
    # and the raw target is still read — by the GATE, which is what keeps §1.3's declared source
    # from becoming a catalog entry nothing reads.
    assert raw_target in {name for node in nodes for name in node.inputs}


def test_render_project_accepts_the_cross_catalog_assembly(cross_catalog) -> None:
    """`_check_wiring`'s seven rules are the correctness oracle: a project whose datasets do not
    close is refused. This is the first full cross-catalog project the repository has ever sealed."""
    sealed = _render(cross_catalog)

    pipeline = next(text for path, text in sealed.files.items() if path.endswith("pipeline.py"))
    assert "validate_bridge_" in pipeline
    assert cross_catalog.datasets.join_gates[_the_step(cross_catalog).realization_revision_id] \
        in pipeline


def test_every_declared_dataset_is_written_and_the_gate_is_not_a_sibling(cross_catalog) -> None:
    """The structural statement, made here rather than left to `_check_wiring`: exactly one node
    writes each non-raw dataset, and the gate's output is read by the projection that traverses the
    hop — not merely by "some node"."""
    step = _the_step(cross_catalog)
    nodes = assemble_nodes(cross_catalog)
    validated = cross_catalog.datasets.join_gates[step.realization_revision_id]

    written = [name for node in nodes for name in node.outputs]
    readers = {name: [node.name for node in nodes if name in node.inputs] for name in written}

    assert sorted(written) == sorted(set(written))
    assert set(written) == set(cross_catalog.datasets.names()) - set(
        cross_catalog.datasets.raw.values())
    assert readers[validated] == [projection_node_name(SUM_30D, "body.expr")]


# ── the negative: `_check_wiring` accepts the bypass, and the assembly is what refuses it ────────


def test_check_wiring_ACCEPTS_a_bypass_that_this_assembly_never_produces(cross_catalog) -> None:
    """Task 26 of the codegen programme: `_check_wiring`'s rule is "the gate output is read by SOME
    node", which is weaker than "read by the projection that renders that hop". This test builds
    exactly the project that gap admits — the projection wired to the RAW target, with an unrelated
    node reading the gate output so rule 7 is satisfied — and `render_project` seals it.

    So the guarantee cannot be the renderer's. It is this module's: `assemble_nodes` has no path
    that puts a bridged table's raw dataset into a projection's `joined_datasets`, and the second
    half of this test is what says so.
    """
    step = _the_step(cross_catalog)
    validated = cross_catalog.datasets.join_gates[step.realization_revision_id]
    raw_target = cross_catalog.datasets.raw[CRM_PHYSICAL]
    honest = assemble_nodes(cross_catalog)
    expression = cross_catalog.authorized.irs[0].expressions[0]

    bypass = render_projection_node(
        expression, cross_catalog.contract, feature_column=SUM_30D,
        source_dataset=cross_catalog.datasets.raw["banking.transactions"],
        projection_dataset=cross_catalog.datasets.projections[(SUM_30D, expression.expr_path)],
        joined_datasets={f"crm::{CRM_PHYSICAL}": raw_target})
    decoy = _node(honest, SPINE_NODE_NAME)
    rewired = [
        dataclasses.replace(decoy, inputs=(*decoy.inputs, validated)) if node is decoy
        else bypass if "projection" in node.tags
        else node
        for node in honest]

    # Rule 7 is real: strip the DECOY and the same bypass is refused. So the rule catches a gate
    # nothing reads at all — and that is the whole of what it catches.
    with pytest.raises(ValueError, match="not consumed by a downstream node"):
        _render(cross_catalog, nodes=[bypass if "projection" in node.tags else node
                                      for node in honest])

    # Put the decoy back and the renderer SEALS it. The gate now runs beside the computation, the
    # projection reads the unchecked raw table, and nothing refused.
    admitted_by_the_renderer = _render(cross_catalog, nodes=rewired)
    assert raw_target in next(text for path, text in admitted_by_the_renderer.files.items()
                              if path.endswith("pipeline.py"))

    # This module never produces it.
    (projection,) = [node for node in honest if "projection" in node.tags]
    assert projection.inputs != bypass.inputs
    assert raw_target not in projection.inputs
    assert validated in projection.inputs


# ── the same-catalog group still assembles ───────────────────────────────────────────────────────


def test_the_same_catalog_group_assembles_and_declares_no_gate(same_catalog) -> None:
    nodes = assemble_nodes(same_catalog)

    assert same_catalog.datasets.join_gates == {}
    assert [node for node in nodes if "bridge_precondition" in node.tags] == []
    assert _render(same_catalog).identity.generated_project_hash


def test_the_assembly_covers_every_planned_feature_and_expression(same_catalog) -> None:
    """One projection per expression and one calculation per feature — §7 shares no scan."""
    nodes = assemble_nodes(same_catalog)

    projections = [node for node in nodes if "projection" in node.tags]
    calculations = [node for node in nodes if "calculation" in node.tags]

    assert len(projections) == len(same_catalog.datasets.projections)
    assert len(calculations) == len(same_catalog.plan.features)
    assert {node.outputs[0] for node in projections} == set(
        same_catalog.datasets.projections.values())


# ── determinism: the sequence feeds `generated_project_hash` ─────────────────────────────────────


@pytest.mark.parametrize("group", ["same_catalog", "cross_catalog"])
def test_two_assemblies_of_the_same_inputs_are_identical(group, request) -> None:
    """`generated_project_hash` must identify WHAT was built, not the day it was built. The node
    sequence is rendered into `nodes.py` and `pipeline.py` in order, so an assembly that walked a
    set would give one compilation two identities."""
    inputs = request.getfixturevalue(group)

    first, second = assemble_nodes(inputs), assemble_nodes(inputs)

    assert first == second
    assert _render(inputs).identity.generated_project_hash == \
        _render(inputs).identity.generated_project_hash


def test_the_assembly_is_a_pure_function_of_its_inputs_not_of_mapping_order(
        cross_catalog) -> None:
    """The mappings the assembly reads are ordered by construction, and nothing here may depend on
    that: a `ProjectDatasets` built with its mappings reversed must assemble byte-identically."""
    reversed_datasets = dataclasses.replace(
        cross_catalog.datasets,
        raw=dict(reversed(list(cross_catalog.datasets.raw.items()))),
        projections=dict(reversed(list(cross_catalog.datasets.projections.items()))))
    shuffled = dataclasses.replace(cross_catalog, datasets=reversed_datasets)

    assert assemble_nodes(shuffled) == assemble_nodes(cross_catalog)


# ── what the assembly refuses ────────────────────────────────────────────────────────────────────


def test_two_steps_sharing_a_revision_id_that_DISAGREE_are_refused(cross_catalog) -> None:
    """The catalog declares one validated dataset per realization revision, so an assembly that
    silently kept whichever gate it saw first would leave the other target frame joined without ever
    having been checked — the exact failure the gate exists to prevent, with a gate present."""
    ir = cross_catalog.authorized.irs[0]
    expression = ir.expressions[0]
    step = expression.join_plan.steps[0]

    elsewhere = dataclasses.replace(step, column_pairs=(
        ("hdfc::public.transactions.cif_id", "hdfc::public.transactions.txn_amt"),))
    twisted = dataclasses.replace(
        expression, expr_path="body.numerator",
        join_plan=dataclasses.replace(expression.join_plan, steps=(elsewhere,)))
    inconsistent = dataclasses.replace(cross_catalog, authorized=dataclasses.replace(
        cross_catalog.authorized,
        irs=(dataclasses.replace(ir, expressions=(expression, twisted)),)))

    with pytest.raises(ValueError, match="render different pre-computation gates"):
        assemble_nodes(inconsistent)


def test_a_bridge_endpoint_outside_the_authorized_read_set_is_refused(cross_catalog) -> None:
    """§1.3 records every join endpoint as its own read. An endpoint the read set does not carry is
    a column the group would join on and was never granted — and it is also a hop whose target
    table this assembly could not resolve to a catalog entry at all."""
    ir = cross_catalog.authorized.irs[0]
    expression = ir.expressions[0]
    step = expression.join_plan.steps[0]

    ungoverned = dataclasses.replace(step, column_pairs=(
        ("hdfc::public.transactions.cif_id", "crm::public.customer_master.never_granted"),))
    inconsistent = dataclasses.replace(cross_catalog, authorized=dataclasses.replace(
        cross_catalog.authorized, irs=(dataclasses.replace(ir, expressions=(
            dataclasses.replace(expression, join_plan=dataclasses.replace(
                expression.join_plan, steps=(ungoverned,))),)),)))

    with pytest.raises(ValueError, match="authorized read set does not carry"):
        assemble_nodes(inconsistent)


def test_a_dataset_map_missing_a_bridge_gate_is_a_caller_error(cross_catalog) -> None:
    """The gate dataset is `project_datasets`' answer, never this module's: a name invented here
    would be a storage location written into the compiler."""
    stripped = dataclasses.replace(cross_catalog,
                                   datasets=dataclasses.replace(cross_catalog.datasets,
                                                                join_gates={}))

    with pytest.raises(ValueError, match="no bridge gate dataset"):
        assemble_nodes(stripped)


def test_a_dataset_map_missing_a_governed_source_is_a_caller_error(same_catalog) -> None:
    stripped = dataclasses.replace(same_catalog,
                                   datasets=dataclasses.replace(same_catalog.datasets, raw={}))

    with pytest.raises(ValueError, match="declares no dataset"):
        assemble_nodes(stripped)


def test_a_feature_with_no_admitted_formula_is_a_caller_error(same_catalog) -> None:
    """`empty_window`/`null_input` are the FORMULA's policies — `PitSpec` excludes them
    deliberately — so a feature whose formula is missing cannot be calculated at all, and guessing
    a policy would answer a question the formula exists to answer."""
    stripped = dataclasses.replace(same_catalog, admitted={})

    with pytest.raises(ValueError, match="no admitted formula"):
        assemble_nodes(stripped)
