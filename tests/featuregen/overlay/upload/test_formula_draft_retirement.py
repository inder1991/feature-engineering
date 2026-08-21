"""Retiring a draft that can never be deleted (migration 1096).

`formula_draft_guard` raises on every DELETE, and rightly: a draft is what a person was shown and
what an authoring run was spent on. So the cleanup runbook's original `DELETE FROM formula_draft`
could not execute at all — these tests exist because a runbook step nobody ran is a step nobody
knows is broken.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.overlay.upload.formula_draft_store import (
    record_draft_replacement,
    retire_formula_draft,
    retired_draft_ids,
)


def _draft(db, draft_id: str) -> str:
    # BLOCKED, because a READY draft must carry a formula (`formula_draft_ready_carries_a_formula`)
    # and this file is about retirement, which is state-agnostic — a draft is retired for what it
    # SAYS, not for how far it got.
    db.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, definition_revision, "
        "formula_identity_hash, state, blockers, requested_by, requested_at) "
        "VALUES (%s,'crev-r',%s,'h1','h2','h3','',%s,'BLOCKED','[\"X\"]'::jsonb,'user:ops','t')",
        (draft_id, f"opt-{draft_id}", f"ident-{draft_id}"))
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


def test_RETIRING_TWICE_IS_ONE_FACT(db):
    """A second row would let two reasons and two replacements disagree about one draft."""
    _draft(db, "fd-twice")
    retire_formula_draft(db, "fd-twice", reason="WITHDRAWN", retired_by="a@bank")
    retire_formula_draft(db, "fd-twice", reason="CANDIDATE_SUPERSEDED", retired_by="b@bank")

    rows = db.execute(
        "SELECT reason, retired_by FROM formula_draft_retirement WHERE formula_draft_id = %s",
        ("fd-twice",)).fetchall()
    assert rows == [("WITHDRAWN", "a@bank")], "the first retirement stands"


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
    record_draft_replacement(db, "fd-old", replacement_draft_id="fd-newer")

    assert db.execute(
        "SELECT replacement_draft_id FROM formula_draft_retirement WHERE formula_draft_id = %s",
        ("fd-old",)).fetchone()[0] == "fd-new", "'what replaced this' has one answer"


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
