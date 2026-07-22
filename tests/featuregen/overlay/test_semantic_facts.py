"""Delivery E / Task E1 — governed semantic fact types entity_assignment + currency_binding.

Registers two new governed, human-confirmed, column-referent fact types across every closed
fact/lifecycle registry (facts / _types / identity write-gate / dependencies / authority / lifecycle
/ projection replay), mirroring the existing single-source governed types. These tests exercise the
SAME command path as an existing governed type (propose -> confirm -> projected), the fail-closed
value + enforcement gates, the dependency index, the no-in-place-VERIFIED-mutation lifecycle rule,
and the owner-or-admin four-eyes authority.
"""
from __future__ import annotations

from typing import get_args

import pytest
from tests.featuregen._helpers import mint_test_identity, mint_test_service_identity
from tests.featuregen.overlay._helpers import seed_verified_via_command

from featuregen.contracts import Command
from featuregen.overlay import facts
from featuregen.overlay._types import FactType
from featuregen.overlay.commands import confirm_fact, enter_fact, propose_fact
from featuregen.overlay.dependencies import fact_dependencies
from featuregen.overlay.facts import FactValidationError, validate_fact_value
from featuregen.overlay.identity import CatalogObjectRef, fact_key
from featuregen.overlay.projection import current_fact, dependents_of
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.taxonomy.dimensions import known_entities

ENTITY = "customer"  # a stable member of the closed known_entities() vocabulary

ALICE = mint_test_identity(subject="user:alice", role_claims=("data_owner",))
EVE = mint_test_identity(subject="user:eve", role_claims=("data_owner",))
ADMIN = mint_test_identity(subject="user:admin", role_claims=("platform-admin",))
SVC = mint_test_service_identity(
    subject="service:overlay", role_claims=("overlay",), attestation="sig"
)


# --- ref / value builders ------------------------------------------------------------------------


def _entity_col() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "column", "sales", "customers", "cust_id")


def _measure_col() -> CatalogObjectRef:
    return CatalogObjectRef("pg:core", "column", "sales", "trades", "notional")


def _ccy_ref(schema="sales", table="trades", column="ccy", source="pg:core") -> dict:
    return {"catalog_source": source, "object_kind": "column", "schema": schema,
            "table": table, "column": column}


def _ea_value(entity_id: str = ENTITY) -> dict:
    return {"entity_id": entity_id}


def _cb_value(currency_column: dict | None = None) -> dict:
    return {"currency_column": currency_column or _ccy_ref()}


def _propose(db, *, ref, fact_type, value, actor=SVC, key="p"):
    return propose_fact(
        db,
        Command(
            "propose_fact", "overlay_fact", None,
            {"ref": ref, "fact_type": fact_type, "proposed_value": value}, actor, key,
        ),
    )


def _confirm(db, *, ref, fact_type, target, actor, key="c"):
    return confirm_fact(
        db,
        Command(
            "confirm_fact", "overlay_fact", None,
            {"ref": ref, "fact_type": fact_type, "target_event_id": target}, actor, key,
        ),
    )


# --- 1) registry completeness --------------------------------------------------------------------


def test_both_types_registered_across_closed_registries():
    for ft in (facts.ENTITY_ASSIGNMENT, facts.CURRENCY_BINDING):
        assert ft in facts.DATA_FACT_TYPES          # facts.py membership
        assert ft in facts.FACT_VALUE_SCHEMAS       # facts.py per-type value schema
        assert ft in get_args(FactType)             # _types.py Literal mirror
    assert facts.ENTITY_ASSIGNMENT == "entity_assignment"
    assert facts.CURRENCY_BINDING == "currency_binding"


def test_entity_assignment_round_trips_via_command_path(db):
    ref = _entity_col()
    key, _confirmed = seed_verified_via_command(
        db, ref=ref, fact_type="entity_assignment", value=_ea_value(), owner="user:alice"
    )
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"
    row = current_fact(db, key)
    assert row["status"] == "VERIFIED" and row["value"] == _ea_value()


def test_currency_binding_round_trips_via_command_path(db):
    ref = _measure_col()
    key, _confirmed = seed_verified_via_command(
        db, ref=ref, fact_type="currency_binding", value=_cb_value(), owner="user:alice"
    )
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"
    row = current_fact(db, key)
    assert row["status"] == "VERIFIED" and row["value"] == _cb_value()


# --- 2) value-schema validation (fail closed) ----------------------------------------------------


