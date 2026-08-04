"""Task 2 — ``FeatureSuggestionV2`` over the REAL engine, and the byte-stable V1 adapter.

Everything here runs the actual per-table grounding pass over the suggestion suite's own FTR
fixtures, so the shapes asserted are the ones the engine produces rather than ones a test invented.
The four proofs this suite exists for:

1. **Trace consumption.** A ``DESIGN_CHECKED`` card whose decision trace is absent or cannot explain
   the decision is INADMISSIBLE — withheld and counted, never softened.
2. **Anchor independence.** The same cross-table candidate opened from either of its operand tables
   yields identical suggestion AND revision bytes.
3. **Withholding is total.** A bound operand this caller may not see removes the whole suggestion —
   from the hits, the counts, the groups and the wire — not just the operand.
4. **V1 is byte-stable.** ``to_table_suggestions_v1(page)`` reproduces today's payload exactly, over
   every fixture state including the unknown-table and all-hidden ones.
"""
from __future__ import annotations

import json
import os
from dataclasses import fields, replace
from pathlib import Path

import pytest
from tests.featuregen.overlay.upload.test_suggestions import (
    _COLUMNS,
    _ENTITY_TABLE,
    _JOIN_COLUMNS,
    _JOIN_SOURCE,
    _MEASURE_TABLE,
    OTHER_TABLE,
    SIBLING_TABLE,
    SOURCE,
    TABLE,
    _Catalog,
    _govern_table_facts,
    _ingest,
    _join_edge,
    _seal,
    _statements,
)

from featuregen.contracts import resolvers
from featuregen.contracts.evidence_axes import (
    AssertionStrength,
    AttributedLabelV1,
    AttributedTextV1,
    EvidenceAuthorityV1,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.upload import suggestion_contract as contract_module
from featuregen.overlay.upload import suggestions as suggestions_module
from featuregen.overlay.upload.suggestion_contract import (
    GENERATION_SOURCES,
    OPERAND_CLASSIFICATIONS,
    PROFILE_STATUS_UNAVAILABLE,
    SCHEMA_VERSION,
    WARNING_CODES,
    SuggestionV1ReconstructionError,
    SuggestionWarningV1,
    is_family_derived_category,
    page_to_json,
    to_table_suggestions_v1,
)
from featuregen.overlay.upload.suggestion_taxonomy import (
    FEATURE_CATEGORY_REGISTRY,
    RECIPE_FAMILY_REGISTRY,
)
from featuregen.overlay.upload.suggestions import (
    suggest_features_for_table,
    suggest_features_page_v2,
)
from featuregen.overlay.upload.template_discovery import (
    ADMISSIBLE_DISCOVERY_DISPOSITIONS,
    DISCOVERY_METADATA,
    discovery_metadata_revision_id,
)


@pytest.fixture
def ftr_catalog(overlay_conn):
    _seal()
    _ingest(overlay_conn, SOURCE, _COLUMNS)
    _govern_table_facts(overlay_conn, TABLE, "cif_id", "as_of_dt")
    _govern_table_facts(overlay_conn, OTHER_TABLE, "book_id", "risk_dt")
    _govern_table_facts(overlay_conn, SIBLING_TABLE, "loan_cif", "due_dt")
    return _Catalog(source=SOURCE, table=TABLE)


@pytest.fixture
def join_catalog(overlay_conn):
    _seal()
    _ingest(overlay_conn, _JOIN_SOURCE, _JOIN_COLUMNS)
    _govern_table_facts(overlay_conn, _MEASURE_TABLE, "ledger_acct", "ledger_dt",
                        source=_JOIN_SOURCE)
    _govern_table_facts(overlay_conn, _ENTITY_TABLE, "master_cif", "master_dt",
                        source=_JOIN_SOURCE)
    return _Catalog(source=_JOIN_SOURCE, table=_MEASURE_TABLE)


# ── the ASYMMETRIC catalog: G — L — X, where the asymmetry IS the fixture ───────────────────────
# `join_catalog` above has exactly two mutually-joined tables, so both anchors see an identical
# grounding universe and every anchor-independence proof over it is unfalsifiable in the one way
# that matters. Here the entity table G holds the customer key, the ledger L holds a balance, and
# the shadow ledger X holds a SECOND balance — joined to L only. At the default one-hop bound the
# two anchors therefore ground over genuinely different universes: from L it is {G, L, X} and the
# monetary-stock tie set has two members; from G it is {G, L} and it has one. X is named to sort
# LAST, and `_ranked_matches` breaks ties on (score, table, column, object_ref), so the SELECTED
# binding — and hence the logical candidate — is identical from either anchor.
_ASYM_SOURCE = "p4_suggestions_asym_ftr"
_ASYM_ENTITY = "acct_master"        # G — the customer key
_ASYM_LEDGER = "ledger_book"        # L — the balance that wins every tie
_ASYM_SHADOW = "zz_shadow_book"     # X — a second balance, one hop further from G
_ASYM_COLUMNS = {
    _ASYM_ENTITY: {
        "MASTER_CIF": ("customer_id", "varchar", "Master Customer Identifier"),
        "MASTER_ACCT": ("account_id", "varchar", "Master Account Identifier"),
        "MASTER_DT": ("as_of_date", "date", "Master As Of Date"),
    },
    _ASYM_LEDGER: {
        "LEDGER_ACCT": ("account_id", "varchar", "Ledger Account Identifier"),
        "BAL_AMT": ("monetary_stock", "decimal", "Ledger Balance"),
        "TXN_AMT": ("monetary_flow", "decimal", "Ledger Transaction Amount"),
        "TXN_TS": ("event_timestamp", "timestamp", "Ledger Posting Timestamp"),
        "LEDGER_DT": ("as_of_date", "date", "Ledger As Of Date"),
    },
    _ASYM_SHADOW: {
        "SHADOW_ACCT": ("account_id", "varchar", "Shadow Account Identifier"),
        "BAL_AMT": ("monetary_stock", "decimal", "Shadow Balance"),
        "SHADOW_DT": ("as_of_date", "date", "Shadow As Of Date"),
    },
}


def _asym_edge(conn, from_ref: str, to_ref: str) -> None:
    """One governed-VERIFIED operational join. VERIFIED on purpose: a file-declared edge would add
    relationship warnings to the comparison and blur what the asymmetry proof is about."""
    conn.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_fact_key, approved_join_status) "
        "VALUES (%s, 'joins', %s, %s, 'N:1', 'operational', 'ajf-verified', 'VERIFIED')",
        (_ASYM_SOURCE, from_ref, to_ref))


@pytest.fixture
def asymmetric_catalog(overlay_conn):
    _seal()
    _ingest(overlay_conn, _ASYM_SOURCE, _ASYM_COLUMNS)
    _govern_table_facts(overlay_conn, _ASYM_ENTITY, "master_cif", "master_dt", source=_ASYM_SOURCE)
    _govern_table_facts(overlay_conn, _ASYM_LEDGER, "ledger_acct", "ledger_dt", source=_ASYM_SOURCE)
    _govern_table_facts(overlay_conn, _ASYM_SHADOW, "shadow_acct", "shadow_dt", source=_ASYM_SOURCE)
    _asym_edge(overlay_conn, f"public.{_ASYM_LEDGER}.ledger_acct",
               f"public.{_ASYM_ENTITY}.master_acct")
    _asym_edge(overlay_conn, f"public.{_ASYM_SHADOW}.shadow_acct",
               f"public.{_ASYM_LEDGER}.ledger_acct")
    return _Catalog(source=_ASYM_SOURCE, table=_ASYM_LEDGER)


def _page(conn, table=TABLE, *, source=SOURCE, roles=(), **kw):
    return suggest_features_page_v2(conn, catalog_source=source, table=table, roles=roles, **kw)


def _suggestions(page) -> dict:
    return {hit.suggestion.name: hit.suggestion for hit in page.hits}


def _codes(suggestion) -> set[str]:
    return {warning.code for warning in suggestion.warnings}


# ── contract shape and closed vocabularies ──────────────────────────────────────────────────────
def test_every_hit_carries_the_frozen_contract_labels(overlay_conn, ftr_catalog):
    page = _page(overlay_conn)
    assert page.hits
    for hit in page.hits:
        suggestion = hit.suggestion
        assert suggestion.schema_version == SCHEMA_VERSION == "feature-suggestion-v2"
        assert suggestion.generation_source in GENERATION_SOURCES
        assert suggestion.validation_status in ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION")
        assert suggestion.binding_quality
        assert all(o.classification in OPERAND_CLASSIFICATIONS for o in suggestion.operands)
        assert all(w.code in WARNING_CODES for w in suggestion.warnings)


def test_the_no_hypothesis_producer_emits_recipe_and_only_recipe(overlay_conn, ftr_catalog):
    """This surface has no hypothesis, no LLM and no user-authored idea. The shared enum can
    DESCRIBE free-form and user-defined candidates; projecting one into global discovery needs a
    lifecycle this plan deliberately does not create."""
    assert {hit.suggestion.generation_source for hit in _page(overlay_conn).hits} == {"recipe"}


def test_release_a_reports_on_demand_with_no_projection(overlay_conn, ftr_catalog):
    """No projection exists yet, so the page says so instead of claiming a currentness it never
    computed. ``facets`` is empty for the same reason — Release B owns search."""
    page = _page(overlay_conn)
    assert page.read_mode == "on_demand"
    assert page.projection is None and page.next_cursor is None and page.facets == {}
    assert all(hit.projection is None for hit in page.hits)
    assert page.read_scope_key


def test_no_card_can_ever_render_a_needs_sme_disposition(overlay_conn, ftr_catalog):
    """Task-1 handoff (a): ``needs_sme`` is UNREPRESENTABLE in v1 — an authored escalation with no
    hashed carrier. A facet or badge that could render it would be showing a state the registry
    refuses to construct."""
    dispositions = {hit.suggestion.discovery_disposition for hit in _page(overlay_conn).hits}
    assert dispositions <= ADMISSIBLE_DISCOVERY_DISPOSITIONS
    assert "needs_sme" not in dispositions


def test_a_warning_outside_the_closed_vocabulary_cannot_be_constructed():
    with pytest.raises(ValueError, match="unknown suggestion warning code"):
        SuggestionWarningV1(code="LOOKS_RISKY", operand_refs=(), detail="")


# ── trace consumption: the V2 admission rule ────────────────────────────────────────────────────
def test_every_hit_carries_the_decision_trace_it_was_admitted_on(overlay_conn, ftr_catalog):
    for hit in _page(overlay_conn).hits:
        assert hit.suggestion.grounding_trace_content_hash
        assert hit.suggestion.validation_rule_content_hashes
        assert hit.suggestion.read_scope_rule_content_hashes


