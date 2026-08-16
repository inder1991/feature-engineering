"""C-C8 — realization identity: what APPLYING a governed policy actually does, and to what.

C-C7's occurrence says a policy is needed at a place. A REALIZATION says what applying it there
does: which column carries the direction flag, that ``debit`` is spelled ``D`` in this ledger, which
table serves a rate. The two are separate because the need is a property of the formula and the
answer is a property of the source, and one formula's occurrence can be realized differently in two
environments.

**The family key is frozen EXPLICITLY** as policy kind + policy ref + bound physical dataset +
environment + semantic role. Every part earns its place: drop the dataset binding and the "current"
realization for ``direction_sign:foundation-signed-by-indicator`` merges two ledgers that spell
debit differently, and one of them starts reading the other's encoding. Drop the role and a policy
used for two purposes over one dataset collapses into one pointer. The key is what "current" is
current FOR, so anything missing from it is something the pointer silently averages over.

**Revision id and executable content hash are SEPARATE, deliberately.** Two proposals with identical
semantics — say a source-derived realization and an LLM-proposed one that happen to agree — share an
``executable_content_hash`` because they execute identically, and keep DIFFERENT ``revision_id``
values because they are different artifacts with different provenance and different review histories.
Collapsing them on content would let an LLM proposal inherit a source's approval by agreeing with it.

**``realizes_occurrences`` is created here** and is the link back to C-C7: a realization names the
occurrence hashes it answers. Without it a realization floats free of any reason to exist, and
deleting an operator from a graph could not be detected as leaving an occurrence unrealized.

**Conflict findings are RETAINED, never cleared.** A realization that resolved a conflict still had
one; dropping the finding on resolution destroys the only record that the question was ever open.

**Pilot realizations are TIMELESS.** There is no validity interval here and no
``POLICY_INTERVAL_UNSUPPORTED`` code, because interval detection was never built — there is nothing
to remove and nothing to disable. A field meaning "no interval known yet" would be worse than its
absence: it would read as "this policy holds for all time", which is a claim nobody made. This is
scoped to policy VALIDITY intervals and does not touch mid-window FX RATE lookups, which are an
as-of join (C-C10a) and remain load-bearing.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.canonical import jcs_sha256
from featuregen.formula.policy_occurrences import PolicyOccurrenceV1

__all__ = [
    "ConflictFindingV1",
    "PolicyRealizationRevisionV1",
    "RealizationFamilyKeyV1",
    "RealizationProvenanceV1",
    "family_key_hash",
    "family_key_for",
    "unrealized_occurrences",
]


class RealizationProvenanceV1(StrEnum):
    """Where a realization's content came from. C-C9's admissibility keys on this.

    ``LLM_PROPOSED`` is never called evidence-validated on its own — agreeing with a source is not
    the same as being derived from one, and the distinction has to survive in the type or it is
    gone by the time anyone asks.
    """

    SOURCE_DERIVED = "source_derived"
    LLM_PROPOSED = "llm_proposed"
    HUMAN_AUTHORED = "human_authored"


@dataclass(frozen=True, slots=True)
class RealizationFamilyKeyV1:
    """What a "current" realization is current FOR.

    Five parts, all identity-bearing. See the module docstring for why each one is load-bearing —
    in short, anything left out is something the ``current`` pointer silently averages over.
    """

    policy_kind: str
    policy_ref: str
    bound_dataset: str
    environment_id: str
    semantic_role: str

    def __post_init__(self) -> None:
        for value, what in (
            (self.policy_kind, "policy_kind"), (self.policy_ref, "policy_ref"),
            (self.bound_dataset, "bound_dataset"), (self.environment_id, "environment_id"),
            (self.semantic_role, "semantic_role"),
        ):
            if not value.strip():
                raise ValueError(
                    f"a realization family key with a blank {what} would merge every family that "
                    f"differs only in {what}, and the 'current' pointer would then be current for "
                    f"more than one thing at once")

    def identity_payload(self) -> dict[str, Any]:
        return {
            "policy_kind": self.policy_kind,
            "policy_ref": self.policy_ref,
            "bound_dataset": self.bound_dataset,
            "environment_id": self.environment_id,
            "semantic_role": self.semantic_role,
        }


def family_key_hash(key: RealizationFamilyKeyV1) -> str:
    """The family's identity — what a ``current`` pointer is keyed on."""
    return jcs_sha256(key.identity_payload())


