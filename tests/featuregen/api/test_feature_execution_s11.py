"""S11 — the user-reachable execution surface (1082).

*"The verification and publication handlers appear in no relay route map and no timer, and the only
callers of ``evaluate_verify`` / ``evaluate_publish_sandbox`` are their two request endpoints —
asserted by an enumeration test over the route table."*

The acceptance is deliberately a set of NEGATIVE claims, and negatives are what this file spends most
of its effort on: an enumeration over the worker's relay routes, control-signal handlers and timer
registry, and an AST walk over every module in the tree that could call either evaluator. A positive
test ("the endpoint works") cannot say anything about the paths that must not exist.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from featuregen.materialize import evaluate_execution
from featuregen.overlay.upload import semantic_eligibility_reasons as R
from featuregen.overlay.upload.evaluator_contracts import EvaluatorAction
from featuregen.overlay.upload.verification_store import StalenessV1

SRC = pathlib.Path("src/featuregen")
#: The two names the acceptance clause is about.
GATED_EVALUATORS = ("evaluate_verify", "evaluate_publish_sandbox")
#: Where each is allowed to be called from. Its own module (the definition lives there) and the one
#: request endpoint. Anything else is a path nobody asked for.
PERMITTED_CALLERS = {
    "materialize/evaluate_execution.py",       # the definitions themselves
    "api/routes/feature_execution.py",         # the two request endpoints
}


def _calls_in(path: pathlib.Path) -> set[str]:
    """Every function NAME called in a module, from its AST.

    An AST walk rather than a text grep, so a docstring that names an evaluator — and both modules'
    docstrings do, at length — is not counted as a call. That distinction is the whole point: the
    prose explaining why nothing else may call these must not itself look like a violation.
    """
    tree = ast.parse(path.read_text())
    called: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                called.add(target.id)
            elif isinstance(target, ast.Attribute):
                called.add(target.attr)
    return called


# ══ ACCEPTANCE — the evaluators have EXACTLY their two request endpoints as callers ═════════════
@pytest.mark.parametrize("evaluator", GATED_EVALUATORS)
def test_NOTHING_ELSE_IN_THE_TREE_CALLS_THE_EVALUATOR(evaluator):
    """Enumerated over every module in `src/featuregen`, not over a list somebody maintains: a new
    caller added anywhere fails this without anyone remembering to update a fixture."""
    callers = {
        str(path.relative_to(SRC))
        for path in SRC.rglob("*.py")
        if evaluator in _calls_in(path)
    }
    assert callers <= PERMITTED_CALLERS, sorted(callers - PERMITTED_CALLERS)


@pytest.mark.parametrize("evaluator", GATED_EVALUATORS)
def test_THE_REQUEST_ENDPOINT_REALLY_DOES_CALL_IT(evaluator):
    """The other half, so the test above cannot pass by the evaluator being dead code."""
    assert evaluator in _calls_in(SRC / "api/routes/feature_execution.py")


def test_NEITHER_EVALUATOR_IS_IN_A_RELAY_ROUTE_MAP():
    """The worker's relay maps topics to HANDLERS. A verification or publication handler there would
    mean an outbox event could execute against a cluster with nobody asking."""
    from featuregen.runtime.worker import _DEFAULT_RELAY_ROUTE

    handlers = set(_DEFAULT_RELAY_ROUTE.values())
    for handler in handlers:
        assert "verif" not in handler.lower(), handler
        assert "publi" not in handler.lower(), handler


def test_NEITHER_EVALUATOR_IS_A_CONTROL_SIGNAL_HANDLER():
    """The other automatic queue path: `CONTROL_SIGNAL_HANDLERS` is polled by a dedicated loop."""
    from featuregen.runtime.queue import CONTROL_SIGNAL_HANDLERS

    for handler in CONTROL_SIGNAL_HANDLERS:
        assert "verif" not in handler.lower(), handler
        assert "publi" not in handler.lower(), handler


def test_THE_WORKER_MODULE_NEVER_NAMES_EITHER_EVALUATOR():
    """The timer half, and the strongest form available: the worker drives every automatic stage —
    relay, timers, projections, pollers — so an evaluator it cannot even name is one no timer can
    reach."""
    called = _calls_in(SRC / "runtime/worker.py")
    text = (SRC / "runtime/worker.py").read_text()
    for evaluator in GATED_EVALUATORS:
        assert evaluator not in called
        assert evaluator not in text


def test_NO_TIMER_KIND_NAMES_VERIFICATION_OR_PUBLICATION():
    """Timers fire by KIND. A kind naming either action would be an automatic trigger under another
    name."""
    timers = (SRC / "runtime/timers.py").read_text().lower()
    for phrase in ("evaluate_verify", "evaluate_publish_sandbox"):
        assert phrase not in timers, phrase


# ══ the route table itself ═════════════════════════════════════════════════════════════════════
def _paths(app) -> set[str]:
    """Every registered path, through FastAPI's lazy inclusion wrapper.

    `app.routes` holds `_IncludedRouter` placeholders rather than the routes themselves in this
    FastAPI version, so a naive walk finds only the six app-level routes and would report the whole
    surface as absent — a test that passed for the wrong reason.
    """
    found: set[str] = set()
    for route in app.routes:
        if hasattr(route, "path"):
            found.add(route.path)
        if hasattr(route, "effective_candidates"):
            for candidate in route.effective_candidates():
                path = getattr(candidate, "path", None)
                if path is None:
                    path = getattr(getattr(candidate, "route", None), "path", None)
                if path is not None:
                    found.add(path)
    return found


def test_THE_SIX_ROUTES_ARE_REGISTERED(monkeypatch):
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_ENABLED", "1")
    from featuregen.api.app import create_app

    paths = {path for path in _paths(create_app()) if "feature-execution" in path}
    assert paths == {
        "/feature-execution/generations",
        "/feature-execution/publications",
        "/feature-execution/verifications",
        "/feature-execution/verifications/{verification_id}",
        "/feature-execution/{artifact_id}/code",
        "/feature-execution/{artifact_id}/verify-eligibility",
        # Step 7 (§9): the two PRODUCTION acts — real endpoints that refuse honestly under the
        # development policy — and the pointer read ("what is actually out there right now").
        "/feature-execution/production-materializations",
        "/feature-execution/production-publications",
        "/feature-execution/production-active",
    }


def test_THE_ROUTE_MODULE_NEVER_RUNS_THE_CHAIN():
    """A route holds ONE transaction for the whole request; a compile is bounded in minutes. The
    same assertion `materialization_runs` carries, over this module."""
    called = _calls_in(SRC / "api/routes/feature_execution.py")
    for forbidden in ("compile_feature_group", "compile_ir", "seal_v2", "render_project",
                      "submit"):
        assert forbidden not in called, forbidden


def test_the_evaluate_endpoint_is_a_GET_and_mints_nothing():
    """"May I verify this?" is a question a workspace asks to decide whether to enable a button.
    A POST that recorded an attempt every time a screen rendered would fill the history with things
    nobody did."""
    tree = ast.parse((SRC / "api/routes/feature_execution.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "verify_eligibility":
            decorators = [ast.unparse(d) for d in node.decorator_list]
            assert any("router.get" in d for d in decorators), decorators
            body = ast.unparse(node)
            assert "record_" not in body and "INSERT" not in body
            return
    raise AssertionError("verify_eligibility not found")


# ══ the ASYMMETRY §0.3 requires, expressed at the wire ═════════════════════════════════════════
def test_THE_VERIFY_BODY_HAS_NO_CAPABILITY_FIELD():
    """Verification must not require a publication capability, so there is nowhere for a caller to
    supply one. Absence, not a default."""
    from featuregen.api.routes.feature_execution import VerificationRequestIn

    fields = set(VerificationRequestIn.model_fields)
    assert not any("capab" in name for name in fields), sorted(fields)


def test_THE_PUBLISH_BODY_REQUIRES_ONE():
    """The other side of the asymmetry — it is the single thing separating the two actions."""
    from featuregen.api.routes.feature_execution import PublicationRequestIn

    field = PublicationRequestIn.model_fields["capability_attestation"]
    assert field.is_required()


def test_evaluate_verify_TAKES_NO_CAPABILITY_ARGUMENT():
    import inspect

    parameters = set(inspect.signature(evaluate_execution.evaluate_verify).parameters)
    assert not any("capab" in name or "publi" in name for name in parameters), sorted(parameters)


# ══ the evaluators' own decisions ══════════════════════════════════════════════════════════════
def test_VERIFY_REFUSES_AN_ARTIFACT_THAT_DOES_NOT_EXIST(db):
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-never-sealed",
        inventory_observation_id=_observation(db, "obs-1", "hdfc-local"),
        execution_permitted=True, activation_blockers=())
    assert verdict.allowed is False
    assert verdict.blockers == (R.ARTIFACT_NOT_SERVABLE,)
    assert verdict.action is EvaluatorAction.VERIFY


def test_VERIFY_REFUSES_A_REFUSED_ARTIFACT(db):
    """A refused artifact is recorded WITH its findings precisely so nothing later mistakes it for a
    servable one. Verifying it would produce a passing verification for a computation nobody was
    willing to seal."""
    _seal(db, servable=False)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1",
        inventory_observation_id=_observation(db, "obs-1", "hdfc-local"),
        execution_permitted=True, activation_blockers=())
    assert verdict.blockers == (R.ARTIFACT_NOT_SERVABLE,)


def test_VERIFY_REFUSES_THE_WRONG_ENVIRONMENT(db):
    """Environment is deployment placement, so this is the wrong ARTIFACT rather than an
    under-configured run."""
    _seal(db)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1",
        # An observation that genuinely belongs to another cluster — not a string the caller chose.
        inventory_observation_id=_observation(db, "obs-prod", "hdfc-prod"),
        execution_permitted=True, activation_blockers=())
    assert verdict.blockers == (R.ENVIRONMENT_INCOMPATIBLE,)


def test_VERIFY_REFUSES_AN_OBSERVATION_THAT_DOES_NOT_EXIST(db):
    """It used to be checked for emptiness and then ignored, so "environment compatibility" was a
    comparison with nothing on one side: an id naming no row reached the same verdict as a correct
    one. From this gate's side "never observed" and "a different cluster" are the same answer —
    nothing establishes that this artifact can run where you are pointing it."""
    _seal(db)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1", inventory_observation_id="obs-imaginary",
        execution_permitted=True, activation_blockers=())
    assert verdict.blockers == (R.ENVIRONMENT_INCOMPATIBLE,)


def test_a_REFUSED_ARTIFACT_does_not_ALSO_report_an_environment_mismatch(db):
    """Reporting both would tell an operator to fix an environment mismatch on an artifact that
    could not run in either."""
    _seal(db, servable=False)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1",
        inventory_observation_id=_observation(db, "obs-prod", "hdfc-prod"),
        execution_permitted=True, activation_blockers=())
    assert verdict.blockers == (R.ARTIFACT_NOT_SERVABLE,)


def test_VERIFY_REFUSES_WITHOUT_EXECUTION_PERMISSION(db):
    _seal(db)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1",
        inventory_observation_id=_observation(db, "obs-1", "hdfc-local"),
        execution_permitted=False, activation_blockers=())
    assert verdict.blockers == (R.EXECUTION_AUTHORITY_UNMET,)


def test_VERIFY_ALLOWS_A_SERVABLE_ARTIFACT_IN_ITS_OWN_ENVIRONMENT(db):
    _seal(db)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1",
        # The observation belongs to the SAME cluster the artifact was sealed for. Both sides of the
        # comparison are now loaded, so this passes for the right reason rather than because the
        # caller supplied the artifact's own environment back to it.
        inventory_observation_id=_observation(db, "obs-1", "hdfc-local"),
        execution_permitted=True, activation_blockers=())
    assert verdict.allowed is True


@pytest.mark.parametrize("staleness", [StalenessV1.STALE, StalenessV1.NEITHER])
def test_PUBLISH_REFUSES_A_VERIFICATION_THAT_IS_NOT_CURRENT(db, staleness):
    """Both non-current answers refuse, for different reasons the code's text carries: a stale one
    vouches for an artifact whose meaning moved, an unverifiable one never vouched on content."""
    path = _verify(db)
    verdict = evaluate_execution.evaluate_publish_sandbox(
        db, verified_output_revision_id="vo-1", staging_path=path, staleness=staleness,
        publication_permitted=True, capability_attestation="cap:publisher", activation_blockers=())
    assert verdict.blockers == (R.VERIFICATION_NOT_CURRENT,)


def test_PUBLISH_REFUSES_THE_WRONG_STAGING_PATH(db):
    """§0.3 asks for THE EXACT staged output, and two attempts deliberately do not share a path."""
    _verify(db)
    verdict = evaluate_execution.evaluate_publish_sandbox(
        db, verified_output_revision_id="vo-1", staging_path="hdfs://nn/somewhere-else",
        staleness=StalenessV1.CURRENT, publication_permitted=True,
        capability_attestation="cap:publisher", activation_blockers=())
    assert verdict.blockers == (R.VERIFICATION_NOT_CURRENT,)


def test_PUBLISH_REFUSES_WITHOUT_A_CAPABILITY(db):
    path = _verify(db)
    verdict = evaluate_execution.evaluate_publish_sandbox(
        db, verified_output_revision_id="vo-1", staging_path=path,
        staleness=StalenessV1.CURRENT, publication_permitted=True, capability_attestation="  ", activation_blockers=())
    assert verdict.blockers == (R.PUBLICATION_CAPABILITY_MISSING,)


def test_PUBLISH_ALLOWS_a_current_verification_with_permission_and_capability(db):
    path = _verify(db)
    verdict = evaluate_execution.evaluate_publish_sandbox(
        db, verified_output_revision_id="vo-1", staging_path=path,
        staleness=StalenessV1.CURRENT, publication_permitted=True,
        capability_attestation="cap:publisher", activation_blockers=())
    assert verdict.allowed is True
    assert verdict.action is EvaluatorAction.PUBLISH_SANDBOX


def test_a_SWEPT_output_cannot_be_published(db):
    """A swept output's bytes are gone."""
    from featuregen.overlay.upload.verification_revisions import RetentionStateV1
    from featuregen.overlay.upload.verification_store import set_retention_state

    path = _verify(db)
    set_retention_state(db, "vo-1", RetentionStateV1.SWEPT)
    verdict = evaluate_execution.evaluate_publish_sandbox(
        db, verified_output_revision_id="vo-1", staging_path=path,
        staleness=StalenessV1.CURRENT, publication_permitted=True,
        capability_attestation="cap:publisher", activation_blockers=())
    assert verdict.blockers == (R.VERIFICATION_NOT_CURRENT,)


