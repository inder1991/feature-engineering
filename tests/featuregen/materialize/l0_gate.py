"""The L0 GATE — the real generated project, imported and built in a real Spark environment.

**Not named ``test_*`` on purpose, so the main suite never collects it.** ``pyspark`` and ``kedro``
are not dependencies of this platform, and ``src/`` never imports either; a suite that pulled them
in would be pinning the validator to the versions of the artifact it validates, and would trade the
whole suite's ~4s for a JVM-capable interpreter. Run it explicitly::

    FEATUREGEN_L0_PYTHON=$PWD/.venv-artifact/bin/python \\
    PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/featuregen/materialize/l0_gate.py -q

pytest collects a file given by path regardless of its name, which is what makes the split work
without a marker or a config change.

**What the main suite proves and what only this can.** The suite proves every branch of
:func:`~featuregen.materialize.validation.run_l0` — that a project which parses and yields no
pipeline is ``PIPELINE_NOT_CONSTRUCTIBLE``, that a failed import is ``PROJECT_DOES_NOT_BUILD``, that
a hand edit is ``PROJECT_HASH_MISMATCH``, that an unreachable environment invents nothing — against
hand-authored stdlib-only projects. What it cannot prove is that the RENDERED project is one of the
ones that builds: that needs kedro, pyspark and a JVM, and it is what this file is.

**The environments, and why there is more than one.** ``make l0-gate`` builds two and runs this file
under both, because they answer different questions:

* ``.venv-artifact`` — installed FROM the golden project's own ``requirements.lock``
  (kedro 0.19.9, kedro-datasets 4.1.0, pyspark 3.5.1). This is *the artifact's declared environment*,
  and it is the only one in which a passing build proof is a proof about **this** project.
* ``.venv-l0-modern`` — kedro 1.5.0, kedro-datasets 9.5.0, pyspark 4.2.0. The forward-looking line.

Both get ``hdfs`` (and ``s3fs`` on the 4.x line) as GATE-environment dependencies, for DEFERRED-WORK
A.32's reason, plus Temurin 17. Both ``PYSPARK_PYTHON`` and ``PYSPARK_DRIVER_PYTHON`` are exported by
the Makefile — without them Spark launches workers on the system Python and pyspark's own
``types.py`` dies on ``X | Y``.

A third environment now exists and is NOT one of these: ``/opt/kedro-venv`` in the kind backend image
(``deploy/kind/Dockerfile.backend``), at kedro 1.5.0 / kedro-datasets 9.5.0 / pyspark 3.5.3. It is
what ``FEATUREGEN_MATERIALIZE_L0_PYTHON`` points at in a deployment, so it is the environment that
matters in production — and it matches neither venv above.

**Only one of the three can carry a build proof, and since DEFERRED-WORK A.42 closed, `run_l0` is
what says so.** The rendered project pins itself, L0 compares those pins against the probe
interpreter's installed distributions, and a disagreement is
``FAILED``/``ENGINE_VERSION_MISMATCH`` rather than a build verdict. So under ``.venv-l0-modern`` and
under the kind image there is no build verdict for this file to assert, and the tests that assert
one take :func:`the_declared_environment` and SKIP with the disagreement named.
:func:`test_the_environment_really_has_the_engines_the_project_pins` takes no such fixture: it runs
under every interpreter and asserts whichever verdict is correct there.

One caveat, because the fixture is honest about it and this docstring previously was not: when NO
L0 interpreter resolves at all — no ``FEATUREGEN_L0_PYTHON`` and none of the three venvs on disk —
:func:`l0_python` skips the whole file and the gate states nothing whatsoever. That is the right
behaviour (it must never fake an environment), but "the gate always states the answer" is true only
once an interpreter is found.
"""
from __future__ import annotations

import dataclasses
import json
import os
import pathlib
import subprocess

import pytest
from tests.featuregen.materialize import fixtures
from tests.featuregen.materialize import test_chain as chain_tests
from tests.featuregen.materialize.test_group_plan import catalog  # noqa: F401 - a fixture
from tests.featuregen.materialize.test_render_project import (  # noqa: F401 - `project` fixture
    _render,
    project,
)
from tests.featuregen.materialize.test_resolve import no_dsn  # noqa: F401 - autouse, see below

