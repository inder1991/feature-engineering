"""Deterministic release-gate CLI for governed recipe discovery coverage."""
from __future__ import annotations

import json

from featuregen.overlay.upload.taxonomy.coverage import coverage_report

TARGET_LEAVES = (
    "credit.monitoring.obligor",
    "fraud.merchant_fraud",
    "treasury_alm.deposit_runoff_forecasting",
    "treasury_alm.net_interest_margin",
)


def evaluate_release_gate() -> tuple[dict, bool]:
    report = coverage_report()
    passed = (
        report["active_zero_effective"] == []
        and all(report["authored_primary_by_leaf"][leaf] for leaf in TARGET_LEAVES)
    )
    return {
        "gate": "minimum_discovery_coverage",
        "passed": passed,
        "target_leaves": list(TARGET_LEAVES),
        "active_zero_effective": report["active_zero_effective"],
        "authored_primary_by_leaf": {
            leaf: report["authored_primary_by_leaf"][leaf] for leaf in TARGET_LEAVES
        },
        "coverage_quality_tier_by_leaf": {
            leaf: report["coverage_quality_tier_by_leaf"][leaf] for leaf in TARGET_LEAVES
        },
    }, passed


def main() -> int:
    result, passed = evaluate_release_gate()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
