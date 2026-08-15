"""Dedicated fenced worker for durable recipe-formula shadow work.

**This worker authors BOTH expectation generations, and the work item's own declaration chooses.**
Each generation has a complete, separate chain and they share nothing but this file:

* ``formula-v1`` (the UNDECLARED shape — absence IS the v1 declaration, see ``recipe_egress``) →
  ``replay_authoring.run_authoring``, ``verify_frozen_configuration``,
  ``recipe_expectation_validator``, ``recipe_tool_runner``, ``formula_facts``. **Byte-frozen**:
  live work items were sealed against exactly these bytes.
* ``formula-v2`` → ``replay_authoring_v2.run_authoring_v2_replay``,
  ``verify_frozen_configuration_v2``, ``recipe_expectation_validator_v2``,
  ``recipe_tool_runner_v2``, ``formula_facts_v2``.

**A4 increment 2's ``V2_AUTHORING_UNAVAILABLE`` terminal is GONE, and its cause with it.** It
existed because A3 could only sibling the *non-production* ``formula/authoring.run_authoring``, so
a v2 work item reaching this worker would have been parsed by ``parse_proposal_v1`` and
terminalized ``invalid_formula → REJECTED`` — a durable, dishonest verdict about a recipe the
platform could not author at all. The replay-shaped v2 orchestrator is what removes that risk;
keeping the guard afterwards would refuse work the platform can now do (the same discipline D3
applied when it deleted D0's guards).

``EXPECTATION_SCHEMA_UNKNOWN`` stays, and it is not the same statement: a declaration THIS BUILD
has never heard of is a fact about us, never about the recipe, and it terminalizes before any
evaluation with ``authoring_axis="NOT_RUN"`` — not ``UNSUPPORTED``, which is a capability verdict
about a proposal that in this arm does not exist.
"""
from __future__ import annotations

import hashlib
from collections.abc import Mapping
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

import psycopg
from psycopg.rows import dict_row

from featuregen.config import get_settings
from featuregen.formula.audited import current_formula_generation_settings
from featuregen.formula.author import AUTHOR_INSTRUCTION, AUTHOR_PROMPT_ID
from featuregen.formula.control import (
    LeaseFence,
    LeaseFenceLost,
    RecoveryRequiresReconciliation,
)
from featuregen.formula.frozen_configuration import (
    ConfigurationDrifted,
    load_frozen_configuration_json,
    verify_frozen_configuration,
    verify_frozen_configuration_v2,
)
from featuregen.formula.recipe_authoring import (
    FrozenRecipeReadContext,
    recipe_expectation_validator,
    recipe_expectation_validator_v2,
    recipe_tool_runner,
    recipe_tool_runner_v2,
)
from featuregen.formula.recipe_egress import (
    FORMULA_EXPECTATION_SCHEMA_V2,
    RecipeEgressViolation,
    validate_recipe_provider_payload,
)
from featuregen.formula.replay_authoring import run_authoring
from featuregen.formula.replay_authoring_v2 import run_authoring_v2_replay
from featuregen.formula.turns import AuthoringIntent
from featuregen.identity.current_principal import (
    PrincipalResolutionStatus,
    resolve_current_principal,
)
from featuregen.intake.llm import current_llm_client
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload.dispatch_audit import formula_dispatches_reconciled
from featuregen.overlay.upload.feature_metadata_snapshot import (
    compare_snapshot_to_current,
)
from featuregen.overlay.upload.recipe_formula_authority import (
    verify_formula_authority_envelope,
)
from featuregen.overlay.upload.recipe_formula_shadow import (
    finalize_manifest,
    verify_work_item_payload,
    write_observation,
)
from featuregen.runtime.queue import (
    claim_recipe_formula_shadow,
    complete_recipe_formula_shadow,
    fail_recipe_formula_shadow,
    renew_recipe_formula_shadow,
)

