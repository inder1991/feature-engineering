"""Blocker-based, cause-labelled scoped feature-readiness diagnostics (spec §9).

This is the readiness contract Phase 0 deferred. It reads what resolve-and-project (Task 8) wrote —
the immutable ``field_decision_event`` rows via :func:`overlay.field_decision.read_field_decisions` —
together with the field-policy registry (:func:`overlay.upload.field_policies.policy_for`), and
reports what is feature-ready vs blocked AS A DIAGNOSTIC, never as a platform gate. The gate that
actually controls feature generation is recipe/run-scoped (Phase 2+); CATALOG / TABLE readiness is
for planning and UI (spec §9: "catalog-wide readiness misleads").

THE KEY DISTINCTION (review #13): every blocking requirement carries a ``cause`` so the report NEVER
conflates three very different situations:

* ``not_promoted_in_phase1`` — a structural fact (grain / join) that Phase 1 deliberately does NOT
  promote (spec §16: "no joins, no grain promotion"). It is EXPECTED — a diagnostic reminder that a
  Phase-2 promotion is pending — and must NEVER read as an ingestion failure.
* ``unresolved_authority`` — an OPERATIONAL field whose load-bearing value is unresolved because the
  ACTIVE evidence's authority is insufficient (e.g. ``additivity`` derived from a still-PROPOSED
  concept, awaiting a concept confirmation). Resolvable later; not an error.
* ``ingestion_error`` — a genuine failure: the field's active evidence CONFLICTS irreconcilably
  (the resolver could not pick a single value), so a human must reconcile the source.

BLOCKER-BASED GATE: ``operational_status`` is ``"blocked"`` iff ANY blocking requirement exists —
derived from the requirement LIST, never from the percentages. ``summary_scores`` are DISPLAY-ONLY,
derived from the same list; they never drive the gate (spec §9: "percentages are derived from the
requirement list, never the gate").
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal

from featuregen.contracts import DbConn
from featuregen.overlay.field_authority import (
    AllOf,
    AnyOf,
    AuthorityPredicate,
    HasEvidence,
    InfluenceTier,
)
from featuregen.overlay.field_decision import FieldDecisionEvent, read_field_decisions
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload.field_policies import policy_for
from featuregen.overlay.upload.object_ref import parse_ref

# --- Cause labels (the review-#13 must-fix). A blocking requirement always carries exactly one. ----
CAUSE_NOT_PROMOTED = "not_promoted_in_phase1"
CAUSE_UNRESOLVED_AUTHORITY = "unresolved_authority"
CAUSE_INGESTION_ERROR = "ingestion_error"

# A non-blocking review requirement's cause: a shown-but-unconfirmed proposal a human could promote.
CAUSE_PROPOSED_UNCONFIRMED = "proposed_unconfirmed"

# A non-None subset (a TABLE name or an explicit ref list) that resolves to ZERO in-scope refs — a
# typo'd / unknown table, or an empty ref list. Surfaced as a blocker (review-#13 Task-9 fix) so an
# unknown subset never reads as a false "ready" indistinguishable from a genuinely clean table.
CAUSE_SUBSET_NOT_FOUND = "subset_not_found"

# The schema/table qualifier in a string ``subset`` — mirrors object_ref's path separator so a TABLE
# subset may be written schema-qualified ("schema.table") to disambiguate a name shared across schemas.
_SUBSET_QUALIFIER = "."

# Decision lifecycle events that RETIRE a decision (mirrors field_resolution._RETIRED_EVENTS): a
# retired latest decision confers nothing, so the field reads as if it had no resolved value.
_RETIRED_EVENTS = frozenset({"rejected", "staled", "superseded"})

# The resolver's unresolved_reason / conflict_status marking an irreconcilable evidence conflict — a
# genuine ingestion failure, distinct from a mere authority shortfall.
_CONFLICT_MARKER = "conflict"

# confidence_band values (the enrich LLM enum is high|medium|low) that make a proposal too weak to be
# worth a human review ask — downgraded from a review requirement to an advisory gap so a stream of
# low-confidence LLM guesses never spams the review queue.
_LOW_CONFIDENCE = frozenset({"low"})

# The structural facts Phase 1 does NOT promote (spec §9 / §16). Each in-scope table gets one
# requirement per fact. Phase 2: grain/availability now READ the table's overlay fact state (Pass B
# proposes them as DRAFT facts; a human confirm makes them VERIFIED), so their requirement flips
# missing -> proposed -> confirmed instead of staying hard-coded missing. `join` stays static —
# Phase 3 owns approved_join state.
_PHASE1_UNPROMOTED: tuple[tuple[str, str], ...] = (
    ("grain", "structural_or_human"),
    ("availability", "structural_or_human"),   # Phase 2 addition
    ("join", "approved_join"),
)

# Requirement name -> the overlay fact_type Pass B proposes under (table_synth). A requirement not
# in this map (join) has no readable fact stream yet and stays a static missing/not-promoted.
_FACT_TYPE_BY_REQUIREMENT = {"grain": "grain", "availability": "availability_time"}

# Granular causes for the non-terminal lifecycle states (must not collapse to "missing"). The
# STATUS stays in the 4-value vocabulary (confirmed/proposed/missing/conflicting) the type allows;
# the CAUSE distinguishes WHY so the diagnostic is honest. Only VERIFIED is feature-ready.
CAUSE_FACT_EXPIRED = "fact_expired_awaiting_reverify"
CAUSE_FACT_STALE = "fact_staled_awaiting_reverify"
CAUSE_FACT_REJECTED = "proposal_rejected"


def _table_fact_status(conn, source, table, requirement) -> tuple[str, str]:
    """Map the table's overlay fact stream to (readiness_status, cause). readiness_status is one of
    the 4 allowed values; cause carries the granular lifecycle reason. Only VERIFIED is ready."""
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact
    from featuregen.overlay.upload.upload_catalog import table_ref
    fact_type = _FACT_TYPE_BY_REQUIREMENT.get(requirement)
    if fact_type is None:
        return "missing", CAUSE_NOT_PROMOTED
    stream = load_fact(conn, fact_key(table_ref(source, table), fact_type))
    if not stream:
        return "missing", CAUSE_NOT_PROMOTED
    status = fold_overlay_state(stream).status
    # NOTE: the folded pending-proposal status literal is "DRAFT" (state.py), NOT "PROPOSED" (that
    # is the EVENT type OVERLAY_FACT_PROPOSED). Verified in Task 7. Use "DRAFT" here.
    if status == "VERIFIED":
        return "confirmed", CAUSE_NOT_PROMOTED           # satisfied; not a blocker
    if status in ("DRAFT", "PARTIALLY_CONFIRMED"):
        return "proposed", CAUSE_PROPOSED_UNCONFIRMED     # in the review queue
    if status == "REJECTED":
        return "missing", CAUSE_FACT_REJECTED             # NOT ready; distinct from never-proposed
    if status == "EXPIRED":
        return "proposed", CAUSE_FACT_EXPIRED             # prior confirmation lapsed -> re-verify
    if status in ("STALE", "REVERIFY"):
        return "proposed", CAUSE_FACT_STALE               # drift/expiry -> awaiting re-confirm
    return "proposed", CAUSE_PROPOSED_UNCONFIRMED


class ReadinessScopeType(StrEnum):
    """The scope a readiness verdict is computed at (spec §9). CATALOG / TABLE are DIAGNOSTIC;
    GENERATION_RUN / RECIPE are the scopes at which the load-bearing gate actually decides promotion
    (Phase 2+ — not evaluated here)."""

    CATALOG = "catalog"
    TABLE = "table"
    GENERATION_RUN = "generation_run"
    RECIPE = "recipe"


@dataclass(frozen=True)
class ReadinessRequirement:
    """One thing a scope needs to be feature-ready (spec §9). ``status`` is the resolved state of the
    underlying decision; ``blocking`` gates ``operational_status``; ``cause`` (the review-#13
    addition) names WHY a blocker blocks so the report never conflates not-promoted / unresolved /
    error; ``authority_required`` is a human-readable rendering of the authority the evidence must
    satisfy."""

    requirement_id: str
    scope: ReadinessScopeType
    status: Literal["confirmed", "proposed", "missing", "conflicting"]
    blocking: bool
    cause: str
    authority_required: str


@dataclass(frozen=True)
class FeatureReadiness:
    """A scoped readiness verdict (spec §9). ``operational_status`` is the blocker-based gate;
    ``blocking_requirements`` / ``review_requirements`` / ``advisory_gaps`` are the actionable lists;
    ``summary_scores`` are DISPLAY-ONLY percentages derived from the requirement list."""

    scope: ReadinessScopeType
    operational_status: Literal["ready", "blocked"]
    blocking_requirements: tuple[ReadinessRequirement, ...]
    review_requirements: tuple[ReadinessRequirement, ...]
    advisory_gaps: tuple[str, ...]
    summary_scores: dict[str, float]


def _render_authority(pred: AuthorityPredicate | None) -> str:
    """Render an authority predicate tree to a compact, human-readable string for a requirement's
    ``authority_required`` (the brief makes this a ``str``, not the raw predicate). ``None`` (a field
    with no operational rule) renders as ``"none"``."""
    if pred is None:
        return "none"
    if isinstance(pred, HasEvidence):
        return f"{pred.producer.value}/{pred.strength.value}"
    if isinstance(pred, AnyOf):
        return "any(" + ",".join(_render_authority(c) for c in pred.conditions) + ")"
    if isinstance(pred, AllOf):
        return "all(" + ",".join(_render_authority(c) for c in pred.conditions) + ")"
    return "unknown"


def _scoped_refs(
    conn: DbConn, *, source: str, subset: str | Sequence[str] | None
) -> list[str]:
    """The in-scope ``logical_ref`` set — the universe readiness reports on (the refs Task 8 recorded
    decisions for), filtered to this ``source`` and narrowed by ``subset``.

    ``subset`` is ``None`` (the whole catalog source), a TABLE selector string, or an explicit
    sequence of logical_refs (a TABLE call the caller already resolved to refs).

    A string TABLE selector is SCHEMA-AWARE (Task-9 review fix — matching on the table name alone
    over-reported across two same-named tables in different schemas). It may be written:

    * schema-qualified — ``"schema.table"`` — matching EXACTLY the refs whose ``(schema, table)``
      equals that pair; or
    * a bare table name — ``"table"`` — matching that table WITHIN A SINGLE schema. If the bare name
      is shared across MORE THAN ONE schema it is ambiguous and raises ``ValueError`` (qualify it as
      ``schema.table``) rather than silently matching two distinct objects."""
    norm_source = source.strip().lower()
    rows = conn.execute("SELECT DISTINCT logical_ref FROM field_decision_event").fetchall()
    refs = [r[0] for r in rows if parse_ref(r[0])[0] == norm_source]
    if subset is None:
        return sorted(refs)
    if isinstance(subset, str):
        parts = subset.strip().lower().split(_SUBSET_QUALIFIER)
        if len(parts) > 2:
            raise ValueError(
                f"invalid TABLE subset {subset!r}: expected 'table' or 'schema.table'"
            )
        matches: list[str] = []
        matched_schemas: set[str] = set()
        for r in refs:
            _src, schema, table, _col = parse_ref(r)
            if len(parts) == 2:
                if (schema, table) == (parts[0], parts[1]):
                    matches.append(r)
            elif table == parts[0]:  # bare table name
                matches.append(r)
                matched_schemas.add(schema)
        if len(parts) == 1 and len(matched_schemas) > 1:
            raise ValueError(
                f"ambiguous TABLE subset {subset!r}: table {parts[0]!r} exists in schemas "
                f"{sorted(matched_schemas)} — qualify it as 'schema.table'"
            )
        return sorted(matches)
    wanted = set(subset)
    return sorted(r for r in refs if r in wanted)


def _subset_label(subset: str | Sequence[str]) -> str:
    """A compact, human-readable rendering of a ``subset`` for a ``subset_not_found`` requirement_id
    (a TABLE selector string, or the joined explicit ref list; an empty list renders ``<empty>``)."""
    if isinstance(subset, str):
        return subset.strip().lower()
    return ",".join(sorted(subset)) or "<empty>"


def _tables_of(refs: Sequence[str]) -> list[tuple[str, str]]:
    """The distinct ``(schema, table)`` pairs present in ``refs``, in stable first-seen order."""
    seen: set[tuple[str, str]] = set()
    tables: list[tuple[str, str]] = []
    for r in refs:
        _src, schema, table, _col = parse_ref(r)
        key = (schema, table)
        if key not in seen:
            seen.add(key)
            tables.append(key)
    return tables


def _decided_fields(conn: DbConn, logical_ref: str) -> list[str]:
    """The distinct fields that have a recorded decision for ``logical_ref`` (what to report on)."""
    rows = conn.execute(
        "SELECT DISTINCT field_name FROM field_decision_event WHERE logical_ref = %s",
        (logical_ref,),
    ).fetchall()
    return sorted(r[0] for r in rows)


def _status_of(latest: FieldDecisionEvent) -> Literal["confirmed", "proposed", "missing", "conflicting"]:
    """The resolved state of a field from its LATEST decision (spec §9 status vocabulary).

    A retired latest decision (rejected/staled/superseded) reads as ``missing`` (it confers nothing).
    An irreconcilable conflict reads as ``conflicting``. Otherwise a present load-bearing hash is
    ``confirmed`` (feature-eligible), a present display hash alone is ``proposed`` (shown, not
    load-bearing), and neither present is ``missing``."""
    if latest.event_type in _RETIRED_EVENTS:
        return "missing"
    if latest.conflict_status == _CONFLICT_MARKER or _CONFLICT_MARKER in latest.reason_codes:
        return "conflicting"
    if latest.load_bearing_value_hash is not None:
        return "confirmed"
    if latest.display_value_hash is not None:
        return "proposed"
    return "missing"


def _is_low_confidence(conn: DbConn, logical_ref: str, field_name: str) -> bool:
    """Whether every ACTIVE proposal for a field is low-confidence — the signal that downgrades a
    proposed advisory field from a review requirement to an advisory gap. An absent confidence band
    (``None``) is NOT low (unknown ≠ weak), so a mixed or unbanded field stays reviewable."""
    evidence = read_active_field_evidence(conn, logical_ref, field_name)
    if not evidence:
        return False
    return all(e.confidence_band in _LOW_CONFIDENCE for e in evidence)


def _summary_scores(reqs: Sequence[ReadinessRequirement]) -> dict[str, float]:
    """DISPLAY-ONLY percentages DERIVED from the requirement list (spec §9). Never consulted by the
    gate — the gate reads the blocking flags directly. An empty list is trivially fully ready."""
    total = len(reqs)
    if total == 0:
        return {"requirements": 0.0, "confirmed": 0.0, "blocking": 0.0, "review": 0.0,
                "ready_fraction": 1.0}
    confirmed = sum(1 for r in reqs if r.status == "confirmed")
    blocking = sum(1 for r in reqs if r.blocking)
    review = sum(1 for r in reqs if not r.blocking and r.status == "proposed")
    return {
        "requirements": float(total),
        "confirmed": float(confirmed),
        "blocking": float(blocking),
        "review": float(review),
        "ready_fraction": round(confirmed / total, 4),
    }


def compute_readiness(
    conn: DbConn,
    *,
    source: str,
    scope: ReadinessScopeType,
    subset: str | Sequence[str] | None = None,
) -> FeatureReadiness:
    """Compute a scoped, blocker-based readiness DIAGNOSTIC for ``source`` (spec §9).

    Reads the recorded ``field_decision_event`` rows for the in-scope logical_refs plus the field
    policies, and reports:

    * ``blocking_requirements`` — each carrying a ``cause`` (``not_promoted_in_phase1`` for the
      grain/join facts Phase 1 does not promote; ``unresolved_authority`` for an OPERATIONAL field
      whose load-bearing value is unresolved; ``ingestion_error`` for irreconcilably conflicting
      evidence). ``operational_status`` is ``"blocked"`` iff this list is non-empty.
    * ``review_requirements`` — shown-but-unconfirmed advisory proposals a human could promote.
    * ``advisory_gaps`` — soft, non-actionable notes (e.g. a low-confidence domain proposal).
    * ``summary_scores`` — DISPLAY-ONLY percentages derived from the requirement list.

    ``scope`` CATALOG reports the whole source; TABLE narrows to one table via ``subset`` — a
    SCHEMA-AWARE selector (``"schema.table"``, or a bare ``"table"`` when unambiguous within a single
    schema; see :func:`_scoped_refs`) or an explicit logical_ref list. A non-None ``subset`` that
    matches NOTHING yields a single ``subset_not_found`` blocker (never a false "ready"). GENERATION_RUN
    / RECIPE gating is Phase 2+ and is stamped on the verdict but computed with the same diagnostic."""
    refs = _scoped_refs(conn, source=source, subset=subset)

    all_reqs: list[ReadinessRequirement] = []
    advisory: list[str] = []

    # 0. A non-None subset that resolves to ZERO refs is NOT "clean" (review-#13 Task-9 fix): a
    #    typo'd / unknown table (or an empty ref list) must surface as a blocker, never read as a
    #    false "ready" indistinguishable from a genuinely clean table. (An empty CATALOG source —
    #    subset is None — stays trivially ready: the gate is blocker-based.)
    if subset is not None and not refs:
        not_found = ReadinessRequirement(
            requirement_id=f"subset_not_found:{source.strip().lower()}:{_subset_label(subset)}",
            scope=scope,
            status="missing",
            blocking=True,
            cause=CAUSE_SUBSET_NOT_FOUND,
            authority_required="none",
        )
        return FeatureReadiness(
            scope=scope,
            operational_status="blocked",
            blocking_requirements=(not_found,),
            review_requirements=(),
            advisory_gaps=(),
            summary_scores=_summary_scores([not_found]),
        )

    # 1. Structural facts — one requirement per in-scope table. Phase 2: grain/availability READ
    #    the table's overlay fact state (missing -> proposed -> confirmed); only a `missing` fact
    #    (never proposed, or proposal rejected — the cause distinguishes them) blocks. A confirmed
    #    fact is satisfied; a proposed one is a non-blocking review ask (the `blocking` partition
    #    below routes it into review_requirements). `join` stays static-missing (Phase 3).
    for schema, table in _tables_of(refs):
        for fact_name, authority in _PHASE1_UNPROMOTED:
            fact_status, fact_cause = _table_fact_status(conn, source, table, fact_name)
            all_reqs.append(
                ReadinessRequirement(
                    requirement_id=f"{fact_name}:{source}.{schema}.{table}",
                    scope=ReadinessScopeType.TABLE,
                    status=fact_status,
                    blocking=fact_status == "missing",
                    cause=fact_cause,
                    authority_required=authority,
                )
            )

    # 2. Per-field decisions — one requirement per decided policy field.
    for logical_ref in refs:
        for field_name in _decided_fields(conn, logical_ref):
            policy = policy_for(field_name)
            if policy is None:
                continue  # not a resolvable field (e.g. sensitivity_floor) — nothing to report
            decisions = read_field_decisions(conn, logical_ref, field_name)
            if not decisions:
                continue
            latest = decisions[-1]  # read_field_decisions is oldest-first
            status = _status_of(latest)
            is_operational = policy.influence_max is InfluenceTier.OPERATIONAL
            req_id = f"field:{logical_ref}:{field_name}"
            authority = _render_authority(policy.operational_rule)

            if status == "confirmed":
                # Feature-eligible: recorded for the scores, but surfaces in no actionable list.
                all_reqs.append(
                    ReadinessRequirement(req_id, ReadinessScopeType.TABLE, status, False, "",
                                         authority)
                )
                continue
            if status == "conflicting":
                # Irreconcilable evidence — a GENUINE failure, distinct from an authority shortfall.
                all_reqs.append(
                    ReadinessRequirement(req_id, ReadinessScopeType.TABLE, status, True,
                                         CAUSE_INGESTION_ERROR, authority)
                )
                continue
            # status is "proposed" or "missing": no load-bearing value.
            if is_operational:
                # An OPERATIONAL field with no load-bearing value is blocked on AUTHORITY, not error.
                all_reqs.append(
                    ReadinessRequirement(req_id, ReadinessScopeType.TABLE, status, True,
                                         CAUSE_UNRESOLVED_AUTHORITY, authority)
                )
                continue
            # An advisory field (RECOMMENDATION/DISPLAY) that is proposed-unconfirmed: a review ask,
            # UNLESS every proposal is low-confidence — then it is only an advisory gap (don't spam
            # the review queue with weak guesses). A field with no shown value at all is a soft
            # `missing` gap, not a review ask either.
            if status == "missing":
                advisory.append(f"missing:{field_name}:{logical_ref}")
            elif _is_low_confidence(conn, logical_ref, field_name):
                advisory.append(f"low_confidence:{field_name}:{logical_ref}")
            else:
                all_reqs.append(
                    ReadinessRequirement(req_id, ReadinessScopeType.TABLE, status, False,
                                         CAUSE_PROPOSED_UNCONFIRMED, authority)
                )

    blocking = tuple(r for r in all_reqs if r.blocking)
    review = tuple(r for r in all_reqs if not r.blocking and r.status == "proposed")
    operational_status: Literal["ready", "blocked"] = "blocked" if blocking else "ready"

    return FeatureReadiness(
        scope=scope,
        operational_status=operational_status,
        blocking_requirements=blocking,
        review_requirements=review,
        advisory_gaps=tuple(advisory),
        summary_scores=_summary_scores(all_reqs),
    )
