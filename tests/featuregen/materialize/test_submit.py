"""Spec A Task 15b — §11.1's submitter: the prepared parameters are what execution reads.

The load-bearing property is not that a process starts. It is that **exactly** the parameters run
preparation resolved cross the boundary — `business_dt` alone is the shape §11.1 rules out by name,
and a parameter nobody planned for is a value the pipeline may read that nothing resolved. Both are
refused before a process exists, and the crossing itself is proved by capturing what a stand-in
interpreter was actually handed rather than by trusting the command this module built.

No `kedro` and no `pyspark` are imported here or in `submit.py`: the run script is a source string
executed by another interpreter, exactly as L0's build probe is.
"""
from __future__ import annotations

import ast
import json
import os
import signal
import sys
import time

import pytest

from featuregen.materialize.render.project import PIPELINE_NAME, REQUIRED_RUN_PARAMETERS
from featuregen.materialize.submit import (
    LocalClusterSubmitter,
    PipelineSubmitter,
    SubmissionOutcome,
    check_run_parameters,
    submission_command,
)

PREPARED = {
    "business_dt": "2026-07-27",
    "generation_id": "gen-0001",
    "input_snapshots": [{"snapshot_id": "a" * 64, "feature_name": "total_debit_amount_30d",
                         "expr_path": "body.expr", "requirement_id": "b" * 64,
                         "catalog_source": "hdfc", "schema": "banking", "table": "transactions",
                         "partition_specs": [[["load_dt", "2026-07-27"]]]}],
    "run_id": "run-0001",
    "sandbox_execution_hash": "c" * 64,
    "staging_root": "hdfs://nn/staging/gen-0001",
}

#: The two variables `submit()` refuses to run without — Spark otherwise launches its workers on
#: whichever python is first on PATH and dies deep inside an executor.
_PYSPARK_ENV = {"PYSPARK_PYTHON": "/bin/true", "PYSPARK_DRIVER_PYTHON": "/bin/true"}


@pytest.fixture
def echo_interpreter(tmp_path):
    """A stand-in for the interpreter, which records the argv it was handed.

    The submitter's own command is not evidence: what matters is what the child process receives, so
    this captures argv on the far side of the boundary.
    """
    received = tmp_path / "received.json"
    stub = tmp_path / "echo-python"
    stub.write_text(
        '#!/bin/sh\n'
        f'printf \'%s\\n\' "$3" "$4" "$5" > "{received}"\n'
        'exit 0\n', encoding="utf-8")
    stub.chmod(0o755)
    return stub, received


@pytest.fixture
def project_root(tmp_path):
    root = tmp_path / "generated"
    root.mkdir()
    return root


# ── the parameters, and only the parameters ──────────────────────────────────────────────────────


def test_business_dt_ALONE_is_refused_before_a_process_exists(project_root) -> None:
    """§11.1's own counter-example, made unconstructible."""
    submitter = LocalClusterSubmitter(python_executable="/nonexistent/python")
    with pytest.raises(ValueError, match="business_dt"):
        submitter.submit(project_root, run_parameters={"business_dt": "2026-07-27"})


def test_a_bare_business_dt_STRING_is_refused_as_a_type_error() -> None:
    with pytest.raises(TypeError, match="business_dt alone"):
        check_run_parameters("2026-07-27")  # type: ignore[arg-type]


def test_a_parameter_NOBODY_PLANNED_FOR_is_refused_too(project_root) -> None:
    """Unexpected matters as much as missing: nothing resolved a value the pipeline may read."""
    submitter = LocalClusterSubmitter(python_executable="/nonexistent/python")
    with pytest.raises(ValueError, match="unexpected"):
        submitter.submit(project_root, run_parameters={**PREPARED, "sample_fraction": "0.01"})