def _patch_engine(monkeypatch, transform):
    original = suggestions_module._template_candidates

    def _patched(*a, **kw):
        return transform(original(*a, **kw))

    monkeypatch.setattr(suggestions_module, "_template_candidates", _patched)


def test_a_candidate_with_no_trace_at_all_is_withheld_and_counted(overlay_conn, ftr_catalog,
                                                                  monkeypatch):
    """Without a trace the card's ``grounding_trace_content_hash`` would be a fabrication and its
    identity would have no path material. It is refused, and the refusal is COUNTED — a silently
    shorter list is how a page starts lying about what it looked at."""
    before = _page(overlay_conn)
    assert before.hits
    _patch_engine(monkeypatch, lambda r: replace(
        r, ideas=[replace(idea, grounding_trace=None) for idea in r.ideas]))
    after = _page(overlay_conn)
    assert after.hits == ()
    assert after.collection.omitted_counts["withheld_missing_trace"] == len(before.hits)
    assert after.collection.summary.suggested == 0


def test_a_design_checked_candidate_whose_trace_cannot_explain_it_is_refused(overlay_conn,
                                                                            ftr_catalog,
                                                                            monkeypatch):
    """THE HEADLINE REFUSAL (freeze 0F-7). A readiness claim nothing can justify or invalidate is
    not a softer card — it is inadmissible. The trace is emptied of its pins, which is exactly the
    state ``trace_completeness_gaps`` exists to detect."""
    before = _page(overlay_conn)
    checked = [h for h in before.hits if h.suggestion.validation_status == "DESIGN_CHECKED"]
    assert checked, "the fixture cleared nothing — the refusal proof would be vacuous"

    def _gut(result):
        ideas = []
        for idea in result.ideas:
            if idea.validation_status == "DESIGN_CHECKED" and idea.grounding_trace is not None:
                idea = replace(idea, grounding_trace=replace(idea.grounding_trace,
                                                             dependency_pins=()))
            ideas.append(idea)
        return replace(result, ideas=ideas)

    _patch_engine(monkeypatch, _gut)
    after = _page(overlay_conn)
    assert after.collection.omitted_counts["withheld_incomplete_trace"] == len(checked)
    assert not [h for h in after.hits if h.suggestion.validation_status == "DESIGN_CHECKED"]
    # ...and the needs-validation cards are untouched: the rule invalidates a READINESS claim, not
    # every candidate that ever had a gap.
    assert len(after.hits) == len(before.hits) - len(checked)


def _withdraw_grain_fact(conn, table=TABLE, source=SOURCE) -> None:
    """Real drift through the real gauntlet: the table's grain is declared but no longer
    governed-VERIFIED, which is exactly the state ``GRAIN_IS_UNIQUE`` exists to report."""
    conn.execute("UPDATE graph_node SET grain_fact_event_id = NULL "
                 "WHERE catalog_source = %s AND table_name = %s", (source, table))


def _withdraw_availability_fact(conn, table=TABLE, source=SOURCE) -> None:
    conn.execute("UPDATE graph_node SET availability_fact_event_id = NULL "
                 "WHERE catalog_source = %s AND table_name = %s", (source, table))


def test_a_needs_validation_candidate_is_not_refused_for_an_incomplete_trace(overlay_conn,
                                                                            ftr_catalog,
                                                                            monkeypatch):
    """The rule is precisely scoped: a card that already claims no readiness is not withheld
    because its trace has a gap. Over-refusing would hide honest work."""
    def _gut(result):
        return replace(result, ideas=[
            replace(idea, grounding_trace=replace(idea.grounding_trace, dependency_pins=()))
            if idea.validation_status != "DESIGN_CHECKED" and idea.grounding_trace else idea
            for idea in result.ideas])

    _withdraw_grain_fact(overlay_conn)
    before = _page(overlay_conn)
    needy = [h for h in before.hits
             if h.suggestion.validation_status == "NEEDS_EXTERNAL_VALIDATION"]
    assert needy, "no needs-validation candidate in the fixture — the proof would be vacuous"
    _patch_engine(monkeypatch, _gut)
    after = _page(overlay_conn)
    assert "withheld_incomplete_trace" not in after.collection.omitted_counts
    assert len(after.hits) == len(before.hits)


def test_a_non_recipe_candidate_never_reaches_this_surface(overlay_conn, ftr_catalog, monkeypatch):
    _patch_engine(monkeypatch, lambda r: replace(
        r, ideas=[replace(idea, generation_source="llm_freeform") for idea in r.ideas]))
    page = _page(overlay_conn)
    assert page.hits == ()
    assert page.collection.omitted_counts["withheld_non_recipe_generation_source"] > 0


def test_the_relationship_dependencies_are_the_ones_the_planner_selected(overlay_conn,
                                                                        join_catalog):
    """The cross-table card carries the legs the gauntlet traversed — read off the trace, never
    re-walked here."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    card = _suggestions(_page(overlay_conn, _MEASURE_TABLE,
                              source=_JOIN_SOURCE))["balance_trend_90d"]
    assert card.relationship_dependencies
    for leg in card.relationship_dependencies:
        assert leg.relationship_kind == "direct_equality"
        assert leg.realization_content_hash and leg.review_status == "VERIFIED"


# ── discovery metadata: display, provenance, bounds ─────────────────────────────────────────────
def test_the_recipe_family_resolves_its_display_name_and_cites_the_recipe(overlay_conn,
                                                                         ftr_catalog):
    for hit in _page(overlay_conn).hits:
        family = hit.suggestion.recipe_family
        assert family is not None and family.basis == "template_authored"
        assert family.display_name == RECIPE_FAMILY_REGISTRY[family.id].display_name
        assert family.source_refs and family.source_refs[0].startswith("recipe-revision:")


def test_a_family_derived_feature_category_is_distinguishable_from_an_authored_one(overlay_conn,
                                                                                   ftr_catalog):
    """Task-1 handoff (b). ``basis`` says ``template_authored`` for a DERIVED category, because the
    derivation is deterministic and sanctioned — so a badge driven by ``basis`` would present a
    taxonomy derivation as an SME-authored objective. The mapping citation is the positive signal,
    and it survives into the wire payload."""
    categorised = [h.suggestion for h in _page(overlay_conn).hits
                   if h.suggestion.feature_category is not None]
    assert categorised, "no categorised template on this table — the proof would be vacuous"
    for suggestion in categorised:
        category = suggestion.feature_category
        assert category.basis == "template_authored"
        assert category.display_name == FEATURE_CATEGORY_REGISTRY[category.id].display_name
        assert is_family_derived_category(category)
    body = page_to_json(_page(overlay_conn))
    flags = {hit["suggestion"]["name"]: hit["suggestion"]
             ["feature_category_derived_from_family_mapping"] for hit in body["hits"]}
    assert any(flags.values())


def test_the_discovery_revision_is_the_registrys_own(overlay_conn, ftr_catalog):
    """Handoff (c): the discovery revision is NOT a per-template key — many templates share one.
    It is carried as a referenced content revision and never used to index anything."""
    revisions = {}
    for hit in _page(overlay_conn).hits:
        entry = DISCOVERY_METADATA[hit.suggestion.template_id]
        assert hit.suggestion.discovery_metadata_revision_id == discovery_metadata_revision_id(
            entry)
        revisions.setdefault(hit.suggestion.discovery_metadata_revision_id, set()).add(
            hit.suggestion.template_id)
    assert revisions


def test_the_business_interpretation_is_the_recipes_own_sentence(overlay_conn, ftr_catalog):
    v1 = suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=TABLE)
    descriptions = {s["name"]: s["description"] for g in v1["groups"] for s in g["suggestions"]}
    for hit in _page(overlay_conn).hits:
        text = hit.suggestion.business_interpretation
        assert text is not None and text.basis == "template_authored"
        assert text.value == descriptions[hit.suggestion.name]


def test_the_remaining_authored_recipe_declarations_reach_the_card(overlay_conn, ftr_catalog):
    """VERIFIED GAP 1, closed. ``_suggestion`` dropped the template's stage, eligibility,
    near-label, notes, additivity and PIT rule before the API response, so no surface could show
    what the recipe actually declares. They travel as attributed text citing the recipe revision —
    and they cost no identity change, because ``recipe_revision_id`` already covers every authored
    ``Template`` field (D5)."""
    from featuregen.overlay.upload.templates import ALL_TEMPLATES

    by_id = {t.id: t for t in ALL_TEMPLATES}
    checked = 0
    for hit in _page(overlay_conn).hits:
        suggestion = hit.suggestion
        template = by_id[suggestion.template_id]
        for field, authored in (("recipe_stage", template.stage),
                                ("eligibility_note", template.eligibility),
                                ("output_additivity", template.additivity),
                                ("point_in_time_declaration", template.pit)):
            value = getattr(suggestion, field)
            if authored:
                assert value is not None and value.value == authored, (suggestion.name, field)
                assert value.basis == "template_authored" and value.evidence
                assert value.source_refs[0].startswith("recipe-revision:")
                checked += 1
            else:
                # silence, not an empty string: "no eligibility note" must stay distinguishable
                assert value is None, (suggestion.name, field)
        assert [n.value for n in suggestion.authoring_notes] == [n for n in template.notes if n]
    assert checked


def test_an_authored_declaration_edit_moves_the_revision_but_not_the_identity(overlay_conn,
                                                                             ftr_catalog,
                                                                             monkeypatch):
    """The identity consequence of D5, made visible: these declarations ride inside the recipe's
    own content hash, so editing one re-revisions the card while the logical candidate — same
    recipe, same bindings, same path — keeps its id."""
    from featuregen.overlay.upload import recipe_grounding_context as rgc

    before = {h.suggestion.template_id: h.suggestion for h in _page(overlay_conn).hits}
    real = rgc.canonical_template

    def _edited(template):
        payload = real(template)
        payload["template"]["pit"] = payload["template"]["pit"] + " (edited)"
        return payload

    monkeypatch.setattr(rgc, "canonical_template", _edited)
    after = {h.suggestion.template_id: h.suggestion for h in _page(overlay_conn).hits}
    assert set(after) == set(before)
    for template_id, suggestion in after.items():
        assert suggestion.suggestion_id == before[template_id].suggestion_id
        assert suggestion.recipe_revision_id != before[template_id].recipe_revision_id
        assert suggestion.suggestion_revision_id != before[template_id].suggestion_revision_id


def test_nothing_authored_is_fabricated_where_the_registry_is_silent(overlay_conn, ftr_catalog):
    """Rule 12: unknown is a valid answer. No template in the baseline registry authors keywords or
    a business value, so the cards carry none — a coverage target must never force invented
    metadata."""
    for hit in _page(overlay_conn).hits:
        entry = DISCOVERY_METADATA[hit.suggestion.template_id]
        assert len(hit.suggestion.keywords) == len(entry.keywords)
        assert (hit.suggestion.business_value is None) == (entry.business_value is None)
        assert len(hit.suggestion.use_cases) == len(entry.canonical_use_cases)


# ── free-text context and the resolver seam (D9) ────────────────────────────────────────────────
def test_catalog_domain_wording_is_attributed_text_and_never_a_facet(overlay_conn, ftr_catalog):
    """No controlled business-domain registry exists (D9), so the catalog's own wording travels as
    searchable attributed TEXT. It is never lowercased or slugged into an id, and it never becomes
    a facet — an uncontrolled string as a filter key is exactly rule 13's failure."""
    hit = _page(overlay_conn).hits[0].suggestion
    assert hit.business_domains == ()
    assert hit.contextual_domain_terms
    for term in hit.contextual_domain_terms:
        assert isinstance(term, AttributedTextV1)
        assert term.basis == "catalog_resolved" and term.operational_influence is None
        assert term.source_refs
        # AND its provenance axes: rules 4/5 forbid an LLM proposal rendering like an attestation
        assert term.evidence


