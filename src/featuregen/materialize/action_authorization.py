"""ONE authorization model for the six actions — and the development policy that answers today.

**What an authorization is, and what it is not.** A PERMISSION answers *"may this person call this
endpoint"*. An AUTHORIZATION answers *"was THIS act, on THIS resource, decided — by whom, under
which policy"*. Only the second survives the request: a worker picking the job up minutes later can
re-check an authorization and cannot re-check a permission, because the caller is long gone.

**THE DEVELOPMENT POLICY** (owner ruling, 2026-08-22). While the tool is under development, any
authenticated user may trigger any implemented NON-PRODUCTION stage, and the server records who.
Segregation of duties, delegation and per-environment entitlement are premature complexity; they
become release-readiness requirements rather than blockers.

▲ **What is deliberately NOT relaxed** — and this is the distinction the whole module turns on:

* roles are never accepted from a request body; ``actor_subject`` comes from the authenticated
  identity;
* a client cannot present an authorization the server did not issue to it;
* production actions are **UNAVAILABLE**, not merely gated — an act that cannot be authorized cannot
  be reached by a bypass either;
* every row stamps ``policy_version``, so *"what was approved under the permissive rules?"* is one
  query on the day those rules change.

> *"'Allow everyone in development' must be an EXPLICIT SERVER POLICY — not permission inferred from
> client-supplied roles."*

The permissive half is temporary. The server-owned half is the production path, and it is built
correctly now precisely so that tightening the policy later is a change of VALUES rather than a
change of ARCHITECTURE.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from featuregen.canonical import jcs_sha256

__all__ = [
    "DEVELOPMENT_POLICY_VERSION",
    "ActionUnavailable",
    "ActionV1",
    "ActionAuthorizationV1",
    "action_available",
    "authorize_action",
    "load_action_authorization",
]

#: ▲ Bump this when the POLICY changes, never when its implementation moves. It is the audit key for
#: "issued under rules we no longer use", and a version that tracked refactors could not answer that.
DEVELOPMENT_POLICY_VERSION = "development-v1"


class ActionV1(StrEnum):
    """The SIX acts. ▲ Deliberately a different vocabulary from ``EvaluatorAction``, which names the
    three a chain gates — widening one into the other silently is how a preview decision comes to
    authorize an execution."""

    AUTHOR_FORMULA = "AUTHOR_FORMULA"
    GENERATE_PREVIEW = "GENERATE_PREVIEW"
    EXECUTE_SANDBOX = "EXECUTE_SANDBOX"
    PUBLISH_SANDBOX = "PUBLISH_SANDBOX"
    MATERIALIZE_PRODUCTION = "MATERIALIZE_PRODUCTION"
    PUBLISH_PRODUCTION = "PUBLISH_PRODUCTION"


#: ▲ The two production acts are UNAVAILABLE, not "blocked pending a certificate". The difference
#: matters: a gated action still has a code path that a bypass could reach, and an unavailable one
#: has none. They become available when production governance exists (§21.0), which is a release
#: gate rather than a switch somebody can flip.
_PRODUCTION_ACTIONS = frozenset({
    ActionV1.MATERIALIZE_PRODUCTION,
    ActionV1.PUBLISH_PRODUCTION,
})


class ActionUnavailable(RuntimeError):
    """This action cannot be authorized in this deployment at all."""


def action_available(action: ActionV1) -> bool:
    """Whether the development policy can authorize this act.

    ``EXECUTE_SANDBOX`` and ``PUBLISH_SANDBOX`` are policy-available. Whether they can actually RUN
    is a separate question owned by the lane that performs them — the policy's job is to say who may
    ask, not to assert that a worker exists. Conflating the two would have this module reporting on
    machinery it does not own.
    """
    return action not in _PRODUCTION_ACTIONS


@dataclass(frozen=True, slots=True)
class ActionAuthorizationV1:
    """One authorized act, content-addressed.

    ``decided_at`` is NOT here: provenance stays outside the identity (1099's rule), so the same act
    decided twice under the same policy is one authorization rather than two that could diverge.
    """

    action: ActionV1
    resource_identity_hash: str
    actor_subject: str
    environment_id: str
    policy_version: str = DEVELOPMENT_POLICY_VERSION
    permission_result: str = "allowed"

    @property
    def evidence_hash(self) -> str:
        """WHY the answer was what it was — the policy inputs, hashed.

        Separate from ``authorization_id`` so a worker can ask "would this policy still decide the
        same way?" without re-deriving the whole identity.
        """
        return jcs_sha256({
            "policy_version": self.policy_version,
            "action": str(self.action),
            "production_action": self.action in _PRODUCTION_ACTIONS,
            "permission_result": self.permission_result,
        })

    @property
    def authorization_id(self) -> str:
        return jcs_sha256({
            "action": str(self.action),
            "resource_identity_hash": self.resource_identity_hash,
            "actor_subject": self.actor_subject,
            "environment_id": self.environment_id,
            "policy_version": self.policy_version,
            "permission_result": self.permission_result,
            "evidence_hash": self.evidence_hash,
        })


def authorize_action(
    conn, *, action: ActionV1, resource_identity_hash: str, actor_subject: str,
    environment_id: str,
) -> ActionAuthorizationV1:
    """Decide the act under the development policy and record it. Idempotent on content.

    ▲ ``actor_subject`` is the CALLER'S OWN authenticated subject, and this function cannot check
    that — its caller must never pass a subject taken from a request body. That is the one invariant
    which does not relax when the policy does.

    Raises:
        ActionUnavailable: a production act. Nothing is recorded — an authorization table holds
            AUTHORIZATIONS, and a refusal is a decision, which belongs to the decision record.
        ValueError: no actor. An act with no actor is an observation, not an authorized act — the
            same rule ``record_generation_authorization`` already states.
    """
    if not action_available(action):
        raise ActionUnavailable(
            f"{action} cannot be authorized: production materialization and publication are "
            f"UNAVAILABLE until production governance exists. This is not a missing certificate — "
            f"the act has no authorized path in this deployment")
    if not actor_subject.strip():
        raise ValueError(
            "an action authorization must record who triggered it: permission is server-owned, and "
            "an act with no actor is an observation rather than an authorized act")

    authorization = ActionAuthorizationV1(
        action=action, resource_identity_hash=resource_identity_hash,
        actor_subject=actor_subject.strip(), environment_id=environment_id)

    conn.execute(
        "INSERT INTO action_authorization_revision (authorization_id, action, "
        "resource_identity_hash, actor_subject, environment_id, policy_version, "
        "permission_result, evidence_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (authorization_id) DO NOTHING",
        (authorization.authorization_id, str(authorization.action),
         authorization.resource_identity_hash, authorization.actor_subject,
         authorization.environment_id, authorization.policy_version,
         authorization.permission_result, authorization.evidence_hash))
    return authorization


def load_action_authorization(conn, authorization_id: str) -> ActionAuthorizationV1 | None:
    """One authorization, reconstructed and identity-verified.

    A row that cannot reproduce the id it is filed under is store corruption: it would authorize an
    act on a resource it no longer names. The same check ``load_generation_authorization`` makes,
    and for the same reason.
    """
    row = conn.execute(
        "SELECT action, resource_identity_hash, actor_subject, environment_id, policy_version, "
        "permission_result FROM action_authorization_revision WHERE authorization_id = %s",
        (authorization_id,)).fetchone()
    if row is None:
        return None
    authorization = ActionAuthorizationV1(
        action=ActionV1(row[0]), resource_identity_hash=row[1], actor_subject=row[2],
        environment_id=row[3], policy_version=row[4], permission_result=row[5])
    if authorization.authorization_id != authorization_id:
        raise ValueError(
            f"action authorization {authorization_id} does not reproduce its own id: it would "
            f"authorize an act on a resource it no longer names")
    return authorization
