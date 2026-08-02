"""Spec A Task 9 — §5.2's classification adapter, and the two axes it must keep apart.

**The claim these tests exist to defend.** `graph_node.sensitivity` and
`graph_node.effective_restriction` are DIFFERENT COLUMNS with DIFFERENT VOCABULARIES (interfaces §2).
The first is a read-scope TAG (`pii`, `restricted`) and produces `access_requirements` via
`SENSITIVITY_ROLES`; the second is an ORDERED restriction level (`public < internal < confidential <
restricted < prohibited`) and produces `sensitivity_class` via `safety_floor.SENSITIVITY_ORDER`. A
column can be `pii`-tagged and `internal`-restricted at the same time, so a resolver that read one
column and reported it as both would still look right on any fixture where the two happen to agree.

**Why "independent" needs a 2×2 and not two assertions.** A test asserting only
`sensitivity_class == "internal"` on a `pii`/`internal` fixture passes on a resolver that ignores
`effective_restriction` entirely and always returns the unclassified default (which IS `internal`).
`test_the_two_axes_are_independent` therefore varies each axis with the other HELD FIXED, in both
directions, and asserts the other axis does not move. `test_the_word_restricted_lives_in_BOTH
_vocabularies_and_means_different_things` is the trap that a real conflation falls into: `restricted`
is a legal value of both columns, so a resolver reading the tag column for the class reports
`restricted` where the truth is `public`.

**The garbage-restriction fixture is reachable, and that was checked, not assumed.** Migration 0993
constrains `graph_node.sensitivity` to exactly `SENSITIVITY_ROLES`' keys (proved below), but it puts
NO constraint on `effective_restriction`, so an unknown label really can reach the column and the
normalize-then-refuse path is a live path rather than a defensive comment.
"""
from __future__ import annotations

import inspect
import io
import tokenize

import psycopg
import pytest

from featuregen.materialize import classify as classify_module
from featuregen.materialize.classify import (
    CLASSIFICATION_POLICY_VERSION,
    UNCLASSIFIED_RESTRICTION,
    Classification,
    classify_read_set,
)
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.overlay.safety_floor import SENSITIVITY_ORDER
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.read_scope import RESTRICTION_ROLES, SENSITIVITY_ROLES

_SRC = "hdfc"
TXN = f"{_SRC}::public.transactions"
AMT = f"{TXN}.txn_amt"
DT = f"{TXN}.txn_dt"
CIF = f"{TXN}.cif_id"
CUSTOMERS = f"{_SRC}::public.customers"
CUSTOMERS_CIF = f"{CUSTOMERS}.cif_id"
ACCOUNTS_ID = f"{_SRC}::public.accounts.account_id"

READ_SET = (AMT, DT, CIF)


def _string_literals(module) -> str:
    """Every non-docstring STRING literal in the module, lower-cased and joined.

    The question "is a second scale minted here?" is a question about LITERALS, not identifiers: a
    local named `prohibited` is a variable, while `"prohibited"` written out is a duplicated rank.
    """
    kept: list[str] = []
    tokens = tokenize.generate_tokens(io.StringIO(inspect.getsource(module)).readline)
    previous = tokenize.INDENT
    for token in tokens:
        if token.type == tokenize.STRING and previous not in (
                tokenize.INDENT, tokenize.DEDENT, tokenize.NEWLINE, tokenize.NL):
            kept.append(token.string)
        if token.type != tokenize.COMMENT:
            previous = token.type
    return " ".join(kept).lower()


def _node(db, ref, *, sensitivity=None, restriction=None) -> None:
    """One `graph_node` row for a logical ref, with either axis set independently.

    Written flat rather than through `build_graph` on purpose: this module tests the ADAPTER over the
    two columns, and the two columns are what a row must be able to carry in every combination —
    including combinations the resolver would never produce, which is exactly where a conflation
    shows.
    """
    source, schema, table, column = parse_ref(ref)
    db.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "schema_name, sensitivity, effective_restriction) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
        (source, _object_ref(ref), "column" if column else "table", table, column, schema,
         sensitivity, restriction))


def _object_ref(ref: str) -> str:
    """The `graph_node.object_ref` half of a logical ref — schema-flattened, source stripped."""
    _source, schema, table, column = parse_ref(ref)
    return ".".join([schema, table, *([column] if column else [])])