def test_a_registered_controlled_resolver_turns_the_same_wording_into_a_facet(overlay_conn,
                                                                              ftr_catalog):
    """The seam is the switch, and this adapter needs no change to flip it: when the semantic plan
    registers its registry, the SAME call site yields a controlled label instead of free text."""
    class _Resolver:
        def resolve(self, text: str) -> AttributedLabelV1 | None:
            return AttributedLabelV1(
                id="retail_payments", display_name="Retail Payments", basis="catalog_resolved",
                evidence=(EvidenceAuthorityV1(EvidenceProducer.TAXONOMY,
                                              AssertionStrength.ATTESTED,
                                              EvidenceLifecycle.ACTIVE, "test", None),),
                operational_influence=None, source_refs=())

    resolvers.register_resolver("business_domain", _Resolver())
    try:
        hit = _page(overlay_conn).hits[0].suggestion
        assert [d.id for d in hit.business_domains] == ["retail_payments"]
        assert hit.contextual_domain_terms == ()
        # RULE 4 AT THE FLIP. The catalog wording is an `llm`/`proposed` value; the resolver
        # attests only the MAPPING. Registering it must not launder the one into the other, so the
        # operands' own `field_evidence` axes and contributing refs ride ON the facet.
        facet = hit.business_domains[0]
        axes = {(e.producer.value, e.strength.value) for e in facet.evidence}
        assert ("taxonomy", "attested") in axes                        # the resolver's own
        wording_axes = axes - {("taxonomy", "attested")}
        recorded = {tuple(row) for row in overlay_conn.execute(
            "SELECT DISTINCT producer, strength FROM field_evidence "
            "WHERE field_name = 'domain' AND lifecycle = 'active'").fetchall()}
        # the WORDING's own axes, asserted against the table rather than guessed
        assert wording_axes and wording_axes <= recorded, (wording_axes, recorded)
        assert facet.source_refs and all(ref.startswith("public.") for ref in facet.source_refs)
    finally:
        resolvers.reset_resolver("business_domain")


def test_a_proposed_domain_does_not_render_like_an_attested_one(overlay_conn, ftr_catalog):
    """RULES 4 AND 5. The catalog's ``domain`` is LLM-enriched at ingest, so its axes are
    ``llm``/``proposed`` — and a card that dropped them would show the same string a human had
    attested. Read from ``field_evidence``, the real trail the enrichment writer left, at both the
    column's own ref and (for the INHERITED case) its table's."""
    terms = [t for hit in _page(overlay_conn).hits
             for t in hit.suggestion.contextual_domain_terms]
    assert terms
    axes = {(e.producer.value, e.strength.value) for t in terms for e in t.evidence}
    # exactly the axes field_evidence actually holds — asserted against the table, not guessed
    recorded = {tuple(row) for row in overlay_conn.execute(
        "SELECT DISTINCT producer, strength FROM field_evidence "
        "WHERE field_name = 'domain' AND lifecycle = 'active'").fetchall()}
    assert recorded and axes and axes <= recorded, (axes, recorded)
    assert ("legacy", "proposed") not in axes          # a real trail was found, not the fallback
    assert all(e.lifecycle.value == "active" for t in terms for e in t.evidence)

    # STRENGTHEN the evidence and the card follows — proof it is READ, not hardcoded
    overlay_conn.execute(
        "UPDATE field_evidence SET producer = 'human', strength = 'confirmed' "
        "WHERE field_name = 'domain'")
    after = {(e.producer.value, e.strength.value)
             for hit in _page(overlay_conn).hits
             for t in hit.suggestion.contextual_domain_terms for e in t.evidence}
    assert after == {("human", "confirmed")}


def test_a_wording_with_no_recorded_evidence_is_explicitly_unattributed(overlay_conn,
                                                                        ftr_catalog):
    """Silence is the failure mode this closes: an empty evidence tuple reads as "nothing to say"
    and renders exactly like an attested value. A projected wording whose trail was retired carries
    the ``legacy``/``proposed`` marker — the vocabulary's own bucket for "producer not classified"
    — so a reader can always tell."""
    overlay_conn.execute("DELETE FROM field_evidence WHERE field_name = 'domain'")
    terms = [t for hit in _page(overlay_conn).hits
             for t in hit.suggestion.contextual_domain_terms]
    assert terms
    for term in terms:
        assert term.evidence
        assert {(e.producer.value, e.strength.value) for e in term.evidence} == {
            ("legacy", "proposed")}


def test_an_invisible_tables_domain_provenance_is_not_read(overlay_conn, ftr_catalog):
    """SAME LEAK CLASS AS THE WITHHELD COUNT. A column's ``domain`` is INHERITED from its table, so
    its provenance is read at the TABLE's ref — a ref this adapter SYNTHESIZES rather than receives
    from the scope-filtered read. Hide the table node and that inherited provenance (producer,
    strength, producer_ref, evidence_id) must not reach the wire, even though the wording itself
    still arrives through a column the caller can see.

    Unreachable today — table nodes carry no sensitivity — which is exactly why it is pinned before
    Release B widens the surface."""
    def _axes(roles=()):
        return {(e.producer.value, e.strength.value)
                for hit in _page(overlay_conn, roles=roles).hits
                for term in hit.suggestion.contextual_domain_terms for e in term.evidence}

    # Drop the per-COLUMN rows so the domain really is INHERITED — the fixture's table-level rows
    # (`llm`) then become the only trail, which is the state this check is about.
    overlay_conn.execute(
        "DELETE FROM field_evidence WHERE field_name = 'domain' AND logical_ref LIKE '%%.%%.%%'")
    assert _axes() == {("llm", "proposed")}, "the inherited trail is not being read at all"

    overlay_conn.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND kind = 'table' AND table_name = %s", (SOURCE, TABLE))
    blind = _page(overlay_conn, roles=()).hits
    assert blind, "the columns are still visible — the caller must still get their cards"
    terms = [t for hit in blind for t in hit.suggestion.contextual_domain_terms]
    assert terms, "the wording still travels; only its provenance is withheld"
    assert {(e.producer.value, e.strength.value) for t in terms for e in t.evidence} == {
        ("legacy", "proposed")}
    # ...and a caller who MAY see the table gets the real provenance through the same code path
    assert _axes(roles=("restricted_reader",)) == {("llm", "proposed")}


def test_the_projection_tally_is_filtered_at_the_wire_too(overlay_conn, ftr_catalog):
    """Release A never populates ``projection``, so this is a REGRESSION GUARD, not a behaviour
    test: the moment Release B fills that tally it must go through the same filter the collection's
    does, or the scope leak returns by a second door."""
    state = contract_module.SuggestionProjectionStateV1(
        state="current", scope_set_id=None, read_scope_key="k", scope_epoch=1,
        target_fingerprint="t", current_fingerprint="t", generated_at=None, stale_reason=None,
        omitted_counts={"withheld_read_scope": 3, "operands": 2})
    published = contract_module._projection_json(state)
    assert published["omitted_counts"] == {"operands": 2}


def test_a_governed_entity_is_distinguishable_from_a_file_declared_one(overlay_conn,
                                                                       ftr_catalog):
    """The entity axis has no ``field_evidence`` row — its provenance is the governed projection on
    the node itself (migration 1015), so it costs no statement. A VERIFIED ``entity_assignment``
    is a human dual-owner confirmation and says so; anything else stays proposed."""
    overlay_conn.execute(
        "UPDATE graph_node SET entity = 'customer' "
        "WHERE catalog_source = %s AND object_ref = %s", (SOURCE, f"public.{TABLE}.cif_id"))
    proposed = [t for hit in _page(overlay_conn).hits
                for t in hit.suggestion.contextual_entity_terms]
    assert proposed
    assert all(("human", "confirmed") not in {(e.producer.value, e.strength.value)
                                              for e in t.evidence} for t in proposed)

    overlay_conn.execute(
        "UPDATE graph_node SET entity_status = 'VERIFIED', entity_fact_key = 'efk-1', "
        "entity_fact_event_id = 'efe-1' WHERE catalog_source = %s AND object_ref = %s",
        (SOURCE, f"public.{TABLE}.cif_id"))
    governed = [t for hit in _page(overlay_conn).hits
                for t in hit.suggestion.contextual_entity_terms]
    axes = {(e.producer.value, e.strength.value) for t in governed for e in t.evidence}
    assert ("human", "confirmed") in axes
    refs = {(e.producer_ref, e.evidence_id) for t in governed for e in t.evidence}
    assert ("efk-1", "efe-1") in refs


def test_the_entity_facet_is_the_engines_own_controlled_entity_link(overlay_conn, ftr_catalog):
    """The entity axis has a real controlled vocabulary today — ``Concept.entity_link``, bound by
    the grounding engine itself — so it IS a facet. Free-text catalog entity wording stays separate
    attributed text."""
    labelled = [h.suggestion for h in _page(overlay_conn).hits if h.suggestion.entity is not None]
    assert labelled
    assert {s.entity.id for s in labelled} <= {"customer", "account"}
    for suggestion in labelled:
        assert suggestion.entity.display_name == suggestion.entity.id
        assert all(isinstance(t, AttributedTextV1) for t in suggestion.contextual_entity_terms)
        # ...and it is visibly a DERIVATION: the authored need concept, through the governed
        # concept registry. A citation naming only the recipe would read as an SME attestation of
        # the entity itself.
        refs = [e.producer_ref or "" for e in suggestion.entity.evidence]
        assert any(r.startswith("concept-registry:") and "#concept=" in r for r in refs), refs
        assert any(r.startswith("recipe-revision:") for r in refs), refs


