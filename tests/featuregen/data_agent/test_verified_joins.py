"""Verified joins — the Release 3 demonstrable that had no code path.

Release 3 must demonstrate "the population spine, zero-transaction customers, PIT dimensions,
**verified joins**, reversal/status filtering and hand-reconciled results". Five of the six were
built and tested. The join was not: `AnalysisExecutionIRV1` took a spine key and an event key as
bare strings and joined them, with nothing asserting that the two columns had ever been OBSERVED to
denote the same entity.

That gap is not bureaucratic. The spine is the LEFT side of the query, so a non-unique spine key
multiplies the entire population — silently, and invisibly to the hand-reconciled fixture, whose
customer table happens to be unique. `test_a_duplicated_spine_key_would_inflate_every_total` builds
the broken case and shows both halves: the inflation that happens without the check, and the refusal
with it.

The evidence already exists. `relationship.observe_relationship` (Release 1) measures uniqueness per
side, referential coverage and fan-out, and deliberately promotes nothing — "Release 1 produces
evidence; Release 2 decides what it may support". This is that decision, for one specific use: may
these two columns be joined in an analysis?
"""
from __future__ import annotations

from dataclasses import replace

import pytest

from featuregen.data_agent.analysis import AnalysisIRError, compile_analysis, run_analysis
from featuregen.data_agent.profile_policy import ProfilePolicyV1
from featuregen.data_agent.relationship import (
    JOIN_EVIDENCE_MISMATCHED,
    JOIN_EVIDENCE_MISSING,
    JOIN_KEY_NOT_UNIQUE,
    JOIN_UNIQUENESS_UNKNOWN,
    RelationshipProbeV1,
    observe_relationship,
)
from featuregen.data_agent.sql_postgres import PostgresDialect
from tests.featuregen.data_agent.pilot_fixture import (
    CUSTOMER_SCHEMA,
    CUSTOMER_TABLE,
    EXPECTED,
    PILOT_JOIN_EVIDENCE,
    TRANSACTION_SCHEMA,
    TRANSACTION_TABLE,
    create_pilot_tables,
)
from tests.featuregen.data_agent.test_analysis_ir import _binding, _ir


@pytest.fixture
def pilot(db):
    create_pilot_tables(db)
    return db


def _probe(conn, *, exact: bool = True):
    return observe_relationship(
        conn,
        RelationshipProbeV1(
            left_binding=_binding(TRANSACTION_SCHEMA, TRANSACTION_TABLE), left_column="cif_id",
            right_binding=_binding(CUSTOMER_SCHEMA, CUSTOMER_TABLE), right_column="cif_id",
            policy=ProfilePolicyV1(exact_distinct=exact)),
        dialect=PostgresDialect())


# ── the evidence must exist, and must be about THIS join ─────────────────────────────────────────

def test_an_analysis_with_no_join_evidence_is_REFUSED(pilot):
    """The default is refusal, not permission. An analysis that has never looked at the relationship
    it joins on is exactly what Release 3 says must not be demonstrable."""
    with pytest.raises(AnalysisIRError) as exc:
        run_analysis(pilot, _ir(join_evidence=None), dialect=PostgresDialect())
    assert exc.value.code == JOIN_EVIDENCE_MISSING


def test_evidence_for_a_DIFFERENT_COLUMN_pair_is_refused(pilot):
    """A false attestation is worse than none: it reads as verification in the audit trail while
    describing a relationship the query does not perform."""
    wrong = replace(PILOT_JOIN_EVIDENCE, left_column="tran_type")
    with pytest.raises(AnalysisIRError) as exc:
        run_analysis(pilot, _ir(join_evidence=wrong), dialect=PostgresDialect())
    assert exc.value.code == JOIN_EVIDENCE_MISMATCHED


def test_evidence_for_a_DIFFERENT_TABLE_pair_is_refused(pilot):
    """Same column names on the wrong tables is the easiest mismatch to miss by eye, because the
    only thing that differs is the physical id."""
    wrong = replace(PILOT_JOIN_EVIDENCE, right_physical_id="some.other.table")
    with pytest.raises(AnalysisIRError) as exc:
        run_analysis(pilot, _ir(join_evidence=wrong), dialect=PostgresDialect())
    assert exc.value.code == JOIN_EVIDENCE_MISMATCHED


# ── THE correctness property the check exists for ────────────────────────────────────────────────

