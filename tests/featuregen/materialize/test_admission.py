"""Spec §1.2 — Gate 1, the governed admission boundary.

Every test here is an ATTACK or the proof that a legitimate artifact survives one. The threat model
is stated once, in ``admission.py``'s docstring: ``AuthoringResult`` is publicly constructible, so a
caller can fabricate a self-consistent "RESOLVED" result, attach any formula, and cite a legitimate
run id. Nothing but the immutable terminal trace event can tell the difference.

Runs are seeded through the REAL authoring path (``run_authoring`` over the real governed catalog,
with scripted ``FakeLLM`` providers) wherever that can produce the state under test — a hand-built
trace row would let a defect in the real writer hide behind a fixture that agrees with the reader.
Only two states cannot be reached that way, and both are forgeries by construction: a payload whose
``payload_hash`` does not match it (``append_event`` computes the hash, and 1022 forbids UPDATE), and
an in-memory result that disagrees with its run.

⚠️ **THE LANE MOVED (Phase G).** These runs are authored through
``formula.replay_authoring.run_authoring`` — migration **1022**'s
``formula_authoring_run`` / ``formula_authoring_trace_event`` — because that is the lane the LIVE
worker writes (``overlay/upload/recipe_formula_worker.py:35``) and therefore the only lane in which
a real governed feature exists. Admission used to prove against migration 1020's store, which
**nothing in ``src/`` has ever written**; see ``docs/DEFERRED-WORK.md`` A.33. Two consequences are
visible in this file and are deliberate: the terminal kinds are lower-case ``completed``/``failed``,
and check 6 now covers ``recipe_authoring_context`` (the 1022 manifest hashes five intent fields
where 1020 hashed four).
"""
from __future__ import annotations

import dataclasses
import inspect

import pytest
from tests.featuregen._helpers import make_actor
from tests.featuregen.materialize.fixtures import (
    authored_formula,
    intent_for,
    raw_proposal,
    rejected_raw_proposal,
    seed_materialize_catalog,
)

from featuregen.formula.author import AUTHOR_TASK
from featuregen.formula.critic import CRITIC_TASK
from featuregen.formula.replay_authoring import run_authoring
from featuregen.formula.replay_trace import append_event, open_authoring_run
from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.materialize.admission import (
    AdmittedFeature,
    FeatureNamePlanError,
    ResolvedFeatureInput,
    admit_artifacts,
)
from featuregen.materialize.authoring_trace import (
    authoring_intent_hash,
    payload_digest,
    read_terminal_event,
)
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

_ACTOR = make_actor(subject="user:materialize-author", roles=("feature_engineer",))
_FEATURE = "total_debit_amount_30d"


