"""Spec A Task 15b — §11.2's validation loop: the finding vocabulary, L0 and L1.

Three properties carry the whole task, and each is asserted directly rather than through a
"there were findings" check that would pass against almost any implementation:

1. **A finding's CLASS is what routes the fix**, so every test names the exact
   :class:`ValidationFindingCode` *and* the exact :class:`FindingClass`. A suite that only counted
   findings would pass against a ``classify`` that answered ``ENVIRONMENT_OR_DATA`` for everything —
   and that mistake is precisely the one that lets a governed-fact contradiction be regenerated away.
2. **Parsing is not building.** L0's whole point is the project that ``ast.parse`` accepts and that
   yields no pipeline: it must be ``PIPELINE_NOT_CONSTRUCTIBLE``, and the tests below build exactly
   that project rather than asserting the code path exists.
3. **A validation that could not run has not found nothing — it has found nothing OUT.** The
   unreachable case is ``status="error"`` with ZERO findings, and both halves are asserted every
   time, because either one alone is satisfied by a plain pass.
"""
from __future__ import annotations

import ast
import dataclasses
import inspect
import subprocess
import sys
import time

import pytest
from tests.featuregen.materialize.test_group_plan import catalog  # noqa: F401 - a fixture
from tests.featuregen.materialize.test_ir import INVENTORY, _compile
from tests.featuregen.materialize.test_ir import _ok as _compiled

from featuregen.materialize import validation
from featuregen.materialize.canonical import materialize_hash
from featuregen.materialize.codes import ValidationFindingCode
from featuregen.materialize.control_plane import MaterializationGeneration, record_generation
from featuregen.materialize.identity import (
    GENERATED_LOCK_FILENAME,
    CompilationIdentity,
    RenderedArtifactIdentity,
    read_lock,
    seal_project,
)
from featuregen.materialize.inputs import PhysicalInputRequirement
from featuregen.materialize.inventory import ClusterInventoryV1
from featuregen.materialize.render.project import materialize_to
from featuregen.materialize.runprep import (
    SPINE_FEATURE_NAME,
    RunInputRequest,
    resolve_snapshots,
    run_input_requests,
    spine_input_request,
)
from featuregen.materialize.validation import (
    FINDING_CLASSES,
    ClusterUnreachable,
    FindingClass,
    FindingSeverity,
    MetastoreMetadata,
    ValidationFinding,
    ValidationLevel,
    ValidationReportV1,
    ValidationStatus,
    classify,
    may_regenerate,
    may_regenerate_for,
    read_validation_reports,
    record_validation_report,
    run_l0,
    run_l1,
)

GEN = "gen-15b"
RUN = "run-15b"
PROJECT_HASH = "0" * 64
PLAN_HASH = "1" * 64
ENVIRONMENT = "hdfc-local"
T0 = "2026-07-28T09:00:00+00:00"
T1 = "2026-07-28T09:00:05+00:00"


def _finding(code: ValidationFindingCode, *, location: str = "banking.transactions",
             expected: str | None = None, observed: str | None = None,
             count: int = 1) -> ValidationFinding:
    return ValidationFinding(code=code, location=location, expected=expected, observed=observed,
                             count=count)


def _report(status: ValidationStatus, findings: tuple[ValidationFinding, ...] = (), *,
            level: ValidationLevel = ValidationLevel.L1) -> ValidationReportV1:
    return ValidationReportV1(
        report_id="rep-1", generation_id=GEN, run_id=RUN,
        generated_project_hash=PROJECT_HASH, group_plan_hash=PLAN_HASH, level=level,
        environment_id=ENVIRONMENT, status=status, started_at=T0, finished_at=T1,
        findings=findings)


# ── the classification, code by code ─────────────────────────────────────────────────────────────
#
# Every member is named individually. A parametrized sweep over the enum comparing against the same
# table the implementation uses would assert only that the table equals itself.


def test_a_type_contradiction_is_a_GOVERNED_FACT_MISMATCH() -> None:
    assert classify(ValidationFindingCode.COLUMN_TYPE_MISMATCH) is \
        FindingClass.GOVERNED_FACT_MISMATCH


def test_an_absent_COLUMN_is_a_GOVERNED_FACT_MISMATCH_not_an_environment_problem() -> None:
    """The catalog attests a column the cluster does not have: the two disagree about a FACT.

    Regenerating produces a project that reads the same absent column, which is why this one blocks
    regeneration while a missing PARTITION does not.
    """
    assert classify(ValidationFindingCode.COLUMN_ABSENT) is FindingClass.GOVERNED_FACT_MISMATCH


def test_a_denied_READ_is_a_GOVERNED_FACT_MISMATCH() -> None:
    """Gate 2 (§1.3) authorized the whole read set from governed read scope.

    A cluster that then denies it contradicts that authorization; the remedy is to reconcile the two,
    never to rebuild from the authorization that was already contradicted.
    """
    assert classify(ValidationFindingCode.READ_DENIED) is FindingClass.GOVERNED_FACT_MISMATCH


def test_a_missing_PARTITION_is_ENVIRONMENT_OR_DATA() -> None:
    """Nobody attests that a partition exists — it is data arrival, and the operator acts."""
    assert classify(ValidationFindingCode.PARTITION_ABSENT) is FindingClass.ENVIRONMENT_OR_DATA


def test_a_project_that_does_not_build_is_a_RENDERER_DEFECT() -> None:
    assert classify(ValidationFindingCode.PROJECT_DOES_NOT_BUILD) is FindingClass.RENDERER_DEFECT


def test_a_pipeline_that_cannot_be_constructed_is_a_RENDERER_DEFECT() -> None:
    assert classify(ValidationFindingCode.PIPELINE_NOT_CONSTRUCTIBLE) is \
        FindingClass.RENDERER_DEFECT


def test_a_hand_edited_project_is_ENVIRONMENT_OR_DATA_and_may_be_regenerated() -> None:
    """The renderer's bytes were right and something on disk changed them.

    Not ``RENDERER_DEFECT``: there is no renderer to fix. Not ``GOVERNED_FACT_MISMATCH``: the
    catalog and the cluster do not disagree about anything. Regeneration is the remedy, so the class
    must be one that permits it.
    """
    assert classify(ValidationFindingCode.PROJECT_HASH_MISMATCH) is FindingClass.ENVIRONMENT_OR_DATA
    assert may_regenerate(_report(ValidationStatus.FAILED,
                                  (_finding(ValidationFindingCode.PROJECT_HASH_MISMATCH),)))


