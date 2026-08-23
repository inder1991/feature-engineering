"""S1A-3 — the governed variant identity model.

The contract under test: a variant of a governed definition is identified by SEPARATE fields —
never by mangling the canonical registry id — because `semantic_option_decision.source_definition_id`
is consumed verbatim as a registry key. See the module docstring of `governed_identity`.
"""
from __future__ import annotations

import hashlib
import re
import subprocess
import sys

import pytest

from featuregen.overlay.upload.contract.governed_identity import GovernedVariantIdentityV1

_CANONICAL = "recipe.balance_volatility.v2"
_REQUEST_A = "a" * 64
_REQUEST_B = "c" * 64
_PLAN_A = "b" * 64
_PLAN_B = "d" * 64


def _identity(**overrides) -> GovernedVariantIdentityV1:
    fields: dict[str, str] = {
        "canonical_definition_id": _CANONICAL,
        "definition_origin": "recipe_v2",
        "planning_request_hash": _REQUEST_A,
        "physical_plan_content_hash": _PLAN_A,
    }
    fields.update(overrides)
    return GovernedVariantIdentityV1(**fields)


def test_two_parameter_variants_of_one_recipe_are_distinct_variants():
    """Two parameter variants of ONE recipe differ only in planning_request_hash (the full,
    field-exhaustive request identity that INCLUDES parameter_values)."""
    ninety_day = _identity(planning_request_hash=_REQUEST_A)
    thirty_day = _identity(planning_request_hash=_REQUEST_B)

    assert ninety_day.governed_variant_id != thirty_day.governed_variant_id
    # ...and both remain the SAME governed definition.
    assert ninety_day.canonical_definition_id == thirty_day.canonical_definition_id == _CANONICAL


def test_two_physical_plans_of_one_request_are_distinct_variants():
    """physical_plan_id is a TRUNCATED 16-hex display id; the identity carries the full content
    hash, so two physical realisations of one request are two variants."""
    first = _identity(physical_plan_content_hash=_PLAN_A)
    second = _identity(physical_plan_content_hash=_PLAN_B)

    assert first.governed_variant_id != second.governed_variant_id
    assert first.planning_request_hash == second.planning_request_hash


def test_canonical_definition_id_is_recoverable_verbatim_never_parsed():
    """The registry id is read off the FIELD. It is never recoverable from — and never
    embedded in — the variant id, so no consumer can be tempted to parse one out."""
    identity = _identity()

    assert identity.canonical_definition_id == _CANONICAL
    assert _CANONICAL not in identity.governed_variant_id
    # The variant id is opaque: prefix + digest only, nothing to split on.
    assert identity.governed_variant_id.count("_") == 1


def test_governed_variant_id_is_a_full_digest_with_the_gvar_prefix():
    identity = _identity()

    assert re.fullmatch(r"gvar_[0-9a-f]{64}", identity.governed_variant_id)


def test_governed_variant_id_is_deterministic():
    identity = _identity()

    assert identity.governed_variant_id == identity.governed_variant_id
    # Two separately constructed, field-equal identities agree.
    assert identity.governed_variant_id == _identity().governed_variant_id


def test_digest_material_order_is_contractual():
    """Later tasks consume this identity verbatim: pin the material ORDER independently of the
    implementation so a reorder cannot slip through as 'still a valid digest'."""
    identity = _identity(parameter_binding_hash="pb-hash")

    material = "|".join((_CANONICAL, "recipe_v2", _REQUEST_A, _PLAN_A, "pb-hash", "1"))
    expected = "gvar_" + hashlib.sha256(material.encode()).hexdigest()

    assert identity.governed_variant_id == expected


def test_optional_fields_default_to_the_documented_values():
    identity = _identity()

    assert identity.parameter_binding_hash == ""
    assert identity.plan_envelope_version == "1"


def test_definition_origin_is_a_closed_pair():
    assert _identity(definition_origin="recipe_v2").definition_origin == "recipe_v2"
    assert _identity(definition_origin="llm_intent").definition_origin == "llm_intent"

    with pytest.raises(ValueError, match="definition_origin"):
        _identity(definition_origin="recipe")


@pytest.mark.parametrize(
    "field",
    ["canonical_definition_id", "planning_request_hash", "physical_plan_content_hash"],
)
@pytest.mark.parametrize("blank", ["", "   "])
def test_identity_bearing_fields_may_not_be_blank(field: str, blank: str):
    with pytest.raises(ValueError, match=field):
        _identity(**{field: blank})


def test_module_is_pure_no_planner_no_db_no_registries():
    """Binding constraint: importing the identity model must not drag in the planner, the DB
    layer, or the registries."""
    probe = (
        "import sys;"
        "import featuregen.overlay.upload.contract.governed_identity;"
        "banned=[m for m in sys.modules if m.startswith(("
        "'featuregen.overlay.upload.planner',"
        "'featuregen.db',"
        "'featuregen.overlay.upload.recipe_registry_v2',"
        "'featuregen.overlay.upload.templates',"
        "'psycopg'))];"
        "print(','.join(sorted(banned)))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=True,
    )

    assert result.stdout.strip() == ""
