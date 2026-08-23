"""The pin between a chosen feature and the formula that will be built for it.

▲ The case worth reading first is `test_THE_DATABASE_REFUSES_A_MISMATCHED_PIN_not_just_the_writer`.
Everything else here tests the message; that one tests the guarantee.
"""
from __future__ import annotations

import psycopg
import pytest
from tests.featuregen.runs._chain import seed_run_chain

from featuregen.overlay.upload.selection_formula_binding import (
    BindingDisagreement,
    read_binding,
    record_selection_formula_binding,
)


def _considered(conn, considered_revision_id: str) -> None:
    """Migration 1116 makes `considered_revision_id` a real foreign key on BOTH tables seeded here,
    so whichever revision a helper names has to exist. Seeding only — no assertion depends on it."""
    seed_run_chain(conn, run_id=f"sfb-{considered_revision_id}",
                   considered_revision_id=considered_revision_id)


def _selection(conn, revision_id="sel-1", *, option_id="opt-a", considered="crev-1",
               planning="sha256:asked", binding_plan="sha256:plan") -> str:
    _considered(conn, considered)
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES ('trr-b','int-b','exploration','h') ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, binding_plan_hash, "
        "content_hash) VALUES (%s,'trr-b',%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
        (revision_id, considered, option_id, f"dec-{revision_id}", planning, binding_plan,
         f"ch-{revision_id}"))
    return revision_id


def _draft(conn, draft_id="fd-1", *, option_id="opt-a", considered="crev-1",
           planning="sha256:asked", state="READY", content="sha256:formula") -> str:
    _considered(conn, considered)
    conn.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, definition_revision, "
        "formula_identity_hash, state, formula_content_hash, formula_json, requested_by, "
        "requested_at) VALUES (%s,%s,%s,%s,'sha256:snap','sha256:cfg','rev-1',%s,%s,%s,"
        "%s::jsonb,'user:sam','t') ON CONFLICT (formula_draft_id) DO NOTHING",
        (draft_id, considered, option_id, planning, f"ident-{draft_id}", state,
         content if state == "READY" else None,
         '{"body":{}}' if state == "READY" else None))
    return draft_id


# ══ THE HAPPY PATH ══════════════════════════════════════════════════════════════════════════════
def test_A_BINDING_PINS_THE_CHOICE_TO_THE_FORMULA(db):
    _selection(db)
    _draft(db)

    binding, created = record_selection_formula_binding(
        db, selection_revision_id="sel-1", formula_draft_id="fd-1")

    assert created is True
    assert binding.formula_content_hash == "sha256:formula"
    assert read_binding(db, binding.binding_id) == binding


def test_PINNING_TWICE_IS_ONE_BINDING(db):
    """Content-addressed: asking again is the same pin, not a second one that could diverge."""
    _selection(db)
    _draft(db)

    first, created_first = record_selection_formula_binding(
        db, selection_revision_id="sel-1", formula_draft_id="fd-1")
    second, created_second = record_selection_formula_binding(
        db, selection_revision_id="sel-1", formula_draft_id="fd-1")

    assert first.binding_id == second.binding_id
    assert (created_first, created_second) == (True, False)


# ══ THE GUARANTEE ══════════════════════════════════════════════════════════════════════════════
def test_THE_DATABASE_REFUSES_A_MISMATCHED_PIN_not_just_the_writer(db):
    """▲ THE POINT OF THE WHOLE MIGRATION, asserted without going through the writer at all.

    A loose `(formula_draft_id, formula_content_hash)` pair on the member would have permitted a
    valid READY formula belonging to a DIFFERENT feature: the id exists, the hash matches, the build
    proceeds. The composite foreign keys make that row unrepresentable — so this holds even for a
    caller that bypasses `record_selection_formula_binding` entirely, which a future writer, a data
    migration or a repair script certainly will.
    """
    _selection(db, "sel-a", option_id="opt-a")
    _draft(db, "fd-b", option_id="opt-b")              # a real formula, for another candidate

    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        db.execute(
            "INSERT INTO selection_formula_binding (binding_id, selection_revision_id, "
            "formula_draft_id, formula_content_hash, considered_revision_id, option_id, "
            "planning_request_hash, binding_plan_hash) "
            "VALUES ('b-forged','sel-a','fd-b','sha256:formula','crev-1','opt-a',"
            "'sha256:asked','sha256:plan')")


# ══ THE MESSAGES ═══════════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("field,over", [
    ("considered revision", {"considered": "crev-other"}),
    ("option", {"option_id": "opt-other"}),
    ("planning request", {"planning": "sha256:different-question"}),
])
def test_A_DISAGREEMENT_NAMES_WHICH_FACT_DISAGREED(db, field, over):
    """▲ The database refuses these too. This exists so the refusal SAYS WHY: "the draft belongs to
    another candidate" is actionable, and a foreign-key violation citing five columns is a puzzle."""
    _selection(db, "sel-a")
    _draft(db, "fd-x", **over)

    with pytest.raises(BindingDisagreement, match=field):
        record_selection_formula_binding(
            db, selection_revision_id="sel-a", formula_draft_id="fd-x")


def test_A_DRAFT_WITH_NO_FORMULA_CANNOT_BE_PINNED(db):
    """A pin names a FORMULA. Pinning a draft that never produced one would put a build set behind a
    pin with nothing behind it — and the build would only discover that much later."""
    _selection(db)
    # REQUESTED rather than BLOCKED: `formula_draft_blocked_names_blockers` requires a BLOCKED
    # draft to carry its blockers, and inventing some here would be seeding a state to satisfy a
    # constraint rather than to test anything.
    _draft(db, "fd-pending", state="REQUESTED")

    with pytest.raises(BindingDisagreement, match="produced no formula"):
        record_selection_formula_binding(
            db, selection_revision_id="sel-1", formula_draft_id="fd-pending")


def test_A_MISSING_SELECTION_OR_DRAFT_IS_NAMED(db):
    _selection(db)
    _draft(db)

    with pytest.raises(BindingDisagreement, match="selection sel-nope does not exist"):
        record_selection_formula_binding(
            db, selection_revision_id="sel-nope", formula_draft_id="fd-1")
    with pytest.raises(BindingDisagreement, match="formula draft fd-nope does not exist"):
        record_selection_formula_binding(
            db, selection_revision_id="sel-1", formula_draft_id="fd-nope")


# ══ IMMUTABILITY ═══════════════════════════════════════════════════════════════════════════════
def test_A_BINDING_CANNOT_BE_EDITED(db):
    """A pin that can be edited after the build was decided is not a pin."""
    _selection(db)
    _draft(db)
    binding, _ = record_selection_formula_binding(
        db, selection_revision_id="sel-1", formula_draft_id="fd-1")

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE selection_formula_binding SET formula_draft_id = 'fd-other' "
                   "WHERE binding_id = %s", (binding.binding_id,))


def test_A_BINDING_CANNOT_BE_DELETED(db):
    _selection(db)
    _draft(db)
    binding, _ = record_selection_formula_binding(
        db, selection_revision_id="sel-1", formula_draft_id="fd-1")

    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("DELETE FROM selection_formula_binding WHERE binding_id = %s",
                   (binding.binding_id,))


def test_AN_UNKNOWN_BINDING_IS_NONE(db):
    assert read_binding(db, "b-nobody-made") is None
