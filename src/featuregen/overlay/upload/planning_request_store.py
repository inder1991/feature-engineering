"""C-D11 — the typed planning request, persisted so ``DECISION_RECORD_TAMPERED`` can actually fire.

**Why the gate is inert today.** ``contract.py:374`` compares ``record["planning_request_hash"]``
against ``record["decision_manifest"]["planning_request_hash"]`` — and BOTH are written from one
in-memory object in one statement. They cannot disagree unless somebody edits the database by hand,
so the branch is unreachable through the production write path. A test that inserted two different
values would prove only that ``!=`` works.

**What makes it real** (product owner's decision, 2026-08-16): store a SECOND, INDEPENDENT source.

1. the canonical typed payload — the request's own bytes, not a summary of them;
2. its ``planning_request_hash``, stored separately from those bytes;
3. the decision record's reference to that hash.

Then :func:`load_verified_planning_request` **re-derives** the hash from the stored payload instead
of trusting the stored one:

* parse the payload back into a :class:`FeaturePlanningRequestV1`;
* recompute its canonical hash;
* compare against the stored hash — catches payload corruption;
* compare the decision record's reference against that *verified* hash — catches reference drift;
* raise :class:`DecisionRecordTampered` on either.

Corrupting any one of the three now fires the gate, because the hash is computed from the bytes
rather than read alongside them.

**Legacy rows are not evidence of tampering.** A row written before this table existed has no typed
payload, and reporting that as tampering would accuse the system of something it did not do.
:class:`LegacyPlanningRequestUnavailable` says what is actually true — there is nothing to verify
against — and is a separate named refusal so a caller can tell the two apart.

**The parser is field-exhaustive**, mirroring ``_canonical_dataclass`` in the other direction: it is
driven by each dataclass's own fields and type hints, so a field added to
``FeaturePlanningRequestV1`` round-trips without anyone editing this module. A hand-written parser
would silently drop the new field and the recomputed hash would then never match.
"""
from __future__ import annotations

import json
import types
import typing
from dataclasses import fields, is_dataclass
from typing import Any

from featuregen.overlay.upload.feature_planning_contracts import (
    FeaturePlanningRequestV1,
    planning_request_hash,
)
from featuregen.overlay.upload.recipe_grounding_context import _canonical_dataclass

__all__ = [
    "DECISION_RECORD_TAMPERED",
    "LEGACY_PLANNING_REQUEST_UNAVAILABLE",
    "DecisionRecordTampered",
    "LegacyPlanningRequestUnavailable",
    "canonical_planning_request_payload",
    "load_verified_planning_request",
    "parse_planning_request",
    "store_planning_request",
]

DECISION_RECORD_TAMPERED = "DECISION_RECORD_TAMPERED"
LEGACY_PLANNING_REQUEST_UNAVAILABLE = "LEGACY_PLANNING_REQUEST_UNAVAILABLE"


class DecisionRecordTampered(Exception):
    """The stored payload, its hash and the decision record's reference do not agree."""


class LegacyPlanningRequestUnavailable(Exception):
    """No typed payload was stored for this request — it predates the store.

    Deliberately NOT a tampering error. Reporting a legacy row as tampered would accuse the system
    of something it did not do, and would train whoever reads the alert to ignore it.
    """


def canonical_planning_request_payload(request: FeaturePlanningRequestV1) -> dict[str, Any]:
    """The request's canonical form — the SAME serialization its hash is computed over.

    Reusing ``_canonical_dataclass`` rather than writing a second serializer is the whole basis of
    the round trip: a payload serialized one way and hashed another would fail verification on
    correct data.
    """
    return _canonical_dataclass(request)


def _resolve(annotation: Any, module: str) -> Any:
    if isinstance(annotation, str):
        import sys
        return typing.ForwardRef(annotation)._evaluate(  # noqa: SLF001
            vars(sys.modules[module]), None, frozenset())
    return annotation


