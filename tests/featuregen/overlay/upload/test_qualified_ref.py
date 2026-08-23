"""Task S1A-5a — the governed read-set parser, as an ACCEPTANCE/REFUSAL TABLE.

A governed cross-catalog read set names columns in OTHER catalogs, so every entry must carry its
own catalog: ``source::schema.table.column``. The thing that makes such a parser safe is not that
it accepts the good shape — it is that it names the defect on every bad one, and that it never
becomes a SECOND ref grammar drifting away from ``object_ref``'s.

Both halves are pinned here. The refusal table asserts the MESSAGE of each refusal (a governed
loader turns these into an integrity error a person has to act on, and "invalid ref" is not
actionable), and one test reads the module's own source to prove it delegates: no ``split("::")``,
no regex over refs, one normalization law.
"""
from __future__ import annotations

import inspect

import pytest

from featuregen.overlay.upload import qualified_ref as module
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.qualified_ref import QualifiedRefV1, parse_qualified_ref

CANONICAL = "bank::public.txns.txn_amt"


# ── acceptance ───────────────────────────────────────────────────────────────────────────────────


def test_a_canonical_column_ref_parses_into_its_four_components():
    ref = parse_qualified_ref(CANONICAL)

    assert ref == QualifiedRefV1("bank", "public", "txns", "txn_amt")
    assert (ref.catalog_source, ref.schema_name, ref.table, ref.column) == (
        "bank", "public", "txns", "txn_amt")


def test_the_parsed_ref_round_trips_through_the_one_normalization_law():
    """``canonical`` is ``normalize_ref`` — not a format string that happens to agree with it
    today. Proven by comparing against ``normalize_ref`` itself, so a divergence fails here."""
    ref = parse_qualified_ref(CANONICAL)

    assert ref.canonical == CANONICAL
    assert ref.canonical == normalize_ref("bank", "public", "txns", "txn_amt")


def test_a_ref_in_a_non_public_schema_is_kept_whole():
    """Schema is PRESERVED (``object_ref``'s §5.1 rule): two same-named tables in two schemas
    are two identities, and the parser must not fold one into the other."""
    ref = parse_qualified_ref("retail::finance.ledger.amount")

    assert (ref.schema_name, ref.table) == ("finance", "ledger")
    assert ref.canonical == "retail::finance.ledger.amount"


def test_the_ref_is_frozen_and_hashable():
    """It is a dict key and a set member in the loader's fold — a mutable ref would let a
    read set be edited after it was authenticated."""
    ref = parse_qualified_ref(CANONICAL)

    assert len({ref, parse_qualified_ref(CANONICAL)}) == 1
    with pytest.raises(Exception):
        ref.column = "somewhere_else"       # type: ignore[misc]


# ── refusals, each asserting the message ─────────────────────────────────────────────────────────


def test_a_table_ref_refuses_because_a_read_set_names_COLUMNS():
    """``parse_ref`` accepts a two-part path as a TABLE ref. A governed read set is a set of
    columns, and a table-shaped entry there is an unbounded read wearing a bounded one's name."""
    with pytest.raises(ValueError) as excinfo:
        parse_qualified_ref("bank::public.txns")

    assert "must name a column" in str(excinfo.value)
    assert "'bank::public.txns'" in str(excinfo.value)


def test_a_doubled_source_separator_refuses_on_ARITY():
    """``parse_ref``'s own arity check, reached rather than reimplemented: the second ``::``
    swallows the path separator, so nothing splits into 2 or 3 parts."""
    with pytest.raises(ValueError) as excinfo:
        parse_qualified_ref("bank::public::txns")

    assert "not a normalized logical_ref" in str(excinfo.value)


def test_a_ref_with_no_source_separator_at_all_refuses():
    """The unqualified half — exactly what a single-catalog plan stores. It is not a governed
    cross-catalog ref, and reading it as one would silently attribute it to some catalog."""
    with pytest.raises(ValueError) as excinfo:
        parse_qualified_ref("public.txns.txn_amt")

    assert "not a normalized logical_ref" in str(excinfo.value)


def test_a_four_part_path_refuses_on_arity():
    with pytest.raises(ValueError) as excinfo:
        parse_qualified_ref("bank::public.txns.txn_amt.extra")

    assert "not a normalized logical_ref" in str(excinfo.value)


def test_a_non_canonical_spelling_refuses_and_NAMES_the_canonical_form():
    """An uppercase source is the same object under ``normalize_ref``'s fold, so a lenient
    parser would silently accept it and store a spelling that no longer matches the bytes the
    plan was sealed over. Refused — and the message says what the canonical form is, because a
    refusal a person cannot act on is an outage with better manners."""
    with pytest.raises(ValueError) as excinfo:
        parse_qualified_ref("BANK::public.txns.txn_amt")

    assert "not in canonical form" in str(excinfo.value)
    assert repr(CANONICAL) in str(excinfo.value)


def test_a_padded_component_refuses_and_names_the_canonical_form():
    with pytest.raises(ValueError) as excinfo:
        parse_qualified_ref("bank::public. txns.txn_amt")

    assert "not in canonical form" in str(excinfo.value)
    assert repr(CANONICAL) in str(excinfo.value)


@pytest.mark.parametrize(("raw", "component"), [
    ("::public.txns.txn_amt", "catalog_source"),
    ("bank::.txns.txn_amt", "schema"),
    ("bank::public..txn_amt", "table"),
    ("bank::public.txns.", "column"),
])
def test_a_blank_component_refuses_and_names_WHICH_one(raw: str, component: str):
    """The holes a pure round-trip check cannot see. ``normalize_ref`` reproduces a blank source
    and a blank table verbatim, so ``'::public.txns.txn_amt'`` round-trips EXACTLY — it is a ref
    naming no catalog that a canonical-form check would wave through."""
    with pytest.raises(ValueError) as excinfo:
        parse_qualified_ref(raw)

    assert "blank" in str(excinfo.value)
    assert component in str(excinfo.value)


def test_an_empty_string_refuses():
    with pytest.raises(ValueError):
        parse_qualified_ref("")


# ── the binding constraint: ONE grammar ──────────────────────────────────────────────────────────


def test_the_module_reimplements_no_ref_grammar():
    """The rule this whole task rests on: ``object_ref`` is the ONLY ref grammar. A second
    parser here would be a second answer about what a ref is — and the two would diverge on the
    day one of them is corrected. Asserted against the module's own source so the constraint is
    enforced rather than remembered."""
    source = inspect.getsource(module)
    code = "\n".join(line for line in source.splitlines() if not line.lstrip().startswith("#"))

    assert 'split("::")' not in code and "split('::')" not in code
    assert "import re" not in code and "re.match" not in code and "re.compile" not in code
    assert ".partition(" not in code
    # ...and it really does call the canonical law.
    assert "normalize_ref" in code and "parse_ref" in code
