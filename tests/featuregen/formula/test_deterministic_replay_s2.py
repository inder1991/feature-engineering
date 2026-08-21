"""S2 — a reviewed recipe authors through the REAL orchestrator with no provider call.

The acceptance is *"a reviewed recipe produces a durable replayable result with no provider call
and no `CRITIC_COMPLETED` event"*. Both halves are asserted against the trace the run actually
wrote, and the provider seams are replaced by explosives so a single call fails the test loudly
rather than being inferred from an event count.
"""
from __future__ import annotations

from tests.featuregen.formula.test_replay_authoring_v2 import _INTENT, _monetary_facts

from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay
from featuregen.formula.result_v2 import ReviewedBlueprintBypassV2
from featuregen.formula.schema_v3 import SelectionKind, TypedFormulaProposalV3
from featuregen.overlay.upload.recipe_formula_blueprint_derivation import derive_blueprint_v2
from featuregen.overlay.upload.recipe_formula_contracts_v2 import bind_formula_expectation_v2
from featuregen.overlay.upload.recipe_grounding_context import (
    RecipeGroundingContextV1,
    semantic_parameter_hash,
)
from featuregen.overlay.upload.recipe_grounding_context import (
    content_hash as grounding_content_hash,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.templates import (
    BindingResolution,
    GroundedNeedBinding,
    SourceEntityRoleResolution,
)

_MODULE = "featuregen.formula.replay_authoring_v2"


def _explode(*args, **kwargs):
    raise AssertionError("a deterministic run reached a provider")


def _binding(role: str, ref: str) -> GroundedNeedBinding:
    return GroundedNeedBinding(
        role=role, catalog_source="hdfc", logical_ref=ref,
        graph_object_ref=ref.replace("hdfc::", ""), expected_concept=role,
        optional=False, join_role=None, temporal_role=None, distinct_binding_group=None,
        binding_resolution=BindingResolution.UNIQUE, tied_candidate_logical_refs=(ref,),
        tied_candidate_set_hash="set-hash")


def _bound(recipe_id: str = "posted_debit_amount"):
    """The pilot's blueprint, derived from the SHIPPED recipe and bound to real refs."""
    blueprint = derive_blueprint_v2(v2_recipe_by_id(recipe_id))
    parameters = (("window", 90),)
    definition_json = {"version": "s2-deterministic-replay"}
    context = RecipeGroundingContextV1(
        recipe_candidate_key="candidate-1", recipe_id=recipe_id,
        source_entity_need_role="account",
        source_entity_role_resolution=SourceEntityRoleResolution.INFERRED_UNAMBIGUOUS,
        need_bindings=(_binding("account", "hdfc::public.transactions.cif_id"),
                       _binding("amount", "hdfc::public.transactions.txn_amt"),
                       _binding("event_ts", "hdfc::public.transactions.txn_dt")),
        semantic_parameters=parameters,
        semantic_parameter_binding_hash=semantic_parameter_hash(recipe_id, parameters),
        template_definition=definition_json,
        template_content_hash=grounding_content_hash(definition_json))
    return bind_formula_expectation_v2(context, blueprint)


def _stages(db, run_id: str) -> list[str]:
    return [row[0] for row in db.execute(
        "SELECT stage FROM formula_authoring_trace_event WHERE authoring_run_id=%s ORDER BY seq",
        (run_id,)).fetchall()]


def _run(db, run_id: str, monkeypatch, *, bound=None):
    """Drive the REAL orchestrator with every provider seam replaced by an explosive."""
    monkeypatch.setattr(f"{_MODULE}.author_formula", _explode)
    monkeypatch.setattr(f"{_MODULE}.critique", _explode)
    return run_authoring_v2_replay(
        db, _INTENT, None, None, actor=None, authoring_run_id=run_id,
        facts_reader=_monetary_facts,
        critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
        tool_runner=_explode,
        reviewed_blueprint=bound if bound is not None else _bound(),
        formula_schema_version=3)


# ══ THE ACCEPTANCE ═══════════════════════════════════════════════════════════════════════════════
def test_A_REVIEWED_RECIPE_AUTHORS_WITH_NO_PROVIDER_CALL(db, monkeypatch):
    """`author_formula`, `critique` and the tool runner are all explosives — a single call fails
    here rather than being inferred from an event count."""
    result = _run(db, "far_s2_det", monkeypatch)
    assert result.candidate_proposal is not None
    assert isinstance(result.candidate_proposal, TypedFormulaProposalV3)


def test_NO_CRITIC_COMPLETED_EVENT_IS_WRITTEN(db, monkeypatch):
    """The other half of the acceptance, asserted against the trace the run actually wrote."""
    _run(db, "far_s2_stages", monkeypatch)
    stages = _stages(db, "far_s2_stages")

    assert "CRITIC_COMPLETED" not in stages
    assert "REVIEW_BYPASSED" in stages


def test_THE_TRACE_SAYS_NO_PROVIDER_WAS_INVOLVED(db, monkeypatch):
    """A distinct stage rather than a relaxed `AUTHOR_PROPOSAL_PARSED`, so a trace answers "was a
    provider involved" from its stages alone — the question an auditor asks first."""
    _run(db, "far_s2_derived", monkeypatch)
    stages = _stages(db, "far_s2_derived")

    assert "BLUEPRINT_PROPOSAL_DERIVED" in stages
    assert "AUTHOR_PROPOSAL_PARSED" not in stages
    assert not any(s.startswith("AUTHOR_TURN_") for s in stages), (
        "no author turn was emitted, because none happened")


def test_the_result_records_the_BYPASS_and_no_critic_status(db, monkeypatch):
    """"clean" would claim the critic ran and found nothing, which is false."""
    result = _run(db, "far_s2_review", monkeypatch)

    assert isinstance(result.review, ReviewedBlueprintBypassV2)
    assert result.review.expectation_hash == _bound().blueprint_content_hash
    assert result.critic_status is None
    assert result.critic_findings_hash is None


def test_C_A3Bs_DECLARED_DIRECTION_SURVIVES_THE_WHOLE_CHAIN(db, monkeypatch):
    """Recipe → blueprint → binder → producer → orchestrator → result, with the selection intact
    and still SEMANTIC — never the pilot ledger's `D`."""
    result = _run(db, "far_s2_direction", monkeypatch)
    selections = result.candidate_proposal.body.expr.row_selections

    assert [(s.kind, s.semantic_value) for s in selections] == [
        (SelectionKind.TRANSACTION_DIRECTION, "debit")]


def test_the_CREDIT_twin_authors_the_OPPOSITE_selection(db, monkeypatch):
    """The two recipes were once distinguishable only by name."""
    result = _run(db, "far_s2_credit", monkeypatch,
                  bound=_bound("posted_credit_amount"))
    assert result.candidate_proposal.body.expr.row_selections[0].semantic_value == "credit"


# ══ durable and REPLAYABLE ═══════════════════════════════════════════════════════════════════════
def test_THE_RESULT_IS_DURABLE_AND_REPLAYS_IDENTICALLY(db, monkeypatch):
    """"a durable replayable result". The second call re-reads the terminal rather than re-deriving,
    and the explosives prove nothing was re-issued."""
    first = _run(db, "far_s2_replay", monkeypatch)
    replayed = _run(db, "far_s2_replay", monkeypatch)
    assert replayed == first


def test_a_replay_appends_NO_new_events(db, monkeypatch):
    _run(db, "far_s2_bytes", monkeypatch)
    before = _stages(db, "far_s2_bytes")
    _run(db, "far_s2_bytes", monkeypatch)
    assert _stages(db, "far_s2_bytes") == before


def test_the_run_records_FORMULA_SCHEMA_3(db, monkeypatch):
    """C-A6's acceptance #8, on the path that actually produces a v3 formula."""
    _run(db, "far_s2_schema", monkeypatch)
    versions = db.execute(
        "SELECT versions FROM formula_authoring_run WHERE authoring_run_id=%s",
        ("far_s2_schema",)).fetchone()[0]
    assert versions["formula_schema"] == 3


# ══ a blueprint that cannot author refuses, and does not fall back to a provider ═════════════════
def test_A_BLUEPRINT_THAT_CANNOT_AUTHOR_REFUSES_rather_than_calling_a_provider(db, monkeypatch):
    """A registry defect surfaces as a governed refusal. Falling back to the provider would spend a
    call to paper over a blueprint nobody can execute."""
    from dataclasses import replace

    broken = replace(_bound(), grain_key_refs=())
    result = _run(db, "far_s2_broken", monkeypatch, bound=broken)

    assert result.authoring_disposition != "RESOLVED"
    assert result.candidate_proposal is None


# ══ the authored path is UNTOUCHED ═══════════════════════════════════════════════════════════════
def test_OMITTING_the_blueprint_still_takes_the_AUTHORED_path(db, monkeypatch):
    """The deterministic branch is OPT-IN. Without a blueprint the orchestrator still calls the
    author — proved by the explosive being reached, which the orchestrator folds to a technical
    failure rather than letting escape (it catches every author exception by design, so a raise
    would never surface to a caller)."""
    reached: list[str] = []

    def _record_and_fail(*args, **kwargs):
        reached.append("author")
        raise AssertionError("the author was called, as it should be")

    monkeypatch.setattr(f"{_MODULE}.author_formula", _record_and_fail)
    result = run_authoring_v2_replay(
        db, _INTENT, None, None, actor=None, authoring_run_id="far_s2_authored",
        facts_reader=_monetary_facts,
        critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
        tool_runner=lambda **kw: {}, formula_schema_version=3)

    assert reached == ["author"], "the authored path was taken"
    assert result.authoring_disposition == "TECHNICAL_FAILURE"


# ══ C-A7 — the provisional intent, captured and terminal ═════════════════════════════════════════
def test_A_V3_RUN_IS_TERMINAL_WITHOUT_OUTPUT_POLICY_RESOLVED(db, monkeypatch):
    """C-A7's gate. Resolving the output policy against C1's governed facts is S5's; emitting
    `OUTPUT_POLICY_RESOLVED` here would represent a stage that has not run as having run and
    agreed."""
    result = _run(db, "far_s2_intent", monkeypatch)
    stages = _stages(db, "far_s2_intent")

    assert "OUTPUT_INTENT_CAPTURED" in stages
    assert "OUTPUT_POLICY_RESOLVED" not in stages
    assert stages[-1] == "TERMINAL"
    assert result.candidate_output is None, "no policy was resolved, and the result says so"


def test_A_NON_EMPTY_CURRENCY_CONVERSION_REF_YIELDS_AN_INTENT(db, monkeypatch):
    """S2's acceptance, verbatim. The pilot recipe declares a conversion; S2 records that the
    author intended one rather than resolving or refusing it."""
    result = _run(db, "far_s2_conversion", monkeypatch)

    assert result.output_intent is not None
    assert result.output_intent.conversion_required is True
    assert result.output_intent.declared_conversion_ref.startswith("currency_conversion:")


def test_the_intent_is_PROVISIONAL_and_the_output_says_so(db, monkeypatch):
    """"deferred_to_compiler" is the truthful status: the compiler resolves it, and claiming
    "resolved" here would assert an authority nobody consulted.

    ▲ It is NOT `needs_authority`, which this asserted until the two were separated. That value
    means a governed read was attempted and FAILED, and a human must look — the opposite of a V3
    run that succeeded at everything it owns. Spelling them the same made admission unable to tell
    the two apart, and it briefly admitted the failed one.
    """
    result = _run(db, "far_s2_provisional", monkeypatch)
    assert result.output_status == "deferred_to_compiler"
    assert result.candidate_proposal is not None, "the artifact is still carried"


def test_the_intent_names_the_PROPOSAL_it_was_derived_from(db, monkeypatch):
    from featuregen.formula.canonical_v3 import proposal_content_hash_v3

    result = _run(db, "far_s2_derived_from", monkeypatch)
    assert result.output_intent.derived_from_proposal_hash == proposal_content_hash_v3(
        result.candidate_proposal)


def test_a_deterministic_run_records_NO_AUTHORED_EXPECTATION(db, monkeypatch):
    """C-A5's producer sets `expected_output=None`, and "the author expected nothing" is a fact
    rather than a gap."""
    result = _run(db, "far_s2_no_expectation", monkeypatch)
    assert result.output_intent.authored_expectation_present is False
    assert result.output_intent.unit is None


def test_THE_INTENT_SURVIVES_A_REPLAY(db, monkeypatch):
    """Rebuilt through the type's own `__post_init__`, so a trace whose two halves drifted is
    caught on the way back in rather than served."""
    first = _run(db, "far_s2_intent_replay", monkeypatch)
    replayed = _run(db, "far_s2_intent_replay", monkeypatch)
    assert replayed.output_intent == first.output_intent
    assert replayed == first
