"""Release C Task 13 — the src/ assembler that builds an ``AdmittedCrosswalkV1`` from STORED rows.

DEFERRED-WORK A.42's fourth row said the two-leg path was COMPILED capability and not WIRED
capability, because every bundle in the repository came out of a fixture bank. These tests drive
the real stores: a published definition (migration 1050), a recorded composed measurement
(migration 1057) and three recorded physical bindings (migration 1036), assembled by
``crosswalk_assembly`` so ``AdmittedCrosswalkV1.__post_init__``'s cross-checks run on real rows.

THE TWO PRODUCT RULES THIS SUITE OWNS, each written so a wrong implementation fails:

* DoD 16 — an UNREVIEWED, unmeasured crosswalk is sandbox-plannable. Approving it changes nothing
  and withholding approval withholds nothing.
* DoD 17 — production admissibility is the direction's own measured verdict. A ``human_verified``
  review does not create it and its absence does not remove it.
"""
from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta

import pytest
from tests.featuregen.overlay.upload._crosswalk_fixtures import (
    CIB,
    CIB_TABLE,
    FTR,
    FTR_TABLE,
    MAP,
    MAP_TABLE,
    binding,
    build_catalog,
    definition,
    leg,
    mapping_observation,
    record_bindings,
)

from featuregen.overlay.upload.bridge_assessment import (
    BridgeContractError,
    ConceptAuthority,
    IdentifierColumnMemberV1,
    IdentifierEndpointV1,
    KeyMemberRole,
    LinkReviewStatus,
    TupleKeyRole,
    TypeBasis,
)
from featuregen.overlay.upload.bridge_realization import (
    BridgeJoinRealizationRevisionV1,
    BridgeRealizationCurrentV1,
    CardinalityBasis,
    ColumnPairV1,
    DirectionalCardinalityVerdictV1,
    ExecutionTier,
    RealizationApplicabilityScopeV1,
    RealizationLifecycle,
    SafetyStatus,
)
from featuregen.overlay.upload.bridge_store import (
    BridgeDependencyRefV1,
    demote_realization_revision,
    record_realization_revision,
)
from featuregen.overlay.upload.crosswalk import JoinLegKind, JoinLegPinV1
from featuregen.overlay.upload.crosswalk_admission import (
    AdmittedCrosswalkV1,
    CrosswalkAdmissionReason,
)
from featuregen.overlay.upload.crosswalk_assembly import (
    CrosswalkAssemblyReason,
    CrosswalkAssemblyRefusalV1,
    active_mapping_count,
    assemble_admitted_crosswalk,
    load_binding_revision,
    production_admissible,
    sandbox_plannable,
)
from featuregen.overlay.upload.crosswalk_observation import (
    MAPPING_TO_TARGET,
    SCOPE_BINDING_SEPARATOR,
    SOURCE_TO_MAPPING,
    SOURCE_TO_TARGET,
    TARGET_TO_SOURCE,
    compose_crosswalk_observation,
    scope_binding_id,
)
from featuregen.overlay.upload.crosswalk_observation_store import (
    crosswalk_observation_history,
    current_crosswalk_observations_for_revision,
    record_crosswalk_observation,
)
from featuregen.overlay.upload.crosswalk_store import (
    publish_crosswalk_definition,
    set_crosswalk_review_status,
)
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.taxonomy.entity_relationships import Cardinality
from featuregen.overlay.upload.temporal_policy import (
    DatasetRowSelectionV1,
    TemporalSelectionKind,
)

NOW = datetime(2026, 8, 5, 10, tzinfo=UTC)
ROLES = ("platform-admin",)
ACTOR = "user:priya"
POLICY_REVISION = "dtp_" + "9" * 64

SANDBOX_SCOPE = RealizationApplicabilityScopeV1(
    scope_id="crosswalk-sandbox-scope", execution_tier=ExecutionTier.SANDBOX,
    purposes=("crosswalk_probe",), environment="dev")
PRODUCTION_SCOPE = RealizationApplicabilityScopeV1(
    scope_id="crosswalk-production-scope", execution_tier=ExecutionTier.PRODUCTION,
    purposes=("feature_materialization",), environment="prod")


def _bound_endpoint(source: str, table: str, column: str, bind, *, concept: str):
    """An EXECUTABLE endpoint — a realization refuses one with no resolved binding revision."""
    return IdentifierEndpointV1(
        logical_table_ref=normalize_ref(source, "public", table),
        members=(IdentifierColumnMemberV1(
            normalize_ref(source, "public", table, column), "text", TypeBasis.DECLARED,
            KeyMemberRole.PRIMARY),),
        entity_id="account", concept=concept,
        concept_authority=ConceptAuthority.DETERMINISTIC,
        tuple_key_role=TupleKeyRole.COMPLETE_UNIQUE_KEY,
        physical_binding=bind, binding_revision_id=bind.binding_revision_id)


