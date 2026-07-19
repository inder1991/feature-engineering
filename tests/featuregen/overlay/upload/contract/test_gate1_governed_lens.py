"""Phase-3C.2a Task 5 — the LIVE governed cross-catalog lens in ``build_considered_set``.

On a flag-on-and-activation-approved entity-scoped run the governed PLANNER (not the LLM) is the
authority for cross-catalog features: its resolved plans surface as options carrying a governed plan
envelope + structured provenance, its unresolved plans surface as rejections, and every cross-catalog
LLM alternative is rejected (it has no governed physical plan). With the flag off the whole branch is
skipped — byte-identical to today.
"""
from __future__ import annotations

from tests.featuregen.overlay.upload.planner.test_plan import (
    _NOW,
    _freshness,
    _split,
    _txn_template,
)
from tests.featuregen.overlay.upload.planner.test_shadow_capture import _cross_seed

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import (
    GOVERNED_CROSS_CATALOG_PLAN_REQUIRED,
    _governed_cross_catalog_options,
    _reject_cross_catalog_llm,
    build_considered_set,
)
from featuregen.overlay.upload.contract.intake import submit_intent
from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet, SetsReport
from featuregen.overlay.upload.graph import build_graph


def _minimal(db):
    """A tiny single-table catalog so the intake / snapshot writes have a graph to read."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric", additivity="semi_additive")])
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (_NOW, _NOW))


def _recommend_set_client() -> FakeLLM:
    return FakeLLM(script={"overlay.feature.recommend_set": FakeResponse(output={
        "recommended_lens": "templates", "reasoning": "advisory"})})


# ── (b) a resolved governed plan → a governed option (helper) ─────────────────────────────────────────
def test_helper_surfaces_resolved_governed_plan_as_option(db):
    _cross_seed(db)   # ops + rev + a VERIFIED bridge + fresh watermarks -> a resolved cross-catalog plan
    ideas, rejections = _governed_cross_catalog_options(
        db, target_entity="account", eligible_recipe_ids=frozenset({"t_roll"}), roles=(),
        now=_NOW, templates=(_txn_template(),))
    assert len(ideas) == 1 and not rejections
    idea = ideas[0]
    assert idea.origin == "governed_planner"
    assert idea.path_authority == "governed_cross_catalog"
    assert idea.plan_envelope is not None            # the exact compiled plan carried forward
    assert idea.plan_envelope.physical_plan_id
    # the option genuinely spans >1 catalog (the whole point of a governed cross-catalog plan)
    assert len({cs for cs, _ref in idea.derives_pairs}) > 1


# ── (c) an unresolved governed plan → a rejection (helper) ────────────────────────────────────────────
def test_helper_unresolved_governed_plan_becomes_a_rejection(db):
    _split(db)                 # ops + rev but NO bridge -> the account roll-up cannot complete
    _freshness(db, "ops", "rev")
    ideas, rejections = _governed_cross_catalog_options(
        db, target_entity="account", eligible_recipe_ids=frozenset({"t_roll"}), roles=(),
        now=_NOW, templates=(_txn_template(),))
    assert ideas == []
    assert len(rejections) == 1
    rej = rejections[0]
    assert rej["lens"] == "governed" and rej["recipe_id"] == "t_roll"
    assert isinstance(rej["reason"], str) and rej["reason"]   # carries a primary reason code


# ── (d)/(e) the LLM cross-catalog filter (pure) ───────────────────────────────────────────────────────
def test_reject_cross_catalog_llm_removes_multi_catalog_and_keeps_single():
    cross = FeatureIdea("cross_feat", "", ["a", "b"], "sum", None,
                        derives_pairs=(("ops", "public.t.a"), ("rev", "public.u.b")))
    single = FeatureIdea("single_feat", "", ["a"], "sum", None,
                         derives_pairs=(("ops", "public.t.a"),))
    filtered, rejections = _reject_cross_catalog_llm([FeatureSet("monetary", [cross, single])])
    surviving = {f.name for s in filtered for f in s.features}
    assert "single_feat" in surviving              # single-catalog untouched
    assert "cross_feat" not in surviving           # cross-catalog removed from its FeatureSet
    assert any(r["name"] == "cross_feat" and r["reason"] == GOVERNED_CROSS_CATALOG_PLAN_REQUIRED
               for r in rejections)


# ── (d)/(e) integration: the filter is wired into build_considered_set's entity-scoped branch ─────────
def test_build_considered_set_filters_cross_catalog_llm_when_live(db, monkeypatch):
    cross = FeatureIdea("cross_feat", "", ["a", "b"], "sum", None,
                        derives_pairs=(("ops", "public.t.a"), ("rev", "public.u.b")))
    single = FeatureIdea("single_feat", "", ["a"], "sum", None,
                         derives_pairs=(("ops", "public.t.a"),))
    report = SetsReport(sets=[FeatureSet("monetary", [cross, single])], rejections=[])
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1.recommend_feature_sets_report",
                        lambda *a, **k: report)
    intent = submit_intent(hypothesis="an entity-scoped hypothesis", actor="ds1")
    # target_entity=None + templates=() isolates the FILTER (no governed-options lens runs here).
    cs = build_considered_set(db, intent, _recommend_set_client(), catalog_source=None, entity=None,
                              is_live=True, target_entity=None, templates=(), now=_NOW)
    names = {f.name for s in cs.alternatives for f in s.features}
    assert "single_feat" in names and "cross_feat" not in names
    assert any(r.get("name") == "cross_feat" and r.get("reason") == GOVERNED_CROSS_CATALOG_PLAN_REQUIRED
               for r in cs.rejections)


# ── (b) integration: build_considered_set surfaces the governed option under the flag ─────────────────
def test_build_considered_set_surfaces_governed_option_when_live(db, monkeypatch):
    _cross_seed(db)
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1.recommend_feature_sets_report",
                        lambda *a, **k: SetsReport(sets=[], rejections=[]))   # no LLM noise
    intent = submit_intent(hypothesis="roll transactions up to the account", actor="ds1")
    cs = build_considered_set(
        db, intent, _recommend_set_client(), catalog_source=None, entity=None, is_live=True,
        target_entity="account", templates=(_txn_template(),), applicability=None, now=_NOW)
    governed = [f for s in cs.alternatives for f in s.features if f.origin == "governed_planner"]
    assert len(governed) == 1
    assert governed[0].path_authority == "governed_cross_catalog"
    assert governed[0].plan_envelope is not None
    # authority rides on the IDEA, never the lens name
    assert all(s.lens != "governed" for s in cs.alternatives)


# ── 3C.2a CRITICAL: a cross-catalog DEFINITION-MODE anchor is dropped when live (fail-closed) ──────────
def test_build_considered_set_drops_cross_catalog_definition_anchor_when_live(db, monkeypatch):
    # On a live entity-scoped run the definition anchor is generated over the WHOLE cross-catalog
    # candidate pool (catalog_source is None), so it CAN span >1 catalog with NO governed physical plan.
    # Such an anchor must never reach the customer-visible considered set / be choosable at Gate #1.
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1.recommend_feature_sets_report",
                        lambda *a, **k: SetsReport(sets=[], rejections=[]))   # isolate the anchor path
    cross_anchor = FeatureIdea("cross_anchor", "", ["a", "b"], "sum", None,
                               derives_pairs=(("ops", "public.t.a"), ("rev", "public.u.b")))
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1.recommend_features",
                        lambda *a, **k: [cross_anchor])
    intent = submit_intent(hypothesis="an entity-scoped hypothesis",
                           definition="a cross-catalog definition", actor="ds1")
    # target_entity=None + templates=() isolates the anchor drop (no governed-options lens runs here).
    cs = build_considered_set(db, intent, _recommend_set_client(), catalog_source=None, entity=None,
                              is_live=True, target_entity=None, templates=(), now=_NOW)
    assert cs.anchor is None    # the ungoverned cross-catalog anchor never becomes customer-visible
    assert any(r.get("name") == "cross_anchor"
               and r.get("reason") == GOVERNED_CROSS_CATALOG_PLAN_REQUIRED for r in cs.rejections)


# ── 3C.2a: a SINGLE-catalog definition anchor under is_live is preserved (no over-rejection) ───────────
def test_build_considered_set_preserves_single_catalog_definition_anchor_when_live(db, monkeypatch):
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1.recommend_feature_sets_report",
                        lambda *a, **k: SetsReport(sets=[], rejections=[]))
    single_anchor = FeatureIdea("single_anchor", "", ["a"], "sum", None,
                                derives_pairs=(("ops", "public.t.a"),))
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1.recommend_features",
                        lambda *a, **k: [single_anchor])
    intent = submit_intent(hypothesis="an entity-scoped hypothesis",
                           definition="a single-catalog definition", actor="ds1")
    cs = build_considered_set(db, intent, _recommend_set_client(), catalog_source=None, entity=None,
                              is_live=True, target_entity=None, templates=(), now=_NOW)
    assert cs.anchor is not None and cs.anchor.name == "single_anchor"   # single-catalog anchor untouched
    assert not any(r.get("reason") == GOVERNED_CROSS_CATALOG_PLAN_REQUIRED for r in cs.rejections)


# ── (a) flag off → the governed branch never runs (byte-identical to today) ───────────────────────────
def test_flag_off_skips_the_governed_branch_entirely(db, monkeypatch):
    _minimal(db)

    def _boom(*a, **k):
        raise AssertionError("the governed branch must not run when is_live is False")

    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1._governed_cross_catalog_options", _boom)
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1._reject_cross_catalog_llm", _boom)
    monkeypatch.setattr(
        "featuregen.overlay.upload.contract.gate1.recommend_feature_sets_report",
        lambda *a, **k: SetsReport(
            sets=[FeatureSet("monetary", [FeatureIdea("llm_feat", "", ["public.accounts.balance"],
                                                      "avg", None,
                                                      derives_pairs=(("bank", "public.accounts.balance"),))])],
            rejections=[]))
    intent = submit_intent(hypothesis="an entity-scoped hypothesis", actor="ds1")
    cs = build_considered_set(db, intent, _recommend_set_client(), catalog_source=None, entity=None,
                              is_live=False, target_entity="account", now=_NOW)
    # neither _boom fired (no plan_bindings, no filter) and no governed provenance leaked in
    assert all(f.origin != "governed_planner" for s in cs.alternatives for f in s.features)
    assert {f.name for s in cs.alternatives for f in s.features} == {"llm_feat"}
