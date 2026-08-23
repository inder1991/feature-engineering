"""Retiring a draft that can never be deleted (migration 1096).

`formula_draft_guard` raises on every DELETE, and rightly: a draft is what a person was shown and
what an authoring run was spent on. So the cleanup runbook's original `DELETE FROM formula_draft`
could not execute at all — these tests exist because a runbook step nobody ran is a step nobody
knows is broken.
"""
from __future__ import annotations

import psycopg
import pytest
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.overlay.upload.formula_draft_store import (
    DraftRetired,
    RetirementDisagreement,
    read_draft,
    record_draft_replacement,
    retire_formula_draft,
    retired_draft_ids,
)

CREV = "crev-r"
CHAIN_RUN = "fdr"

# The write-once guards standing between this file's teardown and the rows it seeded. Same
# disable/delete/re-enable shape the race tests below already use for `formula_draft`.
_CHAIN_GUARDS = (
    ("contract_generation_input", "contract_generation_input_no_mutation"),
    ("contract_considered_revision", "contract_considered_revision_no_mutation"),
    ("catalog_metadata_snapshot", "catalog_metadata_snapshot_no_mutation"),
    ("intent_recognition_attempt", "intent_recognition_attempt_no_mutation"),
)


@pytest.fixture(autouse=True)
def _considered_revision_exists(db, _dsn):
    """Migration 1116 makes `formula_draft.considered_revision_id` a real foreign key, so the one
    revision every draft in this file names has to exist. Seeding only — nothing here asserts on
    the chain.

    ▲ **AND IT MUST CLEAN UP AFTER ITSELF, because three tests below call `db.commit()`.** That
    commit does not only make their draft durable — it makes EVERYTHING in the fixture's
    transaction durable, this chain included, in a database the whole session shares. The three
    already remove their own draft in a `finally`; nothing removed the chain, so a leaked
    `contract_intent` row broke the next suite that asserts the table is empty
    (`contract/test_no_permissive_path_when_live.py`, `api/test_contract_scoped.py`) — and only
    when collection order put this file first, which is the worst way to find out.

    The teardown probes before it erases: for the fourteen tests that never commit, the rows are
    still invisible to another connection and the probe answers "nothing here", so they pay one
    SELECT and no DDL. Only a test that actually committed reaches the guard dance — and a fourth
    such test, added later, is covered without anyone remembering this."""
    chain = seed_run_chain(db, run_id=CHAIN_RUN, considered_revision_id=CREV)
    yield chain
    _erase_chain_if_committed(_dsn, chain)


def _erase_chain_if_committed(dsn: str, chain: dict) -> None:
    """Remove the seeded chain iff it was committed — all of it in ONE transaction, or none of it.

    ▲ NOT autocommit, and the guards go back on inside the same transaction. `durable_evidence.py`
    records why in full: an `ALTER TABLE ... DISABLE TRIGGER` that commits on the spot is visible
    to every other session, so a failure between the disable and the re-enable leaves a durable
    table's tamper-evidence off with nothing to restore it.
    """
    with psycopg.connect(dsn) as probe:
        committed = probe.execute(
            "SELECT 1 FROM contract_intent WHERE intent_id = %s",
            (chain["intent_id"],)).fetchone()
    if committed is None:
        return

    with psycopg.connect(dsn) as cleanup:
        with cleanup.transaction():
            for table, trigger in _CHAIN_GUARDS:
                cleanup.execute(f"ALTER TABLE {table} DISABLE TRIGGER {trigger}")
            # Children first: the input names the run, the revision names the snapshot, and both
            # the run and the recognition name the intent.
            cleanup.execute("DELETE FROM contract_generation_input WHERE generation_run_id = %s",
                            (chain["run_id"],))
            cleanup.execute(
                "DELETE FROM contract_considered_revision WHERE considered_revision_id = %s",
                (chain["considered_revision_id"],))
            cleanup.execute("DELETE FROM catalog_metadata_snapshot WHERE snapshot_id = %s",
                            (chain["snapshot_id"],))
            cleanup.execute("DELETE FROM confirmed_generation_scope WHERE scope_id = %s",
                            (chain["scope_id"],))
            cleanup.execute("DELETE FROM intent_recognition_attempt WHERE recognition_id = %s",
                            (chain["recognition_id"],))
            cleanup.execute("DELETE FROM feature_generation_run WHERE generation_run_id = %s",
                            (chain["run_id"],))
            cleanup.execute("DELETE FROM contract_intent WHERE intent_id = %s",
                            (chain["intent_id"],))
            # 1027's lineage FKs are DEFERRABLE INITIALLY DEFERRED, so the deletes above leave
            # pending trigger events and PostgreSQL refuses ENABLE TRIGGER while any are
            # outstanding. Forcing them immediate resolves them inside THIS transaction, which is
            # what keeps the re-enable here rather than in a second one.
            cleanup.execute("SET CONSTRAINTS ALL IMMEDIATE")
            for table, trigger in _CHAIN_GUARDS:
                cleanup.execute(f"ALTER TABLE {table} ENABLE TRIGGER {trigger}")


