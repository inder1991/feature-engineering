"""Closed table-synthesis vocabularies + normalizers (Phase-2 Slice 2).

The `table_role` vocabulary is enforced CODE-SIDE in `table_synth.make_ref_accept` (an off-vocab
role drops THAT FIELD ONLY) and enumerated in the Pass B PROMPT — deliberately NOT as an enum on
the canonical response schema. The driver validates the whole response with
`reg.validate(schema_id, version, output)`, so a strict schema enum would fail the ENTIRE
synthesis on one off-vocab role (losing a valid grain with it), destroying per-field salvage
([F1]). The canonical v2 schema keeps `table_role` a bounded string.

All normalizers are `strip().lower()`-based; a value that does not normalize into the vocabulary
resolves to ``None`` (the caller records the per-field disposition).
"""
from __future__ import annotations

# Bound on the number of grain columns a synthesis may claim (a "grain" wider than this is a
# hallucination or a mis-modelled table, never a reviewable proposal).
MAX_GRAIN_COLS = 16

# The raw spellings the PROMPT enumerates (accepted pre-normalization). Order is prompt-facing.
TABLE_ROLE_ENUM = ["fact", "dim", "reference", "event_fact", "snapshot_fact", "dimension",
                   "bridge"]

# The canonical post-normalization vocabulary. "fact" is RETAINED as a legacy canonical role: a
# fact table with no event/snapshot signal cannot be split into event_fact/snapshot_fact.
CANONICAL_TABLE_ROLES = frozenset({"event_fact", "snapshot_fact", "dimension", "reference",
                                   "bridge", "fact"})

_ROLE_ALIASES = {"dim": "dimension"}

_EVENT_OR_SNAPSHOT = ("event", "snapshot")


def normalize_event_or_snapshot(raw: str | None) -> str | None:
    """``strip().lower()``-normalize an event/snapshot signal; off-vocab (or non-string) -> None."""
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    return v if v in _EVENT_OR_SNAPSHOT else None


def normalize_table_role(raw: str | None, *, event_or_snapshot: str | None) -> str | None:
    """Normalize a raw table-role into ``CANONICAL_TABLE_ROLES`` (``None`` == off-vocab).

    Order: strip/lower -> ``"fact"`` splits to ``event_fact``/``snapshot_fact`` via the
    ``event_or_snapshot`` signal (retained as legacy ``"fact"`` without one) -> alias map
    (``dim`` -> ``dimension``) -> canonical membership -> ``None``.
    """
    if not isinstance(raw, str):
        return None
    v = raw.strip().lower()
    if v == "fact":
        return {"event": "event_fact", "snapshot": "snapshot_fact"}.get(
            normalize_event_or_snapshot(event_or_snapshot), "fact")
    if v in _ROLE_ALIASES:
        return _ROLE_ALIASES[v]
    if v in CANONICAL_TABLE_ROLES:
        return v
    return None
