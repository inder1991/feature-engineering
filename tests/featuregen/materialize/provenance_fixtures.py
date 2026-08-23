"""A REAL authoring run whose evidence establishes a method — for tests whose subject is SEALING.

▲ **WHY THIS EXISTS AND WHY IT IS NOT A STUB.** `seal_v2` derives each member's authoring method
from its run's own durable evidence and refuses a run that establishes none. So a sealing test needs
a run that genuinely left evidence behind, and the ordinary suite can produce exactly one of the two
kinds:

* ``REVIEWED_RECIPE_BLUEPRINT`` — a deterministic run over a reviewed blueprint. No provider is
  involved by construction, so it needs nothing the rolled-back test transaction cannot hold, and
  the ``REVIEW_BYPASSED`` stage it writes IS the evidence the derivation reads.
* ``LLM_AUTHORED`` — reconciled author and critic dispatches. Those are written by the audit seam on
  its OWN connection (``FEATUREGEN_DSN``), so they exist only under a durable database. That kind
  lives in `tests/featuregen/formula/durable_evidence.py` and is exercised there.

Nothing here inserts a trace event or a dispatch row by hand. The run is driven through the real
orchestrator, which is the whole point: a fixture that wrote a ``REVIEW_BYPASSED`` row directly
would prove the derivation reads a row somebody typed, not that a reviewed blueprint leaves one.

The recipe is chosen for what its blueprint IMPLIES rather than for what it means: a count over a
trailing window, with no currency conversion and no semantic row selection, so the formula stays
inside what this build's renderer advertises. A sealing test that refused at the advertised-operator
gate would never reach the code it is about.
"""
from __future__ import annotations

from tests.featuregen.materialize.fixtures import REF_AMT, REF_CIF, REF_DT

from featuregen.formula.output_authority_v2 import OperandFactsV2
from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay
from featuregen.formula.turns import AuthoringIntent
from featuregen.materialize.authoring_provenance import MemberAuthoringInputV1

#: A count over a trailing window: `count_rows`, no currency conversion, no row selection.
BLUEPRINT_RECIPE = "contact_attempt_count"

#: role -> logical ref, spelled against the materialize catalog so a run driven here and a run
#: driven by a compiling test read the same columns.
BLUEPRINT_BINDINGS = (("facility", REF_CIF), ("attempt", REF_AMT), ("event_ts", REF_DT))

_INTENT = AuthoringIntent(
    "sealed_member_provenance", "a sealed member records how it was authored", "customer",
    target_grain_keys=(REF_CIF,))


def _binding(role: str, ref: str):
    from featuregen.overlay.upload.templates import BindingResolution, GroundedNeedBinding

    return GroundedNeedBinding(
        role=role, catalog_source="hdfc", logical_ref=ref,
        graph_object_ref=ref.split("::", 1)[1], expected_concept=role,
        optional=False, join_role=None, temporal_role=None, distinct_binding_group=None,
        binding_resolution=BindingResolution.UNIQUE, tied_candidate_logical_refs=(ref,),
        tied_candidate_set_hash="set-hash")


def bound_blueprint(recipe_id: str = BLUEPRINT_RECIPE, bindings=BLUEPRINT_BINDINGS, window: int = 30):
    """The shipped recipe's blueprint, bound to real refs — the reviewed expectation a run bypasses to.

    Every step is the production machinery: `derive_blueprint_v2` reads the shipped recipe,
    `bind_formula_expectation_v2` binds its needs to catalog refs, and the bypass the orchestrator
    writes names the expectation this returns.
    """
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
    from featuregen.overlay.upload.templates import SourceEntityRoleResolution

    blueprint = derive_blueprint_v2(v2_recipe_by_id(recipe_id))
    parameters = (("window", window),)
    definition_json = {"version": "member-provenance-fixture", "window": window}
    context = RecipeGroundingContextV1(
        recipe_candidate_key=f"candidate-{window}", recipe_id=recipe_id,
        source_entity_need_role=bindings[0][0],
        source_entity_role_resolution=SourceEntityRoleResolution.INFERRED_UNAMBIGUOUS,
        need_bindings=tuple(_binding(role, ref) for role, ref in bindings),
        semantic_parameters=parameters,
        semantic_parameter_binding_hash=semantic_parameter_hash(recipe_id, parameters),
        template_definition=definition_json,
        template_content_hash=grounding_content_hash(definition_json))
    return bind_formula_expectation_v2(context, blueprint)


def _facts(_paths):
    return ({REF_AMT: OperandFactsV2(
        logical_type="decimal", unit="monetary", currency="fixed:AED")}, ())


def reviewed_blueprint_run(conn, run_id: str, *, intent=None, window: int = 30) -> str:
    """Drive a REAL deterministic reviewed-blueprint run. Returns its run id.

    The provider seams are never reached — that is what a bypass IS — so this needs no durable
    database and no key, and the ``REVIEW_BYPASSED`` stage it leaves is genuine evidence rather
    than a row a fixture typed.
    """
    from featuregen.formula.recipe_authoring import recipe_tool_runner_v2

    # A run that already terminated is not authored again. The trace is append-only, and a test
    # that seals the same artifact twice (idempotence) must not be asking for a second authoring.
    if conn.execute(
            "SELECT count(*) FROM formula_authoring_trace_event "
            " WHERE authoring_run_id = %s AND kind = 'completed'", (run_id,)).fetchone()[0]:
        return run_id

    run_authoring_v2_replay(
        conn, intent or _INTENT, None, None, actor=None, authoring_run_id=run_id,
        facts_reader=_facts,
        critic_metadata_loader=lambda ref: {"found": True, "logical_ref": ref},
        tool_runner=recipe_tool_runner_v2(frozenset({REF_AMT, REF_DT, REF_CIF})),
        reviewed_blueprint=bound_blueprint(window=window),
        formula_schema_version=3)
    return run_id


def proposal_hash_of(conn, run_id: str) -> str:
    """The proposal hash the run's own terminal event recorded — never a made-up digest."""
    return conn.execute(
        "SELECT payload->'result'->>'candidate_proposal_hash' FROM formula_authoring_trace_event "
        "WHERE authoring_run_id = %s AND kind = 'completed'", (run_id,)).fetchone()[0]


def evidenced_members(conn, *names: str, selection: str = "sel-provenance",
                      run_prefix: str = "far-seal") -> tuple[MemberAuthoringInputV1, ...]:
    """One provenance input per published column, each naming a run that really left evidence.

    Each member gets its OWN run, because two published columns are two authoring acts — a fixture
    that pointed both at one run would make the per-member design untestable by construction.
    """
    members = []
    for index, name in enumerate(names):
        run_id = reviewed_blueprint_run(conn, f"{run_prefix}-{index}-{name}", window=30 + index)
        members.append(MemberAuthoringInputV1(
            member_name=name,
            selection_revision_id=f"{selection}-{index}",
            authoring_run_id=run_id,
            formula_content_hash=proposal_hash_of(conn, run_id)))
    return tuple(members)
