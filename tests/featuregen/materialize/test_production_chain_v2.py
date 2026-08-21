"""§0.10 step 4 — the PRODUCTION chain, from a person's selection to a sealed artifact.

`test_end_to_end_v2.py` proves the chain from an admitted V3 proposal. It builds that proposal by
hand, and that is exactly the gap this file closes: everything upstream of admission — a drafted
formula, its write-once authoring trace, the restore, the anti-forgery proof — was never exercised
by anything V3.

**What runs here.** A build set naming a person's selection, whose candidate has a READY formula
draft, whose authoring run left a replayable trace, requested through `request_generation` and
driven by the real fenced worker:

    selection -> formula draft -> authoring trace
      -> restore_build_set_formulas    (rebuilt from the TRACE, cross-checked against the draft)
      -> admit_artifacts_v2            (the anti-forgery proof)
      -> compile_generation_v2         (output authority, physical resolution, both gates)
      -> generate_v2                   (gate, plan, render, seal)
      -> a durable artifact, and a SUCCEEDED request naming it

**The two provider stages are scripted**, for the reason `test_resolve.py` states and
`test_restore_formula_v3.py` repeats: `author_formula`'s audited seam records `llm_call_ref` rows
whose reconciliation `load_verified_checkpoint` then demands, and those rows exist only under a
durable DSN this suite deliberately lacks. Everything else is the real writer — the manifest, the
stage sequence, every payload and every payload hash — and the restore replays it without a
provider.

▲ **THIS FILE FOUND A BREAK IN THE CHAIN.** A V3 authoring run terminates `NEEDS_REVIEW` by design,
because output authority is the COMPILER's to resolve. Admission demanded `RESOLVED`, inherited from
V1 where authoring resolved it. So no V3 formula could ever be admitted, and nothing noticed because
nothing had ever put one through admission. See `_require_admissible_disposition`.
"""
from __future__ import annotations

import json

import pytest
from tests.featuregen.materialize.test_end_to_end_v2 import (  # noqa: F401 — fixtures
    _advertise_this_build,
    catalog,
    spine,
)
from tests.featuregen.materialize.test_ir import (
    DECLARATION,
    INVENTORY,
    TXN,
    TXN_AMT,
    TXN_CIF,
    TXN_DT,
)
from tests.featuregen.materialize.test_pilot_v2 import (
    _ROLES,
    CADENCE,
    ENV,
    GROUP,
    OPERAND_FACTS,
    POLICY,
    _raw_v3,
)

from featuregen.formula.critic import CriticReview
from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.recipe_authoring import recipe_tool_runner_v2
from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay
from featuregen.formula.turns import AuthorTurnRecord, TurnKind
from featuregen.materialize.contract import AvailabilityPromiseV1
from featuregen.materialize.generation_authorization import (
    GenerationAuthorizationV1,
    record_generation_authorization,
)
from featuregen.materialize.generation_lane import (
    GenerationJobV2,
    enqueue_generation,
    process_generation_once,
)
from featuregen.materialize.restore_formula_v3 import _intent_of
from featuregen.materialize.seal_v2 import load_sealed_artifact
from featuregen.overlay.upload.build_set_store import (
    GenerationStatusV1,
    read_request,
    record_build_set,
    request_generation,
)
from featuregen.overlay.upload.selection_revisions import TargetModeV1

FEATURE = "posted_amount_30d"


# ── the candidate a person chose ─────────────────────────────────────────────────────────────────
def _considered(conn) -> None:
    """The FROZEN candidate set the intent is rebuilt FROM.

    Spelled against the materialize catalog (`hdfc`), not the authoring fixture's (`authored`),
    because `compile_ir_v2` resolves these refs PHYSICALLY — a candidate carrying the other
    catalog's spelling refuses for a reason that has nothing to do with the chain under test.

    It must resolve to exactly the intent the run is authored under. That is the guard, not a
    fixture convenience: the restorer DERIVES the intent hash rather than reading it back, so a
    revision describing a different candidate makes the checkpoint refuse.
    """
    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.contract.gate1 import _candidate_identity, _idea_json
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    idea = FeatureIdea(
        name=FEATURE, description="recent debit volume",
        derives_from=["public.txns.txn_amt"],
        derives_pairs=(("hdfc", "public.txns.txn_amt"),),
        aggregation="sum", grain_table="customer",
        operation_kind="sum",
        measure_refs=(("hdfc", "public.txns.txn_amt"),),
        grain_refs=(("hdfc", "public.txns.cif_id"),),
        time_ref=("hdfc", "public.txns.txn_dt"), window="30d")
    identity = _candidate_identity(path="anchor", source="anchor", lens="anchor", feature=idea)
    considered = {
        "version": "contract-considered-v3",
        "public": {"anchor": {**_idea_json(idea), "option_id": "opt-a"},
                   "alternatives": [], "rejections": []},
        "options_by_id": {"opt-a": {
            "source": "anchor", "lens": "anchor",
            "canonical_candidate_identity": identity,
            "canonical_candidate_identity_hash": canonical_hash(identity),
            "recipe_candidate_key": None}},
        "recipe_grounding_context_by_candidate_key": {},
        "recipe_candidate_keys_by_recipe_id": {}}
    conn.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
                 "VALUES ('int-pc',%s,'hypothesis') ON CONFLICT DO NOTHING",
                 ("recent debit volume",))
    conn.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, metadata_snapshot_id, considered_json, considered_content_hash, "
        "canonicalization_version) VALUES ('crev-pc','int-pc','run-1','snap-1',%s::jsonb,'h',"
        "'contract-considered-v3') ON CONFLICT DO NOTHING", (json.dumps(considered),))