def test_a_MISSING_parameter_names_which_one(project_root) -> None:
    missing_one = {k: v for k, v in PREPARED.items() if k != "staging_root"}
    submitter = LocalClusterSubmitter(python_executable="/nonexistent/python")
    with pytest.raises(ValueError, match="staging_root"):
        submitter.submit(project_root, run_parameters=missing_one)


def test_the_complete_prepared_set_is_accepted() -> None:
    assert check_run_parameters(PREPARED) is PREPARED
    assert set(PREPARED) == set(REQUIRED_RUN_PARAMETERS)


# ── a CROSS-CATALOG artifact reads one more parameter, and only it may carry it ──────────────────
#
# The join-gate node §8 renders for a `CrossCatalogJoinStepV1` wires `params:bridge_predicate_values`
# (`render/nodes_join_gate.py:115`), so `render_project` puts that name into the artifact's OWN
# required set and the rendered hook demands it. A same-catalog artifact renders no such node and no
# such parameter. The requirement is therefore CONDITIONAL on the artifact, and the check is strict
# against whichever set the artifact declared — never a static widening that would let a
# same-catalog run carry a value nothing reads.

CROSS_CATALOG_REQUIRED = (*REQUIRED_RUN_PARAMETERS, "bridge_predicate_values")
CROSS_CATALOG_PREPARED = {**PREPARED, "bridge_predicate_values": {"tenant_id": "HDFC"}}


def test_a_cross_catalog_prepared_set_is_ACCEPTED_against_the_artifacts_own_required_set() -> None:
    """P1: before this, every cross-catalog group was unsubmittable at the last mile."""
    assert check_run_parameters(
        CROSS_CATALOG_PREPARED, required_parameters=CROSS_CATALOG_REQUIRED
    ) is CROSS_CATALOG_PREPARED


def test_a_SAME_CATALOG_run_carrying_the_bridge_parameter_is_STILL_refused() -> None:
    """The other direction of the same strictness: nothing in that artifact reads the value."""
    with pytest.raises(ValueError, match="unexpected \\['bridge_predicate_values'\\]"):
        check_run_parameters(CROSS_CATALOG_PREPARED)


def test_a_cross_catalog_run_MISSING_the_bridge_parameter_is_refused() -> None:
    with pytest.raises(ValueError, match="missing \\['bridge_predicate_values'\\]"):
        check_run_parameters(PREPARED, required_parameters=CROSS_CATALOG_REQUIRED)


def test_a_required_set_that_DROPS_a_base_parameter_is_refused_outright() -> None:
    """The base set is a floor, exactly as it is in ``prepare_run``: a caller may only ADD."""
    dropped = tuple(name for name in REQUIRED_RUN_PARAMETERS if name != "staging_root")
    with pytest.raises(ValueError, match="base run parameters"):
        check_run_parameters(
            {k: v for k, v in PREPARED.items() if k != "staging_root"},
            required_parameters=dropped)


def test_the_submitter_refuses_a_cross_catalog_set_it_was_NOT_told_about(project_root) -> None:
    submitter = LocalClusterSubmitter(python_executable="/nonexistent/python")
    with pytest.raises(ValueError, match="unexpected"):
        submitter.submit(project_root, run_parameters=CROSS_CATALOG_PREPARED)


def test_the_submitter_gets_PAST_the_parameter_check_when_the_artifact_requires_it(
        project_root) -> None:
    """It never starts (the interpreter does not exist) — but it got as far as trying, which is
    exactly the boundary P1 could not cross."""
    submitter = LocalClusterSubmitter(python_executable="/nonexistent/python", env=_PYSPARK_ENV)
    outcome = submitter.submit(project_root, run_parameters=CROSS_CATALOG_PREPARED,
                               required_parameters=CROSS_CATALOG_REQUIRED)
    assert not outcome.started
    assert "never started" in outcome.detail


# ── what actually crosses the boundary ───────────────────────────────────────────────────────────