def test_every_profile_field_is_explicitly_unavailable_not_guessed(overlay_conn, ftr_catalog):
    """D9. The profile plan owns data role, authority role, temporal model and primary entity; a
    plausible guess here would be indistinguishable from a governed answer."""
    for hit in _page(overlay_conn).hits:
        assert hit.suggestion.semantic_context_hashes == ()
        assert hit.suggestion.dataset_profile_hashes == ()
        assert hit.suggestion.source_datasets
        for dataset in hit.suggestion.source_datasets:
            assert dataset.catalog_source and dataset.table_ref
            assert dataset.profile_status == PROFILE_STATUS_UNAVAILABLE
            assert (dataset.data_role, dataset.authority_role, dataset.temporal_storage_model,
                    dataset.primary_entity, dataset.dataset_profile_hash) == (
                        None, None, None, None, None)


def test_the_source_datasets_are_the_tables_the_operands_actually_read(overlay_conn, join_catalog):
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    card = _suggestions(_page(overlay_conn, _MEASURE_TABLE,
                              source=_JOIN_SOURCE))["balance_trend_90d"]
    assert {d.table_ref for d in card.source_datasets} == {o.table_ref for o in card.operands}
    assert {_MEASURE_TABLE, _ENTITY_TABLE} == {d.table_ref for d in card.source_datasets}


# ── operands ────────────────────────────────────────────────────────────────────────────────────
def test_the_operands_are_the_engines_binding_order_deduplicated_on_the_ref(overlay_conn,
                                                                           ftr_catalog):
    """Load-bearing for the V1 reconstruction: ``uses`` is exactly this list of refs, so the order
    and the dedupe rule are the engine's own, not a re-derivation."""
    v1 = suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=TABLE)
    uses = {s["name"]: s["uses"] for g in v1["groups"] for s in g["suggestions"]}
    for hit in _page(overlay_conn).hits:
        assert [o.graph_object_ref for o in hit.suggestion.operands] == uses[hit.suggestion.name]


def test_the_recipe_role_is_the_template_authors_own_declaration(overlay_conn, ftr_catalog):
    """Never inferred from a concept (concepts are AI-proposed), a column name or a data type."""
    seen = set()
    for hit in _page(overlay_conn).hits:
        for operand in hit.suggestion.operands:
            assert operand.recipe_role
            seen.add(operand.recipe_role)
    assert len(seen) > 1


def test_the_key_and_the_clock_are_never_classified_as_measures(overlay_conn, ftr_catalog):
    """``measure_refs`` carries EVERY bound pair — the key and the clock included — so classifying
    by membership alone would file the feature's own key as a quantity it aggregates.

    The clock is the subtle half: a recipe's point-in-time anchor may be the table's own as-of
    column and not a bound operand at all, while a bound ``event_timestamp`` need IS a time operand.
    Both are decided from the engine's typed refs and the TEMPLATE-AUTHORED need concept — never
    from a column name or the column's AI-proposed concept."""
    keys = clocks = 0
    for hit in _page(overlay_conn).hits:
        classes = {o.graph_object_ref: o.classification for o in hit.suggestion.operands}
        for _source, ref in hit.suggestion.grain_refs:
            if ref in classes:
                assert classes[ref] == "grain", (hit.suggestion.name, ref)
                keys += 1
        if hit.suggestion.time_ref is not None and hit.suggestion.time_ref[1] in classes:
            assert classes[hit.suggestion.time_ref[1]] == "time"
            clocks += 1
    assert keys and clocks
    # ...and a bound event clock that is NOT the recipe's as-of anchor is still a time operand
    event_clocks = {o.classification for hit in _page(overlay_conn).hits
                    for o in hit.suggestion.operands
                    if o.graph_object_ref.endswith(".txn_ts")}
    assert event_clocks == {"time"}


def test_the_evidence_refs_are_the_pins_the_gauntlet_recorded(overlay_conn, ftr_catalog):
    """Provenance, deliberately: none of these enter a semantic hash, and they are what a reader
    compares against the current pointer."""
    assert any(operand.evidence_refs
               for hit in _page(overlay_conn).hits for operand in hit.suggestion.operands)


# ── warnings ────────────────────────────────────────────────────────────────────────────────────
def test_warnings_come_from_typed_requirements_never_from_prose(overlay_conn, ftr_catalog):
    """Each requirement CODE raises its matching warning code, and it names the requirement's own
    operand. Nothing is parsed out of the human-readable detail, which is a rendering. Driven by
    REAL drift — the table's availability fact is withdrawn, which is what
    ``TEMPORAL_IS_POPULATED`` exists to report."""
    expected = {"UNIT_CONSISTENT": "MISSING_UNIT", "CURRENCY_CONSISTENT": "MISSING_CURRENCY",
                "TEMPORAL_IS_POPULATED": "MISSING_TEMPORAL_EVIDENCE"}
    _withdraw_availability_fact(overlay_conn)
    seen = 0
    for hit in _page(overlay_conn).hits:
        by_code = {w.code: w for w in hit.suggestion.warnings}
        for requirement in hit.suggestion.requirements:
            warning = expected.get(requirement.code)
            if warning is None:
                continue
            assert warning in by_code, (hit.suggestion.name, requirement.code)
            assert requirement.operand in by_code[warning].operand_refs
            seen += 1
    assert seen, "no unit/currency/temporal requirement in the fixture"


def test_a_clean_card_raises_no_requirement_derived_warning(overlay_conn, ftr_catalog):
    """The other half of the contract: with the governing facts in place the same cards carry no
    missing-evidence warning at all, so the warning really is derived from the drift."""
    codes = {w for hit in _page(overlay_conn).hits for w in _codes(hit.suggestion)}
    assert not ({"MISSING_TEMPORAL_EVIDENCE", "MISSING_UNIT", "MISSING_CURRENCY"} & codes)


def test_the_near_label_warning_is_the_recipes_own_declaration(overlay_conn, ftr_catalog):
    """An authored template flag, not a heuristic about the column."""
    from featuregen.overlay.upload.templates import ALL_TEMPLATES

    near = {t.id for t in ALL_TEMPLATES if t.near_label}
    flagged = {hit.suggestion.template_id for hit in _page(overlay_conn).hits
               if "NEAR_LABEL" in _codes(hit.suggestion)}
    assert flagged and flagged <= near
    for hit in _page(overlay_conn).hits:
        assert ("NEAR_LABEL" in _codes(hit.suggestion)) == (hit.suggestion.template_id in near)


def test_an_unconfirmed_relationship_is_a_different_warning_from_unproven_safety(overlay_conn,
                                                                                 join_catalog):
    """The freeze keeps these SEPARATE because they are separate facts: a file-declared join is
    accountable to nobody (review), while a non-clearing one has no governed safety evidence
    (execution). A verified join raises neither."""
    _join_edge(overlay_conn, fact_key=None, status=None)          # file-declared: clears, unreviewed
    declared = _suggestions(_page(overlay_conn, _MEASURE_TABLE,
                                  source=_JOIN_SOURCE))["balance_trend_90d"]
    assert "RELATIONSHIP_UNCONFIRMED" in _codes(declared)
    assert "RELATIONSHIP_SAFETY_UNPROVEN" not in _codes(declared)


def test_a_governed_verified_relationship_raises_neither_relationship_warning(overlay_conn,
                                                                              join_catalog):
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    card = _suggestions(_page(overlay_conn, _MEASURE_TABLE,
                              source=_JOIN_SOURCE))["balance_trend_90d"]
    assert not ({"RELATIONSHIP_UNCONFIRMED", "RELATIONSHIP_SAFETY_UNPROVEN"} & _codes(card))


def test_a_restricted_input_raises_the_sensitive_input_warning(overlay_conn, ftr_catalog):
    """Derived from the visibility classes RE-READ at request time under THIS caller's scope, not
    from a template note and not from a cached indexing-time copy."""
    restricted = f"public.{TABLE}.bal_amt"
    overlay_conn.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND object_ref = %s", (SOURCE, restricted))
    page = _page(overlay_conn, roles=("restricted_reader",))
    flagged = [s for s in _suggestions(page).values() if "SENSITIVE_INPUT" in _codes(s)]
    assert flagged
    for suggestion in flagged:
        sensitive = [o for o in suggestion.operands if o.visibility_requires_current]
        assert sensitive and all(o.visibility_requires_current == ("restricted",)
                                 for o in sensitive)
        warning = next(w for w in suggestion.warnings if w.code == "SENSITIVE_INPUT")
        assert (SOURCE, restricted) in warning.operand_refs
    # the caller who cannot see it never gets the card at all — nothing to warn about
    blind = _suggestions(_page(overlay_conn, roles=()))
    assert not [s for s in blind.values() if "SENSITIVE_INPUT" in _codes(s)]


# ── identity over the real engine ───────────────────────────────────────────────────────────────
def test_the_same_catalog_state_yields_the_same_identities(overlay_conn, ftr_catalog):
    first = {h.suggestion.name: (h.suggestion.suggestion_id, h.suggestion.suggestion_revision_id)
             for h in _page(overlay_conn).hits}
    second = {h.suggestion.name: (h.suggestion.suggestion_id, h.suggestion.suggestion_revision_id)
              for h in _page(overlay_conn).hits}
    assert first == second and first


def test_two_different_candidates_never_share_an_identity(overlay_conn, ftr_catalog):
    hits = _page(overlay_conn).hits
    assert len({h.suggestion.suggestion_id for h in hits}) == len(hits)