def _draft(db, draft_id: str, *, state: str = "BLOCKED") -> str:
    # BLOCKED, because a READY draft must carry a formula (`formula_draft_ready_carries_a_formula`)
    # and this file is about retirement, which is state-agnostic — a draft is retired for what it
    # SAYS, not for how far it got.
    db.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, definition_revision, "
        "formula_identity_hash, state, blockers, requested_by, requested_at) "
        "VALUES (%s,'crev-r',%s,'h1','h2','h3','',%s,%s,%s::jsonb,'user:ops','t')",
        (draft_id, f"opt-{draft_id}", f"ident-{draft_id}", state,
         '["X"]' if state == "BLOCKED" else "[]"))
    return draft_id


def test_a_DRAFT_CANNOT_BE_DELETED_which_is_why_this_table_exists(db):
    """The premise, asserted rather than assumed — the runbook step it invalidates was written
    against a table nobody had tried to delete from."""
    _draft(db, "fd-undeletable")

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("DELETE FROM formula_draft WHERE formula_draft_id = %s", ("fd-undeletable",))


def test_RETIREMENT_MARKS_A_DRAFT_WITHOUT_REMOVING_IT(db):
    """The draft stays exactly as it was: readers exclude or label it, rather than finding it
    absent, which keeps "why is this draft gone?" answerable."""
    _draft(db, "fd-retire")

    retire_formula_draft(db, "fd-retire", reason="SCHEMA_CONTRACT_MISMATCH",
                         detail="manifest 3, formula 2", retired_by="ops@bank")

    assert retired_draft_ids(db) == {"fd-retire"}
    assert db.execute(
        "SELECT state FROM formula_draft WHERE formula_draft_id = %s",
        ("fd-retire",)).fetchone()[0] == "BLOCKED", "the draft itself is untouched"


def test_REPEATING_THE_SAME_RETIREMENT_IS_FREE(db):
    """True idempotency: the same act repeated is one fact, and costs nothing to say twice."""
    _draft(db, "fd-twice")
    retire_formula_draft(db, "fd-twice", reason="WITHDRAWN", retired_by="a@bank")
    retire_formula_draft(db, "fd-twice", reason="WITHDRAWN", retired_by="a@bank")

    rows = db.execute(
        "SELECT reason, retired_by FROM formula_draft_retirement WHERE formula_draft_id = %s",
        ("fd-twice",)).fetchall()
    assert rows == [("WITHDRAWN", "a@bank")]


def test_a_SECOND_OPERATOR_DISAGREEING_IS_LOUD(db):
    """▲ NOT idempotency — two people deciding differently about one draft.

    `ON CONFLICT DO NOTHING` reported this as success, so the second decision vanished while its
    author was told it had taken effect. One of the two would have been acting on a belief the
    system had quietly discarded.
    """
    _draft(db, "fd-disagree")
    retire_formula_draft(db, "fd-disagree", reason="WITHDRAWN", retired_by="a@bank")

    with pytest.raises(RetirementDisagreement, match="already retired"):
        retire_formula_draft(db, "fd-disagree", reason="CANDIDATE_SUPERSEDED", retired_by="b@bank")

    assert db.execute(
        "SELECT reason FROM formula_draft_retirement WHERE formula_draft_id = %s",
        ("fd-disagree",)).fetchone()[0] == "WITHDRAWN", "the recorded decision is unchanged"


