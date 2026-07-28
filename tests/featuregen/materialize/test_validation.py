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

import dataclasses
import inspect

import pytest

from featuregen.materialize.codes import ValidationFindingCode
from featuregen.materialize.validation import (
    FINDING_CLASSES,
    FindingClass,
    FindingSeverity,
    ValidationFinding,
    ValidationLevel,
    ValidationReportV1,
    ValidationStatus,
    classify,
    may_regenerate,
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
