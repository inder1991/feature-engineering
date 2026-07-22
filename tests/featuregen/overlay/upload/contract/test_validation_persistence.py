"""Slice 3A-ii — the honest validation state carried end-to-end and persisted on the contract row.

Task 1 covers the table shape (migration 1003): `contract.validation_status` (CHECK-constrained to
the underscore VALIDATION_STATES vocab — a NEW axis, separate from the hyphenated `verification`
stamp) and `contract.requirements` (jsonb). Later tasks extend this file with the draft/confirm
round-trip; per RF-I3 only symbols that exist at this task are imported here.
"""
import json

import psycopg
import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.contract._serial import (
    requirements_from_json,
    requirements_to_json,
)
from featuregen.overlay.upload.contract.author import ContractDraft, draft_contract
from featuregen.overlay.upload.contract.gate1 import ConsideredSet, _snapshot, chosen_feature
from featuregen.overlay.upload.contract.govern import confirm_contract
from featuregen.overlay.upload.contract.review import MinimumCheck, validate_minimum
from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet, Requirement
from featuregen.overlay.upload.graph import build_graph


def _bank(db):
    """RF-C2: `balance` is genuinely OPERATIONAL-UNKNOWN — `type=UNKNOWN_TYPE` with a numeric
    glossary `declared_type` (the FTR case) — so the confirm-time MCV re-run itself derives
    NEEDS_EXTERNAL_VALIDATION -> TYPE_IS_NUMERIC. (An operationally 'numeric' balance is a KNOWN
    type, which never yields TYPE_IS_NUMERIC.)"""
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", UNKNOWN_TYPE,
                     definition="end-of-day ledger balance"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)],
        declared_types={"public.accounts.balance": "numeric"})


def _nev_idea() -> FeatureIdea:
    """A chosen feature honestly carrying NEEDS_EXTERNAL_VALIDATION + its typed requirement —
    passed DIRECTLY to draft_contract (the snapshot-restore path is Task 3's concern). The detail
    is the VALIDATOR's real string for `_bank`'s operationally-unknown balance (RF-C2)."""
    return FeatureIdea(
        name="avg_balance_90d", description="", derives_from=["public.accounts.balance"],
        aggregation="avg_90d", grain_table="accounts",
        derives_pairs=(("bank", "public.accounts.balance"),),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                                  "operational type unknown; numeric declared hint"),))


def _seed_feature(db, feature_id: str, name: str) -> None:
    """RF-I4: contract.feature_id is FK-constrained (contract_feature_id_fk, migration 0972) —
    every contract insert must reference a REAL feature row, never a bogus id."""
    db.execute(
        "INSERT INTO feature (feature_id, name) VALUES (%s, %s)", (feature_id, name))


def test_contract_has_validation_status_and_requirements_columns(db):
    cols = dict(db.execute(
        "SELECT column_name, data_type FROM information_schema.columns "
        "WHERE table_name = 'contract' "
        "AND column_name IN ('validation_status', 'requirements')").fetchall())
    assert cols.get("validation_status") == "text"
    assert cols.get("requirements") == "jsonb"


def test_contract_validation_status_check_rejects_unknown_value(db):
    _seed_feature(db, "f-check", "fx")
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute(
            "INSERT INTO contract "
            "(contract_id, feature_id, feature_name, version, validation_status) "
            "VALUES ('c-bogus', 'f-check', 'fx', 1, 'BOGUS')")


def test_contract_validation_status_defaults_to_design_checked(db):
    _seed_feature(db, "f-default", "fd")
    db.execute(
        "INSERT INTO contract (contract_id, feature_id, feature_name, version) "
        "VALUES ('c-default', 'f-default', 'fd', 1)")
    row = db.execute(
        "SELECT validation_status, requirements FROM contract WHERE contract_id = 'c-default'"
    ).fetchone()
    assert row[0] == "DESIGN_CHECKED"
    assert row[1] == []


