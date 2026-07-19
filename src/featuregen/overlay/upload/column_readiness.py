"""Delivery F0 Task 1 — the per-column CAPABILITY MATRIX readiness diagnostic.

Unlike the CATALOG / TABLE :func:`overlay.upload.readiness.compute_readiness` (ONE blocker-based
verdict per scope), a COLUMN's readiness is NOT a single score: whether a column is ready depends on
the intended USE. The SAME column can be a perfectly good grain key and a hopeless measure. This
module therefore returns a MATRIX — five separate capabilities, each with its own requirement list
and its own blocker-based gate:

* ``as_measure`` — can it be aggregated as a numeric measure?
* ``as_entity_key`` — can it identify a business entity?
* ``as_event_time`` — can it stamp a point-in-time?
* ``as_grain_key`` — can it be the row grain of its table?
* ``as_join_key`` — can it connect to another table?

DIAGNOSTIC ONLY. This module READS the already-shipped authority and REPORTS; it creates nothing,
triggers no check, opens no task, and writes nothing:

* :func:`overlay.upload.operational_facts.read_operational_value` (Delivery C1) is THE per-field
  operational authority — a requirement's ``status`` / ``authority`` / evidence + fact + decision
  ids are SOURCED from the ``OperationalValue`` it returns, NEVER re-derived here. C0 (the flat
  graph read via :func:`column_authority.read_column_facts`) and C1 (the governed lifecycle) are
  merged: C1 wraps ``read_column_facts`` and is the single read this module consults for authority.
* the governed grain / availability facts are read through C1's ``is_grain`` / ``is_as_of`` axes
  (governed iff the flag is true AND the ``*_fact_event_id`` link is non-null).
* the approved_join reads (:func:`overlay.upload.graph.column_joins`) back join connectivity.

DIAGNOSTIC EXTERNAL-CHECK PREVIEWS vs BLOCKING REQUIREMENTS. A capability may advertise that a
future feature build would require an EXTERNAL data check (``TYPE_IS_NUMERIC``, ``GRAIN_IS_UNIQUE``,
``TEMPORAL_IS_POPULATED`` / ``TEMPORAL_LAG_BOUNDED``, ``CURRENCY_CONSISTENT``, ``JOIN_CONNECTIVITY``
— the closed vocabulary mirrors :data:`feature_assist.REQUIREMENT_CODES`). Such a PREVIEW is
ADVISORY (``status="review"``, ``blocking=False``, ``external_preview=True``): it is NOT a fabricated
pass, and it does NOT create a contract-specific requirement row or trigger a check — it merely names
the check a real feature build would run. Only a requirement that MUST hold for the capability is
``blocking``; a capability's ``operational_status`` is ``"blocked"`` iff any blocking requirement is
currently unmet, else ``"ready"`` — the same blocker-based gate as :class:`readiness.FeatureReadiness`
(percentages never drive it; a preview never blocks-with-no-recourse).

A capability a column PLAINLY cannot serve (a text column ``as_measure`` with a positively non-numeric
type; a plain column ``as_grain_key`` with no governed grain fact) is ``"blocked"`` with a clear
``reason``, NEVER an error.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

from featuregen.contracts import DbConn
from featuregen.overlay.catalog_changes import drift_watermark
from featuregen.overlay.upload.canonical import UNKNOWN_TYPE
from featuregen.overlay.upload.column_authority import logical_ref_of, read_column_facts
from featuregen.overlay.upload.graph import column_joins
from featuregen.overlay.upload.object_ref import parse_ref
from featuregen.overlay.upload.operational_facts import OperationalValue, read_operational_value

# The external-check PREVIEW codes — a diagnostic advertisement that a future feature build would run
# this check. Kept in LOCKSTEP with :data:`feature_assist.REQUIREMENT_CODES` (the gauntlet's closed
# vocabulary); referenced here as bare strings so this read-only diagnostic never imports the heavy
# feature-assist LLM module. A preview NEVER creates one of those requirement rows — it only names it.
_PREVIEW_TYPE_IS_NUMERIC = "TYPE_IS_NUMERIC"
_PREVIEW_CURRENCY_CONSISTENT = "CURRENCY_CONSISTENT"
_PREVIEW_GRAIN_IS_UNIQUE = "GRAIN_IS_UNIQUE"
_PREVIEW_TEMPORAL_IS_POPULATED = "TEMPORAL_IS_POPULATED"
_PREVIEW_TEMPORAL_LAG_BOUNDED = "TEMPORAL_LAG_BOUNDED"
_PREVIEW_JOIN_CONNECTIVITY = "JOIN_CONNECTIVITY"

# Numeric operational-type vocabulary — kept in lockstep with feature_assist._NUMERIC_TYPES (the ONE
# numeric-type decision the gauntlet uses); duplicated (not imported) to keep this diagnostic free of
# the feature-assist dependency chain. Raising one should raise the other.
_NUMERIC_TYPES = frozenset({
    "numeric", "integer", "bigint", "int", "int4", "int8", "smallint", "float",
    "double", "double precision", "decimal", "real", "money",
})

# C1 statuses that mark a GENUINE, irreconcilable failure (not a mere authority shortfall): the
# resolver's conflict, an ambiguous decision head, or a tamper/hash mismatch. These block regardless
# of a requirement's gate (mirrors readiness.py's ingestion_error blocker).
_CONFLICT_STATUSES = frozenset({"conflict", "fork", "hash_mismatch"})

# Machine reason labels (each requirement carries exactly one, mirroring readiness.ReadinessRequirement
# .cause — so a blocked capability never conflates "not governed yet" with "genuine error").
_R_GOVERNED = "governed"
_R_SHOWN_NOT_GOVERNED = "shown_not_governed"
_R_NO_DECISION = "no_authority_decision"
_R_NOT_PRESENT = "not_present"
_R_TYPE_UNKNOWN = "operational_type_unknown"
_R_TYPE_NOT_NUMERIC = "operational_type_not_numeric"
_R_NO_IDENTITY = "column_absent_from_catalog"
_R_EXTERNAL_PREVIEW = "external_check_preview"

# A requirement's gate — which resolved statuses BLOCK the capability:
#   "strict"   -> anything but a governed "confirmed" blocks (a fact that MUST be verified).
#   "present"  -> only a fully absent value blocks (a value that MUST at least be assigned).
#   "advisory" -> only a genuine conflict blocks (a review note that never gates on absence).
_Gate = Literal["strict", "present", "advisory"]

_Status = Literal["confirmed", "proposed", "missing", "conflicting", "review"]


@dataclass(frozen=True)
class ColumnRequirement:
    """One thing a column needs to serve ONE capability. Modelled on
    :class:`readiness.ReadinessRequirement` but per-column: ``status`` is the resolved state,
    ``blocking`` gates the capability, ``authority`` renders the C1 authority the value carries,
    and the provenance (``c1_status`` / ``evidence_ids`` / ``fact_event_id`` / ``decision_event_id``)
    is carried verbatim from the :class:`~operational_facts.OperationalValue` it was sourced from.
    ``external_preview`` marks a diagnostic external-check PREVIEW (advisory, never blocking)."""

    requirement_id: str
    status: _Status
    blocking: bool
    authority: str
    c1_status: str | None
    evidence_ids: tuple[str, ...]
    fact_event_id: str | None
    decision_event_id: str | None
    external_preview: bool
    reason: str


@dataclass(frozen=True)
class ColumnCapability:
    """One column USE and whether it is ready for it. ``operational_status`` is the blocker-based
    gate (``"blocked"`` iff any requirement is ``blocking``), mirroring
    :class:`readiness.FeatureReadiness`."""

    use: str
    operational_status: Literal["ready", "blocked"]
    requirements: tuple[ColumnRequirement, ...]


@dataclass(frozen=True)
class ColumnReadiness:
    """The per-column capability MATRIX (Delivery F0). Five independent capabilities over ONE column;
    a column may be ready for some and blocked for others."""

    source: str
    object_ref: str
    logical_ref: str
    as_measure: ColumnCapability
    as_entity_key: ColumnCapability
    as_event_time: ColumnCapability
    as_grain_key: ColumnCapability
    as_join_key: ColumnCapability


def _authority_label(ov: OperationalValue) -> str:
    """A compact rendering of the authority a C1 read carries: ``"governed"`` when the value is a
    verified governed projection; else the strongest selected evidence's ``producer/strength``; else
    the bare C1 status (``no_decision`` / ``no_value`` / ``not_operational`` / ...)."""
    if ov.status == "resolved":
        return _R_GOVERNED
    if ov.producer is not None and ov.strength is not None:
        return f"{ov.producer.value}/{ov.strength.value}"
    return ov.status


def _status_and_reason(ov: OperationalValue) -> tuple[_Status, str]:
    """Map a C1 :class:`OperationalValue` to a (readiness status, reason). A governed ``resolved`` is
    ``confirmed``; a genuine conflict/fork/tamper is ``conflicting``; a retired / absent / degraded
    read is ``missing``; a live-but-not-governed value that is SHOWN is ``proposed``."""
    s = ov.status
    if s == "resolved":
        return "confirmed", _R_GOVERNED
    if s in _CONFLICT_STATUSES:
        return "conflicting", ov.conflict_status or s
    if s in ("no_decision", "retired", "projection_unavailable"):
        return "missing", (ov.conflict_status or s) if s == "projection_unavailable" else _R_NO_DECISION
    # no_value / not_operational: a live decision/value that is not a governed load-bearing claim.
    if ov.value is not None:
        return "proposed", _R_SHOWN_NOT_GOVERNED
    return "missing", _R_NOT_PRESENT


def _blocks(status: _Status, gate: _Gate) -> bool:
    """Whether a requirement in this ``status`` currently BLOCKS its capability under ``gate``."""
    if status == "conflicting":
        return True
    if gate == "advisory":
        return False
    if gate == "present":
        return status == "missing"
    return status != "confirmed"   # "strict"


def _mk_c1_requirement(
    req_id: str, status: _Status, blocking: bool, reason: str, ov: OperationalValue
) -> ColumnRequirement:
    """Build a requirement whose status/authority/provenance are SOURCED from a C1 read."""
    return ColumnRequirement(
        requirement_id=req_id, status=status, blocking=blocking,
        authority=_authority_label(ov), c1_status=ov.status,
        evidence_ids=tuple(ov.selected_evidence_ids), fact_event_id=ov.fact_event_id,
        decision_event_id=ov.decision_event_id, external_preview=False, reason=reason,
    )


def _preview_requirement(code: str, detail: str) -> ColumnRequirement:
    """A DIAGNOSTIC external-check PREVIEW: advisory (``status="review"``), never blocking, and never
    a fabricated pass. It names the external check a future feature build would run — it creates no
    contract requirement row and triggers nothing."""
    return ColumnRequirement(
        requirement_id=f"external:{code}", status="review", blocking=False,
        authority="external_check", c1_status=None, evidence_ids=(), fact_event_id=None,
        decision_event_id=None, external_preview=True, reason=f"{_R_EXTERNAL_PREVIEW}: {detail}",
    )


def _c1_requirement(
    conn: DbConn, logical_ref: str, field_name: str, req_id: str, *, gate: _Gate
) -> ColumnRequirement:
    ov = read_operational_value(conn, logical_ref, field_name)
    status, reason = _status_and_reason(ov)
    return _mk_c1_requirement(req_id, status, _blocks(status, gate), reason, ov)


def _identity_requirement(conn: DbConn, source: str, object_ref_flat: str) -> ColumnRequirement:
    """Identity — the column node exists in this catalog. Present is satisfied; absent BLOCKS every
    capability (nothing can be asserted about a column that is not in the graph)."""
    exists = conn.execute(
        "SELECT 1 FROM graph_node WHERE catalog_source = %s AND lower(object_ref) = %s "
        "AND kind = 'column'",
        (source, object_ref_flat.lower()),
    ).fetchone() is not None
    return ColumnRequirement(
        requirement_id="identity", status="confirmed" if exists else "missing",
        blocking=not exists, authority="structural", c1_status=None, evidence_ids=(),
        fact_event_id=None, decision_event_id=None, external_preview=False,
        reason=_R_GOVERNED if exists else _R_NO_IDENTITY,
    )


def _fact_requirement(
    conn: DbConn, logical_ref: str, field_name: str, req_id: str, label: str
) -> ColumnRequirement:
    """A governed SPECIALIZED_FACT requirement (grain / availability). Governed (a VERIFIED fact —
    C1 ``resolved``, the flag true AND ``*_fact_event_id`` non-null) satisfies it; a file-DECLARED
    flag with no verified fact is ``proposed`` and still BLOCKS (only VERIFIED governs); no flag is
    ``missing`` and blocks."""
    ov = read_operational_value(conn, logical_ref, field_name)
    if ov.status == "resolved":
        status, blocking, reason = "confirmed", False, _R_GOVERNED
    elif ov.value == "true":
        status, blocking, reason = "proposed", True, f"{label}_declared_not_confirmed"
    else:
        status, blocking, reason = "missing", True, f"{label}_no_verified_fact"
    return ColumnRequirement(
        requirement_id=req_id, status=status, blocking=blocking,
        authority=_R_GOVERNED if ov.status == "resolved" else "hint", c1_status=ov.status,
        evidence_ids=(), fact_event_id=ov.fact_event_id, decision_event_id=None,
        external_preview=False, reason=reason,
    )


def _is_numeric(data_type: str | None) -> bool:
    base = (data_type or "").lower().split("(")[0].strip()   # numeric(10,2) -> numeric
    return base in _NUMERIC_TYPES


def _type_requirement(conn: DbConn, logical_ref: str) -> ColumnRequirement:
    """The ``as_measure`` operational-type requirement. The value axis of ``logical_representation``
    IS the operational ``data_type``. A KNOWN numeric type satisfies it (governed -> ``confirmed``,
    a non-governed numeric hint -> ``proposed``; either way numeric, so it never blocks). An UNKNOWN
    type (``None`` or the ``unknown`` sentinel) emits a ``TYPE_IS_NUMERIC`` external-check PREVIEW —
    advisory, never a fabricated pass. A positively NON-numeric type (text/date/...) BLOCKS with a
    clear reason: the column plainly cannot be a measure."""
    ov = read_operational_value(conn, logical_ref, "logical_representation")
    base = (ov.value or "").strip().lower()
    if base in ("", UNKNOWN_TYPE):
        return _preview_requirement(
            _PREVIEW_TYPE_IS_NUMERIC,
            "operational type not established; a numeric-type check is required before this column "
            "can serve as a measure",
        )
    if _is_numeric(base):
        governed = ov.status == "resolved"
        return _mk_c1_requirement(
            "operational_type", "confirmed" if governed else "proposed", False,
            _R_GOVERNED if governed else _R_SHOWN_NOT_GOVERNED, ov,
        )
    return _mk_c1_requirement(
        "operational_type", "missing", True, f"{_R_TYPE_NOT_NUMERIC}:{base}", ov
    )


def _currency_requirement(conn: DbConn, logical_ref: str) -> ColumnRequirement | None:
    """A ``CURRENCY_CONSISTENT`` external-check PREVIEW — ONLY for a MONETARY measure (a column that
    declares a ``currency`` hint). A non-monetary column gets no currency requirement at all."""
    if not read_column_facts(conn, logical_ref, "currency").value:
        return None
    return _preview_requirement(
        _PREVIEW_CURRENCY_CONSISTENT,
        "monetary column (currency declared); operands must be currency-consistent",
    )


def _freshness_requirement(conn: DbConn, source: str) -> ColumnRequirement:
    """Freshness — advisory (non-blocking). A source with a drift watermark has been scanned (fresh
    enough to reason over); one with none has not been observed yet. Source-level (the finest signal
    the drift scan records), surfaced per-column so a measure's staleness is visible in the matrix."""
    scanned = drift_watermark(conn, source) is not None
    return ColumnRequirement(
        requirement_id="freshness", status="confirmed" if scanned else "missing", blocking=False,
        authority="structural" if scanned else "none", c1_status=None, evidence_ids=(),
        fact_event_id=None, decision_event_id=None, external_preview=False,
        reason="source_scanned" if scanned else "no_drift_watermark",
    )


