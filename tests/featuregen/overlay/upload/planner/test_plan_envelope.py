from __future__ import annotations

import dataclasses

from tests.featuregen.overlay.upload.planner.test_plan import (
    _NOW,
    _c8_fixture,
    _txn_template,
)

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.planner.contracts import ContractResolutionStatus, ReplayFreshness
from featuregen.overlay.upload.planner.declarations import build_compiler_context
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.plan_envelope import (
    PlanEnvelopeV1,
    plan_envelope_from_result,
    recheck_plan_freshness,
)


def _env():
    return PlanEnvelopeV1(
        recipe_id="r", physical_plan_id="pplan_1", generation_run_id="run", catalog_sources=("a", "b"),
        ordered_path=("a.t1->b.t2",), contract_id="c1",
        contract_resolution_status="resolved", contract_reason_codes=(),
        catalog_fingerprint={"a": "fpa", "b": "fpb"}, compiler_version={"plan_contract": "1.0.0"},
        input_stamps=({"catalog_source": "a", "compiler_input_fingerprint": "fpa", "head_seq": 3,
                       "projection_checkpoint": 5},))


def test_envelope_json_roundtrips():
    e = _env()
    assert PlanEnvelopeV1.from_json(e.to_json()) == e


def test_from_json_is_total_and_frozen():
    e = _env()
    assert dataclasses.is_dataclass(e) and getattr(type(e), "__slots__", None) is not None


def test_plan_envelope_from_result_and_freshness(db):
    scope = _c8_fixture(db)
    tmpl = _txn_template()
    # an UNCOMPILED run has no selected contract plan -> nothing governed to carry
    base = plan_bindings(db, template=tmpl, target_entity="account", scope=scope, roles=(), now=_NOW)
    assert plan_envelope_from_result(base) is None
    # compile on: the cross-catalog roll-up is selected on the contract axis
    ctx = build_compiler_context(db, scope, (), _NOW)
    result = plan_bindings(db, template=tmpl, target_entity="account", scope=scope, roles=(),
                           now=_NOW, compile_ctx=ctx)
    env = plan_envelope_from_result(result)
    assert env is not None
    assert env.physical_plan_id == result.selected_contract_physical_plan_id
    assert env.contract_id == result.selected_contract_id
    assert env.contract_resolution_status == str(ContractResolutionStatus.resolved)
    assert env.recipe_id == "t_roll" and env.generation_run_id == result.run_id
    assert env.catalog_sources == ("ops", "rev")
    assert any("bfk_c8" in seg for seg in env.ordered_path)   # the governed crossing is pinned
    assert set(env.catalog_fingerprint) == {"ops", "rev"} and all(env.catalog_fingerprint.values())
    assert PlanEnvelopeV1.from_json(env.to_json()) == env     # persisted shape roundtrips
    # a plain re-read of untouched catalogs is current
    assert recheck_plan_freshness(db, env) is ReplayFreshness.current
    # a graph rebuild changing a classifier input (the FK column's concept) drifts the plan
    rows = [
        (CanonicalRow("ops", "transactions", "transaction_id", "integer", is_grain=True),
         "transaction_id"),
        (CanonicalRow("ops", "transactions", "account_id", "integer"), "customer_id"),  # was account_id
    ]
    build_graph(db, "ops", [r for r, _ in rows], concepts={content_hash(r): c for r, c in rows})
    assert recheck_plan_freshness(db, env) is ReplayFreshness.drifted


# ── the FeatureIdea carry-forward: snapshot (de)serialization threads the envelope + provenance ──
def test_idea_snapshot_roundtrips_envelope_and_provenance():
    from featuregen.overlay.upload.contract.gate1 import _idea_from_json, _idea_json
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    f = FeatureIdea("f", "", ["public.t.c"], "sum", "t", derives_pairs=(("ops", "public.t.c"),),
                    plan_envelope=_env(), origin="governed_planner", path_authority="governed_plan")
    d = _idea_json(f)
    back = _idea_from_json(d)
    assert back.plan_envelope == _env()
    assert back.origin == "governed_planner" and back.path_authority == "governed_plan"


def test_idea_snapshot_defaults_stay_llm_shaped():
    # behaviour-neutral: an existing (LLM/single-catalog) idea serializes a null envelope, and an
    # OLD snapshot dict WITHOUT the new keys deserializes to the defaults.
    from featuregen.overlay.upload.contract.gate1 import _idea_from_json, _idea_json
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    f = FeatureIdea("f", "", ["public.t.c"], "sum", "t")
    d = _idea_json(f)
    assert d["plan_envelope"] is None and d["origin"] == "llm" and d["path_authority"] == "single_or_llm"
    legacy = {"name": "f", "derives_from": ["public.t.c"], "aggregation": "sum", "grain_table": "t",
              "derives_pairs": []}
    back = _idea_from_json(legacy)
    assert back.plan_envelope is None
    assert back.origin == "llm" and back.path_authority == "single_or_llm"
