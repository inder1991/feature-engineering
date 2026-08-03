"""Task 2 — the stable suggestion identity and the immutable revision identity (freeze 0F-8/0F-10).

Two identities, deliberately separated, and this suite is where the separation is PROVED:

* ``suggestion_id`` names the LOGICAL candidate — which recipe, bound to which operands in which
  roles, for which entity/grain/time, over which per-operand relationship path assignment. It is
  independent of the screen it was opened from, of the validation outcome and of every build
  observation.
* ``suggestion_revision_id`` names the exact CONTENT that produced this rendering of it.

The relationship-path input is the subtle one. ``GroundingDecisionTraceV1.ordered_relationship_path``
is a deduplicated leg SET (0F-7 as amended), so it cannot distinguish two candidates whose operands
swapped chains. The identity therefore consumes the per-operand ``JOIN_PATH`` pins — the
``(dependency_key, content_hash)`` assignment — which is exactly what the amendment names as the
identity-bearing material. ``test_two_candidates_that_swap_their_operand_chains_are_not_one`` is the
rule-23 pin.
"""
from __future__ import annotations

import itertools

import pytest

from featuregen.overlay.upload import read_scope as read_scope_module
from featuregen.overlay.upload.grounding_trace import (
    JOIN_PATH,
    READ_SCOPE,
    SuggestionDependencyClass,
    build_trace,
    column_dependency_key,
    dependency_pin,
    join_path_pin_content,
    relationship_leg,
)
from featuregen.overlay.upload.read_scope import allowed_classes
from featuregen.overlay.upload.suggestion_identity import (
    PRODUCER_CONTRACT_VERSION,
    SuggestionReadScopeV1,
    build_read_scope,
    dependency_content_hashes,
    join_path_assignment,
    suggestion_id,
    suggestion_revision_id,
)

SOURCE = "cat"
_G = "public.cust.cif_id"
_A = "public.txn.amt"
_B = "public.bal.amt"


def _leg(from_ref: str, to_ref: str, *, fact: str | None = "ajf") -> object:
    return relationship_leg(
        relationship_ref=f"{SOURCE}:joins:{from_ref}->{to_ref}", relationship_kind="direct_equality",
        from_ref=(SOURCE, from_ref), to_ref=(SOURCE, to_ref),
        realization_content={"from": from_ref, "to": to_ref, "fact": fact},
        cardinality="N:1", safety_status="clearing", review_status="VERIFIED")


def _join_pin(operand_ref: str, *, from_table: str, to_table: str, legs) -> object:
    return dependency_pin(
        dependency_class=SuggestionDependencyClass.VALIDATION, dependency_kind=JOIN_PATH,
        dependency_key=column_dependency_key(SOURCE, operand_ref),
        content=join_path_pin_content(from_table=from_table, to_table=to_table,
                                      outcome_kind="OPERATIONAL", legs=legs))


def _identity(**overrides) -> dict:
    base = dict(
        template_id="balance_trend",
        bound_params=(("window", 90),),
        operands=((SOURCE, "cust.cif_id", "entity"), (SOURCE, "txn.amt", "flow_col")),
        entity_id="customer",
        grain_refs=((SOURCE, _G),),
        time_ref=(SOURCE, "public.cust.as_of_dt"),
        relationship_path_assignment=(),
    )
    base.update(overrides)
    return base


def _revision(**overrides) -> dict:
    base = dict(
        suggestion_id=suggestion_id(**_identity()),
        recipe_revision_id="recipe-hash",
        discovery_metadata_revision_id="discovery-hash",
        semantic_context_hashes=(),
        dataset_profile_hashes=(),
        trace_content_hash="trace-hash",
        dependency_content_hashes=("dep-a", "dep-b"),
        validation_rule_content_hashes=("rule-a",),
        read_scope_rule_content_hashes=("scope-a",),
        validation_status="DESIGN_CHECKED",
    )
    base.update(overrides)
    return base