def test_the_unknown_code_is_UNCLASSIFIED() -> None:
    assert classify(ValidationFindingCode.UNKNOWN_FINDING) is FindingClass.UNCLASSIFIED


def test_a_code_that_is_not_in_the_vocabulary_at_all_is_UNCLASSIFIED() -> None:
    """Fails CLOSED: an unrecognized code is never silently environmental."""
    assert classify("SOMETHING_NOBODY_DECLARED") is FindingClass.UNCLASSIFIED  # type: ignore[arg-type]
    assert classify(None) is FindingClass.UNCLASSIFIED  # type: ignore[arg-type]


def test_the_classification_table_covers_the_CLOSED_finding_vocabulary() -> None:
    """``==``, never ``>=``: a superset would permit an unmapped member to default silently."""
    assert set(FINDING_CLASSES) == set(ValidationFindingCode)


def test_every_finding_class_is_REACHED_by_some_code() -> None:
    """A class nothing maps to is vocabulary nobody can act on."""
    assert set(FINDING_CLASSES.values()) == set(FindingClass)


# ── regeneration ─────────────────────────────────────────────────────────────────────────────────


def test_a_governed_fact_mismatch_BLOCKS_regeneration() -> None:
    report = _report(ValidationStatus.FAILED,
                     (_finding(ValidationFindingCode.COLUMN_TYPE_MISMATCH,
                               expected="decimal(18,2)", observed="string"),))
    assert may_regenerate(report) is False


def test_an_UNCLASSIFIED_finding_BLOCKS_regeneration() -> None:
    report = _report(ValidationStatus.FAILED,
                     (_finding(ValidationFindingCode.UNKNOWN_FINDING),))
    assert may_regenerate(report) is False


def test_a_missing_partition_does_NOT_block_regeneration() -> None:
    report = _report(ValidationStatus.FAILED,
                     (_finding(ValidationFindingCode.PARTITION_ABSENT, count=3),))
    assert may_regenerate(report) is True


def test_ONE_blocking_finding_among_many_blocks_the_whole_report() -> None:
    """The verdict is over the report, not over the last finding read."""
    report = _report(ValidationStatus.FAILED, (
        _finding(ValidationFindingCode.PARTITION_ABSENT, count=3),
        _finding(ValidationFindingCode.COLUMN_ABSENT, location="banking.transactions.txn_amt"),
        _finding(ValidationFindingCode.PARTITION_ABSENT, location="banking.customers", count=1)))
    assert may_regenerate(report) is False


def test_a_passed_report_permits_regeneration() -> None:
    assert may_regenerate(_report(ValidationStatus.PASSED)) is True


def test_an_ERROR_report_does_NOT_permit_regeneration_even_with_zero_findings() -> None:
    """Zero findings under ``error`` is *nothing was looked at*, not *nothing is wrong*.

    Treating it as a pass is the exact confusion §11.2 closes, and it would let a regeneration be
    authorized by a validation that never ran.
    """
    report = _report(ValidationStatus.ERROR)
    assert report.findings == ()
    assert may_regenerate(report) is False


# ── the finding: a class it cannot contradict, and no data values ────────────────────────────────


def test_a_findings_class_is_DERIVED_from_its_code_and_cannot_be_supplied() -> None:
    """A finding carrying a class that disagrees with its code must be unconstructible."""
    assert "classification" not in inspect.signature(ValidationFinding).parameters
    finding = _finding(ValidationFindingCode.COLUMN_TYPE_MISMATCH)
    assert finding.classification is FindingClass.GOVERNED_FACT_MISMATCH


def test_a_findings_payload_carries_only_counts_types_and_locations() -> None:
    """The persisted shape is CLOSED, so a value-carrying field cannot be added quietly."""
    payload = _finding(ValidationFindingCode.COLUMN_TYPE_MISMATCH,
                       location="banking.transactions.txn_amt",
                       expected="decimal(18,2)", observed="string").payload()
    assert set(payload) == {"code", "severity", "classification", "location", "expected",
                            "observed", "count"}
    assert payload["code"] == "COLUMN_TYPE_MISMATCH"
    assert payload["classification"] == "governed_fact_mismatch"
    assert payload["expected"] == "decimal(18,2)"


def test_the_finding_fields_are_pinned_field_for_field() -> None:
    assert [f.name for f in dataclasses.fields(ValidationFinding)] == [
        "code", "location", "expected", "observed", "count", "severity"]


def test_a_finding_needs_a_code_from_the_CLOSED_vocabulary() -> None:
    with pytest.raises(TypeError, match="ValidationFindingCode"):
        ValidationFinding(code="COLUMN_ABSENT", location="x",  # type: ignore[arg-type]
                          expected=None, observed=None, count=1)


def test_a_finding_counting_nothing_is_refused() -> None:
    with pytest.raises(ValueError, match="count"):
        _finding(ValidationFindingCode.PARTITION_ABSENT, count=0)


# ── the report ───────────────────────────────────────────────────────────────────────────────────


def test_an_ERROR_report_carrying_findings_is_UNCONSTRUCTIBLE() -> None:
    """Mirrors migration 1034's own CHECK: a findings list under 'error' is a fabricated observation."""
    with pytest.raises(ValueError, match="error"):
        _report(ValidationStatus.ERROR, (_finding(ValidationFindingCode.PARTITION_ABSENT),))


def test_a_PASSED_report_carrying_findings_is_UNCONSTRUCTIBLE() -> None:
    with pytest.raises(ValueError, match="passed"):
        _report(ValidationStatus.PASSED, (_finding(ValidationFindingCode.PARTITION_ABSENT),))


def test_a_FAILED_report_with_no_findings_is_UNCONSTRUCTIBLE() -> None:
    """'Failed' with nothing to show is a verdict with no evidence behind it."""
    with pytest.raises(ValueError, match="failed"):
        _report(ValidationStatus.FAILED)


def test_the_report_fields_are_the_ones_section_11_names() -> None:
    assert [f.name for f in dataclasses.fields(ValidationReportV1)] == [
        "report_id", "generation_id", "run_id", "generated_project_hash", "group_plan_hash",
        "level", "environment_id", "status", "started_at", "finished_at", "findings"]


def test_the_levels_are_the_three_the_migration_allows() -> None:
    assert {level.value for level in ValidationLevel} == {"L0", "L1", "L2"}


def test_the_statuses_are_the_three_the_migration_allows() -> None:
    assert {status.value for status in ValidationStatus} == {"passed", "failed", "error"}