def test_THE_REPLACEMENT_IS_NAMED_LATER_and_only_once(db):
    """Retirement and regeneration are separate acts — regeneration spends provider money — so the
    replacement starts null rather than as a placeholder that reads as a draft nobody made."""
    _draft(db, "fd-old")
    _draft(db, "fd-new")
    _draft(db, "fd-newer")
    retire_formula_draft(db, "fd-old", reason="SCHEMA_CONTRACT_MISMATCH", retired_by="ops@bank")

    assert db.execute(
        "SELECT replacement_draft_id FROM formula_draft_retirement WHERE formula_draft_id = %s",
        ("fd-old",)).fetchone()[0] is None

    record_draft_replacement(db, "fd-old", replacement_draft_id="fd-new")
    record_draft_replacement(db, "fd-old", replacement_draft_id="fd-new")   # the same act, repeated

    with pytest.raises(RetirementDisagreement, match="already replaced"):
        record_draft_replacement(db, "fd-old", replacement_draft_id="fd-newer")

    assert db.execute(
        "SELECT replacement_draft_id FROM formula_draft_retirement WHERE formula_draft_id = %s",
        ("fd-old",)).fetchone()[0] == "fd-new", "'what replaced this' has one answer"


def test_a_REPLACEMENT_FOR_A_LIVE_DRAFT_IS_REFUSED(db):
    """It silently did nothing. A replacement recorded against a draft nobody retired would claim a
    supersession nobody decided."""
    _draft(db, "fd-live")
    _draft(db, "fd-other")

    with pytest.raises(RetirementDisagreement, match="no retirement"):
        record_draft_replacement(db, "fd-live", replacement_draft_id="fd-other")


def test_a_RETIREMENT_CANNOT_BE_DELETED(db):
    """One that could be deleted would make a draft silently current again."""
    _draft(db, "fd-perm")
    retire_formula_draft(db, "fd-perm", reason="WITHDRAWN", retired_by="ops@bank")

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("DELETE FROM formula_draft_retirement WHERE formula_draft_id = %s",
                   ("fd-perm",))


def test_a_RECORDED_REASON_IS_IMMUTABLE(db):
    _draft(db, "fd-reason")
    retire_formula_draft(db, "fd-reason", reason="WITHDRAWN", retired_by="ops@bank")

    with pytest.raises(psycopg.errors.RaiseException, match="immutable"):
        db.execute("UPDATE formula_draft_retirement SET reason = %s WHERE formula_draft_id = %s",
                   ("CANDIDATE_SUPERSEDED", "fd-reason"))


def test_a_DRAFT_CANNOT_REPLACE_ITSELF(db):
    """A retirement that retires nothing."""
    _draft(db, "fd-self")

    with pytest.raises(psycopg.errors.IntegrityError):
        retire_formula_draft(db, "fd-self", reason="WITHDRAWN", retired_by="ops@bank",
                             replacement_draft_id="fd-self")


def test_an_UNKNOWN_REASON_IS_REFUSED(db):
    """The vocabulary is closed: an open text field becomes a place to write sentences nobody
    queries."""
    _draft(db, "fd-badreason")

    with pytest.raises(psycopg.errors.IntegrityError):
        retire_formula_draft(db, "fd-badreason", reason="because", retired_by="ops@bank")


# ══ RETIREMENT IS AUTHORITATIVE, NOT DECORATIVE ════════════════════════════════════════════════
def test_a_RETIRED_DRAFT_CANNOT_ADVANCE(db):
    """▲ THE SPEND FENCE. A draft retired WHILE IN FLIGHT kept authoring — more provider calls,
    more money, and eventually a READY formula an operator had already withdrawn. Checked on every
    step rather than once at claim time, because the whole point of withdrawing one is that it
    should stop NOW."""
    from featuregen.overlay.upload.formula_draft_store import DraftRetired, DraftStateV1, advance

    _draft(db, "fd-inflight")
    retire_formula_draft(db, "fd-inflight", reason="WITHDRAWN", retired_by="ops@bank")

    with pytest.raises(DraftRetired, match="must not advance"):
        advance(db, "fd-inflight", DraftStateV1.FAILED, failure_reason="x")


