"""Rehydrating a READY draft into something the V2 compiler can take.

Step 4 of the V2-only plan, and the bridge that did not exist: `resolve.py` restores V1 features
from the shadow lane's work items, and nothing converted a formula DRAFT — the V2 anchor, produced
when a person pressed *Draft formula* — into an admission input.

What these tests hold, in the order it matters:

1. **The result comes from the TRACE**, cross-checked against the draft. A stored blob is a copy;
   the trace is what the run decided, and a disagreement is a refusal rather than a preference.
2. **The intent hash is DERIVED**, so the checkpoint's identity guard is a real question rather
   than a value compared to itself.
3. **Every refusal names the SELECTION** a person chose, not an internal draft id.
4. **All or nothing**, and declared order is preserved.
"""
from __future__ import annotations

import pytest
from tests.featuregen.formula.authoring_fixtures import seed_authoring_catalog
from tests.featuregen.materialize.test_admission_v2_s13 import (
    _INTENT,
    ENGINE,
    _advertise,
    _raw,
)

from featuregen.formula.critic import CriticReview
from featuregen.formula.recipe_authoring import recipe_tool_runner_v2
from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay
from featuregen.formula.turns import AuthorTurnRecord, TurnKind
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.materialize.restore_formula_v3 import (
    _intent_of,
    restore_build_set_formulas,
    restore_formula,
)


@pytest.fixture
def catalog(db):
    seed_authoring_catalog(db)
    return db


def _author_the_run(db, monkeypatch, run_id: str, intent=None) -> None:
    """Drive the REAL v2 orchestrator with the two PROVIDER stages scripted.

    Scripted for the reason `test_resolve.py` states for v1, and it applies identically here:
    `author_formula`'s audited seam records `llm_call_ref` rows whose `llm_dispatch` reconciliation
    `load_verified_checkpoint` then demands, and those rows exist only under a durable DSN — which
    this suite deliberately lacks, because write-once trace rows a durable connection commits can
    never be cleaned up between runs. Everything else is the real writer: the manifest, the stage
    sequence, every payload and every payload hash.
    """
    raw = _raw()

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
        lambda *a, **k: CriticReview((), "critic_hash", False, None, 1, {}))
    from tests.featuregen.formula.authoring_fixtures import (
        REF_AMT,
        REF_CIF,
        REF_DT,
        TABLE_REF,
    )

    from featuregen.formula.output_authority_v2 import OperandFactsV2

    run_authoring_v2_replay(
        db, intent if intent is not None else _INTENT, object(), object(), actor=None,
        authoring_run_id=run_id,
        facts_reader=lambda _p: ({REF_AMT: OperandFactsV2(
            logical_type="decimal", unit="monetary", currency="fixed:AED")}, ()),
        critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
        tool_runner=recipe_tool_runner_v2(
            frozenset({TABLE_REF, REF_AMT, REF_DT, REF_CIF})),
        # DERIVED from the scripted proposal: authoring now refuses a run that asks for one grammar
        # and produces another, so a fixture may not disagree with itself either.
        formula_schema_version=raw.get("formula_schema_version", 2))


def _ready_draft(db, monkeypatch, *, draft_id: str = "fd-restore",
                 option_id: str = "opt-a") -> str:
    """A READY draft whose run has a replayable trace."""
    run_id = f"far-restore-{draft_id}"
    # AUTHOR WITH THE INTENT PRODUCTION WOULD DERIVE, not a hand-built one. The restorer re-derives
    # the intent from the frozen revision and the checkpoint compares the two, so a fixture that
    # authored under a different intent would be testing the guard rather than the restorer — and
    # the guard is already covered by its own test below.
    _considered_revision(db)
    _author_the_run(db, monkeypatch, run_id, intent=_intent_of(db, "crev-1", option_id, "fixture"))
    proposal_hash = db.execute(
        "SELECT payload->'result'->>'candidate_proposal_hash' FROM formula_authoring_trace_event "
        "WHERE authoring_run_id=%s AND kind='completed'", (run_id,)).fetchone()[0]
    db.execute(
        "INSERT INTO formula_draft (formula_draft_id, considered_revision_id, option_id, "
        "planning_request_hash, catalog_snapshot_hash, authoring_config_hash, definition_revision, "
        "formula_identity_hash, state, authoring_run_id, formula_content_hash, formula_json, "
        "requested_by, requested_at) VALUES (%s,'crev-1',%s,'h1','h2','h3','',%s,'READY',%s,%s,"
        "'{\"body\":{}}'::jsonb,'user:ops','t')",
        (draft_id, option_id, f"ident-{draft_id}", run_id, proposal_hash))
    return draft_id