@pytest.fixture(autouse=True, scope="module")
def no_dsn():
    """DSN-HERMETIC (same rationale as ``formula/test_authoring.py``): write-once trace rows a
    durable connection commits can physically never be cleaned up between suite runs."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("FEATUREGEN_DSN", raising=False)
        yield


@pytest.fixture
def catalog(db):
    seed_materialize_catalog(db)
    return db


# ── seeding through the REAL authoring path ──────────────────────────────────────────────────────

def _author(raw: dict) -> FakeLLM:
    return FakeLLM(script={AUTHOR_TASK: FakeResponse(
        output={"turn_type": "final_proposal", "final_proposal": raw})})


def _critic(findings: list[dict] | None = None) -> FakeLLM:
    return FakeLLM(script={CRITIC_TASK: FakeResponse(output={"findings": list(findings or [])})})


@pytest.fixture
def resolved_run(catalog):
    """A REAL run that really resolved: real manifest, real terminal event, real formula."""
    return run_authoring(catalog, intent_for(_FEATURE), _author(raw_proposal(_FEATURE)),
                         _critic(), roles=(), actor=_ACTOR)


@pytest.fixture
def rejected_run(catalog):
    """A REAL run that was REJECTED — a proposal the shape gate refuses."""
    return run_authoring(catalog, intent_for(_FEATURE), _author(rejected_raw_proposal()),
                         _critic(), roles=(), actor=_ACTOR)


@pytest.fixture
def needs_review_run(catalog):
    """A REAL NEEDS_REVIEW run — and therefore one that still wrote a ``completed`` terminal event.

    THE check-3 trap in the 1022 lane. A blocking critic finding folds to NEEDS_REVIEW with the
    output still resolved, so the run closes through the orchestrator's normal terminal append,
    which writes ``completed``. A gate that admitted on "a completed event exists" would admit a
    feature whose human review is outstanding."""
    return run_authoring(
        catalog, intent_for(_FEATURE), _author(raw_proposal(_FEATURE)),
        _critic([{"code": "WINDOW_INTENT_MISMATCH", "operand": None,
                  "detail": "30d asked, 90d proposed"}]),
        roles=(), actor=_ACTOR)


@pytest.fixture
def run_without_terminal(catalog):
    """A run opened and started through the real trace API, then abandoned — the honest shape of a
    process that died mid-authoring. ``run_authoring`` always closes a run, so the only way to
    produce this state is to stop before the terminal, which is exactly what a crash does."""
    run_id = open_authoring_run(
        catalog, intent_hash=authoring_intent_hash(intent_for(_FEATURE)),
        versions={"disposition": 1}, actor=None)
    append_event(catalog, run_id, "author_turn", seq=0, idempotency_key=f"{run_id}:0",
                 payload={"index": 0, "kind": "final_proposal"}, stage="AUTHOR_TURN_0")
    return run_id


#: ``actor=None`` on every DIRECT ``open_authoring_run`` below, while the real runs pass ``_ACTOR``:
#: not an inconsistency. ``replay_trace.open_authoring_run`` ``json.dumps`` its ``actor`` argument,
#: so it takes the SERIALIZED form; ``replay_authoring.run_authoring`` takes the envelope and
#: converts it (``_actor_json``). Passing ``_ACTOR`` to the former raises.


def _write_terminal_directly(conn, run_id: str, payload: str, payload_hash: str | None) -> None:
    """Insert a terminal event whose ``payload_hash`` does not cover its payload.

    1022 forbids UPDATE and DELETE, so the ONLY way to produce a payload its hash does not cover is
    to write the row directly — which is exactly what an out-of-band tamper is."""
    conn.execute(
        "INSERT INTO formula_authoring_trace_event "
        "(authoring_run_id, seq, kind, idempotency_key, payload, payload_hash, stage) "
        "VALUES (%s, 0, 'completed', %s, %s::jsonb, %s, 'TERMINAL')",
        (run_id, f"{run_id}:terminal", payload, payload_hash))


@pytest.fixture
def result_for(resolved_run):
    """A result citing an arbitrary run id, carrying the REAL resolved run's artifact and axes.

    This is the forger's most credible object: everything in it is genuine except the run it points
    at. Given the resolved run's own id it is simply that run's result."""
    def _make(run):
        run_id = run if isinstance(run, str) else run.authoring_run_id
        return dataclasses.replace(resolved_run, authoring_run_id=run_id)
    return _make


@pytest.fixture
def forged_result(resolved_run):
    """A result citing a LEGITIMATE run but carrying a DIFFERENT formula.

    The attack §1.2 exists to defeat: run id real, disposition real, axes real, formula swapped."""
    def _make(run):
        run_id = run if isinstance(run, str) else run.authoring_run_id
        return dataclasses.replace(
            resolved_run, authoring_run_id=run_id,
            candidate_formula=authored_formula("distinct_merchant_count_90d"),
            # ...and the field a self-consistency check would have believed.
            candidate_formula_hash="f" * 64)
    return _make


def _input(result, *, name: str = _FEATURE, **intent_kwargs) -> ResolvedFeatureInput:
    return ResolvedFeatureInput(intent_for(name, **intent_kwargs), result)


# ── check 1: a terminal event must exist ─────────────────────────────────────────────────────────

def test_no_terminal_event_is_refused(catalog, run_without_terminal, result_for) -> None:
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(result_for(run_without_terminal))])
    assert e.value.code is CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE


def test_a_run_that_does_not_exist_is_refused_as_incomplete(catalog, result_for) -> None:
    """Absence is derived, so an invented run id and a crashed run are indistinguishable — and both
    mean the same thing: no authoring verdict exists to admit."""
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(result_for("far_invented"))])
    assert e.value.code is CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE


# ── check 2: the payload must validate against its hash ──────────────────────────────────────────

def test_tampered_terminal_payload_is_refused(catalog, result_for) -> None:
    """Migration 1022 forbids UPDATE/DELETE, so tampering means writing the row directly — which is
    the only way to produce a payload its ``payload_hash`` does not cover. Gate 1 recomputes."""
    run_id = open_authoring_run(
        catalog, intent_hash=authoring_intent_hash(intent_for(_FEATURE)),
        versions={"disposition": 1}, actor=None)
    _write_terminal_directly(
        catalog, run_id, '{"authoring_disposition": "RESOLVED"}', "0" * 64)

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(result_for(run_id))])
    assert e.value.code is CompilationRefusalCode.TERMINAL_PAYLOAD_TAMPERED


def test_the_tamper_check_runs_before_the_disposition_is_believed(catalog, result_for) -> None:
    """Order matters: a payload nobody has authenticated must not be allowed to answer check 3.
    Here the doctored payload claims RESOLVED — and is still refused as TAMPERED, not admitted."""
    run_id = open_authoring_run(
        catalog, intent_hash=authoring_intent_hash(intent_for(_FEATURE)),
        versions={"disposition": 1}, actor=None)
    _write_terminal_directly(
        catalog, run_id,
        '{"authoring_disposition": "RESOLVED", "candidate_formula_hash": "deadbeef"}', "0" * 64)

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(result_for(run_id))])
    assert e.value.code is CompilationRefusalCode.TERMINAL_PAYLOAD_TAMPERED


def test_a_null_payload_hash_is_refused_rather_than_skipped(catalog, result_for) -> None:
    """1022's ``payload_hash`` is NULLable (migration 1026 added it to an existing table), so a row
    can carry no tamper evidence at all. An unverifiable payload must FAIL CLOSED — treating a
    missing hash as "nothing to compare, therefore fine" would make check 2 opt-out."""
    run_id = open_authoring_run(
        catalog, intent_hash=authoring_intent_hash(intent_for(_FEATURE)),
        versions={"disposition": 1}, actor=None)
    _write_terminal_directly(catalog, run_id, '{"authoring_disposition": "RESOLVED"}', None)

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(result_for(run_id))])
    assert e.value.code is CompilationRefusalCode.TERMINAL_PAYLOAD_TAMPERED
    # The DETAIL is asserted because the code alone is not load-bearing here: a digest compared
    # against NULL also happens to differ, so this test would pass against an implementation with
    # no NULL guard at all — and would then be silently accepting the wrong reason.
    assert "no payload_hash" in e.value.detail


def test_a_payload_that_is_not_an_object_at_all_is_refused(catalog, result_for) -> None:
    """1022's ``payload`` has no ``jsonb_typeof = 'object'`` CHECK (1020's did), so a directly
    written row may hold an array. Its ``payload_hash`` here is GENUINE, so check 2 passes and
    check 3 is the one that must cope: an unreadable verdict is not a permissive one, and it must
    not surface as an ``AttributeError`` out of a governed gate either."""
    run_id = open_authoring_run(
        catalog, intent_hash=authoring_intent_hash(intent_for(_FEATURE)),
        versions={"disposition": 1}, actor=None)
    _write_terminal_directly(
        catalog, run_id, '["authoring_disposition", "RESOLVED"]',
        payload_digest(["authoring_disposition", "RESOLVED"]))

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(result_for(run_id))])
    assert e.value.code is CompilationRefusalCode.NOT_RESOLVED


def test_the_payload_digest_is_the_one_append_event_computed(catalog, resolved_run) -> None:
    """PINS the two sides of check 2 against each other against a REAL, unaltered run.

    1022 hashes with ``json.dumps(sort_keys=True, separators=(",", ":"), ensure_ascii=False)`` —
    NOT the RFC 8785 (JCS) bytes ``materialize.canonical.materialize_hash`` produces, and not the
    ASCII-escaping ``overlay.field_evidence.canonical_hash`` produces either. Admission therefore
    borrows the writer's own hasher rather than keeping a second copy; this asserts the borrow is
    still the right one, THROUGH the jsonb round-trip that reorders keys."""
    event = read_terminal_event(catalog, resolved_run.authoring_run_id)

    assert payload_digest(event.payload) == event.payload_hash


# ── check 3: THE TRAP — a non-RESOLVED run also writes `completed` ───────────────────────────────

def test_a_NEEDS_REVIEW_run_also_writes_completed_so_check_the_PAYLOAD(
        catalog, needs_review_run, result_for) -> None:
    """VERIFIED against the 1022 lane: the orchestrator's normal terminal append writes
    ``completed`` for every disposition that reaches the fold — RESOLVED, NEEDS_REVIEW and an
    ``invalid_output`` REJECTED alike (``replay_authoring.py:674-683``).

    A gate that admitted on "a completed event exists" would admit this feature while its human
    review is still outstanding."""
    assert needs_review_run.authoring_disposition == "NEEDS_REVIEW"

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(result_for(needs_review_run))])
    assert e.value.code is CompilationRefusalCode.NOT_RESOLVED


def test_the_needs_review_runs_terminal_event_really_is_completed(
        catalog, needs_review_run) -> None:
    """The premise of the test above, asserted rather than assumed — if this ever stops holding,
    the trap has closed and the check-3 test would be passing for the wrong reason."""
    event = read_terminal_event(catalog, needs_review_run.authoring_run_id)
    assert event is not None
    assert event.kind == "completed"
    assert event.payload["authoring_disposition"] == "NEEDS_REVIEW"


def test_a_rejected_run_is_refused(catalog, rejected_run, result_for) -> None:
    """RE-POINTED from the 1020 lane. There, a REJECTED run wrote ``COMPLETED`` and this was the
    trap test. In the 1022 lane a proposal the shape gate refuses is terminated by the parse arm,
    which writes ``failed`` (``replay_authoring.py:426-431``) — so the trap now lives on the
    NEEDS_REVIEW run above, and this test keeps proving the verdict itself is honoured."""
    assert rejected_run.authoring_disposition == "REJECTED"
    assert read_terminal_event(catalog, rejected_run.authoring_run_id).kind == "failed"

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(result_for(rejected_run))])
    assert e.value.code is CompilationRefusalCode.NOT_RESOLVED


def test_a_needs_review_run_is_refused(catalog, needs_review_run) -> None:
    """NEEDS_REVIEW is not a near-miss: a blocking critic finding means a human verdict is
    outstanding, and materializing would decide that review by default."""
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(needs_review_run)])
    assert e.value.code is CompilationRefusalCode.NOT_RESOLVED


# ── check 4: the recorded candidate hash ─────────────────────────────────────────────────────────

def test_forged_result_citing_a_real_run_is_refused(catalog, resolved_run, forged_result) -> None:
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(forged_result(resolved_run))])
    assert e.value.code is CompilationRefusalCode.FORMULA_HASH_MISMATCH


def test_the_supplied_candidate_formula_hash_field_is_never_trusted(
        catalog, resolved_run) -> None:
    """The forger's easiest move: swap the formula AND set the hash field to the recorded one, so
    the object is perfectly self-consistent. Gate 1 re-derives the digest from the formula."""
    recorded = resolved_run.candidate_formula_hash
    swapped = dataclasses.replace(
        resolved_run,
        candidate_formula=authored_formula("cross_border_value_ratio_90d"),
        candidate_formula_hash=recorded)

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(swapped)])
    assert e.value.code is CompilationRefusalCode.FORMULA_HASH_MISMATCH


def test_a_result_with_no_formula_at_all_is_refused(catalog, resolved_run) -> None:
    empty = dataclasses.replace(resolved_run, candidate_formula=None)

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(empty)])
    assert e.value.code is CompilationRefusalCode.FORMULA_HASH_MISMATCH


def test_an_uncanonicalizable_formula_is_a_governed_refusal_not_a_bare_exception(
        catalog, resolved_run) -> None:
    """§14: a governed refusal never surfaces as a bare ``ValueError``. A formula whose refs cannot
    be normalized has no content identity, so it cannot be the recorded one."""
    broken_grain = dataclasses.replace(resolved_run.candidate_formula.grain, keys=("not a ref",))
    broken = dataclasses.replace(
        resolved_run,
        candidate_formula=dataclasses.replace(resolved_run.candidate_formula, grain=broken_grain))

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(broken)])
    assert e.value.code is CompilationRefusalCode.FORMULA_HASH_MISMATCH


# ── check 5: the six axes ────────────────────────────────────────────────────────────────────────

def test_axes_disagreeing_with_the_terminal_event_are_refused(catalog, resolved_run) -> None:
    """The disposition still folds to RESOLVED, so only the axes give the rewrite away."""
    tweaked = dataclasses.replace(resolved_run, expectation_status="match")
    assert tweaked.authoring_disposition == "RESOLVED"

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(tweaked)])
    assert e.value.code is CompilationRefusalCode.AXES_MISMATCH


@pytest.mark.parametrize(
    ("axis", "value"),
    [("structural_status", "unsupported_operation"),
     ("capability_status", "unsupported_capability"),
     ("output_status", "needs_authority"),
     ("expectation_status", "mismatch"),
     ("critic_status", "advisory"),
     ("technical_status", "technical_failure")])
def test_every_axis_is_compared(catalog, resolved_run, axis, value) -> None:
    """All six, not just the convenient ones — an uncompared axis is a field a forger may rewrite."""
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(dataclasses.replace(resolved_run, **{axis: value}))])
    assert e.value.code is CompilationRefusalCode.AXES_MISMATCH


# ── check 6: the intent ──────────────────────────────────────────────────────────────────────────

def test_intent_hash_mismatch_is_refused(catalog, resolved_run) -> None:
    """A LOOK-ALIKE intent — same name, same entity, same grain keys, different hypothesis — is not
    the intent the run was opened for, and the manifest says so."""
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(resolved_run, hypothesis="a different hypothesis")])
    assert e.value.code is CompilationRefusalCode.INTENT_HASH_MISMATCH


def test_an_intent_for_a_different_feature_is_refused(catalog, resolved_run) -> None:
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [_input(resolved_run, name="distinct_merchant_count_90d")])
    assert e.value.code is CompilationRefusalCode.INTENT_HASH_MISMATCH


def test_check_6_now_covers_recipe_authoring_context(catalog, resolved_run) -> None:
    """INVERTED by Phase G, and the single reason the lane move STRENGTHENS this gate.

    Against migration 1020 this was a recorded LIMIT: ``authoring.authoring_intent_hash`` digests
    ``name`` / ``hypothesis`` / ``target_entity`` / ``target_grain_keys`` ONLY, so an intent
    carrying a ``recipe_authoring_context`` the run never had still admitted. The 1022 manifest is
    stamped with ``canonical_hash(replay_authoring._intent_material(intent))``, which digests all
    FIVE fields — so the context is now inside the proof and a fabricated one is refused.

    That field is the whole authoring prompt's catalog context (``author.py:98-99``): an intent
    claiming a context the author never saw is a different authoring request."""
    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [
            _input(resolved_run, recipe_authoring_context={"recipe_id": "never-authored"})])
    assert e.value.code is CompilationRefusalCode.INTENT_HASH_MISMATCH


def test_the_intent_hash_recipe_is_the_one_the_manifest_was_stamped_with(
        catalog, resolved_run) -> None:
    """PINS the two sides of check 6 against each other against a REAL run.

    ``authoring_trace.authoring_intent_hash`` re-derives; ``formula_authoring_run.intent_hash`` is
    what ``replay_authoring.run_authoring`` actually wrote before any provider call. A second copy
    of the recipe that drifted from the writer's would make check 6 unsatisfiable for every genuine
    feature — a gate that refuses everything is as broken as one that admits everything."""
    stored = catalog.execute(
        "SELECT intent_hash FROM formula_authoring_run WHERE authoring_run_id = %s",
        (resolved_run.authoring_run_id,)).fetchone()[0]

    assert authoring_intent_hash(intent_for(_FEATURE)) == stored


# ── admission ────────────────────────────────────────────────────────────────────────────────────

def test_a_genuine_artifact_is_admitted(catalog, resolved_run) -> None:
    admitted = admit_artifacts(catalog, [_input(resolved_run)])

    assert len(admitted) == 1
    assert isinstance(admitted[0], AdmittedFeature)
    assert admitted[0].formula == authored_formula(_FEATURE)
    assert admitted[0].formula_content_hash == resolved_run.candidate_formula_hash
    assert admitted[0].authoring_run_id == resolved_run.authoring_run_id


def test_feature_name_comes_from_the_intent(catalog, resolved_run) -> None:
    """``AuthoringResult`` carries no name (reference §6) — the intent is the only source."""
    admitted = admit_artifacts(catalog, [_input(resolved_run)])
    assert admitted[0].feature_name == "total_debit_amount_30d"


def test_all_three_worked_features_are_admitted_together(catalog) -> None:
    inputs = []
    for name in ("total_debit_amount_30d", "distinct_merchant_count_90d",
                 "cross_border_value_ratio_90d"):
        result = run_authoring(catalog, intent_for(name), _author(raw_proposal(name)),
                               _critic(), roles=(), actor=_ACTOR)
        inputs.append(ResolvedFeatureInput(intent_for(name), result))

    admitted = admit_artifacts(catalog, inputs)

    assert tuple(f.feature_name for f in admitted) == (
        "total_debit_amount_30d", "distinct_merchant_count_90d", "cross_border_value_ratio_90d")


def test_one_bad_input_refuses_the_whole_batch(catalog, resolved_run, forged_result) -> None:
    """No partial admission: admitting the survivors of a refused batch would compile a group whose
    membership nobody decided."""
    with pytest.raises(MaterializationRefused):
        admit_artifacts(catalog, [_input(resolved_run),
                                  _input(forged_result(resolved_run))])


def test_an_empty_batch_admits_nothing(catalog) -> None:
    assert admit_artifacts(catalog, []) == ()


# ── the feature name (spec §1.2, final paragraph) ────────────────────────────────────────────────

def test_the_name_is_normalized_to_a_hive_identifier(catalog) -> None:
    intent = intent_for("Total Debit-Amount 30D")
    result = run_authoring(catalog, intent, _author(raw_proposal(_FEATURE)), _critic(),
                           roles=(), actor=_ACTOR)

    admitted = admit_artifacts(catalog, [ResolvedFeatureInput(intent, result)])

    assert admitted[0].feature_name == "total_debit_amount_30d"


def test_a_post_normalization_collision_is_a_plan_error_not_a_silent_overwrite(catalog) -> None:
    """Spec §1.2. Two names that fold to one Hive column would have one feature overwrite the
    other; there is no §14 code for it because it is a defect in the batch, not a verdict on it."""
    inputs = []
    for name in ("total_debit_amount_30d", "Total.Debit.Amount.30d"):
        intent = intent_for(name)
        result = run_authoring(catalog, intent, _author(raw_proposal(_FEATURE)), _critic(),
                               roles=(), actor=_ACTOR)
        inputs.append(ResolvedFeatureInput(intent, result))

    with pytest.raises(FeatureNamePlanError):
        admit_artifacts(catalog, inputs)


def test_a_name_that_is_not_expressible_as_a_hive_identifier_is_a_plan_error(catalog) -> None:
    intent = intent_for("30_day_debits")   # a leading digit is not a Hive identifier
    result = run_authoring(catalog, intent, _author(raw_proposal(_FEATURE)), _critic(),
                           roles=(), actor=_ACTOR)

    with pytest.raises(FeatureNamePlanError):
        admit_artifacts(catalog, [ResolvedFeatureInput(intent, result)])


# ── the boundary itself ──────────────────────────────────────────────────────────────────────────

def test_no_function_accepts_a_bare_formula():
    """The gate cannot be bypassed by a future convenience helper.

    Every public entry point must take a ``ResolvedFeatureInput`` and a connection, because a
    function that accepts a formula directly is a route into compilation that never touched the
    trace. If this fails, change the signature — not the test."""
    import featuregen.materialize.admission as m

    for name, fn in inspect.getmembers(m, inspect.isfunction):
        if not name.startswith("_"):
            p = inspect.signature(fn).parameters
            assert "formula" not in p and "formulas" not in p


def test_admit_artifacts_requires_a_connection():
    """The proof is a DB read, so the gate cannot be invoked without the database that holds it."""
    import featuregen.materialize.admission as m

    assert "conn" in inspect.signature(m.admit_artifacts).parameters
