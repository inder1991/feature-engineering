"""Phase G T2 — the resolution seam: a durable identity becomes admission's input.

THE ONLY PROOF THAT MATTERS is the round trip. A resolution that does not ADMIT is worthless, so
the happy-path test does not assert the shape of ``resolve_feature_inputs``' own output — it feeds
that output to :func:`~featuregen.materialize.admission.admit_artifacts` and proves the governed
gate accepts it. Everything else here is a refusal.

WHAT IS REAL AND WHAT IS SCRIPTED. The authoring runs are written by the REAL orchestrator
(``formula.replay_authoring.run_authoring``) through the REAL write-once trace: the manifest, the
stage sequence, every payload and every ``payload_hash`` are the writer's own bytes, and the output
policy is resolved from the REAL seeded catalog. Only the two PROVIDER stages are scripted, and
they must be: ``author_formula``'s audited seam records ``llm_call_ref`` rows whose ``llm_dispatch``
reconciliation ``load_verified_checkpoint`` then demands, and those rows only exist when a durable
DSN is configured — which this suite deliberately does not have (write-once trace rows a durable
connection commits can never be cleaned up between suite runs). Scripting the two provider calls is
the same choice ``tests/featuregen/formula/test_replay_authoring.py`` makes, for the same reason.
"""
from __future__ import annotations

import dataclasses
import hashlib

import pytest
from tests.featuregen.materialize.fixtures import (
    REF_CIF,
    authored_formula,
    intent_for,
    raw_proposal,
    seed_materialize_catalog,
)

from featuregen.formula.critic import CriticReview
from featuregen.formula.replay_authoring import run_authoring
from featuregen.formula.turns import AuthorTurnRecord, TurnKind
from featuregen.materialize.admission import admit_artifacts
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.resolve import ResolvedFeature, resolve_feature_inputs
from featuregen.overlay.upload.recipe_formula_shadow import (
    build_capture_entries,
    content_hash,
    declare_expected_run,
    write_manifest,
    write_work_item,
)

_FEATURE = "total_debit_amount_30d"


@pytest.fixture(autouse=True, scope="module")
def no_dsn():
    """DSN-HERMETIC (same rationale as ``test_admission.py``): write-once trace rows a durable
    connection commits can physically never be cleaned up between suite runs."""
    with pytest.MonkeyPatch.context() as mp:
        mp.delenv("FEATUREGEN_DSN", raising=False)
        yield


@pytest.fixture
def catalog(db):
    seed_materialize_catalog(db)
    return db


# ── the durable identity: one recipe-formula work item ───────────────────────────────────────────

def _expectation(name: str) -> dict:
    """The recipe expectation the work item carries. Its ``grain_key_refs`` are what
    ``recipe_formula_worker`` turns into ``AuthoringIntent.target_grain_keys``, so they must be the
    fixture intent's, or the reconstruction is not the intent the run was opened for."""
    return {"final_operation": "identity", "grain_entity": "customer",
            "grain_key_refs": [REF_CIF], "recipe_id": name}


def _provider_input(intent) -> dict:
    """The stored provider input — the material ``recipe_formula_worker.py:343-349`` builds the
    intent from, and (as ``recipe_authoring_context``) the fifth field of the 1022 intent hash."""
    return {"hypothesis": intent.hypothesis, "target_entity": intent.target_entity,
            "formula_expectation": _expectation(intent.name)}


