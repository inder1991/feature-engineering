"""BR-9 — the owned-coverage gate and the honest tier accounting.

The gate's quality vocabulary is :data:`COVERAGE_TIERS`, never "effective" (primary+supporting
merged) coverage: that shortcut let a supporting tag stand in for owning an objective and is
tested REMOVED here. Every gap must be scheduled debt (a backlog row naming its increment) or a
documented intentionally-empty leaf (owner + rationale) — and the executable/conceptual split
stays pinned to the truth that NOTHING is executable-covered until a recipe passes the gold gate.
"""
from __future__ import annotations

from featuregen.overlay.upload.taxonomy.coverage import (
    COVERAGE_TIERS,
    coverage_report,
    coverage_tier,
)
from featuregen.overlay.upload.taxonomy.coverage_cli import (
    TARGET_LEAVES,
    evaluate_release_gate,
    load_coverage_targets,
)


def test_owned_discovery_coverage_gate_passes_non_vacuously() -> None:
    result, passed = evaluate_release_gate()
    assert passed is True
    assert result["gate"] == "owned_discovery_coverage"
    assert set(result["target_leaf_tiers"]) == set(TARGET_LEAVES)
    assert set(result["target_leaf_tiers"].values()) == {"AUTHORED_PRIMARY"}
    assert result["unowned_gaps"] == [] and result["undocumented_intentionally_empty"] == []
    # The shortcut is GONE: nothing effective-shaped reaches the gate's output.
    assert "active_zero_effective" not in result
    # ...and BR-17 drove the debt to ZERO: the active registry declares every applicability
    # (nothing inferred), while nothing is executable-covered until the gold gates run.
    assert result["legacy_derived_recipe_count"] == 0
    assert result["legacy_inferred_leaf_count"] == 0
    assert result["executable_covered_leaves"] == []


def test_the_tier_accounting_is_total_and_honest() -> None:
    report = coverage_report()
    tiers = report["coverage_tier_by_leaf"]
    assert set(tiers) == set(report["by_leaf"]) and set(tiers.values()) <= set(COVERAGE_TIERS)
    # Every intentionally-empty leaf says so; a leaf whose only primaries are legacy-derived is
    # LEGACY_INFERRED debt, never AUTHORED coverage; supporting-only stays supporting.
    for leaf, tier in tiers.items():
        if tier == "AUTHORED_PRIMARY":
            assert report["authored_primary_by_leaf"][leaf]
        if tier == "LEGACY_INFERRED":
            assert report["legacy_primary_by_leaf"][leaf]
            assert not report["authored_primary_by_leaf"][leaf]
        if tier == "AUTHORED_SUPPORTING":
            assert not report["by_leaf"][leaf] and report["secondary_by_leaf"][leaf]


def test_nothing_is_executable_covered_until_the_gold_gate_is_proven() -> None:
    """The BR-9 acceptance, over the ACTIVE registry since BR-17: the two reviewed expectation
    anchors rest at FORMULA_AUTHORABLE (BR-7's fold — gold unproven), which is deliberately NOT
    executable; everything else is honestly blocked or conceptual. UNASSESSED no longer exists
    in release coverage — the active registry cannot construct it."""
    report = coverage_report()
    readiness = report["execution_readiness_by_recipe"]
    assert readiness["merchant_mcc_diversity"] == "FORMULA_AUTHORABLE"
    assert readiness["obligor_facility_count"] == "FORMULA_AUTHORABLE"
    assert set(readiness.values()) == {"CONCEPTUAL_ONLY", "FORMULA_BLOCKED",
                                       "FORMULA_AUTHORABLE"}
    assert report["executable_covered_leaves"] == []
    assert all(not v for v in report["executable_primary_by_leaf"].values())


def test_a_supporting_tag_can_never_move_primary_coverage() -> None:
    """The differential seam: `coverage_tier` is ordered so supporting participates only after
    every primary question is answered. Flipping `supporting` NEVER changes a primary tier, and
    supporting alone never rises above AUTHORED_SUPPORTING."""
    for intentionally_empty in (False, True):
        for authored in (False, True):
            for legacy in (False, True):
                without = coverage_tier(intentionally_empty=intentionally_empty,
                                        authored_primary=authored, legacy_primary=legacy,
                                        supporting=False)
                with_tag = coverage_tier(intentionally_empty=intentionally_empty,
                                         authored_primary=authored, legacy_primary=legacy,
                                         supporting=True)
                if without in ("INTENTIONALLY_EMPTY", "AUTHORED_PRIMARY", "LEGACY_INFERRED"):
                    assert with_tag == without
                else:
                    assert (without, with_tag) == ("ZERO", "AUTHORED_SUPPORTING")


def test_every_gap_is_scheduled_and_every_empty_leaf_is_documented() -> None:
    """The targets FILE is load-bearing: a backlog row names the increment that owns each gap,
    and an intentionally-empty leaf without owner+rationale fails the gate (proven by doctoring
    the loaded targets, not by wrecking the real file)."""
    targets = load_coverage_targets()
    report = coverage_report()
    gaps = {leaf for leaf, tier in report["coverage_tier_by_leaf"].items()
            if tier in ("AUTHORED_SUPPORTING", "ZERO")}
    backlog = {row["leaf"] for row in targets["backlog"]}
    assert gaps == backlog                      # nothing unscheduled, nothing stale
    assert all(row["target_increment"].startswith("BR-") for row in targets["backlog"])
    empty = {row["leaf"] for row in targets["intentionally_empty"]}
    assert empty == set(report["empty_intentional"])

    # The refusal paths, against doctored targets:
    doctored = {**targets, "backlog": [r for r in targets["backlog"]
                                       if r["leaf"] != "fraud.card_fraud"]}
    result, passed = evaluate_release_gate(doctored)
    assert passed is False and result["unowned_gaps"] == ["fraud.card_fraud"]

    doctored = {**targets, "intentionally_empty": [
        {**row, "owner": ""} if row["leaf"] == "aml_cft.tbml" else row
        for row in targets["intentionally_empty"]]}
    result, passed = evaluate_release_gate(doctored)
    assert passed is False
    assert result["undocumented_intentionally_empty"] == ["aml_cft.tbml"]