def test_L0_carries_no_run_id_because_the_project_is_validated_before_any_run_exists() -> None:
    report = ValidationReportV1(
        report_id="rep-l0", generation_id=GEN, run_id=None,
        generated_project_hash=PROJECT_HASH, group_plan_hash=PLAN_HASH, level=ValidationLevel.L0,
        environment_id=ENVIRONMENT, status=ValidationStatus.PASSED, started_at=T0, finished_at=T1,
        findings=())
    assert report.run_id is None


def test_a_blank_run_id_is_refused_because_it_is_not_the_same_as_ABSENT() -> None:
    with pytest.raises(ValueError, match="run_id"):
        ValidationReportV1(
            report_id="rep", generation_id=GEN, run_id="  ",
            generated_project_hash=PROJECT_HASH, group_plan_hash=PLAN_HASH,
            level=ValidationLevel.L1, environment_id=ENVIRONMENT,
            status=ValidationStatus.PASSED, started_at=T0, finished_at=T1, findings=())


# ── severity ─────────────────────────────────────────────────────────────────────────────────────


def test_severity_is_closed() -> None:
    assert {severity.value for severity in FindingSeverity} == {"error", "warning"}


def test_every_L0_and_L1_finding_this_slice_can_emit_is_an_ERROR() -> None:
    """``WARNING`` is L2's (§11.2, on demand), and this pins the claim rather than asserting it.

    Every L0/L1 code describes a condition under which the run cannot be trusted to produce correct
    output, so a warning-severity one would be a finding the operator is invited to ignore.
    """
    assert all(_finding(code).severity is FindingSeverity.ERROR
               for code in ValidationFindingCode)


# ── L0: parsing is not building ──────────────────────────────────────────────────────────────────
#
# Every project below is stdlib-only and hand-authored, so the suite proves L0's behaviour without
# importing `pyspark` or `kedro` and without paying for them. The generated project's own build is
# the L0 GATE (`tests/featuregen/materialize/l0_gate.py`), which runs against `.venv-l0`.

PACKAGE = "sandbox_l0_demo"

_PIPELINE_STUB = (
    "class Pipeline:\n"
    "    def __init__(self, nodes):\n"
    "        self.nodes = list(nodes)\n")


def _project_files(*, create_pipeline_body: str, registry_prelude: str = "") -> dict[str, str]:
    """A project laid out exactly as the renderer lays one out — importable, stdlib only."""
    root = f"src/{PACKAGE}"
    return {
        "README.md": "# a project\n",
        f"{root}/__init__.py": "",
        f"{root}/pipeline_registry.py": (
            registry_prelude
            + f"from {PACKAGE}.pipelines.materialize import create_pipeline\n"
            "\n"
            "\n"
            "def register_pipelines():\n"
            "    materialize = create_pipeline()\n"
            '    return {"materialize": materialize, "__default__": materialize}\n'),
        f"{root}/pipelines/__init__.py": "",
        f"{root}/pipelines/materialize/__init__.py": (
            f"from {PACKAGE}.pipelines.materialize.pipeline import create_pipeline\n"
            '\n__all__ = ["create_pipeline"]\n'),
        f"{root}/pipelines/materialize/pipeline.py": _PIPELINE_STUB + "\n\n" + create_pipeline_body,
    }


_BUILDS = "def create_pipeline():\n    return Pipeline([\"assemble\", \"publish\"])\n"
_NO_NODES = "def create_pipeline():\n    return Pipeline([])\n"
_RAISES = "def create_pipeline():\n    raise RuntimeError(\"the group plan and the IRs disagree\")\n"


def _identity() -> CompilationIdentity:
    return CompilationIdentity(formula_content_hashes=("f" * 64,), ir_hashes=("e" * 64,),
                               materialization_contract_hash="c" * 64, group_plan_hash=PLAN_HASH)


def _on_disk(tmp_path, files: dict[str, str]):
    """Seal the files under a real ``GENERATED.lock`` and write them to a real directory."""
    return materialize_to(seal_project(_identity(), files), tmp_path / "generated")


def _l0(root, *, python_executable: str = sys.executable) -> ValidationReportV1:
    return run_l0(root, generation_id=GEN, environment_id=ENVIRONMENT, report_id="rep-l0",
                  python_executable=python_executable, clock=_clock())


def _clock():
    stamps = iter((T0, T1))
    return lambda: next(stamps)


def _codes(report: ValidationReportV1) -> list[ValidationFindingCode]:
    return [finding.code for finding in report.findings]


def test_a_project_that_builds_PASSES_L0_with_no_findings(tmp_path) -> None:
    report = _l0(_on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS)))
    assert (report.status, report.findings) == (ValidationStatus.PASSED, ())
    assert report.level is ValidationLevel.L0


def test_a_project_that_PARSES_but_yields_no_pipeline_is_PIPELINE_NOT_CONSTRUCTIBLE(tmp_path):
    """The whole point of L0: `ast.parse` accepts this project, and it publishes nothing.

    The parse is asserted here rather than assumed, so the test proves the distinction instead of
    describing it.
    """
    files = _project_files(create_pipeline_body=_NO_NODES)
    for path, text in files.items():
        if path.endswith(".py"):
            ast.parse(text)                      # every file parses — that is the trap

    report = _l0(_on_disk(tmp_path, files))
    assert report.status is ValidationStatus.FAILED
    assert _codes(report) == [ValidationFindingCode.PIPELINE_NOT_CONSTRUCTIBLE]
    assert report.findings[0].classification is FindingClass.RENDERER_DEFECT


def test_a_registry_that_registers_NOTHING_is_PIPELINE_NOT_CONSTRUCTIBLE(tmp_path) -> None:
    files = _project_files(create_pipeline_body=_BUILDS)
    files[f"src/{PACKAGE}/pipeline_registry.py"] = "def register_pipelines():\n    return {}\n"
    report = _l0(_on_disk(tmp_path, files))
    assert _codes(report) == [ValidationFindingCode.PIPELINE_NOT_CONSTRUCTIBLE]


def test_a_pipeline_whose_CONSTRUCTION_raises_is_not_reported_as_an_import_failure(tmp_path):
    """Imported fine, would not build: the two codes route to different renderer bugs."""
    report = _l0(_on_disk(tmp_path, _project_files(create_pipeline_body=_RAISES)))
    assert _codes(report) == [ValidationFindingCode.PIPELINE_NOT_CONSTRUCTIBLE]