# ══ fixtures ═══════════════════════════════════════════════════════════════════════════════════
def _seal(db, *, servable: bool = True):
    from tests.featuregen.materialize.provenance_fixtures import evidenced_members
    from tests.featuregen.materialize.test_subgraph_requirements_v2 import _fx_chain

    from featuregen.materialize.artifact_manifest import manifest_for
    from featuregen.materialize.artifact_store import content_reference_for
    from featuregen.materialize.seal_v2 import RealizationLinkV1, seal_v2

    files = {"conf/base/catalog.yml": "x: 1\n"}
    manifest = manifest_for("art-1", files,
                            content_reference=lambda path: content_reference_for(files[path]))
    return seal_v2(
        db, _fx_chain(duplicate_gate=servable), manifest, files,
        environment_id="hdfc-local", logical_group_name="customer_txn_features",
        compilation_identity_hash="sha256:c", group_plan_hash="sha256:p",
        project_digest="sha256:d",
        realizations=(RealizationLinkV1(revision_id="rev-1", occurrence_hash="occ-1"),),
        # Sealing now records HOW each published column was authored, derived from a real run's
        # evidence. `_fx_chain` publishes one column, `f`.
        member_provenance=evidenced_members(db, "f", run_prefix="far-s11"),
        sealed_at="2026-08-17T00:00:00Z",
        generation_authorization_revision_id=_seal_approval(db))


