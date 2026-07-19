"""Delivery H2d — deterministic pointer repair + one-time legacy backfill.

The ``feature_current_contract`` pointer (1011) is the SOLE source of truth for a feature's CURRENT
contract version; history reads from the immutable ``contract`` versions + append-only
``feature_contract_validation_event`` stream. Two admin/recovery operations keep the pointer honest:

  * :func:`repair_feature_pointer` — DETERMINISTIC + IDEMPOTENT rebuild of ONE feature's pointer from
    the highest VALID confirmed contract version (the max version whose validation stream is not
    SUPERSEDED-away), refreshing the ``feature``/``feature_derives_from`` compat projection from that
    version's immutable ``contract_input_column`` lineage. Same DB state -> same pointer; a re-run when
    the pointer is already correct is a NO-OP (no spurious ``pointer_version`` bump). Serialized under
    the SAME per-feature advisory lock ``confirm_contract`` takes (H2b), so a repair can never interleave
    with a concurrent confirm's pointer CAS.

  * :func:`backfill_feature_pointers` — the ONE-TIME legacy backfill: a feature that HAS >= 1 contract
    but NO pointer (governed before H2b) gets a pointer at its LATEST existing contract. It reads
    ``legacy_unassessed`` effectively (the contract predates the C4 projection so it has no
    ``feature_contract_validation_state`` row, and ``_effective_validation`` maps a missing state row to
    ``legacy_unassessed``) — the pointer is ALL we install; NO snapshot / input / requirement rows are
    fabricated for a legacy contract. A directly-registered feature with NO contract keeps NO pointer and
    reads ``UNVERIFIED`` / ``no_contract``. Idempotent: a feature that already has a pointer is skipped.
"""
from __future__ import annotations

from featuregen.contracts import DbConn
from featuregen.overlay.upload.contract.govern import feature_contract_lock_key


def _highest_valid_confirmed_contract(conn: DbConn, feature_id: str) -> str | None:
    """The pointer's rebuild target: the highest-``version`` contract for ``feature_id`` whose validation
    stream is NOT SUPERSEDED-away. Deterministic — ``version`` is UNIQUE per feature (0961) so the
    ORDER BY resolves to one row. A superseded version is retired and can never be the current pointer;
    since ``contract`` is now WORM (H2d) a version can never be deleted, so "exists" is implied. Returns
    ``None`` only when the feature has NO confirmed contract (a directly-registered feature), in which
    case there is nothing to point at and the caller leaves the pointer absent."""
    row = conn.execute(
        "SELECT c.contract_id FROM contract c WHERE c.feature_id = %s "
        "AND NOT EXISTS (SELECT 1 FROM feature_contract_validation_event e "
        "                WHERE e.contract_id = c.contract_id AND e.event_type = 'SUPERSEDED') "
        "ORDER BY c.version DESC LIMIT 1",
        (feature_id,)).fetchone()
    return row[0] if row is not None else None


def _refresh_compat_projection(conn: DbConn, feature_id: str, contract_id: str) -> None:
    """Rebuild the current-pointer COMPAT projection (``feature`` + ``feature_derives_from``) from the
    target contract's IMMUTABLE ``contract_input_column`` lineage — the SAME projection
    ``confirm_contract`` STEP 7 writes, so a repaired feature's drift/freshness lineage matches a freshly
    confirmed one. Sources: ``description`` <- ``contract.definition``; ``grain_table``/``as_of_column``
    <- the grain/as_of role input rows; ``feature_derives_from`` <- the derives role input rows;
    ``verification`` <- the governed DESIGN-CHECKED stamp. ``aggregation`` is DELIBERATELY left untouched
    — it is not part of the immutable input lineage, so it cannot be reconstructed here (a known limit of
    the pointer model; the pointer + input rows are the authority, not this display projection)."""
    definition = conn.execute(
        "SELECT definition FROM contract WHERE contract_id = %s", (contract_id,)).fetchone()[0]
    rows = conn.execute(
        "SELECT source, logical_ref, physical_ref, role FROM contract_input_column "
        "WHERE contract_id = %s", (contract_id,)).fetchall()
    grain_table = next((r[1] for r in rows if r[3] == "grain"), None)
    as_of_column = next((r[1] for r in rows if r[3] == "as_of"), None)
    derives = [(r[0], r[1] or r[2]) for r in rows if r[3] == "derives"]
    conn.execute(
        "UPDATE feature SET description = %s, grain_table = %s, as_of_column = %s, verification = %s "
        "WHERE feature_id = %s",
        (definition, grain_table, as_of_column, "DESIGN-CHECKED", feature_id))
    conn.execute("DELETE FROM feature_derives_from WHERE feature_id = %s", (feature_id,))
    for catalog_source, object_ref in derives:
        conn.execute(
            "INSERT INTO feature_derives_from (feature_id, catalog_source, object_ref) "
            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
            (feature_id, catalog_source, object_ref))