def _seed_work_item(db, name: str, suffix: str) -> str:
    """One ``recipe_formula_shadow_work_item`` and the FK chain it stands on, through the REAL
    writers — a hand-rolled INSERT would let a defect in the writer hide behind the fixture."""
    intent_id, run_id = f"intent-{suffix}", f"genrun-{suffix}"
    scope_id, revision_id = f"scope-{suffix}", f"revision-{suffix}"
    considered_hash = f"considered-{suffix}"
    db.execute("INSERT INTO contract_intent "
               "(intent_id,hypothesis,intake_mode,redacted_hypothesis) "
               "VALUES (%s,'h','hypothesis','h')", (intent_id,))
    db.execute("INSERT INTO feature_generation_run (generation_run_id,intent_id,actor) "
               "VALUES (%s,%s,'{}'::jsonb)", (run_id, intent_id))
    db.execute("INSERT INTO confirmed_generation_scope "
               "(scope_id,intent_id,generation_run_id,expansion,scope_mode,"
               "confirmation_source,confirmed_by) "
               "VALUES (%s,%s,%s,'strict','scoped','user','user:test')",
               (scope_id, intent_id, run_id))
    db.execute("INSERT INTO contract_considered_revision "
               "(considered_revision_id,intent_id,generation_run_id,considered_json,"
               "considered_content_hash,canonicalization_version) "
               "VALUES (%s,%s,%s,'{}'::jsonb,%s,'test-v1')",
               (revision_id, intent_id, run_id, considered_hash))
    manifest_id = declare_expected_run(
        db, generation_run_id=run_id, intent_id=intent_id, confirmed_scope_id=scope_id,
        considered_revision_id=revision_id, considered_content_hash=considered_hash,
        ranking_flag=True)
    ranked = (_Ranked(name),)
    entries = build_capture_entries(
        generation_run_id=run_id, ranking_version="rank-v1", ranked=ranked,
        candidate_keys_by_recipe_id={name: ("candidate-1",)})
    write_manifest(db, manifest_id=manifest_id, generation_run_id=run_id, intent_id=intent_id,
                   considered_revision_id=revision_id, considered_content_hash=considered_hash,
                   ranking_version="rank-v1", ranked=ranked, entries=entries, ranking_enabled=True)

    intent = intent_for(name)
    expectation, provider_input = _expectation(name), _provider_input(intent)
    work_item_id = f"work-{suffix}"
    write_work_item(
        db, work_item_id=work_item_id, idempotency_key=f"work-key-{suffix}",
        capture_entry_id=entries[0].capture_entry_id, generation_run_id=run_id,
        intent_id=intent_id, considered_revision_id=revision_id,
        considered_content_hash=considered_hash, metadata_snapshot_id=None,
        metadata_snapshot_content_hash=None, recipe_id=name,
        recipe_candidate_key="candidate-1", recipe_expectation=expectation,
        recipe_expectation_hash=content_hash(expectation), binding_envelope={"bindings": []},
        binding_envelope_hash=content_hash({"bindings": []}), provider_input=provider_input,
        provider_input_hash=content_hash(provider_input),
        frozen_configuration={"configuration_hash": f"cfg-{suffix}"},
        frozen_configuration_hash=f"cfg-{suffix}",
        request_identity={"subject": "user:test", "actor_kind": "human", "authenticated": True,
                          "auth_method": "password", "role_claims": ["analyst"]},
        request_read_scope_hash=f"scope-hash-{suffix}")
    return work_item_id


class _Ranked:
    """The duck-typed ranked recipe ``build_capture_entries`` reads."""

    def __init__(self, recipe_id: str) -> None:
        self.recipe_id = recipe_id
        self.canonical_rank = 1
        self.selected_for_initial_view = True
        self.rank_reasons = ("primary",)
        self.initial_view_reasons = ("selected",)


def _run_id_for(work_item_id: str) -> str:
    """The run id the worker DERIVES rather than stores (``recipe_formula_worker.py:338-339``)."""
    return "far_" + hashlib.sha256(work_item_id.encode()).hexdigest()[:24]


# ── authoring the run the work item names ────────────────────────────────────────────────────────

def _author_the_run(db, monkeypatch, work_item_id: str, name: str, intent=None, *,
                    findings=()) -> None:
    """Drive the REAL 1022 orchestrator for the run id the work item derives."""
    raw = raw_proposal(name)

    def _author(*_args, **kwargs):
        kwargs["on_turn"](AuthorTurnRecord(
            index=0, kind=TurnKind.FINAL_PROPOSAL, llm_call_ref=None, tool_name=None,
            tool_result=None, output={"turn_type": "final_proposal", "final_proposal": raw},
            provider_calls=1, usage={"input_tokens": 10, "output_tokens": 5},
            tool_context_hash="fixed-trail-hash"))
        return raw, []

    monkeypatch.setattr("featuregen.formula.replay_authoring.author_formula", _author)
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring.critique",
        lambda *a, **k: CriticReview(tuple(findings), "critic_hash", False, None, 1, {}))
    run_authoring(db, intent if intent is not None else _durable_intent(name), object(), object(),
                  actor=None, authoring_run_id=_run_id_for(work_item_id))