def _observation(db, observation_id: str, environment: str) -> str:
    """A real inventory observation belonging to `environment`.

    The verify gate LOADS this now instead of taking an `environment_id` argument. A test that
    wanted "the wrong environment" used to pass a different string; it now has to seed an
    observation that genuinely belongs to another cluster — which is the whole point, because a
    caller could previously assert the environment the comparison was about.
    """
    db.execute(
        "INSERT INTO generation_inventory_observation (observation_id, environment_id, "
        "inventory_json, used_schema_refs, read_set, content_hash, captured_at) "
        "VALUES (%s,%s,'{}'::jsonb,'[]'::jsonb,'[]'::jsonb,%s,'t') ON CONFLICT DO NOTHING",
        (observation_id, environment, observation_id))
    return observation_id


def _seal_approval(db) -> str:
    """The approval this artifact was produced under — mandatory since the referential chain.

    An artifact that cannot name the approval that produced it leaves "which approval produced this"
    answerable only by matching loose fields, which is the gap the chain closes.
    """
    from featuregen.materialize.generation_authorization import (
        GenerationAuthorizationV1,
        record_generation_authorization,
    )
    from featuregen.overlay.upload.selection_revisions import TargetModeV1

    db.execute("INSERT INTO contract_intent (intent_id, hypothesis, intake_mode, "
               "redacted_hypothesis) VALUES ('int-s11','h','hypothesis','h') "
               "ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO target_reading_revision (revision_id, intent_id, mode, content_hash) "
               "VALUES ('trr-s11','int-s11','exploration','h') ON CONFLICT DO NOTHING")
    db.execute("INSERT INTO build_set_revision (revision_id, target_reading_revision_id, "
               "declaration_hash, declaration_json, content_hash, declared_by, declared_at) "
               "VALUES ('bs-s11','trr-s11','dh','{}'::jsonb,'ch','user:ops','t') "
               "ON CONFLICT DO NOTHING")
    return record_generation_authorization(
        db, GenerationAuthorizationV1(
            environment_id="hdfc-local", logical_group_name="customer_txn_features",
            build_set_revision_id="bs-s11",
            target_mode=TargetModeV1.EXPLORATION, target_ref=None),
        authorized_by="user:ops", authorized_at="t")


