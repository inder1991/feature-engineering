"""`SemanticContextBundleV1` (semantic plan Task 1, amended by the verified-interfaces doc).

Guards, per the controlling doc:

* D1 — one hash scheme (`materialize_hash`), key-order invariant, exclusions honored, and the two
  builders byte-match on shared fields through the same canonicalizer.
* D2 — evidence authority is the REAL (producer, strength, lifecycle) triple; the flat label is a
  DERIVED display projection nothing branches on.
* D3 — relationship context is link + tuple-of-directional-realizations mirroring the shipped
  readers; availability never encodes safety; the pure production predicate labels history only.
* D4 — observation context mirrors `RelationshipObservationV2` field-for-field; a sample may
  disprove uniqueness but never establish it; an observation applies only to its exact realization
  revision + applicability scope.
* D5 — the closed vocabularies are defined HERE, before first emission, and every
  `UNRESOLVED_REASONS` member maps into the three product families.
* D11 — both builders take roles and filter every neighbour/link read through visible_requires.

Review pitfalls covered (2026-08-01 plan review, section C Task 1): the un-scoped-builder hole, the
per-field N×M fan-out (157-scan class), the missing-vocabulary hole, the public-flattening
byte-identity fork, and `concept_path` on the `unclassified` sentinel.
"""
from __future__ import annotations

import dataclasses
from datetime import UTC, datetime

import pytest
from tests.featuregen.overlay.upload._bridge_fixtures import govern_bridge_fact
from tests.featuregen.overlay.upload.test_bridge_assessment_contracts import (
    _executable_pair,
    _realization,
)

