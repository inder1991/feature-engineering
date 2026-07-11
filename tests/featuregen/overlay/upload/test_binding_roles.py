"""Phase-3B.1 Task 1 — the governed join/temporal role vocabularies (leaf enums)."""
from __future__ import annotations

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole


def test_join_role_members():
    assert {r.value for r in JoinRole} == {
        "source_entity_key", "target_entity_key", "intermediate_entity_key", "measure", "time"}


def test_temporal_role_members():
    assert {r.value for r in TemporalRole} == {
        "none", "event_time", "as_of_time", "ingestion_time", "valid_from", "valid_to"}


from featuregen.overlay.upload.templates import ALL_TEMPLATES, Need


def test_existing_need_construction_unchanged():
    # the pre-3B.1 positional constructor still works and the new fields default empty/None
    n = Need("entity", "customer_id")
    assert n.allowed_source_grains == ()
    assert n.join_role is None and n.temporal_role is None


def test_need_accepts_explicit_binding_overrides():
    n = Need("stock", "monetary_stock", allowed_source_grains=("account",), join_role=JoinRole.MEASURE)
    assert n.allowed_source_grains == ("account",)
    assert n.join_role is JoinRole.MEASURE


def test_template_gains_anchor_fields_defaulting_none():
    t = ALL_TEMPLATES[0]
    assert t.source_entity is None or isinstance(t.source_entity, str)
    assert t.source_entity_need_role is None or isinstance(t.source_entity_need_role, str)