def test_draft_contract_carries_validation_status_and_requirements(db):
    _bank(db)
    client = FakeLLM(script={"overlay.contract.draft": FakeResponse(
        output={"definition": "Average 90-day ledger balance per account."})})
    draft = draft_contract(db, _nev_idea(), client)
    assert draft.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert draft.requirements == (
        Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                    "operational type unknown; numeric declared hint"),)


def test_draft_defaults_are_design_checked_and_empty(db):
    _bank(db)
    client = FakeLLM(script={"overlay.contract.draft": FakeResponse(output={"definition": "x"})})
    plain = FeatureIdea(name="f", description="", derives_from=["public.accounts.balance"],
                        aggregation="avg_90d", grain_table="accounts",
                        derives_pairs=(("bank", "public.accounts.balance"),))
    draft = draft_contract(db, plain, client)
    assert draft.validation_status == "DESIGN_CHECKED"
    assert draft.requirements == ()


def _seed_intent(db, intent_id: str) -> None:
    """contract_considered.intent_id is FK-constrained (contract_considered_intent_id_fk, migration
    0972) — a snapshot insert must reference a REAL intent row."""
    db.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES (%s, 'h', 'hypothesis')", (intent_id,))


def test_snapshot_round_trips_validation_status_and_requirements(db):
    _bank(db)
    _seed_intent(db, "intent-rt")
    cs = ConsideredSet("intent-rt", None, [FeatureSet("templates", [_nev_idea()])], None)
    db.execute(
        "INSERT INTO contract_considered (intent_id, considered) VALUES (%s, %s::jsonb)",
        ("intent-rt", json.dumps(_snapshot(db, cs))))
    feat = chosen_feature(db, "intent-rt", "alternative", "avg_balance_90d")
    assert feat is not None
    assert feat.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert feat.requirements == (
        Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                    "operational type unknown; numeric declared hint"),)


def test_snapshot_restores_previously_dropped_verification_fields(db):
    _bank(db)
    _seed_intent(db, "intent-vf")
    idea = FeatureIdea(
        name="f", description="", derives_from=["public.accounts.balance"],
        aggregation="avg_90d", grain_table="accounts",
        derives_pairs=(("bank", "public.accounts.balance"),),
        verification="DESIGN-CHECKED", critic_note="weak grain fit", rationale="proxy for churn")
    cs = ConsideredSet("intent-vf", None, [FeatureSet("templates", [idea])], None)
    db.execute(
        "INSERT INTO contract_considered (intent_id, considered) VALUES (%s, %s::jsonb)",
        ("intent-vf", json.dumps(_snapshot(db, cs))))
    feat = chosen_feature(db, "intent-vf", "alternative", "f")
    assert feat is not None
    assert feat.verification == "DESIGN-CHECKED"
    assert feat.critic_note == "weak grain fit"      # was silently dropped pre-3A-ii
    assert feat.rationale == "proxy for churn"       # was silently dropped pre-3A-ii
    assert feat.validation_status == "DESIGN_CHECKED"
    assert feat.requirements == ()


