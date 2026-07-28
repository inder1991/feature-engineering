"""Spec §11.2 — the validation loop: L0 over the generated project, L1 over the physical inputs.

**A finding's CODE says what was seen; its CLASS says who fixes it, and the class is what routes a
decision.** §11 names four: ``RENDERER_DEFECT`` (fix the renderer and regenerate),
``GOVERNED_FACT_MISMATCH`` (the code is right and the catalog is wrong — re-attest, and
**regeneration is blocked**), ``ENVIRONMENT_OR_DATA`` (the operator acts) and ``UNCLASSIFIED``
(**fails closed**, and never silently environmental).

The asymmetry is the design. A missing partition is data that has not landed; nobody attests that a
partition exists, and the fix is upstream. A type contradiction, an absent column or a denied read
is the catalog and the cluster disagreeing about a fact the compilation was built on — regenerating
from the same wrong facts produces the same wrong project faster, so it is refused rather than
retried.

**A validation that could not run has not found nothing — it has found nothing OUT.** An unreachable
cluster is ``status="error"`` with **zero** findings, never an invented one and never a pass;
migration 1034 carries the same rule as a CHECK, and :class:`ValidationReportV1` carries it as an
invariant so an incoherent report cannot be constructed in the first place. ``may_regenerate``
refuses on an error report for the same reason: nothing was looked at.

**L1 reads metadata only.** Its seam, :class:`MetastoreMetadata`, has three questions — which
partitions a table has, which columns and physical types it has, and whether a set of roles may read
it. There is deliberately no method that returns a row, so the control plane cannot read feature
data even by mistake, and a finding therefore *cannot* carry a data value: counts, types and
locations are all there is to report.

**L0 proves the project BUILDS, which is not what parsing proves.** A rendered project whose
``register_pipelines()`` returns an empty mapping, or a pipeline with no nodes, is accepted by
``ast.parse`` and is exactly the artifact that runs successfully while publishing nothing. L0
therefore imports the project in a **separate interpreter** and constructs the pipeline object,
reporting ``PROJECT_DOES_NOT_BUILD`` when the import fails and ``PIPELINE_NOT_CONSTRUCTIBLE`` when
the import succeeds and no pipeline comes out of it. The interpreter is a parameter because the
generated project imports ``kedro`` and ``pyspark``, which are **not** dependencies of this platform
(``src/`` never imports either); the L0 gate runs it against the environment that has them, and the
suite runs the same code against ``sys.executable`` and hand-authored stdlib-only projects, which is
what keeps this module's behaviour proved without a pyspark import.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.contracts.db import DbConn
from featuregen.materialize.codes import ValidationFindingCode
from featuregen.materialize.identity import (
    GENERATED_LOCK_FILENAME,
    RenderedArtifactIdentity,
    generated_project_hash,
    read_lock,
)

__all__ = [
    "FINDING_CLASSES",
    "ClusterUnreachable",
    "FindingClass",
    "FindingSeverity",
    "ValidationFinding",
    "ValidationLevel",
    "ValidationReportV1",
    "ValidationStatus",
    "classify",
    "may_regenerate",
    "record_validation_report",
    "run_l0",
]


class ValidationLevel(StrEnum):
    """§11.2's levels. ``L3`` is the real run and is not a validation."""

    L0 = "L0"
    L1 = "L1"
    L2 = "L2"


class ValidationStatus(StrEnum):
    """The three verdicts, and the three migration 1034 allows.

    ``ERROR`` is *the validation could not run*, which is neither a pass nor a failure: it carries
    no findings, because a check that never happened observed nothing to report.
    """

    PASSED = "passed"
    FAILED = "failed"
    ERROR = "error"


class FindingClass(StrEnum):
    """§11.2's routing: not how bad a finding is, but WHOSE problem it is."""

    #: The renderer emitted something that does not build. Fix the renderer, regenerate.
    RENDERER_DEFECT = "renderer_defect"
    #: The catalog and the cluster disagree about a governed fact. Re-attest. BLOCKS regeneration.
    GOVERNED_FACT_MISMATCH = "governed_fact_mismatch"
    #: The environment or the data is not in the state the run needs. The operator acts.
    ENVIRONMENT_OR_DATA = "environment_or_data"
    #: Unrecognized. FAILS CLOSED — blocks regeneration, and is never silently environmental.
    UNCLASSIFIED = "unclassified"


