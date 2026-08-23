"""SE-8 (part 1) — every origin plans through ONE entry, with the planner pipeline unchanged."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from featuregen.overlay.upload.binding_roles import JoinRole, TemporalRole
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    RequiredOperandV1,
    planning_request_from_recipe,
    planning_request_from_user_definition,
)
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.need_metadata import (
    RESOLVED_NEED_METADATA,
    ResolvedNeedMetadataV1,
)
from featuregen.overlay.upload.planner import candidates as candidates_module
from featuregen.overlay.upload.planner.candidates import discover_ingredient_candidates
from featuregen.overlay.upload.planner.contracts import PlanResolutionStatus
from featuregen.overlay.upload.planner.declarations import recipe_content_hash
from featuregen.overlay.upload.planner.plan import plan_bindings
from featuregen.overlay.upload.planner.requests import plan_planning_request, planning_probe
from featuregen.overlay.upload.planner.scope import resolve_catalog_scope
from featuregen.overlay.upload.recipe_grounding_context import canonical_recipe_v2_hash
from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id
from featuregen.overlay.upload.templates import ALL_TEMPLATES

_NOW = datetime(2026, 8, 12, 12, 0, tzinfo=UTC)
RECIPE = v2_recipe_by_id("customer_activity_recency")


def _catalog(db, source: str) -> None:
    catalog = [
        # TRANSACTION-grain, as the recipe's allowed_source_grains demand — the projection
        # carries the constraint and the planner enforces it, so the fixture must honor it.
        (CanonicalRow(source, "events", "txn_id", "integer", is_grain=True,
                      entity="Transaction"), "transaction_id"),
        (CanonicalRow(source, "events", "customer_id", "integer",
                      entity="Customer"), "customer_id"),
        (CanonicalRow(source, "events", "account_id", "integer"), "account_id"),
        (CanonicalRow(source, "events", "event_ts", "timestamp"), "event_timestamp"),
    ]
    build_graph(db, source, [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, %s, 'r', 1) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (source, _NOW, _NOW))


def test_the_probe_is_a_faithful_projection_of_the_request():
    request = planning_request_from_recipe(RECIPE)
    probe = planning_probe(request)
    assert probe.id == RECIPE.recipe_id
    assert probe.family == "planning_request:recipe_v2"
    assert [n.role for n in probe.needs] == [op.role for op in RECIPE.operands]
    assert [n.concept for n in probe.needs] == [op.concept for op in RECIPE.operands]
    for need, operand in zip(probe.needs, RECIPE.operands):
        assert need.allowed_source_grains == operand.allowed_source_grains
        assert need.optional == (not operand.required)
    assert probe.additivity == RECIPE.output.additivity
    assert probe.intent == RECIPE.recipe_id               # an id, never prose


def test_probe_carries_the_source_anchor_for_a_multi_entity_recipe():
    from featuregen.overlay.upload.need_metadata import derive_need_metadata
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    recipe = v2_recipe_by_id("own_transfer_outflow_amount")   # account + beneficiary keys
    probe = planning_probe(planning_request_from_recipe(recipe))
    assert probe.source_entity == recipe.source_grain
    assert probe.source_entity_need_role is not None
    derive_need_metadata(probe)                                # must not raise


def test_probe_anchor_derives_for_every_v2_recipe_or_leaves_planner_fallback():
    """The whole registry sweep: 58 probes used to raise inside `derive_need_metadata`; the list
    is now EMPTY, with no id pinned as an expected failure.

    How the residue closed. Grain compatibility alone (the plain rule) settles 314 of the 317
    recipes and leaves 3 whose several entity keys are ALL compatible with the source grain; the
    two declared tie-breaks in `_source_anchor` settle exactly those — `own_transfer_outflow_amount`
    and `first_time_payee_high_value` on the payee's declared `relationship_requirement`,
    `customer_worst_days_in_collection` on the recipe's own `output_grain`. One recipe,
    `device_sharing_velocity`, deliberately keeps the planner FALLBACK: its only `entity_key`
    operand names `device_fingerprint`, a concept the governed registry links to no entity, so
    anchoring it would be a mis-anchor the planner cannot bind — with no anchor its single
    entity-linked need (`account`) is unambiguous and nothing raises."""
    from featuregen.overlay.upload.need_metadata import derive_need_metadata
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    raising = []
    for recipe in V2_RECIPES:
        probe = planning_probe(planning_request_from_recipe(recipe))
        try:
            derive_need_metadata(probe)
        except ValueError:
            raising.append(recipe.recipe_id)
    assert raising == [], f"{len(raising)} probes still raise: {raising[:5]}"


def test_the_declared_tie_breaks_pick_the_source_key_never_the_related_one():
    """WHICH key each tie-break picks — the sweep only proves nothing raises, and a rule that
    anchored the verified payee would satisfy it just as well."""
    from featuregen.overlay.upload.recipe_registry_v2 import v2_recipe_by_id

    def anchor_role(recipe_id: str) -> str | None:
        return planning_probe(
            planning_request_from_recipe(v2_recipe_by_id(recipe_id))).source_entity_need_role

    assert anchor_role("own_transfer_outflow_amount") == "account"        # not the payee
    assert anchor_role("first_time_payee_high_value") == "customer"       # not the payee
    assert anchor_role("customer_worst_days_in_collection") == "customer" # the recipe's own grain
    # a SOLE compatible key that declares a relationship requirement is still the anchor: the
    # relationship tie-break breaks a tie, it never disqualifies the only candidate.
    assert anchor_role("household_relationship_value") == "household"


def test_incompatible_sole_entity_key_is_never_an_anchor():
    """A sole entity key whose allowed grains EXCLUDE the source grain must yield None/None
    (planner fallback), never a mis-anchor the planner cannot bind."""
    request = FeaturePlanningRequestV1(
        origin="user_definition",
        source_definition_id="user:grain_incompatible_anchor",
        source_revision="1",
        source_content_hash="incompatiblehash",
        primary_objective=RECIPE.primary_objective,
        output=RECIPE.output,
        operands=(
            # the ONLY entity key, and its declared grains do NOT name the request's source grain
            RequiredOperandV1(role="who", concept="customer_id", operand_class="entity_key",
                              allowed_source_grains=("account",)),
            RequiredOperandV1(role="when", concept="event_timestamp",
                              operand_class="event_timestamp",
                              allowed_source_grains=("transaction",)),
        ),
        source_grain="transaction", output_grain="customer",
        temporal=RECIPE.temporal,
        computation_kind="conceptual_pattern",
        conceptual_reason="probe: the sole entity key is grain-incompatible by construction")
    probe = planning_probe(request)
    assert probe.source_entity is None
    assert probe.source_entity_need_role is None


def test_a_recipe_origin_request_plans_end_to_end(db):
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    result = plan_planning_request(
        db, request=planning_request_from_recipe(RECIPE), target_entity="customer",
        scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved
    selected = next(p for p in result.candidate_plans
                    if p.physical_plan_id == result.selected_plan_id)
    bound = {b.bound_object_ref for b in selected.ingredient_bindings}
    assert "public.events.event_ts" in bound
    assert result.replay_envelope.planner_input_hash     # the same envelope machinery


def test_a_user_definition_origin_plans_through_the_same_entry(db):
    """The origin-neutrality proof: a user-defined request (no recipe anywhere) reaches the
    SAME planner, same envelope, same fail-closed rules."""
    _catalog(db, "core")
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    request = planning_request_from_user_definition(
        definition_id="user:recency_probe",
        primary_objective=RECIPE.primary_objective,
        output=RECIPE.output,
        operands=(
            RequiredOperandV1(role="who", concept="customer_id", operand_class="entity_key",
                              allowed_source_grains=("transaction",)),
            RequiredOperandV1(role="when", concept="event_timestamp",
                              operand_class="event_timestamp"),
        ),
        source_grain=RECIPE.source_grain, output_grain=RECIPE.output_grain,
        temporal=RECIPE.temporal, content_hash="userhash")
    result = plan_planning_request(
        db, request=request, target_entity="customer", scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.resolved
    selected = next(p for p in result.candidate_plans
                    if p.physical_plan_id == result.selected_plan_id)
    assert {b.bound_object_ref for b in selected.ingredient_bindings} == {
        "public.events.customer_id", "public.events.event_ts"}


def test_no_authorized_catalog_stays_not_applicable_for_requests_too(db):
    scope = resolve_catalog_scope(db, roles=(), target_entity="customer", now=_NOW)
    result = plan_planning_request(
        db, request=planning_request_from_recipe(RECIPE), target_entity="customer",
        scope=scope, roles=(), now=_NOW)
    assert result.result_status is PlanResolutionStatus.not_applicable


# ── S1A-2: the identity-neutral registry bypass ────────────────────────────────────────────────
# `discover_ingredient_candidates` resolves each need's binding metadata out of
# RESOLVED_NEED_METADATA, keyed on `template.id`. 106 ids in the legacy template corpus collide
# with V2 recipe ids, so a probe projected from a V2 recipe silently inherits the LEGACY
# template's resolved metadata (measured: 37 of 317 V2 recipes are shadowed). The discriminator
# has to be a planner ARGUMENT, never a `Template` field — `recipe_grounding_context` enumerates
# Template's fields dynamically, so a new field would move every legacy template's canonical hash.

_COLLIDING_ID = "inflow_outflow_ratio"     # one of the 106 shared ids; `direction` is a shared role

# An entry no honest derivation could ever produce, so a match proves the registry was consulted.
_POISON = ResolvedNeedMetadataV1(
    role="direction", concept="debit_credit_indicator",
    allowed_source_grains=("__sentinel_grain__",),
    join_role=JoinRole.SOURCE_ENTITY_KEY,    # absurd: a debit/credit flag is not an entity key
    temporal_role=TemporalRole.AS_OF_TIME,   # absurd: the flag carries no time at all
    grain_source="explicit_recipe", join_role_source="explicit_recipe",
    temporal_role_source="explicit_recipe")


def _inflow_outflow_catalog(db, source: str) -> None:
    """One TRANSACTION-grain table binding all four `inflow_outflow_ratio` operands, so the recipe
    can plan end-to-end. `direction` is the ONE need role the legacy template and the V2 recipe of
    the same id share — the role the collision shadows."""
    catalog = [
        (CanonicalRow(source, "postings", "txn_id", "integer", is_grain=True,
                      entity="Transaction"), "transaction_id"),
        (CanonicalRow(source, "postings", "account_id", "integer",
                      entity="Account"), "account_id"),
        (CanonicalRow(source, "postings", "amount", "numeric", currency="USD"), "monetary_flow"),
        (CanonicalRow(source, "postings", "dr_cr", "text"), "debit_credit_indicator"),
        (CanonicalRow(source, "postings", "event_ts", "timestamp"), "event_timestamp"),
    ]
    build_graph(db, source, [r for r, _ in catalog],
                concepts={content_hash(r): c for r, c in catalog})
    db.execute(
        "INSERT INTO overlay_drift_watermark (catalog_source, last_completed_at, last_run_id, "
        "head_seq) VALUES (%s, %s, 'r', 1) "
        "ON CONFLICT (catalog_source) DO UPDATE SET last_completed_at = %s",
        (source, _NOW, _NOW))


def test_probe_operand_metadata_is_never_shadowed_by_the_legacy_registry(db, monkeypatch):
    """106 V2 ids collide with legacy template ids. In request_contract mode the resolved-need
    registry must not override a probe's own declared operand metadata; in legacy mode the same
    poisoned entry MUST still be consumed (existing behavior preserved)."""
    _inflow_outflow_catalog(db, "core")
    # RESOLVED_NEED_METADATA is a MappingProxyType (immutable), and `candidates` binds the name at
    # import — so the poison goes on the CONSUMER's module attribute, not via setitem.
    monkeypatch.setattr(candidates_module, "RESOLVED_NEED_METADATA",
                        dict(RESOLVED_NEED_METADATA) | {_COLLIDING_ID: (_POISON,)})

    probe = planning_probe(planning_request_from_recipe(v2_recipe_by_id(_COLLIDING_ID)))
    legacy = next(t for t in ALL_TEMPLATES if t.id == _COLLIDING_ID)
    assert probe.id == legacy.id                      # the collision itself, not a contrivance

    def direction(template, **kw):
        # required_grains / join_role / temporal_role on the candidate ARE the resolved need's
        # three fields verbatim — the narrowest surface that reflects the registry read.
        return discover_ingredient_candidates(
            db, template, "core", roles=(), **kw).candidates["direction"]

    clean = direction(probe, metadata_resolution_mode="request_contract")
    assert clean, "fixture must yield at least one debit_credit_indicator candidate"
    assert {c.required_grains for c in clean} == {("transaction",)}   # the probe's OWN declaration
    # S1A-4c moved these two: the V2 operand still DECLARES neither role, but the probe now
    # projects them from the operand's own class + concept (`direction` on a concept with no
    # entity_link and a `none` pit_role -> MEASURE, no temporal role). They used to read `""`.
    # The bypass proof is unchanged and in fact sharper — both values contradict the poison.
    assert {c.join_role for c in clean} == {"measure"}                # NOT the poison's key role
    assert {c.temporal_role for c in clean} == {""}                   # NOT the poison's as_of_time

    # the SAME probe under the default mode still eats the sentinel — the mode is what changed,
    # not the template (so this can never be passing for an unrelated reason).
    assert {c.required_grains for c in direction(probe)} == {("__sentinel_grain__",)}

    poisoned = direction(legacy)                      # legacy template, default mode: UNCHANGED
    assert poisoned
    assert {c.required_grains for c in poisoned} == {("__sentinel_grain__",)}
    assert {c.join_role for c in poisoned} == {"source_entity_key"}
    assert {c.temporal_role for c in poisoned} == {"as_of_time"}


def test_plan_planning_request_asks_for_request_contract_mode(db, monkeypatch):
    """The SEAM, not the switch: the two tests above drive `discover_ingredient_candidates` and
    `plan_bindings` directly, so deleting the `metadata_resolution_mode="request_contract"`
    argument from `plan_planning_request` would leave them green. This test plans a COLLIDING
    recipe through the request entry itself with the registry poisoned, and the control leg below
    is exactly what that deletion would produce — so the argument is pinned by construction.

    (The three older `plan_planning_request` tests plan `customer_activity_recency`, whose id
    collides with no legacy template, which is why they are structurally blind to the mode.)"""
    _inflow_outflow_catalog(db, "core")
    monkeypatch.setattr(candidates_module, "RESOLVED_NEED_METADATA",
                        dict(RESOLVED_NEED_METADATA) | {_COLLIDING_ID: (_POISON,)})
    request = planning_request_from_recipe(v2_recipe_by_id(_COLLIDING_ID))
    scope = resolve_catalog_scope(db, roles=(), target_entity="account", now=_NOW)

    result = plan_planning_request(db, request=request, target_entity="account",
                                   scope=scope, roles=(), now=_NOW)
    # the UNSHADOWED outcome: `direction` binds on its own declared transaction grain, so the
    # recipe resolves even though the registry entry it collides with says otherwise.
    assert result.result_status is PlanResolutionStatus.resolved
    selected = next(p for p in result.candidate_plans
                    if p.physical_plan_id == result.selected_plan_id)
    assert "public.postings.dr_cr" in {b.bound_object_ref for b in selected.ingredient_bindings}

    # The control — byte-for-byte what `plan_planning_request` would do without the argument.
    # The sentinel grain rejects `direction`, the required need goes unbound, and the recipe no
    # longer resolves: the assertion above is NOT satisfiable under the default mode.
    without_the_argument = plan_bindings(
        db, template=planning_probe(request), target_entity="account", scope=scope,
        roles=(), now=_NOW)
    assert without_the_argument.result_status is not PlanResolutionStatus.resolved


def test_plan_bindings_rejects_an_unknown_resolution_mode():
    """The pair is CLOSED: a typo must fail loudly, never degrade silently to the shadowing
    default. Validation is the first thing `plan_bindings` does, so conn/scope are never touched
    — passing None for both is the proof, and a later move of the check fails this test."""
    with pytest.raises(ValueError) as excinfo:
        plan_bindings(None, template=planning_probe(planning_request_from_recipe(RECIPE)),
                      target_entity=None, scope=None, now=_NOW,
                      metadata_resolution_mode="typo")
    message = str(excinfo.value)
    assert "typo" in message
    assert "legacy_registry" in message and "request_contract" in message


def test_the_registry_bypass_is_identity_neutral():
    """The literals below were computed on the PRE-change checkout (commit 219d8360, clean tree).
    They pin the two canonicalizations a new `Template`/`Need` field would have moved: the legacy
    recipe content hash and the V2 canonical recipe hash. Recomputing both sides with the same
    post-change code would only prove self-agreement — these are the before-values, pasted."""
    assert ALL_TEMPLATES[0].id == "balance_trend"
    assert recipe_content_hash(ALL_TEMPLATES[0]) == "rh_4bc9a3f80f885743"
    assert canonical_recipe_v2_hash(v2_recipe_by_id("posted_debit_amount")) == (
        "37b37069833163063a90c88a77ec2615107392a281bcb3906fbdc47d2c467b34")


# ── S1A-4c: projection-time role declaration ──────────────────────────────────────────────────
# `planning_probe` now DECLARES each projected need's `join_role`/`temporal_role` from the
# request's own declared facts (operand_class + the concept registry's entity_link/pit_role),
# because 0 of the 1195 V2 operands declare either and `metadata_resolution_mode="request_contract"`
# deliberately never consults the legacy resolved-need registry. Without a declared role no
# ingredient binding carries `source_entity_key`, and `plan._assemble_rollups` starts a roll-up
# ONLY from such a binding — so every recipe-origin cross-catalog request died before its first
# hop (G1). The tests below pin the mapping row by row; the frontier's new honest boundary is
# pinned in `tests/.../contract/test_governed_lens_requests.py`.


def _mapping_request(operands, *, source_grain="transaction", output_grain="customer",
                     definition_id="probe:role_mapping") -> FeaturePlanningRequestV1:
    return FeaturePlanningRequestV1(
        origin="user_definition", source_definition_id=definition_id, source_revision="1",
        source_content_hash="rolemappinghash", primary_objective=RECIPE.primary_objective,
        output=RECIPE.output, operands=operands, source_grain=source_grain,
        output_grain=output_grain, temporal=RECIPE.temporal,
        computation_kind="conceptual_pattern",
        conceptual_reason="probe: the projection-time role mapping, one row per rule")


# One request carrying a row for every unconditioned rule. `txn` is the ONLY entity key whose
# declared grains admit the request's source grain, so `_source_anchor` picks it with no tie-break
# — the other two keys are then classified by their concept's entity_link against the OUTPUT grain.
_MAPPING_OPERANDS = (
    RequiredOperandV1(role="txn", concept="transaction_id", operand_class="entity_key",
                      allowed_source_grains=("transaction",)),
    RequiredOperandV1(role="customer", concept="customer_id", operand_class="entity_key",
                      allowed_source_grains=("customer",)),
    RequiredOperandV1(role="account", concept="account_id", operand_class="entity_key",
                      allowed_source_grains=("account",)),
    RequiredOperandV1(role="event_ts", concept="event_timestamp",
                      operand_class="event_timestamp"),
    RequiredOperandV1(role="as_of", concept="as_of_date", operand_class="as_of_timestamp"),
    RequiredOperandV1(role="matures", concept="maturity_date", operand_class="as_of_timestamp"),
    RequiredOperandV1(role="knowledge_ts", concept="system_time",
                      operand_class="event_timestamp"),
    RequiredOperandV1(role="amount", concept="monetary_flow", operand_class="measure"),
    RequiredOperandV1(role="kind", concept="event_type", operand_class="dimension"),
    RequiredOperandV1(role="status", concept="booking_status", operand_class="status"),
    RequiredOperandV1(role="direction", concept="debit_credit_indicator",
                      operand_class="direction"),
    RequiredOperandV1(role="allocation", concept="payment_allocation",
                      operand_class="policy_input"),
)


def _roles(request: FeaturePlanningRequestV1) -> dict[str, tuple[object, object]]:
    return {n.role: (n.join_role, n.temporal_role) for n in planning_probe(request).needs}


def test_the_entity_key_rows_split_source_target_and_intermediate():
    """The three key rows, each decided by a DECLARED fact: the anchor `_source_anchor` chose, the
    key whose concept links the request's OWN output grain, and everything else (a hop key)."""
    roles = _roles(_mapping_request(_MAPPING_OPERANDS))
    assert roles["txn"] == (JoinRole.SOURCE_ENTITY_KEY, None)     # the chosen anchor
    assert roles["customer"] == (JoinRole.TARGET_ENTITY_KEY, None)  # entity_link == output_grain
    assert roles["account"] == (JoinRole.INTERMEDIATE_ENTITY_KEY, None)   # a hop key
    # …and the anchor really is the one `_source_anchor` picked, not a tuple position
    assert planning_probe(_mapping_request(_MAPPING_OPERANDS)).source_entity_need_role == "txn"