from featuregen.materialize.codes import PublicationRefusalCode, ValidationFindingCode
from featuregen.materialize.compile.chain import ChainStage, L0Interpreter
from featuregen.materialize.control_plane import RunEventKind, RunStatus, run_status
from featuregen.materialize.identity import (
    REQUIREMENTS_LOCK_FILENAME,
    CompilationIdentity,
    SealedProject,
    seal_project,
)
from featuregen.materialize.publish import read_active_revision
from featuregen.materialize.render.project import (
    PIPELINE_NAME,
    REQUIRED_RUN_PARAMETERS,
    materialize_to,
)
from featuregen.materialize.request_store import RequestLifecycle
from featuregen.materialize.submit import LocalClusterSubmitter, submission_command
from featuregen.materialize.validation import (
    ValidationStatus,
    read_validation_reports,
    run_l0,
)

GEN = "gen-l0-gate"
ENVIRONMENT = "hdfc-local"
T0 = "2026-07-28T09:00:00+00:00"
T1 = "2026-07-28T09:02:00+00:00"


def _clock():
    stamps = iter((T0, T1))
    return lambda: next(stamps)


@pytest.fixture(scope="session")
def l0_python() -> str:
    """The interpreter that HAS kedro and pyspark. Skipped, never faked, when it is absent."""
    named = os.environ.get("FEATUREGEN_L0_PYTHON")
    candidates = [named] if named else []
    here = pathlib.Path(__file__).resolve()
    # The two names `make l0-gate` actually builds, artifact line FIRST — it is the environment the
    # rendered project declares, so it is the one a bare `pytest l0_gate.py` should default to.
    # `.venv-l0` is kept last because it is the name this file used to document and a developer may
    # still have one lying around.
    candidates += [str(parent / venv / "bin" / "python")
                   for parent in here.parents
                   for venv in (".venv-artifact", ".venv-l0-modern", ".venv-l0")]
    for candidate in candidates:
        if candidate and pathlib.Path(candidate).is_file():
            return candidate
    pytest.skip("no L0 environment: set FEATUREGEN_L0_PYTHON to an interpreter with kedro+pyspark")


@pytest.fixture(scope="session")
def l0_env(l0_python: str) -> dict[str, str]:
    """`PYSPARK_PYTHON` and `PYSPARK_DRIVER_PYTHON` BOTH, which is the trap this fixture exists for."""
    environment = {"PYSPARK_PYTHON": l0_python, "PYSPARK_DRIVER_PYTHON": l0_python}
    java_home = os.environ.get("JAVA_HOME")
    if not java_home:
        found = subprocess.run(["/usr/libexec/java_home", "-v", "17"],  # noqa: S603,S607
                               capture_output=True, text=True, check=False)
        if found.returncode == 0:
            java_home = found.stdout.strip()
    if java_home:
        environment["JAVA_HOME"] = java_home
    return environment


#: The committed golden project — the renderer's own output, held byte-identical to it by
#: ``test_the_rendered_project_matches_its_GOLDENS`` in the collected suite. Sealing a copy of it
#: gives this file a real artifact to ask production about WITHOUT a db round trip, which is what
#: lets the skip decision below be a session-scoped `run_l0` rather than a parse.
GOLDENS = pathlib.Path(__file__).parent / "goldens" / "cif_daily"

#: Ask the interpreter what it actually has, keyed by DISTRIBUTION name — which is what a lock file
#: names, and what `kedro-datasets` vs the importable `kedro_datasets` would otherwise disagree
#: about. `importlib.metadata` reads the installed distribution rather than a module's
#: `__version__`, so a package shipping a stale or absent `__version__` cannot make it wrong.
#:
#: This is NOT the comparison — production's probe owns that (`validation._BUILD_PROBE`). It only
#: tells this file which verdict to demand of `run_l0`, and which environment the gate is in.
_INSTALLED_VERSIONS = (
    "import importlib.metadata as m, json, sys\n"
    "out = {}\n"
    "for dist in sys.argv[1:]:\n"
    "    try:\n"
    "        out[dist] = m.version(dist)\n"
    "    except m.PackageNotFoundError:\n"
    "        out[dist] = None\n"
    "print(json.dumps(out))\n"
)


