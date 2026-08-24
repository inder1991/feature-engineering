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


# ══ Stage I Task 6 — Option 2: the deterministic lane's retries are FREE BY CONSTRUCTION ════════
def _failed_draft(db, draft_id: str) -> str:
    """A FAILED occupant AT THE IDENTITY a re-request will compute — seeded with the store's own
    `formula_identity` over the same fields, or the gates under test would never even see it.
    FAILED carries its reason IN the insert (`formula_draft_failure_reason_belongs_to_failed`)."""
    from featuregen.overlay.upload.formula_draft_store import formula_identity

    identity = formula_identity(
        considered_revision_id="crev-r", option_id=f"opt-{draft_id}",
        planning_request_hash="h1", catalog_snapshot_hash="h2", authoring_config_hash="h3",
        definition_revision="")
    db.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, "
        "definition_revision, formula_identity_hash, state, failure_reason, requested_by, "
        "requested_at) VALUES (%s,'crev-r',%s,'h1','h2','h3','',%s,'FAILED','renderer crash',"
        "'user:ops','t')",
        (draft_id, f"opt-{draft_id}", identity))
    return identity


def _request_again(db, *, draft_id: str, option_id: str,
                   provider_contract_hash=None, strategy_identity_hash=None):
    from featuregen.overlay.upload.formula_draft_store import request_draft

    return request_draft(
        db, formula_draft_id=draft_id, considered_revision_id="crev-r", option_id=option_id,
        planning_request_hash="h1", catalog_snapshot_hash="h2", authoring_config_hash="h3",
        definition_revision="", requested_by="user:ops", requested_at="2026-08-23T00:00:00Z",
        provider_contract_hash=provider_contract_hash,
        strategy_identity_hash=strategy_identity_hash)


def test_a_FAILED_DETERMINISTIC_draft_re_requests_FREE_no_exception_no_spend(db):
    """The owner's Option 2 ruling (spec R4.2 gap 3): a reviewed-blueprint attempt carries no
    provider contract, calls no provider and spends nothing — so the re-spend gate does not
    apply, and a fresh row mints with ZERO exception rows and ZERO spend rows."""
    identity = _failed_draft(db, "fd-det")

    minted, created = _request_again(
        db, draft_id="fd-det-retry", option_id="opt-fd-det")

    assert (minted, created) == ("fd-det-retry", True)
    assert db.execute(
        "SELECT COUNT(*) FROM formula_draft_regeneration_exception").fetchone() == (0,)
    assert db.execute(
        "SELECT COUNT(*) FROM llm_spend_authorization_revision").fetchone() == (0,)
    history = db.execute(
        "SELECT COUNT(*) FROM formula_draft WHERE formula_identity_hash = %s", (identity,)
    ).fetchone()
    assert history == (2,), "the failed row stays as history beside the fresh attempt"


def test_a_TOMBSTONE_refuses_the_deterministic_lane_too_withdrawal_is_not_a_cost(db):
    """The precedence pin the ruling demands: tombstones refuse FIRST, both lanes — the free
    path frees the WALLET check, never the WITHDRAWAL check."""
    from featuregen.overlay.upload.formula_draft_store import DraftRetired
    from featuregen.overlay.upload.retirement_scope import RetirementScope, record_tombstone

    _failed_draft(db, "fd-det-ret")
    record_tombstone(db, formula_draft_id="fd-det-ret",
                     scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
                     reason="superseded", retired_by="user:owner")

    with pytest.raises(DraftRetired):
        _request_again(db, draft_id="fd-det-ret-retry", option_id="opt-fd-det-ret")


def test_an_LLM_FAILURE_without_an_exception_is_UNCHANGED_still_not_an_answer(db):
    from featuregen.overlay.upload.formula_draft_store import DraftNotAnAnswer

    _failed_draft(db, "fd-llm")
    with pytest.raises(DraftNotAnAnswer):
        _request_again(db, draft_id="fd-llm-retry", option_id="opt-fd-llm",
                       provider_contract_hash="sha256:contract",
                       strategy_identity_hash="sih-llm")


