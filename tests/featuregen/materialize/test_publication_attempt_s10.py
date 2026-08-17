"""S10 — exact-output CAS publication (1081; environment scoping in 1085).

*"An older verified output over a newer active revision refuses; a partial group never becomes
visible; environment keying is in place; an interrupted swap lands
``UNKNOWN_RECONCILIATION_REQUIRED`` and blocks retry until reconciled."*

The fourth clause is the one that only exists because publication crosses two planes. A Hive swap
and a PostgreSQL transaction are not one transaction, and the window where the swap succeeds and the
transaction rolls back is real — so the tests treat the uncertain outcome as a state of KNOWLEDGE
rather than an error, and check that it cannot be cleared by trying again.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.materialize.publication_attempt_store import (
    PublicationAttemptV1,
    PublicationBlocked,
    PublicationOutcomeV1,
    blocking_attempt,
    reconcile_attempt,
    record_publication_attempt,
    settle_attempt,
)
from featuregen.overlay.upload.publication_revisions import ActiveRevisionConflict

ENV = "hdfc-local"
GROUP = "customer_txn_features"
CAPABILITY = "cap:publisher@hdfc-local"


def _attempt(
    *, attempt_id: str = "pa-1", environment_id: str = ENV, group: str = GROUP,
    verified: str = "vo-1", expected: str | None = "rev-active-1",
    outcome: PublicationOutcomeV1 = PublicationOutcomeV1.STARTED,
    capability: str = CAPABILITY,
) -> PublicationAttemptV1:
    return PublicationAttemptV1(
        attempt_id=attempt_id, environment_id=environment_id, logical_group_name=group,
        verified_output_revision_id=verified, sealed_artifact_id="art-1",
        expected_active_revision_id=expected, publish_mechanism="versioned_pointer",
        capability_attestation=capability, outcome=outcome)


def _record(db, attempt=None, *, observed: str | None = "rev-active-1", started_at: str = "t0"):
    return record_publication_attempt(
        db, attempt or _attempt(), observed_active_revision_id=observed, started_at=started_at)


# ══ ACCEPTANCE 1 — an OLDER verified output over a NEWER active revision refuses ════════════════
def test_AN_OLDER_OUTPUT_OVER_A_NEWER_ACTIVE_REVISION_REFUSES(db):
    """The caller read a verified output against one active revision; a later one may make that
    output the wrong thing to publish."""
    with pytest.raises(ActiveRevisionConflict):
        _record(db, _attempt(expected="rev-active-1"), observed="rev-active-2")


def test_a_REFUSED_PRECONDITION_LEAVES_NO_ROW(db):
    """It never began. A row would leave an attempt in the history that nothing attempted."""
    with pytest.raises(ActiveRevisionConflict):
        _record(db, _attempt(expected="rev-active-1"), observed="rev-active-2")
    assert db.execute("SELECT count(*) FROM publication_attempt").fetchone()[0] == 0


def test_a_MATCHING_expectation_records(db):
    assert _record(db) == "pa-1"
    row = db.execute(
        "SELECT outcome, capability_attestation FROM publication_attempt "
        "WHERE attempt_id = %s", ("pa-1",)).fetchone()
    assert row == ("started", CAPABILITY)


def test_the_FIRST_publication_of_a_group_expects_NOTHING(db):
    """`None` means "there is no active revision", never "I did not check"."""
    assert _record(db, _attempt(expected=None), observed=None) == "pa-1"


def test_expecting_NOTHING_when_something_IS_active_refuses(db):
    with pytest.raises(ActiveRevisionConflict):
        _record(db, _attempt(expected=None), observed="rev-active-1")


# ══ ACCEPTANCE 2 — PUBLICATION REQUIRES A CAPABILITY (verification must not) ═══════════════════
def test_AN_ATTEMPT_WITHOUT_A_CAPABILITY_ATTESTATION_IS_REFUSED(db):
    """§0.3's asymmetry: it is the single thing that distinguishes publication from verification,
    and an attempt without it is a publish nobody was entitled to make."""
    with pytest.raises(ValueError, match="nobody was entitled"):
        _attempt(capability="   ")


def test_the_CAPABILITY_lives_on_the_publication_table_not_the_verification_one(db):
    """S9 deliberately has no column for one; S10 requires it. Asserted on both schemas, because
    the asymmetry is the requirement."""
    publication = {row[0] for row in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        ("publication_attempt",)).fetchall()}
    verification = {row[0] for row in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        ("verified_output_revision",)).fetchall()}

    assert "capability_attestation" in publication
    assert not any("capab" in column for column in verification), sorted(verification)


# ══ A PARTIAL GROUP NEVER BECOMES VISIBLE ══════════════════════════════════════════════════════
def test_AN_ATTEMPT_PUBLISHES_A_GROUP_not_features():
    """Tested by ABSENCE, which is the only way: there is no per-feature field, so there is no way
    to express publishing part of a group. A column for one is what a partial publish would need."""
    fields = set(PublicationAttemptV1.__dataclass_fields__)
    assert "logical_group_name" in fields
    assert not any("feature" in name or "column" in name for name in fields), sorted(fields)


def test_THE_SCHEMA_HAS_NO_PER_FEATURE_ROW_EITHER(db):
    """The same claim one level down: `publication_attempt` is keyed by group, and there is no
    child table that could hold a subset of its columns."""
    columns = {row[0] for row in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = %s",
        ("publication_attempt",)).fetchall()}
    assert not any("feature" in column for column in columns), sorted(columns)

    children = db.execute(
        "SELECT count(*) FROM information_schema.tables WHERE table_name LIKE %s",
        ("publication_attempt_%",)).fetchone()[0]
    assert children == 0


def test_the_CONTRACT_declares_ATOMIC_GROUP_publication():
    """The rule the mechanism has to honour, stated where the group's contract is derived — a
    publication policy of anything else would make a partial group expressible upstream of here."""
    from featuregen.materialize.contract import PublicationPolicy

    assert PublicationPolicy.ATOMIC_GROUP.value == "atomic_group"


# ══ ACCEPTANCE 3 — ENVIRONMENT KEYING is in place ══════════════════════════════════════════════
def test_TWO_ENVIRONMENTS_PUBLISH_THE_SAME_GROUP_INDEPENDENTLY(db):
    """Environment is deployment placement, so a blocking attempt in one must not block the other."""
    _record(db, _attempt(attempt_id="pa-local", environment_id="hdfc-local"))
    _record(db, _attempt(attempt_id="pa-uat", environment_id="hdfc-uat"))

    assert blocking_attempt(db, environment_id="hdfc-local", logical_group_name=GROUP) == (
        "pa-local", PublicationOutcomeV1.STARTED)
    assert blocking_attempt(db, environment_id="hdfc-uat", logical_group_name=GROUP) == (
        "pa-uat", PublicationOutcomeV1.STARTED)


def test_an_attempt_must_NAME_its_environment():
    with pytest.raises(ValueError, match="what it published"):
        _attempt(environment_id="  ")


def test_the_blocking_index_is_keyed_on_the_PAIR(db):
    """Not on the group alone — an attempt that did not say which environment could block a retry
    in a cluster it never touched."""
    _record(db, _attempt(attempt_id="pa-local", environment_id="hdfc-local"))
    with pytest.raises(PublicationBlocked):
        _record(db, _attempt(attempt_id="pa-local-2", environment_id="hdfc-local"))
    # ... and the other environment is untouched.
    assert _record(db, _attempt(attempt_id="pa-uat", environment_id="hdfc-uat")) == "pa-uat"


# ══ ACCEPTANCE 4 — an INTERRUPTED SWAP lands UNKNOWN and BLOCKS RETRY ══════════════════════════
def test_AN_INTERRUPTED_SWAP_LANDS_UNKNOWN_RECONCILIATION_REQUIRED(db):
    """A state of KNOWLEDGE, not an error: the swap may or may not have landed, and no care inside
    either plane closes that window."""
    _record(db)
    settle_attempt(db, "pa-1", PublicationOutcomeV1.UNKNOWN_RECONCILIATION_REQUIRED,
                   detail="the session died between the swap and the commit")
    assert db.execute(
        "SELECT outcome FROM publication_attempt WHERE attempt_id = %s",
        ("pa-1",)).fetchone()[0] == "unknown_reconciliation_required"


def test_AN_UNCERTAIN_ATTEMPT_BLOCKS_RETRY(db):
    """Retrying one that may already have swapped is how a group gets published twice, or published
    from an artifact the operator believed had failed."""
    _record(db)
    settle_attempt(db, "pa-1", PublicationOutcomeV1.UNKNOWN_RECONCILIATION_REQUIRED)

    with pytest.raises(PublicationBlocked, match="Reconcile it"):
        _record(db, _attempt(attempt_id="pa-2"))


def test_a_STARTED_attempt_BLOCKS_TOO(db):
    """The same uncertainty reached by crashing rather than by catching. Treating it as retryable
    would be assuming that a crash means nothing happened."""
    _record(db)
    assert PublicationOutcomeV1.STARTED.blocks_retry is True
    with pytest.raises(PublicationBlocked):
        _record(db, _attempt(attempt_id="pa-2"))


def test_RECONCILING_UNBLOCKS_and_nothing_else_does(db):
    """The only route out. `reconcile_attempt` takes what the published generation marker SHOWS,
    because the point of reconciliation is to look rather than to choose."""
    _record(db)
    settle_attempt(db, "pa-1", PublicationOutcomeV1.UNKNOWN_RECONCILIATION_REQUIRED)
    reconcile_attempt(db, "pa-1", observed_outcome=PublicationOutcomeV1.FAILED,
                      reconciled_at="2026-08-17T01:00:00Z")

    assert blocking_attempt(db, environment_id=ENV, logical_group_name=GROUP) is None
    assert _record(db, _attempt(attempt_id="pa-2")) == "pa-2"


def test_RECONCILING_TO_STILL_UNKNOWN_IS_REFUSED(db):
    """"Still unknown" is not a reconciliation; it is the state the attempt is already in, and
    recording it would unblock a group nobody checked."""
    _record(db)
    settle_attempt(db, "pa-1", PublicationOutcomeV1.UNKNOWN_RECONCILIATION_REQUIRED)
    with pytest.raises(ValueError, match="nobody checked"):
        reconcile_attempt(db, "pa-1",
                          observed_outcome=PublicationOutcomeV1.UNKNOWN_RECONCILIATION_REQUIRED,
                          reconciled_at="t")


def test_a_SETTLED_attempt_cannot_be_reconciled(db):
    """Reconciling a succeeded one would restate a fact the attempt already established."""
    _record(db)
    settle_attempt(db, "pa-1", PublicationOutcomeV1.SUCCEEDED)
    with pytest.raises(ValueError, match="not an unreconciled uncertain attempt"):
        reconcile_attempt(db, "pa-1", observed_outcome=PublicationOutcomeV1.SUCCEEDED,
                          reconciled_at="t")


def test_RECONCILING_TWICE_is_refused(db):
    """The second look would override the first."""
    _record(db)
    settle_attempt(db, "pa-1", PublicationOutcomeV1.UNKNOWN_RECONCILIATION_REQUIRED)
    reconcile_attempt(db, "pa-1", observed_outcome=PublicationOutcomeV1.SUCCEEDED,
                      reconciled_at="t1")
    with pytest.raises(ValueError, match="not an unreconciled uncertain attempt"):
        reconcile_attempt(db, "pa-1", observed_outcome=PublicationOutcomeV1.FAILED,
                          reconciled_at="t2")


def test_a_SUCCEEDED_or_FAILED_attempt_does_not_block(db):
    """Certainty in either direction is a settled attempt: a retry after a known failure is exactly
    what an operator should be able to do."""
    for outcome in (PublicationOutcomeV1.SUCCEEDED, PublicationOutcomeV1.FAILED):
        assert outcome.blocks_retry is False

    _record(db)
    settle_attempt(db, "pa-1", PublicationOutcomeV1.FAILED, detail="the swap raised")
    assert blocking_attempt(db, environment_id=ENV, logical_group_name=GROUP) is None
    assert _record(db, _attempt(attempt_id="pa-2")) == "pa-2"


def test_the_DATABASE_permits_ONE_blocking_attempt_per_group(db):
    """Against a caller that bypasses the writer: the block is an index, not a check somebody
    remembered to run."""
    _record(db)
    with pytest.raises(psycopg.errors.UniqueViolation):
        db.execute(
            "INSERT INTO publication_attempt (attempt_id, environment_id, logical_group_name, "
            "verified_output_revision_id, sealed_artifact_id, expected_active_revision_id, "
            "publish_mechanism, capability_attestation, outcome, detail, started_at) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, 'started', '', 't')",
            ("pa-bypass", ENV, GROUP, "vo-2", "art-1", None, "versioned_pointer", CAPABILITY))


# ══ what may move, and what may not ════════════════════════════════════════════════════════════
def test_a_SETTLED_OUTCOME_cannot_be_moved_again(db):
    """A settled outcome is a fact about what happened to two systems."""
    _record(db)
    settle_attempt(db, "pa-1", PublicationOutcomeV1.SUCCEEDED)
    with pytest.raises(ValueError, match="is not STARTED"):
        settle_attempt(db, "pa-1", PublicationOutcomeV1.FAILED)


def test_settle_cannot_set_STARTED(db):
    """An attempt starts in that state; 'settling' it there would record a transition that did not
    happen."""
    _record(db)
    with pytest.raises(ValueError, match="did not happen"):
        settle_attempt(db, "pa-1", PublicationOutcomeV1.STARTED)


@pytest.mark.parametrize("column,value", [
    ("verified_output_revision_id", "vo-something-else"),
    ("sealed_artifact_id", "art-something-else"),
    ("capability_attestation", "cap:someone-else"),
    ("environment_id", "hdfc-prod"),
    ("publish_mechanism", "exchange_partition"),
])
def test_WHAT_WAS_PUBLISHED_can_never_be_edited(db, column, value):
    """What it published, where, by which mechanism and under whose capability are frozen on
    arrival. Parametrized rather than looped, because each refusal aborts the transaction — a loop
    would prove the first case and then assert against a dead connection."""
    _record(db)
    with pytest.raises(psycopg.errors.RaiseException, match="except outcome and reconciliation"):
        db.execute(f"UPDATE publication_attempt SET {column} = %s WHERE attempt_id = %s",
                   (value, "pa-1"))


def test_an_attempt_cannot_be_DELETED(db):
    _record(db)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("DELETE FROM publication_attempt WHERE attempt_id = %s", ("pa-1",))


def test_a_RECONCILIATION_MUST_BE_WHOLE(db):
    """A reconciliation with no outcome is a check somebody started and did not finish."""
    _record(db)
    settle_attempt(db, "pa-1", PublicationOutcomeV1.UNKNOWN_RECONCILIATION_REQUIRED)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE publication_attempt SET reconciled_at = %s WHERE attempt_id = %s",
                   ("t", "pa-1"))


def test_the_four_outcomes_are_EXACTLY_four():
    """Two would be the certainty publication cannot promise; a fifth would be a state nobody
    decided what to do about."""
    assert {member.value for member in PublicationOutcomeV1} == {
        "started", "succeeded", "failed", "unknown_reconciliation_required"}


def test_recording_the_same_attempt_twice_is_idempotent(db):
    _record(db)
    _record(db)
    assert db.execute("SELECT count(*) FROM publication_attempt").fetchone()[0] == 1
