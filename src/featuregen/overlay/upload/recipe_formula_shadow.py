"""Durable expected-set, ranking manifest, observations, and reconciliation for formula shadow."""
from __future__ import annotations

import hashlib
import json
import logging
import os
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from typing import Any

from psycopg.types.json import Jsonb

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.formula.audited import current_formula_generation_settings
from featuregen.formula.author import AUTHOR_INSTRUCTION, AUTHOR_PROMPT_ID
from featuregen.formula.frozen_configuration import (
    freeze_current_configuration,
    frozen_configuration_json,
)
from featuregen.formula.recipe_egress import (
    RecipeEgressViolation,
    build_recipe_authoring_egress,
)
from featuregen.overlay.upload.contract.scope_records import (
    GenerationInputUnavailable,
    generation_input_for_run,
)
from featuregen.overlay.upload.recipe_formula_authority import (
    FormulaAuthorityRejection,
    build_formula_authority_envelope,
)
from featuregen.overlay.upload.recipe_formula_contracts import (
    RecipeFormulaPreflightError,
    bind_formula_expectation,
)
from featuregen.overlay.upload.recipe_formula_expectations import (
    RECIPE_FORMULA_EXPECTATIONS,
)
from featuregen.runtime.outbox import (
    OutboxMessage,
    insert_outbox_message_checked,
)

RECIPE_FORMULA_SHADOW_TOPIC = "recipe_formula_shadow.requested.v1"
RECIPE_FORMULA_SHADOW_HANDLER = "recipe_formula_shadow.author.v1"
INVOCATION_PREDICATE_VERSION = "recipe-formula-invocation-v1"
CAPTURE_POLICY_VERSION = "recipe-formula-capture-v1"
MAX_RECIPE_FORMULA_CAPTURES_PER_RUN = 12
logger = logging.getLogger(__name__)


class ShadowIntegrityError(RuntimeError):
    """A repeated shadow identity disagreed with its immutable stored material."""


@dataclass(frozen=True, slots=True)
class RankedCaptureEntryV1:
    capture_entry_id: str
    recipe_id: str
    canonical_rank: int
    selected_for_initial_view: bool
    rank_reasons: tuple[str, ...]
    initial_view_reasons: tuple[str, ...]
    recipe_candidate_key: str | None
    candidate_resolution: str
    capture_required: bool
    capture_reason: str


@dataclass(frozen=True, slots=True)
class ShadowReconciliation:
    generation_run_id: str
    status: str
    expected_observations: int
    actual_observations: int
    reason: str | None
    reconciliation_hash: str


def recipe_formula_shadow_enabled() -> bool:
    return os.environ.get("FEATUREGEN_RECIPE_FORMULA_SHADOW", "0") == "1"


def canonical_json(value: Any) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode()).hexdigest()


def capture_entry_id(
    generation_run_id: str,
    ranking_version: str,
    canonical_rank: int,
    recipe_id: str,
) -> str:
    return content_hash({
        "generation_run_id": generation_run_id,
        "ranking_version": ranking_version,
        "canonical_rank": canonical_rank,
        "recipe_id": recipe_id,
    })


def _checked_existing(existing, expected: tuple, label: str) -> None:
    if existing != expected:
        raise ShadowIntegrityError(f"{label} conflicts with its stored identity")


def _work_item_material(
    *,
    work_item_id: str,
    idempotency_key: str,
    capture_entry_id: str,
    generation_run_id: str,
    intent_id: str,
    considered_revision_id: str,
    considered_content_hash: str,
    metadata_snapshot_id: str | None,
    metadata_snapshot_content_hash: str | None,
    recipe_id: str,
    recipe_candidate_key: str,
    recipe_expectation: dict,
    recipe_expectation_hash: str,
    binding_envelope: dict,
    binding_envelope_hash: str,
    provider_input: dict,
    provider_input_hash: str,
    frozen_configuration: dict,
    frozen_configuration_hash: str,
    request_identity: dict,
    request_read_scope_hash: str | None,
) -> dict[str, Any]:
    return {
        "work_item_id": work_item_id,
        "idempotency_key": idempotency_key,
        "capture_entry_id": capture_entry_id,
        "generation_run_id": generation_run_id,
        "intent_id": intent_id,
        "considered_revision_id": considered_revision_id,
        "considered_content_hash": considered_content_hash,
        "metadata_snapshot_id": metadata_snapshot_id,
        "metadata_snapshot_content_hash": metadata_snapshot_content_hash,
        "recipe_id": recipe_id,
        "recipe_candidate_key": recipe_candidate_key,
        "recipe_expectation": recipe_expectation,
        "recipe_expectation_hash": recipe_expectation_hash,
        "binding_envelope": binding_envelope,
        "binding_envelope_hash": binding_envelope_hash,
        "provider_input": provider_input,
        "provider_input_hash": provider_input_hash,
        "frozen_configuration": frozen_configuration,
        "frozen_configuration_hash": frozen_configuration_hash,
        "request_identity": request_identity,
        "request_read_scope_hash": request_read_scope_hash,
    }


