"""Pass C — candidate blocker + scorer (pure: no DB, no LLM).

Enumerates plausible join-candidate column pairs. Exactly ONE admission story: both columns are
join-key eligible, the tables differ (unless self-join is allowed), and the namespace verdict is
COMPATIBLE or POSSIBLE. AMBIGUOUS and INCOMPATIBLE pairs never reach scoring/propose — AMBIGUOUS
ones are surfaced only as weak diagnostics elsewhere (recomputed there, not carried from here).

Deterministic: columns are sorted by `object_ref` before the i<j double loop, so the same input
set always yields the same ordered candidate list.

`score(...)` turns a blocked pair into self-explaining `JoinCandidateEvidenceV1`. The bucket rules
a two-round review baked in (in order):
1. ONLY `INFERRED_FROM_CONFIRMED_GRAIN` is strong-eligible — both-grain (`AMBIGUOUS_BOTH_GRAINS`,
   a would-be 1:1) and neither-grain (`MANY_TO_MANY_RISK`) are FORCED weak regardless of score,
   with both grains in `missing_requirements`. Two unique columns are not necessarily 1:1
   business-equivalent (`account.account_id` != `card.card_id`), and neither-grain never gets a
   defaulted cardinality.
2. A POSSIBLE namespace caps at weak unless a `related_terms_key_link` signal fired.
3. Else thresholds: strong >= 80, weak >= 50, suppressed < 50.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass

from featuregen.overlay.upload.passc.identifiers import (
    ColMeta, _canon, _words, is_join_key_eligible, normalized_identifier_concept)
from featuregen.overlay.upload.passc.namespace import classify_namespace
from featuregen.overlay.upload.passc.types import (
    ALGORITHM_VERSION, CONFIG_VERSION, CardinalityInferenceStatus, DEFAULT_CONFIG,
    JoinCandidateEvidenceV1, NamespaceCompatibility, PassCConfig, SignalEvidence)

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


def _clean(text: str) -> str:
    return (text or "").strip().lower()


def _positive_signals(pair: CandidatePair, cfg: PassCConfig) -> tuple[SignalEvidence, ...]:
    """Fire every configured positive signal that applies. Blank attributes never fire — notably a
    shared-EMPTY canonical column name is NOT a name match (Task-3 review guard: `id` and `key`
    both canonicalize to "" and share nothing)."""
    a, b = pair.a, pair.b
    refs = (a.object_ref, b.object_ref)
    out: list[SignalEvidence] = []

    def fire(name: str, explanation: str) -> None:
        delta = cfg.weights.get(name, 0)
        if delta:
            out.append(SignalEvidence(signal_name=name, score_delta=delta,
                                      evidence_refs=refs, explanation=explanation))

    concept = normalized_identifier_concept(a)
    if concept and concept == normalized_identifier_concept(b):
        fire("same_identifier_concept", f"both columns denote the identifier concept '{concept}'")
    if "related_terms_link" in pair.namespace_reasons:   # gated upstream: id-like key link only
        fire("related_terms_key_link", "a curated related-terms link ties these identifier columns")
    name_canon = _canon(a.column)
    if name_canon and name_canon == _canon(b.column):
        fire("same_column_name", f"canonical column name '{name_canon}' matches")
    term = " ".join(_words(a.term_name))
    if term and term == " ".join(_words(b.term_name)):
        fire("same_term_name", f"term name '{term}' matches")
    ent = _clean(a.column_entity)
    if ent and ent == _clean(b.column_entity):
        fire("same_column_entity", f"both columns carry the confirmed entity tag '{ent}'")
    leaf = _clean(a.bian_leaf)
    if leaf and leaf == _clean(b.bian_leaf) and leaf not in cfg.mixed_bian_leaves:
        fire("same_bian_leaf", f"same BIAN leaf '{leaf}' (coarse prior)")
    fibo = _clean(a.fibo_leaf)
    if fibo and fibo == _clean(b.fibo_leaf):
        fire("same_fibo_leaf", f"same FIBO leaf '{fibo}' (coarse prior)")
    tent = _clean(a.table_entity)
    if tent and tent == _clean(b.table_entity):
        fire("compatible_phase2_entity", f"both tables carry the Phase-2 entity '{tent}'")
    if a.is_grain != b.is_grain:   # XOR — both-grain gives no directional support
        grain_side = a if a.is_grain else b
        fire("one_side_confirmed_grain",
             f"{grain_side.object_ref} is a confirmed grain (directional support)")
    dom = _clean(a.data_domain)
    if dom and dom == _clean(b.data_domain):
        fire("compatible_domain", f"same data domain '{dom}'")
    return tuple(out)


def _orient(pair: CandidatePair) -> tuple[ColMeta, ColMeta, str | None, CardinalityInferenceStatus]:
    """Direction + cardinality from Phase-2 grain. Returns (from, to, cardinality, status).
    Right-grain-only keeps a→b; left-grain-only flips to b→a (many rows point AT the grain side);
    both-grain is a 1:1 CAUTION, never auto-proposed; neither-grain proposes NO cardinality."""
    a, b = pair.a, pair.b
    status = CardinalityInferenceStatus
    if b.is_grain and not a.is_grain:
        return a, b, "N:1", status.INFERRED_FROM_CONFIRMED_GRAIN
    if a.is_grain and not b.is_grain:
        return b, a, "N:1", status.INFERRED_FROM_CONFIRMED_GRAIN
    if a.is_grain:                                       # both sides grain
        return a, b, "1:1", status.AMBIGUOUS_BOTH_GRAINS
    return a, b, None, status.MANY_TO_MANY_RISK          # neither: NEVER default a cardinality


def score(
    pair: CandidatePair, *, source_snapshot_id: str, cfg: PassCConfig = DEFAULT_CONFIG,
) -> JoinCandidateEvidenceV1:
    """Score one blocked pair into self-explaining candidate evidence (pure, deterministic)."""
    a, b = pair.a, pair.b
    signals = _positive_signals(pair, cfg)
    total = sum(s.score_delta for s in signals)

    frm, to, cardinality, status = _orient(pair)
    from_ref, to_ref = frm.object_ref, to.object_ref
    column_pairs = ((frm.column, to.column),)
    inferred = status is CardinalityInferenceStatus.INFERRED_FROM_CONFIRMED_GRAIN
    direction = f"{from_ref} -> {to_ref}" if inferred else None

    missing: tuple[str, ...] = ()
    demotion = ""
    if not inferred:                                     # rule 1: forced weak, regardless of score
        bucket = "weak"
        missing = (f"grain:{a.object_ref}", f"grain:{b.object_ref}")
        demotion = (" Forced weak: only a one-side-confirmed-grain candidate has an inferable N:1"
                    " — a 1:1 or many-to-many join is never auto-proposed.")
    else:
        bucket = ("strong" if total >= cfg.strong_threshold
                  else "weak" if total >= cfg.weak_threshold else "suppressed")
        related_fired = any(s.signal_name == "related_terms_key_link" for s in signals)
        if (bucket == "strong" and pair.namespace is NamespaceCompatibility.POSSIBLE
                and not related_fired):                  # rule 2: POSSIBLE caps at weak
            bucket = "weak"
            demotion = " Capped at weak: namespace only POSSIBLE without a related-terms key link."

    if inferred:
        base = (f"Proposed {cardinality} join {frm.table}.{frm.column} -> {to.table}.{to.column}:"
                f" {to.table}.{to.column} is a confirmed grain.")
    elif status is CardinalityInferenceStatus.AMBIGUOUS_BOTH_GRAINS:
        base = (f"Both {a.table}.{a.column} and {b.table}.{b.column} are confirmed grains —"
                " a 1:1 equivalence is not assumed.")
    else:
        base = (f"Neither {a.table}.{a.column} nor {b.table}.{b.column} is a confirmed grain —"
                " many-to-many risk, no cardinality proposed.")
    signal_txt = ", ".join(f"{s.signal_name}(+{s.score_delta})" for s in signals) or "none"
    explanation = (f"{base} Signals: {signal_txt} = {total}"
                   f" ({bucket}; namespace {pair.namespace.value}).{demotion}")

    pairs_txt = ",".join(f"{f}->{t}" for f, t in column_pairs)
    candidate_id = hashlib.sha256(
        f"{from_ref}|{to_ref}|{pairs_txt}|{ALGORITHM_VERSION}".encode()).hexdigest()[:16]

    return JoinCandidateEvidenceV1(
        candidate_id=candidate_id, from_ref=from_ref, to_ref=to_ref, column_pairs=column_pairs,
        proposed_direction=direction, proposed_cardinality=cardinality, cardinality_status=status,
        bucket=bucket, score=total, positive_signals=signals, negative_signals=(),
        namespace_compatibility=pair.namespace, namespace_reason_codes=pair.namespace_reasons,
        grain_evidence=tuple(c.object_ref for c in (a, b) if c.is_grain),
        missing_requirements=missing, llm_annotations=(), explanation=explanation,
        producer="deterministic_pass_c", config_version=CONFIG_VERSION,
        candidate_algorithm_version=ALGORITHM_VERSION, source_snapshot_id=source_snapshot_id)
