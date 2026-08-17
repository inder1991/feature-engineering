"""Draft formula, async: the two identities, the state machine, and the money guard.

The product rules under test, in the order they matter:

1. **A double-click must not buy two authoring runs.** Idempotency is on the formula IDENTITY, not a
   caller-supplied key — a client minting a fresh key per click would defeat a key-based guard.
2. **An engine gaining an operator must not buy the same answer again.** Admission has its OWN
   identity, so a moved capability set re-decides an EXISTING formula for free.
3. **BLOCKED is a product result, FAILED is not.** They are different states with different remedies.
4. **No transaction spans a provider call** — enforced by the store only ever moving ONE step.
"""
from __future__ import annotations

import psycopg
import pytest

from featuregen.overlay.upload.formula_draft_store import (
    DraftStateV1,
    InvalidTransition,
    admission_identity,
    advance,
    capability_set_hash,
    existing_admission,
    formula_identity,
    read_draft,
    record_admission,
    request_draft,
)

REV = "crev-1"
OPTION = "opt-a"


def _identity_kwargs(**overrides):
    kwargs = dict(
        considered_revision_id=REV, option_id=OPTION,
        planning_request_hash="sha256:asked", catalog_snapshot_hash="sha256:catalog",
        authoring_config_hash="sha256:config", definition_revision="")
    kwargs.update(overrides)
    return kwargs


def _request(db, draft_id="fd-1", **overrides):
    return request_draft(
        db, formula_draft_id=draft_id, requested_by="user:ops",
        requested_at="2026-08-17T00:00:00Z", **_identity_kwargs(**overrides))


# ══ THE MONEY GUARD ═════════════════════════════════════════════════════════════════════════════
def test_A_DOUBLE_CLICK_DOES_NOT_BUY_TWO_AUTHORING_RUNS(db):
    """The second request finds the first draft. `created=False` is the answer a client needs to
    avoid reporting a spend that did not happen."""
    first_id, first_created = _request(db, "fd-1")
    second_id, second_created = _request(db, "fd-2")

    assert first_created is True
    assert second_created is False
    assert second_id == first_id == "fd-1"
    assert db.execute("SELECT count(*) FROM formula_draft").fetchone()[0] == 1


@pytest.mark.parametrize("field", [
    "planning_request_hash", "catalog_snapshot_hash", "authoring_config_hash",
    "definition_revision", "option_id", "considered_revision_id",
])
def test_EVERY_PART_OF_THE_FORMULA_IDENTITY_IS_LOAD_BEARING(db, field):
    """Each is something whose movement legitimately changes what the model would write, so each
    must produce a NEW draft rather than reuse the old answer. Parametrized so the rule holds for
    all six rather than for whichever one someone thought of."""
    _request(db, "fd-1")
    _id, created = _request(db, "fd-2", **{field: "something-else"})
    assert created is True, f"{field} moved and the draft was reused anyway"
    assert db.execute("SELECT count(*) FROM formula_draft").fetchone()[0] == 2


def test_the_ENGINE_IS_NOT_PART_OF_THE_FORMULA_IDENTITY():
    """The crux of the reuse rule. Capabilities decide whether a formula may be USED, not what the
    model would write — folding them in would buy the same answer again every time an engine gained
    an operator."""
    identity = formula_identity(**_identity_kwargs())
    assert identity == formula_identity(**_identity_kwargs())
    # There is no engine parameter to pass. Asserted on the signature, because absence is the claim.
    import inspect

    parameters = set(inspect.signature(formula_identity).parameters)
    assert not any("engine" in p or "capab" in p for p in parameters), sorted(parameters)


