"""Task 2b — the content-addressed tie-break verdict seam.

A grounding TIE (two or more columns scoring identically for one recipe need) is today resolved by
column-name sort order. The verdict seam stores an LLM deliberation about such a tie ONCE, keyed by
everything the answer depends on, through the platform's replay store (`structured_result`,
migration 1039 — cross-run, provenance-linked; NOT `llm_call`'s run-scoped retry read, which only
looks like a cache).

THE CACHING RULE (plan, owner-settled 2026-08-10): store only what is expensive to remake and cheap
to check. The verdict is an LLM call; its check is a hash compare. The GROUNDING itself is never
stored — it is recomputed per request precisely so nothing here can go stale: correct a definition,
confirm a fact, and the key below changes, making the old verdict unreachable rather than wrong.

The verdict is a RANKING of the tied candidates, not a single winner (second-review finding F): the
gauntlet-refusal re-bind walks the model's own order instead of degrading to alphabetical among the
residual ties.

Read-side discipline mirrors the concept critic's `_from_stored`: the store is typed, but a stored
ranking is RE-VALIDATED against the caller's CURRENT tied set on every read — a verdict that names a
ref not in the tie is refused whole, and the caller falls back to the deterministic order. A wrong
binding is the one failure this platform never accepts silently.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
)

#: The replay-store identity of a tie-break deliberation. Version bumps when the QUESTION changes
#: shape (new prompt wording, new output contract) — stored verdicts for the old version stay
#: untouched and unreachable, exactly like a concept-vocabulary bump re-asks classification.
TIE_BREAK_RESULT_TYPE = "tie_break_verdict"
TIE_BREAK_RESULT_VERSION = 1

#: The prompt identity folded into the INPUT hash: new instructions are a new question.
TIE_BREAK_PROMPT_ID = "overlay_tie_break"
TIE_BREAK_PROMPT_VERSION = 1


@dataclass(frozen=True, slots=True)
class TieBreakCandidate:
    """One tied column, carried WITH the enrichment text the deliberation reads. The text rides the
    key material so a human correction to any of it re-asks the question (the
    `producer_configuration_hash` lesson, applied at design time)."""

    ref: str
    definition: str = ""
    ai_summary: str = ""
    semantic_terms: str = ""


@dataclass(frozen=True, slots=True)
class TieBreakVerdictV1:
    """The stored deliberation: the tied refs in the model's preference order, best first, plus the
    bounded rationale for the top choice."""

    ranking: tuple[str, ...]
    rationale: str


def tie_break_input_hash(*, template_id: str, need_role: str, need_concept: str, intent: str,
                         tied: Sequence[TieBreakCandidate],
                         prompt_version: int = TIE_BREAK_PROMPT_VERSION) -> str:
    """The content key: EVERYTHING the verdict depends on, nothing it does not.

    The tied set is sorted by ref before hashing — the same tie enumerated in a different order is
    the same question and must reuse its verdict. Candidate enrichment text is hashed verbatim: a
    corrected summary is a different question."""
    return canonical_hash({
        "version": "tie-break-input-v1",
        "prompt_id": TIE_BREAK_PROMPT_ID,
        "prompt_version": prompt_version,
        "template_id": template_id,
        "need_role": need_role,
        "need_concept": need_concept,
        "intent": intent,
        "tied": sorted(
            ({"ref": c.ref, "definition": c.definition, "ai_summary": c.ai_summary,
              "semantic_terms": c.semantic_terms} for c in tied),
            key=lambda d: d["ref"]),
    })


def store_tie_break_verdict(conn, *, input_hash: str, ranking: Sequence[str], rationale: str,
                            producer_ref: str) -> None:
    """Record one deliberation. Immutable: the SAME input recording a DIFFERENT ranking raises
    (`StructuredResultCorruption`) — replay identity means one answer per question. `producer_ref`
    is the audited call that produced it (an `llm_call_ref`), riding provenance so "why did this
    recipe bind that column?" ends at a recorded, redacted, budgeted call."""
    record_structured_result(
        conn,
        result_type=TIE_BREAK_RESULT_TYPE,
        result_version=TIE_BREAK_RESULT_VERSION,
        input_content_hash=input_hash,
        output={"ranking": list(ranking), "rationale": rationale},
        producer_kind="llm_call",
        producer_ref=producer_ref,
    )


def find_tie_break_verdict(conn, *, input_hash: str,
                           tied_refs: Iterable[str]) -> TieBreakVerdictV1 | None:
    """The cached deliberation for this exact question, or None (→ deterministic fallback).

    RE-VALIDATED on every read against the CURRENT tied set: the stored ranking must be exactly a
    permutation of ``tied_refs``. Anything else — a ref the tie no longer contains, a missing ref, a
    malformed payload — refuses WHOLE. The content key makes that near-impossible; the check makes
    "near" irrelevant."""
    stored = find_structured_result(
        conn,
        result_type=TIE_BREAK_RESULT_TYPE,
        result_version=TIE_BREAK_RESULT_VERSION,
        input_content_hash=input_hash,
    )
    if stored is None:
        return None
    ranking = stored.output.get("ranking")
    rationale = stored.output.get("rationale")
    if not isinstance(ranking, (list, tuple)) or not all(isinstance(r, str) for r in ranking):
        return None
    if sorted(ranking) != sorted(set(tied_refs)):
        return None
    return TieBreakVerdictV1(ranking=tuple(ranking),
                             rationale=rationale if isinstance(rationale, str) else "")
