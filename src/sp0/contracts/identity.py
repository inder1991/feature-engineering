from __future__ import annotations

from typing import Any, Mapping

# IdentityEnvelope is the shared contract type (overview §6.1); import & re-export
# it here rather than redefining, so all phases share one frozen dataclass.
from sp0.contracts.envelopes import IdentityEnvelope

__all__ = ["IdentityEnvelope", "identity_to_jsonb", "identity_from_jsonb"]


def identity_to_jsonb(env: IdentityEnvelope) -> dict[str, Any]:
    return {
        "subject": env.subject,
        "actor_kind": env.actor_kind,
        "authenticated": env.authenticated,
        "auth_method": env.auth_method,
        "role_claims": list(env.role_claims),
        "groups": list(env.groups),
        "tenant": env.tenant,
        "on_behalf_of": env.on_behalf_of,
        "impersonation": env.impersonation,
        "break_glass": env.break_glass,
        "source_of_authority": env.source_of_authority,
        "attestation": env.attestation,
    }


def identity_from_jsonb(d: Mapping[str, Any]) -> IdentityEnvelope:
    return IdentityEnvelope(
        subject=d["subject"],
        actor_kind=d["actor_kind"],
        authenticated=d["authenticated"],
        auth_method=d["auth_method"],
        role_claims=tuple(d.get("role_claims", ())),
        groups=tuple(d.get("groups", ())),
        tenant=d.get("tenant"),
        on_behalf_of=d.get("on_behalf_of"),
        impersonation=d.get("impersonation"),
        break_glass=d.get("break_glass", False),
        source_of_authority=d.get("source_of_authority"),
        attestation=d.get("attestation"),
    )
