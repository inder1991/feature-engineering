"""A response violating a STRIPPED constraint is still rejected by canonical validation. The wire
projection drops `maxLength` for the provider, but the canonical schema — the source of truth for
validating the model's RESPONSE (the driver's `reg.validate`) — still enforces it. SDK-free."""
import pytest

from featuregen.documents.registry import DocumentSchemaRegistry, SchemaValidationError
from featuregen.overlay.upload.enrich_llm import _SCHEMAS


def test_canonical_validation_still_enforces_maxlength(db):
    reg = DocumentSchemaRegistry(db)
    schema = _SCHEMAS[("overlay_concept_batch", 1)]
    reg.register_schema("overlay_concept_batch", 1, schema, "test")
    too_long = {"results": [{"ref": "x" * 500, "concept": "amount"}]}
    with pytest.raises(SchemaValidationError):
        reg.validate("overlay_concept_batch", 1, too_long)
