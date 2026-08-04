"""Release C Task 10 — a crosswalk is VISIBLE, distinctly, and never looks executable.

The rules this suite holds:

* a direct bridge and a crosswalk between the SAME two endpoints stay distinct records and BOTH
  appear — they answer different questions ("these ids are equal" / "these ids are related through
  this table") and collapsing them loses the difference;
* nothing on a crosswalk payload implies executability: no fabricated safety_status, no borrowed
  realization, `executable_now` always False;
* availability keeps the two-word `LinkAvailability` vocabulary; a crosswalk the caller may not see
  is withheld WHOLE, never listed as "unavailable";
* a human confirmation moves review status and nothing else.
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
from featuregen.overlay.upload.context_graph import build_context_section
from featuregen.overlay.upload.crosswalk import (
    CrosswalkDefinitionRevisionV1,
    LogicalMappingPairV1,
)
from featuregen.overlay.upload.crosswalk_store import (
    CROSSWALK_OMITTED_UNREADABLE,
    CrosswalkStoreConflict,
    load_crosswalk_definition_revision,
    publish_crosswalk_definition,
    set_crosswalk_review_status,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.semantic_context import RelationshipKind
from featuregen.projections.runner import run_projection

ACTOR = "user:priya"
ANCHOR = "public.customers.customer_id"
MAP_TABLE = "cust_xref"
MAP = normalize_ref("cib", "public", MAP_TABLE)
CIB_KEY = normalize_ref("cib", "public", "customers", "customer_id")
FTR_KEY = normalize_ref("ftr", "public", "transactions", "customer_id")


def _unbound(endpoint):
    """A crosswalk definition accepts LOGICAL, UNBOUND endpoints only — the same two endpoints the
    bridge realization pins physically, with the environment stripped out."""
    return dataclasses.replace(endpoint, physical_binding=None, binding_revision_id=None)


def _crosswalk_between(left, right, *, sensitivity: str = "") -> CrosswalkDefinitionRevisionV1:
    return CrosswalkDefinitionRevisionV1(
        source_endpoint=_unbound(left),
        mapping_dataset_ref=MAP,
        source_to_mapping_pairs=(LogicalMappingPairV1(
            CIB_KEY, normalize_ref("cib", "public", MAP_TABLE, "cust_num")),),
        mapping_to_target_pairs=(LogicalMappingPairV1(
            FTR_KEY, normalize_ref("cib", "public", MAP_TABLE, "tran_cust_id")),),
        target_endpoint=_unbound(right),
    )


def _bank(db, *, map_sensitivity: str = "") -> tuple:
    """One governed direct bridge AND one crosswalk between the SAME two endpoints."""
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
    record_realization_revision(
        db, revision,
        BridgeRealizationCurrentV1(
            revision.realization_id, revision.realization_revision_id,
            SafetyStatus.DETERMINISTICALLY_VALIDATED, LinkReviewStatus.UNREVIEWED,
            RealizationLifecycle.ACTIVE, 1),
        dependencies=(BridgeDependencyRefV1("bridge_fact", "bridge-fact-1", "head-1"),))
    # One build_graph per SOURCE rebuilds that source's nodes, so the anchor table and the mapping
    # table go in together.
    build_graph(db, "cib", [
        CanonicalRow("cib", "customers", "customer_id", "text", is_grain=True),
        CanonicalRow("cib", MAP_TABLE, "cust_num", "text", sensitivity=map_sensitivity),
        CanonicalRow("cib", MAP_TABLE, "tran_cust_id", "text", sensitivity=map_sensitivity),
    ])
    build_graph(db, "ftr", [CanonicalRow("ftr", "transactions", "customer_id", "text")])
    run_projection(db, OverlayProjection())
    crosswalk = _crosswalk_between(left, right)
    publish_crosswalk_definition(
        db, crosswalk, expected_pointer_version=0, actor=ACTOR,
        roles=("restricted_reader",) if map_sensitivity else ())
    return assessment, revision, crosswalk


# ── coexistence ─────────────────────────────────────────────────────────────────────────────────

def test_a_direct_bridge_and_a_crosswalk_between_the_same_endpoints_are_BOTH_visible(db) -> None:
    """The pinned rule. Two records, two refs, two kinds, both shown — because "these ids are
    equal" and "these ids are related through this table" are different claims and only one of
    them may turn out to be true."""
    _assessment, _revision, crosswalk = _bank(db)
    bundle = sc.bundle_from_store(db, "cib", ANCHOR, roles=())
    by_kind = {link.kind: link for link in bundle.relationship_context}
    assert set(by_kind) == {RelationshipKind.DIRECT_EQUALITY.value,
                            RelationshipKind.CROSSWALK.value}
    direct, cross = by_kind["direct_equality"], by_kind["crosswalk"]
    assert direct.relationship_ref == "bridge-fact-1"
    assert cross.relationship_ref == crosswalk.definition_id
    assert direct.relationship_ref != cross.relationship_ref
    assert {direct.left_ref, direct.right_ref} == {cross.left_ref, cross.right_ref}


def test_the_crosswalk_carries_its_mapping_dataset_and_both_legs(db) -> None:
    _assessment, _revision, crosswalk = _bank(db)
    bundle = sc.bundle_from_store(db, "cib", ANCHOR, roles=())
    cross = next(link for link in bundle.relationship_context if link.kind == "crosswalk")
    assert cross.crosswalk is not None
    assert cross.crosswalk.mapping_dataset_ref == MAP
    assert cross.crosswalk.definition_revision_id == crosswalk.revision_id
    assert cross.crosswalk.source_to_mapping_refs and cross.crosswalk.mapping_to_target_refs
    # EMPTY by contract: a leg is pinned by RESOLVING it, and nothing in this release resolves.
    assert cross.crosswalk.leg_pins == ()


def test_the_direct_link_keeps_its_realizations_and_the_crosswalk_has_none(db) -> None:
    _assessment, revision, _crosswalk = _bank(db)
    bundle = sc.bundle_from_store(db, "cib", ANCHOR, roles=())
    by_kind = {link.kind: link for link in bundle.relationship_context}
    assert [r.realization_revision_id for r in by_kind["direct_equality"].realizations] == [
        revision.realization_revision_id]
    assert by_kind["crosswalk"].realizations == ()


def _section(db, *, roles=()) -> dict:
    return build_context_section(
        db, source="cib", object_ref=ANCHOR, kind="column", logical_ref=CIB_KEY, roles=roles,
        now=datetime.now(UTC))


# ── nothing implies executability ───────────────────────────────────────────────────────────────

def test_the_context_graph_never_reports_a_crosswalk_as_executable(db) -> None:
    _bank(db)
    section = _section(db)
    cross = next(r for r in section["relationships"] if r["kind"] == "crosswalk")
    assert cross["executable_now"] is False
    assert cross["realizations"] == []
    assert cross["crosswalk"]["mapping_dataset_ref"] == MAP
    assert cross["crosswalk"]["leg_pins"] == []


def test_no_safety_verdict_is_fabricated_anywhere_on_a_crosswalk_payload(db) -> None:
    """A crosswalk has no measured safety on this tree. A `safety_status` key here would be a
    borrowed verdict, and a reader cannot tell a borrowed verdict from a real one."""
    _bank(db)
    section = _section(db)
    cross = next(r for r in section["relationships"] if r["kind"] == "crosswalk")
    assert "safety_status" not in cross
    assert "safety_status" not in cross["crosswalk"]
    assert not any(key in cross["crosswalk"]
                   for key in ("sandbox_eligible", "production_eligible", "execution_tier"))


def test_the_crosswalk_edge_is_drawn_as_its_own_kind(db) -> None:
    """A graph that draws a crosswalk with the identifier-link edge kind renders two different
    relationships identically."""
    _bank(db)
    section = _section(db)
    kinds = {edge["kind"] for edge in section["edges"]}
    assert {"identifier_link", "crosswalk_link"} <= kinds
    node = next(n for n in section["nodes"]
                if n["kind"] == "relationship" and n["detail"]["kind"] == "crosswalk")
    assert node["detail"]["mapping_dataset_ref"] == MAP


def test_the_why_string_names_the_mapping_table_and_claims_nothing(db) -> None:
    _bank(db)
    section = _section(db)
    why = next(e["why"] for e in section["edges"] if e["kind"] == "crosswalk_link")
    assert MAP in why
    assert "executable" not in why


# ── read scope and review ───────────────────────────────────────────────────────────────────────

def test_a_hidden_mapping_dataset_withholds_the_crosswalk_WHOLE(db) -> None:
    """Not "unavailable" — ABSENT. A row saying "there is something here you cannot see" is itself
    the disclosure the scope exists to prevent."""
    _bank(db, map_sensitivity="restricted")
    unprivileged = sc.bundle_from_store(db, "cib", ANCHOR, roles=())
    assert [link.kind for link in unprivileged.relationship_context] == ["direct_equality"]
    privileged = sc.bundle_from_store(db, "cib", ANCHOR, roles=("restricted_reader",))
    assert {link.kind for link in privileged.relationship_context} == {
        "direct_equality", "crosswalk"}


def test_confirming_a_crosswalk_changes_review_status_and_not_availability(db) -> None:
    _assessment, _revision, crosswalk = _bank(db)
    before = next(link for link in sc.bundle_from_store(db, "cib", ANCHOR, roles=()
                                                        ).relationship_context
                  if link.kind == "crosswalk")
    assert (before.review_status, before.availability, before.strength) == (
        "unreviewed", "available", "proposed")
    set_crosswalk_review_status(
        db, definition_id=crosswalk.definition_id,
        review_status=LinkReviewStatus.HUMAN_VERIFIED, expected_pointer_version=1,
        declared_by="user:sam")
    after = next(link for link in sc.bundle_from_store(db, "cib", ANCHOR, roles=()
                                                       ).relationship_context
                 if link.kind == "crosswalk")
    assert after.review_status == "human_verified"
    assert after.availability == before.availability == "available"
    assert after.strength == "confirmed"     # somebody is accountable; nothing else moved
    assert after.crosswalk == before.crosswalk


# ── one corrupt row is not a catalog-wide outage ────────────────────────────────────────────────

def _tamper(db) -> None:
    db.execute(
        "UPDATE crosswalk_definition_revision SET definition_json = jsonb_set("
        "    definition_json, '{mapping_dataset_ref}', '\"cib::public.somewhere_else\"')")


def test_one_corrupt_crosswalk_row_does_not_take_the_whole_dossier_with_it(db) -> None:
    """A crosswalk names THREE datasets, so a single tampered row raising through the listing takes
    out the dossier of every column of all three. The crosswalk is omitted and COUNTED instead —
    the omission has to be visible, or a corrupted record reads as a catalog that never had one."""
    _bank(db)
    _tamper(db)
    bundle = sc.bundle_from_store(db, "cib", ANCHOR, roles=())
    assert [link.kind for link in bundle.relationship_context] == ["direct_equality"]
    section = _section(db)
    assert section["status"] == "available"
    assert section["truncation"]["omitted"][CROSSWALK_OMITTED_UNREADABLE] == 1
    assert not [r for r in section["relationships"] if r["kind"] == "crosswalk"]


def test_a_column_of_the_mapping_dataset_still_gets_its_dossier(db) -> None:
    _bank(db)
    _tamper(db)
    bundle = sc.bundle_from_store(db, "cib", f"public.{MAP_TABLE}.cust_num", roles=())
    assert bundle.relationship_context == ()


def test_a_crosswalk_ref_never_reaches_the_bridge_reader(db, monkeypatch) -> None:
    """`_executable_realization_ids` SAYS no for a crosswalk rather than asking the bridge reader a
    question that happens to miss. Deleting that short-circuit survives every other test, because
    the reader's fail-closed `except` returns the same empty answer — so the guard has to be that
    the reader was never CONSULTED, not that the answer was right."""
    from featuregen.overlay.upload import bridge_store, context_graph

    consulted: list[object] = []

    def _poisoned(_conn, **kwargs):
        consulted.append(kwargs.get("bridge_fact_key"))
        raise AssertionError("the bridge reader was asked about a crosswalk")

    monkeypatch.setattr(bridge_store, "executable_bridge_realizations", _poisoned)
    assert context_graph._executable_realization_ids(db, "cwd_" + "a" * 64) == frozenset()
    assert consulted == []
    # And a BRIDGE ref does reach it — otherwise the guard could be "never ask anyone anything".
    assert context_graph._executable_realization_ids(db, "bfk_1") == frozenset()
    assert consulted == ["bfk_1"]


def test_the_point_read_of_the_corrupt_revision_still_refuses(db) -> None:
    """Omitting from a LISTING and refusing a POINT read are different answers to different
    questions: "show me what is here" versus "give me THIS crosswalk"."""
    _assessment, _revision, crosswalk = _bank(db)
    _tamper(db)
    with pytest.raises(CrosswalkStoreConflict):
        load_crosswalk_definition_revision(db, crosswalk.revision_id)


# ── contract guards ─────────────────────────────────────────────────────────────────────────────

def _relationship(**over) -> sc.RelationshipContextV1:
    base: dict = dict(
        relationship_ref="cwd_" + "a" * 64, kind=RelationshipKind.CROSSWALK.value,
        left_ref=CIB_KEY, right_ref=FTR_KEY, availability="available",
        review_status="unreviewed", assessment_revision_id=None, realizations=(),
        producer="taxonomy", strength="proposed", lifecycle="active", current=True,
        evidence_ids=(),
        crosswalk=sc.CrosswalkContextV1(
            definition_id="cwd_" + "a" * 64, definition_revision_id="cwd_" + "b" * 64,
            mapping_dataset_ref=MAP, source_to_mapping_refs=("cib::public.cust_xref.cust_num",),
            mapping_to_target_refs=("cib::public.cust_xref.tran_cust_id",)))
    base.update(over)
    return sc.RelationshipContextV1(**base)


def test_a_direct_link_may_not_carry_a_crosswalk_extension() -> None:
    with pytest.raises(sc.SemanticContextError):
        _relationship(kind=RelationshipKind.DIRECT_EQUALITY.value)


def test_a_crosswalk_without_its_extension_is_indistinguishable_from_a_direct_link() -> None:
    with pytest.raises(sc.SemanticContextError):
        _relationship(crosswalk=None)


def test_a_crosswalk_may_not_borrow_a_bridge_realization() -> None:
    """MUTATION: presenting a directional realization on a crosswalk would import the bridge
    family's safety verdicts for a relationship nothing has measured."""
    realization = sc.DirectionalRealizationContextV1(
        realization_revision_id="rr_1", from_ref="cib::public.customers",
        to_ref="ftr::public.transactions", lifecycle="active",
        safety_status="deterministically_validated", cardinality="N:1", scope_id="scope-1",
        sandbox_eligible=True, production_eligible=True)
    with pytest.raises(sc.SemanticContextError):
        _relationship(realizations=(realization,))