class FindingSeverity(StrEnum):
    """How a finding reads on its own.

    Not a fifth failure vocabulary: §14's four closed enums are the *codes*, and severity is not a
    code. Every L0 and L1 finding this slice emits is ``ERROR`` — each describes a condition under
    which the run cannot be trusted to produce correct output — and a test pins that. ``WARNING``
    belongs to L2's on-demand sample execution, which is not in this slice.
    """

    ERROR = "error"
    WARNING = "warning"


#: Code → class, TOTAL over :class:`ValidationFindingCode` and pinned by a ``==`` test.
#:
#: The two L0 build failures are the renderer's: nothing but the renderer wrote those bytes.
#: ``PROJECT_HASH_MISMATCH`` is NOT the renderer's — its output was correct and something on disk
#: changed it, so the remedy is to restore or regenerate the tree, which is the operator's act and
#: must therefore not be blocked. The three L1 read-set findings are all governed facts the
#: compilation relied on (a column, its type, and Gate 2's read authorization); a partition is the
#: one L1 checks that nobody attests, so it alone is data.
FINDING_CLASSES: Mapping[ValidationFindingCode, FindingClass] = {
    ValidationFindingCode.PROJECT_DOES_NOT_BUILD: FindingClass.RENDERER_DEFECT,
    ValidationFindingCode.PIPELINE_NOT_CONSTRUCTIBLE: FindingClass.RENDERER_DEFECT,
    ValidationFindingCode.PROJECT_HASH_MISMATCH: FindingClass.ENVIRONMENT_OR_DATA,
    ValidationFindingCode.COLUMN_ABSENT: FindingClass.GOVERNED_FACT_MISMATCH,
    ValidationFindingCode.COLUMN_TYPE_MISMATCH: FindingClass.GOVERNED_FACT_MISMATCH,
    ValidationFindingCode.READ_DENIED: FindingClass.GOVERNED_FACT_MISMATCH,
    ValidationFindingCode.PARTITION_ABSENT: FindingClass.ENVIRONMENT_OR_DATA,
    ValidationFindingCode.UNKNOWN_FINDING: FindingClass.UNCLASSIFIED,
}

#: The classes that mean *do not rebuild from these facts* (§11.2).
_BLOCKING_CLASSES = frozenset({FindingClass.GOVERNED_FACT_MISMATCH, FindingClass.UNCLASSIFIED})


def classify(code: ValidationFindingCode) -> FindingClass:
    """Which class a finding code routes to — TOTAL, and unrecognized input fails CLOSED.

    A lookup rather than a chain of comparisons, and a ``.get`` with ``UNCLASSIFIED`` as the default
    rather than a ``KeyError``: a code this platform does not know is exactly the case §11.2 says
    must never be silently environmental, and a new member added to
    :class:`~featuregen.materialize.codes.ValidationFindingCode` without an entry here lands on the
    fail-closed answer rather than on whichever branch happened to be last.
    """
    try:
        return FINDING_CLASSES.get(code, FindingClass.UNCLASSIFIED)
    except TypeError:                                    # an unhashable "code" is not one either
        return FindingClass.UNCLASSIFIED


def _text(value: object, *, field_name: str, why: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} is blank ({value!r}): {why}")
    return value


