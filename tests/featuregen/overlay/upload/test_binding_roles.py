"""Phase-3B.1 Task 1 — the governed join/temporal role vocabularies (leaf enums)."""
from __future__ import annotations

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole


def test_join_role_members():
    assert {r.value for r in JoinRole} == {
        "source_entity_key", "target_entity_key", "intermediate_entity_key", "measure", "time"}


def test_temporal_role_members():
    assert {r.value for r in TemporalRole} == {
        "none", "event_time", "as_of_time", "ingestion_time", "valid_from", "valid_to"}
