"""Delivery H2d — list/detail API exposure of the pointer's current-contract state.

``list_contracts`` + ``get_contract_detail`` additively expose, sourced from the pointer + immutable
versions/events: the feature's CURRENT contract id + ``pointer_version`` (+ ``is_current``); the
at-confirm INITIAL stamp (1011 columns) AND the EFFECTIVE (read-gated) stamp; the contract's
``requirements`` (1009); the snapshot ``metadata_input_fingerprint`` (1008); the planner ids (nullable);
and the ``invalidation_reasons`` (H2c INVALIDATED payloads). Detail adds a ``history`` section read
STRICTLY from the immutable contract versions + validation-event stream.
"""
from __future__ import annotations

from psycopg.types.json import Jsonb

from featuregen.aggregates.ids import mint_id
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import (
    confirm_contract,
    get_contract_detail,
    list_contracts,
)
from featuregen.overlay.upload.contract.invalidation import (
    REASON_CATALOG_RETYPED,
    ChangedRef,
    invalidate_contracts_for,
)
from featuregen.overlay.upload.feature_validation_projection import catch_up
from featuregen.overlay.upload.graph import build_graph


def _bank_nev(db):
    """needs_external_validation fixture: `balance` has UNKNOWN_TYPE + numeric declared_type, so the
    confirm-time MCV derives NEEDS_EXTERNAL_VALIDATION with a BLOCKING TYPE_IS_NUMERIC requirement."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", UNKNOWN_TYPE, definition="end-of-day balance"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)],
        declared_types={"public.accounts.balance": "numeric"})


def _nev_draft():
    return ContractDraft(
        "avg_balance_90d", "Average 90-day ledger balance per account.", "accounts", "avg_90d",
        "posted_at", ["public.accounts.balance"],
        derives_pairs=(("bank", "public.accounts.balance"),))


def _in_list(db, contract_id):
    for row in list_contracts(db):
        if row["contract_id"] == contract_id:
            return row
    raise AssertionError(f"{contract_id} not in list_contracts")


def _promote_all_blocking(db, contract_id):
    """Drive the contract to DATA-CHECKED: emit an EXTERNAL_PASSED for every blocking requirement + fold."""
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


def test_detail_exposes_pointer_initial_effective_requirements_fingerprint(db):
    _bank_nev(db)
    c = confirm_contract(db, _nev_draft(), actor="ds1")
    d = get_contract_detail(db, c.contract_id)

    # current pointer + is_current.
    assert d["current_contract_id"] == c.contract_id
    assert d["pointer_version"] == 1
    assert d["is_current"] is True

    # INITIAL (1011 columns) vs EFFECTIVE (read-gated) — both present, and they DIVERGE here.
    assert d["initial_verification"] == "DESIGN-CHECKED"
    assert d["initial_validation_status"] == "NEEDS_EXTERNAL_VALIDATION"
    assert d["effective_validation_status"] == "needs_external_validation"
    assert d["effective_verification"] == "UNVERIFIED"

    # requirements (1009): a blocking requirement, shaped code/params/blocking/requirement_id.
    assert d["requirements"], "expected >= 1 requirement row"
    assert set(d["requirements"][0]) == {"requirement_id", "code", "params", "blocking"}
    assert any(r["blocking"] for r in d["requirements"])

    # snapshot fingerprint + planner id columns exposed (planner ids NULL until H1a/H3).
    assert "metadata_input_fingerprint" in d
    assert d["physical_plan_id"] is None
    assert d["planner_declaration_id"] is None
    assert d["invalidation_reasons"] == []   # no drift yet


def test_list_exposes_the_same_pointer_and_provenance_fields(db):
    _bank_nev(db)
    c = confirm_contract(db, _nev_draft(), actor="ds1")
    row = _in_list(db, c.contract_id)
    assert row["current_contract_id"] == c.contract_id
    assert row["pointer_version"] == 1
    assert row["is_current"] is True
    assert row["initial_verification"] == "DESIGN-CHECKED"
    assert row["effective_validation_status"] == "needs_external_validation"
    assert row["requirements"]
    assert "metadata_input_fingerprint" in row
    assert row["physical_plan_id"] is None
    assert row["invalidation_reasons"] == []


def test_invalidation_reasons_surface_from_the_event_stream(db):
    _bank_nev(db)
    c = confirm_contract(db, _nev_draft(), actor="ds1")
    invalidate_contracts_for(db, changed=[ChangedRef(
        catalog_source="bank", reason=REASON_CATALOG_RETYPED, object_ref="public.accounts.balance")])
    d = get_contract_detail(db, c.contract_id)
    assert REASON_CATALOG_RETYPED in [r["reason"] for r in d["invalidation_reasons"]]
    assert _in_list(db, c.contract_id)["invalidation_reasons"], "list surfaces reasons too"


def test_effective_reflects_read_gate_while_initial_stamp_stays_immutable(db):
    """The KEY H2c/H2d property: a drifted dependency downgrades the EFFECTIVE (read-gated) stamp even
    though the at-confirm INITIAL stamp is unchanged (the contract row is immutable)."""
    _bank_nev(db)
    c = confirm_contract(db, _nev_draft(), actor="ds1")
    _promote_all_blocking(db, c.contract_id)      # drive the effective stamp to DATA-CHECKED
    d0 = get_contract_detail(db, c.contract_id)
    assert d0["effective_verification"] == "DATA-CHECKED"

    # DRIFT the derives column's declared type in place (NO INVALIDATED folded — projection lag).
    db.execute("UPDATE graph_node SET declared_type = 'text' "
               "WHERE catalog_source = 'bank' AND object_ref = 'public.accounts.balance'")
    d1 = get_contract_detail(db, c.contract_id)
    # effective HARD-downgraded by the read gate...
    assert d1["effective_validation_status"] == "needs_external_validation"
    assert d1["effective_verification"] == "UNVERIFIED"
    # ...while the INITIAL (immutable) stamp is unchanged.
    assert d1["initial_verification"] == d0["initial_verification"] == "DESIGN-CHECKED"
    assert d1["initial_validation_status"] == "NEEDS_EXTERNAL_VALIDATION"


def test_detail_history_reads_from_immutable_versions_and_events(db):
    _bank_nev(db)
    c1 = confirm_contract(db, _nev_draft(), actor="ds1")
    c2 = confirm_contract(db, _nev_draft(), actor="ds1")   # v2; supersedes v1; pointer -> v2

    d2 = get_contract_detail(db, c2.contract_id)
    hist = d2["history"]
    # versions come from the immutable contract log (both versions, in order).
    assert [(v["version"], v["contract_id"]) for v in hist["versions"]] == \
        [(1, c1.contract_id), (2, c2.contract_id)]
    assert any(e["event_type"] == "ASSESSED" for e in hist["events"])   # this version's own stream
    assert d2["is_current"] is True

    # the prior version carries its SUPERSEDED on ITS immutable stream + is no longer current.
    d1 = get_contract_detail(db, c1.contract_id)
    assert any(e["event_type"] == "SUPERSEDED" for e in d1["history"]["events"])
    assert d1["is_current"] is False
    assert d1["current_contract_id"] == c2.contract_id   # the pointer moved on