def verify_work_item_payload(row: Mapping[str, Any]) -> str | None:
    """Return a stable integrity failure code, or ``None`` for an exact frozen payload."""
    expectation = row.get("recipe_expectation_json")
    binding = row.get("binding_envelope_json")
    provider_input = row.get("provider_input_json")
    configuration = row.get("frozen_configuration_json")
    identity = row.get("request_identity_json")
    if not isinstance(expectation, dict):
        return "WORK_ITEM_SHAPE_INVALID"
    if not isinstance(binding, dict):
        return "WORK_ITEM_SHAPE_INVALID"
    if not isinstance(provider_input, dict):
        return "WORK_ITEM_SHAPE_INVALID"
    if not isinstance(configuration, dict):
        return "WORK_ITEM_SHAPE_INVALID"
    if not isinstance(identity, dict):
        return "WORK_ITEM_SHAPE_INVALID"
    if content_hash(expectation) != row.get("recipe_expectation_hash"):
        return "RECIPE_EXPECTATION_HASH_MISMATCH"
    if content_hash(binding) != row.get("binding_envelope_hash"):
        return "BINDING_ENVELOPE_HASH_MISMATCH"
    if content_hash(provider_input) != row.get("provider_input_hash"):
        return "PROVIDER_INPUT_HASH_MISMATCH"
    if configuration.get("configuration_hash") != row.get("frozen_configuration_hash"):
        return "FROZEN_CONFIGURATION_HASH_MISMATCH"
    material = _work_item_material(
        work_item_id=row["work_item_id"],
        idempotency_key=row["idempotency_key"],
        capture_entry_id=row["capture_entry_id"],
        generation_run_id=row["generation_run_id"],
        intent_id=row["intent_id"],
        considered_revision_id=row["considered_revision_id"],
        considered_content_hash=row["considered_content_hash"],
        metadata_snapshot_id=row.get("metadata_snapshot_id"),
        metadata_snapshot_content_hash=row.get("metadata_snapshot_content_hash"),
        recipe_id=row["recipe_id"],
        recipe_candidate_key=row["recipe_candidate_key"],
        recipe_expectation=expectation,
        recipe_expectation_hash=row["recipe_expectation_hash"],
        binding_envelope=binding,
        binding_envelope_hash=row["binding_envelope_hash"],
        provider_input=provider_input,
        provider_input_hash=row["provider_input_hash"],
        frozen_configuration=configuration,
        frozen_configuration_hash=row["frozen_configuration_hash"],
        request_identity=identity,
        request_read_scope_hash=row.get("request_read_scope_hash"),
    )
    if content_hash(material) != row.get("payload_hash"):
        return "WORK_ITEM_PAYLOAD_HASH_MISMATCH"
    return None


def _observation_material(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "observation_id": row.get("observation_id"),
        "idempotency_key": row.get("idempotency_key"),
        "capture_entry_id": row.get("capture_entry_id"),
        "generation_run_id": row.get("generation_run_id"),
        "intent_id": row.get("intent_id"),
        "considered_revision_id": row.get("considered_revision_id"),
        "considered_content_hash": row.get("considered_content_hash"),
        "metadata_snapshot_id": row.get("metadata_snapshot_id"),
        "metadata_snapshot_content_hash": row.get(
            "metadata_snapshot_content_hash"
        ),
        "recipe_id": row.get("recipe_id"),
        "recipe_candidate_key": row.get("recipe_candidate_key"),
        "recipe_expectation": row.get("recipe_expectation_json"),
        "recipe_expectation_hash": row.get("recipe_expectation_hash"),
        "binding_envelope": row.get("binding_envelope_json"),
        "binding_envelope_hash": row.get("binding_envelope_hash"),
        "provider_input": row.get("provider_input_json"),
        "provider_input_hash": row.get("provider_input_hash"),
        "frozen_configuration": row.get("frozen_configuration_json"),
        "frozen_configuration_hash": row.get("frozen_configuration_hash"),
        "request_identity": row.get("request_identity_json"),
        "request_read_scope_hash": row.get("request_read_scope_hash"),
        "capture_axis": row.get("capture_axis"),
        "authority_axis": row.get("authority_axis"),
        "authorization_axis": row.get("authorization_axis"),
        "drift_axis": row.get("drift_axis"),
        "configuration_axis": row.get("configuration_axis"),
        "delivery_axis": row.get("delivery_axis"),
        "authoring_axis": row.get("authoring_axis"),
        "technical_axis": row.get("technical_axis"),
        "authoring_run_id": row.get("authoring_run_id"),
        "authoring_result": row.get("authoring_result_json"),
    }


def verify_observation_payload(row: Mapping[str, Any]) -> str | None:
    """Return a stable integrity failure code for an observation, if any."""
    nested_payloads = (
        ("recipe_expectation_json", "recipe_expectation_hash",
         "OBSERVATION_RECIPE_EXPECTATION_HASH_MISMATCH"),
        ("binding_envelope_json", "binding_envelope_hash",
         "OBSERVATION_BINDING_ENVELOPE_HASH_MISMATCH"),
        ("provider_input_json", "provider_input_hash",
         "OBSERVATION_PROVIDER_INPUT_HASH_MISMATCH"),
    )
    for payload_key, hash_key, reason in nested_payloads:
        payload = row.get(payload_key)
        expected_hash = row.get(hash_key)
        if payload is None and expected_hash is None:
            continue
        if not isinstance(payload, dict) or content_hash(payload) != expected_hash:
            return reason
    configuration = row.get("frozen_configuration_json")
    configuration_hash = row.get("frozen_configuration_hash")
    if configuration is not None or configuration_hash is not None:
        if (
            not isinstance(configuration, dict)
            or configuration.get("configuration_hash") != configuration_hash
        ):
            return "OBSERVATION_FROZEN_CONFIGURATION_HASH_MISMATCH"
    if content_hash(_observation_material(row)) != row.get("payload_hash"):
        return "OBSERVATION_PAYLOAD_HASH_MISMATCH"
    return None


