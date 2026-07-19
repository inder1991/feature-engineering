"""Phase 5 — confirm + govern (versioned, drift-linked).

`confirm_contract` is the HUMAN GATE — the only write that makes a contract governing. It registers the
draft as a versioned feature contract and wires its derives-from into the feature layer, so freshness
lineage and drift impact apply for free: a governed contract KNOWS when its inputs drifted. A re-confirm
of the same feature is a new version; history stays.
"""
from __future__ import annotations

import json
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime

from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.upload import feature_validation_projection
from featuregen.overlay.upload.contract._serial import actor_json as _actor_json
from featuregen.overlay.upload.contract._serial import requirements_to_json
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
from featuregen.overlay.upload.validation_requirements import DEFAULT_SCHEMA_VERSION, schema_for

# Delivery C4-T3 / C2-C3 review (I-1a): the immutable-requirement schema version stamped on each
# persisted `feature_validation_requirement` row is the REGISTRY's OWN version. Each row now stamps the
# requirement's OWN `schema_version` (the registry "v1"), so a downstream `schema_for(code, version)`
# RESOLVES — the previous separate "req-schema-v1" namespace could not be resolved by the registry. This
# constant is the unified registry default, kept as the shared reference (e.g. the pre-C0 fingerprint).
# A re-assessment against a NEW schema version yields NEW rows (the 1009 UNIQUE key includes it).
REQUIREMENT_SCHEMA_VERSION = DEFAULT_SCHEMA_VERSION


class ContractValidationError(Exception):
    """The draft failed the deterministic MCV — it must not be governed."""


def _confirm_snapshot_binding(conn, intent_id: str | None) -> tuple[str | None, str | None]:
    """MF-3 — the SERVER C0 metadata-snapshot lineage recorded on the considered set for this intent,
    read AT CONFIRM. Returns ``(snapshot_id, content_hash)`` to bind IMMUTABLY onto the write-once-in-
    practice contract row: ``contract_considered.snapshot_id`` is a MUTABLE upsert pointer (a later
    broaden repoints it S1->S2), so recording the value AT CONFIRM on the contract row is what makes
    "what catalog state was this contract authored against" reconstructable and un-repointable. Returns
    ``(None, None)`` when the intent has no considered-set row, or it recorded no snapshot (a pre-C0 /
    READ COMMITTED considered set) — additive, the columns stay NULL."""
    if intent_id is None:
        return None, None
    row = conn.execute(
        "SELECT snapshot_id, snapshot_content_hash FROM contract_considered WHERE intent_id = %s",
        (intent_id,)).fetchone()
    if row is None:
        return None, None
    return row[0], row[1]


@dataclass(frozen=True, slots=True)
class Contract:
    contract_id: str
    feature_id: str
    feature_name: str
    version: int