@dataclass(frozen=True, slots=True)
class ValidationFinding:
    """ONE observation, in §11's shape: a closed code, a location, facts and a count.

    ``expected`` and ``observed`` are **type and schema facts only** — ``decimal(18,2)``,
    ``absent``, ``12 partitions``. There is no field a data value could live in, and L1's seam
    cannot fetch one, so the rule is structural rather than a review convention.

    ``classification`` is DERIVED from ``code`` rather than accepted. A finding whose class
    contradicted its code would route a real contradiction to "the operator will sort it out", and
    that is the one mistake this module exists to make impossible.
    """

    code: ValidationFindingCode
    location: str
    expected: str | None
    observed: str | None
    count: int
    severity: FindingSeverity = FindingSeverity.ERROR

    def __post_init__(self) -> None:
        if not isinstance(self.code, ValidationFindingCode):
            raise TypeError(
                f"a finding's code must be a ValidationFindingCode, got "
                f"{type(self.code).__name__}: §11's vocabulary is CLOSED, and a raw string would "
                f"classify as UNCLASSIFIED while looking exactly like a known code")
        if not isinstance(self.severity, FindingSeverity):
            raise TypeError(
                f"a finding's severity must be a FindingSeverity, got "
                f"{type(self.severity).__name__}")
        _text(self.location, field_name="location",
              why="a finding nobody can locate is not actionable, and the location is all a "
                  "reader gets — the value that provoked it is never recorded")
        for name, value in (("expected", self.expected), ("observed", self.observed)):
            if value is not None and (not isinstance(value, str) or not value.strip()):
                raise ValueError(
                    f"{name} is {value!r}: it is a TYPE or SCHEMA fact or it is absent, and a "
                    f"blank string is a third state nothing reads")
        if not isinstance(self.count, int) or isinstance(self.count, bool) or self.count < 1:
            raise ValueError(
                f"count is {self.count!r}: a finding counts how many things were seen, and a "
                f"finding of nothing is not a finding")

    @property
    def classification(self) -> FindingClass:
        return classify(self.code)

    def payload(self) -> dict[str, Any]:
        """The persisted shape (``pipeline_validation_report.findings``) — CLOSED, and no values."""
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "classification": self.classification.value,
            "location": self.location,
            "expected": self.expected,
            "observed": self.observed,
            "count": self.count,
        }


@dataclass(frozen=True, slots=True)
class ValidationReportV1:
    """§11's report. Its status and its findings cannot disagree.

    Three invariants, each of which migration 1034 either states or implies:

    * ``error`` carries **zero** findings — a validation that could not run observed nothing, and a
      findings list under ``error`` is a fabricated observation (the migration's own CHECK);
    * ``passed`` carries zero findings — every finding this slice emits is a condition under which
      the run cannot be trusted, so a pass with findings is a contradiction;
    * ``failed`` carries at least one — a verdict with no evidence behind it is not a verdict.

    ``run_id`` is ``None`` for L0, which validates the project before any run exists. It is never
    blank: absent and "present but empty" are different claims, and only one of them is true.
    """

    report_id: str
    generation_id: str
    run_id: str | None
    generated_project_hash: str
    group_plan_hash: str
    level: ValidationLevel
    environment_id: str
    status: ValidationStatus
    started_at: str
    finished_at: str
    findings: tuple[ValidationFinding, ...]

    def __post_init__(self) -> None:
        for name in ("report_id", "generation_id", "generated_project_hash", "group_plan_hash",
                     "environment_id", "started_at", "finished_at"):
            _text(getattr(self, name), field_name=name,
                  why="every field of a report is what a later reader keys on")
        if self.run_id is not None:
            _text(self.run_id, field_name="run_id",
                  why="L0 records None because no run exists yet; a blank string claims a run "
                      "whose id is nothing")
        if not isinstance(self.level, ValidationLevel):
            raise TypeError(f"level must be a ValidationLevel, got {type(self.level).__name__}")
        if not isinstance(self.status, ValidationStatus):
            raise TypeError(f"status must be a ValidationStatus, got {type(self.status).__name__}")
        if not isinstance(self.findings, tuple) or \
                any(not isinstance(f, ValidationFinding) for f in self.findings):
            raise TypeError("findings must be a tuple of ValidationFinding")
        if self.status is ValidationStatus.ERROR and self.findings:
            raise ValueError(
                f"a report with status 'error' carries {len(self.findings)} finding(s): a "
                f"validation that could not run has not found nothing, it has found nothing OUT, "
                f"and anything it listed would be an observation nobody made")
        if self.status is ValidationStatus.PASSED and self.findings:
            raise ValueError(
                f"a report with status 'passed' carries {len(self.findings)} finding(s): every L0 "
                f"and L1 finding is a condition under which the run cannot be trusted, so a pass "
                f"that lists one is two verdicts at once")
        if self.status is ValidationStatus.FAILED and not self.findings:
            raise ValueError(
                "a report with status 'failed' carries no findings: a failure with nothing to show "
                "cannot be routed to anybody, which is the whole purpose of a classification")

    def findings_payload(self) -> list[dict[str, Any]]:
        return [finding.payload() for finding in self.findings]