from featuregen.data_agent.physical import record_binding_revision
from featuregen.data_agent.relationship_observation import (
    EndpointTupleObservationV2,
    RelationshipObservationV2,
    RowCoverage,
)
from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.projection import OverlayProjection
from featuregen.overlay.upload import semantic_context as sc
from featuregen.overlay.upload.bridge_assessment import (
    IdentifierLinkAssessmentV1,
    LinkReviewStatus,
    NamespaceVerdict,
    PopulationRelation,
)
from featuregen.overlay.upload.bridge_realization import (
    BridgeRealizationCurrentV1,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    record_candidate_assessment,
    record_realization_revision,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.column_authority import read_column_facts
from featuregen.overlay.upload.concepts import UNCLASSIFIED, concept_path
from featuregen.overlay.upload.glossary_reader import GlossaryRecord
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.ingest import _write_glossary_source_evidence
from featuregen.projections.runner import run_projection

# ── fixtures ─────────────────────────────────────────────────────────────────────────────────────

_ALL_ACCESS = ("pii_reader", "restricted_reader", "confidential_reader")


def _rows(source: str = "cib", table: str = "bo_cib_customer", n_extra: int = 0):
    rows = [
        CanonicalRow(source, table, "cust_num", "unknown", is_grain=True,
                     definition="Customer number."),
        CanonicalRow(source, table, "cust_name", "unknown", sensitivity="pii"),
        CanonicalRow(source, table, "cust_swift_cd", "unknown"),
    ]
    rows += [CanonicalRow(source, table, f"extra_{i:02d}", "unknown") for i in range(n_extra)]
    return rows


def _record(source: str = "cib", table: str = "bo_cib_customer", column: str = "cust_num",
            schema: str = "dpl_core") -> GlossaryRecord:
    return GlossaryRecord(
        logical_ref=f"{source}::{schema}.{table}.{column}",
        term_name="Customer Number",
        definition="Customer number.",
        domain="Party",
        synonyms=("cif",),
        bian_path="Party/Reference",
        fibo_path="FIBO/Party",
        term_type="dimension",
        process_path="Onboarding / KYC",
        related_terms=("customer", "cif"),
        schema=schema,
        physical_fqn=f"{schema}.{table}.{column}",
        declared_type="varchar(20)",
    )


def _graph(db, rows, *, concepts: dict[str, str] | None = None,
           schemas: dict[str, str] | None = None) -> None:
    from featuregen.overlay.upload.enrich import content_hash
    by_hash = {}
    for row in rows:
        if concepts and row.column in concepts:
            by_hash[content_hash(row)] = concepts[row.column]
    build_graph(db, rows[0].source, rows, concepts=by_hash, schemas=schemas or {})


class _CountingConn:
    """Counts every SQL statement issued through this connection (execute + cursor.execute)."""

    def __init__(self, inner):
        self._inner = inner
        self.count = 0

    def execute(self, *args, **kwargs):
        self.count += 1
        return self._inner.execute(*args, **kwargs)

    def cursor(self, *args, **kwargs):
        outer = self

        class _Cur:
            def __init__(self, cur):
                self._cur = cur

            def execute(self, *a, **k):
                outer.count += 1
                return self._cur.execute(*a, **k)

            def __getattr__(self, name):
                return getattr(self._cur, name)

            def __enter__(self):
                self._cur.__enter__()
                return self

            def __exit__(self, *exc):
                return self._cur.__exit__(*exc)

        return _Cur(self._inner.cursor(*args, **kwargs))

    def __getattr__(self, name):
        return getattr(self._inner, name)


# ── concept_path (cycle-safe public ancestor helper) ─────────────────────────────────────────────


def test_concept_path_walks_is_a_ancestry() -> None:
    assert concept_path("monetary_stock") == ("monetary_stock",)
    assert concept_path("ead") == ("ead", "monetary_stock")
    assert concept_path("pd_ttc") == ("pd_ttc", "pd")


def test_concept_path_unclassified_and_unknown_are_empty() -> None:
    """`unclassified` is a sentinel, never a registry member (review C Task 1): empty tuple, and
    the bundle carries the missing-context code instead."""
    assert concept_path(UNCLASSIFIED) == ()
    assert concept_path(None) == ()
    assert concept_path("no_such_concept") == ()


def test_concept_path_refuses_a_corrupt_cycle(monkeypatch) -> None:
    from featuregen.overlay.upload import concepts as concepts_mod

    a = dataclasses.replace(concepts_mod.CONCEPT_REGISTRY["monetary_stock"], is_a="ead")
    monkeypatch.setitem(concepts_mod.CONCEPT_REGISTRY, "monetary_stock", a)
    with pytest.raises(ValueError, match="cycle"):
        concept_path("ead")


# ── closed vocabularies (D5) ─────────────────────────────────────────────────────────────────────


def test_closed_vocabularies_are_non_empty_and_family_mapped() -> None:
    assert sc.MISSING_CONTEXT_CODES and sc.REASON_CODES and sc.UNRESOLVED_REASONS
    assert sc.UNRESOLVED_REASON_FAMILIES == {
        "undecided", "needs_data_check", "structurally_unsuitable"}
    for member, family in sc.UNRESOLVED_REASONS.items():
        assert family in sc.UNRESOLVED_REASON_FAMILIES, member
        assert member.split(":", 1)[0] == family, member
    assert "undecided:no_evidence" in sc.UNRESOLVED_REASONS


def test_emitted_missing_context_codes_are_closed(db) -> None:
    rows = _rows()
    _graph(db, rows)
    bundle = sc.bundle_from_store(db, "cib", "public.bo_cib_customer.cust_num",
                                  roles=_ALL_ACCESS)
    assert bundle.missing_context, "an un-enriched column must report missing context"
    assert set(bundle.missing_context) <= sc.MISSING_CONTEXT_CODES
    assert bundle.missing_context == tuple(sorted(bundle.missing_context))
    assert "concept_unclassified" in bundle.missing_context


# ── D2 display projection ────────────────────────────────────────────────────────────────────────


def test_display_authority_label_projects_the_d2_table() -> None:
    label = sc.display_authority_label
    assert label("source", "attested") == "source_attested"
    assert label("source", "confirmed") == "source_attested"
    assert label("source", "proposed") == "source_proposed"
    assert label("source", "supported") == "source_proposed"
    assert label("human", "confirmed") == "human"
    assert label("llm", "proposed") == "llm_proposed"
    assert label("llm", "confirmed") == "llm_proposed"
    assert label("profiler", "attested") == "deterministic"
    assert label("structural_connector", "attested") == "deterministic"
    assert label("parser", "supported") == "system"
    assert label("taxonomy", "proposed") == "system"
    assert label("legacy", "confirmed") == "system"
    # The governed row comes from the OperationalColumnFacts axis, never a producer.
    assert label("source", "attested", operational_influence="governed") == "governed"


# ── bundle_from_upload ───────────────────────────────────────────────────────────────────────────


def test_bundle_from_upload_carries_schema_preserving_refs_and_source_triples() -> None:
    rows = _rows()
    bundle = sc.bundle_from_upload(
        rows[0], glossary_record=_record(), cohort=rows, roles=_ALL_ACCESS)
    assert bundle.catalog_source == "cib"
    assert bundle.object_ref == "cib::dpl_core.bo_cib_customer.cust_num"
    assert bundle.table_ref == "cib::dpl_core.bo_cib_customer"
    by_field = {v.field_name: v for v in bundle.source_semantics}
    # The glossary profile's own strengths, via strength_for — never hardcoded elsewhere.
    assert by_field["definition"].evidence[0].strength == "attested"
    assert by_field["domain"].evidence[0].strength == "proposed"
    assert by_field["business_term"].value == "Customer Number"
    assert by_field["related_terms"].value == "customer, cif"
    for value in bundle.source_semantics:
        for ev in value.evidence:
            assert ev.producer == "source"
            assert ev.lifecycle == "active"
    # Stable ordering: sorted by field_name.
    fields = [v.field_name for v in bundle.source_semantics]
    assert fields == sorted(fields)
    # No resolution happened yet: resolved semantics are absent, not invented.
    assert bundle.resolved_semantics == ()
    assert bundle.concept_path == ()
    assert "concept_unclassified" in bundle.missing_context


def test_bundle_from_upload_scopes_neighbours_by_declared_tag() -> None:
    rows = _rows()
    unprivileged = sc.bundle_from_upload(
        rows[0], glossary_record=_record(), cohort=rows, roles=())
    names = {n.column_name for n in unprivileged.neighbouring_columns}
    assert "cust_name" not in names, "a pii-declared neighbour leaked to an unprivileged caller"
    privileged = sc.bundle_from_upload(
        rows[0], glossary_record=_record(), cohort=rows, roles=_ALL_ACCESS)
    assert "cust_name" in {n.column_name for n in privileged.neighbouring_columns}
    # The scope decision changes content — the hash must not pretend the two views are one.
    assert unprivileged.content_hash != privileged.content_hash


def test_bundle_from_upload_is_deterministic_and_cohort_order_independent() -> None:
    rows = _rows()
    a = sc.bundle_from_upload(rows[0], glossary_record=_record(), cohort=rows,
                              roles=_ALL_ACCESS)
    b = sc.bundle_from_upload(rows[0], glossary_record=_record(),
                              cohort=list(reversed(rows)), roles=_ALL_ACCESS)
    assert a == b
    assert a.content_hash == b.content_hash


def test_neighbour_roster_is_bounded_with_loud_truncation() -> None:
    rows = _rows(n_extra=sc.NEIGHBOUR_LIMIT + 10)
    bundle = sc.bundle_from_upload(rows[0], glossary_record=_record(), cohort=rows,
                                   roles=_ALL_ACCESS)
    assert len(bundle.neighbouring_columns) == sc.NEIGHBOUR_LIMIT
    assert "neighbour_roster_truncated" in bundle.missing_context
    refs = [n.object_ref for n in bundle.neighbouring_columns]
    assert refs == sorted(refs)


# ── hashing (D1) ─────────────────────────────────────────────────────────────────────────────────


def test_content_hash_uses_the_shared_jcs_hasher() -> None:
    rows = _rows()
    bundle = sc.bundle_from_upload(rows[0], glossary_record=_record(), cohort=rows,
                                   roles=_ALL_ACCESS)
    payload = sc.bundle_payload(bundle)
    assert "content_hash" not in payload
    assert bundle.content_hash == materialize_hash(payload)


def test_profile_identity_fields_rekey_the_hash() -> None:
    """MUTATION (omit profile identity): a bundle with `catalog_profile_revision_id` /
    `dataset_profile_hash` set must hash differently from one without them."""
    rows = _rows()
    plain = sc.bundle_from_upload(rows[0], glossary_record=_record(), cohort=rows,
                                  roles=_ALL_ACCESS)
    profiled = sc.bundle_from_upload(
        rows[0], glossary_record=_record(), cohort=rows, roles=_ALL_ACCESS,
        catalog_profile_revision_id="cprr-1", dataset_profile_hash="dph-1")
    assert plain.content_hash != profiled.content_hash
    assert "dataset_profile_absent" in plain.missing_context
    assert "dataset_profile_absent" not in profiled.missing_context


# ── relationship context (D3) ────────────────────────────────────────────────────────────────────


def _realization_context(**overrides) -> sc.DirectionalRealizationContextV1:
    base = dict(
        realization_revision_id="rrv-1",
        from_ref="cib::public.customers",
        to_ref="ftr::public.transactions",
        lifecycle="active",
        safety_status="deterministically_validated",
        cardinality="many_to_one",
        scope_id="scope-1",
        sandbox_eligible=True,
        production_eligible=True,
    )
    base.update(overrides)
    return sc.DirectionalRealizationContextV1(**base)


def _relationship(realizations=()) -> sc.RelationshipContextV1:
    return sc.RelationshipContextV1(
        relationship_ref="bridge-fact-1",
        kind="direct_equality",
        left_ref="cib::public.customers.customer_id",
        right_ref="ftr::public.transactions.customer_id",
        availability="available",
        review_status="unreviewed",
        assessment_revision_id="crv-1",
        realizations=tuple(realizations),
        producer="taxonomy",
        strength="proposed",
        lifecycle="active",
        current=True,
        evidence_ids=(),
    )


def test_swapping_realization_direction_changes_the_hash() -> None:
    """MUTATION (invert direction): the two directions of one link are distinct content."""
    rows = _rows()
    forward = sc.bundle_from_upload(
        rows[0], glossary_record=_record(), cohort=rows, roles=_ALL_ACCESS,
        relationship_context=(_relationship([_realization_context()]),))
    swapped = sc.bundle_from_upload(
        rows[0], glossary_record=_record(), cohort=rows, roles=_ALL_ACCESS,
        relationship_context=(_relationship([_realization_context(
            from_ref="ftr::public.transactions", to_ref="cib::public.customers")]),))
    assert forward.content_hash != swapped.content_hash


def test_missing_realization_scope_reports_missing_context() -> None:
    """MUTATION (drop scope): a realization without an applicability scope is honest, loud
    missing context — never silently equivalent to a scoped one."""
    rows = _rows()
    unscoped = sc.bundle_from_upload(
        rows[0], glossary_record=_record(), cohort=rows, roles=_ALL_ACCESS,
        relationship_context=(_relationship(
            [_realization_context(scope_id=None, sandbox_eligible=False,
                                  production_eligible=False)]),))
    assert "realization_scope_missing" in unscoped.missing_context


def test_a_lying_production_eligibility_dies() -> None:
    """MUTATION (stale projected as current / review implies safety): `production_eligible` is the
    PURE predicate over (lifecycle, safety); a context claiming production eligibility for a stale
    or unvalidated realization must refuse to construct."""
    with pytest.raises(sc.SemanticContextError):
        _realization_context(lifecycle="stale")
    with pytest.raises(sc.SemanticContextError):
        _realization_context(safety_status="unassessed")
    with pytest.raises(sc.SemanticContextError):
        _realization_context(sandbox_eligible=False)
    # And availability never encodes safety: only the two LinkAvailability words are legal.
    with pytest.raises(sc.SemanticContextError):
        dataclasses.replace(_relationship(), availability="executable")


def _stored_link_with_realization(db):
    """Seed ONE governed link with one current realization, visible to any caller, and return the
    realization revision (shared by the D3 composition test and the D4 currentness tests)."""
    left, right = _executable_pair()
    assessment = IdentifierLinkAssessmentV1(
        left_endpoint=left, right_endpoint=right,
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1", bridge_fact_key="bridge-fact-1")
    record_candidate_assessment(db, assessment, expected_pointer_version=0)
    govern_bridge_fact(
        db, "bridge-fact-1", entity="customer", left_source="cib",
        left_ref="public.customers.customer_id", right_source="ftr",
        right_ref="public.transactions.customer_id", status="DRAFT")
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    revision = _realization(left, right)
    current = BridgeRealizationCurrentV1(
        revision.realization_id, revision.realization_revision_id,
        SafetyStatus.DETERMINISTICALLY_VALIDATED, LinkReviewStatus.UNREVIEWED,
        RealizationLifecycle.ACTIVE, 1)
    record_realization_revision(
        db, revision, current,
        dependencies=(BridgeDependencyRefV1("bridge_fact", "bridge-fact-1", "head-1"),))
    # Both endpoint columns exist in the graph and are unrestricted, so the link is visible.
    _graph(db, [CanonicalRow("cib", "customers", "customer_id", "text", is_grain=True)])
    build_graph(db, "ftr", [CanonicalRow("ftr", "transactions", "customer_id", "text")])
    run_projection(db, OverlayProjection())     # catch the overlay checkpoint up to the head
    return assessment, revision


def test_relationship_context_from_the_store_composes_the_shipped_readers(db) -> None:
    assessment, revision = _stored_link_with_realization(db)

    bundle = sc.bundle_from_store(db, "cib", "public.customers.customer_id", roles=())
    (link,) = bundle.relationship_context
    assert link.relationship_ref == "bridge-fact-1"
    assert link.kind == "direct_equality"
    assert link.availability == "available"
    assert link.review_status == "unreviewed"
    assert link.assessment_revision_id == assessment.candidate_revision_id
    (realized,) = link.realizations
    assert realized.realization_revision_id == revision.realization_revision_id
    assert realized.scope_id == revision.applicability_scope.scope_id
    assert realized.from_ref == "cib::public.customers"
    assert realized.to_ref == "ftr::public.transactions"
    assert realized.production_eligible is True
    assert realized.cardinality == "many_to_one"


def test_entity_map_and_bundle_share_one_realization_composition(db, monkeypatch) -> None:
    """The link+realizations composition is ONE shared function: entity_map renders through it, so
    a divergence is impossible by construction (poisoning the shared seam changes the map)."""
    from featuregen.overlay.upload import entity_map

    left, right = _executable_pair()
    assessment = IdentifierLinkAssessmentV1(
        left_endpoint=left, right_endpoint=right,
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1", bridge_fact_key="bridge-fact-1")
    record_candidate_assessment(db, assessment, expected_pointer_version=0)
    govern_bridge_fact(
        db, "bridge-fact-1", entity="customer", left_source="cib",
        left_ref="public.customers.customer_id", right_source="ftr",
        right_ref="public.transactions.customer_id", status="DRAFT")
    record_binding_revision(db, left.physical_binding)
    record_binding_revision(db, right.physical_binding)
    revision = _realization(left, right)
    current = BridgeRealizationCurrentV1(
        revision.realization_id, revision.realization_revision_id,
        SafetyStatus.DETERMINISTICALLY_VALIDATED, LinkReviewStatus.UNREVIEWED,
        RealizationLifecycle.ACTIVE, 1)
    record_realization_revision(
        db, revision, current,
        dependencies=(BridgeDependencyRefV1("bridge_fact", "bridge-fact-1", "head-1"),))

    assert entity_map.composed_link_realizations is sc.composed_link_realizations
    monkeypatch.setattr(sc, "composed_link_realizations", lambda conn, key: ())
    monkeypatch.setattr(entity_map, "composed_link_realizations",
                        lambda conn, key: ())
    (link,) = entity_map.build_entity_map(db).links
    assert link.realizations == (), "entity_map did not render through the shared composition"


# ── observation context (D4) ─────────────────────────────────────────────────────────────────────


def _endpoint_obs(*, unique: bool = True) -> EndpointTupleObservationV2:
    return EndpointTupleObservationV2(
        physical_id="phys-1", binding_revision_id="pbr_left", binding_content_hash="h1",
        columns=("customer_id",), row_count=100, non_null_row_count=100,
        distinct_tuple_count=100 if unique else 60,
        duplicate_tuple_count=0 if unique else 20,
        duplicate_row_count=0 if unique else 40,
        max_rows_per_tuple=1 if unique else 3)


def _observation(*, coverage: RowCoverage = RowCoverage.FULL,
                 realization_revision_id: str = "rrv-1",
                 scope_id: str = "scope-1") -> RelationshipObservationV2:
    return RelationshipObservationV2(
        realization_revision_id=realization_revision_id,
        plan_hash="plan-1", scope_id=scope_id,
        left=_endpoint_obs(), right=_endpoint_obs(),
        matched_left_distinct=100, unmatched_left_distinct=0,
        matched_right_distinct=100, unmatched_right_distinct=0,
        left_orphan_rows=0, right_orphan_rows=0, joined_row_count=100,
        max_right_matches_per_left_row=1, max_left_matches_per_right_row=1,
        normalization_ids=(), predicate_ids=(),
        left_source_snapshot_id="snap-l", right_source_snapshot_id="snap-r",
        snapshot_or_as_of=None, execution_principal="svc-observer",
        method="exact", row_coverage=coverage, complete=True,
        observed_at=datetime(2026, 8, 1, 12, 0, tzinfo=UTC))


def test_observation_context_mirrors_the_v2_store_shape() -> None:
    ctx = sc.observation_context_from(
        _observation(), realization=_realization_context(), current=True)
    assert ctx.realization_revision_id == "rrv-1"
    assert ctx.scope_id == "scope-1"
    # Sides preserved, both directional maxima kept — no single `direction` field exists.
    assert ctx.left.binding_revision_id == "pbr_left"
    assert ctx.max_right_matches_per_left_row == 1
    assert ctx.max_left_matches_per_right_row == 1
    assert not hasattr(ctx, "direction")
    assert not hasattr(ctx, "evidence_basis")
    assert not hasattr(ctx, "lifecycle_status")
    assert ctx.method == "exact"
    assert ctx.row_coverage == "full"
    assert ctx.current is True
    assert ctx.left_uniqueness == "unique"


def test_sampled_observation_never_projects_uniqueness() -> None:
    """MUTATION (sample as uniqueness proof): the store's asymmetry rule is READ, not re-derived —
    a sampled observation whose endpoint LOOKS unique projects `unknown`, never `unique`."""
    ctx = sc.observation_context_from(
        _observation(coverage=RowCoverage.SAMPLED),
        realization=_realization_context(), current=True)
    assert ctx.row_coverage == "sampled"
    assert ctx.left_uniqueness == "unknown"
    assert ctx.right_uniqueness == "unknown"
    # A sample may still DISPROVE uniqueness.
    duplicated = dataclasses.replace(
        _observation(coverage=RowCoverage.SAMPLED), left=_endpoint_obs(unique=False))
    ctx = sc.observation_context_from(
        duplicated, realization=_realization_context(), current=True)
    assert ctx.left_uniqueness == "not_unique"


def test_observation_reused_across_realizations_or_scopes_dies() -> None:
    """MUTATION (reuse across bindings): an observation is applicable ONLY to its exact realization
    revision (which pins both binding revisions) and applicability scope."""
    with pytest.raises(sc.SemanticContextError):
        sc.observation_context_from(
            _observation(realization_revision_id="rrv-OTHER"),
            realization=_realization_context(), current=True)
    with pytest.raises(sc.SemanticContextError):
        sc.observation_context_from(
            _observation(scope_id="scope-OTHER"),
            realization=_realization_context(), current=True)
    with pytest.raises(sc.SemanticContextError):
        sc.observation_context_from(
            _observation(), realization=_realization_context(scope_id=None),
            current=True)


def _bundle_observation(revision) -> RelationshipObservationV2:
    return _observation(realization_revision_id=revision.realization_revision_id,
                        scope_id=revision.applicability_scope.scope_id)


def test_bundle_observations_without_a_currentness_pointer_die(db) -> None:
    """`ObservationContextV1.current` mirrors the `relationship_observation_current` pointer (D4),
    which this module cannot read yet — so it is SUPPLIED, never assumed. Observations without the
    pointer set are refused rather than stamped current."""
    _, revision = _stored_link_with_realization(db)
    with pytest.raises(sc.SemanticContextError):
        sc.bundle_from_store(db, "cib", "public.customers.customer_id", roles=(),
                             observations=(_bundle_observation(revision),))


def test_bundle_observation_currentness_comes_from_the_supplied_pointer(db) -> None:
    """MUTATION (currentness defaulted to True): the SAME observation projects `current` from the
    caller's pointer set alone. An EMPTY set is a legitimate answer — every supplied observation is
    superseded — and must NOT be confused with "no pointer supplied", which raises above.

    Without this, a superseded observation (measured against an older binding revision) would
    present itself to feature generation as the live measurement."""
    _, revision = _stored_link_with_realization(db)
    observation = _bundle_observation(revision)

    superseded = sc.bundle_from_store(
        db, "cib", "public.customers.customer_id", roles=(),
        observations=(observation,), current_observation_revision_ids=())
    (stale_ctx,) = superseded.observation_context
    assert stale_ctx.current is False

    live = sc.bundle_from_store(
        db, "cib", "public.customers.customer_id", roles=(),
        observations=(observation,),
        current_observation_revision_ids=(observation.observation_revision_id,))
    (live_ctx,) = live.observation_context
    assert live_ctx.current is True

    # Currentness is bundle CONTENT (D1): it re-keys the identity hash.
    assert superseded.content_hash != live.content_hash


# ── bundle_from_store: batching, scope, scalar-reader agreement ──────────────────────────────────


def test_bundle_from_store_matches_the_scalar_operational_reader(db) -> None:
    rows = _rows()
    _graph(db, rows, concepts={"cust_num": "customer_id"})
    bundle = sc.bundle_from_store(db, "cib", "public.bo_cib_customer.cust_num",
                                  roles=_ALL_ACCESS)
    by_field = {v.field_name: v for v in bundle.resolved_semantics}
    for field_name, reader_field in (("additivity", "additivity"), ("data_type",
                                     "logical_representation"), ("unit", "unit"),
                                     ("currency", "currency"), ("entity", "entity"),
                                     ("declared_type", "declared_type"),
                                     ("is_grain", "is_grain"), ("is_as_of", "is_as_of")):
        facts = read_column_facts(db, bundle.object_ref, reader_field)
        got = by_field.get(field_name)
        assert (got.value if got else None) == facts.value, field_name
        if got is not None:
            assert got.operational_influence == facts.authority, field_name
    assert by_field["concept"].value == "customer_id"
    assert bundle.concept_path == ("customer_id",)
    assert bundle.identifier_namespace is not None
    assert bundle.identifier_namespace.scheme == "cif"
    assert bundle.identifier_namespace.basis == "unresolved"
    assert "issuer_scope_unresolved" in bundle.missing_context


def test_bundle_from_store_query_count_is_column_count_independent(db) -> None:
    """The 157-scan defect class: the store builder is BATCHED — its query count must not grow
    with the number of columns or per-column fields."""
    _graph(db, _rows(table="narrow_t", n_extra=3))
    narrow = _CountingConn(db)
    sc.bundle_from_store(narrow, "cib", "public.narrow_t.cust_num", roles=_ALL_ACCESS)

    _graph(db, _rows(table="wide_t", n_extra=40))
    wide = _CountingConn(db)
    sc.bundle_from_store(wide, "cib", "public.wide_t.cust_num", roles=_ALL_ACCESS)
    assert wide.count == narrow.count, (
        f"query count grew with column count ({narrow.count} -> {wide.count}); "
        "a per-field/per-column reader loop is back")
    assert wide.count <= 30


def test_bundle_from_store_scopes_anchor_neighbours_and_links(db) -> None:
    rows = _rows()
    _graph(db, rows)
    # Restricted anchor: an unprivileged caller gets no bundle (and no existence oracle).
    with pytest.raises(LookupError):
        sc.bundle_from_store(db, "cib", "public.bo_cib_customer.cust_name", roles=())
    # Restricted neighbour: hidden from an unprivileged caller's roster.
    bundle = sc.bundle_from_store(db, "cib", "public.bo_cib_customer.cust_num", roles=())
    assert "cust_name" not in {n.column_name for n in bundle.neighbouring_columns}
    privileged = sc.bundle_from_store(db, "cib", "public.bo_cib_customer.cust_num",
                                      roles=_ALL_ACCESS)
    assert "cust_name" in {n.column_name for n in privileged.neighbouring_columns}


def test_bundle_from_store_hides_a_link_with_a_restricted_endpoint(db) -> None:
    left, right = _executable_pair()
    assessment = IdentifierLinkAssessmentV1(
        left_endpoint=left, right_endpoint=right,
        namespace_verdict=NamespaceVerdict.POSSIBLE,
        governed_population_relation=PopulationRelation.UNKNOWN,
        assessment_version="assessment-v1", bridge_fact_key="bridge-fact-1")
    record_candidate_assessment(db, assessment, expected_pointer_version=0)
    govern_bridge_fact(
        db, "bridge-fact-1", entity="customer", left_source="cib",
        left_ref="public.customers.customer_id", right_source="ftr",
        right_ref="public.transactions.customer_id", status="DRAFT")
    _graph(db, [CanonicalRow("cib", "customers", "customer_id", "text", is_grain=True)])
    # The FTR counterpart column is pii-restricted: the link must not leak it.
    build_graph(db, "ftr", [CanonicalRow("ftr", "transactions", "customer_id", "text",
                                         sensitivity="pii")])
    run_projection(db, OverlayProjection())     # catch the overlay checkpoint up to the head
    hidden = sc.bundle_from_store(db, "cib", "public.customers.customer_id", roles=())
    assert hidden.relationship_context == ()
    shown = sc.bundle_from_store(db, "cib", "public.customers.customer_id",
                                 roles=_ALL_ACCESS)
    assert len(shown.relationship_context) == 1


def test_bundle_from_store_fails_closed_on_a_degraded_projection(db) -> None:
    from featuregen.overlay.upload.feature_metadata_snapshot import CatalogProjectionUnavailable

    _graph(db, _rows())
    db.execute(
        "INSERT INTO projection_degraded "
        "(projection_name, aggregate, aggregate_id, reason, poison_seq) "
        "VALUES ('overlay', 'upload', 'u-1', 'poisoned', 1)")
    with pytest.raises(CatalogProjectionUnavailable):
        sc.bundle_from_store(db, "cib", "public.bo_cib_customer.cust_num",
                             roles=_ALL_ACCESS)


# ── builder byte-identity on shared fields (D1) ──────────────────────────────────────────────────


def _seed_store_side(db, rows, record) -> None:
    """Persist the SAME upload material the way ingest does: graph rows (schema-preserving) plus
    the glossary SOURCE evidence, written by the production writer."""
    schema = record.schema
    schemas = {f"public.{r.table}.{r.column}": schema for r in rows}
    schemas.update({f"public.{r.table}": schema for r in rows})
    _graph(db, rows, schemas=schemas)
    _write_glossary_source_evidence(
        db, logical_ref=record.logical_ref, rec=record, snapshot_id="snap-1")


def test_builders_byte_match_on_shared_fields_including_non_public_schema(db) -> None:
    """The public-flattening caveat (review C Task 1): identity is keyed by SCHEMA-PRESERVING
    logical refs on both sides, and equivalent shared facts serialize identically through the one
    canonicalizer."""
    rows = _rows()
    record = _record(schema="dpl_core")
    upload = sc.bundle_from_upload(rows[0], glossary_record=record, cohort=rows,
                                   roles=_ALL_ACCESS)
    _seed_store_side(db, rows, record)
    store = sc.bundle_from_store(db, "cib", "public.bo_cib_customer.cust_num",
                                 roles=_ALL_ACCESS)
    assert store.object_ref == "cib::dpl_core.bo_cib_customer.cust_num"
    up = sc.shared_identity_payload(upload)
    st = sc.shared_identity_payload(store)
    assert up == st
    assert materialize_hash(up) == materialize_hash(st)


# ── source vs resolved separation ────────────────────────────────────────────────────────────────


def test_source_and_resolved_semantics_stay_separate(db) -> None:
    rows = _rows()
    record = _record()
    _seed_store_side(db, rows, record)
    bundle = sc.bundle_from_store(db, "cib", "public.bo_cib_customer.cust_num",
                                  roles=_ALL_ACCESS)
    source_fields = {v.field_name for v in bundle.source_semantics}
    assert "business_term" in source_fields
    for value in bundle.source_semantics:
        assert value.resolution_status == "declared"
        assert all(ev.producer == "source" for ev in value.evidence)
    resolved = {v.field_name: v for v in bundle.resolved_semantics}
    assert resolved["definition"].value == "Customer number."
    assert resolved["definition"].resolution_status == "current"
    statuses = {v.resolution_status for v in (*bundle.source_semantics,
                                              *bundle.resolved_semantics)}
    assert statuses <= sc.RESOLUTION_STATUSES


# ── purpose adapters ─────────────────────────────────────────────────────────────────────────────


def test_purpose_adapters_are_bounded_plain_dicts(db) -> None:
    rows = _rows(n_extra=60)
    record = _record()
    _seed_store_side(db, rows, record)
    bundle = sc.bundle_from_store(db, "cib", "public.bo_cib_customer.cust_num",
                                  roles=_ALL_ACCESS)
    adapters = {
        "concept": sc.for_concept_enrichment(bundle),
        "critic": sc.for_critic(bundle),
        "summary": sc.for_summary(bundle),
        "feature": sc.for_feature_generation(bundle),
        "analysis": sc.for_analysis_planning(bundle),
    }
    for name, payload in adapters.items():
        assert isinstance(payload, dict), name
        for key, value in payload.items():
            assert isinstance(key, str), (name, key)
            assert not dataclasses.is_dataclass(value), (name, key)
            if isinstance(value, list):
                assert len(value) <= sc.ADAPTER_LIST_LIMIT, (name, key)
    # Existing-allowlist key names are reused where a matching key exists (D10).
    assert adapters["concept"]["column"] == "cust_num"
    assert adapters["concept"]["business_definition"] == "Customer number."
    assert adapters["concept"]["data_domain"] == "Party"
    feature = adapters["feature"]
    assert feature["object_ref"] == bundle.object_ref
    for fact_key in ("data_type", "declared_type", "entity", "additivity", "unit",
                     "currency", "is_grain", "is_as_of"):
        wrapper = feature[fact_key]
        assert set(wrapper) == {"value", "authority"}, fact_key
        assert wrapper["authority"] in ("governed", "hint"), fact_key
        assert wrapper["value"] is None or isinstance(wrapper["value"], str), fact_key
    assert feature["missing_context"] == list(bundle.missing_context)
    assert "concept_path" in feature
    assert "identifier_namespace" in adapters["analysis"]
