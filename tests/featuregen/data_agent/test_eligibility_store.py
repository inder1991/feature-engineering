"""Where "a transaction that counts" lives.

The analysis IR refuses without an eligibility policy, and no deployment could supply one — every
test invented its own. So this was the last thing making every plan unrunnable by construction, even
once a table was bound.

It is a JUDGEMENT, not configuration: is a PENDING transaction activity? was a reversal that arrived
after the cutoff known at the cutoff? Two reasonable people can disagree and the answer changes the
number, so the record keeps who decided and when — and an unconfirmed policy is usable but disclosed.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.data_agent.eligibility import (
    EligibilityError,
    NullBehavior,
    ReversalMode,
    TransactionEligibilityPolicyV1,
)
from featuregen.data_agent.eligibility_store import (
    confirm_eligibility,
    record_eligibility,
    resolve_eligibility,
)

_NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _policy(**over) -> TransactionEligibilityPolicyV1:
    kw = dict(status_column="tran_status", included_status_values=("POSTED",),
              reversal_mode=ReversalMode.BOOLEAN_OR_CODE_COLUMN, reversal_column="reversal_flag",
              non_reversed_values=("N",), null_behavior=NullBehavior.EXCLUDE)
    kw.update(over)
    return TransactionEligibilityPolicyV1(**kw)


def _record(db, **over):
    kw = dict(catalog_source="ftr", table="tran_repos", policy=_policy(), proposed_by="llm")
    kw.update(over)
    record_eligibility(db, **kw)


# ── the round trip ───────────────────────────────────────────────────────────────────────────────

def test_a_policy_round_trips_through_the_contract(db):
    _record(db)
    stored = resolve_eligibility(db, catalog_source="ftr", table="tran_repos")
    assert stored is not None
    assert stored.policy.status_column == "tran_status"
    assert stored.policy.included_status_values == ("POSTED",)
    assert stored.policy.reversal_mode is ReversalMode.BOOLEAN_OR_CODE_COLUMN
    assert stored.policy.null_behavior is NullBehavior.EXCLUDE


def test_the_stored_policy_produces_the_same_PREDICATES_as_the_one_that_was_written(db):
    """The policy is only worth storing if it compiles to the same filter. Comparing the rendered
    SQL rather than the fields catches a round trip that loses, say, the null behaviour."""
    written = _policy()
    _record(db, policy=written)
    stored = resolve_eligibility(db, catalog_source="ftr", table="tran_repos")
    assert stored.policy.predicates(lambda n: f'"{n}"') == written.predicates(lambda n: f'"{n}"')


def test_an_undefined_table_is_an_ABSENCE_not_an_error(db):
    """Most tables have no policy. Raising would turn "nobody has decided yet" into a fault."""
    assert resolve_eligibility(db, catalog_source="ftr", table="never_defined") is None


def test_lookup_is_case_insensitive_on_the_table(db):
    _record(db)
    assert resolve_eligibility(db, catalog_source="ftr", table="TRAN_REPOS") is not None


# ── provenance: who decided, and whether anyone agreed ───────────────────────────────────────────

def test_a_proposed_policy_is_usable_but_NOT_confirmed(db):
    """Usable before confirmation is the product rule — refusing every question until someone signs
    a form is how a governance step becomes theatre. What it must not do is pass silently."""
    _record(db, proposed_by="llm")
    stored = resolve_eligibility(db, catalog_source="ftr", table="tran_repos")
    assert stored.confirmed is False
    assert stored.proposed_by == "llm"


def test_confirming_records_who_and_when(db):
    _record(db)
    assert confirm_eligibility(db, catalog_source="ftr", table="tran_repos",
                               actor="priya", now=_NOW) is True
    stored = resolve_eligibility(db, catalog_source="ftr", table="tran_repos")
    assert stored.confirmed and stored.confirmed_by == "priya"
    assert stored.confirmed_at == _NOW
    assert stored.proposed_by == "llm"          # both are kept: proposed-then-confirmed is not
                                                # the same thing as human-authored


def test_confirming_a_policy_that_does_not_exist_reports_it_rather_than_inventing_one(db):
    assert confirm_eligibility(db, catalog_source="ftr", table="nope",
                               actor="priya", now=_NOW) is False
    assert resolve_eligibility(db, catalog_source="ftr", table="nope") is None


def test_RE_PROPOSING_clears_a_previous_confirmation(db):
    """THE property. A human agreed to a SPECIFIC definition of "counts". Changing the status codes
    underneath that agreement while keeping the signature would make the audit trail say a person
    approved something they never saw."""
    _record(db)
    confirm_eligibility(db, catalog_source="ftr", table="tran_repos", actor="priya", now=_NOW)

    _record(db, policy=_policy(included_status_values=("POSTED", "PENDING")), proposed_by="llm")
    stored = resolve_eligibility(db, catalog_source="ftr", table="tran_repos")
    assert stored.policy.included_status_values == ("POSTED", "PENDING")
    assert stored.confirmed is False, "a changed definition kept its old approval"


def test_one_policy_per_table(db):
    """Two definitions of "counts" for one table would let read order decide the answer."""
    _record(db)
    _record(db, policy=_policy(status_column="other_status"))
    count = db.execute("SELECT count(*) FROM eligibility_policy "
                       "WHERE catalog_source = 'ftr'").fetchone()[0]
    assert count == 1


# ── a stored row is still held to the contract ───────────────────────────────────────────────────

def test_a_row_whose_reversal_mode_is_UNSUPPORTED_fails_loudly_on_read(db):
    """Written straight to the table, bypassing the dataclass — which is exactly how a bad row gets
    there in practice, via a migration or a fix-up script. Resolution must refuse rather than build a
    query that counts a transaction and its reversal."""
    _record(db)
    db.execute("UPDATE eligibility_policy SET reversal_mode = 'compensating_row'")
    with pytest.raises(EligibilityError, match="not implemented"):
        resolve_eligibility(db, catalog_source="ftr", table="tran_repos")


def test_a_row_with_no_eligible_status_fails_loudly_on_read(db):
    """An empty status list excludes every transaction — a query that returns zero for everyone and
    looks like a real answer."""
    _record(db)
    db.execute("UPDATE eligibility_policy SET included_status_values = '{}'")
    with pytest.raises(EligibilityError, match="no status counts"):
        resolve_eligibility(db, catalog_source="ftr", table="tran_repos")


# ── it drives a real query ───────────────────────────────────────────────────────────────────────

def test_the_stored_policy_reproduces_the_fixtures_hand_counted_answer(db):
    """The point of storing it: a policy read back from the table filters the pilot rows exactly as
    the hand-written one does, including the traps — C4's current month is only reversals, and C6's
    only previous transaction is reversed."""
    from tests.featuregen.analysis.test_plan_to_execution import _grounded, _inputs
    from tests.featuregen.data_agent.pilot_fixture import EXPECTED, create_pilot_tables

    from featuregen.analysis.execution import plan_to_execution_ir
    from featuregen.data_agent.analysis import run_analysis
    from featuregen.data_agent.sql_postgres import PostgresDialect

    create_pilot_tables(db)
    _record(db)
    stored = resolve_eligibility(db, catalog_source="ftr", table="tran_repos")

    ir = plan_to_execution_ir(_grounded(), _inputs(eligibility=stored.policy))
    rows = run_analysis(db, ir, dialect=PostgresDialect())
    by_key = {r.key: r for r in rows}
    assert (by_key["C4"].previous_count, by_key["C4"].current_count) == (2, 0)
    assert (by_key["C6"].previous_count, by_key["C6"].current_count) == (0, 0)
    assert tuple(sorted(r.key for r in rows if r.decreased)) == EXPECTED["decreased_customers"]