def test_the_timestamp_rows_carry_time_and_the_pit_derived_temporal_role():
    """`TIME` for both timestamp classes, with the temporal role read out of `_derive_one`'s OWN
    `pit_role` vocabulary map — never a second table. `maturity_date` is the proof that the map is
    reused rather than reinvented: its `pit_role` is a business future date, which that map sends
    to `TemporalRole.NONE`, and no plausible invented mapping would land there."""
    roles = _roles(_mapping_request(_MAPPING_OPERANDS))
    assert roles["event_ts"] == (JoinRole.TIME, TemporalRole.EVENT_TIME)
    assert roles["as_of"] == (JoinRole.TIME, TemporalRole.AS_OF_TIME)
    assert roles["knowledge_ts"] == (JoinRole.TIME, TemporalRole.INGESTION_TIME)
    assert roles["matures"] == (JoinRole.TIME, TemporalRole.NONE)


def test_the_value_rows_all_land_on_measure_reproducing_the_legacy_derivation():
    """`measure` plus the four non-key, non-time classes. The Step-0 design read settles these:
    `need_metadata._derive_one` sends every need whose concept has no `entity_link` and a `none`
    `pit_role` — which is what a dimension/status/direction/policy_input concept is — to
    `JoinRole.MEASURE` (measured: 264 of 264 such legacy needs, source `template_default`), and
    `declarations.compile_aggregation` stages exactly the `join_role == "measure"` bindings. So the
    projection REPRODUCES legacy semantics here; the misclassification (an operand nobody intended
    to aggregate typed as a measure) is G2 — a pre-existing semantic shared by both paths, left
    open on purpose and chartered with G3, not invented here."""
    roles = _roles(_mapping_request(_MAPPING_OPERANDS))
    for role in ("amount", "kind", "status", "direction", "allocation"):
        assert roles[role] == (JoinRole.MEASURE, None), role