#: The generation a payload that DECLARES nothing is: the v1 shape carries no version key and never
#: did, and every work item written before A4 is exactly that shape.
DEFAULT_EXPECTATION_SCHEMA = "formula-v1"
#: The expectation generations this worker can author, each through its own complete chain.
AUTHORABLE_EXPECTATION_SCHEMAS = frozenset(
    {DEFAULT_EXPECTATION_SCHEMA, FORMULA_EXPECTATION_SCHEMA_V2})
#: A declaration this build has never heard of. A statement about US, never about the recipe.
EXPECTATION_SCHEMA_UNKNOWN = "EXPECTATION_SCHEMA_UNKNOWN"


@dataclass(frozen=True, slots=True)
class FormulaShadowWorkerOutcome:
    status: str
    work_item_id: str | None = None
    observation_id: str | None = None


def declared_expectation_schema(row: Mapping[str, Any]) -> str:
    """The expectation generation this work item DECLARES, read from its frozen provider input.

    Absence is ``formula-v1``: the v1 payload shape carries no version key and never did (see
    ``recipe_egress``), and every work item written before A4 is exactly that shape.
    """
    provider_input = row.get("provider_input_json")
    expectation = (provider_input.get("formula_expectation")
                   if isinstance(provider_input, Mapping) else None)
    declared = (expectation.get("formula_schema_version")
                if isinstance(expectation, Mapping) else None)
    return DEFAULT_EXPECTATION_SCHEMA if declared is None else str(declared)


