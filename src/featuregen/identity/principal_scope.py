"""Frozen principal/data-scope revisions and their bindings (task B0a; migration 1133).

**The hole this closes.** Both generation routes took the caller's word for its own read scope —
``GenerationIn.roles`` on ``/build-sets/generations`` and ``execution_parameters["roles"]`` on
``/code-generation-jobs`` — and those strings reached ``decide_read_scope`` as Gate 2's read scope.
A caller could claim read roles it was never granted, and a cross-catalog generation makes that
reach across catalogs. Roles are now resolved SERVER-SIDE from the authenticated principal
(:func:`featuregen.api.deps.get_identity` → :func:`featuregen.identity.local_session.resolve_session`
→ ``IdentityEnvelope.role_claims``), frozen as a revision here, and the queue carries a BINDING ID
rather than any claims.

Two types, because they answer two questions:

``PrincipalScopeRevisionV1``
    WHAT was resolved — an immutable, content-addressed snapshot of the principal and its data
    scope. Same principal + same scope → same ``revision_id``, always, and one row.

``PrincipalScopeBindingV1``
    WHICH act that snapshot authorized — one row per ``(subject_kind, subject_id)``, naming the
    revision and (where one exists yet) the action decision. The queue payload names this binding;
    the worker compares the claim against the durable row before believing anything.

**The recheck is two prongs, and neither alone is sufficient** (:func:`authorize_frozen_scope`):

1. *Frozen claims authorize.* The claims that reach compilation are the ones frozen in the
   revision the binding pins — never the payload's, never re-resolved. That is what makes a build
   reproducible and what a person was actually shown at request time.
2. *Current claims gate.* The worker ALSO asks
   :func:`featuregen.identity.current_principal.resolve_current_principal` whether the principal is
   still current, and refuses when it is not — a revoked account must not go on executing on frozen
   claims. It refuses again when the current claims no longer CONTAIN the frozen ones: a scope that
   changed shape must not have its frozen authorization silently re-interpreted. (Claims a
   principal has GAINED since are irrelevant — the frozen set is still what runs.)

Fail-closed on anything not ``CURRENT`` is the platform's existing posture, stated at
``formula_draft_worker._author`` and ``recipe_formula_worker``: the platform declines to read the
catalog on behalf of a principal it cannot vouch for.

Store discipline (the A3/A7 idiom): ``conn`` positional, everything else keyword-only; validation
refuses blanks and off-vocabulary values with :class:`PrincipalScopeDefect` BEFORE any SQL; inserts
are ``ON CONFLICT DO NOTHING`` with a content-verified read-back, and the tables' UNIQUE
``content_hash`` is the DB backstop that makes concurrent writers converge on one row.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from featuregen.canonical import jcs_sha256
from featuregen.contracts import DbConn, IdentityEnvelope

__all__ = [
    "PRINCIPAL_NOT_CURRENT",
    "PRINCIPAL_SCOPE_BINDING_ID_PREFIX",
    "PRINCIPAL_SCOPE_BINDING_MISMATCH",
    "PRINCIPAL_SCOPE_ID_PREFIX",
    "PRINCIPAL_SCOPE_MISSING",
    "PRINCIPAL_SCOPE_NARROWED",
    "PRINCIPAL_SCOPE_REFUSAL_CODES",
    "PRINCIPAL_SCOPE_SUBJECT_KINDS",
    "FrozenScopeAuthorization",
    "PrincipalScopeBindingV1",
    "PrincipalScopeDefect",
    "PrincipalScopeRebound",
    "PrincipalScopeRevisionV1",
    "PrincipalScopeStoreConflict",
    "ScopeAuthorizationRefused",
    "authorize_frozen_scope",
    "bind_principal_scope",
    "ensure_principal_scope_revision",
    "load_principal_scope_binding",
    "load_principal_scope_binding_by_id",
    "load_principal_scope_revision",
    "principal_scope_revision_of",
]

_REVISION_CONTRACT = "principal_scope_revision_v1"
_BINDING_CONTRACT = "principal_scope_binding_v1"

#: Deterministic id prefixes (the ``ecx_``/``dtp_``/``jvp_`` family).
PRINCIPAL_SCOPE_ID_PREFIX = "psc_"
PRINCIPAL_SCOPE_BINDING_ID_PREFIX = "psb_"

#: The CLOSED subject vocabulary — mirrors migration 1133's named CHECK. Widening it is a new
#: migration, which is the review gate we want.
PRINCIPAL_SCOPE_SUBJECT_KINDS: tuple[str, ...] = (
    "code_generation_job", "generation_request")

#: The four refusals :func:`authorize_frozen_scope` raises. Spelled here and mirrored member-for-
#: member in ``materialize.codes.CompilationRefusalCode``; the generation lane converts by VALUE,
#: so a drift between the two spellings raises rather than refusing under an invented code.
#: No durable binding for this act, or a payload that names none — an act with no recorded
#: authority is a queue bypass however it got here.
PRINCIPAL_SCOPE_MISSING = "PRINCIPAL_SCOPE_MISSING"
#: The payload names a binding that is not this act's. THE ROW IS AUTHORITATIVE (the 1108 rule).
PRINCIPAL_SCOPE_BINDING_MISMATCH = "PRINCIPAL_SCOPE_BINDING_MISMATCH"
#: The authentication authority no longer vouches for the principal (revoked, or unverifiable).
PRINCIPAL_NOT_CURRENT = "PRINCIPAL_NOT_CURRENT"
#: The principal still exists and has LOST claims the frozen scope carries.
PRINCIPAL_SCOPE_NARROWED = "PRINCIPAL_SCOPE_NARROWED"

PRINCIPAL_SCOPE_REFUSAL_CODES: tuple[str, ...] = (
    PRINCIPAL_SCOPE_MISSING, PRINCIPAL_SCOPE_BINDING_MISMATCH,
    PRINCIPAL_NOT_CURRENT, PRINCIPAL_SCOPE_NARROWED)


class PrincipalScopeDefect(ValueError):
    """A refused declaration — a blank subject, an unknown subject kind, a non-string claim —
    raised BEFORE any SQL."""


class PrincipalScopeStoreConflict(RuntimeError):
    """The store and the table disagree (a row failing content verification, or an ensure whose
    read-back found nothing) — corruption, never served."""


class PrincipalScopeRebound(RuntimeError):
    """A second, DIFFERENT scope binding was attempted for one act. One authority per act: the
    alternative is "under whose scope did this run" having two answers."""


class ScopeAuthorizationRefused(Exception):
    """A governed refusal from the worker's two-prong recheck, carrying one of
    :data:`PRINCIPAL_SCOPE_REFUSAL_CODES`."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _text(raw: object, *, what: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise PrincipalScopeDefect(
            f"{what} must be a non-blank string, got {raw!r} — a blank one addresses nothing")
    return raw.strip()