def _verify(db) -> str:
    """One recorded attempt and its verified output. Returns the staging path."""
    from featuregen.overlay.upload.verification_revisions import (
        VerificationExecutionIdentityV1,
        VerifiedOutputRevisionV1,
    )
    from featuregen.overlay.upload.verification_store import (
        record_verification_attempt,
        record_verified_output,
    )

    identity = VerificationExecutionIdentityV1(
        generation_authorization_revision_id="gar-1", check_set_hash="sha256:cs",
        inventory_observation_id="obs-1", attempt=1)
    execution_hash = record_verification_attempt(
        db, identity, sealed_artifact_id="art-1", staging_root="hdfs://nn/staging",
        started_at="t0")
    record_verified_output(
        db,
        VerifiedOutputRevisionV1(
            revision_id="vo-1", execution_hash=execution_hash, check_set_hash="sha256:cs",
            validator_versions=(("schema", "1"),), pinned_policy_hashes=("sha256:policy",),
            input_observation_strength="observed"),
        reads_enforced=False)
    return identity.staging_path("hdfs://nn/staging")


# ══ ONE AUTHORIZATION VOCABULARY ════════════════════════════════════════════════════════════════
def test_A_FEATURE_ENGINEER_REACHES_THE_EXECUTION_ROUTES(client, conn, monkeypatch):
    """The role that BUILDS features can reach the routes that build features.

    Found by review. Every route here demanded the raw hyphenated `platform-admin` claim through
    `require_confirmer` — the DUAL-OWNER GOVERNANCE gate, which is a different question entirely —
    while `_may_execute` inside accepted `feature_engineer`. So a feature engineer was rejected by
    the guard before the function that would have authorised them ever ran, and the narrowest of the
    three vocabularies won by accident of ordering.

    Asserted as NOT-403 rather than 200: what these routes answer depends on stored state this test
    does not build. The bug was never about the answer, it was about never being allowed to ask.
    """
    monkeypatch.setenv("FEATUREGEN_MATERIALIZE_ENABLED", "1")
    engineer = {"X-User": "sam", "X-Roles": "feature_engineer"}

    for method, path in (
        ("get", "/feature-execution/art-1/code"),
        ("get", "/feature-execution/art-1/verify-eligibility"
                "?inventory_observation_id=inv-1&environment_id=env-1"),
        ("get", "/feature-execution/verifications/exec-1"),
        ("get", "/feature-execution/publications?environment_id=e&logical_group_name=g"),
    ):
        response = getattr(client, method)(path, headers=engineer)
        assert response.status_code != 403, f"{method.upper()} {path}: {response.text[:200]}"