def verify_expected_run_payload(row: Mapping[str, Any]) -> str | None:
    """Verify the independently declared shadow population identity."""
    material = {
        "generation_run_id": row.get("generation_run_id"),
        "intent_id": row.get("intent_id"),
        "confirmed_scope_id": row.get("confirmed_scope_id"),
        "considered_revision_id": row.get("considered_revision_id"),
        "considered_content_hash": row.get("considered_content_hash"),
        "shadow_flag": row.get("shadow_flag"),
        "ranking_flag": row.get("ranking_flag"),
        "invocation_predicate_version": row.get("invocation_predicate_version"),
        "capture_policy_version": row.get("capture_policy_version"),
        "expected_manifest_id": row.get("expected_manifest_id"),
    }
    if content_hash(material) != row.get("declaration_hash"):
        return "EXPECTED_RUN_DECLARATION_HASH_MISMATCH"
    return None


def verify_manifest_payload(row: Mapping[str, Any]) -> str | None:
    """Verify ranking, capture identities, counts, manifest hash and terminal reconciliation."""
    ranking = row.get("ranking_json")
    entries = row.get("capture_entries")
    if not isinstance(ranking, list) or not isinstance(entries, list):
        return "MANIFEST_SHAPE_INVALID"
    if content_hash(ranking) != row.get("ranking_hash"):
        return "MANIFEST_RANKING_HASH_MISMATCH"
    seen_entry_ids: set[str] = set()
    required = 0
    for entry in entries:
        if not isinstance(entry, dict):
            return "MANIFEST_CAPTURE_ENTRY_INVALID"
        entry_id = entry.get("capture_entry_id")
        recipe_id = entry.get("recipe_id")
        rank = entry.get("canonical_rank")
        ranking_version = row.get("ranking_version")
        if (
            not isinstance(entry_id, str)
            or not isinstance(recipe_id, str)
            or not isinstance(rank, int)
            or isinstance(rank, bool)
            or not isinstance(ranking_version, str)
            or entry_id != capture_entry_id(
                str(row.get("generation_run_id")), ranking_version, rank, recipe_id)
            or entry_id in seen_entry_ids
        ):
            return "MANIFEST_CAPTURE_ENTRY_IDENTITY_INVALID"
        seen_entry_ids.add(entry_id)
        if entry.get("capture_required") is True:
            required += 1
    if required != row.get("expected_observation_count"):
        return "MANIFEST_EXPECTED_COUNT_MISMATCH"
    material = {
        "manifest_id": row.get("manifest_id"),
        "generation_run_id": row.get("generation_run_id"),
        "intent_id": row.get("intent_id"),
        "considered_revision_id": row.get("considered_revision_id"),
        "considered_content_hash": row.get("considered_content_hash"),
        "ranking_version": row.get("ranking_version"),
        "ranking_json": ranking,
        "capture_entries": entries,
        "expected_observation_count": row.get("expected_observation_count"),
        "capture_axis": row.get("capture_axis"),
        "capture_policy_version": row.get("capture_policy_version"),
    }
    if content_hash(material) != row.get("manifest_hash"):
        return "MANIFEST_CONTENT_HASH_MISMATCH"
    status = row.get("status")
    if status not in {"COMPLETE", "INCOMPLETE"}:
        return "MANIFEST_NOT_TERMINAL"
    reconciliation = {
        "run": row.get("generation_run_id"),
        "status": status,
        "expected": row.get("expected_observation_count"),
        "actual": row.get("actual_observation_count"),
        "reason": None if status == "COMPLETE" else "OBSERVATION_COUNT_MISMATCH",
    }
    if content_hash(reconciliation) != row.get("reconciliation_hash"):
        return "MANIFEST_RECONCILIATION_HASH_MISMATCH"
    return None


def declare_expected_run(
    conn,
    *,
    generation_run_id: str,
    intent_id: str,
    confirmed_scope_id: str,
    considered_revision_id: str,
    considered_content_hash: str,
    ranking_flag: bool,
) -> str:
    """Persist the independent source from which a wholly missing manifest is detected."""
    manifest_id = f"rfm_{generation_run_id}"
    material: dict[str, Any] = {
        "generation_run_id": generation_run_id,
        "intent_id": intent_id,
        "confirmed_scope_id": confirmed_scope_id,
        "considered_revision_id": considered_revision_id,
        "considered_content_hash": considered_content_hash,
        "shadow_flag": True,
        "ranking_flag": ranking_flag,
        "invocation_predicate_version": INVOCATION_PREDICATE_VERSION,
        "capture_policy_version": CAPTURE_POLICY_VERSION,
        "expected_manifest_id": manifest_id,
    }
    digest = content_hash(material)
    inserted = conn.execute(
        "INSERT INTO recipe_formula_shadow_expected_run "
        "(generation_run_id, intent_id, confirmed_scope_id, considered_revision_id, "
        "considered_content_hash, shadow_flag, ranking_flag, invocation_predicate_version, "
        "capture_policy_version, expected_manifest_id, declaration_hash) "
        "VALUES (%s, %s, %s, %s, %s, true, %s, %s, %s, %s, %s) "
        "ON CONFLICT (generation_run_id) DO NOTHING RETURNING expected_manifest_id",
        (
            generation_run_id,
            intent_id,
            confirmed_scope_id,
            considered_revision_id,
            considered_content_hash,
            ranking_flag,
            INVOCATION_PREDICATE_VERSION,
            CAPTURE_POLICY_VERSION,
            manifest_id,
            digest,
        ),
    ).fetchone()
    if inserted is not None:
        return inserted[0]
    existing = conn.execute(
        "SELECT intent_id, confirmed_scope_id, considered_revision_id, "
        "considered_content_hash, shadow_flag, ranking_flag, invocation_predicate_version, "
        "capture_policy_version, expected_manifest_id, declaration_hash "
        "FROM recipe_formula_shadow_expected_run WHERE generation_run_id=%s",
        (generation_run_id,),
    ).fetchone()
    _checked_existing(
        existing,
        (
            intent_id,
            confirmed_scope_id,
            considered_revision_id,
            considered_content_hash,
            True,
            ranking_flag,
            INVOCATION_PREDICATE_VERSION,
            CAPTURE_POLICY_VERSION,
            manifest_id,
            digest,
        ),
        "formula shadow expected run",
    )
    return manifest_id