def confirm_contract(conn, draft: ContractDraft, *, actor, roles: Iterable[str] = (),
                     target_ref: str | None = None,
                     now: datetime | None = None, intent_id: str | None = None) -> Contract:
    """The human gate. RE-RUNS the deterministic MCV (B1) and refuses to govern an invalid draft, then
    registers a versioned governed contract + wires its derives-from into the feature layer. Re-confirming
    the same feature bumps the version. A non-empty definition is required (no empty-narrative contract).
    `roles` is the CONFIRMING actor's read-scope, threaded into the re-run so the cross-table
    join-authority disposition judges the confirmer's real authority — without it a sensitivity-tagged
    hop would read DENIED and over-reject a legitimately authorized feature."""
    tref = target_ref if target_ref is not None else draft.target_ref   # M3: fall back to the draft's
    check = validate_minimum(conn, draft, target_ref=tref, now=now, roles=roles)
    if not check.ok:
        raise ContractValidationError(f"contract failed MCV, not governed: {check.reasons}")
    if not (draft.definition or "").strip():
        raise ContractValidationError("contract has an empty definition, not governed")
    pairs = draft.derives_pairs   # B3: resolved (catalog_source, object_ref) carried on the draft
    # B4: ONE feature per feature_name — re-confirm reuses + refreshes the feature (no proliferation),
    # so drift impact/freshness point at a single live feature, not N duplicates.
    prev = conn.execute("SELECT contract_id, feature_id, version FROM contract "
                        "WHERE feature_name = %s ORDER BY version DESC LIMIT 1",
                        (draft.feature_name,)).fetchone()
    if prev is not None:
        # MF-4: the PRIOR latest contract_id is retired by this re-confirm — carried so the seed
        # emits a SUPERSEDED event for it (below), demoting its now-dead validation stamp.
        superseded_contract_id, feature_id, version = prev[0], prev[1], prev[2] + 1
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
        superseded_contract_id = None   # first version — nothing to retire
        feature_id = register_feature(conn, FeatureSpec(
            name=draft.feature_name, description=draft.definition, grain_table=draft.grain_table,
            aggregation=draft.aggregation, as_of_column=draft.as_of_column, derives_from=pairs,
            verification="DESIGN-CHECKED"))   # governed => EARNS DESIGN-CHECKED (default is UNVERIFIED)
        version = 1
    contract_id = mint_id("contract")
    # MF-3: bind THIS contract to the immutable metadata snapshot the considered set was authored against,
    # read AT CONFIRM from the server row. Persisted onto the never-repointed contract row so a later
    # broaden (which repoints the mutable contract_considered.snapshot_id) cannot change what catalog state
    # this governed contract was authored against. NULL on a pre-C0 / READ COMMITTED set (additive).
    metadata_snapshot_id, metadata_content_hash = _confirm_snapshot_binding(conn, intent_id)
    conn.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, definition, version, actor, "
        "join_path, intent_id, verification, validation_status, requirements, "
        "metadata_snapshot_id, metadata_content_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s, %s, %s, %s::jsonb, %s, %s)",
        (contract_id, feature_id, draft.feature_name, draft.definition, version, _actor_json(actor),
         json.dumps(list(draft.join_path)), intent_id,   # intent_id: audit link to the hypothesis (M5)
         "DESIGN-CHECKED",   # §14.5 stamp — gauntlet-passed; predictive value unverified (0968).
         #                     A SEPARATE (hyphenated, 0973-constrained) axis from validation_status.
         check.validation_status,   # RF-C1: the CONFIRM-TIME re-run's honest tri-state — NOT the
         #                            draft's carried value (an upgrade/downgrade since Gate #1 is
         #                            a real change and must be recorded, never silently kept stale)
         json.dumps(requirements_to_json(check.requirements)),
         metadata_snapshot_id, metadata_content_hash))   # MF-3: immutable contract -> snapshot binding
    # Delivery C4-T3: ADDITIVELY seed the event-sourced validation lifecycle from the SAME confirm-time
    # MCV re-run. The 1003 columns above stay the INITIAL stamp (unchanged); this emits the ASSESSED
    # event + persists the immutable requirement rows + projects the current-state row — all on THIS
    # transaction, so the lifecycle seed is atomic with confirm.
    _seed_validation_lifecycle(conn, contract_id, check, pairs, metadata_content_hash,
                               superseded_contract_id=superseded_contract_id)
    return Contract(contract_id, feature_id, draft.feature_name, version)


