from featuregen.overlay.upload.passc.identifiers import ColMeta
from featuregen.overlay.upload.passc.namespace import classify_namespace
from featuregen.overlay.upload.passc.types import NamespaceCompatibility as N


def _c(**kw):
    b = dict(object_ref="src::public.t.c", table="t", column="c", data_type="text", term_name="",
             term_type="", concept="", synonyms="", bian_leaf="", fibo_leaf="", table_entity="",
             column_entity="", data_domain="", is_grain=False)
    b.update(kw)
    return ColMeta(**b)


def test_same_column_entity_compatible_even_across_table_entities():
    # transactions.customer_id → customer.customer_id: DIFFERENT table entities, SAME column entity.
    # Keying on table_entity here would kill the single most common legitimate join.
    a = _c(table="transactions", column="customer_id", table_entity="transaction", column_entity="customer")
    b = _c(table="customer", column="customer_id", table_entity="customer", column_entity="customer")
    verdict, reasons = classify_namespace(a, b)
    assert verdict is N.COMPATIBLE
    assert reasons == ("same_column_entity",)


def test_different_column_entity_incompatible():
    a = _c(column="customer_id", column_entity="customer")
    b = _c(column="merchant_id", column_entity="merchant")
    verdict, reasons = classify_namespace(a, b)
    assert verdict is N.INCOMPATIBLE
    assert reasons == ("different_column_entity",)


def test_same_concept_same_canonical_column_name_compatible():
    # cif_id vs cif → both canonicalize to "cif" (generic id tail stripped)
    a = _c(column="cif_id", term_name="Customer Information File Identifier")
    b = _c(column="cif", term_name="Customer Information File Identifier")
    verdict, reasons = classify_namespace(a, b)
    assert verdict is N.COMPATIBLE
    assert reasons == ("same_identifier_concept", "same_column_name")


def test_same_concept_synonym_corroboration_compatible():
    a = _c(column="cif_id", term_name="Customer Information File Identifier")
    b = _c(column="cust_ref", term_name="CIF", synonyms="Customer Information File")
    verdict, reasons = classify_namespace(a, b)
    assert verdict is N.COMPATIBLE
    assert reasons == ("same_identifier_concept", "synonym_corroboration")


def test_same_concept_different_name_no_synonyms_is_possible():
    # Load-bearing reachability: this tier must exist — neither promoted to COMPATIBLE nor
    # suppressed to AMBIGUOUS.
    a = _c(column="cif_id", term_name="Customer Information File Identifier")
    b = _c(column="cust_file_ref", term_name="Customer Information File Identifier")
    verdict, reasons = classify_namespace(a, b)
    assert verdict is N.POSSIBLE
    assert reasons == ("same_identifier_concept",)


def test_placeholder_synonyms_do_not_corroborate():
    # "(blank)" is an upload placeholder, not an alias — must stay POSSIBLE, not COMPATIBLE.
    a = _c(column="cif_id", term_name="Customer Information File Identifier")
    b = _c(column="cust_file_ref", term_name="Customer Information File Identifier", synonyms="(blank)")
    verdict, reasons = classify_namespace(a, b)
    assert verdict is N.POSSIBLE
    assert reasons == ("same_identifier_concept",)


def test_mixed_bian_leaf_ambiguous():
    leaf = "Customer and Counterparty Identification"     # in DEFAULT_CONFIG.mixed_bian_leaves
    verdict, reasons = classify_namespace(
        _c(column="party_ref", bian_leaf=leaf), _c(column="counterparty_ref", bian_leaf=leaf))
    assert verdict is N.AMBIGUOUS
    assert reasons == ("mixed_bian_leaf",)


def test_same_bian_leaf_only_ambiguous():
    verdict, reasons = classify_namespace(
        _c(column="agmt_ref", bian_leaf="Current Account"), _c(column="deal_ref", bian_leaf="Current Account"))
    assert verdict is N.AMBIGUOUS
    assert reasons == ("same_bian_leaf_only",)


def test_generic_reference_without_context_ambiguous():
    verdict, reasons = classify_namespace(_c(column="ref1"), _c(column="ref2"))
    assert verdict is N.AMBIGUOUS
    assert reasons == ("generic_reference_without_context",)