def test_an_exception_that_does_not_NAME_the_withdrawal_cannot_override_it(db):
    """Task 6 review item 3: a coupon minted BEFORE the tombstone (tombstone_id NULL) must not
    unlock a LATER withdrawal — overriding a withdrawal is an act about THAT withdrawal, and an
    approval nobody weighed against it carries no audit linkage. The retry refuses as retired."""
    from featuregen.overlay.upload.formula_draft_store import DraftRetired
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        RetirementScope,
        approve_regeneration_exception,
        record_tombstone,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-prearm")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-prearm",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-prearm",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")
    # The coupon, minted while NO tombstone covers the target → tombstone_id NULL.
    approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z",
        scope_key=scope)
    # The withdrawal lands AFTERWARDS.
    record_tombstone(db, formula_draft_id="fd-prearm",
                     scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
                     reason="superseded", retired_by="user:owner")

    with pytest.raises(DraftRetired):
        _request_again(db, draft_id="fd-prearm-retry", option_id="opt-fd-prearm",
                       provider_contract_hash="sha256:llm",
                       strategy_identity_hash="sih-llm")


def test_THE_OVERRIDE_MINT_CAN_FINISH_the_fence_honors_the_naming_coupon(db):
    """Task 6 round-2 item 3, the acceptance chain: FAILED → tombstone → approval NAMING it →
    retry MINTS → and the worker's advances SUCCEED all the way to READY. Without the fence
    exemption, the override case bought a draft that could never complete — the surface was
    ornamental for its one purpose. A sibling draft with NO naming coupon still fences."""
    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, advance
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        RetirementScope,
        approve_regeneration_exception,
        record_tombstone,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-finish")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-finish",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    record_tombstone(db, formula_draft_id="fd-finish",
                     scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
                     reason="superseded", retired_by="user:owner")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-finish",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")
    # Approved AFTER the withdrawal — through the writer, which derives and NAMES the covering
    # tombstone via the one reader.
    approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z", scope_key=scope)

    minted, created = _request_again(
        db, draft_id="fd-finish-retry", option_id="opt-fd-finish",
        provider_contract_hash="sha256:llm", strategy_identity_hash="sih-llm")
    assert created is True

    # The worker's whole ladder, under the covering tombstone, to READY.
    advance(db, minted, DraftStateV1.AUTHORING, authoring_run_id=None)
    advance(db, minted, DraftStateV1.CRITIC_REVIEW)
    advance(db, minted, DraftStateV1.VALIDATING)
    advance(db, minted, DraftStateV1.ADMISSION)
    final = advance(db, minted, DraftStateV1.READY,
                    formula_content_hash="sha256:f-finish",
                    formula_json={"formula_schema_version": 3})
    assert final is DraftStateV1.READY

    # Negative control: a DIFFERENT candidate under a tombstone with NO naming coupon fences.
    from featuregen.overlay.upload.formula_draft_store import DraftRetired

    other = _draft(db, "fd-nofinish", state="REQUESTED")
    record_tombstone(db, formula_draft_id="fd-nofinish",
                     scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
                     reason="withdrawn", retired_by="user:owner")
    with pytest.raises(DraftRetired, match="must not advance"):
        advance(db, other, DraftStateV1.AUTHORING)


def test_a_YOUNGER_NAMING_COUPON_is_not_shadowed_by_an_older_blank_one(db):
    """Round-2 item 2: the filter lives INSIDE the locator. Coupon A (pre-withdrawal, names
    nothing) → withdrawal → coupon B (names it): the retry MINTS under B — the refusal's own
    remedy actually works now, instead of A shadowing B until it expires."""
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        RetirementScope,
        approve_regeneration_exception,
        record_tombstone,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-shadow")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-shadow",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")

    def _spend(tag):
        return authorize_spend(
            db, action="AUTHOR_FORMULA", actor_subject="user:owner",
            job_identity=f"job-shadow-{tag}", member_identities=[identity],
            provider_contract_hash="sha256:llm", max_calls=5, max_tokens=1000,
            currency="USD", max_cost="1.00", pricing_version="p@1",
            expires_at="2026-12-31T00:00:00Z")

    # Coupon A, minted while nothing covers the target (tombstone_id NULL).
    approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=_spend("a"), expires_at="2026-12-31T00:00:00Z",
        scope_key=scope)
    record_tombstone(db, formula_draft_id="fd-shadow",
                     scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
                     reason="superseded", retired_by="user:owner")
    # Coupon B — the operator obeying the refusal — NAMES the withdrawal.
    approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=_spend("b"), expires_at="2026-12-31T00:00:00Z",
        scope_key=scope)

    minted, created = _request_again(
        db, draft_id="fd-shadow-retry", option_id="opt-fd-shadow",
        provider_contract_hash="sha256:llm", strategy_identity_hash="sih-llm")
    assert created is True, "B unlocks; A no longer shadows it"


