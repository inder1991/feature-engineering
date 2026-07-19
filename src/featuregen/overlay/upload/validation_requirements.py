"""Versioned validation-requirement registry + the sanctioned requirement factory (Delivery C2-C3).

Today `_validate_idea` (feature_assist.py) builds requirements inline as
`Requirement(code, operand, detail_string)` with hardcoded detail strings and no typed params or
result schema. C3 requires each requirement CODE to be defined in a VERSIONED registry that pins:
  - `subject_kind`  — what the operand refers to (e.g. "column_ref" = a (catalog_source, object_ref)),
  - `params_schema` — the typed inputs the EXTERNAL check needs (name -> python type),
  - `result_schema` — the typed shape the external check RETURNS,
  - `unit`          — the measurement unit where one is meaningful (e.g. days for a lag), else None,
  - `blocking`      — whether an unmet requirement blocks by default.

A candidate requirement is therefore an IMMUTABLE VALUE OBJECT validated against this registry: an
unknown code / version / parameter is a PROGRAMMER ERROR (a raised exception), never open JSON quietly
accepted from the LLM. `build_requirement` is the ONLY sanctioned way to mint a `Requirement` going
forward; it validates params against the schema and returns the frozen, hashable value object.

PURE module — no DB, no I/O. The registry is DETERMINISTIC and CLOSED.
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType

from featuregen.overlay.upload.feature_assist import REQUIREMENT_CODES, Requirement

DEFAULT_SCHEMA_VERSION = "v1"


class UnknownRequirement(Exception):
    """Raised by `schema_for` when a (code, schema_version) pair is not in the registry."""


class RequirementValidationError(Exception):
    """Raised by `build_requirement` when a candidate requirement violates its registry schema — an
    unknown code/version, an unknown/missing param, or a param supplied with the wrong python type.
    This signals a PROGRAMMER ERROR, not open JSON that should be tolerated."""


@dataclass(frozen=True, slots=True)
class ValidationRequirementSchema:
    """The versioned, immutable contract for one requirement CODE.

    `params_schema` / `result_schema` map a field name to its python type. A key in `params_schema`
    is REQUIRED (must be supplied) UNLESS it is also listed in `optional_params` — an optional param
    may be omitted, but if supplied it is still type-checked and no undeclared param is ever allowed.
    """

    code: str
    schema_version: str
    subject_kind: str
    params_schema: Mapping[str, type] = field(default_factory=dict)
    result_schema: Mapping[str, type] = field(default_factory=dict)
    unit: str | None = None
    blocking: bool = True
    optional_params: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        # M-1: freeze the schema mappings. `frozen=True` only blocks attribute REBINDING — it does not
        # stop a caller writing THROUGH a shared dict (`schema_for(code).params_schema["x"] = int`),
        # which would silently rewrite validation for every subsequent build_requirement. Wrapping in
        # MappingProxyType makes the returned mapping read-only, so the registry stays DETERMINISTIC and
        # CLOSED as documented. (`optional_params` is already an immutable frozenset.)
        object.__setattr__(self, "params_schema", MappingProxyType(dict(self.params_schema)))
        object.__setattr__(self, "result_schema", MappingProxyType(dict(self.result_schema)))


# ── The registry — DETERMINISTIC + CLOSED. Exactly the 8 codes in REQUIREMENT_CODES, all v1. Each
#    schema reflects what that specific external check actually NEEDS (params) and RETURNS (result). ──
_SCHEMAS: tuple[ValidationRequirementSchema, ...] = (
    # A numeric-type check needs nothing but the operand; it returns whether the column is numeric.
    ValidationRequirementSchema(
        code="TYPE_IS_NUMERIC",
        schema_version="v1",
        subject_kind="column_ref",
        params_schema={},
        result_schema={"is_numeric": bool},
    ),
    # A grain-uniqueness check counts duplicate grain values across the table.
    ValidationRequirementSchema(
        code="GRAIN_IS_UNIQUE",
        schema_version="v1",
        subject_kind="column_ref",
        params_schema={},
        result_schema={"is_unique": bool, "duplicate_count": int},
    ),
    # A temporal-population check counts null / unpopulated as-of values.
    ValidationRequirementSchema(
        code="TEMPORAL_IS_POPULATED",
        schema_version="v1",
        subject_kind="column_ref",
        params_schema={},
        result_schema={"is_populated": bool, "null_count": int},
    ),
    # A lag-bound check needs the bound (max_lag + its unit) and returns the worst observed lag.
    ValidationRequirementSchema(
        code="TEMPORAL_LAG_BOUNDED",
        schema_version="v1",
        subject_kind="column_ref",
        params_schema={"max_lag": int, "unit": str},
        result_schema={"max_observed_lag": float, "within_bound": bool},
        unit="days",
    ),
    # A join-connectivity check verifies the operand's rows actually connect across the join path.
    ValidationRequirementSchema(
        code="JOIN_CONNECTIVITY",
        schema_version="v1",
        subject_kind="column_ref",
        params_schema={},
        result_schema={"is_connected": bool, "orphan_count": int},
    ),
    # A unit-consistency check verifies the operand carries a single, consistent unit-of-measure.
    ValidationRequirementSchema(
        code="UNIT_CONSISTENT",
        schema_version="v1",
        subject_kind="column_ref",
        params_schema={},
        result_schema={"is_consistent": bool, "distinct_units": int},
    ),
    # A currency-consistency check agrees against a reference currency column WHEN one is bound.
    # `currency_ref` is OPTIONAL: `_validate_idea` mints this requirement precisely for an operand
    # whose currency is UNKNOWN (no bound currency column), so the reference ref is genuinely
    # unavailable at mint time — supply it when known, omit it (the external check discovers it) when not.
    ValidationRequirementSchema(
        code="CURRENCY_CONSISTENT",
        schema_version="v1",
        subject_kind="column_ref",
        params_schema={"currency_ref": tuple},
        result_schema={"is_consistent": bool, "distinct_currencies": int},
        optional_params=frozenset({"currency_ref"}),
    ),
    # An additivity check needs the operation being applied and returns whether it is supported.
    ValidationRequirementSchema(
        code="ADDITIVITY_SUPPORTS_OPERATION",
        schema_version="v1",
        subject_kind="column_ref",
        params_schema={"operation": str},
        result_schema={"supports": bool, "additivity_class": str},
    ),
)

REQUIREMENT_SCHEMA_REGISTRY: dict[str, ValidationRequirementSchema] = {
    s.code: s for s in _SCHEMAS
}

# Guard the closed vocabulary at import time: the registry MUST cover exactly the 8 codes and no more.
# M-3: an explicit raise, NOT a bare `assert` — `python -O` strips asserts, which would let a registry/
# vocabulary drift fail OPEN (surfacing only as an UnknownRequirement crash at the first mint). The
# integrity guard must hold even under optimized bytecode, so it fails LOUD at import.
if set(REQUIREMENT_SCHEMA_REGISTRY) != set(REQUIREMENT_CODES):
    raise RuntimeError("validation-requirement registry must cover exactly REQUIREMENT_CODES")


def schema_for(code: str, schema_version: str = DEFAULT_SCHEMA_VERSION) -> ValidationRequirementSchema:
    """Look up the schema for `code` at `schema_version`. Raises `UnknownRequirement` for an unknown
    code or a version that does not match the registered schema."""
    schema = REQUIREMENT_SCHEMA_REGISTRY.get(code)
    if schema is None:
        raise UnknownRequirement(f"unknown requirement code {code!r}")
    if schema.schema_version != schema_version:
        raise UnknownRequirement(
            f"requirement {code!r} has no schema version {schema_version!r} "
            f"(registered: {schema.schema_version!r})"
        )
    return schema


def _validate_params(
    schema: ValidationRequirementSchema, params: Mapping[str, object]
) -> tuple[tuple[str, object], ...]:
    """Validate `params` against `schema.params_schema` and return the hashable sorted (name, value)
    tuple form. Every declared param is REQUIRED unless listed in `schema.optional_params`; no extra
    params are allowed, and each supplied value must be an instance of its declared type. Any
    violation is a `RequirementValidationError`."""
    allowed = schema.params_schema
    extra = set(params) - set(allowed)
    if extra:
        raise RequirementValidationError(
            f"requirement {schema.code!r} got unknown param(s) {sorted(extra)}; "
            f"allowed: {sorted(allowed)}"
        )
    missing = (set(allowed) - schema.optional_params) - set(params)
    if missing:
        raise RequirementValidationError(
            f"requirement {schema.code!r} missing required param(s) {sorted(missing)}"
        )
    for name, expected_type in allowed.items():
        if name not in params:   # an omitted OPTIONAL param — nothing to type-check
            continue
        value = params[name]
        if not isinstance(value, expected_type):
            raise RequirementValidationError(
                f"requirement {schema.code!r} param {name!r} must be {expected_type.__name__}, "
                f"got {type(value).__name__}"
            )
        # bool is a subclass of int — a bool passed where a non-bool type (e.g. int) is expected is
        # a type error, so the typed contract is exact and not silently widened.
        if expected_type is not bool and isinstance(value, bool):
            raise RequirementValidationError(
                f"requirement {schema.code!r} param {name!r} must be {expected_type.__name__}, "
                f"got bool"
            )
    param_tuple = tuple(sorted(params.items()))
    # M-2: a bare `tuple` type-check admits a tuple carrying NESTED unhashable members (e.g. a list
    # inside a `currency_ref` tuple). That would make the resulting Requirement unhashable and blow up
    # only at a distant set/dict-key use site, not here. Probe hashability NOW so build_requirement can
    # NEVER return an unhashable value object — an unhashable param is a RequirementValidationError,
    # like any other schema violation. (Sorting above only compares the unique keys, never the values,
    # so it does not itself require hashable values.)
    try:
        hash(param_tuple)
    except TypeError as exc:
        raise RequirementValidationError(
            f"requirement {schema.code!r} has an unhashable param value: {exc}"
        ) from exc
    return param_tuple


def build_requirement(
    *,
    code: str,
    operand: tuple[str, str],
    detail: str = "",
    params: Mapping[str, object] | None = None,
    schema_version: str = DEFAULT_SCHEMA_VERSION,
) -> Requirement:
    """The ONLY sanctioned way to mint a `Requirement`.

    Validates the candidate against the versioned registry: the code must exist, the schema version
    must match, and every param must be a declared, correctly-typed, required key (no extra, no
    missing). A violation is a `RequirementValidationError` — a PROGRAMMER ERROR, never open JSON
    tolerated from the LLM. Returns the immutable, hashable `Requirement` with `params` stored as the
    sorted (name, value) tuple form.
    """
    schema = schema_for(code, schema_version)
    param_tuple = _validate_params(schema, params or {})
    return Requirement(
        code=code,
        operand=operand,
        detail=detail,
        schema_version=schema_version,
        params=param_tuple,
    )