def test_a_duplicated_spine_key_would_inflate_every_total(pilot):
    """The whole reason this check exists, shown in both directions.

    A second row for C1 in the customer table is not a data-quality curiosity: because the spine is
    the LEFT side, C1's counts appear twice and every group total that includes C1 is overstated.
    The pilot fixture cannot catch it — its customer table is unique — so nothing in the suite
    before this test would have noticed.
    """
    pilot.execute(f"INSERT INTO {CUSTOMER_SCHEMA}.{CUSTOMER_TABLE} VALUES ('C1')")
    evidence = _probe(pilot)
    assert not evidence.right_is_unique

    # First: the harm. Compilation does not verify — it renders a preview — so this is the query
    # that WOULD have run, and it returns one row more than there are customers.
    cursor = pilot.cursor()
    cursor.execute(compile_analysis(_ir(), dialect=PostgresDialect()))
    inflated = cursor.fetchall()
    assert len(inflated) == EXPECTED["customer_rows"] + 1
    assert [r[0] for r in inflated].count("C1") == 2

    # Then: the refusal.
    with pytest.raises(AnalysisIRError) as exc:
        run_analysis(pilot, _ir(join_evidence=evidence), dialect=PostgresDialect())
    assert exc.value.code == JOIN_KEY_NOT_UNIQUE


def test_an_APPROXIMATE_probe_cannot_verify_the_join(pilot):
    """`uniqueness_verdict` is deliberately asymmetric: only an exact probe may assert uniqueness,
    because an approximate distinct count that happens to equal the row count proves nothing. The
    join check must inherit that asymmetry rather than reading `right_is_unique` directly — reading
    the raw property is precisely how a cheap profile silently promotes a bad key."""
    evidence = _probe(pilot, exact=False)
    assert evidence.right_is_unique                      # the raw property says yes...
    with pytest.raises(AnalysisIRError) as exc:          # ...and the verdict still refuses
        run_analysis(pilot, _ir(join_evidence=evidence), dialect=PostgresDialect())
    assert exc.value.code == JOIN_UNIQUENESS_UNKNOWN


# ── the refusal names the relationship, not just the request ─────────────────────────────────────

def test_the_refusal_carries_the_columns_it_is_about(pilot):
    """`RELATIONSHIP_UNVERIFIED` is only actionable if it says WHICH relationship. The exception
    carries the subjects so the learning event can be specific rather than naming the request."""
    with pytest.raises(AnalysisIRError) as exc:
        run_analysis(pilot, _ir(join_evidence=None), dialect=PostgresDialect())
    subjects = exc.value.subjects
    assert any("customer_master.cif_id" in s for s in subjects), subjects
    assert any("tran_repos.cif_id" in s for s in subjects), subjects


# ── the hand-written evidence is not allowed to lie ──────────────────────────────────────────────

def test_the_fixtures_hand_written_evidence_matches_a_REAL_probe(pilot):
    """`PILOT_JOIN_EVIDENCE` is written out by hand, like `EXPECTED`, so every other test in this
    file can construct the verified case without a probe. This is the test that keeps it honest: a
    hand-written attestation that drifts from what the data says would make the whole suite pass
    against a relationship that does not hold."""
    assert _probe(pilot) == PILOT_JOIN_EVIDENCE


def test_the_probe_reproduces_the_fixtures_hand_counted_numbers(pilot):
    """Cross-check against the numbers counted by hand in the fixture docstring, so the evidence is
    anchored to something a human verified rather than to the probe's own output."""
    evidence = _probe(pilot)
    assert evidence.right_distinct == EXPECTED["customer_cif_distinct"]
    assert evidence.left_distinct == EXPECTED["transaction_cif_distinct"]
    assert evidence.left_nulls == EXPECTED["transaction_cif_nulls"]
    assert evidence.unmatched_distinct == EXPECTED["unmatched_ids"]     # C9
    assert evidence.matched_distinct == EXPECTED["matched_ids"]
    assert evidence.max_left_rows_per_right_key == EXPECTED["max_rows_per_customer"]
    assert evidence.observed_cardinality == "many_to_one"


# ── verification changes nothing about the answer ────────────────────────────────────────────────

def test_the_verified_pilot_join_returns_the_SAME_answers(pilot):
    """Adding the gate must not move a single number. The hand-reconciled expectations are the
    contract; verification is an admissibility check in front of them, not a change to them."""
    rows = run_analysis(pilot, _ir(), dialect=PostgresDialect())
    decreased = tuple(sorted(r.key for r in rows if r.decreased))
    assert decreased == EXPECTED["decreased_customers"]
    assert len(rows) == EXPECTED["customer_rows"]


def test_evidence_is_NOT_part_of_the_plan_hash(pilot):
    """The plan hash is the identity of WHAT is computed. Evidence describes the state of the data,
    not the computation — re-probing the same relationship must not invalidate a cached result, and
    two plans differing only in how recently they were probed are the same plan. This mirrors
    `question` being excluded as provenance."""
    assert _ir().plan_hash == _ir(join_evidence=None).plan_hash
    assert _ir().plan_hash == _ir(join_evidence=_probe(pilot, exact=False)).plan_hash
