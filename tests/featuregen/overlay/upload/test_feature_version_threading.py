"""Slice 3A-iv Task 2: the feature-gen versions thread to `audited_structured_call` and land on the
immutable `llm_call` record — 2 when FEATUREGEN_FEATURE_CONTEXT is on, 1 when off (byte-for-byte v1)."""
from __future__ import annotations

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.feature_assist import feature_context_enabled, recommend_features
from featuregen.overlay.upload.graph import build_graph


def _bank_graph(db):
    build_graph(db, "bank", [
        CanonicalRow("bank", "transactions", "acct_id", "integer",
                     joins_to="accounts.account_id", cardinality="N:1"),
        CanonicalRow("bank", "transactions", "amount", "numeric", definition="txn amount",
                     additivity="additive", unit="dollars", currency="USD", entity="Transaction"),
        CanonicalRow("bank", "transactions", "txn_date", "timestamp", as_of=True),
        CanonicalRow("bank", "accounts", "account_id", "integer", is_grain=True, entity="Account"),
        CanonicalRow("bank", "accounts", "churned", "boolean", definition="customer churned flag"),
    ])


def _fake():
    return FakeLLM(script={"overlay.feature.recommend": FakeResponse(output={"features": [
        {"name": "txn_count_90d", "description": "count of txns",
         "derives_from": ["public.transactions.amount"], "aggregation": "count", "grain_table": "accounts"},
    ]})})


def _feature_ideas_versions(db):
    return db.execute(
        "SELECT output_schema_version, prompt_version FROM llm_call "
        "WHERE output_schema_id = 'feature_ideas'").fetchall()


def test_flag_default_is_off():
    assert feature_context_enabled() is False


def test_versions_are_1_when_flag_off(db, monkeypatch):
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    _bank_graph(db)
    recommend_features(db, "predict churn", _fake(), catalog_source="bank", critic=False)
    rows = _feature_ideas_versions(db)
    assert rows, "recommend must record at least one feature_ideas llm_call"
    assert all(tuple(r) == (1, 1) for r in rows), rows


def test_versions_are_2_when_flag_on(db, monkeypatch):
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    assert feature_context_enabled() is True
    _bank_graph(db)
    recommend_features(db, "predict churn", _fake(), catalog_source="bank", critic=False)
    rows = _feature_ideas_versions(db)
    assert rows, "recommend must record at least one feature_ideas llm_call"
    assert all(tuple(r) == (2, 2) for r in rows), rows