def test_the_guard_and_the_authorizer_ask_THE_SAME_QUESTION():
    """They used to disagree, and the disagreement was invisible because each looked right alone.

    `_may_execute` now asks for the same PERMISSION the route guard asks for, so the two cannot
    drift into the state where one admits a caller the other rejects.
    """
    from tests.featuregen._helpers import mint_test_identity

    from featuregen.api.routes.feature_execution import _may_execute
    from featuregen.identity.permissions import FEATURE_GENERATE, ROLE_PERMISSIONS

    for role, permissions in ROLE_PERMISSIONS.items():
        identity = mint_test_identity(subject="user:x", role_claims=(role,))
        assert _may_execute(identity) == (FEATURE_GENERATE in permissions), role


def test_PUBLISHING_STAYS_NARROWER_THAN_EXECUTING():
    """The asymmetry is deliberate: publication makes a number visible to everyone downstream.

    A feature engineer may execute and may NOT publish — and now learns that by name from the
    governed refusal, having reached the route, rather than from a bare 403 that says only that
    something somewhere said no.
    """
    from tests.featuregen._helpers import mint_test_identity

    from featuregen.api.routes.feature_execution import _may_execute, _may_publish

    engineer = mint_test_identity(subject="user:e", role_claims=("feature_engineer",))
    admin = mint_test_identity(subject="user:a", role_claims=("platform_admin",))

    assert _may_execute(engineer) and not _may_publish(engineer)
    assert _may_execute(admin) and _may_publish(admin)


