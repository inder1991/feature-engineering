from featuregen.overlay.upload.enrich import _MAX_DEFINITION_LEN, bounded_definition
from featuregen.overlay.upload.enrich_llm import (
    _MAX_LEN_DEFAULT,
    _column_profile_ok,
    _item_egress_ok,
)


def test_medium_definition_survives_whole():
    text = "The customer's primary settlement account identifier used for regulatory reporting. " * 4
    text = text.strip()[:500]
    out = bounded_definition(text, _MAX_DEFINITION_LEN)
    assert out == text  # under the bound, untouched


def test_long_definition_truncates_on_word_boundary():
    # Sized OFF the bound: at a fixed 2000 chars this stopped exercising truncation entirely once
    # the 2026-08-06 raise took _MAX_DEFINITION_LEN to 4000, and would have passed vacuously.
    text = "word " * (_MAX_DEFINITION_LEN // 2)
    assert len(text) > _MAX_DEFINITION_LEN, "the input must actually overflow the bound"
    out = bounded_definition(text, _MAX_DEFINITION_LEN)
    assert len(out) <= _MAX_DEFINITION_LEN
    assert not out.endswith("wor")  # no mid-token cut
    assert out.split()[-1] == "word"


def test_egress_allows_business_definition_up_to_the_definition_bound():
    """Bounds read from the constants, not restated. The 2026-08-06 zero-truncation raise moved
    them (600 -> 4000, 200 -> 1000) and every literal here would have silently inverted into an
    assertion that the NEW admissible length is refused."""
    meta = {"table": "t", "column": "c", "business_definition": "x" * _MAX_DEFINITION_LEN}
    assert _item_egress_ok(meta) is True
    meta_bad = {"table": "t", "column": "c", "business_definition": "x" * (_MAX_DEFINITION_LEN + 1)}
    assert _item_egress_ok(meta_bad) is False


def test_egress_other_scalars_stay_at_the_tighter_default_bound():
    assert _MAX_LEN_DEFAULT < _MAX_DEFINITION_LEN, "a definition must get the WIDER window"
    assert _item_egress_ok({"table": "t", "column": "c",
                            "term_name": "x" * _MAX_LEN_DEFAULT}) is True
    assert _item_egress_ok({"table": "t", "column": "c",
                            "term_name": "x" * (_MAX_LEN_DEFAULT + 1)}) is False


def test_column_profile_business_definition_up_to_the_definition_bound():
    assert _column_profile_ok({"column": "c", "type": "unknown",
                               "business_definition": "y" * _MAX_DEFINITION_LEN}) is True
    assert _column_profile_ok({"column": "c", "type": "unknown",
                               "business_definition": "y" * (_MAX_DEFINITION_LEN + 1)}) is False