# ══ ADMISSION HAS ITS OWN IDENTITY ═════════════════════════════════════════════════════════════
def test_A_MOVED_CAPABILITY_SET_IS_A_NEW_ADMISSION_OF_THE_SAME_FORMULA(db):
    """The whole point of the second identity: the engine gains an operator, admission is re-decided,
    and no LLM is asked anything."""
    _request(db, "fd-1")
    advance(db, "fd-1", DraftStateV1.AUTHORING)
    advance(db, "fd-1", DraftStateV1.CRITIC_REVIEW)
    advance(db, "fd-1", DraftStateV1.VALIDATING)
    advance(db, "fd-1", DraftStateV1.ADMISSION,
            formula_content_hash="sha256:formula", formula_json={"body": {}})

    before = record_admission(
        db, formula_draft_id="fd-1", formula_content_hash="sha256:formula",
        engine_id="kedro-pyspark", advertised=["aggregate", "governed_scan"],
        admitted=False, blockers=[{"code": "RENDERER_CANNOT_DISPATCH", "detail": "no as_of_fx_join"}])

    after = record_admission(
        db, formula_draft_id="fd-1", formula_content_hash="sha256:formula",
        engine_id="kedro-pyspark",
        advertised=["aggregate", "governed_scan", "as_of_fx_join"], admitted=True)

    assert before != after, "a moved capability set must be a DIFFERENT admission identity"
    assert db.execute("SELECT count(*) FROM formula_draft_admission").fetchone()[0] == 2
    # And the formula itself was never re-authored — one draft, one authoring run.
    assert db.execute("SELECT count(*) FROM formula_draft").fetchone()[0] == 1


def test_THE_SAME_CAPABILITY_SET_IS_ONE_DECISION(db):
    """Deciding the same formula against the same set twice is one decision, not two."""
    _request(db, "fd-1")
    for state in (DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW, DraftStateV1.VALIDATING,
                  DraftStateV1.ADMISSION):
        advance(db, "fd-1", state)
    for _ in range(2):
        record_admission(db, formula_draft_id="fd-1", formula_content_hash="sha256:f",
                         engine_id="e", advertised=["aggregate"], admitted=True)
    assert db.execute("SELECT count(*) FROM formula_draft_admission").fetchone()[0] == 1


def test_a_PRIOR_DECISION_IS_FOUND_WHEN_NOTHING_MOVED(db):
    """What "re-run admission without LLM spend" reads first: if the capability set has not moved,
    the stored decision still holds and nothing needs re-deciding either."""
    _request(db, "fd-1")
    for state in (DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW, DraftStateV1.VALIDATING,
                  DraftStateV1.ADMISSION):
        advance(db, "fd-1", state)
    record_admission(db, formula_draft_id="fd-1", formula_content_hash="sha256:f",
                     engine_id="e", advertised=["aggregate"], admitted=True)

    assert existing_admission(db, formula_content_hash="sha256:f", engine_id="e",
                              advertised=["aggregate"]) == (True, ())
    # A different set has no decision yet — which is what triggers a re-decide, not a re-author.
    assert existing_admission(db, formula_content_hash="sha256:f", engine_id="e",
                              advertised=["aggregate", "quote_inversion"]) is None


def test_the_capability_hash_ignores_ORDER_and_duplicates():
    """The order a reader happened to receive the operators in is not part of the identity."""
    assert capability_set_hash(["b", "a"]) == capability_set_hash(["a", "b", "a"])


def test_the_admission_identity_includes_the_POLICY_VERSION():
    """A change to the CHECKS re-decides existing formulas without re-authoring them."""
    base = admission_identity(formula_content_hash="f", engine_id="e", capability_hash="c")
    bumped = admission_identity(formula_content_hash="f", engine_id="e", capability_hash="c",
                                admission_policy_version=2)
    assert base != bumped


# ══ THE STATE MACHINE ══════════════════════════════════════════════════════════════════════════
def test_THE_HAPPY_PATH_IS_THE_SPECIFIED_ONE(db):
    _request(db, "fd-1")
    path = [DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW, DraftStateV1.VALIDATING,
            DraftStateV1.ADMISSION]
    for state in path:
        assert advance(db, "fd-1", state) is state
    advance(db, "fd-1", DraftStateV1.READY,
            formula_content_hash="sha256:f", formula_json={"body": {}})
    assert read_draft(db, "fd-1").state is DraftStateV1.READY


def test_A_STAGE_CANNOT_BE_SKIPPED(db):
    """A worker that skipped a stage would produce a READY draft whose trace never records a critic,
    and nothing downstream could tell."""
    _request(db, "fd-1")
    with pytest.raises(InvalidTransition, match="cannot move REQUESTED → VALIDATING"):
        advance(db, "fd-1", DraftStateV1.VALIDATING)