def _disagreements(declared: dict[str, str], l0_python: str) -> dict[str, tuple[str, str | None]]:
    """Which declared pins this interpreter does not satisfy — ``{}`` when it is the declared one.

    **Used to WRITE an expectation, never to decide whether a test asserts.** That distinction is
    the whole of why this may exist beside production's parser: if it drifts from
    `validation._BUILD_PROBE`, the single test it feeds goes RED against the real report. It cannot
    turn a test into a silent skip — which is what an earlier draft did, by deciding
    `the_declared_environment` from a parse that stripped neither inline comments, extras nor
    environment markers where the probe strips all three.
    """
    asked = subprocess.run(  # noqa: S603
        [l0_python, "-c", _INSTALLED_VERSIONS, *sorted(declared)],
        capture_output=True, text=True, check=False)
    assert asked.returncode == 0, asked.stderr
    installed = json.loads(asked.stdout.strip().splitlines()[-1])
    return {dist: (want, installed.get(dist))
            for dist, want in sorted(declared.items())
            if installed.get(dist) != want}


@pytest.fixture(scope="session")
def engines_the_interpreter_lacks(l0_python: str, l0_env, tmp_path_factory) -> tuple:
    """What PRODUCTION says about this gate environment — one real `run_l0`, computed once.

    Not a parse. The golden tree is sealed and materialized into a real project and handed to the
    real `run_l0`; the answer is whichever `ENGINE_VERSION_MISMATCH` findings it reports. So the
    decision to skip a build proof is made by the same code that would refuse the build, and there
    is no second implementation of the comparison for it to drift from.

    Fails LOUD in every degenerate direction: if the interpreter cannot be launched the report is
    `ERROR` with no findings, so this returns `()` and nothing skips — the build tests then run and
    fail against the real environment rather than quietly reporting success by absence.
    """
    files = {str(path.relative_to(GOLDENS)): path.read_text(encoding="utf-8")
             for path in GOLDENS.rglob("*") if path.is_file()}
    identity = CompilationIdentity(
        formula_content_hashes=("f" * 64,), ir_hashes=("e" * 64,),
        materialization_contract_hash="c" * 64, group_plan_hash="1" * 64)
    root = materialize_to(seal_project(identity, files),
                          tmp_path_factory.mktemp("declared-environment") / "generated")
    report = run_l0(root, generation_id=GEN, environment_id=ENVIRONMENT,
                    report_id="rep-declared", python_executable=l0_python, clock=_clock(),
                    env=l0_env)
    return tuple(finding for finding in report.findings
                 if finding.code is ValidationFindingCode.ENGINE_VERSION_MISMATCH)


@pytest.fixture
def the_declared_environment(engines_the_interpreter_lacks) -> None:
    """Skip unless this interpreter IS the environment the artifact pins itself to.

    A build proof is a claim about ONE environment, and since DEFERRED-WORK **A.42** closed,
    `run_l0` refuses to make it anywhere else: under `.venv-l0-modern` and under the kind image the
    report is `FAILED` carrying `ENGINE_VERSION_MISMATCH`, and there is no build verdict inside it
    for a test to assert. A skip is therefore the accurate reading — *nothing to check here* — and
    not a tolerated red.

    A skip cannot hide a regression, because
    :func:`test_the_environment_really_has_the_engines_the_project_pins` takes no such fixture: it
    runs in every environment and asserts the production verdict either way.
    """
    if engines_the_interpreter_lacks:
        pytest.skip(
            "not the environment this artifact declares, so `run_l0` refuses to prove a build "
            "here (A.42): " + "; ".join(
                f"{finding.location} expected {finding.expected}, observed {finding.observed}"
                for finding in engines_the_interpreter_lacks))