def seed_leg_realization(db, mapping, ftr, *, safety=SafetyStatus.DETERMINISTICALLY_VALIDATED):
    """The governed bridge realization the CROSS-catalog leg (mapping -> target) resolves through.

    Real, not stubbed: ``JoinLegPinV1`` refuses a cross-catalog pin that carries no realization
    revision, and ``crosswalk_assembly`` then revalidates that revision against the CURRENT
    pointer. A bank that skipped this would be testing an assembler nobody could call.
    """
    revision = BridgeJoinRealizationRevisionV1(
        bridge_fact_key="ebf-cwx-leg2",
        from_endpoint=_bound_endpoint("cib", MAP_TABLE, "ext_acct_ref", mapping,
                                      concept="external_account_ref"),
        to_endpoint=_bound_endpoint("ftr", FTR_TABLE, "counter_party_acct_no", ftr,
                                    concept="external_account_ref"),
        column_pairs=(ColumnPairV1(
            normalize_ref("cib", "public", MAP_TABLE, "ext_acct_ref"),
            normalize_ref("ftr", "public", FTR_TABLE, "counter_party_acct_no")),),
        predicates=(),
        applicability_scope=PRODUCTION_SCOPE,
        cardinality=DirectionalCardinalityVerdictV1(Cardinality.MANY_TO_ONE),
        cardinality_basis=CardinalityBasis.GOVERNED_KEY,
        evidence_refs=(),
        dependency_snapshot_id="dps-cwx-1",
        derivation_version="derivation-v1",
        admission_policy_version="bridge-admission-v1")
    record_realization_revision(
        db, revision,
        BridgeRealizationCurrentV1(
            revision.realization_id, revision.realization_revision_id,
            safety, LinkReviewStatus.UNREVIEWED, RealizationLifecycle.ACTIVE, 1),
        dependencies=(BridgeDependencyRefV1("bridge_fact", "ebf-cwx-leg2", "head-1"),))
    return revision


@pytest.fixture
def bank(db):
    """One catalog, three RECORDED bindings, one PUBLISHED crosswalk, one governed leg realization."""
    build_catalog(db)
    cib = binding("cib", CIB_TABLE)
    ftr = binding("ftr", FTR_TABLE, schema="dpl_ftr")
    mapping = binding("cib", MAP_TABLE)
    record_bindings(db, cib, ftr, mapping)
    revision = definition()
    publish_crosswalk_definition(
        db, revision, expected_pointer_version=0, actor=ACTOR, roles=ROLES)
    realization = seed_leg_realization(db, mapping, ftr)
    return db, revision, cib, ftr, mapping, realization


def pins(cib, ftr, mapping, realization) -> tuple[JoinLegPinV1, JoinLegPinV1]:
    """Two pins in the shapes their real owners produce: one same-catalog, one cross-catalog.

    The same-catalog leg pins NO fact key and NO realization revision — ``JoinLegPinV1`` refuses
    that pretence structurally — and the cross-catalog leg pins the governed realization the bank
    seeded, which is what ``_legs_currently_executable`` revalidates.
    """
    source = JoinLegPinV1(
        kind=JoinLegKind.SAME_CATALOG, plan_hash="plan-" + "1" * 16,
        from_dataset_ref=CIB, to_dataset_ref=MAP,
        from_binding_revision_id=cib.binding_revision_id,
        to_binding_revision_id=mapping.binding_revision_id,
        read_set_hash="read-" + "1" * 16)
    target = JoinLegPinV1(
        kind=JoinLegKind.CROSS_CATALOG, plan_hash="plan-" + "2" * 16,
        from_dataset_ref=MAP, to_dataset_ref=FTR,
        from_binding_revision_id=mapping.binding_revision_id,
        to_binding_revision_id=ftr.binding_revision_id,
        read_set_hash="read-" + "2" * 16,
        fact_keys=("ebf-cwx-leg2",),
        realization_revision_ids=(realization.realization_revision_id,),
        dependency_snapshot_ids=(realization.dependency_snapshot_id,))
    return source, target


def row_selection(**over) -> DatasetRowSelectionV1:
    kwargs = dict(
        dataset_logical_ref=MAP, dataset_profile_hash="dph-" + "c" * 16,
        temporal_policy_revision_id=POLICY_REVISION,
        selection_kind=TemporalSelectionKind.CURRENT_RECORD)
    kwargs.update(over)
    return DatasetRowSelectionV1(**kwargs)


def measure(revision, cib, ftr, mapping, *, scope=None, scope_id: str | None = None,
            selection=None, as_of: str = "2026-08-04", **over):
    """A CLEAN composed measurement — 1:1 forward, fanning 2:1 in reverse (the ordinary shape).

    ``scope`` records a SCOPE-BOUND id (``crosswalk_observation.scope_binding_id``), which is what a
    measurement that can prove which scope it ran under looks like. ``scope_id`` records a bare
    string — every 1057 row written before the binding existed, and the shape the assembler refuses
    to read as production evidence. Exactly one of the two is supplied, so no test is ever vague
    about which kind of evidence it is standing on.
    """
    if (scope is None) == (scope_id is None):
        raise AssertionError("measure() takes EITHER a bound scope OR a bare scope_id")
    scope_id = scope_id if scope is None else scope_binding_id(scope)
    source_leg = leg(which=SOURCE_TO_MAPPING, endpoint_binding=cib, mapping_binding=mapping,
                     endpoint_column="acct_no", mapping_column="acct_no", as_of=as_of)
    target_leg = leg(which=MAPPING_TO_TARGET, endpoint_binding=ftr, mapping_binding=mapping,
                     endpoint_column="counter_party_acct_no", mapping_column="ext_acct_ref",
                     cross_catalog=True, as_of=as_of)
    kwargs = dict(
        crosswalk_definition_revision_id=revision.revision_id,
        source_leg=source_leg, target_leg=target_leg,
        mapping=mapping_observation(mapping),
        scope_id=scope_id,
        matched_source_distinct=3, unmatched_source_distinct=1,
        matched_target_distinct=3, unmatched_target_distinct=0,
        composed_row_count=3,
        source_to_target_max_matches=1, target_to_source_max_matches=1,
        observed_at=NOW,
        mapping_temporal_policy_revision_id=POLICY_REVISION,
        mapping_row_selection_hash=(
            None if selection is None else selection.content_hash))
    kwargs.update(over)
    return compose_crosswalk_observation(**kwargs)