def test_a_project_that_cannot_be_IMPORTED_is_PROJECT_DOES_NOT_BUILD(tmp_path) -> None:
    files = _project_files(create_pipeline_body=_BUILDS,
                           registry_prelude="import a_module_nobody_installed\n")
    ast.parse(files[f"src/{PACKAGE}/pipeline_registry.py"])       # parses; does not import
    report = _l0(_on_disk(tmp_path, files))
    assert report.status is ValidationStatus.FAILED
    assert _codes(report) == [ValidationFindingCode.PROJECT_DOES_NOT_BUILD]
    assert report.findings[0].classification is FindingClass.RENDERER_DEFECT
    assert report.findings[0].observed == "ModuleNotFoundError"


def test_L0_never_imports_the_project_into_THIS_interpreter(tmp_path) -> None:
    """An isolated environment, not `importlib` in the validator's own process.

    A project imported here would leave modules in `sys.modules` that the next validation would
    import in preference to the files on disk — so a hand-edited project could pass because its
    predecessor was still loaded.
    """
    _l0(_on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS)))
    assert not [name for name in sys.modules if name.startswith(PACKAGE)]


# ── L0: the hand-edited project ──────────────────────────────────────────────────────────────────


def test_an_UNEDITED_project_reports_no_hash_mismatch(tmp_path) -> None:
    """The control for the three edits below: the same project, untouched."""
    report = _l0(_on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS)))
    assert ValidationFindingCode.PROJECT_HASH_MISMATCH not in _codes(report)


def test_an_EDITED_file_is_caught_as_PROJECT_HASH_MISMATCH(tmp_path) -> None:
    root = _on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))
    edited = root / "README.md"
    edited.write_text(edited.read_text(encoding="utf-8") + "\n<!-- one character of drift -->\n",
                      encoding="utf-8")

    report = _l0(root)
    assert report.status is ValidationStatus.FAILED
    assert _codes(report) == [ValidationFindingCode.PROJECT_HASH_MISMATCH]
    assert report.findings[0].classification is FindingClass.ENVIRONMENT_OR_DATA
    assert may_regenerate(report) is True


def test_an_ADDED_file_is_caught_too(tmp_path) -> None:
    """An added module can shadow a rendered one, so the hash covers absence as well as content."""
    root = _on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))
    (root / "src" / PACKAGE / "sitecustomize_helper.py").write_text("X = 1\n", encoding="utf-8")
    assert _codes(_l0(root)) == [ValidationFindingCode.PROJECT_HASH_MISMATCH]


def test_a_DELETED_file_is_caught_too(tmp_path) -> None:
    root = _on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))
    (root / "README.md").unlink()
    assert ValidationFindingCode.PROJECT_HASH_MISMATCH in _codes(_l0(root))


def test_pythons_byte_CACHE_is_not_drift(tmp_path) -> None:
    """`__pycache__` appears by IMPORTING the project, which is the thing L0 does to it."""
    root = _on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))
    cache = root / "src" / PACKAGE / "__pycache__"
    cache.mkdir()
    (cache / "__init__.cpython-311.pyc").write_bytes(b"\x00\x01")
    assert _l0(root).status is ValidationStatus.PASSED


def test_the_report_names_the_hash_the_LOCK_records(tmp_path) -> None:
    root = _on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))
    recorded = read_lock((root / "GENERATED.lock").read_text(encoding="utf-8"))
    report = _l0(root)
    assert report.generated_project_hash == recorded.generated_project_hash
    assert report.group_plan_hash == recorded.compilation.group_plan_hash


def test_L0_carries_no_run_id(tmp_path) -> None:
    assert _l0(_on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))).run_id is None


# ── L0: an environment that could not be reached ─────────────────────────────────────────────────


def test_an_L0_environment_that_cannot_be_REACHED_is_error_with_zero_findings(tmp_path) -> None:
    """Not a finding and not a pass: the isolated environment never ran, so nothing was observed."""
    report = _l0(_on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS)),
                 python_executable=str(tmp_path / "no-such-interpreter"))
    assert report.status is ValidationStatus.ERROR
    assert report.findings == ()


def test_an_unreachable_environment_does_not_hide_a_hash_mismatch_as_a_PASS(tmp_path) -> None:
    """`error` is not `passed`, and the difference is the only thing separating the two reports."""
    root = _on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))
    (root / "README.md").write_text("edited\n", encoding="utf-8")
    report = _l0(root, python_executable=str(tmp_path / "no-such-interpreter"))
    assert report.status is ValidationStatus.ERROR
    assert report.findings == ()
    assert may_regenerate(report) is False


def test_a_directory_that_is_not_a_generated_project_RAISES_rather_than_failing(tmp_path) -> None:
    """No lock means nothing states what this project should hash to — there is no verdict to give."""
    (tmp_path / "loose").mkdir()
    (tmp_path / "loose" / "thing.py").write_text("x = 1\n", encoding="utf-8")
    with pytest.raises(ValueError, match=GENERATED_LOCK_FILENAME):
        _l0(tmp_path / "loose")


# ── L2 is not run unless requested ───────────────────────────────────────────────────────────────


def test_no_validation_this_slice_runs_L2(tmp_path) -> None:
    """L2 is on demand (§11.2). There is no `run_l2`, so nothing can reach it by default."""
    assert not hasattr(validation, "run_l2")
    assert _l0(_on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))).level \
        is ValidationLevel.L0


# ── L1: the physical inputs the run will actually read ───────────────────────────────────────────
#
# The IRs are REAL — compiled through the chain against the seeded catalog — so the read set, the
# requirements and the spine are the ones a run would use rather than a hand-written approximation
# of them. Every test below moves exactly ONE thing away from an environment that agrees with the
# catalog in every respect, which is what makes the finding it produces mean something.

BUSINESS_DT = "2026-07-27"
ROLES = ("feature_engineer", "pii_reader")


def _spine_requirement(layout) -> PhysicalInputRequirement:
    return PhysicalInputRequirement(
        catalog_source="hdfc", schema=layout.schema, table=layout.table,
        partition_columns=layout.partition_columns, partition_mapping=layout.partition_mapping,
        layout_fingerprint=materialize_hash(layout.semantic_payload()),
        catalog_state_stamp=(("stamp_kind", "drift_watermark"), ("head_seq", "42")))