def test_the_same_candidate_from_either_operand_table_is_byte_identical(overlay_conn,
                                                                       join_catalog):
    """ANCHOR INDEPENDENCE, the load-bearing proof. ``balance_trend_90d`` binds the ledger's balance
    and the master's customer key, so it is a suggestion FOR BOTH tables. Opened from either one it
    must be ONE card: same id, same revision, and the same canonical bytes — otherwise a global page
    would have to choose between two conflicting renderings of one logical candidate."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    from_measure = _suggestions(_page(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE))
    from_entity = _suggestions(_page(overlay_conn, _ENTITY_TABLE, source=_JOIN_SOURCE))
    shared = set(from_measure) & set(from_entity)
    assert "balance_trend_90d" in shared, (sorted(from_measure), sorted(from_entity))
    for name in shared:
        left, right = from_measure[name], from_entity[name]
        assert left.suggestion_id == right.suggestion_id, name
        assert left.suggestion_revision_id == right.suggestion_revision_id, name
        assert left == right, name


def _asym_pages(conn, **kw):
    """The same catalog read from the ledger (three tables in reach) and from the entity table (two),
    with the candidates keyed by name."""
    left = _page(conn, _ASYM_LEDGER, source=_ASYM_SOURCE, **kw)
    right = _page(conn, _ASYM_ENTITY, source=_ASYM_SOURCE, **kw)
    return left, right


def _tie_sensitive(left: dict, right: dict) -> list[str]:
    """The shared candidates whose GROUNDING PASS provably ran over different universes: their trace
    hashes differ, because the trace covers ``candidate_key``, which folds in each role's tie set.
    Every anchor-independence assertion below is vacuous unless this list is non-empty."""
    return sorted(name for name in set(left) & set(right)
                  if left[name].grounding_trace_content_hash
                  != right[name].grounding_trace_content_hash)


def test_the_same_candidate_from_asymmetric_anchors_shares_both_identities(overlay_conn,
                                                                          asymmetric_catalog):
    """ANCHOR INDEPENDENCE where it can actually fail. ``join_catalog``'s two mutually-joined tables
    give both anchors an IDENTICAL grounding universe, so the proof above cannot fail whatever the
    revision hashes. Here the universes genuinely differ: from the ledger the monetary-stock role
    ties between two tables' balances, from the entity table it does not — same winner either way.

    That difference reaches ``candidate_key`` (via ``binding_resolution_hash`` ->
    ``tied_candidate_set_hash``) and therefore ``trace_content_hash``, which is why the revision
    hashes a build-universe-INDEPENDENT projection of the trace instead. One logical candidate, one
    id, one revision — otherwise Release B's global page would hold two disagreeing revisions of it
    and rule 26 / DoD 17 would withhold it permanently."""
    left, right = (_suggestions(page) for page in _asym_pages(overlay_conn))
    shared = sorted(set(left) & set(right))
    assert "balance_trend_90d" in shared, (sorted(left), sorted(right))

    # NON-VACUITY: the two reads really did ground over different universes.
    sensitive = _tie_sensitive(left, right)
    assert sensitive, "both anchors saw the same tie sets — the asymmetry fixture is not asymmetric"
    assert "balance_trend_90d" in sensitive

    for name in shared:
        assert left[name].suggestion_id == right[name].suggestion_id, name
        assert left[name].suggestion_revision_id == right[name].suggestion_revision_id, name


def test_the_revision_does_not_move_with_the_neighbourhood_bound(overlay_conn,
                                                                 asymmetric_catalog):
    """The same property against ``max_hops`` rather than the anchor. From the entity table one hop
    reaches the ledger only; two hops also reach the shadow ledger, which adds a second equally
    fitting balance and so widens the tie set. A CLIENT-SUPPLIED bound must not be able to mint a
    second revision of a candidate it did not otherwise change."""
    near = _suggestions(_page(overlay_conn, _ASYM_ENTITY, source=_ASYM_SOURCE, max_hops=1))
    far = _suggestions(_page(overlay_conn, _ASYM_ENTITY, source=_ASYM_SOURCE, max_hops=2))
    shared = sorted(set(near) & set(far))
    assert shared
    sensitive = _tie_sensitive(near, far)
    assert sensitive, "widening the bound changed no tie set — the proof would be vacuous"
    for name in shared:
        assert near[name].suggestion_id == far[name].suggestion_id, name
        assert near[name].suggestion_revision_id == far[name].suggestion_revision_id, name


def test_the_revision_does_not_move_with_the_callers_read_scope(overlay_conn, asymmetric_catalog):
    """And against the READ SCOPE. Hiding the shadow ledger's balance from a public caller removes
    it from that caller's grounding universe and shrinks the tie set — the third way one logical
    candidate could have acquired two revisions."""
    overlay_conn.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND object_ref = %s",
        (_ASYM_SOURCE, f"public.{_ASYM_SHADOW}.bal_amt"))
    public = _suggestions(_page(overlay_conn, _ASYM_LEDGER, source=_ASYM_SOURCE))
    privileged = _suggestions(_page(overlay_conn, _ASYM_LEDGER, source=_ASYM_SOURCE,
                                    roles=("restricted_reader",)))
    shared = sorted(set(public) & set(privileged))
    assert shared
    sensitive = _tie_sensitive(public, privileged)
    assert sensitive, "the two scopes saw the same tie sets — the proof would be vacuous"
    for name in shared:
        assert public[name].suggestion_id == privileged[name].suggestion_id, name
        assert public[name].suggestion_revision_id == privileged[name].suggestion_revision_id, name


def test_exactly_which_payload_fields_still_read_the_build_universe(overlay_conn,
                                                                    asymmetric_catalog):
    """THE PINNED RESIDUAL. The two identities are anchor-independent; the canonical payload is not
    yet, in exactly two places, and both are build OBSERVATIONS rather than candidate content:

    * ``binding_quality`` is ``AMBIGUOUS`` precisely when the pass saw a tie, so it reports the
      universe it ran over;
    * ``grounding_trace_content_hash`` is the identity of THAT build's trace, which is what it is
      for.

    Relocating both onto ``SuggestionBuildProvenanceV1`` (where the other exact build ids already
    live) is the modelling-correct answer, but it is a wire + UI contract change and is recorded as
    a Release-B decision in the freeze doc's deviation log rather than taken unilaterally here. This
    test exists so the residual cannot grow silently: a THIRD anchor-sensitive payload field fails
    it."""
    left, right = (_suggestions(page) for page in _asym_pages(overlay_conn))
    divergent: set[str] = set()
    for name in sorted(set(left) & set(right)):
        for field in fields(left[name]):
            if getattr(left[name], field.name) != getattr(right[name], field.name):
                divergent.add(field.name)
    assert divergent == {"binding_quality", "grounding_trace_content_hash"}


# ── the captured server body the frontend renders ───────────────────────────────────────────────
_CAPTURE = (Path(__file__).resolve().parents[4] / "frontend" / "src" / "screens"
            / "SuggestedFeaturesScreen.serverCapture.json")
#: Sub-trees whose KEYS are data rather than contract: an omission tally is keyed by whatever was
#: omitted, and the facet map by whatever facets exist. Their presence is pinned, their key sets
#: are not.
_DATA_KEYED = ("facets", "omitted_counts")


def _key_paths(node, prefix: str = "") -> set[str]:
    """Every dotted key path in a wire body, with list indices collapsed — the body's CONTRACT
    surface, independent of the values a particular fixture happens to hold."""
    paths: set[str] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            paths.add(f"{prefix}{key}")
            if key not in _DATA_KEYED:
                paths |= _key_paths(value, f"{prefix}{key}.")
    elif isinstance(node, list):
        for item in node:
            paths |= _key_paths(item, prefix)
    return paths


def _captured_body(conn) -> dict:
    """The join catalog with a FILE-DECLARED, cardinality-less edge — so the captured body carries
    real relationship warnings, which is exactly what the hand-written frontend fixtures could not
    produce and what the nested-ref defect hid behind."""
    _join_edge(conn, fact_key=None, status=None, cardinality=None)
    return page_to_json(_page(conn, _MEASURE_TABLE, source=_JOIN_SOURCE))


def test_the_frontends_captured_server_body_is_still_the_body_the_server_sends(overlay_conn,
                                                                               join_catalog):
    """THE CONTRACT TEST the frontend suite never had. Its fixtures are hand-written literals, so
    they agree with the server only for as long as someone re-reads both — and the one shape nobody
    hand-wrote (a relationship warning) shipped with the wrong arity through every green suite.

    ``frontend/src/screens/SuggestedFeaturesScreen.serverCapture.json`` is a REAL ``page_to_json``
    body, checked in and rendered by ``SuggestionCard.capture.test.tsx``. This test is the other
    half: it re-derives that body from the real engine and compares the CONTRACT SURFACE — every
    dotted key path — so a field the server adds, drops or renames fails here instead of silently
    leaving the capture describing a server that no longer exists. Values are deliberately not
    compared: a template edit legitimately moves every hash on the page.

    Regenerate with::

        FEATUREGEN_REGEN_SUGGESTION_CAPTURE=1 uv run pytest \\
          tests/featuregen/overlay/upload/test_suggestion_contract.py -k captured_server_body
    """
    body = _captured_body(overlay_conn)
    if os.environ.get("FEATUREGEN_REGEN_SUGGESTION_CAPTURE"):
        _CAPTURE.write_text(json.dumps(body, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    assert _CAPTURE.exists(), f"the frontend capture is missing: {_CAPTURE}"
    captured = json.loads(_CAPTURE.read_text(encoding="utf-8"))
    live, pinned = _key_paths(body), _key_paths(captured)
    assert live == pinned, {"server_only": sorted(live - pinned),
                            "capture_only": sorted(pinned - live)}
    # ...and the capture really does carry the shapes the hand-written fixtures cannot invent.
    codes = {w["code"] for h in captured["hits"] for w in h["suggestion"]["warnings"]}
    assert {"RELATIONSHIP_UNCONFIRMED", "DIRECTIONAL_CARDINALITY_UNAVAILABLE"} <= codes, codes
    assert any(h["suggestion"]["relationship_dependencies"] for h in captured["hits"])


def test_the_anchor_travels_on_the_collection_and_nowhere_else(overlay_conn, join_catalog):
    """The two pages above differ — they must, they are different readings — but ONLY in the
    collection envelope: the anchor, the counts, the groups and the neighbourhood."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    left = _page(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE)
    right = _page(overlay_conn, _ENTITY_TABLE, source=_JOIN_SOURCE)
    assert left.collection.anchor_table_ref != right.collection.anchor_table_ref
    hits = page_to_json(left)["hits"]
    assert _MEASURE_TABLE in json.dumps(hits)   # operand tables DO appear — they are content

    def _keys(node):
        if isinstance(node, dict):
            yield from node
            for value in node.values():
                yield from _keys(value)
        elif isinstance(node, list):
            for item in node:
                yield from _keys(item)

    # ...but no anchor-shaped FIELD rides anywhere inside a hit, at any depth
    assert not [key for key in _keys(hits) if key.startswith("anchor")]
    assert {key for key in _keys(page_to_json(left)["collection"]) if key.startswith("anchor")} == {
        "anchor_catalog_source", "anchor_table_ref", "anchor_column_ref"}


def test_a_byte_identical_reground_does_not_churn_the_revision(overlay_conn, ftr_catalog):
    """Rule 24: content, not provenance. Re-running the same read produces the same revisions, and
    the build provenance that DID move (nothing here, by construction) is carried separately."""
    first = _page(overlay_conn)
    second = _page(overlay_conn)
    assert [h.suggestion for h in first.hits] == [h.suggestion for h in second.hits]
    assert all(hit.provenance.producer_commit is None and hit.provenance.refresh_id is None
               and hit.provenance.generated_at is None for hit in first.hits)


def test_a_changed_validation_result_produces_a_new_revision(overlay_conn, ftr_catalog,
                                                             monkeypatch):
    before = {h.suggestion.name: h.suggestion for h in _page(overlay_conn).hits}
    # The governed grain fact is withdrawn: real drift, through the real gauntlet.
    overlay_conn.execute(
        "UPDATE graph_node SET grain_fact_event_id = NULL "
        "WHERE catalog_source = %s AND table_name = %s", (SOURCE, TABLE))
    after = {h.suggestion.name: h.suggestion for h in _page(overlay_conn).hits}
    moved = [name for name in set(before) & set(after)
             if before[name].validation_status != after[name].validation_status]
    assert moved, "the drift changed no validation result — the proof would be vacuous"
    for name in moved:
        assert before[name].suggestion_revision_id != after[name].suggestion_revision_id
        assert before[name].suggestion_id == after[name].suggestion_id