def test_a_RETIRED_IDENTITY_IS_NOT_HANDED_BACK_AS_A_USABLE_DRAFT(db):
    """▲ The runbook's "use a new draft id" was incomplete, and this is why. `formula_identity_hash`
    is UNIQUE, so a fresh id lands on the SAME retired row — and the request reported
    `created=False`, which reads as "you already have an identical, usable draft"."""
    from featuregen.overlay.upload.formula_draft_store import DraftRetired, request_draft

    first, created = request_draft(
        db, formula_draft_id="fd-id-1", considered_revision_id="crev-r", option_id="opt-id",
        planning_request_hash="p", catalog_snapshot_hash="c", authoring_config_hash="a",
        definition_revision="", requested_by="ops@bank", requested_at="t")
    assert created
    retire_formula_draft(db, first, reason="SCHEMA_CONTRACT_MISMATCH", retired_by="ops@bank")

    with pytest.raises(DraftRetired, match="identity-bearing"):
        request_draft(
            db, formula_draft_id="fd-id-2", considered_revision_id="crev-r", option_id="opt-id",
            planning_request_hash="p", catalog_snapshot_hash="c", authoring_config_hash="a",
            definition_revision="", requested_by="ops@bank", requested_at="t")


def test_CHANGING_AN_IDENTITY_BEARING_INPUT_MINTS_A_NEW_DRAFT(db):
    """The other half — otherwise the refusal above would be a dead end. Correcting the authoring
    configuration changes the identity, so the request is genuinely new work."""
    from featuregen.overlay.upload.formula_draft_store import request_draft

    first, _ = request_draft(
        db, formula_draft_id="fd-cfg-1", considered_revision_id="crev-r", option_id="opt-cfg",
        planning_request_hash="p", catalog_snapshot_hash="c", authoring_config_hash="a-broken",
        definition_revision="", requested_by="ops@bank", requested_at="t")
    retire_formula_draft(db, first, reason="SCHEMA_CONTRACT_MISMATCH", retired_by="ops@bank")

    second, created = request_draft(
        db, formula_draft_id="fd-cfg-2", considered_revision_id="crev-r", option_id="opt-cfg",
        planning_request_hash="p", catalog_snapshot_hash="c", authoring_config_hash="a-fixed",
        definition_revision="", requested_by="ops@bank", requested_at="t")

    assert created is True and second == "fd-cfg-2"


def test_THE_READ_CARRIES_RETIREMENT_so_no_caller_can_forget_to_ask(db):
    """A retired draft rendered from `state` alone still reads "Formula ready"."""
    from featuregen.overlay.upload.formula_draft_store import read_draft

    _draft(db, "fd-read")
    retire_formula_draft(db, "fd-read", reason="CANDIDATE_SUPERSEDED",
                         detail="the candidate moved", retired_by="ops@bank")

    draft = read_draft(db, "fd-read")

    assert draft.is_retired is True
    assert draft.retirement.reason == "CANDIDATE_SUPERSEDED"
    assert draft.retirement.detail == "the candidate moved"
    assert draft.retirement.retired_by == "ops@bank"
    assert draft.retirement.retired_at