def _declared_pins(sealed: SealedProject) -> dict[str, str]:
    """The artifact's OWN pins, parsed out of the file it ships them in.

    Read from the rendered bytes rather than from ``fixtures.ENGINE_VERSIONS`` on purpose: the
    fixture is what this suite happened to render with, and the lock is what the artifact SAYS. A
    test that compared the interpreter against the fixture would still pass if the renderer stopped
    writing the pins into the project at all.

    This is now used only to WRITE the expected-value table of the assertions below. The comparison
    itself is production's (`run_l0`), which is the whole of A.42's closure.
    """
    pins: dict[str, str] = {}
    for line in sealed.files[REQUIREMENTS_LOCK_FILENAME].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "==" not in stripped:
            continue
        name, _, version = stripped.partition("==")
        pins[name.strip()] = version.strip()
    return pins


def test_the_environment_really_has_the_engines_the_project_pins(
        project: SealedProject, tmp_path, l0_python: str, l0_env) -> None:  # noqa: F811
    """The interpreter's INSTALLED engines must equal the ones the rendered project pins itself to —
    and the thing that says so is now **`run_l0`**, not this test.

    **This test has lied once and re-implemented production once.** It first asserted
    `returncode == 0` on `import kedro, pyspark`, which compared nothing while its name promised the
    comparison every other claim in this file leans on. It was then made honest by parsing the
    rendered lock and diffing it against `importlib.metadata` here, in the test — true, but a second
    implementation of a comparison production did not have, so a green here proved nothing about a
    deployment. DEFERRED-WORK **A.42** is closed by moving that comparison into L0's build probe,
    and this test's job is now to assert the PRODUCTION verdict.

    So: one real `run_l0` against the real rendered artifact, and the pins decide the assertion.
    When they agree the report must be `PASSED` (and the build was therefore proved in the
    environment the artifact declares, which is the only environment a proof means anything in);
    when they disagree it must be `FAILED` carrying one `ENGINE_VERSION_MISMATCH` per disagreeing
    distribution, each naming its package, its pin and what is installed.

    **This is consequently GREEN in all three environments now, and the red it replaces has not
    been papered over — it has moved into the product.**

    ==============================  ========================  ==========================
    environment                     engines                   what `run_l0` now returns
    ==============================  ========================  ==========================
    ``.venv-artifact``              0.19.9 / 4.1.0 / 3.5.1    ``PASSED``
    ``.venv-l0-modern``             1.5.0 / 9.5.0 / 4.2.0     ``ENGINE_VERSION_MISMATCH``
    kind image ``/opt/kedro-venv``  1.5.0 / 9.5.0 / 3.5.3     ``ENGINE_VERSION_MISMATCH``
    ==============================  ========================  ==========================

    A disagreement is still a true and important statement about the environment — it says the L0
    interpreter is not the one the artifact declares — but it is no longer a *failing test*, because
    a deployment does not run this file. It is a governed finding on the report, classified
    `GOVERNED_FACT_MISMATCH`, recorded in `pipeline_validation_report`, and it stops the chain.
    """
    declared = _declared_pins(project)
    assert declared, "the rendered project shipped no parseable pins in requirements.lock"

    disagreements = _disagreements(declared, l0_python)

    root = materialize_to(project, tmp_path / "generated")
    report = run_l0(root, generation_id=GEN, environment_id=ENVIRONMENT, report_id="rep-engines",
                    python_executable=l0_python, clock=_clock(), env=l0_env)

    if not disagreements:
        assert (report.status, report.findings) == (ValidationStatus.PASSED, ()), \
            [finding.payload() for finding in report.findings]
        return

    assert report.status is ValidationStatus.FAILED, (
        f"the L0 interpreter {l0_python} is not the environment this artifact declares, and "
        f"`run_l0` did not say so — which is DEFERRED-WORK A.42 reopened: "
        + "; ".join(f"{dist} pinned {want}, installed {have}"
                    for dist, (want, have) in disagreements.items()))
    assert [(f.code, f.location, f.expected, f.observed) for f in report.findings] == [
        (ValidationFindingCode.ENGINE_VERSION_MISMATCH,
         f"{REQUIREMENTS_LOCK_FILENAME}:{dist}", f"{dist}=={want}",
         f"{dist} is not installed" if have is None else f"{dist}=={have}")
        for dist, (want, have) in sorted(disagreements.items())]