def _optional_text(raw: object, *, what: str) -> str | None:
    if raw is None:
        return None
    return _text(raw, what=what)


def _claims(raw: object) -> tuple[str, ...]:
    """The data scope, canonicalized: sorted, deduplicated, every claim a non-blank string.

    Sorted and deduplicated because the SCOPE is a set — two requests granting the same claims in
    a different order are the same authority, and hashing the caller's ordering would fork the
    identity of an unchanged scope.
    """
    if isinstance(raw, str) or not hasattr(raw, "__iter__"):
        raise PrincipalScopeDefect(
            f"role_claims must be a sequence of claim strings, got {raw!r}")
    return tuple(sorted({_text(claim, what="a role claim") for claim in raw}))


def _subject_kind(raw: object) -> str:
    if raw not in PRINCIPAL_SCOPE_SUBJECT_KINDS:
        raise PrincipalScopeDefect(
            f"subject_kind must be one of {list(PRINCIPAL_SCOPE_SUBJECT_KINDS)} (migration 1133's "
            f"closed CHECK), got {raw!r}")
    return str(raw)


@dataclass(frozen=True, slots=True)
class PrincipalScopeRevisionV1:
    """One immutable, content-addressed snapshot of a resolved principal and its data scope.

    Every STORED field is hashed: on a content-addressed table with a UNIQUE content hash, a column
    outside the hash would let two different values share one id and the first writer's value would
    answer for the second's. ``recorded_at`` is the exception and is not stored by the store at all
    — it is the table's own default, filled on load, never hashed.

    **THE THREE ENVELOPE FIELDS THIS TYPE DOES NOT CARRY, on the record.**

    ``groups`` is excluded because local IAM derives ROLES from group memberships and the roles are
    what ``decide_read_scope`` reads — groups are how the scope was reached rather than the scope
    itself, and hashing them would fork the identity of an unchanged scope whenever somebody joined
    an unrelated group.

    ``source_of_authority`` and ``attestation`` are excluded for a DIFFERENT reason, and the
    ``groups`` argument does not cover them: they are claims about WHO VOUCHED for the principal,
    which is exactly the kind of fact a scope revision should carry once anything sets one. Nothing
    does today — ``build_human_identity`` sets neither, and ``resolve_session`` is the only
    authenticated producer — so storing them now would hash a constant ``null``.

    ▲ When OIDC or service principals land and either becomes non-null, adding it is a NEW
    migration **and a RE-IDENTIFICATION**: every existing revision's content hash changes, so
    existing rows keep ids that no longer describe their payload and every binding pinning one
    keeps pointing at the older content. Plan that as an expand/adopt — a new revision minted
    beside the old — never an in-place widening of this payload.
    """

    subject: str
    actor_kind: str
    authenticated: bool
    auth_method: str
    role_claims: tuple[str, ...]
    tenant: str | None = None
    on_behalf_of: str | None = None
    impersonation: str | None = None
    break_glass: bool = False
    recorded_at: datetime | None = None
    content_hash: str = field(init=False, default="")
    revision_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject", _text(self.subject, what="subject"))
        object.__setattr__(self, "actor_kind", _text(self.actor_kind, what="actor_kind"))
        object.__setattr__(self, "auth_method", _text(self.auth_method, what="auth_method"))
        object.__setattr__(self, "role_claims", _claims(self.role_claims))
        object.__setattr__(self, "tenant", _optional_text(self.tenant, what="tenant"))
        object.__setattr__(
            self, "on_behalf_of", _optional_text(self.on_behalf_of, what="on_behalf_of"))
        object.__setattr__(
            self, "impersonation", _optional_text(self.impersonation, what="impersonation"))
        object.__setattr__(self, "authenticated", bool(self.authenticated))
        object.__setattr__(self, "break_glass", bool(self.break_glass))
        content_hash = jcs_sha256(self.content_payload())
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(self, "revision_id", f"{PRINCIPAL_SCOPE_ID_PREFIX}{content_hash}")

    def content_payload(self) -> dict[str, Any]:
        """Canonical serialization — every stored fact, and no provenance."""
        return {
            "contract": _REVISION_CONTRACT,
            "subject": self.subject,
            "actor_kind": self.actor_kind,
            "authenticated": self.authenticated,
            "auth_method": self.auth_method,
            "role_claims": list(self.role_claims),
            "tenant": self.tenant,
            "on_behalf_of": self.on_behalf_of,
            "impersonation": self.impersonation,
            "break_glass": self.break_glass,
        }


