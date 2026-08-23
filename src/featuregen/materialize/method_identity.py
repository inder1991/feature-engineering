"""WHICH EXACT METHOD authored a member — the hash a production certificate is matched against.

**Why the method alone is not enough.** 1099 records *which of two methods* authored each member.
A certificate covers more than a category: a MODEL, a pair of frozen author/critic contracts, a
grammar and a schema version — or a blueprint revision and the expectation it stood on. Matching a
certificate against ``"LLM_AUTHORED"`` matches on the wrong thing, and would pass for a model the
platform never evaluated.

**DERIVED FROM THE RUN'S OWN EVIDENCE, never from what is deployed now.** The obvious shortcut is to
read `current_formula_generation_settings()` at sealing time. That is 1099's backfill error moved
earlier: it records *today's* configuration against a run authored under a different one. What was
actually sent lives in ``llm_dispatch`` — `provider`, `model`, `provider_contract_hash` per
`call_role` — and in the run's ``REVIEW_BYPASSED`` trace payload, which names the exact blueprint
revision and expectation hash the deterministic producer stood on.

▲ **The production authoring lane freezes no configuration per run** — `frozen_configuration_json`
has exactly one writer and it is the shadow evaluation lane — so the dispatch audit is not merely a
convenient source, it is the only truthful one.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from featuregen.canonical import jcs_sha256
from featuregen.materialize.authoring_provenance import (
    LLM_AUTHORED,
    REVIEWED_RECIPE_BLUEPRINT,
)

__all__ = [
    "METHOD_IDENTITY_VERSION",
    "MethodIdentityConflict",
    "MethodIdentityUndecidable",
    "MethodIdentityV1",
    "derive_method_identity",
    "record_method_identity",
]

#: ▲ Bump when the PAYLOAD's composition changes, so an older identity is never silently compared
#: against a newer one. It is inside the hash, so a bump re-mints every identity — which is correct:
#: a certificate issued against a different composition did not certify this.
METHOD_IDENTITY_VERSION = 1

_AUTHOR_ROLE = "formula.author"
_CRITIC_ROLE = "formula.critic"


class MethodIdentityUndecidable(RuntimeError):
    """The run's evidence cannot name one exact method identity, so no certificate can match it."""


class MethodIdentityConflict(RuntimeError):
    """The store already holds a DIFFERENT identity for this member, and it cannot be corrected.

    1102 is append-only, so first-writer-wins-silently would mean a sealing act proceeds believing
    the identity it just derived was recorded, while certificate matching for ever evaluates a hash
    that act never produced. A conflicting write on an append-only evidence table is a refusal,
    never a shrug.
    """


@dataclass(frozen=True, slots=True)
class MethodIdentityV1:
    """One member's exact authoring identity, and the payload that produced its hash."""

    authoring_method: str
    payload: dict[str, Any]

    @property
    def method_identity_hash(self) -> str:
        return jcs_sha256(self.payload)


def _versions(conn, authoring_run_id: str) -> dict[str, Any]:
    """The run's own version vector — schema, grammar, disposition, critic.

    Read from `formula_authoring_run` rather than from the deployed constants, for the reason this
    module exists: the constants describe the platform NOW, and the run happened when it happened.
    """
    row = conn.execute(
        "SELECT versions FROM formula_authoring_run WHERE authoring_run_id = %s",
        (authoring_run_id,)).fetchone()
    if row is None or not isinstance(row[0], dict):
        raise MethodIdentityUndecidable(
            f"authoring run {authoring_run_id} has no recorded version vector, so the grammar and "
            f"schema a certificate would cover cannot be named")
    versions = row[0]
    missing = [k for k in ("formula_schema", "operation_grammar") if k not in versions]
    if missing:
        raise MethodIdentityUndecidable(
            f"authoring run {authoring_run_id} records no {', '.join(missing)} version. A "
            f"certificate covers a grammar and a schema; an identity that omits them would match "
            f"a certificate issued for different ones")
    return {
        "formula_schema_version": versions["formula_schema"],
        "operation_grammar_version": versions["operation_grammar"],
        "disposition_policy_version": versions.get("disposition"),
        "critic_policy_version": versions.get("critic"),
    }