def test_the_RENDERED_project_builds_its_kedro_pipeline(
        the_declared_environment, project: SealedProject, tmp_path,  # noqa: F811
        l0_python: str, l0_env) -> None:
    """L0 over the real artifact: it hashes to its lock, imports, and yields a pipeline with nodes.

    This is the claim the main suite structurally cannot make, and the reason `PROJECT_DOES_NOT_BUILD`
    and `PIPELINE_NOT_CONSTRUCTIBLE` are not theoretical codes.

    `the_declared_environment` first: a PASSED report means "the build was proven", and since A.42
    closed that sentence is only available in the environment the artifact pins itself to.
    """
    root = materialize_to(project, tmp_path / "generated")
    report = run_l0(root, generation_id=GEN, environment_id=ENVIRONMENT, report_id="rep-gate",
                    python_executable=l0_python, clock=_clock(), env=l0_env)
    assert (report.status, report.findings) == (ValidationStatus.PASSED, ()), \
        [finding.payload() for finding in report.findings]


def test_a_hand_edit_of_the_RENDERED_project_is_caught_in_the_real_environment(
        the_declared_environment, project: SealedProject, tmp_path,  # noqa: F811
        l0_python: str, l0_env) -> None:
    """The same artifact, one comment added. Everything else is held equal.

    Held equal includes the ENGINES: the assertion is that the drift finding is the ONLY one, which
    is a statement about the hash check and is only readable where the engine check is silent.
    """
    root = materialize_to(project, tmp_path / "generated")
    readme = root / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n<!-- drift -->\n", encoding="utf-8")

    report = run_l0(root, generation_id=GEN, environment_id=ENVIRONMENT, report_id="rep-gate-2",
                    python_executable=l0_python, clock=_clock(), env=l0_env)
    assert report.status is ValidationStatus.FAILED
    assert [finding.code for finding in report.findings] == \
        [ValidationFindingCode.PROJECT_HASH_MISMATCH]


def test_the_CHAIN_ITSELF_seals_a_project_whose_build_is_PROVED(
        the_declared_environment, db, monkeypatch, tmp_path, l0_python: str, l0_env) -> None:
    """Phase G T6: L0 driven BY `compile_feature_group`, against a real kedro+pyspark interpreter.

    The collected suite proves the failing direction end to end — it runs the real `run_l0` against
    `sys.executable`, which has no kedro, and shows the run terminating `RUN_FAILED` with the build
    unproven. It structurally cannot prove the PASSING direction, because that needs the engines.
    This is the other half, and it is the claim the G-1 terminal now makes: *compiled, rendered,
    sealed, and proven to import and construct its pipeline in a separate interpreter.*

    Everything is the real thing — the seeded catalog, an authoring run written by the 1022
    orchestrator, the real assembler, the real `run_l0` — and the assertion is on the DURABLE
    record, read back through `read_validation_reports`, rather than on the object this process
    happened to hold. `no_dsn` is imported above because it is autouse in `test_resolve` and autouse
    applies per collected module; without it the authoring lane would look for a durable DSN.

    A generous timeout: importing pyspark and constructing the pipeline in a cold interpreter is
    seconds, but this gate also runs on machines that are paging the JVM in for the first time.
    """
    seeded = chain_tests._seed(db)
    request_id = chain_tests._request(seeded)
    work_items = [chain_tests._authored(seeded, monkeypatch)]

    outcome = chain_tests._run(
        seeded, request_id, work_items, tmp_path,
        l0=L0Interpreter(python_executable=l0_python, timeout_seconds=600.0, env=l0_env))

    assert outcome.validation_report is not None
    assert (outcome.validation_report.status, outcome.validation_report.findings) == \
        (ValidationStatus.PASSED, ()), \
        [finding.payload() for finding in outcome.validation_report.findings]

    stored = read_validation_reports(seeded, generation_id=outcome.generation_id)
    assert [(r.status, r.generated_project_hash) for r in stored] == \
        [(ValidationStatus.PASSED, outcome.generated_project_hash)]

    assert outcome.stopped_at is ChainStage.PUBLISHER
    assert outcome.terminal_event is RunEventKind.PUBLICATION_REFUSED
    assert outcome.refusal.code is PublicationRefusalCode.CAPABILITY_UNPROVEN
    assert outcome.lifecycle_state is RequestLifecycle.COMMITTED


