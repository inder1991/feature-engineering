"""SECURITY regression — POST /contract/confirm reconciles grain_table + derives_from server-side.

The confirm route's match check validates ONLY feature_name / derives_pairs / aggregation against the
SERVER-reconstructed chosen feature; ``grain_table`` and ``derives_from`` are NOT matched, yet they
drive the confirm-time MCV re-run (confirm_contract -> validate_minimum -> _validate_idea). Before the
fix a caller could echo a matching name/derives_pairs/aggregation and:

* send a TRIMMED ``derives_from`` — an operand kept in ``derives_pairs`` but dropped here never enters
  the per-operand dispositions (``validate_minimum`` builds ``known`` from the body's derives_from), or
* send ``grain_table=None`` — the grain + cross-table join dispositions are gated on ``if grain_table``
  and silently no-op,

ERASING honest requirements and flipping NEEDS_EXTERNAL_VALIDATION -> DESIGN_CHECKED at the GOVERNING
write. The fix (routes/contract.py, right after the match check) overwrites BOTH from the server
``chosen``, so the tampered request still SUCCEEDS (200 — it matches the human's recorded choice) but
the persisted contract keeps the honest state. These tests drive the REAL route (the fix lives there,
not in confirm_contract).

Harness: the FakeLLM /considered-set flow cannot ground a NEEDS_EXTERNAL_VALIDATION feature onto an
operational-unknown / file-declared-grain column, so the Gate-#1 server state (intent + considered-set
snapshot + recorded choice) is SEEDED directly on the API suite's rolled-back conn — mirroring
tests/featuregen/overlay/upload/contract/test_validation_persistence.py — and the confirm body is then
POSTed through make_client, which serves requests on that same conn.
"""
import json
from datetime import UTC, datetime

from tests.featuregen.api._helpers import AUTH

from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow
from featuregen.overlay.upload.contract.gate1 import ConsideredSet, _snapshot, confirm_gate1
from featuregen.overlay.upload.feature_assist import FeatureIdea, FeatureSet, Requirement
from featuregen.overlay.upload.graph import build_graph


def _bank(conn) -> None:
    """RF-C2 catalog + a FRESH drift watermark (the route confirms with now=wall-clock, so the MCV
    freshness gate is LIVE here, unlike the direct confirm_contract tests). ``balance`` is genuinely
    operational-unknown with a numeric declared hint (-> TYPE_IS_NUMERIC), ``amount`` is operationally
    numeric (clears the numeric disposition), ``id`` is the FILE-declared — never governed — grain
    (-> GRAIN_IS_UNIQUE). Both measures carry unit+currency so a combining op adds no UNIT/CURRENCY
    requirements and the honest requirement set stays single-cause."""
    build_graph(conn, "bank", [
        CanonicalRow("bank", "accounts", "id", "integer", is_grain=True),
        CanonicalRow("bank", "accounts", "balance", UNKNOWN_TYPE,
                     definition="end-of-day ledger balance", unit="dollars", currency="USD"),
        CanonicalRow("bank", "accounts", "amount", "numeric",
                     definition="posted amount", unit="dollars", currency="USD")],
        declared_types={"public.accounts.balance": "numeric"})
    conn.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES ('bank', %s, 'r', 0)", (datetime.now(UTC),))


def _seed_gate1(conn, intent_id: str, idea: FeatureIdea) -> None:
    """The server state /contract/confirm reads: the intent row (FK target), the considered-set
    snapshot (``chosen_feature`` reconstructs the chosen from HERE) and the recorded Gate #1 choice."""
    conn.execute(
        "INSERT INTO contract_intent (intent_id, hypothesis, intake_mode) "
        "VALUES (%s, 'h', 'hypothesis')", (intent_id,))
    cs = ConsideredSet(intent_id, None, [FeatureSet("templates", [idea])], None)
    conn.execute(
        "INSERT INTO contract_considered (intent_id, considered) VALUES (%s, %s::jsonb)",
        (intent_id, json.dumps(_snapshot(conn, cs))))
    confirm_gate1(conn, cs, chosen_source="alternative", chosen_option_id=idea.name,
                  actor="user:tester")


def _contract_row(conn, contract_id: str):
    return conn.execute(
        "SELECT validation_status, requirements FROM contract WHERE contract_id = %s",
        (contract_id,)).fetchone()