def test_entity_assignment_schema_rejects_target_ref_and_extras():
    validate_fact_value("entity_assignment", _ea_value())  # good
    with pytest.raises(FactValidationError):  # a stray target ref is forbidden (additionalProperties)
        validate_fact_value("entity_assignment", _ea_value() | {"currency_column": _ccy_ref()})
    with pytest.raises(FactValidationError):  # missing entity_id
        validate_fact_value("entity_assignment", {})


def test_currency_binding_schema_rejects_free_value_and_extras():
    validate_fact_value("currency_binding", _cb_value())  # good
    with pytest.raises(FactValidationError):  # a free value beyond currency_column is forbidden
        validate_fact_value("currency_binding", _cb_value() | {"rate": 1.0})
    with pytest.raises(FactValidationError):  # missing currency_column
        validate_fact_value("currency_binding", {})


def test_use_case_is_prohibited_on_both_types():
    with pytest.raises(FactValidationError):
        validate_fact_value("entity_assignment", _ea_value(), use_case="fraud")
    with pytest.raises(FactValidationError):
        validate_fact_value("currency_binding", _cb_value(), use_case="fraud")


# --- 3) enforcement (write gate, fail closed) ----------------------------------------------------


def test_unknown_entity_id_is_rejected(db, catalog):
    catalog.set_owner(_entity_col(), "user:alice")
    res = _propose(db, ref=_entity_col(), fact_type="entity_assignment",
                   value=_ea_value("not_a_real_entity"))
    assert res.accepted is False and "not a known entity" in res.denied_reason


def test_entity_assignment_requires_a_column_subject(db, catalog):
    table_ref = CatalogObjectRef("pg:core", "table", "sales", "customers")  # no column
    res = _propose(db, ref=table_ref, fact_type="entity_assignment", value=_ea_value())
    assert res.accepted is False and "must be a column" in res.denied_reason


def test_currency_binding_cross_table_target_is_rejected(db, catalog):
    catalog.set_owner(_measure_col(), "user:alice")
    other_table = _ccy_ref(table="fx_rates")  # target in a DIFFERENT table than the measure
    res = _propose(db, ref=_measure_col(), fact_type="currency_binding",
                   value=_cb_value(other_table))
    assert res.accepted is False and "same source/schema/table" in res.denied_reason


def test_currency_binding_cross_source_target_is_rejected(db, catalog):
    catalog.set_owner(_measure_col(), "user:alice")
    other_source = _ccy_ref(source="pg:other")  # cross-source binding is forbidden
    res = _propose(db, ref=_measure_col(), fact_type="currency_binding",
                   value=_cb_value(other_source))
    assert res.accepted is False and "same source/schema/table" in res.denied_reason


def test_currency_binding_target_must_reference_a_column(db, catalog):
    catalog.set_owner(_measure_col(), "user:alice")
    no_col = _ccy_ref(column=None)
    res = _propose(db, ref=_measure_col(), fact_type="currency_binding", value=_cb_value(no_col))
    assert res.accepted is False and "concrete column" in res.denied_reason


def test_use_case_present_is_rejected_by_propose(db, catalog):
    catalog.set_owner(_entity_col(), "user:alice")
    res = propose_fact(
        db,
        Command(
            "propose_fact", "overlay_fact", None,
            {"ref": _entity_col(), "fact_type": "entity_assignment",
             "use_case": "fraud", "proposed_value": _ea_value()}, SVC, "p",
        ),
    )
    assert res.accepted is False and "prohibits a use_case" in res.denied_reason


# --- 4) dependencies (subject + target; drift on either invalidates) ------------------------------


def test_fact_dependencies_currency_binding_indexes_subject_and_target():
    deps = fact_dependencies("sales.trades.notional", "currency_binding", _cb_value(), "pg:core")
    assert deps == {("pg:core", "sales.trades.notional"), ("pg:core", "sales.trades.ccy")}


def test_fact_dependencies_entity_assignment_indexes_only_the_subject_column():
    deps = fact_dependencies("sales.customers.cust_id", "entity_assignment", _ea_value(), "pg:core")
    assert deps == {("pg:core", "sales.customers.cust_id")}


def test_currency_binding_confirmed_fact_indexes_both_referents(db):
    ref = _measure_col()
    key, _ = seed_verified_via_command(
        db, ref=ref, fact_type="currency_binding", value=_cb_value(), owner="user:alice"
    )
    # The reverse index the drift-staler (_stale_dependents) walks: a drop/retype of EITHER the
    # measure or the target currency column resolves back to this fact_key and would STALE it.
    assert key in dependents_of(db, "pg:core", "sales.trades.notional")
    assert key in dependents_of(db, "pg:core", "sales.trades.ccy")


