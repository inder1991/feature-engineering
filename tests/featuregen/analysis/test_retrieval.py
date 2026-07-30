"""Bounded retrieval — which catalog objects one question's prompt may see.

"Never put the whole catalog in a prompt" is a cost rule and a correctness rule: a model offered
150,000 columns picks plausible wrong ones and the wrongness never shows in the answer.

The property this module exists for is the one a relevance-only retriever gets wrong. A question
names CONCEPTS — "which customers had fewer transactions this month" mentions neither `cif_id` nor
`tran_month` — yet the plan needs both, one as `entity_ref` and one as a window's `anchor_ref`. Since
intent extraction now REJECTS a ref it was not offered, retrieving by relevance alone makes every
such question unanswerable: the model has nothing legitimate to name. So grain and as-of columns are
always offered, and offered FIRST so a budget can never drop them for a better-matching descriptive
column.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.analysis.retrieval import RetrievalBudget, retrieve_candidates

_NOW = datetime(2026, 7, 30, tzinfo=UTC)


@pytest.fixture
def catalog(db):
    """A transaction table with a governed grain + as-of, and a dimension table.

    `cif_id` and `tran_month` carry definitions that do NOT contain the question's words, which is
    the whole point: they must arrive through the structural leg, not by matching.
    """
    db.execute("INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id) "
               "VALUES ('ftr', %s, 'r1') "
               "ON CONFLICT (catalog_source) DO UPDATE "
               "  SET last_completed_at = EXCLUDED.last_completed_at",
               (_NOW,))
    rows = [
        # source, object_ref, table, column, definition, is_grain, is_as_of, sensitivity
        ("ftr", "public.tran_repos.cif_id", "tran_repos", "cif_id",
         "party identifier on the posting", True, False, None),
        ("ftr", "public.tran_repos.tran_month", "tran_repos", "tran_month",
         "posting period partition", False, True, None),
        ("ftr", "public.tran_repos.tran_amt", "tran_repos", "tran_amt",
         "value of the transaction posted to the account", False, False, None),
        ("ftr", "public.tran_repos.narrative", "tran_repos", "narrative",
         "free text describing the transaction", False, False, "restricted"),
        ("ftr", "public.cust_dim.segment", "cust_dim", "segment",
         "customer segment classification", False, False, None),
    ]
    # THE case the governed predicate exists for, and the one a raw-tag filter misses entirely: a
    # glossary attests no sensitivity, so the tag is NULL while the concept cascade independently
    # ruled the column restricted. On the real FTR catalog that was 28 of 126 columns, including a
    # national ID. It is also a GRAIN column, so it arrives through the structural leg — the path
    # that bypasses `search`'s own read-scope and needs its own.
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "  definition, is_grain, is_as_of, sensitivity, effective_restriction, search_doc) "
        "VALUES ('ftr','public.tran_repos.emirates_id','column','tran_repos','emirates_id',"
        "        'national identity number of the transacting customer', true, false, "
        "        NULL, 'restricted', "
        "        setweight(to_tsvector('english','emirates_id'),'A')) "
        "ON CONFLICT (catalog_source, object_ref) DO NOTHING")
    for source, ref, table, column, definition, grain, as_of, sensitivity in rows:
        db.execute(
            "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
            "  definition, is_grain, is_as_of, sensitivity, search_doc) "
            "VALUES (%s,%s,'column',%s,%s,%s,%s,%s,%s, "
            "        setweight(to_tsvector('english', coalesce(%s,'')),'A') || "
            "        setweight(to_tsvector('english', coalesce(%s,'')),'B')) "
            "ON CONFLICT (catalog_source, object_ref) DO NOTHING",
            (source, ref, table, column, definition, grain, as_of, sensitivity, column, definition))
    return db


# ── the property a relevance-only retriever gets wrong ───────────────────────────────────────────

def test_the_grain_and_as_of_columns_are_offered_even_though_the_question_never_names_them(catalog):
    """THE test. "transaction" matches `tran_amt`'s definition; nothing in the question matches
    `cif_id` or `tran_month`. Without them the model cannot fill `entity_ref` or a window's
    `anchor_ref`, and since an un-offered ref is rejected, the question becomes unanswerable."""
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    assert "ftr::tran_repos.cif_id" in got.candidates.column_refs
    assert "ftr::tran_repos.tran_month" in got.candidates.column_refs


def test_the_structural_columns_survive_a_budget_too_small_for_them(catalog):
    """Ordering, asserted. A budget applied to relevance first would keep the best-matching
    descriptive column and drop the grain — producing a richer-looking set that cannot express a
    single period-over-period question."""
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW,
                              budget=RetrievalBudget(max_columns=2))
    assert got.candidates.column_refs == {"ftr::tran_repos.cif_id", "ftr::tran_repos.tran_month"}
    assert got.dropped_columns > 0


# ── read scope: this set becomes prompt text ─────────────────────────────────────────────────────

def test_a_column_the_caller_may_not_read_is_never_offered_to_the_model(catalog):
    """`narrative` is `restricted`. A default caller must not see it — and here "seeing" means it
    would be written into an LLM prompt, which is exactly what the governed read-scope fix exists to
    prevent."""
    got = retrieve_candidates(catalog, "free text narrative describing the transaction", now=_NOW)
    assert "ftr::tran_repos.narrative" not in got.candidates.column_refs


def test_a_caller_holding_the_visibility_class_does_see_it(catalog):
    """The negative test's partner — otherwise "not offered" could just mean the query is broken.

    The grant comes from `restricted_reader`, NOT from a functional role like `platform_admin`:
    visibility classes are a separate axis on purpose, so being an administrator of the platform does
    not by itself entitle you to the contents of a restricted column."""
    got = retrieve_candidates(catalog, "free text narrative describing the transaction", now=_NOW,
                              roles=("restricted_reader",))
    assert "ftr::tran_repos.narrative" in got.candidates.column_refs


def test_an_UNTAGGED_column_the_concept_cascade_restricted_is_not_offered(catalog):
    """The leak that was live on the deployed catalog, asserted on the path that could reintroduce it.

    `emirates_id` carries `sensitivity = NULL` — a business glossary attests none — and an
    `effective_restriction` of `restricted` derived by the concept cascade. A filter on the raw TAG
    lets it through; only `visible_requires` catches it. It is a GRAIN column, so it comes via the
    structural leg, which does its own read-scoping rather than inheriting `search`'s.

    Failing this test means a national ID number is being written into an LLM prompt.
    """
    got = retrieve_candidates(catalog, "customer identity for the transaction", now=_NOW)
    assert "ftr::tran_repos.emirates_id" not in got.candidates.column_refs


def test_the_same_column_IS_offered_to_a_holder_of_the_class(catalog):
    got = retrieve_candidates(catalog, "customer identity for the transaction", now=_NOW,
                              roles=("restricted_reader",))
    assert "ftr::tran_repos.emirates_id" in got.candidates.column_refs


def test_a_PLATFORM_ADMIN_alone_does_not_unlock_a_restricted_column(catalog):
    """Pins the separation, because assuming otherwise is the natural mistake — and here it would put
    restricted text into an LLM prompt for anyone with an admin role."""
    got = retrieve_candidates(catalog, "free text narrative describing the transaction", now=_NOW,
                              roles=("platform_admin",))
    assert "ftr::tran_repos.narrative" not in got.candidates.column_refs


# ── bounding, reported ───────────────────────────────────────────────────────────────────────────

def test_truncation_is_reported_not_silent(catalog):
    """A bound that quietly discards matches reads as "this is everything relevant" — the same
    defect class as a stage that reports success and produces nothing."""
    got = retrieve_candidates(catalog, "transaction customer segment", now=_NOW,
                              budget=RetrievalBudget(max_columns=3))
    assert got.dropped_columns > 0


def test_nothing_is_dropped_when_it_all_fits(catalog):
    got = retrieve_candidates(catalog, "transaction customer segment", now=_NOW)
    assert got.dropped_columns == 0


def test_the_table_budget_keeps_the_best_matching_tables_whole(catalog):
    """Ranked by their best column, so a tight budget yields fewer COMPLETE tables rather than a
    scattering of columns from many — a half-retrieved table cannot be planned against."""
    got = retrieve_candidates(catalog, "customer segment classification", now=_NOW,
                              budget=RetrievalBudget(max_tables=1))
    assert len(got.candidates.table_refs) == 1
    assert got.tables_considered == tuple(got.candidates.table_refs)


def test_a_question_matching_nothing_reports_why_rather_than_returning_an_empty_set(catalog):
    got = retrieve_candidates(catalog, "zzzz nonexistent terminology", now=_NOW)
    assert got.is_empty
    assert "matched" in got.empty_reason


# ── the refs are the ones grounding can actually resolve ─────────────────────────────────────────

def test_the_refs_are_in_the_form_grounding_parses(catalog):
    """`grounding._parse` reads the last two dotted segments and looks up `public.<table>.<column>`.
    A ref in any other shape resolves to nothing and reads as a catalog gap rather than a retrieval
    bug — which is how a whole class of defect stayed hidden in this codebase."""
    from featuregen.analysis.grounding import _parse

    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    for ref in got.candidates.column_refs:
        source, table, column = _parse(ref)
        assert source == "ftr"
        assert table and column
    for ref in got.candidates.table_refs:
        _source, table, _ = _parse(ref + ".x")
        assert table


def test_retrieval_feeds_intent_extraction_without_a_single_rejected_ref(catalog):
    """The join between the two modules: everything retrieval offers must pass intent's candidate
    check, or the two have drifted on ref format and every question fails validation."""
    from featuregen.analysis.intent import validate_intent

    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    output = {
        "entity": "customer", "entity_ref": "ftr::tran_repos.cif_id",
        "base_table_ref": "ftr::tran_repos",
        "measure": {"op": "count", "logical_ref": ""},
        "windows": [{"label": "current", "anchor_ref": "ftr::tran_repos.tran_month",
                     "calendar_unit": "month", "calendar_length": 1, "calendar_offset": 0}],
        "dimensions": [], "comparison": "decrease", "unresolved": [],
    }
    validate_intent(output, got.candidates)      # must not raise
