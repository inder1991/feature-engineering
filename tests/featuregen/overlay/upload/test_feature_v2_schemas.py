"""Slice 3A-iv Task 1: the feature-gen v2 output schemas register and resolve as v1 aliases.

v1 stays permissive and semantic validation stays code-side in `_validate_idea` — v2 exists only to
stamp which INPUT contract egressed (spec §8), so the OUTPUT body must be byte-for-byte v1."""
from __future__ import annotations

from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.overlay.upload.enrich_llm import _SCHEMAS, register_enrichment_schemas

_FEATURE_IDS = ("feature_ideas", "feature_recipe", "leakage", "feature_set_rec")


def test_feature_v2_present_in_schemas_dict_as_v1_alias():
    for sid in _FEATURE_IDS:
        assert (sid, 1) in _SCHEMAS, f"{sid} v1 missing"
        assert (sid, 2) in _SCHEMAS, f"{sid} v2 not registered in _SCHEMAS"
        # Intentional alias: the SAME object, so the two versions can never drift.
        assert _SCHEMAS[(sid, 2)] is _SCHEMAS[(sid, 1)], f"{sid} v2 must be the v1 object"


def test_feature_v2_resolves_through_registry(db):
    register_enrichment_schemas(db)
    reg = DocumentSchemaRegistry(db)
    for sid in _FEATURE_IDS:
        v1 = reg.schema_for(sid, 1)
        v2 = reg.schema_for(sid, 2)
        assert v1 is not None, f"{sid} v1 did not register"
        assert v2 is not None, f"{sid} v2 did not register"
        assert v2 == v1, f"{sid} v2 body must equal v1 body"


def test_bootstrap_provider_compat_guard_still_passes(db):
    # register_enrichment_schemas asserts every schema projects to an Anthropic-compatible wire
    # schema before touching the DB; adding the v2 aliases must not trip that guard.
    register_enrichment_schemas(db)
