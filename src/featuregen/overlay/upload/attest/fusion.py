"""P0 shadow-measurement harness — Task 4: the CONFIDENCE FUSION (design §4).

``fuse`` combines the three shadow-measurement signals — the proposer's original classification,
an independent re-classification (Task 3, a second LLM call blind to the first), and deterministic
grounding (Task 2, no LLM) — into ONE transparent confidence score in ``[0, 1]``. It is a PURE
function: no DB connection, no LLM call, no write, no gold-label lookup (calibration against gold
is the *report*'s job, Task 6 — this function must never see a gold value).

Fusion rules (design §4 — "a transparent agreement vector ... a simple monotone fusion"):

* **Agreement raises, disagreement lowers.** When the proposer and the independent reclassifier
  produce the SAME value, confidence moves above a neutral prior; when they differ — or either
  value is missing (``None``), which is NEVER treated as an agreement — confidence moves below it.
* **A grounding conflict caps confidence LOW, unconditionally.** ``grounding.conflict`` means at
  least one deterministic check (type/path/sibling consistency — see ``grounding.py``) actively
  FAILED against recorded evidence. That hard contradiction overrides even two agreeing LLMs: two
  providers can share a correlated mistake, but a failed deterministic check cannot be argued away
  by more LLM agreement.
* **Grounding coverage scales how much the LLM-agreement is trusted (the decorrelation guard).**
  Two independently-prompted LLMs agreeing is *some* signal even with zero grounding evidence
  (``coverage == 0.0``) — but it must contribute LESS than the same agreement corroborated by fully
  present deterministic checks (``coverage == 1.0``). Without this guard, two ungrounded LLMs that
  happen to agree (including on a shared blind spot neither can see) would score identically to two
  LLMs whose agreement is independently checked against the catalog's own recorded evidence —
  defeating the entire point of decorrelation.

No gold peeking: this module has no ``field_evidence``/``attestation_gold_label`` import, no
``DbConn`` parameter, and no provider client — it is import-light on purpose (only the sibling
``grounding`` module, for its result type).
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.attest.grounding import GroundingV1

# ── tunable weights (named + local, not magic numbers, per the "transparent" design requirement) ───
_BASE = 0.5                     # neutral prior — no agreement signal either way
_AGREE_BASE_WEIGHT = 0.05       # minimal trust in bare LLM-agreement, even at ZERO grounding coverage
_AGREE_COVERAGE_WEIGHT = 0.40   # additional trust, scaled linearly by grounding coverage in [0, 1]
_CONFLICT_CAP = 0.2             # hard ceiling applied whenever grounding.conflict is True


@dataclass(frozen=True, slots=True)
class FusionV1:
    """The fused confidence for one ``(logical_ref, field_name)`` observation (design §4)."""

    confidence: float
    agreement: dict[str, bool | float]


def _values_agree(proposer_value: str | None, reclassify_value: str | None) -> bool:
    """True only when BOTH values are present and match (case/whitespace-insensitive, mirroring
    ``grounding.py``'s value-comparison convention). A missing value on either side is never an
    agreement — it is scored the same as an active disagreement."""
    if proposer_value is None or reclassify_value is None:
        return False
    return proposer_value.strip().lower() == reclassify_value.strip().lower()


def fuse(*, proposer_value: str | None, reclassify_value: str | None,
         grounding: GroundingV1) -> FusionV1:
    """Combine proposer/reclassifier agreement with deterministic grounding into one transparent,
    monotone confidence in ``[0, 1]``. PURE — no DB, no LLM call, no gold label.

    Returning the ``agreement`` components alongside the scalar ``confidence`` lets the shadow
    observation row and the downstream report audit exactly *why* a confidence landed where it did.
    """
    agree = _values_agree(proposer_value, reclassify_value)

    # Coverage scales how much the LLM-agreement (or disagreement) signal is trusted — the
    # decorrelation guard: zero coverage still contributes (the base weight), but strictly less
    # than fully-covered grounding does.
    weight = _AGREE_BASE_WEIGHT + _AGREE_COVERAGE_WEIGHT * grounding.coverage
    confidence = _BASE + weight if agree else _BASE - weight

    if grounding.conflict:
        # A hard deterministic contradiction overrides agreement entirely, regardless of coverage.
        confidence = min(confidence, _CONFLICT_CAP)

    confidence = max(0.0, min(1.0, confidence))

    agreement: dict[str, bool | float] = {
        "proposer_reclassify_agree": agree,
        "grounding_coverage": grounding.coverage,
        "grounding_conflict": grounding.conflict,
    }
    return FusionV1(confidence=confidence, agreement=agreement)