def _set(db, ref, *, sensitivity=..., restriction=...) -> None:
    """Change ONE axis on an existing node, leaving the other exactly as it was."""
    if sensitivity is not ...:
        db.execute("UPDATE graph_node SET sensitivity = %s WHERE catalog_source = %s "
                   "AND object_ref = %s", (sensitivity, _SRC, _object_ref(ref)))
    if restriction is not ...:
        db.execute("UPDATE graph_node SET effective_restriction = %s WHERE catalog_source = %s "
                   "AND object_ref = %s", (restriction, _SRC, _object_ref(ref)))


@pytest.fixture
def catalog(db):
    """Three transaction columns, the spine's key and a join endpoint — every node EXPLICITLY
    classified `public` and untagged, so any non-`public` answer below came from a change a test
    made rather than from a default."""
    for ref in (AMT, DT, CIF, CUSTOMERS_CIF, ACCOUNTS_ID):
        _node(db, ref, restriction="public")
    return db


def _ok(value) -> Classification:
    assert isinstance(value, Classification), value
    return value


# ══ §5.2 — axis one: the ORDERED restriction level ═══════════════════════════════════════════════


def test_sensitivity_class_comes_from_effective_restriction(catalog):
    _set(catalog, AMT, restriction="confidential")
    assert _ok(classify_read_set(catalog, READ_SET)).sensitivity_class == "confidential"


def test_the_MAXIMUM_restriction_wins_over_the_whole_read_set(catalog):
    """A max, not a first, a last or a mode — and the rank order is `safety_floor`'s."""
    _set(catalog, AMT, restriction="internal")
    _set(catalog, DT, restriction="confidential")
    _set(catalog, CIF, restriction="public")
    assert _ok(classify_read_set(catalog, READ_SET)).sensitivity_class == "confidential"
    assert _ok(classify_read_set(catalog, tuple(reversed(READ_SET)))).sensitivity_class == (
        "confidential")


def test_a_join_key_or_the_spine_can_be_the_most_restrictive(catalog):
    """The read set §1.3 assembles includes the spine's own columns and every join endpoint, so
    either can decide the class even when every expression column is public."""
    _set(catalog, CUSTOMERS_CIF, restriction="restricted")
    spine_decided = _ok(classify_read_set(catalog, (*READ_SET, CUSTOMERS_CIF)))
    assert spine_decided.sensitivity_class == "restricted"

    _set(catalog, ACCOUNTS_ID, restriction="confidential")
    endpoint_decided = _ok(classify_read_set(catalog, (*READ_SET, ACCOUNTS_ID)))
    assert endpoint_decided.sensitivity_class == "confidential"

    # The control: without those two elements the same expression columns are public, so the two
    # answers above are statements about the ADDED element rather than about the read set at large.
    assert _ok(classify_read_set(catalog, READ_SET)).sensitivity_class == "public"


# ══ §5.2 — axis two: the read-scope TAGS ═════════════════════════════════════════════════════════


def test_access_requirements_come_from_the_read_scope_TAGS(catalog):
    _set(catalog, AMT, sensitivity="pii")
    assert "pii_reader" in _ok(classify_read_set(catalog, READ_SET)).access_requirements


@pytest.mark.parametrize(("tag", "role"), sorted(SENSITIVITY_ROLES.items()))
def test_every_shipped_TAG_maps_through_SENSITIVITY_ROLES(catalog, tag, role):
    """The whole shipped vocabulary, taken from the mapping itself so a new tag cannot be added
    without this test noticing that nothing maps it."""
    _set(catalog, AMT, sensitivity=tag)
    assert _ok(classify_read_set(catalog, READ_SET)).access_requirements == (role,)


def test_access_requirements_are_a_deduplicated_sorted_UNION(catalog):
    _set(catalog, AMT, sensitivity="pii")
    _set(catalog, DT, sensitivity="pii")
    _set(catalog, CIF, sensitivity="restricted")
    requirements = _ok(classify_read_set(catalog, READ_SET)).access_requirements
    assert requirements == ("pii_reader", "restricted_reader")


def test_an_untagged_read_set_requires_NOTHING(catalog):
    """Untagged nodes are visible to everyone (`read_scope`'s shipped rule), so an untagged read set
    carries no access requirement — a non-empty answer here would be a privilege nobody declared."""
    assert _ok(classify_read_set(catalog, READ_SET)).access_requirements == ()


# ══ §5.2 — axis ONE also feeds the requirements: the governed floor's reader roles ═══════════════


def test_access_requirements_carry_the_floor_not_only_the_tag(catalog):
    """An untagged column whose governed floor is `restricted` must publish `restricted_reader` in
    the contract. Gate 2 (since migration 1032) refuses this very read to a caller without the role,
    so a tuple that stayed empty would let the artifact travel requirement-free while the platform
    refuses the same read at compile time — the requirement is supposed to travel with the data."""
    _set(catalog, AMT, restriction="restricted")
    assert "restricted_reader" in _ok(classify_read_set(catalog, READ_SET)).access_requirements


