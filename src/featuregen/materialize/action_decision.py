"""ONE question, asked at two moments — and recorded, so the second one has something to compare to.

**ASK and DECIDE are different calls over one implementation.** A workspace enabling a button asks;
only an act that will be enqueued decides. The codebase already draws this line and says why —
``verify_eligibility``'s docstring: *"A QUESTION — it records nothing. Recording an attempt every
time a screen rendered would fill the history with things nobody did."* Making every evaluation
durable would be write amplification per render and an audit trail full of decisions nobody acted on.

**DRIFT IS A REFUSAL, NOT A RE-DECISION**, and this is the easiest thing here to get backwards. A
worker that finds moved evidence and simply re-evaluates will usually get "allowed" again and
proceed — silently executing under an answer no human was ever shown. So the worker recomputes the
evidence hash and COMPARES it; a difference refuses and names which pin moved.

▲ **The caller supplies FACTS, never verdicts.** The blockers a member carries come from the
server-side evaluators that computed them, and this folds them. A client may not supply readiness,
blocker codes, formula method, certificate identity or roles — anything a caller can pass is
something a caller can forge.
"""
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from featuregen.canonical import jcs_sha256
from featuregen.materialize.action_authorization import (
    ActionUnavailable,
    ActionV1,
    action_available,
    load_action_authorization,
)

__all__ = [
    "DECISION_POLICY_VERSION",
    "ActionDecisionV1",
    "ActionRequestV1",
    "DecisionDrift",
    "DecisionMissing",
    "MemberVerdictV1",
    "ask",
    "decide",
    "recheck",
]

#: Bump when the FOLD changes. It is recorded on every decision, so "which rules produced this
#: answer" is a column rather than an inference from a date.
DECISION_POLICY_VERSION = "action-decision-v1"


class DecisionDrift(RuntimeError):
    """The evidence moved between the decision and the act. A person re-requests; nothing proceeds."""


class DecisionMissing(RuntimeError):
    """This act carries no request-time decision — a queue bypass by definition."""


class AuthorizationUnusable(RuntimeError):
    """The named authorization cannot carry a decision for THIS act, so none can be recorded.

    ▲ Distinct from a refused DECISION, and the distinction is structural: 1106's composite foreign
    key requires the decision's (action, resource, authorization) triple to exist in the
    authorization table, so a decision citing a missing or mismatched authorization is UNWRITABLE —
    recording the refusal is not an option the schema offers. Without this type, the caller got a
    bare ForeignKeyViolation out of the INSERT: an ungoverned crash where `ask()` with identical
    inputs returns clean typed blockers — a direct ask/decide divergence.
    """


@dataclass(frozen=True, slots=True)
class MemberVerdictV1:
    member_name: str
    allowed: bool
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ActionRequestV1:
    """What is being asked, and the immutable evidence it is asked against.

    ``evidence_pins`` are revision ids and content hashes — the things whose movement would change
    the answer. They are what the worker re-reads; a pin that is not here is a fact this decision
    silently does not depend on, which is how a decision survives a change that should have voided
    it.
    """

    action: ActionV1
    resource_identity_hash: str
    member_blockers: Mapping[str, Sequence[str]] = field(default_factory=dict)
    member_warnings: Mapping[str, Sequence[str]] = field(default_factory=dict)
    evidence_pins: Mapping[str, str] = field(default_factory=dict)

    @property
    def evidence_hash(self) -> str:
        return jcs_sha256({"pins": dict(sorted(self.evidence_pins.items()))})


@dataclass(frozen=True, slots=True)
class ActionDecisionV1:
    allowed: bool
    per_member: tuple[MemberVerdictV1, ...]
    blockers: tuple[str, ...]
    warnings: tuple[str, ...]
    policy_version: str
    evidence_hash: str


def _fold(conn, request: ActionRequestV1, *, authorization_id: str) -> ActionDecisionV1:
    """The one implementation. Reads; writes nothing.

    ▲ **ALL-MUST-PASS.** One refused member refuses the act. A caller handed the survivors of a
    refused group would build a group whose membership nobody decided — the rule `admit_artifacts_v2`
    already states, and it applies with more force to an act that spends money or writes values.
    """
    blockers: list[str] = []

    # ▲ AVAILABILITY FIRST. An unavailable action has no authorized path at all, so asking about its
    # evidence would be answering a question the platform cannot act on either way.
    if not action_available(request.action):
        blockers.append("ACTION_UNAVAILABLE")

    authorization = load_action_authorization(conn, authorization_id)
    if authorization is None:
        blockers.append("ACTION_AUTHORIZATION_MISSING")
    else:
        # ▲ THE AUTHORIZATION MUST BE FOR THIS ACT ON THIS RESOURCE. The composite foreign key makes
        # a mismatched pair unwritable, and this makes it unaskable — so the refusal names the
        # relationship rather than a constraint.
        if (authorization.action is not request.action
                or authorization.resource_identity_hash != request.resource_identity_hash):
            blockers.append("ACTION_AUTHORIZATION_NOT_FOR_THIS_ACT")
        if authorization.permission_result != "allowed":
            blockers.append("ACTION_AUTHORIZATION_REFUSED")

    members = sorted(set(request.member_blockers) | set(request.member_warnings))
    verdicts = tuple(
        MemberVerdictV1(
            member_name=name,
            allowed=not tuple(request.member_blockers.get(name, ())),
            blockers=tuple(request.member_blockers.get(name, ())),
            warnings=tuple(request.member_warnings.get(name, ())))
        for name in members)

    allowed = not blockers and all(v.allowed for v in verdicts)
    warnings = tuple(sorted({w for v in verdicts for w in v.warnings}))
    return ActionDecisionV1(
        allowed=allowed, per_member=verdicts,
        blockers=tuple(sorted(set(blockers))), warnings=warnings,
        policy_version=DECISION_POLICY_VERSION, evidence_hash=request.evidence_hash)