def _rehydrate(annotation: Any, value: Any, module: str) -> Any:
    """One value, coerced back to what ``annotation`` says it is.

    Driven by type hints so nested dataclasses and tuples come back as themselves. JSON has no
    tuple, so every list must be re-tupled — the field that makes this load-bearing is
    ``binding_hint_refs`` on a user-definition request, which is a nested tuple two levels down.
    """
    annotation = _resolve(annotation, module)
    origin = typing.get_origin(annotation)

    if origin in (types.UnionType, typing.Union):
        args = [a for a in typing.get_args(annotation) if a is not type(None)]
        if value is None:
            return None
        return _rehydrate(args[0], value, module) if len(args) == 1 else value

    if origin is tuple:
        args = typing.get_args(annotation)
        if not args or value is None:
            return tuple(value or ())
        if len(args) == 2 and args[1] is Ellipsis:
            return tuple(_rehydrate(args[0], item, module) for item in value)
        return tuple(_rehydrate(arg, item, module) for arg, item in zip(args, value, strict=False))

    if is_dataclass(annotation) and isinstance(annotation, type):
        return _from_canonical(annotation, value)

    return value


def _from_canonical(cls: type, payload: Any) -> Any:
    if payload is None:
        return None
    kwargs = {}
    for field in fields(cls):
        if field.name not in payload:
            raise DecisionRecordTampered(
                f"the stored payload has no {field.name!r} for {cls.__name__}: it was serialized "
                f"by a different build, so the hash recomputed from it could never match")
        kwargs[field.name] = _rehydrate(field.type, payload[field.name], cls.__module__)
    return cls(**kwargs)


def parse_planning_request(payload: Any) -> FeaturePlanningRequestV1:
    """The stored canonical payload, back as a typed request.

    Raises:
        DecisionRecordTampered: the payload is not a planning request this build can reconstruct.
    """
    if not isinstance(payload, dict):
        raise DecisionRecordTampered(
            f"the stored planning-request payload is {type(payload).__name__}, not an object")
    try:
        return _from_canonical(FeaturePlanningRequestV1, payload)
    except DecisionRecordTampered:
        raise
    except Exception as exc:
        raise DecisionRecordTampered(
            f"the stored planning-request payload does not reconstruct: {exc}") from exc


def store_planning_request(
    conn, *, considered_revision_id: str, option_id: str,
    request: FeaturePlanningRequestV1,
) -> str:
    """Persist the canonical payload and its hash as TWO columns, and return the hash.

    Two columns rather than one, because a hash stored inside the payload it describes cannot
    disprove the payload — which is exactly the shape the inert gate already has.
    """
    stored_hash = planning_request_hash(request)
    conn.execute(
        "INSERT INTO typed_planning_request "
        "(considered_revision_id, option_id, request_payload, planning_request_hash) "
        "VALUES (%s, %s, %s::jsonb, %s) "
        "ON CONFLICT (considered_revision_id, option_id) DO NOTHING",
        (considered_revision_id, option_id,
         json.dumps(canonical_planning_request_payload(request)), stored_hash))
    return stored_hash


def load_verified_planning_request(
    conn, *, considered_revision_id: str, option_id: str,
    decision_record_reference: str,
) -> FeaturePlanningRequestV1:
    """The five checks, in order. Any mismatch is tampering.

    Args:
        decision_record_reference: the ``planning_request_hash`` the DECISION record claims.

    Raises:
        LegacyPlanningRequestUnavailable: no typed payload — the row predates this store.
        DecisionRecordTampered: the payload, its stored hash and the reference do not all agree.
    """
    row = conn.execute(
        "SELECT request_payload, planning_request_hash FROM typed_planning_request "
        "WHERE considered_revision_id = %s AND option_id = %s",
        (considered_revision_id, option_id)).fetchone()
    if row is None:
        raise LegacyPlanningRequestUnavailable(
            f"{LEGACY_PLANNING_REQUEST_UNAVAILABLE}: no typed planning request is stored for "
            f"option {option_id!r} of {considered_revision_id!r}. The row predates this store, so "
            f"there is nothing to verify against — this is NOT evidence of tampering")

    payload, stored_hash = row[0], row[1]
    request = parse_planning_request(payload)                      # 1. parse
    recomputed = planning_request_hash(request)                    # 2. recompute

    if recomputed != stored_hash:                                  # 3. compare
        raise DecisionRecordTampered(
            f"{DECISION_RECORD_TAMPERED}: the stored planning-request payload hashes to "
            f"{recomputed} but its stored hash is {stored_hash}. The bytes and the identity "
            f"claimed for them disagree, so one of the two was changed after the fact")

    if decision_record_reference != recomputed:                    # 4. compare the reference
        raise DecisionRecordTampered(
            f"{DECISION_RECORD_TAMPERED}: the decision record references planning request "
            f"{decision_record_reference} but the stored, verified request is {recomputed}. The "
            f"decision was made about a different request than the one on file")

    return request                                                 # 5. verified