@pytest.mark.parametrize(("rank", "role"), sorted(RESTRICTION_ROLES.items()))
def test_every_role_bearing_FLOOR_maps_through_RESTRICTION_ROLES(catalog, rank, role):
    """The whole role-bearing half of the governed scale, taken from the mapping itself so a new
    floor level cannot grow a role without this test noticing that classification never adds it.
    The other ranks are covered elsewhere: `public`/`internal` add nothing (the fixture's all-public
    baseline requires NOTHING, and the 2×2 holds requirements empty under `internal`), and
    `prohibited` never reaches the requirements loop because it refused the compilation above."""
    _set(catalog, AMT, restriction=rank)
    assert _ok(classify_read_set(catalog, READ_SET)).access_requirements == (role,)


def test_a_tagged_AND_floored_read_set_publishes_the_UNION(catalog):
    _set(catalog, AMT, sensitivity="pii")
    _set(catalog, DT, restriction="confidential")
    requirements = _ok(classify_read_set(catalog, READ_SET)).access_requirements
    assert requirements == ("confidential_reader", "pii_reader")


def test_the_same_role_from_BOTH_axes_is_published_once(catalog):
    """`restricted` lives in both vocabularies and both map it to `restricted_reader`: a column
    tagged AND floored `restricted` names one requirement, not a duplicated pair."""
    _set(catalog, AMT, sensitivity="restricted", restriction="restricted")
    assert _ok(classify_read_set(catalog, READ_SET)).access_requirements == ("restricted_reader",)


# ══ §5.2 — the two axes are INDEPENDENT ══════════════════════════════════════════════════════════


@pytest.mark.parametrize("tag", [None, "pii"])
@pytest.mark.parametrize("restriction", ["public", "internal"])
def test_the_two_axes_are_independent(catalog, tag, restriction):
    """The 2×2: each axis moves with the other HELD FIXED, and the other does not follow.

    Asserting only `sensitivity_class == "internal"` on the `pii`/`internal` cell would pass on a
    resolver that never reads `effective_restriction` at all and always returns the unclassified
    default — which is `internal`. The `public` row is what makes the assertion a discriminator, and
    the `tag=None` column is what proves the tag is not feeding the class.
    """
    _set(catalog, AMT, sensitivity=tag, restriction=restriction)
    classification = _ok(classify_read_set(catalog, READ_SET))
    assert classification.sensitivity_class == restriction
    assert classification.access_requirements == (("pii_reader",) if tag else ())


def test_the_word_restricted_lives_in_BOTH_vocabularies_and_means_different_things(catalog):
    """`restricted` is a legal value of BOTH columns — the exact overlap a conflation hides in.

    A `restricted` TAG on a `public` column is a read-scope requirement, NOT a `restricted`
    classification. A resolver reading the tag column for the class returns `restricted` here.
    """
    _set(catalog, AMT, sensitivity="restricted", restriction="public")
    classification = _ok(classify_read_set(catalog, READ_SET))
    assert classification.sensitivity_class == "public"
    assert classification.access_requirements == ("restricted_reader",)


def test_the_two_vocabularies_are_not_the_same_set():
    """Recorded as an assertion rather than a comment: the sets overlap on exactly one word, so a
    resolver that swapped the columns would still produce legal-looking output most of the time."""
    assert set(SENSITIVITY_ROLES) & set(SENSITIVITY_ORDER) == {"restricted"}
    assert "pii" not in SENSITIVITY_ORDER
    assert "confidential" not in SENSITIVITY_ROLES


def test_the_read_scope_TAG_vocabulary_is_the_one_the_DATABASE_enforces(db):
    """Migration 0993 constrains `graph_node.sensitivity` to exactly `SENSITIVITY_ROLES`' keys.

    Demonstrated rather than asserted in prose (Task 7 recorded this as untested): it is what makes
    "a tag with no role" an impossible row rather than a silent drop of an access requirement.
    """
    with pytest.raises(psycopg.errors.CheckViolation):
        _node(db, AMT, sensitivity="top_secret")