# ── suggestion_id: the logical candidate ────────────────────────────────────────────────────────
def test_the_same_logical_candidate_always_hashes_to_the_same_id():
    assert suggestion_id(**_identity()) == suggestion_id(**_identity())


def test_display_order_of_the_operands_does_not_move_the_id():
    """Operands are a SET of role bindings: which order the engine happened to emit them in is an
    implementation detail of the needs loop, not a different candidate."""
    forward = _identity()
    reversed_ = _identity(operands=tuple(reversed(forward["operands"])))
    assert suggestion_id(**forward) == suggestion_id(**reversed_)


def test_the_composite_grain_is_ORDERED_and_reordering_it_is_a_different_candidate():
    """Rule 25: grain is an ordered tuple of key operands. ``BY (customer, account)`` and
    ``BY (account, customer)`` are different groupings, so they are different candidates."""
    two = ((SOURCE, _G), (SOURCE, "public.cust.acct_id"))
    assert suggestion_id(**_identity(grain_refs=two)) != suggestion_id(
        **_identity(grain_refs=tuple(reversed(two))))


@pytest.mark.parametrize("field,value", [
    ("template_id", "other_recipe"),
    ("bound_params", (("window", 30),)),
    ("operands", ((SOURCE, "cust.cif_id", "entity"), (SOURCE, "bal.amt", "flow_col"))),
    ("entity_id", "account"),
    ("entity_id", None),
    ("grain_refs", ((SOURCE, "public.cust.acct_id"),)),
    ("grain_refs", ()),
    ("time_ref", None),
])
def test_every_identity_input_actually_moves_the_id(field, value):
    """A mutation on any frozen input must die. Without this the payload could silently drop a
    member and every candidate that differs only there would collapse into one card."""
    assert suggestion_id(**_identity(**{field: value})) != suggestion_id(**_identity())


def test_a_role_change_alone_is_a_different_candidate():
    """The recipe ROLE is part of the binding: the same column read as the flow and as the stock is
    not the same feature."""
    swapped = ((SOURCE, "cust.cif_id", "entity"), (SOURCE, "txn.amt", "stock_col"))
    assert suggestion_id(**_identity(operands=swapped)) != suggestion_id(**_identity())


# ── rule 23: the relationship path is logical identity ──────────────────────────────────────────
def test_the_same_columns_over_a_different_relationship_path_are_two_candidates():
    """Rule 23, minimally: identical operands, different traversed path -> different identity."""
    direct = (_join_pin("txn.amt", from_table="cust", to_table="txn",
                        legs=(_leg(_G, _A),)),)
    bridged = (_join_pin("txn.amt", from_table="cust", to_table="txn",
                         legs=(_leg(_G, "public.bridge.cif"), _leg("public.bridge.txn", _A))),)
    assert suggestion_id(**_identity(
        relationship_path_assignment=join_path_assignment_of(direct))) != suggestion_id(**_identity(
            relationship_path_assignment=join_path_assignment_of(bridged)))


def test_two_candidates_that_swap_their_operand_chains_are_not_one():
    """THE 0F-7 AMENDMENT, as identity. Two operands, two chains — and the two candidates differ
    ONLY in which operand used which chain. Their ``ordered_relationship_path`` leg SETS are equal,
    so an identity derived from that field alone would fuse them into one card. The per-operand
    ``JOIN_PATH`` assignment keeps them apart."""
    leg_a, leg_b = _leg(_G, _A), _leg(_G, _B)
    straight = (_join_pin("txn.amt", from_table="cust", to_table="txn", legs=(leg_a,)),
                _join_pin("bal.amt", from_table="cust", to_table="bal", legs=(leg_b,)))
    crossed = (_join_pin("txn.amt", from_table="cust", to_table="txn", legs=(leg_b,)),
               _join_pin("bal.amt", from_table="cust", to_table="bal", legs=(leg_a,)))
    # the leg SETS really are identical — otherwise this proof would be trivial
    assert {(leg.from_ref, leg.to_ref) for leg in (leg_a, leg_b)} == {
        (leg.from_ref, leg.to_ref) for leg in (leg_b, leg_a)}
    assert suggestion_id(**_identity(
        relationship_path_assignment=join_path_assignment_of(straight))) != suggestion_id(
            **_identity(relationship_path_assignment=join_path_assignment_of(crossed)))


