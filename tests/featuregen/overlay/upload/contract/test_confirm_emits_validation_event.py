"""Delivery C4 Task 3 — confirm ADDITIVELY seeds the event-sourced validation lifecycle.

`confirm_contract` still re-runs the deterministic MCV and writes the tri-state into the 1003
`contract.validation_status`/`requirements` columns (the INITIAL stamp) — that stays intact. On TOP
of that (same transaction), confirm now: emits an ASSESSED `feature_contract_validation_event`,
persists the immutable `feature_validation_requirement` rows, and folds the event into
`feature_contract_validation_state` (via the C4-T2 projection). The C4 event/state vocabulary is
LOWERCASE (mirrors the 1009 CHECK), a DISTINCT axis from the 1003 UPPERCASE column.
"""
from psycopg.types.json import Jsonb

from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft
from featuregen.overlay.upload.contract.govern import (
    REQUIREMENT_SCHEMA_VERSION,
    _seed_validation_lifecycle,
    confirm_contract,
)
from featuregen.overlay.upload.feature_validation_projection import catch_up, read_state
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.validation_requirements import schema_for


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
# 2b — C2-C3 review (I-1 a/b/c): the persisted requirement row carries the REGISTRY-typed params at the
#      registry schema version, with registry-driven blocking — not a lossy stand-in.
# --------------------------------------------------------------------------------------------------
def _additivity(db):
    """A `sum` over an operationally-numeric measure whose additivity is NOT governed-confirmed → the
    confirm-time MCV mints ADDITIVITY_SUPPORTS_OPERATION carrying the typed `operation` param."""
    build_graph(db, "ledger", [CanonicalRow("ledger", "postings", "amount", "numeric")])


def _additivity_draft():
    return ContractDraft(
        "total_amount", "Total posted amount.", None, "sum", None,
        ["public.postings.amount"], derives_pairs=(("ledger", "public.postings.amount"),))


def test_confirm_persists_typed_params_at_registry_version_with_registry_blocking(db):
    _additivity(db)
    c = confirm_contract(db, _additivity_draft(), actor="ds1")
    row = db.execute(
        "SELECT requirement_schema_version, params_json, blocking FROM "
        "feature_validation_requirement WHERE contract_id = %s AND code = %s",
        (c.contract_id, "ADDITIVITY_SUPPORTS_OPERATION")).fetchone()
    assert row is not None                                          # the ADDITIVITY row was persisted

    # (a) the persisted version is the REGISTRY's own "v1" — schema_for MUST resolve it (the old
    #     "req-schema-v1" namespace could not be resolved by the registry).
    assert row[0] == "v1"
    schema_for("ADDITIVITY_SUPPORTS_OPERATION", row[0])            # resolves, does not raise

    # (b) the TYPED operation param survives into params_json (was dropped into a {"detail": ...} blob).
    assert row[1]["params"] == {"operation": "sum"}
    assert "detail" in row[1]                                       # detail kept alongside the params

    # (c) blocking reflects the registry schema, not a hardcoded literal.
    assert row[2] == schema_for("ADDITIVITY_SUPPORTS_OPERATION").blocking


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


# --------------------------------------------------------------------------------------------------
# 4 — MF-4: a re-confirm emits SUPERSEDED for the PRIOR version, whose stamp goes not-live, and a
#     late EXTERNAL_PASSED can never resurrect it.
# --------------------------------------------------------------------------------------------------
def test_reconfirm_emits_superseded_for_prior_version_and_is_terminal(db):
    _bank_nev(db)
    c1 = confirm_contract(db, _nev_draft(), actor="ds1")   # v1: needs_external, blocking requirement
    # Before the re-confirm, v1 has ONLY an ASSESSED event (no SUPERSEDED yet).
    assert [r[0] for r in db.execute(
        "SELECT event_type FROM feature_contract_validation_event WHERE contract_id = %s "
        "ORDER BY seq", (c1.contract_id,)).fetchall()] == ["ASSESSED"]

    c2 = confirm_contract(db, _nev_draft(), actor="ds1")   # v2: mints a NEW contract_id
    assert c2.contract_id != c1.contract_id and c2.version == c1.version + 1

    # A SUPERSEDED event was emitted for v1, pointing at the new version.
    v1_events = db.execute(
        "SELECT event_type, payload FROM feature_contract_validation_event WHERE contract_id = %s "
        "ORDER BY seq", (c1.contract_id,)).fetchall()
    assert [e[0] for e in v1_events] == ["ASSESSED", "SUPERSEDED"]
    assert v1_events[1][1]["superseded_by"] == c2.contract_id

    # v1's state now reads not-live: terminal superseded + UNVERIFIED (NOT its old live stamp).
    s1 = read_state(db, c1.contract_id)
    assert s1["superseded"] is True
    assert s1["effective_verification"] == "UNVERIFIED"

    # v2 is the live version.
    s2 = read_state(db, c2.contract_id)
    assert s2["superseded"] is False
    assert s2["validation_status"] == "needs_external_validation"

    # A late/redelivered EXTERNAL_PASSED for v1's blocking requirement must NOT resurrect DATA-CHECKED.
    req_id = db.execute(
        "SELECT requirement_id FROM feature_validation_requirement "
        "WHERE contract_id = %s AND blocking LIMIT 1", (c1.contract_id,)).fetchone()[0]
    db.execute("INSERT INTO feature_contract_validation_event "
               "(event_id, contract_id, event_type, payload) VALUES "
               "('ev_late_pass', %s, 'EXTERNAL_PASSED', %s)",
               (c1.contract_id, Jsonb({"requirement_id": req_id})))
    catch_up(db)
    s1_after = read_state(db, c1.contract_id)
    assert s1_after["effective_verification"] == "UNVERIFIED"   # still not-live
    assert s1_after["superseded"] is True
