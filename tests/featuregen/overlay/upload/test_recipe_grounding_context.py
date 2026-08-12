"""BR-2 — the two canonical forms live side by side and neither can drift silently.

canonical-recipe-v1 is PINNED by literal hash: a fixed probe template's canonical form must
produce the same digest forever, so any change to v1 canonicalization — a new Template/Need field,
a serialization tweak — fails HERE and becomes a conscious re-versioning decision instead of a
silent identity shift (the `Need.alternates` addition changed v1 hashes silently; this pin makes
the next such change loud). canonical-recipe-v2 is FIELD-EXHAUSTIVE by construction (fields()-
driven), proven by walking every nested dataclass.
"""
from __future__ import annotations

from dataclasses import fields, is_dataclass

from featuregen.overlay.upload.recipe_grounding_context import (
    assert_canonical_recipe_exhaustive,
    canonical_recipe_v2,
    canonical_recipe_v2_hash,
    canonical_template,
    content_hash,
)
from featuregen.overlay.upload.recipe_registry_v2 import PROBE_RECIPE
from featuregen.overlay.upload.templates import Need, Template

# The v1 pin. Breaking this is ALLOWED — but only as a deliberate decision that re-versions the
# canonical form, with the persisted-context consequences argued in the commit that does it.
_V1_PIN_HASH = "b8c9d69583a0f3ffb3e1b2e5a8c38d8778aa17fb952095849793a50eb09bca37"


def _v1_probe() -> Template:
    return Template(
        id="v1_hash_pin_probe", family="probe", intent="pin the v1 canonical form",
        needs=(Need("entity", "customer_id"), Need("event_ts", "event_timestamp")),
        params={"window": (30, 90)}, aggregation="count", additivity="additive", explain="H",
        use_cases=("retail_churn",), pit="events in (as_of − {window}d, as_of]")


def test_canonical_recipe_v1_is_byte_identical():
    assert content_hash(canonical_template(_v1_probe())) == _V1_PIN_HASH, (
        "canonical-recipe-v1 changed. If this is intentional (a new Template/Need field), "
        "re-version the canonical form and update this pin in the same reviewed commit — "
        "persisted grounding contexts key on it.")


def test_v1_exhaustiveness_seam_still_passes():
    assert_canonical_recipe_exhaustive()


def _every_field_serialized(value, serialized) -> None:
    if is_dataclass(value) and not isinstance(value, type):
        assert isinstance(serialized, dict)
        for f in fields(value):
            assert f.name in serialized, f"field {f.name!r} missing from canonical-recipe-v2"
            _every_field_serialized(getattr(value, f.name), serialized[f.name])
    elif isinstance(value, tuple):
        for item, item_serialized in zip(value, serialized, strict=True):
            _every_field_serialized(item, item_serialized)


def test_canonical_recipe_v2_serializes_every_field_of_every_nested_spec():
    body = canonical_recipe_v2(PROBE_RECIPE)
    assert body["version"] == "canonical-recipe-v2"
    _every_field_serialized(PROBE_RECIPE, body["definition"])


def test_the_two_canonical_forms_are_independent():
    """The v2 hash of the probe recipe and the v1 hash of any template can never collide by
    construction (different version keys inside the hashed body) — and computing one does not
    touch the other's machinery."""
    assert canonical_recipe_v2_hash(PROBE_RECIPE) != content_hash(
        canonical_template(_v1_probe()))
