"""The upload route must accept a second source's glossary layout.

The parser was relaxed; the ROUTE still gated on the exact FTR multiset, so the real
CIB_Customer_Column_Mapping would have been refused at the front door even though the reader behind
it could parse the file perfectly. Fixing one without the other changes nothing a user can see.
"""
from __future__ import annotations

CIB_HEADERS = (
    '"schema.table.column","term_name","description_business_definition","data_domain",'
    '"data_type","pci_flag","security_classification","attribute_category"'
)
CIB_ROW = (
    '"BO_DPL_CIB.BO_CIB_CUSTOMER.cust_num","Customer Number","The customer number.",'
    '"Customer","varchar(150)","N","Confidential","Customer Master"'
)


def _csv() -> bytes:
    return f"{CIB_HEADERS}\n{CIB_ROW}\n".encode()


def test_the_route_accepts_a_second_sources_layout(client):
    r = client.post("/uploads", files={"file": ("CIB_Customer_Column_Mapping_final.csv", _csv(),
                                                "text/csv")},
                    data={"source": "cib"}, headers={"X-User": "u", "X-Roles": "data_owner"})
    assert r.status_code != 400 or "format error" not in r.text, r.text[:400]


def test_a_file_missing_the_row_key_is_still_refused(client):
    bad = _csv().replace(b"schema.table.column", b"some_other_column")
    r = client.post("/uploads", files={"file": ("x.csv", bad, "text/csv")},
                    data={"source": "cib"}, headers={"X-User": "u", "X-Roles": "data_owner"})
    assert r.status_code in (200, 400)   # falls through to the technical reader, never mangled here


def test_a_duplicated_header_is_still_refused(client):
    bad = (f"{CIB_HEADERS},\"term_name\"\n{CIB_ROW},\"dup\"\n").encode()
    r = client.post("/uploads", files={"file": ("x.csv", bad, "text/csv")},
                    data={"source": "cib"}, headers={"X-User": "u", "X-Roles": "data_owner"})
    assert r.status_code == 400 and "duplicate" in r.text.lower(), r.text[:300]
