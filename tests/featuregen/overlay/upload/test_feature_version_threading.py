"""Slice 3A-iv Task 2: the feature-gen versions thread to `audited_structured_call` and land on the
immutable `llm_call` record — 2 when FEATUREGEN_FEATURE_CONTEXT is on, 1 when off (byte-for-byte v1)."""
from __future__ import annotations

from featuregen.intake.llm import FakeLLM, FakeResponse
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload import feature_assist
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


def test_versions_are_3_when_the_rollback_override_selects_v3(db, monkeypatch):
    """Bumped 2 -> 3 when `ai_summary` joined the column descriptor. Both numbers come from the one
    `_feature_schema_version`, so the contract carries a single version — leaving it at 2 would make
    a v2 record ambiguous, with or without summaries, which is the one thing the stamp exists to
    prevent.

    v3 is now reached through the D8 rollback ladder (`FEATUREGEN_FEATURE_CONTEXT_VERSION=3`)
    rather than by being the default. That the shipped behaviour stays REACHABLE — rather than a
    rollback dropping to the v1 thin menu — is the property the override exists for, so the
    threading is pinned here at v3 and at the current default below."""
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(feature_assist.FEATURE_CONTEXT_VERSION_ENV, "3")
    assert feature_context_enabled() is True
    _bank_graph(db)
    recommend_features(db, "predict churn", _fake(), catalog_source="bank", critic=False)
    rows = _feature_ideas_versions(db)
    assert rows, "recommend must record at least one feature_ideas llm_call"
    assert all(tuple(r) == (3, 3) for r in rows), rows


def test_versions_are_the_current_contract_when_flag_on(db, monkeypatch):
    """The default branch of the same ladder: both numbers still come from the ONE
    `_feature_schema_version`, so an audit record can never disagree with itself."""
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.delenv(feature_assist.FEATURE_CONTEXT_VERSION_ENV, raising=False)
    current = feature_assist._FEATURE_CONTEXT_SCHEMA_VERSION
    _bank_graph(db)
    recommend_features(db, "predict churn", _fake(), catalog_source="bank", critic=False)
    rows = _feature_ideas_versions(db)
    assert rows, "recommend must record at least one feature_ideas llm_call"
    assert all(tuple(r) == (current, current) for r in rows), rows


def test_a_recommendation_SURVIVES_with_the_flag_on(db, monkeypatch):
    """The version stamp is worthless if the call it stamps fails.

    Bumping the contract to 3 without registering a v3 schema alias made `schema_for(id, 3)` return
    None, so structured output went unenforced and the response failed repair — feature generation
    returned NOTHING with the flag on, while the version test above still passed because it only
    inspects the recorded number.

    Mutation-proven: drop the v3 alias and flag-on yields 0 features while flag-off yields 1.
    """
    _bank_graph(db)
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    on = recommend_features(db, "predict churn", _fake(), catalog_source="bank", critic=False)
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    off = recommend_features(db, "predict churn", _fake(), catalog_source="bank", critic=False)
    assert len(on) > 0, "flag-on produced no features — the widened contract broke generation"
    assert len(on) == len(off), "the flag changes GROUNDING, never whether generation works"


def test_every_feature_schema_resolves_at_the_stamped_version(db):
    """The general form: whatever version the request stamps must be registered, for every schema
    the feature path can emit — not just the one the happy-path test happens to exercise."""
    from featuregen.overlay.upload.enrich_llm import register_enrichment_schemas
    from featuregen.documents.registry import DocumentSchemaRegistry

    register_enrichment_schemas(db)
    reg = DocumentSchemaRegistry(db)
    for schema_id in ("feature_ideas", "feature_recipe", "leakage", "feature_set_rec"):
        for version in (1, 2, 3):
            assert reg.schema_for(schema_id, version) is not None, f"{schema_id} v{version}"
