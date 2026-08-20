"""`ExpectedOutput` is one shared, immutable contract — V1, V2 and V3 all use it.

It lived in `schema.py` and both v2 and v3 built that exact object anyway, declaring the field as
`object | None` purely because the type sat in a module named for V1. That looseness is what let
`output_intent_v2` read it with `getattr(..., None)`: a renamed or missing field became a silent
`unit=None` while `authored_expectation_present` stayed True, and `AuthoredOutputIntentV2` does not
refuse that shape (it validates only the converse — values without an expectation). So an unreadable
expectation was reported as an author who declared nothing.

The ruling was to extract it EXACTLY, not rename or loosely copy it. These tests are that ruling's
teeth:

1. **The hashes do not move**, byte for byte, on BOTH wire versions.
2. **Unit and currency survive** parsing and reach the sealed intent.
3. **A missing or renamed field fails CLOSED**, rather than becoming `unit=None`.
4. **The golden fixtures are stable.**
"""
from __future__ import annotations

import dataclasses
import json
import pathlib

import pytest
from tests.featuregen.materialize.test_admission_v2_s13 import _raw

from featuregen.formula.canonical_v2 import proposal_content_hash_v2
from featuregen.formula.canonical_v3 import proposal_content_hash_v3
from featuregen.formula.output_intent_v2 import derive_output_intent_v2
from featuregen.formula.parse_v2 import parse_proposal_v2
from featuregen.formula.parse_v3 import parse_proposal_v3
from featuregen.formula.schema import ExpectedOutput as ExpectedOutputV1Import
from featuregen.formula.schema_leaves import ExpectedOutput
from featuregen.formula.schema_v3 import FORMULA_SCHEMA_VERSION_V3

GOLD_V2 = pathlib.Path("tests/featuregen/formula/gold_v2")

#: Captured from the SHIPPED code before `ExpectedOutput` moved, over a real gold proposal carrying
#: a non-null expectation. Pinned as literals rather than recomputed, because a test that derives
#: both sides of its own equality proves only that the code agrees with itself.
V2_HASH_WITH_EXPECTED_OUTPUT = (
    "d0ee93e68effab676b6dacfcd9b4cc18aa1df46646cd5cc8dab20f59c2d6c6d8")
V3_HASH_WITH_EXPECTED_OUTPUT = (
    "f344e94409370cbc8dca15160bc587da474555a18e582d093d277eec6adf420e")

EXPECTATION = {"output_type": "decimal", "unit": "monetary", "currency": "AED"}


def _gold_with_expectation() -> dict:
    raw = json.loads((GOLD_V2 / "01_avg_txn_amt_90d.json").read_text())
    raw = raw.get("proposal", raw)
    raw["expected_output"] = dict(EXPECTATION)
    return raw


# ══ 1. THE HASHES DO NOT MOVE ══════════════════════════════════════════════════════════════════
def test_the_V2_CONTENT_HASH_IS_BYTE_FOR_BYTE_UNCHANGED():
    """A non-null expected_output IS walked into `proposal_content_hash_v2` — the v2/v3
    canonicalizers are `dataclasses.fields`-driven and recurse into any dataclass — and that hash is
    folded into the sealed `formula_content_hashes`. Moving the type must therefore change nothing,
    and "nothing" means these exact bytes."""
    assert proposal_content_hash_v2(
        parse_proposal_v2(_gold_with_expectation())) == V2_HASH_WITH_EXPECTED_OUTPUT


def test_the_V3_CONTENT_HASH_IS_BYTE_FOR_BYTE_UNCHANGED():
    raw = _gold_with_expectation()
    raw["formula_schema_version"] = FORMULA_SCHEMA_VERSION_V3
    assert proposal_content_hash_v3(
        parse_proposal_v3(raw)) == V3_HASH_WITH_EXPECTED_OUTPUT