def _override_minted(db, tag: str) -> tuple[str, str]:
    """A draft minted under a coupon naming the candidate-wide withdrawal — round-3's shared
    arrangement. Returns (minted_draft_id, its identity)."""
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        RetirementScope,
        approve_regeneration_exception,
        record_tombstone,
        retirement_scope_key,
    )

    identity = _failed_draft(db, f"fd-{tag}")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id=f"opt-fd-{tag}",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    record_tombstone(db, formula_draft_id=f"fd-{tag}",
                     scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
                     reason="superseded", retired_by="user:owner")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity=f"job-{tag}",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")
    approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z", scope_key=scope)
    minted, created = _request_again(
        db, draft_id=f"fd-{tag}-retry", option_id=f"opt-fd-{tag}",
        provider_contract_hash="sha256:llm", strategy_identity_hash="sih-llm")
    assert created is True
    return minted, identity


def test_an_EXACT_withdrawal_of_the_override_draft_STOPS_it_mid_ladder(db):
    """Round-3's blocking probe: the candidate-first pick let the OLD broad tombstone (which the
    coupon names) MASK a fresh EXACT_DRAFT withdrawal of the mint itself — the fence advanced a
    freshly-withdrawn draft. The ONE LAW carries this now: every covering withdrawal must be
    individually NAMED by a consumed coupon, and this fresh exact withdrawal is named by none —
    so the fence refuses. (NOT because exact withdrawals post-date coupons — NB-1's governed
    exact override disproves that — but because THIS one is un-named.)"""
    from featuregen.overlay.upload.formula_draft_store import (
        DraftRetired,
        DraftStateV1,
        advance,
    )
    from featuregen.overlay.upload.retirement_scope import RetirementScope, record_tombstone

    minted, _identity = _override_minted(db, "exact")
    advance(db, minted, DraftStateV1.AUTHORING)   # the ladder starts under the named override

    # The operator withdraws the OVERRIDE DRAFT ITSELF, mid-ladder, exactly.
    record_tombstone(db, formula_draft_id=minted, scope=RetirementScope.EXACT_DRAFT,
                     reason="wrong after all", retired_by="user:owner")

    with pytest.raises(DraftRetired, match="no consumed regeneration exception names"):
        advance(db, minted, DraftStateV1.CRITIC_REVIEW)


def test_an_EXPIRED_coupon_still_lets_the_ladder_FINISH_deliberately(db):
    """Round-3, stated and pinned rather than implied: the coupon's expiry bounds NEW MINTS —
    the authorized moment was the mint, the money was reserved then, and refusing mid-ladder
    because the coupon aged out would re-strand exactly the draft the approval freed."""
    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, advance

    minted, identity = _override_minted(db, "expired")
    db.execute(
        "UPDATE formula_draft_regeneration_exception SET expires_at = now() - interval '1 day' "
        "WHERE target_formula_identity_hash = %s", (identity,))

    advance(db, minted, DraftStateV1.AUTHORING)
    advance(db, minted, DraftStateV1.CRITIC_REVIEW)
    advance(db, minted, DraftStateV1.VALIDATING)
    advance(db, minted, DraftStateV1.ADMISSION)
    final = advance(db, minted, DraftStateV1.READY,
                    formula_content_hash="sha256:f-exp",
                    formula_json={"formula_schema_version": 3})
    assert final is DraftStateV1.READY


