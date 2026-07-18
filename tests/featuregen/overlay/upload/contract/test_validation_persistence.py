"""Slice 3A-ii — the honest validation state carried end-to-end and persisted on the contract row.

Task 1 covers the table shape (migration 1002): `contract.validation_status` (CHECK-constrained to
the underscore VALIDATION_STATES vocab — a NEW axis, separate from the hyphenated `verification`
stamp) and `contract.requirements` (jsonb). Later tasks extend this file with the draft/confirm
round-trip; per RF-I3 only symbols that exist at this task are imported here.
"""
import json

import psycopg
import pytest

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.author import ContractDraft, draft_contract
from featuregen.overlay.upload.contract.gate1 import ConsideredSet, _snapshot, chosen_feature
from featuregen.overlay.upload.contract.review import MinimumCheck, validate_minimum
from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet, Requirement
from featuregen.overlay.upload.graph import build_graph


def _bank(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", "numeric",
                     definition="end-of-day ledger balance"),
        CanonicalRow("bank", "accounts", "posted_at", "timestamp", as_of=True)])


def _nev_idea() -> FeatureIdea:
    """A chosen feature honestly carrying NEEDS_EXTERNAL_VALIDATION + its typed requirement —
    passed DIRECTLY to draft_contract (the snapshot-restore path is Task 3's concern)."""
    return FeatureIdea(
        name="avg_balance_90d", description="", derives_from=["public.accounts.balance"],
        aggregation="avg_90d", grain_table="accounts",
        derives_pairs=(("bank", "public.accounts.balance"),),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                                  "declared numeric; operational type unknown"),))


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
                    "declared numeric; operational type unknown"),)


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
                    "declared numeric; operational type unknown"),)


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
