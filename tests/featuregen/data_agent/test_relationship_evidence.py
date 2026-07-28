"""Relationship evidence — Release 1 step 7.

The evidence three things have been waiting on:

* the **bridge critic**, which currently judges whether two customer ids denote the same namespace
  on MEANING alone, because value evidence did not exist;
* **automatic attestation**, the thing that stops "a human approves 100,000 columns";
* **observed cardinality**, which replaces the `N:1` the ingest path fabricates.

Every expectation is counted by hand in `pilot_fixture.EXPECTED`, so an implementation cannot
quietly redefine what correct means.

**Promotes nothing.** Release 1 produces evidence; Release 2 decides what it may support. The rule
that governs that decision is asserted here: a sample that finds a duplicate DISPROVES uniqueness,
while a sample that finds none proves nothing.
"""
from __future__ import annotations

import pytest

from featuregen.data_agent.physical import PhysicalDatasetBindingV1, PhysicalObjectIdentityV1
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.relationship import RelationshipProbeV1, observe_relationship
from featuregen.data_agent.sql_postgres import PostgresDialect
from tests.featuregen.data_agent.pilot_fixture import (
    CUSTOMER_SCHEMA,
    CUSTOMER_TABLE,
    EXPECTED,
    TRANSACTION_SCHEMA,
    TRANSACTION_TABLE,
    create_pilot_tables,
)


@pytest.fixture
def pilot(db):
    create_pilot_tables(db)
    return db


def _identity(schema: str, table: str) -> PhysicalObjectIdentityV1:
    return PhysicalObjectIdentityV1(catalog_source="ftr", database="featuregen_test",
                                    schema=schema, table=table, object_kind="table")


def _binding(schema: str, table: str) -> PhysicalDatasetBindingV1:
    return PhysicalDatasetBindingV1(
        binding_id=f"b-{table}", catalog_logical_ref=f"ftr::{schema}.{table}",
        connection_id="local-pg", identity=_identity(schema, table))


def _probe(**over) -> RelationshipProbeV1:
    kw = dict(left_binding=_binding(TRANSACTION_SCHEMA, TRANSACTION_TABLE), left_column="cif_id",
              right_binding=_binding(CUSTOMER_SCHEMA, CUSTOMER_TABLE), right_column="cif_id",
              policy=ProfilePolicyV1())
    kw.update(over)
    return RelationshipProbeV1(**kw)


def _run(conn, probe=None):
    return observe_relationship(conn, probe or _probe(), dialect=PostgresDialect())


# ── the numbers, counted by hand ─────────────────────────────────────────────────────────────────

def test_uniqueness_differs_between_the_two_sides(pilot):
    """The customer side is a key; the transaction side is not. A join between them therefore has a
    direction and a multiplier — which is exactly what a fabricated `N:1` guesses at."""
    e = _run(pilot)
    assert e.right_distinct == EXPECTED["customer_cif_distinct"] == 5
    assert e.right_rows == EXPECTED["customer_rows"] == 5
    assert e.right_is_unique is True
    assert e.left_distinct == EXPECTED["transaction_cif_distinct"] == 6
    assert e.left_is_unique is False


def test_null_rate_on_the_referencing_side(pilot):
    e = _run(pilot)
    assert e.left_nulls == EXPECTED["transaction_cif_nulls"] == 2


def test_referential_coverage_finds_the_unmatched_key(pilot):
    """C9 has transactions and no customer row. Coverage below 1.0 is the single most useful signal
    for whether two identifier columns really share a namespace."""
    e = _run(pilot)
    assert e.unmatched_distinct == EXPECTED["unmatched_ids"] == 1
    assert e.matched_distinct == EXPECTED["matched_ids"] == 5
    assert e.coverage_ratio == pytest.approx(5 / 6)


def test_the_observed_join_multiplier(pilot):
    """How many left rows a single right row attracts. This is the fan-out fact, measured rather
    than declared."""
    e = _run(pilot)
    assert e.max_left_rows_per_right_key == EXPECTED["max_rows_per_customer"] == 5   # C3: 1 + 4
    assert e.observed_cardinality == "many_to_one"


# ── what the evidence may and may not support ────────────────────────────────────────────────────

def test_an_exact_probe_may_assert_uniqueness(pilot):
    e = _run(pilot, _probe(policy=ProfilePolicyV1(exact_distinct=True)))
    assert e.method == "exact"
    assert e.uniqueness_verdict("right") == "unique"


def test_a_sample_that_finds_a_duplicate_DISPROVES_uniqueness(pilot):
    """Asymmetry is the whole point: a duplicate is proof of non-uniqueness however it was found."""
    e = _run(pilot)
    assert e.uniqueness_verdict("left") == "not_unique"


def test_a_sample_that_finds_no_duplicate_proves_NOTHING(pilot):
    """The rule automatic attestation rests on. An approximate probe seeing no duplicate must return
    `unknown`, never `unique` — otherwise a cheap profile silently promotes a bad key."""
    e = _run(pilot, _probe(policy=ProfilePolicyV1(exact_distinct=False)))
    assert e.method == "approximate"
    assert e.uniqueness_verdict("right") == "unknown"


def test_nothing_is_promoted(pilot):
    """Release 1 produces evidence. It carries no decision and no attestation."""
    e = _run(pilot)
    assert not hasattr(e, "attested") and not hasattr(e, "decision")


# ── egress ───────────────────────────────────────────────────────────────────────────────────────

def test_the_evidence_carries_no_identifier_values(pilot):
    """Counts and ratios only. An unmatched-key COUNT is a statistic; the unmatched keys themselves
    are customer identifiers."""
    text = repr(_run(pilot))
    for identifier in ("C1", "C2", "C3", "C4", "C5", "C9"):
        assert identifier not in text