# ══ THE FENCE UNDER CONCURRENCY ════════════════════════════════════════════════════════════════
def test_a_TRANSITION_CANNOT_OVERLAP_AN_IN_FLIGHT_RETIREMENT(db, _dsn):
    """▲ THE RACE `WHERE NOT EXISTS` ALONE DOES NOT CLOSE, proved with two real connections.

    That predicate is evaluated under the statement's SNAPSHOT, so an UNCOMMITTED retirement is
    invisible to it — and the FK lock a retirement takes on `formula_draft` is compatible with a
    non-key update of the same row. Without `_lock_draft`, the advance below would simply commit
    while the retirement was in flight.

    The proof is that it BLOCKS: with the retirement holding the draft's lock and uncommitted, the
    advance cannot proceed, and a short `statement_timeout` turns "blocked" into an observable
    fact. Once the retirement commits, the same advance is refused outright.
    """
    import psycopg

    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, advance

    _draft(db, "fd-race")
    db.commit()

    try:
        with psycopg.connect(_dsn) as retiring, psycopg.connect(_dsn) as advancing:
            # A: retire inside an OUTER transaction, so the lock is still held when the block
            # returns. `_draft_locked` nests via savepoint when a transaction is already open and
            # commits only when it opened one itself — which is what makes it safe on the
            # runbook's autocommit connection and still caller-controlled here.
            with retiring.transaction():
                retire_formula_draft(retiring, "fd-race", reason="WITHDRAWN",
                                     retired_by="ops@bank")

                # B: the advance must not slip past an in-flight retirement.
                advancing.execute("SET statement_timeout = '750ms'")
                with pytest.raises(psycopg.errors.QueryCanceled):
                    advance(advancing, "fd-race", DraftStateV1.AUTHORING)
                advancing.rollback()

            retiring.commit()

            # And once it IS visible, the refusal is the ordinary one.
            advancing.execute("SET statement_timeout = 0")
            with pytest.raises(DraftRetired):
                advance(advancing, "fd-race", DraftStateV1.AUTHORING)
            advancing.rollback()
    finally:
        # Both tables refuse DELETE by trigger — correctly. This is TEST evidence in a durable
        # database, so it is removed the way `test_fenced_replay_integration` removes its own:
        # disable, delete, re-enable. Production never does this, which is why it is confined to a
        # `finally` in a test that needed a real second connection to exist at all.
        with psycopg.connect(_dsn, autocommit=True) as cleanup:
            cleanup.execute("ALTER TABLE formula_draft_retirement DISABLE TRIGGER "
                            "formula_draft_retirement_no_change")
            cleanup.execute("DELETE FROM formula_draft_retirement WHERE formula_draft_id = %s",
                            ("fd-race",))
            cleanup.execute("ALTER TABLE formula_draft_retirement ENABLE TRIGGER "
                            "formula_draft_retirement_no_change")
            cleanup.execute("ALTER TABLE formula_draft DISABLE TRIGGER "
                            "formula_draft_no_identity_edit")
            cleanup.execute("DELETE FROM formula_draft WHERE formula_draft_id = %s", ("fd-race",))
            cleanup.execute("ALTER TABLE formula_draft ENABLE TRIGGER "
                            "formula_draft_no_identity_edit")


def test_the_AUTHORIZED_TRANSITION_FINISHES_and_the_retirement_waits(db, _dsn):
    """▲ THE OTHER HALF OF THE CONTRACT: "an already-authorized action finishes".

    The first race test proves retirement-then-transition. This proves the reverse ordering, which
    is the half the documented contract actually promises operators — a turn already under way is
    not torn up; the retirement lands after it and stops the NEXT one.
    """
    import psycopg

    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, advance

    _draft(db, "fd-race2", state="REQUESTED")
    db.commit()

    try:
        with psycopg.connect(_dsn) as advancing, psycopg.connect(_dsn) as retiring:
            with advancing.transaction():
                advance(advancing, "fd-race2", DraftStateV1.AUTHORING)

                # The retirement must WAIT for the in-flight transition rather than interleave.
                retiring.execute("SET statement_timeout = '750ms'")
                with pytest.raises(psycopg.errors.QueryCanceled):
                    retire_formula_draft(retiring, "fd-race2", reason="WITHDRAWN",
                                         retired_by="ops@bank")
                retiring.rollback()

            advancing.commit()

            # The authorized transition stood, and the retirement now succeeds.
            retiring.execute("SET statement_timeout = 0")
            retire_formula_draft(retiring, "fd-race2", reason="WITHDRAWN", retired_by="ops@bank")
            retiring.commit()

            assert read_draft(retiring, "fd-race2").state is DraftStateV1.AUTHORING
            with pytest.raises(DraftRetired):
                advance(advancing, "fd-race2", DraftStateV1.CRITIC_REVIEW)
            advancing.rollback()
    finally:
        with psycopg.connect(_dsn, autocommit=True) as cleanup:
            cleanup.execute("ALTER TABLE formula_draft_retirement DISABLE TRIGGER "
                            "formula_draft_retirement_no_change")
            cleanup.execute("DELETE FROM formula_draft_retirement WHERE formula_draft_id = %s",
                            ("fd-race2",))
            cleanup.execute("ALTER TABLE formula_draft_retirement ENABLE TRIGGER "
                            "formula_draft_retirement_no_change")
            cleanup.execute("ALTER TABLE formula_draft DISABLE TRIGGER "
                            "formula_draft_no_identity_edit")
            cleanup.execute("DELETE FROM formula_draft WHERE formula_draft_id = %s", ("fd-race2",))
            cleanup.execute("ALTER TABLE formula_draft ENABLE TRIGGER "
                            "formula_draft_no_identity_edit")


