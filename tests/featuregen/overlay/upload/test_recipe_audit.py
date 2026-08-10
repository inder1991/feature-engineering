"""BR-1 — the debt audit and CI ratchet: every identified defect machine-countable, new debt
impossible to add silently, intentional decreases welcome, strict mode ready for BR-17.

The committed baseline (docs/architecture/banking-recipe-debt-baseline.json) IS the program's
progress meter: 126 multi-measure recipes, 145 identity-collision recipes, exactly the two known
PIT defects, 155 formula-unassessed, 157 not-yet-V2. The ratchet test here is the CI gate the
plan's every later task is judged against — a task that claims to reduce debt must move a number
in that file downward, in the same commit.
"""
from __future__ import annotations

import json
from pathlib import Path

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.recipe_audit import (
    RATCHETED_COUNTERS,
    audit_registry,
    compare_to_baseline,
    dynamic_binding_audit,
    strict_violations,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES, Need, Template

_BASELINE_PATH = (Path(__file__).resolve().parents[4]
                  / "docs" / "architecture" / "banking-recipe-debt-baseline.json")


def _baseline() -> dict:
    return json.loads(_BASELINE_PATH.read_text())


def test_the_baseline_reproduces_the_reviewed_numbers():
    """The audit against the live registry must equal the committed baseline byte-for-byte —
    including the review's headline counts (126 / 145 / 1,122 / 33 / 14 / 2), now measured
    rather than estimated. A registry change without a same-commit baseline regeneration (or an
    intentional-decrease edit) fails HERE."""
    report = audit_registry()
    baseline = _baseline()
    assert report.counters == baseline["counters"]
    assert report.informational == baseline["informational"]
    # the review's headline numbers, pinned by value so a silent definition change is loud
    assert report.counters["multi_measure_recipes"] == 126
    assert report.counters["identity_collision_recipes"] == 145
    assert report.informational["parameter_combinations"] == 1122
    assert report.counters["downstream_derivation_recipes"] == 33
    assert report.informational["primary_objective_recipes"] == 14
    assert report.informational["formula_v1_authorable_recipes"] == 2


def test_the_two_known_pit_defects_are_pinned_and_are_the_only_two():
    report = audit_registry()
    assert report.examples["pit_unmatched_placeholder_recipes"] == (
        "merchant_mcc_diversity", "maturity_ladder_runoff") or sorted(
        report.examples["pit_unmatched_placeholder_recipes"]) == [
        "maturity_ladder_runoff", "merchant_mcc_diversity"]
    assert report.counters["pit_unmatched_placeholder_recipes"] == 2


def test_the_named_defect_examples_are_pinned():
    """One multi-measure/additivity conflict, one display collision — exact ids, per the plan."""
    report = audit_registry()
    assert "maturity_ladder_runoff" in report.examples[
        "multi_measure_additivity_conflict_recipes"], \
        "runoff_share vs runoff_amount is the canonical additivity conflict"
    assert "balance_trend" in report.examples["identity_collision_recipes"], \
        "its `measure` never reaches the rendered name — two features, one label"
    assert "product_breadth" in report.examples["missing_concept_admission_recipes"]


def _probe(**overrides) -> Template:
    base = dict(
        id="audit_probe", family="probe", intent="probe",
        needs=(Need("entity", "customer_id"),),
        params={}, aggregation="probe", additivity="n/a", explain="H",
        use_cases=("retail_churn",), pit="")
    base.update(overrides)
    return Template(**base)


def test_adding_a_multi_measure_legacy_recipe_fails_the_ratchet():
    grown = [*ALL_TEMPLATES, _probe(params={"measure": ("net_amount", "net_share")})]
    regressions = compare_to_baseline(audit_registry(grown), _baseline()["counters"])
    assert any(r.startswith("multi_measure_recipes") for r in regressions)
    assert any(r.startswith("legacy_recipes_not_in_v2") for r in regressions), \
        "any new legacy recipe is new debt under this program, multi-measure or not"


def test_adding_an_unmatched_pit_placeholder_fails_the_ratchet():
    grown = [*ALL_TEMPLATES, _probe(pit="knowable strictly before {window}", params={})]
    regressions = compare_to_baseline(audit_registry(grown), _baseline()["counters"])
    assert any(r.startswith("pit_unmatched_placeholder_recipes") for r in regressions)


def test_intentional_decreases_pass_the_ratchet():
    """The ratchet is one-directional: retiring a multi-measure recipe is progress, not a diff
    to explain."""
    shrunk = [t for t in ALL_TEMPLATES if t.id != "balance_trend"]
    assert compare_to_baseline(audit_registry(shrunk), _baseline()["counters"]) == []


def test_a_v2_replacement_reduces_the_migration_debt():
    report = audit_registry(v2_recipe_ids=("balance_trend",))
    assert report.counters["legacy_recipes_not_in_v2"] == 156
    assert "balance_trend" not in report.examples["legacy_recipes_not_in_v2"]


def test_a_counter_missing_from_the_baseline_is_itself_a_regression():
    """Silently unratcheted debt is how ratchets die."""
    crippled = {k: v for k, v in _baseline()["counters"].items()
                if k != "multi_measure_recipes"}
    regressions = compare_to_baseline(audit_registry(), crippled)
    assert any("no baseline recorded" in r for r in regressions)


def test_strict_mode_reports_every_nonzero_counter_and_stays_off_in_ci():
    """BR-17's exit gate exists and works TODAY (so BR-17 is a flag flip, not a build), and it is
    deliberately not wired into this suite as a failure — the debt is the starting point."""
    violations = strict_violations(audit_registry())
    assert violations, "strict mode must currently report the known debt"
    assert any(v.startswith("multi_measure_recipes") for v in violations)
    assert not strict_violations(
        audit_registry(templates=[], v2_recipe_ids=())), "an empty registry is strict-clean"


def test_dynamic_binding_audit_pins_an_ambiguous_binding(db):
    """The catalog-dependent half, proven on a fixture with a REAL tie (two event_timestamp
    columns) — dormancy_days grounds with a recorded tie and is named in the examples. Static CI
    never depends on this; it is the operator's per-catalog lens."""
    rows = [
        (CanonicalRow("auditbank", "transactions", "cust_ref", "integer", is_grain=True,
                      entity="Customer", definition="the customer"), "customer_id"),
        (CanonicalRow("auditbank", "transactions", "aaa_load_ts", "timestamp",
                      definition="warehouse load stamp"), "event_timestamp"),
        (CanonicalRow("auditbank", "transactions", "zzz_event_ts", "timestamp",
                      definition="when it happened"), "event_timestamp"),
    ]
    build_graph(db, "auditbank", [r for r, _ in rows],
                concepts={content_hash(r): c for r, c in rows})
    result = dynamic_binding_audit(db, catalog_source="auditbank", roles=("data_owner",))
    assert result["grounded"] >= 1
    assert "dormancy_days" in result["ambiguous_binding_recipes"]


def test_every_ratcheted_counter_is_present_in_the_report():
    report = audit_registry()
    assert set(RATCHETED_COUNTERS) <= set(report.counters)
