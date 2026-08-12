"""A.34's acceptance set: the five shipped recipes the missing policy disabled (D14).

THE FINDING, in the deferral's own words: "the platform's OWN recipe registry already ships five
templates whose REQUIRED anchor is a `pii`-classed concept, so every one of them is now permanently
refused with PERSONAL_DATA_POLICY_REQUIRED on every catalog" — `screening_exposure_365d` (pep_flag),
`device_sharing_velocity` and `new_device_flag` (device_fingerprint), `geo_velocity_impossible`
(geolocation), and `external_own_transfer_trend_90d` (pii + beneficiary_name). "The only bypass
today is FEATUREGEN_FEATURE_USE_GATE=0, which disables ALL FOUR classes at once."

This file is the proof that the aimed answer works, run through the REAL recipe path
(`contract.gate1._template_candidates` — grounding + the full gauntlet, the same call the Gate-#1
options are built from), never through `_use_gate` directly:

1. before any approval, exactly those five are refused, with that code;
2. FIVE OTHER recipes ground and are ACCEPTED on the same catalog at the same moment — the control
   that makes (1) a statement about the policy rather than about a catalog that grounds nothing;
3. approving their anchor concepts lights all five up, and each accepted idea NAMES the policy
   revisions that licensed it;
4. revoking ONE anchor kills exactly the recipes that use it, and nothing else.

THE FIXTURE IS DELIBERATELY ORDINARY: a customer master, a session log and a transaction table,
joined on the customer key, with as-of columns — a shape any retail bank has. The five recipes are
not made to ground by special pleading; they ground because their anchor concepts are present.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.contract.gate1 import _template_candidates
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.feature_assist import RejectCode
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.pii_policy_store import (
    approve_pii_use_policy,
    revoke_pii_use_policy,
)

SOURCE = "bank"

#: The five A.34 recipes, by the NAME the recipe path emits (template id + its default window), and
#: the pii-classed concepts each one's grounding binds. Written out rather than derived, so a
#: template whose needs change has to be reconciled here by a person.
A34_RECIPES: dict[str, tuple[str, ...]] = {
    "screening_exposure_365d": ("pep_flag",),
    "device_sharing_velocity": ("device_fingerprint",),
    "new_device_flag": ("device_fingerprint",),
    "geo_velocity_impossible": ("geolocation",),
    "external_own_transfer_trend_90d": ("pii", "beneficiary_name"),
}

#: Every anchor concept the five need, deduped.
ANCHORS: tuple[str, ...] = tuple(sorted(
    {concept for concepts in A34_RECIPES.values() for concept in concepts}))

#: A caller who may SEE the personal-data columns. Read scope and the USE gate are different
#: questions and this fixture keeps them separate: the roles decide what grounds, the POLICY decides
#: what may be built from what grounded.
ROLES = ("pii_reader", "restricted_reader")

_CATALOG: list[tuple[CanonicalRow, str]] = [
    # customer master — the grain, plus the KYC screening marker and the customer's own name
    (CanonicalRow(SOURCE, "customers", "cif_id", "text", is_grain=True, entity="Customer"),
     "customer_id"),
    (CanonicalRow(SOURCE, "customers", "as_of_date", "timestamp", as_of=True), "as_of_date"),
    (CanonicalRow(SOURCE, "customers", "full_name", "text", sensitivity="pii"), "pii"),
    (CanonicalRow(SOURCE, "customers", "pep_ind", "text", sensitivity="pii"), "pep_flag"),
    # session log — the two account-takeover access markers
    (CanonicalRow(SOURCE, "sessions", "device_fp", "text", sensitivity="pii"),
     "device_fingerprint"),
    (CanonicalRow(SOURCE, "sessions", "geo_point", "text", sensitivity="pii"), "geolocation"),
    (CanonicalRow(SOURCE, "sessions", "session_ts", "timestamp", as_of=True), "event_timestamp"),
    (CanonicalRow(SOURCE, "sessions", "cif_id", "text", entity="Customer",
                  joins_to="customers.cif_id"), "customer_id"),
    # transactions — the primacy-loss outflow signal
    (CanonicalRow(SOURCE, "transactions", "amount", "numeric", additivity="additive",
                  currency="USD"), "monetary_flow"),
    (CanonicalRow(SOURCE, "transactions", "txn_ts", "timestamp", as_of=True), "event_timestamp"),
    (CanonicalRow(SOURCE, "transactions", "bene_name", "text", sensitivity="pii"),
     "beneficiary_name"),
    (CanonicalRow(SOURCE, "transactions", "bene_bank", "text"), "beneficiary_bank"),
    (CanonicalRow(SOURCE, "transactions", "cif_id", "text", entity="Customer",
                  joins_to="customers.cif_id"), "customer_id"),
]
# Deliberately ABSENT: sanctions_hit_flag / adverse_media_flag / watchlist_hit_flag. They are
# OPTIONAL needs of screening_exposure and are themselves pii-classed, so including them would make
# that recipe's anchor set three concepts wide and blur which approval did the work.


@pytest.fixture
def catalog(db):
    rows = [row for row, _concept in _CATALOG]
    concepts = {content_hash(row): concept for row, concept in _CATALOG if concept}
    build_graph(db, SOURCE, rows, concepts=concepts)
    return db


def _recipe_path(db):
    """The REAL Gate-#1 recipe path: ground every template on this catalog and run each grounded
    candidate through the identical gauntlet an LLM candidate runs. Returns
    (accepted names, {name: reject code}).

    `now=None` skips the freshness check only — nothing else is relaxed, and the USE gate does not
    read it."""
    result = _template_candidates(
        db, catalog_source=SOURCE, roles=ROLES, target_ref=None, now=None)
    return ({idea.name for idea in result.ideas},
            {r["name"]: r["code"] for r in result.rejections})


def _approve_anchors(db, *concepts: str) -> dict[str, str]:
    out = {}
    for concept_name in (concepts or ANCHORS):
        revision_id, _v = approve_pii_use_policy(
            db, concept_name=concept_name,
            purpose="financial crime detection and account-takeover monitoring",
            expected_pointer_version=0, actor="admin@bank")
        out[concept_name] = revision_id
    return out


# ── 1 + 2 · refused before approval, and the control that makes that non-vacuous ─────────────────


def test_the_five_A34_recipes_are_refused_before_any_policy_exists(catalog):
    """The deferral's claim, reproduced on the real path. Exactly five, exactly this code."""
    accepted, refused = _recipe_path(catalog)

    assert set(A34_RECIPES) <= set(refused), sorted(refused)
    for name in A34_RECIPES:
        assert refused[name] == RejectCode.PERSONAL_DATA_POLICY_REQUIRED, (name, refused[name])
    assert not (set(A34_RECIPES) & accepted)
    # nothing ELSE is refused for this reason — the gate is aimed, not a blanket
    assert {name for name, code in refused.items()
            if code == RejectCode.PERSONAL_DATA_POLICY_REQUIRED} == set(A34_RECIPES)