def test_EXACTLY_the_prepared_parameters_reach_the_interpreter(echo_interpreter,
                                                               project_root) -> None:
    stub, received = echo_interpreter
    outcome = LocalClusterSubmitter(python_executable=str(stub), env=_PYSPARK_ENV).submit(
        project_root, run_parameters=PREPARED)

    assert outcome.completed is True
    root_arg, params_arg, pipeline_arg = received.read_text(encoding="utf-8").splitlines()
    assert root_arg == str(project_root)
    assert pipeline_arg == PIPELINE_NAME
    assert json.loads(params_arg) == dict(PREPARED)


def test_the_nested_input_snapshots_survive_the_crossing_INTACT(echo_interpreter,
                                                                project_root) -> None:
    """`input_snapshots` is a list of objects, so any `key=value` flattening would lose its shape.

    Losing it is not cosmetic: the partitions this run may read live in there, and a project given a
    stringified version would have to invent a decoding nothing specified.
    """
    stub, received = echo_interpreter
    LocalClusterSubmitter(python_executable=str(stub), env=_PYSPARK_ENV).submit(
        project_root, run_parameters=PREPARED)

    _root, params_arg, _pipeline = received.read_text(encoding="utf-8").splitlines()
    assert json.loads(params_arg)["input_snapshots"] == PREPARED["input_snapshots"]


def test_a_value_carrying_shell_METACHARACTERS_is_not_interpreted(echo_interpreter,
                                                                  project_root) -> None:
    """argv is a list, so nothing between here and the child is a shell."""
    stub, received = echo_interpreter
    hostile = {**PREPARED, "staging_root": "hdfs://nn/staging/$(touch /tmp/pwned); rm -rf ."}
    LocalClusterSubmitter(python_executable=str(stub), env=_PYSPARK_ENV).submit(
        project_root, run_parameters=hostile)

    _root, params_arg, _pipeline = received.read_text(encoding="utf-8").splitlines()
    assert json.loads(params_arg)["staging_root"] == hostile["staging_root"]


def test_the_command_is_a_LIST_and_carries_the_run_script(project_root) -> None:
    command = submission_command("/usr/bin/python3", project_root, PREPARED, PIPELINE_NAME)
    assert isinstance(command, tuple)
    assert command[:2] == ("/usr/bin/python3", "-c")
    ast.parse(command[2])                      # the script the child will execute is valid Python
    assert "KedroSession" in command[2] and "runtime_params" in command[2]


def test_no_engine_is_imported_by_this_module() -> None:
    """`kedro` appears only inside a source string executed by ANOTHER interpreter."""
    import featuregen.materialize.submit as module

    source = ast.parse(open(module.__file__, encoding="utf-8").read())
    imported = {node.module for node in ast.walk(source) if isinstance(node, ast.ImportFrom)}
    imported |= {alias.name for node in ast.walk(source) if isinstance(node, ast.Import)
                 for alias in node.names}
    assert not any(name and name.split(".")[0] in {"kedro", "pyspark"} for name in imported)
    assert "kedro" not in sys.modules and "pyspark" not in sys.modules


# ── started, failed, and never started ───────────────────────────────────────────────────────────


def test_an_interpreter_that_cannot_be_LAUNCHED_never_started(project_root, tmp_path) -> None:
    """`returncode is None` is the distinction §11.2 draws for a validation that could not run."""
    outcome = LocalClusterSubmitter(python_executable=str(tmp_path / "no-such"),
                                    env=_PYSPARK_ENV).submit(
        project_root, run_parameters=PREPARED)
    assert (outcome.completed, outcome.returncode, outcome.started) == (False, None, False)