def _one(values: set[str], *, what: str, run_id: str) -> str:
    """Exactly one distinct value, or a refusal naming the ambiguity.

    ▲ A run that dispatched to two models is not "mostly" one of them. Picking either would mint an
    identity a certificate could match while half the work was done by something else.
    """
    if not values:
        raise MethodIdentityUndecidable(
            f"authoring run {run_id} records no {what}, so the identity a certificate would cover "
            f"cannot be named")
    if len(values) > 1:
        raise MethodIdentityUndecidable(
            f"authoring run {run_id} used {len(values)} different values for {what} "
            f"({sorted(values)!r}). One run, one method identity — a certificate cannot cover a run "
            f"whose own dispatches disagree about what authored it")
    return next(iter(values))


def _llm_identity(conn, authoring_run_id: str) -> dict[str, Any]:
    rows = conn.execute(
        "SELECT call_role, provider, model, provider_contract_hash, prompt_content_hash, "
        "       schema_content_hash "
        "  FROM llm_dispatch WHERE authoring_run_id = %s AND call_role = ANY(%s)",
        (authoring_run_id, [_AUTHOR_ROLE, _CRITIC_ROLE])).fetchall()

    by_role: dict[str, list[tuple]] = {_AUTHOR_ROLE: [], _CRITIC_ROLE: []}
    for row in rows:
        by_role[row[0]].append(row)

    for role in (_AUTHOR_ROLE, _CRITIC_ROLE):
        if not by_role[role]:
            raise MethodIdentityUndecidable(
                f"authoring run {authoring_run_id} has no {role} dispatches, so an LLM identity "
                f"cannot be derived from it. `derive_authoring_method` requires BOTH roles to call "
                f"a run LLM-authored; an identity built from one of them would describe half the "
                f"method a certificate has to cover")

    identity = {
        "identity_version": METHOD_IDENTITY_VERSION,
        "method": LLM_AUTHORED,
        # Provider and model span BOTH roles: a run whose critic ran on a different provider is a
        # run two certificates would have to cover, and one identity cannot say that.
        "provider": _one({r[1] for r in rows}, what="provider", run_id=authoring_run_id),
        "model": _one({r[2] for r in rows}, what="model", run_id=authoring_run_id),
    }
    for role, label in ((_AUTHOR_ROLE, "author"), (_CRITIC_ROLE, "critic")):
        role_rows = by_role[role]
        identity[f"{label}_contract_hash"] = _one(
            {r[3] for r in role_rows}, what=f"{label} contract hash", run_id=authoring_run_id)
        identity[f"{label}_prompt_hash"] = _one(
            {r[4] for r in role_rows}, what=f"{label} prompt hash", run_id=authoring_run_id)
        identity[f"{label}_schema_hash"] = _one(
            {r[5] for r in role_rows}, what=f"{label} schema hash", run_id=authoring_run_id)
    identity.update(_versions(conn, authoring_run_id))
    return identity


