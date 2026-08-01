"""Entity/role/alias/issuer semantics (semantic plan Task 2, amended by D12.1).

* `counterparty` is a PARTY ROLE. The registry member `counterparty_id` KEEPS its persisted
  `entity_link="counterparty"` (governed bridge fact keys hash `entity_id` — fact-key
  preservation), while the correction happens at PROJECTION: the alias seam
  (`canonical_concept_name` / `display_entity`) maps the display entity to `customer` in the
  Entity Map, the semantic bundle, new enrichment projections and the migration-1045 backfill.
* New SELECTION attempts canonicalize (`counterparty_id` -> `customer_id`); the classification
  vocabulary excludes `counterparty_id`; `bank_bic` is untouched.
* `Concept.namespace` stays the identifier SCHEME; the ISSUER comes from the new per-catalog
  semantic scope (migration 1045) or a global scheme registry — scheme equality alone is never
  equality proof (functional rule 5). The issuer folds into
  `bridge_grounding.assess_grounded_identifier_link`, so the truth table cannot be bypassed by
  the grounded path (`identifier_scope` is NOT a third namespace surface).
"""
from __future__ import annotations

from pathlib import Path

from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import (
    CONCEPT_REGISTRY,
    UNCLASSIFIED,
    canonical_concept_name,
    classification_vocabulary,
    concept,
    display_entity,
)
from featuregen.overlay.upload.enrich import _accept_concept, content_hash
from featuregen.overlay.upload.graph import (
    build_graph,
    reproject_alias_entity_search_docs,
)
from featuregen.overlay.upload.identifier_scope import (
    GLOBAL_SCHEME_ISSUERS,
    declare_catalog_semantic_scope,
    read_catalog_semantic_scope,
    resolve_identifier_issuer,
)
from featuregen.overlay.upload.taxonomy.dimensions import known_entities

_ROOT = Path(__file__).parents[4]

# ── the alias seam (concept level) ───────────────────────────────────────────────────────────────


def test_counterparty_id_is_excluded_from_the_classification_vocabulary() -> None:
    names = {c["name"] for c in classification_vocabulary()}
    assert "counterparty_id" not in names
    assert "customer_id" in names
    assert "bank_bic" in names            # untouched


def test_counterparty_id_registry_member_is_byte_stable() -> None:
    """Fact-key preservation (D12.1): the persisted entity semantics of `counterparty_id` do NOT
    move — `fact_key` hashes `entity_id`, and flipping `entity_link` would orphan confirmation
    streams. The correction is projection-layer only."""
    registered = concept("counterparty_id")
    assert registered is not None
    assert registered.entity_link == "counterparty"
    assert registered.namespace == "cif"


def test_canonical_concept_name_maps_the_alias_and_nothing_else() -> None:
    assert canonical_concept_name("counterparty_id") == "customer_id"
    assert canonical_concept_name("customer_id") == "customer_id"
    assert canonical_concept_name("bank_bic") == "bank_bic"
    assert canonical_concept_name("monetary_amount") == "monetary_amount"  # no canonical target
    assert canonical_concept_name(UNCLASSIFIED) == UNCLASSIFIED


def test_new_selection_attempts_canonicalize_to_customer_id() -> None:
    assert _accept_concept("counterparty_id") == ("customer_id", "valid")
    assert _accept_concept("bank_bic") == ("bank_bic", "valid")
    assert _accept_concept("unclassified") == ("unclassified", "valid")
    assert _accept_concept("not_a_concept") == (None, "invalid_value")


def test_display_entity_resolves_through_the_alias_seam() -> None:
    assert display_entity("counterparty_id", "counterparty") == "customer"
    assert display_entity("counterparty_id", None) == "customer"
    # An explicitly different stored entity is a decision, not an alias artifact — untouched.
    assert display_entity("counterparty_id", "legal_entity") == "legal_entity"
    assert display_entity("customer_id", "customer") == "customer"
    assert display_entity(None, "counterparty") == "counterparty"
    assert display_entity("bank_bic", "bank") == "bank"