def assemble(db, revision, cib, ftr, mapping, realization, *, scope=SANDBOX_SCOPE, selection=None, **over):
    source_pin, target_pin = pins(cib, ftr, mapping, realization)
    kwargs = dict(
        definition_id=revision.definition_id, scope=scope, now=NOW,
        source_leg=source_pin, target_leg=target_pin, mapping_row_selection=selection)
    kwargs.update(over)
    return assemble_admitted_crosswalk(db, **kwargs)


# ── the bundle assembles from real rows ─────────────────────────────────────────────────────────

def test_a_stored_crosswalk_assembles_into_the_bundle_a_compilation_plans_on(bank):
    """A.44's closure (numbered A.42 before the 2026-08-05 main merge renumbering): definition + measurement + decision + selection + three bindings, from rows."""
    db, revision, cib, ftr, mapping, realization = bank
    selection = row_selection()
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping,
                    scope=SANDBOX_SCOPE, selection=selection))

    admitted = assemble(db, revision, cib, ftr, mapping, realization, selection=selection)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    # Every part came from the store, and `__post_init__` cross-checked them against each other.
    assert admitted.definition.revision_id == revision.revision_id
    assert admitted.observation is not None
    assert admitted.execution.composition_observation_revision_id == (
        admitted.observation.observation_revision_id)
    assert admitted.mapping_row_selection == selection
    assert admitted.mapping_binding.binding_revision_id == mapping.binding_revision_id
    assert admitted.source_binding.binding_revision_id == cib.binding_revision_id
    assert admitted.target_binding.binding_revision_id == ftr.binding_revision_id


def test_the_two_directions_are_admitted_independently_from_the_measured_shape(bank):
    """1:1 forward, 2:1 reverse — one usable direction and one refused, from ONE measurement."""
    db, revision, cib, ftr, mapping, realization = bank
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=PRODUCTION_SCOPE,
                    target_to_source_max_matches=2))

    admitted = assemble(db, revision, cib, ftr, mapping, realization, scope=PRODUCTION_SCOPE)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    forward = admitted.verdict_for(SOURCE_TO_TARGET)
    reverse = admitted.verdict_for(TARGET_TO_SOURCE)
    assert forward.safety_status is SafetyStatus.DETERMINISTICALLY_VALIDATED
    assert reverse.safety_status is SafetyStatus.UNSAFE
    assert production_admissible(admitted, direction=SOURCE_TO_TARGET) is True
    assert production_admissible(admitted, direction=TARGET_TO_SOURCE) is False
    assert CrosswalkAdmissionReason.DIRECTIONAL_FANOUT.value in reverse.reason_codes


def test_an_unmeasured_crosswalk_assembles_and_is_sandbox_plannable(bank):
    """"Discoverable, unmeasured" is a STATE. Nothing about it is a refusal or a blocker."""
    db, revision, cib, ftr, mapping, realization = bank

    admitted = assemble(db, revision, cib, ftr, mapping, realization)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    assert admitted.observation is None
    assert admitted.decision.measured is False
    assert sandbox_plannable(admitted, direction=SOURCE_TO_TARGET) is True
    assert sandbox_plannable(admitted, direction=TARGET_TO_SOURCE) is True
    assert production_admissible(admitted, direction=SOURCE_TO_TARGET) is False
    assert CrosswalkAdmissionReason.NOT_MEASURED.value in admitted.decision.reason_codes


# ── DoD 16 / DoD 17: review is not permission, in BOTH directions ───────────────────────────────

def test_an_llm_only_unreviewed_crosswalk_is_sandbox_plannable_without_approval(bank):
    """DoD 16. Nobody has reviewed it and the sandbox may still plan through it."""
    db, revision, cib, ftr, mapping, realization = bank
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=SANDBOX_SCOPE))

    admitted = assemble(db, revision, cib, ftr, mapping, realization)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    stored = db.execute(
        "SELECT review_status FROM crosswalk_definition_current WHERE definition_id = %s",
        (revision.definition_id,)).fetchone()
    assert stored[0] == LinkReviewStatus.UNREVIEWED.value, "the fixture must be unreviewed"
    assert sandbox_plannable(admitted, direction=SOURCE_TO_TARGET) is True