def _seed_validation_lifecycle(conn, contract_id, check, pairs, snapshot_content_hash,
                               *, superseded_contract_id=None) -> None:
    """C4-T3: from the confirm-time ``MinimumCheck``, persist the immutable requirement rows, emit the
    ASSESSED event, and fold it into ``feature_contract_validation_state`` — all on ``conn`` (atomic
    with the contract insert). Idempotent: requirement rows use ``ON CONFLICT DO NOTHING`` on the 1009
    identity key, and the projection's sequence guard makes the fold a replay-safe no-op.

    The requirement fingerprint is the IMMUTABLE metadata-snapshot content_hash (MF-3 binding — what
    catalog state the contract was authored against) when present, else a canonical hash of the draft's
    resolved (catalog, ref) pairs + the confirm-time requirements (a pre-C0 / snapshot-less confirm).

    MF-1: takes the projection checkpoint row FOR UPDATE (``lock_checkpoint``) BEFORE inserting any
    event, so concurrent confirms serialize their seq-assignment WITH the fold (no skip/regress).
    MF-4: when ``superseded_contract_id`` is given (a re-confirm), emits a SUPERSEDED event for that
    retired version BEFORE this version's ASSESSED — folded in the same transaction, under the same
    lock, so the retired version's live stamp is demoted terminally.
    """
    # MF-1: serialize emit+fold across concurrent confirms — lock BEFORE any event is inserted.
    feature_validation_projection.lock_checkpoint(conn)
    fingerprint = snapshot_content_hash or canonical_hash(
        {"derives_pairs": [[cs, ref] for cs, ref in pairs],
         "requirements": requirements_to_json(check.requirements)})
    for req in check.requirements:
        operand = [req.operand[0], req.operand[1]]
        content_hash = canonical_hash({"code": req.code, "operand": operand, "detail": req.detail})
        # C2-C3 review (I-1): persist the requirement's REGISTRY-typed shape, not a lossy stand-in. The
        # requirements come from the confirm-time MCV re-mint (build_requirement), so they are always
        # registry-valid; schema_for resolves.
        #   (a) requirement_schema_version = the requirement's OWN schema_version (the registry "v1"),
        #       so a downstream schema_for(code, version) RESOLVES (the old "req-schema-v1" could not);
        #   (b) params_json carries the TYPED params (e.g. ADDITIVITY's {"operation": ...}) the external
        #       check (Delivery I) reads — kept alongside `detail`, which used to be all this column held;
        #   (c) blocking is the REGISTRY schema's blocking flag, not a hardcoded True.
        # Write-once + identity-keyed (1009).
        schema = schema_for(req.code, req.schema_version)
        conn.execute(
            "INSERT INTO feature_validation_requirement (requirement_id, contract_id, "
            "requirement_schema_version, metadata_input_fingerprint, code, subject_json, "
            "params_json, blocking, content_hash) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (contract_id, requirement_schema_version, metadata_input_fingerprint, "
            "content_hash) DO NOTHING",
            (mint_id("req"), contract_id, req.schema_version, fingerprint, req.code,
             Jsonb({"operand": operand}),
             Jsonb({"params": dict(req.params), "detail": req.detail}),
             schema.blocking, content_hash))
    # MF-4: a re-confirm mints a NEW contract_id for this feature; the PRIOR latest version is now
    # dead. Emit SUPERSEDED for it in THIS transaction, BEFORE the new version's ASSESSED, so the
    # fold demotes the retired version's live stamp AND marks it terminally superseded — a late
    # EXTERNAL_PASSED can never resurrect it, and a consumer filtering on effective_verification
    # never picks the dead version. Same lock + seq space -> folded in order by catch_up below.
    if superseded_contract_id is not None:
        conn.execute(
            "INSERT INTO feature_contract_validation_event "
            "(event_id, contract_id, event_type, payload) VALUES (%s, %s, 'SUPERSEDED', %s)",
            (mint_id("fcve"), superseded_contract_id, Jsonb({"superseded_by": contract_id})))
    # The ASSESSED payload is MINIMAL + honest: the C4 lowercase status vocabulary (mirrors the 1009
    # CHECK — a DISTINCT axis from the 1003 UPPERCASE column), plus counts. The fold reads the
    # requirement rows above for the authoritative blocking detail, so the requirement rows MUST be
    # persisted before this event is folded.
    conn.execute(
        "INSERT INTO feature_contract_validation_event "
        "(event_id, contract_id, event_type, payload) VALUES (%s, %s, 'ASSESSED', %s)",
        (mint_id("fcve"), contract_id, Jsonb({
            "validation_status": check.validation_status.lower(),
            "requirement_count": len(check.requirements),
            "has_blocking": bool(check.requirements)})))
    feature_validation_projection.catch_up(conn)   # fold SUPERSEDED (if any) + ASSESSED in seq order


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


# Delivery C4-T4: the effective-fields sentinels the fail-closed read returns instead of the
# projection's real stamp. UNVERIFIED is always the safe effective verification when we cannot trust
# the read model — NEVER the legacy 1003 stamp.
_EFFECTIVE_UNVERIFIED = "UNVERIFIED"
_EFFECTIVE_UNAVAILABLE = "unavailable"        # projection DEGRADED/LAGGED — fail closed
_EFFECTIVE_LEGACY_UNASSESSED = "legacy_unassessed"  # a pre-C4 contract with no projected state row


