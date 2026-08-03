"""Semantic Task 9 — the four retrieval legs, the versioned intent input, and the learning gap.

D12.2 fixes the ordering the plan proposed inverting: grain/time is leg 1 BY DESIGN (the shipped
expressibility invariant), lexical is leg 2, controlled semantic expansion is leg 3 and a bounded
one-hop available-link neighbourhood is leg 4.

What this file pins:

* leg 1 still bypasses the column budget — the founding invariant, and the one an "add a leg"
  refactor is most likely to break;
* leg 3 expands on CONTROLLED vocabulary (concept, domain, sub-domain, entity, glossary terms and
  the D13.1 BIAN/process paths) and finds a column whose NAME shares nothing with the question —
  the functional improvement, with no embeddings anywhere;
* leg 4 offers one-hop link partners, both endpoints re-scoped, bounded;
* THE OFFERED SET STAYS CLOSED: every ref legs 3/4 add is in `column_refs` before the model is
  called, and `validate_intent` still rejects anything outside it — a neighbour that was not
  offered can never enter a plan;
* truncation is reported PER LEG;
* the v2 intent input carries the bundles and the missing-context codes in the SAME metadata block
  the offered refs already ride in — placement unchanged, which is what the recorded
  repair-exhaustion history demands;
* a retrieval refusal now RECORDS a learning gap, which is what finally gives that store a
  production producer — with the subject taken from REDACTED text so a customer name in a question
  can never become an ontology candidate.
"""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.analysis.intent import (
    AnalysisIntentInputV2,
    IntentCandidates,
    validate_intent,
)
from featuregen.analysis.retrieval import (
    LEG_GRAIN_TIME,
    LEG_LEXICAL,
    LEG_LINK_NEIGHBOURHOOD,
    LEG_SEMANTIC_EXPANSION,
    LEGS,
    RetrievalBudget,
    record_retrieval_gap,
    retrieve_candidates,
)
from tests.featuregen._helpers import mint_test_identity

from featuregen.contracts import SchemaValidationError
from featuregen.overlay.upload.graph import rebuild_search_doc

_ACTOR = mint_test_identity(subject="user:analyst", role_claims=("data_scientist",))

_NOW = datetime(2026, 7, 30, tzinfo=UTC)


def _watermark(db, source: str) -> None:
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id) "
        "VALUES (%s, %s, 'r1') ON CONFLICT (catalog_source) DO UPDATE "
        "  SET last_completed_at = EXCLUDED.last_completed_at", (source, _NOW))


def _column(db, source, table, column, *, definition=None, grain=False, as_of=False,
            sensitivity=None, concept=None, domain=None, sub_domain=None, entity=None,
            semantic_terms=None, bian_path=None, process_path=None):
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "  definition, is_grain, is_as_of, sensitivity, concept, domain, sub_domain, entity, "
        "  semantic_terms, bian_path, process_path, search_doc) "
        "VALUES (%s,%s,'column',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, "
        "        setweight(to_tsvector('english', coalesce(%s,'')),'A') || "
        "        setweight(to_tsvector('english', coalesce(%s,'')),'B')) "
        "ON CONFLICT (catalog_source, object_ref) DO NOTHING",
        (source, f"public.{table}.{column}", table, column, definition, grain, as_of, sensitivity,
         concept, domain, sub_domain, entity, semantic_terms, bian_path, process_path,
         column, definition))
    # Rebuild the document through the PRODUCTION expression, so this fixture indexes concept /
    # domain / entity exactly as an upload does. Hand-rolling a narrower doc here would make the
    # expansion leg untestable for the very reason it exists.
    rebuild_search_doc(db, source, f"public.{table}.{column}")


def _table(db, source, table, *, definition=None, business_context=None, domain=None,
           data_role=None, primary_entity=None):
    """A TABLE node carrying the profile prose leg 3 harvests (profile Task 5)."""
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, "
        "  definition, business_context, domain, data_role, primary_entity) "
        "VALUES (%s,%s,'table',%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT (catalog_source, object_ref) DO NOTHING",
        (source, f"public.{table}", table, definition, business_context, domain, data_role,
         primary_entity))