def principal_scope_revision_of(principal: IdentityEnvelope) -> PrincipalScopeRevisionV1:
    """The revision a resolved principal denotes — PURE, so a read-only surface (``/plan``) can
    address the same identity the write surface will record without writing anything.

    ``groups`` is deliberately absent: local IAM derives ROLES from group memberships and the roles
    are what ``decide_read_scope`` reads, so groups are how the scope was reached rather than the
    scope itself. Hashing them would fork the identity of an unchanged scope whenever a person
    joined an unrelated group.
    """
    return PrincipalScopeRevisionV1(
        subject=principal.subject,
        actor_kind=principal.actor_kind,
        authenticated=principal.authenticated,
        auth_method=principal.auth_method,
        role_claims=principal.role_claims,
        tenant=principal.tenant,
        on_behalf_of=principal.on_behalf_of,
        impersonation=principal.impersonation,
        break_glass=principal.break_glass)


def ensure_principal_scope_revision(conn: DbConn, *, principal: IdentityEnvelope) -> str:
    """Mint-or-find the revision for one resolved principal and return its ``revision_id``."""
    revision = principal_scope_revision_of(principal)
    conn.execute(
        "INSERT INTO principal_scope_revision "
        "  (revision_id, subject, actor_kind, authenticated, auth_method, role_claims, tenant, "
        "   on_behalf_of, impersonation, break_glass, content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s) "
        "ON CONFLICT (revision_id) DO NOTHING",
        (revision.revision_id, revision.subject, revision.actor_kind, revision.authenticated,
         revision.auth_method, _json(list(revision.role_claims)), revision.tenant,
         revision.on_behalf_of, revision.impersonation, revision.break_glass,
         revision.content_hash))
    if load_principal_scope_revision(conn, revision.revision_id) is None:
        raise PrincipalScopeStoreConflict(
            f"principal scope revision {revision.revision_id} did not persist")
    return revision.revision_id


