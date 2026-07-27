from __future__ import annotations

import hashlib
import json
from dataclasses import fields
from pathlib import Path

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.templates import (
    ALL_TEMPLATES,
    ground_template_outcome,
)

_FIXTURES = Path(__file__).with_name("fixtures")
_MANIFEST = _FIXTURES / "grounding_regression_manifest_v1.json"

# Closed, reviewed changes introduced after the frozen commit. Each key is a pre-existing recipe;
# new recipes are intentionally outside the before-side denominator.
_ALLOWED_DELTAS: dict[str, str] = {}


def _load_baseline() -> tuple[dict, dict]:
    manifest = json.loads(_MANIFEST.read_text())
    baseline_path = _FIXTURES / manifest["baseline_file"]
    raw = baseline_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == manifest["baseline_file_sha256"]
    baseline = json.loads(raw)
    assert baseline["baseline_commit"] == manifest["baseline_commit"]
    assert baseline["fixture_content_hash"] == manifest["fixture_content_hash"]
    assert baseline["recipe_count"] == manifest["registry_recipe_count"]
    fixture_bytes = json.dumps(
        baseline["fixtures"], sort_keys=True, separators=(",", ":")).encode()
    assert hashlib.sha256(fixture_bytes).hexdigest() == manifest["fixture_content_hash"]
    return manifest, baseline


def _current_outcome(conn, template, fixture: dict) -> dict:
    row_fields = {field.name for field in fields(CanonicalRow)}
    rows = [
        CanonicalRow(**{
            key: value for key, value in raw.items() if key in row_fields
        })
        for raw in fixture["rows"]
    ]
    concepts = {
        content_hash(row): concept_name
        for row, concept_name in zip(rows, fixture["concepts"], strict=True)
    }
    build_graph(conn, fixture["catalog_source"], rows, concepts=concepts)
    outcome = ground_template_outcome(
        conn,
        template,
        catalog_source=fixture["catalog_source"],
        roles=("pii_reader",),
    )
    feature = outcome.feature
    return {
        "status": outcome.status.value,
        "selected_grain": feature.grain_table if feature else None,
        "ordered_bound_refs": (
            [list(pair) for pair in feature.derives_pairs] if feature else []
        ),
        "binding_quality": (
            [resolution.resolution.value for resolution in feature.binding_resolutions]
            if feature else []
        ),
        "reason_codes": list(outcome.reason_codes),
    }


def test_full_pre_change_registry_has_no_unexplained_grounding_delta(db) -> None:
    _manifest, baseline = _load_baseline()
    current = {template.id: template for template in ALL_TEMPLATES}
    assert set(baseline["registry_ids"]) <= set(current)

    unexplained = {}
    observed_allowed = set()
    for recipe_id in baseline["registry_ids"]:
        actual = _current_outcome(
            db, current[recipe_id], baseline["fixtures"][recipe_id])
        expected = baseline["outcomes"][recipe_id]
        if actual != expected:
            if recipe_id in _ALLOWED_DELTAS:
                observed_allowed.add(recipe_id)
            else:
                unexplained[recipe_id] = {"before": expected, "after": actual}

    assert observed_allowed == set(_ALLOWED_DELTAS)
    assert unexplained == {}
