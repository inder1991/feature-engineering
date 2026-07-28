"""Accepting a SECOND source's glossary layout.

The FTR adapter demanded the exact 17-column FTR multiset. That was right when one source existed —
it stopped a mis-shaped file falling through to a reader that would mangle it — and wrong the moment
a second arrived: the real `CIB_Customer_Column_Mapping.csv` is missing 13 FTR headers and carries
20 of its own.

Contorting every new source into FTR's shape is not the answer, because the "extra" columns are the
most valuable thing in that file: `security_classification` plus `pci/aml/kyc/privacy` and seven
`pi_*` flags STATE sensitivity, where FTR forced us to derive it from concepts.

So the rule becomes: **a required core, recognised optional columns, and tolerated-but-REPORTED
extras.** The safety intent is preserved — a file that cannot be parsed is still refused, loudly —
while a source is allowed to carry its own columns.
"""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.ftr_adapter import (
    GLOSSARY_CORE_HEADERS,
    glossary_shape_error,
    is_ftr_glossary,
    is_glossary_mapping,
    unrecognised_headers,
)

_FTR = [
    "source_row", "schema.table.column", "term_name", "description_business_definition",
    "data_domain", "term_type", "related_business_process_l1", "related_terms",
    "related_business_process_l2", "related_business_process_l3", "synonyms_aliases",
    "bian_level_1", "bian_level_2", "bian_level_3", "bian_level_4", "fibo_level_1", "data_type",
]

#: The real second source, plus the `data_type` column being added at source.
_CIB = [
    "schema.table.column", "term_name", "description_business_definition", "data_domain",
    "data_type", "bian_reference_1", "bian_reference_2", "pci_flag", "aml_flag", "kyc_flag",
    "critical_data_flag", "regulatory_reporting_flag", "risk_data_flag", "customer_data_flag",
    "employee_data_flag", "privacy_flag", "pi_basic_demographic_flag", "pi_contacts_address_flag",
    "pi_documents_flag", "pi_financials_flag", "pi_special_flag", "pi_web_data_flag",
    "pi_derived_data_flag", "attribute_category", "security_classification",
]


# ── what must still be REFUSED ───────────────────────────────────────────────────────────────────

def test_a_file_without_the_row_key_is_refused():
    """`schema.table.column` IS the row identity. Without it every row is anonymous and the reader
    would mangle the file — the exact outcome the original fingerprint existed to prevent."""
    headers = [h for h in _CIB if h != "schema.table.column"]
    assert is_glossary_mapping(headers) is False


@pytest.mark.parametrize("missing", ["term_name", "description_business_definition"])
def test_a_file_missing_a_CORE_semantic_header_is_refused(missing):
    headers = [h for h in _CIB if h != missing]
    assert is_glossary_mapping(headers) is False
    assert missing.replace("_", "") in glossary_shape_error(headers)


def test_a_duplicated_header_is_refused():
    """Genuinely ambiguous: two columns claiming one meaning cannot be resolved by guessing."""
    assert is_glossary_mapping([*_CIB, "term_name"]) is False
    assert "duplicate" in glossary_shape_error([*_CIB, "term_name"])


def test_a_file_that_is_not_a_glossary_at_all_is_not_claimed():
    """A technical CSV must fall through to its own reader, not be claimed by this one."""
    assert is_glossary_mapping(["source", "table", "column", "type"]) is False


# ── what must now be ACCEPTED ────────────────────────────────────────────────────────────────────

def test_the_exact_FTR_layout_is_still_accepted():
    """The relaxation must not disturb the source that already works."""
    assert is_glossary_mapping(_FTR) is True
    assert is_ftr_glossary(_FTR) is True
    assert glossary_shape_error(_FTR) is None


def test_the_real_second_source_layout_is_accepted():
    """13 FTR headers absent, 20 of its own present — and it parses."""
    assert is_glossary_mapping(_CIB) is True
    assert glossary_shape_error(_CIB) is None


def test_the_second_source_is_NOT_mistaken_for_FTR():
    """`is_ftr_glossary` stays an exact check, so anything keyed to the FTR layout specifically
    still knows the difference."""
    assert is_ftr_glossary(_CIB) is False


def test_missing_OPTIONAL_ftr_columns_do_not_refuse():
    core_only = list(GLOSSARY_CORE_HEADERS)
    assert is_glossary_mapping(core_only) is True


# ── extras are tolerated, never SILENTLY tolerated ───────────────────────────────────────────────

def test_unknown_columns_are_reported_not_swallowed():
    """A column nobody reads is a column whose meaning is being lost. Tolerating it silently is how
    `security_classification` would sit unused for months."""
    unknown = unrecognised_headers(_CIB)
    assert "securityclassification" in unknown
    assert "pciflag" in unknown
    assert "schema.table.column" not in unknown and "datatype" not in unknown


def test_a_recognised_ftr_column_is_not_reported_as_unknown():
    assert unrecognised_headers(_FTR) == ()


# ── data_type is optional but consequential ──────────────────────────────────────────────────────

def test_a_file_without_data_type_is_accepted_but_flagged():
    """Its absence is not a parse failure — but it silently disables cross-catalog bridging,
    because every column resolves to an unclassifiable type family. That was the defect that made
    FTR inert, so it is surfaced rather than discovered later."""
    without = [h for h in _CIB if h != "data_type"]
    assert is_glossary_mapping(without) is True
    assert "datatype" in unrecognised_headers(without, report_missing_optional=True)
