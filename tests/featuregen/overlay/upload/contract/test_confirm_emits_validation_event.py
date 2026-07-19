"""Delivery C4 Task 3 — confirm ADDITIVELY seeds the event-sourced validation lifecycle.

`confirm_contract` still re-runs the deterministic MCV and writes the tri-state into the 1003
`contract.validation_status`/`requirements` columns (the INITIAL stamp) — that stays intact. On TOP
of that (same transaction), confirm now: emits an ASSESSED `feature_contract_validation_event`,
persists the immutable `feature_validation_requirement` rows, and folds the event into
`feature_contract_validation_state` (via the C4-T2 projection). The C4 event/state vocabulary is
LOWERCASE (mirrors the 1009 CHECK), a DISTINCT axis from the 1003 UPPERCASE column.
"""
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import (
    REQUIREMENT_SCHEMA_VERSION,
    _seed_validation_lifecycle,
    confirm_contract,
)
from featuregen.overlay.upload.feature_validation_projection import read_state
from featuregen.overlay.upload.graph import build_graph


def _bank_nev(db):
    """RF-C2 operational-unknown fixture: `balance` has type=UNKNOWN_TYPE + a numeric declared_type
    (the FTR case), so the confirm-time MCV re-run derives NEEDS_EXTERNAL_VALIDATION with a BLOCKING
    TYPE_IS_NUMERIC requirement (plus temporal + grain on `_bank`'s as-of/grain columns)."""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", UNKNOWN_TYPE,
                     definition="end-of-day ledger balance"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)],
        declared_types={"public.accounts.balance": "numeric"})


def _nev_draft():
    return ContractDraft(
        "avg_balance_90d", "Average 90-day ledger balance per account.", "accounts", "avg_90d",
        "posted_at", ["public.accounts.balance"],
        derives_pairs=(("bank", "public.accounts.balance"),))


def _clean(db):
    """A confirm whose FAITHFUL re-run yields ZERO requirements -> DESIGN_CHECKED: a non-numeric,
    non-windowed `count` over a table with NO declared grain / as-of column, so none of the numeric /
    temporal / grain / join dispositions fire."""
    build_graph(db, "shop", [CanonicalRow("shop", "events", "kind", "text")])


def _clean_draft():
    return ContractDraft(
        "event_kinds", "Count of event kinds.", "events", "count", None,
        ["public.events.kind"], derives_pairs=(("shop", "public.events.kind"),))


# --------------------------------------------------------------------------------------------------
# 1 — needs_external_validation: ASSESSED event + blocking requirement + projected UNVERIFIED state,
#     while the 1003 INITIAL stamp is preserved.
# --------------------------------------------------------------------------------------------------
def test_confirm_emits_assessed_needs_external_and_persists_requirement(db):
    _bank_nev(db)
    c = confirm_contract(db, _nev_draft(), actor="ds1")

    # An ASSESSED event row exists for the contract.
    ev = db.execute(
        "SELECT event_type, payload FROM feature_contract_validation_event "
        "WHERE contract_id = %s", (c.contract_id,)).fetchall()
    assert len(ev) == 1
    assert ev[0][0] == "ASSESSED"
    assert ev[0][1]["validation_status"] == "needs_external_validation"   # C4 lowercase vocab
    assert ev[0][1]["has_blocking"] is True

    # A blocking TYPE_IS_NUMERIC requirement row exists (write-once, fingerprint-keyed).
    reqs = db.execute(
        "SELECT code, blocking, requirement_schema_version FROM feature_validation_requirement "
        "WHERE contract_id = %s ORDER BY code", (c.contract_id,)).fetchall()
    codes = [r[0] for r in reqs]
    assert "TYPE_IS_NUMERIC" in codes
    assert all(r[1] is True for r in reqs)                                # all blocking
    assert all(r[2] == REQUIREMENT_SCHEMA_VERSION for r in reqs)

    # The projected current-state row reflects the ASSESSED (needs_external -> UNVERIFIED).
    state = read_state(db, c.contract_id)
    assert state is not None
    assert state["validation_status"] == "needs_external_validation"
    assert state["effective_verification"] == "UNVERIFIED"

    # The 1003 INITIAL stamp is PRESERVED (uppercase axis, separate verification stamp intact).
    row = db.execute(
        "SELECT validation_status, verification FROM contract WHERE contract_id = %s",
        (c.contract_id,)).fetchone()
    assert row[0] == "NEEDS_EXTERNAL_VALIDATION"
    assert row[1] == "DESIGN-CHECKED"


# --------------------------------------------------------------------------------------------------
# 2 — design_checked: ASSESSED event + design_checked/DESIGN-CHECKED state + NO requirement rows.
# --------------------------------------------------------------------------------------------------
def test_confirm_emits_assessed_design_checked_no_requirements(db):
    _clean(db)
    c = confirm_contract(db, _clean_draft(), actor="ds1")

    ev = db.execute(
        "SELECT event_type, payload FROM feature_contract_validation_event "
        "WHERE contract_id = %s", (c.contract_id,)).fetchall()
    assert len(ev) == 1 and ev[0][0] == "ASSESSED"
    assert ev[0][1]["validation_status"] == "design_checked"
    assert ev[0][1]["has_blocking"] is False and ev[0][1]["requirement_count"] == 0

    assert db.execute(
        "SELECT count(*) FROM feature_validation_requirement WHERE contract_id = %s",
        (c.contract_id,)).fetchone()[0] == 0                             # no requirement rows

    state = read_state(db, c.contract_id)
    assert state["validation_status"] == "design_checked"
    assert state["effective_verification"] == "DESIGN-CHECKED"

    # 1003 INITIAL stamp preserved.
    assert db.execute("SELECT validation_status FROM contract WHERE contract_id = %s",
                      (c.contract_id,)).fetchone()[0] == "DESIGN_CHECKED"


# --------------------------------------------------------------------------------------------------
# 3 — idempotent: re-seeding the SAME contract + fingerprint does not duplicate requirement rows.
# --------------------------------------------------------------------------------------------------
def test_reseed_is_idempotent_on_conflict(db):
    _bank_nev(db)
    c = confirm_contract(db, _nev_draft(), actor="ds1")
    before = db.execute(
        "SELECT count(*) FROM feature_validation_requirement WHERE contract_id = %s",
        (c.contract_id,)).fetchone()[0]
    assert before >= 1

    # Re-run the exact seed for the same contract (a retry) — ON CONFLICT DO NOTHING holds, and the
    # projection's sequence guard keeps the fold a no-op: no duplicated requirement rows.
    from featuregen.overlay.upload.contract.review import validate_minimum
    check = validate_minimum(db, _nev_draft())
    _seed_validation_lifecycle(db, c.contract_id, check, list(_nev_draft().derives_pairs), None)
    after = db.execute(
        "SELECT count(*) FROM feature_validation_requirement WHERE contract_id = %s",
        (c.contract_id,)).fetchone()[0]
    assert after == before                                              # no duplicates

    # A re-confirm (a NEW version -> a new contract_id) gets its own requirement rows; state exists.
    c2 = confirm_contract(db, _nev_draft(), actor="ds1")
    assert c2.contract_id != c.contract_id and c2.version == c.version + 1
    assert read_state(db, c2.contract_id)["validation_status"] == "needs_external_validation"
