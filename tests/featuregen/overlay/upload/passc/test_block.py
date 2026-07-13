from featuregen.overlay.upload.passc.candidates import CandidatePair, block_candidates
from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.types import NamespaceCompatibility as N


def _c(table, column, **kw):
    b = dict(object_ref=f"src::public.{table}.{column}", table=table, column=column,
             data_type="text", term_name="", term_type="", concept="", synonyms="",
             bian_leaf="", fibo_leaf="", table_entity="", column_entity="",
             data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


_CIF_TERM = "Customer Information File Identifier"


def test_two_cif_ids_across_tables_paired_with_namespace_carried():
    a = _c("accounts", "cif_id", term_name=_CIF_TERM)
    b = _c("customers", "cif_id", term_name=_CIF_TERM)
    pairs = block_candidates([a, b])
    assert len(pairs) == 1
    p = pairs[0]
    assert isinstance(p, CandidatePair)
    assert (p.a.object_ref, p.b.object_ref) == (a.object_ref, b.object_ref)
    assert p.namespace is N.COMPATIBLE
    assert p.namespace_reasons == ("same_identifier_concept", "same_column_name")


def test_possible_namespace_is_admitted():
    # Same concept, different name, no synonyms → POSSIBLE. The gate is {COMPATIBLE, POSSIBLE};
    # POSSIBLE must be reachable, not silently dropped.
    a = _c("accounts", "cif_id", term_name=_CIF_TERM)
    b = _c("loans", "cust_file_ref", term_name=_CIF_TERM)
    pairs = block_candidates([a, b])
    assert len(pairs) == 1
    assert pairs[0].namespace is N.POSSIBLE
    assert pairs[0].namespace_reasons == ("same_identifier_concept",)


def test_ineligible_columns_never_paired():
    # cust_name ("name" negative token) and a measure amount must never anchor a pair,
    # even sitting next to perfectly pairable cif ids.
    cif_a = _c("accounts", "cif_id", term_name=_CIF_TERM)
    cif_b = _c("customers", "cif_id", term_name=_CIF_TERM)
    name = _c("customers", "cust_name", term_name="Customer Name")
    amount = _c("transactions", "txn_amount", data_type="numeric",
                term_name="Transaction Amount", term_type="measure", concept="amount")
    pairs = block_candidates([cif_a, cif_b, name, amount])
    refs = {p.a.object_ref for p in pairs} | {p.b.object_ref for p in pairs}
    assert name.object_ref not in refs
    assert amount.object_ref not in refs
    assert len(pairs) == 1      # only the cif pair survives


def test_incompatible_column_entities_excluded():
    a = _c("transactions", "customer_id", column_entity="customer")
    b = _c("merchants", "merchant_id", column_entity="merchant")
    assert block_candidates([a, b]) == []


def test_mixed_bian_leaf_ambiguous_excluded():
    # Same mixed BIAN leaf → AMBIGUOUS, and AMBIGUOUS does NOT pass the blocker. No special case.
    leaf = "Customer and Counterparty Identification"
    a = _c("parties", "party_ref", bian_leaf=leaf)
    b = _c("deals", "counterparty_ref", bian_leaf=leaf)
    assert block_candidates([a, b]) == []


def test_same_table_excluded_unless_allow_self_join():
    a = _c("customers", "cif_id", term_name=_CIF_TERM)
    b = _c("customers", "cif", term_name=_CIF_TERM)
    assert block_candidates([a, b]) == []
    pairs = block_candidates([a, b], allow_self_join=True)
    assert len(pairs) == 1
    assert pairs[0].namespace is N.COMPATIBLE


def test_output_order_stable_regardless_of_input_order():
    cols = [_c(t, "cif_id", term_name=_CIF_TERM) for t in ("accounts", "customers", "loans")]
    expected = block_candidates(cols)
    assert len(expected) == 3   # 3 tables → 3 cross-table pairs
    for perm in (list(reversed(cols)), [cols[1], cols[2], cols[0]]):
        assert block_candidates(perm) == expected
    for p in expected:
        assert p.a.object_ref < p.b.object_ref  # i<j over the object_ref-sorted list