def may_regenerate(report: ValidationReportV1) -> bool:
    """Whether regenerating the project is a legitimate response to this report (§11.2).

    ``False`` when the report carries any ``GOVERNED_FACT_MISMATCH`` or ``UNCLASSIFIED`` finding —
    the catalog and the cluster disagree, or nobody knows, and rebuilding from the same facts
    produces the same wrong project faster.

    ``False`` for ``error`` as well, with no findings to point at: an unreachable cluster is not
    evidence that regeneration is the right move, and treating "nothing was found" as "nothing is
    wrong" is exactly the confusion §11.2's zero-finding rule exists to prevent.
    """
    if not isinstance(report, ValidationReportV1):
        raise TypeError(
            f"may_regenerate judges a ValidationReportV1, got {type(report).__name__}: the verdict "
            f"depends on the report's STATUS as well as its findings, and a bare finding list "
            f"cannot express 'the validation never ran'")
    if report.status is ValidationStatus.ERROR:
        return False
    return all(finding.classification not in _BLOCKING_CLASSES for finding in report.findings)


def record_validation_report(conn: DbConn, report: ValidationReportV1) -> None:
    """Append one report to ``pipeline_validation_report`` (§12).

    The table is append-only and its ``error``-has-no-findings CHECK restates this module's own
    invariant, so a report that reached here already satisfies it — the database is the second
    statement of the rule, not the only one.
    """
    conn.execute(
        "INSERT INTO pipeline_validation_report (report_id, generation_id, run_id, "
        "generated_project_hash, group_plan_hash, level, environment_id, status, started_at, "
        "finished_at, findings) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (report.report_id, report.generation_id, report.run_id, report.generated_project_hash,
         report.group_plan_hash, report.level.value, report.environment_id, report.status.value,
         report.started_at, report.finished_at, json.dumps(report.findings_payload())))


class ClusterUnreachable(Exception):
    """The environment could not be asked — NOT a verdict about what it contains.

    Raised by a metastore adapter when the cluster is down, unauthenticated or unroutable, and the
    ONLY exception L1 converts into ``status="error"``. Anything else propagates: catching bare
    ``Exception`` would turn a defect in this module into "the cluster was unreachable", which is an
    invented verdict of exactly the kind §11.2 forbids.
    """


# ── L0: the generated project itself ─────────────────────────────────────────────────────────────

#: What the build probe prints, on its own line, so the project's own output cannot be mistaken for
#: a verdict. A project that prints during import is legal; a project that prints something shaped
#: like a verdict would otherwise be able to declare itself buildable.
_VERDICT_MARKER = "@@L0-VERDICT@@ "

#: The probe. Stdlib ONLY and run in ANOTHER interpreter, which is what lets the suite exercise it
#: against ``sys.executable`` and hand-authored projects while the gate runs the identical code
#: against the environment that has ``kedro`` and ``pyspark``. It duck-types the registry's answer
#: rather than importing ``kedro.pipeline.Pipeline``: importing it here would put this platform's
#: control plane one dependency away from the artifact it validates.
_BUILD_PROBE = r'''
import importlib, json, sys, traceback

MARKER, root, package = sys.argv[1], sys.argv[2], sys.argv[3]
sys.path.insert(0, root + "/src")


def emit(stage, ok, observed=""):
    print(MARKER + json.dumps({"stage": stage, "ok": ok, "observed": observed}))
    raise SystemExit(0)


try:
    importlib.import_module(package)
    registry = importlib.import_module(package + ".pipeline_registry")
except BaseException as error:            # noqa: BLE001 - the project's failure, not the probe's
    del traceback
    emit("import", False, type(error).__name__)

try:
    pipelines = registry.register_pipelines()
    named = dict(pipelines.items())
    counts = {name: len(list(getattr(pipeline, "nodes"))) for name, pipeline in named.items()}
except BaseException as error:            # noqa: BLE001
    emit("build", False, type(error).__name__)

if not counts:
    emit("build", False, "0 pipelines")
empty = sorted(name for name, count in counts.items() if count == 0)
if empty:
    emit("build", False, "0 nodes in " + ",".join(empty))
emit("build", True, str(max(counts.values())) + " nodes")
'''