def test_the_REQUEST_gate_shares_the_masking_fix(db):
    """The same class at the mint: a coupon naming the broad tombstone must not slip past an
    EXACT withdrawal of the target identity that the candidate-first pick masked."""
    from featuregen.overlay.upload.formula_draft_store import DraftRetired
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        RetirementScope,
        approve_regeneration_exception,
        record_tombstone,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-mask")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-mask",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    record_tombstone(db, formula_draft_id="fd-mask",
                     scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
                     reason="superseded", retired_by="user:owner")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-mask",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")
    approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z", scope_key=scope)
    # An EXACT withdrawal of the target identity, recorded after the approval, masked by the
    # broad pick — must still refuse the mint.
    record_tombstone(db, formula_draft_id="fd-mask", scope=RetirementScope.EXACT_DRAFT,
                     reason="this exact one is wrong", retired_by="user:owner")

    with pytest.raises(DraftRetired):
        _request_again(db, draft_id="fd-mask-retry", option_id="opt-fd-mask",
                       provider_contract_hash="sha256:llm", strategy_identity_hash="sih-llm")


def test_NB1_a_governed_override_of_an_EXACT_withdrawal_completes(db):
    """Round-4's NB-1: under round 3, this journey minted (coupon consumed, spend reserved) and
    died at the first advance — dead on arrival with no exit. Under the ONE LAW the coupon names
    the exact withdrawal and the ladder runs to READY."""
    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, advance
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        RetirementScope,
        approve_regeneration_exception,
        record_tombstone,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-nb1")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-nb1",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    # The withdrawal is EXACT — the default scope an operator reaches for.
    record_tombstone(db, formula_draft_id="fd-nb1", scope=RetirementScope.EXACT_DRAFT,
                     reason="bad numbers", retired_by="user:owner")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-nb1",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")
    ids, created = approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z", scope_key=scope)
    assert created and len(ids) == 1, "one covering withdrawal, one binding"

    minted, was_created = _request_again(
        db, draft_id="fd-nb1-retry", option_id="opt-fd-nb1",
        provider_contract_hash="sha256:llm", strategy_identity_hash="sih-llm")
    assert was_created is True

    for to in (DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW, DraftStateV1.VALIDATING,
               DraftStateV1.ADMISSION):
        advance(db, minted, to)
    final = advance(db, minted, DraftStateV1.READY, formula_content_hash="sha256:f-nb1",
                    formula_json={"formula_schema_version": 3})
    assert final is DraftStateV1.READY


def test_NB2_both_kinds_cover_ONE_approval_binds_both_and_the_ladder_runs(db):
    """Round-4's NB-2: both scopes covering was unmintable by construction (the writer picked
    one; the gate demanded the other). One approval act now binds BOTH; the mint consumes BOTH;
    the ladder runs to READY."""
    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, advance
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        RetirementScope,
        approve_regeneration_exception,
        record_tombstone,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-nb2")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-nb2",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    record_tombstone(db, formula_draft_id="fd-nb2",
                     scope=RetirementScope.CANDIDATE_ACROSS_CONFIGURATIONS,
                     reason="superseded", retired_by="user:owner")
    record_tombstone(db, formula_draft_id="fd-nb2", scope=RetirementScope.EXACT_DRAFT,
                     reason="and this exact one is wrong", retired_by="user:owner")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-nb2",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")
    ids, _ = approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z", scope_key=scope)
    assert len(ids) == 2, "one binding PER covering withdrawal, in one act"

    minted, was_created = _request_again(
        db, draft_id="fd-nb2-retry", option_id="opt-fd-nb2",
        provider_contract_hash="sha256:llm", strategy_identity_hash="sih-llm")
    assert was_created is True
    consumed = db.execute(
        "SELECT COUNT(*) FROM formula_draft_regeneration_exception "
        "WHERE target_formula_identity_hash = %s AND uses_consumed = 1", (identity,)).fetchone()
    assert consumed == (2,), "the mint consumed BOTH coupons"

    for to in (DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW, DraftStateV1.VALIDATING,
               DraftStateV1.ADMISSION):
        advance(db, minted, to)
    assert advance(db, minted, DraftStateV1.READY, formula_content_hash="sha256:f-nb2",
                   formula_json={"formula_schema_version": 3}) is DraftStateV1.READY