def test_a_crosswalk_context_must_name_both_legs() -> None:
    with pytest.raises(sc.SemanticContextError):
        sc.CrosswalkContextV1(
            definition_id="cwd_a", definition_revision_id="cwd_b", mapping_dataset_ref=MAP,
            source_to_mapping_refs=("cib::public.cust_xref.cust_num",),
            mapping_to_target_refs=())


# ── Release C Task 11: measurement + admission state on the read models ─────────────────────────

def test_an_unmeasured_crosswalk_reads_as_discoverable_unmeasured_not_as_a_failure(db):
    """The no-blocked rule, at the surface. `measurement=None` and no directions is a STATE."""
    from tests.featuregen.overlay.upload._crosswalk_fixtures import definition

    from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus
    from featuregen.overlay.upload.crosswalk_store import (
        CrosswalkDefinitionCurrentV1,
        CurrentCrosswalkV1,
    )
    from featuregen.overlay.upload.semantic_context import relationship_context_from_crosswalk

    revision = definition()
    current = CurrentCrosswalkV1(
        revision=revision,
        current=CrosswalkDefinitionCurrentV1(
            definition_id=revision.definition_id, revision_id=revision.revision_id,
            review_status=LinkReviewStatus.UNREVIEWED, pointer_version=1, declared_by="t"))
    link = relationship_context_from_crosswalk(current)
    assert link.crosswalk is not None
    assert link.crosswalk.measured is False
    assert link.crosswalk.measurement is None
    assert link.crosswalk.directions == ()
    assert link.availability == "available"          # discoverable, and it says so
    assert link.crosswalk.executable_now is False