def build_capture_entries(
    *,
    generation_run_id: str,
    ranking_version: str,
    ranked: Sequence,
    candidate_keys_by_recipe_id: Mapping[str, Sequence[str]],
    capture_recipe_ids: frozenset[str] | None = None,
) -> tuple[RankedCaptureEntryV1, ...]:
    entries: list[RankedCaptureEntryV1] = []
    for item in ranked:
        keys = tuple(candidate_keys_by_recipe_id.get(item.recipe_id, ()))
        resolution = "EXACT" if len(keys) == 1 else "MISSING" if not keys else "AMBIGUOUS"
        authorable = (
            True if capture_recipe_ids is None else item.recipe_id in capture_recipe_ids)
        capture_required = item.selected_for_initial_view and authorable
        entries.append(RankedCaptureEntryV1(
            capture_entry_id=capture_entry_id(
                generation_run_id,
                ranking_version,
                item.canonical_rank,
                item.recipe_id,
            ),
            recipe_id=item.recipe_id,
            canonical_rank=item.canonical_rank,
            selected_for_initial_view=item.selected_for_initial_view,
            rank_reasons=tuple(str(reason) for reason in item.rank_reasons),
            initial_view_reasons=tuple(
                str(reason) for reason in item.initial_view_reasons),
            recipe_candidate_key=keys[0] if len(keys) == 1 else None,
            candidate_resolution=resolution,
            capture_required=capture_required,
            capture_reason=(
                "SELECTED_FORMULA_V1_AUTHORABLE"
                if capture_required
                else "NOT_SELECTED"
                if not item.selected_for_initial_view
                else "RECIPE_NOT_FORMULA_V1_AUTHORABLE"
            ),
        ))
    return tuple(entries)


def write_manifest(
    conn,
    *,
    manifest_id: str,
    generation_run_id: str,
    intent_id: str,
    considered_revision_id: str,
    considered_content_hash: str,
    ranking_version: str | None,
    ranked: Sequence,
    entries: Sequence[RankedCaptureEntryV1],
    ranking_enabled: bool,
) -> None:
    ranking_json = [
        {
            "recipe_id": item.recipe_id,
            "canonical_rank": item.canonical_rank,
            "selected_for_initial_view": item.selected_for_initial_view,
            "rank_reasons": [str(reason) for reason in item.rank_reasons],
            "initial_view_reasons": [
                str(reason) for reason in item.initial_view_reasons],
        }
        for item in ranked
    ]
    entries_json = [asdict(entry) for entry in entries]
    expected_count = sum(1 for entry in entries if entry.capture_required)
    capture_axis = "CAPTURED" if ranking_enabled else "SKIPPED_RANKING_DISABLED"
    material: dict[str, Any] = {
        "manifest_id": manifest_id,
        "generation_run_id": generation_run_id,
        "intent_id": intent_id,
        "considered_revision_id": considered_revision_id,
        "considered_content_hash": considered_content_hash,
        "ranking_version": ranking_version,
        "ranking_json": ranking_json,
        "capture_entries": entries_json,
        "expected_observation_count": expected_count,
        "capture_axis": capture_axis,
        "capture_policy_version": CAPTURE_POLICY_VERSION,
    }
    digest = content_hash(material)
    inserted = conn.execute(
        "INSERT INTO recipe_formula_shadow_run_manifest "
        "(manifest_id, generation_run_id, intent_id, considered_revision_id, "
        "considered_content_hash, ranking_version, ranking_json, ranking_hash, capture_entries, "
        "expected_observation_count, capture_axis, capture_policy_version, manifest_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (manifest_id) DO NOTHING RETURNING manifest_id",
        (
            manifest_id,
            generation_run_id,
            intent_id,
            considered_revision_id,
            considered_content_hash,
            ranking_version,
            Jsonb(ranking_json),
            content_hash(ranking_json),
            Jsonb(entries_json),
            expected_count,
            capture_axis,
            CAPTURE_POLICY_VERSION,
            digest,
        ),
    ).fetchone()
    if inserted is not None:
        return
    existing = conn.execute(
        "SELECT generation_run_id, intent_id, considered_revision_id, considered_content_hash, "
        "ranking_version, ranking_hash, expected_observation_count, capture_axis, "
        "capture_policy_version, manifest_hash "
        "FROM recipe_formula_shadow_run_manifest WHERE manifest_id=%s",
        (manifest_id,),
    ).fetchone()
    _checked_existing(
        existing,
        (
            generation_run_id,
            intent_id,
            considered_revision_id,
            considered_content_hash,
            ranking_version,
            content_hash(ranking_json),
            expected_count,
            capture_axis,
            CAPTURE_POLICY_VERSION,
            digest,
        ),
        "formula shadow manifest",
    )