def _author_run(conn, monkeypatch, *, run_id: str, raw: dict, findings=()) -> str:
    """Drive the REAL v2 orchestrator with the two PROVIDER stages scripted. Returns the run id."""
    def _author(*_args, **kwargs):
        kwargs["on_turn"](AuthorTurnRecord(
            index=0, kind=TurnKind.FINAL_PROPOSAL, llm_call_ref=None, tool_name=None,
            tool_result=None, output={"turn_type": "final_proposal", "final_proposal": raw},
            provider_calls=1, usage={"input_tokens": 10, "output_tokens": 5},
            tool_context_hash="fixed-trail-hash"))
        return raw, []

    monkeypatch.setattr("featuregen.formula.replay_authoring_v2.author_formula", _author)
    monkeypatch.setattr(
        "featuregen.formula.replay_authoring_v2.critique",
        lambda *a, **k: CriticReview(tuple(findings), "critic_hash", False, None, 1, {}))

    run_authoring_v2_replay(
        conn, _intent_of(conn, "crev-pc", "opt-a", "production-chain"), object(), object(),
        actor=None, authoring_run_id=run_id,
        facts_reader=lambda _p: ({TXN_AMT: OperandFactsV2(
            logical_type="decimal", unit="monetary", currency="fixed:AED")}, ()),
        critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
        tool_runner=recipe_tool_runner_v2(frozenset({TXN, TXN_AMT, TXN_DT, TXN_CIF})))
    return run_id


def _ready_draft(conn, monkeypatch, *, raw=None, findings=(), draft_id="fd-pc") -> str:
    """A READY draft whose run left a replayable trace, and whose stored hash is the trace's."""
    _considered(conn)
    run_id = _author_run(conn, monkeypatch, run_id=f"far-{draft_id}",
                         raw=raw if raw is not None else _raw_v3(), findings=findings)
    proposal_hash = conn.execute(
        "SELECT payload->'result'->>'candidate_proposal_hash' FROM formula_authoring_trace_event "
        "WHERE authoring_run_id=%s AND kind='completed'", (run_id,)).fetchone()[0]
    conn.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, definition_revision, "
        "formula_identity_hash, state, authoring_run_id, formula_content_hash, formula_json, "
        "requested_by, requested_at) VALUES (%s,'crev-pc','opt-a','h1','h2','h3','',%s,'READY',%s,"
        "%s,'{\"body\":{}}'::jsonb,'user:ops','t')",
        (draft_id, f"ident-{draft_id}", run_id, proposal_hash))
    return draft_id


def _requested(conn, *, request_id="req-pc") -> str:
    """A selection, a build set, an approval and a REQUESTED generation — what a person's click
    leaves behind."""
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES ('trr-pc','int-pc','exploration','h') ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, binding_plan_hash, "
        "content_hash) VALUES ('sel-pc','trr-pc','crev-pc','opt-a','dec-pc','h1','h2','ch-pc') "
        "ON CONFLICT DO NOTHING")
    build_set, _ = record_build_set(
        conn, revision_id="bs-pc", target_reading_revision_id="trr-pc",
        selection_revision_ids=["sel-pc"], declaration={"grain": "customer"},
        declared_by="user:ops", declared_at="t")
    approval = record_generation_authorization(
        conn, GenerationAuthorizationV1(
            environment_id=ENV, logical_group_name=GROUP, build_set_revision_id=build_set,
            target_mode=TargetModeV1.EXPLORATION, target_ref=None),
        authorized_by="user:ops", authorized_at="t")
    request_id, created = request_generation(
        conn, request_id=request_id, build_set_revision_id=build_set, environment_id=ENV,
        requested_by="user:ops", requested_at="t",
        generation_authorization_revision_id=approval)
    assert created
    enqueue_generation(
        conn, environment_id=ENV, logical_group_name=GROUP,
        job=GenerationJobV2(
            request_id=request_id, spine_declaration=DECLARATION, cadence=CADENCE,
            availability_promise=AvailabilityPromiseV1(calendar_days=1),
            physical_type_policy=POLICY, empty_values={FEATURE: "0"},
            operand_facts=OPERAND_FACTS, engine_id="kedro-pyspark",
            roles=tuple(_ROLES), compiled_at="t", sealed_at="2026-08-21T00:00:00Z"))
    return request_id