def test_confirming_a_crosswalk_changes_no_admission_answer_at_all(bank):
    """DoD 17, the direction that matters: a review does not CREATE production admissibility.

    Fails against the obvious wrong implementation — one that ORs `review_status` into the verdict —
    because the byte-identical decision is asserted, not merely a boolean.
    """
    db, revision, cib, ftr, mapping, realization = bank
    # Fanning in reverse and 1:1 forward, so there IS a refused direction for a review to "fix".
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=PRODUCTION_SCOPE,
                    target_to_source_max_matches=2))
    before = assemble(db, revision, cib, ftr, mapping, realization, scope=PRODUCTION_SCOPE)

    set_crosswalk_review_status(
        db, definition_id=revision.definition_id,
        review_status=LinkReviewStatus.HUMAN_VERIFIED, expected_pointer_version=1,
        declared_by=ACTOR)

    after = assemble(db, revision, cib, ftr, mapping, realization, scope=PRODUCTION_SCOPE)
    assert isinstance(before, AdmittedCrosswalkV1) and isinstance(after, AdmittedCrosswalkV1)
    assert after.decision == before.decision, (
        "a human review moved a deterministic admission decision: approval became permission")
    assert production_admissible(after, direction=TARGET_TO_SOURCE) is False, (
        "a reviewer confirmed a crosswalk and a measured fan-out became executable")
    assert after.execution.execution_revision_id == before.execution.execution_revision_id


def test_withholding_review_withholds_no_capability(bank):
    """The mirror: an unreviewed crosswalk with clean evidence IS production-admissible."""
    db, revision, cib, ftr, mapping, realization = bank
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=PRODUCTION_SCOPE))

    admitted = assemble(db, revision, cib, ftr, mapping, realization, scope=PRODUCTION_SCOPE)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    assert production_admissible(admitted, direction=SOURCE_TO_TARGET) is True


def test_a_sandbox_scoped_measurement_is_never_production_admissible(bank):
    """THE NAMED INVARIANT, tested at last: ONE scope_id string, TWO tiers.

    The earlier version of this test passed for a reason that had nothing to do with tiers — the
    sandbox and production fixtures happened to carry different ``scope_id`` strings, so the
    production assembly found no measurement at all. Migration 1057 persists the scope's NAME and
    not its tier, so the real question is what happens when the names AGREE and the tiers do not:
    the review's probe recorded under ``scope_id="probe-scope"`` at SANDBOX, read it back under a
    PRODUCTION scope of the same name, and got a clean production-admissible bundle.

    Both spellings of the recorded scope are checked, in that order, because they fail in different
    places and only the second is the probe verbatim:

    * a scope-BOUND id does not match the production scope at all, because the tier is inside the
      binding — a sandbox measurement and a production one are two different questions and the
      store now says so, which leaves the production read honestly UNMEASURED;
    * a BARE id (every 1057 row written before the binding existed) still matches by NAME, and is
      refused as production evidence because nothing in the store can prove which tier it ran at.
    """
    db, revision, cib, ftr, mapping, realization = bank
    shared_name = "probe-scope"
    sandbox = RealizationApplicabilityScopeV1(
        scope_id=shared_name, execution_tier=ExecutionTier.SANDBOX,
        purposes=("crosswalk_probe",), environment="dev")
    production = RealizationApplicabilityScopeV1(
        scope_id=shared_name, execution_tier=ExecutionTier.PRODUCTION,
        purposes=("crosswalk_probe",), environment="dev")

    # ── the bound shape: the tier is INSIDE the recorded id ─────────────────────────────────────
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=sandbox))

    at_sandbox = assemble(db, revision, cib, ftr, mapping, realization, scope=sandbox)
    assert isinstance(at_sandbox, AdmittedCrosswalkV1), (
        "the measurement is real and its own tier may read it — otherwise everything below is "
        "just a broken fixture")
    assert at_sandbox.verdict_for(SOURCE_TO_TARGET).production_admissible is True, (
        "the measurement's NUMBERS are clean; the SCOPE is what production cannot accept")

    bound = assemble(db, revision, cib, ftr, mapping, realization, scope=production)
    assert isinstance(bound, AdmittedCrosswalkV1)
    assert bound.observation is None, (
        "a SANDBOX measurement answered a production question because the two scopes share a name")
    assert production_admissible(bound, direction=SOURCE_TO_TARGET) is False
    assert CrosswalkAdmissionReason.NOT_MEASURED.value in bound.decision.reason_codes

    # ── the bare, unprovable shape: the review's probe verbatim ─────────────────────────────────
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope_id=shared_name, as_of="2026-08-03"))

    at_production = assemble(db, revision, cib, ftr, mapping, realization, scope=production)
    assert isinstance(at_production, CrosswalkAssemblyRefusalV1), (
        "a sandbox measurement was served as production evidence because the two scopes share a "
        "name and nothing persists the tier")
    assert at_production.code is CrosswalkAssemblyReason.OBSERVATION_SCOPE_IDENTITY_UNPROVED


def test_a_scope_bound_production_measurement_is_the_one_shape_production_reads(bank):
    """The mirror of the refusal above: proving the scope is what makes production evidence real.

    Without this, "refuse the unprovable" would be indistinguishable from "refuse everything", and
    every DoD-17 assertion in this file would be passing for the wrong reason.
    """
    db, revision, cib, ftr, mapping, realization = bank
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=PRODUCTION_SCOPE))

    admitted = assemble(db, revision, cib, ftr, mapping, realization, scope=PRODUCTION_SCOPE)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    assert admitted.observation is not None
    assert admitted.observation.scope_id == scope_binding_id(PRODUCTION_SCOPE)
    assert production_admissible(admitted, direction=SOURCE_TO_TARGET) is True


