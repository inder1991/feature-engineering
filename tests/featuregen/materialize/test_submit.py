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
import sys

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


# ── what actually crosses the boundary ───────────────────────────────────────────────────────────


def test_EXACTLY_the_prepared_parameters_reach_the_interpreter(echo_interpreter,
                                                               project_root) -> None:
    stub, received = echo_interpreter
    outcome = LocalClusterSubmitter(python_executable=str(stub)).submit(
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
    LocalClusterSubmitter(python_executable=str(stub)).submit(
        project_root, run_parameters=PREPARED)

    _root, params_arg, _pipeline = received.read_text(encoding="utf-8").splitlines()
    assert json.loads(params_arg)["input_snapshots"] == PREPARED["input_snapshots"]


def test_a_value_carrying_shell_METACHARACTERS_is_not_interpreted(echo_interpreter,
                                                                  project_root) -> None:
    """argv is a list, so nothing between here and the child is a shell."""
    stub, received = echo_interpreter
    hostile = {**PREPARED, "staging_root": "hdfs://nn/staging/$(touch /tmp/pwned); rm -rf ."}
    LocalClusterSubmitter(python_executable=str(stub)).submit(
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
    outcome = LocalClusterSubmitter(python_executable=str(tmp_path / "no-such")).submit(
        project_root, run_parameters=PREPARED)
    assert (outcome.completed, outcome.returncode, outcome.started) == (False, None, False)


def test_a_pipeline_that_RAN_and_failed_is_distinguishable_from_one_that_never_ran(
        project_root, tmp_path) -> None:
    """The control for the case above: same shape of failure, entirely different remedy."""
    stub = tmp_path / "failing-python"
    stub.write_text('#!/bin/sh\necho "SPINE_DUPLICATE_KEY: 3 duplicate keys" >&2\nexit 4\n',
                    encoding="utf-8")
    stub.chmod(0o755)

    outcome = LocalClusterSubmitter(python_executable=str(stub)).submit(
        project_root, run_parameters=PREPARED)
    assert (outcome.completed, outcome.returncode, outcome.started) == (False, 4, True)
    assert "SPINE_DUPLICATE_KEY" in outcome.detail


def test_a_run_that_exceeds_its_timeout_never_STARTED_either(project_root, tmp_path) -> None:
    stub = tmp_path / "slow-python"
    stub.write_text("#!/bin/sh\nsleep 5\n", encoding="utf-8")
    stub.chmod(0o755)

    outcome = LocalClusterSubmitter(python_executable=str(stub), timeout_seconds=0.4).submit(
        project_root, run_parameters=PREPARED)
    assert (outcome.completed, outcome.returncode) == (False, None)
    assert "verdict" in outcome.detail


def test_the_environment_reaches_the_run(project_root, tmp_path) -> None:
    """`PYSPARK_PYTHON` and `PYSPARK_DRIVER_PYTHON` must reach the child or Spark uses another one."""
    received = tmp_path / "env.txt"
    stub = tmp_path / "env-python"
    stub.write_text(f'#!/bin/sh\nprintf \'%s\' "$PYSPARK_PYTHON" > "{received}"\nexit 0\n',
                    encoding="utf-8")
    stub.chmod(0o755)

    LocalClusterSubmitter(python_executable=str(stub),
                          env={"PYSPARK_PYTHON": "/opt/py/bin/python"}).submit(
        project_root, run_parameters=PREPARED)
    assert received.read_text(encoding="utf-8") == "/opt/py/bin/python"


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