def test_a_changed_discovery_mapping_produces_a_new_revision(overlay_conn, ftr_catalog,
                                                             monkeypatch):
    """A category/use-case remapping is a SEMANTIC change to the card, so the revision moves — while
    the logical candidate, which the mapping does not touch, keeps its id."""
    before = {h.suggestion.name: h.suggestion for h in _page(overlay_conn).hits}
    template_id = next(s.template_id for s in before.values()
                       if s.feature_category is not None)
    entry = DISCOVERY_METADATA[template_id]
    patched = dict(DISCOVERY_METADATA)
    patched[template_id] = replace(entry, feature_category=None,
                                   disposition="partial" if entry.canonical_use_cases
                                   else "unclassified")
    monkeypatch.setattr(contract_module, "DISCOVERY_METADATA", patched)
    after = {h.suggestion.name: h.suggestion for h in _page(overlay_conn).hits}
    changed = [s for s in after.values() if s.template_id == template_id]
    assert changed
    for suggestion in changed:
        assert suggestion.suggestion_id == before[suggestion.name].suggestion_id
        assert suggestion.suggestion_revision_id != before[
            suggestion.name].suggestion_revision_id


# ── read scope: withholding is total ────────────────────────────────────────────────────────────
def test_a_hidden_operand_withholds_the_whole_suggestion(overlay_conn, ftr_catalog, monkeypatch):
    """Rule 9. Not the operand — the WHOLE suggestion, and it may not leak through the counts, the
    groups, the ids or the wire. Simulated at the context-read seam, because on-demand grounding is
    itself read-scoped: this is the fail-closed control for the case where an operand becomes
    invisible or is withdrawn between the grounding read and the context read."""
    page = _page(overlay_conn)
    victim = page.hits[0].suggestion
    hidden = victim.operands[0].graph_object_ref
    real = contract_module._read_node_facts

    def _blind(conn, pairs, roles):
        facts = real(conn, pairs, roles)
        return {key: value for key, value in facts.items() if key[1] != hidden}

    monkeypatch.setattr(contract_module, "_read_node_facts", _blind)
    after = _page(overlay_conn)
    names = {hit.suggestion.name for hit in after.hits}
    assert victim.name not in names
    assert after.collection.omitted_counts["withheld_read_scope"] >= 1
    assert after.collection.summary.suggested == len(after.hits) < len(page.hits)
    body = json.dumps(page_to_json(after))
    assert victim.suggestion_id not in body and victim.name not in body
    assert all(victim.suggestion_id not in g.suggestion_ids for g in after.collection.groups)


def test_the_wire_cannot_tell_that_anything_was_withheld(overlay_conn, ftr_catalog, monkeypatch):
    """RULE 9, the last inch. Publishing ``withheld_read_scope: 3`` tells a caller that three
    suggestions exist which they may not see — the COUNT is the disclosure, exactly the "leak
    through facets, counts, snippets or provenance" the rule names. The server still counts it (for
    telemetry and for the V1-safety gate); the wire cannot distinguish "nothing was withheld" from
    "N were withheld"."""
    clean = page_to_json(_page(overlay_conn))
    victim = _page(overlay_conn).hits[0].suggestion
    real = contract_module._read_node_facts

    def _blind(conn, pairs, roles):
        facts = real(conn, pairs, roles)
        return {key: value for key, value in facts.items()
                if key[1] != victim.operands[0].graph_object_ref}

    monkeypatch.setattr(contract_module, "_read_node_facts", _blind)
    page = _page(overlay_conn)
    withheld = page_to_json(page)

    assert page.collection.omitted_counts["withheld_read_scope"] >= 1     # counted server-side
    assert "withheld_read_scope" not in withheld["collection"]["omitted_counts"]
    assert withheld["collection"]["omitted_counts"] == clean["collection"]["omitted_counts"]
    assert not [k for k in json.dumps(withheld).split('"') if "withheld" in k]


def test_a_scope_independent_omission_is_still_published(overlay_conn, ftr_catalog, monkeypatch):
    """The suppression is surgical, not a blanket: a bound that bit is a property of the SERVER's
    limits, identical for every caller, so hiding it would cost honesty for no privacy."""
    monkeypatch.setattr(contract_module, "MAX_OPERANDS", 1)
    body = page_to_json(_page(overlay_conn))
    assert body["collection"]["omitted_counts"]["operands"] > 0


def test_a_candidate_with_no_grounding_context_is_withheld_not_guessed(overlay_conn, ftr_catalog,
                                                                       monkeypatch):
    """The 2A freeze makes ``contexts[candidate_key]`` THE identity join. Without it there are no
    logical refs, no recipe roles, no bound parameters and no recipe revision — a card built anyway
    would carry a deterministic-but-WRONG identity, silently. Every other admission failure refuses;
    so does this one."""
    before = _page(overlay_conn)
    assert before.hits
    _patch_engine(monkeypatch, lambda r: replace(r, contexts={}))
    after = _page(overlay_conn)
    assert after.hits == ()
    assert after.collection.omitted_counts["withheld_missing_context"] == len(before.hits)
    assert after.collection.summary.suggested == 0
    with pytest.raises(SuggestionV1ReconstructionError):
        to_table_suggestions_v1(after)


def test_a_candidate_whose_path_cannot_be_projected_is_withheld(overlay_conn, join_catalog,
                                                                monkeypatch):
    """FAIL CLOSED (finding 1's other half). If a JOIN_PATH pin names a realization the trace's own
    path does not contain, the logical chain cannot be projected — and falling back to the physical
    hash would silently restore the governance churn. Refuse and count."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    before = _page(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE)
    crossed = [h for h in before.hits if h.suggestion.relationship_dependencies]
    assert crossed, "no candidate crossed the join — the proof would be vacuous"

    def _corrupt_the_pin(result):
        """Repoint ONE JOIN_PATH pin at a realization the path does not contain. The path itself is
        left intact, so the completeness gate still passes and this refusal is reached in
        isolation."""
        ideas = []
        for idea in result.ideas:
            trace = idea.grounding_trace
            if trace is not None and trace.ordered_relationship_path:
                pins = tuple(
                    replace(pin, path_realization_hashes=("no-such-realization",))
                    if pin.dependency_kind == "join_path" else pin
                    for pin in trace.dependency_pins)
                idea = replace(idea, grounding_trace=replace(trace, dependency_pins=pins))
            ideas.append(idea)
        return replace(result, ideas=ideas)

    _patch_engine(monkeypatch, _corrupt_the_pin)
    after = _page(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE)
    assert after.collection.omitted_counts["withheld_unresolvable_path"] == len(crossed)
    assert len(after.hits) == len(before.hits) - len(crossed)


def test_on_demand_grounding_differs_correctly_across_public_and_sensitive_callers(overlay_conn,
                                                                                   join_catalog):
    """The scope is DERIVED from the caller's canonical allowed classes and threaded into grounding,
    so a blind caller and a privileged one get genuinely different pages — not one maximum-scope
    page filtered afterwards. The restricted endpoint hides the whole far table from the blind
    caller, exactly as it does on the V1 surface."""
    overlay_conn.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND object_ref = %s",
        (_JOIN_SOURCE, f"public.{_ENTITY_TABLE}.master_acct"))
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    blind = _page(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE, roles=())
    privileged = _page(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE,
                       roles=("restricted_reader",))
    assert "balance_trend_90d" not in _suggestions(blind)
    assert "balance_trend_90d" in _suggestions(privileged)
    assert blind.read_scope_key != privileged.read_scope_key
    refs = {o.graph_object_ref for s in _suggestions(blind).values() for o in s.operands}
    assert all(ref.startswith(f"public.{_MEASURE_TABLE}.") for ref in refs)


def test_the_scope_key_is_the_canonical_class_tuple_not_the_role_claims(overlay_conn,
                                                                        ftr_catalog):
    """Never echoes role claims, and two functional roles with the same data scope share one key."""
    left = _page(overlay_conn, roles=("feature_engineer",))
    right = _page(overlay_conn, roles=("data_owner", "catalog_viewer"))
    assert left.read_scope_key == right.read_scope_key
    assert "feature_engineer" not in json.dumps(page_to_json(left))


# ── the collection envelope ─────────────────────────────────────────────────────────────────────
def test_the_collection_carries_the_anchor_the_counts_and_the_typed_rejections(overlay_conn,
                                                                               ftr_catalog):
    page = _page(overlay_conn)
    collection = page.collection
    assert collection.anchor_catalog_source == SOURCE
    assert collection.anchor_table_ref == TABLE and collection.table_known is True
    assert collection.anchor_column_ref is None
    assert collection.summary.suggested == len(page.hits)
    assert (collection.summary.design_checked + collection.summary.needs_external_validation
            == collection.summary.suggested)
    assert collection.rejections
    for rejection in collection.rejections:
        assert rejection.template_id and rejection.code and rejection.candidate_name
    assert collection.neighbourhood is not None


def test_a_rejection_names_its_template_instead_of_being_matched_by_name(overlay_conn,
                                                                        ftr_catalog):
    """V1's wire rejection carries only ``{name, reason, code}``. V2 carries the template the engine
    already knew, so nothing downstream has to re-attribute a rejection by its rendered name."""
    from featuregen.overlay.upload.contract.gate1 import _template_candidates

    engine = _template_candidates(overlay_conn, catalog_source=SOURCE, roles=(), target_ref=None,
                                  now=None, table=TABLE)
    page = _page(overlay_conn)
    assert [(r.template_id, r.candidate_name, r.code) for r in page.collection.rejections] == [
        (rec.template_id, rec.candidate_name, rec.rejection.code)
        for rec in engine.rejection_records]


def test_the_groups_keep_v1s_entity_semantics(overlay_conn, ftr_catalog):
    """The summary's ``groups`` is V1's ``entities``: NAMED buckets only. A grain bucket with no
    resolvable entity is not an entity, and counting it would claim the catalog attested one."""
    page = _page(overlay_conn)
    v1 = suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=TABLE)
    assert page.collection.summary.groups == v1["summary"]["entities"]
    assert [g.grain_refs[0][1] if g.grain_refs else "" for g in page.collection.groups] == [
        g["entity_ref"] for g in v1["groups"]]
    assert [g.entity.display_name if g.entity else "" for g in page.collection.groups] == [
        g["entity_label"] for g in v1["groups"]]


def test_the_unknown_table_page_echoes_the_requested_string_verbatim(overlay_conn, ftr_catalog):
    """0F-11's two-case rule. There is no resolver output to carry, so substituting a normalized
    spelling would answer a question the caller did not ask."""
    page = _page(overlay_conn, "no_such_table")
    assert page.collection.table_known is False
    assert page.collection.anchor_table_ref == "no_such_table"
    assert page.hits == () and page.collection.groups == ()
    assert page.collection.neighbourhood is not None       # never None on the table route
    assert page.collection.neighbourhood.as_metadata()["max_hops"] == 1


# ── the byte-stable V1 adapter (0F-11) ──────────────────────────────────────────────────────────
def _assert_v1_identical(conn, table, *, source=SOURCE, roles=(), **kw):
    legacy = suggest_features_for_table(conn, catalog_source=source, table=table, roles=roles,
                                        **kw)
    rebuilt = to_table_suggestions_v1(
        suggest_features_page_v2(conn, catalog_source=source, table=table, roles=roles, **kw))
    assert rebuilt == legacy
    assert json.dumps(rebuilt, sort_keys=True) == json.dumps(legacy, sort_keys=True)
    return legacy


@pytest.mark.parametrize("table", [TABLE, OTHER_TABLE, SIBLING_TABLE, "no_such_table"])
def test_the_v1_adapter_reproduces_the_legacy_payload_byte_for_byte(overlay_conn, ftr_catalog,
                                                                    table):
    """The migration contract: every V1 byte has a V2 carrier, so the adapter never re-queries and
    never invents. Asserted on the WHOLE payload, over every fixture state."""
    _assert_v1_identical(overlay_conn, table)


def test_the_v1_adapter_reproduces_the_widened_cross_table_payload(overlay_conn, join_catalog):
    legacy = _assert_v1_identical(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE)
    assert legacy["summary"]["suggested"] >= 1
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    widened = _assert_v1_identical(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE)
    assert widened["summary"]["suggested"] > legacy["summary"]["suggested"]


def test_the_v1_adapter_reproduces_the_all_hidden_table_payload(overlay_conn, join_catalog):
    """The read-scoped unknown state (Task 0C defect 2) has to survive the round trip too."""
    overlay_conn.execute(
        "UPDATE graph_node SET sensitivity = 'restricted' "
        "WHERE catalog_source = %s AND kind = 'column' AND table_name = %s",
        (_JOIN_SOURCE, _ENTITY_TABLE))
    blind = _assert_v1_identical(overlay_conn, _ENTITY_TABLE, source=_JOIN_SOURCE, roles=())
    assert blind["table_known"] is False
    _assert_v1_identical(overlay_conn, _ENTITY_TABLE, source=_JOIN_SOURCE,
                         roles=("restricted_reader",))


def test_the_v1_adapter_reproduces_a_truncated_neighbourhood_block(overlay_conn, join_catalog):
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    _assert_v1_identical(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE, max_hops=3)


def test_the_v1_grain_table_is_the_entity_operands_table(overlay_conn, join_catalog):
    """0F-11 derives ``grain_table`` from the carried operands: the SOURCE-ENTITY operand's table
    when one bound, else the first bound operand's. The cross-table card is the case that would
    expose a wrong derivation — it is grained on the master while sitting on the ledger's screen."""
    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    rebuilt = to_table_suggestions_v1(_page(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE))
    cards = {s["name"]: s for g in rebuilt["groups"] for s in g["suggestions"]}
    assert cards["balance_trend_90d"]["grain_table"] == _ENTITY_TABLE


