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

**A build proof is a proof about ONE environment, and the artifact names which.** The project pins
itself — ``requirements.lock``, rendered from ``ClusterInventoryV1.engine_versions`` and inside
``generated_project_hash`` — and until DEFERRED-WORK A.42 nothing compared those pins to the
interpreter doing the proving, so a project declaring engines the prover did not have came back
``PASSED``. That was the one failure in this chain that failed OPEN. The probe therefore reads the
declared pins and compares them to ``importlib.metadata`` **in its own interpreter**, and reports
``ENGINE_VERSION_MISMATCH`` — one finding per disagreeing distribution, naming the package, the pin
and what is installed — before it imports anything. Before, because the check must not be
answerable by code the artifact ships, and because a build that failed *because* the engines are
wrong would otherwise be filed as ``RENDERER_DEFECT`` and send an operator to fix a renderer that
did nothing wrong.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from featuregen.contracts.db import DbConn
from featuregen.materialize.codes import ValidationFindingCode
from featuregen.materialize.identity import (
    GENERATED_LOCK_FILENAME,
    REQUIREMENTS_LOCK_FILENAME,
    RenderedArtifactIdentity,
    generated_project_hash,
    read_lock,
)
from featuregen.materialize.inventory import ClusterInventoryV1
from featuregen.materialize.ir import FormulaExecutionIRV1
from featuregen.materialize.runprep import (
    SPINE_FEATURE_NAME,
    MetastorePartitions,
    PhysicalInputSnapshot,
)
from featuregen.materialize.spine import SpineSpec

