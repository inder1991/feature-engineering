"""Upload object identity: pin uploaded rows to object bindings, separating identity ambiguity from
metadata conflict (spec §5.1; review #12).

Phase-0 :func:`overlay.object_identity.resolve_object_identity` resolves a ref against a LIVE catalog
adapter — but the upload flow has no such adapter, so every uploaded column would resolve
``UNRESOLVED``. Yet the uploaded rows ARE the catalog: each distinct normalized ref
(:func:`overlay.upload.object_ref.normalize_ref`) names exactly one source object. This module
classifies an upload's rows into object bindings by REUSING the Phase-0 pure classifier
(:func:`overlay.object_identity.classify_identity`) over the upload's own rows as the candidate set.

The load-bearing distinction (review #12 must-fix): two rows with the SAME FQN but a different
``definition`` / metadata are NOT identity-ambiguous — they pin to ONE object and MUST stay
attachable. Their disagreement is a METADATA CONFLICT surfaced as a :class:`MetadataConflict` (handed
to Task 10 to open a ``conflict_review`` item), never a reason to block evidence attach. Identity
``AMBIGUOUS`` is reserved for a ref that genuinely cannot be pinned to one object — an unparseable or
structurally-duplicated FQN where more than one distinct source object collides onto one normalized
ref.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.field_evidence import canonical_hash
from featuregen.overlay.object_identity import (
    LogicalObjectRef,
    ObjectBinding,
    ObjectIdentityStatus,
    classify_identity,
)
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.object_ref import normalize_ref, parse_ref

# The attested per-object fields whose disagreement across rows for ONE object is a metadata conflict
# (never an identity problem). `definition` — the value Phase-0 dedup treats as advisory (see
# `canonical._material`) — is FIRST because it is the review-#12 exemplar: a definition disagreement
# must surface a conflict yet leave the binding attachable. The rest are `_material`'s load-bearing
# fields; ingest validation already fails closed on those, but classifying raw (pre-validation) rows
# still names the field that disagrees so Task 10 can open a precise conflict.
_ATTESTED_FIELDS: tuple[str, ...] = (
    "definition", "type", "is_grain", "as_of", "as_of_basis", "sensitivity",
    "joins_to", "cardinality", "additivity", "unit", "currency", "entity",
)

# ASCII unit separator: it cannot occur in a normal identifier, so joining normalized components with
# it yields a raw-object-identity string that is injective in (source, table, column).
_RAW_ID_SEP = "\x1f"


@dataclass(frozen=True, slots=True)
class MetadataConflict:
    """One attested ``field`` on which the rows naming a SINGLE object disagree.

    ``competing_value_hashes`` are the :func:`overlay.field_evidence.canonical_hash`es of the distinct
    disagreeing values (deduped + sorted) — the SAME hashing the field-evidence store uses for
    ``proposed_value_hash``. Sorting makes the tuple order-independent so Task 10 can mint a stable
    :func:`overlay.conflict_review.conflict_fingerprint` ``(logical_ref, field, competing_value_hashes,
    policy_version)`` and open / reopen exactly one ``conflict_review`` per disagreement.
    """

    logical_ref: str
    field: str
    competing_value_hashes: tuple[str, ...]


def _norm(value: str) -> str:
    """Normalize one ref component (strip + lower-case), mirroring ``object_ref._norm``. Used ONLY to
    build the raw-object-identity key below; the ref STRING itself is always produced by
    :func:`normalize_ref`, so the two never drift on the format that is persisted."""
    return value.strip().lower()


def logical_ref_str(row: CanonicalRow) -> str:
    """The stable ``logical_ref`` naming this row's source object — reuses :func:`normalize_ref`
    (§5.1). Uploads carry no schema, so :func:`normalize_ref` defaults it to ``public``."""
    return normalize_ref(row.source, None, row.table, row.column)


def _raw_object_id(row: CanonicalRow) -> str:
    """The row's pre-flattening object identity: normalized ``(source, table, column)``. Two rows
    share this IFF they name the same object. They can still share a ``logical_ref`` while differing
    here when an embedded separator blurs the table/column boundary — that collision is the AMBIGUOUS
    case (more than one distinct object reachable through one FQN)."""
    return _RAW_ID_SEP.join((_norm(row.source), _norm(row.table), _norm(row.column)))


def _round_trips(logical_ref: str) -> bool:
    """Whether a ``logical_ref`` pins to exactly one object: it must parse AND re-normalize to itself.
    A ref carrying an embedded separator (e.g. a dotted table name) fails this — its
    schema/table/column boundary is underdetermined, so it cannot be pinned to one object."""
    try:
        source, schema, table, column = parse_ref(logical_ref)
    except ValueError:
        return False
    return normalize_ref(source, schema, table, column) == logical_ref


def _metadata_conflicts(logical_ref: str, rows: list[CanonicalRow]) -> list[MetadataConflict]:
    """Emit one :class:`MetadataConflict` per attested field on which ``rows`` (all naming ONE
    object) disagree. A field's competing values are its DISTINCT *asserted* values across the rows;
    a default/omitted value (``""`` for text, ``False`` for a flag) is "not asserted" and never
    manufactures a conflict against a row that did assert. Deterministic: fields in
    ``_ATTESTED_FIELDS`` order, value hashes sorted."""
    conflicts: list[MetadataConflict] = []
    for field in _ATTESTED_FIELDS:
        values = {getattr(r, field) for r in rows}
        values.discard("")     # an omitted string is "not asserted", not a competing value
        values.discard(False)  # a flag's default is "not asserted" (True is the only asserted value)
        if len(values) > 1:
            hashes = tuple(sorted(canonical_hash(v) for v in values))
            conflicts.append(MetadataConflict(logical_ref, field, hashes))
    return conflicts


def classify_upload(
    rows: list[CanonicalRow],
) -> tuple[dict[str, ObjectBinding], list[MetadataConflict]]:
    """Classify an upload's rows into object bindings + metadata conflicts (review #12).

    Rows are grouped by ``logical_ref`` (:func:`normalize_ref`). Each group yields ONE
    :class:`ObjectBinding`:

      * pins to exactly one source object (the normal case, EVEN when the group's rows carry
        differing metadata) -> ``EXACT`` / attachable (:func:`may_attach` True);
      * more than one distinct source object collides onto the ref, or the FQN cannot be parsed to a
        single object -> ``AMBIGUOUS`` / not attachable.

    The pin decision reuses Phase-0 :func:`classify_identity` over the group's distinct raw object
    identities; a non-round-tripping (unparseable) FQN is ``AMBIGUOUS`` outright.

    Independently, an ``EXACT`` group whose rows disagree on an attested field (``definition``,
    ``type``, …) emits a :class:`MetadataConflict` per such field — WITHOUT downgrading the binding
    (evidence attach is never blocked on a metadata disagreement). Identical-duplicate rows collapse
    into their group and raise no conflict. An ``AMBIGUOUS`` group is not one object, so its
    differences are identity ambiguity — not field conflicts — and no conflict is emitted.

    Returns ``(bindings_by_logical_ref, conflicts)``.
    """
    groups: dict[str, list[CanonicalRow]] = {}
    for row in rows:
        groups.setdefault(logical_ref_str(row), []).append(row)

    bindings: dict[str, ObjectBinding] = {}
    conflicts: list[MetadataConflict] = []
    for ref, group in groups.items():
        candidates = tuple(sorted({_raw_object_id(r) for r in group}))
        status = (
            classify_identity(candidates)
            if _round_trips(ref)
            else ObjectIdentityStatus.AMBIGUOUS
        )
        logical: LogicalObjectRef | None = None
        if status is ObjectIdentityStatus.EXACT:
            source, schema, table, column = parse_ref(ref)
            logical = LogicalObjectRef(
                logical_catalog_id=source, schema=schema, table=table, column=column
            )
            conflicts.extend(_metadata_conflicts(ref, group))
        bindings[ref] = ObjectBinding(
            logical_ref=logical, status=status, candidates=candidates
        )
    return bindings, conflicts
