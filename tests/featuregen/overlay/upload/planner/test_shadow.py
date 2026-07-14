from tests.featuregen.overlay.upload.planner.test_plan import _NOW, _catalog, _tmpl

from featuregen.overlay.upload.planner.shadow import run_shadow_planner


def test_run_shadow_planner_logs_per_recipe(db, caplog):
    _catalog(db, "core")
    import logging
    with caplog.at_level(logging.INFO):
        results = run_shadow_planner(db, eligible_recipe_ids=frozenset({"t_bal"}),
                                     target_entity="customer", roles=(), run_id="run1", now=_NOW,
                                     templates=(_tmpl(),))
    assert len(results) == 1 and results[0].recipe_id == "t_bal"