def test_the_v1_adapter_refuses_rather_than_return_a_quietly_different_body(overlay_conn,
                                                                           ftr_catalog,
                                                                           monkeypatch):
    """A bounded operand list would make ``uses`` a lie, and a withheld card would make the whole
    payload one. V1 has no carrier for either, so the adapter refuses — a migration tool that cannot
    tell the two bodies apart is worse than none."""
    monkeypatch.setattr(contract_module, "MAX_OPERANDS", 1)
    page = _page(overlay_conn)
    assert page.collection.omitted_counts["operands"] > 0
    with pytest.raises(SuggestionV1ReconstructionError, match="without lying"):
        to_table_suggestions_v1(page)


def test_a_bound_that_bites_reports_what_it_left_out(overlay_conn, ftr_catalog, monkeypatch):
    """Every bound reports its omissions rather than silently truncating — otherwise a card claims
    it lists every operand it has."""
    monkeypatch.setattr(contract_module, "MAX_OPERANDS", 1)
    page = _page(overlay_conn)
    assert all(len(hit.suggestion.operands) == 1 for hit in page.hits)
    assert page.collection.omitted_counts["operands"] >= len(page.hits)


# ── cost and read-only posture ──────────────────────────────────────────────────────────────────
def test_the_v2_page_costs_one_bounded_statement_more_than_v1(overlay_conn, ftr_catalog,
                                                              monkeypatch):
    """N+1 PREVENTION. Visibility+context (one statement) and the provenance axes behind every
    domain wording (one more) are read for the WHOLE page, so the extra cost is a CONSTANT — it does
    not grow with the number of cards. Measured on a narrow table and a wide one: if either read
    were per card the two deltas would differ, which is the property this pins. The absolute number
    is secondary; the CONSTANCY is the contract."""
    counter = _statements(overlay_conn, monkeypatch)

    def _delta(table):
        counter[0] = 0
        legacy = suggest_features_for_table(overlay_conn, catalog_source=SOURCE, table=table)
        v1_cost = counter[0]
        counter[0] = 0
        page = _page(overlay_conn, table)
        return counter[0] - v1_cost, len(page.hits), legacy["summary"]["suggested"]

    narrow_delta, narrow_hits, narrow_v1 = _delta(OTHER_TABLE)
    wide_delta, wide_hits, wide_v1 = _delta(TABLE)
    assert narrow_hits == narrow_v1 and wide_hits == wide_v1
    assert wide_hits > narrow_hits, "the two fixtures have the same card count — not a proof"
    assert narrow_delta == wide_delta == 2, (narrow_delta, wide_delta)


def test_the_unknown_table_page_costs_exactly_the_resolve(overlay_conn, ftr_catalog, monkeypatch):
    counter = _statements(overlay_conn, monkeypatch)
    counter[0] = 0
    _page(overlay_conn, "no_such_table")
    assert counter[0] == 1


def test_the_v2_read_writes_nothing(overlay_conn, ftr_catalog):
    """Release A is strictly read-only. A row COUNT cannot see an in-place write, so the row CONTENT
    is fingerprinted."""
    tables = ("field_evidence", "field_decision_event", "graph_node", "graph_edge",
              "contract_intent")

    def fingerprint():
        return tuple(overlay_conn.execute(
            f"SELECT count(*), md5(coalesce(string_agg(t::text, '|' ORDER BY t::text), '')) "
            f"FROM {t} t").fetchone() for t in tables)

    before = fingerprint()
    assert _page(overlay_conn).hits
    assert fingerprint() == before


# ── wire serialization ──────────────────────────────────────────────────────────────────────────
def test_the_page_serializes_to_plain_json(overlay_conn, ftr_catalog):
    body = page_to_json(_page(overlay_conn))
    assert json.loads(json.dumps(body)) == body
    assert body["read_mode"] == "on_demand" and body["projection"] is None
    suggestion = body["hits"][0]["suggestion"]
    assert suggestion["schema_version"] == SCHEMA_VERSION
    assert suggestion["operands"] and suggestion["source_datasets"]
    assert suggestion["grounding_trace_content_hash"]


# ── governance provenance must not re-key a candidate (rule 23 / rule 24) ───────────────────────
def test_confirming_a_join_moves_the_revision_but_never_the_suggestion_id(overlay_conn,
                                                                          join_catalog):
    """THE MISSING MUTATION. An admin confirms a file-declared join — REAL drift through the real
    graph, not a hand-edited dataclass: `approved_join_fact_key` NULL/NULL becomes
    'ajf-verified'/'VERIFIED'. Same recipe, same columns, same endpoints, same direction, same
    traversal. Nothing about WHICH CANDIDATE this is has changed, so `suggestion_id` must not move.

    Its `suggestion_revision_id` MUST move: the governed safety evidence is exactly the kind of
    content the revision exists to track, and the card's own RELATIONSHIP_UNCONFIRMED warning
    disappears with it.

    This is the defect the first implementation shipped: the identity hashed the JOIN_PATH pin's
    content_hash, which covers the realization content — fact key, status and edge authority —
    so confirming a join re-keyed every suggestion that crossed it."""
    _join_edge(overlay_conn, fact_key=None, status=None)          # file-declared, unconfirmed
    before = _suggestions(_page(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE))
    card = before["balance_trend_90d"]
    assert card.relationship_dependencies, "no join was crossed — the proof would be vacuous"
    assert "RELATIONSHIP_UNCONFIRMED" in _codes(card)

    overlay_conn.execute(
        "UPDATE graph_edge SET approved_join_fact_key = 'ajf-verified', "
        "approved_join_status = 'VERIFIED' WHERE catalog_source = %s AND kind = 'joins'",
        (_JOIN_SOURCE,))
    after = _suggestions(_page(overlay_conn, _MEASURE_TABLE, source=_JOIN_SOURCE))
    confirmed = after["balance_trend_90d"]

    # the drift really landed: the leg's realization hash and its review status both moved
    assert "RELATIONSHIP_UNCONFIRMED" not in _codes(confirmed)
    assert {leg.review_status for leg in confirmed.relationship_dependencies} == {"VERIFIED"}
    assert ({leg.realization_content_hash for leg in card.relationship_dependencies}
            != {leg.realization_content_hash for leg in confirmed.relationship_dependencies})

    assert confirmed.suggestion_id == card.suggestion_id
    assert confirmed.suggestion_revision_id != card.suggestion_revision_id
    # ...and every OTHER card on the page is equally unmoved
    assert {s.suggestion_id for s in after.values()} >= {s.suggestion_id for s in before.values()}


def _joins_edge(conn, from_ref: str, to_ref: str) -> None:
    conn.execute(
        "INSERT INTO graph_edge (catalog_source, kind, from_ref, to_ref, cardinality, authority, "
        "approved_join_fact_key, approved_join_status) "
        "VALUES (%s, 'joins', %s, %s, 'N:1', 'operational', NULL, NULL)",
        (_JOIN_SOURCE, from_ref, to_ref))


