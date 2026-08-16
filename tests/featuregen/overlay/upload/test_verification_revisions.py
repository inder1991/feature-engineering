"""C-D1/C-D2/C-D3/C-D13 — verification identity, check set, verified output.

Four gates: *"constructible with NO attestation; V1's hash still requires one"*, *"the mapping is
explicit and total"* with *"a keys/types check does not satisfy JOIN_CONNECTIVITY"*, *"the type
carries all five"* with staleness and sweeping, and *"two attempts do not share a path"*.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.overlay.upload.verification_revisions import (
    CHECK_REQUIREMENT_COVERAGE,
    EXTERNAL_REQUIREMENTS,
    CheckSetV1,
    RetentionStateV1,
    VerificationCheckV1,
    VerificationExecutionIdentityV1,
    VerifiedOutputRevisionV1,
    check_set_hash,
    stale_against,
)

FX_POLICY = "sha256:fx-policy-v1"
STATUS_POLICY = "sha256:status-policy-v1"


def _check_set(**overrides) -> CheckSetV1:
    kwargs = dict(version=1, checks=tuple(VerificationCheckV1),
                  validator_versions=(("spark_sql", "1.2.0"),))
    kwargs.update(overrides)
    return CheckSetV1(**kwargs)


def _identity(**overrides) -> VerificationExecutionIdentityV1:
    kwargs = dict(generation_authorization_revision_id="gar-1",
                  check_set_hash=check_set_hash(_check_set()),
                  inventory_observation_id="obs-1", attempt=1)
    kwargs.update(overrides)
    return VerificationExecutionIdentityV1(**kwargs)


def _verified(**overrides) -> VerifiedOutputRevisionV1:
    kwargs = dict(revision_id="vor-1", execution_hash=_identity().execution_hash,
                  check_set_hash=check_set_hash(_check_set()),
                  validator_versions=(("spark_sql", "1.2.0"),),
                  pinned_policy_hashes=(FX_POLICY, STATUS_POLICY),
                  input_observation_strength="observed")
    kwargs.update(overrides)
    return VerifiedOutputRevisionV1(**kwargs)


# ══ C-D1 — NO publication attestation ════════════════════════════════════════════════════════════
def test_A_VERIFICATION_EXECUTION_CARRIES_NO_PUBLICATION_ATTESTATION():
    """The gate. V1's identity requires one, which forces the two to happen together — you cannot
    record "this ran and here are the results" without also recording "and it was published"."""
    names = {f.name for f in dataclasses.fields(VerificationExecutionIdentityV1)}
    for attestation in ("attestation", "publication", "published", "publisher"):
        assert not any(attestation in name for name in names), attestation
    assert _identity().execution_hash


@pytest.mark.parametrize("blank", ["generation_authorization_revision_id", "check_set_hash",
                                   "inventory_observation_id"])
def test_it_still_pins_what_it_verified(blank):
    with pytest.raises(ValueError, match="cannot be tied to what it verified"):
        _identity(**{blank: "  "})


def test_the_run_parameters_enter_identity_and_are_order_independent():
    assert _identity(run_parameters=(("as_of", "2026-06-30"), ("ccy", "AED"))).execution_hash == \
        _identity(run_parameters=(("ccy", "AED"), ("as_of", "2026-06-30"))).execution_hash
    assert _identity(run_parameters=(("as_of", "2026-06-30"),)).execution_hash != \
        _identity().execution_hash


# ══ C-D13 — the staging location is PER-ATTEMPT ══════════════════════════════════════════════════
def test_TWO_ATTEMPTS_DO_NOT_SHARE_A_PATH():
    """The gate. The existing root is generation-scoped, so without this a second verification
    writes over the first and S10's "exact staging output" names two things."""
    first, second = _identity(attempt=1), _identity(attempt=2)
    assert first.staging_path("s3://staging") != second.staging_path("s3://staging")
    assert first.execution_hash != second.execution_hash


def test_the_attempt_is_IN_the_identity_not_only_the_path():
    assert "attempt" in _identity().identity_payload()


def test_attempt_zero_is_refused():
    """0 would collide with the generation-scoped root this field exists to replace."""
    with pytest.raises(ValueError, match="counted from 1"):
        _identity(attempt=0)


def test_the_path_is_rooted_under_the_authorization_it_verifies():
    path = _identity(attempt=3).staging_path("s3://staging/")
    assert path == "s3://staging/gar-1/attempt=3"


# ══ C-D2 — the mapping is explicit and TOTAL ═════════════════════════════════════════════════════
def test_THE_COVERAGE_MAPPING_IS_TOTAL_OVER_THE_CHECK_VOCABULARY():
    """A check with no declared coverage would silently satisfy nothing while appearing to run."""
    assert set(CHECK_REQUIREMENT_COVERAGE) == set(VerificationCheckV1)


