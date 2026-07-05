"""Shared JSON serialization for the contract flow (single source — was duplicated in gate1 + govern)."""
from __future__ import annotations

import json

from featuregen.contracts.identity import identity_to_jsonb


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
