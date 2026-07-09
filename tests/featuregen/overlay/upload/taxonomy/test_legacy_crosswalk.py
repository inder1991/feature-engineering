"""Phase-0 Task 3 — the 107-legacy-tag crosswalk.

The headline guarantee is *coverage*: every tag that appears on any of the 153 recipes
(`ALL_TEMPLATES[*].use_cases`) must have a crosswalk entry, every `use_case`-dimension target
must resolve in `USE_CASE_REGISTRY`, and the reclassified-out frameworks/measures/contexts must
have left the `use_case` dimension (spec §5 / D1–D7).
"""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.dimensions import is_known
from featuregen.overlay.upload.taxonomy.legacy_crosswalk import (
    LEGACY_TAG_CROSSWALK,
    crosswalk,
)
from featuregen.overlay.upload.taxonomy.use_cases import USE_CASE_REGISTRY
from featuregen.overlay.upload.templates import ALL_TEMPLATES

_VALID_STATUSES = {"mapped", "merged", "deprecated"}


def test_every_legacy_tag_is_covered():
    # Derive the full legacy-tag set from the recipes themselves — do not hand-guess it.
    tags = {uc for t in ALL_TEMPLATES for uc in t.use_cases}
    missing = tags - set(LEGACY_TAG_CROSSWALK)
    assert not missing, missing


def test_use_case_targets_resolve():
    for tag, e in LEGACY_TAG_CROSSWALK.items():
        if e["dimension"] == "use_case":
            assert e["target"] in USE_CASE_REGISTRY, (tag, e["target"])


def test_other_dimension_targets_resolve():
    # Every non-use_case, non-metadata target must be a governed member of its dimension.
    for tag, e in LEGACY_TAG_CROSSWALK.items():
        if e["dimension"] in ("use_case", "metadata"):
            continue
        assert is_known(e["dimension"], e["target"]), (tag, e["dimension"], e["target"])


def test_status_values_are_valid():
    for tag, e in LEGACY_TAG_CROSSWALK.items():
        assert e["status"] in _VALID_STATUSES, (tag, e["status"])


def test_frameworks_left_use_case_dimension():
    for tag in ("ifrs9_staging", "frtb", "xva", "lgd", "lcr", "nsfr"):
        assert crosswalk(tag)["dimension"] != "use_case"


def test_crypto_is_product_context():
    assert crosswalk("crypto")["dimension"] == "product_context"


def test_authorised_push_payment_is_merged():
    assert crosswalk("authorised_push_payment")["status"] == "merged"


def test_crosswalk_lookup_returns_none_for_unknown_tag():
    assert crosswalk("not_a_real_tag") is None


def test_settlement_risk_tag_points_at_renamed_failure_risk_leaf():
    entry = crosswalk("settlement_risk")
    assert entry is not None and entry["dimension"] == "use_case"
    assert entry["target"] == "securities_services.custody.settlement_failure_risk"