def test_confirm_trimmed_derives_from_cannot_erase_type_requirement(make_client, conn):
    """Tamper: DROP the operational-unknown measure's ref from the body's ``derives_from`` while
    KEEPING it in ``derives_pairs`` (name/pairs/aggregation still match the recorded choice, so the
    request passes the match check). The fix rebuilds derives_from from the server chosen, so the
    confirm-time re-run still reasons over ``balance`` and the persisted contract stays
    NEEDS_EXTERNAL_VALIDATION + TYPE_IS_NUMERIC instead of flipping to DESIGN_CHECKED."""
    _bank(conn)
    idea = FeatureIdea(
        name="avg_balance", description="",
        derives_from=["public.accounts.balance", "public.accounts.amount"],
        aggregation="avg", grain_table=None,
        derives_pairs=(("bank", "public.accounts.balance"), ("bank", "public.accounts.amount")),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("TYPE_IS_NUMERIC", ("bank", "public.accounts.balance"),
                                  "operational type unknown; numeric declared hint"),))
    _seed_gate1(conn, "intent-df", idea)
    client = make_client()
    honest = {"feature_name": "avg_balance", "definition": "Average of balance and amount.",
              "grain_table": None, "aggregation": "avg", "as_of_column": None,
              "derives_from": ["public.accounts.balance", "public.accounts.amount"],
              "derives_pairs": [["bank", "public.accounts.balance"],
                                ["bank", "public.accounts.amount"]],
              "join_path": [], "intent_id": "intent-df"}
    res = client.post("/contract/confirm", json=honest, headers=AUTH)
    assert res.status_code == 200, res.text
    row = _contract_row(conn, res.json()["contract_id"])
    assert row[0] == "NEEDS_EXTERNAL_VALIDATION"       # the honest confirm-time re-run state
    assert "TYPE_IS_NUMERIC" in [r["code"] for r in row[1]]

    tampered = {**honest, "derives_from": ["public.accounts.amount"]}   # balance ref erased
    res = client.post("/contract/confirm", json=tampered, headers=AUTH)
    assert res.status_code == 200, res.text            # the match check DOES pass (pairs untouched)
    row = _contract_row(conn, res.json()["contract_id"])
    assert row[0] == "NEEDS_EXTERNAL_VALIDATION", (
        "a trimmed derives_from erased the honest requirement at the governing write")
    assert "TYPE_IS_NUMERIC" in [r["code"] for r in row[1]]


def test_confirm_null_grain_table_cannot_erase_grain_requirement(make_client, conn):
    """Tamper (the Critical vector): send ``grain_table=None`` with everything matched — the grain
    disposition is gated on ``if grain_table and single-catalog``, so pre-fix it silently no-ops and
    GRAIN_IS_UNIQUE vanishes. The fix restores grain_table from the server chosen, so the persisted
    contract stays NEEDS_EXTERNAL_VALIDATION + GRAIN_IS_UNIQUE."""
    _bank(conn)
    idea = FeatureIdea(
        name="distinct_accounts", description="", derives_from=["public.accounts.id"],
        aggregation="count_distinct", grain_table="accounts",
        derives_pairs=(("bank", "public.accounts.id"),),
        validation_status="NEEDS_EXTERNAL_VALIDATION",
        requirements=(Requirement("GRAIN_IS_UNIQUE", ("bank", "public.accounts.id"),
                                  "grain declared, not governed-verified"),))
    _seed_gate1(conn, "intent-gt", idea)
    client = make_client()
    honest = {"feature_name": "distinct_accounts", "definition": "Distinct account count.",
              "grain_table": "accounts", "aggregation": "count_distinct", "as_of_column": None,
              "derives_from": ["public.accounts.id"],
              "derives_pairs": [["bank", "public.accounts.id"]],
              "join_path": [], "intent_id": "intent-gt"}
    res = client.post("/contract/confirm", json=honest, headers=AUTH)
    assert res.status_code == 200, res.text
    row = _contract_row(conn, res.json()["contract_id"])
    assert row[0] == "NEEDS_EXTERNAL_VALIDATION"       # honest: the file-declared grain needs check
    assert [r["code"] for r in row[1]] == ["GRAIN_IS_UNIQUE"]

    tampered = {**honest, "grain_table": None}         # the grain disposition's gate erased
    res = client.post("/contract/confirm", json=tampered, headers=AUTH)
    assert res.status_code == 200, res.text            # matched fields untouched -> passes the check
    row = _contract_row(conn, res.json()["contract_id"])
    assert row[0] == "NEEDS_EXTERNAL_VALIDATION", (
        "grain_table=None silenced the grain disposition at the governing write")
    assert "GRAIN_IS_UNIQUE" in [r["code"] for r in row[1]]