def _join_requirement(
    conn: DbConn, source: str, object_ref_flat: str, roles: Iterable[str]
) -> ColumnRequirement:
    """Join connectivity — DIAGNOSTIC, never blocking (a join key is only REQUIRED when another
    table participates). A VERIFIED ``approved_join`` touching this column confirms it; otherwise a
    ``JOIN_CONNECTIVITY`` external-check PREVIEW. Read-scoped on join endpoints via ``roles``."""
    verified = any(
        e.resolved and e.approved_join_status == "VERIFIED"
        for e in column_joins(conn, source, object_ref_flat, roles=roles)
    )
    if verified:
        return ColumnRequirement(
            requirement_id="join_connectivity", status="confirmed", blocking=False,
            authority=_R_GOVERNED, c1_status=None, evidence_ids=(), fact_event_id=None,
            decision_event_id=None, external_preview=False, reason="verified_approved_join",
        )
    return _preview_requirement(
        _PREVIEW_JOIN_CONNECTIVITY,
        "no verified approved_join yet; connectivity is checked when another table is required",
    )


def _capability(use: str, reqs: Iterable[ColumnRequirement | None]) -> ColumnCapability:
    """Fold a requirement list into a blocker-based capability verdict (drops ``None`` optionals)."""
    requirements = tuple(r for r in reqs if r is not None)
    blocked = any(r.blocking for r in requirements)
    return ColumnCapability(
        use=use, operational_status="blocked" if blocked else "ready", requirements=requirements
    )


