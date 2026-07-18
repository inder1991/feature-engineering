from __future__ import annotations

from datetime import UTC, datetime

from featuregen.overlay.upload.planner.gate_operate import select_window

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


def test_out_of_range_runs_are_excluded(db):
    _dispatch(db, "inrange", at=_T0)
    _dispatch(db, "before", at=datetime(2026, 7, 1, tzinfo=UTC))
    sel = select_window(db, cohort="sha1", since=_T0, until=datetime(2026, 7, 19, tzinfo=UTC))
    assert set(sel.run_ids) == {"inrange"}


def test_empty_window_is_reproducible_and_empty(db):
    sel = select_window(db, cohort="ghost", since=_T0, until=datetime(2026, 7, 19, tzinfo=UTC))
    assert sel.run_ids == () and sel.coverage.qualifying == 0
