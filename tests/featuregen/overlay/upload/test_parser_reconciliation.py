from featuregen.overlay.upload.sample_parser import ParsedProfile, reconcile_profile


def _p(logical, semantic):
    return ParsedProfile(logical_representation=logical, semantic_type=semantic,
                         computational_type=None, sample_values=(), diagnostic=None)


def test_temporal_declared_type_withholds_identifier():
    # epoch-like integers sampled → parser said numeric_string/identifier; declared TIMESTAMP contradicts
    out = reconcile_profile(_p("numeric_string", "identifier"),
                            declared_type="timestamp", column="event_ts")
    assert out.semantic_type is None and out.logical_representation is None
    assert out.diagnostic and "timestamp" in out.diagnostic.lower()


def test_decimal_declared_type_withholds_identifier():
    out = reconcile_profile(_p("numeric_string", "identifier"),
                            declared_type="double", column="fee_amount")
    assert out.semantic_type is None and out.logical_representation is None
    assert out.diagnostic


def test_identifier_name_withholds_amount_measure():
    out = reconcile_profile(_p("decimal", "amount"),
                            declared_type="varchar", column="account_id")
    assert out.semantic_type is None
    assert out.diagnostic


def test_consistent_profile_is_unchanged():
    out = reconcile_profile(_p("decimal", "amount"),
                            declared_type="decimal", column="fee_amount")
    assert out.logical_representation == "decimal" and out.semantic_type == "amount"
    assert out.diagnostic is None


def test_unknown_declared_type_is_permissive():
    out = reconcile_profile(_p("numeric_string", "identifier"),
                            declared_type="unknown", column="cust_ref")
    assert out.semantic_type == "identifier"  # no declared signal to contradict
