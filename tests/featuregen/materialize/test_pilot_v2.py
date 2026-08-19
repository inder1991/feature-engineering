"""Step 9 — the narrow pilot: an admitted V3 feature through every stage, for real.

§4.1 records the generation orchestrator as a MISSING contract, and it was. The V1 chain is
assembled by hand inside tests, which proves the pieces fit a test's idea of them and proves nothing
about a caller. This is the first thing that runs the whole chain.

Narrow on purpose — sum and count, one relation, no policy. The pilot is the first thing in this
program that can be wrong in an *interesting* way, and a slice is where that is cheapest to find out.

What these tests hold:

1. **The chain closes.** A real V3 proposal compiles, passes both gates, gets a contract, a type, a
   packing list and an operator graph — against a seeded catalog, with nothing stubbed between.
2. **The FIRST refusal wins**, and it is the one that describes the cause rather than a consequence.
3. **Nothing is defaulted into existence.** A missing empty-window answer is a caller error, not a
   value this function picks.
4. **The plan and the graphs describe the same columns**, so a published column always has something
   that computes it.
"""
from __future__ import annotations

import pytest
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize.test_ir import (
    _ROLES,
    CUSTOMERS,
    DECLARATION,
    INVENTORY,
    TXN,
    TXN_AMT,
    TXN_CIF,
    TXN_DT,
    compile_ir,
    seed_catalog,
)

from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.parse_v3 import parse_proposal_v3
from featuregen.materialize.admission_v2 import AdmittedFeatureV2
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.contract import (
    AvailabilityPromiseV1,
    CadenceDecl,
    CadencePeriod,
    CadenceTrigger,
)
from featuregen.materialize.generation_authorization import GenerationAuthorizationV1
from featuregen.materialize.operator_graph_v2 import OperatorKindV2
from featuregen.materialize.pilot_v2 import CompiledGenerationV2, compile_generation_v2
from featuregen.overlay.upload.field_resolution import resolve_and_project
from featuregen.overlay.upload.selection_revisions import TargetModeV1

ENV = "hdfc-local"
GROUP = "customer_txn_features"
POLICY = "formula-v2/physical-types@1"
FEATURE = "posted_amount_30d"

#: What the catalog governs about the operand. Supplied rather than assumed: output authority is
#: what decides the published unit, and an operand with no facts is one whose meaning nobody
#: established.
OPERAND_FACTS = {TXN_AMT: OperandFactsV2(unit="monetary", currency="fixed:AED")}

CADENCE = CadenceDecl(period=CadencePeriod.DAILY, timezone="Asia/Kolkata",
                      business_date_cutoff="00:00:00", trigger=CadenceTrigger.SCHEDULED)


@pytest.fixture
def catalog(db):
    """`test_ir`'s governed catalog, PLUS the one governed type decision a SUM needs.

    `seed_catalog` governs grain, entity and availability and attests no `logical_representation`,
    so a SUM over it refuses `OUTPUT_TYPE_NOT_GOVERNED` — correctly, and uselessly for a pilot. The
    decision is made through the REAL machinery (`record_field_evidence` + `resolve_and_project`),
    because a flat insert would skip the lifecycle the governed reader derives its status from and
    the resolved type would then be a fiction.

    The flat `graph_node.data_type` is written to the decided value for `test_group_plan`'s reason:
    this catalog is hand-built rather than uploaded, so no projection ever filled that column, and
    C1 fails CLOSED when the decision and the column disagree.
    """
    seed_catalog(db)
    fixtures._attest(db, TXN_AMT, "logical_representation", "numeric")
    resolve_and_project(db, source="hdfc", logical_refs=[TXN_AMT])
    db.execute(
        "UPDATE graph_node SET data_type = 'numeric' "
        "WHERE catalog_source = 'hdfc' AND object_ref = 'public.transactions.txn_amt'")
    return db


@pytest.fixture
def spine(catalog):
    """The declared population, taken from a REAL V1 compile against the same catalog.

    Not hand-built: §4's declaration is validated against governed facts, and a fixture spine would
    be a population nobody showed the catalog agrees with.
    """
    return compile_ir(catalog, _v1_admitted(), roles=_ROLES,
                      spine_decl=DECLARATION, inventory=INVENTORY).spine


def _v1_admitted():
    from tests.featuregen.materialize.test_ir import _admitted

    return _admitted("total_debit_amount_30d")


