"""Spec §11.1 — submission: the PREPARED parameters are what execution reads.

Resolving and validating exact partitions is worthless if the generated project then reads whatever
it likes, so submission passes :attr:`~featuregen.materialize.runprep.RunPreparation.parameters` —
**not ``business_dt`` alone**. This module makes that structural rather than advisory: a submitter
that is handed anything but exactly ``REQUIRED_RUN_PARAMETERS`` refuses before a process is started,
naming what is missing and what is extra. "Unexpected" matters as much as "missing": a parameter
nobody planned for is a value the pipeline may read and run preparation never resolved, and the
rendered ``RunParametersHook`` refuses it at the other end with ``RUN_PARAMETERS_MISSING``. Refusing
here as well means the run does not start rather than starting and failing inside Spark.

**The parameters cross the boundary as ONE canonical JSON document**, given to
``KedroSession.create`` under whichever keyword the installed kedro names it — ``runtime_params``
on kedro >= 1.0, ``extra_params`` on the 0.19.x line; the run script asks
``inspect.signature`` rather than remembering either. Both feed the same ``runtime_params``
resolver the rendered catalog interpolates (``${runtime_params:staging_root}``). They are not
flattened into ``key=value`` pairs:
``input_snapshots`` is a list of objects, and any flattening of it invents a text encoding that the
project would have to invent back.

**A submission that never started is not a submission that failed.** ``returncode is None`` is the
distinction, and it is the same rule §11.2 applies to a validation that could not run: an
interpreter that could not be launched or a run killed by a timeout produced no verdict about the
pipeline, and reporting one would be an invented observation.

The engines are **not** imported here. ``kedro`` is named only inside a source string executed by
another interpreter, exactly as :mod:`featuregen.materialize.validation`'s L0 probe is, which is
what keeps ``src/`` free of the artifact's own dependencies.
"""
from __future__ import annotations

import json
import os
import pathlib
import signal
import subprocess
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from featuregen.materialize.render.project import PIPELINE_NAME, REQUIRED_RUN_PARAMETERS

__all__ = [
    "LocalClusterSubmitter",
    "PipelineSubmitter",
    "SubmissionOutcome",
    "check_run_parameters",
    "submission_command",
]


@dataclass(frozen=True, slots=True)
class SubmissionOutcome:
    """What one submission did — and whether it did anything at all.

    ``returncode is None`` means execution never started: the interpreter could not be launched, or
    the run was killed by the timeout. ``completed`` is then ``False``, but it is ``False`` for a
    different reason than a pipeline that ran and raised, and the two route to different people.
    """

    completed: bool
    returncode: int | None
    detail: str

    @property
    def started(self) -> bool:
        return self.returncode is not None


@runtime_checkable
class PipelineSubmitter(Protocol):
    """The seam §11.2 names. One implementation in this slice, :class:`LocalClusterSubmitter`."""

    def submit(self, project_root: str | os.PathLike[str], *,
               run_parameters: Mapping[str, Any],
               pipeline_name: str = PIPELINE_NAME) -> SubmissionOutcome:
        """Run the generated project with EXACTLY these prepared parameters."""
        ...


#: Executed by the target interpreter, never imported here. The keyword is ``runtime_params`` on
#: kedro >= 1.0 but ``extra_params`` on the 0.19.x line — both supported targets — so the script
#: asks the INSTALLED signature at run time instead of remembering either name. Whichever it is,
#: the rendered catalog resolves ``${runtime_params:…}`` against the same values.
_RUN_SCRIPT = r'''
import inspect, json, sys

from kedro.framework.session import KedroSession
from kedro.framework.startup import bootstrap_project

root, params_json, pipeline_name = sys.argv[1], sys.argv[2], sys.argv[3]
bootstrap_project(root)
kwargs = {("runtime_params"
           if "runtime_params" in inspect.signature(KedroSession.create).parameters
           else "extra_params"): json.loads(params_json)}
with KedroSession.create(project_path=root, **kwargs) as session:
    session.run(pipeline_name=pipeline_name)
'''


def check_run_parameters(run_parameters: Mapping[str, Any]) -> Mapping[str, Any]:
    """Refuse anything but EXACTLY §11.1's prepared parameters, and return them unchanged.

    Raises:
        TypeError: ``run_parameters`` is not a mapping — ``business_dt`` passed as a bare string is
            the shape §11.1 explicitly rules out.
        ValueError: a required parameter is missing or an unplanned one is present.
    """
    if not isinstance(run_parameters, Mapping):
        raise TypeError(
            f"run_parameters must be the prepared mapping, got {type(run_parameters).__name__}: "
            f"§11.1 says submission passes RunPreparation.parameters and NOT business_dt alone, "
            f"because the partitions this run may read are resolved into those parameters and "
            f"nowhere else")
    missing = sorted(set(REQUIRED_RUN_PARAMETERS) - set(run_parameters))
    unexpected = sorted(set(run_parameters) - set(REQUIRED_RUN_PARAMETERS))
    if missing or unexpected:
        raise ValueError(
            f"the run parameters are {sorted(run_parameters)} and execution requires exactly "
            f"{sorted(REQUIRED_RUN_PARAMETERS)} (missing {missing}, unexpected {unexpected}): the "
            f"rendered hook refuses a run that is missing one OR carries one nothing planned to "
            f"read, so anything but equality cannot run — and refusing here means it never starts")
    return run_parameters