def _durable_intent(name: str):
    """The intent as the WORKER would have assembled it from the work item — including the
    ``recipe_authoring_context``, which the 1022 manifest hash covers."""
    intent = intent_for(name)
    return type(intent)(
        name=intent.name, hypothesis=intent.hypothesis, target_entity=intent.target_entity,
        target_grain_keys=intent.target_grain_keys,
        recipe_authoring_context=_provider_input(intent))


@pytest.fixture
def resolvable(catalog, monkeypatch):
    """One work item whose authoring run RESOLVED — the whole durable identity, end to end."""
    work_item_id = _seed_work_item(catalog, _FEATURE, "a")
    _author_the_run(catalog, monkeypatch, work_item_id, _FEATURE)
    return work_item_id


# ── THE decisive test ────────────────────────────────────────────────────────────────────────────

def test_a_resolved_work_item_becomes_an_input_admission_ACCEPTS(catalog, resolvable) -> None:
    """The round trip. Resolve from a durable id, then hand the result to the governed gate.

    Nothing here asserts the shape of the seam's own output before admission has spoken: the six
    §1.2 checks re-derive every claim from the write-once trace, so ``admit_artifacts`` returning at
    all is the proof that the reconstruction IS the run's intent and the run's artifact."""
    resolved = resolve_feature_inputs(catalog, work_item_ids=[resolvable])

    admitted = admit_artifacts(catalog, [item.input for item in resolved])

    assert len(admitted) == 1
    assert admitted[0].feature_name == _FEATURE
    assert admitted[0].formula == authored_formula(_FEATURE)
    assert admitted[0].authoring_run_id == _run_id_for(resolvable)


def test_the_seam_carries_the_provenance_that_proves_it(catalog, resolvable) -> None:
    """The resolution names the run it restored and the manifest hash it matched, so a later stage
    can re-derive the same proof instead of trusting that someone upstream performed it."""
    (resolved,) = resolve_feature_inputs(catalog, work_item_ids=[resolvable])

    assert isinstance(resolved, ResolvedFeature)
    assert resolved.work_item_id == resolvable
    assert resolved.authoring_run_id == _run_id_for(resolvable)
    stored = catalog.execute(
        "SELECT intent_hash FROM formula_authoring_run WHERE authoring_run_id = %s",
        (resolved.authoring_run_id,)).fetchone()[0]
    assert resolved.intent_hash == stored


def test_resolution_restores_the_authoring_runs_own_artifact(catalog, resolvable) -> None:
    """Not a look-alike formula: the restored result carries the run's own content hash, which is
    the value check 4 compares the supplied formula's digest against."""
    (resolved,) = resolve_feature_inputs(catalog, work_item_ids=[resolvable])

    result = resolved.input.result
    assert result.authoring_disposition == "RESOLVED"
    assert result.candidate_formula == authored_formula(_FEATURE)
    assert result.authoring_run_id == _run_id_for(resolvable)


# ── the invariant: a mismatched intent refuses AT ADMISSION, via check 6 ─────────────────────────

def test_a_look_alike_intent_is_refused_by_ADMISSIONS_check_6(catalog, resolvable) -> None:
    """THE invariant test, and NOTHING in ``resolve`` is stubbed to reach it.

    The seam resolves genuinely; then the intent alone is swapped for a look-alike and the pair is
    handed to ``admit_artifacts``. The result is still the run's own restored artifact, so checks
    1-5 all pass — the refusal can only be check 6 re-hashing the intent against the write-once
    manifest. That is exactly the "look-alike rebuilt from its parts" case ``admission.py:115-117``
    names, and it is the one thing the hash exists to catch.

    Asserted positively too: the SAME resolution admits when its intent is left alone, so the
    refusal below is the swap and not some ambient defect in the fixture."""
    (resolved,) = resolve_feature_inputs(catalog, work_item_ids=[resolvable])
    assert admit_artifacts(catalog, [resolved.input])

    swapped = dataclasses.replace(resolved.input, intent=_look_alike(resolved.input.intent))

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [swapped])
    assert e.value.code is CompilationRefusalCode.INTENT_HASH_MISMATCH