def _raw_v3(*, aggregation="sum", operand=TXN_AMT, rounding="half_up") -> dict:
    """A V3 proposal spelled against THIS catalog's refs, not the authoring fixture's.

    The refs matter: `compile_ir_v2` resolves them physically, so a proposal carrying another
    catalog's spelling would refuse for a reason that has nothing to do with the pilot.
    """
    return {
        "formula_schema_version": 3,
        "operation_grammar_version": 1,
        "canonicalization_version": 1,
        "grain": {"entity": "customer", "keys": [TXN_CIF]},
        "body": {
            "final_operation": "identity",
            "expr": {
                "aggregation": aggregation,
                "operand": operand,
                "source_relation": {"table_ref": TXN},
                "filter": None,
                "window": {
                    "event_time_ref": TXN_DT, "basis": "trailing", "length": 30, "unit": "day",
                    "start_inclusive": "inclusive", "end_inclusive": "exclusive",
                    "timezone": "Asia/Kolkata", "empty_window": "null", "null_input": "ignore",
                    "offset_periods": 0,
                },
                "aggregation_argument": None, "second_operand": None, "authority_refs": None,
                "row_selections": [],
            },
        },
        "parameters": [],
        "expected_output": None,
        "allocation_policy_ref": "",
        "decimal": {"precision": 38, "scale": 6, "rounding": rounding, "overflow": "error"},
    }


def _admitted_v2(name=FEATURE, **kwargs) -> AdmittedFeatureV2:
    from tests.featuregen.materialize.test_admission_v2_s13 import _INTENT

    proposal = parse_proposal_v3(_raw_v3(**kwargs))
    return AdmittedFeatureV2(
        feature_name=name, proposal=proposal, proposal_content_hash=f"sha256:{name}",
        intent=_INTENT, authoring_run_id="run-pilot-1",
        operator_kinds=(("aggregate", kwargs.get("aggregation", "sum")),))


def _authorization(mode=TargetModeV1.EXPLORATION, target_ref=None):
    return GenerationAuthorizationV1(
        environment_id=ENV, logical_group_name=GROUP, build_set_revision_id="bs-pilot",
        target_mode=mode, target_ref=target_ref)


def _run(catalog, spine, admitted=None, *, empty_values=None, **kwargs):
    features = admitted if admitted is not None else [_admitted_v2()]
    return compile_generation_v2(
        catalog, features, spine=spine, inventory=INVENTORY,
        authorization=kwargs.pop("authorization", _authorization()),
        cadence=CADENCE, availability_promise=AvailabilityPromiseV1(calendar_days=1),
        physical_type_policy=POLICY, logical_group_name=GROUP, roles=_ROLES,
        operand_facts=kwargs.pop("operand_facts", OPERAND_FACTS),
        empty_values=(empty_values if empty_values is not None
                      else {f.feature_name: "0" for f in features}),
        **kwargs)


# ══ THE CHAIN CLOSES ═══════════════════════════════════════════════════════════════════════════
def test_A_V3_FEATURE_REACHES_AN_OPERATOR_GRAPH(catalog, spine):
    """Compile, both gates, contract, type, packing list, graph — nothing stubbed between them."""
    result = _run(catalog, spine)

    assert isinstance(result, CompiledGenerationV2), result
    assert [f.column_name for f in result.plan.features] == [FEATURE]
    assert set(result.graphs) == {FEATURE}
    assert result.graphs[FEATURE].terminal.kind is OperatorKindV2.GROUP_ASSEMBLY


def test_THE_PUBLISHED_TYPE_IS_THE_DECLARED_ONE(catalog, spine):
    """A sum publishes the formula's own decimal policy — not a width derived from a precision
    nobody read."""
    result = _run(catalog, spine)

    assert isinstance(result, CompiledGenerationV2), result
    assert result.plan.features[0].physical_type.sql_type == "DECIMAL(38,6)"


def test_A_COUNT_PUBLISHES_BIGINT(catalog, spine):
    """The other pilot arm, and the discriminator for the one above."""
    counting = [_admitted_v2(aggregation="count_rows", operand=None)]
    result = _run(catalog, spine, counting)

    assert isinstance(result, CompiledGenerationV2), result
    assert result.plan.features[0].physical_type.sql_type == "BIGINT"


def test_THE_GRAPH_AND_THE_PLAN_DESCRIBE_THE_SAME_COLUMNS(catalog, spine):
    """Enforced at construction, so a published column with nothing computing it is
    unconstructible rather than caught downstream."""
    result = _run(catalog, spine)

    assert isinstance(result, CompiledGenerationV2), result
    assert set(result.graphs) == {f.column_name for f in result.plan.features}


