"""BR-6 increment 1 — Formula-v2 canonicalization, its own pin, v1's untouched.

Same discipline as v1 (RFC 8785 JCS over a deterministic plain projection; sha256 of exactly
those bytes), separate machinery: v1's serializer walks v1 types and this one walks v2 types, so
neither can move the other's identity. The version triple is INSIDE the hashed body (pinned to 2
by validation), which is what makes a v1 and a v2 canonical form structurally non-colliding —
no wrapper key needed, exactly as v1 does it.

Field-exhaustive by construction: the projection is ``dataclasses.fields``-driven, so a field
added to any v2 type is hash-bearing automatically — the same guarantee canonical-recipe-v2 gives
on the recipe side, proven by the exhaustiveness test.
"""
from __future__ import annotations

import hashlib
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any

from featuregen.formula._jcs import dumps as _jcs_dumps
from featuregen.formula.schema_v2 import TypedFormulaProposalV2


def _plain_v2(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain_v2(getattr(value, f.name))
                for f in sorted(fields(value), key=lambda f: f.name)}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_plain_v2(item) for item in value]
    return value


def canonical_json_v2(proposal: TypedFormulaProposalV2) -> str:
    """The canonical JSON text of a v2 proposal (decoded JCS bytes)."""
    return _canonical_bytes_v2(proposal).decode("utf-8")


def proposal_content_hash_v2(proposal: TypedFormulaProposalV2) -> str:
    """``sha256(canonical_json_v2(proposal))`` — the v2 proposal's content identity."""
    return hashlib.sha256(_canonical_bytes_v2(proposal)).hexdigest()


def _canonical_bytes_v2(proposal: TypedFormulaProposalV2) -> bytes:
    return _jcs_dumps(_plain_v2(proposal))