def column_readiness(
    conn: DbConn, *, source: str, object_ref: str, roles: Iterable[str] = ()
) -> ColumnReadiness:
    """The per-column capability MATRIX for ``(source, object_ref)`` (Delivery F0) — READ-ONLY.

    Reports five independent capabilities (``as_measure`` / ``as_entity_key`` / ``as_event_time`` /
    ``as_grain_key`` / ``as_join_key``), each a blocker-based verdict over its own requirement list.
    Every requirement's status/authority/provenance is SOURCED from C1
    (:func:`operational_facts.read_operational_value`) — never re-derived. DIAGNOSTIC only: it
    creates nothing, triggers no check, and writes nothing (see the module docstring for the
    external-check PREVIEW vs blocking distinction). ``roles`` read-scopes the join-connectivity read.
    """
    norm_source = source.strip().lower()
    logical_ref = logical_ref_of(norm_source, object_ref)
    _src, _schema, table, column = parse_ref(logical_ref)
    # The public-flattened graph object_ref (matches how build_graph stores nodes/edges).
    object_ref_flat = ".".join(["public", table, *([column] if column else [])])

    identity = _identity_requirement(conn, norm_source, object_ref_flat)

    as_measure = _capability("as_measure", (
        identity,
        _c1_requirement(conn, logical_ref, "concept", "semantic_role", gate="advisory"),
        _type_requirement(conn, logical_ref),
        _c1_requirement(conn, logical_ref, "additivity", "additivity", gate="advisory"),
        _currency_requirement(conn, logical_ref),
        _c1_requirement(conn, logical_ref, "sensitivity", "safety", gate="advisory"),
        _freshness_requirement(conn, norm_source),
    ))

    as_entity_key = _capability("as_entity_key", (
        identity,
        _c1_requirement(conn, logical_ref, "entity", "entity_assignment", gate="present"),
        _c1_requirement(conn, logical_ref, "sensitivity", "safety", gate="advisory"),
    ))

    as_event_time = _capability("as_event_time", (
        identity,
        _fact_requirement(conn, logical_ref, "is_as_of", "event_time", "availability"),
        _preview_requirement(
            _PREVIEW_TEMPORAL_IS_POPULATED,
            "temporal population is an external data check (is the column reliably populated)",
        ),
        _preview_requirement(
            _PREVIEW_TEMPORAL_LAG_BOUNDED,
            "event-time lag boundedness is an external data check",
        ),
    ))

    as_grain_key = _capability("as_grain_key", (
        identity,
        _fact_requirement(conn, logical_ref, "is_grain", "grain", "grain"),
        _preview_requirement(
            _PREVIEW_GRAIN_IS_UNIQUE,
            "grain uniqueness is an external data check (is the key row-unique)",
        ),
    ))

    as_join_key = _capability("as_join_key", (
        identity,
        _join_requirement(conn, norm_source, object_ref_flat, roles),
    ))

    return ColumnReadiness(
        source=norm_source, object_ref=object_ref, logical_ref=logical_ref,
        as_measure=as_measure, as_entity_key=as_entity_key, as_event_time=as_event_time,
        as_grain_key=as_grain_key, as_join_key=as_join_key,
    )