def ask(conn, request: ActionRequestV1, *, authorization_id: str) -> ActionDecisionV1:
    """Answer without recording. For eligibility reads, button state and cost estimates.

    ▲ Nothing here writes. A decision row per screen render is write amplification and an audit
    trail of decisions nobody acted on — which makes the ones somebody DID act on harder to find,
    not easier.
    """
    return _fold(conn, request, authorization_id=authorization_id)


def decide(
    conn, request: ActionRequestV1, *, authorization_id: str,
) -> tuple[str, ActionDecisionV1]:
    """Answer and RECORD it. Only for an act that will be enqueued. Idempotent on content.

    Raises:
        ActionUnavailable: a production act. It is refused here rather than recorded as a refusal,
            because an authorization for it cannot exist either — and a decision referencing no
            authorization cannot be written (the column is NOT NULL, deliberately).
    """
    if not action_available(request.action):
        raise ActionUnavailable(
            f"{request.action} cannot be decided: production materialization and publication are "
            f"unavailable until production governance exists. Nothing is recorded, because a "
            f"decision names an authorization and none can be issued for this act")

    decision = _fold(conn, request, authorization_id=authorization_id)
    # ▲ REFUSE TYPED before the INSERT the schema would refuse anyway. These blockers mean no
    # authorization row matches the (action, resource, authorization) triple — so the composite FK
    # makes the refused decision UNWRITABLE, and proceeding would surface as a ForeignKeyViolation
    # that also aborts the caller's transaction (which, on the route, carries the queue enqueue).
    unusable = {"ACTION_AUTHORIZATION_MISSING", "ACTION_AUTHORIZATION_NOT_FOR_THIS_ACT"}
    if unusable & set(decision.blockers):
        raise AuthorizationUnusable(
            f"authorization {authorization_id!r} cannot carry a decision for "
            f"{request.action} on {request.resource_identity_hash!r}: "
            f"{sorted(unusable & set(decision.blockers))}. Ask() answers this question without "
            f"recording; a decision cannot be recorded against an authorization that does not "
            f"cover the act")
    decision_id = jcs_sha256({
        "action": str(request.action),
        "resource_identity_hash": request.resource_identity_hash,
        "authorization_id": authorization_id,
        "evidence_hash": decision.evidence_hash,
        "allowed": decision.allowed,
        "policy_version": decision.policy_version,
    })
    conn.execute(
        "INSERT INTO action_decision_revision (decision_id, action, resource_identity_hash, "
        "authorization_id, per_member_verdicts_json, allowed, blockers_json, warnings_json, "
        "policy_version, evidence_pins_json, evidence_hash) "
        "VALUES (%s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, %s, %s::jsonb, %s) "
        "ON CONFLICT (decision_id) DO NOTHING",
        (decision_id, str(request.action), request.resource_identity_hash, authorization_id,
         json.dumps([{"member_name": v.member_name, "allowed": v.allowed,
                      "blockers": list(v.blockers), "warnings": list(v.warnings)}
                     for v in decision.per_member]),
         decision.allowed, json.dumps(list(decision.blockers)),
         json.dumps(list(decision.warnings)), decision.policy_version,
         json.dumps(dict(sorted(request.evidence_pins.items()))), decision.evidence_hash))
    return decision_id, decision


def recheck(conn, decision_id: str, *, current_pins: Mapping[str, str]) -> ActionDecisionV1:
    """The worker's second look: recompute the evidence hash and COMPARE.

    Raises:
        DecisionMissing: no such decision. An act with no request-time decision is a queue bypass.
        DecisionDrift: a pin moved. ▲ **The refusal is the point** — re-evaluating instead would
            usually return "allowed" again and proceed under an answer nobody was shown.
    """
    row = conn.execute(
        "SELECT allowed, blockers_json, warnings_json, policy_version, evidence_pins_json, "
        "       evidence_hash, per_member_verdicts_json "
        "  FROM action_decision_revision WHERE decision_id = %s", (decision_id,)).fetchone()
    if row is None:
        raise DecisionMissing(
            f"no action decision {decision_id!r}: this act carries no request-time decision, which "
            f"is a queue bypass however well-formed the message looks")

    stored_pins = row[4] if isinstance(row[4], dict) else json.loads(row[4])
    current = jcs_sha256({"pins": dict(sorted(current_pins.items()))})
    if current != row[5]:
        moved = sorted(
            key for key in set(stored_pins) | set(current_pins)
            if stored_pins.get(key) != current_pins.get(key))
        raise DecisionDrift(
            f"action decision {decision_id} was taken against evidence that has since moved: "
            f"{moved!r}. The act is REFUSED rather than re-decided — proceeding on a fresh answer "
            f"would execute under a verdict nobody was shown. Request it again")

    verdicts = row[6] if isinstance(row[6], list) else json.loads(row[6])
    return ActionDecisionV1(
        allowed=row[0],
        per_member=tuple(
            MemberVerdictV1(member_name=v["member_name"], allowed=v["allowed"],
                            blockers=tuple(v["blockers"]), warnings=tuple(v["warnings"]))
            for v in verdicts),
        blockers=tuple(row[1] if isinstance(row[1], list) else json.loads(row[1])),
        warnings=tuple(row[2] if isinstance(row[2], list) else json.loads(row[2])),
        policy_version=row[3], evidence_hash=row[5])