def _probe_verdict(root: pathlib.Path, package: str, *, python_executable: str,
                   timeout_seconds: float) -> dict[str, Any] | None:
    """Run the probe in its own interpreter. ``None`` means the environment never answered.

    ``None`` is deliberately distinct from every verdict the probe can print: an interpreter that
    could not be launched, a probe killed by a timeout and a probe whose output carries no verdict
    line are all *the validation did not run*, which §11.2 records as ``status="error"`` with no
    findings. Turning any of them into ``PROJECT_DOES_NOT_BUILD`` would blame the artifact for the
    environment.
    """
    try:
        completed = subprocess.run(                                       # noqa: S603 - fixed argv
            [python_executable, "-c", _BUILD_PROBE, _VERDICT_MARKER, str(root), package],
            capture_output=True, text=True, timeout=timeout_seconds, check=False,
            cwd=str(root))
    except (OSError, subprocess.SubprocessError):
        return None
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(_VERDICT_MARKER):
            try:
                verdict = json.loads(line[len(_VERDICT_MARKER):])
            except json.JSONDecodeError:
                return None
            return verdict if isinstance(verdict, dict) else None
    return None


def _project_package(root: pathlib.Path) -> str | None:
    """The one importable package under ``src/``. ``None`` when there is not exactly one."""
    source_root = root / "src"
    if not source_root.is_dir():
        return None
    packages = sorted(entry.name for entry in source_root.iterdir()
                      if entry.is_dir() and (entry / "__init__.py").is_file())
    return packages[0] if len(packages) == 1 else None


def _files_on_disk(root: pathlib.Path) -> Mapping[str, str] | None:
    """The project as it EXISTS, in ``generated_project_hash``'s shape. ``None`` if not all text.

    The skip list is the rendered ``PROJECT_INTEGRITY`` gate's, for the reason that gate states:
    ``__pycache__``, ``*.egg-info`` and dot-directories appear by *using* a project rather than by
    rendering one, and L0's own probe creates the first of them. Anything else unsealed is drift —
    an added module can shadow a rendered one.
    """
    files: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        parts = path.relative_to(root).parts
        if any(part == "__pycache__" or part.startswith(".") or part.endswith(".egg-info")
               for part in parts):
            continue
        relative = "/".join(parts)
        if relative.endswith(".pyc"):
            continue
        try:
            files[relative] = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            return None
    return files


def _lock_of(root: pathlib.Path) -> RenderedArtifactIdentity:
    lock = root / GENERATED_LOCK_FILENAME
    if not lock.is_file():
        raise ValueError(
            f"{root} has no {GENERATED_LOCK_FILENAME}: L0 verifies a project against the hash the "
            f"lock records, and a directory that states no hash is not a generated project — there "
            f"is no verdict to give about it, which is different from a failing one")
    return read_lock(lock.read_text(encoding="utf-8"))


