"""C-C6a — the two types C-B3 is defined over, frozen.

Two properties carry the weight: S5's acceptance that *"a compiler version bump leaves the
bound-formula hash unchanged"*, and C-B3's that its own type is *"constructible from hashes alone"*.
"""
from __future__ import annotations

import dataclasses

import pytest

from featuregen.formula.output_authority_v2 import FormulaOutputPolicyV2
from featuregen.formula.schema_leaves import AdditivityClass
from featuregen.materialize.bound_formula_v2 import (
    BoundFormulaRevisionV2,
    ExecutableOutputPolicyV2,
    bound_formula_hash_v2,
    executable_output_hash_v2,
)
from featuregen.materialize.physical_types_v2 import PHYSICAL_TYPE_POLICY_V2

CONVERSION = "currency_conversion:foundation-base-currency"


def _executable(**overrides) -> ExecutableOutputPolicyV2:
    kwargs = dict(physical_type="DECIMAL(38,8)", unit="monetary", currency_code="AED",
                  conversion_policy_ref="", output_additivity=AdditivityClass.ADDITIVE,
                  nullable=False)
    kwargs.update(overrides)
    return ExecutableOutputPolicyV2(**kwargs)


def _bound(**overrides) -> BoundFormulaRevisionV2:
    kwargs = dict(formula_content_hash="sha256:formula", bound_input_set_hash="sha256:inputs",
                  environment_id="hdfc-local",
                  executable_output_hash=executable_output_hash_v2(_executable()),
                  compiler_version="1.4.2")
    kwargs.update(overrides)
    return BoundFormulaRevisionV2(**kwargs)


# ══ declared is not executable ═══════════════════════════════════════════════════════════════════
def test_the_executable_policy_holds_a_CURRENCY_not_a_declaration():
    """`FormulaOutputPolicyV2.currency` is `"converted:<ref>"` — a declaration. The executable type
    holds what the number IS, so "what currency is this column in" is never a policy reference."""
    declared = FormulaOutputPolicyV2(
        output_type="decimal", unit="monetary", currency=f"converted:{CONVERSION}",
        output_additivity=AdditivityClass.ADDITIVE, external_type_required=False)
    assert declared.currency.startswith("converted:")

    executable = _executable(currency_code="AED", conversion_policy_ref=CONVERSION)
    assert executable.currency_code == "AED"
    assert executable.conversion_policy_ref == CONVERSION
    assert executable.was_converted


@pytest.mark.parametrize("bad", [f"converted:{CONVERSION}", "fixed:AED", "aed", "AEDX", "A1D", "12"])
def test_a_DECLARATION_cannot_be_smuggled_into_the_currency_code(bad):
    with pytest.raises(ValueError, match="three-letter currency code"):
        _executable(currency_code=bad)


def test_a_conversion_with_NO_resulting_currency_is_refused():
    """A conversion whose result currency is unknown converted to nothing nameable."""
    with pytest.raises(ValueError, match="converted to nothing nameable"):
        _executable(unit="count", currency_code="", conversion_policy_ref=CONVERSION)


def test_a_monetary_column_with_no_currency_is_refused():
    """The unit says it is money and nothing says which money — it can be summed with nothing and
    compared to nothing."""
    with pytest.raises(ValueError, match="which money"):
        _executable(unit="monetary", currency_code="")


def test_a_non_monetary_column_needs_no_currency():
    assert _executable(unit="count", currency_code="", conversion_policy_ref="").currency_code == ""


def test_an_executable_policy_needs_a_physical_type():
    with pytest.raises(ValueError, match="no physical type"):
        _executable(physical_type="  ")


def test_conversion_is_IDENTITY_bearing():
    """A converted AED total and a natively-AED total are not the same column."""
    native = executable_output_hash_v2(_executable())
    converted = executable_output_hash_v2(_executable(conversion_policy_ref=CONVERSION))
    assert native != converted


def test_the_physical_type_policy_is_stamped_by_default():
    assert _executable().physical_type_policy == PHYSICAL_TYPE_POLICY_V2
    assert "physical_type_policy" in _executable().identity_payload()


# ══ S5's acceptance — the compiler is provenance ═════════════════════════════════════════════════
def test_A_COMPILER_BUMP_LEAVES_THE_BOUND_FORMULA_HASH_UNCHANGED():
    """S5's acceptance. Recompiling the same formula against the same inputs is ONE bound revision;
    letting the toolchain in would invalidate every downstream pin on a version bump that changed
    nothing about the computation."""
    assert bound_formula_hash_v2(_bound(compiler_version="1.4.2")) == bound_formula_hash_v2(
        _bound(compiler_version="9.0.0-rc1"))


def test_the_compiler_version_is_CARRIED_even_though_it_is_not_identity():
    """Excluded from identity, not dropped — an auditor still needs to know what built it."""
    assert _bound(compiler_version="1.4.2").compiler_version == "1.4.2"
    assert "compiler_version" not in _bound().identity_payload()


@pytest.mark.parametrize("field_name", [
    "formula_content_hash", "bound_input_set_hash", "environment_id", "executable_output_hash"])
def test_every_OTHER_field_is_identity_bearing(field_name):
    """Different inputs, or the same inputs in a different environment, is a different bound
    revision — those are the facts that decide what the computation reads."""
    assert bound_formula_hash_v2(_bound()) != bound_formula_hash_v2(
        _bound(**{field_name: "sha256:different"}))


@pytest.mark.parametrize("field_name", [
    "formula_content_hash", "bound_input_set_hash", "environment_id", "executable_output_hash"])
def test_a_blank_binding_fact_is_refused(field_name):
    with pytest.raises(ValueError, match="not bound to anything"):
        _bound(**{field_name: "   "})


# ══ C-B3 — constructible from HASHES alone ═══════════════════════════════════════════════════════
def test_the_bound_revision_names_its_output_policy_BY_HASH():
    """What lets `ExecutableFeatureRevisionV2` be defined over opaque content hashes here while S5
    produces the instances: no field holds the object."""
    types = {f.name: f.type for f in dataclasses.fields(BoundFormulaRevisionV2)}
    assert "ExecutableOutputPolicyV2" not in " ".join(str(t) for t in types.values())
    assert types["executable_output_hash"] == "str"
    assert set(types) == {"formula_content_hash", "bound_input_set_hash", "environment_id",
                          "executable_output_hash", "compiler_version"}


def test_a_bound_revision_is_constructible_from_STRINGS_only():
    revision = BoundFormulaRevisionV2(
        formula_content_hash="sha256:a", bound_input_set_hash="sha256:b",
        environment_id="env", executable_output_hash="sha256:c", compiler_version="v")
    assert bound_formula_hash_v2(revision)


def test_both_types_are_frozen():
    for cls in (ExecutableOutputPolicyV2, BoundFormulaRevisionV2):
        params = cls.__dataclass_params__
        assert params.frozen, cls.__name__
    with pytest.raises(dataclasses.FrozenInstanceError):
        _bound().environment_id = "other"  # type: ignore[misc]
