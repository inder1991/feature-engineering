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
from featuregen.overlay.upload.contract._serial import actor_json as _actor_json
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.review import validate_minimum
from featuregen.overlay.upload.features import (
    FeatureFreshness,
    FeatureSpec,
    consumers_of_feature,
    feature_freshness,
    features_affected_by,
    get_feature,
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



def confirm_contract(conn, draft: ContractDraft, *, actor, target_ref: str | None = None,
                     now: datetime | None = None, intent_id: str | None = None) -> Contract:
    """The human gate. RE-RUNS the deterministic MCV (B1) and refuses to govern an invalid draft, then
    registers a versioned governed contract + wires its derives-from into the feature layer. Re-confirming
    the same feature bumps the version. A non-empty definition is required (no empty-narrative contract)."""
    tref = target_ref if target_ref is not None else draft.target_ref   # M3: fall back to the draft's
    ok, reasons = validate_minimum(conn, draft, target_ref=tref, now=now)
    if not ok:
        raise ContractValidationError(f"contract failed MCV, not governed: {reasons}")
    if not (draft.definition or "").strip():
        raise ContractValidationError("contract has an empty definition, not governed")
    pairs = draft.derives_pairs   # B3: resolved (catalog_source, object_ref) carried on the draft
    # B4: ONE feature per feature_name — re-confirm reuses + refreshes the feature (no proliferation),
    # so drift impact/freshness point at a single live feature, not N duplicates.
    prev = conn.execute("SELECT feature_id, version FROM contract WHERE feature_name = %s "
                        "ORDER BY version DESC LIMIT 1", (draft.feature_name,)).fetchone()
    if prev is not None:
        feature_id, version = prev[0], prev[1] + 1
        conn.execute(
            "UPDATE feature SET description = %s, grain_table = %s, aggregation = %s, "
            "as_of_column = %s, verification = %s WHERE feature_id = %s",   # refresh the stamp too
            (draft.definition, draft.grain_table, draft.aggregation, draft.as_of_column,
             "DESIGN-CHECKED", feature_id))
        conn.execute("DELETE FROM feature_derives_from WHERE feature_id = %s", (feature_id,))
        for catalog_source, object_ref in pairs:
            conn.execute("INSERT INTO feature_derives_from (feature_id, catalog_source, object_ref) "
                         "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                         (feature_id, catalog_source, object_ref))
    else:
        feature_id = register_feature(conn, FeatureSpec(
            name=draft.feature_name, description=draft.definition, grain_table=draft.grain_table,
            aggregation=draft.aggregation, as_of_column=draft.as_of_column, derives_from=pairs,
            verification="DESIGN-CHECKED"))   # governed => EARNS DESIGN-CHECKED (default is UNVERIFIED)
        version = 1
    contract_id = mint_id("contract")
    conn.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, definition, version, actor, "
        "join_path, intent_id, verification) VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s)",
        (contract_id, feature_id, draft.feature_name, draft.definition, version, _actor_json(actor),
         json.dumps(list(draft.join_path)), intent_id,   # intent_id: audit link to the hypothesis (M5)
         "DESIGN-CHECKED"))   # §14.5 stamp — gauntlet-passed; predictive value unverified (0968)
    return Contract(contract_id, feature_id, draft.feature_name, version)


def contract_freshness(conn, contract_id: str, *, now: datetime) -> FeatureFreshness:
    """A contract is only as fresh as its feature's stalest source — catalog drift stales the contract."""
    row = conn.execute("SELECT feature_id FROM contract WHERE contract_id = %s",
                       (contract_id,)).fetchone()
    if row is None:
        raise KeyError(contract_id)
    return feature_freshness(conn, row[0], now=now)


def contracts_affected_by(conn, catalog_source: str, object_ref: str) -> list[str]:
    """Drift impact: the CURRENT contract (max version) per feature that derives from a drifted column
    — not every historical version (B4)."""
    feature_ids = features_affected_by(conn, catalog_source, object_ref)
    if not feature_ids:
        return []
    rows = conn.execute(
        "SELECT DISTINCT ON (feature_name) contract_id FROM contract "
        "WHERE feature_id = ANY(%s) ORDER BY feature_name, version DESC",
        (feature_ids,)).fetchall()
    return sorted(r[0] for r in rows)


def list_contracts(conn, *, limit: int = 50) -> list[dict]:
    """The governed-contract inventory (registry READ surface)."""
    rows = conn.execute(
        "SELECT contract_id, feature_id, feature_name, version, verification, created_at "
        "FROM contract ORDER BY created_at DESC LIMIT %s", (limit,)).fetchall()
    return [{"contract_id": r[0], "feature_id": r[1], "feature_name": r[2], "version": r[3],
             "verification": r[4], "created_at": r[5].isoformat()} for r in rows]


def get_contract_detail(conn, contract_id: str) -> dict | None:
    row = conn.execute(
        "SELECT contract_id, feature_id, feature_name, definition, version, verification, intent_id, "
        "created_at FROM contract WHERE contract_id = %s", (contract_id,)).fetchone()
    if row is None:
        return None
    return {"contract_id": row[0], "feature_id": row[1], "feature_name": row[2], "definition": row[3],
            "version": row[4], "verification": row[5], "intent_id": row[6],
            "created_at": row[7].isoformat()}


def feature_detail(conn, feature_id: str, *, roles=()) -> dict | None:
    """Feature 360: everything about one feature in a single view — its definition + verification stamp
    + lineage (from get_feature, READ-SCOPED by roles), the governed contract's narrative + join path,
    the HYPOTHESIS it was born from (feature -> latest contract -> intent), and its consumers (which
    models use it). The hypothesis is present only for features born through the hypothesis-driven flow."""
    feat = get_feature(conn, feature_id, roles=roles)
    if feat is None:
        return None
    row = conn.execute(
        "SELECT contract_id, definition, version, verification, intent_id, join_path FROM contract "
        "WHERE feature_id = %s ORDER BY version DESC LIMIT 1", (feature_id,)).fetchone()
    contract = None
    hypothesis = None
    if row is not None:
        contract = {"contract_id": row[0], "definition": row[1], "version": row[2],
                    "verification": row[3], "join_path": row[5]}
        if row[4]:   # intent_id -> the hypothesis behind the feature
            i = conn.execute(
                "SELECT hypothesis, definition, intake_mode, target_ref FROM contract_intent "
                "WHERE intent_id = %s", (row[4],)).fetchone()
            if i is not None:
                hypothesis = {"hypothesis": i[0], "definition": i[1], "intake_mode": i[2],
                              "target_ref": i[3]}
    return {**feat, "contract": contract, "hypothesis": hypothesis,
            "consumers": consumers_of_feature(conn, feature_id)}
