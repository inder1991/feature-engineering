"""Task E0 — THE SEAM WALKTHROUGH GATE: this plan's own acceptance test.

ONE test walks §0.5 items 1–4 against a real database and two real seeded catalogs, through the
same surfaces a human drives, and it runs in the DEFAULT suite: from the day it lands, any
regression that breaks a weld of this plan breaks the build.

    reviews recorded → the semantic engine SERVES the reviewed v2 exemplar → a durable
    formula-shadow work item carrying the FROZEN PLAN ENVELOPE → the platform's own worker gate
    (the honest boundary: no v2 authoring exists) → the activation fold ALLOWS
    ``execute_materialization`` on a real frozen row → ``POST /materialization-runs`` ACCEPTS →
    the worker lane compiles → ``run_l0`` PASSED → the compiled read set IS the frozen
    envelope's.

Modelled on the parent plan's Task E0 (``tests/featuregen/api/test_e2e_walkthrough.py``,
``bd43964d``), which is the precedent for a walkthrough gate that runs in CI.

WHAT IS REAL, AND WHERE THE WALK IS HONESTLY IN TWO PIECES
----------------------------------------------------------
The seam is genuinely welded at both ends and genuinely unwelded in the middle, and this file says
so rather than papering over it:

* **The governed half is the v2 exemplar.** ``posted_debit_amount`` is the ONE ``formula-v2``
  recipe with a reviewed expectation (A5), so it is the only candidate §0.5 item 1 can be about.
  Its serving run, its work item, its frozen envelope and its option decision are all produced by
  the real engine on the real route.
* **It cannot be the feature that compiles, and the reason is recorded, not hidden.** No
  replay-shaped v2 authoring orchestrator exists (A3's plan defect 1; A4 increment 2 makes the live
  worker terminalize every v2 work item with ``V2_AUTHORING_UNAVAILABLE`` precisely so no dishonest
  verdict is written). ``materialize/resolve.py`` restores a v1 ``AuthoringResult``; a v2 artifact
  is a *pair* (proposal + policy) with no path into materialization at all. So §0.5 item 2's
  authoring half is asserted **up to the platform's own refusal** — :func:`_the_worker_refuses_to_
  author_v2` reads the gate's answer off the REAL captured row — and the execution half of the walk
  runs on ``total_debit_amount_30d``, the recorded-fixture v1 feature whose authoring genuinely
  RESOLVES.

  **The day the v2 orchestrator lands, that assertion goes RED and this test must be extended.**
  That is the point of asserting a gap instead of omitting it.

WHAT IS SEEDED, EACH NAMED (the C3 milestone's vocabulary, deliberately)
-----------------------------------------------------------------------
* The recipe **reviews** are real ``recipe_review_event`` rows written by ``record_review_event``
  at the live revision hash, by every role the recipe itself names — and they are recorded BEFORE
  the generation run, so the SERVED fold measures them (a review recorded afterwards clears the
  current layer and leaves the frozen one blocking, which is correct and is not a walkthrough).
* The **gold/provider evaluation** has no store — ``_gold_evaluation_recorded`` is the documented
  hook C3 named, and it is seeded here for the same reason.
* **Snapshot freshness** is seeded: snapshot minting rides the generation pipeline, not this test.
* The **external validation results** are real rows in the real store — one
  ``feature_validation_requirement`` per code the candidate's own fold named, one recorded
  ``EXTERNAL_PASSED`` event each, read back through C3's real ``_requirements_closed``.
* ``run_l0``'s **verdict** is injected at ``chain.run_l0`` (``_inject_l0``), exactly as the whole
  materialize suite does: the project is really rendered, really sealed and really materialized on
  disk, and the injected report reads its identity off the lock that is actually there. Proving the
  build in a real kedro+pyspark interpreter is E1's job and lives in the JVM gate — ``pyspark`` and
  ``kedro`` are not dependencies of this platform.

A DEFECT THIS WALKTHROUGH FOUND, ASSERTED IN BOTH DIRECTIONS
------------------------------------------------------------
``api/routes/materialization_runs.py`` calls ``assemble_current_activation_state`` **without
``contract_id``**, so C3's contract-keyed ``_requirements_closed`` read is dead on the one route
that gates materialization: ``EXTERNAL_VALIDATION_OUTSTANDING`` can only clear through the
``validation_status == "DESIGN_CHECKED"`` short-circuit (``activation_policy.py:195``). For the
served exemplar — whose own eligibility fold names eight outstanding codes, ``STATUS_POLICY_
UNRESOLVED`` among them *unconditionally*, because the recipe reads a governed status policy — no
recorded data check can ever open the gate. :func:`test_the_route_cannot_close_the_named_homework`
asserts both halves: the fold says ALLOWED when the contract is named, and the route says 409 for
the same option because it never names one.
"""
from __future__ import annotations