class _Environment:
    """A metastore that AGREES with the inventory until a test moves one thing.

    Its three questions are §11.2's: which partitions a table has, which columns and physical types
    it has, and whether these roles may read it. There is deliberately no fourth — a seam that could
    return a row is a seam through which the control plane could read feature data.
    """

    def __init__(self, inventory: ClusterInventoryV1) -> None:
        self._columns = {
            key: dict(tuple(layout.columns) + tuple(layout.partition_columns or ()))
            for key, layout in inventory.tables.items()}
        self._partitions: dict[str, list[tuple[tuple[str, str], ...]]] = {
            key: [] for key in inventory.tables}
        self._denied: set[str] = set()
        self._unreachable = False
        self._raises: Exception | None = None
        self.described: list[str] = []

    def stock(self, snapshots) -> _Environment:
        """Make every partition the run resolved exist. The baseline every test moves away from."""
        for snapshot in snapshots:
            if snapshot.partition_specs is None:
                continue
            key = f"{snapshot.requirement.schema}.{snapshot.requirement.table}"
            for spec in snapshot.partition_specs:
                if spec.columns not in self._partitions[key]:
                    self._partitions[key].append(spec.columns)
        return self

    def drop_partition(self, key: str, value: str) -> _Environment:
        self._partitions[key] = [p for p in self._partitions[key]
                                 if all(v != value for _c, v in p)]
        return self

    def drop_column(self, key: str, column: str) -> _Environment:
        del self._columns[key][column]
        return self

    def retype_column(self, key: str, column: str, physical_type: str) -> _Environment:
        self._columns[key][column] = physical_type
        return self

    def deny(self, key: str) -> _Environment:
        self._denied.add(key)
        return self

    def unreachable(self) -> _Environment:
        self._unreachable = True
        return self

    def raising(self, error: Exception) -> _Environment:
        self._raises = error
        return self

    def _check(self) -> None:
        if self._unreachable:
            raise ClusterUnreachable("the metastore did not answer")
        if self._raises is not None:
            raise self._raises

    def list_partitions(self, *, schema: str, table: str):
        self._check()
        return tuple(self._partitions.get(f"{schema}.{table}", ()))

    def describe_table(self, *, schema: str, table: str):
        self._check()
        self.described.append(f"{schema}.{table}")
        columns = self._columns.get(f"{schema}.{table}")
        return None if columns is None else tuple(columns.items())

    def can_read(self, *, schema: str, table: str, roles) -> bool:
        self._check()
        return f"{schema}.{table}" not in self._denied


@pytest.fixture
def compiled(db, catalog):  # noqa: F811 - `catalog` seeds the governed facts the chain reads
    """Two real features whose expressions read DIFFERENT windows of one table."""
    return (_compiled(_compile(db, "total_debit_amount_30d")),
            _compiled(_compile(db, "distinct_merchant_count_90d")))


@pytest.fixture
def prepared(compiled):
    """Requests for every expression AND the spine, resolved against a stocked environment."""
    spine_request = spine_input_request(
        compiled[0].spine, _spine_requirement(INVENTORY.tables["banking.customers"]),
        business_dt=BUSINESS_DT)
    assert isinstance(spine_request, RunInputRequest), spine_request
    requests = (*run_input_requests(compiled), spine_request)
    environment = _Environment(INVENTORY)
    snapshots = resolve_snapshots(INVENTORY, environment, requests=requests,
                                  business_dt=BUSINESS_DT)
    assert isinstance(snapshots, tuple), snapshots
    return snapshots, environment.stock(snapshots)


def _rendered() -> RenderedArtifactIdentity:
    return RenderedArtifactIdentity(compilation=_identity(), generated_project_hash=PROJECT_HASH)


def _l1(prepared, *, irs, roles=ROLES, snapshots=None) -> ValidationReportV1:
    resolved, environment = prepared
    return run_l1(_rendered(), resolved if snapshots is None else snapshots, irs=irs,
                  inventory=INVENTORY, metastore=environment, roles=roles,
                  generation_id=GEN, run_id=RUN, report_id="rep-l1", clock=_clock())


def test_an_environment_that_agrees_with_the_catalog_PASSES_L1(prepared, compiled) -> None:
    report = _l1(prepared, irs=compiled)
    assert (report.status, report.findings) == (ValidationStatus.PASSED, ())
    assert (report.level, report.run_id) == (ValidationLevel.L1, RUN)


def test_ONE_missing_partition_is_PARTITION_ABSENT_and_nothing_else(prepared, compiled) -> None:
    """Held equal to the passing case in every respect but one absent partition."""
    _snapshots, environment = prepared
    environment.drop_partition("banking.transactions", "2026-07-20")

    report = _l1(prepared, irs=compiled)
    assert report.status is ValidationStatus.FAILED
    assert {f.code for f in report.findings} == {ValidationFindingCode.PARTITION_ABSENT}
    assert all(f.classification is FindingClass.ENVIRONMENT_OR_DATA for f in report.findings)
    assert may_regenerate(report) is True


def test_a_partition_only_the_90_DAY_expression_reads_is_still_checked(prepared, compiled) -> None:
    """L1 runs over EVERY expression, not over the union or over the first one it finds.

    2026-05-01 is inside the 90-day read and outside the 30-day one, so a validator that checked one
    window — or that de-duplicated the two reads of `transactions` before comparing their semantics
    — would report nothing here.
    """
    _snapshots, environment = prepared
    environment.drop_partition("banking.transactions", "2026-05-01")

    report = _l1(prepared, irs=compiled)
    located = [f.location for f in report.findings
               if f.code is ValidationFindingCode.PARTITION_ABSENT]
    assert located, "the 90-day expression's own partition set was never checked"
    assert all("distinct_merchant_count_90d" in location for location in located)


def test_a_wrong_TYPE_on_the_cluster_is_a_GOVERNED_FACT_MISMATCH(prepared, compiled) -> None:
    _snapshots, environment = prepared
    assert INVENTORY.tables["banking.transactions"].columns[0] == ("txn_amt", "string")
    environment.retype_column("banking.transactions", "txn_amt", "double")

    report = _l1(prepared, irs=compiled)
    mismatches = [f for f in report.findings
                  if f.code is ValidationFindingCode.COLUMN_TYPE_MISMATCH]
    assert len(mismatches) == 1
    assert mismatches[0].location == "banking.transactions.txn_amt"
    assert (mismatches[0].expected, mismatches[0].observed) == ("string", "double")
    assert mismatches[0].classification is FindingClass.GOVERNED_FACT_MISMATCH
    assert may_regenerate(report) is False


def test_an_absent_COLUMN_is_reported_against_the_column_that_is_absent(prepared, compiled):
    _snapshots, environment = prepared
    environment.drop_column("banking.transactions", "merchant_id")

    report = _l1(prepared, irs=compiled)
    absent = [f for f in report.findings if f.code is ValidationFindingCode.COLUMN_ABSENT]
    assert [f.location for f in absent] == ["banking.transactions.merchant_id"]
    assert absent[0].classification is FindingClass.GOVERNED_FACT_MISMATCH


