"""C-C8 — realization identity.

The plan's gate is two claims: *"two proposals with identical semantics keep separate revisions;
`realizes_occurrences` exists on the type"*. The first is the one with teeth, because the failure it
prevents is an LLM proposal inheriting a source's approval by agreeing with it.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.formula.policy_occurrences import PolicyOccurrenceV1
from featuregen.formula.policy_realization import (
    ConflictFindingV1,
    PolicyRealizationRevisionV1,
    RealizationFamilyKeyV1,
    RealizationProvenanceV1,
    family_key_for,
    family_key_hash,
    unrealized_occurrences,
)

DIR_REF = "direction_sign:foundation-signed-by-indicator"
LEDGER_A = "hdfc::public.transactions"
LEDGER_B = "adcb::public.postings"
ENV = "hdfc-local"


def _occurrence(*, dataset: str = LEDGER_A, environment: str = ENV,
                role: str = "direction", ref: str = DIR_REF) -> PolicyOccurrenceV1:
    return PolicyOccurrenceV1(
        expr_path="body.expr", policy_ref_field="direction_policy_ref",
        policy_kind=ref.split(":")[0], policy_ref=ref, semantic_role=role,
        bound_dataset=dataset, bound_column=f"{dataset}.txn_amt", environment_id=environment)


def _revision(**overrides) -> PolicyRealizationRevisionV1:
    kwargs = dict(
        revision_id="rev-0001", family_key=family_key_for(_occurrence()),
        executable_content_hash="sha256:executable", cas_pointer="cas://blob/abc",
        provenance=RealizationProvenanceV1.SOURCE_DERIVED,
        realizes_occurrences=(_occurrence().occurrence_hash,))
    kwargs.update(overrides)
    return PolicyRealizationRevisionV1(**kwargs)


# ══ THE GATE — identical semantics, separate revisions ═══════════════════════════════════════════
def test_TWO_PROPOSALS_WITH_IDENTICAL_SEMANTICS_KEEP_SEPARATE_REVISIONS():
    """The failure this prevents: an LLM proposal inheriting a source's approval by agreeing with
    it. They execute identically and are still two artifacts with two review histories."""
    from_source = _revision(revision_id="rev-source",
                            provenance=RealizationProvenanceV1.SOURCE_DERIVED)
    from_llm = _revision(revision_id="rev-llm",
                         provenance=RealizationProvenanceV1.LLM_PROPOSED)

    assert from_source.executable_content_hash == from_llm.executable_content_hash
    assert from_source.revision_id != from_llm.revision_id
    assert from_source.identity_payload() != from_llm.identity_payload()


def test_an_LLM_proposal_is_NOT_evidence_validated_even_when_it_AGREES():
    """"Derived from evidence" and "consistent with evidence" are the two things this keeps apart."""
    assert _revision(provenance=RealizationProvenanceV1.SOURCE_DERIVED).is_evidence_validated
    assert _revision(provenance=RealizationProvenanceV1.HUMAN_AUTHORED).is_evidence_validated
    assert not _revision(provenance=RealizationProvenanceV1.LLM_PROPOSED).is_evidence_validated


def test_collapsing_the_two_identities_is_REFUSED():
    """If the revision id WERE the content hash, two agreeing proposals would BE one revision."""
    with pytest.raises(ValueError, match="collapses the artifact"):
        _revision(revision_id="sha256:executable", executable_content_hash="sha256:executable")


# ══ the family key, frozen explicitly ════════════════════════════════════════════════════════════
def test_the_family_key_is_exactly_the_five_frozen_parts():
    assert {f.name for f in dataclasses.fields(RealizationFamilyKeyV1)} == {
        "policy_kind", "policy_ref", "bound_dataset", "environment_id", "semantic_role"}


def test_THE_SAME_POLICY_OVER_TWO_LEDGERS_IS_TWO_FAMILIES():
    """R19's reason for freezing the dataset binding into the key: drop it and the `current`
    realization for one policy ref merges two ledgers that spell debit differently, and one starts
    reading the other's encoding."""
    a = family_key_for(_occurrence(dataset=LEDGER_A))
    b = family_key_for(_occurrence(dataset=LEDGER_B))
    assert a.policy_ref == b.policy_ref
    assert family_key_hash(a) != family_key_hash(b)


def test_the_same_policy_in_two_ENVIRONMENTS_is_two_families():
    assert family_key_hash(family_key_for(_occurrence(environment="hdfc-local"))) != \
        family_key_hash(family_key_for(_occurrence(environment="hdfc-prod")))


def test_the_same_policy_in_two_ROLES_is_two_families():
    """A policy used for two purposes over one dataset would otherwise collapse into one pointer."""
    assert family_key_hash(family_key_for(_occurrence(role="direction"))) != \
        family_key_hash(family_key_for(_occurrence(role="reversal")))


