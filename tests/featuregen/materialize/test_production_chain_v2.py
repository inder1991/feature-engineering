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


def _raw_v2(**kwargs) -> dict:
    """The same feature in the V2 WIRE SHAPE, spelled against this catalog.

    V2 is not a lesser V3: it resolves output authority DURING authoring, which is the whole reason
    the two languages take different admission paths. `row_selections` is v3's addition, so dropping
    it and declaring version 2 is the honest difference rather than a trimmed copy.
    """
    raw = _raw_v3(**kwargs)
    raw["formula_schema_version"] = 2
    raw["body"]["expr"].pop("row_selections", None)
    return raw


def _author_run(conn, monkeypatch, *, run_id: str, raw: dict, findings=(), facts=None) -> str:
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
        facts_reader=facts if facts is not None else (lambda _p: ({TXN_AMT: OperandFactsV2(
            logical_type="decimal", unit="monetary", currency="fixed:AED")}, ())),
        critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
        tool_runner=recipe_tool_runner_v2(frozenset({TXN, TXN_AMT, TXN_DT, TXN_CIF})),
        # WHAT PRODUCTION PASSES (`formula_draft_worker._DRAFT_FORMULA_SCHEMA_VERSION = 3`). It is
        # not a detail: this is the version the run's MANIFEST declares, and a fixture that left it
        # at the parameter's default would author V3 proposals under a manifest saying V2 — the
        # exact disagreement `_verify_manifest_declares_the_language` now refuses.
        formula_schema_version=3 if raw.get("formula_schema_version") == 3 else 2)
    return run_id


def _ready_draft(conn, monkeypatch, *, raw=None, findings=(), facts=None,
                 draft_id="fd-pc") -> str:
    """A READY draft whose run left a replayable trace, and whose stored hash is the trace's."""
    _considered(conn)
    run_id = _author_run(conn, monkeypatch, run_id=f"far-{draft_id}",
                         raw=raw if raw is not None else _raw_v3(), findings=findings,
                         facts=facts)
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


# ══ THE GATE THIS FILE FOUND, AND ITS EDGES ═══════════════════════════════════════════════════
def _admit(catalog, selection="sel-pc"):
    from featuregen.materialize.admission_v2 import admit_artifacts_v2
    from featuregen.materialize.restore_formula_v3 import restore_formula

    _advertise_this_build(catalog)
    restored = restore_formula(catalog, selection_revision_id=selection)
    return admit_artifacts_v2(catalog, [restored.input], engine_id="kedro-pyspark")


def _refused(catalog, selection="sel-pc"):
    from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

    with pytest.raises(MaterializationRefused) as refusal:
        _admit(catalog, selection)
    assert refusal.value.code is CompilationRefusalCode.NOT_RESOLVED, refusal.value
    return refusal.value


def _disposition(catalog, run_id="far-fd-pc") -> str:
    return catalog.execute(
        "SELECT payload->'result'->>'authoring_disposition' FROM formula_authoring_trace_event "
        "WHERE authoring_run_id=%s AND kind='completed'", (run_id,)).fetchone()[0]


def test_a_V3_RUN_TERMINATES_READY_FOR_OUTPUT_BINDING_not_NEEDS_REVIEW(catalog, monkeypatch):
    """The state model, pinned. A V3 run that did everything IT owns is not "awaiting a human" —
    it is awaiting the COMPILER, and a queue or a screen showing "Needs review" while a worker
    legitimately proceeds without review is a status that lies to whoever reads it."""
    _ready_draft(catalog, monkeypatch)
    _requested(catalog)

    assert _disposition(catalog) == "READY_FOR_OUTPUT_BINDING"
    assert [f.feature_name for f in _admit(catalog)] == [FEATURE]