def write_observation(
    conn,
    *,
    observation_id: str,
    idempotency_key: str,
    capture_entry_id: str,
    generation_run_id: str,
    intent_id: str,
    considered_revision_id: str,
    considered_content_hash: str,
    recipe_id: str,
    capture_axis: str,
    recipe_candidate_key: str | None = None,
    metadata_snapshot_id: str | None = None,
    metadata_snapshot_content_hash: str | None = None,
    recipe_expectation: dict | None = None,
    recipe_expectation_hash: str | None = None,
    binding_envelope: dict | None = None,
    binding_envelope_hash: str | None = None,
    provider_input: dict | None = None,
    provider_input_hash: str | None = None,
    frozen_configuration: dict | None = None,
    frozen_configuration_hash: str | None = None,
    request_identity: dict | None = None,
    request_read_scope_hash: str | None = None,
    authority_axis: str = "NOT_EVALUATED",
    authorization_axis: str = "NOT_EVALUATED",
    drift_axis: str = "NOT_EVALUATED",
    configuration_axis: str = "NOT_EVALUATED",
    delivery_axis: str = "NOT_ENQUEUED",
    authoring_axis: str = "NOT_RUN",
    technical_axis: str = "OK",
    authoring_run_id: str | None = None,
    authoring_result: dict | None = None,
) -> None:
    payload = _observation_material({
        "observation_id": observation_id,
        "idempotency_key": idempotency_key,
        "capture_entry_id": capture_entry_id,
        "generation_run_id": generation_run_id,
        "intent_id": intent_id,
        "considered_revision_id": considered_revision_id,
        "considered_content_hash": considered_content_hash,
        "metadata_snapshot_id": metadata_snapshot_id,
        "metadata_snapshot_content_hash": metadata_snapshot_content_hash,
        "recipe_id": recipe_id,
        "recipe_candidate_key": recipe_candidate_key,
        "recipe_expectation_json": recipe_expectation,
        "recipe_expectation_hash": recipe_expectation_hash,
        "binding_envelope_json": binding_envelope,
        "binding_envelope_hash": binding_envelope_hash,
        "provider_input_json": provider_input,
        "provider_input_hash": provider_input_hash,
        "frozen_configuration_json": frozen_configuration,
        "frozen_configuration_hash": frozen_configuration_hash,
        "request_identity_json": request_identity,
        "request_read_scope_hash": request_read_scope_hash,
        "capture_axis": capture_axis,
        "authority_axis": authority_axis,
        "authorization_axis": authorization_axis,
        "drift_axis": drift_axis,
        "configuration_axis": configuration_axis,
        "delivery_axis": delivery_axis,
        "authoring_axis": authoring_axis,
        "technical_axis": technical_axis,
        "authoring_run_id": authoring_run_id,
        "authoring_result_json": authoring_result,
    })
    digest = content_hash(payload)
    inserted = conn.execute(
        "INSERT INTO recipe_formula_shadow_observation "
        "(observation_id, idempotency_key, capture_entry_id, generation_run_id, intent_id, "
        "considered_revision_id, considered_content_hash, metadata_snapshot_id, "
        "metadata_snapshot_content_hash, recipe_id, recipe_candidate_key, "
        "recipe_expectation_json, recipe_expectation_hash, binding_envelope_json, "
        "binding_envelope_hash, provider_input_json, provider_input_hash, "
        "frozen_configuration_json, frozen_configuration_hash, request_identity_json, "
        "request_read_scope_hash, capture_axis, authorization_axis, authority_axis, drift_axis, "
        "configuration_axis, delivery_axis, authoring_axis, technical_axis, "
        "authoring_run_id, authoring_result_json, payload_hash) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
        "%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING observation_id",
        (
            observation_id,
            idempotency_key,
            capture_entry_id,
            generation_run_id,
            intent_id,
            considered_revision_id,
            considered_content_hash,
            metadata_snapshot_id,
            metadata_snapshot_content_hash,
            recipe_id,
            recipe_candidate_key,
            Jsonb(recipe_expectation) if recipe_expectation is not None else None,
            recipe_expectation_hash,
            Jsonb(binding_envelope) if binding_envelope is not None else None,
            binding_envelope_hash,
            Jsonb(provider_input) if provider_input is not None else None,
            provider_input_hash,
            Jsonb(frozen_configuration) if frozen_configuration is not None else None,
            frozen_configuration_hash,
            Jsonb(request_identity) if request_identity is not None else None,
            request_read_scope_hash,
            capture_axis,
            authorization_axis,
            authority_axis,
            drift_axis,
            configuration_axis,
            delivery_axis,
            authoring_axis,
            technical_axis,
            authoring_run_id,
            Jsonb(authoring_result) if authoring_result is not None else None,
            digest,
        ),
    ).fetchone()
    if inserted is not None:
        return
    existing = conn.execute(
        "SELECT observation_id, payload_hash FROM recipe_formula_shadow_observation "
        "WHERE idempotency_key=%s",
        (idempotency_key,),
    ).fetchone()
    _checked_existing(existing, (observation_id, digest), "formula shadow observation")


