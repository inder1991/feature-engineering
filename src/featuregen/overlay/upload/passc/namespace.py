"""Pass C — namespace-compatibility classifier (pure: no DB, no LLM).

The safety gate that stops "same-looking id, different namespace" bad joins. Two rules a
two-round review baked in:

- Key on **column_entity**, never table_entity. `transactions.customer_id → customer.customer_id`
  crosses table entities but shares the "customer" column namespace — keying on table_entity would
  suppress the most common legitimate join. Only a *different column entity* is INCOMPATIBLE.
- Do NOT over-suppress. COMPATIBLE only makes a candidate *proposable* — the dual-human confirm
  gate is the real namespace net. Same concept needs one corroborator (same canonical column name
  or real synonyms) for COMPATIBLE; without one it is POSSIBLE, a deliberately reachable tier.
"""
from __future__ import annotations

from featuregen.overlay.upload.passc.identifiers import (
    ColMeta, _canon, _synonym_canons, normalized_identifier_concept)
from featuregen.overlay.upload.passc.types import (
    DEFAULT_CONFIG, NamespaceCompatibility, PassCConfig)


def classify_namespace(
    a: ColMeta, b: ColMeta, cfg: PassCConfig = DEFAULT_CONFIG,
) -> tuple[NamespaceCompatibility, tuple[str, ...]]:
    """Classify whether two identifier columns live in the same join namespace.

    Returns the verdict plus auditable reason codes (why, not just what)."""
    N = NamespaceCompatibility
    ea, eb = (a.column_entity or "").strip().lower(), (b.column_entity or "").strip().lower()
    if ea and eb:
        if ea == eb:
            return N.COMPATIBLE, ("same_column_entity",)
        return N.INCOMPATIBLE, ("different_column_entity",)

    ca, cb = normalized_identifier_concept(a), normalized_identifier_concept(b)
    if ca and cb and ca == cb:
        reasons = ["same_identifier_concept"]
        # Non-empty guard (mirrors candidates._positive_signals): generic names like "id"/"key"
        # both _canon to "" — a shared-EMPTY name is no corroboration, and treating "" == "" as
        # same_column_name would upgrade POSSIBLE -> COMPATIBLE, defeating the weak cap.
        ca_name = _canon(a.column)
        same_name = bool(ca_name) and ca_name == _canon(b.column)
        # _synonym_canons, not raw truthiness: "(blank)"/"n/a" placeholders are not corroboration.
        if same_name or _synonym_canons(a.synonyms) or _synonym_canons(b.synonyms):
            reasons.append("same_column_name" if same_name else "synonym_corroboration")
            return N.COMPATIBLE, tuple(reasons)
        return N.POSSIBLE, tuple(reasons)   # same concept, different name, no synonyms — reachable

    la, lb = (a.bian_leaf or "").strip().lower(), (b.bian_leaf or "").strip().lower()
    if la and la == lb:
        if la in cfg.mixed_bian_leaves:     # a leaf that mixes entity namespaces proves nothing
            return N.AMBIGUOUS, ("mixed_bian_leaf",)
        return N.AMBIGUOUS, ("same_bian_leaf_only",)
    return N.AMBIGUOUS, ("generic_reference_without_context",)