def test_THERE_IS_NO_STEP_BACKWARDS(db):
    """Re-authoring is a NEW request against a new identity — never a rewind, which would leave the
    row claiming a stage it had already passed."""
    _request(db, "fd-1")
    advance(db, "fd-1", DraftStateV1.AUTHORING)
    advance(db, "fd-1", DraftStateV1.CRITIC_REVIEW)
    with pytest.raises(InvalidTransition, match="never a step backwards"):
        advance(db, "fd-1", DraftStateV1.AUTHORING)


@pytest.mark.parametrize("terminal", [DraftStateV1.FAILED, DraftStateV1.CANCELLED])
def test_FAILURE_AND_CANCELLATION_ARE_REACHABLE_FROM_ANYWHERE(db, terminal):
    """A provider can refuse at any point and a user may cancel at any point."""
    _request(db, "fd-1")
    advance(db, "fd-1", DraftStateV1.AUTHORING)
    kwargs = {"failure_reason": "provider refused"} if terminal is DraftStateV1.FAILED else {}
    assert advance(db, "fd-1", terminal, **kwargs) is terminal


def test_A_TERMINAL_DRAFT_DOES_NOT_MOVE_AGAIN(db):
    _request(db, "fd-1")
    advance(db, "fd-1", DraftStateV1.CANCELLED)
    with pytest.raises(InvalidTransition, match="it is terminal"):
        advance(db, "fd-1", DraftStateV1.AUTHORING)


# ══ BLOCKED IS A PRODUCT RESULT, FAILED IS NOT ═════════════════════════════════════════════════
def test_BLOCKED_CARRIES_NAMED_BLOCKERS_and_no_failure_reason(db):
    """A valid formula naming an operator the engine does not advertise. Recording that as FAILED
    would send an operator to investigate an outage."""
    _request(db, "fd-1")
    for state in (DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW, DraftStateV1.VALIDATING,
                  DraftStateV1.ADMISSION):
        advance(db, "fd-1", state)
    advance(db, "fd-1", DraftStateV1.BLOCKED,
            blockers=[{"code": "RENDERER_CANNOT_DISPATCH",
                       "detail": "the engine does not advertise as_of_fx_join"}])

    draft = read_draft(db, "fd-1")
    assert draft.state is DraftStateV1.BLOCKED
    assert draft.blockers[0]["code"] == "RENDERER_CANNOT_DISPATCH"
    assert draft.failure_reason is None
    assert draft.stage_label == "Blocked"


def test_THE_DATABASE_REFUSES_A_BLOCKED_DRAFT_WITH_NO_BLOCKERS(db):
    """A refusal nobody can act on."""
    _request(db, "fd-1")
    for state in (DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW, DraftStateV1.VALIDATING,
                  DraftStateV1.ADMISSION):
        advance(db, "fd-1", state)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE formula_draft SET state = 'BLOCKED' WHERE formula_draft_id = %s",
                   ("fd-1",))


def test_THE_DATABASE_REFUSES_A_READY_DRAFT_WITH_NO_FORMULA(db):
    """READY must carry the artifact it claims to have produced."""
    _request(db, "fd-1")
    for state in (DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW, DraftStateV1.VALIDATING,
                  DraftStateV1.ADMISSION):
        advance(db, "fd-1", state)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE formula_draft SET state = 'READY' WHERE formula_draft_id = %s",
                   ("fd-1",))


def test_a_failure_reason_belongs_only_to_FAILED(db):
    _request(db, "fd-1")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE formula_draft SET failure_reason = %s WHERE formula_draft_id = %s",
                   ("something", "fd-1"))


# ══ the identity is frozen; only progress moves ════════════════════════════════════════════════
def test_THE_CANDIDATE_AND_IDENTITY_CANNOT_BE_EDITED(db):
    """Editing them would repoint a draft at a different candidate after the fact — and its paid
    answer with it."""
    _request(db, "fd-1")
    with pytest.raises(psycopg.errors.RaiseException, match="identity is frozen"):
        db.execute("UPDATE formula_draft SET option_id = %s WHERE formula_draft_id = %s",
                   ("opt-b", "fd-1"))


def test_a_draft_cannot_be_DELETED(db):
    _request(db, "fd-1")
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("DELETE FROM formula_draft WHERE formula_draft_id = %s", ("fd-1",))