def test_a_V2_RUN_THAT_RESOLVED_ITS_OUTPUT_IS_ADMITTED(catalog, monkeypatch):
    """The unchanged half. V2 resolves output authority DURING authoring, so RESOLVED there means
    every question was answered — and it is admitted on exactly the rule it always was."""
    _ready_draft(catalog, monkeypatch, raw=_raw_v2())
    _requested(catalog)

    assert _disposition(catalog) == "RESOLVED"
    assert [f.feature_name for f in _admit(catalog)] == [FEATURE]


def test_a_V2_RUN_WHOSE_AUTHORITY_LOOKUP_FAILED_IS_STILL_REFUSED(catalog, monkeypatch):
    """▲ THE HOLE THE FIRST FIX OPENED, pinned shut.

    A V2 run reaches an unresolved output too: `replay_authoring_v2` folds `needs_authority` when a
    governed-facts read fails CLOSED. That is a real failure a human must look at — NOT a deferral.
    The first version of the disposition exception took only the terminal event, could not ask what
    language it held, and admitted exactly this. The two are now different values on the axis and
    different dispositions, and the check requires the authenticated proposal to be V3.
    """
    from featuregen.formula.authoring_result_leaves import AuthorityFailure

    _ready_draft(catalog, monkeypatch, raw=_raw_v2(),
                 facts=lambda _p: ({}, (AuthorityFailure(
                     reason="projection_unavailable", operand=TXN_AMT, field="unit"),)))
    _requested(catalog)

    assert _disposition(catalog) == "NEEDS_REVIEW"
    _refused(catalog)


def test_a_V3_RUN_WITH_A_BLOCKING_CRITIC_IS_REFUSED(catalog, monkeypatch):
    """The deferral is NARROW. A run whose critic blocked is outstanding for a second reason that
    nothing downstream resolves, and admitting it would generate code from a formula a reviewer
    stopped."""
    from featuregen.formula.critic import CriticFinding, CriticFindingCode

    # Severity is a FIXED property of the code, never the caller's to assert.
    blocking = CriticFinding(
        code=CriticFindingCode.WINDOW_INTENT_MISMATCH, severity="blocking",
        operand=TXN_AMT, detail="the window is not the one the candidate asked for")
    _ready_draft(catalog, monkeypatch, findings=(blocking,))
    _requested(catalog)

    assert _disposition(catalog) == "NEEDS_REVIEW", "a blocking critic must outrank the deferral"
    _refused(catalog)


def test_a_V3_RUN_WITH_AN_ADVISORY_CRITIC_IS_ADMITTED(catalog, monkeypatch):
    """The other side of the same line: advisory findings are recorded, not blocking, so they do
    not make a run outstanding. Without this the previous test would pass for a version that
    refused every critic finding."""
    from featuregen.formula.critic import CriticFinding, CriticFindingCode

    advisory = CriticFinding(
        code=CriticFindingCode.WEAK_PROXY, severity="advisory",
        operand=TXN_AMT, detail="a weaker proxy than the candidate implies")
    _ready_draft(catalog, monkeypatch, findings=(advisory,))
    _requested(catalog)

    assert _disposition(catalog) == "READY_FOR_OUTPUT_BINDING"
    assert [f.feature_name for f in _admit(catalog)] == [FEATURE]


# ── the axis edges, asked of the GATE directly ───────────────────────────────────────────────────
# Not by rewriting a stored trace event: 1022 makes those payloads physically immutable and check 2
# verifies the digest, so a tampered one refuses as a HASH MISMATCH long before the disposition is
# read — which would prove the tamper-evidence, not this gate. These build the event the gate
# receives, with a REAL parsed proposal and a REAL result, and ask it the question directly.
class _Event:
    """The two fields `_require_admissible_disposition` reads off a terminal event."""

    kind = "completed"

    def __init__(self, **payload):
        self.payload = payload


