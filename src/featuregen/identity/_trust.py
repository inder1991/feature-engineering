"""Private trust capability for authenticated identity minting (SP-0.5 BLOCKER #1 hardening).

An ``authenticated=True`` IdentityEnvelope may be produced ONLY by code that holds the private
``_TRUST_CAPABILITY`` singleton. This replaces the old forgeable ``_verified: bool`` kwarg — a
plain bool any caller could pass (a worked bypass even lived in the test shim) — with an object
ordinary code cannot name (module-private, absent from every ``__all__``) and that is checked by
identity (``is``), never by value. Naming it at all means reaching into a private module, which the
grep-guard test flags.

Two — and only two — kinds of code legitimately hold the capability:
  (a) the verifiers in ``verify.py``, which mint an authenticated principal from a PROVEN token; and
  (b) ``mint_trusted_identity`` below, the sanctioned factory for the internal trust ROOTS that must
      reconstruct/produce an authenticated principal WITHOUT a token — the write-once event store
      (via ``events/serde.py``) and the durable timer runtime (via ``aggregates/activation.py``).

``test_no_stray_authenticated_mints`` makes the boundary auditable: no other module in
``src/featuregen/`` may reference the capability, call ``mint_trusted_identity``, or construct
``authenticated=True`` directly.
"""

from __future__ import annotations

from collections.abc import Iterable

from featuregen.contracts.identity import IdentityEnvelope


class _TrustCapability:
    """Unforgeable capability object. Not exported; ordinary code cannot name its type."""

    __slots__ = ()


# The single capability instance. Compared by identity (``is``) — never reconstructable by value.
_TRUST_CAPABILITY = _TrustCapability()


def mint_trusted_identity(
    *,
    subject: str,
    actor_kind: str,
    auth_method: str,
    role_claims: Iterable[str] = (),
    groups: Iterable[str] = (),
    tenant: str | None = None,
    on_behalf_of: str | None = None,
    impersonation: str | None = None,
    break_glass: bool = False,
    source_of_authority: str | None = None,
    attestation: str | None = None,
) -> IdentityEnvelope:
    """Mint an AUTHENTICATED IdentityEnvelope for an internal trust ROOT that has no bearer token
    to verify.

    The ONLY sanctioned call sites are ``events/serde.py`` (reconstructing a historically
    authenticated actor from a write-once stored event) and ``aggregates/activation.py`` (the
    trusted timer-runtime actor behind auto-expiry). Every OTHER authenticated mint must come from a
    verifier proving a token. This factory is deliberately permissive about ``auth_method`` and
    validation because it reconstructs already-established principals (e.g. the timer's
    ``internal`` method, or arbitrary stored auth methods) rather than attesting a fresh one.
    """
    return IdentityEnvelope(
        subject=subject,
        actor_kind=actor_kind,
        authenticated=True,
        auth_method=auth_method,
        role_claims=tuple(role_claims),
        groups=tuple(groups),
        tenant=tenant,
        on_behalf_of=on_behalf_of,
        impersonation=impersonation,
        break_glass=break_glass,
        source_of_authority=source_of_authority,
        attestation=attestation,
    )