import dataclasses
import json

import pytest
from tests.featuregen.api.test_contract_ranked import (
    _arm_shadow,
    _posted_debit_catalog,
    _shadow_run,
)
from tests.featuregen.api.test_materialization_runs import _PATH, _body
from tests.featuregen.materialize.test_chain import _inject_l0, _seed
from tests.featuregen.materialize.test_ir import (
    CUSTOMERS,
    CUSTOMERS_ASOF,
    CUSTOMERS_CIF,
    INVENTORY,
    TXN,
)
from tests.featuregen.materialize.test_resolve import (  # noqa: F401 — `no_dsn` is autouse
    _seed_work_item,
    no_dsn,
)
from tests.featuregen.overlay.upload.test_reviewed_expectation_seam import _candidate

from featuregen.materialize.codes import CompilationRefusalCode
from featuregen.materialize.compile.wiring import assemble_nodes
from featuregen.materialize.queue_lane import (
    MATERIALIZATION_FLAG,
    MaterializationLaneConfig,
    process_materialization_once,
)
from featuregen.materialize.validation import ValidationStatus, read_validation_reports
from featuregen.overlay.upload import feature_metadata_snapshot, semantic_option_decision
from featuregen.overlay.upload.activation_policy import activation_decision
from featuregen.overlay.upload.feature_assist import FeatureIdea
from featuregen.overlay.upload.feature_metadata_snapshot import SnapshotFreshness
from featuregen.overlay.upload.recipe_formula_worker import (
    AUTHORABLE_EXPECTATION_SCHEMA,
    V2_AUTHORING_UNAVAILABLE,
    declared_expectation_schema,
)
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.recipe_review import record_review_event
from featuregen.overlay.upload.recipe_review_validity import required_reviewer_roles
from featuregen.overlay.upload.semantic_option_decision import (
    assemble_current_activation_state,
    decision_facts_for_candidate,
    load_frozen_option_facts,
    persist_option_decisions,
)

#: The ONE `formula-v2` recipe with a reviewed expectation (A5) — the only candidate §0.5 item 1
#: can be about, and the whole governed half of this walk.
EXEMPLAR = "posted_debit_amount"

#: The recorded-fixture v1 feature the execution half runs on — the one whose authoring genuinely
#: RESOLVES (see the module docstring for why it cannot be the exemplar).
COMPILED = "total_debit_amount_30d"

#: The FROZEN PLAN ENVELOPE for `COMPILED`, in the envelope's own dialect (object refs, with the
#: catalog carried once beside them) — the nine keys `fold_frozen_binding_plan` writes. Authored
#: from the FORMULA's operands, which is where the serving fold's role bindings come from too;
#: `compile_ir` then proves the two derivations agree, or refuses (B3).
COMPILED_ENVELOPE = {
    "plan_kind": "single_source",
    "catalog_source": "hdfc",
    "source_table": "transactions",
    "population_ref": "customers",
    "read_set": [
        "public.transactions.txn_amt",        # the aggregate's operand
        "public.transactions.txn_dt",         # the window's clock
        "public.transactions.cif_id",         # the grain key
        "public.transactions.dr_cr_flag",     # the posted-debit filter, left side
        "public.transactions.status_cd",      # ...and its second predicate
    ],
    "role_bindings": {"amount": "public.transactions.txn_amt"},
    "pit": ("trailing 30d observation window over txn_dt events: (cutoff - 30d, cutoff], "
            "values knowable strictly at or before the cutoff"),
    "output_grain": "customer",
    "window": 30,
}


