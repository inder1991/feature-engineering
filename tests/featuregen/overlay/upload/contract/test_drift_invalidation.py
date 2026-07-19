"""Delivery H2c — reverse-dependency rows + INVALIDATED on drift + the read-time SECOND fail-closed gate.

`confirm_contract` now records what catalog state each contract depended on
(`contract_metadata_dependency`, write-once). `invalidate_contracts_for` appends an INVALIDATED
validation event (append-only, idempotent) for every contract version whose dependency drifted — the C4
projection ALREADY demotes a prior DATA-CHECKED when it folds an INVALIDATED (this task only EMITs). And
`contract_read_status` adds the SECOND, read-time fail-closed gate: it recomputes each dependency's
current hash and HARD-downgrades a promoted stamp if ANY item drifted — so a stale DATA-CHECKED can never
be served even while the projection state row still reads DATA-CHECKED (projection lag).
"""
from __future__ import annotations

import psycopg
import pytest
from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import confirm_contract, contract_read_status
from featuregen.overlay.upload.contract.invalidation import (
    REASON_CATALOG_RETYPED,
    ChangedRef,
    dependencies_drifted,
    invalidate_contracts_for,
)
from featuregen.overlay.upload.feature_validation_projection import catch_up, read_state
from featuregen.overlay.upload.graph import build_graph


# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────────
def _bank(db, source="bank"):
    build_graph(db, source, [
        CanonicalRow(source, "accounts", "id", "integer", is_grain=True),
        CanonicalRow(source, "accounts", "balance", "numeric"),
        CanonicalRow(source, "accounts", "posted_at", "timestamp", as_of=True)])