def test_a_pipeline_that_RAN_and_failed_is_distinguishable_from_one_that_never_ran(
        project_root, tmp_path) -> None:
    """The control for the case above: same shape of failure, entirely different remedy."""
    stub = tmp_path / "failing-python"
    stub.write_text('#!/bin/sh\necho "SPINE_DUPLICATE_KEY: 3 duplicate keys" >&2\nexit 4\n',
                    encoding="utf-8")
    stub.chmod(0o755)

    outcome = LocalClusterSubmitter(python_executable=str(stub), env=_PYSPARK_ENV).submit(
        project_root, run_parameters=PREPARED)
    assert (outcome.completed, outcome.returncode, outcome.started) == (False, 4, True)
    assert "SPINE_DUPLICATE_KEY" in outcome.detail
    assert outcome.detail.startswith("stderr: ")      # labeled, so an operator knows which stream


def test_a_run_that_exceeds_its_timeout_never_STARTED_either(project_root, tmp_path) -> None:
    stub = tmp_path / "slow-python"
    stub.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    stub.chmod(0o755)

    outcome = LocalClusterSubmitter(python_executable=str(stub), timeout_seconds=0.4,
                                    env=_PYSPARK_ENV).submit(
        project_root, run_parameters=PREPARED)
    assert (outcome.completed, outcome.returncode) == (False, None)
    assert "exceeded 0.4s" in outcome.detail and "process group was killed" in outcome.detail


def test_the_environment_reaches_the_run(project_root, tmp_path) -> None:
    """`PYSPARK_PYTHON` and `PYSPARK_DRIVER_PYTHON` must reach the child or Spark uses another one."""
    received = tmp_path / "env.txt"
    stub = tmp_path / "env-python"
    stub.write_text(f'#!/bin/sh\nprintf \'%s\' "$PYSPARK_PYTHON" > "{received}"\nexit 0\n',
                    encoding="utf-8")
    stub.chmod(0o755)

    LocalClusterSubmitter(python_executable=str(stub),
                          env={"PYSPARK_PYTHON": "/opt/py/bin/python",
                               "PYSPARK_DRIVER_PYTHON": "/opt/py/bin/python"}).submit(
        project_root, run_parameters=PREPARED)
    assert received.read_text(encoding="utf-8") == "/opt/py/bin/python"


# ── the timeout is a real bound, and diagnostics survive it ──────────────────────────────────────


def test_a_grandchild_holding_the_pipes_cannot_wedge_the_submitter(tmp_path) -> None:
    """Killing only the DIRECT child leaves the spark-submit grandchild running (report §4).

    On Windows and pre-bpo-37424 CPython, ``run(timeout=)`` then re-enters ``communicate()`` with
    no timeout and blocks until the grandchild exits (~120s here); on this platform the drain is
    bounded but the grandchild survives as an ORPHAN that keeps writing into ``staging_root``.
    Both are the same missing property — the process GROUP dies — so this asserts both: the wall
    clock stays bounded AND the grandchild is dead when ``submit()`` returns.
    """
    stub = tmp_path / "python"
    grandchild_pid_file = tmp_path / "grandchild.pid"
    stub.write_text("#!/bin/sh\n"
                    "( sleep 120 ) &\n"
                    f'echo $! > "{grandchild_pid_file}"\n'
                    "sleep 120\n", encoding="utf-8")
    stub.chmod(0o755)
    submitter = LocalClusterSubmitter(python_executable=str(stub), timeout_seconds=1.0,
                                      env=_PYSPARK_ENV)
    started = time.monotonic()
    try:
        outcome = submitter.submit(tmp_path, run_parameters=dict(PREPARED))

        assert time.monotonic() - started < 40.0      # old code: ~120s pipe-drain hang (Windows)
        assert outcome.completed is False and outcome.returncode is None
        grandchild_pid = int(grandchild_pid_file.read_text(encoding="utf-8"))
        deadline = time.monotonic() + 10.0            # a killed orphan may be a zombie briefly
        while True:
            try:
                os.kill(grandchild_pid, 0)
            except ProcessLookupError:
                break                                 # dead: the whole GROUP was killed
            if time.monotonic() > deadline:
                pytest.fail("the grandchild outlived submit(): only the direct child was killed, "
                            "and the orphan would keep writing into staging_root")
            time.sleep(0.05)
    finally:
        # Leave no 120s sleepers behind even if the assertions above (or a wedged submit) got here
        # first. Guarded: under the OLD code the stub shares the test runner's process group, so
        # only the recorded grandchild pid is safe to signal — never a pgid we did not create.
        try:
            os.kill(int(grandchild_pid_file.read_text(encoding="utf-8")), signal.SIGKILL)
        except (FileNotFoundError, ValueError, ProcessLookupError, PermissionError):
            pass