@pytest.fixture
def built(catalog, spine, monkeypatch):
    """THE RUN: a drafted V3 formula driven to a sealed artifact by the real worker."""
    _ready_draft(catalog, monkeypatch)
    request_id = _requested(catalog)
    _advertise_this_build(catalog)
    return request_id, process_generation_once(catalog, owner="w1", inventory=INVENTORY)


# ══ THE CHAIN CLOSES, FROM A SELECTION ═════════════════════════════════════════════════════════
def test_A_DRAFTED_FORMULA_REACHES_A_SEALED_ARTIFACT(built, catalog):
    """The claim the whole program has been working toward, on the PRODUCTION path: nothing here
    constructs an admitted feature — it is restored from the trace and admitted by the real checks.
    """
    request_id, outcome = built

    assert outcome.status == "generated", outcome.detail
    artifact = load_sealed_artifact(catalog, outcome.artifact_id)
    assert artifact is not None
    assert artifact.servable is True
    assert artifact.logical_group_name == GROUP


def test_THE_REQUEST_SUCCEEDS_AND_NAMES_ITS_ARTIFACT(built, catalog):
    """A worker that sealed an artifact and left the request mid-lifecycle would have produced
    something nobody could find."""
    request_id, outcome = built

    request = read_request(catalog, request_id)
    assert request.status is GenerationStatusV1.SUCCEEDED
    assert request.sealed_artifact_id == outcome.artifact_id
    assert request.refusals == ()


def test_THE_QUEUE_ROW_IS_DONE_not_left_leased(built, catalog):
    """A completed generation whose message stayed leased would block the next build of this group
    until the lease expired."""
    request_id, _ = built

    assert catalog.execute(
        "SELECT status FROM queue WHERE message_id = %s",
        (f"generation:{request_id}",)).fetchone()[0] == "done"


def test_THE_STORED_CODE_IS_REAL_SPARK_for_this_group(built, catalog):
    """What was sealed is generated source naming this group — not an empty project."""
    _request_id, outcome = built

    stored = catalog.execute(
        "SELECT b.content FROM generated_artifact_file f "
        "JOIN generated_artifact_blob b ON b.content_reference = f.content_reference "
        "WHERE f.artifact_id = %s", (outcome.artifact_id,)).fetchall()

    assert stored
    texts = [row[0] for row in stored]
    assert any("def " in text for text in texts)
    assert any(GROUP in text for text in texts)


# ══ THE BREAK THIS FILE FOUND ══════════════════════════════════════════════════════════════════
def test_a_V3_RUN_IS_ADMISSIBLE_although_its_disposition_is_NEEDS_REVIEW(catalog, monkeypatch):
    """The chain break, pinned. A V3 run ends `NEEDS_REVIEW` BY DESIGN — output authority is the
    compiler's to resolve, and claiming otherwise at authoring time would record a stage that never
    ran as having run and agreed. Admission demanded `RESOLVED`, V1's rule, under which no V3
    formula could ever be admitted."""
    from featuregen.materialize.restore_formula_v3 import restore_formula

    _ready_draft(catalog, monkeypatch)
    _requested(catalog)

    disposition = catalog.execute(
        "SELECT payload->'result'->>'authoring_disposition' FROM formula_authoring_trace_event "
        "WHERE authoring_run_id='far-fd-pc' AND kind='completed'").fetchone()[0]
    assert disposition == "NEEDS_REVIEW", "a V3 run that resolved its own output is a changed design"

    restored = restore_formula(catalog, selection_revision_id="sel-pc")
    from featuregen.materialize.admission_v2 import admit_artifacts_v2
    _advertise_this_build(catalog)

    admitted = admit_artifacts_v2(catalog, [restored.input], engine_id="kedro-pyspark")

    assert [feature.feature_name for feature in admitted] == [FEATURE]


