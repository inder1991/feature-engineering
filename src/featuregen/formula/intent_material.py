"""The intent projection every authoring generation hashes — shared, and version-neutral.

Five fields off an authoring intent, projected to a dict. It says nothing about which formula
language will be authored from that intent, and both generations hash exactly this into their run
manifests — which is precisely why `materialize.authoring_trace` had to reach into a module named
`replay_authoring` (v1) to re-derive a trace for a v2 run.

**The five fields are the contract, not an implementation detail.** `authoring_trace` recomputes
`canonical_hash(_intent_material(intent))` and compares it against what the run recorded, so adding
or reordering a field here changes an identity that durable rows were sealed under. It is extracted
verbatim for that reason: a move that also tidied would be a re-identification wearing a
refactor's commit message.
"""
from __future__ import annotations

__all__ = ["_intent_material"]


def _intent_material(intent) -> dict:
    return {
        "name": intent.name,
        "hypothesis": intent.hypothesis,
        "target_entity": intent.target_entity,
        "target_grain_keys": list(intent.target_grain_keys),
        "recipe_authoring_context": getattr(intent, "recipe_authoring_context", None),
    }