# ══ STALENESS IS COMPARED, NOT ASSUMED ══════════════════════════════════════════════════════════
def _pinned_output(conn, revision_id: str, pinned: list[str]) -> None:
    """A passed verification pinning some policy realizations.

    Real rows through the real schema, including the attempt the output points at — the CHECKs and
    the foreign key are part of what makes this a verified output rather than a shape, and a fake
    connection would prove only that the function reads what a fake handed it.
    """
    import json

    conn.execute(
        "INSERT INTO verification_attempt (execution_hash, generation_authorization_revision_id, "
        "check_set_hash, inventory_observation_id, attempt, run_parameters, staging_path, "
        "sealed_artifact_id, started_at) VALUES (%s,'gar-1','cs-1','inv-1',1,'{}'::jsonb,%s,"
        "'art-1','2026-08-18T00:00:00Z') ON CONFLICT DO NOTHING",
        (f"exec-{revision_id}", f"hdfs://nn/staging/{revision_id}"))
    conn.execute(
        "INSERT INTO verified_output_revision (revision_id, execution_hash, check_set_hash, "
        "validator_versions, pinned_policy_hashes, input_observation_strength, reads_enforced, "
        "retention_state) VALUES (%s,%s,'cs-1','[]'::jsonb,%s::jsonb,'pinned',true,'live')",
        (revision_id, f"exec-{revision_id}", json.dumps(pinned)))