class _WatchedAssembly:
    """The real assembler, and a record of the read set the compilation authorized.

    Borrowed from ``test_chain._Watched``: the seam is a parameter of the lane's configuration, so
    watching it costs nothing and is the only way to read the COMPILED read set back out of a run
    driven through the queue."""

    def __init__(self) -> None:
        self.authorized_refs: tuple[str, ...] = ()

    def __call__(self, inputs):
        self.authorized_refs = tuple(inputs.authorized.authorized_refs)
        return assemble_nodes(inputs)


@pytest.fixture(autouse=True)
def _materialization_on(monkeypatch):
    monkeypatch.setenv(MATERIALIZATION_FLAG, "1")


def _record_the_reviews(conn, recipe_id: str) -> str:
    """Every role the recipe itself names, at its LIVE revision hash — real events, real fold.

    Returns the revision hash, so the assertions below can prove the reviews are about the same
    revision the serving fold froze."""
    from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash

    definition = v2_recipe_by_id(recipe_id)
    revision_hash = canonical_recipe_v2_hash(definition)
    for index, role in enumerate(required_reviewer_roles(definition)):
        record_review_event(conn, recipe_id=recipe_id, recipe_revision_hash=revision_hash,
                            decision="approved", reviewer=f"reviewer-{index}",
                            reviewer_role=role)
    return revision_hash


def _seed_the_recorded_data_checks(conn, codes) -> str:
    """One governed contract carrying the candidate's own named homework, and a recorded
    ``EXTERNAL_PASSED`` result for every code — the real store, read back by C3's real
    ``_requirements_closed``."""
    conn.execute("INSERT INTO feature (feature_id, name, lifecycle_state) "
                 "VALUES ('feat-e0', 'feat-e0', 'governed') ON CONFLICT DO NOTHING")
    conn.execute("INSERT INTO contract (contract_id, feature_id, feature_name, version) "
                 "VALUES ('contract-e0', 'feat-e0', 'feat-e0', 1)")
    for index, code in enumerate(codes):
        conn.execute(
            "INSERT INTO feature_validation_requirement (requirement_id, contract_id, "
            "requirement_schema_version, metadata_input_fingerprint, code, content_hash) "
            "VALUES (%s, 'contract-e0', 'v1', 'fp', %s, %s)",
            (f"req-e0-{index}", code, f"ch-e0-{index}"))
        conn.execute(
            "INSERT INTO feature_contract_validation_event (event_id, contract_id, "
            "event_type, payload) VALUES (%s, 'contract-e0', 'EXTERNAL_PASSED', %s)",
            (f"evt-e0-{index}", json.dumps({"requirement_id": f"req-e0-{index}"})))
    return "contract-e0"


