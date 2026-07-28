"""Persisting observations as durable evidence — Release 1 step 6.

Without this the profiling numbers evaporate the moment the process ends, and nothing downstream —
ontology candidates, the bridge critic's value evidence, code sets — can be built on them.

The properties under test are about **what a stored profile is allowed to claim later**, which is
where a cheap profile turns into a wrong governed fact if the provenance is lost.
"""
from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from featuregen.data_agent.results import ColumnObservationV1, DataObservationResultV1
from featuregen.data_agent.store import (
    latest_observation,
    observation_history,
    record_observation,
)

_T0 = datetime(2026, 7, 28, 9, 0, tzinfo=UTC)
_PHYSICAL = "ftr::banking::dpl_eib::tran_repos"


def _result(**over) -> DataObservationResultV1:
    kw = dict(
        physical_id=_PHYSICAL, row_count=7,
        columns=(
            ColumnObservationV1(column="cif_id", non_null_count=5, distinct_count=3,
                                observed_rows=7),
            ColumnObservationV1(column="tran_amt", non_null_count=7, distinct_count=7,
                                minimum="10.50", maximum="70.00", observed_rows=7),
        ),
        partitions_read=("2026-06-01", "2026-06-02"), method="exact", complete=True)
    kw.update(over)
    return DataObservationResultV1(**kw)


def _record(db, result=None, *, at=_T0, principal="svc_featuregen_ro"):
    return record_observation(db, result or _result(), catalog_source="ftr",
                              connection_id="hive-pilot", execution_principal=principal,
                              dialect="hive", now=at)


# ── round trip ───────────────────────────────────────────────────────────────────────────────────

def test_a_stored_observation_reads_back_identically(db):
    _record(db)
    stored = latest_observation(db, _PHYSICAL)
    assert stored.row_count == 7 and stored.method == "exact" and stored.complete
    assert stored.partitions_read == ("2026-06-01", "2026-06-02")
    by_column = {c.column: c for c in stored.columns}
    assert by_column["cif_id"].non_null_count == 5
    assert by_column["cif_id"].null_count == 2
    assert by_column["tran_amt"].minimum == "10.50"


def test_bounds_absent_upstream_stay_absent(db):
    """`cif_id` was never opted into value bounds, so no identifier may appear here."""
    _record(db)
    cif = {c.column: c for c in latest_observation(db, _PHYSICAL).columns}["cif_id"]
    assert cif.minimum is None and cif.maximum is None


def test_an_unobserved_table_has_no_observation(db):
    assert latest_observation(db, "ftr::banking::dpl_eib::nothing") is None


# ── immutable versions, derived current pointer ──────────────────────────────────────────────────

def test_a_second_observation_does_not_mutate_the_first(db):
    """Profiles are DATED EVIDENCE. Overwriting would make it impossible to say what was true when a
    governed fact was accepted."""
    first = _record(db)
    second = _record(db, _result(row_count=99), at=_T0 + timedelta(days=1))
    assert first != second
    history = observation_history(db, _PHYSICAL)
    assert [o.row_count for o in history] == [99, 7], "newest first, both retained"


def test_latest_is_the_newest_not_the_last_written(db):
    """Ordering is by observation time, not insertion order — a backfilled older profile must not
    become current just because it was written second."""
    _record(db, _result(row_count=7), at=_T0)
    _record(db, _result(row_count=1), at=_T0 - timedelta(days=30))
    assert latest_observation(db, _PHYSICAL).row_count == 7


# ── a partial profile can never read as a whole one ──────────────────────────────────────────────

def test_a_partial_observation_stores_its_coverage_and_failures(db):
    partial = _result(complete=False, failures=("UndefinedColumn: no_such_column",), columns=())
    _record(db, partial)
    stored = latest_observation(db, _PHYSICAL)
    assert stored.complete is False
    assert stored.coverage == "partial"
    assert "no_such_column" in stored.failures[0]


def test_a_later_partial_run_does_not_erase_an_earlier_complete_one(db):
    """The rule that matters most here: a failed re-profile must not retract what a good profile
    already proved. Both are retained and the caller can see which is which."""
    _record(db, _result(), at=_T0)
    _record(db, _result(complete=False, columns=(), failures=("timeout",)),
            at=_T0 + timedelta(hours=1))
    history = observation_history(db, _PHYSICAL)
    assert [o.complete for o in history] == [False, True]
    assert any(o.complete and o.row_count == 7 for o in history)


# ── provenance decides what the evidence may support ─────────────────────────────────────────────

def test_the_method_is_stored_because_it_bounds_what_the_evidence_proves(db):
    """A sampled profile that finds a duplicate DISPROVES uniqueness; one that finds none proves
    nothing. Losing `method` turns an approximate profile into a claim it cannot support."""
    _record(db, _result(method="approximate"))
    assert latest_observation(db, _PHYSICAL).method == "approximate"


def test_the_execution_principal_is_stored(db):
    """Two profiles of one table under different principals are not interchangeable — the principal
    decides what the read could SEE."""
    _record(db, principal="svc_restricted_ro")
    assert latest_observation(db, _PHYSICAL).execution_principal == "svc_restricted_ro"


def test_partitions_read_are_stored_and_empty_never_means_everything(db):
    _record(db, _result(partitions_read=()))
    assert latest_observation(db, _PHYSICAL).partitions_read == ()


# ── integrity the database itself enforces ───────────────────────────────────────────────────────

def test_non_null_count_cannot_exceed_observed_rows(db):
    """A column cannot have more non-null values than rows scanned. Enforced by CHECK, so a broken
    executor cannot persist an impossible profile."""
    import psycopg
    bad = _result(columns=(ColumnObservationV1(column="x", non_null_count=99, distinct_count=1,
                                               observed_rows=7),))
    with pytest.raises(psycopg.errors.CheckViolation):
        _record(db, bad)


def test_an_unknown_method_is_refused_by_the_database(db):
    import psycopg
    with pytest.raises(psycopg.errors.CheckViolation):
        _record(db, _result(method="guessed"))