def test_the_run_parameters_hook_fires_and_passes_inside_a_REAL_kedro_session(
        catalog, db, tmp_path, l0_python: str, l0_env) -> None:  # noqa: F811
    """Task 1 proved `RunParametersHook`'s refusal by reading a substring of the rendered bytes;
    this proves it by EXECUTION. A real `KedroSession` is created over the rendered project through
    the production run script (`submission_command`'s `_RUN_SCRIPT`, which asks the INSTALLED
    `KedroSession.create` signature whether the kwarg is `runtime_params` — kedro >= 1.0 — or
    `extra_params` — the 0.19.x line), and `session.run(pipeline_name=...)` is what fires the hook.

    The project is rendered with THIS gate environment's own installed versions, not the suite's
    fixture pins: §7 runs the artifact in the environment it was rendered FOR, and kedro's
    `bootstrap_project` enforces the `kedro_init_version` pin at session startup — a project
    pinned 0.19.9 refuses to even bootstrap under kedro 1.5.0, before any hook could fire. Asking
    the interpreter is exactly what a real `ClusterInventoryV1` capture of this venv-as-cluster
    would record.

    Two runs, one claim each, and the first is what keeps the second from passing vacuously:

    * MISSING a required parameter -> the run dies WITH ``RUN_PARAMETERS_MISSING``. The hook fires
      on this exact execution path in this exact environment. (`business_dt` is the one dropped
      because the rendered catalog interpolates only ``staging_root`` — removing THAT would abort
      config resolution before the hook ever ran, proving nothing about it.)
    * ALL prepared parameters -> whatever the environment then fails on (no `banking.*` tables, no
      cluster), the failure is NOT ``RUN_PARAMETERS_MISSING``. The hook fired and PASSED — a run
      that dies later is fine; a run refused by the gate is the regression this test exists for.
    """
    proved = subprocess.run(  # noqa: S603
        [l0_python, "-c",
         "import json, platform, kedro, kedro_datasets, pyspark; "
         "print(json.dumps({'kedro': kedro.__version__, "
         "'kedro_datasets': kedro_datasets.__version__, "
         "'pyspark': pyspark.__version__, 'python': platform.python_version()}))"],
        capture_output=True, text=True, check=False)
    assert proved.returncode == 0, proved.stderr
    installed = json.loads(proved.stdout.strip().splitlines()[-1])
    sealed = _render(db, engine_versions=dataclasses.replace(
        fixtures.ENGINE_VERSIONS, **installed))
    root = materialize_to(sealed, tmp_path / "generated")
    parameters = {
        "business_dt": "2026-07-28",
        "generation_id": GEN,
        "input_snapshots": [
            {"catalog_source": "hdfc", "object_ref": "banking.transactions",
             "snapshot_id": "snap-transactions-0001"}],
        "run_id": "run-l0-gate",
        "sandbox_execution_hash": "0" * 64,
        "staging_root": str(tmp_path / "staging"),
    }
    assert sorted(parameters) == sorted(REQUIRED_RUN_PARAMETERS)
    environment = {**os.environ, **l0_env}

    short = {name: value for name, value in parameters.items() if name != "business_dt"}
    refused = subprocess.run(  # noqa: S603 - fixed argv, the production submission command
        submission_command(l0_python, root, short, PIPELINE_NAME),
        capture_output=True, text=True, timeout=480, check=False, cwd=str(root), env=environment)
    assert refused.returncode != 0
    assert "RUN_PARAMETERS_MISSING" in refused.stdout + refused.stderr, \
        (refused.stdout[-2000:], refused.stderr[-2000:])

    completed = subprocess.run(  # noqa: S603 - fixed argv, the production submission command
        submission_command(l0_python, root, parameters, PIPELINE_NAME),
        capture_output=True, text=True, timeout=480, check=False, cwd=str(root), env=environment)
    assert "RUN_PARAMETERS_MISSING" not in completed.stdout + completed.stderr, \
        (completed.stdout[-2000:], completed.stderr[-2000:])


