"""Profile-aware validation (spec §U): the SAME `validate_rows`, made profile-conditional on `type`.

These tests lock the asymmetry that drives the unified-ingestion model — a glossary's absent physical
type (`type="unknown"`) is a readiness gap under the glossary profile but a quarantine under a
type-attesting (technical) profile or the default (no profile = today's behaviour). Every other check
is unchanged.
"""
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE, CanonicalRow, validate_rows
from featuregen.overlay.upload.source_profile import FTR_GLOSSARY_PROFILE, TECHNICAL_CSV_PROFILE


def _glossary_row(**kw):
    base = dict(source="ftr", table="comp_repos_dly", column="cust_name",
                type=UNKNOWN_TYPE, definition="the customer name")
    base.update(kw)
    return CanonicalRow(**base)


def test_glossary_unknown_type_passes_under_glossary_profile():
    vr = validate_rows([_glossary_row()], "ftr", profile=FTR_GLOSSARY_PROFILE)
    assert len(vr.good) == 1
    assert vr.quarantined == []


def test_literal_unknown_type_validates_under_technical_profile():
    # MINOR-6 (technical-path parity): the `UNKNOWN_TYPE` sentinel is meaningful ONLY under a
    # non-type-attesting (glossary) profile. Under a type-attesting profile a literal "unknown" is a
    # PRESENT type value (pre-branch behaviour) — it validates, it does NOT quarantine.
    vr = validate_rows([_glossary_row()], "ftr", profile=TECHNICAL_CSV_PROFILE)
    assert len(vr.good) == 1
    assert vr.quarantined == []


def test_literal_unknown_type_validates_with_no_profile():
    # MINOR-6: no profile == today's default. A technical row literally carrying `type="unknown"`
    # validates (pre-branch behaviour); only an EMPTY type is a real missing type.
    vr = validate_rows([_glossary_row()], "ftr")
    assert len(vr.good) == 1
    assert vr.quarantined == []


def test_technical_row_with_empty_type_still_quarantines():
    # Today's behaviour preserved: a real missing type (empty) always quarantines.
    r = CanonicalRow("deposits", "accounts", "balance", "")
    for profile in (None, TECHNICAL_CSV_PROFILE):
        vr = validate_rows([r], "deposits", profile=profile)
        assert vr.good == []
        assert "type" in vr.quarantined[0].message


def test_glossary_profile_still_quarantines_missing_identity():
    # Only the `type` requirement relaxes — a row with no table/column has no resolvable identity.
    r = CanonicalRow("ftr", "", "", UNKNOWN_TYPE, definition="orphan term")
    vr = validate_rows([r], "ftr", profile=FTR_GLOSSARY_PROFILE)
    assert vr.good == []
    assert len(vr.quarantined) == 1
    assert "table" in vr.quarantined[0].message and "column" in vr.quarantined[0].message


def test_glossary_profile_still_quarantines_source_mismatch():
    vr = validate_rows([_glossary_row(source="other")], "ftr", profile=FTR_GLOSSARY_PROFILE)
    assert vr.good == []
    assert "does not match" in vr.quarantined[0].message


def test_glossary_profile_still_enforces_sensitivity_validity():
    vr = validate_rows([_glossary_row(sensitivity="bogus")], "ftr", profile=FTR_GLOSSARY_PROFILE)
    assert vr.good == []
    assert "sensitivity" in vr.quarantined[0].message


def test_relaxation_only_drops_the_requirement_not_a_real_type():
    # If a glossary somehow carries a physical type, it is still accepted (the check is dropped, not
    # inverted).
    vr = validate_rows([_glossary_row(type="varchar")], "ftr", profile=FTR_GLOSSARY_PROFILE)
    assert len(vr.good) == 1
    assert vr.quarantined == []


def test_technical_profile_valid_row_passes():
    r = CanonicalRow("deposits", "accounts", "balance", "numeric")
    vr = validate_rows([r], "deposits", profile=TECHNICAL_CSV_PROFILE)
    assert len(vr.good) == 1
    assert vr.quarantined == []