def _considered_revision(conn) -> None:
    """The frozen candidate set the intent is rebuilt FROM.

    It must resolve to EXACTLY the intent the run was opened for. That is not a fixture convenience
    — it is the guard under test: the restorer derives the intent hash rather than reading it back,
    so a revision describing a different candidate makes the checkpoint refuse. Getting this to
    agree is the test proving the check is real.
    """
    import json

    from featuregen.overlay.field_evidence import canonical_hash
    from featuregen.overlay.upload.contract.gate1 import _candidate_identity, _idea_json
    from featuregen.overlay.upload.feature_assist import FeatureIdea

    idea = FeatureIdea(
        name=_INTENT.name, description="recent debit volume",
        derives_from=["public.txns.txn_amt"],
        derives_pairs=(("authored", "public.txns.txn_amt"),),
        aggregation="sum", grain_table=_INTENT.target_entity,
        # The TYPED COMPUTATION, which a v3 candidate carries and a v2 one did not. The grain is
        # what the restorer reads; the rest is here because a candidate that declares one and not
        # the others is not a shape production produces.
        operation_kind="sum",
        measure_refs=(("authored", "public.txns.txn_amt"),),
        grain_refs=(("authored", "public.txns.cif_id"),),
        time_ref=("authored", "public.txns.txn_dt"),
        window="90d")
    identity = _candidate_identity(path="anchor", source="anchor", lens="anchor", feature=idea)
    # A SECOND candidate, because `feature_selection_revision` enforces one selection per option:
    # two selections need two options, which is the truthful shape anyway — a person picking two
    # features picked two different ones.
    other = _candidate_identity(path=["alternative", "lens", 0], source="alternative",
                                lens="lens", feature=idea)
    considered = {
        "version": "contract-considered-v3",
        "public": {
            "anchor": {**_idea_json(idea), "option_id": "opt-a"},
            "alternatives": [{"lens": "lens",
                              "features": [{**_idea_json(idea), "option_id": "opt-b"}]}],
            "rejections": []},
        "options_by_id": {
            "opt-a": {
                "source": "anchor", "lens": "anchor",
                "canonical_candidate_identity": identity,
                "canonical_candidate_identity_hash": canonical_hash(identity),
                "recipe_candidate_key": None},
            "opt-b": {
                "source": "alternative", "lens": "lens",
                "canonical_candidate_identity": other,
                "canonical_candidate_identity_hash": canonical_hash(other),
                "recipe_candidate_key": None}},
        "recipe_grounding_context_by_candidate_key": {},
        "recipe_candidate_keys_by_recipe_id": {},
    }
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) VALUES "
        "('int-1',%s,'hypothesis') ON CONFLICT DO NOTHING", (_INTENT.hypothesis,))
    conn.execute(
        "INSERT INTO contract_considered_revision (considered_revision_id, intent_id, "
        "generation_run_id, metadata_snapshot_id, considered_json, considered_content_hash, "
        "canonicalization_version) VALUES ('crev-1','int-1','run-1','snap-1',%s::jsonb,'h',"
        "'contract-considered-v3') ON CONFLICT DO NOTHING", (json.dumps(considered),))


def _selection(conn, revision_id: str, *, option_id: str = "opt-a") -> str:
    """A person's recorded choice of one candidate — what a build set is made of."""
    _considered_revision(conn)
    conn.execute(
        "INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
        "VALUES ('trr-1','int-1','exploration','h') ON CONFLICT DO NOTHING")
    conn.execute(
        "INSERT INTO feature_selection_revision (revision_id, target_reading_revision_id, "
        "considered_revision_id, option_id, decision_id, planning_request_hash, binding_plan_hash, "
        "content_hash) VALUES (%s,'trr-1','crev-1',%s,%s,'h1','h2',%s) ON CONFLICT DO NOTHING",
        (revision_id, option_id, f"dec-{revision_id}", f"ch-{revision_id}"))
    return revision_id



def _bind(conn, selection_revision_id: str, draft_id: str = "fd-restore") -> str:
    """Pin this selection to this draft, the way a coordinator does before declaring a build set.

    ▲ Restoring now resolves THROUGH the pin rather than searching for "the newest draft", so every
    test that used to name a selection names a binding instead.
    """
    from featuregen.overlay.upload.selection_formula_binding import (
        record_selection_formula_binding,
    )

    binding, _ = record_selection_formula_binding(
        conn, selection_revision_id=selection_revision_id, formula_draft_id=draft_id)
    return binding.binding_id