def test_both_streams_survive_into_the_detail(tmp_path) -> None:
    """`stderr or stdout` discards the cause on stdout whenever Spark's chatty stderr is not empty."""
    stub = tmp_path / "python"
    stub.write_text("#!/bin/sh\necho the-cause\necho noise 1>&2\nexit 3\n", encoding="utf-8")
    stub.chmod(0o755)
    outcome = LocalClusterSubmitter(python_executable=str(stub), env=_PYSPARK_ENV)\
        .submit(tmp_path, run_parameters=dict(PREPARED))
    assert "the-cause" in outcome.detail and "noise" in outcome.detail


def test_missing_pyspark_python_is_refused_before_a_process_exists(tmp_path, monkeypatch) -> None:
    """Documented-mandatory was never checked; now it is, on the MERGED environment."""
    monkeypatch.delenv("PYSPARK_PYTHON", raising=False)
    monkeypatch.delenv("PYSPARK_DRIVER_PYTHON", raising=False)
    with pytest.raises(ValueError, match="PYSPARK_PYTHON"):
        LocalClusterSubmitter(python_executable="/bin/true", env={})\
            .submit(tmp_path, run_parameters=dict(PREPARED))


def test_an_ambient_pyspark_python_satisfies_the_check_through_the_MERGE(tmp_path,
                                                                         monkeypatch) -> None:
    """The check reads self.env OVER os.environ — exactly what the child will receive."""
    monkeypatch.setenv("PYSPARK_PYTHON", "/bin/true")
    monkeypatch.setenv("PYSPARK_DRIVER_PYTHON", "/bin/true")
    stub = tmp_path / "python"
    stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    stub.chmod(0o755)
    outcome = LocalClusterSubmitter(python_executable=str(stub))\
        .submit(tmp_path, run_parameters=dict(PREPARED))
    assert outcome.completed is True


def test_the_run_script_speaks_both_kedro_majors() -> None:
    """kedro >= 1.0 names the kwarg `runtime_params`; the 0.19.x line names it `extra_params`.

    The script must ask the INSTALLED signature rather than remember either name, or the submitter
    cannot start a run in a 0.19 artifact venv (2026-08-01 addendum: kedro 1.0+ is a supported
    target by user directive).
    """
    from featuregen.materialize.submit import _RUN_SCRIPT

    ast.parse(_RUN_SCRIPT)
    assert "runtime_params" in _RUN_SCRIPT and "extra_params" in _RUN_SCRIPT
    assert "inspect.signature(KedroSession.create).parameters" in _RUN_SCRIPT


def test_the_local_submitter_satisfies_the_SEAM() -> None:
    """Checked structurally — no process is started, and the seam has exactly one method."""
    import inspect

    assert isinstance(LocalClusterSubmitter(python_executable=sys.executable), PipelineSubmitter)
    assert {name for name in dir(PipelineSubmitter) if not name.startswith("_")} == {"submit"}
    assert list(inspect.signature(LocalClusterSubmitter.submit).parameters) == \
        list(inspect.signature(PipelineSubmitter.submit).parameters)


def test_the_outcome_carries_no_field_a_data_VALUE_could_live_in() -> None:
    import dataclasses

    assert [f.name for f in dataclasses.fields(SubmissionOutcome)] == \
        ["completed", "returncode", "detail"]
