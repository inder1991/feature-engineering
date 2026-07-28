"""Vocabulary for a CUSTOMER MASTER, not just risk and transactions.

The registry was built for credit risk and payments — delinquency, default, SICR, non-performing
exposure, PEP, sanctions. A real customer-master table landed on it and had nowhere to go: 12 of
CIB's columns collapsed to `boolean_flag` and 13 to `category_code`, not because the classifier
failed but because those were the only honest answers available. `cust_staff_flg` next to
`cust_curr_ntb_flg` next to `cust_calypso_stat_flg` — three unrelated things, one label.

Every concept added here is grounded in a column that actually exists in the loaded catalog, not
invented from a taxonomy. The clusters, by the columns that drove them:

  new_to_bank_flag        cust_curr_ntb_flg, cust_prev_9mnth_ntb_flg
  staff_indicator         cust_staff_flg
  legal_entity_type       cust_indv_flg, cust_const_cd/desc, cust_kyc_legal_structure, entity_cd
  residency_status        cust_non_resi_flg, cust_resi_stat_cd, cust_free_zone_cd
  restriction_status      cust_susp_flg, cust_negated_flg
  restriction_reason      cust_susp_rsn_cd, cust_blacklist_rsn_cd, cust_negated_rsn_cd
  nominee_indicator       cust_nominee_flg
  customer_relationship_status  cust_status_flg, cust_special_stat
  source_system_status    cust_advent/calypso/finacle/finone/visonplus_stat_flg
  customer_group_id       cust_group_cd, cust_group_nm
  parent_customer_id      cust_parent_cust_null_flg
  record_deleted_flag     delete_flg
  record_author           create_user_nm, update_user_nm
  kyc_narrative           cust_kyc_business_nature, cust_kyc_crp_background, cust_kyc_high_risk_reason
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.concepts import concept

_NEW = [
    "new_to_bank_flag", "staff_indicator", "legal_entity_type", "residency_status",
    "restriction_status", "restriction_reason", "nominee_indicator",
    "customer_relationship_status", "source_system_status", "customer_group_id",
    "parent_customer_id", "record_deleted_flag", "record_author", "kyc_narrative",
]


@pytest.mark.parametrize("name", _NEW)
def test_the_concept_exists_and_is_described(name):
    c = concept(name)
    assert c is not None, f"{name} is missing from the registry"
    assert len(c.description) > 40, f"{name} needs a description a reviewer can judge it by"


# ── aggregation honesty ──────────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize("name", _NEW)
def test_none_of_these_are_additive(name):
    """Not one of these is a measure you sum. Declaring additivity on a status or a flag is exactly
    the defect that made a bureau-inquiry DATE inherit `additive` — the registry must not seed a new
    instance of it."""
    assert concept(name).additivity in {"n/a", "non_additive"}


# ── the identifiers must be able to BRIDGE ───────────────────────────────────────────────────────

@pytest.mark.parametrize("name,entity", [
    ("customer_group_id", "customer_group"),
    ("parent_customer_id", "customer"),
])
def test_an_identifier_declares_the_entity_it_links(name, entity):
    """`derive_bridge_candidates` pairs identifier concepts that share an entity_link. Without one an
    identifier can never bridge catalogs, which is the whole reason the cross-catalog link exists."""
    c = concept(name)
    assert c.group == "identifier"
    assert c.entity_link == entity


def test_parent_customer_shares_the_customer_entity_so_it_can_bridge():
    """A parent-customer reference points at the SAME entity as `customer_id` — a self-referencing
    hierarchy. Giving it its own entity would silently make the hierarchy unbridgeable."""
    assert concept("parent_customer_id").entity_link == concept("customer_id").entity_link


# ── safety: what borders an outcome, and what is personal ────────────────────────────────────────

@pytest.mark.parametrize("name", ["restriction_status", "restriction_reason"])
def test_restriction_concepts_are_flagged_as_bordering_a_label(name):
    """Suspension, negation and blacklisting are AML/fraud CONSEQUENCES. A model predicting financial
    crime that trains on them is reading its own answer back. Not a hard leakage_anchor — they have
    legitimate uses — so `near_label`, which the 3-part leakage control flags rather than blocks."""
    assert concept(name).near_label is True


@pytest.mark.parametrize("name", ["staff_indicator", "record_author", "kyc_narrative"])
def test_the_personal_ones_carry_a_sensitivity_floor(name):
    """`staff_indicator` says this customer is an employee; `record_author` is a named member of
    staff; KYC narrative is free prose about a person. All three are personal data and must not
    default to `public`, since this value is the FLOOR fed to apply_sensitivity_floor."""
    assert concept(name).sensitivity != "public"


def test_a_soft_delete_flag_is_not_a_feature_signal_but_a_population_filter():
    """`delete_flg` decides whether a row should be in the population at all. It must be findable and
    described as such — a model that treats it as an ordinary predictor is training on rows the bank
    considers deleted."""
    c = concept("record_deleted_flag")
    assert c.group == "flag"
    assert "population" in c.description.lower() or "exclude" in c.description.lower()


# ── the registry stays coherent ──────────────────────────────────────────────────────────────────

def test_the_new_concepts_do_not_disturb_the_existing_ones():
    """A regression guard on the concepts these sit closest to."""
    assert concept("customer_id").entity_link == "customer"
    assert concept("boolean_flag").additivity == "n/a"
    assert concept("bureau_inquiry").additivity == "additive"