def test_known_entities_keeps_counterparty_as_a_readable_legacy_member() -> None:
    entities = known_entities()
    assert "counterparty" in entities     # stored target_entity values stay recognizable
    assert "customer" in entities


# ── the catalog semantic scope + issuer resolution ───────────────────────────────────────────────


def test_catalog_semantic_scope_round_trips(db) -> None:
    assert read_catalog_semantic_scope(db, "cat_a") is None
    declare_catalog_semantic_scope(
        db, catalog_source="cat_a", issuer_scope="bank_one", basis="catalog_scope",
        declared_by="user:one")
    scope = read_catalog_semantic_scope(db, "cat_a")
    assert (scope.issuer_scope, scope.basis, scope.declared_by) == (
        "bank_one", "catalog_scope", "user:one")
    # Re-declaration replaces (a configuration, not an append-only fact).
    declare_catalog_semantic_scope(
        db, catalog_source="cat_a", issuer_scope="bank_two", basis="catalog_scope",
        declared_by="user:two")
    assert read_catalog_semantic_scope(db, "cat_a").issuer_scope == "bank_two"


def test_issuer_resolution_truth_table(db) -> None:
    declare_catalog_semantic_scope(
        db, catalog_source="cat_a", issuer_scope="bank_one", basis="catalog_scope",
        declared_by="user:one")
    # catalog-scoped scheme + declared catalog -> the catalog's issuer
    assert resolve_identifier_issuer(db, "cat_a", "customer_id") == ("bank_one", "catalog_scope")
    # catalog-scoped scheme + undeclared catalog -> honest unresolved, never a guess
    assert resolve_identifier_issuer(db, "cat_b", "customer_id") == (None, "unresolved")
    # globally-issued scheme -> the scheme authority, regardless of catalog declaration
    assert resolve_identifier_issuer(db, "cat_b", "bank_bic") == ("swift", "global_scheme")
    assert "swift_bic" in GLOBAL_SCHEME_ISSUERS
    # non-identifier concept -> unresolved (no scheme, no issuer)
    assert resolve_identifier_issuer(db, "cat_a", "monetary_stock") == (None, "unresolved")
    assert resolve_identifier_issuer(db, "cat_a", None) == (None, "unresolved")


# ── the pairing truth table, folded into the ONE verdict function ────────────────────────────────


def _grounding(ref: str, *, concept_name: str, issuer: str | None, basis: str = "catalog_scope"):
    """A grounded endpoint shaped like `ground_bridge_endpoint`'s output, for direct verdict
    tests (the DB-integrated path is covered by the derivation tests below)."""
    from featuregen.overlay.upload.attest.bridge_grounding import (
        BridgeConceptGroundingV1,
        BridgeEndpointGroundingV1,
        EvidencePresence,
        MetadataFacetV1,
    )
    from featuregen.overlay.upload.attest.representation import RepresentationRole
    from featuregen.overlay.upload.bridge_assessment import (
        ConceptAuthority,
        KeyMemberRole,
        TupleKeyRole,
        TypeBasis,
    )

    source, rest = ref.split("::", 1)
    table_ref = f"{source}::{rest.rsplit('.', 1)[0]}"
    registered = CONCEPT_REGISTRY[concept_name]
    return BridgeEndpointGroundingV1(
        logical_column_ref=ref,
        logical_table_ref=table_ref,
        exists=True,
        column_name=rest.rsplit(".", 1)[1],
        concept=BridgeConceptGroundingV1(
            concept_name, ConceptAuthority.SOURCE, "source", (), True),
        governed_entity_id=None,
        advisory_entity_id=display_entity(concept_name, registered.entity_link),
        explicit_namespace=None,
        governed_population=None,
        representation_role=RepresentationRole.IDENTIFIER_VALUE,
        data_type_family="text",
        type_basis=TypeBasis.DECLARED,
        is_grain=False,
        key_member_role=KeyMemberRole.UNKNOWN,
        tuple_key_role=TupleKeyRole.UNKNOWN,
        observed_format=None,
        metadata_facets=(MetadataFacetV1("names", EvidencePresence.ABSENT),),
        evidence_refs=(),
        issuer_scope=issuer,
        issuer_basis=basis if issuer is not None else "unresolved",
    )