def test_the_fence_refusal_carries_the_REPLACEMENT_POINTER(db):
    """Round-4 NB-3's fence half: the refusal names the actual un-named withdrawal INCLUDING its
    replacement pointer — the thing the operator usually actually needs."""
    from featuregen.overlay.upload.formula_draft_store import (
        DraftRetired,
        DraftStateV1,
        advance,
    )
    from featuregen.overlay.upload.retirement_scope import RetirementScope, record_tombstone

    minted, _identity = _override_minted(db, "ptr")
    advance(db, minted, DraftStateV1.AUTHORING)
    # A replacement-bearing EXACT withdrawal lands mid-ladder.
    other = _draft(db, "fd-ptr-replacement", state="BLOCKED")
    record_tombstone(db, formula_draft_id=minted, scope=RetirementScope.EXACT_DRAFT,
                     reason="superseded mid-flight", retired_by="user:owner",
                     replacement_draft_id=other)

    with pytest.raises(DraftRetired, match="use fd-ptr-replacement"):
        advance(db, minted, DraftStateV1.CRITIC_REVIEW)


def test_SECOND_FAILURE_a_fresh_approval_is_a_FRESH_coupon_not_the_exhausted_one(db):
    """Round-4 acceptance probe 1: without the regeneration ordinal in the exception identity, a
    same-day same-ceiling re-approval after the override FAILED AGAIN content-addressed straight
    back to the exhausted coupon — the dead end one level up. Each approval generation now has
    its own identity, and the second journey completes."""
    from featuregen.overlay.upload.formula_draft_store import DraftStateV1, advance
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        approve_regeneration_exception,
        retirement_scope_key,
    )

    minted, identity = _override_minted(db, "second")   # first override consumed its coupon
    db.execute("UPDATE formula_draft SET state = 'FAILED', failure_reason = 'again' "
               "WHERE formula_draft_id = %s", (minted,))

    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-second",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-second",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")
    ids, created = approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z", scope_key=scope)
    assert created is True, "a fresh GENERATION, not the exhausted coupon handed back"

    second, was_created = _request_again(
        db, draft_id="fd-second-retry2", option_id="opt-fd-second",
        provider_contract_hash="sha256:llm", strategy_identity_hash="sih-llm")
    assert was_created is True
    advance(db, second, DraftStateV1.AUTHORING)   # and it can run — not dead on arrival


def test_ORDINAL_EDGES_no_stacking_while_live_and_no_resurrection_after_newer(db):
    """Round-4 delta probes: (a) an identical re-approval while a coupon LIVES converges on that
    coupon — the ordinal is unchanged, so the identity is, so ON CONFLICT collapses it: no
    stacking. (b) After exhaustion mints generation 1, a replay of the ORIGINAL approval also
    lands on generation 1 — the ordinal counts exhausted coupons, so the old request's identity
    now points at the newest generation: no resurrection, no third coupon."""
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        approve_regeneration_exception,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-ord")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-ord",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-ord",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")

    def _approve():
        return approve_regeneration_exception(
            db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
            strategy_identity_hash="sih-llm", actor_subject="user:owner",
            llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z",
            scope_key=scope)

    gen0, created0 = _approve()
    again, created_again = _approve()
    assert created0 is True and created_again is False
    assert again == gen0, "(a) live coupon: identical re-approval is THAT coupon, not a stack"

    minted, _ = _request_again(db, draft_id="fd-ord-retry", option_id="opt-fd-ord",
                               provider_contract_hash="sha256:llm",
                               strategy_identity_hash="sih-llm")
    db.execute("UPDATE formula_draft SET state = 'FAILED', failure_reason = 'again' "
               "WHERE formula_draft_id = %s", (minted,))
    gen1, created1 = _approve()
    assert created1 is True and gen1 != gen0, "post-exhaustion: a fresh generation"

    replay, created_replay = _approve()
    assert created_replay is False
    assert replay == gen1, "(b) the old request's replay lands on the NEWEST generation"
    total = db.execute(
        "SELECT COUNT(*) FROM formula_draft_regeneration_exception "
        "WHERE target_formula_identity_hash = %s", (identity,)).fetchone()
    assert total == (2,), "two generations, never a third from replays"


