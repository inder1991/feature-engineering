"""Audit fix I-1 — case-variant duplicate rows must not leak a PII column (fail-OPEN).

`validate_rows` keyed dedup/conflict on the RAW `(source, table, column)` while object identity
and the graph normalize with strip+lower (`object_ref._norm`). Two rows for ONE physical column
differing only in case — one tagged `pii`, one untagged — therefore had different raw keys, the
fail-closed conflict path never fired, BOTH landed in `vr.good`, and build_graph wrote an
untagged (world-visible) twin of the PII column. The comparison KEYS must use the same
normalizer as object identity; what flows to build_graph is unchanged.
"""
from featuregen.overlay.upload.canonical import CanonicalRow, validate_rows


def test_case_variant_pii_conflict_fails_closed():
    # ONE physical column (object identity normalizes case), two rows disagreeing on sensitivity:
    # this is exactly the conflict the fail-closed path exists for — an untagged twin in vr.good
    # would graph a world-visible node leaking the PII column's name/table.
    rows = [
        CanonicalRow("s", "Accounts", "SSN", "text", sensitivity="pii"),
        CanonicalRow("s", "accounts", "ssn", "text"),               # untagged case-variant twin
    ]
    vr = validate_rows(rows)
    assert vr.good == []                                            # neither graphs (fail-closed)
    assert len(vr.quarantined) == 2                                 # both surfaced for review
    assert all("conflicting metadata" in q.message for q in vr.quarantined)


def test_case_variant_identical_material_dedups_to_one():
    # Same material metadata, names differing only in case -> ONE column, deduped, no conflict.
    rows = [
        CanonicalRow("s", "Accounts", "SSN", "text", sensitivity="pii"),
        CanonicalRow("s", "accounts", "ssn", "text", sensitivity="pii"),
    ]
    vr = validate_rows(rows)
    assert len(vr.good) == 1
    assert vr.quarantined == []
    assert vr.good[0].table == "accounts"    # accepted rows carry the normalized identity (finding #1)
    assert vr.good[0].column == "ssn"        # so build_graph / snapshot / cache all key on one ref


def test_source_mismatch_check_uses_identity_normalizer():
    # 'Deposits' and 'deposits' are the SAME source under object identity (_norm strips+lowers);
    # the raw comparison wrongly quarantined a legitimate row as a foreign-source one.
    rows = [CanonicalRow("Deposits", "accounts", "id", "integer")]
    vr = validate_rows(rows, catalog_source="deposits")
    assert len(vr.good) == 1
    assert vr.quarantined == []


def test_genuinely_foreign_source_still_quarantined():
    rows = [CanonicalRow("cards", "accounts", "id", "integer")]
    vr = validate_rows(rows, catalog_source="deposits")
    assert vr.good == []
    assert len(vr.quarantined) == 1
    assert "does not match upload source" in vr.quarantined[0].message
