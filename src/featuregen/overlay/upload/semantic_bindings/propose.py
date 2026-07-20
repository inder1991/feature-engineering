"""D2 — map a confirmed-strong candidate onto E1's governed DRAFT fact command.

``propose`` takes a persisted ``strong`` candidate, builds E1's ``propose_fact`` command, dispatches
it, and — ONLY after that DRAFT proposal succeeds — writes the ``semantic_binding_candidate_proposal``
LINK. It NEVER confirms: ``propose_fact`` appends an ``OVERLAY_FACT_PROPOSED`` (a DRAFT) and opens
the human gate — this module never calls ``confirm_fact`` / ``enter_fact``, so it can NEVER create a
VERIFIED fact. The four-eyes confirm is a separate human step (E2).

Mapping (the brief's contract):
* currency → ``currency_binding`` with ``{"currency_column": <target CatalogObjectRef>}``.
* entity   → ``entity_assignment`` with ``{"entity_id": <known-entity value>}``.
The subject column becomes the fact ``ref`` (a ``CatalogObjectRef``); E1's write gate re-checks
column-ness, ``known_entities()`` membership, and same-table currency targeting.
"""
from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.proposal_commands import propose_fact as _propose_fact
from featuregen.overlay.upload.semantic_bindings.store import link_proposal
from featuregen.overlay.upload.semantic_bindings.types import (
    CURRENCY_BINDING,
    ENTITY_ASSIGNMENT,
    STRONG,
    SemanticBindingCandidate,
)

# Only a ``strong`` (or a human-confirmed) candidate is proposed — a ``weak``/``rejected`` candidate
# is a review artefact, never auto-proposed into a governed fact.
PROPOSABLE_DISPOSITIONS = frozenset({STRONG})


@dataclass(frozen=True, slots=True)
class ProposeOutcome:
    accepted: bool
    fact_key: str | None
    proposed_event_id: str | None
    linked: bool
    denied_reason: str | None = None


def _subject_ref(candidate: SemanticBindingCandidate) -> CatalogObjectRef:
    s = candidate.subject
    return CatalogObjectRef(catalog_source=s.catalog_source, object_kind="column",
                            schema=s.schema, table=s.table, column=s.column)


def to_fact_command(
    candidate: SemanticBindingCandidate, *, actor: IdentityEnvelope, idempotency_key: str,
) -> Command:
    """PURE map: a candidate → E1's ``propose_fact`` command (a DRAFT proposal, server-minted, NEVER
    verified). Raises ``ValueError`` on a mis-shaped candidate (currency without a target / entity
    without a value) — the same fail-closed shape D1's store enforces."""
    ref = _subject_ref(candidate)
    if candidate.binding_kind == CURRENCY_BINDING:
        if candidate.target is None:
            raise ValueError("currency_binding candidate has no target currency column")
        t = candidate.target
        value: dict[str, object] = {"currency_column": {
            "catalog_source": t.catalog_source, "object_kind": "column",
            "schema": t.schema, "table": t.table, "column": t.column}}
        fact_type = CURRENCY_BINDING
    elif candidate.binding_kind == ENTITY_ASSIGNMENT:
        if not candidate.entity_id:
            raise ValueError("entity_assignment candidate has no entity_id")
        value = {"entity_id": candidate.entity_id}
        fact_type = ENTITY_ASSIGNMENT
    else:
        raise ValueError(f"unknown binding_kind {candidate.binding_kind!r}")
    return Command(
        action="propose_fact", aggregate="overlay_fact", aggregate_id=None,
        args={"ref": ref, "fact_type": fact_type, "proposed_value": value},
        actor=actor, idempotency_key=idempotency_key)


def propose(
    conn: DbConn,
    candidate: SemanticBindingCandidate,
    *,
    candidate_id: str,
    actor: IdentityEnvelope,
    idempotency_key: str,
    propose_fact: Callable[[DbConn, Command], CommandResult] = _propose_fact,
) -> ProposeOutcome:
    """Propose a ``strong`` candidate as an E1 DRAFT fact, then link it. Order matters: the
    ``semantic_binding_candidate_proposal`` LINK is written ONLY after ``propose_fact`` returns
    accepted (a denied proposal never orphans a link). NEVER verifies — the fact is left DRAFT for
    the human four-eyes gate. ``candidate_id`` is the D1 id of the ALREADY-PERSISTED candidate row
    (the link FKs to it) — see ``store.candidate_id_for``."""
    if candidate.disposition not in PROPOSABLE_DISPOSITIONS:
        return ProposeOutcome(
            accepted=False, fact_key=None, proposed_event_id=None, linked=False,
            denied_reason=f"not proposable (disposition={candidate.disposition!r}; "
                          "only strong/confirmed candidates are proposed)")
    command = to_fact_command(candidate, actor=actor, idempotency_key=idempotency_key)
    result = propose_fact(conn, command)
    if not result.accepted:
        return ProposeOutcome(accepted=False, fact_key=result.aggregate_id or None,
                              proposed_event_id=None, linked=False,
                              denied_reason=result.denied_reason)
    proposed_event_id = result.produced_event_ids[0]
    link_proposal(conn, candidate_id=candidate_id, fact_key=result.aggregate_id,
                  proposed_event_id=proposed_event_id)
    return ProposeOutcome(accepted=True, fact_key=result.aggregate_id,
                          proposed_event_id=proposed_event_id, linked=True)