def _freeze_a_governed_option(conn, *, revision_id: str, generation_run_id: str,
                              revision_hash: str, envelope: dict,
                              metadata_snapshot_id: str | None) -> str:
    """C3's milestone seeding, on the SAME recipe, the SAME revision hash and the recipe's OWN
    served plan envelope, through the SAME real writers, into the revision the served run minted.

    Why a second option row rather than the served one, and it is the ONE seeded thing that is not
    a floor: the served candidate's own eligibility fold names eight outstanding requirement codes
    (``STATUS_POLICY_UNRESOLVED`` among them, and that one fires unconditionally because the recipe
    reads a governed status policy), and the ROUTE cannot close them — see
    :func:`test_the_route_cannot_close_the_named_homework`, which asserts exactly that in both
    directions. This row is the C3 milestone's shape: a ``DESIGN_CHECKED`` idea, which is the ONLY
    state ``activation_policy.py:195`` lets through today. Everything else about it — the plan, the
    revision, the reviews, the floors — is the served run's own.
    """
    candidate = dataclasses.replace(
        _candidate(EXEMPLAR), binding_state="bound", review_current=True,
        recipe_revision_hash=revision_hash, binding_plan=dict(envelope))
    idea = FeatureIdea(name=EXEMPLAR, description="", derives_from=[], aggregation=None,
                       grain_table=None, source_definition_id=EXEMPLAR)
    assert idea.validation_status == "DESIGN_CHECKED", (
        "the seeded shape is the C3 milestone's; if the default moved, the seed must be re-argued")
    facts = decision_facts_for_candidate(candidate, idea, None, "ctx")
    assert persist_option_decisions(
        conn, considered_revision_id=revision_id, generation_run_id=generation_run_id,
        metadata_snapshot_id=metadata_snapshot_id,
        facts_by_option_id={"opt-e0-governed": facts}) == 1
    return "opt-e0-governed"


def _config(tmp_path, assembly) -> MaterializationLaneConfig:
    """The DEPLOYMENT's half of the lane configuration. G-2's three seams are ``None`` because that
    is what ``lane_config_from_env`` produces in every deployment: no ``MetastoreMetadata``
    implementation exists in ``src/`` at all (D1's acceptance row), so a lane-driven run is honestly
    unprepared after L0 — which is exactly §0.5's item-4/item-5 line, and E1's starting point."""
    from tests.featuregen.materialize.test_chain import _L0, _clock

    return MaterializationLaneConfig(
        inventory=INVENTORY, project_root=str(tmp_path), l0=_L0, assemble_nodes=assembly,
        clock=_clock)


# ── the gate ─────────────────────────────────────────────────────────────────────────────────────


