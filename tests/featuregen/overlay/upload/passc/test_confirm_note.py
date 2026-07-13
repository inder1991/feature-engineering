"""Confirmation-surface Task 1 — the optional approver note rides the confirm events.

A dual approved_join takes the two-step PARTIALLY_CONFIRMED -> CONFIRMED flow
(`join_confirmation._confirm_approved_join`): the FIRST admin's note must land in the
OVERLAY_FACT_PARTIALLY_CONFIRMED payload (so the second approver can read it), and the SECOND
admin's note in OVERLAY_FACT_CONFIRMED. Backward-compatible: `cmd.args.get("note")` -> None when
absent. Task 3's approval reader depends on this key.
"""
from __future__ import annotations

from tests.featuregen.overlay.upload.passc.conftest import _propose_join

from featuregen.contracts.envelopes import Command
from featuregen.overlay._lifecycle import _cas_target
from featuregen.overlay.confirmation_commands import confirm_fact
from featuregen.overlay.identity import fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.passc.candidates import block_candidates, score
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.lifecycle import build_join_ref

_CIF_TERM = "Customer Information File Identifier"


# ── Evidence/ref builders (the Task-7 test shapes: one blocked pair, scored) ─────────────────────


def _c(table, column, **kw):
    b = dict(object_ref=f"src::public.{table}.{column}", table=table, column=column,
             data_type="text", term_name="", term_type="", concept="", synonyms="",
             bian_leaf="", fibo_leaf="", table_entity="", column_entity="",
             data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


def _strong_evidence():
    """A strong, grain-inferred N:1 candidate: transactions.cif_id -> customers.cif_id."""
    pairs = block_candidates([_c("transactions", "cif_id", term_name=_CIF_TERM),
                              _c("customers", "cif_id", term_name=_CIF_TERM, is_grain=True)])
    assert len(pairs) == 1, "test setup must yield exactly one blocked pair"
    ev = score(pairs[0], source_snapshot_id="snap-1")
    assert ev.bucket == "strong" and ev.proposed_cardinality == "N:1"
    return ev


def _proposed_dual_join(conn):
    """Propose the dual (both-unknown -> governance-queue) approved_join; return (ref, key)."""
    ref = build_join_ref(_strong_evidence(), "src")
    _propose_join(conn, ref)
    return ref, fact_key(ref, "approved_join")


def _confirm_cmd(ref, target_event_id, actor, *, ik, note=None):
    return Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "use_case": None,
         "target_event_id": target_event_id, "note": note},
        actor, ik)


# ── The note rides the confirm events ────────────────────────────────────────────────────────────


def test_partial_confirm_persists_note(passc_conn, human_admin_1):
    ref, key = _proposed_dual_join(passc_conn)
    target = _cas_target(fold_overlay_state(load_fact(passc_conn, key)))   # the DRAFT head
    res = confirm_fact(passc_conn, _confirm_cmd(
        ref, target, human_admin_1, ik=f"confirm1-{target}", note="check the CIF namespace"))
    assert res.accepted, res.denied_reason
    stream = load_fact(passc_conn, key)
    partial = [e for e in stream if e.type == "OVERLAY_FACT_PARTIALLY_CONFIRMED"][-1]
    assert partial.payload["note"] == "check the CIF namespace"


def test_second_confirm_persists_its_own_note(passc_conn, human_admin_1, human_admin_2):
    """admin1 partial-confirms with one note, DISTINCT admin2 fully confirms with another: each
    note rides ITS confirmer's event — CONFIRMED carries admin2's, the partial keeps admin1's."""
    ref, key = _proposed_dual_join(passc_conn)
    target = _cas_target(fold_overlay_state(load_fact(passc_conn, key)))
    first = confirm_fact(passc_conn, _confirm_cmd(
        ref, target, human_admin_1, ik=f"confirm1-{target}", note="check the CIF namespace"))
    assert first.accepted, first.denied_reason

    state = fold_overlay_state(load_fact(passc_conn, key))    # re-read between the two confirms
    assert state.status == "PARTIALLY_CONFIRMED", state.status
    target = _cas_target(state)
    second = confirm_fact(passc_conn, _confirm_cmd(
        ref, target, human_admin_2, ik=f"confirm2-{target}", note="namespace verified, approving"))
    assert second.accepted, second.denied_reason

    stream = load_fact(passc_conn, key)
    confirmed = [e for e in stream if e.type == "OVERLAY_FACT_CONFIRMED"][-1]
    assert confirmed.payload["note"] == "namespace verified, approving"
    partial = [e for e in stream if e.type == "OVERLAY_FACT_PARTIALLY_CONFIRMED"][-1]
    assert partial.payload["note"] == "check the CIF namespace"   # admin1's note untouched


def test_confirm_without_note_persists_none(passc_conn, human_admin_1):
    """Backward-compatible: a confirm Command whose args OMIT `note` still lands `note: None`."""
    ref, key = _proposed_dual_join(passc_conn)
    target = _cas_target(fold_overlay_state(load_fact(passc_conn, key)))
    cmd = Command(
        "confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "approved_join", "use_case": None,
         "target_event_id": target},                          # no "note" key at all
        human_admin_1, f"confirm1-{target}")
    res = confirm_fact(passc_conn, cmd)
    assert res.accepted, res.denied_reason
    partial = [e for e in load_fact(passc_conn, key)
               if e.type == "OVERLAY_FACT_PARTIALLY_CONFIRMED"][-1]
    assert partial.payload["note"] is None