def _blueprint_identity(conn, authoring_run_id: str) -> dict[str, Any]:
    row = conn.execute(
        "SELECT payload FROM formula_authoring_trace_event "
        " WHERE authoring_run_id = %s AND stage = 'REVIEW_BYPASSED' "
        " ORDER BY seq DESC LIMIT 1", (authoring_run_id,)).fetchone()
    if row is None or not isinstance(row[0], dict):
        raise MethodIdentityUndecidable(
            f"authoring run {authoring_run_id} is a reviewed-blueprint run with no REVIEW_BYPASSED "
            f"payload, so the blueprint it stood on cannot be named")
    payload = row[0]
    for field in ("blueprint_revision", "expectation_hash"):
        if not str(payload.get(field, "")).strip():
            raise MethodIdentityUndecidable(
                f"authoring run {authoring_run_id} records a review bypass with no {field}. A "
                f"bypass names the exact blueprint it stood on, or the claim is unverifiable")

    identity = {
        "identity_version": METHOD_IDENTITY_VERSION,
        "method": REVIEWED_RECIPE_BLUEPRINT,
        "blueprint_revision": payload["blueprint_revision"],
        "expectation_hash": payload["expectation_hash"],
    }
    identity.update(_versions(conn, authoring_run_id))
    # ▲ `expectation_generation` is DELIBERATELY ABSENT until step 4 persists it.
    #
    # Parent 10 lists it, and it is load-bearing — two of the three reviewed recipes are Formula V1,
    # so "a reviewed expectation exists" does not identify a V3-producible method. But the trace
    # proves only the blueprint revision and the expectation hash. Deriving the generation from
    # today's registry would assert a fact about the run from a source that can move independently
    # of it, which is the one thing this module exists not to do. It joins the payload when
    # `formula_draft_authoring_plan` (1104) records it AS RESOLVED AT AUTHORING TIME — and that is a
    # payload change, so METHOD_IDENTITY_VERSION bumps with it.
    return identity


def derive_method_identity(
    conn, *, authoring_run_id: str, authoring_method: str,
) -> MethodIdentityV1:
    """The exact identity of the method that authored this run — reading only, writing nothing.

    Raises:
        MethodIdentityUndecidable: the run's evidence cannot name one identity. **Never a default**:
            a fallback would be applied exactly when the evidence is unclear, which is when matching
            the wrong certificate does the damage.
    """
    if authoring_method == LLM_AUTHORED:
        payload = _llm_identity(conn, authoring_run_id)
    elif authoring_method == REVIEWED_RECIPE_BLUEPRINT:
        payload = _blueprint_identity(conn, authoring_run_id)
    else:
        raise MethodIdentityUndecidable(
            f"unknown authoring method {authoring_method!r} for run {authoring_run_id}: an "
            f"identity can only be derived for a method this platform knows how to certify")
    return MethodIdentityV1(authoring_method=authoring_method, payload=payload)


def record_method_identity(
    conn, artifact_id: str, member_name: str, identity: MethodIdentityV1,
) -> None:
    """Append one member's method identity. Idempotent on the exact row.

    Written by the SAME sealing act that writes 1099's provenance, in the same transaction: an
    artifact whose members have provenance but no identity is one production can never evaluate,
    and it would look complete.
    """
    import json

    conn.execute(
        "INSERT INTO sealed_artifact_member_method_identity (artifact_id, member_name, "
        "authoring_method, method_identity_hash, method_identity_json) "
        "VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT (artifact_id, member_name) DO NOTHING",
        (artifact_id, member_name, identity.authoring_method, identity.method_identity_hash,
         json.dumps(identity.payload, sort_keys=True)))
    # ▲ INSERT-OR-VERIFY. "Idempotent" means the stored row IS this row — DO NOTHING alone is
    # first-writer-wins-silently, which after a METHOD_IDENTITY_VERSION bump would leave a sealing
    # act believing its freshly derived identity was recorded while the store keeps the old one,
    # and the append-only guard makes the divergence permanent. Same discipline as the ledger's
    # checksum check: skip only what is provably identical.
    stored = conn.execute(
        "SELECT method_identity_hash FROM sealed_artifact_member_method_identity "
        "WHERE artifact_id = %s AND member_name = %s", (artifact_id, member_name)).fetchone()
    if stored[0] != identity.method_identity_hash:
        raise MethodIdentityConflict(
            f"member {member_name!r} of artifact {artifact_id!r} already records method identity "
            f"{stored[0]}, and this act derived {identity.method_identity_hash}. The table is "
            f"append-only, so the stored identity cannot be corrected — the artifact must be "
            f"re-sealed rather than this divergence ignored")