# ── Task E1 — §0.5 items 5–7: the governed contract MATERIALIZES ─────────────────────────────────
#
# E0 (`tests/featuregen/api/test_seam_walkthrough.py`) walks items 1–4 in the DEFAULT suite and
# stops exactly where the collected suite structurally must: it injects `run_l0`'s verdict, and it
# never executes the artifact. The two tests below are the other half, and they can only live here.
#
# WHAT IS REAL AND WHAT IS A TEST-SCOPED FAKE, stated once so neither is mistaken for the other.
# The project is compiled, rendered, sealed and materialized by the production chain; `run_l0` is
# the real one against a real kedro+pyspark interpreter; `LocalClusterSubmitter` is the production
# submitter and it really launches the rendered pipeline in that interpreter.
# `_G2Metastore` and `_Swap` are fakes DEFINED IN THE TESTS, and they are the honest vehicle here
# for a reason that is recorded in D1's and D3's acceptance rows: **no `MetastoreMetadata` and no
# `PublicationSwap` implementation exists anywhere in `src/`** — `lane_config_from_env` produces
# `metastore=None` and every deployed run is honestly unprepared. Writing those adapters is E2's
# deployment work against a real cluster; what this gate proves is that the CHAIN composes through
# their seams and publishes, on an artifact whose build is genuinely verified. Neither fake is
# importable from `src/`, and nothing here makes the platform claim a cluster it has not met.


def test_the_chain_SUBMITS_the_rendered_project_into_a_REAL_kedro_session(
        the_declared_environment, db, monkeypatch, tmp_path, l0_python: str, l0_env) -> None:
    """§0.5 item 5, as far as an environment with no cluster honestly allows: `prepare_run` →
    `run_l1` → `submit`, with the SUBMISSION really executed by the production submitter.

    `test_the_run_parameters_hook_fires_and_passes_inside_a_REAL_kedro_session` proves the hook
    fires when a caller assembles the parameters by hand. This proves the CHAIN's own
    `prepared.parameters` clear it — the parameters `prepare_run` resolved, handed to
    `LocalClusterSubmitter` through `RunExecution`, launched in the interpreter that has the
    engines. The run then dies on `banking.transactions` not existing, which is the truth of this
    machine: there is no Hive, no warehouse and no data. What the assertion demands is that it did
    NOT die at the parameter gate — a run refused before it started proves nothing about
    submission, and that distinction is the whole content of this test.
    """
    seeded = chain_tests._seed(db)
    request_id = chain_tests._request(seeded, request_id="req-e1-submit")
    work_items = [chain_tests._authored(seeded, monkeypatch, suffix="e1submit")]
    chain_tests._attest_capability(seeded)
    submitter = LocalClusterSubmitter(
        python_executable=l0_python, env=l0_env, timeout_seconds=900.0)

    outcome = chain_tests._run(
        seeded, request_id, work_items, tmp_path,
        l0=L0Interpreter(python_executable=l0_python, timeout_seconds=600.0, env=l0_env),
        execution=chain_tests._execution(
            metastore=chain_tests._G2Metastore(chain_tests.INVENTORY), submitter=submitter))

    assert outcome.validation_report is not None
    assert outcome.validation_report.status is ValidationStatus.PASSED
    assert outcome.l1_report is not None and outcome.l1_report.status is ValidationStatus.PASSED, \
        outcome.l1_report
    assert outcome.submission is not None, "the chain reached SUBMIT"
    assert outcome.submission.returncode is not None, (
        "execution never STARTED — that is an environment fault, not a pipeline verdict: "
        + outcome.submission.detail)
    assert "RUN_PARAMETERS_MISSING" not in outcome.submission.detail, outcome.submission.detail
    # ...and the run stopped where a submission that ran and failed must stop, saying so.
    assert outcome.stopped_at is ChainStage.SUBMIT, outcome.stopped_at
    assert outcome.terminal_event is RunEventKind.RUN_FAILED
    assert str(outcome.submission.returncode) in chain_tests.read_run_events(
        seeded, outcome.run_id)[-1].detail