def test_a_tag_outside_the_vocabulary_FAILS_CLOSED(catalog):
    """The guard behind the constraint, exercised by removing the constraint.

    The test above proves the row cannot exist today; this one proves what happens on the day that
    changes. A tag no role grants cannot be dropped (that silently loses an access requirement) and
    cannot be given an invented role name (that grants a privilege nobody defined), so it stops the
    classification instead. The DDL is inside the test's own transaction, which the `db` fixture
    rolls back — PostgreSQL DDL is transactional, so no other test sees an unconstrained column.
    """
    catalog.execute("ALTER TABLE graph_node DROP CONSTRAINT graph_node_sensitivity_check")
    _set(catalog, AMT, sensitivity="top_secret")
    with pytest.raises(ValueError, match="outside the shipped read-scope"):
        classify_read_set(catalog, READ_SET)


def test_a_ref_in_a_DIFFERENT_CASE_is_the_same_node(catalog):
    """`normalize_ref`'s own contract: `Accounts` and `accounts` are one object. A classification
    that missed the node would silently fall to the missing-classification policy — reporting a
    CONFIDENTIAL column as merely unclassified, which is the fail-OPEN direction."""
    _set(catalog, AMT, restriction="confidential")
    shouted = (AMT.upper(), DT, CIF)
    assert _ok(classify_read_set(catalog, shouted)).sensitivity_class == "confidential"


# ══ §5.2 — normalize, THEN refuse ════════════════════════════════════════════════════════════════


def test_unknown_restriction_normalizes_then_REFUSES(catalog):
    """Normalize-then-refuse (§14): an unknown label is normalized to the most restrictive rank
    INTERNALLY and the public API then refuses ONCE. Rev 3's "unknown returns prohibited AND
    prohibited raises" cannot both hold through one public API."""
    _set(catalog, AMT, restriction="top_secret_ultra")
    refusal = classify_read_set(catalog, READ_SET)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.PROHIBITED_INPUT


def test_an_unknown_label_is_NEVER_returned_verbatim(catalog):
    """`safety_floor` never persists or returns a label it cannot rank, and neither may a refusal:
    echoing the string would put an unrankable value back into circulation as if it were a class."""
    _set(catalog, AMT, restriction="top_secret_ultra")
    refusal = classify_read_set(catalog, READ_SET)
    assert isinstance(refusal, MaterializationRefused)
    assert "top_secret_ultra" not in str(refusal)
    assert AMT in refusal.detail                      # the LOCATION, which is what an operator needs


def test_prohibited_REFUSES_rather_than_producing_a_prohibited_group(catalog):
    """§5.2: a `prohibited` input refuses materialization. There is no "prohibited feature group"."""
    _set(catalog, DT, restriction="prohibited")
    refusal = classify_read_set(catalog, READ_SET)
    assert isinstance(refusal, MaterializationRefused)
    assert refusal.code is CompilationRefusalCode.PROHIBITED_INPUT
    assert not isinstance(refusal, Classification)


def test_the_refusal_names_the_offending_elements_and_counts_only(catalog):
    """Refusal detail carries counts and locations, never data values (§14)."""
    _set(catalog, AMT, restriction="prohibited")
    _set(catalog, DT, restriction="prohibited")
    refusal = classify_read_set(catalog, READ_SET)
    assert isinstance(refusal, MaterializationRefused)
    assert "2 of 3" in refusal.detail
    assert AMT in refusal.detail and DT in refusal.detail and CIF not in refusal.detail


def test_the_most_restrictive_rank_is_the_one_that_refuses():
    """Pinned so the refusal cannot drift off the top of the scale: the module names the refusing
    class by RANK (the last element of `SENSITIVITY_ORDER`) rather than by a duplicated literal, and
    this is the test that says which word that rank is."""
    assert SENSITIVITY_ORDER[-1] == "prohibited"
    assert SENSITIVITY_ORDER[0] == "public"


# ══ §5.2 — the STATED policy for a missing classification ════════════════════════════════════════


def test_the_policy_version_is_PINNED():
    """It enters the contract hash and is derived from nothing, so a test that names the value is
    the only thing between an accidental edit and a silent change of artifact identity."""
    assert CLASSIFICATION_POLICY_VERSION == 2


def test_missing_classification_uses_the_STATED_policy_not_an_implicit_default(catalog):
    """§5.2 requires the policy to be STATED in `CLASSIFICATION_POLICY_VERSION`, not implied.

    `internal` is that policy: an unclassified column is not PROVEN public (nobody attested it), and
    it is not `prohibited` either — an absent classification and a governed prohibition route an
    operator to two different remedies, so collapsing them would send them to the wrong one.
    """
    _set(catalog, AMT, restriction=None)
    classification = _ok(classify_read_set(catalog, READ_SET))
    assert classification.sensitivity_class == UNCLASSIFIED_RESTRICTION == "internal"
    assert classification.unclassified_refs == (AMT,)
    assert classification.classification_policy_version == CLASSIFICATION_POLICY_VERSION