def load_principal_scope_revision(
    conn: DbConn, revision_id: str,
) -> PrincipalScopeRevisionV1 | None:
    """Load and CONTENT-VERIFY one revision; ``None`` when absent, corruption raises."""
    row = conn.execute(
        "SELECT subject, actor_kind, authenticated, auth_method, role_claims, tenant, "
        "on_behalf_of, impersonation, break_glass, content_hash, recorded_at "
        "FROM principal_scope_revision WHERE revision_id = %s", (revision_id,)).fetchone()
    if row is None:
        return None
    revision = PrincipalScopeRevisionV1(
        subject=row[0], actor_kind=row[1], authenticated=row[2], auth_method=row[3],
        role_claims=tuple(_decoded(row[4])), tenant=row[5], on_behalf_of=row[6],
        impersonation=row[7], break_glass=row[8], recorded_at=row[10])
    if revision.content_hash != row[9] or revision.revision_id != revision_id:
        raise PrincipalScopeStoreConflict(
            f"principal scope revision {revision_id} fails content verification")
    return revision


@dataclass(frozen=True, slots=True)
class PrincipalScopeBindingV1:
    """The frozen scope one act runs under — content-addressed, so re-binding the same act to the
    same revision under the same decision is the same row and never a second answer."""

    revision_id: str
    subject_kind: str
    subject_id: str
    action_decision_revision_id: str | None = None
    bound_at: datetime | None = None
    content_hash: str = field(init=False, default="")
    binding_id: str = field(init=False, default="")

    def __post_init__(self) -> None:
        object.__setattr__(self, "revision_id", _text(self.revision_id, what="revision_id"))
        object.__setattr__(self, "subject_kind", _subject_kind(self.subject_kind))
        object.__setattr__(self, "subject_id", _text(self.subject_id, what="subject_id"))
        object.__setattr__(self, "action_decision_revision_id", _optional_text(
            self.action_decision_revision_id, what="action_decision_revision_id"))
        content_hash = jcs_sha256(self.content_payload())
        object.__setattr__(self, "content_hash", content_hash)
        object.__setattr__(
            self, "binding_id", f"{PRINCIPAL_SCOPE_BINDING_ID_PREFIX}{content_hash}")

    def content_payload(self) -> dict[str, Any]:
        return {
            "contract": _BINDING_CONTRACT,
            "revision_id": self.revision_id,
            "subject_kind": self.subject_kind,
            "subject_id": self.subject_id,
            "action_decision_revision_id": self.action_decision_revision_id,
        }