# ══ THE HAPPY PATH ══════════════════════════════════════════════════════════════════════════════
def test_A_READY_DRAFT_BECOMES_AN_ADMISSION_INPUT(db, catalog, monkeypatch):
    """The conversion the V2 path was missing entirely."""
    _ready_draft(db, monkeypatch)
    _selection(db, "sel-1")

    restored = restore_formula(db, selection_formula_binding_id=_bind(db, "sel-1"))

    assert restored.selection_revision_id == "sel-1"
    assert restored.formula_draft_id == "fd-restore"
    # Rebuilt from the trace, not read off the draft's stored JSON.
    assert restored.input.result.candidate_proposal is not None
    assert restored.input.intent.name


def test_the_restored_input_is_ADMISSIBLE(db, catalog, monkeypatch):
    """The point of restoring at all: what comes out must be what admission takes."""
    from featuregen.materialize.admission_v2 import admit_artifacts_v2

    _ready_draft(db, monkeypatch)
    _selection(db, "sel-1")
    _advertise(db)                     # renderer support recorded for this engine
    restored = restore_formula(db, selection_formula_binding_id=_bind(db, "sel-1"))

    admitted = admit_artifacts_v2(db, [restored.input], engine_id=ENGINE)
    assert admitted and admitted[0].proposal_content_hash


# ══ EVERY REFUSAL NAMES THE SELECTION ══════════════════════════════════════════════════════════
def test_a_selection_with_NO_DRAFT_CANNOT_EVEN_BE_PINNED(db, catalog, monkeypatch):
    """▲ THIS REFUSAL MOVED EARLIER, and that is the improvement. Selecting and drafting are still
    separate acts, so a selected candidate may genuinely have no formula — but the platform now
    finds out when the build set is DECLARED rather than when the worker tries to build it. A pin
    cannot be created for a candidate nobody drafted."""
    from featuregen.overlay.upload.selection_formula_binding import (
        BindingDisagreement,
        record_selection_formula_binding,
    )

    _selection(db, "sel-undrafted", option_id="opt-never-drafted")

    with pytest.raises(BindingDisagreement, match="does not exist"):
        record_selection_formula_binding(
            db, selection_revision_id="sel-undrafted", formula_draft_id="fd-never-drafted")


def test_a_MEMBER_WITH_NO_PIN_refuses_and_says_why(db, catalog, monkeypatch):
    """The build-time half: a member naming a binding that is not there. Resolving "the newest
    draft" instead is exactly the drift the pin exists to prevent, so this is a refusal."""
    with pytest.raises(MaterializationRefused) as raised:
        restore_formula(db, selection_formula_binding_id="bind-nobody-made")
    assert raised.value.code is CompilationRefusalCode.NOT_RESOLVED
    assert "bind-nobody-made" in raised.value.detail


def test_a_draft_that_stopped_short_refuses_WITHOUT_re_explaining_itself(db, catalog, monkeypatch):
    """A BLOCKED draft already recorded why. Restoring must not invent a second explanation."""

    _ready_draft(db, monkeypatch)
    _selection(db, "sel-1")
    binding = _bind(db, "sel-1")       # pinned while READY, as a coordinator would
    db.execute("UPDATE formula_draft SET state='FAILED', failure_reason='x' "
               "WHERE formula_draft_id='fd-restore'")

    with pytest.raises(MaterializationRefused) as raised:
        restore_formula(db, selection_formula_binding_id=binding)
    assert "rather than READY" in raised.value.detail
    assert "already recorded why" in raised.value.detail


def test_a_selection_that_does_not_exist_CANNOT_BE_PINNED(db, catalog, monkeypatch):
    """The refusal moved to pinning time along with the one above: there is no choice to pin to."""
    from featuregen.overlay.upload.selection_formula_binding import (
        BindingDisagreement,
        record_selection_formula_binding,
    )

    _ready_draft(db, monkeypatch)

    with pytest.raises(BindingDisagreement, match="does not exist"):
        record_selection_formula_binding(
            db, selection_revision_id="sel-nonexistent", formula_draft_id="fd-restore")