def test_other_recipes_ground_and_are_accepted_on_the_same_catalog_at_the_same_moment(catalog):
    """The control. Without it, "the five are refused" would also be satisfied by a catalog that
    grounds nothing at all, and the test above would be measuring the fixture."""
    accepted, _refused = _recipe_path(catalog)
    assert len(accepted) >= 5, accepted
    assert not (accepted & set(A34_RECIPES))


# ── 3 · approving the anchors lights all five up ─────────────────────────────────────────────────


def test_approving_their_anchor_concepts_lights_every_one_of_the_five(catalog):
    before_accepted, _before = _recipe_path(catalog)
    revisions = _approve_anchors(catalog)

    accepted, refused = _recipe_path(catalog)

    assert set(A34_RECIPES) <= accepted, sorted(refused)
    assert RejectCode.PERSONAL_DATA_POLICY_REQUIRED not in set(refused.values())
    # and nothing that already worked stopped working
    assert before_accepted <= accepted
    assert len(revisions) == len(ANCHORS)


def test_each_lit_recipe_names_the_policy_revisions_that_licensed_it(catalog):
    """Provenance the reviewer can read off the feature: which decision, taken when, allowed this.
    `external_own_transfer_trend_90d` is the sharp case — it needs TWO approvals and names both."""
    revisions = _approve_anchors(catalog)
    ideas = _template_candidates(
        catalog, catalog_source=SOURCE, roles=ROLES, target_ref=None, now=None).ideas
    by_name = {idea.name: idea for idea in ideas}

    for name, concepts in A34_RECIPES.items():
        expected = tuple(sorted(revisions[concept] for concept in concepts))
        assert by_name[name].personal_data_policy_revision_ids == expected, name

    # a recipe that binds no personal data names nothing — empty stays distinguishable from licensed
    plain = next(idea for idea in ideas if idea.name not in A34_RECIPES)
    assert plain.personal_data_policy_revision_ids == ()


# ── 4 · revoking one kills exactly it ────────────────────────────────────────────────────────────


