"""Object-level run visibility (spec §11). Applied INSIDE queries, before pagination and counts —
a count over rows the caller may not see leaks the shape of other people's work.

Aliases are fixed by contract: every consumer query aliases feature_run_identity AS fri and
feature_generation_run AS fgr."""
from __future__ import annotations

from featuregen.contracts.envelopes import IdentityEnvelope


def is_platform_admin(identity: IdentityEnvelope) -> bool:
    """The FUNCTIONAL role bundle name from `identity/permissions.py` (underscore).

    Not to be confused with the raw `platform-admin` (hyphen) role CLAIM the governance-confirmer
    gate in `api/deps.py` checks: the two spellings are different authorities, and this policy
    keys on the functional bundle."""
    return "platform_admin" in identity.role_claims


def visibility_where(identity: IdentityEnvelope) -> tuple[str, list]:
    """The SQL fragment (and its params) restricting a run query to what `identity` may see.

    The fragment is a standalone boolean expression the caller ANDs into its WHERE clause. Its
    params bind positionally AT THE POINT THE FRAGMENT IS SPLICED into the SQL — the caller appends
    them in splice order, wherever that is: `WHERE {frag} AND ...` binds `(*params, ...)`, while
    `WHERE fgr.generation_run_id = %s AND {frag}` binds `(run_id, *params)`. Getting that order
    wrong does not raise; it silently matches nothing.

    Invariant: if this fragment ever becomes compound, parenthesize it. Comparison binds tighter
    than OR/NOT, so today's single equality needs none — the invariant, not the punctuation, is
    the durable protection."""
    if is_platform_admin(identity):
        return "TRUE", []
    # ONE comparison: a V1 run's owner is the immutable identity row; a pre-spine run falls back
    # to the mutable actor subject (spec §11 [R3.1]) — and a pre-spine run with no subject
    # compares NULL = <caller>, which is never true, so it is admin-only by construction.
    #
    # The comparison is against whatever subject the caller's envelope carries; keeping that
    # shape aligned with what the two owner sources store is the calling query's concern.
    return ("COALESCE(fri.owner_subject, fgr.actor->>'subject') = %s", [identity.subject])