@pytest.fixture
def catalog(db):
    """A payments fact with a governed grain + as-of, and a SEPARATE table whose only connection to
    the question is its shared controlled vocabulary — the case leg 3 exists for."""
    _watermark(db, "ftr")
    _column(db, "ftr", "tran_repos", "cif_id", definition="party identifier on the posting",
            grain=True, concept="customer_id", domain="Payments", entity="customer")
    _column(db, "ftr", "tran_repos", "tran_month", definition="posting period partition",
            as_of=True, concept="event_time", domain="Payments")
    _column(db, "ftr", "tran_repos", "tran_amt",
            definition="value of the transaction posted to the account",
            concept="monetary_flow", domain="Payments", sub_domain="Retail Payments",
            bian_path="Payment Order", process_path="Payments > Screening")
    # NOTHING in this column's name or definition shares a word with the question. It is reachable
    # ONLY through the controlled vocabulary its sibling carries (`monetary_flow`, `Payments`,
    # the BIAN path) — which is precisely the functional improvement leg 3 is for.
    _column(db, "ftr", "settlement_ledger", "stlmt_val", definition="booked settlement figure",
            concept="monetary_flow", domain="Payments", bian_path="Payment Order")
    _column(db, "ftr", "settlement_ledger", "stlmt_ref", definition="settlement reference",
            grain=True)
    return db


def _legs(retrieval) -> dict:
    return {leg.leg: leg for leg in retrieval.legs}


# ── leg order and the founding invariant ────────────────────────────────────────────────────────


def test_all_four_legs_are_reported_in_order_even_when_one_finds_nothing(catalog):
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    assert tuple(leg.leg for leg in got.legs) == LEGS
    # An ABSENT entry could not distinguish "found nothing" from "never ran".
    assert len(got.legs) == 4


def test_leg_one_still_bypasses_the_column_budget(catalog):
    """The invariant an "add two legs" refactor is most likely to break: a budget that could drop
    the grain or the time anchor yields a richer-looking set that cannot express a single
    period-over-period question."""
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW,
                              budget=RetrievalBudget(max_columns=1))
    assert "ftr::tran_repos.cif_id" in got.candidates.column_refs
    assert "ftr::tran_repos.tran_month" in got.candidates.column_refs
    assert _legs(got)[LEG_GRAIN_TIME].dropped == 0


def test_the_empty_answer_still_reports_all_four_legs(catalog):
    got = retrieve_candidates(catalog, "zzzz nothing matches this", now=_NOW)
    assert got.is_empty
    assert tuple(leg.leg for leg in got.legs) == LEGS
    assert got.empty_reason


# ── leg 3: controlled semantic expansion, no embeddings ─────────────────────────────────────────


def test_expansion_reaches_a_column_whose_name_shares_nothing_with_the_question(catalog):
    """`stlmt_val` contains none of the question's words. It arrives because its CONCEPT
    (`monetary_flow`), domain and BIAN path are the vocabulary the matched columns carry."""
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    assert "ftr::settlement_ledger.stlmt_val" in got.candidates.column_refs
    assert _legs(got)[LEG_SEMANTIC_EXPANSION].offered > 0


def test_expansion_terms_are_controlled_platform_vocabulary_not_the_question(catalog):
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    # Terms are TOKENISED, not passed through whole: the search document indexes a concept
    # HUMANIZED (`monetary_flow` -> "monetary flow"), so a query carrying the snake_case token
    # would match nothing. Splitting here is what makes the expansion actually reach a document.
    assert {"monetary", "flow"} <= set(got.expansion_terms)
    assert "payments" in got.expansion_terms          # the governed domain, lower-cased
    assert "payment" in got.expansion_terms           # from the D13.1 BIAN path
    assert "retail" in got.expansion_terms            # from the D13.2 sub-domain
    assert "screening" in got.expansion_terms         # from the D13.1 process path
    # Deterministic, so the same catalog state produces the same prompt.
    assert list(got.expansion_terms) == sorted(got.expansion_terms)


def test_expansion_never_harvests_vocabulary_from_a_column_the_caller_cannot_read(db):
    """A restricted column's CONCEPT is as disclosing as its name. Expansion is read-scoped at the
    harvest, not only at the offer."""
    _watermark(db, "ftr")
    _column(db, "ftr", "t", "id", definition="the identifier", grain=True)
    _column(db, "ftr", "t", "hidden_col", definition="the identifier detail",
            sensitivity="restricted", concept="emirates_identity_number")
    got = retrieve_candidates(db, "the identifier", now=_NOW)
    assert "emirates_identity_number" not in " ".join(got.expansion_terms)


# ── leg 3: the TABLE-profile harvest is read-scoped like the column harvest ─────────────────────