def test_the_LOCK_LIVES_ACROSS_THE_MUTATION_ON_AN_AUTOCOMMIT_CONNECTION(db, _dsn):
    """▲ THE HOLE THE LOCK ITSELF HAD, exercised at the moment it existed.

    `pg_advisory_xact_lock` releases on commit, so on an AUTOCOMMIT connection the lock statement
    was its own transaction and the lock was gone BEFORE the insert that depended on it. The
    production worker calls inside a transaction and was fine; the cleanup runbook's operator
    connection is autocommit, and so was the fixture I first wrote for it.

    An earlier version of this test retired the draft COMPLETELY and only then tried a transition —
    which would pass with no advisory lock at all, because the committed retirement is already
    visible. The lock's LIFETIME is the thing under test, so this pauses the retirement between
    taking the lock and inserting, and proves another connection blocks during exactly that window.
    """
    import threading

    import psycopg

    from featuregen.overlay.upload import formula_draft_store as store
    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, advance

    _draft(db, "fd-auto", state="REQUESTED")
    db.commit()

    holding = threading.Event()      # the lock is taken, the insert has not happened
    release = threading.Event()      # the observer is done looking
    failures: list[BaseException] = []

    original = store._retire_locked

    def _pause_between_lock_and_insert(conn, formula_draft_id, **kwargs):
        holding.set()
        release.wait(timeout=10)
        return original(conn, formula_draft_id, **kwargs)

    def _retire() -> None:
        try:
            with psycopg.connect(_dsn, autocommit=True) as operator:
                store._retire_locked = _pause_between_lock_and_insert
                retire_formula_draft(operator, "fd-auto", reason="WITHDRAWN",
                                     retired_by="ops@bank")
        except BaseException as exc:                       # surfaced on the main thread below
            failures.append(exc)
        finally:
            store._retire_locked = original
            holding.set()

    worker = threading.Thread(target=_retire, daemon=True)
    try:
        worker.start()
        assert holding.wait(timeout=10), "the retirement never reached the lock"

        # THE WINDOW: lock held, nothing inserted yet. Nothing is visible to a reader, so only the
        # lock can stop this — which is the whole claim.
        with psycopg.connect(_dsn) as other:
            assert other.execute(
                "SELECT count(*) FROM formula_draft_retirement WHERE formula_draft_id = %s",
                ("fd-auto",)).fetchone()[0] == 0, "nothing is committed yet, so visibility is out"
            other.execute("SET statement_timeout = '750ms'")
            with pytest.raises(psycopg.errors.QueryCanceled):
                advance(other, "fd-auto", DraftStateV1.AUTHORING)
            other.rollback()

        release.set()
        worker.join(timeout=10)
        assert not failures, failures

        # And once the retirement has finished, the ordinary refusal applies.
        with psycopg.connect(_dsn) as after:
            with pytest.raises(DraftRetired):
                advance(after, "fd-auto", DraftStateV1.AUTHORING)
            after.rollback()
    finally:
        release.set()
        worker.join(timeout=10)
        store._retire_locked = original
        with psycopg.connect(_dsn, autocommit=True) as cleanup:
            cleanup.execute("ALTER TABLE formula_draft_retirement DISABLE TRIGGER "
                            "formula_draft_retirement_no_change")
            cleanup.execute("DELETE FROM formula_draft_retirement WHERE formula_draft_id = %s",
                            ("fd-auto",))
            cleanup.execute("ALTER TABLE formula_draft_retirement ENABLE TRIGGER "
                            "formula_draft_retirement_no_change")
            cleanup.execute("ALTER TABLE formula_draft DISABLE TRIGGER "
                            "formula_draft_no_identity_edit")
            cleanup.execute("DELETE FROM formula_draft WHERE formula_draft_id = %s", ("fd-auto",))
            cleanup.execute("ALTER TABLE formula_draft ENABLE TRIGGER "
                            "formula_draft_no_identity_edit")
