"""Phase 5 — confirm + govern (versioned, drift-linked).

`confirm_contract` is the HUMAN GATE — the only write that makes a contract governing. It registers the
draft as a versioned feature contract and wires its derives-from into the feature layer, so freshness
lineage and drift impact apply for free: a governed contract KNOWS when its inputs drifted. A re-confirm
of the same feature is a new version; history stays.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from featuregen.aggregates.ids import mint_id
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.review import validate_minimum
from featuregen.overlay.upload.features import (
    FeatureFreshness,
    FeatureSpec,
    feature_freshness,
    features_affected_by,
    register_feature,
)


class ContractValidationError(Exception):
    """The draft failed the deterministic MCV — it must not be governed."""


@dataclass(frozen=True, slots=True)
class Contract:
    contract_id: str
    feature_id: str
    feature_name: str
    version: int


def _actor_json(actor) -> str:
    if isinstance(actor, str):
        return json.dumps(actor)
    try:
        return json.dumps(identity_to_jsonb(actor))
    except Exception:
        return json.dumps(str(actor))


def _derives_pairs(conn, object_refs: list[str]) -> tuple[tuple[str, str], ...]:
    # map each grounded object_ref back to its catalog_source (the feature layer keys on the pair)
    if not object_refs:
        return ()
    rows = conn.execute(
        "SELECT DISTINCT object_ref, catalog_source FROM graph_node WHERE object_ref = ANY(%s)",
        (list(object_refs),)).fetchall()
    return tuple((r[1], r[0]) for r in rows)


def confirm_contract(conn, draft: ContractDraft, *, actor, target_ref: str | None = None,
                     now: datetime | None = None) -> Contract:
    """The human gate. RE-RUNS the deterministic MCV (B1) and refuses to govern an invalid draft, then
    registers a versioned governed contract + wires its derives-from into the feature layer. Re-confirming
    the same feature bumps the version. A non-empty definition is required (no empty-narrative contract)."""
    ok, reasons = validate_minimum(conn, draft, target_ref=target_ref, now=now)
    if not ok:
        raise ContractValidationError(f"contract failed MCV, not governed: {reasons}")
    if not (draft.definition or "").strip():
        raise ContractValidationError("contract has an empty definition, not governed")
    pairs = _derives_pairs(conn, draft.derives_from)
    feature_id = register_feature(conn, FeatureSpec(
        name=draft.feature_name, description=draft.definition, grain_table=draft.grain_table,
        aggregation=draft.aggregation, as_of_column=draft.as_of_column, derives_from=pairs))
    prev = conn.execute("SELECT COALESCE(MAX(version), 0) FROM contract WHERE feature_name = %s",
                        (draft.feature_name,)).fetchone()
    version = (prev[0] or 0) + 1
    contract_id = mint_id("contract")
    conn.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, definition, version, actor) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb)",
        (contract_id, feature_id, draft.feature_name, draft.definition, version, _actor_json(actor)))
    return Contract(contract_id, feature_id, draft.feature_name, version)


def contract_freshness(conn, contract_id: str, *, now: datetime) -> FeatureFreshness:
    """A contract is only as fresh as its feature's stalest source — catalog drift stales the contract."""
    row = conn.execute("SELECT feature_id FROM contract WHERE contract_id = %s",
                       (contract_id,)).fetchone()
    if row is None:
        raise KeyError(contract_id)
    return feature_freshness(conn, row[0], now=now)


def contracts_affected_by(conn, catalog_source: str, object_ref: str) -> list[str]:
    """Drift impact: the contracts whose feature derives from a drifted column."""
    feature_ids = features_affected_by(conn, catalog_source, object_ref)
    if not feature_ids:
        return []
    rows = conn.execute(
        "SELECT contract_id FROM contract WHERE feature_id = ANY(%s) ORDER BY contract_id",
        (feature_ids,)).fetchall()
    return [r[0] for r in rows]
