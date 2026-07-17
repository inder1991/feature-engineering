from featuregen.overlay.upload.passc.identifiers import (
    ColMeta, is_join_key_eligible, normalized_identifier_concept)


def _c(**kw):
    b = dict(object_ref="src::public.t.c", table="t", column="c", data_type="text", term_name="",
             term_type="", concept="", synonyms="", bian_leaf="", fibo_leaf="", table_entity="",
             column_entity="", data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


def test_foracid_and_ref_num_eligible():
    assert is_join_key_eligible(_c(column="foracid", term_name="Customer Account Number", term_type="Dimension"))
    assert is_join_key_eligible(_c(column="ref_num", term_name="Reference Number"))     # _is_id_like catches _num


def test_negative_filter_fields_never_eligible():
    assert not is_join_key_eligible(_c(column="cust_name", term_name="Customer Name", concept="name"))
    assert not is_join_key_eligible(_c(column="tran_amt", term_name="Transaction Amount", term_type="Measure"))


def test_measure_term_type_alone_blocks_an_otherwise_id_like_column():
    # The term_type gate must act on its own: "settlement_id" passes the id-suffix heuristic and
    # "Settlement Total" trips no negative token, so ONLY term_type separates these two outcomes.
    assert is_join_key_eligible(_c(column="settlement_id", term_name="Settlement Total", term_type="measure")) is False
    assert is_join_key_eligible(_c(column="settlement_id", term_name="Settlement Total", term_type="dimension")) is True


def test_word_boundary_negatives_do_not_trip_real_ids():
    # "Mandate Reference" contains substring "date"; "Corporate Account Number" contains "rate" — both are IDs
    assert is_join_key_eligible(_c(column="mandate_ref", term_name="Mandate Reference"))
    assert is_join_key_eligible(_c(column="corp_acct_no", term_name="Corporate Account Number"))


def test_concept_normalization_folds_synonyms():
    a = normalized_identifier_concept(_c(column="cif_id", term_name="Customer Information File Identifier"))
    b = normalized_identifier_concept(_c(column="cif", term_name="Customer Information File Identifier", synonyms="CIF"))
    assert a and a == b
    assert normalized_identifier_concept(_c(column="foracid", term_name="Customer Account Number")) != a
