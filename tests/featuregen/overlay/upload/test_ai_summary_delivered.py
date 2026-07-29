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


def test_the_stage_sits_with_the_other_enrichment_stages():
    """Order matters: the report is read as a sequence, so a stage listed out of place implies it
    ran at a different point than it did."""
    stages = list(CANONICAL_STAGES)
    assert stages.index("enrich_definition") < stages.index("enrich_summary")
    assert stages.index("enrich_summary") < stages.index("enrich_domain")


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