def test_validate_minimum_carries_needs_external_validation(db, monkeypatch):
    _bank(db)
    draft = ContractDraft(
        "avg_balance_90d", "Average 90-day balance.", "accounts", "avg_90d", "posted_at",
        ["public.accounts.balance"], derives_pairs=(("bank", "public.accounts.balance"),),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"), "x"),))
    crafted = FeatureIdea(
        name="avg_balance_90d", description="", derives_from=["public.accounts.balance"],
        aggregation="avg_90d", grain_table="accounts",
        derives_pairs=(("bank", "public.accounts.balance"),),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"), "x"),))
    monkeypatch.setattr(
        "featuregen.overlay.upload.contract.review._validate_idea",
        lambda *a, **k: (crafted, None))
    check = validate_minimum(db, draft)
    assert isinstance(check, MinimumCheck)
    assert check.ok is True
    assert check.reasons == []
    assert check.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert check.requirements == (
        Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"), "x"),)


def test_validate_minimum_rejected_reports_reason_and_status(db):
    _bank(db)
    # derives from a column that does not exist -> the gauntlet REJECTS (ungrounded)
    draft = ContractDraft(
        "bad", "d", "accounts", "avg_90d", "posted_at", ["public.accounts.nope"],
        derives_pairs=(("bank", "public.accounts.nope"),))
    check = validate_minimum(db, draft)
    assert check.ok is False
    assert check.reasons                                 # a non-empty rejection reason
    assert check.validation_status == "REJECTED"
    assert check.requirements == ()


def test_author_contract_consumes_minimumcheck(db):
    _bank(db)
    draft = ContractDraft(
        "avg_balance_90d", "Average 90-day balance.", "accounts", "avg_90d", "posted_at",
        ["public.accounts.balance"], derives_pairs=(("bank", "public.accounts.balance"),))
    client = FakeLLM(script={
        "overlay.contract.critique": FakeResponse(output={"findings": []}),
        "overlay.contract.refine": FakeResponse(output={"definition": "Average 90-day balance."})})
    from featuregen.overlay.upload.contract.review import author_contract
    result_draft, unresolved = author_contract(db, draft, client)
    assert unresolved == []                              # MCV clean + no critique -> nothing unresolved
    assert result_draft.feature_name == "avg_balance_90d"


def test_confirm_persists_validation_status_and_requirements(db):
    """RF-C1 + RF-C2: confirm persists the CONFIRM-TIME MCV re-run's status + requirements. The
    draft carries ONE requirement; the FAITHFUL live re-run (grain_table threaded — the whole-
    branch-review Critical) on `_bank` derives THREE (numeric + temporal + grain) — the persisted
    row must hold the RE-RUN's, not the draft's.
    Recon #1: the hyphenated `verification` stamp is a SEPARATE axis and stays 'DESIGN-CHECKED'."""
    _bank(db)
    draft = ContractDraft(
        "avg_balance_90d", "Average 90-day ledger balance per account.", "accounts", "avg_90d",
        "posted_at", ["public.accounts.balance"],
        derives_pairs=(("bank", "public.accounts.balance"),),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                                  "operational type unknown; numeric declared hint"),))
    c = confirm_contract(db, draft, actor="ds1")
    row = db.execute(
        "SELECT validation_status, requirements, verification FROM contract "
        "WHERE contract_id = %s", (c.contract_id,)).fetchone()
    assert row[0] == "NEEDS_EXTERNAL_VALIDATION"         # honest, not silently DESIGN_CHECKED
    assert row[1] == [
        {"code": "TYPE_IS_NUMERIC", "operand": ["bank", "public.accounts.balance"],
         "detail": "operational type unknown; numeric declared hint"},
        {"code": "TEMPORAL_IS_POPULATED", "operand": ["bank", "public.accounts.posted_at"],
         "detail": "as-of column declared, not governed-verified"},
        {"code": "GRAIN_IS_UNIQUE", "operand": ["bank", "public.accounts.id"],
         "detail": "grain declared, not governed-verified"},
    ]                                                    # the RE-RUN's three, not the draft's one
    assert row[1] == requirements_to_json(validate_minimum(db, draft).requirements)
    assert row[2] == "DESIGN-CHECKED"                    # the SEPARATE verification axis intact


def test_confirm_default_draft_persists_rerun_status_not_draft(db):
    """RF-C1 (downgrade caught): a draft still claiming its default DESIGN_CHECKED confirms against
    a catalog whose measure is operationally unknown — the persisted status is the re-run's
    NEEDS_EXTERNAL_VALIDATION, never the draft's stale claim."""
    _bank(db)
    draft = ContractDraft(
        "avg_balance_90d", "Average 90-day ledger balance.", "accounts", "avg_90d", "posted_at",
        ["public.accounts.balance"], derives_pairs=(("bank", "public.accounts.balance"),))
    assert draft.validation_status == "DESIGN_CHECKED"   # the stale draft default
    c = confirm_contract(db, draft, actor="ds1")
    row = db.execute("SELECT validation_status, requirements FROM contract WHERE contract_id = %s",
                     (c.contract_id,)).fetchone()
    assert row[0] == "NEEDS_EXTERNAL_VALIDATION"
    assert [r["code"] for r in row[1]] == [
        "TYPE_IS_NUMERIC", "TEMPORAL_IS_POPULATED", "GRAIN_IS_UNIQUE"]   # the FAITHFUL re-run set


def test_needs_external_validation_survives_gate1_to_persisted_contract(db):
    """Task 6 e2e (RF-C2, real catalog): a NEEDS_EXTERNAL_VALIDATION feature chosen at Gate #1 is
    snapshotted, reconstructed, drafted, re-validated (MCV), and confirmed — and the CONTRACT ROW
    records NEEDS_EXTERNAL_VALIDATION + the confirm-time RE-RUN's requirements (RF-C1), NOT a
    silent DESIGN_CHECKED at any hop. The hyphenated verification stamp stays a SEPARATE axis
    (recon #1 — does NOT reuse governance VERIFICATION_STAMPS)."""
    _bank(db)
    _seed_intent(db, "intent-e2e")
    # Gate #1: the human's chosen option lands in the persisted considered-set snapshot.
    cs = ConsideredSet("intent-e2e", None, [FeatureSet("templates", [_nev_idea()])], None)
    db.execute(
        "INSERT INTO contract_considered (intent_id, considered) VALUES (%s, %s::jsonb)",
        ("intent-e2e", json.dumps(_snapshot(db, cs))))
    # Reconstruct the chosen feature from the SERVER snapshot — the honest state must survive.
    chosen = chosen_feature(db, "intent-e2e", "alternative", "avg_balance_90d")
    assert chosen is not None
    assert chosen.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert chosen.requirements == (
        Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                    "operational type unknown; numeric declared hint"),)
    # Author the draft; the state rides onto the draft.
    client = FakeLLM(script={"overlay.contract.draft": FakeResponse(
        output={"definition": "Average 90-day ledger balance per account."})})
    draft = draft_contract(db, chosen, client)
    assert draft.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    assert draft.requirements == chosen.requirements
    # The MCV re-runs and passes the gate (grounded; no `now`, so freshness is skipped) — and the
    # re-run itself derives the honest state from the operationally-unknown catalog column.
    check = validate_minimum(db, draft)
    assert check.ok is True
    assert check.validation_status == "NEEDS_EXTERNAL_VALIDATION"
    # Confirm persists the CONFIRM-TIME re-run's honest state (RF-C1), never the draft's copy.
    c = confirm_contract(db, draft, actor="ds1", intent_id="intent-e2e")
    row = db.execute(
        "SELECT validation_status, requirements, verification FROM contract "
        "WHERE contract_id = %s", (c.contract_id,)).fetchone()
    assert row[0] == "NEEDS_EXTERNAL_VALIDATION"         # honest at the END of the whole path
    assert row[1] == [
        {"code": "TYPE_IS_NUMERIC", "operand": ["bank", "public.accounts.balance"],
         "detail": "operational type unknown; numeric declared hint"},
        {"code": "TEMPORAL_IS_POPULATED", "operand": ["bank", "public.accounts.posted_at"],
         "detail": "as-of column declared, not governed-verified"},
        {"code": "GRAIN_IS_UNIQUE", "operand": ["bank", "public.accounts.id"],
         "detail": "grain declared, not governed-verified"},
    ]                                                    # the RE-RUN's real requirements (RF-C2)
    assert row[1] == requirements_to_json(check.requirements)
    assert row[2] == "DESIGN-CHECKED"                    # the SEPARATE verification axis intact


