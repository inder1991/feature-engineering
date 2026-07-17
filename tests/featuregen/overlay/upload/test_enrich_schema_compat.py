"""Every enrichment schema must project to an Anthropic-compatible schema, and registration must
refuse an incompatible one. SDK-free — runs in CI (the projection is pure)."""
import pytest

from featuregen.intake.schema_projection import project_for_anthropic, provider_incompatibilities
from featuregen.overlay.upload.enrich_llm import _SCHEMAS


def test_every_enrichment_schema_projects_clean():
    for (name, ver), schema in _SCHEMAS.items():
        projected = project_for_anthropic(schema)
        assert provider_incompatibilities(projected) == [], f"{name} v{ver} still incompatible"
        # structure preserved: top-level required/properties survive
        if "properties" in schema:
            assert set(projected["properties"]) == set(schema["properties"])


def test_registration_rejects_incompatible_schema(db, monkeypatch):
    from featuregen.overlay.upload import enrich_llm
    poisoned = dict(_SCHEMAS)
    poisoned[("overlay_bad", 1)] = {"type": "object", "properties": {"x": {"maxLength": 3}}}
    monkeypatch.setattr(enrich_llm, "_SCHEMAS", poisoned)
    with pytest.raises(ValueError):
        enrich_llm.register_enrichment_schemas(db)