def test_C1_a_SIBLING_bindings_exhaustion_never_bumps_THIS_bindings_ordinal(db):
    """Round-5 C1, the probe verbatim: two admins approve the same regeneration (two bindings
    differing only in actor), the mint consumes ONE coupon, and a plain replay of the OTHER
    admin's still-live approval must be THAT coupon — created False, no third row. The ordinal
    counts exhaustion per EXACT binding; a sibling binding's spent coupon is not this
    binding's history, and one governance decision never becomes two paid regenerations."""
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        approve_regeneration_exception,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-sib")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-sib",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-sib",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")

    def _approve(actor):
        return approve_regeneration_exception(
            db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
            strategy_identity_hash="sih-llm", actor_subject=actor,
            llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z",
            scope_key=scope)

    (first,), created_first = _approve("user:admin-one")
    (second,), created_second = _approve("user:admin-two")
    assert created_first is True and created_second is True and first != second

    minted, _ = _request_again(db, draft_id="fd-sib-retry", option_id="opt-fd-sib",
                               provider_contract_hash="sha256:llm",
                               strategy_identity_hash="sih-llm")
    assert minted is not None

    consumed = {row[0] for row in db.execute(
        "SELECT exception_id FROM formula_draft_regeneration_exception "
        "WHERE target_formula_identity_hash = %s AND uses_consumed >= max_uses",
        (identity,)).fetchall()}
    survivors = {first, second} - consumed
    assert len(survivors) == 1, "the mint consumed one binding's coupon, not both"
    live_actor = "user:admin-one" if first in survivors else "user:admin-two"

    (replayed,), created_replay = _approve(live_actor)
    assert created_replay is False, "a replay of a still-live approval mints NOTHING"
    assert replayed in survivors, "the replay IS the live coupon"
    total = db.execute(
        "SELECT COUNT(*) FROM formula_draft_regeneration_exception "
        "WHERE target_formula_identity_hash = %s", (identity,)).fetchone()
    assert total == (2,), "no third row — one decision, one paid regeneration per binding"


def test_DEAD_TICKET_an_exhausted_ceiling_refuses_BEFORE_the_coupon_is_consumed(db):
    """Task 7 review item 1 — round-3's dead-ticket shape at the ORIGINAL door: with the
    approved ceiling spent to zero, the request must refuse (typed) BEFORE consuming the naming
    coupon and BEFORE minting a draft that dies at the dispatch seam. And the refusal must be
    remediable: a fresh cost-confirmed approval makes the same request mint."""
    import pytest

    from featuregen.overlay.upload.formula_draft_store import DraftCeilingExhausted
    from featuregen.overlay.upload.llm_spend import authorize_spend, reserve_spend
    from featuregen.overlay.upload.retirement_scope import (
        approve_regeneration_exception,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-dead")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-dead",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")

    def _approve(spend_id):
        return approve_regeneration_exception(
            db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
            strategy_identity_hash="sih-llm", actor_subject="user:owner",
            llm_spend_authorization_id=spend_id, expires_at="2026-12-31T00:00:00Z",
            scope_key=scope)

    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-dead",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")
    (coupon,), _ = _approve(spend)
    now = db.execute("SELECT now()").fetchone()[0]
    reserve_spend(db, spend_authorization_id=spend, calls=5, tokens=1000, cost="1.00", now=now)

    with pytest.raises(DraftCeilingExhausted, match="cannot dispatch"):
        _request_again(db, draft_id="fd-dead-retry", option_id="opt-fd-dead",
                       provider_contract_hash="sha256:llm", strategy_identity_hash="sih-llm")
    consumed, drafts = db.execute(
        "SELECT (SELECT uses_consumed FROM formula_draft_regeneration_exception "
        "        WHERE exception_id = %s), "
        "       (SELECT COUNT(*) FROM formula_draft WHERE formula_identity_hash = %s)",
        (coupon, identity)).fetchone()
    assert consumed == 0, "the refusal did NOT burn the naming coupon"
    assert drafts == 1, "only the FAILED history row — no dead ticket was minted"

    fresh = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-dead-2",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-30T00:00:00Z")
    (fresh_coupon,), _ = _approve(fresh)
    # In-test both approvals share one frozen transaction now(); real acts are separate
    # transactions, so the later one carries the later approved_at the DESC pick rides on.
    db.execute("UPDATE formula_draft_regeneration_exception "
               "SET approved_at = approved_at + interval '1 second' "
               "WHERE exception_id = %s", (fresh_coupon,))
    minted, created = _request_again(db, draft_id="fd-dead-retry2", option_id="opt-fd-dead",
                                     provider_contract_hash="sha256:llm",
                                     strategy_identity_hash="sih-llm")
    assert created is True and minted == "fd-dead-retry2", \
        "a fresh cost-confirmed approval is the remedy, and it works"


