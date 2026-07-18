"""Slice 3a-i Task 2 — the OperationalColumnFacts adapter (spec §4).

Asserts the governed-vs-hint authority boundary: authority comes from the DECISION log
(is_feature_eligible) or the governed *_fact_event_id link, NEVER from the flat display column;
the VALUE always comes from the flat graph_node column (the decision log stores only a HASH, so
no test — and no reader — ever dereferences a decision's load_bearing_value).
"""
from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.column_authority import (
    OperationalColumnFacts,
    logical_ref_of,
    read_column_facts,
)
from featuregen.overlay.upload.object_ref import normalize_ref

_SRC = "bank"
_OBJ = "public.accounts.balance"
_REF = normalize_ref(_SRC, "public", "accounts", "balance")   # "bank::public.accounts.balance"


def _col(db, **cols):
    keys = ["catalog_source", "object_ref", "kind", "table_name", "column_name"]
    vals = [_SRC, _OBJ, "column", "accounts", "balance"]
    for k, v in cols.items():
        keys.append(k)
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    db.execute(f"INSERT INTO graph_node ({', '.join(keys)}) VALUES ({placeholders})", vals)


def _govern(db, field_name, value):
    """Record a load-bearing RESOLVED decision so is_feature_eligible(_REF, field) is True."""
    record_field_decision(
        db, logical_ref=_REF, field_name=field_name,
        event_type=FieldDecisionEventType.RESOLVED, selected_evidence_ids=[],
        evidence_set_hash=canonical_hash([]), display_value_hash=canonical_hash(value),
        load_bearing_value_hash=canonical_hash(value), conflict_status="resolved",
        reason_codes=[], field_policy_version="upload-field-policy-v1",
        resolver_version="upload-resolve-and-project-v1", actor_ref=None,
        supersedes_event_id=None)


def test_logical_ref_of_round_trips_public_flattened_ref():
    assert logical_ref_of(_SRC, _OBJ) == _REF


def test_additivity_hint_without_a_governing_decision(db):
    _col(db, additivity="non_additive", additivity_decision_id="fde_x")
    facts = read_column_facts(db, _REF, "additivity")
    assert isinstance(facts, OperationalColumnFacts)
    assert facts.value == "non_additive"     # flat display value still read
    assert facts.authority == "hint"         # no load-bearing decision -> not governed
    assert facts.provenance is None


def test_additivity_governed_reads_flat_value_and_decision_provenance(db):
    _col(db, additivity="non_additive", additivity_decision_id="fde_add_1")
    _govern(db, "additivity", "non_additive")
    facts = read_column_facts(db, _REF, "additivity")
    assert facts.value == "non_additive"
    assert facts.authority == "governed"
    assert facts.provenance == "fde_add_1"   # the *_decision_id link, never the load-bearing value


def test_logical_representation_value_is_operational_data_type(db):
    _col(db, data_type="unknown", declared_type="numeric",
         logical_type_decision_id="fde_lt_1")
    _govern(db, "logical_representation", "decimal")
    facts = read_column_facts(db, _REF, "logical_representation")
    assert facts.value == "unknown"          # numeric check uses OPERATIONAL data_type
    assert facts.authority == "governed"
    assert facts.provenance == "fde_lt_1"


def test_is_grain_governed_requires_flag_and_fact_event_id(db):
    _col(db, is_grain=True, grain_fact_event_id="evt_grain_1")
    facts = read_column_facts(db, _REF, "is_grain")
    assert facts.authority == "governed"
    assert facts.provenance == "evt_grain_1"
    assert facts.value == "true"             # RF-I7: BOOLEAN flat column coerced to str for egress


def test_is_grain_declared_not_confirmed_is_hint(db):
    _col(db, is_grain=True)               # flag true, grain_fact_event_id NULL -> file-declared only
    facts = read_column_facts(db, _REF, "is_grain")
    assert facts.authority == "hint"
    assert facts.provenance is None
    assert facts.value == "true"             # RF-I7 coercion applies on the hint path too


def test_is_as_of_governed_requires_availability_fact_event_id(db):
    _col(db, is_as_of=True, availability_fact_event_id="evt_av_1")
    facts = read_column_facts(db, _REF, "is_as_of")
    assert facts.authority == "governed"
    assert facts.provenance == "evt_av_1"
    assert facts.value == "true"


def test_declared_type_and_unit_and_currency_and_entity_are_hints(db):
    _col(db, declared_type="numeric", unit="dollars", currency="USD", entity="Account")
    for field_name, expected in [("declared_type", "numeric"), ("unit", "dollars"),
                                 ("currency", "USD"), ("entity", "Account")]:
        facts = read_column_facts(db, _REF, field_name)
        assert facts.authority == "hint", field_name
        assert facts.provenance is None, field_name
        assert facts.value == expected, field_name


def test_absent_node_reads_none_value_as_hint(db):
    facts = read_column_facts(db, _REF, "unit")
    assert facts == OperationalColumnFacts(value=None, authority="hint", provenance=None)
