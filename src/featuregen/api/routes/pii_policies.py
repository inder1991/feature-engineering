"""The PII allow-policy authoring surface (verified-interfaces doc D14; DEFERRED-WORK A.34).

``GET  /governance/data-use-policies``
``POST /governance/data-use-policies/{concept_name}/approve``
``POST /governance/data-use-policies/{concept_name}/revoke``

**Why this surface exists.** ``feature_assist._use_gate`` refuses a personal-data operand with
``PERSONAL_DATA_POLICY_REQUIRED`` and tells the reviewer "a governance owner must declare one". Until
this landed there was nowhere to declare one, so five shipped financial-crime recipes were
permanently refused and the only bypass was ``FEATUREGEN_FEATURE_USE_GATE=0``, which drops all four
refusal classes at once — protected characteristics included. This is the aimed answer.

**AuthZ, and the deviation it encodes.**

* GET is ``catalog:read`` and nothing more. TRANSPARENCY IS THE POINT: any catalog reader who can
  be shown "this feature cannot be built, no policy licenses ``pep_flag``" must be able to see
  whether one exists, who approved it and for what. A read gate here would leave the refusal
  pointing at a page the person it was written for cannot open. Nothing read-scoped is exposed —
  the listing is registry concepts and governance decisions, never a column, a catalog or a value.
* The two POSTs take ``require_confirmer`` — the raw ``platform-admin`` role claim — but there is
  ONE action, not a propose/confirm pair. **This deviates from the four-eyes convention every other
  governed declaration on this branch follows, by explicit user decision recorded as D14.** The
  control is the immutable who/when/purpose record plus the platform-admin gate. ``require_confirmer``
  is reused because it is exactly the check we want (the platform-admin claim); the NAME carries a
  second-approver connotation this route deliberately does not honour. Do not re-add a confirmer
  step without a new user decision.

**CAS.** ``expected_pointer_version`` is REQUIRED and rides in the BODY (the ``governance.py`` /
``dataset_policies.py`` convention). It is deliberately not an optional query parameter: the
optional-gate pattern fails OPEN when omitted, which turns a concurrency guard into a suggestion.
``0`` CLAIMS first-write and is refused if a policy appeared meanwhile.

**No flag.** Unlike the Release-B policy routes there is no env gate. A hidden approval surface
would leave the refusal it answers pointing at nothing, which is the state this slice exists to end.

**No-blocked framing.** A concept with no policy is not an error, not a gap and not a failure —
nobody has had to decide about it yet. The payload says so with a closed ``status`` vocabulary
(``none`` / ``active`` / ``revoked``) rather than a boolean, because "nobody decided" and "somebody
withdrew a decision" are different facts and only the GATE may collapse them.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import (
    get_conn,
    get_feature_gen_conn,
    get_identity,
    require_catalog_read,
    require_confirmer,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.concepts import concept as concept_record
from featuregen.overlay.upload.pii_policy import (
    MAX_PURPOSE_LEN,
    MIN_PURPOSE_LEN,
    validate_policy_concept,
)
from featuregen.overlay.upload.pii_policy_store import (
    PiiUsePolicyStateV1,
    approve_pii_use_policy,
    pii_use_policy_states,
    revoke_pii_use_policy,
)
from featuregen.overlay.upload.source_selection import (
    PolicyStoreConflict,
    PolicyValidationError,
    SelectionError,
)

router = APIRouter()
_RRConn = Annotated[psycopg.Connection, Depends(get_feature_gen_conn, scope="function")]
_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]

_BASE = "/governance/data-use-policies"


def _state_payload(state: PiiUsePolicyStateV1) -> dict:
    record = concept_record(state.concept_name)
    revision = state.revision
    return {
        "concept_name": state.concept_name,
        # The registry's own words, so the approver reads what the concept MEANS beside the purpose
        # they are about to declare for it, rather than deciding from a bare identifier.
        "description": "" if record is None else record.description,
        "group": "" if record is None else record.group,
        "status": state.status,                       # none | active | revoked
        "pointer_version": state.pointer_version,     # 0 == never declared; the first-write claim
        "purpose": None if revision is None else revision.purpose,
        "revision_id": None if revision is None else revision.revision_id,
        # WHO first authored this content vs WHO made it current — different questions, and the
        # house split (identity excludes the approver) is exactly why both are served.
        "approved_by": state.approved_by,
        "approved_at": None if state.approved_at is None else state.approved_at.isoformat(),
        "declared_by": None if state.pointer is None else state.pointer.declared_by,
        "updated_at": (None if state.pointer is None or state.pointer.updated_at is None
                       else state.pointer.updated_at.isoformat()),
    }


@router.get(_BASE, dependencies=[Depends(require_catalog_read)])
def list_data_use_policies(conn: _RRConn) -> dict:
    """Every pii-classed concept and its current policy state.

    REGISTRY-DRIVEN, not table-driven: the 20-odd concepts nobody has declared anything about are
    the ones a reviewer most needs to see, and a listing of the rows that happen to exist would
    hide exactly them."""
    return {
        "concepts": [_state_payload(s) for s in pii_use_policy_states(conn)],
        "purpose_bounds": {"min": MIN_PURPOSE_LEN, "max": MAX_PURPOSE_LEN},
    }


class ApprovePolicyRequest(BaseModel):
    """``expected_pointer_version`` is REQUIRED (module docstring): 0 claims first-write, >= 1 names
    the exact version read. ``purpose`` is bounded free text in v1 (D14); the server re-validates
    the bound and normalizes whitespace before anything is written."""

    expected_pointer_version: int = Field(ge=0)
    purpose: str = Field(min_length=1, max_length=MAX_PURPOSE_LEN)


class RevokePolicyRequest(BaseModel):
    """Revocation carries no purpose: what is withdrawn is what was declared, and inventing a new
    purpose here would record a declaration nobody made."""

    expected_pointer_version: int = Field(ge=0)


def _concept(raw: str) -> str:
    try:
        return validate_policy_concept(raw)
    except PolicyValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post(_BASE + "/{concept_name}/approve",
             dependencies=[Depends(require_catalog_read), Depends(require_confirmer)])
def approve_data_use_policy(concept_name: str, body: ApprovePolicyRequest, conn: _Conn,
                            identity: _Identity) -> dict:
    """Declare that one pii-classed concept may be a model input, for a stated purpose.

    SINGLE ACTION, platform-admin only — no propose/confirm split, by explicit user decision
    (D14). The policy is ACTIVE the moment this returns and the feature use gate stops refusing
    operands of this concept.

    400 on a concept no policy can license (a protected characteristic) or a purpose outside the
    bound; 409 when somebody else declared first."""
    name = _concept(concept_name)
    try:
        revision_id, version = approve_pii_use_policy(
            conn, concept_name=name, purpose=body.purpose,
            expected_pointer_version=body.expected_pointer_version, actor=identity.subject)
    # 400 covers `PolicyValidationError` too (it IS a `SelectionError`): a store-level refusal no
    # retry can fix is the request being wrong, not a race. 409 is reserved for the race.
    except SelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PolicyStoreConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"concept_name": name, "revision_id": revision_id, "pointer_version": version,
            "status": "active"}


@router.post(_BASE + "/{concept_name}/revoke",
             dependencies=[Depends(require_catalog_read), Depends(require_confirmer)])
def revoke_data_use_policy(concept_name: str, body: RevokePolicyRequest, conn: _Conn,
                           identity: _Identity) -> dict:
    """Withdraw the active policy for one concept — as a NEW revision, never a delete.

    The features it licensed are refused again from the next validation onwards. The approval it
    withdraws stays readable forever, which is what makes "who allowed this, and for how long"
    answerable after the fact.

    400 when there is nothing current to revoke, or when it is already revoked; 409 on a CAS
    miss."""
    name = _concept(concept_name)
    try:
        revision_id, version = revoke_pii_use_policy(
            conn, concept_name=name, expected_pointer_version=body.expected_pointer_version,
            actor=identity.subject)
    except SelectionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except PolicyStoreConflict as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"concept_name": name, "revision_id": revision_id, "pointer_version": version,
            "status": "revoked"}
