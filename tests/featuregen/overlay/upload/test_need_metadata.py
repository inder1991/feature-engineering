"""Phase-3B.1 Tasks 3/4 — derive governed per-need binding metadata (grain constraint / join role /
temporal role) from concept.entity_link, concept.pit_role, and the EXPLICIT template anchor."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.need_metadata import (
    derive_need_metadata,
    validate_template_anchor,
)
from featuregen.overlay.upload.templates import Need, Template


def _t(needs, **over) -> Template:
    base = dict(id="t", family="f", intent="i", needs=tuple(needs), params={}, aggregation="avg",
                additivity="additive", explain="M", use_cases=(), pit="trailing window")
    base.update(over)
    return Template(**base)


def test_identifier_need_grain_constrained_and_is_source_anchor():
    # a single entity-linked need is the unambiguous source anchor; its grain is constrained to its entity
    t = _t([Need("entity", "customer_id")])
    (m,) = derive_need_metadata(t)
    assert m.allowed_source_grains == ("customer",)          # from concept.entity_link (customer_id -> customer)
    assert m.join_role is JoinRole.SOURCE_ENTITY_KEY
    assert m.grain_source == "concept_registry"


def test_measure_need_is_grain_unconstrained_and_measure_role():
    # a non-identifier measure (monetary_stock has no entity_link) -> unconstrained grain, MEASURE role
    t = _t([Need("entity", "customer_id"), Need("stock", "monetary_stock")])
    metas = {m.role: m for m in derive_need_metadata(t)}
    assert metas["stock"].allowed_source_grains == ()         # unconstrained; actual grain derived at bind time
    assert metas["stock"].join_role is JoinRole.MEASURE


def test_temporal_role_from_pit_role_not_name():
    # temporal role comes from concept.pit_role (governed), never the column/concept name.
    # event_timestamp has pit_role 'event'; customer_id has pit_role 'none'.
    t = _t([Need("entity", "customer_id"), Need("event_ts", "event_timestamp")])
    metas = {m.role: m for m in derive_need_metadata(t)}
    assert metas["event_ts"].temporal_role is TemporalRole.EVENT_TIME
    assert metas["event_ts"].join_role is JoinRole.TIME
    assert metas["entity"].temporal_role is TemporalRole.NONE


def test_multi_distinct_entity_without_anchor_is_rejected():
    t = _t([Need("cust", "customer_id"), Need("acct", "account_id")])   # two distinct entity keys, no anchor
    with pytest.raises(ValueError, match="distinct entity keys"):
        validate_template_anchor(t)


def test_explicit_anchor_resolves_multi_entity_recipe():
    t = _t([Need("cust", "customer_id"), Need("acct", "account_id")],
           source_entity_need_role="acct")
    metas = {m.role: m for m in derive_need_metadata(t)}          # no raise
    assert metas["acct"].join_role is JoinRole.SOURCE_ENTITY_KEY
    assert metas["cust"].join_role is JoinRole.INTERMEDIATE_ENTITY_KEY


def test_anchor_role_must_name_an_entity_need():
    t = _t([Need("cust", "customer_id"), Need("acct", "account_id")],
           source_entity_need_role="balance")                    # not an entity-linked need
    with pytest.raises(ValueError, match="not an entity-linked need"):
        validate_template_anchor(t)


def test_explicit_anchor_naming_non_entity_need_rejected_even_single_entity():
    # a single-entity (or measure-only) template with an anchor pointing at a NON-entity need must raise
    t = _t([Need("entity", "customer_id"), Need("stock", "monetary_stock")],
           source_entity_need_role="stock")
    with pytest.raises(ValueError, match="not an entity-linked need"):
        validate_template_anchor(t)


def test_explicit_field_overrides_win():
    t = _t([Need("entity", "customer_id",
                 allowed_source_grains=("account",), temporal_role=TemporalRole.AS_OF_TIME)])
    (m,) = derive_need_metadata(t)
    assert m.allowed_source_grains == ("account",) and m.grain_source == "explicit_recipe"
    assert m.temporal_role is TemporalRole.AS_OF_TIME and m.temporal_role_source == "explicit_recipe"
