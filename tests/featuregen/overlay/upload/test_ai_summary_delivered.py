"""ai_summary, delivered end to end.

The field was built, cached, projected and indexed — and then reported complete while four wires
were missing, so the asset screen declared it and rendered it empty on every column.

The deep one is the feature agent. `_FEATURE_COLUMN_DEFINITION_KEYS` is
`{"definition", "semantic_terms"}` (enrich_llm.py:203), so an `ai_summary` key on a column
descriptor is a genuinely-unknown key and BLOCKS the whole item. Adding it to the SQL and stopping
there would have made the column vanish from the menu rather than gain a summary — a silent
regression dressed as a feature.
"""
from __future__ import annotations

from featuregen.overlay.upload import feature_assist
from featuregen.overlay.upload.asset_detail import _ANCHOR_COLUMNS
from featuregen.overlay.upload.enrich_llm import (
    _FEATURE_COLUMN_DEFINITION_KEYS,
    sanitize_feature_context,
)
from featuregen.overlay.upload.stage_report import CANONICAL_STAGES


# ── the three reporting/display wires ────────────────────────────────────────────────────────────

def test_the_anchor_query_actually_selects_the_value():
    """`_METADATA_FIELDS` already declared the row; without this the value is always missing."""
    assert "ai_summary" in _ANCHOR_COLUMNS


def test_the_stage_is_canonical():
    assert "enrich_summary" in CANONICAL_STAGES


def test_the_stage_sits_at_the_ingest_tail():
    """Order matters: the report is read as a sequence, so a stage listed out of place implies it
    ran at a different point than it did. Richness Task 3 Step 6c MOVED the draft to the ingest
    TAIL — after the axis projection — so the ONE summary per ingest is written from the full
    enriched dossier (concept, party_role, term_type, processes, grain/table roles), not the
    file-side payload it used to paraphrase."""
    stages = list(CANONICAL_STAGES)
    assert stages.index("axis_projection") < stages.index("enrich_summary")
    assert stages.index("enrich_summary") < stages.index("quarantine")
    assert stages.count("enrich_summary") == 1                  # exactly one draft per ingest


# ── the feature-agent path ───────────────────────────────────────────────────────────────────────

def test_the_agent_query_selects_it():
    import inspect
    sql = inspect.getsource(feature_assist._candidate_columns)
    assert "ai_summary" in sql, "the agent cannot ground on a column it never selects"


def test_the_menu_carries_it_as_definition_like_prose():
    """Not an identity or fact field: it is prose, so it must ride the DEFINITION kind and be
    sample-stripped and PII-scanned like `definition` rather than passed through raw."""
    assert "ai_summary" in feature_assist._MENU_DEFINITION_FIELDS


def test_egress_permits_it_so_the_item_is_not_blocked():
    """THE trap. An unknown key blocks the whole column descriptor, so a half-wired ai_summary
    would have REMOVED columns from the menu instead of enriching them."""
    assert "ai_summary" in _FEATURE_COLUMN_DEFINITION_KEYS


def test_a_real_menu_column_carrying_a_summary_is_not_rejected():
    """Through the ACTUAL egress function, not a key-set assertion. The feature menu uses `columns`
    (Pass B's `column_profiles` is a different consumer with its own allowlist), and an
    unclassified key there returns None for the whole context — fail-closed."""
    ctx, _spans, _audits, _v = sanitize_feature_context({
        "columns": [{"object_ref": "public.bo_cib_customer.cust_curr_ntb_flg",
                     "column": "cust_curr_ntb_flg",
                     "definition": "Status or indicator used to classify customer condition.",
                     "ai_summary": "Whether the customer is currently new to the bank."}],
    })
    assert ctx is not None, "the column descriptor was rejected outright"
    assert ctx["columns"][0]["ai_summary"]


def test_an_unknown_prose_key_is_still_rejected():
    """The fail-closed rule is intact — this change widens the allowlist by exactly one key."""
    ctx, _s, _a, _v = sanitize_feature_context({
        "columns": [{"column": "c", "some_new_prose": "anything"}]})
    assert ctx is None


def test_relevance_scoring_reads_it():
    """A column findable in SEARCH by words that appear only in its summary must be findable to the
    agent by the same words, or the two surfaces disagree about what the catalog contains."""
    toks = feature_assist._column_tokens(
        {"ai_summary": "whether the customer is currently new to the bank"})
    assert "bank" in toks and "customer" in toks


# ── the audit record must identify WHICH contract egressed ───────────────────────────────────────

def test_the_input_contract_version_was_bumped(monkeypatch):
    """`_feature_schema_version` is stamped on the immutable llm_call so a reader knows what shape
    egressed. Adding `ai_summary` to the payload while leaving it at 2 would make a v2 record
    ambiguous — with or without summaries — defeating the reason it is stamped.

    v3 is the version THIS delivery minted, and it is still reachable on demand (semantic Task 8
    added v4 and the D8 rollback ladder, whose whole point is that v3 does not disappear). Pinned
    both ways: the override selects exactly v3, and the default selects the current contract."""
    monkeypatch.setenv("FEATUREGEN_FEATURE_CONTEXT", "1")
    monkeypatch.setenv(feature_assist.FEATURE_CONTEXT_VERSION_ENV, "3")
    assert feature_assist._feature_schema_version() == 3
    monkeypatch.delenv(feature_assist.FEATURE_CONTEXT_VERSION_ENV)
    assert feature_assist._feature_schema_version() == (
        feature_assist._FEATURE_CONTEXT_SCHEMA_VERSION)


def test_the_base_contract_is_v5_without_env(monkeypatch):
    """Pre-live simplification (2026-08-11): the on/off gate retired — v1 is unreachable; the
    ladder default (v5) stamps with no env, and the VERSION override remains the selector."""
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT", raising=False)
    monkeypatch.delenv("FEATUREGEN_FEATURE_CONTEXT_VERSION", raising=False)
    assert feature_assist._feature_schema_version() == 5


# ── the API key must match the evidence field name ───────────────────────────────────────────────

def test_the_payload_key_matches_the_evidence_field_name():
    """The client looks up `proposals_by_field[key]` and `latest_decision_by_field[key]` using the
    effective-metadata key. A display-shaped "ai summary" key rendered the value while silently
    DETACHING its proposal and decision history — visible, but with no provenance behind it."""
    from featuregen.overlay.upload.asset_detail import _METADATA_FIELDS
    keys = {label for label, _flat, _c1 in _METADATA_FIELDS}
    assert "ai_summary" in keys
    assert "ai summary" not in keys


def test_every_metadata_key_is_evidence_shaped():
    """The general form: no metadata key may contain a space, or its evidence silently detaches."""
    from featuregen.overlay.upload.asset_detail import _METADATA_FIELDS
    assert [k for k, _f, _c in _METADATA_FIELDS if " " in k] == []
