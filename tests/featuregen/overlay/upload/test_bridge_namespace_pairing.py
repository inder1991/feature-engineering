"""The namespace pairing rule — identifier NAMESPACE gates candidacy, entity only corroborates.

Handoff spec: docs/superpowers/plans/2026-07-31-ingestion-richness-and-correctness-remediation.md
("Handoff to the Bridge Session — Namespace Pairing Rule").

`derive_bridge_candidates` groups identifier columns by ``Concept.namespace`` instead of
``Concept.entity_link``. Cross-namespace pairs are IMPOSSIBLE (the live decoys: swift_bic↔cif,
swift_bic↔correspondent_scheme_code, internal↔external account). Entity is still carried on the
candidate — ``fact_key`` hashes ``entity_id`` — so the pick must be DETERMINISTIC:

* endpoints' entities agree -> that entity;
* disagree -> the SUBJECT-role endpoint's entity where ``normalize_party_role`` distinguishes
  exactly one subject;
* otherwise the lexicographic minimum, and an ``entity_disagreement`` explanation code either way.

The fixture is CIB/FTR-shaped: glossary-declared types, the real column names, and same-entity
DIFFERENT-namespace decoy columns so an entity-grouping regression cannot pass the must-die test.
"""
from __future__ import annotations

from datetime import UTC, datetime

from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact

from featuregen.overlay.identity import CatalogObjectRef, EntityBridgeRef, fact_key
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.store import load_fact
from featuregen.overlay.upload.bridge_assessment import PopulationRelation
from featuregen.overlay.upload.bridge_candidates import (
    derive_bridge_candidate_for_refs,
    derive_bridge_candidates,
)
from featuregen.overlay.upload.bridge_propose import propose_bridge
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter

_T0 = datetime(2026, 8, 1, 12, 0, tzinfo=UTC)

#: The entity-era fact key of the existing Customer link (cib cust_num <-> ftr cif_id, entity
#: customer), computed under the pre-namespace derivation on this exact fixture. The namespace
#: rule changes which candidates DERIVE, never how an existing bridge fact is identified — if this
#: literal ever fails, live Customer links would silently re-key.
_ENTITY_ERA_CUSTOMER_LINK_FACT_KEY = (
    "db7949340659c3c056ecdab263f6c67b128bf8ed60d547db6d4d69925ed96248")

_CIB_COLUMNS = (
    # (column, concept, is_grain) — concepts: customer_id/counterparty_id share namespace "cif";
    # bank_bic is "swift_bic"; clearing_member_code is "correspondent_scheme_code";
    # account_id ("internal_account") vs external_account_ref ("external_account") share the
    # ACCOUNT entity but not a namespace — the entity-grouping decoy shape.
    ("cust_num", "customer_id", True),
    ("client_no", "customer_id", False),
    ("cust_swift_cd", "bank_bic", False),
    ("cust_acct_no", "account_id", False),
)
_FTR_COLUMNS = (
    ("cif_id", "customer_id", False),
    ("counter_party_cif_id", "counterparty_id", False),
    ("beneficiary_cif", "counterparty_id", False),
    ("sender_bic", "bank_bic", False),
    ("counter_party_scheme_code", "clearing_member_code", False),
    ("counter_party_acct_no", "external_account_ref", False),
)


def _load_glossary(db, source: str, table: str, columns, *, declared: str) -> None:
    rows = [CanonicalRow(source, table, name, "unknown", is_grain=is_grain)
            for name, _concept, is_grain in columns]
    build_graph(
        db, source, rows,
        concepts={content_hash(row): concept_name
                  for row, (_name, concept_name, _grain) in zip(rows, columns, strict=True)},
        declared_types={f"public.{table}.{name}": declared for name, _c, _g in columns})


def _cib_ftr_fixture(db, *, cib_columns=_CIB_COLUMNS, ftr_columns=_FTR_COLUMNS) -> None:
    _load_glossary(db, "cib", "bo_cib_customer", cib_columns, declared="varchar(150)")
    _load_glossary(db, "ftr", "tran_repos", ftr_columns, declared="string")