def test_THE_LEAKAGE_VERDICT_TRAVELS_WITH_THE_RESULT(catalog, spine):
    """An exploration build makes NO claim — and "no claim" is not the same value as "passed"."""
    result = _run(catalog, spine)

    assert isinstance(result, CompiledGenerationV2), result
    assert result.authorized.leakage is None
    assert "No leakage claim" in result.authorized.leakage_claim


def test_A_PREDICTION_BUILD_CARRIES_A_REAL_VERDICT(catalog, spine):
    """The discriminator: with a target, the question was actually asked."""
    result = _run(catalog, spine, authorization=_authorization(
        TargetModeV1.PREDICTION, f"{CUSTOMERS}.status_cd"))

    assert isinstance(result, CompiledGenerationV2), result
    assert result.authorized.leakage is not None
    assert result.authorized.leakage.admitted is True


# ══ THE FIRST REFUSAL WINS, AND IT NAMES THE CAUSE ═════════════════════════════════════════════
def test_A_FEATURE_READING_ITS_OWN_TARGET_REFUSES_AT_THE_GATE(catalog, spine):
    """Not at the contract, the type or the graph — all of which would also have something to say
    about a build that must not exist."""
    result = _run(catalog, spine, authorization=_authorization(
        TargetModeV1.PREDICTION, TXN_AMT))

    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.TARGET_LEAKAGE_DETECTED


def test_A_DECLARED_POLICY_WITH_NO_REALIZATION_REFUSES_AT_COMPILE(catalog, spine):
    """The narrow pilot has no policies. One that declares a policy is refused where the policy is,
    rather than rendered without it."""
    raw = _raw_v3()
    raw["body"]["expr"]["authority_refs"] = {"status_policy_ref": "status:posted-only"}
    proposal = parse_proposal_v3(raw)
    from tests.featuregen.materialize.test_admission_v2_s13 import _INTENT

    declared = AdmittedFeatureV2(
        feature_name=FEATURE, proposal=proposal, proposal_content_hash="sha256:p",
        intent=_INTENT, authoring_run_id="run-pilot-1",
        operator_kinds=(("aggregate", "sum"),))
    result = _run(catalog, spine, [declared])

    assert isinstance(result, MaterializationRefused)
    assert result.code is CompilationRefusalCode.POLICY_REFERENCE_UNRESOLVABLE


# ══ NOTHING IS DEFAULTED INTO EXISTENCE ════════════════════════════════════════════════════════
def test_A_MISSING_EMPTY_WINDOW_ANSWER_IS_A_CALLER_ERROR(catalog, spine):
    """"Had no transactions" and "transacted zero" are different published answers, and the compiled
    IR does not carry which one the author declared. An orchestrator that filled it in would be
    choosing what gets published."""
    with pytest.raises(ValueError, match="empty_values must describe exactly"):
        _run(catalog, spine, empty_values={})


def test_AN_EMPTY_GENERATION_IS_A_CALLER_ERROR(catalog, spine):
    """Every check below would pass over it, which is the shape of a green build that produced
    nothing."""
    with pytest.raises(ValueError, match="no admitted features"):
        _run(catalog, spine, [], empty_values={})


# ══ STEP 11 — THE ORDINARY AGGREGATES REACH A GRAPH ════════════════════════════════════════════
@pytest.mark.parametrize("aggregation", ["avg", "min", "max"])
def test_AN_ORDINARY_AGGREGATE_REACHES_AN_OPERATOR_GRAPH(catalog, spine, aggregation):
    """"Average balance over 90 days" was unrenderable when this program started. The whole chain
    is exercised rather than the renderer alone, because an aggregate is only really added once it
    types, compiles, authorizes and shapes — advertising it on any one of those is the gap."""
    result = _run(catalog, spine, [_admitted_v2(aggregation=aggregation)])

    assert isinstance(result, CompiledGenerationV2), result
    aggregate = next(n for n in result.graphs[FEATURE].nodes
                     if n.kind is OperatorKindV2.AGGREGATE)
    assert aggregate.payload.function.value == aggregation
    assert result.plan.features[0].physical_type.sql_type == "DECIMAL(38,6)"


def test_an_aggregate_THIS_BUILD_STILL_CANNOT_EMIT_refuses_by_name(catalog, spine):
    """The discriminator for the three above, and the reason they are worth adding one at a time:
    `median` types cleanly nowhere and rendering it is the same piece of work as typing it."""
    result = _run(catalog, spine, [_admitted_v2(aggregation="median")])

    assert isinstance(result, MaterializationRefused)
    assert "median" in result.detail