def test_a_sandbox_tier_reads_a_bare_measurement_because_that_is_the_question_asked(bank):
    """A bare id is not a defect at the sandbox tier — refusing it there would delete real evidence.

    The rule is asymmetric on purpose: production must PROVE the scope its verdict rests on, and a
    probe reading its own probe's numbers has nothing to prove to anybody.
    """
    db, revision, cib, ftr, mapping, realization = bank
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope_id=SANDBOX_SCOPE.scope_id))

    admitted = assemble(db, revision, cib, ftr, mapping, realization, scope=SANDBOX_SCOPE)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    assert admitted.observation is not None
    assert sandbox_plannable(admitted, direction=SOURCE_TO_TARGET) is True


def test_a_scope_may_not_be_NAMED_as_though_it_were_bound(bank):
    """Review ATTACK-2: the forgery that needs no hash — just a scope with the right name.

    A measurement declares the whole scope it ran under by recording `<scope_id>#scope:<digest>`,
    and a reader tells "declared" from "named only" by that separator. So a scope legitimately
    CALLED `crosswalk-production-scope#scope:ec94a641e86b4053` breaks the distinction from the other
    end: a BARE measurement under that scope records exactly the string the real production scope's
    binding produces, and `observation_scope_is_proved` answers True for a measurement that bound
    nothing. The double-bound `name#scope:h1#scope:h2` shape is the same defect with one more hat.

    Refused where a scope is first SPELLED, so no reader downstream has to distrust its own
    separator — and refused again in `scope_binding_id`, which is duck-typed and therefore reachable
    without ever passing through the dataclass.
    """
    db, revision, cib, ftr, mapping, realization = bank
    forged_name = scope_binding_id(PRODUCTION_SCOPE)
    assert SCOPE_BINDING_SEPARATOR in forged_name, "the fixture must carry a real bound id"

    with pytest.raises(BridgeContractError, match=re.escape(SCOPE_BINDING_SEPARATOR)):
        RealizationApplicabilityScopeV1(
            scope_id=forged_name, execution_tier=ExecutionTier.SANDBOX,
            purposes=("crosswalk_probe",), environment="dev")

    # And the minter refuses the same name, for a caller that never went through the dataclass.
    class _DuckTypedScope:
        scope_id = forged_name

        def identity_payload(self):
            return {"scope_id": self.scope_id, "execution_tier": "sandbox"}

    with pytest.raises(ValueError, match=re.escape(SCOPE_BINDING_SEPARATOR)):
        scope_binding_id(_DuckTypedScope())

    # The real production scope still binds, so the refusal is about the NAME and not the mechanism.
    assert scope_binding_id(PRODUCTION_SCOPE) == forged_name


def test_two_current_measurements_of_one_scope_refuse_rather_than_take_the_newest(bank):
    """`refuse_on_multiple`, applied to EVIDENCE (§6.6) — the discipline mapping definitions keep.

    Two current rows can share a scope: migration 1057 keys the pointer on `current_scope_key`,
    which carries the as-of instant and the principal as well as the scope, so re-measuring the same
    scope at a different instant advances a DIFFERENT pointer. Sorting them by `observed_at` and
    taking the first makes a safety verdict depend on a tie-break.
    """
    db, revision, cib, ftr, mapping, realization = bank
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=SANDBOX_SCOPE, as_of="2026-08-04"))
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=SANDBOX_SCOPE, as_of="2026-08-05",
                    observed_at=NOW + timedelta(days=1), composed_row_count=99))

    current = current_crosswalk_observations_for_revision(db, revision.revision_id)
    assert len({item.scope_id for item in current}) == 1 and len(current) == 2, (
        "the fixture must produce TWO current rows under ONE scope for this test to mean anything")

    refusal = assemble(db, revision, cib, ftr, mapping, realization, scope=SANDBOX_SCOPE)

    assert isinstance(refusal, CrosswalkAssemblyRefusalV1)
    assert refusal.code is CrosswalkAssemblyReason.MULTIPLE_OBSERVATIONS_FOR_SCOPE


# ── duplicates refuse, they are never deduplicated ──────────────────────────────────────────────

def test_two_active_mappings_for_one_endpoint_pair_refuse_rather_than_pick_one(bank):
    """§6.6 `refuse_on_multiple`. No DISTINCT, no LIMIT 1, no newest-wins."""
    db, revision, cib, ftr, mapping, realization = bank
    second_map = normalize_ref("cib", "public", "acct_xref_v2")
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, schema_name, "
        "  column_name, sensitivity) "
        "SELECT catalog_source, replace(object_ref, %s, 'acct_xref_v2'), kind, "
        "  'acct_xref_v2', schema_name, column_name, sensitivity "
        "FROM graph_node WHERE catalog_source = 'cib' AND table_name = %s",
        (MAP_TABLE, MAP_TABLE))
    rival = definition(
        mapping_dataset_ref=second_map,
        source_to_mapping_pairs=(_pair(f"{second_map}.acct_no", which="source"),),
        mapping_to_target_pairs=(_pair(f"{second_map}.ext_acct_ref", which="target"),))
    publish_crosswalk_definition(
        db, rival, expected_pointer_version=0, actor=ACTOR, roles=ROLES)

    assert active_mapping_count(db, revision) == 2
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=PRODUCTION_SCOPE))
    admitted = assemble(db, revision, cib, ftr, mapping, realization, scope=PRODUCTION_SCOPE)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    assert (CrosswalkAdmissionReason.MULTIPLE_ACTIVE_MAPPINGS.value
            in admitted.decision.reason_codes)
    assert production_admissible(admitted, direction=SOURCE_TO_TARGET) is False
    assert production_admissible(admitted, direction=TARGET_TO_SOURCE) is False


