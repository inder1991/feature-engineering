"""Dedicated fenced worker for durable recipe-formula shadow work."""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from psycopg.rows import dict_row

from featuregen.config import get_settings
from featuregen.formula.audited import current_formula_generation_settings
from featuregen.formula.author import AUTHOR_INSTRUCTION, AUTHOR_PROMPT_ID
from featuregen.formula.authoring import run_authoring
from featuregen.formula.frozen_configuration import (
    ConfigurationDrifted,
    load_frozen_configuration_json,
    verify_frozen_configuration,
)
from featuregen.formula.recipe_authoring import (
    FrozenRecipeReadContext,
    recipe_expectation_validator,
    recipe_tool_runner,
)
from featuregen.formula.recipe_egress import (
    RecipeEgressViolation,
    validate_recipe_provider_payload,
)
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


@dataclass(frozen=True, slots=True)
class FormulaShadowWorkerOutcome:
    status: str
    work_item_id: str | None = None
    observation_id: str | None = None


class LeaseFenceLost(RuntimeError):
    """The worker no longer owns the fenced lease and must stop before dispatch."""


def _plain(value: Any) -> Any:
    if is_dataclass(value):
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
    refs = set(expectation.get("grain_key_refs") or ())
    for expression in expectation.get("expressions") or ():
        for key in ("operand_ref", "event_time_ref"):
            ref = expression.get(key)
            if isinstance(ref, str):
                refs.add(ref)
    return frozenset(refs)


def _current_read_scope_hash(conn, snapshot_id: str, roles) -> str:
    refs = conn.execute(
        "SELECT DISTINCT catalog_source,graph_ref "
        "FROM catalog_metadata_snapshot_item WHERE snapshot_id=%s "
        "ORDER BY catalog_source,graph_ref",
        (snapshot_id,),
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
            "AND lease_fence=%s FOR UPDATE",
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


def _terminal_authoring_event(conn, authoring_run_id: str) -> tuple[str, dict] | None:
    row = conn.execute(
        "SELECT kind,payload FROM formula_authoring_trace_event "
        "WHERE authoring_run_id=%s AND kind IN ('completed','failed')",
        (authoring_run_id,),
    ).fetchone()
    if row is None or not isinstance(row[1], dict):
        return None
    return row[0], row[1]


def _trace_event_count(conn, authoring_run_id: str) -> int:
    return conn.execute(
        "SELECT count(*) FROM formula_authoring_trace_event WHERE authoring_run_id=%s",
        (authoring_run_id,),
    ).fetchone()[0]


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
        if conn.execute(
            "SELECT 1 FROM formula_authoring_run WHERE authoring_run_id=%s",
            (deterministic_run_id,),
        ).fetchone() is not None:
            terminal = _terminal_authoring_event(conn, deterministic_run_id)
            if terminal is not None and formula_dispatches_reconciled(
                conn, deterministic_run_id
            ):
                _kind, terminal_result = terminal
                return _terminalize(conn, claim, row, axes={
                    "authorization_axis": "AUTHORIZED_CURRENT",
                    "authority_axis": "VERIFIED_AT_CAPTURE",
                    "drift_axis": "CURRENT",
                    "configuration_axis": "CURRENT",
                    "delivery_axis": "DISPATCHED_AUDITED",
                    "authoring_axis": terminal_result["authoring_disposition"],
                    "technical_axis": str(
                        terminal_result["technical_status"]).upper(),
                }, result=terminal_result, authoring_run_id=deterministic_run_id)
            if (
                terminal is not None
                or _trace_event_count(conn, deterministic_run_id) > 0
                or _dispatch_count(conn, deterministic_run_id) > 0
            ):
                return _terminalize(conn, claim, row, axes={
                    "authorization_axis": "AUTHORIZED_CURRENT",
                    "authority_axis": "VERIFIED_AT_CAPTURE",
                    "drift_axis": "CURRENT",
                    "configuration_axis": "CURRENT",
                    "delivery_axis": "PRIOR_DISPATCH_UNRECONCILED",
                    "authoring_axis": "TECHNICAL_FAILURE",
                    "technical_axis": "RECOVERY_REQUIRES_RECONCILIATION",
                }, authoring_run_id=deterministic_run_id)
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
            if not renew_recipe_formula_shadow(conn, claim, lease_seconds=900):
                raise LeaseFenceLost("formula queue lease fence changed")

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
    except Exception as exc:
        fail_recipe_formula_shadow(
            conn,
            claim,
            error=f"{type(exc).__name__}: {exc}",
            permanent=False,
        )
        return FormulaShadowWorkerOutcome("retryable", str(work_item_id))