def join_path_assignment_of(pins) -> tuple[tuple[str, str], ...]:
    """The assignment as it is read off a real trace, built here from bare pins."""
    trace = build_trace(
        candidate_key="k", ordered_operand_roles=(), ordered_relationship_path=(),
        validation_status="DESIGN_CHECKED", requirements=(),
        dependency_pins=(*pins, dependency_pin(
            dependency_class=SuggestionDependencyClass.HARD_AVAILABILITY,
            dependency_kind=READ_SCOPE, dependency_key="scope", content={"classes": []})),
        validation_rule_content_hashes=(), read_scope_rule_content_hashes=("r",))
    return join_path_assignment(trace)


def test_the_assignment_reads_only_the_join_path_pins():
    """Every OTHER pin (the governed type read, the grain lookup, the read scope) belongs to the
    revision, not to the logical identity: a column whose governed type was re-attested is the same
    candidate."""
    pins = (_join_pin("txn.amt", from_table="cust", to_table="txn", legs=(_leg(_G, _A),)),)
    assert len(join_path_assignment_of(pins)) == 1
    assert join_path_assignment_of(pins)[0][0] == column_dependency_key(SOURCE, "txn.amt")


def test_a_candidate_with_no_traversed_path_has_an_empty_assignment():
    assert join_path_assignment_of(()) == ()


def test_the_dependency_content_hashes_are_content_only_and_order_independent():
    """The revision's dependency input covers EVERY read plus every selected realization — as a
    sorted set, because the gauntlet's check order is not meaning, and as CONTENT, because
    ``current_revision_id`` and evidence occurrence ids are provenance."""
    leg = _leg(_G, _A)
    pin = _join_pin("txn.amt", from_table="cust", to_table="txn", legs=(leg,))
    scope = dependency_pin(
        dependency_class=SuggestionDependencyClass.HARD_AVAILABILITY, dependency_kind=READ_SCOPE,
        dependency_key="scope", content={"classes": []}, current_revision_id="rev-1")
    trace = build_trace(
        candidate_key="k", ordered_operand_roles=(), ordered_relationship_path=(leg,),
        validation_status="DESIGN_CHECKED", requirements=(), dependency_pins=(pin, scope),
        validation_rule_content_hashes=(), read_scope_rule_content_hashes=("r",))
    hashes = dependency_content_hashes(trace)
    assert hashes == tuple(sorted(hashes)) and len(hashes) == 3
    assert leg.realization_content_hash in hashes and pin.content_hash in hashes
    assert "rev-1" not in hashes
    assert dependency_content_hashes(None) == ()


# ── suggestion_revision_id: exact content ───────────────────────────────────────────────────────
def test_the_same_content_always_yields_the_same_revision():
    assert suggestion_revision_id(**_revision()) == suggestion_revision_id(**_revision())


@pytest.mark.parametrize("field,value", [
    ("suggestion_id", "another"),
    ("recipe_revision_id", "recipe-hash-2"),
    ("discovery_metadata_revision_id", "discovery-hash-2"),
    ("discovery_metadata_revision_id", None),
    ("semantic_context_hashes", ("sem-a",)),
    ("dataset_profile_hashes", ("prof-a",)),
    ("trace_content_hash", "trace-hash-2"),
    ("dependency_content_hashes", ("dep-a", "dep-c")),
    ("validation_rule_content_hashes", ("rule-b",)),
    ("read_scope_rule_content_hashes", ("scope-b",)),
    ("validation_status", "NEEDS_EXTERNAL_VALIDATION"),
])
def test_every_meaning_bearing_input_moves_the_revision(field, value):
    assert suggestion_revision_id(**_revision(**{field: value})) != suggestion_revision_id(
        **_revision())