def _terminal(**overrides):
    """A terminal payload for a clean V3 deferral, with any axis overridable."""
    payload = dict(
        authoring_disposition="READY_FOR_OUTPUT_BINDING",
        structural_status="ok", capability_status="ok",
        output_status="deferred_to_compiler", expectation_status="not_provided",
        critic_status="clean", review=None, technical_status="ok")
    payload.update(overrides)
    return _Event(**payload)


#: "not supplied", distinct from an explicit `None` — which is itself a case under test.
_MISSING = object()


def _gate(event, *, proposal=None, output_intent=_MISSING, candidate_output=None):
    """Call the gate with a real proposal and a result carrying the two fields it inspects."""
    from featuregen.formula.parse_v3 import parse_proposal_v3
    from featuregen.formula.result_v2 import _content_hash
    from featuregen.materialize.admission_v2 import _require_admissible_disposition

    parsed = proposal if proposal is not None else parse_proposal_v3(_raw_v3())

    class _Result:
        pass

    result = _Result()
    result.output_intent = (_intent_for(parsed, _content_hash(parsed))
                            if output_intent is _MISSING else output_intent)
    result.candidate_output = candidate_output
    _require_admissible_disposition(event, result, parsed, "far-gate")


def _intent_for(proposal, proposal_hash):
    from featuregen.formula.output_intent_v2 import derive_output_intent_v2

    return derive_output_intent_v2(proposal, proposal_hash=proposal_hash)


def _gate_refuses(event, **kwargs):
    from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused

    with pytest.raises(MaterializationRefused) as refusal:
        _gate(event, **kwargs)
    assert refusal.value.code is CompilationRefusalCode.NOT_RESOLVED
    return refusal.value


def test_the_CLEAN_V3_DEFERRAL_is_admitted_by_the_gate():
    """The positive control. Without it every refusal below could be passing for the wrong
    reason — a gate that refused everything would satisfy all of them."""
    _gate(_terminal())


@pytest.mark.parametrize("status", ["needs_authority", "external_requirement", "invalid_output",
                                    "resolved"])
def test_ONLY_deferred_to_compiler_IS_DEFERRED(status):
    """The output axis must be EXACTLY the deferral. `needs_authority` means the governed read
    failed, `external_requirement` means something outside this run must happen first, and
    `invalid_output` means the declared output is wrong — none becomes true later on its own, so
    none may be waved through as "the compiler will handle it". `resolved` is here because a run
    that resolved its output has no business claiming the deferral either."""
    _gate_refuses(_terminal(output_status=status))


def test_a_V2_PROPOSAL_CANNOT_CLAIM_THE_DEFERRAL_whatever_its_axes_say():
    """▲ THE HOLE THE FIRST FIX OPENED, pinned shut at the gate. The deferral is V3's contract, so
    it is the authenticated PROPOSAL that decides — not the axes, which a V2 run can also write."""
    from tests.featuregen.materialize.test_admission_v2_s13 import _raw as _raw_v2_authoring

    from featuregen.formula.parse_v2 import parse_proposal_v2

    v2 = parse_proposal_v2(_raw_v2_authoring())
    _gate_refuses(_terminal(), proposal=v2, output_intent=None)


def test_a_DEFERRAL_WITH_NO_OUTPUT_INTENT_IS_REFUSED():
    """The intent is what the compiler RECONCILES the governed output against. A deferred run
    without one defers to a stage that would have nothing to check the answer against."""
    _gate_refuses(_terminal(), output_intent=None)


def test_an_OUTPUT_INTENT_FROM_A_DIFFERENT_PROPOSAL_IS_REFUSED():
    """`derived_from_proposal_hash` is what makes the intent checkable rather than merely asserted.
    An intent carried over from another proposal would have the compiler reconcile this formula's
    output against what somebody intended for a different one."""
    from featuregen.formula.parse_v3 import parse_proposal_v3

    other = parse_proposal_v3(_raw_v3(aggregation="count_rows", operand=None))
    _gate_refuses(_terminal(), output_intent=_intent_for(other, "sha256:a-different-proposal"))


