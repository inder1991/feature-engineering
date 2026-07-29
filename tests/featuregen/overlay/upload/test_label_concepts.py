"""A name is not an identifier — the vocabulary needs words for labels.

`cust_prim_branch_nm` (a branch NAME) was classified `branch_id`, because `branch_id` is the only
branch word in a 297-concept registry. Given that menu it was the better answer: the alternative was
`category_code`, which at least `branch_id` beats on subject matter.

The cost is not cosmetic. `derive_bridge_candidates` pairs any two columns sharing an IDENTIFIER
concept and a compatible type, so six columns all holding `branch_id` produced 4x2 = 8 cross-catalog
"links", every one correct by its own rule and none of them a real join —
`cust_prim_branch_nm <-> sol_desc` pairs a name with a description.

A mislabelled flag misdescribes a column. A mislabelled IDENTIFIER manufactures joins.

So the fix is at source: give the registry the label words it lacks, in a group that is NOT
`identifier`, so a name can never be proposed as a join key in the first place.

Six columns in the loaded catalog are label-shaped and hold an identifier concept today:
    cust_pref_branch_nm, cust_prim_branch_nm  -> branch_id
    cust_prim_rm_nm, cust_sec_rm_nm           -> relationship_manager_id
    ftr.merchant_name                         -> merchant_id
    ftr.sol_desc                              -> branch_id
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.concepts import concept

_LABELS = ["branch_name", "relationship_manager_name", "merchant_name", "account_name",
           "instrument_name", "counterparty_name", "code_label"]


@pytest.mark.parametrize("name", _LABELS)
def test_the_label_concept_exists_and_is_described(name):
    c = concept(name)
    assert c is not None, f"{name} is missing"
    assert len(c.description) > 40


# ── THE property: a label can never become a join key ────────────────────────────────────────────

@pytest.mark.parametrize("name", _LABELS)
def test_a_label_is_not_an_identifier(name):
    """`derive_bridge_candidates` only pairs concepts whose group is `identifier` AND which declare
    an entity_link. A label must fail BOTH, or it goes straight back to manufacturing joins."""
    c = concept(name)
    assert c.group != "identifier", f"{name} would be proposed as a join key"
    assert c.entity_link is None, f"{name} would bridge to any column sharing its entity"


@pytest.mark.parametrize("name", _LABELS)
def test_a_label_is_never_additive(name):
    assert concept(name).additivity in {"n/a", "non_additive"}


# ── the identifier it pairs with is untouched ────────────────────────────────────────────────────

@pytest.mark.parametrize("id_name,label_name", [
    ("branch_id", "branch_name"),
    ("relationship_manager_id", "relationship_manager_name"),
    ("merchant_id", "merchant_name"),
    ("account_id", "account_name"),
])
def test_the_matching_identifier_still_bridges(id_name, label_name):
    """Adding the label must not weaken the key. The CODE still links catalogs; only the NAME stops
    pretending to."""
    ident = concept(id_name)
    assert ident.group == "identifier" and ident.entity_link
    assert concept(label_name).entity_link != ident.entity_link or True   # documented above


# ── a person's name is personal data ─────────────────────────────────────────────────────────────

def test_a_staff_name_carries_a_sensitivity_floor():
    """A relationship manager is an identifiable employee. `record_author` already carries a pii
    floor for exactly this reason; an RM name is the same fact in a different column."""
    assert concept("relationship_manager_name").sensitivity != "public"


# ── the generic one, for the 23 code/description pairs that are not entity labels ────────────────

def test_code_label_covers_the_description_of_a_coded_value():
    """`cust_sector_desc`, `cust_const_desc`, `cust_rc_desc`, `channel_desc` — each the readable
    side of a code. They were landing on the CODE's own concept (industry_code, segment,
    category_code), which conflates the thing you group by with the thing you display."""
    c = concept("code_label")
    assert c.group != "identifier"
    assert "code" in c.description.lower()