def test_the_governed_contract_REACHES_A_PUBLISHED_TABLE(
        the_declared_environment, db, monkeypatch, tmp_path, l0_python: str, l0_env) -> None:
    """§0.5 items 5–7 on a BUILD-VERIFIED artifact: `prepare_run` → `run_l1` → `submit` → publish,
    terminal `PUBLISHED`, and the object the plane says is readable is named on the durable
    active-revision pointer (migration 1055).

    The collected suite proves this ladder with `run_l0`'s verdict injected. Here the verdict is
    the real one from a real interpreter, so the generation that publishes is a generation whose
    project genuinely imports and constructs its kedro pipeline — which is what makes "this
    contract materialized" a claim about an artifact rather than about a mock.

    **Which seam is real in WHICH test, because the two must not be confused.** The submitter here
    is `_Submitter`, the recording fake: this test's subject is the LADDER ABOVE submission —
    that a build-verified generation reaches `PUBLISHED` and leaves a pointer. The test above is
    where the submission is genuinely executed, and it cannot reach here because a real run against
    a machine with no Hive fails and the chain correctly stops at `SUBMIT`. Neither test can be
    both, and neither pretends to be.

    **The honest limit, and it is the whole of E2.** `sandbox_feature.<group>` is not queried,
    because there is no metastore on this machine to query and no adapter in `src/` that could ask
    one. What is asserted instead is everything the control plane can prove without a cluster: the
    swap was handed the published object, the generation and the columns; the pointer row exists;
    the terminal is `PUBLISHED`; and `run_status` — the ONLY place a run's status comes from —
    folds to it. Making the last hop real is a deployment against a live cluster and needs an
    operator, which is exactly where §0.5 item 7 stays until then.
    """
    seeded = chain_tests._seed(db)
    request_id = chain_tests._request(seeded, request_id="req-e1-publish")
    work_items = [chain_tests._authored(seeded, monkeypatch, suffix="e1publish")]
    chain_tests._attest_capability(seeded)
    swap = chain_tests._Swap()

    outcome = chain_tests._run(
        seeded, request_id, work_items, tmp_path,
        l0=L0Interpreter(python_executable=l0_python, timeout_seconds=600.0, env=l0_env),
        execution=chain_tests._execution(swap=swap))

    # The build proof is the REAL one, read back off the durable record.
    stored = read_validation_reports(seeded, generation_id=outcome.generation_id)
    assert [report.status for report in stored if report.level.value == "L0"] == \
        [ValidationStatus.PASSED], [report.payload() for report in stored]

    # `stopped_at` names the stage the run REACHED, and for a published run that is the publish
    # step itself — not `None`. Asserted rather than left out: a run that "stopped" anywhere
    # earlier and still claimed `PUBLISHED` would be the one shape this ladder must never produce.
    assert outcome.stopped_at is ChainStage.PUBLISH, outcome.stopped_at
    assert outcome.terminal_event is RunEventKind.PUBLISHED
    assert outcome.lifecycle_state is RequestLifecycle.COMMITTED
    assert run_status(seeded, outcome.run_id) is RunStatus.PUBLISHED

    revision = read_active_revision(seeded, outcome.logical_group_name)
    assert revision is not None
    assert revision.generation_id == outcome.generation_id
    assert [call["published_object"] for call in swap.calls] == [revision.published_object]
    assert swap.calls[0]["generation_id"] == outcome.generation_id
    assert chain_tests._FEATURE in swap.calls[0]["columns"], swap.calls[0]["columns"]