# --- 5) terminal / reverify: no in-place VERIFIED mutation ---------------------------------------


def test_reproposing_a_changed_target_does_not_mutate_verified_in_place(db):
    ref = _measure_col()
    key, _ = seed_verified_via_command(
        db, ref=ref, fact_type="currency_binding", value=_cb_value(), owner="user:alice"
    )
    assert fold_overlay_state(load_fact(db, key)).status == "VERIFIED"
    # A fresh proposal with a DIFFERENT target over a live VERIFIED fact is DENIED — the value can
    # only change via the terminal/reverify lifecycle, never by re-propose.
    changed = _cb_value(_ccy_ref(column="settlement_ccy"))
    res = _propose(db, ref=ref, fact_type="currency_binding", value=changed)
    assert res.accepted is False and "non-terminal fact already exists" in res.denied_reason
    row = current_fact(db, key)
    assert row["status"] == "VERIFIED" and row["value"] == _cb_value()  # unchanged in place


# --- 6) authority: source owner OR platform admin; four-eyes; non-owner refused ------------------


def test_source_owner_can_confirm(db, catalog):
    catalog.set_owner(_entity_col(), "user:alice")
    draft = _propose(db, ref=_entity_col(), fact_type="entity_assignment", value=_ea_value()).produced_event_ids[0]
    res = _confirm(db, ref=_entity_col(), fact_type="entity_assignment", target=draft, actor=ALICE)
    assert res.accepted is True
    assert fold_overlay_state(load_fact(db, fact_key(_entity_col(), "entity_assignment"))).status == "VERIFIED"


def test_platform_admin_can_confirm_even_when_owner_is_known(db, catalog):
    # owner-or-admin (E1): a platform admin is an accepted confirmer ALONGSIDE the known owner.
    catalog.set_owner(_measure_col(), "user:alice")
    draft = _propose(db, ref=_measure_col(), fact_type="currency_binding", value=_cb_value()).produced_event_ids[0]
    res = _confirm(db, ref=_measure_col(), fact_type="currency_binding", target=draft, actor=ADMIN)
    assert res.accepted is True, res.denied_reason
    assert fold_overlay_state(load_fact(db, fact_key(_measure_col(), "currency_binding"))).status == "VERIFIED"


def test_four_eyes_proposer_cannot_confirm_own_fact(db, catalog):
    # The proposer (a human owner) may not also confirm — proposer != confirmer still holds even for
    # a single-owner governed fact.
    catalog.set_owner(_entity_col(), "user:alice")
    draft = _propose(db, ref=_entity_col(), fact_type="entity_assignment",
                     value=_ea_value(), actor=ALICE).produced_event_ids[0]
    res = _confirm(db, ref=_entity_col(), fact_type="entity_assignment", target=draft, actor=ALICE)
    assert res.accepted is False and "four-eyes" in res.denied_reason


def test_non_owner_non_admin_is_refused(db, catalog):
    catalog.set_owner(_entity_col(), "user:alice")
    draft = _propose(db, ref=_entity_col(), fact_type="entity_assignment", value=_ea_value()).produced_event_ids[0]
    res = _confirm(db, ref=_entity_col(), fact_type="entity_assignment", target=draft, actor=EVE)
    assert res.accepted is False and "not the resolved authority" in res.denied_reason


def test_enter_fact_self_confirm_is_blocked_for_governed_semantic_facts(db, catalog):
    # enter_fact is the single-party self-confirm exception; it must be denied for these types so
    # four-eyes always holds (one principal may not propose AND approve the same value).
    catalog.set_owner(_entity_col(), "user:alice")
    res = enter_fact(
        db,
        Command(
            "enter_fact", "overlay_fact", None,
            {"ref": _entity_col(), "fact_type": "entity_assignment", "proposed_value": _ea_value()},
            ALICE, "e",
        ),
    )
    assert res.accepted is False and "two-party propose/confirm" in res.denied_reason
    stream = load_fact(db, fact_key(_entity_col(), "entity_assignment"))
    assert not any(e.type == "OVERLAY_FACT_CONFIRMED" for e in stream)


def test_known_entities_vocabulary_is_nonempty_and_contains_the_probe():
    # Guards the enforcement test's probe entity against vocabulary drift.
    assert ENTITY in known_entities()