def test_requirements_json_no_param_shape_is_byte_identical():
    """I-1d: the base serialized shape for a no-param requirement stays {code, operand, detail} — the
    typed params are emitted ADDITIVELY (only when present), so no-param requirements (all but
    ADDITIVITY today) are byte-identical to pre-fix and the persisted contract.requirements column /
    snapshot shape is preserved."""
    from featuregen.overlay.upload.validation_requirements import build_requirement
    r = build_requirement(code="TYPE_IS_NUMERIC", operand=("bank", "public.accounts.balance"),
                          detail="operational type unknown; numeric declared hint")
    assert requirements_to_json((r,)) == [
        {"code": "TYPE_IS_NUMERIC", "operand": ["bank", "public.accounts.balance"],
         "detail": "operational type unknown; numeric declared hint"}]


def test_requirements_json_round_trip_preserves_typed_params():
    """I-1d: an ADDITIVITY requirement's TYPED operation param survives the snapshot round trip, and the
    re-materialized requirement is registry-valid — reconstructed through the sanctioned factory, its
    schema_version resolves, params equal the original."""
    from featuregen.overlay.upload.validation_requirements import build_requirement, schema_for
    r = build_requirement(code="ADDITIVITY_SUPPORTS_OPERATION",
                          operand=("ledger", "public.postings.amount"),
                          detail="additivity not governed-confirmed", params={"operation": "sum"})
    serialized = requirements_to_json((r,))
    assert serialized[0]["params"] == [["operation", "sum"]]   # typed param carried through
    back = requirements_from_json(serialized)
    assert back == (r,)                                        # registry-valid, params preserved
    assert back[0].params == (("operation", "sum"),)
    schema_for(back[0].code, back[0].schema_version)           # version resolves (would raise if not)


