"""Pass C — candidate blocker (pure: no DB, no LLM).

Enumerates plausible join-candidate column pairs. Exactly ONE admission story: both columns are
join-key eligible, the tables differ (unless self-join is allowed), and the namespace verdict is
COMPATIBLE or POSSIBLE. AMBIGUOUS and INCOMPATIBLE pairs never reach scoring/propose — AMBIGUOUS
ones are surfaced only as weak diagnostics elsewhere (recomputed there, not carried from here).

Deterministic: columns are sorted by `object_ref` before the i<j double loop, so the same input
set always yields the same ordered candidate list.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.upload.passc.identifiers import ColMeta, is_join_key_eligible
from featuregen.overlay.upload.passc.namespace import classify_namespace
from featuregen.overlay.upload.passc.types import DEFAULT_CONFIG, NamespaceCompatibility, PassCConfig

_ADMITTED = frozenset({NamespaceCompatibility.COMPATIBLE, NamespaceCompatibility.POSSIBLE})


@dataclass(frozen=True, slots=True)
class CandidatePair:
    """One blocked candidate: `a` precedes `b` in object_ref order; the namespace verdict and its
    reason codes are carried forward so scoring never re-derives them."""
    a: ColMeta
    b: ColMeta
    namespace: NamespaceCompatibility
    namespace_reasons: tuple[str, ...]


def block_candidates(
    columns: list[ColMeta], *, allow_self_join: bool = False,
    cfg: PassCConfig = DEFAULT_CONFIG,
) -> list[CandidatePair]:
    """Enumerate join-candidate pairs from upload column metadata alone."""
    eligible = sorted((c for c in columns if is_join_key_eligible(c, cfg)),
                      key=lambda c: c.object_ref)
    out: list[CandidatePair] = []
    for i, a in enumerate(eligible):
        for b in eligible[i + 1:]:
            if a.table == b.table and not allow_self_join:
                continue
            namespace, reasons = classify_namespace(a, b, cfg)
            if namespace in _ADMITTED:
                out.append(CandidatePair(a=a, b=b, namespace=namespace, namespace_reasons=reasons))
    return out