def test_the_unclassified_policy_is_not_applied_to_a_CLASSIFIED_read_set(catalog):
    """The discriminator for the test above: a fully classified `public` read set stays `public`, so
    `internal` above is a statement about the MISSING classification rather than a blanket floor."""
    classification = _ok(classify_read_set(catalog, READ_SET))
    assert classification.sensitivity_class == "public"
    assert classification.unclassified_refs == ()


def test_a_ref_with_no_graph_node_row_is_unclassified_not_public(catalog):
    """A ref the catalog does not describe carries no classification, so the policy applies to it
    exactly as it does to a NULL — the alternative is treating "we know nothing" as "public"."""
    ghost = f"{TXN}.not_in_the_catalog"
    classification = _ok(classify_read_set(catalog, (*READ_SET, ghost)))
    assert classification.sensitivity_class == UNCLASSIFIED_RESTRICTION
    assert classification.unclassified_refs == (ghost,)


def test_a_blank_classification_is_treated_as_MISSING(catalog):
    """An empty string is not a rank; treating it as one would make `''` an unknown label and refuse
    the whole compilation over a catalog write that meant "nothing recorded"."""
    _set(catalog, AMT, restriction="   ")
    classification = _ok(classify_read_set(catalog, READ_SET))
    assert classification.sensitivity_class == UNCLASSIFIED_RESTRICTION
    assert classification.unclassified_refs == (AMT,)


# ══ §5.2 — shape, reuse and caller errors ════════════════════════════════════════════════════════


def test_the_class_is_always_a_member_of_the_shipped_ORDER(catalog):
    _set(catalog, AMT, restriction="confidential")
    assert _ok(classify_read_set(catalog, READ_SET)).sensitivity_class in SENSITIVITY_ORDER


def test_no_parallel_restriction_SCALE_is_minted(catalog):
    """§5.2: "Do not mint a parallel enum." The module may name the one class its own POLICY
    declares (`internal`, the unclassified default) and nothing else — every other rank is reached
    through `safety_floor`, so the two scales cannot drift apart.

    The refusing rank is the sharpest case: it is addressed as `SENSITIVITY_ORDER[-1]`, so a scale
    that grows a level above `prohibited` moves the refusal with it instead of leaving it behind on
    a word that is no longer the top."""
    literals = _string_literals(classify_module)
    for rank in SENSITIVITY_ORDER:
        if rank != UNCLASSIFIED_RESTRICTION:
            assert rank not in literals, f"{rank!r} is a literal in classify.py — a second scale"
    assert UNCLASSIFIED_RESTRICTION in literals


def test_the_ordering_comes_from_safety_floor(catalog):
    """Reuse asserted structurally: the ranking function is the shipped one, so a change to the
    governed scale changes this adapter with it."""
    source = inspect.getsource(classify_module)
    assert "from featuregen.overlay.safety_floor import" in source
    assert "apply_sensitivity_floor" in source


def test_classification_is_frozen_and_slotted(catalog):
    classification = _ok(classify_read_set(catalog, READ_SET))
    with pytest.raises(Exception):
        classification.sensitivity_class = "public"          # type: ignore[misc]
    assert not hasattr(classification, "__dict__")


def test_duplicate_refs_are_counted_once(catalog):
    """The read set is a SET of nodes: a column read as both an operand and a grain key is one
    element, and counting it twice would inflate every refusal count."""
    _set(catalog, AMT, restriction="prohibited")
    refusal = classify_read_set(catalog, (AMT, AMT, DT, CIF))
    assert isinstance(refusal, MaterializationRefused)
    assert "1 of 3" in refusal.detail


def test_an_empty_read_set_is_a_CALLER_error(catalog):
    """A classification over no columns is a classification of nothing — and the next stage cannot
    tell it apart from a genuinely public group. The line `authorize_compilation` draws."""
    with pytest.raises(ValueError, match="no elements"):
        classify_read_set(catalog, ())


def test_a_ref_from_ANOTHER_catalog_source_is_not_read(catalog):
    """Nodes are addressed by `(catalog_source, object_ref)`: a same-named column in a different
    source is a different node, and reading it would classify a column nobody named."""
    catalog.execute(
        "INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, column_name, "
        "sensitivity, effective_restriction) VALUES ('other', 'public.transactions.txn_amt', "
        "'column', 'transactions', 'txn_amt', 'pii', 'prohibited')")
    classification = _ok(classify_read_set(catalog, READ_SET))
    assert classification.sensitivity_class == "public"
    assert classification.access_requirements == ()