def test_a_declared_role_string_wins_over_the_class_mapping():
    """`_derive_one`'s own first rung: an EXPLICIT declaration is never overridden by derivation.
    Both fields are declared here on operands the class mapping would have decided otherwise."""
    roles = _roles(_mapping_request((
        RequiredOperandV1(role="txn", concept="transaction_id", operand_class="entity_key",
                          allowed_source_grains=("transaction",)),
        # a measure the author declares is really the hop key
        RequiredOperandV1(role="amount", concept="monetary_flow", operand_class="measure",
                          join_role="intermediate_entity_key"),
        # an event timestamp the author declares carries INGESTION time, not the pit-derived event
        RequiredOperandV1(role="event_ts", concept="event_timestamp",
                          operand_class="event_timestamp", temporal_role="ingestion_time"),
    )))
    assert roles["amount"] == (JoinRole.INTERMEDIATE_ENTITY_KEY, None)
    assert roles["event_ts"] == (JoinRole.TIME, TemporalRole.INGESTION_TIME)


def test_a_declared_string_naming_no_vocabulary_member_derives_nothing():
    """Fail closed on a declaration the planner cannot honor: a non-empty string that names no
    `JoinRole`/`TemporalRole` member leaves THAT field unset rather than quietly substituting the
    derived value the author's declaration contradicts. The other field is unaffected."""
    roles = _roles(_mapping_request((
        RequiredOperandV1(role="txn", concept="transaction_id", operand_class="entity_key",
                          allowed_source_grains=("transaction",)),
        RequiredOperandV1(role="amount", concept="monetary_flow", operand_class="measure",
                          join_role="grouping_key"),
        RequiredOperandV1(role="event_ts", concept="event_timestamp",
                          operand_class="event_timestamp", temporal_role="trade_time"),
    )))
    assert roles["amount"] == (None, None)                 # NOT JoinRole.MEASURE
    assert roles["event_ts"] == (JoinRole.TIME, None)      # NOT TemporalRole.EVENT_TIME


