"""C-C9 — which realization candidate may be used, and under what name (invariant 16).

**LLM-proposed realizations are USABLE, visibly.** That is the whole shape of invariant 16: an LLM
proposal is not discarded for being one, and it is not laundered into evidence either. It gets used
under its own name, and where a source disagrees the disagreement is RETAINED and visible rather
than resolved away.

The four cases, and why each is what it is:

* **Source beats LLM, with a retained visible conflict.** The source wins because it is derived from
  the data rather than about it. The conflict is kept because "the model read this differently" is
  information a reviewer wants, and silently preferring the source destroys it.
* **Two governed declarations REFUSE — even when they agree.** Two declarations for one policy over
  one dataset is a governance defect in the source, not a tie to break. Agreement today is not
  agreement tomorrow: the next edit to either one diverges silently, and whichever this code had
  picked becomes the answer nobody chose. The fix is upstream.
* **Two LLM-only candidates refuse unless they AGREE.** Agreement is decidable —
  ``executable_content_hash`` is exactly "what this realization does" — so two proposals that
  execute identically are one answer arrived at twice. Two that differ are a coin flip, and a coin
  flip about which column carries a direction flag is not a governed answer.
* **One valid LLM-only candidate is usable as ``LLM_PROPOSED``** and is never called
  evidence-validated. :attr:`PolicyRealizationRevisionV1.is_evidence_validated` owns that
  distinction so it cannot be re-decided per caller.

**Total over its inputs.** Every combination of governed and LLM candidate counts returns a verdict;
none raises and none falls through. A decision table with a hole is a decision table that admits
whatever the hole happens to evaluate to.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum

from featuregen.formula.policy_realization import (
    ConflictFindingV1,
    PolicyRealizationRevisionV1,
    RealizationProvenanceV1,
)

__all__ = [
    "AdmissibilityOutcomeV1",
    "AdmissibilityVerdictV1",
    "NO_CANDIDATE",
    "NO_DETERMINISTIC_WINNER",
    "SOURCE_OVERRODE_LLM",
    "TWO_GOVERNED_DECLARATIONS",
    "decide_admissibility",
]

TWO_GOVERNED_DECLARATIONS = "TWO_GOVERNED_DECLARATIONS"
NO_DETERMINISTIC_WINNER = "NO_DETERMINISTIC_WINNER"
NO_CANDIDATE = "NO_CANDIDATE"
#: Not a refusal — the RETAINED finding recorded when a source wins over a disagreeing proposal.
SOURCE_OVERRODE_LLM = "SOURCE_OVERRODE_LLM"


class AdmissibilityOutcomeV1(StrEnum):
    """What may be used, and under what name."""

    ADMITTED_EVIDENCE_LINKED = "admitted_evidence_linked"
    ADMITTED_LLM_PROPOSED = "admitted_llm_proposed"
    REFUSED = "refused"


@dataclass(frozen=True, slots=True)
class AdmissibilityVerdictV1:
    """The decision, the winner if any, and every conflict observed reaching it."""

    outcome: AdmissibilityOutcomeV1
    winner: PolicyRealizationRevisionV1 | None
    refusal_code: str
    conflicts: tuple[ConflictFindingV1, ...]

    def __post_init__(self) -> None:
        refused = self.outcome is AdmissibilityOutcomeV1.REFUSED
        if refused and self.winner is not None:
            raise ValueError(
                "a refusal cannot name a winner: a caller reading `winner` would use a realization "
                "the decision refused")
        if refused and not self.refusal_code.strip():
            raise ValueError("a refusal with no code cannot be reported or acted on")
        if not refused:
            if self.winner is None:
                raise ValueError(
                    "an admission with no winner admits nothing — the outcome and the payload "
                    "would disagree")
            if self.refusal_code:
                raise ValueError(
                    f"an admitted verdict carries refusal_code={self.refusal_code!r}: a reader "
                    f"cannot tell whether it was admitted or refused")
        if (self.outcome is AdmissibilityOutcomeV1.ADMITTED_LLM_PROPOSED
                and self.winner is not None and self.winner.is_evidence_validated):
            raise ValueError(
                "an LLM_PROPOSED outcome names an evidence-validated winner: the outcome and the "
                "winner's own provenance disagree about what this realization is")
        if (self.outcome is AdmissibilityOutcomeV1.ADMITTED_EVIDENCE_LINKED
                and self.winner is not None and not self.winner.is_evidence_validated):
            raise ValueError(
                "an evidence-linked outcome names an LLM-proposed winner, which would call a "
                "proposal evidence-validated — the exact laundering invariant 16 forbids")


def decide_admissibility(
    candidates: Sequence[PolicyRealizationRevisionV1],
) -> AdmissibilityVerdictV1:
    """Invariant 16's four-way table, total over its inputs.

    Args:
        candidates: every realization revision offered for ONE family. Callers must not mix
            families — a winner chosen across families would be current for more than one thing,
            which is what C-C8's family key exists to prevent.
    """
    governed = [c for c in candidates if c.is_evidence_validated]
    proposed = [c for c in candidates
                if c.provenance is RealizationProvenanceV1.LLM_PROPOSED]

    if len(governed) >= 2:
        return AdmissibilityVerdictV1(
            outcome=AdmissibilityOutcomeV1.REFUSED, winner=None,
            refusal_code=TWO_GOVERNED_DECLARATIONS,
            conflicts=(ConflictFindingV1(
                code=TWO_GOVERNED_DECLARATIONS,
                detail=(
                    f"{len(governed)} governed declarations for one family. This refuses even when "
                    f"they agree: agreement today is not agreement tomorrow, and the next edit to "
                    f"either one diverges silently while whichever was picked becomes the answer "
                    f"nobody chose. The fix is upstream, in the source"),
                resolved=False),))

    if len(governed) == 1:
        winner = governed[0]
        conflicts = tuple(
            ConflictFindingV1(
                code=SOURCE_OVERRODE_LLM,
                detail=(
                    f"the source-derived realization {winner.revision_id} was used over LLM "
                    f"proposal {other.revision_id}, which executes differently "
                    f"({other.executable_content_hash} vs {winner.executable_content_hash}). The "
                    f"disagreement is retained rather than resolved away: 'the model read this "
                    f"differently' is information a reviewer wants"),
                resolved=True)
            for other in proposed
            if other.executable_content_hash != winner.executable_content_hash)
        return AdmissibilityVerdictV1(
            outcome=AdmissibilityOutcomeV1.ADMITTED_EVIDENCE_LINKED, winner=winner,
            refusal_code="", conflicts=conflicts)

    if not proposed:
        return AdmissibilityVerdictV1(
            outcome=AdmissibilityOutcomeV1.REFUSED, winner=None, refusal_code=NO_CANDIDATE,
            conflicts=(ConflictFindingV1(
                code=NO_CANDIDATE,
                detail="no realization was offered for this family, so the policy this occurrence "
                       "needs has no executable answer at all",
                resolved=False),))

    executables = {c.executable_content_hash for c in proposed}
    if len(executables) > 1:
        return AdmissibilityVerdictV1(
            outcome=AdmissibilityOutcomeV1.REFUSED, winner=None,
            refusal_code=NO_DETERMINISTIC_WINNER,
            conflicts=(ConflictFindingV1(
                code=NO_DETERMINISTIC_WINNER,
                detail=(
                    f"{len(proposed)} LLM proposals executing {len(executables)} different ways, "
                    f"with no source to decide between them. Picking one would be a coin flip, and "
                    f"a coin flip about which column carries a governed policy is not a governed "
                    f"answer"),
                resolved=False),))

    # They AGREE — one answer arrived at more than once. Deterministic by revision id so the same
    # candidate set always yields the same winner.
    winner = sorted(proposed, key=lambda c: c.revision_id)[0]
    return AdmissibilityVerdictV1(
        outcome=AdmissibilityOutcomeV1.ADMITTED_LLM_PROPOSED, winner=winner, refusal_code="",
        conflicts=())