def test_a_DEFERRAL_CARRYING_AN_AUTHORITATIVE_OUTPUT_IS_REFUSED():
    """§F's honesty core. No authoritative output exists yet, so one that is present was invented —
    and admitting it would launder a guess into authority."""
    _gate_refuses(_terminal(), candidate_output={"unit": "monetary"})


@pytest.mark.parametrize("axis,value", [
    ("structural_status", "invalid_formula"),
    ("structural_status", "unsupported_operation"),
    ("capability_status", "unsupported_capability"),
    ("technical_status", "technical_failure"),
    ("critic_status", "blocking"),
    ("expectation_status", "mismatch"),
])
def test_a_PAYLOAD_DISAGREEING_WITH_ITS_OWN_AXES_IS_REFUSED(axis, value):
    """The re-fold, per axis. A terminal event whose recorded disposition does not follow from its
    recorded axes is refused rather than believed — and the test is the FOLD's own precedence, not
    a second list here, so it cannot drift from the rule that produced the verdict."""
    _gate_refuses(_terminal(**{axis: value}))


def test_a_RESOLVED_run_needs_none_of_this():
    """RESOLVED is admitted on sight, as it always was. The exception is an addition, not a
    replacement — a V1/V2 run that answered every question is unaffected by any of it."""
    _gate(_terminal(authoring_disposition="RESOLVED", output_status="resolved"),
          output_intent=None)


def test_the_REVIEWED_BLUEPRINT_BYPASS_shape_is_admitted_too():
    """C-A5's OTHER review provenance. A deterministic run over a reviewed blueprint records HOW
    review was obtained — a bypass, with `critic_status=None` — and the v1-shaped axes builder
    REJECTS that None. So a gate that folded every payload through one shape would fail closed on
    exactly this path and report a reviewed, deterministic run as inadmissible.
    """
    review = {"blueprint_revision": "candidate-1", "expectation_hash": "sha256:blueprint"}

    _gate(_terminal(review=review, critic_status=None))


def test_the_bypass_shape_ALSO_refuses_when_something_else_is_outstanding():
    """The bypass is a different review provenance, not a weaker gate."""
    review = {"blueprint_revision": "candidate-1", "expectation_hash": "sha256:blueprint"}

    _gate_refuses(_terminal(review=review, critic_status=None,
                            expectation_status="mismatch"))


# ══ THE COMPILER'S HALF OF THE BARGAIN ═════════════════════════════════════════════════════════
def test_a_COMPILER_OUTPUT_FAILURE_SEALS_NOTHING(catalog, spine, monkeypatch):
    """Admission defers output authority TO the compiler; this is the compiler declining it.

    A monetary operand whose currency is decided per row, with no declared conversion, cannot be
    summed — the result would add dirhams to dollars. `resolve_output_v2` refuses, and the whole
    point of deferring is that this refusal happens somewhere that can make it: no artifact, no
    SUCCEEDED request, and a refusal naming what was wrong.
    """
    _ready_draft(catalog, monkeypatch)
    request_id = _requested(catalog)
    _advertise_this_build(catalog)
    # The job's operand facts are what output authority reads. Per-row currency, no conversion.
    catalog.execute(
        "UPDATE queue SET payload = jsonb_set(payload, '{operand_facts}', %s::jsonb) "
        "WHERE message_id = %s",
        (json.dumps({TXN_AMT: {"logical_type": "decimal", "unit": "monetary",
                               "currency": "per_row"}}),
         f"generation:{request_id}"))

    outcome = process_generation_once(catalog, owner="w1", inventory=INVENTORY)

    assert outcome.status == "refused", outcome.detail
    assert "OUTPUT_TYPE_NOT_GOVERNED" in outcome.detail
    assert read_request(catalog, request_id).status is GenerationStatusV1.REFUSED
    assert catalog.execute("SELECT count(*) FROM sealed_artifact_v2").fetchone()[0] == 0


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