# ══ THE TRACE AND THE DRAFT MUST AGREE ═════════════════════════════════════════════════════════
def test_A_PINNED_FORMULA_CANNOT_BE_CHANGED_UNDERNEATH_THE_BUILD(db, catalog, monkeypatch):
    """▲ THE DATABASE REFUSES THIS OUTRIGHT, which is stronger than the runtime check beside it.

    `formula_draft` freezes its IDENTITY columns and permits its RESULT columns to move — so before
    the pin existed, a draft's contents could change after a build set was declared and the build
    would compile whatever was there. Migration 1101's composite foreign key includes
    `formula_content_hash`, so once a binding references the draft, that column cannot move at all
    while the pin exists.

    `restore_formula` still compares the two. That comparison is DEFENCE IN DEPTH rather than the
    guarantee — the same relationship the queue has between its partition predicate and
    `queue_one_inflight_per_partition`: the index is what makes it true, the check is what makes it
    legible.
    """
    import psycopg

    _ready_draft(db, monkeypatch)
    _selection(db, "sel-1")
    _bind(db, "sel-1")

    with pytest.raises(psycopg.errors.ForeignKeyViolation, match="selection_formula_binding"):
        db.execute("UPDATE formula_draft SET formula_content_hash='sha256:something-else' "
                   "WHERE formula_draft_id='fd-restore'")


def test_A_DRAFT_DISAGREEING_WITH_ITS_TRACE_IS_REFUSED(db, catalog, monkeypatch):
    """The draft is what a person READ; the trace is what the run DECIDED.

    ▲ Pinned AFTER the tamper on purpose, so the pin agrees with the draft and this test exercises
    the TRACE comparison rather than the pin. Without that ordering the pin would refuse first and
    this check would be dead while still appearing to pass.
    """
    _ready_draft(db, monkeypatch)
    _selection(db, "sel-1")
    db.execute("UPDATE formula_draft SET formula_content_hash='sha256:something-else' "
               "WHERE formula_draft_id='fd-restore'")
    binding = _bind(db, "sel-1")

    with pytest.raises(MaterializationRefused) as raised:
        restore_formula(db, selection_formula_binding_id=binding)
    assert raised.value.code is CompilationRefusalCode.INTENT_HASH_MISMATCH
    assert "nobody reviewed" in raised.value.detail


# ══ GROUPS: ALL OR NOTHING, IN DECLARED ORDER ══════════════════════════════════════════════════
def test_declared_ORDER_IS_PRESERVED(db, catalog, monkeypatch):
    """Unlike V1's restorer, which sorts. A build set records the order a person chose in, and
    re-sorting here would discard a fact the set went to trouble to keep."""
    _ready_draft(db, monkeypatch, draft_id="fd-a", option_id="opt-a")
    _ready_draft(db, monkeypatch, draft_id="fd-b", option_id="opt-b")
    _selection(db, "sel-a", option_id="opt-a")
    _selection(db, "sel-b", option_id="opt-b")

    restored = restore_build_set_formulas(
        db, selection_formula_binding_ids=[_bind(db, "sel-b", "fd-b"), _bind(db, "sel-a", "fd-a")])
    assert [r.selection_revision_id for r in restored] == ["sel-b", "sel-a"]


def test_ONE_BAD_MEMBER_STOPS_THE_WHOLE_SET(db, catalog, monkeypatch):
    """A caller handed the survivors of a refused group would compile a group whose membership
    nobody decided — admit_artifacts_v2's rule, and it applies with more force to a build set."""
    _ready_draft(db, monkeypatch, draft_id="fd-a", option_id="opt-a")
    _selection(db, "sel-a", option_id="opt-a")
    _selection(db, "sel-missing", option_id="opt-b")     # a real option nobody drafted or pinned

    with pytest.raises(MaterializationRefused) as raised:
        restore_build_set_formulas(
            db, selection_formula_binding_ids=[_bind(db, "sel-a", "fd-a"), "bind-missing"])
    assert "bind-missing" in raised.value.detail


def test_an_empty_or_duplicated_group_is_a_CALLER_DEFECT_not_a_verdict(db, catalog, monkeypatch):
    """The closed refusal vocabulary has no member for "the caller assembled the batch wrongly",
    and inventing one would type a caller defect as a verdict about an artifact."""
    with pytest.raises(ValueError, match="restores nothing"):
        restore_build_set_formulas(db, selection_formula_binding_ids=[])
    with pytest.raises(ValueError, match="appears twice"):
        restore_build_set_formulas(db, selection_formula_binding_ids=["b-1", "b-1"])
