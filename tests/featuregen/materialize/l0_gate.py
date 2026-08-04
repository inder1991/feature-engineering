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
from featuregen.materialize.control_plane import RunEventKind
from featuregen.materialize.identity import SealedProject
from featuregen.materialize.render.project import (
    PIPELINE_NAME,
    REQUIRED_RUN_PARAMETERS,
    materialize_to,
)
from featuregen.materialize.request_store import RequestLifecycle
from featuregen.materialize.submit import submission_command
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


#: Ask the interpreter what it actually has, keyed by DISTRIBUTION name — which is what a lock file
#: names, and what `kedro-datasets` vs the importable `kedro_datasets` would otherwise disagree about.
#: `importlib.metadata` reads the installed distribution rather than a module's `__version__`
#: attribute, so a package that ships a stale or absent `__version__` cannot make this answer wrong.
_INSTALLED_VERSIONS = (
    "import importlib.metadata as m, json\n"
    "out = {}\n"
    "for dist in ('kedro', 'kedro-datasets', 'pyspark'):\n"
    "    try:\n"
    "        out[dist] = m.version(dist)\n"
    "    except m.PackageNotFoundError:\n"
    "        out[dist] = None\n"
    "print(json.dumps(out))\n"
)


def _declared_pins(sealed: SealedProject) -> dict[str, str]:
    """The artifact's OWN pins, parsed out of the file it ships them in.

    Read from the rendered bytes rather than from ``fixtures.ENGINE_VERSIONS`` on purpose: the
    fixture is what this suite happened to render with, and the lock is what the artifact SAYS. A
    test that compared the interpreter against the fixture would still pass if the renderer stopped
    writing the pins into the project at all.
    """
    pins: dict[str, str] = {}
    for line in sealed.files["requirements.lock"].splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "==" not in stripped:
            continue
        name, _, version = stripped.partition("==")
        pins[name.strip()] = version.strip()
    return pins


def test_the_environment_really_has_the_engines_the_project_pins(
        project: SealedProject, l0_python: str) -> None:  # noqa: F811
    """The interpreter's INSTALLED engines must equal the ones the rendered project pins itself to.

    **This test used to lie.** It asserted `returncode == 0` and non-empty stdout on
    `import kedro, pyspark` — it compared nothing at all, while its name promised the comparison
    every other claim in this file leans on. It is the check that decides whether a passing L0 is
    evidence about *this* artifact or about some other project that happens to share its source, and
    for the whole of Phase G it was answering a different question (*is anything installed?*).

    **EXPECT THIS RED IN TWO OF THE THREE ENVIRONMENTS, AND DO NOT MAKE IT GREEN.**

    ===========================  ==========================  =============
    environment                  engines                     verdict
    ===========================  ==========================  =============
    ``.venv-artifact``           0.19.9 / 4.1.0 / 3.5.1      **green**
    ``.venv-l0-modern``          1.5.0 / 9.5.0 / 4.2.0       **red**
    kind image ``/opt/kedro-venv``  1.5.0 / 9.5.0 / 3.5.3    **red**
    ===========================  ==========================  =============

    Red here is a true statement about the environment, not a broken test. The three ways to make it
    green are all worse than the red: weakening the assertion restores the lie; ``xfail`` records
    that we expect our own build proofs to be about the wrong project; and re-rendering the fixture
    with the interpreter's versions (what
    :func:`test_the_run_parameters_hook_fires_and_passes_inside_a_REAL_kedro_session` legitimately
    does, because ``bootstrap_project`` forces its hand) would make the comparison tautological.

    **What a reader should actually do about a red.** Nothing, in the gate. The finding belongs to
    production, and it is recorded as DEFERRED-WORK **A.41**: `run_l0` never consults the declared
    pins, because `_BUILD_PROBE` imports the project and calls `register_pipelines()` without
    bootstrapping it, so `kedro_init_version` is never enforced. A deployment whose mounted inventory
    declares engines its L0 interpreter does not have gets `PASSED` — "the build was proven" — with
    no signal at all. Closing that needs an engine-version check in L0 carrying its own refusal code,
    which is a §14 closed-vocabulary decision rather than a test change. Until it lands, this red is
    the only place the platform says the disagreement out loud, so it is load-bearing precisely
    because it is failing.
    """
    proved = subprocess.run(  # noqa: S603
        [l0_python, "-c", _INSTALLED_VERSIONS], capture_output=True, text=True, check=False)
    assert proved.returncode == 0, proved.stderr
    installed = json.loads(proved.stdout.strip().splitlines()[-1])

    declared = _declared_pins(project)
    assert declared, "the rendered project shipped no parseable pins in requirements.lock"

    disagreements = {
        dist: (want, installed.get(dist))
        for dist, want in sorted(declared.items())
        if installed.get(dist) != want
    }
    assert not disagreements, (
        "the L0 interpreter is not the environment this artifact declares, so a passing build proof "
        "here would be a proof about a DIFFERENT project (DEFERRED-WORK A.41):\n"
        + "\n".join(
            f"  {dist}: the project pins {want!r}, the interpreter has "
            + ("NOTHING INSTALLED" if have is None else repr(have))
            for dist, (want, have) in disagreements.items())
        + f"\n  interpreter: {l0_python}")


def test_the_RENDERED_project_builds_its_kedro_pipeline(project: SealedProject, tmp_path,  # noqa: F811
                                                        l0_python: str, l0_env) -> None:
    """L0 over the real artifact: it hashes to its lock, imports, and yields a pipeline with nodes.

    This is the claim the main suite structurally cannot make, and the reason `PROJECT_DOES_NOT_BUILD`
    and `PIPELINE_NOT_CONSTRUCTIBLE` are not theoretical codes.
    """
    root = materialize_to(project, tmp_path / "generated")
    report = run_l0(root, generation_id=GEN, environment_id=ENVIRONMENT, report_id="rep-gate",
                    python_executable=l0_python, clock=_clock(), env=l0_env)
    assert (report.status, report.findings) == (ValidationStatus.PASSED, ()), \
        [finding.payload() for finding in report.findings]


def test_a_hand_edit_of_the_RENDERED_project_is_caught_in_the_real_environment(
        project: SealedProject, tmp_path, l0_python: str, l0_env) -> None:  # noqa: F811
    """The same artifact, one comment added. Everything else is held equal."""
    root = materialize_to(project, tmp_path / "generated")
    readme = root / "README.md"
    readme.write_text(readme.read_text(encoding="utf-8") + "\n<!-- drift -->\n", encoding="utf-8")

    report = run_l0(root, generation_id=GEN, environment_id=ENVIRONMENT, report_id="rep-gate-2",
                    python_executable=l0_python, clock=_clock(), env=l0_env)
    assert report.status is ValidationStatus.FAILED
    assert [finding.code for finding in report.findings] == \
        [ValidationFindingCode.PROJECT_HASH_MISMATCH]


def test_the_CHAIN_ITSELF_seals_a_project_whose_build_is_PROVED(
        db, monkeypatch, tmp_path, l0_python: str, l0_env) -> None:
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
