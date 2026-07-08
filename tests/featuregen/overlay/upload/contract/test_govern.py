"""Phase 5 — confirm + govern: versioned, drift-linked contract."""
from datetime import UTC, datetime, timedelta

import pytest

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import (
    confirm_contract,
    contract_freshness,
    contracts_affected_by,
)
from featuregen.overlay.upload.graph import build_graph

NOW = datetime(2026, 7, 5, tzinfo=UTC)


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
                         "avg_90d", "posted_at", ["public.accounts.balance"],
                         derives_pairs=(("bank", "public.accounts.balance"),))


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


def test_confirm_stamps_both_feature_and_contract_design_checked(db):
    # The governed path EARNS DESIGN-CHECKED: the confirm re-runs the MCV, so BOTH the feature row and
    # the contract row it registers must be stamped DESIGN-CHECKED (not the UNVERIFIED default). Guards
    # the first-confirm register_feature(..., verification="DESIGN-CHECKED") fix.
    _bank(db)
    c = confirm_contract(db, _draft(), actor="ds1")
    feat_stamp = db.execute("SELECT verification FROM feature WHERE feature_id = %s",
                            (c.feature_id,)).fetchone()[0]
    contract_stamp = db.execute("SELECT verification FROM contract WHERE contract_id = %s",
                                (c.contract_id,)).fetchone()[0]
    assert feat_stamp == "DESIGN-CHECKED"
    assert contract_stamp == "DESIGN-CHECKED"


def test_restamp_update_flips_only_contract_less_features(db):
    # Validates the 0973 re-stamp UPDATE directly: a legacy contract-less feature stamped DESIGN-CHECKED
    # gets honestly demoted to UNVERIFIED, while a governed feature (a contract exists) keeps its earned
    # DESIGN-CHECKED. (The migration runs on an empty test schema, so exercise its UPDATE here.)
    from featuregen.overlay.upload.features import FeatureSpec, register_feature
    _bank(db)
    legacy = register_feature(db, FeatureSpec(name="legacy", verification="DESIGN-CHECKED"))  # no contract
    governed = confirm_contract(db, _draft(), actor="ds1").feature_id                          # has a contract
    db.execute("UPDATE feature SET verification = 'UNVERIFIED' "
               "WHERE verification = 'DESIGN-CHECKED' "
               "AND feature_id NOT IN (SELECT feature_id FROM contract)")
    assert db.execute("SELECT verification FROM feature WHERE feature_id = %s",
                      (legacy,)).fetchone()[0] == "UNVERIFIED"
    assert db.execute("SELECT verification FROM feature WHERE feature_id = %s",
                      (governed,)).fetchone()[0] == "DESIGN-CHECKED"


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
                          ["public.accounts.balance"],
                          derives_pairs=(("bank", "public.accounts.balance"),))
    with pytest.raises(ContractValidationError):
        confirm_contract(db, empty, actor="ds1", now=NOW)
    # draft referencing a vanished column refused (grounding via live graph)
    ghost = ContractDraft("g", "def", "accounts", "avg_90d", "posted_at", ["public.accounts.vanished"],
                          derives_pairs=(("bank", "public.accounts.vanished"),))
    with pytest.raises(ContractValidationError):
        confirm_contract(db, ghost, actor="ds1", now=NOW)


def test_reconfirm_reuses_one_feature_not_a_new_one(db):
    # B4: re-confirm reuses + refreshes the SAME feature (no proliferation)
    _bank(db)
    c1 = confirm_contract(db, _draft(), actor="ds1", now=NOW)
    c2 = confirm_contract(db, _draft(), actor="ds1", now=NOW)
    assert c1.feature_id == c2.feature_id
    assert c2.version == 2
    assert db.execute("SELECT count(*) FROM feature WHERE name = 'avg_balance_90d'").fetchone()[0] == 1


def test_confirm_wires_the_carried_catalog_pairs(db):
    # B3: govern uses the draft's carried (catalog_source, object_ref) — feature lineage binds ONLY to
    # the catalog the feature actually read, even with a same-named column in another catalog.
    _bank(db)
    build_graph(db, "bank2", [CanonicalRow("bank2", "accounts", "balance", "numeric")])  # same object_ref
    c = confirm_contract(db, _draft(), actor="ds1", now=NOW)   # draft carries ("bank", ...)
    pairs = db.execute("SELECT catalog_source FROM feature_derives_from WHERE feature_id = %s",
                       (c.feature_id,)).fetchall()
    assert {r[0] for r in pairs} == {"bank"}                  # bound to 'bank' only, not 'bank2'