def test_the_seam_walks_from_a_served_candidate_to_a_build_verified_run(
        make_client, conn, monkeypatch, tmp_path):
    """§0.5 items 1–4, in one walk, against a real database and two real catalogs."""
    _arm_shadow(conn, monkeypatch)

    # ── Step 0: the governance ACT comes FIRST, so the SERVING fold measures it. ────────────
    revision_hash = _record_the_reviews(conn, EXEMPLAR)

    # ── Step 1 (§0.5 item 1): the semantic engine SERVES the reviewed v2 exemplar, and the
    # frozen option decision says its expectation is reviewed. ─────────────────────────────
    _posted_debit_catalog(conn)
    served = _shadow_run(
        make_client, conn,
        use_case="payments.behaviour",
        hypothesis="accounts posting more debit value are more likely to attrite",
        objective="understand payment behaviour",
        catalog_source="posting_bank",
        target_ref="public.txns.attrited")

    options = conn.execute(
        "SELECT considered_revision_id, option_id, has_reviewed_formula_expectation, "
        "binding_state, review_current, metadata_snapshot_id "
        "FROM semantic_option_decision WHERE source_definition_id = %s ORDER BY option_id",
        (EXEMPLAR,)).fetchall()
    assert options, "the reviewed v2 exemplar must be SERVED, or there is nothing to walk"
    served_revision_id, served_snapshot_id = options[0][0], options[0][5]
    assert served_snapshot_id, "the generation SEALED, so its options name a metadata snapshot"
    assert all(row[2] is True for row in options), (
        "§0.5 item 1: has_reviewed_formula_expectation must be true on the frozen decision")
    assert all(row[3] == "bound" for row in options), options
    assert all(row[4] is True for row in options), (
        "the reviews were recorded BEFORE generation, so the SERVED fold must have measured them")

    # ── Step 2 (§0.5 item 2, capture half): the shadow capture produced a durable work item
    # for that candidate, and it carries the FROZEN PLAN ENVELOPE (B2). ────────────────────
    work = conn.execute(
        "SELECT work_item_id, recipe_candidate_key, binding_plan_json, binding_plan_hash, "
        "provider_input_json FROM recipe_formula_shadow_work_item "
        "WHERE generation_run_id = %s AND recipe_id = %s",
        (served["generation_run_id"], EXEMPLAR)).fetchall()
    assert len(work) == 1, work
    _work_item_id, candidate_key, envelope, envelope_hash, provider_input = work[0]
    assert candidate_key, "an EXACT candidate, not a family"
    assert envelope["plan_kind"] == "single_source"
    assert envelope["catalog_source"] == "posting_bank"
    assert envelope["source_table"] == "txns"
    assert envelope["output_grain"] == "account"
    assert envelope_hash and len(envelope_hash) == 64
    # ...and the plan is NOT on the wire to a provider: the egress whitelist is fail-close and a
    # provider authors a formula, it does not read our governed plan.
    assert "binding_plan" not in provider_input and "read_set" not in provider_input

    # ── Step 3 (§0.5 item 2, authoring half): THE HONEST BOUNDARY. The platform's own gate,
    # asked about the REAL captured row. No v2 authoring exists, and the worker says so
    # without writing a verdict about the recipe. ──────────────────────────────────────────
    declared = declared_expectation_schema({"provider_input_json": provider_input})
    assert declared != AUTHORABLE_EXPECTATION_SCHEMA, (
        "if this is now authorable, the v2 orchestrator landed and this walkthrough must be "
        "extended through it — that is what asserting the gap is for")
    assert V2_AUTHORING_UNAVAILABLE == "V2_AUTHORING_UNAVAILABLE"
    assert declared == "formula-v2"

    # ── Step 4 (§0.5 item 3): the activation fold ALLOWS execute_materialization, on a real
    # frozen row, with all four §0.3 codes cleared. ────────────────────────────────────────
    monkeypatch.setattr(semantic_option_decision, "_gold_evaluation_recorded",
                        lambda recipe_id: True)
    monkeypatch.setattr(feature_metadata_snapshot, "compare_snapshot_to_current",
                        lambda conn, snapshot_id: SnapshotFreshness("current", None, None))
    option_id = _freeze_a_governed_option(
        conn, revision_id=served_revision_id,
        generation_run_id=served["generation_run_id"], revision_hash=revision_hash,
        envelope=envelope, metadata_snapshot_id=served_snapshot_id)
    frozen = load_frozen_option_facts(
        conn, considered_revision_id=served_revision_id, option_id=option_id)
    assert frozen is not None and frozen.has_reviewed_formula_expectation is True
    assert frozen.recipe_revision_hash == revision_hash, (
        "the reviews and the frozen decision must be about ONE revision")

    current = assemble_current_activation_state(
        conn, frozen=frozen, snapshot_id=frozen.snapshot_id)
    assert current.effective_readiness == "MATERIALIZATION_READY", current
    assert current.formula_schema_supported is True
    assert current.execution_authority_evaluated and current.execution_floor_met
    decision = activation_decision(frozen, current, "execute_materialization")
    assert decision.blockers == (), [b.code for b in decision.blockers]
    assert decision.allowed is True

    # ── Step 5 (§0.5 item 4): POST /materialization-runs ACCEPTS — 202, not 409 — carrying
    # that governed option as the request's provenance. ────────────────────────────────────
    _seed(conn)
    _inject_l0(monkeypatch)
    work_item_id = _seed_work_item(conn, COMPILED, "e0", binding_plan=dict(COMPILED_ENVELOPE))
    _author_the_compiled_feature(conn, monkeypatch, work_item_id)

    client = make_client()
    response = client.post(_PATH, json=_body(
        [work_item_id], key="e0-walkthrough",
        considered_revision_id=served_revision_id, option_id=option_id),
        headers={"X-User": "priya", "X-Roles": "platform-admin"})
    assert response.status_code == 202, response.text
    request_id = response.json()["request_id"]

    # ── Step 6 (§0.5 item 4): the lane compiles, run_l0 PASSED, and the compiled read set IS
    # the frozen envelope's. ───────────────────────────────────────────────────────────────
    assembly = _WatchedAssembly()
    outcome = process_materialization_once(conn, owner="w-e0", config=_config(tmp_path, assembly))
    assert outcome.request_id == request_id, outcome
    assert outcome.generation_id, outcome

    reports = read_validation_reports(conn, generation_id=outcome.generation_id)
    assert [report.status for report in reports] == [ValidationStatus.PASSED], reports

    # THE HEADLINE. `compile_ir` already REFUSES a compilation whose read set the frozen plan did
    # not name (B3, `PLAN_ENVELOPE_DIVERGENCE`), so reaching a PASSED L0 at all is one proof; the
    # SET EQUALITY below is the other, so a widening cannot hide inside a passing compile. Every
    # ref the compilation authorized that the human's plan did not name is one of B3's four
    # structurally-explained additions, each pinned here with its reason rather than carved out.
    from featuregen.materialize.ir import _envelope_ref

    frozen_refs = {_envelope_ref(COMPILED_ENVELOPE["catalog_source"], ref)
                   for ref in COMPILED_ENVELOPE["read_set"]}
    compiled_refs = {_envelope_ref("", ref) for ref in assembly.authorized_refs}
    additions = {
        _envelope_ref("", CUSTOMERS),        # the spine's relation (a separate declaration, §4)
        _envelope_ref("", CUSTOMERS_CIF),    # ...its ordered key
        _envelope_ref("", CUSTOMERS_ASOF),   # ...and its availability column
        _envelope_ref("", TXN),              # the expression's source relation (admission check 7)
    }
    assert compiled_refs == frozen_refs | additions, {
        "unnamed": compiled_refs - frozen_refs - additions,
        "missing": frozen_refs - compiled_refs}