def test_a_BLOCKING_CRITIC_is_STILL_refused(catalog, monkeypatch):
    """The other half, and the one that makes the change narrow rather than a loosening. The output
    axis is the ONLY one a V3 run defers; a run that also has a blocking critic is `NEEDS_REVIEW`
    for a second reason nothing downstream resolves, and admitting it would generate code from a
    formula a reviewer stopped."""
    from featuregen.formula.critic import CriticFinding, CriticFindingCode
    from featuregen.materialize.admission_v2 import admit_artifacts_v2
    from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
    from featuregen.materialize.restore_formula_v3 import restore_formula

    # Severity is a FIXED property of the code, never the caller's to assert — `_SEVERITY` maps
    # this one to "blocking", which is what makes the run's critic axis blocking.
    blocking = CriticFinding(
        code=CriticFindingCode.WINDOW_INTENT_MISMATCH, severity="blocking",
        operand=TXN_AMT, detail="the window is not the one the candidate asked for")
    _ready_draft(catalog, monkeypatch, findings=(blocking,))
    _requested(catalog)
    _advertise_this_build(catalog)

    restored = restore_formula(catalog, selection_revision_id="sel-pc")
    with pytest.raises(MaterializationRefused) as refusal:
        admit_artifacts_v2(catalog, [restored.input], engine_id="kedro-pyspark")

    assert refusal.value.code is CompilationRefusalCode.NOT_RESOLVED


# ══ ANTI-FORGERY, ON THE PRODUCTION PATH ═══════════════════════════════════════════════════════
def test_A_DRAFT_DISAGREEING_WITH_ITS_TRACE_STOPS_THE_BUILD(catalog, monkeypatch):
    """The draft's stored hash is what a person READ; the trace is what the run DECIDED. Building
    while they disagree would generate code for a formula nobody reviewed — so the lane refuses,
    by name, and seals nothing."""
    _ready_draft(catalog, monkeypatch)
    catalog.execute(
        "UPDATE formula_draft SET formula_content_hash = 'sha256:something-else' "
        "WHERE formula_draft_id = 'fd-pc'")
    request_id = _requested(catalog)
    _advertise_this_build(catalog)

    outcome = process_generation_once(catalog, owner="w1", inventory=INVENTORY)

    assert outcome.status == "refused"
    assert "INTENT_HASH_MISMATCH" in outcome.detail
    assert read_request(catalog, request_id).status is GenerationStatusV1.REFUSED
    assert catalog.execute("SELECT count(*) FROM sealed_artifact_v2").fetchone()[0] == 0


def test_a_DRAFT_THAT_NEVER_REACHED_READY_stops_the_build(catalog, monkeypatch):
    """Only a finished formula can be compiled, and a draft that stopped short has already recorded
    why — so the refusal does not re-explain it."""
    _ready_draft(catalog, monkeypatch)
    catalog.execute("UPDATE formula_draft SET state = 'FAILED', failure_reason = 'x' "
                    "WHERE formula_draft_id = 'fd-pc'")
    _requested(catalog)
    _advertise_this_build(catalog)

    outcome = process_generation_once(catalog, owner="w1", inventory=INVENTORY)

    assert outcome.status == "refused"
    assert "READY" in outcome.detail


# ══ MUTATION ═══════════════════════════════════════════════════════════════════════════════════
def test_A_DIFFERENT_FORMULA_SEALS_A_DIFFERENT_ARTIFACT(catalog, spine, monkeypatch):
    """Change what the feature COMPUTES and the sealed bytes change with it. An identity that
    survived a changed aggregation would let a verified artifact stand in for one nobody verified.
    """
    _ready_draft(catalog, monkeypatch)
    _requested(catalog)
    _advertise_this_build(catalog)
    first = process_generation_once(catalog, owner="w1", inventory=INVENTORY)
    assert first.status == "generated", first.detail
    original = load_sealed_artifact(catalog, first.artifact_id)

    # A SECOND build of the same group, from a formula that aggregates differently. Nothing is
    # deleted — `formula_draft` is append-only and `generation_request` records every attempt — so
    # the second draft simply supersedes the first, which is how the restorer already reads them
    # (`ORDER BY d.updated_at DESC`), and the second request is a NEW attempt at the same set,
    # permitted because the first is terminal.
    _ready_draft(catalog, monkeypatch, draft_id="fd-pc2",
                 raw=_raw_v3(aggregation="count_rows", operand=None))
    catalog.execute("UPDATE formula_draft SET updated_at = now() + interval '1 second' "
                    "WHERE formula_draft_id = 'fd-pc2'")
    second_id = _requested(catalog, request_id="req-pc2")
    second = process_generation_once(catalog, owner="w2", inventory=INVENTORY)

    assert second.status == "generated", second.detail
    changed = load_sealed_artifact(catalog, second.artifact_id)
    assert changed.artifact_id != original.artifact_id
    assert changed.compilation_identity_hash != original.compilation_identity_hash
    assert changed.project_digest != original.project_digest
    assert second_id