def bind_principal_scope(
    conn: DbConn, *, revision_id: str, subject_kind: str, subject_id: str,
    action_decision_revision_id: str | None = None,
) -> str:
    """Bind a frozen scope to one act and return the ``binding_id`` the queue will carry.

    Idempotent on content: the same act bound to the same revision under the same decision answers
    the same id. A SECOND, DIFFERENT binding for one act raises :class:`PrincipalScopeRebound`
    rather than reaching the UNIQUE constraint — a caller gets a named refusal instead of an
    aborted transaction.
    """
    binding = PrincipalScopeBindingV1(
        revision_id=revision_id, subject_kind=subject_kind, subject_id=subject_id,
        action_decision_revision_id=action_decision_revision_id)
    # The revision must EXIST and verify before an act is allowed to name it: there is no FK to do
    # this (see migration 1133's header — an FK onto an append-only table replaces its own
    # append-only refusal with a foreign-key one), so the store is the referential check.
    if load_principal_scope_revision(conn, binding.revision_id) is None:
        raise PrincipalScopeDefect(
            f"no principal scope revision {binding.revision_id!r}: an act cannot name an authority "
            f"that was never recorded")
    existing = load_principal_scope_binding(
        conn, subject_kind=binding.subject_kind, subject_id=binding.subject_id)
    if existing is not None and existing.binding_id != binding.binding_id:
        raise PrincipalScopeRebound(
            f"{binding.subject_kind} {binding.subject_id!r} is already bound to scope "
            f"{existing.revision_id!r} (binding {existing.binding_id!r}); re-binding it to "
            f"{binding.revision_id!r} would make 'under whose scope did this run' a question with "
            f"two answers")
    conn.execute(
        "INSERT INTO principal_scope_binding "
        "  (binding_id, revision_id, subject_kind, subject_id, action_decision_revision_id, "
        "   content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s) ON CONFLICT (binding_id) DO NOTHING",
        (binding.binding_id, binding.revision_id, binding.subject_kind, binding.subject_id,
         binding.action_decision_revision_id, binding.content_hash))
    if load_principal_scope_binding_by_id(conn, binding.binding_id) is None:
        raise PrincipalScopeStoreConflict(
            f"principal scope binding {binding.binding_id} did not persist")
    return binding.binding_id


def load_principal_scope_binding(
    conn: DbConn, *, subject_kind: str, subject_id: str,
) -> PrincipalScopeBindingV1 | None:
    """THE act's authority, by the act's own identity — the row a payload's claim is checked
    against."""
    return _binding_from(conn.execute(
        "SELECT binding_id, revision_id, subject_kind, subject_id, action_decision_revision_id, "
        "content_hash, bound_at FROM principal_scope_binding "
        "WHERE subject_kind = %s AND subject_id = %s",
        (_subject_kind(subject_kind), _text(subject_id, what="subject_id"))).fetchone())


def load_principal_scope_binding_by_id(
    conn: DbConn, binding_id: str,
) -> PrincipalScopeBindingV1 | None:
    return _binding_from(conn.execute(
        "SELECT binding_id, revision_id, subject_kind, subject_id, action_decision_revision_id, "
        "content_hash, bound_at FROM principal_scope_binding WHERE binding_id = %s",
        (binding_id,)).fetchone())


def _binding_from(row: Any) -> PrincipalScopeBindingV1 | None:
    if row is None:
        return None
    binding = PrincipalScopeBindingV1(
        revision_id=row[1], subject_kind=row[2], subject_id=row[3],
        action_decision_revision_id=row[4], bound_at=row[6])
    if binding.content_hash != row[5] or binding.binding_id != row[0]:
        raise PrincipalScopeStoreConflict(
            f"principal scope binding {row[0]} fails content verification")
    return binding


@dataclass(frozen=True, slots=True)
class FrozenScopeAuthorization:
    """What the two prongs agreed on: the frozen claims that authorize, and the current claims that
    let the act proceed."""

    binding: PrincipalScopeBindingV1
    revision: PrincipalScopeRevisionV1
    #: PRONG 1 — what compilation reads. The FROZEN claims, never re-resolved.
    role_claims: tuple[str, ...]
    #: PRONG 2's evidence — what the authentication authority says the principal holds NOW.
    current_role_claims: tuple[str, ...]


