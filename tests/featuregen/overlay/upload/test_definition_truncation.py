from featuregen.overlay.upload.enrich import _MAX_DEFINITION_LEN, bounded_definition
from featuregen.overlay.upload.enrich_llm import _column_profile_ok, _item_egress_ok


def test_medium_definition_survives_whole():
    text = "The customer's primary settlement account identifier used for regulatory reporting. " * 4
    text = text.strip()[:500]
    out = bounded_definition(text, _MAX_DEFINITION_LEN)
    assert out == text  # <= 600, untouched


def test_long_definition_truncates_on_word_boundary():
    text = "word " * 400  # 2000 chars
    out = bounded_definition(text, _MAX_DEFINITION_LEN)
    assert len(out) <= _MAX_DEFINITION_LEN
    assert not out.endswith("wor")  # no mid-token cut
    assert out.split()[-1] == "word"


def test_egress_allows_business_definition_up_to_600():
    meta = {"table": "t", "column": "c", "business_definition": "x" * 600}
    assert _item_egress_ok(meta) is True
    meta_bad = {"table": "t", "column": "c", "business_definition": "x" * 601}
    assert _item_egress_ok(meta_bad) is False


def test_egress_other_scalars_still_capped_at_200():
    assert _item_egress_ok({"table": "t", "column": "c", "term_name": "x" * 201}) is False


def test_column_profile_business_definition_up_to_600():
    assert _column_profile_ok({"column": "c", "type": "unknown",
                               "business_definition": "y" * 600}) is True
    assert _column_profile_ok({"column": "c", "type": "unknown",
                               "business_definition": "y" * 601}) is False