def test_the_operand_class_vocabulary_is_covered_exhaustively():
    """The projection's three class sets PARTITION `OPERAND_CLASSES`. A new operand class must be
    an explicit decision — this fails the moment one is added without a role rule, instead of that
    class silently projecting no role at all."""
    from featuregen.overlay.upload.planner import requests as requests_module
    from featuregen.overlay.upload.recipe_contract_v2 import OPERAND_CLASSES

    key = {"entity_key"}
    time = set(requests_module._TIME_OPERAND_CLASSES)
    measure = set(requests_module._MEASURE_OPERAND_CLASSES)
    assert key | time | measure == set(OPERAND_CLASSES)
    assert not (key & time) and not (key & measure) and not (time & measure)


def test_every_v2_recipe_probe_declares_the_roles_the_frontier_needs():
    """The registry sweep: after the projection, every entity_key / timestamp / measure operand of
    every V2 recipe carries a join role, and each probe carrying an anchor resolves EXACTLY one
    `SOURCE_ENTITY_KEY` — the binding `plan._assemble_rollups` needs to start a roll-up.

    `device_sharing_velocity` is the one recipe with no anchor and therefore no source key: its
    only `entity_key` operand names `device_fingerprint`, a concept the governed registry links to
    no entity, so no honest source key exists to declare (`_source_anchor` returns None by design —
    see `test_probe_anchor_derives_for_every_v2_recipe_or_leaves_planner_fallback`). Its frontier
    does not start, exactly as before this change; a role invented for it would be a mis-anchor."""
    from featuregen.overlay.upload.need_metadata import derive_need_metadata
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    roleless, raising, without_source = [], [], []
    for recipe in V2_RECIPES:
        request = planning_request_from_recipe(recipe)
        probe = planning_probe(request)
        by_role = {n.role: n for n in probe.needs}
        for operand in request.operands:
            if (operand.operand_class in ("entity_key", "measure", "event_timestamp",
                                          "as_of_timestamp")
                    and by_role[operand.role].join_role is None):
                roleless.append((recipe.recipe_id, operand.role))
        sources = [n for n in probe.needs if n.join_role is JoinRole.SOURCE_ENTITY_KEY]
        if probe.source_entity_need_role is None:
            without_source.append(recipe.recipe_id)
        elif len(sources) != 1:
            raising.append((recipe.recipe_id, len(sources)))
        try:
            derive_need_metadata(probe)          # the S1A-1 sweep stays green
        except ValueError:
            raising.append((recipe.recipe_id, "derive_need_metadata raised"))

    assert roleless == [], f"{len(roleless)} bindable operands carry no join role: {roleless[:5]}"
    assert raising == []
    assert without_source == ["device_sharing_velocity"]