def test_an_EXPIRED_authorization_is_not_a_dead_ticket_the_mint_rides_the_envelope(db):
    """The guard's None path, pinned: when the approval's authorization has EXPIRED, the
    preference locator returns None and the service rides its bounded development envelope —
    the mint CAN complete, so the store must NOT refuse. Exhaustion refuses; expiry falls
    through to the envelope. (Expired-AND-exhausted is unconstructible in THIS order —
    `reserve_spend` refuses expired authorizations — but exhaust-then-WAIT reaches it; benign,
    expiry dominates and the locator filters the row either way, so expiry alone is the pin.)"""
    from featuregen.overlay.upload.llm_spend import authorize_spend
    from featuregen.overlay.upload.retirement_scope import (
        approve_regeneration_exception,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-exp")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-exp",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-exp",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2020-01-01T00:00:00Z")
    (coupon,), _ = approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z", scope_key=scope)

    minted, created = _request_again(db, draft_id="fd-exp-retry", option_id="opt-fd-exp",
                                     provider_contract_hash="sha256:llm",
                                     strategy_identity_hash="sih-llm")
    assert created is True, "expired is the envelope's case, not the dead-ticket refusal's"
    consumed = db.execute(
        "SELECT uses_consumed FROM formula_draft_regeneration_exception "
        "WHERE exception_id = %s", (coupon,)).fetchone()
    assert consumed == (1,), "the coupon still authorizes THIS mint and is consumed by it"


def test_a_SLIVER_remainder_below_one_call_is_still_a_dead_ticket(db):
    """Scoped-review item 1, the probe verbatim: a remainder above ZERO but below ONE per-call
    worst-case reservation (5-call/1000-token/$1.00 ceiling, 4 calls/900 tokens/$0.80 already
    reserved → 1 call/100 tokens/$0.20 left, but one call reserves tokens=ceil(1000/5)=200)
    must refuse with the coupon unburned — the zero floor let it through to die at the FIRST
    dispatch reserve, one seam late and one coupon poorer."""
    import pytest

    from featuregen.overlay.upload.formula_draft_store import DraftCeilingExhausted
    from featuregen.overlay.upload.llm_spend import authorize_spend, reserve_spend
    from featuregen.overlay.upload.retirement_scope import (
        approve_regeneration_exception,
        retirement_scope_key,
    )

    identity = _failed_draft(db, "fd-sliv")
    scope = retirement_scope_key(
        considered_revision_id="crev-r", option_id="opt-fd-sliv",
        planning_request_hash="h1", catalog_snapshot_hash="h2", definition_revision="")
    spend = authorize_spend(
        db, action="AUTHOR_FORMULA", actor_subject="user:owner", job_identity="job-sliv",
        member_identities=[identity], provider_contract_hash="sha256:llm", max_calls=5,
        max_tokens=1000, currency="USD", max_cost="1.00", pricing_version="p@1",
        expires_at="2026-12-31T00:00:00Z")
    (coupon,), _ = approve_regeneration_exception(
        db, target_formula_identity_hash=identity, provider_contract_hash="sha256:llm",
        strategy_identity_hash="sih-llm", actor_subject="user:owner",
        llm_spend_authorization_id=spend, expires_at="2026-12-31T00:00:00Z", scope_key=scope)
    now = db.execute("SELECT now()").fetchone()[0]
    reserve_spend(db, spend_authorization_id=spend, calls=4, tokens=900, cost="0.80", now=now)

    with pytest.raises(DraftCeilingExhausted, match="cannot cover one more call"):
        _request_again(db, draft_id="fd-sliv-retry", option_id="opt-fd-sliv",
                       provider_contract_hash="sha256:llm", strategy_identity_hash="sih-llm")
    consumed, drafts = db.execute(
        "SELECT (SELECT uses_consumed FROM formula_draft_regeneration_exception "
        "        WHERE exception_id = %s), "
        "       (SELECT COUNT(*) FROM formula_draft WHERE formula_identity_hash = %s)",
        (coupon, identity)).fetchone()
    assert consumed == 0 and drafts == 1, "coupon unburned, no dead ticket minted"