def write_work_item(
    conn,
    *,
    work_item_id: str,
    idempotency_key: str,
    capture_entry_id: str,
    generation_run_id: str,
    intent_id: str,
    considered_revision_id: str,
    considered_content_hash: str,
    metadata_snapshot_id: str | None,
    metadata_snapshot_content_hash: str | None,
    recipe_id: str,
    recipe_candidate_key: str,
    recipe_expectation: dict,
    recipe_expectation_hash: str,
    binding_envelope: dict,
    binding_envelope_hash: str,
    provider_input: dict,
    provider_input_hash: str,
    frozen_configuration: dict,
    frozen_configuration_hash: str,
    request_identity: dict,
    request_read_scope_hash: str | None,
) -> None:
    """Persist immutable worker input and its minimal outbox pointer atomically."""
    material = _work_item_material(
        work_item_id=work_item_id,
        idempotency_key=idempotency_key,
        capture_entry_id=capture_entry_id,
        generation_run_id=generation_run_id,
        intent_id=intent_id,
        considered_revision_id=considered_revision_id,
        considered_content_hash=considered_content_hash,
        metadata_snapshot_id=metadata_snapshot_id,
        metadata_snapshot_content_hash=metadata_snapshot_content_hash,
        recipe_id=recipe_id,
        recipe_candidate_key=recipe_candidate_key,
        recipe_expectation=recipe_expectation,
        recipe_expectation_hash=recipe_expectation_hash,
        binding_envelope=binding_envelope,
        binding_envelope_hash=binding_envelope_hash,
        provider_input=provider_input,
        provider_input_hash=provider_input_hash,
        frozen_configuration=frozen_configuration,
        frozen_configuration_hash=frozen_configuration_hash,
        request_identity=request_identity,
        request_read_scope_hash=request_read_scope_hash,
    )
    digest = content_hash(material)
    inserted = conn.execute(
        "INSERT INTO recipe_formula_shadow_work_item "
        "(work_item_id,idempotency_key,capture_entry_id,generation_run_id,intent_id,"
        "considered_revision_id,considered_content_hash,metadata_snapshot_id,"
        "metadata_snapshot_content_hash,recipe_id,recipe_candidate_key,"
        "recipe_expectation_json,recipe_expectation_hash,binding_envelope_json,"
        "binding_envelope_hash,provider_input_json,provider_input_hash,"
        "frozen_configuration_json,frozen_configuration_hash,request_identity_json,"
        "request_read_scope_hash,payload_hash) VALUES "
        "(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (idempotency_key) DO NOTHING RETURNING work_item_id",
        (
            work_item_id,
            idempotency_key,
            capture_entry_id,
            generation_run_id,
            intent_id,
            considered_revision_id,
            considered_content_hash,
            metadata_snapshot_id,
            metadata_snapshot_content_hash,
            recipe_id,
            recipe_candidate_key,
            Jsonb(recipe_expectation),
            recipe_expectation_hash,
            Jsonb(binding_envelope),
            binding_envelope_hash,
            Jsonb(provider_input),
            provider_input_hash,
            Jsonb(frozen_configuration),
            frozen_configuration_hash,
            Jsonb(request_identity),
            request_read_scope_hash,
            digest,
        ),
    ).fetchone()
    if inserted is None:
        existing = conn.execute(
            "SELECT work_item_id,payload_hash FROM recipe_formula_shadow_work_item "
            "WHERE idempotency_key=%s",
            (idempotency_key,),
        ).fetchone()
        _checked_existing(existing, (work_item_id, digest), "formula shadow work item")
    insert_outbox_message_checked(
        conn,
        OutboxMessage(
            message_id=f"formula-shadow:{work_item_id}",
            partition_key=f"formula-run:{generation_run_id}",
            topic=RECIPE_FORMULA_SHADOW_TOPIC,
            payload={"work_item_id": work_item_id},
        ),
    )


def reconcile_run(conn, generation_run_id: str) -> ShadowReconciliation:
    expected = conn.execute(
        "SELECT expected_manifest_id FROM recipe_formula_shadow_expected_run "
        "WHERE generation_run_id=%s",
        (generation_run_id,),
    ).fetchone()
    if expected is None:
        material = {"run": generation_run_id, "status": "NOT_IN_SHADOW_POPULATION"}
        return ShadowReconciliation(
            generation_run_id,
            "NOT_IN_SHADOW_POPULATION",
            0,
            0,
            None,
            content_hash(material),
        )
    manifest = conn.execute(
        "SELECT expected_observation_count, status FROM recipe_formula_shadow_run_manifest "
        "WHERE manifest_id=%s",
        (expected[0],),
    ).fetchone()
    if manifest is None:
        material = {"run": generation_run_id, "status": "INCOMPLETE",
                    "reason": "CAPTURE_MANIFEST_MISSING"}
        return ShadowReconciliation(
            generation_run_id,
            "INCOMPLETE",
            0,
            0,
            "CAPTURE_MANIFEST_MISSING",
            content_hash(material),
        )
    actual = conn.execute(
        "SELECT count(*) FROM recipe_formula_shadow_observation "
        "WHERE generation_run_id=%s",
        (generation_run_id,),
    ).fetchone()[0]
    expected_count = manifest[0]
    status = "COMPLETE" if actual == expected_count else "INCOMPLETE"
    reason = None if status == "COMPLETE" else "OBSERVATION_COUNT_MISMATCH"
    reconciliation_material: dict[str, Any] = {
        "run": generation_run_id,
        "status": status,
        "expected": expected_count,
        "actual": actual,
        "reason": reason,
    }
    return ShadowReconciliation(
        generation_run_id,
        status,
        expected_count,
        actual,
        reason,
        content_hash(reconciliation_material),
    )