def test_no_v2_recipe_projects_a_target_entity_key_today():
    """The TARGET/INTERMEDIATE split is brief-specified and CORPUS-UNPROVEN — pin the zero so the
    first real occurrence is a deliberate, visible event rather than a silent guess.

    Measured at ``1c656743``: the ``TARGET_ENTITY_KEY`` line in ``_derived_roles`` fires for 0 of
    the 1195 V2 operands. Only 4 non-anchor ``entity_key`` operands exist in the whole registry and
    none has ``entity_link == output_grain``, so every one takes the ``INTERMEDIATE_ENTITY_KEY``
    line and the TARGET line has never executed on real data. The rule is kept because it is the
    honest reading of a key that names the grain the feature is PRODUCED at — but nothing in this
    corpus proves it, and a test that merely re-asserted the hand-made mapping unit row would hide
    that.

    The comparison also spans TWO UNVALIDATED STRING SPACES: ``output_grain`` is authored on the
    recipe, ``entity_link`` is the concept registry's vocabulary, and nothing reconciles them —
    40 of the 317 recipes carry an ``output_grain`` naming no value in the registry's 40-strong
    ``entity_link`` vocabulary (``card`` vs ``card_account``, ``security`` vs ``instrument``,
    ``reporting_entity`` vs ``legal_entity``, ``debtor`` vs ``customer``), saved today only by
    those recipes carrying a single anchor key. Both halves are pinned below. Reconciling the two
    spaces is a governance act on the registry and belongs to the G2/G3 charter, so **when this
    test fails, do not delete the assertion**: decide the vocabulary question first, then record
    the answer here.
    """
    from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    targets, non_anchor_keys = [], []
    for recipe in V2_RECIPES:
        request = planning_request_from_recipe(recipe)
        probe = planning_probe(request)
        by_role = {n.role: n for n in probe.needs}
        for operand in request.operands:
            need = by_role[operand.role]
            if need.join_role is JoinRole.TARGET_ENTITY_KEY:
                targets.append((recipe.recipe_id, operand.role, operand.concept,
                                recipe.output_grain))
            if (operand.operand_class == "entity_key"
                    and need.join_role is not JoinRole.SOURCE_ENTITY_KEY):
                non_anchor_keys.append((recipe.recipe_id, operand.role))

    assert targets == [], (
        "the corpus-unproven TARGET_ENTITY_KEY branch fired — see this test's docstring: settle "
        f"the output_grain-vs-entity_link vocabulary question before accepting it. {targets}")
    # …and the zero is not vacuous: non-anchor keys DO exist, they simply never match.
    assert sorted(non_anchor_keys) == [
        ("customer_worst_days_in_collection", "facility"),
        ("device_sharing_velocity", "device"),
        ("first_time_payee_high_value", "payee"),
        ("own_transfer_outflow_amount", "payee")]

    # the vocabulary gap itself, measured — the reason the branch cannot be trusted on sight
    entity_links = {c.entity_link for c in CONCEPT_REGISTRY.values() if c.entity_link}
    off_vocabulary = sorted({r.output_grain for r in V2_RECIPES} - entity_links)
    assert off_vocabulary == ["book_bucket", "card", "client", "debtor", "device", "legal_group",
                              "pool", "position", "program", "reporting_entity",
                              "respondent_bank", "security"]
    assert len([r for r in V2_RECIPES if r.output_grain not in entity_links]) == 40


