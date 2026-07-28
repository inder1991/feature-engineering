"""Physical identity — the address a data observation attaches to.

Release 1 step 1. Before anything reads real data, the system must be certain **which** physical
object it read. Today the catalog's `normalize_ref` defaults a missing schema to `public`
(`object_ref.py:85`), which is right for a display/attachment key and wrong for a physical address:
it turns "we don't know the schema" into a confident, specific, possibly incorrect answer. A profile
attached to the wrong table poisons every candidate, feature and analysis built on it.

So physical identity refuses what the catalog key substitutes.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.physical import (
    PhysicalBindingError,
    PhysicalDatasetBindingV1,
    PhysicalObjectIdentityV1,
    UnknownSchema,
)


def _identity(**over):
    kw = dict(catalog_source="ftr", database="banking", schema="dpl_eib_compliance",
              table="comp_financial_tran_repos_dly", column="cif_id", object_kind="column")
    kw.update(over)
    return PhysicalObjectIdentityV1(**kw)


# ── unknown schema is refused, never defaulted ───────────────────────────────────────────────────

@pytest.mark.parametrize("missing", [None, "", "   "])
def test_a_missing_schema_is_refused_not_defaulted_to_public(missing):
    """THE rule. `normalize_ref` substitutes `public` for a blank schema — correct for an attachment
    key, wrong for an address. Here it must fail loudly."""
    with pytest.raises(UnknownSchema):
        _identity(schema=missing)


def test_the_refusal_names_the_object_without_inventing_a_schema():
    """The error must be actionable and must not echo a guess back at the caller."""
    with pytest.raises(UnknownSchema) as exc:
        _identity(schema=None)
    message = str(exc.value)
    assert "comp_financial_tran_repos_dly" in message
    assert "public" not in message, "a refusal must never suggest the substitution it just refused"


def test_a_missing_database_is_also_refused():
    """Two same-named schemas can live in different Hive databases; the database is part of the
    address, not context."""
    with pytest.raises(UnknownSchema):
        _identity(database="")


# ── identity is stable, normalized and round-trippable ───────────────────────────────────────────

def test_identity_is_case_and_padding_insensitive():
    """Hive is case-insensitive for identifiers; two spellings of one table are one object, or the
    same physical table would accumulate two divergent profiles."""
    assert _identity().physical_id == _identity(
        database=" BANKING ", schema="DPL_EIB_Compliance",
        table="COMP_FINANCIAL_TRAN_REPOS_DLY", column="CIF_ID").physical_id


def test_a_table_identity_is_distinct_from_its_column():
    table = _identity(column=None, object_kind="table")
    assert table.physical_id != _identity().physical_id
    assert table.physical_id in _identity().physical_id, "a column's address extends its table's"


def test_same_table_name_in_two_databases_is_two_objects():
    """The case the flattened catalog key cannot express, and the reason this contract exists."""
    assert _identity(database="sales").physical_id != _identity(database="hr").physical_id


def test_same_table_name_in_two_schemas_is_two_objects():
    assert _identity(schema="sales").physical_id != _identity(schema="hr").physical_id


def test_a_column_identity_requires_a_column():
    with pytest.raises(PhysicalBindingError):
        _identity(column=None, object_kind="column")


def test_a_table_identity_must_not_carry_a_column():
    with pytest.raises(PhysicalBindingError):
        _identity(object_kind="table")


# ── the binding: catalog object -> physical dataset ──────────────────────────────────────────────

def _binding(**over):
    kw = dict(binding_id="b-1", catalog_logical_ref="ftr::dpl_eib_compliance.comp_financial_tran_repos_dly",
              connection_id="hive-pilot", identity=_identity(column=None, object_kind="table"),
              partition_columns=("tran_date",), business_time_column="tran_date")
    kw.update(over)
    return PhysicalDatasetBindingV1(**kw)


def test_a_binding_references_a_physical_identity_it_does_not_redefine_one():
    """Roadmap §3: the object says WHAT EXISTS; the binding says HOW A WORKER READS IT. Two layers,
    one identity — not two identity models."""
    b = _binding()
    assert b.identity.physical_id == _identity(column=None, object_kind="table").physical_id


def test_a_binding_must_be_to_a_table_not_a_column():
    with pytest.raises(PhysicalBindingError):
        _binding(identity=_identity())


def test_a_business_time_column_must_be_a_declared_partition():
    """The mapping from business time to partition is what makes pruning possible. Declaring a
    business-time column that is not a partition would silently produce a full scan — the exact cost
    failure §3c says belongs in the plan."""
    with pytest.raises(PhysicalBindingError):
        _binding(business_time_column="posted_ts")


def test_a_binding_with_no_partitions_is_allowed_but_declares_no_business_time():
    """Small unpartitioned dimension tables are legitimate; what is refused is CLAIMING a
    business-time mapping that cannot be pruned."""
    b = _binding(partition_columns=(), business_time_column=None)
    assert b.partition_columns == () and b.business_time_column is None
    with pytest.raises(PhysicalBindingError):
        _binding(partition_columns=(), business_time_column="tran_date")


def test_a_binding_carries_no_credentials():
    """A connection is referenced by id. A binding that could hold a secret is a binding that will
    end up in a log, a JSON column or an LLM prompt."""
    serialized = repr(_binding())
    for smell in ("password", "keytab", "token", "jdbc:", "://"):
        assert smell not in serialized.lower()
