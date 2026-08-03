"""Slice 3a-i Task 2 — the OperationalColumnFacts adapter (spec §4).

Asserts the governed-vs-hint authority boundary: authority comes from the DECISION log
(is_feature_eligible) or the governed *_fact_event_id link, NEVER from the flat display column;
the VALUE always comes from the flat graph_node column (the decision log stores only a HASH, so
no test — and no reader — ever dereferences a decision's load_bearing_value).
"""
import pytest

from featuregen.overlay.field_decision import FieldDecisionEventType, record_field_decision
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.column_authority import (
    OperationalColumnFacts,
    logical_ref_of,
    read_column_facts,
)
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

_SRC = "bank"
_OBJ = "public.accounts.balance"
_REF = normalize_ref(_SRC, "public", "accounts", "balance")   # "bank::public.accounts.balance"
_TBL_OBJ = "public.accounts"                                  # the TABLE node's graph object_ref


def _col(db, **cols):
    keys = ["catalog_source", "object_ref", "kind", "table_name", "column_name"]
    vals = [_SRC, _OBJ, "column", "accounts", "balance"]
    for k, v in cols.items():
        keys.append(k)
        vals.append(v)
    placeholders = ", ".join(["%s"] * len(vals))
    db.execute(f"INSERT INTO graph_node ({', '.join(keys)}) VALUES ({placeholders})", vals)


def _table_node(db, source=_SRC, object_ref=_TBL_OBJ, table_name="accounts", **cols):
    keys = ["catalog_source", "object_ref", "kind", "table_name", "column_name"]
    vals = [source, object_ref, "table", table_name, None]
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


def test_logical_ref_of_round_trips_public_flattened_ref(db):
    assert logical_ref_of(db, _SRC, _OBJ) == _REF


def test_logical_ref_of_is_schema_aware_for_a_real_schema_column(db):
    """The bug: logical_ref_of used to hardcode schema="public" no matter what the graph_node
    actually declared, so evidence/decisions recorded under a real (non-public) schema's
    schema-preserving logical_ref were never readable. It must now read graph_node.schema_name."""
    _col(db, schema_name="DPL_EIB_COMPLIANCE")
    expected = normalize_ref(_SRC, "DPL_EIB_COMPLIANCE", "accounts", "balance")
    assert expected == "bank::dpl_eib_compliance.accounts.balance"
    assert logical_ref_of(db, _SRC, _OBJ) == expected


def test_logical_ref_of_falls_back_to_public_when_schema_name_is_null(db):
    # An explicit graph_node row with schema_name NULL (public/technical upload) must still resolve
    # to the public-flattened ref — unchanged behavior.
    _col(db)   # schema_name defaults NULL
    assert logical_ref_of(db, _SRC, _OBJ) == _REF


def test_logical_ref_of_falls_back_to_public_when_no_graph_node_row_exists(db):
    # No row at all (e.g. a stale/derived object_ref) must not raise or crash — fall back to public.
    assert logical_ref_of(db, _SRC, _OBJ) == _REF


# ── Task 0C defect 1: object kind/schema come from the canonical graph row, not from positional
# guessing over the dot-count of the ref. The two-part TABLE ref `public.accounts` used to be read
# as table="public", column="accounts" and so became the phantom COLUMN ref
# `bank::public.public.accounts` — a table-anchored field decision then keyed under a column that
# does not exist. ────────────────────────────────────────────────────────────────────────────────


def test_logical_ref_of_resolves_a_table_node_as_a_table_ref_not_a_phantom_column(db):
    """The table-anchor field-decision fixture: the ref that keys a TABLE's evidence/decisions must
    be the schema-preserving TABLE ref. Positional guessing turned it into a phantom column."""
    _table_node(db)
    ref = logical_ref_of(db, _SRC, _TBL_OBJ)
    assert ref == normalize_ref(_SRC, "public", "accounts") == "bank::public.accounts"
    assert ref != "bank::public.public.accounts"          # the phantom the defect produced
    _src, _schema, table, column = parse_ref(ref)
    assert (table, column) == ("accounts", None)          # a TABLE decision key, not a column's


def test_logical_ref_of_preserves_a_table_nodes_real_schema(db):
    """A non-public source's TABLE node records its real (pre-flatten) schema in ``schema_name``;
    the rebuilt ref must carry it — on the TABLE identity, not on a phantom column."""
    _table_node(db, schema_name="DPL_EIB_COMPLIANCE")
    assert logical_ref_of(db, _SRC, _TBL_OBJ) == "bank::dpl_eib_compliance.accounts"


def test_logical_ref_of_keeps_the_same_table_name_distinct_across_schemas(db):
    """Same table name declared under two different real schemas (one per catalog source — the graph
    key is public-flattened, so within one source a table name has one schema row). The two refs
    must stay two distinct schema-preserving identities."""
    _table_node(db, source="bank_a", schema_name="crm")
    _table_node(db, source="bank_b", schema_name="fin")
    ref_a = logical_ref_of(db, "bank_a", _TBL_OBJ)
    ref_b = logical_ref_of(db, "bank_b", _TBL_OBJ)
    assert ref_a == "bank_a::crm.accounts"
    assert ref_b == "bank_b::fin.accounts"
    assert parse_ref(ref_a)[1:] != parse_ref(ref_b)[1:]   # distinct even ignoring the source


def test_logical_ref_of_column_ref_resolution_is_unchanged_beside_a_table_node(db):
    """The column-ref fixture: with BOTH the table node and its column node present, the 3-part
    column ref keeps resolving to exactly the ref it always did."""
    _table_node(db)
    _col(db)
    assert logical_ref_of(db, _SRC, _OBJ) == _REF


def test_logical_ref_of_rejects_an_ambiguous_two_part_ref_with_no_graph_row(db):
    """A two-part ref with no graph row to say what it is could be `schema.table` OR the legacy
    `table.column` spelling. Guessing is the defect; with no explicit kind it must be rejected."""
    with pytest.raises(ValueError):
        logical_ref_of(db, _SRC, "accounts.balance")


def test_logical_ref_of_resolves_a_two_part_legacy_spelling_with_explicit_kind(db):
    """The legacy two-part column spelling stays usable — but only when the caller SAYS it is a
    column (and a rowless table ref likewise says it is a table). Nothing is guessed."""
    assert logical_ref_of(db, _SRC, "accounts.balance", kind="column") == _REF
    assert logical_ref_of(db, _SRC, _TBL_OBJ, kind="table") == "bank::public.accounts"


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
