"""Phase-3C.2a Task 5 — the LIVE governed cross-catalog lens in ``build_considered_set``.

On a flag-on-and-activation-approved entity-scoped run the governed PLANNER is the authority for
cross-catalog features: its resolved plans surface as options carrying a governed plan envelope +
structured provenance, and its unresolved plans surface as rejections. With the flag off the whole
branch is skipped — byte-identical to today.

The E4 cutover (2026-08-14) settled the OTHER half of that guarantee by construction. The free-form
generator that used to propose ungoverned cross-catalog candidates — the things
``_reject_cross_catalog_llm`` and the cross-catalog anchor drop existed to catch — is deleted, so an
entity-scoped run has no ungoverned source at all: no option and no anchor can arrive without a
governed plan behind it. The filter itself is still covered as a pure function below; the
integration tests now assert the stronger fact, that there is nothing left for it to remove.
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
from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet
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


# ── (d)/(e) integration: an entity-scoped run has NO ungoverned option to filter ──────────────────────
def test_no_ungoverned_option_can_reach_a_live_entity_scoped_run(db):
    """The filter's guarantee, now structural. This test used to inject a cross-catalog and a
    single-catalog candidate through the free-form generator and watch ``_reject_cross_catalog_llm``
    remove the first. The E4 cutover (2026-08-14) deleted that generator, so there is no source of
    ungoverned candidates on this branch at all — every lens on the returned set is one the governed
    planner authored. That is what the filter was protecting, asserted at its stronger form."""
    intent = submit_intent(hypothesis="an entity-scoped hypothesis", actor="ds1")
    # target_entity=None + templates=() keeps the governed-options lens out, so anything present
    # would have to have come from somewhere ungoverned.
    cs = build_considered_set(db, intent, _recommend_set_client(), catalog_source=None,
                              is_live=True, target_entity=None, templates=(), now=_NOW)
    assert cs.alternatives == []   # nothing ungoverned was even proposed
    # …so there is nothing for the filter to reject either: the rejection is the trace of a candidate
    # that WAS generated and then removed, and none can be generated here any more.
    assert not any(r.get("reason") == GOVERNED_CROSS_CATALOG_PLAN_REQUIRED for r in cs.rejections)


# ── (b) integration: build_considered_set surfaces the governed option under the flag ─────────────────
def test_build_considered_set_surfaces_governed_option_when_live(db):
    _cross_seed(db)
    intent = submit_intent(hypothesis="roll transactions up to the account", actor="ds1")
    cs = build_considered_set(
        db, intent, _recommend_set_client(), catalog_source=None, is_live=True,
        target_entity="account", templates=(_txn_template(),), applicability=None, now=_NOW)
    governed = [f for s in cs.alternatives for f in s.features if f.origin == "governed_planner"]
    assert len(governed) == 1
    assert governed[0].path_authority == "governed_cross_catalog"
    assert governed[0].plan_envelope is not None
    # authority rides on the IDEA, never the lens name
    assert all(s.lens != "governed" for s in cs.alternatives)


# ── 3C.2a CRITICAL: no ungoverned DEFINITION-MODE anchor is customer-visible when live (fail-closed) ──
def test_no_ungoverned_definition_anchor_on_a_live_entity_scoped_run(db):
    """The anchor half of the fail-closed guarantee. An entity-scoped run has NO single catalog to
    plan over, so a definition anchor built there could span >1 catalog with no governed physical
    plan — it had to be dropped and surfaced as a rejection. Since the E4 cutover (2026-08-14) the
    anchor comes from the engine's extraction, which needs a frozen catalog context: with no
    ``catalog_source`` there is no context, so there is NO anchor to drop. Honest absence rather than
    a free-form guess — and the customer-visible outcome the drop existed to produce."""
    intent = submit_intent(hypothesis="an entity-scoped hypothesis",
                           definition="a cross-catalog definition", actor="ds1")
    # target_entity=None + templates=() keeps the governed-options lens out of the way.
    cs = build_considered_set(db, intent, _recommend_set_client(), catalog_source=None,
                              is_live=True, target_entity=None, templates=(), now=_NOW)
    assert intent.intake_mode == "definition"   # the anchor path really was the one exercised
    assert cs.anchor is None                    # no ungoverned anchor is choosable at Gate #1


# ── (a) flag off → the governed branch never runs (byte-identical to today) ───────────────────────────
def test_flag_off_skips_the_governed_branch_entirely(db, monkeypatch):
    _minimal(db)

    def _boom(*a, **k):
        raise AssertionError("the governed branch must not run when is_live is False")

    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1._governed_cross_catalog_options", _boom)
    monkeypatch.setattr("featuregen.overlay.upload.contract.gate1._reject_cross_catalog_llm", _boom)
    intent = submit_intent(hypothesis="an entity-scoped hypothesis", actor="ds1")
    cs = build_considered_set(db, intent, _recommend_set_client(), catalog_source=None,
                              is_live=False, target_entity="account", now=_NOW)
    # Neither _boom fired: no plan_bindings compile, no cross-catalog filter. The set itself is empty
    # — an entity-only run has no candidate source at all since the E4 cutover (2026-08-14) — so what
    # this pins is the skip, proven by the booms that never raised.
    assert cs.alternatives == []
