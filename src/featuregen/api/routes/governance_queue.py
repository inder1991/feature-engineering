"""``GET /governance/queue`` — every pending governance decision, every catalog, one request.

Every other governance route is ``/sources/{source}/governance/…``, so the Governance Review screen
opened on an empty text input and rendered nothing until the operator guessed a slug. ``GET
/catalogs`` gave the screen the slugs; this route gives it the WORK, so it can open on the queue
instead of on a question.

Thin HTTP wiring over :func:`~featuregen.overlay.upload.governance_queue.governance_queue`, which
owns the merge, the scoping, the vocabulary and the usage measurability — see that module for every
rule. Two things this route is responsible for and the read model is not:

* **The gate.** ``require_confirmer`` — the raw ``platform-admin`` claim, the SAME gate as every
  listing this queue merges (``/sources/{source}/governance/joins``, ``…/table-facts``,
  ``…/entity-bridges``). A merged view may not be reachable by a caller who could not reach its
  parts.
* **The scope.** ``roles`` come from the authenticated session's ``role_claims``, NEVER from the
  request, so this surface cannot be asked for someone else's scope. Passing ``identity`` as
  ``actor`` additionally makes ``available_actions`` a SERVER decision (four-eyes: a proposer is not
  offered ``confirm`` on their own fact).

The upload-context catalog adapter is self-ensured, exactly as its sibling handlers do: an API-only
process registers no adapter at startup (the worker does), and the listings' authority resolution
fails closed without one.

NEVER 404 and never an error for "nothing to review": an empty ``items`` with ``complete: true`` is
the honest answer for both "no decisions are waiting" and "none that you may see", so the response
cannot be used to probe for hidden catalogs. ``complete: false`` with a populated ``unreadable``
means something could not be READ — a different answer, deliberately distinguishable.
"""
from __future__ import annotations

from dataclasses import asdict
from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, Query

from featuregen.api.deps import get_conn, get_identity, require_confirmer
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.governance_queue import governance_queue
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

router = APIRouter()
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]


@router.get("/governance/queue", dependencies=[Depends(require_confirmer)])
def get_governance_queue(conn: _Conn, identity: _Identity,
                         limit: int = Query(default=100, ge=1, le=500)) -> dict:
    """Every pending decision this caller may see, across every catalog they may see.

    ``items`` are merged from the three governance listings (entity bridges, discovered joins,
    grain/availability table facts) and grouped by kind. Each item carries the sanctioned ``state``
    label, the INDEPENDENT ``production_eligibility`` axis, the server's ``available_actions``, and —
    for a bridge — ``already_depended_on_by``, whose per-category ``state`` is ``counted`` /
    ``not_tracked_yet`` / ``unreadable``. A category that has recorded nothing reports
    ``not tracked yet``; it never reports 0.

    ``items_visible_to_you_by_catalog`` is SCOPE-RELATIVE: two operators legitimately see different
    numbers for the same catalog, and ``counts_are_scope_relative`` says so in the payload.
    """
    ensure_upload_catalog_adapter()
    queue = governance_queue(conn, roles=identity.role_claims, actor=identity, limit=limit)
    body = asdict(queue)
    # Tuples serialize as JSON arrays; the two per-key count maps read better as objects, and the
    # names carry the scope-relative meaning so neither can be mistaken for a catalog total.
    body["items_visible_to_you_by_catalog"] = dict(queue.items_visible_to_you_by_catalog)
    body["items_visible_to_you_by_kind"] = dict(queue.items_visible_to_you_by_kind)
    body["next_cursor"] = None
    return body