@pytest.fixture
def two_catalogs(db, monkeypatch):
    """The reviewer's cross-source probe.

    Two catalogs whose tables share a NAME. `srca.shared_name` is legitimately offered; `srcb`
    reaches the harvest's source list only through a DIFFERENT table, and `srcb.shared_name` — whose
    every column is restricted — is the pair the ANY/ANY cross product invents."""
    from featuregen.overlay.upload.profile_vocab import DATASET_PROFILES_FLAG

    monkeypatch.setenv(DATASET_PROFILES_FLAG, "1")
    _watermark(db, "srca")
    _watermark(db, "srcb")
    _column(db, "srca", "shared_name", "settlement_id", definition="settlement identifier",
            grain=True)
    _table(db, "srca", "shared_name", definition="the clearing ledger",
           business_context="run by the clearing operations desk", domain="Clearing",
           data_role="fact", primary_entity="settlement")
    _column(db, "srcb", "other_tbl", "settlement_code", definition="settlement identifier code")
    _table(db, "srcb", "other_tbl", definition="reference data", domain="Reference")
    _column(db, "srcb", "shared_name", "deal_id", definition="restricted deal identifier",
            sensitivity="restricted")
    _table(db, "srcb", "shared_name", definition="zzsecretword merger book",
           business_context="zzsecretword project", domain="Zzconfidential")
    return db


def test_the_table_profile_harvest_never_leaks_a_restricted_table_from_another_source(
        two_catalogs):
    """The harvest bound `catalog_source = ANY` AND `table_name = ANY` with no post-filter, so a
    table name shared across two catalogs pulled the OTHER catalog's prose into the expansion — and
    from there into `expansion_terms` and the `/analysis/plan` response."""
    got = retrieve_candidates(two_catalogs, "settlement identifier", now=_NOW)
    # The offered set is what the caller may read — both visible columns, neither restricted one.
    assert "srca::shared_name.settlement_id" in got.candidates.column_refs
    assert "srcb::other_tbl.settlement_code" in got.candidates.column_refs
    assert "srcb::shared_name.deal_id" not in got.candidates.column_refs

    joined = " ".join(got.expansion_terms)
    assert "zzsecretword" not in joined
    assert "zzconfidential" not in joined
    assert "merger" not in got.expansion_terms
    # …and the legitimately-offered table's OWN prose still expands: the fix narrows the harvest,
    # it does not switch it off.
    assert "clearing" in got.expansion_terms
    assert "ledger" in got.expansion_terms


def test_the_table_profile_harvest_refuses_a_table_with_no_readable_column(two_catalogs):
    """Defence in depth, independent of the pair post-filter: asked DIRECTLY for the restricted
    table's own pair, the harvest query's derived-visibility predicate still refuses it. Table nodes
    carry `visible_requires = {}`, so their scope can only ever be DERIVED from their columns."""
    from featuregen.analysis.retrieval import _expansion_terms

    terms = _expansion_terms(two_catalogs, [("srcb", "shared_name", "deal_id")], roles=(),
                             limit=50)
    assert "zzsecretword" not in " ".join(terms)


def test_expansion_cannot_widen_the_catalog_without_bound(catalog):
    """Expansion is meant to reach the table a question IMPLIES, not to widen the catalog until
    something matches. With no room for a new table, the settlement column cannot arrive."""
    wide = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    assert "ftr::settlement_ledger.stlmt_val" in wide.candidates.column_refs

    narrow = retrieve_candidates(catalog, "transaction value by customer", now=_NOW,
                                 budget=RetrievalBudget(max_expansion_tables=0))
    assert "ftr::settlement_ledger.stlmt_val" not in narrow.candidates.column_refs
    assert "ftr::settlement_ledger" not in narrow.candidates.table_refs
    # …and the structural answer is unchanged: a bound narrows expansion, never leg 1.
    assert wide.candidates.grain_refs >= {"ftr::tran_repos.cif_id"}
    assert narrow.candidates.grain_refs >= {"ftr::tran_repos.cif_id"}


