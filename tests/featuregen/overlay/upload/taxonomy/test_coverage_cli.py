from __future__ import annotations

from featuregen.overlay.upload.taxonomy.coverage_cli import (
    TARGET_LEAVES,
    evaluate_release_gate,
)


def test_minimum_discovery_coverage_gate_passes_non_vacuously() -> None:
    result, passed = evaluate_release_gate()
    assert passed is True
    assert result["gate"] == "minimum_discovery_coverage"
    assert result["active_zero_effective"] == []
    assert set(result["authored_primary_by_leaf"]) == set(TARGET_LEAVES)
    assert all(result["authored_primary_by_leaf"].values())
    assert set(result["coverage_quality_tier_by_leaf"].values()) == {"MINIMUM_ANCHOR"}