def _pair(mapping_member_ref: str, *, which: str):
    from featuregen.overlay.upload.crosswalk import LogicalMappingPairV1

    endpoint_ref = (normalize_ref("cib", "public", CIB_TABLE, "acct_no") if which == "source"
                    else normalize_ref("ftr", "public", FTR_TABLE, "counter_party_acct_no"))
    return LogicalMappingPairV1(endpoint_ref, mapping_member_ref)


# ── assembly refusals are VALUES, and they name what to fix ─────────────────────────────────────

def test_an_unknown_definition_is_a_typed_refusal_not_an_exception(bank):
    db, revision, cib, ftr, mapping, realization = bank
    source_pin, target_pin = pins(cib, ftr, mapping, realization)

    refusal = assemble_admitted_crosswalk(
        db, definition_id="cwd_" + "0" * 64, scope=SANDBOX_SCOPE, now=NOW,
        source_leg=source_pin, target_leg=target_pin)

    assert isinstance(refusal, CrosswalkAssemblyRefusalV1)
    assert refusal.code is CrosswalkAssemblyReason.DEFINITION_NOT_FOUND


def test_two_legs_through_two_mapping_tables_are_refused_as_not_one_crosswalk(bank):
    db, revision, cib, ftr, mapping, realization = bank
    source_pin, target_pin = pins(cib, ftr, mapping, realization)
    # The target leg departs from a DIFFERENT table than the source leg arrived at.
    diverged = JoinLegPinV1(
        kind=target_pin.kind, plan_hash=target_pin.plan_hash,
        from_dataset_ref=target_pin.from_dataset_ref, to_dataset_ref=target_pin.to_dataset_ref,
        from_binding_revision_id=cib.binding_revision_id,
        to_binding_revision_id=target_pin.to_binding_revision_id,
        read_set_hash=target_pin.read_set_hash,
        fact_keys=target_pin.fact_keys,
        realization_revision_ids=target_pin.realization_revision_ids,
        dependency_snapshot_ids=target_pin.dependency_snapshot_ids)

    refusal = assemble_admitted_crosswalk(
        db, definition_id=revision.definition_id, scope=SANDBOX_SCOPE, now=NOW,
        source_leg=source_pin, target_leg=diverged)

    assert isinstance(refusal, CrosswalkAssemblyRefusalV1)
    assert refusal.code is CrosswalkAssemblyReason.LEG_PINS_DISAGREE_ON_MAPPING


def test_a_pin_naming_an_unstored_binding_refuses_rather_than_assembling_blind(bank):
    db, revision, cib, ftr, mapping, realization = bank
    unknown = binding("cib", CIB_TABLE, schema="somewhere_else")   # never recorded
    source_pin, target_pin = pins(cib, ftr, mapping, realization)
    fabricated = JoinLegPinV1(
        kind=source_pin.kind, plan_hash=source_pin.plan_hash,
        from_dataset_ref=source_pin.from_dataset_ref, to_dataset_ref=source_pin.to_dataset_ref,
        from_binding_revision_id=unknown.binding_revision_id,
        to_binding_revision_id=source_pin.to_binding_revision_id,
        read_set_hash=source_pin.read_set_hash)

    refusal = assemble_admitted_crosswalk(
        db, definition_id=revision.definition_id, scope=SANDBOX_SCOPE, now=NOW,
        source_leg=fabricated, target_leg=target_pin)

    assert isinstance(refusal, CrosswalkAssemblyRefusalV1)
    assert refusal.code is CrosswalkAssemblyReason.BINDING_REVISION_NOT_STORED
    assert unknown.binding_revision_id in refusal.detail


def test_a_measurement_of_another_mapping_table_refuses_instead_of_lending_its_verdict(bank):
    db, revision, cib, ftr, mapping, realization = bank
    other_map = binding("cib", MAP_TABLE, schema="dpl_eib_v2")
    record_bindings(db, other_map)
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, other_map, scope=SANDBOX_SCOPE))

    refusal = assemble(db, revision, cib, ftr, mapping, realization)

    assert isinstance(refusal, CrosswalkAssemblyRefusalV1)
    assert refusal.code is CrosswalkAssemblyReason.MEASUREMENT_MAPPING_BINDING_MISMATCH


def test_a_row_selection_for_the_wrong_dataset_surfaces_as_a_bundle_refusal(bank):
    """`__post_init__` raises; a read surface returns the refusal rather than a 500."""
    db, revision, cib, ftr, mapping, realization = bank

    refusal = assemble(db, revision, cib, ftr, mapping, realization,
                       selection=row_selection(dataset_logical_ref=CIB))

    assert isinstance(refusal, CrosswalkAssemblyRefusalV1)
    assert refusal.code is CrosswalkAssemblyReason.BUNDLE_INCONSISTENT


