"""Deterministic release-gate CLI for governed recipe discovery coverage.

BR-9 rewrote the gate's QUALITY vocabulary. The old gate accepted "effective" coverage — primary
and supporting merged — which let a supporting tag stand in for owning an objective; that shortcut
is REMOVED. The gate now reads :data:`~featuregen.overlay.upload.taxonomy.coverage.COVERAGE_TIERS`
and the authored targets file, and passes only when:

* every :data:`TARGET_LEAVES` release anchor is tier ``AUTHORED_PRIMARY``;
* every leaf without a primary recipe (``AUTHORED_SUPPORTING`` or ``ZERO``, not intentionally
  empty) has an EXPLICIT backlog row naming the release increment that owns it — a gap is
  acceptable only as declared, scheduled debt;
* every ``INTENTIONALLY_EMPTY`` leaf carries an owner and a rationale in the targets file.

Legacy-derived applicability and the executable/conceptual split are REPORTED (they are the debt
Task 17 retires and the honesty BR-8's contract renders) but do not gate this release: failing the
build for debt the plan already schedules would only teach people to stop running the audit.
"""
from __future__ import annotations

import json
from pathlib import Path

from featuregen.overlay.upload.taxonomy.coverage import coverage_report

TARGET_LEAVES = (
    "credit.monitoring.obligor",
    "fraud.merchant_fraud",
    "treasury_alm.deposit_runoff_forecasting",
    "treasury_alm.net_interest_margin",
)

#: The authored coverage schedule this gate cross-checks. Resolved from the repo root so the CLI
#: answers identically from any working directory.
COVERAGE_TARGETS_PATH = (
    Path(__file__).resolve().parents[5] / "docs" / "architecture"
    / "banking-recipe-coverage-targets.json")


def load_coverage_targets(path: Path = COVERAGE_TARGETS_PATH) -> dict:
    return json.loads(path.read_text())


def evaluate_release_gate(targets: dict | None = None) -> tuple[dict, bool]:
    report = coverage_report()
    targets = load_coverage_targets() if targets is None else targets
    tiers = report["coverage_tier_by_leaf"]

    backlog_rows = {row["leaf"] for row in targets.get("backlog", ())}
    empty_rows = {row["leaf"]: row for row in targets.get("intentionally_empty", ())}

    unowned_gaps = sorted(
        leaf for leaf, tier in tiers.items()
        if tier in ("AUTHORED_SUPPORTING", "ZERO") and leaf not in backlog_rows)
    undocumented_empty = sorted(
        leaf for leaf, tier in tiers.items()
        if tier == "INTENTIONALLY_EMPTY"
        and not (empty_rows.get(leaf, {}).get("owner", "").strip()
                 and empty_rows.get(leaf, {}).get("rationale", "").strip()))

    tier_counts: dict[str, int] = {}
    for tier in tiers.values():
        tier_counts[tier] = tier_counts.get(tier, 0) + 1

    passed = (
        all(tiers[leaf] == "AUTHORED_PRIMARY" for leaf in TARGET_LEAVES)
        and unowned_gaps == []
        and undocumented_empty == []
    )
    return {
        "gate": "owned_discovery_coverage",
        "passed": passed,
        "target_leaves": list(TARGET_LEAVES),
        "target_leaf_tiers": {leaf: tiers[leaf] for leaf in TARGET_LEAVES},
        "tier_counts": tier_counts,
        "unowned_gaps": unowned_gaps,
        "undocumented_intentionally_empty": undocumented_empty,
        # Reported, not gated: the honesty counters.
        "executable_covered_leaves": report["executable_covered_leaves"],
        "conceptual_only_covered_leaf_count": len(report["conceptual_only_covered_leaves"]),
        "legacy_inferred_leaf_count": len(report["legacy_inferred_leaves"]),
        "legacy_derived_recipe_count": report["legacy_derived_recipe_count"],
    }, passed


def main() -> int:
    result, passed = evaluate_release_gate()
    print(json.dumps(result, sort_keys=True, separators=(",", ":")))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