def test_the_route_cannot_close_the_named_homework(make_client, conn, monkeypatch, tmp_path):
    """THE DEFECT THIS WALKTHROUGH FOUND, asserted in both directions on ONE option.

    C3 built ``_requirements_closed`` as a real contract-keyed read of the validation store, and
    ``api/routes/materialization_runs.py`` calls ``assemble_current_activation_state`` without
    ``contract_id`` — so on the one route that gates materialization the read is dead and
    ``EXTERNAL_VALIDATION_OUTSTANDING`` can only clear through the ``DESIGN_CHECKED``
    short-circuit. For a candidate whose own fold names outstanding codes, no recorded data check
    can ever open the gate.
    """
    _arm_shadow(conn, monkeypatch)
    _record_the_reviews(conn, EXEMPLAR)
    _posted_debit_catalog(conn)
    _shadow_run(
        make_client, conn,
        use_case="payments.behaviour",
        hypothesis="accounts posting more debit value are more likely to attrite",
        objective="understand payment behaviour",
        catalog_source="posting_bank",
        target_ref="public.txns.attrited")
    monkeypatch.setattr(semantic_option_decision, "_gold_evaluation_recorded",
                        lambda recipe_id: True)
    monkeypatch.setattr(feature_metadata_snapshot, "compare_snapshot_to_current",
                        lambda conn, snapshot_id: SnapshotFreshness("current", None, None))

    revision_id, option_id = conn.execute(
        "SELECT considered_revision_id, option_id FROM semantic_option_decision "
        "WHERE source_definition_id = %s ORDER BY option_id LIMIT 1", (EXEMPLAR,)).fetchone()
    frozen = load_frozen_option_facts(
        conn, considered_revision_id=revision_id, option_id=option_id)
    assert frozen.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert "STATUS_POLICY_UNRESOLVED" in frozen.outstanding_requirement_codes, (
        "the exemplar reads a governed status policy, so this code rides every candidate "
        "unconditionally — it is named homework, not a defect")

    # The homework, RECORDED — real requirement rows and real recorded results.
    contract_id = _seed_the_recorded_data_checks(conn, frozen.outstanding_requirement_codes)

    named = assemble_current_activation_state(
        conn, frozen=frozen, snapshot_id="snap-e0", contract_id=contract_id)
    assert named.requirements_closed is True
    assert activation_decision(frozen, named, "execute_materialization").allowed is True

    unnamed = assemble_current_activation_state(conn, frozen=frozen, snapshot_id="snap-e0")
    assert unnamed.requirements_closed is False
    assert {b.code for b in activation_decision(
        frozen, unnamed, "execute_materialization").blockers} == {
        "EXTERNAL_VALIDATION_OUTSTANDING"}

    # ...and the ROUTE takes the second reading, so the same option is refused.
    _seed(conn)
    work_item_id = _seed_work_item(conn, COMPILED, "e0d")
    response = make_client().post(_PATH, json=_body(
        [work_item_id], key="e0-defect",
        considered_revision_id=revision_id, option_id=option_id),
        headers={"X-User": "priya", "X-Roles": "platform-admin"})
    assert response.status_code == 409, response.text
    assert {b["code"] for b in response.json()["detail"]["blockers"]} == {
        "EXTERNAL_VALIDATION_OUTSTANDING"}