def test_no_embeddings_are_used_anywhere_in_retrieval():
    """Task 9 is explicit: exact controlled expansion first; embeddings can be measured later. A
    vector dependency creeping in here would change the failure mode from "did not match" to
    "matched something nothing sanctioned"."""
    import featuregen.analysis.retrieval as module

    import ast

    tree = ast.parse(open(module.__file__, encoding="utf-8").read())
    # CODE, not prose: the module's own docstring says the word "embeddings" on purpose (it is the
    # decision being recorded). What must be absent is a dependency or an identifier.
    identifiers = {
        node.id if isinstance(node, ast.Name) else getattr(node, "attr", "")
        for node in ast.walk(tree) if isinstance(node, (ast.Name, ast.Attribute))
    }
    imports = {
        alias.name for node in ast.walk(tree)
        if isinstance(node, ast.Import) for alias in node.names
    } | {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    for banned in ("embedding", "embeddings", "vector", "cosine", "faiss", "pgvector"):
        assert not any(banned in name.lower() for name in identifiers), banned
        assert not any(banned in name.lower() for name in imports), banned


# ── leg 4: bounded one-hop link neighbourhood ───────────────────────────────────────────────────


def test_the_link_leg_reports_itself_even_with_no_links(catalog):
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    leg = _legs(got)[LEG_LINK_NEIGHBOURHOOD]
    assert leg.leg == LEG_LINK_NEIGHBOURHOOD
    assert leg.offered == 0 and leg.considered == 0


# ── the offered set stays CLOSED ────────────────────────────────────────────────────────────────


def test_every_ref_any_leg_added_is_inside_the_offered_set(catalog):
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    offered = got.candidates.column_refs
    # The labels and role sets are projections of the offered set, never a second, wider one.
    assert set(got.candidates.labels) <= offered
    assert got.candidates.grain_refs <= offered
    assert got.candidates.as_of_refs <= offered
    # And every bundle explains a ref that was actually offered.
    for entry in got.context_bundles:
        assert entry["ref"] in offered


def test_a_neighbour_ref_that_was_not_offered_is_still_rejected(catalog):
    """The closed-candidate guarantee, unchanged by the new legs: expansion widens the OFFERED set
    before the call; it never lets an output ref bypass validation."""
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    with pytest.raises(SchemaValidationError) as exc:
        validate_intent({
            "entity_ref": "ftr::some_other_table.some_col",
            "base_table_ref": "ftr::tran_repos",
            "measure": {"op": "count", "logical_ref": ""},
            "windows": [], "dimensions": [], "comparison": "", "unresolved": [],
        }, got.candidates)
    assert "not one of the columns offered" in str(exc.value)


# ── the versioned input contract ────────────────────────────────────────────────────────────────


def test_the_v2_input_keeps_the_offered_refs_exactly_where_they_were(catalog):
    """Placement is the recorded hazard: moving metadata out of the user turn once separated the
    refs from the instruction telling the model to choose only from them, and the extraction failed
    validation until the repair budget ran out."""
    from featuregen.intake.redaction import INPUT_KEY_CATALOG

    from featuregen.analysis import intent as intent_module

    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    captured: dict = {}

    class _Capture:
        def call(self, request):
            captured["request"] = request
            raise AssertionError("stop after capture")

    with pytest.raises(AssertionError):
        intent_module.extract_intent(
            catalog, _Capture(), "transaction value by customer",
            AnalysisIntentInputV2(candidates=got.candidates, context=got.context_bundles,
                                  missing_context=("definition_missing",)),
            actor=_ACTOR)
    request = captured["request"]
    metadata = request.inputs[INPUT_KEY_CATALOG]
    # SAME block, new keys.
    assert set(metadata) >= {"column_refs", "table_refs", "labels", "instruction",
                             "contract_version", "semantic_context", "missing_context"}
    assert metadata["contract_version"] == 2
    assert sorted(got.candidates.column_refs) == metadata["column_refs"]
    # No cacheable-metadata split: the refs stay in the user turn beside their instruction.
    assert request.cacheable_metadata_keys == ()
    # The version is stamped on the request so the immutable record says WHICH input egressed.
    assert request.prompt_version == 2


def test_the_v1_input_is_still_accepted_and_stamps_v1(catalog):
    from featuregen.analysis import intent as intent_module

    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    captured: dict = {}

    class _Capture:
        def call(self, request):
            captured["request"] = request
            raise AssertionError("stop after capture")

    with pytest.raises(AssertionError):
        intent_module.extract_intent(catalog, _Capture(), "transaction value by customer",
                                     got.candidates, actor=_ACTOR)
    request = captured["request"]
    assert request.prompt_version == 1
    assert "semantic_context" not in request.inputs[
        __import__("featuregen.intake.redaction", fromlist=["x"]).INPUT_KEY_CATALOG]


def test_bundles_carry_authority_axes_and_missing_context_codes(catalog):
    from featuregen.overlay.upload.semantic_context import MISSING_CONTEXT_CODES

    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    assert got.context_bundles
    for entry in got.context_bundles:
        assert "ref" in entry
        assert set(entry.get("missing_context", ())) <= MISSING_CONTEXT_CODES
        for label in entry.get("authority", {}).values():
            producer, _, strength = label.partition("/")
            assert producer and strength


def test_a_bundle_FAULT_does_not_abort_the_CALLER_transaction(catalog, monkeypatch):
    """"Context is explanation; its absence must never change what may be named" is right, and the
    bare `except Exception` did not achieve it: a DATABASE fault leaves PostgreSQL's transaction
    ABORTED, so the caller's very next statement raises `InFailedSqlTransaction` and the planning
    request dies anyway — on a fault the offered set was defined to survive. The guarded read runs
    inside a SAVEPOINT."""
    from featuregen.overlay.upload import semantic_context

    def _bad_read(conn, *_args, **_kwargs):
        conn.execute("SELECT no_such_column_zz FROM graph_node")

    monkeypatch.setattr(semantic_context, "bundle_from_store", _bad_read)
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW)
    assert got.context_bundles == ()
    assert got.candidates.column_refs, "the offered set must be untouched by a context fault"
    assert catalog.execute("SELECT 1").fetchone() == (1,)