def test_same_scheme_same_known_issuer_is_a_normal_candidate() -> None:
    from featuregen.overlay.upload.attest.bridge_grounding import assess_grounded_identifier_link

    grounding = assess_grounded_identifier_link(
        _grounding("cat_a::public.t1.cust_num", concept_name="customer_id", issuer="bank_one"),
        _grounding("cat_b::public.t2.cif_id", concept_name="customer_id", issuer="bank_one"))
    assert grounding.hard_conflicts == ()
    assert "same_identifier_issuer_scope" in grounding.explanation_codes
    assert "issuer_unresolved" not in grounding.explanation_codes


def test_same_scheme_different_known_issuer_refuses_with_a_code() -> None:
    from featuregen.overlay.upload.attest.bridge_grounding import assess_grounded_identifier_link
    from featuregen.overlay.upload.bridge_assessment import NamespaceVerdict

    grounding = assess_grounded_identifier_link(
        _grounding("cat_a::public.t1.cust_num", concept_name="customer_id", issuer="bank_one"),
        _grounding("cat_b::public.t2.cif_id", concept_name="customer_id", issuer="bank_two"))
    assert "different_identifier_issuer_scope" in grounding.hard_conflicts
    assert grounding.namespace_verdict is NamespaceVerdict.DIFFERENT


def test_unresolved_issuer_is_advisory_and_never_equality_proof() -> None:
    """MUTATION (missing issuer never appears as verified): either side unresolved keeps the
    candidate ENUMERABLE (no hard conflict — AI-proposed stays usable) but flags it and never
    claims a SAME-namespace proof."""
    import dataclasses

    from featuregen.overlay.upload.attest.bridge_grounding import assess_grounded_identifier_link
    from featuregen.overlay.upload.bridge_assessment import NamespaceVerdict

    left = _grounding("cat_a::public.t1.cust_num", concept_name="customer_id", issuer=None)
    right = _grounding("cat_b::public.t2.cif_id", concept_name="customer_id", issuer="bank_one")
    grounding = assess_grounded_identifier_link(left, right)
    assert grounding.hard_conflicts == ()
    assert "issuer_unresolved" in grounding.explanation_codes
    assert grounding.namespace_verdict is not NamespaceVerdict.SAME

    # Even a governed same-scheme FACT pair is demoted to POSSIBLE while the issuer is unknown.
    governed_left = dataclasses.replace(left, explicit_namespace="cif")
    governed_right = dataclasses.replace(
        _grounding("cat_b::public.t2.cif_id", concept_name="customer_id", issuer=None),
        explicit_namespace="cif")
    grounding = assess_grounded_identifier_link(governed_left, governed_right)
    assert grounding.namespace_verdict is NamespaceVerdict.POSSIBLE
    assert "issuer_unresolved" in grounding.explanation_codes


def test_bic_and_cif_still_never_pair() -> None:
    """MUST-DIE: cross-scheme pairs stay impossible whatever the issuers say."""
    from featuregen.overlay.upload.attest.bridge_grounding import assess_grounded_identifier_link

    grounding = assess_grounded_identifier_link(
        _grounding("cat_a::public.t1.cust_swift_cd", concept_name="bank_bic", issuer="swift",
                   basis="global_scheme"),
        _grounding("cat_b::public.t2.cif_id", concept_name="customer_id", issuer="swift"))
    # Different schemes never reach the issuer comparison as a same-scheme pair.
    assert "same_identifier_issuer_scope" not in grounding.explanation_codes
    assert "different_identifier_issuer_scope" not in grounding.hard_conflicts