def _by_pair(candidates):
    """Candidates keyed by their unordered column-name pair (tables are unique per side here)."""
    return {frozenset({c.left_ref.column, c.right_ref.column}): c for c in candidates}


def _fact_key_of(candidate) -> str:
    return fact_key(
        EntityBridgeRef(entity_id=candidate.entity_id, left_ref=candidate.left_ref,
                        right_ref=candidate.right_ref),
        "entity_bridge")


# ── (1) cross-namespace candidates are impossible ────────────────────────────────────────────────

def test_zero_cross_namespace_candidates(db):
    """swift_bic↔cif, swift_bic↔correspondent_scheme_code and internal↔external account pairs —
    all same-entity or same-type decoy shapes — must not derive."""
    _cib_ftr_fixture(db)
    pairs = set(_by_pair(derive_bridge_candidates(db)))
    assert frozenset({"cust_swift_cd", "counter_party_scheme_code"}) not in pairs, (
        "swift_bic paired with a correspondent scheme code — entity grouping is back")
    assert frozenset({"cust_acct_no", "counter_party_acct_no"}) not in pairs, (
        "an internal account paired with an EXTERNAL institution's account reference")
    cif_columns = {"cust_num", "client_no", "cif_id", "counter_party_cif_id", "beneficiary_cif"}
    swift_columns = {"cust_swift_cd", "sender_bic"}
    for pair in pairs:
        assert not (pair & cif_columns and pair & swift_columns), (
            f"a swift_bic column paired with a cif column: {sorted(pair)}")


def test_must_die_no_candidate_spans_two_namespaces(db):
    """MUST-DIE MUTATION GUARD (owned here per the handoff): over ALL derived candidates on the
    rich fixture, the two endpoint concepts declare the SAME identifier namespace. Any grouping
    regression that pairs across namespaces dies here."""
    _cib_ftr_fixture(db)
    candidates = derive_bridge_candidates(db)
    assert candidates, "the guard must not pass vacuously"
    for candidate in candidates:
        namespaces = set()
        for ref in (candidate.left_ref, candidate.right_ref):
            concept_name = db.execute(
                "SELECT concept FROM graph_node WHERE catalog_source = %s "
                "AND object_ref = %s AND kind = 'column'",
                (ref.catalog_source, f"public.{ref.table}.{ref.column}")).fetchone()[0]
            registered = concept(concept_name)
            assert registered is not None and registered.namespace, (
                f"candidate endpoint {ref.catalog_source}.{ref.table}.{ref.column} has no "
                f"registered identifier namespace ({concept_name!r})")
            namespaces.add(registered.namespace)
        assert len(namespaces) == 1, (
            f"candidate {candidate.candidate_id} spans namespaces {sorted(namespaces)}: "
            f"{candidate.left_ref.column} <-> {candidate.right_ref.column}")


# ── (2) the cif group: existing Customer link identity + the new subset candidate ────────────────

def test_cif_group_yields_both_customer_and_counterparty_subset_links(db):
    _cib_ftr_fixture(db)
    by_pair = _by_pair(derive_bridge_candidates(db))

    customer_link = by_pair.get(frozenset({"cust_num", "cif_id"}))
    assert customer_link is not None, "the existing Customer link must still derive"
    assert customer_link.entity_id == "customer"
    assert customer_link.assessment is not None
    assert "entity_disagreement" not in customer_link.assessment.explanation_codes

    subset = by_pair.get(frozenset({"cust_num", "counter_party_cif_id"}))
    assert subset is not None, (
        "cust_num and counter_party_cif_id share the cif namespace and must derive")
    assert subset.assessment is not None
    assert subset.assessment.governed_population_relation is PopulationRelation.UNKNOWN


