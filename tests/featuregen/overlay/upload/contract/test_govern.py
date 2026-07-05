"""Phase 5 — confirm + govern: versioned, drift-linked contract."""
from datetime import datetime, timedelta, timezone

import pytest

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import (
    confirm_contract,
    contract_freshness,
    contracts_affected_by,
)
from featuregen.overlay.upload.graph import build_graph

NOW = datetime(2026, 7, 5, tzinfo=timezone.utc)


def _bank(db, watermark=NOW):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)])
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, head_seq) "
        "VALUES ('bank', %s, 'r', 0) ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (watermark, watermark))


def _draft():
    return ContractDraft("avg_balance_90d", "Average 90-day ledger balance.", "accounts",
                         "avg_90d", "posted_at", ["public.accounts.balance"])


def test_confirm_registers_versioned_contract_and_wires_feature(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    assert c.version == 1
    # the feature was registered with the contract's definition + derives-from wired
    name, desc = db.execute("SELECT name, description FROM feature WHERE feature_id = %s",
                            (c.feature_id,)).fetchone()
    assert name == "avg_balance_90d"
    assert desc == "Average 90-day ledger balance."
    pair = db.execute("SELECT catalog_source, object_ref FROM feature_derives_from "
                      "WHERE feature_id = %s", (c.feature_id,)).fetchone()
    assert pair == ("bank", "public.accounts.balance")
    # re-confirm the same feature -> a new version (history stays)
    c2 = confirm_contract(db, _draft(), actor="ds1")
    assert c2.version == 2


def test_contract_freshness_follows_source_drift(db):
    _bank(db, watermark=NOW)                          # fresh
    c = confirm_contract(db, _draft(), actor="ds1")
    assert contract_freshness(db, c.contract_id, now=NOW).fresh

    # a stale source makes the contract not fresh (fail-closed governance)
    db.execute("UPDATE overlay_drift_watermark SET last_completed_at = %s WHERE catalog_source = 'bank'",
               (NOW - timedelta(days=3),))
    f = contract_freshness(db, c.contract_id, now=NOW)
    assert not f.fresh and "bank" in f.stale_sources


def test_contracts_affected_by_drift(db):
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    # drift on the balance column surfaces the contract as impacted
    assert c.contract_id in contracts_affected_by(db, "bank", "public.accounts.balance")
    assert contracts_affected_by(db, "bank", "public.accounts.nonexistent") == []


def test_confirm_reruns_mcv_and_refuses_bad_drafts(db):
    from featuregen.overlay.upload.contract.govern import ContractValidationError
    _bank(db)
    # B1: leaky draft (derives the declared target) refused at the gate
    with pytest.raises(ContractValidationError):
        confirm_contract(db, _draft(), actor="ds1", target_ref="public.accounts.balance", now=NOW)
    # empty-definition draft refused (no empty-narrative governing contract)
    empty = ContractDraft("avg_balance_90d", "", "accounts", "avg_90d", "posted_at",
                          ["public.accounts.balance"])
    with pytest.raises(ContractValidationError):
        confirm_contract(db, empty, actor="ds1", now=NOW)
    # draft referencing a vanished column refused (grounding via live graph)
    ghost = ContractDraft("g", "def", "accounts", "avg_90d", "posted_at", ["public.accounts.vanished"])
    with pytest.raises(ContractValidationError):
        confirm_contract(db, ghost, actor="ds1", now=NOW)