# ── scope exactness: a sandbox measurement never answers a production question ───────────────────

def test_the_measurement_is_picked_by_SCOPE_and_never_by_recency(bank):
    """A production assembly must not pick up a newer SANDBOX measurement."""
    db, revision, cib, ftr, mapping, realization = bank
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=PRODUCTION_SCOPE))
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=SANDBOX_SCOPE,
                    observed_at=NOW + timedelta(days=1), composed_row_count=99))

    admitted = assemble(db, revision, cib, ftr, mapping, realization, scope=PRODUCTION_SCOPE)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    assert admitted.observation is not None
    assert admitted.observation.scope_id == scope_binding_id(PRODUCTION_SCOPE)
    assert admitted.observation.composed_row_count == 3


# ── item 5: sandbox graduation evidence is PERSISTED, not discarded ─────────────────────────────

def test_a_sandbox_measurement_stays_readable_after_a_production_one_is_recorded(bank):
    """The graduation evidence survives. A later production assessment can still read what the
    sandbox probe found — it simply may not COUNT it as production evidence.

    Migration 1057 keys the current pointer per `current_scope_key` (which carries the scope) and
    keeps every measurement in `crosswalk_observation_revision`, so both survive with no new table.
    """
    db, revision, cib, ftr, mapping, realization = bank
    sandbox = measure(revision, cib, ftr, mapping, scope=SANDBOX_SCOPE)
    record_crosswalk_observation(db, sandbox)
    production = measure(revision, cib, ftr, mapping, scope=PRODUCTION_SCOPE,
                         observed_at=NOW + timedelta(days=2))
    record_crosswalk_observation(db, production)

    current = current_crosswalk_observations_for_revision(db, revision.revision_id)
    history = crosswalk_observation_history(db, revision.revision_id)

    # BOTH are current — one per measured scope, never merged and never displaced.
    assert {item.scope_id for item in current} == {
        scope_binding_id(SANDBOX_SCOPE), scope_binding_id(PRODUCTION_SCOPE)}
    assert sandbox.observation_revision_id in {item.observation_revision_id for item in history}
    # And the production assembly still refuses to READ the sandbox one as its evidence.
    admitted = assemble(db, revision, cib, ftr, mapping, realization, scope=PRODUCTION_SCOPE)
    assert isinstance(admitted, AdmittedCrosswalkV1)
    assert admitted.observation is not None
    assert admitted.observation.observation_revision_id == production.observation_revision_id


# ── a leg's realization is REVALIDATED, not assumed ─────────────────────────────────────────────

def test_demoting_the_legs_realization_withdraws_the_crosswalk_it_carried(bank):
    """The pin still names it; the CURRENT pointer no longer serves it, so the crosswalk refuses.

    This is the crosswalk half of ``authorize_execution_realizations``' doctrine: a leg whose
    realization was withdrawn between one read and the next must not carry the withdrawn link's
    authority into a fresh compilation.
    """
    db, revision, cib, ftr, mapping, realization = bank
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=PRODUCTION_SCOPE))
    healthy = assemble(db, revision, cib, ftr, mapping, realization, scope=PRODUCTION_SCOPE)
    assert isinstance(healthy, AdmittedCrosswalkV1)
    assert production_admissible(healthy, direction=SOURCE_TO_TARGET) is True

    demote_realization_revision(db, realization.realization_revision_id)

    withdrawn = assemble(db, revision, cib, ftr, mapping, realization, scope=PRODUCTION_SCOPE)
    assert isinstance(withdrawn, AdmittedCrosswalkV1)
    assert (CrosswalkAdmissionReason.LEG_NOT_CURRENTLY_EXECUTABLE.value
            in withdrawn.decision.reason_codes)
    assert production_admissible(withdrawn, direction=SOURCE_TO_TARGET) is False


# ── the mapping row rule is pinned from the MEASUREMENT, never from the caller ──────────────────

def test_a_row_selection_resolved_under_a_MOVED_policy_pointer_refuses(bank):
    """The drift refusal, made reachable. Measured under one row rule, resolved under another.

    `AdmittedCrosswalkV1.__post_init__` has always carried this refusal — "the current policy
    pointer moved after the crosswalk was measured, so the rows this traversal would read are not
    the rows its uniqueness was proved over" — and through this assembler it could never fire,
    because the pin it compares the selection against was DERIVED FROM THAT SELECTION. Both sides of
    the comparison were one value.

    `mapping_row_selection_hash` is deliberately absent from the measurement here: the content-hash
    backstop below the policy check would otherwise mask the defect, and `compose_crosswalk_
    observation` allows None, so a real measurement can arrive in exactly this shape.
    """
    db, revision, cib, ftr, mapping, realization = bank
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=SANDBOX_SCOPE))   # under POLICY_REVISION

    drifted = row_selection(temporal_policy_revision_id="dtp_" + "7" * 64)
    refusal = assemble(db, revision, cib, ftr, mapping, realization, selection=drifted)

    assert isinstance(refusal, CrosswalkAssemblyRefusalV1), (
        "a selection resolved under a policy that had since advanced assembled clean: the "
        "execution's pin followed the caller instead of the measurement")
    assert refusal.code is CrosswalkAssemblyReason.BUNDLE_INCONSISTENT
    assert "policy pointer moved" in refusal.detail