def _author_the_compiled_feature(conn, monkeypatch, work_item_id: str) -> None:
    from tests.featuregen.materialize.test_resolve import _author_the_run

    _author_the_run(conn, monkeypatch, work_item_id, COMPILED)


def test_a_narrowed_envelope_breaks_the_walk_INSIDE_the_lane(conn, monkeypatch, tmp_path):
    """THE MUTANT PROOF: the envelope weld is live in the LANE, not only in ``compile_ir``'s unit
    tests. Drop one genuinely-read column from the frozen plan and the same lane-driven run refuses
    with ``PLAN_ENVELOPE_DIVERGENCE`` naming the column — so the walkthrough above cannot be passing
    because the check is absent.

    Driven through the queue rather than the route, because the subject here is the COMPILE and a
    request that never reaches it would prove nothing about the envelope.
    """
    from tests.featuregen.materialize.test_chain import _CADENCE, _GROUP, _PROMISE
    from tests.featuregen.materialize.test_ir import DECLARATION

    from featuregen.materialize.publish import PublishMechanism
    from featuregen.materialize.queue_lane import MaterializationJobV1, enqueue_materialization
    from featuregen.materialize.request_store import record_request

    _seed(conn)
    _inject_l0(monkeypatch)
    narrowed = {**COMPILED_ENVELOPE,
                "read_set": [ref for ref in COMPILED_ENVELOPE["read_set"]
                             if "txn_amt" not in ref]}
    work_item_id = _seed_work_item(conn, COMPILED, "e0m", binding_plan=narrowed)
    _author_the_compiled_feature(conn, monkeypatch, work_item_id)
    request = record_request(
        conn, request_id="req-e0-mutant", logical_group_name=_GROUP, requested_by="user:asha",
        authorized_roles=("feature_engineer",), idempotency_key="e0-mutant",
        activation_state={"flag": "on"})
    enqueue_materialization(conn, request, job=MaterializationJobV1(
        request_id=request.request_id, work_item_ids=(work_item_id,),
        spine_declaration=DECLARATION, cadence=_CADENCE, availability_promise=_PROMISE,
        mechanism=PublishMechanism.VERSIONED_POINTER, published_schema=None,
        contract_overrides=None, business_dt=None))

    outcome = process_materialization_once(
        conn, owner="w-e0m", config=_config(tmp_path, _WatchedAssembly()))

    assert outcome.status == "refused", outcome
    assert CompilationRefusalCode.PLAN_ENVELOPE_DIVERGENCE.value in (outcome.detail or ""), outcome
    assert "txn_amt" in (outcome.detail or ""), outcome
