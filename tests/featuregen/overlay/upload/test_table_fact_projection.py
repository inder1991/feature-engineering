"""Task 9: SPECIALIZED_FACT projection bridge — confirmed grain/as-of -> graph_node.

The load-bearing truth is the fact stream (resolve_fact, VERIFIED-only); this bridge PROJECTS it
onto the physical column nodes (is_grain/is_as_of + the confirmed-event provenance id). The
projection is clear-then-set (idempotent): a grain that changes columns / expires / is rejected
must never leave a stale ``is_grain=true`` on an old column.

Flow per the Task 7 helpers: ``_propose_table_facts`` (service actor) opens the gate task,
``_confirm_grain`` (platform-admin human) confirms it -> VERIFIED and drains the projection so
``resolve_fact`` sees the read model.
"""
from featuregen.overlay.upload.table_fact_projection import project_table_facts
from featuregen.overlay.upload.table_synth import _propose_table_facts
from tests.featuregen.overlay.upload.conftest import _confirm_grain, _reconfirm_grain


def _propose_grain(conn, columns, *, actor):
    _propose_table_facts(conn, "src",
                         {"txn": {"grain": {"columns": columns, "is_unique": True},
                                  "availability_time": None,
                                  "table_role": None, "primary_entity": None}},
                         actor=actor, source_snapshot_id="snap-test")


def test_confirmed_grain_sets_is_grain_on_columns(overlay_conn, service_actor, human_actor,
                                                  seeded_graph):
    # seeded_graph: a source "src" with table "txn" columns id/amt/txn_id (all is_grain=false)
    _propose_grain(overlay_conn, ["id"], actor=service_actor)
    _confirm_grain(overlay_conn, "src", "txn", ["id"], actor=human_actor)   # helper -> VERIFIED
    project_table_facts(overlay_conn, source="src", tables=["txn"])
    rows = overlay_conn.execute(
        "SELECT column_name, is_grain, grain_fact_event_id FROM graph_node "
        "WHERE catalog_source='src' AND table_name='txn' AND kind='column'").fetchall()
    grain = {c for c, g, _e in rows if g}
    assert grain == {"id"}
    # The provenance link: the confirmed event id (resolve_fact provenance) MUST land — a
    # getattr-on-a-missing-attribute implementation would silently write NULL here.
    event_ids = {e for c, g, e in rows if g}
    assert event_ids and None not in event_ids


def test_proposed_but_unconfirmed_grain_projects_nothing(overlay_conn, service_actor, seeded_graph):
    _propose_grain(overlay_conn, ["id"], actor=service_actor)
    project_table_facts(overlay_conn, source="src", tables=["txn"])
    rows = overlay_conn.execute(
        "SELECT is_grain FROM graph_node WHERE catalog_source='src' AND table_name='txn' "
        "AND kind='column'").fetchall()
    assert not any(g for (g,) in rows)   # PROPOSED is not load-bearing


def test_reprojection_clears_stale_grain_flags(overlay_conn, service_actor, human_actor,
                                               seeded_graph):
    # THE idempotency guarantee: a confirmed grain that later CHANGES columns must not leave the old
    # column flagged. Confirm grain=[id], project; then confirm a replacement grain=[amt] (after the
    # first expires/re-verifies), re-project, and assert `id` is now false and `amt` is true.
    _propose_grain(overlay_conn, ["id"], actor=service_actor)
    _confirm_grain(overlay_conn, "src", "txn", ["id"], actor=human_actor)
    project_table_facts(overlay_conn, source="src", tables=["txn"])
    _reconfirm_grain(overlay_conn, "src", "txn", ["amt"], actor=human_actor)  # helper -> new VERIFIED
    project_table_facts(overlay_conn, source="src", tables=["txn"])
    flags = dict(overlay_conn.execute(
        "SELECT column_name, is_grain FROM graph_node WHERE catalog_source='src' "
        "AND table_name='txn' AND kind='column'").fetchall())
    assert flags["id"] is False and flags["amt"] is True