def finalize_manifest(conn, generation_run_id: str) -> ShadowReconciliation:
    result = reconcile_run(conn, generation_run_id)
    if result.status == "NOT_IN_SHADOW_POPULATION" or result.reason == "CAPTURE_MANIFEST_MISSING":
        return result
    row = conn.execute(
        "UPDATE recipe_formula_shadow_run_manifest "
        "SET status=%s, actual_observation_count=%s, reconciliation_hash=%s, terminal_at=now() "
        "WHERE generation_run_id=%s AND status='OPEN' RETURNING manifest_id",
        (
            result.status,
            result.actual_observations,
            result.reconciliation_hash,
            generation_run_id,
        ),
    ).fetchone()
    if row is None:
        existing = conn.execute(
            "SELECT status, actual_observation_count, reconciliation_hash "
            "FROM recipe_formula_shadow_run_manifest WHERE generation_run_id=%s",
            (generation_run_id,),
        ).fetchone()
        _checked_existing(
            existing,
            (
                result.status,
                result.actual_observations,
                result.reconciliation_hash,
            ),
            "formula shadow manifest terminal",
        )
    return result


def _capture_selected_entry(
    conn,
    *,
    index: int,
    entry: RankedCaptureEntryV1,
    common: dict[str, Any],
    grounding_context_by_candidate_key: Mapping[str, Any],
    metadata_snapshot_id: str | None,
    metadata_snapshot_content_hash: str | None,
    identity: IdentityEnvelope,
    request_read_scope_hash: str | None,
) -> None:
    if index >= MAX_RECIPE_FORMULA_CAPTURES_PER_RUN:
        write_observation(
            conn,
            **common,
            capture_axis="BUDGET_TRUNCATED",
            technical_axis="CAPTURE_INCOMPLETE",
        )
        return
    if entry.candidate_resolution != "EXACT" or entry.recipe_candidate_key is None:
        write_observation(
            conn,
            **common,
            capture_axis="CAPTURE_INPUT_INCOMPLETE",
            technical_axis=f"CANDIDATE_{entry.candidate_resolution}",
        )
        return
    context = grounding_context_by_candidate_key.get(entry.recipe_candidate_key)
    blueprint = RECIPE_FORMULA_EXPECTATIONS.get(entry.recipe_id)
    if context is None or blueprint is None:
        write_observation(
            conn,
            **common,
            capture_axis="CAPTURE_INPUT_INCOMPLETE",
            technical_axis="PRIVATE_CONTEXT_MISSING",
        )
        return
    if (
        metadata_snapshot_id is None
        or metadata_snapshot_content_hash is None
        or request_read_scope_hash is None
    ):
        write_observation(
            conn,
            **common,
            capture_axis="CAPTURE_INPUT_INCOMPLETE",
            technical_axis="METADATA_SNAPSHOT_LINEAGE_MISSING",
        )
        return
    try:
        sealed_input = generation_input_for_run(
            conn, common["generation_run_id"])
        if (
            sealed_input is None
            or sealed_input.intent_id != common["intent_id"]
        ):
            raise RecipeEgressViolation(
                "formula authoring requires a verified sealed generation input")
        expectation = bind_formula_expectation(context, blueprint)
        egress = build_recipe_authoring_egress(
            hypothesis=sealed_input.redacted_hypothesis,
            prediction_goal=sealed_input.redacted_prediction_goal,
            expectation=expectation,
        )
        configuration = freeze_current_configuration(
            generation_settings=current_formula_generation_settings(),
            author_instruction=AUTHOR_INSTRUCTION,
            author_prompt_id=AUTHOR_PROMPT_ID,
        )
    except RecipeFormulaPreflightError as exc:
        write_observation(
            conn,
            **common,
            capture_axis="CAPTURE_INPUT_INCOMPLETE",
            technical_axis=exc.code,
        )
        return
    except (RecipeEgressViolation, GenerationInputUnavailable):
        write_observation(
            conn,
            **common,
            capture_axis="CAPTURED",
            delivery_axis="EGRESS_REJECTED",
            authoring_axis="NOT_RUN",
            technical_axis="OK",
        )
        return
    except ValueError:
        write_observation(
            conn,
            **common,
            capture_axis="CAPTURE_INPUT_INCOMPLETE",
            technical_axis="FORMULA_PREFLIGHT_INVALID",
        )
        return
    authority = build_formula_authority_envelope(
        conn,
        context=context,
        expectation=expectation,
    )
    expectation_json = asdict(expectation)
    bound_expectation_hash = content_hash(expectation_json)
    if isinstance(authority, FormulaAuthorityRejection):
        write_observation(
            conn,
            **common,
            capture_axis="CAPTURED",
            recipe_expectation=expectation_json,
            recipe_expectation_hash=bound_expectation_hash,
            authority_axis=authority.reason,
            delivery_axis="NOT_ENQUEUED",
            technical_axis="OK",
        )
        return
    provider_input = egress.provider_payload()
    configuration_json = frozen_configuration_json(configuration)
    idempotency_key = common["idempotency_key"]
    write_work_item(
        conn,
        work_item_id=f"rfw_{idempotency_key[:24]}",
        idempotency_key=idempotency_key,
        capture_entry_id=entry.capture_entry_id,
        generation_run_id=common["generation_run_id"],
        intent_id=common["intent_id"],
        considered_revision_id=common["considered_revision_id"],
        considered_content_hash=common["considered_content_hash"],
        metadata_snapshot_id=metadata_snapshot_id,
        metadata_snapshot_content_hash=metadata_snapshot_content_hash,
        recipe_id=entry.recipe_id,
        recipe_candidate_key=entry.recipe_candidate_key,
        recipe_expectation=expectation_json,
        recipe_expectation_hash=bound_expectation_hash,
        binding_envelope=authority.to_json(),
        binding_envelope_hash=authority.content_hash,
        provider_input=provider_input,
        provider_input_hash=egress.content_hash,
        frozen_configuration=configuration_json,
        frozen_configuration_hash=configuration.configuration_hash,
        request_identity=identity_to_jsonb(identity),
        request_read_scope_hash=request_read_scope_hash,
    )