def test_the_class_keyed_projection_diverges_from_the_concept_ladder_only_where_g2_lives():
    """The projection keys on ``operand_class``; ``_derive_one``'s ladder keys on the CONCEPT
    (entity_link, then pit_role, then MEASURE). They agree on 1113 of the 1195 V2 operands and
    disagree on 82 — every one of them an operand whose authored class and whose concept's
    governed facts say different things. Pinned by SHAPE so the set cannot quietly widen:

    * a ``dimension``/``status``/``policy_input`` operand on an ENTITY-LINKED concept (63): the
      ladder calls it a hop key, the class calls it a value. This is G2's territory exactly — the
      recipe author declared it a dimension, and the projection may not overrule that with a role
      the author did not choose;
    * a ``dimension``/``policy_input`` operand on a PIT-bearing concept (17): same shape, time
      instead of an entity;
    * ``device_sharing_velocity`` (2): its ``entity_key`` operand names a concept the registry
      links to no entity (ladder: MEASURE; class: a key), and its entity-linked ``account``
      operand is authored as a ``dimension`` (ladder: the fallback source key; class: a value).

    The count is not asserted — the SHAPE is, in BOTH directions (``seen == known``, not merely
    ``<=``): a divergence kind that disappears must be struck from the worklist just as loudly as
    a new one must be added, so the G2 worklist stays self-maintaining rather than drifting into a
    list of kinds that no longer exist. G2 and G3 are chartered together; when G2 is settled this
    test is the record of exactly which operands its ruling has to decide."""
    import dataclasses

    from featuregen.overlay.upload.need_metadata import derive_need_metadata
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    entity_link_kinds = {("intermediate_entity_key", "measure", cls)
                         for cls in ("dimension", "status", "policy_input")}
    pit_kinds = {("time", "measure", cls) for cls in ("dimension", "policy_input")}
    no_anchor_kinds = {("measure", "intermediate_entity_key", "entity_key"),
                       ("source_entity_key", "measure", "dimension")}
    known = entity_link_kinds | pit_kinds | no_anchor_kinds

    seen, unknown, agreeing = set(), [], 0
    for recipe in V2_RECIPES:
        request = planning_request_from_recipe(recipe)
        probe = planning_probe(request)
        # the SAME probe with its projected roles stripped — i.e. exactly the concept-keyed
        # ladder this change layered a declaration on top of, with nothing re-implemented here
        ladder = {m.role: m for m in derive_need_metadata(dataclasses.replace(
            probe, needs=tuple(dataclasses.replace(n, join_role=None, temporal_role=None)
                               for n in probe.needs)))}
        for need in probe.needs:
            legacy = ladder[need.role].join_role
            if legacy is need.join_role:
                agreeing += 1
                continue
            kind = (legacy.value, need.join_role.value if need.join_role else None,
                    next(o.operand_class for o in request.operands if o.role == need.role))
            seen.add(kind)
            if kind not in known:
                unknown.append((recipe.recipe_id, need.role, kind))

    assert unknown == [], f"{len(unknown)} unexpected divergences: {unknown[:5]}"
    assert seen == known, f"the G2 worklist drifted: gone={known - seen}, new={seen - known}"
    assert agreeing > 1000, agreeing        # the divergence is the exception, not the rule