def test_a_genuinely_different_logical_path_still_forks_the_identity(overlay_conn, join_catalog):
    """THE COMPLEMENT, so the fix above cannot be "stop hashing the path at all".

    Two catalogs identical in every way the recipe cares about — same template, same bound columns,
    same entity, same grain, same time anchor — differing ONLY in which join connects the two
    tables: the account key, or the as-of date. The traversal is genuinely different, so these are
    different logical candidates and their ids must fork.

    This is the integration-level half of rule 23. It is deliberately NOT an anchor test: the
    account-key edge is REPLACED by the date edge, so the second read crosses a different chain
    rather than the same one from another side."""
    _joins_edge(overlay_conn, f"public.{_MEASURE_TABLE}.ledger_acct",
                f"public.{_ENTITY_TABLE}.master_acct")
    over_account = _suggestions(_page(overlay_conn, _MEASURE_TABLE,
                                      source=_JOIN_SOURCE))["balance_trend_90d"]
    overlay_conn.execute(
        "DELETE FROM graph_edge WHERE catalog_source = %s AND kind = 'joins'", (_JOIN_SOURCE,))
    _joins_edge(overlay_conn, f"public.{_MEASURE_TABLE}.ledger_dt",
                f"public.{_ENTITY_TABLE}.master_dt")
    over_date = _suggestions(_page(overlay_conn, _MEASURE_TABLE,
                                   source=_JOIN_SOURCE))["balance_trend_90d"]

    # everything the recipe binds is IDENTICAL — otherwise the fork would prove nothing about paths
    assert ([o.graph_object_ref for o in over_account.operands]
            == [o.graph_object_ref for o in over_date.operands])
    assert (over_account.template_id, over_account.grain_refs, over_account.time_ref) == (
        over_date.template_id, over_date.grain_refs, over_date.time_ref)
    assert over_account.entity == over_date.entity
    # ...and ONLY the traversal differs
    account_legs = [(leg.from_ref[1], leg.to_ref[1])
                    for leg in over_account.relationship_dependencies]
    date_legs = [(leg.from_ref[1], leg.to_ref[1]) for leg in over_date.relationship_dependencies]
    assert account_legs and date_legs and account_legs != date_legs

    assert over_account.suggestion_id != over_date.suggestion_id
    assert over_account.suggestion_revision_id != over_date.suggestion_revision_id


def test_the_identity_is_independent_of_the_presentation_bound(overlay_conn, ftr_catalog,
                                                               monkeypatch):
    """MAX_OPERANDS is a PRESENTATION bound and the plan keeps page truncation out of the canonical
    revision. Computing identity from the truncated list would emit, on a page where the bound bites,
    cards whose stable ids belong to a different logical candidate — one with fewer operands, which
    nothing ever grounded, and which a global page would then fail to deduplicate against."""
    full = {h.suggestion.template_id: h.suggestion for h in _page(overlay_conn).hits}
    monkeypatch.setattr(contract_module, "MAX_OPERANDS", 1)
    bounded = {h.suggestion.template_id: h.suggestion for h in _page(overlay_conn).hits}
    assert set(bounded) == set(full)
    truncated = [s for s in bounded.values() if len(s.operands) == 1]
    assert truncated and any(len(full[s.template_id].operands) > 1 for s in truncated)
    for template_id, suggestion in bounded.items():
        assert suggestion.suggestion_id == full[template_id].suggestion_id, template_id
        assert suggestion.suggestion_revision_id == full[template_id].suggestion_revision_id


def test_one_template_index_is_built_once_not_scanned_per_suggestion(overlay_conn, ftr_catalog):
    """The registry is 157 templates and a page renders ten cards; a per-card linear scan is 1,570
    comparisons for an answer one dict already holds. The index is built at import, and this pins
    that the assembly never walks ``ALL_TEMPLATES`` again."""
    from featuregen.overlay.upload import templates as templates_module

    assert set(contract_module._TEMPLATES_BY_ID) == {t.id for t in templates_module.ALL_TEMPLATES}
    scans: list[int] = []
    real = templates_module.ALL_TEMPLATES

    class _CountingRegistry(tuple):
        def __iter__(self):
            scans.append(1)
            return super().__iter__()

    monkey = _CountingRegistry(real)
    original = contract_module.ALL_TEMPLATES
    contract_module.ALL_TEMPLATES = monkey
    try:
        page = _page(overlay_conn)
        assert page.hits
    finally:
        contract_module.ALL_TEMPLATES = original
    assert scans == []


# ── the mutation set: every one of these must die ───────────────────────────────────────────────
def test_collapsing_an_evidence_tuple_would_lose_the_derivation_citation(overlay_conn,
                                                                        ftr_catalog):
    """MUTATION: keep only the "best" evidence occurrence. A derived ``feature_category`` cites BOTH
    the mapping that produced it AND the recipe its family was read from; collapsing to one drops
    the mapping citation, and the provenance badge silently starts calling a taxonomy derivation an
    SME-authored objective."""
    from featuregen.overlay.upload.template_discovery import (
        FAMILY_MAPPING_CITATION_PREFIX,
        RECIPE_REVISION_CITATION_PREFIX,
    )

    categorised = [h.suggestion.feature_category for h in _page(overlay_conn).hits
                   if h.suggestion.feature_category is not None]
    assert categorised
    for category in categorised:
        refs = [e.producer_ref or "" for e in category.evidence]
        assert len(refs) >= 2
        assert any(r.startswith(FAMILY_MAPPING_CITATION_PREFIX) for r in refs)
        assert any(r.startswith(RECIPE_REVISION_CITATION_PREFIX) for r in refs)
        # the mutation: keep the first only
        collapsed = replace(category, evidence=category.evidence[1:])
        assert not is_family_derived_category(collapsed)


def test_choosing_the_first_domain_would_drop_the_others(overlay_conn, ftr_catalog):
    """MUTATION: pick one domain as the feature's one true domain (rule 3). A multi-source feature
    genuinely has several, and every one keeps its own provenance."""
    before = _suggestions(_page(overlay_conn))["balance_trend_90d"]
    original = {t.value for t in before.contextual_domain_terms}
    assert len(original) == 1, original          # one domain today — the mutation adds a second
    overlay_conn.execute(
        "UPDATE graph_node SET domain = 'treasury' WHERE catalog_source = %s AND object_ref = %s",
        (SOURCE, f"public.{TABLE}.bal_amt"))
    card = _suggestions(_page(overlay_conn))["balance_trend_90d"]
    values = {t.value for t in card.contextual_domain_terms}
    assert values == original | {"treasury"}, values
    assert all(t.source_refs for t in card.contextual_domain_terms)
    # ...and each term names the operands it came from, so neither is an unattributed string
    by_value = {t.value: t for t in card.contextual_domain_terms}
    assert by_value["treasury"].source_refs == (f"public.{TABLE}.bal_amt",)


def test_dropping_the_relationship_path_from_identity_would_fuse_two_candidates(overlay_conn,
                                                                                join_catalog):
    """MUTATION: exclude the logical relationship path (rule 23). Rebuilt WITHOUT the per-operand
    ``JOIN_PATH`` assignment, the cross-table candidate's id collides with the id of the same
    columns reached over no path at all."""
    from featuregen.overlay.upload.suggestion_identity import suggestion_id

    _join_edge(overlay_conn, fact_key="ajf-verified", status="VERIFIED")
    card = _suggestions(_page(overlay_conn, _MEASURE_TABLE,
                              source=_JOIN_SOURCE))["balance_trend_90d"]
    assert card.relationship_dependencies, "no path traversed — the mutation proof would be vacuous"
    inputs = dict(
        template_id=card.template_id,
        bound_params=(),
        operands=tuple((o.catalog_source, o.logical_ref, o.recipe_role) for o in card.operands),
        entity_id=card.entity.id if card.entity else None,
        grain_refs=card.grain_refs, time_ref=card.time_ref)
    pathless = suggestion_id(**inputs, relationship_path_assignment=())
    assert card.suggestion_id != pathless


def test_the_rendered_name_is_never_the_identity(overlay_conn, ftr_catalog, monkeypatch):
    """MUTATION: use the rendered name as the id — today's V1 gap, where React keys on ``s.name``.
    Renaming every candidate must move NO identity: the name is a rendering, not a key."""
    before = {h.suggestion.template_id: h.suggestion for h in _page(overlay_conn).hits}
    _patch_engine(monkeypatch, lambda r: replace(
        r, ideas=[replace(idea, name=f"{idea.name}__renamed") for idea in r.ideas]))
    after = {h.suggestion.template_id: h.suggestion for h in _page(overlay_conn).hits}
    assert set(before) == set(after)
    for template_id, suggestion in after.items():
        assert suggestion.name.endswith("__renamed")
        assert suggestion.suggestion_id == before[template_id].suggestion_id
        assert suggestion.suggestion_revision_id == before[template_id].suggestion_revision_id


def test_a_build_observation_never_reaches_the_revision(overlay_conn, ftr_catalog, monkeypatch):
    """MUTATION: put a snapshot id (or a producer commit, or a refresh id) into the semantic hash.
    A byte-identical re-upload mints new snapshot ids, so a revision that hashed one would churn
    every suggestion in the catalog for no semantic change at all (rule 24)."""
    before = {h.suggestion.template_id: h.suggestion.suggestion_revision_id
              for h in _page(overlay_conn).hits}
    _patch_engine(monkeypatch, lambda r: replace(
        r, ideas=[replace(idea, metadata_snapshot_id="snap-9999") for idea in r.ideas]))
    page = _page(overlay_conn)
    after = {h.suggestion.template_id: h.suggestion.suggestion_revision_id for h in page.hits}
    assert after == before
    # ...and the new id IS carried, as build provenance where a reader can compare currentness
    assert {p for hit in page.hits for p in hit.provenance.metadata_snapshot_ids} == {"snap-9999"}


def test_every_engine_rejection_reaches_the_collection(overlay_conn, ftr_catalog):
    """MUTATION: drop a collection rejection. The rejections are the table's catalog-readiness
    to-dos; a page that quietly shortened them would report a cleaner catalog than it read."""
    from featuregen.overlay.upload.contract.gate1 import _template_candidates

    engine = _template_candidates(overlay_conn, catalog_source=SOURCE, roles=(), target_ref=None,
                                  now=None, table=TABLE)
    collection = _page(overlay_conn).collection
    assert len(collection.rejections) == len(engine.rejection_records) > 0
    assert {r.candidate_name for r in collection.rejections} == {
        r["name"] for r in engine.rejections}


def test_the_wire_keeps_every_evidence_axis_on_an_attributed_value(overlay_conn, ftr_catalog):
    """Rule 4: authority travels. A card that dropped the producer/strength/lifecycle axes would
    render an LLM proposal and a human attestation identically."""
    suggestion = page_to_json(_page(overlay_conn))["hits"][0]["suggestion"]
    family = suggestion["recipe_family"]
    assert set(family) == {"id", "display_name", "basis", "evidence", "operational_influence",
                           "source_refs"}
    assert family["evidence"] and set(family["evidence"][0]) == {
        "producer", "strength", "lifecycle", "producer_ref", "evidence_id"}
