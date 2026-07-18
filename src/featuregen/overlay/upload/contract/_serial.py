"""Shared JSON serialization for the contract flow (single source — was duplicated in gate1 + govern)."""
from __future__ import annotations

import json

from featuregen.contracts.identity import identity_to_jsonb
from featuregen.overlay.upload.feature_assist import Requirement


def actor_json(actor) -> str | None:
    """Serialize an actor to jsonb text, or None -> SQL NULL ("unknown actor"). A string subject -> a
    JSON string; an IdentityEnvelope -> identity_to_jsonb; anything else -> a structured {"repr": ...}
    (parseable JSON, never a bare Python-repr string)."""
    if actor is None:
        return None
    if isinstance(actor, str):
        return json.dumps(actor)
    try:
        return json.dumps(identity_to_jsonb(actor))
    except Exception:
        return json.dumps({"repr": str(actor)})


def requirements_to_json(reqs: tuple[Requirement, ...]) -> list[dict]:
    """Serialize typed requirements for a jsonb column / snapshot — {code, operand:[catalog, ref],
    detail}. Never carries a raw sample/PII value (detail is human-readable prose only)."""
    return [{"code": r.code, "operand": [r.operand[0], r.operand[1]], "detail": r.detail}
            for r in reqs]


def requirements_from_json(data) -> tuple[Requirement, ...]:
    """Restore typed requirements from a jsonb column / snapshot. Tolerates a missing/None payload
    (-> empty tuple) so a pre-3A-ii snapshot deserializes as no requirements."""
    out: list[Requirement] = []
    for d in data or []:
        op = d.get("operand", ["", ""])
        out.append(Requirement(code=str(d.get("code", "")),
                               operand=(str(op[0]), str(op[1])),
                               detail=str(d.get("detail", ""))))
    return tuple(out)