__all__ = [
    "FINDING_CLASSES",
    "ClusterUnreachable",
    "FindingClass",
    "FindingSeverity",
    "MetastoreMetadata",
    "ValidationFinding",
    "ValidationLevel",
    "ValidationReportV1",
    "ValidationStatus",
    "classify",
    "may_regenerate",
    "may_regenerate_for",
    "read_validation_reports",
    "record_validation_report",
    "run_l0",
    "run_l1",
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
    """§11.2's routing: not how bad a finding is, but WHOSE problem it is.

    **"Blocks regeneration" below is a POLICY these classes declare, not a gate they currently
    close.** :func:`may_regenerate` and :func:`may_regenerate_for` implement it correctly and are
    called only by tests; as of G-1 the chain decides on ``report.status is PASSED`` alone and reads
    no class. Stated here as well as at the two functions because a reader meets the vocabulary
    first, and DEFERRED-WORK A.42 exists precisely because something claimed more than it did.
    """

    #: The renderer emitted something that does not build. Fix the renderer, regenerate.
    RENDERER_DEFECT = "renderer_defect"
    #: The catalog and the cluster disagree about a governed fact. Re-attest. Declares that
    #: regeneration is blocked (see the class docstring: declared, not yet enforced).
    GOVERNED_FACT_MISMATCH = "governed_fact_mismatch"
    #: The environment or the data is not in the state the run needs. The operator acts.
    ENVIRONMENT_OR_DATA = "environment_or_data"
    #: Unrecognized. FAILS CLOSED — declares regeneration blocked, and is never silently
    #: environmental.
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
#:
#: ``ENGINE_VERSION_MISMATCH`` is a GOVERNED_FACT_MISMATCH on this table's own criterion, and the
#: reasoning is worth stating because two other classes look plausible. The pins are not the
#: renderer's opinion — it copied them faithfully out of §0's captured
#: ``ClusterInventoryV1.engine_versions`` — so ``RENDERER_DEFECT`` would send a reader to fix code
#: that is correct. And it is not ``ENVIRONMENT_OR_DATA``, which that class is reserved for by the
#: test that pins ``PROJECT_HASH_MISMATCH`` there: regeneration must be a legitimate remedy. Here it
#: is not. Re-rendering from the same mounted inventory produces the same three pins and the same
#: disagreement — "rebuilding from the same wrong facts produces the same wrong project faster",
#: which is precisely GOVERNED_FACT_MISMATCH. The remedy is to re-capture the inventory or to point
#: L0 at the environment the artifact declares, and a later L0 that passes supersedes this one
#: through :func:`may_regenerate_for`, so the refusal is a hold rather than a dead end.
#:
#: **What "blocks regeneration" means here today: the classification is DECLARED, not enforced.**
#: :func:`may_regenerate` and :func:`may_regenerate_for` have no production callers as of G-1 — the
#: chain decides on ``report.status is PASSED`` alone (``chain.py``'s ``built``) and never consults
#: a class. So this entry states the policy a regeneration path must honour when one is wired; it
#: does not gate anything yet. Said explicitly because A.42 exists precisely because something
#: claimed more than it did, and repeating that shape in its own fix would be the same defect.
FINDING_CLASSES: Mapping[ValidationFindingCode, FindingClass] = {
    ValidationFindingCode.PROJECT_DOES_NOT_BUILD: FindingClass.RENDERER_DEFECT,
    ValidationFindingCode.PIPELINE_NOT_CONSTRUCTIBLE: FindingClass.RENDERER_DEFECT,
    ValidationFindingCode.PROJECT_HASH_MISMATCH: FindingClass.ENVIRONMENT_OR_DATA,
    ValidationFindingCode.ENGINE_VERSION_MISMATCH: FindingClass.GOVERNED_FACT_MISMATCH,
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

    **NOTHING IN PRODUCTION CALLS THIS YET.** Its only callers are tests; the chain gates on
    ``report.status is PASSED`` and never consults a class. So this function is the §11.2 rule
    written down and proved, not a door that is currently shut — see DEFERRED-WORK A.42's 🟡 row.
    The first code that regenerates a refused artifact (G-2's re-drive) should route through
    :func:`may_regenerate_for`; until it does, do not describe a finding as "blocking regeneration"
    without that qualification.
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


def _finding_from_payload(payload: Mapping[str, Any]) -> ValidationFinding:
    """The exact inverse of :meth:`ValidationFinding.payload`.

    ``classification`` is deliberately NOT read back: it was derived from ``code`` on the way out
    and the constructor derives it again on the way in, so a stored row cannot smuggle in a routing
    its own code contradicts.
    """
    return ValidationFinding(
        code=ValidationFindingCode(payload["code"]),
        location=payload["location"],
        expected=payload["expected"],
        observed=payload["observed"],
        count=payload["count"],
        severity=FindingSeverity(payload["severity"]))


def read_validation_reports(conn: DbConn, *, generation_id: str
                            ) -> tuple[ValidationReportV1, ...]:
    """Every report recorded for one generation, oldest ``started_at`` first.

    The exact inverse of :func:`record_validation_report`: the eleven columns it INSERTs come back
    through the shapes they went in — the enums through their enums, the findings through the
    inverse of :meth:`ValidationReportV1.findings_payload` — so a read-back report compares EQUAL
    (``==`` over the frozen dataclass) to the one recorded, and every constructor invariant
    (``error`` has no findings, ``failed`` has at least one) re-runs on the way out. ``started_at``
    is ISO-8601 text, so its lexicographic order is its chronological order; ``recorded_at`` and
    ``report_id`` only make a tie deterministic.
    """
    rows = conn.execute(
        "SELECT report_id, generation_id, run_id, generated_project_hash, group_plan_hash, "
        "level, environment_id, status, started_at, finished_at, findings "
        "FROM pipeline_validation_report WHERE generation_id = %s "
        "ORDER BY started_at, recorded_at, report_id",
        (generation_id,)).fetchall()
    return tuple(
        ValidationReportV1(
            report_id=row[0], generation_id=row[1], run_id=row[2],
            generated_project_hash=row[3], group_plan_hash=row[4],
            level=ValidationLevel(row[5]), environment_id=row[6],
            status=ValidationStatus(row[7]), started_at=row[8], finished_at=row[9],
            findings=tuple(_finding_from_payload(payload) for payload in row[10]))
        for row in rows)


def may_regenerate_for(conn: DbConn, *, generation_id: str) -> bool:
    """The CROSS-PROCESS form of :func:`may_regenerate` — the recorded rule, not the in-memory one.

    :func:`may_regenerate` judges the report object its caller happens to hold, which binds only
    the process that ran the validation. This reads what was RECORDED and applies the same rule to
    the NEWEST report of each level: a re-validation re-checked the findings of the report it
    supersedes, so a superseded blocker no longer blocks — and every level's newest verdict must
    permit regeneration, because L1's clean re-run says nothing about L0's standing refusal. No
    recorded reports block nothing: ``True``.
    """
    newest: dict[ValidationLevel, ValidationReportV1] = {}
    for report in read_validation_reports(conn, generation_id=generation_id):
        newest[report.level] = report        # ordered oldest-first, so the last one seen is newest
    return all(may_regenerate(report) for report in newest.values())


class ClusterUnreachable(Exception):
    """The environment could not be asked — NOT a verdict about what it contains.

    Raised by a metastore adapter when the cluster is down, unauthenticated or unroutable, and the
    ONLY exception L1 converts into ``status="error"``. Anything else propagates: catching bare
    ``Exception`` would turn a defect in this module into "the cluster was unreachable", which is an
    invented verdict of exactly the kind §11.2 forbids.
    """


# ── L0: the generated project itself ─────────────────────────────────────────────────────────────

#: The fixed PREFIX of the marker the build probe prints before its verdict, kept stable so a human
#: can find the line in a log. The marker the probe is actually GIVEN — and the only string the
#: scan matches — is minted fresh per invocation (:func:`_verdict_marker`): a project that prints
#: during import is legal, but a project that printed a fixed, verdict-shaped line would otherwise
#: be able to declare itself buildable, and the nonce did not exist until the probe was launched.
_VERDICT_MARKER_PREFIX = "@@L0-VERDICT@@"


def _verdict_marker() -> str:
    """One probe invocation's marker. Fresh every call — yesterday's log teaches a forger nothing.

    The trailing space is the delimiter between the marker and the verdict JSON, exactly as the
    probe prints it (``MARKER + json.dumps(...)``).
    """
    return f"{_VERDICT_MARKER_PREFIX}{uuid.uuid4().hex} "

#: The probe's stage for the declared-pin comparison. Bound INTO the probe's source below rather
#: than spelled a second time inside it: :func:`run_l0` dispatches on this exact value, and a stage
#: the two sides spelled differently would silently route an engine mismatch to
#: ``PIPELINE_NOT_CONSTRUCTIBLE`` — a wrong code that still fails, which is the kind of drift no
#: test notices.
_ENGINE_STAGE = "engines"

#: The probe. Stdlib ONLY and run in ANOTHER interpreter, which is what lets the suite exercise it
#: against ``sys.executable`` and hand-authored projects while the gate runs the identical code
#: against the environment that has ``kedro`` and ``pyspark``. It duck-types the registry's answer
#: rather than importing ``kedro.pipeline.Pipeline``: importing it here would put this platform's
#: control plane one dependency away from the artifact it validates.
#:
#: **The engine comparison is IN HERE rather than around this call, and it is FIRST.** In here,
#: because ``importlib.metadata`` answers for the interpreter it is executed by: the alternative —
#: a second ``subprocess`` from :func:`run_l0` asking the same ``python_executable`` what it has —
#: proves something about a second process that is merely BELIEVED to be the one that built, and a
#: wrapper script, a re-pointed symlink or an install landing between the two launches makes that
#: belief false. That is A.42's own defect at a smaller scale, and it would need a second "the
#: interpreter never answered" channel besides :func:`_probe_verdict`'s ``None``.
#:
#: First, so that no module the artifact ships has executed when the pins are read — which is
#: exactly the residual forgery :func:`_probe_verdict` documents and cannot close for the build
#: verdict. **Ordering alone is not enough, and two successive reviews found that out the hard way;
#: both holes are recorded because the reasoning that produced them is more instructive than the
#: patch.**
#:
#: 1. ``importlib.metadata`` SCANS ``sys.path``, and the probe is launched ``python -c`` with
#:    ``cwd=root``, so **the project root is already at ``sys.path[0]`` before the probe's first
#:    statement**. A tree shipping a root-level ``kedro.egg-info/`` answered for its own pin — and
#:    because :func:`_files_on_disk` deliberately skips ``*.egg-info`` (an editable install writes
#:    one by *using* a project, so it is correctly not drift), that forgery was invisible to
#:    ``PROJECT_HASH_MISMATCH`` too: ``PASSED``, zero findings. A.42 through its own fix.
#: 2. The probe's own first line was
#:    ``import importlib, importlib.metadata, json, os.path, sys, traceback`` — and it ran on that
#:    same unsanitized path. A tree shipping ``<root>/json.py`` therefore executed arbitrary code
#:    with the live verdict nonce in ``sys.argv[1]`` and forged a passing build verdict. Strictly
#:    weaker than (1): ``json.py`` IS inside ``generated_project_hash``, so a dropped-in copy is
#:    ``PROJECT_HASH_MISMATCH`` and :func:`~featuregen.materialize.compile.chain._prove_the_build`'s
#:    identity check keeps the chain honest — it needs an adversary-SEALED tree, not a hand edit.
#:    Weaker, but it falsified the sentence above, and a false comment is worse than none.
#:
#: So the probe imports ``sys`` alone (a built-in: unshadowable), REMOVES every ``sys.path`` entry
#: that lies inside the project, imports everything else, asks, and restores the list exactly before
#: it imports the artifact. Sanitizing inside the probe rather than launching it differently,
#: deliberately:
#:
#: * it is COMPLETE — it does not matter how an entry under ``root`` got onto the path (the ``-c``
#:   cwd entry, a caller's ``PYTHONPATH``, a future interpreter's own additions); ``-P`` /
#:   ``PYTHONSAFEPATH`` close only the cwd door and leave ``PYTHONPATH=<root>`` wide open;
#: * it does not depend on HOW the probe was launched, so it cannot be undone by a caller;
#: * ``-P`` is 3.11+, and passing it to an older L0 interpreter would fail the launch and be
#:   reported as "the environment did not answer" — a regression dressed as a hardening;
#: * ``PYTHONSAFEPATH=1`` in the env would silently change ``run_l0``'s documented contract that
#:   ``env=None`` inherits this process's environment unchanged, and still closes only one door.
#:
#: The restore is exact. This narrows the METADATA QUERY; the build that follows must import the
#: project under precisely the path it always did, or the engine fix would have changed what
#: ``PROJECT_DOES_NOT_BUILD`` means.
_BUILD_PROBE = f"ENGINE_STAGE = {_ENGINE_STAGE!r}\n" + r'''
# `sys` ONLY, and nothing else yet. It is a built-in module (`sys.builtin_module_names`), so it can
# never be loaded from `sys.path` and no tree can shadow it. Every other import waits until the
# project is off the path: this probe's own first line used to be
# `import importlib, importlib.metadata, json, os.path, sys, traceback`, executed while `cwd=root`
# still had the project at `sys.path[0]`, so a tree shipping `<root>/json.py` ran arbitrary code —
# with the live verdict nonce in `sys.argv[1]` — and forged a passing build verdict.
import sys

MARKER, root, package, lockfile = sys.argv[1], sys.argv[2], sys.argv[3], sys.argv[4]

# Pass 1, with NOTHING imported: drop the cwd entries. `python -c` puts "" at `sys.path[0]` and
# `_probe_verdict` launches with `cwd=root`, so those ARE the project. Done with pure `sys` because
# importing anything at all — even `os` — is what pass 2 needs and pass 1 must therefore precede.
whole_path = list(sys.path)
sys.path[:] = [entry for entry in whole_path if entry not in ("", ".")]

# Safe now: `os` cannot be resolved from the tree. (`os` is normally already in `sys.modules` from
# `site`, but this does not rely on that.)
import os.path

# Pass 2: every remaining entry that RESOLVES inside the project — a caller's `PYTHONPATH=<root>`,
# a relative entry (`cwd` is the project, so it resolves inside), `<root>/./`, `<root>/src/../`, a
# symlink whose target is the tree, a nested directory. `realpath` collapses all of those and the
# `+ os.sep` guard stops `<root>-sibling` matching as if it were inside.
#
# DELIBERATELY NOT stripped: an entry that merely passes THROUGH the tree by symlink to a target
# outside it. `realpath` resolves to the target, the target is not the artifact, and the artifact
# cannot create it — a project cannot author a `sys.path` entry, only files under its own root.
inside = os.path.realpath(root)
sys.path[:] = [entry for entry in sys.path
               if os.path.realpath(entry) != inside
               and not os.path.realpath(entry).startswith(inside + os.sep)]

# The rest, resolved with the project off the path — so these are the interpreter's own modules.
import importlib, importlib.metadata, json, traceback

importlib.invalidate_caches()


def emit(stage, ok, observed="", pins=()):
    print(MARKER + json.dumps(
        {"stage": stage, "ok": ok, "observed": observed, "pins": [list(pin) for pin in pins]}))
    raise SystemExit(0)


try:
    with open(os.path.join(root, lockfile), encoding="utf-8") as handle:
        lock_lines = handle.read().splitlines()
except (OSError, UnicodeDecodeError):
    # UnicodeDecodeError is a ValueError, NOT an OSError. Uncaught it kills the probe, prints no
    # verdict, and `run_l0` reports "the environment did not answer" — blaming the interpreter for
    # a lock the ARTIFACT wrote unreadable, which is the mis-routing this whole change exists to
    # stop. It is a finding about the project, and the tree's PROJECT_HASH_MISMATCH survives it.
    emit(ENGINE_STAGE, False, lockfile + " could not be read")

declared = []
for lock_line in lock_lines:
    requirement = lock_line.split("#", 1)[0].strip()
    if "==" not in requirement:
        continue
    name, _, pinned = requirement.partition("==")
    # The pin names a DISTRIBUTION, which is not an import name: `kedro-datasets` installs the
    # module `kedro_datasets`. `importlib.metadata.version` is asked for the distribution and
    # normalizes the spelling itself; importing the name would fail on the dash, and reading a
    # module's `__version__` would trust an attribute the package may ship stale or not at all.
    name = name.split(";", 1)[0].split("[", 1)[0].strip()          # drop markers and extras
    pinned = pinned.split(";", 1)[0].strip()
    if name and pinned:
        declared.append((name, pinned))

if not declared:
    # Fails CLOSED. A project that declares no environment cannot have its prover checked against
    # one, and "nothing to compare" must not read as "they agree" — that IS A.42.
    emit(ENGINE_STAGE, False, lockfile + " pins no distribution")

disagreements = []
for name, pinned in sorted(set(declared)):
    try:
        installed = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        installed = None
    # EXACT string equality, on a name `importlib.metadata` has already normalized (PEP 503, so
    # `kedro-datasets` and `kedro_datasets` are one distribution). Versions are NOT normalized:
    # `1.5` vs `1.5.0` and `1.5.0RC1` vs `1.5.0rc1` are PEP 440-equal and are reported here as
    # disagreements. That is FALSE POSITIVES ONLY — no version string can slip through by being
    # merely equivalent — which is the direction to be wrong in, and the alternative is `packaging`
    # inside a stdlib-only probe. A capture that writes `kedro: "1.5"` renders a lock that can
    # never pass; the fix is to capture the full version. Note also that a `--hash=` annotation or
    # an unevaluated environment marker rides along in `pinned` and always disagrees: today's
    # `_render_requirements` emits bare `name==version`, and a pip-compile-style lock would need
    # this parser widened rather than discovering it as a mystery refusal.
    if installed != pinned:
        disagreements.append([name, pinned, installed])

# Restored EXACTLY, before anything is imported: the narrowing above is scoped to the metadata
# query, and the build must run under the path it always did.
sys.path[:] = whole_path
importlib.invalidate_caches()

if disagreements:
    emit(ENGINE_STAGE, False,
         str(len(disagreements)) + " of " + str(len(set(declared))) + " declared pin(s) disagree",
         disagreements)

sys.path.insert(0, root + "/src")

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
                   timeout_seconds: float,
                   env: Mapping[str, str] | None) -> dict[str, Any] | None:
    """Run the probe in its own interpreter. ``None`` means the environment never answered.

    ``None`` is deliberately distinct from every verdict the probe can print: an interpreter that
    could not be launched, a probe killed by a timeout and a probe whose output carries no verdict
    line are all *the validation did not run*, which §11.2 records as ``status="error"`` with no
    findings. Turning any of them into ``PROJECT_DOES_NOT_BUILD`` would blame the artifact for the
    environment.

    The verdict line is matched against a marker minted for THIS invocation and handed to the probe
    through its argv. That defeats fixed-string forgery and log replay — a project that prints a
    verdict-shaped line carrying the well-known prefix, or one copied from an earlier run's log,
    matches nothing. It does NOT defeat an in-interpreter echo: the probe imports the project in its
    own interpreter, so module code that reads ``sys.argv[1]`` at import time can print the live
    marker and forge a verdict. Closing that would mean not importing the artifact in the process
    that holds the marker, which is out of scope at this seam.

    The ENGINE verdict is decided before that exposure begins, but "by construction" was too strong
    a phrase and is not used here: it is true only because the probe now takes the project off
    ``sys.path`` before importing anything but ``sys``. Until it did, its own
    ``import … json …`` line ran off the artifact's root and could be answered by ``<root>/json.py``
    — the marker forged from inside the probe's *own* imports rather than the project's. The
    property is real; it rests on that ordering, not on the shape of the seam.

    The lock filename travels through argv rather than being spelled inside the probe's source, so
    the file the renderer WRITES and the file the probe READS are one constant
    (:data:`~featuregen.materialize.identity.REQUIREMENTS_LOCK_FILENAME`).
    """
    marker = _verdict_marker()
    try:
        completed = subprocess.run(                                       # noqa: S603 - fixed argv
            [python_executable, "-c", _BUILD_PROBE, marker, str(root), package,
             REQUIREMENTS_LOCK_FILENAME],
            capture_output=True, text=True, timeout=timeout_seconds, check=False,
            cwd=str(root), env=None if env is None else {**os.environ, **env})
    except (OSError, subprocess.SubprocessError):
        return None
    for line in reversed(completed.stdout.splitlines()):
        if line.startswith(marker):
            try:
                verdict = json.loads(line[len(marker):])
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


def _engine_findings(verdict: Mapping[str, Any]) -> list[ValidationFinding]:
    """The probe's engine verdict, as findings — ONE per disagreeing distribution.

    Per distribution rather than one aggregate, because each field of a finding has to stay a fact
    about one thing: an aggregate would have to write three pins into ``expected`` and three
    installed versions into ``observed``, and the reader would be back to parsing prose. Each
    finding names the package in all three of ``location``, ``expected`` and ``observed``, so no
    single field of it reads as a bare "mismatch" — recreating A.42's own complaint one level up.

    The fallback is not defensive padding. The probe emits this stage for two conditions that name
    no package at all — the lock could not be read, and the lock pins nothing — and both must still
    produce a finding, because ``FAILED`` with an empty findings tuple is refused by
    :class:`ValidationReportV1` and would abort the validation with a ``ValueError`` instead of
    reporting it. Any unrecognized payload lands there too, which is the fail-closed answer.
    """
    findings: list[ValidationFinding] = []
    for entry in verdict.get("pins") or ():
        if not isinstance(entry, list | tuple) or len(entry) != 3:
            continue
        name, pinned, installed = entry
        if not isinstance(name, str) or not name.strip() or \
                not isinstance(pinned, str) or not pinned.strip():
            continue
        findings.append(ValidationFinding(
            code=ValidationFindingCode.ENGINE_VERSION_MISMATCH,
            location=f"{REQUIREMENTS_LOCK_FILENAME}:{name}",
            expected=f"{name}=={pinned}",
            observed=(f"{name} is not installed" if installed is None
                      else f"{name}=={installed}"),
            count=1))
    if findings:
        return findings
    return [ValidationFinding(
        code=ValidationFindingCode.ENGINE_VERSION_MISMATCH,
        location=REQUIREMENTS_LOCK_FILENAME,
        expected="a <distribution>==<version> pin for every engine the project runs on",
        observed=str(verdict.get("observed") or "unknown") or "unknown",
        count=1)]


def run_l0(
    root: str | os.PathLike[str],
    *,
    generation_id: str,
    environment_id: str,
    report_id: str,
    python_executable: str,
    clock: Callable[[], str],
    env: Mapping[str, str] | None = None,
    timeout_seconds: float = 300.0,
) -> ValidationReportV1:
    """L0 (§11.2): the project hashes to its lock, is proved in the environment it DECLARES, and
    imports and yields a pipeline there.

    It takes a DIRECTORY, not a
    :class:`~featuregen.materialize.identity.SealedProject`. A sealed object's files are by
    construction the ones its lock was computed from, so ``PROJECT_HASH_MISMATCH`` would be
    unreachable through it — and a hand-edited project, which is the whole reason that code exists,
    only exists on disk. Materializing a sealed project to a temporary directory is
    :func:`~featuregen.materialize.render.project.materialize_to`; L0 validates what that wrote.

    The hash check and the probe always both run: a project may be edited *and* fail to build, and
    reporting one of the two would send an operator to fix the half that was visible. Inside the
    probe the ENGINE comparison is the exception — it short-circuits, because the build verdict
    that would follow it is not a fact about the artifact but about the wrong environment, and this
    module does not record verdicts nobody can act on. ``ENGINE_VERSION_MISMATCH`` is therefore the
    only L0 code that appears without one of the build codes beside it.

    Args:
        root: the materialized project.
        generation_id: the generation these bytes were rendered for.
        environment_id: the environment the report is keyed on.
        report_id: supplied, never minted — this module has no id factory, matching
            :mod:`featuregen.materialize.control_plane`.
        python_executable: the interpreter the project is imported in. It is a parameter because
            the generated project imports ``kedro`` and ``pyspark``, which this platform does not
            depend on; there is no default, so nothing can silently validate the artifact against
            the validator's own environment. It is also the interpreter whose INSTALLED
            distributions the project's own ``requirements.lock`` is compared against, so handing
            L0 an interpreter other than the declared one now yields ``ENGINE_VERSION_MISMATCH``
            instead of a build proof about somewhere else.
        clock: read twice — once before the work and once after — so ``finished_at`` is the time
            the validation ended rather than a value the caller supplied in advance.
        env: variables OVERLAID on this process's environment for the probe. The environment that
            has ``pyspark`` needs several of them, and the one that costs a debugging cycle is that
            ``PYSPARK_PYTHON`` and ``PYSPARK_DRIVER_PYTHON`` must BOTH name the same interpreter —
            without them Spark launches workers on the system Python and its own ``types.py`` dies
            on ``X | Y``. ``None`` inherits this process's environment unchanged.
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
                                 timeout_seconds=timeout_seconds, env=env)
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
        if verdict.get("stage") == _ENGINE_STAGE:
            # The probe stopped BEFORE importing anything, so there is no build verdict to report
            # and none is invented. A build attempted in an interpreter the artifact does not
            # declare would fail for reasons that are not the renderer's, and filing that as
            # PROJECT_DOES_NOT_BUILD (a RENDERER_DEFECT) is the mis-routing this ordering avoids.
            # Dispatched on the STAGE and not on `ok`: the probe emits this stage only to refuse,
            # so an "engines are fine" verdict is not one it can produce — and a forged one lands
            # on `_engine_findings`' fallback and still FAILS, rather than skipping both checks.
            findings.extend(_engine_findings(verdict))
        elif not verdict.get("ok"):
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


# ── L1: the physical inputs the run will actually read ───────────────────────────────────────────


class MetastoreMetadata(MetastorePartitions, Protocol):
    """The three questions L1 asks the environment — and the fourth it deliberately cannot.

    Partitions, columns-and-physical-types, and whether a set of roles may read the table. Nothing
    here returns a row: §11.2 says L1 reads metadata only, and a seam with a row-returning method
    would make that a review convention rather than a property. It is also why a finding *cannot*
    carry a data value — counts, types and locations are all this seam can produce.

    It extends :class:`~featuregen.materialize.runprep.MetastorePartitions` rather than restating
    ``list_partitions``: run preparation resolves the partitions and L1 checks the same ones exist,
    so a second declaration of that method would be a second chance to disagree about its shape.
    """

    def describe_table(self, *, schema: str, table: str
                       ) -> Sequence[tuple[str, str]] | None:
        """Ordered ``(column, physical type)`` for one table, or ``None`` when it does not exist."""
        ...

    def can_read(self, *, schema: str, table: str, roles: Sequence[str]) -> bool:
        """Whether these roles may read this table — a metadata question, not a read."""
        ...


def _fold(identifier: str) -> str:
    return identifier.strip().lower()


def _physical_type(declared: str) -> str:
    """Compare physical types the way a metastore prints them: case- and space-insensitive.

    ``DECIMAL(18, 2)`` and ``decimal(18,2)`` are one type, and reporting them as a contradiction
    would send an operator to re-attest a column nothing is wrong with.
    """
    return "".join(declared.split()).lower()


def _declared_columns(inventory: ClusterInventoryV1, schema: str, table: str) -> dict[str, str]:
    """What the §0 inventory declares this table's columns to be, DATA and partition together.

    ``TableLayout`` lists the two separately because they answer different questions; the read set
    does not care, and a read of a partition column would otherwise find no declared type at all.
    """
    layout = inventory.layout_for(schema, table)
    if layout is None:
        return {}
    return {_fold(name): physical_type
            for name, physical_type in (*layout.columns, *(layout.partition_columns or ()))}


def _canonical_partition(columns: Sequence[tuple[str, str]]) -> tuple[tuple[str, str], ...]:
    """A partition as it is IDENTIFIED — its column/value pairs, ordered by column.

    ``PartitionSpec`` keeps the metastore's order because that is how a partition is ADDRESSED, and
    addressing is the renderer's concern. L1 asks only whether the partition exists, and two
    listings of one partition that disagree about column order still name the same partition, so
    sorting here removes a false ``PARTITION_ABSENT`` rather than hiding a real one.
    """
    return tuple(sorted((_fold(str(column)), str(value)) for column, value in columns))


def _read_set(irs: Sequence[FormulaExecutionIRV1], spine: SpineSpec,
              spine_table: tuple[str, str]) -> dict[tuple[str, str], list[str]]:
    """Every column the run reads, per physical table — EVERY IR, every expression, and the spine.

    The spine's refs are catalog-side (§3.5 flattens their schema segment), so only the column
    segment is taken from them; the physical table is the one its own resolved requirement names.

    Keys are CASE-FOLDED, exactly as every column comparison already is: unquoted Hive identifiers
    fold, so ``RISK.TXN`` and ``risk.txn`` are one table, and unfolded keys would ask the metastore
    about it twice and report each answer as its own finding.
    """
    read: dict[tuple[str, str], list[str]] = {}

    def add(schema: str, table: str, column: str) -> None:
        columns = read.setdefault((_fold(schema), _fold(table)), [])
        if column not in columns:
            columns.append(column)

    for ir in irs:
        for expression in ir.expressions:
            for ref in expression.physical_read_set:
                if ref.column:
                    add(ref.schema, ref.table, ref.column)
    for spine_ref in spine.read_set:
        add(spine_table[0], spine_table[1], spine_ref.rsplit(".", 1)[-1])
    return read


def _spine_of(irs: Sequence[FormulaExecutionIRV1]) -> SpineSpec:
    if not irs:
        raise ValueError(
            "L1 was given no IRs: §11.2 runs it over every feature IR, every expression AND the "
            "spine, and a run with no features has no compiled spine to check either")
    spine = irs[0].spine
    if any(ir.spine != spine for ir in irs):
        raise ValueError(
            "the IRs carry different spines: a materialization contract declares ONE population "
            "(§4), and validating one of them would leave the other's inputs unchecked")
    return spine


def _spine_table(snapshots: Sequence[PhysicalInputSnapshot]) -> tuple[str, str]:
    """The physical table the spine reads, from its own snapshot — which must be present.

    Required rather than optional. §11.2 says L1 covers the spine, and a validator that skipped it
    when the caller forgot :func:`~featuregen.materialize.runprep.spine_input_request` would make
    that coverage a claim nothing enforces: the population would be the one part of the run whose
    inputs were never checked, and it is the part that decides which entities exist at all.
    """
    for snapshot in snapshots:
        if snapshot.feature_name == SPINE_FEATURE_NAME:
            return (snapshot.requirement.schema, snapshot.requirement.table)
    raise ValueError(
        f"no snapshot names the spine ({SPINE_FEATURE_NAME!r}): §11.2's L1 covers every IR, every "
        f"expression and the SPINE, so a run whose population was never resolved cannot be "
        f"validated — `runprep.spine_input_request` builds the request this needs")


def run_l1(
    rendered: RenderedArtifactIdentity,
    snapshots: Sequence[PhysicalInputSnapshot],
    *,
    irs: Sequence[FormulaExecutionIRV1],
    inventory: ClusterInventoryV1,
    metastore: MetastoreMetadata,
    roles: Sequence[str],
    generation_id: str,
    run_id: str,
    report_id: str,
    clock: Callable[[], str],
) -> ValidationReportV1:
    """L1 (§11.2): metastore METADATA only, over every IR, every expression and the spine.

    Three questions per physical table — may these roles read it, does every read-set column exist
    with the type the environment declares, and does every partition this run resolved exist — and
    the partitions are asked per SNAPSHOT rather than per table, because two expressions over one
    table under two windows resolved two different partition sets and checking their union would
    report neither honestly.

    The spine is not a parameter: it is read off the IRs (which must all agree on it) and its
    physical table off its own snapshot, which must be present. A caller cannot therefore validate a
    population other than the compiled one, nor omit it.

    ``environment_id`` is read off the inventory for
    :func:`~featuregen.materialize.runprep.prepare_run`'s reason: a report cannot be filed against
    one environment while the checks were run against another's declarations.

    Returns:
        A report. ``status="error"`` with **zero** findings when the cluster could not be asked —
        including findings already collected, since a validation that could not complete reports
        nothing rather than the half of itself that finished.

    Raises:
        ValueError: ``irs`` is empty, the IRs disagree about the spine, or no snapshot names it.
        Exception: anything the metastore adapter raises that is not
            :class:`ClusterUnreachable` — a defect in the adapter is not a verdict about the
            cluster, and converting one into the other is how an invented verdict gets recorded.
    """
    spine = _spine_of(irs)
    spine_table = _spine_table(snapshots)
    started_at = clock()

    def report(status: ValidationStatus,
               findings: tuple[ValidationFinding, ...]) -> ValidationReportV1:
        return ValidationReportV1(
            report_id=report_id, generation_id=generation_id, run_id=run_id,
            generated_project_hash=rendered.generated_project_hash,
            group_plan_hash=rendered.compilation.group_plan_hash, level=ValidationLevel.L1,
            environment_id=inventory.environment_id, status=status, started_at=started_at,
            finished_at=clock(), findings=findings)

    findings: list[ValidationFinding] = []
    try:
        # Finding locations spell schema.table FOLDED (Hive-canonical, the read-set key) for the
        # read/column/type findings below, and in the snapshot requirement's OWN spelling for
        # PARTITION_ABSENT — intentional: two observed casings have no single observed spelling.
        denied: set[tuple[str, str]] = set()
        for (schema, table), columns in _read_set(irs, spine, spine_table).items():
            if not metastore.can_read(schema=schema, table=table, roles=tuple(roles)):
                denied.add((schema, table))
                findings.append(ValidationFinding(
                    code=ValidationFindingCode.READ_DENIED, location=f"{schema}.{table}",
                    expected=f"readable by {len(tuple(roles))} role(s)", observed="denied",
                    count=1))
                # No column checks for a denied table: a schema nobody may read is not a schema
                # that was observed, and reporting its columns absent would invent a second fault
                # out of the first one.
                continue
            observed = metastore.describe_table(schema=schema, table=table)
            if observed is None:
                findings.append(ValidationFinding(
                    code=ValidationFindingCode.COLUMN_ABSENT, location=f"{schema}.{table}",
                    expected=f"{len(columns)} column(s)", observed="the table does not exist",
                    count=len(columns)))
                continue
            present = {_fold(name): physical_type for name, physical_type in observed}
            declared = _declared_columns(inventory, schema, table)
            for column in columns:
                key = _fold(column)
                if key not in present:
                    findings.append(ValidationFinding(
                        code=ValidationFindingCode.COLUMN_ABSENT,
                        location=f"{schema}.{table}.{column}",
                        expected=declared.get(key, "a column of this table"), observed="absent",
                        count=1))
                    continue
                # Only when the environment DECLARES a type: the inventory is what states one, and
                # an undeclared column can be checked for existence and nothing more.
                if key in declared and \
                        _physical_type(declared[key]) != _physical_type(present[key]):
                    findings.append(ValidationFinding(
                        code=ValidationFindingCode.COLUMN_TYPE_MISMATCH,
                        location=f"{schema}.{table}.{column}",
                        expected=declared[key], observed=present[key], count=1))

        live: dict[tuple[str, str], set[tuple[tuple[str, str], ...]]] = {}
        for snapshot in snapshots:
            if snapshot.partition_specs is None:
                continue
            physical = (snapshot.requirement.schema, snapshot.requirement.table)
            # `denied` and `live` are keyed by the FOLDED pair (the read-set loop's keys), so a
            # requirement spelling the same table in another case still hits the denial and the
            # cached partition listing; the finding below keeps the requirement's own spelling.
            folded = (_fold(physical[0]), _fold(physical[1]))
            if folded in denied:
                continue
            if folded not in live:
                live[folded] = {
                    _canonical_partition(partition) for partition
                    in metastore.list_partitions(schema=physical[0], table=physical[1])}
            absent = [spec for spec in snapshot.partition_specs
                      if _canonical_partition(spec.columns) not in live[folded]]
            if absent:
                findings.append(ValidationFinding(
                    code=ValidationFindingCode.PARTITION_ABSENT,
                    location=(f"{snapshot.feature_name}/{snapshot.expr_path} "
                              f"{physical[0]}.{physical[1]}"),
                    expected=f"{len(snapshot.partition_specs)} partition(s)",
                    observed=f"{len(snapshot.partition_specs) - len(absent)} present",
                    count=len(absent)))
    except ClusterUnreachable:
        return report(ValidationStatus.ERROR, ())

    return report(ValidationStatus.FAILED if findings else ValidationStatus.PASSED,
                  tuple(findings))
