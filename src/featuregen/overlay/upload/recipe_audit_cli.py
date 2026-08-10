"""BR-1 — the recipe-audit CLI: the debt report as JSON (for the baseline file and dashboards)
or human-readable text (for a terminal). Read-only; exit codes are the only side channel:

    0  clean against the requested gates
    1  --baseline given and a ratcheted counter regressed, or --strict given and any is nonzero

Usage:
    uv run python -m featuregen.overlay.upload.recipe_audit_cli --format json
    uv run python -m featuregen.overlay.upload.recipe_audit_cli --format text
    uv run python -m featuregen.overlay.upload.recipe_audit_cli \
        --baseline docs/architecture/banking-recipe-debt-baseline.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from featuregen.overlay.upload.recipe_audit import (
    RATCHETED_COUNTERS,
    audit_registry,
    compare_to_baseline,
    strict_violations,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Banking-recipe production-readiness debt audit")
    parser.add_argument("--format", choices=("json", "text"), default="text")
    parser.add_argument("--baseline", type=Path, default=None,
                        help="baseline JSON to ratchet against (exit 1 on any regression)")
    parser.add_argument("--strict", action="store_true",
                        help="require every ratcheted counter to be zero (BR-17's exit gate)")
    args = parser.parse_args(argv)

    report = audit_registry()
    exit_code = 0
    regressions: list[str] = []
    if args.baseline is not None:
        baseline = json.loads(args.baseline.read_text())
        regressions = compare_to_baseline(report, baseline["counters"])
        exit_code = 1 if regressions else exit_code
    violations = strict_violations(report) if args.strict else []
    exit_code = 1 if violations else exit_code

    if args.format == "json":
        print(json.dumps({
            "version": "banking-recipe-debt-baseline-v1",
            "counters": report.counters,
            "informational": report.informational,
            "examples": {k: list(v) for k, v in report.examples.items()},
            "regressions": regressions,
            "strict_violations": violations,
        }, indent=2, sort_keys=True))
    else:
        print("banking-recipe debt audit")
        print("  ratcheted counters:")
        for name in RATCHETED_COUNTERS:
            ids = report.examples.get(name, ())
            sample = f"  e.g. {', '.join(list(ids)[:3])}" if ids else ""
            print(f"    {name:45s} {report.counters.get(name, 0):5d}{sample}")
        print("  informational:")
        for name, value in sorted(report.informational.items()):
            print(f"    {name:45s} {value:5d}")
        for line in regressions:
            print(f"  REGRESSION: {line}")
        for line in violations:
            print(f"  STRICT: {line}")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