def authorize_frozen_scope(
    conn: DbConn, *, subject_kind: str, subject_id: str, claimed_binding_id: str | None,
    observed_at: datetime, resolver: Any | None = None,
) -> FrozenScopeAuthorization:
    """The worker's recheck, BOTH prongs — frozen claims authorize, current claims gate.

    Raises :class:`ScopeAuthorizationRefused` carrying one of
    :data:`PRINCIPAL_SCOPE_REFUSAL_CODES`. Returns the frozen claims to compile under; the caller
    never reads a role from the payload.

    ``resolver`` is the explicit override; absent it, prong 2 runs against the DEPLOYMENT'S
    configured authority (``FEATUREGEN_WORKER_IDENTITY_RESOLVER``, resolved by
    :func:`~featuregen.identity.current_principal.configured_worker_identity_resolver` and named in
    the API's boot log). Lane callers pass nothing precisely so that the authority is a deployment
    setting rather than a per-call-site decision.
    """
    from featuregen.identity.current_principal import (
        PrincipalResolutionStatus,
        resolve_current_principal,
    )

    binding = load_principal_scope_binding(
        conn, subject_kind=subject_kind, subject_id=subject_id)
    if binding is None:
        raise ScopeAuthorizationRefused(
            PRINCIPAL_SCOPE_MISSING,
            f"{subject_kind} {subject_id!r} has no recorded principal scope: an act whose read "
            f"scope was never resolved from an authenticated principal is a queue bypass however "
            f"it got here")
    if not claimed_binding_id:
        raise ScopeAuthorizationRefused(
            PRINCIPAL_SCOPE_MISSING,
            f"the work item for {subject_kind} {subject_id!r} names no principal scope binding, "
            f"and this act runs under {binding.binding_id!r}: a payload that names no authority "
            f"is refused rather than run under one it never claimed")
    if claimed_binding_id != binding.binding_id:
        raise ScopeAuthorizationRefused(
            PRINCIPAL_SCOPE_BINDING_MISMATCH,
            f"the work item names principal scope binding {claimed_binding_id!r} and "
            f"{subject_kind} {subject_id!r} is bound to {binding.binding_id!r}: the recorded row "
            f"is the authority and the payload is a claim")

    revision = load_principal_scope_revision(conn, binding.revision_id)
    if revision is None:
        raise ScopeAuthorizationRefused(
            PRINCIPAL_SCOPE_MISSING,
            f"principal scope binding {binding.binding_id!r} names revision "
            f"{binding.revision_id!r}, which is not recorded")

    # ── PRONG 2: the CURRENT authority. Frozen claims prove what WAS authorized; they cannot
    # prove the account still exists. Fail-closed on anything but CURRENT — the posture
    # `formula_draft_worker._author` already takes: the platform declines to read the catalog on
    # behalf of a principal it cannot vouch for.
    current = resolve_current_principal(
        conn, revision.subject, revision.tenant, observed_at, resolver=resolver)
    if current.status is not PrincipalResolutionStatus.CURRENT or current.principal is None:
        raise ScopeAuthorizationRefused(
            PRINCIPAL_NOT_CURRENT,
            f"the authentication authority reports {revision.subject!r} as "
            f"{current.status.value} ({current.reason or 'no reason given'}): frozen claims prove "
            f"what was authorized, never that the account still is")
    current_claims = tuple(current.principal.role_claims)
    lost = tuple(claim for claim in revision.role_claims if claim not in set(current_claims))
    if lost:
        raise ScopeAuthorizationRefused(
            PRINCIPAL_SCOPE_NARROWED,
            f"{revision.subject!r} no longer holds {list(lost)}, which this act's frozen scope "
            f"carries: a scope that changed shape must not have its frozen authorization silently "
            f"re-interpreted")
    return FrozenScopeAuthorization(
        binding=binding, revision=revision, role_claims=revision.role_claims,
        current_role_claims=current_claims)


def _json(value: Any) -> str:
    import json

    return json.dumps(value)


def _decoded(value: Any) -> list[str]:
    import json

    return value if isinstance(value, list) else json.loads(value)