def _effective_validation(ready: bool, state_status, state_verification) -> tuple[str, str]:
    """Map the projection health + a contract's ``feature_contract_validation_state`` row (its
    ``validation_status``/``effective_verification``, or ``None`` when there is no state row) to the
    authoritative ``(effective_validation_status, effective_verification)`` the read APIs expose.

    FAIL CLOSED (C4-T4): the effective stamp is PROJECTION-sourced only. A DEGRADED/LAGGED projection
    (``ready is False``) → ``('unavailable', 'UNVERIFIED')`` — never the legacy 1003 column. A
    contract with NO state row (historical / pre-C4) → ``('legacy_unassessed', 'UNVERIFIED')``, not
    fabricated as design_checked. Otherwise the real projected effective stamp is returned verbatim.
    """
    if not ready:
        return _EFFECTIVE_UNAVAILABLE, _EFFECTIVE_UNVERIFIED
    if state_status is None:
        return _EFFECTIVE_LEGACY_UNASSESSED, _EFFECTIVE_UNVERIFIED
    return state_status, state_verification


def list_contracts(conn, *, limit: int = 50) -> list[dict]:
    """The governed-contract inventory (registry READ surface).

    C4-T4: each row carries the EFFECTIVE validation stamp from the ``feature_contract_validation``
    PROJECTION (``effective_validation_status``/``effective_verification`` — the authoritative fields
    for consumers), LEFT JOINed on ``contract_id``, gated FAIL-CLOSED by the projection's read
    readiness. ``verification`` remains the 1003 INITIAL confirm-time stamp (kept, additive) — the
    effective fields, not it, are authoritative, and a lagged/degraded projection serves
    'unavailable'/UNVERIFIED here, never that legacy column."""
    ready = feature_validation_projection.is_read_ready(conn)   # ONE fail-closed gate for the read
    rows = conn.execute(
        "SELECT c.contract_id, c.feature_id, c.feature_name, c.version, c.verification, "
        "c.created_at, s.validation_status, s.effective_verification "
        "FROM contract c "
        "LEFT JOIN feature_contract_validation_state s ON s.contract_id = c.contract_id "
        "ORDER BY c.created_at DESC LIMIT %s", (limit,)).fetchall()
    out = []
    for r in rows:
        eff_status, eff_verif = _effective_validation(ready, r[6], r[7])
        out.append({"contract_id": r[0], "feature_id": r[1], "feature_name": r[2], "version": r[3],
                    "verification": r[4], "created_at": r[5].isoformat(),
                    "effective_validation_status": eff_status,
                    "effective_verification": eff_verif})
    return out


def get_contract_detail(conn, contract_id: str) -> dict | None:
    """C4-T4: the contract detail carries the EFFECTIVE validation stamp read from the
    ``feature_contract_validation`` PROJECTION (``effective_validation_status``/
    ``effective_verification``), gated FAIL-CLOSED. ``verification`` stays the 1003 INITIAL stamp
    (kept, additive); the effective fields are the authoritative ones and never fall back to that
    legacy column when the projection is degraded/lagged."""
    row = conn.execute(
        "SELECT contract_id, feature_id, feature_name, definition, version, verification, intent_id, "
        "created_at FROM contract WHERE contract_id = %s", (contract_id,)).fetchone()
    if row is None:
        return None
    ready = feature_validation_projection.is_read_ready(conn)
    state = feature_validation_projection.read_state(conn, contract_id)
    eff_status, eff_verif = _effective_validation(
        ready, None if state is None else state["validation_status"],
        None if state is None else state["effective_verification"])
    return {"contract_id": row[0], "feature_id": row[1], "feature_name": row[2], "definition": row[3],
            "version": row[4], "verification": row[5], "intent_id": row[6],
            "created_at": row[7].isoformat(),
            "effective_validation_status": eff_status,
            "effective_verification": eff_verif}


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
