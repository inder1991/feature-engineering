"""Per-fact authority resolution + separation-of-duties helpers (SP-1 design §6).

The engine/profiler *proposes*; a **human** authority *confirms*. This module answers two
questions for the command handlers:

* WHO may confirm a given fact — `resolve_authority(...)` maps a fact to its `Authority`
  (data owner / Compliance / governance queue), resolved **per side** for `approved_join`;
  `_actor_is_authority(...)` then checks a concrete actor against it
  (accepting the `platform-admin` role for governance-queue tasks).
* Four-eyes — `proposer_ne_confirmer(...)` blocks a confirmer who is the recorded proposer.

Unknown ownership NEVER falls back to the request submitter; it routes to the
platform-admin / data-governance queue (§6 step 1).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.contracts.identity import IdentityEnvelope
from featuregen.overlay._types import EligibleAssignee, FactType, Gate, JoinSide, Role
from featuregen.overlay.catalog import CatalogAdapter
from featuregen.overlay.identity import ApprovedJoinRef, CatalogObjectRef, EntityBridgeRef


@dataclass(frozen=True, slots=True)
class Authority:
    """Resolved authority for a fact.

    `subjects` holds the resolved owner subject(s) — two ordered entries (`from_ref`, `to_ref`)
    for an `approved_join`, with `None` in a slot whose owner is unknown. `governance_queue` is
    True when ANY required owner is unknown: that side's task routes to the
    platform-admin/data-governance queue, NEVER to whoever submitted the request (§6 step 1).
    `dual` is True when two DISTINCT confirmations are required — an `approved_join` whose two
    sides do not share a single owner (two known owners, one known + one governance side, OR
    even both-unknown → two distinct governance approvals; §6.4).

    `admin_confirmable` is True for the Delivery E governed semantic facts (entity_assignment /
    currency_binding): even when the source owner IS known, a platform admin is ALSO an accepted
    confirmer (owner-or-admin, E1 authz). It defaults False, so every other type keeps the strict
    owner-only rule when the owner is known."""

    role: Role
    gate: Gate
    subjects: tuple[str | None, ...]
    governance_queue: bool
    dual: bool = False
    admin_confirmable: bool = False

    @property
    def eligible_assignees(self) -> dict[str, str]:
        """Coarse, single-task assignee descriptor (the SP-0 gate `eligible_assignees`)."""
        a: dict[str, str] = {"role": self.role}
        known = [s for s in self.subjects if s]
        if known:
            a["subject"] = known[0]
        return a

    @property
    def task_assignees(self) -> tuple[EligibleAssignee, ...]:
        """Per-side task plan — one assignee mapping per required confirmation, **side-labelled**.

        A known side → `{"role": "data_owner", "subject": <owner>, "side": <from|to>}`; an unknown
        side → `{"role": "platform-admin", "side": <from|to>}` (governance). The known owner is
        NEVER folded onto the governance side. The two sides are **never collapsed** —
        even a both-unknown join opens TWO side-labelled governance tasks, so two distinct approvals
        are required. The ONLY single-task case is same-owner-both-sides.

        This single per-side plan is the authoritative source used by BOTH the initial proposal
        (one task per entry) AND `open_reverify_task` (which reopens one re-verify task per entry) —
        so proposal and re-verify always target the same per-side assignees."""
        if self.role == "compliance":
            return ({"role": "compliance"},)
        if len(self.subjects) == 2:  # approved_join: one task PER SIDE
            from_owner, to_owner = self.subjects
            if from_owner is not None and from_owner == to_owner:
                # same principal owns BOTH sides — a single task (the only collapse case)
                return ({"role": "data_owner", "subject": from_owner},)
            plans: list[EligibleAssignee] = []
            sides: tuple[tuple[JoinSide, str | None], ...] = (
                ("from", from_owner),
                ("to", to_owner),
            )
            for side, owner in sides:
                if owner:
                    plans.append({"role": "data_owner", "subject": owner, "side": side})
                else:
                    plans.append({"role": "platform-admin", "side": side})
            return tuple(plans)
        known = [s for s in self.subjects if s]
        if known:
            return ({"role": "data_owner", "subject": known[0]},)
        return ({"role": "platform-admin"},)


def resolve_authority(
    conn: DbConn,
    adapter: CatalogAdapter,
    ref: CatalogObjectRef | ApprovedJoinRef,
    fact_type: FactType,
) -> Authority:
    # conn is part of the stable contract (owner overrides / governance config may be stored
    # in future); today authority is derived purely from the catalog adapter.
    del conn
    if fact_type == "policy_tag":
        return Authority(
            role="compliance", gate="OVERLAY_COMPLIANCE", subjects=(), governance_queue=False
        )
    if fact_type == "approved_join":
        if not isinstance(ref, ApprovedJoinRef):
            raise TypeError(
                f"approved_join authority requires an ApprovedJoinRef, "
                f"got {type(ref).__name__}"
            )
        from_owner = adapter.owner_of(ref.from_ref)
        to_owner = adapter.owner_of(ref.to_ref)
        unknown = from_owner is None or to_owner is None
        # One distinct confirmation per resolved side; an unknown side resolves to the governance
        # (platform-admin) queue, NEVER to the other known owner. A join needs TWO
        # distinct confirmations unless ONE principal owns BOTH sides — so both-unknown is still
        # dual (two distinct governance approvals), preserving two-party accountability.
        same_owner = from_owner is not None and from_owner == to_owner
        return Authority(
            role=("data_owner" if (from_owner or to_owner) else "platform-admin"),
            gate="OVERLAY_DATA_OWNER",
            subjects=(from_owner, to_owner),
            governance_queue=unknown,
            dual=not same_owner,
        )
    if fact_type == "entity_bridge":
        if not isinstance(ref, EntityBridgeRef):
            raise TypeError(
                f"entity_bridge authority requires an EntityBridgeRef, got {type(ref).__name__}")
        # 3B.2B shadow: a SINGLE governance confirmation (four-eyes: proposer != confirmer). Two-owner
        # dual sign-off is deferred to 3C (when a bridge becomes live-traversable). owner_of is consulted
        # only to collapse onto a shared owner when both catalogs happen to share one.
        left_owner = adapter.owner_of(ref.left_ref)
        right_owner = adapter.owner_of(ref.right_ref)
        if left_owner is not None and left_owner == right_owner:
            return Authority(role="data_owner", gate="OVERLAY_DATA_OWNER",
                             subjects=(left_owner,), governance_queue=False)
        return Authority(role="platform-admin", gate="OVERLAY_DATA_OWNER",
                         subjects=(), governance_queue=True)
    if fact_type in ("entity_assignment", "currency_binding"):
        # Delivery E governed semantic facts (E1 authz): ONE authorized confirmer constrained to the
        # owned table — the registered SOURCE OWNER *or* a platform admin. Like the generic single-
        # owner path below, but `admin_confirmable=True` so `_actor_is_authority` accepts a platform
        # admin ALONGSIDE a KNOWN owner (the generic path accepts only the owner). Four-eyes
        # (proposer != confirmer) is enforced separately in confirm_fact, so owner-or-admin never
        # permits one principal to both propose AND approve the same value.
        if not isinstance(ref, CatalogObjectRef):
            raise TypeError(
                f"{fact_type!r} authority requires a CatalogObjectRef, got {type(ref).__name__}"
            )
        owner = adapter.owner_of(ref)
        if owner is None:
            return Authority(
                role="platform-admin", gate="OVERLAY_DATA_OWNER", subjects=(),
                governance_queue=True,
            )
        return Authority(
            role="data_owner", gate="OVERLAY_DATA_OWNER", subjects=(owner,),
            governance_queue=False, admin_confirmable=True,
        )
    if not isinstance(ref, CatalogObjectRef):
        raise TypeError(
            f"{fact_type!r} authority requires a CatalogObjectRef, got {type(ref).__name__}"
        )
    owner = adapter.owner_of(ref)
    if owner is None:
        return Authority(
            role="platform-admin", gate="OVERLAY_DATA_OWNER", subjects=(), governance_queue=True
        )
    return Authority(
        role="data_owner", gate="OVERLAY_DATA_OWNER", subjects=(owner,), governance_queue=False
    )


def _actor_is_authority(authority: Authority, actor: IdentityEnvelope) -> bool:
    """True when `actor` is a valid confirming authority for `authority` (§6 fine-grained authz).

    * compliance fact → actor must hold the `compliance` role claim.
    * data-owner fact → actor must BE one of the resolved owner subjects (owner-of-object).
    * owner-or-admin fact (`admin_confirmable`, the Delivery E semantic facts) → the known owner OR
      a `platform-admin` role claim confirms (E1); four-eyes is enforced separately in confirm_fact.
    * governance-queue fact (an unknown owner) → actor must hold the `platform-admin` role claim
      (there is no specific owner subject to match against).

    For an `approved_join` with one known + one governance side, EITHER the known owner OR a
    platform-admin is an authority; the per-side binding (which actor confirms which side) is
    enforced by the per-side task plan, not by this coarse predicate."""
    roles = set(actor.role_claims)
    if authority.role == "compliance":
        return "compliance" in roles
    # Delivery E owner-or-admin (E1): a platform admin is an accepted confirmer for these types even
    # when the owner is known. `admin_confirmable` is False for every other type, so this never
    # widens the owner-only rule elsewhere.
    if authority.admin_confirmable and "platform-admin" in roles:
        return True
    if authority.governance_queue and "platform-admin" in roles:
        return True
    known = {s for s in authority.subjects if s}
    return actor.subject in known


def proposer_ne_confirmer(stream: Sequence, actor: IdentityEnvelope) -> bool:
    """Four-eyes SoD predicate (§6.5): True when the confirmer differs from the recorded
    proposer. `proposed_by` is a string subject; a service/profiler proposal is
    trivially distinct from a human confirmer."""
    for e in reversed(list(stream)):
        if e.type == "OVERLAY_FACT_PROPOSED":
            proposed_by = e.payload.get("proposed_by")
            return proposed_by != actor.subject
    return True