def repair_feature_pointer(conn: DbConn, feature_id: str) -> bool:
    """Deterministically rebuild ONE feature's ``feature_current_contract`` pointer from the highest VALID
    confirmed contract version, under the per-feature advisory lock. Returns True iff the pointer was
    (re)pointed (a repair happened); False on the IDEMPOTENT no-op (pointer already correct) or when the
    feature has no confirmed contract to point at.

    DETERMINISTIC: same DB state -> same target (the highest non-superseded version).
    IDEMPOTENT: if the pointer already points at the target, NOTHING changes — no ``pointer_version``
    bump, no compat rewrite. ``pointer_version`` only ever advances (monotonic): a repoint sets it to the
    current value + 1; a first install starts it at 1.

    Raises ``KeyError`` if ``feature_id`` is unknown (repair cannot invent a feature identity for the
    lock key). Never fabricates a contract or lineage rows.
    """
    frow = conn.execute("SELECT name FROM feature WHERE feature_id = %s", (feature_id,)).fetchone()
    if frow is None:
        raise KeyError(feature_id)
    # Serialize vs a concurrent confirm's pointer CAS (H2b): SAME lock key, derived from the feature
    # identity. pg_advisory_xact_lock binds to the caller's transaction and releases on COMMIT/ROLLBACK.
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (feature_contract_lock_key(frow[0]),))

    target = _highest_valid_confirmed_contract(conn, feature_id)
    if target is None:
        return False   # no confirmed contract — nothing to point at (never fabricate a pointer)

    pointer = conn.execute(
        "SELECT contract_id, pointer_version FROM feature_current_contract WHERE feature_id = %s",
        (feature_id,)).fetchone()
    if pointer is None:
        conn.execute(
            "INSERT INTO feature_current_contract (feature_id, contract_id, pointer_version, set_at) "
            "VALUES (%s, %s, 1, now())", (feature_id, target))
        _refresh_compat_projection(conn, feature_id, target)
        return True
    if pointer[0] == target:
        return False   # IDEMPOTENT no-op — pointer already correct, no bump, no compat rewrite
    conn.execute(
        "UPDATE feature_current_contract SET contract_id = %s, pointer_version = %s, set_at = now() "
        "WHERE feature_id = %s", (target, pointer[1] + 1, feature_id))
    _refresh_compat_projection(conn, feature_id, target)
    return True


def backfill_feature_pointers(conn: DbConn) -> int:
    """One-time legacy backfill: install a ``feature_current_contract`` pointer for every feature that HAS
    at least one contract but NO pointer (a feature governed before H2b). The pointer is set to the
    feature's LATEST existing contract; NOTHING else is written — no snapshot / input / requirement rows
    are fabricated for a legacy contract, so it reads ``legacy_unassessed`` effectively (it has no C4
    projection state row). Returns the number of pointers installed.

    A directly-registered feature with NO contract keeps NO pointer (it stays UNVERIFIED / no_contract).
    IDEMPOTENT: a feature that already has a pointer is skipped, so re-running changes nothing. Each
    install is serialized under the feature's advisory lock (re-checking the pointer under the lock) so it
    can never race a concurrent confirm. Processes features in a stable ``feature_id`` order.
    """
    feature_ids = [r[0] for r in conn.execute(
        "SELECT DISTINCT c.feature_id FROM contract c "
        "WHERE NOT EXISTS (SELECT 1 FROM feature_current_contract p WHERE p.feature_id = c.feature_id) "
        "ORDER BY c.feature_id").fetchall()]
    installed = 0
    for feature_id in feature_ids:
        frow = conn.execute("SELECT name FROM feature WHERE feature_id = %s", (feature_id,)).fetchone()
        if frow is None:   # a contract whose feature row is missing — leave for human remediation
            continue
        conn.execute("SELECT pg_advisory_xact_lock(%s)", (feature_contract_lock_key(frow[0]),))
        # Re-check UNDER the lock: a concurrent confirm may have installed the pointer since the scan.
        if conn.execute("SELECT 1 FROM feature_current_contract WHERE feature_id = %s",
                        (feature_id,)).fetchone() is not None:
            continue
        latest = conn.execute(
            "SELECT contract_id FROM contract WHERE feature_id = %s ORDER BY version DESC LIMIT 1",
            (feature_id,)).fetchone()[0]
        conn.execute(
            "INSERT INTO feature_current_contract (feature_id, contract_id, pointer_version, set_at) "
            "VALUES (%s, %s, 1, now())", (feature_id, latest))
        installed += 1
    return installed
