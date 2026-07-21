from __future__ import annotations

from datetime import UTC, datetime

from featuregen.overlay.upload.planner.gate_operate import (
    run_double_compile,
    run_drift_checks,
    run_gold_suite,
    select_window,
)

_T0 = datetime(2026, 7, 18, tzinfo=UTC)


def _dispatch(db, rid, *, cohort="sha1", compile=True, telem=True, scoped=True, ranking=True,
              at=_T0):
    db.execute(
        "INSERT INTO planner_shadow_dispatch (generation_run_id, eligible_recipe_ids, recipe_hash,"
        " expected_count, invocation_predicate, compile_flag, telemetry_flag, scoped_applicability_flag,"
        " ranking_flag, applicability_version, producer_commit, compiler_versions, compiler_versions_hash,"
        " payload_schema_version, created_at) VALUES (%s,'{}','h',0,'p',%s,%s,%s,%s,'v',%s,'{}','ch','pv',%s)",
        (rid, compile, telem, scoped, ranking, cohort, at))


def test_only_fully_qualifying_runs_are_selected(db):
    _dispatch(db, "ok1")
    _dispatch(db, "ok2")
    _dispatch(db, "no_scope", scoped=False)          # a flag off -> excluded
    _dispatch(db, "null_rank", ranking=None)         # unprovable (NULL) -> excluded
    _dispatch(db, "other_cohort", cohort="sha2")     # wrong cohort -> excluded
    _dispatch(db, "uncertified", cohort="unset")     # sentinel cohort is never selectable
    sel = select_window(db, cohort="sha1", since=_T0, until=datetime(2026, 7, 19, tzinfo=UTC))
    assert set(sel.run_ids) == {"ok1", "ok2"}
    assert sel.coverage.qualifying == 2
    assert sel.coverage.excluded["flag_off"] == 1
    assert sel.coverage.excluded["flag_unprovable"] == 1
    assert sel.coverage.excluded["wrong_cohort"] == 2  # other_cohort + uncertified are not this cohort


def test_unset_cohort_is_never_selectable_even_when_requested_verbatim(db):
    # A caller passing the 'unset' sentinel AS the cohort must select nothing, even though rows
    # with producer_commit='unset' match it verbatim — guards the `or cohort == "unset"` disjunct.
    _dispatch(db, "uncertified", cohort="unset")
    sel = select_window(db, cohort="unset", since=_T0, until=datetime(2026, 7, 19, tzinfo=UTC))
    assert sel.run_ids == ()
    assert sel.coverage.dispatched_in_range == 1
    assert sel.coverage.excluded["wrong_cohort"] == 1


def test_out_of_range_runs_are_excluded(db):
    _dispatch(db, "inrange", at=_T0)
    _dispatch(db, "before", at=datetime(2026, 7, 1, tzinfo=UTC))
    sel = select_window(db, cohort="sha1", since=_T0, until=datetime(2026, 7, 19, tzinfo=UTC))
    assert set(sel.run_ids) == {"inrange"}


def test_empty_window_is_reproducible_and_empty(db):
    sel = select_window(db, cohort="ghost", since=_T0, until=datetime(2026, 7, 19, tzinfo=UTC))
    assert sel.run_ids == () and sel.coverage.qualifying == 0


def test_gold_suite_matches_the_live_classifier(db):
    report = run_gold_suite(db)
    assert report.passed and report.false_resolves == ()


def test_double_compile_is_stable_on_the_frozen_gold_fixtures(db):
    result = run_double_compile(db)
    assert result.stable and result.compared >= 1 and result.mismatched_keys == ()


def test_drift_checks_detect_every_controlled_mutation(db):
    assert run_drift_checks(db) == 1.0


def test_drivers_leave_no_durable_catalog_state(db):
    # the controlled drivers seed the RESERVED __gate_gold__ source but roll it back — no rows survive
    run_gold_suite(db)
    run_double_compile(db)
    run_drift_checks(db)
    remaining = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = '__gate_gold__'").fetchone()[0]
    assert remaining == 0


def test_gold_seed_targets_reserved_source_and_leaves_a_real_core_untouched(db):
    """[8] A bank naming its catalog 'core' must be UNTOUCHED by the gate console. The gold fixture
    now seeds the RESERVED __gate_gold__ source, so `build_graph`'s DELETE-this-source-then-reinsert
    can no longer wipe (or lock) the real 'core' graph rows. Discriminating: the OLD seed built
    'core' — its DELETE would drop the real row and its 6 fixture rows would replace it."""
    from featuregen.overlay.upload.canonical import CanonicalRow
    from featuregen.overlay.upload.enrich import content_hash
    from featuregen.overlay.upload.graph import build_graph
    from featuregen.overlay.upload.planner import contract_gold

    real = CanonicalRow("core", "customers", "id", "integer", is_grain=True)
    build_graph(db, "core", [real], concepts={content_hash(real): "customer_id"})
    core_before = db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'core'").fetchone()[0]
    assert core_before > 0

    contract_gold._seed(db)   # seed the gold fixture DIRECTLY (no rollback wrapper)

    assert db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = '__gate_gold__'"
    ).fetchone()[0] > 0                                        # gold rows land under the reserved source
    assert db.execute(
        "SELECT count(*) FROM graph_node WHERE catalog_source = 'core'"
    ).fetchone()[0] == core_before                            # the real 'core' catalog is UNTOUCHED