def test_revoking_one_anchor_kills_exactly_the_recipes_that_use_it(catalog):
    """`geolocation` anchors ONE of the five. Withdrawing it must refuse that one and leave the
    other four building — a revocation that took down the whole surface would be as useless as one
    that took down nothing."""
    _approve_anchors(catalog)
    accepted, _refused = _recipe_path(catalog)
    assert set(A34_RECIPES) <= accepted

    revoke_pii_use_policy(catalog, concept_name="geolocation", expected_pointer_version=1,
                          actor="admin@bank")

    accepted, refused = _recipe_path(catalog)
    assert refused["geo_velocity_impossible"] == RejectCode.PERSONAL_DATA_POLICY_REQUIRED
    assert "geo_velocity_impossible" not in accepted
    assert set(A34_RECIPES) - {"geo_velocity_impossible"} <= accepted
    assert {name for name, code in refused.items()
            if code == RejectCode.PERSONAL_DATA_POLICY_REQUIRED} == {"geo_velocity_impossible"}


def test_revoking_a_SHARED_anchor_kills_both_recipes_that_lean_on_it(catalog):
    """`device_fingerprint` anchors two of the five. Both go; nothing else does."""
    _approve_anchors(catalog)
    revoke_pii_use_policy(catalog, concept_name="device_fingerprint",
                          expected_pointer_version=1, actor="admin@bank")

    accepted, refused = _recipe_path(catalog)
    assert {name for name, code in refused.items()
            if code == RejectCode.PERSONAL_DATA_POLICY_REQUIRED} == {
        "device_sharing_velocity", "new_device_flag"}
    assert {"screening_exposure_365d", "geo_velocity_impossible",
            "external_own_transfer_trend_90d"} <= accepted


def test_a_partially_licensed_recipe_stays_refused_and_the_refusal_names_what_is_missing(catalog):
    """`external_own_transfer_trend_90d` needs BOTH `pii` and `beneficiary_name`. One approval is
    not most of the way there — it is not licensed, and the reviewer is told exactly which concept
    is still undeclared rather than being sent to re-read the whole list."""
    _approve_anchors(catalog, "pii", "pep_flag", "device_fingerprint", "geolocation")

    rejections = _template_candidates(
        catalog, catalog_source=SOURCE, roles=ROLES, target_ref=None, now=None).rejections
    by_name = {r["name"]: r for r in rejections}

    refusal = by_name["external_own_transfer_trend_90d"]
    assert refusal["code"] == RejectCode.PERSONAL_DATA_POLICY_REQUIRED
    assert "beneficiary_name" in refusal["reason"]
    assert "Governance -> Data-use policies" in refusal["reason"]
    # the four fully-licensed recipes are unaffected
    assert set(A34_RECIPES) - {"external_own_transfer_trend_90d"} <= {
        idea.name for idea in _template_candidates(
            catalog, catalog_source=SOURCE, roles=ROLES, target_ref=None, now=None).ideas}


def test_the_gate_kill_switch_is_no_longer_the_only_way_through(catalog):
    """A.34's sharpest sentence: "THE ONLY BYPASS TODAY IS FEATUREGEN_FEATURE_USE_GATE=0, which
    disables ALL FOUR classes at once ... so using it to run an AML model also un-gates ECOA."

    The aimed answer is aimed. Approving every pii anchor lights the five WITH THE GATE ON, and a
    protected characteristic sitting in the SAME catalog under the SAME approvals is still refused
    — which is precisely what the kill switch could not do."""
    from featuregen.overlay.upload.feature_assist import (
        _validate_idea,
        feature_use_gate_enabled,
    )

    _approve_anchors(catalog)
    assert feature_use_gate_enabled(), "the acceptance must hold with the gate ON, not around it"

    accepted, refused = _recipe_path(catalog)
    assert set(A34_RECIPES) <= accepted
    assert RejectCode.PERSONAL_DATA_POLICY_REQUIRED not in set(refused.values())

    # ECOA, in the same catalog, under every approval this test just made.
    protected = "public.customers.ctzn_ctry_cd"
    catalog.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "data_type, concept) VALUES (%s, %s, 'column', 'customers', 'ctzn_ctry_cd', 'text', "
        "'protected_attribute')", (SOURCE, protected))
    idea, rej = _validate_idea(
        catalog, {"name": "f", "derives_from": [protected], "aggregation": "latest"},
        {protected}, {protected: {SOURCE}}, None, None, None, roles=ROLES)
    assert idea is None
    assert rej.code == RejectCode.PROTECTED_CHARACTERISTIC
