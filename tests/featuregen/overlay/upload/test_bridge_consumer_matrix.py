"""The bridge-reader boundary: discovery evidence is not production execution authority."""
from __future__ import annotations

from pathlib import Path

_ROOT = Path(__file__).parents[4]

# This is the maintained consumer matrix from the remediation design. Adding a consumer requires
# classifying it here; the production class is also guarded below against the two historical
# shortcuts that admitted a symmetric proposal as though it proved directional cardinality.
CONSUMER_MATRIX = {
    "search_asset_lineage_review": "available_identifier_links",
    "feature_suggestion_realization_construction": "available_identifier_links",
    "marked_sandbox_planner": "available_link_plus_provisional_realization_and_runtime_probe",
    "production_analysis_materialization": "executable_directional_realization_only",
    "contract_invalidation_reuse": "realization_revision_plus_dependency_snapshot",
}

_PRODUCTION_CONSUMERS = (
    "src/featuregen/analysis/execution.py",
    "src/featuregen/materialize/joins.py",
    "src/featuregen/materialize/expression_ir.py",
    "src/featuregen/materialize/ir.py",
    "src/featuregen/materialize/render/nodes_compute.py",
    "src/featuregen/materialize/render/nodes_join_gate.py",
    "src/featuregen/overlay/upload/contract/invalidation.py",
)


def test_consumer_matrix_names_every_required_reader_class() -> None:
    assert CONSUMER_MATRIX == {
        "search_asset_lineage_review": "available_identifier_links",
        "feature_suggestion_realization_construction": "available_identifier_links",
        "marked_sandbox_planner":
            "available_link_plus_provisional_realization_and_runtime_probe",
        "production_analysis_materialization": "executable_directional_realization_only",
        "contract_invalidation_reuse": "realization_revision_plus_dependency_snapshot",
    }


def test_production_consumers_never_import_available_link_authority() -> None:
    forbidden = ("active_bridges", "CrossCatalogLink.usable", ".usable")
    violations: list[str] = []
    for relative in _PRODUCTION_CONSUMERS:
        source = (_ROOT / relative).read_text()
        for token in forbidden:
            if token in source:
                violations.append(f"{relative}: {token}")
    assert violations == []


def test_production_paths_name_exact_realization_material() -> None:
    materialize = (_ROOT / "src/featuregen/materialize/ir.py").read_text()
    analysis = (_ROOT / "src/featuregen/analysis/execution.py").read_text()
    invalidation = (
        _ROOT / "src/featuregen/overlay/upload/contract/invalidation.py"
    ).read_text()
    assert "executable_bridge_realizations" in materialize
    assert "realization_revision_id" in analysis
    assert "dependency_snapshot_id" in analysis
    assert "bridge-realization-v2:" in invalidation
