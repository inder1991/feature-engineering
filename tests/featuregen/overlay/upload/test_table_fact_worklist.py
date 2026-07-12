"""Task 11: pending-proposal worklist reader — a READ MODEL over the existing human_tasks gate
tasks (NOT a new queue/lifecycle). An open grain/availability proposal appears with its proposed
value + CAS target; a confirmed one drops off (its gate task is no longer open). Each row surfaces
uniqueness_basis="llm_proposed_not_profiled" so a reviewer sees the origin is an unprofiled LLM
proposal, not proof.
"""
from featuregen.overlay.upload.table_fact_projection import list_open_table_fact_proposals
from featuregen.overlay.upload.table_synth import _propose_table_facts
from tests.featuregen.overlay.upload.conftest import _confirm_grain


def _propose_grain(conn, columns, *, actor):
    _propose_table_facts(conn, "src",
                         {"txn": {"grain": {"columns": columns, "is_unique": True},
                                  "availability_time": None,
                                  "table_role": None, "primary_entity": None}},
                         actor=actor, source_snapshot_id="snap-test")


def test_open_grain_proposal_appears(overlay_conn, service_actor):
    _propose_grain(overlay_conn, ["id"], actor=service_actor)
    work = list_open_table_fact_proposals(overlay_conn)
    assert any(w["fact_type"] == "grain" and w["proposed_value"]["columns"] == ["id"]
               for w in work)


def test_worklist_row_surfaces_origin_and_cas_target(overlay_conn, service_actor):
    _propose_grain(overlay_conn, ["id"], actor=service_actor)
    row = next(w for w in list_open_table_fact_proposals(overlay_conn)
               if w["fact_type"] == "grain" and w["object_ref"] == "public.txn")
    # Origin surfacing (must-fix #4): an unprofiled LLM proposal, not proof.
    assert row["uniqueness_basis"] == "llm_proposed_not_profiled"
    # The CAS target a confirmer needs is carried through from the gate task.
    assert row["task_id"] and row["target_event_id"]


def test_confirmed_proposal_drops_off(overlay_conn, service_actor, human_actor):
    _propose_grain(overlay_conn, ["id"], actor=service_actor)   # opens the gate task
    # Guard against a vacuous pass: the proposal must be visible BEFORE confirmation.
    assert any(w["object_ref"] == "public.txn" and w["fact_type"] == "grain"
               for w in list_open_table_fact_proposals(overlay_conn))
    _confirm_grain(overlay_conn, "src", "txn", ["id"], actor=human_actor)  # helper -> VERIFIED
    assert all(w["object_ref"] != "public.txn" or w["fact_type"] != "grain"
               for w in list_open_table_fact_proposals(overlay_conn))
