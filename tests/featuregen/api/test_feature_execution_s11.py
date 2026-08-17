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
        "/feature-execution/verifications/{execution_hash}",
        "/feature-execution/{artifact_id}/code",
        "/feature-execution/{artifact_id}/verify-eligibility",
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
        db, sealed_artifact_hash="art-never-sealed", inventory_observation_id="obs-1",
        environment_id="hdfc-local", execution_permitted=True)
    assert verdict.allowed is False
    assert verdict.blockers == (R.ARTIFACT_NOT_SERVABLE,)
    assert verdict.action is EvaluatorAction.VERIFY


def test_VERIFY_REFUSES_A_REFUSED_ARTIFACT(db):
    """A refused artifact is recorded WITH its findings precisely so nothing later mistakes it for a
    servable one. Verifying it would produce a passing verification for a computation nobody was
    willing to seal."""
    _seal(db, servable=False)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1", inventory_observation_id="obs-1",
        environment_id="hdfc-local", execution_permitted=True)
    assert verdict.blockers == (R.ARTIFACT_NOT_SERVABLE,)


def test_VERIFY_REFUSES_THE_WRONG_ENVIRONMENT(db):
    """Environment is deployment placement, so this is the wrong ARTIFACT rather than an
    under-configured run."""
    _seal(db)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1", inventory_observation_id="obs-1",
        environment_id="hdfc-prod", execution_permitted=True)
    assert verdict.blockers == (R.ENVIRONMENT_INCOMPATIBLE,)


def test_a_REFUSED_ARTIFACT_does_not_ALSO_report_an_environment_mismatch(db):
    """Reporting both would tell an operator to fix an environment mismatch on an artifact that
    could not run in either."""
    _seal(db, servable=False)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1", inventory_observation_id="obs-1",
        environment_id="hdfc-prod", execution_permitted=True)
    assert verdict.blockers == (R.ARTIFACT_NOT_SERVABLE,)


def test_VERIFY_REFUSES_WITHOUT_EXECUTION_PERMISSION(db):
    _seal(db)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1", inventory_observation_id="obs-1",
        environment_id="hdfc-local", execution_permitted=False)
    assert verdict.blockers == (R.EXECUTION_AUTHORITY_UNMET,)


def test_VERIFY_ALLOWS_A_SERVABLE_ARTIFACT_IN_ITS_OWN_ENVIRONMENT(db):
    _seal(db)
    verdict = evaluate_execution.evaluate_verify(
        db, sealed_artifact_hash="art-1", inventory_observation_id="obs-1",
        environment_id="hdfc-local", execution_permitted=True)
    assert verdict.allowed is True


@pytest.mark.parametrize("staleness", [StalenessV1.STALE, StalenessV1.NEITHER])
def test_PUBLISH_REFUSES_A_VERIFICATION_THAT_IS_NOT_CURRENT(db, staleness):
    """Both non-current answers refuse, for different reasons the code's text carries: a stale one
    vouches for an artifact whose meaning moved, an unverifiable one never vouched on content."""
    path = _verify(db)
    verdict = evaluate_execution.evaluate_publish_sandbox(
        db, verified_output_revision_id="vo-1", staging_path=path, staleness=staleness,
        publication_permitted=True, capability_attestation="cap:publisher")
    assert verdict.blockers == (R.VERIFICATION_NOT_CURRENT,)


def test_PUBLISH_REFUSES_THE_WRONG_STAGING_PATH(db):
    """§0.3 asks for THE EXACT staged output, and two attempts deliberately do not share a path."""
    _verify(db)
    verdict = evaluate_execution.evaluate_publish_sandbox(
        db, verified_output_revision_id="vo-1", staging_path="hdfs://nn/somewhere-else",
        staleness=StalenessV1.CURRENT, publication_permitted=True,
        capability_attestation="cap:publisher")
    assert verdict.blockers == (R.VERIFICATION_NOT_CURRENT,)


def test_PUBLISH_REFUSES_WITHOUT_A_CAPABILITY(db):
    path = _verify(db)
    verdict = evaluate_execution.evaluate_publish_sandbox(
        db, verified_output_revision_id="vo-1", staging_path=path,
        staleness=StalenessV1.CURRENT, publication_permitted=True, capability_attestation="  ")
    assert verdict.blockers == (R.PUBLICATION_CAPABILITY_MISSING,)


def test_PUBLISH_ALLOWS_a_current_verification_with_permission_and_capability(db):
    path = _verify(db)
    verdict = evaluate_execution.evaluate_publish_sandbox(
        db, verified_output_revision_id="vo-1", staging_path=path,
        staleness=StalenessV1.CURRENT, publication_permitted=True,
        capability_attestation="cap:publisher")
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
        capability_attestation="cap:publisher")
    assert verdict.blockers == (R.VERIFICATION_NOT_CURRENT,)


# ══ fixtures ═══════════════════════════════════════════════════════════════════════════════════
def _seal(db, *, servable: bool = True):
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
        sealed_at="2026-08-17T00:00:00Z")


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
