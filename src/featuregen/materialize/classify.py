"""Spec §5.2 — classification over the group's complete physical read set: TWO independent axes.

``graph_node.sensitivity`` and ``graph_node.effective_restriction`` are **different columns with
different vocabularies** (interfaces §2), and conflating them is a governance error rather than a
naming quibble:

* ``effective_restriction`` is an ORDERED restriction level ranked by
  ``safety_floor.SENSITIVITY_ORDER`` (``public < internal < confidential < restricted <
  prohibited``). Its maximum over the read set is the contract's ``sensitivity_class``. No parallel
  enum is minted here: the ranking and the fail-closed normalization are the shipped ones, reached
  through ``apply_sensitivity_floor``, so a change to the governed scale changes this adapter too.
* ``sensitivity`` is a READ-SCOPE TAG (``pii``, ``restricted``) whose only meaning is which role may
  see the node. Mapped through ``read_scope.SENSITIVITY_ROLES``, the tags present give
  ``access_requirements``.

A column can be ``pii``-tagged and ``internal``-restricted at once, and ``restricted`` is a legal
value of BOTH columns — which is why a resolver that read one column and reported it as both would
still look right on most fixtures. The two answers below are computed from two different columns and
never from each other.

**Normalize, then refuse — once (§14).** An ``effective_restriction`` this platform cannot rank is
normalized INTERNALLY to the most restrictive rank (``safety_floor`` behaviour: an unrankable label
can never sort below the floor and is never persisted or returned verbatim), and the public API then
returns ONE refusal: ``PROHIBITED_INPUT``. A ``prohibited`` input refuses materialization; it does
not produce a "prohibited feature group", and the refusal names the offending refs and counts rather
than echoing a label nobody can rank.

**The policy for a MISSING classification is stated, not implied (§5.2).**
``CLASSIFICATION_POLICY_VERSION`` covers this rule: a node with no ``effective_restriction`` — NULL,
blank, or no ``graph_node`` row at all — is classified ``UNCLASSIFIED_RESTRICTION`` (``internal``).
Both other candidates are worse and for opposite reasons. ``public`` would publish a claim nobody
attested. ``prohibited`` would refuse every group over an uncatalogued column AND would collapse
"nobody classified this" into "a governance decision forbids this" — two conditions whose remedies
go to two different people. The unclassified refs are carried on the result so the assumption is
visible downstream rather than silent.

**What this module deliberately does NOT do.** It is not Gate 2. Gate 2 (``ir.authorize_compilation``)
decides whether the supplied ROLES may read the tagged nodes at all; this module records what the
published artifact would REQUIRE. A group can pass Gate 2 (the caller holds ``pii_reader``) and still
carry ``pii_reader`` in its access requirements — that is the point: the requirement travels with the
data, not with whoever compiled it.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

from featuregen.contracts.db import DbConn
from featuregen.materialize.codes import CompilationRefusalCode, MaterializationRefused
from featuregen.overlay.safety_floor import SENSITIVITY_ORDER, apply_sensitivity_floor
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.read_scope import SENSITIVITY_ROLES

__all__ = [
    "CLASSIFICATION_POLICY_VERSION",
    "UNCLASSIFIED_RESTRICTION",
    "Classification",
    "classify_read_set",
]

#: The version of the RULES below — which column feeds which answer, how the maximum is taken, and
#: what an absent classification means. It enters the materialization contract hash (§5.5): a group
#: classified under different rules is a different artifact even when the words coincide.
CLASSIFICATION_POLICY_VERSION = 1

#: The stated policy for a node with no recorded ``effective_restriction`` (module docstring). It is
#: the ONE rank this module names as a literal; every other rank is reached through the shipped
#: ordering, so no second scale can drift away from ``safety_floor``'s.
UNCLASSIFIED_RESTRICTION = "internal"

#: The least restrictive rank — the base a maximum is taken from, never an answer in its own right
#: (an empty read set is refused before it could become one).
_BASE_RESTRICTION = SENSITIVITY_ORDER[0]

#: The rank that refuses materialization: the top of the governed scale, addressed BY RANK so the
#: refusal cannot drift off the end of a scale that grows. ``test_classify`` pins which word it is.
_REFUSING_RESTRICTION = SENSITIVITY_ORDER[-1]


@dataclass(frozen=True, slots=True)
class Classification:
    """What the group's read set makes the published artifact, on both axes.

    ``unclassified_refs`` is EVIDENCE, not identity: it says which elements fell to the stated
    missing-classification policy so an operator can close the gap. It is deliberately excluded from
    the contract hash — a column later classified at the value the policy already assumed describes
    the same artifact, and letting the evidence in would split a group the day a catalog gap closed.
    """

    sensitivity_class: str
    access_requirements: tuple[str, ...]
    unclassified_refs: tuple[str, ...]
    classification_policy_version: int


def _fold(value: str) -> str:
    return value.strip().lower()


def _object_ref(logical_ref: str) -> tuple[str, str]:
    """``(catalog_source, object_ref)`` — the pair that addresses a ``graph_node``.

    The same derivation ``ir._Union`` uses, and for the same reason: a node classified under one
    spelling and read under another is a node nobody classified.
    """
    source, schema, table, column = parse_ref(logical_ref)
    return _fold(source), _fold(".".join([schema, table, *([column] if column else [])]))


def classify_read_set(
    conn: DbConn, refs: Iterable[str]
) -> Classification | MaterializationRefused:
    """Classify the complete physical read set of ONE feature (§5.2), or refuse it.

    ``refs`` is the union §1.3 assembles — every expression read, every join endpoint, every
    availability column AND the spine's own columns — so the spine or a join key can be the most
    restrictive element. Duplicates collapse: a column read as both an operand and a grain key is one
    node, and counting it twice would inflate every count a refusal reports.

    Returns:
        A :class:`Classification`, or a :class:`MaterializationRefused` carrying
        ``PROHIBITED_INPUT`` when any element normalizes to the most restrictive rank.

    Raises:
        ValueError: ``refs`` is empty (a classification of nothing, which the next stage cannot tell
            apart from a genuinely public group), a ref is not a normalized ``logical_ref``, or the
            catalog holds a read-scope tag outside the shipped vocabulary. The last is a catalog
            integrity violation rather than a governed verdict — migration 0993 constrains the column
            to exactly ``SENSITIVITY_ROLES``' keys — and §14 has no member for it.
    """
    ordered = tuple(dict.fromkeys(refs))
    if not ordered:
        raise ValueError(
            "classify_read_set was called with no elements: a classification over no columns is a "
            "classification of nothing, and the next stage cannot tell it apart from a group whose "
            "inputs were genuinely public")

    rows = _read_axes(conn, ordered)

    # Axis one — the ordered restriction level, normalized per element so an unrankable label cannot
    # be reported verbatim and so the refusal can name WHICH elements are at the top of the scale.
    unclassified: list[str] = []
    restriction: dict[str, str] = {}
    for ref in ordered:
        declared = rows.get(ref, (None, None))[1]
        if declared is None or not str(declared).strip():
            unclassified.append(ref)
            declared = UNCLASSIFIED_RESTRICTION
        restriction[ref] = apply_sensitivity_floor(_BASE_RESTRICTION, [str(declared)])

    prohibited = sorted(ref for ref, rank in restriction.items() if rank == _REFUSING_RESTRICTION)
    if prohibited:
        return MaterializationRefused(
            CompilationRefusalCode.PROHIBITED_INPUT,
            f"{len(prohibited)} of {len(ordered)} elements of the physical read set classify as "
            f"{_REFUSING_RESTRICTION!r}: {', '.join(prohibited)}. Materialization is refused rather "
            f"than published as a {_REFUSING_RESTRICTION!r} feature group — a group that exists is "
            f"a group somebody can read. An element whose recorded classification is not a rank of "
            f"the governed scale is counted here too: it is normalized to the top rank rather than "
            f"reported as itself, because a label that cannot be ranked cannot be trusted below it")

    # Axis two — the read-scope tags, mapped through the SHIPPED role table.
    requirements: set[str] = set()
    for ref in ordered:
        tag = rows.get(ref, (None, None))[0]
        if tag is None:
            continue
        role = SENSITIVITY_ROLES.get(tag)
        if role is None:
            raise ValueError(
                f"graph_node.sensitivity {tag!r} on {ref} is outside the shipped read-scope "
                f"vocabulary {sorted(SENSITIVITY_ROLES)}: migration 0993 constrains the column to "
                f"exactly those values, so this row is a catalog integrity violation. Dropping it "
                f"would silently lose an access requirement and inventing a role name would grant a "
                f"privilege nobody defined")
        requirements.add(role)

    return Classification(
        sensitivity_class=apply_sensitivity_floor(
            _BASE_RESTRICTION, sorted(restriction.values())),
        access_requirements=tuple(sorted(requirements)),
        unclassified_refs=tuple(sorted(unclassified)),
        classification_policy_version=CLASSIFICATION_POLICY_VERSION)


def _read_axes(
    conn: DbConn, refs: tuple[str, ...]
) -> dict[str, tuple[str | None, str | None]]:
    """``ref -> (sensitivity tag, effective_restriction)`` for every ref the catalog describes.

    Both axes come from ONE row per node, read in one statement per catalog source (the shape
    ``ir._hidden`` uses). A ref with no row is simply absent from the result: what it is, is a read
    of a column the catalog does not describe, which §11's L1 validation reports against the live
    metastore as ``COLUMN_ABSENT``. Here it falls to the stated missing-classification policy.
    """
    indexed: dict[str, dict[str, list[str]]] = {}
    for ref in refs:
        source, object_ref = _object_ref(ref)
        indexed.setdefault(source, {}).setdefault(object_ref, []).append(ref)

    axes: dict[str, tuple[str | None, str | None]] = {}
    for source, by_object in indexed.items():
        rows = conn.execute(
            "SELECT lower(object_ref), sensitivity, effective_restriction FROM graph_node "
            "WHERE catalog_source = %s AND lower(object_ref) = ANY(%s)",
            (source, list(by_object))).fetchall()
        for object_ref, sensitivity, restriction in rows:
            for ref in by_object[object_ref]:
                axes[ref] = (sensitivity, restriction)
    return axes