def _plain(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _plain(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, tuple):
        return [_plain(item) for item in value]
    if isinstance(value, list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _formula_refs(expectation: dict) -> frozenset[str]:
    """Every ref the frozen read context must serve for this expectation.

    ``second_operand_ref`` is a v2 key and a v1 expectation never carries it, so this stays
    byte-identical for v1 — but a v2 body that HAS one (``date_diff_avg``,
    ``effective_at_cutoff``) needs its second column's governed facts as much as its first, and
    without it the tool runner would refuse to read it and the facts reader would resolve it to
    nothing (the forward gap A4 increment 3 recorded, now reachable and closed)."""
    refs = set(expectation.get("grain_key_refs") or ())
    for expression in expectation.get("expressions") or ():
        for key in ("operand_ref", "second_operand_ref", "event_time_ref"):
            ref = expression.get(key)
            if isinstance(ref, str):
                refs.add(ref)
    return frozenset(refs)


def _current_read_scope_hash(conn, snapshot_id: str, roles) -> str:
    """Recompute the CATALOG read scope this snapshot pins, to compare with the hash frozen at
    capture (``gate1``'s ``canonical_hash({"refs": …, "roles": …})`` over the candidates'
    ``(catalog_source, object_ref)`` pairs).

    ``generation_semantic_context`` items are EXCLUDED, and that is a correction, not a loophole.
    SE-2 seals one such item per catalog run — an identity PIN for the frozen Layer-A context,
    whose ``graph_ref`` is a read-scope KEY (``context:<…>``) and not a catalog object at all. It
    was never in the population ``gate1`` hashed, so including it here made the comparison
    unsatisfiable: on any run that seals a semantic context — which is the live path — this hash
    could never equal the frozen one and EVERY work item terminalized
    ``AUTHORIZATION_SCOPE_CHANGED`` without authoring anything.

    Nothing is unverified as a result: the context pin has its own freshness comparator
    (``compare_generation_context_item``, D6's dispatch) and is checked by the
    ``compare_snapshot_to_current`` call a few lines below this one.

    ``IS DISTINCT FROM`` rather than ``<>`` is defence in depth only: ``item_kind`` is NOT NULL
    today, so the exclusion is total either way, and the test that pins that constraint is where
    the assumption is written down.
    """
    from featuregen.overlay.upload.generation_semantic_context import (
        GENERATION_CONTEXT_ITEM_KIND,
    )

    refs = conn.execute(
        "SELECT DISTINCT catalog_source,graph_ref "
        "FROM catalog_metadata_snapshot_item WHERE snapshot_id=%s "
        "AND item_kind IS DISTINCT FROM %s "
        "ORDER BY catalog_source,graph_ref",
        (snapshot_id, GENERATION_CONTEXT_ITEM_KIND),
    ).fetchall()
    return canonical_hash({
        "refs": [[source, ref] for source, ref in refs],
        "roles": sorted(str(role) for role in roles),
    })


def _terminalize(conn, claim, row: dict, *, axes: dict, result: dict | None = None,
                 authoring_run_id: str | None = None) -> FormulaShadowWorkerOutcome:
    observation_id = f"rfo_{row['idempotency_key'][:24]}"
    with conn.transaction():
        current = conn.execute(
            "SELECT 1 FROM queue WHERE id=%s AND status='leased' AND lease_owner=%s "
            "AND lease_fence=%s AND lease_expires_at > now() FOR UPDATE",
            (claim.id, claim.lease_owner, claim.lease_fence),
        ).fetchone()
        if current is None:
            return FormulaShadowWorkerOutcome("stale_lease", row["work_item_id"])
        write_observation(
            conn,
            observation_id=observation_id,
            idempotency_key=row["idempotency_key"],
            capture_entry_id=row["capture_entry_id"],
            generation_run_id=row["generation_run_id"],
            intent_id=row["intent_id"],
            considered_revision_id=row["considered_revision_id"],
            considered_content_hash=row["considered_content_hash"],
            metadata_snapshot_id=row["metadata_snapshot_id"],
            metadata_snapshot_content_hash=row["metadata_snapshot_content_hash"],
            recipe_id=row["recipe_id"],
            recipe_candidate_key=row["recipe_candidate_key"],
            recipe_expectation=row["recipe_expectation_json"],
            recipe_expectation_hash=row["recipe_expectation_hash"],
            binding_envelope=row["binding_envelope_json"],
            binding_envelope_hash=row["binding_envelope_hash"],
            provider_input=row["provider_input_json"],
            provider_input_hash=row["provider_input_hash"],
            frozen_configuration=row["frozen_configuration_json"],
            frozen_configuration_hash=row["frozen_configuration_hash"],
            request_identity=row["request_identity_json"],
            request_read_scope_hash=row["request_read_scope_hash"],
            capture_axis="CAPTURED",
            authoring_run_id=authoring_run_id,
            authoring_result=result,
            **axes,
        )
        if not complete_recipe_formula_shadow(conn, claim):
            raise RuntimeError("formula queue lease fence changed during terminal write")
        finalize_manifest(conn, row["generation_run_id"])
    return FormulaShadowWorkerOutcome(
        "completed", row["work_item_id"], observation_id)


def _dispatch_count(conn, authoring_run_id: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM llm_dispatch WHERE authoring_run_id=%s",
        (authoring_run_id,),
    ).fetchone()[0]


def process_recipe_formula_shadow_once(
    conn,
    *,
    owner: str,
    now: datetime | None = None,
    author_client=None,
    critic_client=None,
    identity_resolver=None,
) -> FormulaShadowWorkerOutcome:
    """Claim, reauthorize, drift-check, author and terminalize one immutable work item."""
    claim = claim_recipe_formula_shadow(conn, owner=owner, lease_seconds=900)
    if claim is None:
        return FormulaShadowWorkerOutcome("idle")
    work_item_id = claim.payload.get("work_item_id")
    try:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT * FROM recipe_formula_shadow_work_item WHERE work_item_id=%s",
                (work_item_id,),
            )
            row = cur.fetchone()
        if row is None:
            fail_recipe_formula_shadow(
                conn, claim, error="work item missing", permanent=True)
            return FormulaShadowWorkerOutcome("dead", str(work_item_id))
        integrity_failure = verify_work_item_payload(row)
        if integrity_failure is not None:
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "NOT_EVALUATED",
                "authority_axis": "NOT_EVALUATED",
                "drift_axis": "NOT_EVALUATED",
                "configuration_axis": "NOT_EVALUATED",
                "delivery_axis": "NOT_DISPATCHED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": integrity_failure,
            })
        existing = conn.execute(
            "SELECT observation_id FROM recipe_formula_shadow_observation "
            "WHERE capture_entry_id=%s",
            (row["capture_entry_id"],),
        ).fetchone()
        if existing is not None:
            complete_recipe_formula_shadow(conn, claim)
            finalize_manifest(conn, row["generation_run_id"])
            return FormulaShadowWorkerOutcome(
                "already_completed", row["work_item_id"], existing[0])

        declared_schema = declared_expectation_schema(row)
        if declared_schema not in AUTHORABLE_EXPECTATION_SCHEMAS:
            # Every axis stays NOT_EVALUATED, exactly like the integrity terminal: we stopped
            # before evaluating anything. authoring_axis is NOT_RUN, not UNSUPPORTED — the
            # latter is a capability verdict about a PROPOSAL, and no proposal exists.
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "NOT_EVALUATED",
                "authority_axis": "NOT_EVALUATED",
                "drift_axis": "NOT_EVALUATED",
                "configuration_axis": "NOT_EVALUATED",
                "delivery_axis": "NOT_DISPATCHED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": EXPECTATION_SCHEMA_UNKNOWN,
            })
        authors_v2 = declared_schema == FORMULA_EXPECTATION_SCHEMA_V2

        frozen_identity = row["request_identity_json"]
        principal = resolve_current_principal(
            conn,
            str(frozen_identity["subject"]),
            frozen_identity.get("tenant"),
            now or datetime.now(UTC),
            resolver=identity_resolver,
        )
        if (
            principal.status is not PrincipalResolutionStatus.CURRENT
            or principal.principal is None
        ):
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": f"AUTHORIZATION_{principal.status.value.upper()}",
                "authority_axis": "VERIFIED_AT_CAPTURE",
                "drift_axis": "NOT_EVALUATED",
                "configuration_axis": "NOT_EVALUATED",
                "delivery_axis": "NOT_DISPATCHED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": "OK",
            })
        snapshot_id = row["metadata_snapshot_id"]
        if (
            not snapshot_id
            or _current_read_scope_hash(
                conn, snapshot_id, principal.principal.role_claims
            ) != row["request_read_scope_hash"]
        ):
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "AUTHORIZATION_SCOPE_CHANGED",
                "authority_axis": "VERIFIED_AT_CAPTURE",
                "drift_axis": "NOT_EVALUATED",
                "configuration_axis": "NOT_EVALUATED",
                "delivery_axis": "NOT_DISPATCHED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": "OK",
            })
        freshness = compare_snapshot_to_current(conn, snapshot_id)
        if (
            freshness.status != "current"
            or freshness.current_content_hash != row["metadata_snapshot_content_hash"]
        ):
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "AUTHORIZED_CURRENT",
                "authority_axis": "VERIFIED_AT_CAPTURE",
                "drift_axis": f"DRIFT_{freshness.status.upper()}",
                "configuration_axis": "NOT_EVALUATED",
                "delivery_axis": "NOT_DISPATCHED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": "OK",
            })
        authority_drift = verify_formula_authority_envelope(
            conn, row["binding_envelope_json"])
        if authority_drift is not None:
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "AUTHORIZED_CURRENT",
                "authority_axis": authority_drift,
                "drift_axis": "AUTHORITY_DRIFTED",
                "configuration_axis": "NOT_EVALUATED",
                "delivery_axis": "NOT_DISPATCHED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": "OK",
            })
        try:
            configuration = load_frozen_configuration_json(
                row["frozen_configuration_json"])
            # The ENVELOPE is generation-neutral (``load_…`` only re-hashes stored bytes); the
            # MATERIAL inside it is not. A v2 work item frozen under the v1 author identity is
            # DRIFT, and verifying it as v1 would author a v2 formula under a prompt no v2 run
            # uses — A3 made the two identities distinct for exactly this.
            if authors_v2:
                verify_frozen_configuration_v2(
                    configuration,
                    generation_settings=current_formula_generation_settings(),
                )
            else:
                verify_frozen_configuration(
                    configuration,
                    generation_settings=current_formula_generation_settings(),
                    author_instruction=AUTHOR_INSTRUCTION,
                    author_prompt_id=AUTHOR_PROMPT_ID,
                )
        except ConfigurationDrifted:
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "AUTHORIZED_CURRENT",
                "authority_axis": "VERIFIED_AT_CAPTURE",
                "drift_axis": "CURRENT",
                "configuration_axis": "DRIFTED",
                "delivery_axis": "NOT_DISPATCHED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": "OK",
            })
        except (TypeError, ValueError, KeyError):
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "AUTHORIZED_CURRENT",
                "authority_axis": "VERIFIED_AT_CAPTURE",
                "drift_axis": "CURRENT",
                "configuration_axis": "UNVERIFIABLE",
                "delivery_axis": "NOT_DISPATCHED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": "FROZEN_CONFIGURATION_INVALID",
            })
        provider_input = row["provider_input_json"]
        try:
            validate_recipe_provider_payload(provider_input)
        except RecipeEgressViolation:
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "AUTHORIZED_CURRENT",
                "authority_axis": "VERIFIED_AT_CAPTURE",
                "drift_axis": "CURRENT",
                "configuration_axis": "CURRENT",
                "delivery_axis": "EGRESS_REJECTED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": "OK",
            })
        expectation = provider_input["formula_expectation"]
        try:
            frozen_reads = FrozenRecipeReadContext.load(
                conn, snapshot_id, _formula_refs(expectation))
        except (TypeError, ValueError, KeyError):
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "AUTHORIZED_CURRENT",
                "authority_axis": "SNAPSHOT_INPUT_INVALID",
                "drift_axis": "CURRENT",
                "configuration_axis": "CURRENT",
                "delivery_axis": "NOT_DISPATCHED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": "OK",
            })
        if not get_settings().dsn:
            # Formula shadow is an enablement harness, not a best-effort development path.
            # Deterministic preflight above may still classify bad frozen input, but no provider
            # call is allowed without an independently committed pre-dispatch record.
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "AUTHORIZED_CURRENT",
                "authority_axis": "VERIFIED_AT_CAPTURE",
                "drift_axis": "CURRENT",
                "configuration_axis": "CURRENT",
                "delivery_axis": "NOT_DISPATCHED",
                "authoring_axis": "NOT_RUN",
                "technical_axis": "AUDIT_STORE_UNAVAILABLE",
            })
        deterministic_run_id = "far_" + hashlib.sha256(
            row["work_item_id"].encode()).hexdigest()[:24]
        client = author_client or current_llm_client()
        critic = critic_client or client
        intent = AuthoringIntent(
            name=row["recipe_id"],
            hypothesis=provider_input["hypothesis"],
            target_entity=provider_input["target_entity"],
            target_grain_keys=tuple(expectation["grain_key_refs"]),
            recipe_authoring_context=provider_input,
        )

        def renew_lease() -> None:
            dsn = get_settings().dsn
            if not dsn:
                raise LeaseFenceLost("formula queue lease cannot be renewed durably")
            try:
                with psycopg.connect(dsn) as lease_conn:
                    renewed = renew_recipe_formula_shadow(
                        lease_conn, claim, lease_seconds=900)
            except Exception as exc:
                raise LeaseFenceLost(
                    "formula queue lease renewal is unavailable") from exc
            if not renewed:
                raise LeaseFenceLost("formula queue lease fence changed")

        fence = LeaseFence(
            queue_id=claim.id,
            lease_owner=claim.lease_owner,
            lease_fence=claim.lease_fence,
        )
        if authors_v2:
            # Every seam is the v2 one, and the pairing is the point: a v2 proposal validated by
            # the v1 validator, or resolved over a body-path-keyed fact bundle, would produce a
            # confident verdict out of the wrong evidence.
            result = run_authoring_v2_replay(
                conn,
                intent,
                client,
                critic,
                roles=principal.principal.role_claims,
                actor=principal.principal,
                frozen_configuration=configuration,
                proposal_validator=recipe_expectation_validator_v2(expectation),
                tool_runner=recipe_tool_runner_v2(
                    _formula_refs(expectation), frozen_context=frozen_reads),
                authoring_run_id=deterministic_run_id,
                facts_reader=frozen_reads.formula_facts_v2,
                critic_metadata_loader=frozen_reads.get_column_metadata,
                progress_callback=renew_lease,
                lease_fence=fence,
            )
        else:
            result = run_authoring(
                conn,
                intent,
                client,
                critic,
                roles=principal.principal.role_claims,
                actor=principal.principal,
                frozen_configuration=configuration,
                proposal_validator=recipe_expectation_validator(expectation),
                tool_runner=recipe_tool_runner(
                    _formula_refs(expectation), frozen_context=frozen_reads),
                authoring_run_id=deterministic_run_id,
                facts_reader=frozen_reads.formula_facts,
                critic_metadata_loader=frozen_reads.get_column_metadata,
                progress_callback=renew_lease,
                lease_fence=fence,
            )
        result_json = _plain(result)
        if not formula_dispatches_reconciled(conn, deterministic_run_id):
            dispatch_count = _dispatch_count(conn, deterministic_run_id)
            return _terminalize(conn, claim, row, axes={
                "authorization_axis": "AUTHORIZED_CURRENT",
                "authority_axis": "VERIFIED_AT_CAPTURE",
                "drift_axis": "CURRENT",
                "configuration_axis": "CURRENT",
                "delivery_axis": (
                    "PRIOR_DISPATCH_UNRECONCILED"
                    if dispatch_count else "NOT_DISPATCHED"),
                "authoring_axis": "TECHNICAL_FAILURE",
                "technical_axis": (
                    "DISPATCH_RECONCILIATION_FAILED"
                    if dispatch_count else result.technical_status.upper()),
            }, result=result_json, authoring_run_id=result.authoring_run_id)
        return _terminalize(conn, claim, row, axes={
            "authorization_axis": "AUTHORIZED_CURRENT",
            "authority_axis": "VERIFIED_AT_CAPTURE",
            "drift_axis": "CURRENT",
            "configuration_axis": "CURRENT",
            "delivery_axis": "DISPATCHED_AUDITED",
            "authoring_axis": result.authoring_disposition,
            "technical_axis": result.technical_status.upper(),
        }, result=result_json, authoring_run_id=result.authoring_run_id)
    except LeaseFenceLost:
        return FormulaShadowWorkerOutcome("stale_lease", str(work_item_id))
    except RecoveryRequiresReconciliation:
        return _terminalize(conn, claim, row, axes={
            "authorization_axis": "AUTHORIZED_CURRENT",
            "authority_axis": "VERIFIED_AT_CAPTURE",
            "drift_axis": "CURRENT",
            "configuration_axis": "CURRENT",
            "delivery_axis": "PRIOR_DISPATCH_UNRECONCILED",
            "authoring_axis": "TECHNICAL_FAILURE",
            "technical_axis": "RECOVERY_REQUIRES_RECONCILIATION",
        }, authoring_run_id=(
            "far_" + hashlib.sha256(str(work_item_id).encode()).hexdigest()[:24]
        ))
    except Exception as exc:
        fail_recipe_formula_shadow(
            conn,
            claim,
            error=f"{type(exc).__name__}: {exc}",
            permanent=False,
        )
        return FormulaShadowWorkerOutcome("retryable", str(work_item_id))