def test_an_admission_decision_cannot_be_EDITED(db):
    """A later capability set writes a NEW row — that is the reuse, and editing would destroy it."""
    _request(db, "fd-1")
    for state in (DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW, DraftStateV1.VALIDATING,
                  DraftStateV1.ADMISSION):
        advance(db, "fd-1", state)
    record_admission(db, formula_draft_id="fd-1", formula_content_hash="sha256:f",
                     engine_id="e", advertised=["aggregate"], admitted=True)
    with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
        db.execute("UPDATE formula_draft_admission SET admitted = false")


# ══ the stage words come from the SERVER ═══════════════════════════════════════════════════════
def test_EVERY_STATE_HAS_THE_WORDS_THE_CARD_SHOWS(db):
    """Owned server-side so the API and the screen cannot describe one state with two sentences.
    Total over the enum, so a state added later cannot ship without its wording."""
    _request(db, "fd-1")
    draft = read_draft(db, "fd-1")
    import dataclasses

    for state in DraftStateV1:
        assert dataclasses.replace(draft, state=state).stage_label


def test_the_in_flight_stages_read_as_the_spec_names_them(db):
    _request(db, "fd-1")
    import dataclasses

    draft = read_draft(db, "fd-1")
    labels = {state: dataclasses.replace(draft, state=state).stage_label
              for state in DraftStateV1}
    assert labels[DraftStateV1.AUTHORING] == "Authoring formula…"
    assert labels[DraftStateV1.CRITIC_REVIEW] == "Critic review…"
    assert labels[DraftStateV1.ADMISSION] == "Checking execution support…"
    assert labels[DraftStateV1.READY] == "Formula ready"


def test_reading_an_unknown_draft_is_None(db):
    assert read_draft(db, "fd-never") is None


# ══ DRAFTING IS NOT SELECTING ══════════════════════════════════════════════════════════════════
def test_THE_DRAFT_ROUTE_CANNOT_RECORD_A_GATE1_CHOICE():
    """The product rule: a user inspects a formula and THEN decides.

    `POST /contract/draft` records a Gate-1 choice as its first act — on that route, drafting IS
    selecting. This route must not be able to, and "we simply do not call it" is a habit whereas an
    absent import is a fact. Checked against the CODE, since the module's docstring explains the
    rule at length and a whole-file grep would read the explanation as the thing it disclaims.
    """
    import ast
    import inspect

    from featuregen.api.routes import formula_drafts

    tree = ast.parse(inspect.getsource(formula_drafts))
    imported = {alias.name for node in ast.walk(tree)
                if isinstance(node, ast.ImportFrom) for alias in node.names}
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}

    for selection_writer in ("select_and_record_gate1_choice", "_record_exact_choice",
                             "record_selection", "contractConfirm"):
        assert selection_writer not in imported, selection_writer
        assert selection_writer not in called, selection_writer


def test_THE_DRAFT_ROUTE_RUNS_NO_MODEL_AND_NO_CLUSTER():
    """The route records and enqueues. Two provider calls plus validation plus admission happen in
    the worker — a route that ran them would hold one database transaction across both models."""
    import ast
    import inspect

    from featuregen.api.routes import formula_drafts

    tree = ast.parse(inspect.getsource(formula_drafts))
    called = {node.func.id for node in ast.walk(tree)
              if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)}
    for forbidden in ("run_authoring_v2_replay", "run_authoring", "admit_artifacts_v2",
                      "author_formula", "compile_feature_group"):
        assert forbidden not in called, forbidden


def test_READY_CANNOT_CARRY_AN_EMPTY_FORMULA(db):
    """`{}` is not a formula, and NOT NULL alone does not say so.

    Found for real: an authoring run that parsed nothing produced an empty object, the row satisfied
    every constraint, and the draft went READY carrying it — which every downstream reader would
    have treated as a formula with no body. The worker now blocks that outcome; this is the floor
    underneath it, because a future caller with a different bug should hit a constraint rather than
    a silent empty artifact.
    """
    _request(db, "fd-1")
    for state in (DraftStateV1.AUTHORING, DraftStateV1.CRITIC_REVIEW, DraftStateV1.VALIDATING,
                  DraftStateV1.ADMISSION):
        advance(db, "fd-1", state)

    with pytest.raises(psycopg.errors.CheckViolation):
        advance(db, "fd-1", DraftStateV1.READY,
                formula_content_hash="sha256:nothing", formula_json={})