def test_unordered_hash_sets_are_order_independent():
    """These are SETS of content hashes; the order the producer collected them in is not meaning."""
    assert suggestion_revision_id(**_revision(dependency_content_hashes=("dep-b", "dep-a"))) == (
        suggestion_revision_id(**_revision()))
    assert suggestion_revision_id(**_revision(
        semantic_context_hashes=("s2", "s1"))) == suggestion_revision_id(
            **_revision(semantic_context_hashes=("s1", "s2")))


def test_the_producer_contract_version_is_part_of_the_revision():
    """A change to what the BUILDER means by these fields re-revisions every suggestion — that is
    the point of an explicit producer contract version (0F-10)."""
    assert suggestion_revision_id(**_revision(),
                                  producer_contract_version="other") != suggestion_revision_id(
                                      **_revision())
    assert PRODUCER_CONTRACT_VERSION


def test_the_revision_refuses_the_provenance_fields_it_must_never_hash():
    """0F-10's exclusion list, enforced by the SIGNATURE rather than by discipline: a producer
    commit, refresh id, snapshot id or timestamp cannot be passed in at all, so it cannot leak into
    identity through a later careless edit."""
    for forbidden in ("producer_commit", "refresh_id", "generated_at", "metadata_snapshot_ids",
                      "scope_set_id", "evidence_event_ids", "discovery_registry_content_hash"):
        with pytest.raises(TypeError):
            suggestion_revision_id(**_revision(), **{forbidden: "x"})


def test_the_identity_refuses_the_anchor_it_must_never_hash():
    """The requested anchor is excluded so one candidate has ONE identity on its table, column and
    global surfaces (0F-10). Same signature-level proof."""
    for forbidden in ("anchor_table_ref", "anchor_catalog_source", "requested_table", "max_hops"):
        with pytest.raises(TypeError):
            suggestion_id(**_identity(), **{forbidden: "x"})


# ── read scope (0F-8) ───────────────────────────────────────────────────────────────────────────
def test_the_scope_is_the_canonical_allowed_class_tuple_never_the_roles():
    scope = build_read_scope(("feature_engineer", "pii_reader"))
    assert isinstance(scope, SuggestionReadScopeV1)
    assert scope.allowed_classes == ("pii",)
    assert scope.tenant is None


def test_two_different_functional_roles_with_the_same_data_scope_share_one_key():
    """Functional roles and user ids may never mint scope variants (0F-8/D8)."""
    assert build_read_scope(("feature_engineer", "pii_reader")).scope_key == build_read_scope(
        ("data_owner", "pii_reader", "catalog_viewer")).scope_key


def test_a_wider_scope_is_a_different_key():
    assert build_read_scope(("pii_reader",)).scope_key != build_read_scope(
        ("pii_reader", "restricted_reader")).scope_key
    assert build_read_scope(()).scope_key != build_read_scope(("pii_reader",)).scope_key


def test_the_scope_lattice_bound_is_recomputed_from_the_role_registries():
    """IMPORT GATE (0F-8). The bound is ``2 ** |grantable classes|``, recomputed from the two role
    maps — never the literal 8 — so a fourth grantable class fails loudly here instead of silently
    widening every scope-keyed structure."""
    universe = set(read_scope_module.SENSITIVITY_ROLES) | set(read_scope_module.RESTRICTION_ROLES)
    keys = set()
    for size in range(len(universe) + 1):
        for combo in itertools.combinations(sorted(universe), size):
            roles = tuple(
                {**read_scope_module.SENSITIVITY_ROLES,
                 **read_scope_module.RESTRICTION_ROLES}[cls] for cls in combo)
            scope = build_read_scope(roles)
            assert tuple(allowed_classes(roles)) == scope.allowed_classes
            keys.add(scope.scope_key)
    assert len(keys) == 2 ** len(universe)


def test_an_unknown_role_claim_contributes_nothing_to_the_scope():
    assert build_read_scope(("wizard", "root")).scope_key == build_read_scope(()).scope_key
