"""Composition audit finding [8] — the gate console's gold/drift fixtures seed under a RESERVED
source (``__gate_gold__``) so their ``build_graph`` (DELETE-this-source-then-reinsert) can never wipe
or lock a real customer catalog's graph rows. The reserved ``__…__`` form is rejected as a USER
upload source name at BOTH the route boundary (``normalize_source_name``) and the ingest boundary
(``validate_rows``), so the two name spaces can never collide."""
from __future__ import annotations

import pytest

from featuregen.overlay.upload.canonical import CanonicalRow, validate_rows
from featuregen.overlay.upload.object_ref import (
    is_reserved_source_name,
    normalize_source_name,
)


def test_is_reserved_source_name_matches_only_the_double_underscore_form():
    assert is_reserved_source_name("__gate_gold__")
    assert is_reserved_source_name("  __Gate_Gold__ ")   # normalized (strip + lower) first
    assert not is_reserved_source_name("core")           # a real catalog name is NOT reserved
    assert not is_reserved_source_name("__core")         # not wrapped on both sides
    assert not is_reserved_source_name("core__")


def test_normalize_source_name_rejects_a_reserved_name_but_passes_a_real_one():
    assert normalize_source_name("Core") == "core"       # a normal name folds case + passes
    with pytest.raises(ValueError, match="reserved"):
        normalize_source_name("__gate_gold__")


def test_normalize_source_name_rejects_the_ref_separator():
    """A ``::`` in a source name is the REF separator: ``retail::eu`` builds refs whose
    ``split_part(logical_ref, '::', 1)`` reads as source ``retail``, so ingesting the SEPARATE source
    ``retail`` would sweep ``retail::eu``'s evidence into its own reconciliation and retire it."""
    with pytest.raises(ValueError, match="single path segment"):
        normalize_source_name("Retail::EU")


def test_validate_rows_rejects_a_reserved_catalog_source():
    # An ingest into a reserved source is refused fail-closed as a structural error — nothing is
    # accepted, so no direct ingest_upload caller can write the gate console's fixture namespace.
    rows = [CanonicalRow("__gate_gold__", "accounts", "id", "integer", is_grain=True)]
    vr = validate_rows(rows, "__gate_gold__")
    assert vr.structural_error is not None and "reserved" in vr.structural_error
    assert not vr.good