def test_a_row_selection_under_the_MEASURED_policy_assembles(bank):
    """The other direction, so the refusal above is a rule and not a wall.

    Without this the drift test would be satisfied by an assembler that refused every selection.
    """
    db, revision, cib, ftr, mapping, realization = bank
    selection = row_selection()
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=SANDBOX_SCOPE, selection=selection))

    admitted = assemble(db, revision, cib, ftr, mapping, realization, selection=selection)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    assert admitted.execution.mapping_temporal_policy_revision_id == POLICY_REVISION
    assert admitted.mapping_row_selection == selection


def test_the_pin_is_the_MEASURED_row_rule_even_when_the_definition_declares_none(bank):
    """The second face: an execution that pinned None over a measurement taken WITH a filter.

    The fixture definition declares no mapping temporal policy and the measurement was taken under
    one, which is the ordinary shape — the row policy lives on the dataset, and a crosswalk
    published before anybody declared it says nothing. Pinning the DEFINITION's None there left
    `joins.plan_crosswalk_join` with nothing to require: its "pins a policy and no resolved row
    selection was supplied" refusal never fired, so the traversal read the mapping table UNFILTERED
    under a uniqueness verdict measured over the filtered rows.

    Proved through the real planner, not by inspecting the pin: the refusal is what the pin is for.
    """
    from featuregen.materialize.joins import MaterializationRefused, plan_crosswalk_join

    db, revision, cib, ftr, mapping, realization = bank
    assert revision.mapping_temporal_policy_revision_id is None, "the fixture declares no policy"
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=SANDBOX_SCOPE))

    admitted = assemble(db, revision, cib, ftr, mapping, realization)   # NO row selection supplied

    assert isinstance(admitted, AdmittedCrosswalkV1), (
        "the execution's mapping row rule stopped following the measurement, so the assembler's "
        "own derivation guard refused the bundle")
    assert admitted.execution.mapping_temporal_policy_revision_id == POLICY_REVISION

    outcome = plan_crosswalk_join(
        admitted, direction=SOURCE_TO_TARGET,
        from_identity=_identity(cib), to_identity=_identity(ftr),
        mapping_identity=_identity(mapping), execution_tier=ExecutionTier.SANDBOX)

    assert isinstance(outcome, MaterializationRefused), (
        "the traversal planned against UNFILTERED mapping rows under a verdict measured with a "
        "filter, because the execution pinned the definition's absent policy")
    assert "no resolved row selection was supplied" in outcome.detail


def test_an_unmeasured_crosswalk_pins_the_definitions_own_declaration(bank):
    """Nothing measured it, so the crosswalk's own declaration is the only recorded answer."""
    db, _revision, cib, ftr, mapping, realization = bank
    declared = definition(mapping_temporal_policy_revision_id=POLICY_REVISION)
    publish_crosswalk_definition(
        db, declared, expected_pointer_version=1, actor=ACTOR, roles=ROLES)

    admitted = assemble(db, declared, cib, ftr, mapping, realization)

    assert isinstance(admitted, AdmittedCrosswalkV1)
    assert admitted.observation is None
    assert admitted.execution.mapping_temporal_policy_revision_id == POLICY_REVISION


def test_a_pin_that_stops_following_the_measurement_is_refused_not_assembled(bank, monkeypatch):
    """The DERIVATION guard, exercised by breaking the derivation it guards.

    Unreachable while `pinned_mapping_temporal_policy` is right — which is the point. The shipped
    defect was a pin derived from the caller's row selection, and anything that re-points the
    derivation at the caller lands here, with both policies named, instead of assembling a bundle
    whose verdict describes another row set.
    """
    from featuregen.overlay.upload import crosswalk_assembly

    db, revision, cib, ftr, mapping, realization = bank
    record_crosswalk_observation(
        db, measure(revision, cib, ftr, mapping, scope=SANDBOX_SCOPE))
    monkeypatch.setattr(
        crosswalk_assembly, "pinned_mapping_temporal_policy",
        lambda definition, observation: "dtp_" + "7" * 64)

    refusal = assemble(db, revision, cib, ftr, mapping, realization)

    assert isinstance(refusal, CrosswalkAssemblyRefusalV1)
    assert refusal.code is CrosswalkAssemblyReason.MEASUREMENT_TEMPORAL_POLICY_MISMATCH
    assert POLICY_REVISION in refusal.detail


def _identity(bind):
    """The binding's address, in the planner's own shape."""
    from featuregen.materialize.joins import PhysicalIdentity

    return PhysicalIdentity(
        catalog_source=bind.identity.catalog_source, schema=bind.identity.schema,
        table=bind.identity.table)


# ── the binding loader ──────────────────────────────────────────────────────────────────────────

def test_a_stored_binding_round_trips_by_revision_id(bank):
    db, _revision, cib, _ftr, _mapping, _realization = bank
    assert load_binding_revision(db, cib.binding_revision_id) == cib


def test_an_unknown_binding_revision_is_absence_not_a_guess(bank):
    db, _revision, _cib, _ftr, _mapping, _realization = bank
    assert load_binding_revision(db, "pbr_" + "0" * 64) is None
