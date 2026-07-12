"""Task 2: upload identity — EXACT/AMBIGUOUS vs metadata-conflict (review #12).

The load-bearing distinction under test: two rows with the SAME FQN but a different `definition`
are a METADATA CONFLICT (still ONE attachable EXACT binding + a surfaced conflict), NOT identity
ambiguity. `AMBIGUOUS` is reserved for a ref that genuinely cannot be pinned to one object.
"""
from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.object_identity import ObjectIdentityStatus, may_attach
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.upload_identity import (
    MetadataConflict,
    classify_upload,
    logical_ref_str,
)


def _row(**kw):
    base = dict(source="deposits", table="accounts", column="balance", type="numeric")
    base.update(kw)
    return CanonicalRow(**base)


def test_logical_ref_str_reuses_normalize_ref():
    row = _row()
    assert logical_ref_str(row) == normalize_ref("deposits", None, "accounts", "balance")


def test_unique_column_is_exact_and_attachable():
    ref = logical_ref_str(_row())
    bindings, conflicts = classify_upload([_row()])

    assert set(bindings) == {ref}
    assert bindings[ref].status is ObjectIdentityStatus.EXACT
    assert may_attach(bindings[ref]) is True
    assert bindings[ref].logical_ref is not None
    assert bindings[ref].logical_ref.table == "accounts"
    assert bindings[ref].logical_ref.column == "balance"
    assert conflicts == []


def test_same_fqn_different_definition_is_exact_plus_metadata_conflict():
    # THE review-#12 must-fix: differing `definition` for the same object is a conflict, NOT
    # ambiguity. The binding stays ONE EXACT / attachable object.
    rows = [
        _row(definition="ledger balance"),
        _row(definition="available balance"),
    ]
    ref = logical_ref_str(rows[0])
    bindings, conflicts = classify_upload(rows)

    assert len(bindings) == 1
    assert bindings[ref].status is ObjectIdentityStatus.EXACT
    assert may_attach(bindings[ref]) is True                     # evidence attach NOT blocked

    assert len(conflicts) == 1
    conflict = conflicts[0]
    assert isinstance(conflict, MetadataConflict)
    assert conflict.logical_ref == ref
    assert conflict.field == "definition"
    # the competing values' hashes, matching the field-evidence store's `proposed_value_hash`
    assert conflict.competing_value_hashes == tuple(
        sorted({canonical_hash("ledger balance"), canonical_hash("available balance")})
    )


def test_identical_duplicate_rows_dedup_no_conflict():
    rows = [_row(definition="ledger balance"), _row(definition="ledger balance")]
    ref = logical_ref_str(rows[0])
    bindings, conflicts = classify_upload(rows)

    assert set(bindings) == {ref}                # collapsed into one binding
    assert bindings[ref].status is ObjectIdentityStatus.EXACT
    assert conflicts == []                        # identical rows do not disagree


def test_unpinnable_ref_is_ambiguous_and_not_attachable():
    # Two DISTINCT source objects collide onto one normalized FQN (`s::public.a.b.c`): (table=a,
    # column=b.c) and (table=a.b, column=c). The ref cannot be pinned to one object -> AMBIGUOUS.
    r1 = CanonicalRow("s", "a", "b.c", "text")
    r2 = CanonicalRow("s", "a.b", "c", "text")
    assert logical_ref_str(r1) == logical_ref_str(r2)             # the structural duplication
    ref = logical_ref_str(r1)

    bindings, conflicts = classify_upload([r1, r2])

    assert bindings[ref].status is ObjectIdentityStatus.AMBIGUOUS
    assert may_attach(bindings[ref]) is False                    # never attachable
    assert bindings[ref].logical_ref is None
    assert len(bindings[ref].candidates) == 2                    # the two colliding objects
    assert conflicts == []                                        # not a field conflict


def test_ambiguous_pin_decision_reuses_classify_identity():
    # A round-tripping FQN (`s::public.a.b`) reachable from two objects — table `a.b` (no column)
    # and column `a.b` — flows through `classify_identity` (>1 candidate) to AMBIGUOUS.
    r1 = CanonicalRow("s", "a.b", "", "text")     # table a.b, no column
    r2 = CanonicalRow("s", "a", "b", "text")      # table a, column b
    assert logical_ref_str(r1) == logical_ref_str(r2)
    ref = logical_ref_str(r1)

    bindings, _ = classify_upload([r1, r2])
    assert bindings[ref].status is ObjectIdentityStatus.AMBIGUOUS
    assert may_attach(bindings[ref]) is False


def test_partial_field_omission_is_not_a_conflict():
    # One row asserts a definition, the other leaves it blank -> not a disagreement (no competing
    # assertion), so no conflict is raised.
    rows = [_row(definition="ledger balance"), _row(definition="")]
    _, conflicts = classify_upload(rows)
    assert conflicts == []


def test_two_objects_one_upload_yield_two_bindings():
    rows = [_row(column="balance"), _row(column="posted_at", type="timestamp")]
    bindings, conflicts = classify_upload(rows)
    assert len(bindings) == 2
    assert all(b.status is ObjectIdentityStatus.EXACT for b in bindings.values())
    assert conflicts == []