def test_every_external_requirement_is_covered_by_SOME_check():
    """A requirement no check covers would look satisfied because nothing said otherwise."""
    covered = frozenset().union(*CHECK_REQUIREMENT_COVERAGE.values())
    assert covered == set(EXTERNAL_REQUIREMENTS)


def test_A_KEYS_AND_TYPES_CHECK_DOES_NOT_SATISFY_JOIN_CONNECTIVITY():
    """The plan's named gate, and the reason the mapping is written down: a schema check proves the
    columns are the ones promised and says nothing about whether the join found anything. Treating
    "the shape is right" as "the join worked" is how an all-null feature ships looking healthy."""
    schema_only = CHECK_REQUIREMENT_COVERAGE[VerificationCheckV1.RESULT_SCHEMA]
    assert "JOIN_CONNECTIVITY" not in schema_only
    assert schema_only == {"SCHEMA_CONFORMANCE", "TYPE_STABILITY"}
    assert CHECK_REQUIREMENT_COVERAGE[VerificationCheckV1.JOIN_ORPHANS] == {"JOIN_CONNECTIVITY"}


def test_a_check_set_reports_only_what_ITS_checks_cover():
    partial = _check_set(checks=(VerificationCheckV1.RESULT_SCHEMA,))
    assert partial.satisfies() == {"SCHEMA_CONFORMANCE", "TYPE_STABILITY"}
    assert "JOIN_CONNECTIVITY" not in partial.satisfies()
    assert _check_set().satisfies() == set(EXTERNAL_REQUIREMENTS)


def test_an_empty_or_duplicated_check_set_is_refused():
    with pytest.raises(ValueError, match="the word without the work"):
        _check_set(checks=())
    with pytest.raises(ValueError, match="appears twice"):
        _check_set(checks=(VerificationCheckV1.RESULT_SCHEMA, VerificationCheckV1.RESULT_SCHEMA))


def test_the_check_set_is_VERSIONED():
    assert _check_set(version=1).identity_payload()["version"] == 1
    assert check_set_hash(_check_set(version=1)) != check_set_hash(_check_set(version=2))
    with pytest.raises(ValueError, match="names no published rule set"):
        _check_set(version=0)


def test_validator_versions_enter_the_check_set_hash():
    """A different validator can pass what the previous one failed."""
    assert check_set_hash(_check_set()) != check_set_hash(
        _check_set(validator_versions=(("spark_sql", "2.0.0"),)))


# ══ C-D3 — all five, and staleness ═══════════════════════════════════════════════════════════════
def test_the_verified_output_carries_ALL_FIVE():
    names = {f.name for f in dataclasses.fields(VerifiedOutputRevisionV1)}
    assert {"check_set_hash", "validator_versions", "pinned_policy_hashes",
            "input_observation_strength", "retention_state"} <= names


def test_A_POLICY_CHANGED_AFTER_VERIFICATION_MAKES_THE_PASS_STALE():
    """The gate. Without the pins it would keep vouching for an artifact whose meaning moved."""
    verified = _verified()
    assert stale_against(verified, [FX_POLICY, STATUS_POLICY]) == ()
    assert stale_against(verified, [STATUS_POLICY]) == (FX_POLICY,)


def test_staleness_names_WHICH_policy_moved():
    """A currency conversion changing is a different conversation from a status policy changing."""
    assert stale_against(_verified(), []) == (FX_POLICY, STATUS_POLICY)


def test_an_output_pinning_no_policies_is_refused():
    """It could never go stale, so nothing would ever notice."""
    with pytest.raises(ValueError, match="cannot go stale"):
        _verified(pinned_policy_hashes=())


def test_AN_EXPIRED_STAGED_OUTPUT_IS_SWEPT_AND_NOT_SERVABLE():
    """Reusing `runtime/blob_gc`'s discipline rather than inventing a second lifecycle."""
    assert [s.value for s in RetentionStateV1] == [
        "live", "marked_orphan", "quarantined", "swept"]
    assert _verified().is_servable
    assert _verified(retention_state=RetentionStateV1.MARKED_ORPHAN).is_servable
    assert not _verified(retention_state=RetentionStateV1.QUARANTINED).is_servable
    assert not _verified(retention_state=RetentionStateV1.SWEPT).is_servable


def test_retention_state_is_NOT_identity_bearing():
    """Sweeping an output's bytes does not change which verification it recorded — the record and
    the blob have different lifetimes, which is the point of the state living beside the hash."""
    assert _verified().content_hash == _verified(
        retention_state=RetentionStateV1.SWEPT).content_hash


def test_the_input_observation_strength_is_recorded():
    """"Verified against observed data" and "verified against a declaration" are different claims."""
    assert _verified(input_observation_strength="declared").content_hash != _verified(
        input_observation_strength="observed").content_hash
    with pytest.raises(ValueError, match="a word rather than a claim"):
        _verified(input_observation_strength=" ")