# ── the derivation path consumes the same verdict (no bypass) ────────────────────────────────────

_CIB_COLUMNS = (("cust_num", "customer_id", True), ("client_no", "customer_id", False))
_FTR_COLUMNS = (("cif_id", "customer_id", False), ("counter_party_cif_id", "counterparty_id",
                                                   False))


def _load(db, source: str, table: str, columns, *, declared: str) -> None:
    rows = [CanonicalRow(source, table, name, "unknown", is_grain=grain)
            for name, _c, grain in columns]
    build_graph(db, source, rows,
                concepts={content_hash(row): concept_name
                          for row, (_n, concept_name, _g) in zip(rows, columns, strict=True)},
                declared_types={f"public.{table}.{name}": declared
                                for name, _c, _g in columns})


def _fixture(db) -> None:
    _load(db, "cib", "bo_cib_customer", _CIB_COLUMNS, declared="varchar(150)")
    _load(db, "ftr", "tran_repos", _FTR_COLUMNS, declared="string")


def _pairs(candidates):
    return {frozenset({c.left_ref.column, c.right_ref.column}) for c in candidates}


def test_different_declared_issuers_refuse_cross_catalog_cif_pairs(db) -> None:
    from featuregen.overlay.upload.bridge_candidates import derive_bridge_candidates_with_report

    _fixture(db)
    declare_catalog_semantic_scope(db, catalog_source="cib", issuer_scope="bank_one",
                                   basis="catalog_scope", declared_by="user:one")
    declare_catalog_semantic_scope(db, catalog_source="ftr", issuer_scope="bank_two",
                                   basis="catalog_scope", declared_by="user:one")
    derivation = derive_bridge_candidates_with_report(db)
    assert _pairs(derivation.candidates) == set(), (
        "cif columns issued by two different banks must not pair")
    reasons = dict(derivation.reason_counts)
    assert reasons.get("hard_conflict:different_identifier_issuer_scope", 0) > 0


def test_same_declared_issuer_keeps_the_normal_candidates(db) -> None:
    _fixture(db)
    for source in ("cib", "ftr"):
        declare_catalog_semantic_scope(db, catalog_source=source, issuer_scope="bank_one",
                                       basis="catalog_scope", declared_by="user:one")
    candidates = derive_bridge_candidates(db)
    assert frozenset({"cust_num", "cif_id"}) in _pairs(candidates)
    for candidate in candidates:
        assert candidate.assessment is not None
        assert "same_identifier_issuer_scope" in candidate.assessment.explanation_codes
        assert "issuer_unresolved" not in candidate.assessment.explanation_codes


def test_undeclared_issuers_keep_advisory_candidates_flagged(db) -> None:
    _fixture(db)
    candidates = derive_bridge_candidates(db)
    assert frozenset({"cust_num", "cif_id"}) in _pairs(candidates), (
        "an unresolved issuer must keep the candidate enumerable — AI-proposed stays usable")
    for candidate in candidates:
        assert candidate.assessment is not None
        assert "issuer_unresolved" in candidate.assessment.explanation_codes


def test_entity_display_resolves_customer_but_keys_are_untouched(db) -> None:
    """The alias seam corrects the DISPLAY entity of counterparty-classified endpoints to
    `customer`; the registry's persisted `entity_link` (the fact-key input) is untouched."""
    _fixture(db)
    candidates = derive_bridge_candidates(db)
    by_pair = {frozenset({c.left_ref.column, c.right_ref.column}): c for c in candidates}
    subset = by_pair[frozenset({"client_no", "counter_party_cif_id"})]
    assert subset.entity_id == "customer"
    assert concept("counterparty_id").entity_link == "counterparty"


# ── Entity Map + semantic bundle display through the seam ────────────────────────────────────────