def test_existing_customer_link_fact_key_is_byte_identical_to_the_entity_era_key(db):
    """The pick rule resolves the agreeing pair to its shared entity, so the fact key minted under
    the namespace rule equals the entity-era key byte for byte — proven against both the recorded
    literal and a recomputation under the OLD derivation rule (entity = the shared entity_link)."""
    _cib_ftr_fixture(db)
    candidate = _by_pair(derive_bridge_candidates(db))[frozenset({"cust_num", "cif_id"})]
    entity_era_key = fact_key(
        EntityBridgeRef(
            entity_id=concept("customer_id").entity_link,   # the OLD grouping key
            left_ref=CatalogObjectRef("cib", "column", "public", "bo_cib_customer", "cust_num"),
            right_ref=CatalogObjectRef("ftr", "column", "public", "tran_repos", "cif_id")),
        "entity_bridge")
    assert entity_era_key == _ENTITY_ERA_CUSTOMER_LINK_FACT_KEY
    assert _fact_key_of(candidate) == _ENTITY_ERA_CUSTOMER_LINK_FACT_KEY


# ── (3) the swift_bic group + the deterministic entity pick ──────────────────────────────────────

def test_swift_bic_group_yields_bank_links_with_a_deterministic_entity(db):
    _cib_ftr_fixture(db)
    by_pair = _by_pair(derive_bridge_candidates(db))
    bank_link = by_pair.get(frozenset({"cust_swift_cd", "sender_bic"}))
    assert bank_link is not None, "bank-namespace endpoints must pair inside swift_bic"
    assert bank_link.entity_id == "bank"
    assert bank_link.assessment is not None
    assert "entity_disagreement" not in bank_link.assessment.explanation_codes


def test_entity_disagreement_prefers_the_single_subject_role_endpoint(db):
    """cust_num (SUBJECT per party_vocab) x counter_party_cif_id (COUNTERPARTY): exactly one
    subject -> the subject's entity, plus the explanation code. Same for beneficiary_cif, whose
    name resolves NO party role at all."""
    _cib_ftr_fixture(db)
    by_pair = _by_pair(derive_bridge_candidates(db))
    for other in ("counter_party_cif_id", "beneficiary_cif"):
        candidate = by_pair[frozenset({"cust_num", other})]
        assert candidate.entity_id == "customer", other
        assert candidate.assessment is not None
        assert "entity_disagreement" in candidate.assessment.explanation_codes, other


def test_entity_disagreement_without_a_single_subject_falls_to_lexicographic_minimum(db):
    """client_no resolves no party role and counter_party_cif_id is COUNTERPARTY (not subject):
    zero subjects -> min('counterparty', 'customer') = 'counterparty', plus the code."""
    _cib_ftr_fixture(db)
    by_pair = _by_pair(derive_bridge_candidates(db))
    for other in ("counter_party_cif_id", "beneficiary_cif"):
        candidate = by_pair[frozenset({"client_no", other})]
        assert candidate.entity_id == "counterparty", other
        assert candidate.assessment is not None
        assert "entity_disagreement" in candidate.assessment.explanation_codes, other


# ── (4) determinism across input orders ──────────────────────────────────────────────────────────

def test_entity_pick_and_candidate_ids_are_input_order_independent(db):
    _cib_ftr_fixture(db)
    forward = sorted(
        (c.candidate_id, c.entity_id, _fact_key_of(c)) for c in derive_bridge_candidates(db))
    build_graph(db, "cib", [], concepts={})
    build_graph(db, "ftr", [], concepts={})
    _cib_ftr_fixture(
        db,
        cib_columns=tuple(reversed(_CIB_COLUMNS)),
        ftr_columns=tuple(reversed(_FTR_COLUMNS)))
    shuffled = sorted(
        (c.candidate_id, c.entity_id, _fact_key_of(c)) for c in derive_bridge_candidates(db))
    assert forward == shuffled


# ── the full expected candidate set on the fixture (the before/after pin) ────────────────────────

