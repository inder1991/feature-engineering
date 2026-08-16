"""C-A2 — Formula-**v3** canonicalization, its own pin; v1's and v2's untouched.

Same discipline as v1 and v2: RFC 8785 JCS over a deterministic, ``dataclasses.fields``-driven plain
projection, then sha256 of exactly those bytes. The version triple is INSIDE the hashed body (pinned
to 3 by validation), which is what makes a v1, v2 and v3 canonical form structurally
non-colliding — no wrapper key needed, exactly as v1 and v2 do it.

**The projection function is v2's, deliberately reused.** ``_plain_v2`` is a generic walker over
dataclasses, enums and tuples — it carries no v2 grammar in it. Copying it to walk v3 types would
create a second definition of "what a canonical projection is", free to drift from the one
``test_canonical_v2::test_the_projection_is_field_exhaustive`` exercises, and the two would then
disagree about the shared leaf types v3 imports unchanged (``WindowPolicyV2``, ``AuthorityRefsV2``).
One walker, three entry points, three independently-pinned hashes.

**Field-exhaustive by construction:** a field added to any v3 type is hash-bearing automatically.
That is the property that made adding a field to a V2 dataclass unacceptable, and it is equally
load-bearing here — v3's own exhaustiveness test pins it.
"""
from __future__ import annotations

import hashlib

from featuregen.formula._jcs import dumps as _jcs_dumps
from featuregen.formula.canonical_v2 import _plain_v2
from featuregen.formula.schema_v3 import TypedFormulaProposalV3

__all__ = ["canonical_json_v3", "proposal_content_hash_v3"]


def canonical_json_v3(proposal: TypedFormulaProposalV3) -> str:
    """The canonical JSON text of a v3 proposal (decoded JCS bytes)."""
    return _canonical_bytes_v3(proposal).decode("utf-8")


def proposal_content_hash_v3(proposal: TypedFormulaProposalV3) -> str:
    """``sha256(canonical_json_v3(proposal))`` — the v3 proposal's content identity."""
    return hashlib.sha256(_canonical_bytes_v3(proposal)).hexdigest()


def _canonical_bytes_v3(proposal: TypedFormulaProposalV3) -> bytes:
    return _jcs_dumps(_plain_v2(proposal))
