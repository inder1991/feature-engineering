"""Slice 3A-iv Task 4 (RF-I9): the considered-set snapshot is NOT flag-gated.

RF-I9 REVISES the plan's original "flag-gate the snapshot" step. The internal governed snapshot
(`_idea_json`) ALWAYS serializes ``validation_status`` + ``requirements``: they are produced by the
always-on tri-state validator and feed the confirm-time MCV reconstruction (RF-C1/C2). Flag-OFF
byte-identity applies to the ``/features/recommend`` RESPONSE + the outbound LLM request payload only
(Tasks 2/3 + the enriched-menu gate), NOT this internal record. Gating the snapshot would drop the
honest tri-state flag-OFF and reopen the 3A-i F1 safety window (a non-additive SUM could confirm
DESIGN-CHECKED because the reconstructed requirements went missing). This test LOCKS the invariant:
the governed tri-state rides the snapshot regardless of the display flag, and the
``_idea_json`` -> ``_idea_from_json`` round-trip preserves it either way.
"""
from __future__ import annotations

from featuregen.overlay.upload.contract.gate1 import _idea_from_json, _idea_json
from featuregen.overlay.upload.feature_assist import FeatureIdea, Requirement


def _idea_with_new_fields() -> FeatureIdea:
    # Non-default Slice-3 fields, so a flag-gate that dropped them would be visible.
    return FeatureIdea(
        name="avg_balance", description="average balance", derives_from=["public.accounts.balance"],
        aggregation="avg", grain_table="accounts",
        derives_pairs=(("deposits", "public.accounts.balance"),),
        verification="DESIGN-CHECKED", critic_note="", rationale="why",
        operation_kind="avg", measure_refs=(("deposits", "public.accounts.balance"),),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("TYPE_IS_NUMERIC", ("deposits", "public.accounts.balance"),
                                  "verify"),))


def test_none_snapshots_to_none():
    assert _idea_json(None) is None


def test_snapshot_always_carries_governed_tristate_flag_off(monkeypatch):
    # RF-I9: flag-OFF, the governed snapshot STILL carries the honest tri-state (NOT stripped).
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    out = _idea_json(_idea_with_new_fields())
    assert out["validation_status"] == "NEEDS_EXTERNAL_VALIDATION"
    assert out["requirements"] == [
        {"code": "TYPE_IS_NUMERIC", "operand": ["deposits", "public.accounts.balance"],
         "detail": "verify"}]


def test_snapshot_is_flag_independent(monkeypatch):
    # The governed snapshot must be identical flag-OFF and flag-ON — the display flag never changes
    # the internal record (that is exactly why byte-identity was scoped to the RESPONSE, not here).
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    off = _idea_json(_idea_with_new_fields())
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    on = _idea_json(_idea_with_new_fields())
    assert off == on


def test_round_trip_preserves_tristate_flag_off(monkeypatch):
    # The confirm-time reconstruction (_idea_from_json) restores the honest tri-state even flag-OFF,
    # so a flag-OFF confirm still re-runs the MCV over the real requirements (no F1 window reopens).
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    idea = _idea_with_new_fields()
    restored = _idea_from_json(_idea_json(idea))
    assert restored.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert restored.requirements == idea.requirements
    assert restored.derives_pairs == idea.derives_pairs
    assert restored.verification == "DESIGN-CHECKED"