def test_the_fifth_intent_field_is_inside_the_proof(catalog, resolvable) -> None:
    """The lane move's dividend, asserted at the seam. An intent differing ONLY in
    ``recipe_authoring_context`` — every field 1020's four-field digest covered still identical —
    is refused by check 6. Against the old lane this admitted."""
    (resolved,) = resolve_feature_inputs(catalog, work_item_ids=[resolvable])
    intent = resolved.input.intent
    context_only = dataclasses.replace(
        resolved.input,
        intent=type(intent)(
            name=intent.name, hypothesis=intent.hypothesis, target_entity=intent.target_entity,
            target_grain_keys=intent.target_grain_keys,
            recipe_authoring_context={"recipe_id": "never-authored"}))

    with pytest.raises(MaterializationRefused) as e:
        admit_artifacts(catalog, [context_only])
    assert e.value.code is CompilationRefusalCode.INTENT_HASH_MISMATCH


def _look_alike(intent):
    """Same name, same entity, same grain keys — a different hypothesis. Everything a
    self-consistency check would compare still agrees."""
    return type(intent)(
        name=intent.name, hypothesis="a hypothesis nobody authored under",
        target_entity=intent.target_entity, target_grain_keys=intent.target_grain_keys,
        recipe_authoring_context=intent.recipe_authoring_context)


def test_the_seam_refuses_a_mismatched_manifest_before_it_gets_that_far(
        catalog, resolvable, monkeypatch) -> None:
    """The seam's own attribution. Admission would catch this anyway (the test above proves it), but
    a resolution that cannot name WHICH member failed is useless to an operator, so the seam checks
    the manifest itself and reports the member."""
    from featuregen.materialize import resolve as resolve_module

    genuine = resolve_module._read_intent
    monkeypatch.setattr(
        resolve_module, "_read_intent",
        lambda conn, work_item_id: _look_alike(genuine(conn, work_item_id)))

    with pytest.raises(MaterializationRefused) as e:
        resolve_feature_inputs(catalog, work_item_ids=[resolvable])
    assert e.value.code is CompilationRefusalCode.INTENT_HASH_MISMATCH
    assert resolvable in e.value.detail


# ── refusals ─────────────────────────────────────────────────────────────────────────────────────

def test_a_work_item_that_names_nothing_is_refused(catalog) -> None:
    with pytest.raises(MaterializationRefused) as e:
        resolve_feature_inputs(catalog, work_item_ids=["work-never-written"])
    assert e.value.code is CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE


def test_a_work_item_whose_run_never_closed_is_refused(catalog) -> None:
    """The work item is real; the authoring run it names never reached a terminal event. Absence is
    derived, so a crashed run and a run that never started read the same way."""
    work_item_id = _seed_work_item(catalog, _FEATURE, "unclosed")

    with pytest.raises(MaterializationRefused) as e:
        resolve_feature_inputs(catalog, work_item_ids=[work_item_id])
    assert e.value.code is CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE


def test_a_needs_review_run_cannot_be_resolved(catalog, monkeypatch) -> None:
    """A blocking critic finding folds to NEEDS_REVIEW. The run is complete and its terminal event
    is genuine — but there is no admissible artifact, and the seam must not hand one over."""
    from featuregen.formula.critic import CriticFinding, CriticFindingCode

    work_item_id = _seed_work_item(catalog, _FEATURE, "review")
    _author_the_run(catalog, monkeypatch, work_item_id, _FEATURE, findings=(
        CriticFinding(code=CriticFindingCode.WINDOW_INTENT_MISMATCH, severity="blocking",
                      operand=None, detail="30d asked, 90d proposed"),))

    with pytest.raises(MaterializationRefused) as e:
        resolve_feature_inputs(catalog, work_item_ids=[work_item_id])
    assert e.value.code is CompilationRefusalCode.NOT_RESOLVED