def test_the_namespace_rule_candidate_set_on_the_cib_ftr_fixture(db):
    """The entity-era rule derived 5 candidates here, two of them cross-namespace decoys, and
    missed every cross-entity cif pair. The namespace rule derives exactly these seven."""
    _cib_ftr_fixture(db)
    derived = {
        (c.left_ref.column, c.right_ref.column, c.entity_id)
        for c in derive_bridge_candidates(db)}
    assert derived == {
        ("cust_num", "cif_id", "customer"),
        ("cust_num", "counter_party_cif_id", "customer"),
        ("cust_num", "beneficiary_cif", "customer"),
        ("client_no", "cif_id", "customer"),
        ("client_no", "counter_party_cif_id", "counterparty"),
        ("client_no", "beneficiary_cif", "counterparty"),
        ("cust_swift_cd", "sender_bic", "bank"),
    }


# ── party_role explains the pick; it never gates candidacy ───────────────────────────────────────

def test_candidate_pair_set_is_party_role_invariant(db, monkeypatch):
    """The advisory contract survives the entity pick: with every party role forced to None, the
    SAME pairs derive with the SAME candidate ids — only the tie-broken entity label may move.
    A future predicate that let a role decide WHETHER a pair derives dies here."""
    import featuregen.overlay.upload.bridge_candidates as bridge_candidates

    _cib_ftr_fixture(db)
    with_roles = {
        frozenset({c.left_ref.column, c.right_ref.column}): c.candidate_id
        for c in derive_bridge_candidates(db)}
    monkeypatch.setattr(
        bridge_candidates, "normalize_party_role", lambda _name: None)
    without_roles = {
        frozenset({c.left_ref.column, c.right_ref.column}): c.candidate_id
        for c in derive_bridge_candidates(db)}
    assert with_roles == without_roles


# ── the per-refs reassessment sibling follows the same rule ──────────────────────────────────────

def test_per_refs_reassessment_pairs_by_namespace_not_entity(db):
    _cib_ftr_fixture(db)
    cust_num = CatalogObjectRef("cib", "column", "public", "bo_cib_customer", "cust_num")
    cp_cif = CatalogObjectRef("ftr", "column", "public", "tran_repos", "counter_party_cif_id")
    candidate = derive_bridge_candidate_for_refs(db, cust_num, cp_cif)
    assert candidate is not None, "same cif namespace must reassess despite differing entities"
    assert candidate.entity_id == "customer"

    swift = CatalogObjectRef("cib", "column", "public", "bo_cib_customer", "cust_swift_cd")
    scheme = CatalogObjectRef("ftr", "column", "public", "tran_repos",
                              "counter_party_scheme_code")
    assert derive_bridge_candidate_for_refs(db, swift, scheme) is None, (
        "same entity (bank) but different namespaces must not reassess into a candidate")


# ── sticky rejection does not transfer across entities ───────────────────────────────────────────

def test_rejecting_an_old_decoy_never_blocks_rederivation_under_a_different_entity(db):
    """An old decoy fact for the SAME column pair under a DIFFERENT entity has a different
    fact_key, so its sticky rejection cannot block the namespace-rule candidate."""
    ensure_upload_catalog_adapter()
    _cib_ftr_fixture(db)
    cust_num = CatalogObjectRef("cib", "column", "public", "bo_cib_customer", "cust_num")
    cp_cif = CatalogObjectRef("ftr", "column", "public", "tran_repos", "counter_party_cif_id")
    decoy_key = fact_key(
        EntityBridgeRef(entity_id="counterparty", left_ref=cust_num, right_ref=cp_cif),
        "entity_bridge")
    govern_bridge_fact(
        db, decoy_key, entity="counterparty",
        left_source="cib", left_ref="public.bo_cib_customer.cust_num",
        right_source="ftr", right_ref="public.tran_repos.counter_party_cif_id",
        status="REJECTED")

    candidate = _by_pair(derive_bridge_candidates(db))[
        frozenset({"cust_num", "counter_party_cif_id"})]
    assert candidate.entity_id == "customer"
    new_key = propose_bridge(db, candidate, actor=_ENRICH_ACTOR, now=_T0)
    assert new_key != decoy_key
    assert fold_overlay_state(load_fact(db, new_key)).status == "DRAFT"
    assert fold_overlay_state(load_fact(db, decoy_key)).status == "REJECTED"
