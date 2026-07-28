"""The L0 GATE — the real generated project, imported and built in a real Spark environment.

**Not named ``test_*`` on purpose, so the main suite never collects it.** ``pyspark`` and ``kedro``
are not dependencies of this platform, and ``src/`` never imports either; a suite that pulled them
in would be pinning the validator to the versions of the artifact it validates, and would trade the
whole suite's ~4s for a JVM-capable interpreter. Run it explicitly::

    FEATUREGEN_L0_PYTHON=$PWD/.venv-l0/bin/python \\
    PYTHONPATH=$PWD/src .venv/bin/python -m pytest tests/featuregen/materialize/l0_gate.py -q

pytest collects a file given by path regardless of its name, which is what makes the split work
without a marker or a config change.

**What the main suite proves and what only this can.** The suite proves every branch of
:func:`~featuregen.materialize.validation.run_l0` — that a project which parses and yields no
pipeline is ``PIPELINE_NOT_CONSTRUCTIBLE``, that a failed import is ``PROJECT_DOES_NOT_BUILD``, that
a hand edit is ``PROJECT_HASH_MISMATCH``, that an unreachable environment invents nothing — against
hand-authored stdlib-only projects. What it cannot prove is that the RENDERED project is one of the
ones that builds: that needs kedro, pyspark and a JVM, and it is what this file is.

The environment is the one built for this task: ``.venv-l0`` (PySpark 4.2.0, kedro 1.5.0,
kedro-datasets 9.5.0, Python 3.11) with Temurin 17. Both ``PYSPARK_PYTHON`` and
``PYSPARK_DRIVER_PYTHON`` are exported below — without them Spark launches workers on the system
Python and pyspark's own ``types.py`` dies on ``X | Y``.
"""
from __future__ import annotations

import os
import pathlib
import subprocess

import pytest
from tests.featuregen.materialize.test_group_plan import catalog  # noqa: F401 - a fixture
from tests.featuregen.materialize.test_render_project import project  # noqa: F401 - a fixture

from featuregen.materialize.codes import ValidationFindingCode
from featuregen.materialize.identity import SealedProject
from featuregen.materialize.render.project import materialize_to
from featuregen.materialize.validation import ValidationStatus, run_l0

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
    candidates += [str(parent / ".venv-l0" / "bin" / "python") for parent in here.parents]
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


def test_the_environment_really_has_the_engines_the_project_pins(l0_python: str) -> None:
    """A gate that ran against an environment without pyspark would pass vacuously as 'no build'."""
    proved = subprocess.run(  # noqa: S603
        [l0_python, "-c", "import kedro, pyspark; print(kedro.__version__, pyspark.__version__)"],
        capture_output=True, text=True, check=False)
    assert proved.returncode == 0, proved.stderr
    assert proved.stdout.strip(), "the environment answered nothing"


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