def test_a_DENIED_table_is_READ_DENIED_and_its_columns_are_not_then_reported_absent(
        prepared, compiled) -> None:
    """A table nobody may read cannot also be a table whose schema was observed."""
    _snapshots, environment = prepared
    environment.deny("banking.transactions")

    report = _l1(prepared, irs=compiled)
    assert {f.code for f in report.findings} == {ValidationFindingCode.READ_DENIED}
    assert "banking.transactions" in {f.location for f in report.findings}
    assert may_regenerate(report) is False


def test_two_casings_of_one_table_are_ONE_read_and_ONE_finding(prepared, compiled) -> None:
    """BANKING.TRANSACTIONS and banking.transactions name the same Hive table (unquoted
    identifiers fold), so L1 must ask about it ONCE and report it ONCE. Read-set keys that never
    folded — while every column comparison did — would ask twice, and the unfolded ask, sailing
    past the denial recorded under the folded spelling, would come back as a table that "does not
    exist": a second, invented finding about the first one's table."""
    shouted = dataclasses.replace(compiled[0], expressions=tuple(
        dataclasses.replace(expression, physical_read_set=tuple(
            dataclasses.replace(ref, schema=ref.schema.upper(), table=ref.table.upper())
            for ref in expression.physical_read_set))
        for expression in compiled[0].expressions))
    _snapshots, environment = prepared
    environment.deny("banking.transactions")
    asked: list[tuple[str, str]] = []
    inner = environment.can_read

    def counting(*, schema: str, table: str, roles) -> bool:
        asked.append((schema, table))
        return inner(schema=schema, table=table, roles=roles)

    environment.can_read = counting  # instance attribute shadows the method

    report = _l1(prepared, irs=(shouted, compiled[1]))
    assert {f.code for f in report.findings} == {ValidationFindingCode.READ_DENIED}
    denied = [f for f in report.findings if f.code is ValidationFindingCode.READ_DENIED]
    assert len(denied) == 1
    assert denied[0].location == "banking.transactions"
    assert asked.count(("banking", "transactions")) == 1
    assert ("BANKING", "TRANSACTIONS") not in asked


# ── L1 covers the SPINE, which no feature expression reads ───────────────────────────────────────


def test_the_spines_table_is_in_the_read_set_although_no_expression_reads_it(prepared, compiled):
    """The control for the two spine tests below: `customers` is the spine's alone."""
    expression_tables = {f"{ref.schema}.{ref.table}"
                         for ir in compiled for expr in ir.expressions
                         for ref in expr.physical_read_set}
    assert "banking.customers" not in expression_tables

    _l1(prepared, irs=compiled)
    assert "banking.customers" in prepared[1].described


def test_a_broken_column_on_the_SPINES_table_is_found(prepared, compiled) -> None:
    _snapshots, environment = prepared
    environment.drop_column("banking.customers", "cif_id")

    report = _l1(prepared, irs=compiled)
    assert [f.location for f in report.findings
            if f.code is ValidationFindingCode.COLUMN_ABSENT] == ["banking.customers.cif_id"]


def test_a_missing_partition_under_the_SPINE_is_found(prepared, compiled) -> None:
    _snapshots, environment = prepared
    environment.drop_partition("banking.customers", BUSINESS_DT)

    report = _l1(prepared, irs=compiled)
    absent = [f for f in report.findings if f.code is ValidationFindingCode.PARTITION_ABSENT]
    assert [f.location for f in absent] == ["__spine__/spine banking.customers"]


def test_L1_REFUSES_to_run_without_the_spines_snapshot(prepared, compiled) -> None:
    """§11.2 says L1 covers the spine, so a run that resolved no spine read cannot be validated.

    Silently skipping it would make "L1 covers the spine" a claim nothing enforces — the population
    would be the one part of the run whose inputs were never checked.
    """
    snapshots, _environment = prepared
    without_spine = tuple(s for s in snapshots if s.feature_name != SPINE_FEATURE_NAME)
    assert len(without_spine) < len(snapshots)
    with pytest.raises(ValueError, match="spine"):
        _l1(prepared, irs=compiled, snapshots=without_spine)


# ── L1 reads metadata only, and an unreachable cluster invents nothing ───────────────────────────


def test_the_metastore_seam_has_no_way_to_read_a_ROW() -> None:
    """Structural, not a review convention: there is no method that could return data."""
    assert {name for name in dir(MetastoreMetadata) if not name.startswith("_")} == \
        {"list_partitions", "describe_table", "can_read"}


def test_an_unreachable_cluster_is_error_with_ZERO_findings(prepared, compiled) -> None:
    _snapshots, environment = prepared
    environment.drop_partition("banking.transactions", "2026-07-20")   # would have been a finding
    environment.unreachable()

    report = _l1(prepared, irs=compiled)
    assert report.status is ValidationStatus.ERROR
    assert report.findings == ()
    assert may_regenerate(report) is False


def test_a_DEFECT_in_the_metastore_adapter_is_not_reported_as_unreachable(prepared, compiled):
    """Catching bare `Exception` would turn a bug into a verdict about the cluster."""
    _snapshots, environment = prepared
    environment.raising(ValueError("the adapter built a malformed query"))
    with pytest.raises(ValueError, match="malformed"):
        _l1(prepared, irs=compiled)


def test_L1_reports_the_hashes_the_RENDERED_identity_states(prepared, compiled) -> None:
    report = _l1(prepared, irs=compiled)
    assert report.generated_project_hash == PROJECT_HASH
    assert report.group_plan_hash == PLAN_HASH


def test_the_environment_given_to_L0_reaches_the_interpreter_that_imports_the_project(tmp_path):
    """The L0 gate needs `PYSPARK_PYTHON` and friends to reach the probe, so prove they do.

    The project refuses to import without the marker, so the two runs below differ in exactly one
    thing: whether `env` was passed.
    """
    files = _project_files(create_pipeline_body=_BUILDS)
    files[f"src/{PACKAGE}/__init__.py"] = (
        "import os\n"
        "\n"
        'if os.environ.get("L0_MARKER") != "yes":\n'
        '    raise RuntimeError("the probe did not carry the environment it was given")\n')
    root = _on_disk(tmp_path, files)

    without = run_l0(root, generation_id=GEN, environment_id=ENVIRONMENT, report_id="rep-a",
                     python_executable=sys.executable, clock=_clock())
    assert _codes(without) == [ValidationFindingCode.PROJECT_DOES_NOT_BUILD]

    with_env = run_l0(root, generation_id=GEN, environment_id=ENVIRONMENT, report_id="rep-b",
                      python_executable=sys.executable, clock=_clock(), env={"L0_MARKER": "yes"})
    assert (with_env.status, with_env.findings) == (ValidationStatus.PASSED, ())


