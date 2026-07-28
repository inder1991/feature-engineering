"""Stop discarding a source's own columns — pass ALL of them to the classifier.

CIB carries 25 headers. The reader mapped only the FTR-shaped ones and silently dropped the rest, so
`attribute_category`, both BIAN references, `security_classification` and 13 governance flags
(`pci_flag`, `aml_flag`, `kyc_flag`, the seven `pi_*` …) never reached the model. A mapping file's
columns DESCRIBE columns — they are meaning, not customer rows — so there is no reason to throw them
away, and every reason to send them: they are the only per-column signal that varies when a source
auto-fills its description column by bucket.

Extras ride as ONE list of bounded ``"header: value"`` strings, deliberately reusing the shape
`synonyms` already uses, so they inherit the existing per-item PII scan and egress caps rather than
opening a new unscanned channel.

Two things are still refused, because "send everything" is about METADATA breadth, not about
bypassing the data boundary: an empty value carries nothing, and the payload stays bounded.
"""
from __future__ import annotations

from featuregen.overlay.upload.enrich_llm import _ITEM_META_ALLOWED, _item_shape_ok
from featuregen.overlay.upload.ftr_adapter import read_ftr_glossary

_HDR = ("schema.table.column,term_name,description_business_definition,data_domain,data_type,"
        "bian_reference_1,bian_reference_2,attribute_category,security_classification,"
        "pci_flag,aml_flag,kyc_flag")
_ROW = ("BO.CUST.cust_staff_flg,Customer Staff Flag,Status or indicator used to classify customer,"
        "Customer,varchar(1),Sales and Service - Customer Management,Reference Data - Party,"
        "Customer Master Profile,Confidential,N,Y,Y")


def _record(text: str):
    prepared = read_ftr_glossary(text, source="cib")
    assert prepared.quarantined == [], prepared.quarantined
    return prepared.records[0]


def _attrs(text: str) -> dict[str, str]:
    out = {}
    for entry in _record(text).source_attributes:
        key, _, value = entry.partition(":")
        out[key.strip()] = value.strip()
    return out


# ── the columns that used to be dropped now travel ───────────────────────────────────────────────

def test_the_governance_and_category_columns_are_captured():
    """These are the per-column signal that actually varies — the reason the classifier had nothing
    to separate `cust_staff_flg` from `cust_curr_ntb_flg` with."""
    attrs = _attrs(f"{_HDR}\n{_ROW}\n")
    assert attrs["attribute_category"] == "Customer Master Profile"
    assert attrs["security_classification"] == "Confidential"
    assert attrs["aml_flag"] == "Y" and attrs["kyc_flag"] == "Y" and attrs["pci_flag"] == "N"


def test_a_recognised_column_does_NOT_also_ride_as_an_extra():
    """`term_name`/`data_domain`/`data_type` already have first-class slots; duplicating them would
    waste budget and let two copies disagree."""
    attrs = _attrs(f"{_HDR}\n{_ROW}\n")
    for already_mapped in ("term_name", "data_domain", "data_type",
                           "description_business_definition", "schema.table.column"):
        assert already_mapped not in attrs


def test_an_empty_value_is_not_sent():
    """A header with nothing in it carries no meaning and would only consume budget."""
    text = f"{_HDR}\nBO.CUST.c,Term,Definition,Customer,varchar,,,,,,,\n"
    assert _attrs(text) == {}


def test_a_negative_flag_is_still_sent():
    """`pci_flag: N` is a real statement — this column is explicitly NOT PCI. Dropping falsey values
    would turn an explicit 'no' into an unknown."""
    assert _attrs(f"{_HDR}\n{_ROW}\n")["pci_flag"] == "N"


# ── BIAN gets its first-class slot, not just a passenger seat ────────────────────────────────────

def test_bian_reference_columns_populate_the_real_bian_path():
    """CIB names them `bian_reference_1/2` where FTR names them `bian_level_1..4`, so the reader
    looked up a header that wasn't there and read empty. `bian_path` is consumed beyond the
    classifier (the grounding path-agreement check), so it must be the real field, not an extra."""
    rec = _record(f"{_HDR}\n{_ROW}\n")
    assert "Customer Management" in rec.bian_path
    assert "Reference Data - Party" in rec.bian_path


def test_the_ftr_bian_levels_still_win_when_present():
    """FTR's own layout must be untouched by the alias."""
    hdr = ("schema.table.column,term_name,description_business_definition,"
           "bian_level_1,bian_level_2,bian_reference_1")
    rec = _record(f"{hdr}\nS.T.C,Term,Definition,Customer Management,Customer Profile,SHOULD_NOT_WIN\n")
    assert rec.bian_path.startswith("Customer Management")
    assert "SHOULD_NOT_WIN" not in rec.bian_path


# ── the egress boundary still holds ──────────────────────────────────────────────────────────────

def test_the_new_key_is_allowlisted_for_egress():
    """Without this the whole payload would be silently dropped by the per-item gate — the failure
    mode where 'we send everything' quietly sends nothing."""
    assert "source_attributes" in _ITEM_META_ALLOWED


def test_an_item_carrying_extras_passes_the_egress_shape_gate():
    assert _item_shape_ok({"table": "t", "column": "c", "type": "varchar",
                           "source_attributes": ["attribute_category: Customer Master Profile"]})


def test_the_extras_list_is_capped():
    """Breadth is the goal; unboundedness is not. A file with hundreds of columns must not blow the
    per-item budget and push the real signal out of the batch."""
    assert _item_shape_ok({"source_attributes": [f"h{i}: v" for i in range(500)]}) is False


def test_a_value_is_bounded():
    text = f"{_HDR}\nBO.CUST.c,Term,Definition,Customer,varchar,,,{'x' * 900},Confidential,N,N,N\n"
    assert all(len(a) <= 260 for a in _record(text).source_attributes)
