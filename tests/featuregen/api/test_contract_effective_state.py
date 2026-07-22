"""Delivery C4 Task 4 — the contract READ APIs return the EFFECTIVE validation stamp from the
``feature_contract_validation`` PROJECTION, FAIL-CLOSED.

``list_contracts`` / ``get_contract_detail`` (behind GET /contracts and /contracts/{id}) must expose
``effective_validation_status`` / ``effective_verification`` read from
``feature_contract_validation_state`` — NEVER the legacy 1003 ``contract.validation_status`` /
``verification`` columns. When the projection is DEGRADED or LAGGED at read time the effective fields
FAIL CLOSED to ``'unavailable'`` / ``'UNVERIFIED'`` (never the stale legacy stamp); a contract with
no projected state row (historical / pre-C4) reads ``'legacy_unassessed'`` (not fabricated as
design_checked).
"""
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import (
    confirm_contract,
    get_contract_detail,
    list_contracts,
)
from featuregen.overlay.upload.feature_validation_projection import (
    PROJECTION_NAME,
    _mark_degraded,
    catch_up,
)
from featuregen.overlay.upload.graph import build_graph


def _bank_nev(db):
    """needs_external_validation fixture (mirrors test_confirm_emits_validation_event): `balance` has
    UNKNOWN_TYPE + numeric declared_type, so the confirm-time MCV derives NEEDS_EXTERNAL_VALIDATION
    with a BLOCKING TYPE_IS_NUMERIC requirement."""
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
    """A confirm whose faithful re-run yields ZERO requirements -> design_checked/DESIGN-CHECKED."""
    build_graph(db, "shop", [CanonicalRow("shop", "events", "kind", "text")])


def _clean_draft():
    return ContractDraft(
        "event_kinds", "Count of event kinds.", "events", "count", None,
        ["public.events.kind"], derives_pairs=(("shop", "public.events.kind"),))


def _detail_in_list(db, contract_id):
    for row in list_contracts(db):
        if row["contract_id"] == contract_id:
            return row
    raise AssertionError(f"{contract_id} not in list_contracts")


# --------------------------------------------------------------------------------------------------
# 1 — needs_external_validation: the effective stamp is PROJECTION-sourced, not the 1003 column.
# --------------------------------------------------------------------------------------------------
def test_effective_needs_external_is_projection_sourced_not_column(db):
    _bank_nev(db)
    c = confirm_contract(db, _nev_draft(), actor="ds1")

    detail = get_contract_detail(db, c.contract_id)
    assert detail["effective_validation_status"] == "needs_external_validation"
    assert detail["effective_verification"] == "UNVERIFIED"
    # list surface agrees.
    assert _detail_in_list(db, c.contract_id)["effective_verification"] == "UNVERIFIED"
    assert _detail_in_list(
        db, c.contract_id)["effective_validation_status"] == "needs_external_validation"

    # PROOF the read is projection-sourced: the `contract` row is now IMMUTABLE (H2d WORM), so the old
    # "mutate the legacy 1003 column" proof is physically impossible (that RAISE is covered by the 1012
    # migration test). Instead mutate ONLY the PROJECTION state row (the authority the effective read is
    # sourced from) to a PASSING value and show the effective read FOLLOWS it, while the untouched 1003
    # `verification` column stays DESIGN-CHECKED — i.e. the effective stamp is NOT that legacy column.
    db.execute("UPDATE feature_contract_validation_state SET validation_status = 'design_checked', "
               "effective_verification = 'DATA-CHECKED' WHERE contract_id = %s", (c.contract_id,))
    detail2 = get_contract_detail(db, c.contract_id)
    assert detail2["effective_validation_status"] == "design_checked"
    assert detail2["effective_verification"] == "DATA-CHECKED"
    assert detail2["verification"] == "DESIGN-CHECKED"   # the 1003 column, untouched — not the source


# --------------------------------------------------------------------------------------------------
# 2 — design_checked -> effective_verification DESIGN-CHECKED.
# --------------------------------------------------------------------------------------------------
def test_effective_design_checked(db):
    _clean(db)
    c = confirm_contract(db, _clean_draft(), actor="ds1")

    detail = get_contract_detail(db, c.contract_id)
    assert detail["effective_validation_status"] == "design_checked"
    assert detail["effective_verification"] == "DESIGN-CHECKED"
    assert _detail_in_list(db, c.contract_id)["effective_verification"] == "DESIGN-CHECKED"


