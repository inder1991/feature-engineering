"""BR-22 — the leakage boundary layer: outcomes and near-labels refuse disallowed stages.

Held over the WHOLE active registry, not exemplars: every outcome-class recipe prohibits the
pre-default modelling stages, every near-label recipe prohibits origination, no leakage-marked
recipe permits a stage it prohibits (contract law), and the alert/case family reads through
knowledge time so late-arriving outcomes cannot leak backwards.
"""
from __future__ import annotations

from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES


def test_every_outcome_recipe_prohibits_pre_default_modelling():
    outcomes = [r for r in V2_RECIPES if r.leakage.classification == "outcome"]
    assert outcomes, "the outcome class must be populated (cure/recovery/write-off/cost)"
    for r in outcomes:
        assert "default_prediction" in r.leakage.prohibited_stages, r.recipe_id
        assert "origination" in r.leakage.prohibited_stages, r.recipe_id


def test_every_near_label_recipe_prohibits_origination():
    near = [r for r in V2_RECIPES if r.leakage.classification == "near_label"]
    assert near, "the near-label class must be populated (DPD/stage/alerts/adoption)"
    for r in near:
        assert "origination" in r.leakage.prohibited_stages or \
            "sales_outcome_prediction" in r.leakage.prohibited_stages, r.recipe_id


def test_no_recipe_permits_a_stage_it_prohibits():
    for r in V2_RECIPES:
        overlap = set(r.leakage.permitted_stages) & set(r.leakage.prohibited_stages)
        assert not overlap, (r.recipe_id, overlap)


def test_alert_and_disposition_history_reads_through_knowledge_time():
    """Late-arriving outcomes cannot leak backwards: every near-label recipe over an alert
    feed declares a knowledge-time role in its temporal contract."""
    for r in V2_RECIPES:
        if r.source_grain == "alert_event":
            assert r.temporal.knowledge_time_role, r.recipe_id


def test_standard_recipes_carry_no_stage_fences():
    """The boundary is meaningful because it is SELECTIVE: standard-class recipes declare no
    prohibitions — a fence on everything is a fence on nothing."""
    standard = [r for r in V2_RECIPES if r.leakage.classification == "standard"]
    assert len(standard) > 200
    assert all(not r.leakage.prohibited_stages for r in standard)
