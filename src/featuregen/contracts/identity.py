from __future__ import annotations

from typing import Any

# IdentityEnvelope is the shared contract type (overview §6.1); import & re-export
# it here rather than redefining, so all phases share one frozen dataclass.
from featuregen.contracts.envelopes import IdentityEnvelope

# NOTE: this module deliberately exposes only the SERIALIZER (``identity_to_jsonb``). The inverse
# ``identity_from_jsonb`` deserializer lives solely in ``events/serde.py`` (SP-0.5 BLOCKER #1): a
# stored ``authenticated=True`` actor must be reconstructed through the sanctioned trust-capability
# factory, and ``contracts`` is the foundational layer that CANNOT import the ``identity/`` package
# (where that factory lives) without a cycle. A duplicate deserializer here would forge an
# authenticated envelope straight from an untrusted dict, so it is intentionally absent.
__all__ = ["IdentityEnvelope", "identity_to_jsonb"]


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