def family_key_for(occurrence: PolicyOccurrenceV1) -> RealizationFamilyKeyV1:
    """The family an occurrence must be realized in.

    Derived from the occurrence rather than chosen, so an occurrence and its realization cannot
    disagree about which dataset, environment or role they are talking about.
    """
    return RealizationFamilyKeyV1(
        policy_kind=occurrence.policy_kind,
        policy_ref=occurrence.policy_ref,
        bound_dataset=occurrence.bound_dataset,
        environment_id=occurrence.environment_id,
        semantic_role=occurrence.semantic_role,
    )


@dataclass(frozen=True, slots=True)
class ConflictFindingV1:
    """A conflict observed while deriving a realization — RETAINED after resolution.

    ``resolved`` records what happened to it, and the finding stays either way. Dropping a finding
    on resolution destroys the only record that the question was ever open, which is exactly what a
    later reviewer needs to see.
    """

    code: str
    detail: str
    resolved: bool

    def __post_init__(self) -> None:
        if not self.code.strip():
            raise ValueError("a conflict finding with no code cannot be looked up or counted")

    def identity_payload(self) -> dict[str, Any]:
        return {"code": self.code, "detail": self.detail, "resolved": self.resolved}


@dataclass(frozen=True, slots=True)
class PolicyRealizationRevisionV1:
    """ONE realization revision: an executable answer, its provenance, and what it realizes.

    ``revision_id`` identifies the ARTIFACT; ``executable_content_hash`` identifies what it DOES.
    Two revisions may share the second and never the first — see the module docstring.
    """

    revision_id: str
    family_key: RealizationFamilyKeyV1
    executable_content_hash: str
    cas_pointer: str
    provenance: RealizationProvenanceV1
    realizes_occurrences: tuple[str, ...]
    conflict_findings: tuple[ConflictFindingV1, ...] = ()

    def __post_init__(self) -> None:
        for value, what in (
            (self.revision_id, "revision_id"),
            (self.executable_content_hash, "executable_content_hash"),
            (self.cas_pointer, "cas_pointer"),
        ):
            if not value.strip():
                raise ValueError(
                    f"a realization revision with a blank {what} is not identifiable: it could "
                    f"neither be pinned by an execution nor found again by an auditor")
        if self.revision_id == self.executable_content_hash:
            raise ValueError(
                "revision_id equals executable_content_hash, which collapses the artifact and what "
                "it does into one identity: two proposals that agree on semantics would then BE "
                "one revision, and an LLM proposal could inherit a source's approval by agreeing "
                "with it")
        if not self.realizes_occurrences:
            raise ValueError(
                "a realization that realizes no occurrence has no reason to exist: `current` would "
                "point at an answer to a question nobody asked, and nothing would notice if the "
                "occurrence it was built for disappeared")
        ordered = tuple(sorted(self.realizes_occurrences))
        if len(set(ordered)) != len(ordered):
            raise ValueError(
                f"realizes_occurrences names the same occurrence twice ({self.revision_id}): one "
                f"occurrence realized once, or a coverage count is wrong by construction")
        for occurrence_hash in ordered:
            if not occurrence_hash.strip():
                raise ValueError("realizes_occurrences carries a blank occurrence hash")
        object.__setattr__(self, "realizes_occurrences", ordered)

    @property
    def is_evidence_validated(self) -> bool:
        """Whether this realization may be CALLED evidence-validated (C-C9's input).

        An LLM proposal is not evidence-validated by agreeing with a source. Stated as a property
        rather than left to each caller, because "derived from evidence" and "consistent with
        evidence" are the two things this distinction exists to keep apart.
        """
        return self.provenance is not RealizationProvenanceV1.LLM_PROPOSED

    def identity_payload(self) -> dict[str, Any]:
        return {
            "revision_id": self.revision_id,
            "family_key": self.family_key.identity_payload(),
            "executable_content_hash": self.executable_content_hash,
            "cas_pointer": self.cas_pointer,
            "provenance": self.provenance.value,
            "realizes_occurrences": list(self.realizes_occurrences),
            "conflict_findings": [f.identity_payload() for f in self.conflict_findings],
        }


def unrealized_occurrences(
    occurrences: tuple[PolicyOccurrenceV1, ...],
    revisions: tuple[PolicyRealizationRevisionV1, ...],
) -> tuple[PolicyOccurrenceV1, ...]:
    """Occurrences no revision claims to realize — the check ``realizes_occurrences`` makes possible.

    This is what lets a deleted operator be DETECTED rather than merely regretted: an occurrence
    with no realization is a governed policy the compilation needs and nothing answers.
    """
    realized = {occurrence_hash
                for revision in revisions
                for occurrence_hash in revision.realizes_occurrences}
    return tuple(o for o in occurrences if o.occurrence_hash not in realized)