# --------------------------------------------------------------------------------------------------
# 3 — FAIL CLOSED: a DEGRADED projection serves 'unavailable'/UNVERIFIED, never the legacy stamp.
# --------------------------------------------------------------------------------------------------
def test_fail_closed_when_projection_degraded(db):
    _clean(db)
    c = confirm_contract(db, _clean_draft(), actor="ds1")
    # Healthy first: the real effective state.
    assert get_contract_detail(db, c.contract_id)["effective_verification"] == "DESIGN-CHECKED"

    # Mark the validation projection DEGRADED -> the read FAILS CLOSED (never the legacy stamp).
    _mark_degraded(db, c.contract_id, reason="poison", seq=999)
    degraded = get_contract_detail(db, c.contract_id)
    assert degraded["effective_validation_status"] == "unavailable"
    assert degraded["effective_verification"] == "UNVERIFIED"
    assert _detail_in_list(db, c.contract_id)["effective_verification"] == "UNVERIFIED"

    # Clear the marker -> the real projected effective state returns.
    db.execute("DELETE FROM projection_degraded WHERE projection_name = %s", (PROJECTION_NAME,))
    cleared = get_contract_detail(db, c.contract_id)
    assert cleared["effective_validation_status"] == "design_checked"
    assert cleared["effective_verification"] == "DESIGN-CHECKED"


def test_fail_closed_when_projection_lagged(db):
    _clean(db)
    c = confirm_contract(db, _clean_draft(), actor="ds1")
    assert get_contract_detail(db, c.contract_id)["effective_verification"] == "DESIGN-CHECKED"

    # Append an unfolded event: the checkpoint now sits below the max event seq -> LAGGED.
    db.execute(
        "INSERT INTO feature_contract_validation_event (event_id, contract_id, event_type, payload) "
        "VALUES (%s, %s, 'INVALIDATED', '{}')", ("fcve_lagtest", c.contract_id))
    lagged = get_contract_detail(db, c.contract_id)
    assert lagged["effective_validation_status"] == "unavailable"
    assert lagged["effective_verification"] == "UNVERIFIED"

    # Fold the pending event -> caught up -> the real (now INVALIDATED) effective state returns.
    catch_up(db)
    caught = get_contract_detail(db, c.contract_id)
    assert caught["effective_validation_status"] == "needs_external_validation"
    assert caught["effective_verification"] == "UNVERIFIED"


# --------------------------------------------------------------------------------------------------
# 4 — a contract with NO projected state row (historical / pre-C4) -> legacy_unassessed.
# --------------------------------------------------------------------------------------------------
def test_no_state_row_is_legacy_unassessed_not_design_checked(db):
    _clean(db)
    c = confirm_contract(db, _clean_draft(), actor="ds1")
    # Simulate a pre-C4 contract: no projected state row (its events predate C4 emission).
    db.execute("DELETE FROM feature_contract_validation_state WHERE contract_id = %s",
               (c.contract_id,))

    detail = get_contract_detail(db, c.contract_id)
    assert detail["effective_validation_status"] == "legacy_unassessed"
    assert detail["effective_verification"] == "UNVERIFIED"
    assert _detail_in_list(db, c.contract_id)["effective_validation_status"] == "legacy_unassessed"


# --------------------------------------------------------------------------------------------------
# 5 — additive: the existing fields (incl. the 1003 INITIAL `verification` stamp) are preserved.
# --------------------------------------------------------------------------------------------------
def test_additive_keeps_initial_stamp_and_existing_fields(db):
    _clean(db)
    c = confirm_contract(db, _clean_draft(), actor="ds1")
    detail = get_contract_detail(db, c.contract_id)
    # existing keys still present, including the 1003 INITIAL stamp.
    for key in ("contract_id", "feature_id", "feature_name", "definition", "version",
                "verification", "intent_id", "created_at"):
        assert key in detail
    assert detail["verification"] == "DESIGN-CHECKED"       # 1003 initial stamp, unchanged
    listed = _detail_in_list(db, c.contract_id)
    for key in ("contract_id", "feature_id", "feature_name", "version", "verification",
                "created_at"):
        assert key in listed