@pytest.mark.parametrize("blank", ["policy_kind", "policy_ref", "bound_dataset",
                                   "environment_id", "semantic_role"])
def test_a_blank_key_part_is_refused(blank):
    kwargs = dict(policy_kind="direction_sign", policy_ref=DIR_REF, bound_dataset=LEDGER_A,
                  environment_id=ENV, semantic_role="direction")
    kwargs[blank] = "  "
    with pytest.raises(ValueError, match="current' pointer would then be current for"):
        RealizationFamilyKeyV1(**kwargs)


def test_the_family_key_is_DERIVED_from_the_occurrence_not_chosen():
    """So an occurrence and its realization cannot disagree about which dataset, environment or
    role they are talking about."""
    occurrence = _occurrence()
    key = family_key_for(occurrence)
    assert (key.bound_dataset, key.environment_id, key.semantic_role) == (
        occurrence.bound_dataset, occurrence.environment_id, occurrence.semantic_role)


# ══ realizes_occurrences — created here ══════════════════════════════════════════════════════════
def test_realizes_occurrences_EXISTS_on_the_type():
    assert "realizes_occurrences" in {f.name for f in dataclasses.fields(
        PolicyRealizationRevisionV1)}
    assert _revision().realizes_occurrences == (_occurrence().occurrence_hash,)


def test_a_realization_that_realizes_NOTHING_is_refused():
    """`current` would point at an answer to a question nobody asked, and nothing would notice if
    the occurrence it was built for disappeared."""
    with pytest.raises(ValueError, match="no reason to exist"):
        _revision(realizes_occurrences=())


def test_the_same_occurrence_twice_is_refused():
    one = _occurrence().occurrence_hash
    with pytest.raises(ValueError, match="same occurrence twice"):
        _revision(realizes_occurrences=(one, one))


def test_AN_UNREALIZED_OCCURRENCE_IS_DETECTABLE():
    """What `realizes_occurrences` makes possible: an occurrence with no realization is a governed
    policy the compilation needs and nothing answers — detected, not merely regretted."""
    realized, orphan = _occurrence(), _occurrence(dataset=LEDGER_B)
    revisions = (_revision(realizes_occurrences=(realized.occurrence_hash,)),)

    assert unrealized_occurrences((realized,), revisions) == ()
    assert unrealized_occurrences((realized, orphan), revisions) == (orphan,)


def test_occurrence_hashes_are_stored_SORTED():
    """Two revisions realizing the same set in different orders are one artifact."""
    hashes = ("sha256:zzz", "sha256:aaa", "sha256:mmm")
    assert _revision(realizes_occurrences=hashes).realizes_occurrences == tuple(sorted(hashes))


# ══ conflicts are retained ═══════════════════════════════════════════════════════════════════════
def test_a_RESOLVED_conflict_is_still_carried():
    """A realization that resolved a conflict still HAD one; dropping the finding on resolution
    destroys the only record that the question was ever open."""
    finding = ConflictFindingV1(code="DIRECTION_ENCODING_AMBIGUOUS",
                                detail="both D/C and DR/CR observed", resolved=True)
    revision = _revision(conflict_findings=(finding,))
    assert revision.conflict_findings == (finding,)
    assert revision.identity_payload()["conflict_findings"][0]["resolved"] is True


def test_a_conflict_with_no_code_is_refused():
    with pytest.raises(ValueError, match="cannot be looked up"):
        ConflictFindingV1(code="  ", detail="d", resolved=False)


def test_conflicts_are_IDENTITY_bearing():
    """A revision that recorded a conflict is not the same artifact as one that recorded none."""
    clean = _revision()
    conflicted = _revision(conflict_findings=(
        ConflictFindingV1(code="X", detail="d", resolved=True),))
    assert clean.identity_payload() != conflicted.identity_payload()


# ══ pilot realizations are TIMELESS ══════════════════════════════════════════════════════════════
def test_there_is_NO_validity_interval_field_and_that_is_deliberate():
    """Interval detection was never built, so there is nothing to remove and nothing to disable. A
    field meaning "no interval known yet" would read as "this policy holds for all time" — a claim
    nobody made."""
    names = {f.name for f in dataclasses.fields(PolicyRealizationRevisionV1)}
    for temporal in ("valid_from", "valid_to", "validity_interval", "effective_from",
                     "effective_to", "as_of"):
        assert temporal not in names, temporal


def test_the_withdrawal_does_not_touch_FX_RATE_lookups():
    """Scoped to policy VALIDITY intervals. Mid-window rate lookup is an as-of JOIN in the operator
    graph and remains load-bearing — the two live in different modules, which is the point."""
    from featuregen.materialize.operator_graph_v2 import AsOfFxJoinV2, OperatorKindV2

    assert OperatorKindV2.AS_OF_FX_JOIN in set(OperatorKindV2)
    assert "as_of_ref" in {f.name for f in dataclasses.fields(AsOfFxJoinV2)}
