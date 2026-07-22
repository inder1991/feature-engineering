"""Shared JSON serialization for the contract flow (single source — was duplicated in gate1 + govern)."""
from __future__ import annotations

import json

from featuregen.contracts.identity import identity_to_jsonb
from featuregen.overlay.upload.feature_assist import Requirement
from featuregen.overlay.upload.validation_requirements import (
    DEFAULT_SCHEMA_VERSION,
    RequirementValidationError,
    UnknownRequirement,
    build_requirement,
)


def actor_json(actor) -> str | None:
    """Serialize an actor to jsonb text, or None -> SQL NULL ("unknown actor"). A string subject -> a
    JSON string; an IdentityEnvelope -> identity_to_jsonb; anything else -> a structured {"repr": ...}
    (parseable JSON, never a bare Python-repr string)."""
    if actor is None:
        return None
    if isinstance(actor, str):
        return json.dumps(actor)
    try:
        return json.dumps(identity_to_jsonb(actor))
    except Exception:
        return json.dumps({"repr": str(actor)})


def _param_value_to_json(value):
    """A registry-typed param VALUE may be a tuple (e.g. a `currency_ref` (catalog, ref)); JSON has no
    tuple, so emit a list. Scalars pass through. Symmetric with `_param_value_from_json`."""
    return list(value) if isinstance(value, tuple) else value


def _param_value_from_json(value):
    """Restore a JSON list back to the tuple form the registry's typed params expect (e.g. `currency_ref`
    is a `tuple`), so a re-materialized value type-checks in `build_requirement`."""
    return tuple(value) if isinstance(value, list) else value


def requirements_to_json(reqs: tuple[Requirement, ...]) -> list[dict]:
    """Serialize typed requirements for a jsonb column / snapshot. The base shape stays
    {code, operand:[catalog, ref], detail} — byte-identical for every no-param requirement (all but
    ADDITIVITY today). The REGISTRY-typed `params` (C2-C3) are emitted ADDITIVELY, only when present, so
    the sanctioned factory can re-materialize a registry-valid requirement on read; a non-default
    `schema_version` is emitted likewise. Never carries a raw sample/PII value (detail is
    human-readable prose only)."""
    out: list[dict] = []
    for r in reqs:
        d: dict = {"code": r.code, "operand": [r.operand[0], r.operand[1]], "detail": r.detail}
        if r.params:
            d["params"] = [[name, _param_value_to_json(value)] for name, value in r.params]
        if r.schema_version and r.schema_version != DEFAULT_SCHEMA_VERSION:
            d["schema_version"] = r.schema_version
        out.append(d)
    return out


def requirements_from_json(data) -> tuple[Requirement, ...]:
    """Restore typed requirements from a jsonb column / snapshot. Tolerates a missing/None payload
    (-> empty tuple) so a pre-3A-ii snapshot deserializes as no requirements.

    C2-C3 review (I-1d): re-materialize through the SANCTIONED factory (`build_requirement`), NOT a raw
    `Requirement(...)`, so a deserialized requirement is REGISTRY-VALID — a params-carrying code (e.g.
    ADDITIVITY) is reconstructed WITH its typed params instead of a registry-invalid object that bypassed
    validation. Legacy / lossy rows (no params / schema_version, or a param a newer registry now
    requires) must STILL deserialize: they fall back to the raw value object rather than raising, since
    the confirm-time MCV re-mint is the authoritative params-carrying source (snapshots are re-derived
    at confirm)."""
    out: list[Requirement] = []
    for d in data or []:
        op = d.get("operand", ["", ""])
        code = str(d.get("code", ""))
        operand = (str(op[0]), str(op[1]))
        detail = str(d.get("detail", ""))
        schema_version = str(d.get("schema_version") or DEFAULT_SCHEMA_VERSION)
        raw_params = d.get("params")
        params = (
            {str(name): _param_value_from_json(value) for name, value in raw_params}
            if raw_params else None
        )
        try:
            out.append(build_requirement(code=code, operand=operand, detail=detail,
                                         params=params, schema_version=schema_version))
        except (RequirementValidationError, UnknownRequirement):
            # A legacy / lossy serialized row the current registry cannot mint — do NOT raise; restore
            # the immutable value object directly so the snapshot still deserializes.
            out.append(Requirement(code=code, operand=operand, detail=detail,
                                   schema_version=schema_version,
                                   params=tuple(sorted((params or {}).items()))))
    return tuple(out)