def run_l0(
    root: str | os.PathLike[str],
    *,
    generation_id: str,
    environment_id: str,
    report_id: str,
    python_executable: str,
    clock: Callable[[], str],
    timeout_seconds: float = 300.0,
) -> ValidationReportV1:
    """L0 (§11.2): the project on disk hashes to its lock, imports, and yields a pipeline.

    It takes a DIRECTORY, not a
    :class:`~featuregen.materialize.identity.SealedProject`. A sealed object's files are by
    construction the ones its lock was computed from, so ``PROJECT_HASH_MISMATCH`` would be
    unreachable through it — and a hand-edited project, which is the whole reason that code exists,
    only exists on disk. Materializing a sealed project to a temporary directory is
    :func:`~featuregen.materialize.render.project.materialize_to`; L0 validates what that wrote.

    Both checks always run: a project may be edited *and* fail to build, and reporting one of the
    two would send an operator to fix the half that was visible.

    Args:
        root: the materialized project.
        generation_id: the generation these bytes were rendered for.
        environment_id: the environment the report is keyed on.
        report_id: supplied, never minted — this module has no id factory, matching
            :mod:`featuregen.materialize.control_plane`.
        python_executable: the interpreter the project is imported in. It is a parameter because
            the generated project imports ``kedro`` and ``pyspark``, which this platform does not
            depend on; there is no default, so nothing can silently validate the artifact against
            the validator's own environment.
        clock: read twice — once before the work and once after — so ``finished_at`` is the time
            the validation ended rather than a value the caller supplied in advance.
        timeout_seconds: after which the probe is *unreachable*, not failing.

    Raises:
        ValueError: ``root`` holds no ``GENERATED.lock``, so nothing states what it should hash to.
    """
    directory = pathlib.Path(root)
    lock = _lock_of(directory)
    started_at = clock()

    findings: list[ValidationFinding] = []
    observed_files = _files_on_disk(directory)
    if observed_files is None or \
            generated_project_hash(observed_files) != lock.generated_project_hash:
        findings.append(ValidationFinding(
            code=ValidationFindingCode.PROJECT_HASH_MISMATCH,
            location=str(directory),
            expected=lock.generated_project_hash,
            observed=("not all files are UTF-8 text" if observed_files is None
                      else generated_project_hash(observed_files)),
            count=1))

    package = _project_package(directory)
    if package is None:
        findings.append(ValidationFinding(
            code=ValidationFindingCode.PROJECT_DOES_NOT_BUILD, location=f"{directory}/src",
            expected="exactly one importable package", observed="0 or more than 1", count=1))
        verdict: dict[str, Any] | None = {"stage": "import", "ok": False, "observed": "no package"}
    else:
        verdict = _probe_verdict(directory, package, python_executable=python_executable,
                                 timeout_seconds=timeout_seconds)
        if verdict is None:
            # The environment, not the artifact. Every finding above is DISCARDED: §11.2's zero
            # findings under `error` means a report that could not complete reports nothing at all,
            # not the part of itself that happened to finish.
            return ValidationReportV1(
                report_id=report_id, generation_id=generation_id, run_id=None,
                generated_project_hash=lock.generated_project_hash,
                group_plan_hash=lock.compilation.group_plan_hash, level=ValidationLevel.L0,
                environment_id=environment_id, status=ValidationStatus.ERROR,
                started_at=started_at, finished_at=clock(), findings=())
        if not verdict.get("ok"):
            findings.append(ValidationFinding(
                code=(ValidationFindingCode.PROJECT_DOES_NOT_BUILD
                      if verdict.get("stage") == "import"
                      else ValidationFindingCode.PIPELINE_NOT_CONSTRUCTIBLE),
                location=f"{package}.pipeline_registry",
                expected=("importable" if verdict.get("stage") == "import"
                          else "a pipeline with at least one node"),
                # The exception TYPE, never its message: §11 closes a finding to type and schema
                # facts, and a message is unbounded text this module cannot bound.
                observed=str(verdict.get("observed") or "unknown") or "unknown",
                count=1))

    return ValidationReportV1(
        report_id=report_id, generation_id=generation_id, run_id=None,
        generated_project_hash=lock.generated_project_hash,
        group_plan_hash=lock.compilation.group_plan_hash, level=ValidationLevel.L0,
        environment_id=environment_id,
        status=ValidationStatus.FAILED if findings else ValidationStatus.PASSED,
        started_at=started_at, finished_at=clock(), findings=tuple(findings))