def test_the_projection_moves_no_temporal_fact():
    """The temporal half is a REUSE proof, not a new opinion: for every operand of every V2 recipe
    the metadata the planner resolves off the probe carries exactly the temporal role
    `_derive_one` would have derived from the concept's `pit_role` — the projection declares the
    same value the derivation already produced, so nothing temporal moved."""
    from featuregen.overlay.upload.concepts import concept as resolve_concept
    from featuregen.overlay.upload.need_metadata import (
        _PIT_ROLE_TO_TEMPORAL,
        derive_need_metadata,
    )
    from featuregen.overlay.upload.recipe_registry_v2 import V2_RECIPES

    moved = []
    for recipe in V2_RECIPES:
        request = planning_request_from_recipe(recipe)
        resolved = {m.role: m for m in derive_need_metadata(planning_probe(request))}
        for operand in request.operands:
            c = resolve_concept(operand.concept)
            expected = _PIT_ROLE_TO_TEMPORAL.get(c.pit_role if c is not None else "none",
                                                 TemporalRole.NONE)
            if resolved[operand.role].temporal_role is not expected:
                moved.append((recipe.recipe_id, operand.role,
                              resolved[operand.role].temporal_role, expected))
    assert moved == [], f"{len(moved)} temporal roles moved: {moved[:5]}"