# ── L0: the verdict channel cannot be forged ─────────────────────────────────────────────────────
#
# The marker's own docstring names the attack — a project that prints something shaped like a
# verdict declares itself buildable — and these are the tests that attack never had. The defence is
# a marker minted fresh per invocation: the probe reads it from argv, so an honest probe still
# answers, while a FIXED string printed by the project (or by anything standing where the
# interpreter should be) matches nothing.

_FORGED_PASS = '@@L0-VERDICT@@ {"stage": "build", "ok": true, "observed": "2 nodes"}'


def _stub_python(tmp_path, script_body: str) -> str:
    """Something standing where the interpreter should be — it never runs the probe it was given."""
    stub = tmp_path / "stub-python"
    stub.write_text("#!/bin/sh\n" + script_body, encoding="utf-8")
    stub.chmod(0o755)
    return str(stub)


def test_a_project_printing_the_verdict_marker_cannot_bless_itself(tmp_path) -> None:
    """The self-blessing project: it prints a verdict-shaped line with the well-known prefix during
    import and exits before the probe can speak, so its line is the only candidate verdict. The
    files are SEALED with that print in place — no hash mismatch — so the forged line is the only
    signal, and the report must still not be a pass."""
    files = _project_files(create_pipeline_body=_BUILDS)
    files[f"src/{PACKAGE}/__init__.py"] = (
        "import os, sys\n"
        f"sys.stdout.write({_FORGED_PASS!r} + chr(10))\n"
        "sys.stdout.flush()\n"
        "os._exit(0)\n")
    report = _l0(_on_disk(tmp_path, files))
    assert report.status is not ValidationStatus.PASSED


@pytest.mark.parametrize("forged", [
    '@@L0-VERDICT@@ {"builds": true}',    # verdict-shaped only loosely — must still not pass
    _FORGED_PASS,                          # byte-for-byte what a passing probe would print
])
def test_an_interpreter_that_prints_the_FIXED_marker_cannot_bless_the_project(tmp_path, forged):
    root = _on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))
    stub = _stub_python(tmp_path, f"printf '%s\\n' '{forged}'\n")
    assert _l0(root, python_executable=stub).status is not ValidationStatus.PASSED


def test_the_verdict_marker_is_a_fresh_nonce_per_invocation(tmp_path) -> None:
    """Two invocations, two markers — yesterday's log does not teach a forger today's marker.

    The stub captures the marker the probe would have received (its argv, ``$3``), which is also
    the only string the scan will match.
    """
    root = _on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))
    captured = tmp_path / "markers.txt"
    stub = _stub_python(tmp_path, f'printf \'%s\\n\' "$3" >> "{captured}"\n')
    _l0(root, python_executable=stub)
    _l0(root, python_executable=stub)

    first, second = captured.read_text(encoding="utf-8").splitlines()
    assert first != second
    assert first.startswith("@@L0-VERDICT@@") and second.startswith("@@L0-VERDICT@@")
    assert first.endswith(" ") and second.endswith(" ")   # the delimiter the probe prepends
    assert "@@L0-VERDICT@@ " not in (first, second)       # never the guessable fixed string


# ── L0: the probe's timeout and the file that is not text ────────────────────────────────────────


def test_a_probe_that_outlives_timeout_seconds_is_error_not_a_hang(tmp_path) -> None:
    """§11.2: a probe killed by the timeout is *the validation did not run* — and it IS killed."""
    files = _project_files(create_pipeline_body=_BUILDS)
    files[f"src/{PACKAGE}/__init__.py"] = "import time\ntime.sleep(30)\n"
    root = _on_disk(tmp_path, files)

    began = time.monotonic()
    report = run_l0(root, generation_id=GEN, environment_id=ENVIRONMENT, report_id="rep-timeout",
                    python_executable=sys.executable, clock=_clock(), timeout_seconds=1.0)
    elapsed = time.monotonic() - began

    assert report.status is ValidationStatus.ERROR
    assert report.findings == ()
    assert elapsed < 20                                # returned at the timeout, not after the sleep
    survivors = subprocess.run(["pgrep", "-f", str(root)], capture_output=True, text=True,
                               check=False)
    assert survivors.stdout.strip() == ""              # the timed-out probe was killed, not orphaned


def test_a_non_utf8_file_is_PROJECT_HASH_MISMATCH_naming_the_utf8_problem(tmp_path) -> None:
    """`_files_on_disk` answers None for a tree it cannot read as text; the verdict must be the
    fail-closed drift finding with an HONEST observed — the hash of the readable files would claim
    an observation of the whole tree that was never made."""
    root = _on_disk(tmp_path, _project_files(create_pipeline_body=_BUILDS))
    (root / "src" / PACKAGE / "junk.py").write_bytes(b"\xff\xfe")

    report = _l0(root)
    assert report.status is ValidationStatus.FAILED
    assert _codes(report) == [ValidationFindingCode.PROJECT_HASH_MISMATCH]
    assert "UTF-8" in report.findings[0].observed
    assert report.findings[0].expected == read_lock(
        (root / GENERATED_LOCK_FILENAME).read_text(encoding="utf-8")).generated_project_hash


# ── ingestion: the report the control plane deliberately did not invent ──────────────────────────


def test_a_report_is_APPENDED_with_its_findings_intact(db) -> None:
    """`control_plane` created the table and left this path to §11's owner. This is that path."""
    record_generation(db, MaterializationGeneration(
        generation_id=GEN, logical_group_name="cif_daily",
        materialization_contract_hash="c" * 64, group_plan_hash=PLAN_HASH,
        generated_project_hash=PROJECT_HASH, created_at=T0))
    report = _report(ValidationStatus.FAILED, (
        _finding(ValidationFindingCode.COLUMN_TYPE_MISMATCH,
                 location="banking.transactions.txn_amt", expected="decimal(18,2)",
                 observed="string"),))
    record_validation_report(db, report)

    row = db.execute(
        "SELECT level, status, run_id, findings FROM pipeline_validation_report "
        "WHERE report_id = %s", (report.report_id,)).fetchone()
    assert row[:3] == ("L1", "failed", RUN)
    assert row[3] == [{"code": "COLUMN_TYPE_MISMATCH", "severity": "error",
                       "classification": "governed_fact_mismatch",
                       "location": "banking.transactions.txn_amt",
                       "expected": "decimal(18,2)", "observed": "string", "count": 1}]