def test_the_GOLDEN_FIXTURES_ARE_STABLE():
    """Every gold_v2 proposal still parses and hashes. The fixtures all carry a NULL
    expected_output, which is exactly why they could not have caught a regression in this field —
    stated so nobody reads their passing as coverage of the tests above."""
    hashed = 0
    for doc in sorted(GOLD_V2.glob("*.json")):
        raw = json.loads(doc.read_text())
        raw = raw.get("proposal", raw)
        try:
            parsed = parse_proposal_v2(raw)
        except Exception:
            continue                      # the deliberately-invalid fixtures
        assert parsed.expected_output is None, f"{doc.name} now carries an expectation"
        proposal_content_hash_v2(parsed)
        hashed += 1
    assert hashed == 26


# ══ 2. UNIT AND CURRENCY SURVIVE ═══════════════════════════════════════════════════════════════
def test_ONE_SHARED_TYPE_not_three():
    """V1's import and the leaves' export are the SAME object. Two structurally-identical
    ExpectedOutput classes would hash the same and compare unequal — the exact trap that made a V2
    aggregate take the wrong renderer branch."""
    assert ExpectedOutputV1Import is ExpectedOutput


def test_the_PARSED_EXPECTATION_IS_TYPED_not_a_bare_object():
    parsed = parse_proposal_v2(_gold_with_expectation())
    assert isinstance(parsed.expected_output, ExpectedOutput)
    assert (parsed.expected_output.unit, parsed.expected_output.currency) == ("monetary", "AED")


def test_UNIT_AND_CURRENCY_REACH_THE_SEALED_INTENT():
    """The whole point of the field: what the author said the number means must survive to the
    intent, not be dropped somewhere between parsing and sealing."""
    raw = _gold_with_expectation()
    raw["formula_schema_version"] = FORMULA_SCHEMA_VERSION_V3
    intent = derive_output_intent_v2(parse_proposal_v3(raw), proposal_hash="sha256:test")

    assert intent.authored_expectation_present is True
    assert intent.unit == "monetary"
    assert intent.target_currency == "AED"


# ══ 3. A MISSING OR RENAMED FIELD FAILS CLOSED ═════════════════════════════════════════════════
def test_AN_UNREADABLE_EXPECTATION_RAISES_rather_than_reporting_unit_None():
    """The defect this ruling closes, reproduced against the fix.

    Before: `getattr(expectation, "unit", None)` turned a foreign shape into
    `unit=None, target_currency=None` with `authored_expectation_present=True` — an expectation that
    exists and carries nothing, which is indistinguishable from an author who declared nothing and
    which `AuthoredOutputIntentV2` does not refuse.
    """
    raw = _gold_with_expectation()
    raw["formula_schema_version"] = FORMULA_SCHEMA_VERSION_V3
    parsed = parse_proposal_v3(raw)

    renamed = dataclasses.make_dataclass("RenamedOutput", [("units", str), ("ccy", str)],
                                         frozen=True)
    forged = dataclasses.replace(parsed, expected_output=renamed("monetary", "AED"))
    with pytest.raises(TypeError, match="present-and-empty"):
        derive_output_intent_v2(forged, proposal_hash="sha256:test")


def test_a_MISSING_FIELD_is_refused_at_construction():
    """`ExpectedOutput` is frozen and slotted, so an incomplete one cannot be built at all —
    the failure lands where the object is assembled rather than where it is read."""
    with pytest.raises(TypeError):
        ExpectedOutput(output_type="decimal", unit="monetary")     # type: ignore[call-arg]


def test_NO_EXPECTATION_AT_ALL_IS_STILL_A_CLEAN_ABSENCE():
    """The discriminator: declaring nothing is legitimate and must stay distinguishable from
    declaring something unreadable."""
    intent = derive_output_intent_v2(
        parse_proposal_v3({**_raw(), "formula_schema_version": FORMULA_SCHEMA_VERSION_V3}),
        proposal_hash="sha256:test")
    assert intent.authored_expectation_present is False
    assert intent.unit is None and intent.target_currency is None