def _make_current(conn, revision_id: str, family: str) -> None:
    """Record one policy realization as CURRENT for its family."""
    conn.execute(
        "INSERT INTO policy_realization_revision (revision_id, family_key_hash, policy_kind, "
        "policy_ref, bound_dataset, environment_id, semantic_role, executable_content_hash, "
        "cas_pointer, provenance) VALUES (%s,%s,'fx','ref','ds','env','role','ech','cas',"
        "'source_derived') ON CONFLICT DO NOTHING", (revision_id, family))
    conn.execute(
        "INSERT INTO policy_realization_current (family_key_hash, revision_id, pointer_version, "
        "declared_by) VALUES (%s,%s,1,'user:test') "
        "ON CONFLICT (family_key_hash) DO UPDATE SET revision_id = EXCLUDED.revision_id",
        (family, revision_id))


def test_STALENESS_IS_UNVERIFIABLE_WHEN_NOTHING_IS_RECORDED_AS_CURRENT(conn):
    """The live state today, and the one this function got wrong.

    An earlier version returned CURRENT for every pinned output without comparing anything. Policy
    drift after a verification is precisely what this answers, so a bare CURRENT told an operator
    the verification still held when nobody had checked. With no realization pointers recorded,
    "has it moved?" has no answer — and NEITHER is this vocabulary's word for that.
    """
    from featuregen.api.routes.feature_execution import _staleness_of
    from featuregen.overlay.upload.verification_store import StalenessV1

    _pinned_output(conn, "vor-unknown", ["pol-a"])
    assert conn.execute("SELECT count(*) FROM policy_realization_current").fetchone()[0] == 0

    verdict = _staleness_of(conn, "vor-unknown")
    assert verdict is StalenessV1.NEITHER
    assert verdict.is_unverifiable, "the surfaces render this flag; it must say unverifiable"


def test_a_pinned_policy_that_is_no_longer_current_is_STALE(conn):
    """Drift is what makes a passed verification untrue, and one moved policy is enough."""
    from featuregen.api.routes.feature_execution import _staleness_of
    from featuregen.overlay.upload.verification_store import StalenessV1

    _pinned_output(conn, "vor-drifted", ["pol-old"])
    _make_current(conn, "pol-new", "family-1")     # something IS current — just not what it pinned

    assert _staleness_of(conn, "vor-drifted") is StalenessV1.STALE


def test_a_pinned_policy_that_is_still_current_is_CURRENT(conn):
    """The positive answer, now earned by a comparison rather than assumed."""
    from featuregen.api.routes.feature_execution import _staleness_of
    from featuregen.overlay.upload.verification_store import StalenessV1

    _pinned_output(conn, "vor-fresh", ["pol-a"])
    _make_current(conn, "pol-a", "family-1")

    assert _staleness_of(conn, "vor-fresh") is StalenessV1.CURRENT


def test_ONE_DRIFTED_POLICY_AMONG_MANY_IS_ENOUGH(conn):
    """A verification is untrue if ANY policy it depended on moved — not most of them."""
    from featuregen.api.routes.feature_execution import _staleness_of
    from featuregen.overlay.upload.verification_store import StalenessV1

    _pinned_output(conn, "vor-mixed", ["pol-a", "pol-b"])
    _make_current(conn, "pol-a", "family-1")
    _make_current(conn, "pol-moved", "family-2")   # pol-b is no longer current for its family

    assert _staleness_of(conn, "vor-mixed") is StalenessV1.STALE