def test_bundles_are_bounded_far_below_the_offered_set(catalog):
    got = retrieve_candidates(catalog, "transaction value by customer", now=_NOW,
                              budget=RetrievalBudget(max_context_bundles=2))
    assert len(got.context_bundles) <= 2


# ── the learning gap gets a production producer ─────────────────────────────────────────────────


def test_a_retrieval_refusal_records_an_actionable_gap(catalog):
    event_id = record_retrieval_gap(
        catalog, "which merchants had chargeback velocity spikes", roles=(),
        analysis_request_id="areq-1", now=_NOW)
    assert event_id
    row = catalog.execute(
        "SELECT code, required_action, subject_refs FROM analysis_learning_event "
        "WHERE event_id = %s", (event_id,)).fetchone()
    assert row[0] == "SEMANTIC_TERM_UNRESOLVED"
    assert row[1] == "define_semantic_term"
    assert "chargeback" in row[2]


def test_the_same_unanswered_question_dedupes_under_one_catalog_state(catalog):
    first = record_retrieval_gap(catalog, "chargeback velocity", roles=(),
                                 analysis_request_id="areq-1", now=_NOW)
    again = record_retrieval_gap(catalog, "chargeback velocity", roles=(),
                                 analysis_request_id="areq-1", now=_NOW)
    assert first == again
    assert catalog.execute(
        "SELECT count(*) FROM analysis_learning_event WHERE kind = 'gap'").fetchone()[0] == 1


def test_a_new_catalog_state_re_records_because_the_gap_may_have_been_resolved(catalog):
    record_retrieval_gap(catalog, "chargeback velocity", roles=(), analysis_request_id="areq-1",
                         now=_NOW)
    catalog.execute("UPDATE overlay_drift_watermark SET last_completed_at = %s",
                    (datetime(2026, 8, 1, tzinfo=UTC),))
    record_retrieval_gap(catalog, "chargeback velocity", roles=(), analysis_request_id="areq-1",
                         now=_NOW)
    assert catalog.execute(
        "SELECT count(*) FROM analysis_learning_event WHERE kind = 'gap'").fetchone()[0] == 2


def test_the_gap_subject_comes_from_redacted_text_never_the_raw_question(catalog):
    """`subject_refs` is durable and reviewable. A question is free text from a person, so a
    customer name in it must never become an ontology candidate."""
    from featuregen.intake.redaction import redact_free_text

    # A pattern the deterministic detectors DO find, so the assertion below is about the
    # redaction actually happening rather than about a regex that never fired.
    question = "spend for customer 4111 1111 1111 1111 last month"
    assert redact_free_text(question).redacted_spans, (
        "this fixture only means something if the redactor actually finds the identifier")
    event_id = record_retrieval_gap(catalog, question, roles=(), analysis_request_id="areq-1",
                                    now=_NOW)
    subjects = catalog.execute(
        "SELECT subject_refs FROM analysis_learning_event WHERE event_id = %s",
        (event_id,)).fetchone()[0]
    assert "4111" not in " ".join(subjects)
    assert "1111" not in " ".join(subjects)
    # The ANALYSABLE words survive — the gap is still actionable.
    assert "spend" in subjects


def test_a_question_with_no_recordable_term_records_nothing(catalog):
    """A gap with no subject cannot be actioned or deduplicated; inventing one is worse than
    recording nothing."""
    assert record_retrieval_gap(catalog, "?? !!", roles=(), analysis_request_id="areq-1",
                                now=_NOW) is None