def test_entity_map_endpoint_displays_customer_for_counterparty_concept() -> None:
    from featuregen.overlay.upload.bridge_assessment import (
        IdentifierColumnMemberV1,
        IdentifierEndpointV1,
        KeyMemberRole,
        TypeBasis,
    )
    from featuregen.overlay.upload.entity_map import _endpoint_view

    endpoint = IdentifierEndpointV1(
        logical_table_ref="ftr::public.tran_repos",
        members=(IdentifierColumnMemberV1(
            "ftr::public.tran_repos.counter_party_cif_id", "text", TypeBasis.DECLARED,
            KeyMemberRole.UNKNOWN),),
        entity_id="counterparty",
        concept="counterparty_id",
    )
    view = _endpoint_view(endpoint)
    assert view.entity_id == "customer"
    assert view.concept == "counterparty_id"
    assert view.namespace == "cif"


def test_bundle_carries_issuer_aware_namespace_and_display_entity(db) -> None:
    _fixture(db)
    declare_catalog_semantic_scope(db, catalog_source="ftr", issuer_scope="bank_one",
                                   basis="catalog_scope", declared_by="user:one")
    from featuregen.overlay.upload import semantic_context as sc

    bundle = sc.bundle_from_store(db, "ftr", "public.tran_repos.counter_party_cif_id", roles=())
    assert bundle.identifier_namespace is not None
    assert bundle.identifier_namespace.scheme == "cif"
    assert bundle.identifier_namespace.issuer_scope == "bank_one"
    assert bundle.identifier_namespace.basis == "catalog_scope"
    assert "issuer_scope_unresolved" not in bundle.missing_context
    by_field = {v.field_name: v for v in bundle.resolved_semantics}
    assert by_field["entity"].value == "customer"


# ── migration 1045: backfill + search-doc reprojection ───────────────────────────────────────────

_MIGRATION = "src/featuregen/db/migrations/1045_catalog_semantic_scope_and_entity_alias.sql"


def test_migration_1045_backfills_display_entity_and_spares_governed_rows(db) -> None:
    """The migration's UPDATE against a SEEDED legacy shape (migration audits are blind to legacy
    data otherwise): counterparty display entities flip to customer; a governed VERIFIED entity
    and an explicitly different entity survive byte-for-byte. Idempotent by construction."""
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "concept, entity) VALUES "
        "('legacy','public.t.cp_cif','column','t','cp_cif','counterparty_id','counterparty'),"
        "('legacy','public.t.other','column','t','other','counterparty_id','legal_entity'),"
        "('legacy','public.t.cust','column','t','cust','customer_id','customer')")
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "concept, entity, entity_status) VALUES "
        "('legacy','public.t.governed','column','t','governed','counterparty_id','counterparty',"
        "'VERIFIED')")
    sql = (_ROOT / _MIGRATION).read_text()
    db.execute(sql)
    rows = dict(db.execute(
        "SELECT column_name, entity FROM graph_node WHERE catalog_source='legacy'").fetchall())
    assert rows["cp_cif"] == "customer"
    assert rows["other"] == "legal_entity"      # an explicit different entity is a decision
    assert rows["cust"] == "customer"
    assert rows["governed"] == "counterparty"   # governed VERIFIED rows are never backfilled
    # The companion reprojection rebuilds the affected search docs via graph.py's ONE expression.
    rebuilt = reproject_alias_entity_search_docs(db)
    assert rebuilt >= 3
    (doc_matches,) = db.execute(
        "SELECT search_doc @@ plainto_tsquery('english', 'customer') FROM graph_node "
        "WHERE catalog_source='legacy' AND column_name='cp_cif'").fetchone()
    assert doc_matches is True


def test_migration_1045_is_reserved_and_unique() -> None:
    migrations = sorted(p.name for p in (_ROOT / "src/featuregen/db/migrations").glob("1045_*"))
    assert migrations == ["1045_catalog_semantic_scope_and_entity_alias.sql"], (
        "migration prefix 1045 is reserved for semantic Task 2 (interface doc D7) and must stay "
        "a single full filename")
