"""Hand reconciliation: does what a person counted agree with what the platform recorded?

Release C Task 13. The plan's activation step is *"hand-reconcile one direct bridge and one real
mapping-table crosswalk before activation"*, and that step happens against LIVE data behind a
separate approval (Gate B). What lives here is the part that must exist BEFORE anyone runs it: the
comparison itself, as executable code rather than as a paragraph somebody follows by eye.

**Why a person's count is the only check that can catch this class of defect.** Every other gate in
Release C compares one recorded artifact against another — the observation against the scope, the
binding against the pin, the verdict against the policy. All of them are satisfied by a
measurement that is internally consistent and wrong: a probe that read the wrong partition, an
`as_of` a day off, a mapping table whose rows were filtered by a rule nobody meant. None of that is
visible from inside the record. A human running the same join by hand and reporting four numbers is
the only signal in the system that comes from OUTSIDE it.

**Divergence REFUSES; it never rounds.** There is no tolerance parameter, deliberately. A tolerance
is a policy, and a policy belongs where somebody can review it — not compiled into the comparison
that is supposed to be the independent check. If the operator counted 41,882 composed rows and the
observation says 41,884, that is a finding, and the honest thing to do with a finding is report it.

**Nothing here reads a cluster, a catalog or a connection.** It takes the numbers a person brings
back and the artifacts the platform already holds. That is what makes it testable at fixture level,
and it is why running the reconciliation is a desk exercise that a live query FEEDS rather than a
live query that this module performs.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.overlay.upload.bridge_realization import (
    BridgeJoinRealizationRevisionV1,
    DirectionalCardinalityVerdictV1,
)
from featuregen.overlay.upload.crosswalk_admission import AdmittedCrosswalkV1
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality

__all__ = [
    "HandCountsV1",
    "BridgeHandCountsV1",
    "ReconciliationFinding",
    "ReconciliationReportV1",
    "reconcile_crosswalk",
    "reconcile_direct_bridge",
]


class ReconciliationFinding(StrEnum):
    """CLOSED vocabulary. Each names WHICH number disagreed, never "the check failed"."""

    COMPOSED_ROW_COUNT_DISAGREES = "composed_row_count_disagrees"
    MAPPING_ROW_COUNT_DISAGREES = "mapping_row_count_disagrees"
    FORWARD_FANOUT_DISAGREES = "forward_fanout_disagrees"
    REVERSE_FANOUT_DISAGREES = "reverse_fanout_disagrees"
    #: The operator read a table the measurement did not pin. Every number they brought back
    #: describes a different table, so nothing else in the report can be trusted either.
    ADDRESS_DISAGREES = "physical_address_disagrees"
    #: The operator applied a different row rule (or none). Same consequence as a wrong address:
    #: the counts are about a different row set.
    ROW_RULE_DISAGREES = "mapping_row_rule_disagrees"
    #: Nothing to reconcile AGAINST. Not a disagreement — an absence, and it is reported as one.
    NOT_MEASURED = "crosswalk_not_measured"
    CARDINALITY_DISAGREES = "directional_cardinality_disagrees"


@dataclass(frozen=True, slots=True)
class HandCountsV1:
    """What a person counted, by hand, against the real tables.

    The four numbers are exactly the ones a composed observation records, so the comparison is
    field-for-field and no derivation stands between the operator and the check. ``read_addresses``
    and ``row_rule_applied`` are the operator's own account of WHAT they counted — asked for
    separately because the commonest reconciliation error is not a wrong count, it is a right count
    of the wrong thing.
    """

    composed_row_count: int
    mapping_row_count: int
    source_to_target_max_matches: int
    target_to_source_max_matches: int
    #: ``(source, mapping, target)`` as ``<catalog>::<schema>.<table>``, exactly as typed into the
    #: query the operator actually ran.
    read_addresses: tuple[str, str, str]
    #: The mapping temporal policy revision the operator filtered by, or None if they filtered by
    #: nothing. None is a legitimate answer and is compared as such.
    row_rule_applied: str | None = None
    counted_by: str = ""


@dataclass(frozen=True, slots=True)
class BridgeHandCountsV1:
    """The direct-bridge half. One relation pair, one directional fan-out, no mapping table."""

    joined_row_count: int
    max_matches_per_source_row: int
    read_addresses: tuple[str, str]
    counted_by: str = ""


@dataclass(frozen=True, slots=True)
class ReconciliationReportV1:
    """The comparison's answer. ``agrees`` is True only when the findings tuple is EMPTY.

    A report is a VALUE and never an exception: a divergence is a finding about the bank's data or
    about how the query was run, and both are things a person reads and acts on — not a stack trace.
    """

    subject: str
    agrees: bool
    findings: tuple[ReconciliationFinding, ...]
    detail: tuple[str, ...] = ()
    counted_by: str = ""

    @property
    def blocks_activation(self) -> bool:
        """Any finding at all. There is no partial pass and no tolerance.

        Named for what it does rather than for a severity, because the activation decision is the
        only consumer: a reconciliation that disagreed about ANY of its numbers has not established
        the thing activation needs it to establish.
        """
        return bool(self.findings)


def _address(binding) -> str:
    identity = binding.identity
    return (f"{identity.catalog_source}::{identity.schema}.{identity.table}").strip().lower()


def reconcile_crosswalk(
    admitted: AdmittedCrosswalkV1, counts: HandCountsV1,
) -> ReconciliationReportV1:
    """Compare a person's hand counts against the composed measurement this crosswalk was admitted on.

    Order matters and is not cosmetic. The ADDRESS and the ROW RULE are checked first, because a
    disagreement in either means every count below describes a different question — and reporting
    "the composed row count disagrees" when the operator queried another schema sends whoever reads
    the report looking for a data defect that is not there.
    """
    findings: list[ReconciliationFinding] = []
    detail: list[str] = []
    observation = admitted.observation

    pinned = (_address(admitted.source_binding), _address(admitted.mapping_binding),
              _address(admitted.target_binding))
    read = tuple(item.strip().lower() for item in counts.read_addresses)
    if read != pinned:
        findings.append(ReconciliationFinding.ADDRESS_DISAGREES)
        detail.append(
            f"counted against {read} and the measurement pinned {pinned}: every number below "
            "describes a different table")

    expected_rule = admitted.execution.mapping_temporal_policy_revision_id
    if counts.row_rule_applied != expected_rule:
        findings.append(ReconciliationFinding.ROW_RULE_DISAGREES)
        detail.append(
            f"counted under mapping row rule {counts.row_rule_applied!r} and the execution pins "
            f"{expected_rule!r}: uniqueness over one row set says nothing about the other")

    if observation is None:
        # NOT a disagreement. There is simply nothing recorded to reconcile against, and saying so
        # is a different sentence from saying the numbers are wrong.
        findings.append(ReconciliationFinding.NOT_MEASURED)
        detail.append(
            "this crosswalk carries no composed measurement, so the hand counts have nothing to "
            "be compared with — profile it first, then reconcile")
        return ReconciliationReportV1(
            subject=admitted.definition.definition_id, agrees=False,
            findings=tuple(findings), detail=tuple(detail), counted_by=counts.counted_by)

    for finding, label, counted, recorded in (
            (ReconciliationFinding.COMPOSED_ROW_COUNT_DISAGREES, "composed rows",
             counts.composed_row_count, observation.composed_row_count),
            (ReconciliationFinding.MAPPING_ROW_COUNT_DISAGREES, "mapping rows",
             counts.mapping_row_count, observation.mapping.row_count),
            (ReconciliationFinding.FORWARD_FANOUT_DISAGREES, "forward max matches",
             counts.source_to_target_max_matches, observation.source_to_target_max_matches),
            (ReconciliationFinding.REVERSE_FANOUT_DISAGREES, "reverse max matches",
             counts.target_to_source_max_matches, observation.target_to_source_max_matches)):
        if counted != recorded:
            findings.append(finding)
            # BOTH numbers, always. "They disagree" is not actionable; "you counted 41882 and the
            # observation says 41884" is.
            detail.append(f"{label}: counted {counted}, recorded {recorded}")

    return ReconciliationReportV1(
        subject=admitted.definition.definition_id, agrees=not findings,
        findings=tuple(findings), detail=tuple(detail), counted_by=counts.counted_by)


def reconcile_direct_bridge(
    revision: BridgeJoinRealizationRevisionV1, counts: BridgeHandCountsV1,
) -> ReconciliationReportV1:
    """The direct-bridge half of the same exercise, so both are reconciled by ONE mechanism.

    The plan asks for a direct bridge AND a crosswalk deliberately: reconciling only the crosswalk
    would leave "does hand-counting this platform's joins agree at all?" unanswered, and a
    divergence on BOTH would point at the method rather than at the crosswalk.

    The bridge's own recorded cardinality is the claim under test. A realization that says ``N:1``
    is claiming at most one match per source row, so an operator who counted two has found the
    fan-out the whole family exists to refuse.
    """
    findings: list[ReconciliationFinding] = []
    detail: list[str] = []

    pinned = (_address_of_endpoint(revision.from_endpoint),
              _address_of_endpoint(revision.to_endpoint))
    read = tuple(item.strip().lower() for item in counts.read_addresses)
    if read != pinned:
        findings.append(ReconciliationFinding.ADDRESS_DISAGREES)
        detail.append(f"counted against {read} and the realization pins {pinned}")

    if not _cardinality_admits(revision.cardinality, counts.max_matches_per_source_row):
        findings.append(ReconciliationFinding.CARDINALITY_DISAGREES)
        detail.append(
            f"the realization records "
            f"{revision.cardinality.value.value if revision.cardinality.value else 'unknown'} and "
            f"the hand count found {counts.max_matches_per_source_row} match(es) per source row")

    return ReconciliationReportV1(
        subject=revision.realization_revision_id, agrees=not findings,
        findings=tuple(findings), detail=tuple(detail), counted_by=counts.counted_by)


def _address_of_endpoint(endpoint) -> str:
    binding = endpoint.physical_binding
    if binding is None:
        return endpoint.logical_table_ref.strip().lower()
    return _address(binding)


def _cardinality_admits(
    verdict: DirectionalCardinalityVerdictV1, observed_max: int,
) -> bool:
    """Does a recorded fan-in verdict survive the number a person counted?

    An UNKNOWN verdict claims nothing, so nothing can contradict it — and that is the honest answer
    rather than a free pass: the reconciliation is checking a CLAIM, and there is no claim here.
    """
    value = verdict.value
    if value is None:
        return True
    # The two FAN-IN shapes are the ones that make a claim a count can contradict: both promise at
    # most one match per source row. `one_to_many`/`many_to_many` promise nothing an operator could
    # disprove by counting more than one.
    if value is Cardinality.ONE_TO_ONE or value is Cardinality.MANY_TO_ONE:
        return observed_max <= 1
    return True