def capture_ranked_shadow(
    conn,
    *,
    generation_run_id: str,
    intent_id: str,
    confirmed_scope_id: str,
    considered_revision_id: str,
    considered_content_hash: str,
    metadata_snapshot_id: str | None,
    metadata_snapshot_content_hash: str | None,
    ranked: Sequence,
    ranking_version: str | None,
    ranking_enabled: bool,
    candidate_keys_by_recipe_id: Mapping[str, Sequence[str]],
    grounding_context_by_candidate_key: Mapping[str, Any],
    identity: IdentityEnvelope,
    request_read_scope_hash: str | None,
) -> ShadowReconciliation:
    """Capture the complete ranked population without making a provider call.

    Exact, preflight-valid inputs are authority-verified and written as immutable work items with a
    transactional outbox pointer. Terminal capture/authority failures are observations immediately;
    successful work becomes an observation only after the dedicated worker finishes.
    """
    manifest_id = declare_expected_run(
        conn,
        generation_run_id=generation_run_id,
        intent_id=intent_id,
        confirmed_scope_id=confirmed_scope_id,
        considered_revision_id=considered_revision_id,
        considered_content_hash=considered_content_hash,
        ranking_flag=ranking_enabled,
    )
    effective_ranking_version = ranking_version or "ranking-disabled"
    entries = build_capture_entries(
        generation_run_id=generation_run_id,
        ranking_version=effective_ranking_version,
        ranked=ranked,
        candidate_keys_by_recipe_id=candidate_keys_by_recipe_id,
        capture_recipe_ids=frozenset(RECIPE_FORMULA_EXPECTATIONS),
    )
    write_manifest(
        conn,
        manifest_id=manifest_id,
        generation_run_id=generation_run_id,
        intent_id=intent_id,
        considered_revision_id=considered_revision_id,
        considered_content_hash=considered_content_hash,
        ranking_version=ranking_version,
        ranked=ranked,
        entries=entries,
        ranking_enabled=ranking_enabled,
    )
    if not ranking_enabled:
        return finalize_manifest(conn, generation_run_id)
    selected = [entry for entry in entries if entry.capture_required]
    for index, entry in enumerate(selected):
        idempotency_key = content_hash({
            "generation_run_id": generation_run_id,
            "considered_revision_id": considered_revision_id,
            "considered_content_hash": considered_content_hash,
            "capture_entry_id": entry.capture_entry_id,
            "recipe_candidate_key": entry.recipe_candidate_key,
        })
        observation_id = f"rfo_{idempotency_key[:24]}"
        common: dict[str, Any] = {
            "observation_id": observation_id,
            "idempotency_key": idempotency_key,
            "capture_entry_id": entry.capture_entry_id,
            "generation_run_id": generation_run_id,
            "intent_id": intent_id,
            "considered_revision_id": considered_revision_id,
            "considered_content_hash": considered_content_hash,
            "metadata_snapshot_id": metadata_snapshot_id,
            "metadata_snapshot_content_hash": metadata_snapshot_content_hash,
            "recipe_id": entry.recipe_id,
            "recipe_candidate_key": entry.recipe_candidate_key,
        }
        try:
            with conn.transaction():
                _capture_selected_entry(
                    conn,
                    index=index,
                    entry=entry,
                    common=common,
                    grounding_context_by_candidate_key=grounding_context_by_candidate_key,
                    metadata_snapshot_id=metadata_snapshot_id,
                    metadata_snapshot_content_hash=metadata_snapshot_content_hash,
                    identity=identity,
                    request_read_scope_hash=request_read_scope_hash,
                )
        except Exception:
            logger.exception(
                "recipe formula capture failed for %s; recording terminal failure",
                entry.capture_entry_id,
            )
            try:
                with conn.transaction():
                    write_observation(
                        conn,
                        **common,
                        capture_axis="CAPTURE_INPUT_INCOMPLETE",
                        delivery_axis="NOT_ENQUEUED",
                        technical_axis="CAPTURE_PERSIST_FAILED",
                    )
            except Exception:
                # The declared manifest still expects this entry. Reconciliation therefore remains
                # INCOMPLETE; the log is diagnostic, never the source of readiness truth.
                logger.exception(
                    "recipe formula terminal capture failure could not be persisted for %s",
                    entry.capture_entry_id,
                )
    result = reconcile_run(conn, generation_run_id)
    return finalize_manifest(conn, generation_run_id) if result.status == "COMPLETE" else result
