"""C-D1/C-D2/C-D3/C-D13 — verification identity, the check set, and the verified output.

**A verification execution carries NO publication attestation** (C-D1). V1's identity requires one,
which forces the two to happen together: you cannot record "this ran and these are the results"
without also recording "and it was published". Separating them is what makes an on-demand sandbox
verification possible at all — it runs, it produces a verdict, and publishing is a later decision
somebody else makes.

**The staging location is PER-ATTEMPT** (C-D13). The existing root is generation-scoped, so a second
verification of the same generation would write over the first and S10's "exact staging output"
would name two different things at two different times. ``attempt`` is part of the identity, so two
attempts cannot share a path by construction rather than by convention.

**The check set is VERSIONED and its mapping to external requirements is TOTAL** (C-D2). Every check
declares which of the eight external requirements it may satisfy, and the mapping is exhaustive over
the check vocabulary — a check with no declared coverage would silently satisfy nothing while
appearing to run, and a requirement no check covers would look satisfied because nothing said
otherwise.

**A verified output can go STALE** (C-D3). It pins the executable policy hashes it verified against,
so a policy changed afterwards makes the pass stale rather than silently continuing to vouch for an
artifact whose meaning moved. Retention reuses ``runtime/blob_gc``'s existing discipline —
``marked_orphan → quarantined → swept`` — rather than inventing a second lifecycle.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.canonical import jcs_sha256

__all__ = [
    "CHECK_REQUIREMENT_COVERAGE",
    "EXTERNAL_REQUIREMENTS",
    "CheckSetV1",
    "RetentionStateV1",
    "VerificationCheckV1",
    "VerificationExecutionIdentityV1",
    "VerifiedOutputRevisionV1",
    "check_set_hash",
    "stale_against",
]

#: The eight external requirements a verification can bear on. Frozen as a closed set so
#: :data:`CHECK_REQUIREMENT_COVERAGE` can be proved total over it.
EXTERNAL_REQUIREMENTS: tuple[str, ...] = (
    "SCHEMA_CONFORMANCE",
    "NON_NULL_CONTRACT",
    "FEATURE_NULL_RULES",
    "SPINE_COMPLETENESS",
    "SPINE_UNIQUENESS",
    "JOIN_CONNECTIVITY",
    "JOIN_AMPLIFICATION",
    "TYPE_STABILITY",
)


class VerificationCheckV1(StrEnum):
    """The versioned check vocabulary."""

    RESULT_SCHEMA = "result_schema"
    NON_NULL_COLUMNS = "non_null_columns"
    FEATURE_NULL_RULES = "feature_null_rules"
    SPINE_COMPLETENESS = "spine_completeness"
    SPINE_UNIQUENESS = "spine_uniqueness"
    JOIN_ORPHANS = "join_orphans"
    JOIN_AMPLIFICATION = "join_amplification"


#: WHICH external requirements each check may satisfy. Total over the check vocabulary by test.
#:
#: The load-bearing entry is ``RESULT_SCHEMA``: it covers keys and types, and NOT
#: ``JOIN_CONNECTIVITY``. A schema check proves the columns are the ones promised; it says nothing
#: about whether the join that produced them found anything, and treating "the shape is right" as
#: "the join worked" is how an all-null feature ships looking healthy.
CHECK_REQUIREMENT_COVERAGE: Mapping[VerificationCheckV1, frozenset[str]] = {
    VerificationCheckV1.RESULT_SCHEMA: frozenset({"SCHEMA_CONFORMANCE", "TYPE_STABILITY"}),
    VerificationCheckV1.NON_NULL_COLUMNS: frozenset({"NON_NULL_CONTRACT"}),
    VerificationCheckV1.FEATURE_NULL_RULES: frozenset({"FEATURE_NULL_RULES"}),
    VerificationCheckV1.SPINE_COMPLETENESS: frozenset({"SPINE_COMPLETENESS"}),
    VerificationCheckV1.SPINE_UNIQUENESS: frozenset({"SPINE_UNIQUENESS"}),
    VerificationCheckV1.JOIN_ORPHANS: frozenset({"JOIN_CONNECTIVITY"}),
    VerificationCheckV1.JOIN_AMPLIFICATION: frozenset({"JOIN_AMPLIFICATION"}),
}


@dataclass(frozen=True, slots=True)
class CheckSetV1:
    """The versioned set of checks a verification ran, and the validators that ran them."""

    version: int
    checks: tuple[VerificationCheckV1, ...]
    validator_versions: tuple[tuple[str, str], ...]

    def __post_init__(self) -> None:
        if self.version < 1:
            raise ValueError("a check set version below 1 names no published rule set")
        if not self.checks:
            raise ValueError(
                "a check set with no checks verifies nothing, and an output 'verified' by it would "
                "carry the word without the work")
        if len(set(self.checks)) != len(self.checks):
            raise ValueError(f"a check appears twice: {[c.value for c in self.checks]}")

    def satisfies(self) -> frozenset[str]:
        """Which external requirements THIS set can bear on."""
        return frozenset().union(*(CHECK_REQUIREMENT_COVERAGE[c] for c in self.checks))

    def identity_payload(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "checks": sorted(c.value for c in self.checks),
            "validator_versions": [list(pair) for pair in sorted(self.validator_versions)],
        }


def check_set_hash(check_set: CheckSetV1) -> str:
    return jcs_sha256(check_set.identity_payload())


@dataclass(frozen=True, slots=True)
class VerificationExecutionIdentityV1:
    """What a verification RUN is — with no publication attestation (C-D1).

    ``attempt`` is part of the identity (C-D13): the existing staging root is generation-scoped, so
    without it a second verification of the same generation writes over the first and "the exact
    staging output" names two things.
    """

    generation_authorization_revision_id: str
    check_set_hash: str
    inventory_observation_id: str
    attempt: int
    run_parameters: tuple[tuple[str, str], ...] = ()

    def __post_init__(self) -> None:
        for value, what in (
            (self.generation_authorization_revision_id, "generation_authorization_revision_id"),
            (self.check_set_hash, "check_set_hash"),
            (self.inventory_observation_id, "inventory_observation_id"),
        ):
            if not value.strip():
                raise ValueError(
                    f"a verification execution with a blank {what} cannot be tied to what it "
                    f"verified, which environment it ran in, or what it checked")
        if self.attempt < 1:
            raise ValueError(
                f"attempt={self.attempt} is not an attempt: attempts are counted from 1, and 0 "
                f"would collide with the generation-scoped root this field exists to replace")
        names = [n for n, _ in self.run_parameters]
        if len(set(names)) != len(names):
            raise ValueError(f"a run parameter is bound twice: {sorted(names)}")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "generation_authorization_revision_id": self.generation_authorization_revision_id,
            "check_set_hash": self.check_set_hash,
            "inventory_observation_id": self.inventory_observation_id,
            "attempt": self.attempt,
            "run_parameters": [list(p) for p in sorted(self.run_parameters)],
        }

    @property
    def execution_hash(self) -> str:
        return jcs_sha256(self.identity_payload())

    def staging_path(self, root: str) -> str:
        """A PER-ATTEMPT staging location. Two attempts do not share a path."""
        return f"{root.rstrip('/')}/{self.generation_authorization_revision_id}/attempt={self.attempt}"


class RetentionStateV1(StrEnum):
    """Reused verbatim from ``runtime/blob_gc`` rather than invented — one lifecycle, one meaning."""

    LIVE = "live"
    MARKED_ORPHAN = "marked_orphan"
    QUARANTINED = "quarantined"
    SWEPT = "swept"

    @property
    def is_servable(self) -> bool:
        """A swept output's bytes are gone; a quarantined one's are pending deletion. Neither may be
        served, and the distinction matters to an operator deciding whether recovery is possible."""
        return self in (RetentionStateV1.LIVE, RetentionStateV1.MARKED_ORPHAN)


@dataclass(frozen=True, slots=True)
class VerifiedOutputRevisionV1:
    """A verification that PASSED, and everything that could later make it untrue."""

    revision_id: str
    execution_hash: str
    check_set_hash: str
    validator_versions: tuple[tuple[str, str], ...]
    pinned_policy_hashes: tuple[str, ...]
    input_observation_strength: str
    retention_state: RetentionStateV1 = RetentionStateV1.LIVE

    def __post_init__(self) -> None:
        for value, what in ((self.revision_id, "revision_id"),
                            (self.execution_hash, "execution_hash"),
                            (self.check_set_hash, "check_set_hash"),
                            (self.input_observation_strength, "input_observation_strength")):
            if not value.strip():
                raise ValueError(
                    f"a verified output with a blank {what} cannot say what it verified or how "
                    f"strongly, and 'verified' without those is a word rather than a claim")
        if not self.pinned_policy_hashes:
            raise ValueError(
                "a verified output pinning no policy hashes cannot go stale: a policy changed "
                "afterwards would leave it vouching for an artifact whose meaning moved, with "
                "nothing able to notice")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "execution_hash": self.execution_hash,
            "check_set_hash": self.check_set_hash,
            "validator_versions": [list(p) for p in sorted(self.validator_versions)],
            "pinned_policy_hashes": sorted(self.pinned_policy_hashes),
            "input_observation_strength": self.input_observation_strength,
        }

    @property
    def content_hash(self) -> str:
        return jcs_sha256(self.identity_payload())

    @property
    def is_servable(self) -> bool:
        return self.retention_state.is_servable


def stale_against(
    verified: VerifiedOutputRevisionV1, current_policy_hashes: Sequence[str],
) -> tuple[str, ...]:
    """The pinned policies whose current hash no longer matches — empty when the pass still holds.

    Returns the DRIFTED policies rather than a boolean, because an operator deciding whether to
    re-verify needs to know which policy moved: a currency conversion changing is a different
    conversation from a status policy changing.
    """
    current = set(current_policy_hashes)
    return tuple(sorted(set(verified.pinned_policy_hashes) - current))