def _bank_nev(db):
    """`balance` is operationally-unknown (UNKNOWN_TYPE + a numeric declared_type) so the confirm-time
    MCV re-run derives a BLOCKING requirement — the seed for a DATA-CHECKED promotion."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", UNKNOWN_TYPE, definition="end-of-day balance"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)],
        declared_types={"public.accounts.balance": "numeric"})


def _draft(name="avg_balance_90d", source="bank"):
    return ContractDraft(name, "Average 90-day ledger balance.", "accounts", "avg_90d", "posted_at",
                         ["public.accounts.balance"],
                         derives_pairs=((source, "public.accounts.balance"),))


def _deps(db, contract_id):
    return db.execute(
        "SELECT catalog_source, logical_ref, item_hash FROM contract_metadata_dependency "
        "WHERE contract_id = %s ORDER BY logical_ref", (contract_id,)).fetchall()


def _invalidated_count(db, contract_id):
    return db.execute(
        "SELECT count(*) FROM feature_contract_validation_event "
        "WHERE contract_id = %s AND event_type = 'INVALIDATED'", (contract_id,)).fetchone()[0]


def _promote_all_blocking(db, contract_id):
    """Drive the contract to DATA-CHECKED: emit an EXTERNAL_PASSED for every blocking requirement, then
    fold. DATA-CHECKED requires a current pass on EVERY blocking requirement (projection fold)."""
    req_ids = [r[0] for r in db.execute(
        "SELECT requirement_id FROM feature_validation_requirement "
        "WHERE contract_id = %s AND blocking", (contract_id,)).fetchall()]
    assert req_ids, "fixture must produce >= 1 blocking requirement to reach DATA-CHECKED"
    for rid in req_ids:
        db.execute(
            "INSERT INTO feature_contract_validation_event "
            "(event_id, contract_id, event_type, payload) VALUES (%s, %s, 'EXTERNAL_PASSED', %s)",
            (mint_id("fcve"), contract_id, Jsonb({"requirement_id": rid})))
    catch_up(db)


# ── TEST 1 — confirming writes write-once reverse-dep rows for each check-clearing/input item ─────────
def test_confirm_writes_reverse_dependency_rows_write_once(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")

    rows = _deps(db, c.contract_id)
    refs = {r[1] for r in rows}
    # one row per check-clearing / input-binding item: the derives measure, the grain table, the as-of
    # column — each PUBLIC-FLATTENED to its graph object_ref and each carrying an item_hash.
    assert refs == {"public.accounts.balance", "public.accounts", "public.accounts.posted_at"}
    assert all(r[0] == "bank" for r in rows)
    assert all(r[2] for r in rows)                         # every row has a non-empty item_hash

    # write-once: the 1011 no-mutation trigger rejects UPDATE and DELETE (savepoint per attempt).
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute("UPDATE contract_metadata_dependency SET logical_ref = 'x' WHERE contract_id = %s",
                   (c.contract_id,))
    with pytest.raises(psycopg.errors.RaiseException), db.transaction():
        db.execute("DELETE FROM contract_metadata_dependency WHERE contract_id = %s", (c.contract_id,))
    assert len(_deps(db, c.contract_id)) == 3              # rows survived both rejected mutations


# ── TEST 2 — invalidate_contracts_for emits INVALIDATED (demotes) and is idempotent ──────────────────
def test_invalidate_contracts_for_emits_invalidated_and_is_idempotent(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    assert _invalidated_count(db, c.contract_id) == 0

    ref = ChangedRef(catalog_source="bank", reason=REASON_CATALOG_RETYPED,
                     object_ref="public.accounts.balance")
    appended = invalidate_contracts_for(db, changed=[ref])
    assert appended == 1
    assert _invalidated_count(db, c.contract_id) == 1

    # the projection folded the INVALIDATED: the effective stamp is demoted to needs-revalidation.
    st = read_state(db, c.contract_id)
    assert st["validation_status"] == "needs_external_validation"
    assert st["effective_verification"] == "UNVERIFIED"

    # idempotent: re-invalidating for the SAME (reason, ref) appends NO duplicate event.
    assert invalidate_contracts_for(db, changed=[ref]) == 0
    assert _invalidated_count(db, c.contract_id) == 1


# ── TEST 3 — the KEY test: the read gate fails closed on drift WITHOUT any INVALIDATED (projection lag)
def test_read_gate_downgrades_stale_data_checked_without_invalidated(db):
    _bank_nev(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)

    # the projection now serves a genuine DATA-CHECKED, and the read gate serves it too (no drift yet).
    st = read_state(db, c.contract_id)
    assert (st["validation_status"], st["effective_verification"]) == ("design_checked", "DATA-CHECKED")
    assert contract_read_status(db, c.contract_id) == ("design_checked", "DATA-CHECKED")

    # DRIFT the derives column's declared type IN PLACE, emitting NO INVALIDATED event (projection lag).
    db.execute("UPDATE graph_node SET declared_type = 'text' "
               "WHERE catalog_source = 'bank' AND object_ref = 'public.accounts.balance'")

    # the PROJECTION ALONE still says DATA-CHECKED (no INVALIDATED was folded)...
    st_lagged = read_state(db, c.contract_id)
    assert st_lagged["effective_verification"] == "DATA-CHECKED"
    assert _invalidated_count(db, c.contract_id) == 0
    # ...but dependency drift is detected, and the READ GATE HARD-downgrades: a stale DATA-CHECKED can
    # NEVER be served.
    assert dependencies_drifted(db, c.contract_id) is True
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")


# ── TEST 3b — the read gate also fails closed when the dependency's node is DROPPED (missing) ─────────
def test_read_gate_downgrades_when_dependency_node_dropped(db):
    _bank_nev(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)
    assert contract_read_status(db, c.contract_id) == ("design_checked", "DATA-CHECKED")

    # DROP the derives column's graph_node (a dropped column) — no INVALIDATED emitted.
    db.execute("DELETE FROM graph_node WHERE catalog_source = 'bank' "
               "AND object_ref = 'public.accounts.balance'")
    assert read_state(db, c.contract_id)["effective_verification"] == "DATA-CHECKED"   # projection lag
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")


# ── TEST 4 — PRIMARY drift wiring: a two-upload ingest that drops a column INVALIDATES the contract ───
_RETIRE_SRC = "h2c_retire_src"


def _retire_actor():
    from featuregen.contracts.envelopes import IdentityEnvelope
    return IdentityEnvelope(subject="upload", actor_kind="human", authenticated=True,
                            auth_method="oidc", role_claims=("data_owner",))


def _retire_glossary_csv(balance_def):
    header = ("physical_name,business_term,description_business_definition,data_domain,"
              "bian_path,fibo_path\n")
    return (header
            + f'public.accounts.balance,Account Balance,"{balance_def}",Deposits,,\n')


def _retire_ingest(db, csv_text):
    from datetime import UTC, datetime

    from featuregen.overlay.upload.glossary_reader import read_glossary
    from featuregen.overlay.upload.ingest import ingest_upload
    upload = read_glossary(csv_text, source=_RETIRE_SRC)
    res = ingest_upload(db, _RETIRE_SRC, upload.rows, actor=_retire_actor(),
                        now=datetime(2026, 7, 20, tzinfo=UTC), client=None, glossary=upload)
    assert res.status == "ingested"


_PROFILED = ("The ledger balance. The sample profile is NUMERIC, with representative values such as "
             "1250.00; 9.99; 42.50, which supports interpretation.")
_PLAIN = "The ledger balance (revised, no sample profile)."


def test_two_upload_drop_invalidates_dependent_contract_via_ingest_wire(db):
    """Upload 1 makes balance's parsed shape load-bearing; a contract is confirmed on it; upload 2 drops
    the sample facet -> `_retire_dropped_field_decisions` retires that decision -> the wired
    `invalidate_contracts_for` appends an INVALIDATED to the dependent contract."""
    from datetime import timedelta

    from featuregen.overlay.config import OverlayConfig, register_overlay_config
    register_overlay_config(OverlayConfig(
        ttl_default=timedelta(days=180), ttl_min=timedelta(days=30), ttl_max=timedelta(days=365),
        ttl_jitter_fraction=0.1, renewal_grace=timedelta(days=14),
        drift_scan_interval=timedelta(minutes=15), drift_freshness_sla=timedelta(hours=24),
        profiler_require_restricted_role=False))

    _retire_ingest(db, _retire_glossary_csv(_PROFILED))     # upload 1

    draft = ContractDraft("h2c_bal_count", "Count of balances.", None, "count", None,
                          ["public.accounts.balance"],
                          derives_pairs=((_RETIRE_SRC, "public.accounts.balance"),))
    c = confirm_contract(db, draft, actor="ds1")
    assert _invalidated_count(db, c.contract_id) == 0       # no drift yet

    _retire_ingest(db, _retire_glossary_csv(_PLAIN))        # upload 2 — drops the facet

    # the ingest's `_retire_dropped_field_decisions -> invalidate_contracts_for` wire fired.
    assert _invalidated_count(db, c.contract_id) >= 1
    # and the effective stamp is demoted (both the eager fold AND the read gate agree).
    assert contract_read_status(db, c.contract_id) == ("needs_external_validation", "UNVERIFIED")


# ── TEST 5 — an unaffected contract (depends only on ref Y) is NOT invalidated by a drift on ref X ────
def test_unaffected_contract_is_not_invalidated(db):
    _bank(db)
    a = confirm_contract(db, _draft(name="feat_balance"), actor="ds1")   # derives public.accounts.balance
    draft_b = ContractDraft("feat_id", "Count of ids.", None, "count", None,
                            ["public.accounts.id"], derives_pairs=(("bank", "public.accounts.id"),))
    b = confirm_contract(db, draft_b, actor="ds1")                       # derives public.accounts.id ONLY

    invalidate_contracts_for(db, changed=[ChangedRef(
        catalog_source="bank", reason=REASON_CATALOG_RETYPED, object_ref="public.accounts.balance")])

    assert _invalidated_count(db, a.contract_id) == 1       # X drifted -> A invalidated
    assert _invalidated_count(db, b.contract_id) == 0       # B depends only on Y -> untouched