def test_an_L0_report_records_a_NULL_run_id(db) -> None:
    record_generation(db, MaterializationGeneration(
        generation_id=GEN, logical_group_name="cif_daily",
        materialization_contract_hash="c" * 64, group_plan_hash=PLAN_HASH,
        generated_project_hash=PROJECT_HASH, created_at=T0))
    record_validation_report(db, ValidationReportV1(
        report_id="rep-l0-db", generation_id=GEN, run_id=None,
        generated_project_hash=PROJECT_HASH, group_plan_hash=PLAN_HASH, level=ValidationLevel.L0,
        environment_id=ENVIRONMENT, status=ValidationStatus.PASSED, started_at=T0, finished_at=T1,
        findings=()))

    row = db.execute("SELECT run_id, findings FROM pipeline_validation_report "
                     "WHERE report_id = 'rep-l0-db'").fetchone()
    assert row == (None, [])


# ── the reader the table never had ───────────────────────────────────────────────────────────────
#
# `record_validation_report` INSERTs and nothing in src ever SELECTed, so `may_regenerate`'s
# blocking rule held only for the process that happened to hold the report object. These tests are
# the cross-process form: what was RECORDED reads back EQUAL (frozen-dataclass `==`, every field),
# and the recorded history — not an in-memory object — answers whether regeneration is legitimate.

T2 = "2026-07-28T09:00:10+00:00"


def _seed_generation(db, generation_id: str = GEN) -> None:
    record_generation(db, MaterializationGeneration(
        generation_id=generation_id, logical_group_name="cif_daily",
        materialization_contract_hash="c" * 64, group_plan_hash=PLAN_HASH,
        generated_project_hash=PROJECT_HASH, created_at=T0))


def test_a_recorded_report_reads_back_EQUAL_and_gates_regeneration(db) -> None:
    """FULL equality, not field sampling: one `==` over the frozen dataclass covers all eleven
    columns and every finding field, including the None-valued expected/observed."""
    _seed_generation(db)
    report = _report(ValidationStatus.FAILED, (
        _finding(ValidationFindingCode.COLUMN_TYPE_MISMATCH,
                 location="banking.transactions.txn_amt", expected="decimal(18,2)",
                 observed="string"),
        _finding(ValidationFindingCode.PARTITION_ABSENT,
                 location="txn_sum_30d/e0 banking.transactions",
                 expected="3 partition(s)", observed="2 present", count=1),
        _finding(ValidationFindingCode.UNKNOWN_FINDING),        # expected/observed both None
    ))
    record_validation_report(db, report)

    (read,) = read_validation_reports(db, generation_id=GEN)
    assert read == report
    assert may_regenerate_for(db, generation_id=GEN) is False   # COLUMN_TYPE_MISMATCH blocks


def test_reports_read_back_in_STARTED_order_with_an_L0_null_run_id_intact(db) -> None:
    """Recorded newest-first on purpose: the order read back is `started_at`, not insertion."""
    _seed_generation(db)
    l1 = dataclasses.replace(
        _report(ValidationStatus.FAILED,
                (_finding(ValidationFindingCode.PARTITION_ABSENT, observed="0 present"),)),
        report_id="rep-order-l1", started_at=T1, finished_at=T2)
    l0 = ValidationReportV1(
        report_id="rep-order-l0", generation_id=GEN, run_id=None,
        generated_project_hash=PROJECT_HASH, group_plan_hash=PLAN_HASH,
        level=ValidationLevel.L0, environment_id=ENVIRONMENT, status=ValidationStatus.PASSED,
        started_at=T0, finished_at=T1, findings=())
    record_validation_report(db, l1)
    record_validation_report(db, l0)

    assert read_validation_reports(db, generation_id=GEN) == (l0, l1)


def test_no_recorded_reports_block_nothing_and_other_generations_do_not_bleed(db) -> None:
    _seed_generation(db)
    _seed_generation(db, "gen-15b-other")
    record_validation_report(db, dataclasses.replace(
        _report(ValidationStatus.FAILED,
                (_finding(ValidationFindingCode.READ_DENIED, observed="denied"),)),
        report_id="rep-other", generation_id="gen-15b-other"))

    assert read_validation_reports(db, generation_id=GEN) == ()
    assert may_regenerate_for(db, generation_id=GEN) is True


def test_a_superseded_blocker_no_longer_blocks_and_a_fresh_one_blocks_again(db) -> None:
    """`may_regenerate_for` judges the NEWEST report of each level. A re-validation re-checked the
    findings of the report it supersedes, so a blocker followed by a clean re-run no longer blocks
    — and a clean run followed by a fresh blocker blocks again. History's ORDER decides."""
    _seed_generation(db)
    record_validation_report(db, dataclasses.replace(
        _report(ValidationStatus.FAILED,
                (_finding(ValidationFindingCode.COLUMN_ABSENT, observed="absent"),)),
        report_id="rep-seq-blocked", started_at=T0))
    assert may_regenerate_for(db, generation_id=GEN) is False

    record_validation_report(db, dataclasses.replace(
        _report(ValidationStatus.PASSED), report_id="rep-seq-clean", started_at=T1))
    assert may_regenerate_for(db, generation_id=GEN) is True

    record_validation_report(db, dataclasses.replace(
        _report(ValidationStatus.FAILED,
                (_finding(ValidationFindingCode.COLUMN_ABSENT, observed="absent"),)),
        report_id="rep-seq-blocked-again", started_at=T2))
    assert may_regenerate_for(db, generation_id=GEN) is False


def test_every_LEVELS_newest_report_must_permit_regeneration(db) -> None:
    """The levels AND together: L1's clean re-run says nothing about L0's standing refusal —
    here an `error` report, which never permits regeneration (nothing was looked at)."""
    _seed_generation(db)
    record_validation_report(db, dataclasses.replace(
        _report(ValidationStatus.PASSED), report_id="rep-and-l1", started_at=T1))
    record_validation_report(db, dataclasses.replace(
        _report(ValidationStatus.ERROR, level=ValidationLevel.L0),
        report_id="rep-and-l0", run_id=None, started_at=T0))

    assert may_regenerate_for(db, generation_id=GEN) is False