def test_a_measured_crosswalk_shows_its_per_direction_verdicts_and_the_numbers_behind_them(db):
    from datetime import UTC, datetime

    from tests.featuregen.overlay.upload._crosswalk_fixtures import (
        CIB_TABLE,
        FTR_TABLE,
        MAP_TABLE,
        binding,
        definition,
        leg,
        mapping_observation,
    )

    from featuregen.overlay.upload.bridge_assessment import LinkReviewStatus
    from featuregen.overlay.upload.bridge_realization import (
        ExecutionTier,
        RealizationApplicabilityScopeV1,
    )
    from featuregen.overlay.upload.crosswalk_admission import (
        CrosswalkAdmissionPolicyV1,
        evaluate_crosswalk_admission,
    )
    from featuregen.overlay.upload.crosswalk_observation import (
        MAPPING_TO_TARGET,
        SOURCE_TO_MAPPING,
        SOURCE_TO_TARGET,
        TARGET_TO_SOURCE,
        compose_crosswalk_observation,
    )
    from featuregen.overlay.upload.crosswalk_store import (
        CrosswalkDefinitionCurrentV1,
        CurrentCrosswalkV1,
    )
    from featuregen.overlay.upload.semantic_context import relationship_context_from_crosswalk

    now = datetime(2026, 8, 4, 13, tzinfo=UTC)
    cib, ftr, mapping = (binding("cib", CIB_TABLE), binding("ftr", FTR_TABLE, schema="dpl_ftr"),
                         binding("cib", MAP_TABLE))
    scope = RealizationApplicabilityScopeV1(
        scope_id="crosswalk-sandbox-scope", execution_tier=ExecutionTier.SANDBOX,
        purposes=("crosswalk_probe",), environment="dev")
    revision = definition()
    observation = compose_crosswalk_observation(
        crosswalk_definition_revision_id=revision.revision_id,
        source_leg=leg(which=SOURCE_TO_MAPPING, endpoint_binding=cib, mapping_binding=mapping,
                       endpoint_column="acct_no", mapping_column="acct_no"),
        target_leg=leg(which=MAPPING_TO_TARGET, endpoint_binding=ftr, mapping_binding=mapping,
                       endpoint_column="counter_party_acct_no", mapping_column="ext_acct_ref",
                       cross_catalog=True),
        # A 1:1 forward with an N:1 reverse — the shape the surface must not flatten.
        mapping=mapping_observation(mapping, duplicate_source_tuple_count=1,
                                    max_rows_per_source_tuple=2, distinct_source_tuple_count=2),
        scope_id=scope.scope_id,
        matched_source_distinct=3, unmatched_source_distinct=0,
        matched_target_distinct=3, unmatched_target_distinct=0,
        composed_row_count=3, source_to_target_max_matches=1, target_to_source_max_matches=2,
        observed_at=now, mapping_temporal_policy_revision_id="dtp_" + "b" * 64,
        mapping_row_selection_hash="c" * 64)
    decision = evaluate_crosswalk_admission(
        crosswalk_definition_revision_id=revision.revision_id, scope=scope,
        policy=CrosswalkAdmissionPolicyV1(), now=now, observation=observation)
    current = CurrentCrosswalkV1(
        revision=revision,
        current=CrosswalkDefinitionCurrentV1(
            definition_id=revision.definition_id, revision_id=revision.revision_id,
            review_status=LinkReviewStatus.UNREVIEWED, pointer_version=1, declared_by="t"))

    link = relationship_context_from_crosswalk(
        current, observation=observation, decision=decision)
    extension = link.crosswalk
    assert extension is not None and extension.measured is True
    assert extension.measurement.observation_revision_id == observation.observation_revision_id
    assert extension.measurement.source_to_target_max_matches == 1
    assert extension.measurement.target_to_source_max_matches == 2
    # BOTH directions, with DIFFERENT verdicts. One of them alone would read as a verdict about the
    # pair, which is what the whole two-direction record exists to prevent.
    assert {d.direction for d in extension.directions} == {SOURCE_TO_TARGET, TARGET_TO_SOURCE}
    forward = next(d for d in extension.directions if d.direction == SOURCE_TO_TARGET)
    reverse = next(d for d in extension.directions if d.direction == TARGET_TO_SOURCE)
    assert forward.production_admissible is True
    assert reverse.production_admissible is False and reverse.sandbox_admissible is False
    assert extension.admission_policy_version == decision.policy_version
    # AND STILL NOT RUNNABLE. Task 12 wires execution; no verdict substitutes for it.
    assert extension.executable_now is False


def test_a_direction_cannot_claim_production_without_deterministic_validation():
    import pytest as _pytest

    from featuregen.overlay.upload.semantic_context import (
        CrosswalkDirectionContextV1,
        SemanticContextError,
    )

    with _pytest.raises(SemanticContextError, match="deterministic validation"):
        CrosswalkDirectionContextV1(
            direction="source_to_target", safety_status="unassessed", cardinality=None,
            sandbox_admissible=True, production_admissible=True)


def test_a_crosswalk_carries_both_directions_or_neither():
    import pytest as _pytest

    from featuregen.overlay.upload.semantic_context import (
        CrosswalkContextV1,
        CrosswalkDirectionContextV1,
        SemanticContextError,
    )

    one = CrosswalkDirectionContextV1(
        direction="source_to_target", safety_status="unassessed", cardinality=None,
        sandbox_admissible=True, production_admissible=False)
    with _pytest.raises(SemanticContextError, match="BOTH named directions"):
        CrosswalkContextV1(
            definition_id="cwd_1", definition_revision_id="cwd_2",
            mapping_dataset_ref="cib::public.m", source_to_mapping_refs=("a",),
            mapping_to_target_refs=("b",), directions=(one,))