def test_requirements_from_json_tolerates_legacy_rows_without_params():
    """I-1d: a LEGACY serialized row (no params / no schema_version) still deserializes — even for a
    code the registry now requires params for (ADDITIVITY): it must NOT raise, falling back to the raw
    value object rather than crashing snapshot restore."""
    legacy = [
        {"code": "TYPE_IS_NUMERIC", "operand": ["bank", "public.t.c"], "detail": "d"},
        {"code": "ADDITIVITY_SUPPORTS_OPERATION", "operand": ["ledger", "public.t.amount"],
         "detail": "legacy, no params"},
    ]
    reqs = requirements_from_json(legacy)                      # must not raise
    assert [r.code for r in reqs] == ["TYPE_IS_NUMERIC", "ADDITIVITY_SUPPORTS_OPERATION"]
    assert reqs[0].schema_version == "v1" and reqs[0].params == ()
    assert reqs[1].params == ()                               # tolerated legacy ADDITIVITY, no crash


def test_confirm_grain_feature_persists_faithful_grain_requirement(db):
    """The whole-branch review's proof case: a grain-only feature (non-numeric, non-windowed op on
    the file-declared grain) used to persist a SILENTLY-DOWNGRADED clean DESIGN_CHECKED + [] because
    the re-run dropped grain_table and the grain disposition never fired. The FAITHFUL re-run
    persists NEEDS_EXTERNAL_VALIDATION + [GRAIN_IS_UNIQUE] — and, no-over-rejection invariant, the
    feature CONFIRMS (needs-checked, not REJECTED)."""
    _bank(db)
    draft = ContractDraft(
        "distinct_accounts", "Distinct account count.", "accounts", "count_distinct", "posted_at",
        ["public.accounts.id"], derives_pairs=(("bank", "public.accounts.id"),))
    c = confirm_contract(db, draft, actor="ds1")         # must NOT raise: governable, just honest
    row = db.execute("SELECT validation_status, requirements FROM contract WHERE contract_id = %s",
                     (c.contract_id,)).fetchone()
    assert row[0] == "NEEDS_EXTERNAL_VALIDATION"         # NOT the silent DESIGN_CHECKED downgrade
    assert row[1] == [
        {"code": "GRAIN_IS_UNIQUE", "operand": ["bank", "public.accounts.id"],
         "detail": "grain declared, not governed-verified"}]