# ── group resolution: all-or-nothing, deterministic order ────────────────────────────────────────

def test_a_group_resolves_every_member_and_admits_as_one_batch(catalog, monkeypatch) -> None:
    names = ("total_debit_amount_30d", "distinct_merchant_count_90d",
             "cross_border_value_ratio_90d")
    ids = []
    for index, name in enumerate(names):
        work_item_id = _seed_work_item(catalog, name, f"g{index}")
        _author_the_run(catalog, monkeypatch, work_item_id, name)
        ids.append(work_item_id)

    resolved = resolve_feature_inputs(catalog, work_item_ids=ids)
    admitted = admit_artifacts(catalog, [item.input for item in resolved])

    assert tuple(f.feature_name for f in admitted) == names


def test_one_unresolvable_member_refuses_the_WHOLE_group(catalog, monkeypatch) -> None:
    """Mirrors ``admit_artifacts``' own all-or-nothing contract. A caller that compiled the
    survivors of a refused group would be compiling a membership nobody decided."""
    good = _seed_work_item(catalog, _FEATURE, "ok")
    _author_the_run(catalog, monkeypatch, good, _FEATURE)
    bad = _seed_work_item(catalog, "distinct_merchant_count_90d", "unauthored")

    with pytest.raises(MaterializationRefused) as e:
        resolve_feature_inputs(catalog, work_item_ids=[good, bad])
    assert e.value.code is CompilationRefusalCode.AUTHORING_RUN_INCOMPLETE
    assert bad in e.value.detail


def test_the_order_is_the_same_however_the_caller_assembled_the_group(
        catalog, monkeypatch) -> None:
    """Deterministic by CONTENT, not by call order: the group plan and the contract name the same
    columns, and two callers that listed the same members differently must reach the same plan."""
    # The suffixes are chosen so the CALLER's order is NOT the sorted order — otherwise this test
    # would pass against an implementation that simply preserved the caller's sequence.
    members = (("cross_border_value_ratio_90d", "d2"), ("total_debit_amount_30d", "d0"),
               ("distinct_merchant_count_90d", "d1"))
    ids = []
    for name, suffix in members:
        work_item_id = _seed_work_item(catalog, name, suffix)
        _author_the_run(catalog, monkeypatch, work_item_id, name)
        ids.append(work_item_id)
    assert ids != sorted(ids)

    forward = resolve_feature_inputs(catalog, work_item_ids=ids)
    reversed_ = resolve_feature_inputs(catalog, work_item_ids=list(reversed(ids)))

    assert [item.work_item_id for item in forward] == sorted(ids)
    assert [item.work_item_id for item in forward] == [item.work_item_id for item in reversed_]
    # ...and the ORDER IS the resolution's, not an accident of which run was authored first.
    assert [item.input.intent.name for item in forward] == [
        "total_debit_amount_30d", "distinct_merchant_count_90d", "cross_border_value_ratio_90d"]


def test_repeated_resolution_is_byte_identical(catalog, resolvable) -> None:
    assert resolve_feature_inputs(catalog, work_item_ids=[resolvable]) == \
        resolve_feature_inputs(catalog, work_item_ids=[resolvable])


# ── a malformed REQUEST is a caller defect, not a governed verdict ───────────────────────────────

def test_an_empty_group_is_a_caller_error(catalog) -> None:
    """``authorize_compilation`` raises on an empty group (``ir.py:565``), so a seam that returned
    ``()`` would only move the failure one stage later, with less context."""
    with pytest.raises(ValueError, match="at least one"):
        resolve_feature_inputs(catalog, work_item_ids=[])


def test_a_duplicated_member_is_a_caller_error(catalog, resolvable) -> None:
    """Not a governed refusal: §14's closed vocabulary has no member for "the caller assembled the
    batch wrongly", and silently de-duplicating would hide it."""
    with pytest.raises(ValueError, match="duplicate"):
        resolve_feature_inputs(catalog, work_item_ids=[resolvable, resolvable])
