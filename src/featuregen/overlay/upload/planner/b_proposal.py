"""Phase 3C.2b-i-B · Task 2 — ``RawFeatureProposalV1``: lossless capture.

The very first link in turning an untrusted LLM feature proposal into a governed cross-catalog
plan: a frozen, lossless record of the raw LLM proposal captured BEFORE the ``_vet`` gauntlet runs.
``_vet`` silently drops unknown operands — a later task (T4) diffs this raw record against the
vetted idea to detect any dropped/rewritten operand (``proposal_lossy``).

Pure data. NO ``_vet``, NO A, NO DB, NO I/O, NO pydantic. This task only defines the capture type
and proves it round-trips verbatim."""
from __future__ import annotations

from dataclasses import dataclass

# ---------------------------------------------------------------------------
# Policy version constant — a pinned literal, bumped on any policy change.
# ---------------------------------------------------------------------------

RAW_PROPOSAL_VERSION = "3c2bib.proposal.1.0.0"


@dataclass(frozen=True, slots=True)
class RawFeatureProposalV1:
    """The raw LLM feature proposal, captured verbatim before ``_vet`` runs.

    ``operands`` are the raw operand refs exactly as the LLM emitted them, order preserved. The
    remaining fields are captured, not consumed — in particular ``window`` is captured-not-consumed
    (a later task decides RECENCY/TREND; deferred here)."""
    operands: tuple[str, ...]
    operation: str | None
    window: str | None
    grain_hint: str | None
    version: str


def new_raw_proposal(
    *,
    operands: tuple[str, ...] | list[str],
    operation: str | None,
    window: str | None,
    grain_hint: str | None,
) -> RawFeatureProposalV1:
    """Construct a ``RawFeatureProposalV1``, stamping ``version=RAW_PROPOSAL_VERSION`` so callers
    get the version pinned without hand-passing it. ``operands`` is converted to a ``tuple`` if
    given as a ``list``; order is preserved verbatim."""
    return RawFeatureProposalV1(
        operands=tuple(operands),
        operation=operation,
        window=window,
        grain_hint=grain_hint,
        version=RAW_PROPOSAL_VERSION,
    )