def submission_command(python_executable: str, project_root: pathlib.Path,
                       run_parameters: Mapping[str, Any], pipeline_name: str) -> tuple[str, ...]:
    """The exact argv. A LIST, never a shell string — nothing here is interpolated by a shell.

    The parameters travel as one canonical JSON document with sorted keys, so the bytes a run was
    given are reproducible from the preparation that produced them.
    """
    return (python_executable, "-c", _RUN_SCRIPT, str(project_root),
            json.dumps(dict(run_parameters), sort_keys=True), pipeline_name)


@dataclass(frozen=True, slots=True)
class LocalClusterSubmitter:
    """The one submitter (§11.2): a local ``kedro`` run in the environment that has the engines.

    ``python_executable`` has no default for L0's reason — nothing may silently run the artifact in
    the control plane's own interpreter, which does not have ``pyspark`` and must not acquire it.
    ``env`` is overlaid on this process's environment; ``PYSPARK_PYTHON`` and
    ``PYSPARK_DRIVER_PYTHON`` must BOTH be present in the merged result — ``submit`` refuses to
    start a process without them, because Spark would launch its workers on whichever Python is
    first on the path and die deep inside an executor.

    The run is its own session (``start_new_session=True``), so a timeout kills the whole process
    group: killing only the direct child would leave a ``spark-submit`` grandchild orphaned,
    holding the inherited pipes and writing into ``staging_root`` after the submitter gave up.

    Known residual limitation: ``os.killpg`` reaches only descendants that STAYED in the submitted
    session's process group. A descendant that itself calls ``setsid()`` acquires a different pgid
    and escapes this kill entirely — the orphan-writes-into-``staging_root`` defect recurring one
    level deeper. That is inherent to the process-group mechanism; a cluster-mode application must
    be bounded by cluster-side controls (the resource manager killing the application), not by
    this local kill.
    """

    python_executable: str
    env: Mapping[str, str] | None = None
    timeout_seconds: float = 3600.0

    def submit(self, project_root: str | os.PathLike[str], *,
               run_parameters: Mapping[str, Any],
               pipeline_name: str = PIPELINE_NAME) -> SubmissionOutcome:
        """Run the project. Parameters AND environment are checked BEFORE a process exists."""
        check_run_parameters(run_parameters)
        merged = os.environ | self.env if self.env is not None else dict(os.environ)
        missing_env = [name for name in ("PYSPARK_PYTHON", "PYSPARK_DRIVER_PYTHON")
                       if not merged.get(name)]
        if missing_env:
            raise ValueError(
                f"env is missing {missing_env}: without them Spark launches workers on "
                f"whatever python is on PATH and dies deep inside an executor")
        root = pathlib.Path(project_root)
        command = submission_command(self.python_executable, root, run_parameters, pipeline_name)
        try:
            process = subprocess.Popen(                                   # noqa: S603 - fixed argv
                command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                cwd=str(root), env=merged, start_new_session=True)
        except (OSError, subprocess.SubprocessError) as error:
            return SubmissionOutcome(
                completed=False, returncode=None,
                detail=f"execution never started ({type(error).__name__}): {error}")
        try:
            stdout, stderr = process.communicate(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGKILL)   # pgid == pid: start_new_session
            except ProcessLookupError:                   # the group died AT the timeout boundary
                pass
            try:
                stdout, stderr = process.communicate(timeout=30.0)
            except subprocess.TimeoutExpired:            # a pipe survived even SIGKILL
                stdout, stderr = "", ""
                try:
                    process.wait(timeout=0)              # explicit reap of the SIGKILLed leader —
                except subprocess.TimeoutExpired:        # not left to a GC finalizer; if it is not
                    pass                                 # reapable yet, still return, never hang
            return SubmissionOutcome(
                completed=False, returncode=None,
                detail=f"the run exceeded {self.timeout_seconds}s; its process group was "
                       f"killed. stderr: {stderr.strip()[-1000:]}")
        return SubmissionOutcome(
            completed=process.returncode == 0, returncode=process.returncode,
            # Operator-facing and bounded: the run's own last words from BOTH streams, labeled and
            # never a value it computed — the control plane does not read feature data, including
            # out of a log.
            detail=f"stderr: {stderr.strip()[-1500:]} | stdout: {stdout.strip()[-500:]}")
