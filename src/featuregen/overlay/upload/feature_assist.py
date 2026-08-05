"""Phase-2 LLM feature-assist — suggestions a human acts on, never auto-wired.

Three capabilities on top of the deterministic feature spine, all via the SP-2 LLMClient seam and all
GROUNDED against the real graph (hallucinated columns are dropped):
  - recommend_features: an objective -> candidate features built from columns that actually exist.
  - feature_recipe: an NL request -> a recipe combining the LLM's intent (grain/columns/aggregation)
    with the DETERMINISTIC join path between the tables (find_join_path).
  - leakage_check: flag derives-from columns likely to be the target or derived from it.
A wrong suggestion here is a wrong *model*, so nothing is applied without a human — these return
proposals only.
"""
from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import psycopg

from featuregen.analysis.explain import (
    NEEDS_DATA_CHECK,
    NEEDS_SETUP,
    STRUCTURALLY_UNSUITABLE,
    UNDECIDED,
    UNMAPPED,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.contracts.evidence_axes import EvidenceAuthorityV1
from featuregen.intake.llm import LLMClient
from featuregen.overlay.catalog_changes import drift_watermark
from featuregen.overlay.evidence import EvidenceProducer
from featuregen.overlay.field_evidence import read_active_field_evidence
from featuregen.overlay.upload import grounding_trace as _gt
from featuregen.overlay.upload.column_authority import (
    logical_ref_of,
    read_column_facts,
)
from featuregen.overlay.upload.concepts import (
    carries_currency,
    denomination_concepts,
    is_descriptive,
    is_personal_data,
    is_protected_characteristic,
)
from featuregen.overlay.upload.enrich_llm import audited_structured_call
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CATALOG_PROJECTION_UNAVAILABLE,
    CatalogProjectionUnavailable,
)
from featuregen.overlay.upload.grounding_trace import (
    GroundingDecisionTraceV1,
    GroundingTraceRecorder,
    SuggestionDependencyClass,
)
from featuregen.overlay.upload.join_path import (
    JoinOutcome,
    JoinStep,
    classify_join_path,
    find_join_path,
    join_outcome_relationship_path,
)
from featuregen.overlay.upload.need_metadata import is_measure_need_role
from featuregen.overlay.upload.operational_facts import read_operational_value
from featuregen.overlay.upload.pii_policy_store import active_pii_use_policies
from featuregen.overlay.upload.planner.plan_envelope import PlanEnvelopeV1
from featuregen.overlay.upload.read_scope import (
    allowed_sensitivities,
    read_scope_rule_content_hash,
)
from featuregen.overlay.upload.semantic_context import (
    bundle_from_store,
    for_feature_generation,
)
from featuregen.overlay.upload.taxonomy.applicability import ConfirmedScope

logger = logging.getLogger(__name__)

# A number + a time unit, tolerating space/underscore separators: 90d, 30 d, 12m, "90 days",
# "last_12_months", "1y". (`[\s_]*` so "12_months" matches; unit optionally pluralised.)
_WINDOW_RE = re.compile(
    r"\d+[\s_]*(?:d|w|m|y|h|day|week|month|year|hour|qtr|quarter)s?\b")
# Time-window vocabulary that carries no digit. Widened after review (naming-based detection is
# inherently incomplete — the real fix is structured aggregation metadata, tracked as a follow-up).
_WINDOW_WORDS = ("trend", "rolling", "window", "velocity", "growth", "over_time", "all_time",
                 "delta", "moving", "cumulative", "running", "ytd", "mtd", "qtd", "since",
                 "lifetime", "recent", "lag", "daily", "weekly", "monthly", "quarterly",
                 "annual", "yearly", "period")
# Aggregations that sum values over rows/time — unsafe on a semi/non-additive measure.
_UNSAFE_ADDITIVE_WORDS = ("sum", "total", "cumulative", "running", "net_", "aggregate")


def _is_windowed(aggregation: str | None) -> bool:
    a = (aggregation or "").lower()
    return bool(_WINDOW_RE.search(a)) or any(w in a for w in _WINDOW_WORDS)


def _is_additive_unsafe(aggregation: str | None) -> bool:
    a = (aggregation or "").lower()
    return any(w in a for w in _UNSAFE_ADDITIVE_WORDS)


class RejectCode:
    """Machine-readable gauntlet rejection codes (SP-12 reserved single-scorer/rejection-enum hook).
    Deterministic-gate codes plus the loop's quality codes (redundant / already-registered / critic)."""
    UNGROUNDED = "UNGROUNDED"
    MALFORMED_ITEM = "MALFORMED_ITEM"       # LLM returned a non-object feature item (guarded, not fatal)
    AMBIGUOUS_CATALOG = "AMBIGUOUS_CATALOG"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    LEAKAGE = "LEAKAGE"
    STALE = "STALE"
    ADDITIVITY = "ADDITIVITY"
    MIXED_UNITS = "MIXED_UNITS"
    MIXED_CURRENCY = "MIXED_CURRENCY"
    NON_NUMERIC = "NON_NUMERIC"             # numeric op on a positively non-numeric declared type
    NO_POINT_IN_TIME = "NO_POINT_IN_TIME"
    NO_JOIN_PATH = "NO_JOIN_PATH"           # cross-table feature with no structural join path
    JOIN_DENIED = "JOIN_DENIED"             # the only path crosses a read-scope-denied hop
    REDUNDANT = "REDUNDANT"                 # near-duplicate of an already-accepted candidate (item 1a)
    ALREADY_REGISTERED = "ALREADY_REGISTERED"   # duplicates a confirmed/registered feature (item 2)
    CRITIC = "CRITIC"                       # LLM-2 critic flagged a quality/fit issue (item 5)
    NO_REVISION = "NO_REVISION"             # refine_idea: the model produced no revision to validate
    CONTEXT_TOO_LARGE = "CONTEXT_TOO_LARGE"
    # ── the USE gate (Bar 4). Sensitivity says who may SEE a column; these say whether a visible
    #    column may be USED to build a feature. See `_use_gate` for what each one reads. ──
    PROTECTED_CHARACTERISTIC = "PROTECTED_CHARACTERISTIC"   # ECOA/GDPR-Art-9 class as an input
    DESCRIPTIVE_OPERAND = "DESCRIPTIVE_OPERAND"             # a human-readable label as an operand/key
    PERSONAL_DATA_POLICY_REQUIRED = "PERSONAL_DATA_POLICY_REQUIRED"   # PII input, no allow-policy
    CURRENCY_POLICY_REQUIRED = "CURRENCY_POLICY_REQUIRED"   # amount + visible currency dim, unbound


#: Every :class:`RejectCode` mapped to one of the four product families the UI renders, so a
#: refusal is never a bare red badge (the no-blocked rule). The families are imported from the ONE
#: place they are defined — a second vocabulary spelled the same way is a vocabulary that drifts.
#:
#:   undecided               — nobody has decided yet; a human action resolves it.
#:   needs_data_check        — the metadata is settled; an observation of the DATA is outstanding.
#:   structurally_unsuitable — the column/feature cannot answer this question, ever. No setting helps.
#:   needs_setup             — an operator/governance artifact does not exist yet. Name it, don't
#:                             blame the column.
#:
#: `_validate_idea` never emits an `undecided` refusal today (an undecided FACT becomes a
#: requirement on a NEEDS_EXTERNAL_VALIDATION idea, not a rejection) — the family is listed for the
#: loop-level codes below, which really are "nobody has decided which of these duplicates wins".
_REFUSAL_FAMILIES: frozenset[str] = frozenset(
    {UNDECIDED, NEEDS_DATA_CHECK, STRUCTURALLY_UNSUITABLE, NEEDS_SETUP})

FEATURE_REFUSAL_FAMILIES: dict[str, str] = {
    # Structural: the proposal does not describe a computable, authorized feature at all.
    RejectCode.UNGROUNDED: STRUCTURALLY_UNSUITABLE,
    RejectCode.MALFORMED_ITEM: STRUCTURALLY_UNSUITABLE,
    RejectCode.AMBIGUOUS_CATALOG: STRUCTURALLY_UNSUITABLE,
    RejectCode.UNKNOWN_COLUMN: STRUCTURALLY_UNSUITABLE,
    RejectCode.LEAKAGE: STRUCTURALLY_UNSUITABLE,
    RejectCode.NON_NUMERIC: STRUCTURALLY_UNSUITABLE,
    RejectCode.NO_JOIN_PATH: STRUCTURALLY_UNSUITABLE,
    RejectCode.PROTECTED_CHARACTERISTIC: STRUCTURALLY_UNSUITABLE,
    RejectCode.DESCRIPTIVE_OPERAND: STRUCTURALLY_UNSUITABLE,
    # The declared metadata CONTRADICTS itself or the operation; only a look at the data (or a
    # correction to the declaration) settles it.
    RejectCode.ADDITIVITY: NEEDS_DATA_CHECK,
    RejectCode.MIXED_UNITS: NEEDS_DATA_CHECK,
    RejectCode.MIXED_CURRENCY: NEEDS_DATA_CHECK,
    RejectCode.NO_POINT_IN_TIME: NEEDS_DATA_CHECK,
    RejectCode.STALE: NEEDS_DATA_CHECK,
    # Something an operator or a governance owner has to create/grant. Not the column's fault.
    RejectCode.JOIN_DENIED: NEEDS_SETUP,
    RejectCode.PERSONAL_DATA_POLICY_REQUIRED: NEEDS_SETUP,
    RejectCode.CURRENCY_POLICY_REQUIRED: NEEDS_SETUP,
    RejectCode.CONTEXT_TOO_LARGE: NEEDS_SETUP,
    # Nobody has decided which of these near-identical candidates is the one to keep.
    RejectCode.REDUNDANT: UNDECIDED,
    RejectCode.ALREADY_REGISTERED: UNDECIDED,
    RejectCode.CRITIC: UNDECIDED,
    RejectCode.NO_REVISION: UNDECIDED,
}


def _validate_refusal_families() -> None:
    """Every code in the closed vocabulary declares a family, and every family is a real one.

    Import-time, so a code added without a family is a startup failure rather than a bare red badge
    discovered by a user. This is the mechanical half of the no-blocked rule.
    """
    codes = {v for k, v in vars(RejectCode).items()
             if not k.startswith("_") and isinstance(v, str)}
    missing = codes - set(FEATURE_REFUSAL_FAMILIES)
    if missing:
        raise ValueError(f"RejectCode members with no product family: {sorted(missing)}")
    unknown = set(FEATURE_REFUSAL_FAMILIES) - codes
    if unknown:
        raise ValueError(f"FEATURE_REFUSAL_FAMILIES names non-codes: {sorted(unknown)}")
    bad = {c: f for c, f in FEATURE_REFUSAL_FAMILIES.items() if f not in _REFUSAL_FAMILIES}
    if bad:
        raise ValueError(f"unknown refusal families: {bad}")


_validate_refusal_families()


def refusal_family(code: str) -> str:
    """The product family for a refusal code, or the LOUD sentinel for one this build cannot read.

    Defaulting to `undecided` would tell a reader "somebody is still deciding" about a refusal we
    cannot classify — a claim that may simply be false. `explain.UNMAPPED` is the same choice the
    selection renderer already made, for the same reason.
    """
    return FEATURE_REFUSAL_FAMILIES.get(code, UNMAPPED)


@dataclass(frozen=True, slots=True)
class Rejection:
    code: str
    message: str
    # Task 2A: WHY this candidate was refused, in the same shape a survivor carries — the pins the
    # gauntlet had already collected when it refused. Defaulted last, so every existing
    # `Rejection(code, message)` construction and every two-field comparison is unchanged, and it is
    # None on the paths that thread no candidate identity (see `_validate_idea`).
    trace: GroundingDecisionTraceV1 | None = None

    def __str__(self) -> str:
        return self.message


# Requirement codes — a CLOSED vocabulary. A requirement rides on a NEEDS_EXTERNAL_VALIDATION idea,
# tying an unverified fact (e.g. TYPE_IS_NUMERIC) to the specific named operand it concerns.
REQUIREMENT_CODES = frozenset({
    "TYPE_IS_NUMERIC", "GRAIN_IS_UNIQUE", "TEMPORAL_IS_POPULATED", "TEMPORAL_LAG_BOUNDED",
    "JOIN_CONNECTIVITY", "UNIT_CONSISTENT", "CURRENCY_CONSISTENT", "ADDITIVITY_SUPPORTS_OPERATION",
})

# The tri-state validator dispositions. A SEPARATE axis from the hyphenated `verification` stamp.
VALIDATION_STATES = ("DESIGN_CHECKED", "NEEDS_EXTERNAL_VALIDATION", "REJECTED")


@dataclass(frozen=True, slots=True)
class Requirement:
    code: str                       # in REQUIREMENT_CODES
    operand: tuple[str, str]        # (catalog_source, object_ref) the requirement concerns
    detail: str = ""                # human-readable, no PII / no sample values
    # C2-C3: a requirement is an IMMUTABLE VALUE OBJECT validated against the versioned
    # ValidationRequirementSchema registry (see validation_requirements.py). These fields are ADDED
    # LAST with defaults so every existing positional/keyword Requirement(code, operand, detail)
    # construction stays byte-identical. `params` is a sorted tuple of (name, value) pairs — a
    # HASHABLE, immutable representation (never a mutable dict) so the frozen dataclass stays hashable.
    schema_version: str = "v1"      # which registry schema version this requirement was minted against
    params: tuple[tuple[str, object], ...] = ()  # typed check parameters, sorted (name, value) pairs


def _call_raw(conn, client: LLMClient, task: str, prompt_id: str, schema_id: str,
              instruction: str, catalog_metadata: dict, *,
              actor: IdentityEnvelope | None = None,
              prompt_version: int = 1, schema_version: int = 1) -> dict:
    """Every feature-assist LLM call goes through the AUDITED seam (M6): the egress guard scans the
    user text (`instruction`) + metadata before dispatch, and the call is recorded in llm_call.
    `prompt_version`/`schema_version` (default 1 — byte-for-byte v1) pin the request's contract so the
    immutable record stamps WHICH input contract egressed, not a hardcoded 1. `actor` is the HUMAN
    subject the route threaded in; absent, the seam falls back to the service identity."""
    out = audited_structured_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, instruction=instruction, actor=actor,
        prompt_version=prompt_version, schema_version=schema_version)
    return out if isinstance(out, dict) else {}


def _candidate_columns(conn, catalog_source: str | None, roles: Iterable[str],
                       entity: str | None = None) -> list[dict]:
    # Read-scope: never feed a sensitivity-tagged column the caller can't see to the LLM (M6).
    # The LEFT JOIN reads the column's OWN table node (kind='table') for the table-level definition
    # and primary_entity — one scoped query, NOT a second unscoped fetch (spec §5). One table node
    # per (catalog, table), so the join never fans a column into duplicate rows.
    sql = ("SELECT c.catalog_source, c.object_ref, c.table_name, c.column_name, c.concept, "
           "c.domain, c.definition, c.ai_summary, c.data_type, c.declared_type, c.semantic_terms, "
           "c.entity, "
           "c.additivity, c.unit, c.currency, c.is_grain, c.is_as_of, c.grain_fact_event_id, "
           "c.availability_fact_event_id, t.definition, t.primary_entity, "
           # Release-A profile ADVISORIES (profile Task 5), from the SAME already-scoped table
           # join — never a second unscoped fetch. Display projections, so they inform the model
           # and change no gate: the numeric, currency, grain, availability and join checks stay
           # exactly as authoritative as they were.
           "t.data_role, t.authority_role, t.temporal_storage_model, t.business_context "
           "FROM graph_node c "
           "LEFT JOIN graph_node t ON t.catalog_source = c.catalog_source AND t.kind = 'table' "
           "AND t.table_name = c.table_name "
           "WHERE c.kind = 'column' "
           "AND COALESCE(c.visible_requires, '{}') <@ %s")
    params: list = [allowed_sensitivities(roles)]
    if entity:
        # Cross-domain gather: candidates from EVERY catalog that contains this entity, not one source.
        sql += (" AND c.catalog_source IN "
                "(SELECT DISTINCT catalog_source FROM graph_node WHERE entity = %s)")
        params.append(entity)
    elif catalog_source:
        sql += " AND c.catalog_source = %s"
        params.append(catalog_source)
    rows = conn.execute(sql, params).fetchall()
    return [{"catalog_source": r[0], "object_ref": r[1], "table": r[2], "column": r[3],
             "concept": r[4], "domain": r[5], "definition": r[6], "ai_summary": r[7],
             "data_type": r[8], "declared_type": r[9], "semantic_terms": r[10], "entity": r[11],
             "additivity": r[12], "unit": r[13], "currency": r[14], "is_grain": r[15],
             "is_as_of": r[16], "grain_fact_event_id": r[17], "availability_fact_event_id": r[18],
             # The LEFT JOIN's table fields sit AFTER the column fields — inserting `ai_summary`
             # shifted every index past it, and these two were the tail I missed first time.
             "table_definition": r[19], "table_primary_entity": r[20],
             "table_data_role": r[21], "table_authority_role": r[22],
             "table_temporal_storage_model": r[23], "table_business_context": r[24]}
            for r in rows]


def _menu(cols: list[dict]) -> list[dict]:
    return [{k: c[k] for k in ("object_ref", "table", "column", "concept", "domain")} for c in cols]


FEATURE_CONTEXT_FLAG = "FEATUREGEN_FEATURE_CONTEXT"


def feature_context_enabled() -> bool:
    """The single env gate for the whole Slice-3 enrichment (menu widening, per-table context,
    relevance, versioned shape). Default OFF ⟹ the thin pre-Slice-3 menu, byte-for-byte.
    RF-C3: the ONE public definition — 3a-iv imports and reuses this; never redefine it."""
    return os.environ.get(FEATURE_CONTEXT_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


#: The input contract's numeric version, stamped on the immutable llm_call record.
#:   1 — the base menu.
#:   2 — the widened feature-context menu (definition + semantic_terms prose).
#:   3 — v2 plus `ai_summary`.
#:   4 — the shared `SemanticContextBundleV1` contract (semantic Task 8): v3 plus concept ancestry,
#:       identifier namespace/issuer, party role, the D2 (producer, strength) axes per semantic
#:       field, current cross-catalog links and the closed missing-context codes.
#: Bumped because the record must identify WHICH contract egressed. Adding a field to the payload
#: while leaving the version at 2 makes a v2 record ambiguous — with or without summaries — which
#: defeats the reason the version is stamped at all.
_FEATURE_CONTEXT_SCHEMA_VERSION = 4

#: The D8 ROLLBACK LADDER, in one place:
#:   flag off                              -> v1, the thin pre-Slice-3 menu, byte-for-byte;
#:   flag on + FEATUREGEN_FEATURE_CONTEXT_VERSION=3 -> today's SHIPPED v3 behaviour;
#:   flag on (default)                     -> v4.
#: The env override exists precisely so v3 stays REACHABLE after Task 8 — a rollback that dropped
#: to the v1 thin menu would be a functional regression dressed as a safety valve. Only versions
#: this module can actually render are honoured; anything else falls back to the default and warns,
#: because a typo in a deploy manifest must not silently downgrade the contract.
FEATURE_CONTEXT_VERSION_ENV = "FEATUREGEN_FEATURE_CONTEXT_VERSION"
_SELECTABLE_CONTEXT_VERSIONS = (3, 4)


def _feature_schema_version() -> int:
    """The contract version this process will stamp AND render (the D8 ladder above).

    D10 makes registration a precondition: `enrich_llm._SCHEMAS` carries every version this can
    return, and `_require_schema` refuses to dispatch a pair it cannot resolve — so a version
    returned here without a registered body is a loud failure at dispatch, not an unenforced call
    whose response fails repair with the flag on."""
    if not feature_context_enabled():
        return 1
    raw = os.environ.get(FEATURE_CONTEXT_VERSION_ENV, "").strip()
    if not raw:
        return _FEATURE_CONTEXT_SCHEMA_VERSION
    try:
        requested = int(raw)
    except ValueError:
        requested = -1
    if requested not in _SELECTABLE_CONTEXT_VERSIONS:
        logger.warning("%s=%r is not one of %s — using v%d",
                       FEATURE_CONTEXT_VERSION_ENV, raw, list(_SELECTABLE_CONTEXT_VERSIONS),
                       _FEATURE_CONTEXT_SCHEMA_VERSION)
        return _FEATURE_CONTEXT_SCHEMA_VERSION
    return requested


# Menu fact key -> read_column_facts field_name. `data_type` reads the OPERATIONAL structural type
# under the contract's `logical_representation` authority field (value = graph_node.data_type).
_MENU_FACT_FIELDS = {
    "data_type": "logical_representation",
    "declared_type": "declared_type",
    "entity": "entity",
    "additivity": "additivity",
    "unit": "unit",
    "currency": "currency",
    "is_grain": "is_grain",
    "is_as_of": "is_as_of",
}
_MENU_IDENTITY_FIELDS = ("object_ref", "table", "column", "concept", "domain")
# Prose, so it rides the DEFINITION kind: sample-stripped and PII-scanned like `definition`, never
# passed through raw. Where a source fills descriptions by bucket this is the only text that
# distinguishes one column from its siblings.
_MENU_DEFINITION_FIELDS = ("definition", "ai_summary", "semantic_terms")
# The menu fields whose "governed" authority is LOAD-BEARING (they can clear a design check): the two
# decision-governed fields + the two fact-governed fields. Their {value, authority} is sourced from C1
# (read_operational_value) so the menu shows "governed" ONLY for a hash-verified status=="resolved" —
# a drifted / forked / hash-mismatched read shows a "hint", never a false "governed". The remaining
# menu facts (declared_type/entity/unit/currency) are hints by policy → stay on read_column_facts.
_MENU_GOVERNED_FIELDS: frozenset[str] = frozenset(
    {"logical_representation", "additivity", "is_grain", "is_as_of"})


def _enriched_column(conn, c: dict) -> dict:
    """One flag-ON menu column: structural identity bare, definition-kind free text kept (sanitized
    at egress in enrich_llm), and each governed/hint fact wrapped as {value, authority} (never a bare
    display value; spec §5). The GOVERNED-clearing facts come from C1 (read_operational_value) so the
    menu never shows a false "governed" for a drifted/tampered value; the hint facts stay on
    read_column_facts. The candidate dict carries the PUBLIC-FLATTENED object_ref, so the decision-log
    key is rebuilt through the same logical_ref_of bridge the validator uses."""
    out: dict = {}
    for k in _MENU_IDENTITY_FIELDS:
        v = c.get(k)
        if v is not None:
            out[k] = v
    for k in _MENU_DEFINITION_FIELDS:
        v = c.get(k)
        if v:
            out[k] = v
    lref = logical_ref_of(conn, c["catalog_source"], c["object_ref"])
    for menu_key, field_name in _MENU_FACT_FIELDS.items():
        if field_name in _MENU_GOVERNED_FIELDS:
            ov = read_operational_value(conn, lref, field_name)
            authority = "governed" if ov.status == "resolved" else "hint"
            out[menu_key] = {"value": ov.value, "authority": authority}
        else:
            facts = read_column_facts(conn, lref, field_name)
            out[menu_key] = {"value": facts.value, "authority": facts.authority}
    return out


def _enriched_menu(conn, cols: list[dict], *, roles: Iterable[str] = ()) -> list[dict]:
    """The flag-ON menu (feature_context_enabled()). When the flag is OFF, callers keep serving
    the thin `_menu` projection unchanged — flag-off byte-identity is a Slice-3 invariant."""
    return [_context_column(conn, c, roles=roles) for c in cols]


# ── feature-context v4: the shared semantic bundle (semantic Task 8) ───────────────────────────────

#: Per-column keys the v4 payload may SHED, in order, when the mandatory set does not fit the byte
#: budget. Prose goes first (it is the largest and the least load-bearing), then the discovery
#: extras. `missing_context`, the governed|hint fact wrappers and `semantic_authority` are NOT
#: trimmable: they are what stop the model treating an AI proposal as a governed fact, and a
#: context that dropped them to fit would be smaller and less safe.
_V4_TRIM_ORDER: tuple[str, ...] = ("semantic_terms", "ai_summary", "definition", "relationships",
                                   "concept_path")


def _semantic_authority(bundle) -> dict[str, str]:
    """``{field: "producer/strength"}`` for every semantic value that has evidence — the D2 axes ON
    THE WIRE (D10: the wire carries the triple, never the derived `llm_proposed` display label).

    This is what makes an AI-proposed concept legible AS a proposal without corrupting the
    `governed|hint` wrapper, which means something else entirely (operational influence). Lifecycle
    is omitted because the bundle only carries ACTIVE evidence — a constant on this surface is
    bytes, not information."""
    out: dict[str, str] = {}
    for value in bundle.resolved_semantics:
        if not value.evidence:
            continue
        lead = value.evidence[0]
        out[value.field_name] = f"{lead.producer}/{lead.strength}"
    return out


def _context_v4_column(conn, c: dict, *, roles: Iterable[str]) -> dict:
    """One v4 column payload: `SemanticContextBundleV1.for_feature_generation` plus the D2 axes.

    The bundle is the ONE assembly (semantic Task 1) — read-scoped and batched at that seam — so
    this path cannot drift from the Context tab or from data-agent retrieval, which read the same
    contract. Everything the adapter emits is already egress-classified (D10); the two keys added
    here (`semantic_authority`, and `party_role` promoted out of the adapter's identity block) are
    classified alongside them.

    A column whose bundle cannot be assembled (it vanished between the candidate read and here, or
    read scope narrowed) falls back to the v3 shape rather than dropping the column: a MISSING
    column is a worse answer than a thinner one, and the fallback is visible in the payload as the
    absence of the v4 keys."""
    try:
        bundle = bundle_from_store(conn, c["catalog_source"], c["object_ref"], roles=roles)
    except KeyError:
        return _enriched_column(conn, c)
    out = for_feature_generation(bundle)
    # THE MENU'S REF IS THE PUBLIC-FLATTENED GRAPH REF, NOT THE BUNDLE'S LOGICAL REF. The bundle
    # deliberately re-keys everything by the SCHEMA-PRESERVING logical ref, but grounding matches
    # an LLM-named ref against the CANDIDATE set (`known = {c["object_ref"] ...}`) and the
    # validator rebuilds the decision-log key through `logical_ref_of`. Emitting the logical form
    # here would make every ref the model faithfully copied back UNGROUNDED — the whole menu would
    # generate nothing, silently and with the flag on. The bundle's own ref rides nowhere: one
    # spelling per identity on this surface.
    out["object_ref"] = c["object_ref"]
    out["table"], out["column"] = c["table"], c["column"]
    # `missing_context` is read-scoped ABSENCE, not a data-quality verdict — it tells the model
    # what this view does not carry so it stops inferring from silence.
    out["semantic_authority"] = _semantic_authority(bundle)
    # The adapter emits `null` for anything the bundle does not hold; a null in a prompt is noise
    # the egress scanner still has to walk. Drop the empties — absence IS the honest signal.
    return {k: v for k, v in out.items() if v not in (None, "", [], {})}


def _context_column(conn, c: dict, *, roles: Iterable[str] = ()) -> dict:
    """ONE column of the flag-on menu, at whatever version the D8 ladder selected."""
    if _feature_schema_version() >= 4:
        return _context_v4_column(conn, c, roles=roles)
    return _enriched_column(conn, c)


def _trimmed(column: dict, level: int) -> dict:
    """The column payload with the first ``level`` trimmable keys removed (the explicit trim policy
    — never a silent truncation: the caller reports what it shed, per kind)."""
    if level <= 0:
        return column
    shed = set(_V4_TRIM_ORDER[:level])
    return {k: v for k, v in column.items() if k not in shed}


def _table_context(cols: list[dict]) -> list[dict]:
    """One context block per TABLE, assembled ONLY from the already-authorized candidate rows
    (spec §5): a table whose columns were all read-scope-excluded has no rows here and gets no
    block. Confirmed grain columns require a non-null grain_fact_event_id and the as-of column a
    non-null availability_fact_event_id (governed-VERIFIED, not merely file-declared);
    primary_entity is ADVISORY."""
    by_table: dict[tuple[str, str], list[dict]] = {}
    for c in cols:
        by_table.setdefault((c["catalog_source"], c["table"]), []).append(c)
    blocks: list[dict] = []
    for (_catalog, table), members in sorted(by_table.items()):
        block: dict = {"table": table}
        tdef = next((m["table_definition"] for m in members if m.get("table_definition")), None)
        if tdef:
            block["table_definition"] = tdef
        grain_cols = sorted(m["column"] for m in members
                            if m["is_grain"] and m["grain_fact_event_id"])
        if grain_cols:
            block["grain_columns"] = grain_cols
        as_of = next((m["column"] for m in sorted(members, key=lambda x: x["column"])
                      if m["is_as_of"] and m["availability_fact_event_id"]), None)
        if as_of:
            block["as_of_column"] = as_of
        pentity = next((m["table_primary_entity"] for m in members
                        if m.get("table_primary_entity")), None)
        if pentity:
            block["primary_entity"] = pentity
        block.update(_profile_advisories(members))
        blocks.append(block)
    return blocks


#: What each data role means for FEATURE construction. ADVISORY, never a gate (profile Task 5:
#: "data role produces useful warnings… it does not hard-refuse by itself"). The numeric, currency,
#: additivity, grain, availability and join checks in `_vet` remain the only things that can refuse
#: a candidate — these sentences exist so the model stops proposing the mistake in the first place,
#: which is a better outcome than rejecting it afterwards.
_DATA_ROLE_ADVISORIES: dict[str, str] = {
    "dimension": ("this table is a DIMENSION: its rows describe an entity, so aggregating its "
                  "columns over rows usually counts descriptions rather than events"),
    "snapshot_fact": ("this table is a SNAPSHOT fact: each row restates a position at a point in "
                      "time, so summing across snapshots double-counts — compare positions or "
                      "pick one as-of instant instead"),
    "crosswalk": ("this table is a CROSSWALK (identifier mapping): its columns identify, they do "
                  "not measure"),
    "reference": ("this table is a REFERENCE list: its columns label and group, they do not "
                  "measure"),
}


def _profile_advisories(members: list[dict]) -> dict:
    """The Release-A profile fields for one table block, as CONTEXT and ADVISORIES.

    Flag-gated (`FEATUREGEN_DATASET_PROFILES`): with it off, the block is byte-identical to the
    pre-profile shape — no key, not an empty one. Every value is a rebuildable DISPLAY projection,
    so nothing here is consulted as authority by anything downstream.
    """
    from featuregen.overlay.upload.profile_vocab import dataset_profiles_enabled

    if not dataset_profiles_enabled():
        return {}
    out: dict = {}
    for key, source_key in (("data_role", "table_data_role"),
                            ("authority_role", "table_authority_role"),
                            ("temporal_storage_model", "table_temporal_storage_model"),
                            ("business_context", "table_business_context")):
        value = next((m[source_key] for m in members if m.get(source_key)), None)
        if value:
            out[key] = value
    advisory = _DATA_ROLE_ADVISORIES.get(str(out.get("data_role") or ""))
    if advisory:
        out["advisories"] = [advisory]
    # A SNAPSHOT table that also carries a governed as-of column is the mismatch worth naming: the
    # time column reads like an event stream and the storage model says it is not one.
    if out.get("data_role") == "snapshot_fact" and any(
            m["is_as_of"] and m["availability_fact_event_id"] for m in members):
        out.setdefault("advisories", []).append(
            "its as-of column marks WHEN the snapshot was taken, not when an event happened — a "
            "windowed count over it counts snapshots, not activity")
    return out


# One hard byte budget on the assembled feature-context batch (spec §6). Referenced at call time so
# tests can monkeypatch it; select_relevant_context reads this module global when byte_budget is None.
#
# RE-BUDGETED for feature-context v4 (semantic Task 8). MEASURED, not guessed, against the real
# catalog shapes the review named — the committed 126-column FTR glossary export routed through the
# real reader, plus a 111-column CIB-shaped technical table, with EVERY column entity-matched so
# all 237 are mandatory (`test_feature_context_budget.py` builds both and records the numbers):
#
#   v3 mandatory bytes, 237 columns:  175_520   (~740 bytes/column)
#   v4 mandatory bytes, 237 columns:  248_601   (~1_048 bytes/column)
#   v4 with every trimmable field shed: 203_629
#
# The finding that matters: at 60_000 the SHIPPED v3 payload ALREADY raised ContextTooLarge on
# these catalogs — nearly 3x over. v4 did not create that cliff; it would have deepened it. So the
# budget is set above the measured v4 worst case with ~20% headroom for prose variance, AND the
# cliff itself is removed: an over-budget MANDATORY set is TRIMMED by the explicit `_V4_TRIM_ORDER`
# policy (prose first) and refuses only when even the fully trimmed set does not fit. A mandatory
# column is never silently dropped — a missing grain or time column produces a confidently wrong
# feature rather than a smaller one.
#
# WHAT THIS BUDGET DOES NOT BOUND — the COST. This is the assembly's own byte ceiling and nothing
# downstream caps input size: the provider call carries no input-token limit, so raising the budget
# from 60_000 to 300_000 removed a refusal, not a spend control. On the measured catalogs above v4
# sends ~2.2x the v3 prompt bytes for the same 237 columns (248_601 vs 175_520), and a mid-size
# catalog that previously refused now succeeds at ~4x the bytes it used to attempt. Input tokens are
# the cheaper half of a call and this is metadata, not data — but "cheaper" is not "free", and the
# number belongs beside the constant that produces it rather than in a review nobody re-reads.
# `FEATUREGEN_FEATURE_CONTEXT_VERSION=3` is the lever that takes it back (the D8 ladder above).
FEATURE_CONTEXT_BYTE_BUDGET = 300_000

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class ContextTooLarge(Exception):
    """The mandatory feature-context set alone exceeds the single-call byte budget — surfaced as
    RejectCode.CONTEXT_TOO_LARGE. We do NOT chunk: one audited_structured_call is one audited
    llm_call, so chunking would need N calls + cross-chunk dedup and defeat the single fail-open
    audit; relevance ordering already floats the highest-relevance items into the one bounded call
    ([F13])."""


def _tokenize(text: str | None) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _objective_tokens(objective: str | None, entity: str | None, scope) -> set[str]:
    """The objective token set, by source priority (spec §6): the GOVERNED confirmed scope (leaf ids
    + target_entity + modelling_contexts) when present and not unscoped; else the DIRECT-ASSIST
    objective free-text + explicit entity; else the LEXICAL objective alone. NO LLM call."""
    if scope is not None and not scope.unscoped:
        toks: set[str] = set()
        for uid in ([scope.primary] if scope.primary else []) + list(scope.secondary):
            toks |= _tokenize(uid)
        toks |= _tokenize(scope.target_entity)
        for mc in scope.modelling_contexts:
            toks |= _tokenize(mc)
        return toks
    return _tokenize(objective) | _tokenize(entity)


def _objective_entity(entity: str | None, scope) -> str | None:
    """The entity used for the mandatory entity-match: the confirmed target_entity (governed) else
    the explicit assist entity."""
    if scope is not None and not scope.unscoped and scope.target_entity:
        return scope.target_entity
    return entity


def _column_tokens(col: dict) -> set[str]:
    toks: set[str] = set()
    # `ai_summary` included so the agent finds a column by the same words SEARCH finds it by —
    # otherwise the two surfaces disagree about what the catalog contains.
    for k in ("object_ref", "table", "column", "concept", "domain", "semantic_terms", "entity",
              "ai_summary"):
        v = col.get(k)
        if isinstance(v, str):
            toks |= _tokenize(v)
    return toks


def _is_mandatory(col: dict, objective_entity: str | None) -> bool:
    """Always-included: a confirmed grain column, the confirmed as-of column, or a column whose
    entity matches the objective entity (spec §6)."""
    if col["is_grain"] and col["grain_fact_event_id"]:
        return True
    if col["is_as_of"] and col["availability_fact_event_id"]:
        return True
    ent = col.get("entity")
    return (objective_entity is not None and isinstance(ent, str)
            and ent.lower() == objective_entity.lower())


def _assembled_bytes(columns: list[dict], table_context: list[dict]) -> int:
    return len(json.dumps({"columns": columns, "table_context": table_context},
                          sort_keys=True, default=str).encode("utf-8"))


def select_relevant_context(conn, cols: list[dict], *, objective: str | None,
                            entity: str | None, scope=None,
                            byte_budget: int | None = None,
                            roles: Iterable[str] = ()) -> tuple[list[dict], list[dict], int]:
    """Deterministic relevance selection ([F13], spec §6). Returns
    (selected_enriched_columns, table_context, dropped_count). Mandatory columns (confirmed grain,
    as-of, entity-match) are ALWAYS included; the rest are added by descending shared-token score,
    stable (-score, object_ref asc), until the ONE hard byte budget on the assembled batch is
    reached. Logs the dropped count.

    ENRICHMENT IS LAZY (semantic Task 8). It used to build the enriched payload for EVERY candidate
    before scoring — hundreds of per-column reads whose results the budget then discarded. Scoring
    needs only the already-loaded candidate row, so enrichment now happens per column, memoized,
    as the budget admits it: the same output, bounded by what actually fits rather than by catalog
    size. This matters more at v4, where a column costs a semantic bundle rather than a few
    scalar reads.

    OVER-BUDGET MANDATORY SET: trimmed, not refused (the explicit `_V4_TRIM_ORDER` policy). Prose
    is shed first, level by level, and the level that fits is used; `ContextTooLarge` is raised only
    when even the fully trimmed mandatory set exceeds the budget. Dropping a mandatory column
    instead would silently remove the grain or the time anchor and produce a confidently wrong
    feature."""
    if byte_budget is None:
        byte_budget = FEATURE_CONTEXT_BYTE_BUDGET
    obj_tokens = _objective_tokens(objective, entity, scope)
    obj_entity = _objective_entity(entity, scope)
    enriched_by_ref: dict[tuple[str, str], dict] = {}

    def _enriched(rows: list[dict], *, trim: int = 0) -> list[dict]:
        out: list[dict] = []
        for c in rows:
            key = (c["catalog_source"], c["object_ref"])
            if key not in enriched_by_ref:
                enriched_by_ref[key] = _context_column(conn, c, roles=roles)
            out.append(_trimmed(enriched_by_ref[key], trim))
        return out

    mandatory = [c for c in cols if _is_mandatory(c, obj_entity)]
    optional = [c for c in cols if not _is_mandatory(c, obj_entity)]
    scored = sorted(optional,
                    key=lambda c: (-len(_column_tokens(c) & obj_tokens), c["object_ref"]))

    selected = list(mandatory)
    trim = 0
    while (_assembled_bytes(_enriched(selected, trim=trim), _table_context(selected))
           > byte_budget):
        if trim >= len(_V4_TRIM_ORDER):
            raise ContextTooLarge(
                f"mandatory feature context ({len(mandatory)} columns) exceeds byte budget "
                f"{byte_budget} even with every trimmable field removed "
                f"({', '.join(_V4_TRIM_ORDER)}); not chunking")
        trim += 1
    if trim:
        logger.info("feature-context trimmed %s from %d mandatory columns to fit byte budget %d",
                    ", ".join(_V4_TRIM_ORDER[:trim]), len(mandatory), byte_budget)
    dropped = 0
    for i, c in enumerate(scored):
        trial = selected + [c]
        if _assembled_bytes(_enriched(trial, trim=trim), _table_context(trial)) > byte_budget:
            dropped = len(scored) - i
            break
        selected = trial
    if dropped:
        logger.info("feature-context relevance dropped %d of %d optional columns (byte budget %d)",
                    dropped, len(optional), byte_budget)
    return _enriched(selected, trim=trim), _table_context(selected), dropped


def _build_menu(conn, cols: list[dict], *, objective: str | None = None,
                entity: str | None = None, scope=None,
                roles: Iterable[str] = ()) -> tuple[list[dict], list[dict]]:
    """The menu + per-table context for one generation call. Flag-OFF ⟹ the thin pre-Slice-3 menu
    and NO context (byte-identical). Flag-ON ⟹ the enriched, relevance-selected menu + context
    (may raise ContextTooLarge).

    `roles` is the CALLER's scope, threaded down to the v4 bundle build (D11). The candidate rows
    were already read-scoped; the bundle re-applies the same scope at its own boundary rather than
    inheriting a clearance from a row that passed it earlier."""
    if not feature_context_enabled():
        return _menu(cols), []
    columns, table_context, _dropped = select_relevant_context(
        conn, cols, objective=objective, entity=entity, scope=scope, roles=roles)
    return columns, table_context


# ── H1a carry-through value objects ────────────────────────────────────────────────────────────────
# Small, frozen, HASHABLE (tuple members only) value objects the feature assistant carries so H1b's
# Gate-1 confirmation write and H3's planner have their metadata. H1a establishes the SHAPE only; H1b
# mints the durable ids / persists the CONFIRMED bindings. Every field is defaulted so an idea that
# carries none serializes byte-identically to the pre-H1a shape.
@dataclass(frozen=True, slots=True)
class RoleBinding:
    """One role→source binding on a FeatureIdea (entity / time / currency / measure …). `ref` is the
    (catalog_source, object_ref) the role bound to; `authority` is the governing authority (governed /
    declared / hint); `confirmation_required` flags a binding the human must confirm at Gate 1."""
    role: str = ""
    ref: tuple[str, str] | None = None
    evidence_ids: tuple[str, ...] = ()
    fact_ids: tuple[str, ...] = ()
    authority: str = ""
    confirmation_required: bool = False

    def to_json(self) -> dict:
        d: dict = {"role": self.role, "authority": self.authority}
        if self.ref is not None:
            d["ref"] = [self.ref[0], self.ref[1]]
        if self.evidence_ids:
            d["evidence_ids"] = list(self.evidence_ids)
        if self.fact_ids:
            d["fact_ids"] = list(self.fact_ids)
        if self.confirmation_required:
            d["confirmation_required"] = True
        return d

    @staticmethod
    def from_json(d: dict) -> RoleBinding:
        ref = d.get("ref")
        return RoleBinding(
            role=str(d.get("role", "")),
            ref=(str(ref[0]), str(ref[1])) if ref else None,
            evidence_ids=tuple(str(x) for x in d.get("evidence_ids", ())),
            fact_ids=tuple(str(x) for x in d.get("fact_ids", ())),
            authority=str(d.get("authority", "")),
            confirmation_required=bool(d.get("confirmation_required", False)))


@dataclass(frozen=True, slots=True)
class ExternalRequirementPreview:
    """A PREVIEW of an external-validation requirement carried on a candidate (content + schema version
    + content hash). H1b mints the durable requirement ids from these previews; H1a only carries them."""
    content: str = ""
    schema_version: str = "v1"
    content_hash: str = ""

    def to_json(self) -> dict:
        return {"content": self.content, "schema_version": self.schema_version,
                "content_hash": self.content_hash}

    @staticmethod
    def from_json(d: dict) -> ExternalRequirementPreview:
        return ExternalRequirementPreview(
            content=str(d.get("content", "")),
            schema_version=str(d.get("schema_version", "v1")),
            content_hash=str(d.get("content_hash", "")))


@dataclass(frozen=True, slots=True)
class FeatureIdea:
    name: str
    description: str
    derives_from: list[str]           # object_refs, grounded (they exist in the graph)
    aggregation: str | None
    grain_table: str | None
    # B3: (catalog_source, object_ref) resolved at recommend time from the candidate context, so
    # downstream carries the catalog and never re-derives it ambiguously from the whole graph.
    derives_pairs: tuple[tuple[str, str], ...] = ()
    # §14.5 honest verification stamp. In the no-DB world a gauntlet-passed candidate is DESIGN-CHECKED
    # (structurally safe — leakage/freshness/additivity/point-in-time); predictive value is unverified
    # until a downstream backtest (DATA-/USEFULNESS-CHECKED). Never a production-ready claim.
    verification: str = "DESIGN-CHECKED"
    # Residual ADVISORY note from the LLM-2 critic when it was still unsatisfied after the review cap —
    # the feature goes forward to Gate #1 carrying it, and the HUMAN decides whether it's a fit.
    critic_note: str = ""
    # §14.2 reason->rules: a one-line causal rationale for WHY this feature operationalizes the
    # hypothesis, surfaced at Gate #1 so the reviewer audits the logic before any code exists.
    rationale: str = ""
    # ── Slice 3 typed computation operands (deterministically resolved from the proposal) ──
    operation_kind: str = ""                              # "sum"|"count"|"avg"|"ratio"|"recency"|...
    measure_refs: tuple[tuple[str, str], ...] = ()        # (catalog_source, object_ref) columns aggregated
    grain_ref: tuple[str, str] | None = None              # the grain the feature is computed per
    time_ref: tuple[str, str] | None = None               # the point-in-time column
    window: str | None = None                             # e.g. "90d"
    grouping_refs: tuple[tuple[str, str], ...] = ()       # group-by columns
    # ── Slice 3 tri-state honest status (a NEW axis; `verification` above is unchanged) ──
    validation_status: str = "DESIGN_CHECKED"             # in VALIDATION_STATES
    requirements: tuple[Requirement, ...] = ()            # typed requirements on named operands
    # 3C.2a — governed-plan carry-forward + provenance. All defaulted so every existing constructor
    # and persisted snapshot stays valid: an LLM/single-catalog idea has no envelope, origin "llm",
    # and today's permissive path authority. A governed cross-catalog option carries the exact
    # compiled plan envelope so drafting NEVER recomputes a permissive path.
    plan_envelope: PlanEnvelopeV1 | None = None
    origin: str = "llm"
    path_authority: str = "single_or_llm"
    # ── H1a carry-through metadata (additive; all defaulted so every existing constructor + persisted
    #    snapshot stays byte-identical). Consumed by H1b's Gate-1 confirmation write and H3's planner.
    #    RECONCILES with the 3C.2a fields — it does NOT duplicate them:
    #      • generation_source is the AUTHORITATIVE, SERVER-assigned generation-path label
    #        (recipe | llm_freeform | user_defined). It is NEVER read from LLM/client output. `origin`
    #        ("llm" / "governed_planner") is KEPT as-is for the 3C.2a envelope-path back-compat; the two
    #        differ by design (origin = envelope provenance, generation_source = server path authority).
    #      • planner_applicability is DERIVED from the governed plan_envelope + cross-catalog flag state:
    #        a governed plan_envelope present ⟹ "applicable_cross_catalog"; a recipe idea with no
    #        envelope ⟹ "not_applicable_single_catalog"; a non-recipe (llm_freeform) idea ⟹
    #        "not_applicable_nonrecipe" (default); a recipe eligible-but-flag-off ⟹ "gated_off". It maps
    #        onto path_authority ("single_or_llm" / "governed_cross_catalog") without repurposing it.
    generation_source: str = "llm_freeform"
    recipe_id: str | None = None
    candidate_status: str = ""
    input_role_bindings: tuple[RoleBinding, ...] = ()
    external_requirement_previews: tuple[ExternalRequirementPreview, ...] = ()
    metadata_snapshot_id: str | None = None            # the C0 snapshot this idea was grounded on
    metadata_input_fingerprint: str | None = None
    binding_fact_keys: tuple[str, ...] = ()            # entity/time/currency fact keys used
    planner_applicability: str = "not_applicable_nonrecipe"
    physical_plan_id: str | None = None
    planner_declaration_id: str | None = None
    # ── The TEMPLATE AUTHOR's own declaration of what each bound operand IS: (object_ref, role)
    #    pairs carried straight off grounding's `binding_resolutions` (`Need.role` — "stock_col" /
    #    "flow_col" / "asof" / "entity" / "event_ts", hand-written in the recipe library and versioned
    #    in the repo). SORTED + deduped so the frozen dataclass is deterministic and hashable, matching
    #    `Requirement.params`' precedent.
    #
    #    EMPTY for an LLM-proposed candidate: it has no template, so there is no declaration to carry.
    #    An absent role is NOT a licence to guess — every consumer must fall back to today's behaviour
    #    on it. A role is NEVER inferred from a concept (concepts are AI-PROPOSED, so one wrong concept
    #    would rest a safety decision on a guess), a column name, or a data type.
    #
    #    Purely carried here — nothing reads it yet; no disposition, requirement or status depends on it.
    operand_roles: tuple[tuple[str, str], ...] = ()
    # ── D14: the governed `pii_use_policy` revisions that LICENSED this feature's personal-data
    #    operands. EMPTY for the overwhelming majority of features, which bind no personal data at
    #    all — empty means "nothing needed licensing here", never "we did not check": a candidate
    #    that DID need a policy and lacked one was refused and never became a FeatureIdea.
    #
    #    THIS IS PROVENANCE, NOT A REQUIREMENT. A `Requirement` names an external CHECK somebody
    #    must run against the data before the feature can be trusted; a policy revision is a
    #    decision that was already taken, so putting it in `requirements` would ask a reviewer to
    #    re-verify an approval. It rides beside `binding_fact_keys` for the same reason those do:
    #    the immutable, content-addressed ids of the governed facts this candidate leaned on, so
    #    "who allowed this feature, and under what purpose" is answerable from the feature itself
    #    rather than by re-deriving the gate's reasoning later.
    #
    #    Sorted + deduped so the frozen dataclass stays deterministic and hashable.
    personal_data_policy_revision_ids: tuple[str, ...] = ()
    # ── Task 2A: the DECISION TRACE this candidate's validation produced (freeze 0F-7 P1) ──
    #    Defaulted last and never serialized: it is TRANSIENT CARRY from the gauntlet to the
    #    projection built in the same call. A persisted-and-reloaded idea has None here, which is
    #    correct — V2 assembly always consumes FRESH grounding output, never a reloaded snapshot,
    #    so a trace that survived a round trip could only ever describe a decision made elsewhere.
    #    None also on every path that threads no candidate identity (LLM / planner / confirm-time
    #    revalidation). Nothing in the V1 payload reads it.
    grounding_trace: GroundingDecisionTraceV1 | None = None


def _column_meta(conn, pairs: list[tuple[str, str]]) -> dict[str, dict]:
    """Additivity/catalog for each (catalog_source, object_ref) pair — scoped to the EXACT pair, so a
    same-named column in another catalog cannot contaminate the reading (M3), and a fabricated pair is
    simply absent from the result (used for the M4 existence check).

    `concept` and `table_name` ride along for the USE gate (`_use_gate`). They come off the SAME
    already-scoped row rather than a second query, for the reason `_candidate_columns` states about
    its own table join: a second fetch is a second chance to read a different catalog's column."""
    if not pairs:
        return {}
    refs = [ref for _, ref in pairs]
    rows = conn.execute(
        "SELECT catalog_source, object_ref, additivity, unit, currency, concept, table_name "
        "FROM graph_node WHERE kind = 'column' AND object_ref = ANY(%s)", (refs,)).fetchall()
    wanted = set(pairs)
    return {ref: {"catalog_source": cs, "additivity": add, "unit": unit, "currency": cur,
                  "concept": concept, "table_name": table}
            for cs, ref, add, unit, cur, concept, table in rows if (cs, ref) in wanted}


# Aggregation words that REQUIRE a numeric measure (ratio/mean/sum/…); count/count_distinct do not.
_NUMERIC_OP_WORDS = ("sum", "total", "avg", "average", "mean", "ratio", "rate", "net_",
                     "percent", "pct", "std", "variance", "median")


#: Aggregations that COUNT ROWS and never touch the amounts themselves. Mirrors
#: `analysis.grounding._COUNTING_OPS`, which draws the same line for the same reason.
_COUNTING_OPS = frozenset({"count", "count_distinct", "distinct_count"})


def _needs_numeric(aggregation: str | None) -> bool:
    a = (aggregation or "").lower()
    return any(w in a for w in _NUMERIC_OP_WORDS)


def _mixes_currency_values(aggregation: str | None) -> bool:
    """Would this aggregation COMBINE OR RETURN the amounts themselves — the only case in which a
    missing denomination can produce a wrong number?

    `count(amount)` and `count_distinct(amount)` answer "how many", and how many is the same number
    in dollars and in fils: no currency policy could change the result, so demanding one would be a
    refusal with nothing behind it — and a refusal a reviewer cannot act on teaches them to ignore
    the whole class. Everything else stays gated. Two adjudications are worth stating because they
    are not obvious:

    * `latest` / `max` / `min` are GATED. They pick rather than combine, but they hand back a value
      that IS denominated in something the caller was never told, so the wrong-number problem simply
      moves one row along.
    * an UNRECOGNISED aggregation is GATED. The aggregation is free text from a model, so failing
      closed on the unknown is the only safe direction: the exemption has to be EARNED by matching a
      counting word, never granted by failing to match a value-mixing one.

    A trailing window is stripped first (`count_90d`, `count_distinct 30 days`), because a window
    narrows WHICH rows are counted and never makes counting currency-sensitive.
    """
    stem = _WINDOW_RE.sub(" ", (aggregation or "").strip().lower())
    return re.sub(r"[^a-z]+", "_", stem).strip("_") not in _COUNTING_OPS


def _window_of(aggregation: str | None) -> str | None:
    m = _WINDOW_RE.search((aggregation or "").lower())
    return m.group(0) if m else None


def _as_of_column_ref(conn, catalog_source: str, table: str) -> str | None:
    row = conn.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source = %s AND table_name = %s "
        "AND is_as_of = true AND kind = 'column' LIMIT 1", (catalog_source, table)).fetchone()
    return row[0] if row else None


def _grain_column_ref(conn, catalog_source: str, table: str) -> str | None:
    row = conn.execute(
        "SELECT object_ref FROM graph_node WHERE catalog_source = %s AND table_name = %s "
        "AND is_grain = true AND kind = 'column' LIMIT 1", (catalog_source, table)).fetchone()
    return row[0] if row else None


def _ground_refs(raw_refs: object, known: set[str]) -> list[str]:
    """Resolve each LLM-proposed ``derives_from`` entry to a real catalog ``object_ref``. Exact match
    first; else a UNIQUE bare-column-name / suffix match, so a model that emits ``actual_tran_amt``
    (or ``public.t.actual_tran_amt`` verbatim) both ground to the same object_ref — the model's
    reference FORMAT must not silently un-ground an otherwise-valid feature. Ambiguous column names
    (same name in >1 table) and unknown refs are dropped. Order-preserving + de-duplicated."""
    by_col: dict[str, str | None] = {}
    for ref in known:
        col = ref.rsplit(".", 1)[-1]
        by_col[col] = None if col in by_col else ref   # 2nd occurrence -> None marks it AMBIGUOUS
    # The model returns derives_from as EITHER a JSON list OR a single string — and measured on Opus, that
    # string is frequently a COMMA/semicolon/newline-separated list of several refs
    # ("public.t.a, public.t.b, public.t.c"). Split it so a multi-column feature grounds on ALL its
    # columns; a bare string wrapped whole would only ever match its LAST ref via the suffix resolver,
    # silently collapsing a 5-column feature to 1 (the cause of the mis-grounded free-form features).
    if isinstance(raw_refs, str):
        raw_refs = [p.strip() for p in re.split(r"[,;\n]", raw_refs) if p.strip()]
    elif not isinstance(raw_refs, list):
        raw_refs = []
    out: list[str] = []
    seen: set[str] = set()
    for r in raw_refs:
        if not isinstance(r, str):
            continue
        resolved = r if r in known else by_col.get(r.rsplit(".", 1)[-1])
        if resolved and resolved not in seen:
            seen.add(resolved)
            out.append(resolved)
    return out


# The AI's suggestion is DISPLAY TEXT on a review card, so it is bounded like one. T2's drafter
# already caps a drafted unit at 32 chars; this is the independent read-side ceiling (a suggestion
# from any future writer can never turn a requirement into an unbounded payload).
_MAX_SUGGESTION_LEN = 64


def _ai_suggestion_with_evidence(conn, logical_ref: str, field_name: str):
    """:func:`_ai_suggestion`'s value PLUS the evidence axes of the records it read, from the ONE
    read both need (Task 2A). The trace pins what this read saw; the gauntlet uses only the value.

    The records are real ``field_evidence`` rows, so every axis — producer, strength, lifecycle
    (``active`` by the reader's own filter) — and the occurrence ids are the store's, not inferred.
    """
    records = [e for e in read_active_field_evidence(conn, logical_ref, field_name)
               if e.producer == EvidenceProducer.LLM.value and e.proposed_value is not None]
    values = {str(e.proposed_value).strip() for e in records}
    values.discard("")
    evidence = tuple(EvidenceAuthorityV1(e.producer, e.strength, e.lifecycle, e.producer_ref,
                                         e.evidence_id)
                     for e in records)
    if len(values) != 1:
        return None, evidence
    return next(iter(values))[:_MAX_SUGGESTION_LEN], evidence


def _ai_suggestion(conn, logical_ref: str, field_name: str) -> str | None:
    """The AI's ACTIVE ``llm/proposed`` value for ``field_name`` at this column, for SURFACING on a
    requirement (E4a T3) — or ``None`` when the AI proposed nothing.

    THIS NEVER AFFECTS A DISPOSITION. The gauntlet clears ``UNIT_CONSISTENT`` /
    ``CURRENCY_CONSISTENT`` from ``graph_node.unit``/``.currency`` (``_column_meta``) and from
    nothing else; this reads ``field_evidence``, which that path never consults. Its only job is to
    turn "unit unknown" into "unit not confirmed — AI suggests AED" so a human can confirm it in one
    action. Producer-scoped to ``llm`` (a source/human value would have projected and there would be
    no requirement to decorate), and a self-contradicting AI (two active values) suggests NOTHING
    rather than picking one arbitrarily."""
    return _ai_suggestion_with_evidence(conn, logical_ref, field_name)[0]


def _pin_governed(trace: GroundingTraceRecorder, catalog_source: str, object_ref: str,
                  field_name: str, dependency_kind: str, ov) -> None:
    """Pin ONE C1 governed read from the value it already returned (Task 2A; no second read).

    The CONTENT is what decides — the value and the C1 status, because only ``resolved`` may clear a
    check; the exact decision / fact event is the REVISION pin, which is provenance for currentness
    and is excluded from every content identity. The evidence axes are the read's own selected
    evidence, so a value that weakens from ``confirmed`` to ``proposed`` moves the trace hash while
    a replayed identical occurrence does not.
    """
    trace.pin(SuggestionDependencyClass.VALIDATION, dependency_kind,
              _gt.column_dependency_key(catalog_source, object_ref),
              {"field_name": field_name,
               "value": None if ov.value is None else str(ov.value),
               "status": ov.status},
              current_revision_id=ov.decision_event_id or ov.fact_event_id,
              evidence=_gt.governed_read_evidence(ov))


FEATURE_USE_GATE_FLAG = "FEATUREGEN_FEATURE_USE_GATE"
_FLAG_OFF = frozenset({"0", "false", "no", "off"})


def feature_use_gate_enabled() -> bool:
    """The USE gate ships ON. The flag exists to DISABLE it, never to enable it.

    Every other flag in this module defaults OFF because it widens behaviour, and a widening that
    nobody asked for is a surprise. This one NARROWS: it closes the Release-A finding that a
    visible PII column, a protected characteristic, a currency-blind amount and a free-text label
    were all accepted as DESIGN_CHECKED with zero requirements. Default-off would have shipped the
    hole with a switch beside it, so the default is on and the escape hatch is explicit.
    """
    return os.environ.get(FEATURE_USE_GATE_FLAG, "1").strip().lower() not in _FLAG_OFF


def _denomination_siblings(conn, tables: set[tuple[str, str]]) -> dict[tuple[str, str], str]:
    """(catalog_source, table) -> the object_ref of a currency-dimension column ON that table.

    "The currency column exists on the same table" is the fact that makes a currency-blind amount a
    refusal rather than an unanswerable question: the platform can SEE the denomination and the
    feature dropped it, and the refusal can name the exact column that fixes it. Concept-driven —
    :func:`concepts.denomination_concepts` decides what a currency column IS, never a column name.
    Deterministic pick (lowest object_ref) so the refusal text is stable across runs.
    """
    if not tables:
        return {}
    sources = [src for src, _table in tables]
    names = [table for _src, table in tables]
    rows = conn.execute(
        "SELECT catalog_source, table_name, min(object_ref) FROM graph_node "
        "WHERE kind = 'column' AND concept = ANY(%s) "
        "AND (catalog_source, table_name) IN (SELECT * FROM unnest(%s::text[], %s::text[])) "
        "GROUP BY catalog_source, table_name",
        (sorted(denomination_concepts()), sources, names)).fetchall()
    return {(src, table): ref for src, table, ref in rows}


@dataclass(frozen=True, slots=True)
class UseGateOutcome:
    """The USE gate's answer: the refusal (or its absence) AND what licensed the clearing.

    Two fields rather than a bare ``Rejection | None`` because a personal-data operand can now be
    CLEARED, and a clearing that names nothing is indistinguishable from a gate that never asked.
    ``personal_data_policy_revision_ids`` are the immutable, content-addressed policy revisions that
    covered this candidate's pii-classed concepts — the answer to "who allowed this feature, and
    under what purpose", carried onto the accepted idea so it survives past the validator."""

    rejection: Rejection | None = None
    personal_data_policy_revision_ids: tuple[str, ...] = ()


def _use_gate(conn, pairs: list[tuple[str, str]], meta: dict[str, dict],
              aggregation: str | None = None) -> UseGateOutcome:
    """May a feature be BUILT from these operands? Four refusals, or a clearing.

    THE FINDING THIS CLOSES (Release-A evaluation, 2026-08-03). ``sensitivity`` controls who may SEE
    a column and nothing controlled whether a visible column may be USED. Of five unsafe gold
    classes the platform refused exactly one — target leakage. This is the other four.

    WHAT IT READS. The concept REGISTRY (:mod:`concepts`) and the column's own governed currency
    fact. Never a column name: ``sol_desc`` is refused because its concept is ``branch_name`` and
    the registry marks that concept descriptive, and a column called ``customer_description`` whose
    concept is ``customer_id`` is NOT refused. An operand with no concept at all is not refused by
    this gate — absence is not an assertion, and the ungoverned-catalog case must keep working.

    WHY A CONCEPT MAY DRIVE THIS WHEN IT MAY NOT DRIVE THE UNIT CHECK. The unit/currency narrowing
    a few blocks down refuses to read concepts, and says so loudly: one wrong AI-proposed concept
    would CLEAR a real dollars-vs-fils mismatch. The direction is the whole difference. Here a
    concept can only ADD a refusal, never remove one — a wrong concept costs a false refusal that a
    human corrects by fixing the concept, where a wrong concept there would cost a silent wrong
    number. Tightening on an AI proposal is safe; clearing on one is not.

    WHY IT IS NOT ``visible_requires``. That column answers "who may see this", which is exactly
    the question the finding says was mistaken for this one. Reading it here would re-fuse the two
    axes the gate exists to separate — and would make the refusal depend on the CALLER's roles,
    so the same feature would be safe for one reviewer and unsafe for another.

    ORDER. Structurally-unsuitable classes first, then the ones a policy could license, then
    operands in the order the model proposed them. Deterministic, and it means a candidate that is
    both PII and a protected characteristic reports the refusal no policy can ever lift.

    ``aggregation`` is read by exactly ONE class (currency): whether the operation combines or
    returns the amounts decides whether a missing denomination can produce a wrong number at all.

    THE ONE CLASS THAT CAN NOW BE CLEARED (D14). ``PERSONAL_DATA_POLICY_REQUIRED`` was the only
    refusal here that named an artifact which did not exist. It exists now — a governed
    ``pii_use_policy`` revision per CONCEPT — and this gate is its only operational reader. The
    clearing rule is deliberately ALL-OR-NOTHING: every pii-classed concept the candidate uses must
    have an ACTIVE policy, because a feature that mixes one licensed and one unlicensed personal-data
    operand is an unlicensed feature. Protected characteristics are untouched by any of it — class 2
    runs FIRST and is `structurally_unsuitable`, so no policy can ever reach them.
    """
    if not feature_use_gate_enabled():
        return UseGateOutcome()

    # ── class 2 — a protected characteristic as an operand or a grouping key. structurally_
    #    unsuitable: ECOA/fair-lending and GDPR Article 9 have no "allow" switch, so there is no
    #    setup step to name and the wording must not imply one.
    #
    #    WHAT THIS CLASS CAN SEE (the full note lives on `concepts.is_protected_characteristic`):
    #    the registry holds THREE concepts in these sensitivity classes — the umbrellas
    #    `protected_attribute` / `special_category` and `vulnerability_flag` — and no per-attribute
    #    concept, so this fires only when ENRICHMENT landed the column on one of those three. It is
    #    a floor over a vocabulary, not a detector for every protected characteristic in a catalog,
    #    and a `gender_cd` that enrichment left unclassified passes it. ──
    for _src, ref in pairs:
        concept_name = meta.get(ref, {}).get("concept")
        if is_protected_characteristic(concept_name):
            return UseGateOutcome(rejection=Rejection(
                RejectCode.PROTECTED_CHARACTERISTIC,
                f"{ref} is a protected characteristic ({concept_name}) and cannot be a model "
                f"input or a grouping key. No approval makes it one — use a legitimate business "
                f"driver instead, or correct the concept if this column is not one"))

    # ── class 4 — the LABEL THAT STANDS BESIDE A CODE, used as an operand. structurally_unsuitable:
    #    no setting makes a display name a value, and the code that IS the value is right there.
    #    The registry already said this in every such concept's description; `descriptive` is that
    #    sentence as a field, SELF-DECLARED PER CONCEPT — there is no group sweep, because a group
    #    is a taxonomy bucket and cannot make a claim about a specific column's semantics. Free
    #    prose (`payment_narrative`, `free_text`) is deliberately NOT here: it is computable text
    #    that carries personal data, so it lands in class 1, where a policy can license it.
    #
    #    THE MESSAGE DOES NOT CLAIM A JOIN RULE, because this gate does not enforce one. Join
    #    candidacy is excluded STRUCTURALLY and elsewhere: every label concept is `categorical` with
    #    no entity_link, and `derive_bridge_candidates` pairs only columns sharing an IDENTIFIER
    #    concept — so a label can never be proposed as a join key whether or not this gate ever
    #    runs, and the read-set pairs own the rest. Saying "can never be a join key" here would
    #    credit this code with a guarantee two other components actually provide, and would go on
    #    being printed if either of them regressed. ──
    for _src, ref in pairs:
        concept_name = meta.get(ref, {}).get("concept")
        if is_descriptive(concept_name):
            return UseGateOutcome(rejection=Rejection(
                RejectCode.DESCRIPTIVE_OPERAND,
                f"{ref} is a descriptive label ({concept_name}), not a computable value — it "
                f"displays and groups but can never be a measure. Use the CODE column beside it"))

    # ── class 1 — personal data as a model input. needs_setup, and the ONE class with an artifact
    #    that can now answer it: a governed `pii_use_policy` revision per CONCEPT (D14, migration
    #    1056). The rule is EVERY pii-classed concept this candidate uses must have an ACTIVE
    #    policy, and the reasoning is that a partially-licensed feature is an unlicensed feature —
    #    a purpose declared for `pep_flag` says nothing about `geolocation`, and clearing on the
    #    first covered operand would let one approval license every other personal-data concept
    #    that happened to ride along beside it.
    #
    #    ONE BULK READ PER CANDIDATE, whatever the operand count — not per validation PASS, which
    #    is what this note used to claim. `_validate_idea` runs on every candidate from every
    #    producer (the menu, the confirm-time MCV, the recipe options, the planner's cross-catalog
    #    proposals) and each pii-binding candidate asks once; the read is skipped entirely when the
    #    candidate binds no personal data at all, which is most of them. What that rules out is the
    #    per-OPERAND query storm, and that is the whole claim. See the TODO seam on
    #    `active_pii_use_policies` for the cross-candidate batching a 157-recipe grounding pass
    #    would still benefit from — deliberately not plumbed, because the caching lifetime of a
    #    licence is the entire design question and a stale one licenses a revoked concept.
    #
    #    ABSENCE, REVOCATION AND CORRUPTION ALL REFUSE. `active_pii_use_policies` returns only
    #    concepts whose CURRENT revision is `active` and content-verifies each one, so a concept
    #    nobody declared, a concept whose policy was revoked, and a policy row somebody edited are
    #    indistinguishable here — none of them clears. That is the property the whole surface rests
    #    on, and the mutation harness has an entry that kills the bar if revocation stops mattering.
    #
    #    THE REFUSAL NAMES THE UNCOVERED CONCEPTS, not the covered ones and not the column: the
    #    reviewer's next action is approving a concept in Governance, so the message has to say
    #    WHICH. A partially-covered candidate is the case that makes this load-bearing. ──
    # The `isinstance` narrowing is not decoration: `meta` is an untyped catalog projection, so
    # every downstream use (the store call, the sort, the join, the dict index) was typed
    # `Any | None`. `is_personal_data(None)` was already False, so the behaviour is identical —
    # what changes is that the four values the refusal message and the licence lookup are built
    # from are now KNOWN to be concept names.
    personal_data: list[tuple[str, str]] = []
    for _src, ref in pairs:
        candidate_concept = meta.get(ref, {}).get("concept")
        if isinstance(candidate_concept, str) and is_personal_data(candidate_concept):
            personal_data.append((ref, candidate_concept))
    covering: tuple[str, ...] = ()
    if personal_data:
        needed = {concept_name for _ref, concept_name in personal_data}
        # ONE read per candidate; absence is a refusal. A `PolicyStoreConflict` from here is NOT
        # caught: it propagates and 500s the whole recommendation pass, BY DESIGN — a store whose
        # policy rows fail content or approver verification cannot be reasoned about candidate by
        # candidate, and degrading to "no licence for this one" would turn tamper into a quiet
        # refusal nobody investigates. Noted, not changed.
        licensed = active_pii_use_policies(conn, needed)
        uncovered = sorted(needed - set(licensed))
        if uncovered:
            ref, concept_name = next((r, c) for r, c in personal_data if c in set(uncovered))
            missing = ", ".join(uncovered)
            return UseGateOutcome(rejection=Rejection(
                RejectCode.PERSONAL_DATA_POLICY_REQUIRED,
                f"{ref} is personal data ({concept_name}) and no active personal-data use policy "
                f"covers {missing}, so nothing authorizes it as a model input. A governance owner "
                f"must declare one (purpose) under Governance -> Data-use policies before this "
                f"feature can be built"))
        covering = tuple(sorted(licensed[concept_name] for concept_name in needed))

    # ── class 3 — a currency-carrying amount whose denomination is neither declared on the column
    #    nor bound as an operand, while the currency column sits on the SAME table, AND the
    #    aggregation actually combines or returns the amounts. needs_setup: the fix is a decision
    #    (bind the dimension, or declare a conversion policy), and the refusal names the exact
    #    column that supplies it.
    #
    #    THE COUNTING EXEMPTION. `count` / `count_distinct` over an amount are currency-agnostic —
    #    how many is the same number in dollars and in fils — so "the result would silently mix
    #    currencies" is simply FALSE about them, and the reviewer is handed a setup task that could
    #    not change their answer. `latest` / `max` / `min` stay gated: they return a denominated
    #    value, and so does the problem. See :func:`_mixes_currency_values`, which fails closed on
    #    an aggregation it does not recognise. ──
    bound = {ref for _src, ref in pairs}
    amounts: list[tuple[str, str]] = []
    if _mixes_currency_values(aggregation):
        amounts = [(src, ref) for src, ref in pairs
                   if carries_currency(meta.get(ref, {}).get("concept"))
                   and not meta.get(ref, {}).get("currency")]
    if amounts:
        siblings = _denomination_siblings(
            conn, {(src, meta[ref]["table_name"]) for src, ref in amounts
                   if meta.get(ref, {}).get("table_name")})
        for src, ref in amounts:
            dimension = siblings.get((src, meta[ref]["table_name"]))
            if dimension is None or dimension in bound:
                # Either the platform cannot see a currency dimension at all (the existing
                # MIXED_CURRENCY / CURRENCY_CONSISTENT machinery owns that case and nothing here
                # can name a fix), or the feature already binds it — which is the safe shape.
                continue
            return UseGateOutcome(rejection=Rejection(
                RejectCode.CURRENCY_POLICY_REQUIRED,
                f"{ref} carries no declared currency and the feature does not bind {dimension}, "
                f"the currency dimension on its own table — the result would silently mix "
                f"currencies. Bind that column, declare the column's currency, or record a "
                f"conversion policy"))
    return UseGateOutcome(personal_data_policy_revision_ids=covering)


def _validate_idea(conn, raw: dict, known: set[str], src_of: dict[str, set[str]],
                   target_ref: str | None, now: datetime | None, fresh_within: timedelta,
                   *, roles: Iterable[str] = (),
                   operand_roles: tuple[tuple[str, str], ...] = (),
                   candidate_key: str | None = None, template_id: str | None = None):
    """The deterministic TRI-STATE gauntlet (spec §2). Returns (FeatureIdea, None) for DESIGN_CHECKED
    or NEEDS_EXTERNAL_VALIDATION — the returned idea carries validation_status + typed requirements +
    resolved operands — or (None, Rejection) for REJECTED (deterministically invalid / unauthorized).
    `roles` gates cross-table join authority (a read-scope-DENIED hop rejects). `src_of` maps
    object_ref -> the candidate catalog source(s), used to resolve each derive's catalog (B3).

    `operand_roles` is the `(object_ref, role)` map the TEMPLATE declared for this candidate's bound
    operands (empty for an LLM-proposed candidate, which declares none). It narrows ONE thing — which
    operands the unit/currency needs-check may ask about — and an EMPTY map means "nothing declared",
    never "no measures": the E4a structural rule then runs exactly as before.

    TASK 2A — THE DECISION TRACE (freeze 0F-7 P1). `candidate_key` / `template_id` let a caller
    thread this candidate's identity in; when it does, the gauntlet records a
    `GroundingDependencyPinV1` AT each read it already performs and returns the assembled
    `GroundingDecisionTraceV1` on the object it was already returning — `FeatureIdea.grounding_trace`
    for a survivor, `Rejection.trace` for a refusal. THE RETURN ARITY IS UNCHANGED, so not one of the
    six production unpack sites moves. Threading nothing (the LLM / planner / confirm-time paths)
    disables the recorder entirely: no pins, no hashing, no trace, byte-identical behaviour.

    Nothing here reads the trace, and nothing here decides differently because of it: every pin is
    written from a value the check had already read, and no pin adds a query."""
    # ── the USE gate (Bar 4). Sensitivity decided who may SEE these operands; this decides whether
    #    the feature may be BUILT from them. Placed with the other hard rejects, AFTER leakage and
    #    freshness — a leaky or stale candidate is refused for the reason it has always been
    #    refused, so no existing rejection changes code — and BEFORE any requirement is minted,
    #    because a refused feature must never reach the tri-state at all. ──
    use = _use_gate(conn, pairs, meta, raw.get("aggregation"))
    if use.rejection is not None:
        return None, use.rejection

    # C2-C3: every requirement below is minted through the SANCTIONED, registry-validated factory
    # (validation_requirements.build_requirement) — the deterministic code picks code + typed params
    # from server-known refs; a bad code/param is a PROGRAMMER error (raises), never swallowed. Imported
    # here (function-local) because validation_requirements imports REQUIREMENT_CODES/Requirement from
    # this module — a module-top import would be a circular import at load time.
    from featuregen.overlay.upload.validation_requirements import (
        build_requirement,
        evaluated_rule_content_hashes,
    )

    trace = GroundingTraceRecorder(
        candidate_key=candidate_key, template_id=template_id,
        read_scope_rule_content_hashes=(read_scope_rule_content_hash(),) if candidate_key else ())

    def _reject(code: str, message: str):
        """A refusal carrying the trace of what had been read WHEN it refused — the pins collected
        so far, the rules evaluated so far, no requirements (a rejection mints none)."""
        return None, Rejection(code, message, trace=trace.build(
            validation_status="REJECTED", requirements=(),
            validation_rule_content_hashes=evaluated_rule_content_hashes(
                trace.evaluated_rule_codes)))

    # The VISIBILITY dependency: which classes this run could see. It gates the candidate universe
    # (`known` / `src_of`, both read-scoped by the caller) and every join hop below, so it is pinned
    # once, up front, for survivors and refusals alike.
    trace.pin(SuggestionDependencyClass.HARD_AVAILABILITY, _gt.READ_SCOPE, "read-scope",
              {"allowed_classes": allowed_sensitivities(roles)})

    derives = _ground_refs(raw.get("derives_from", []), known)
    trace.pin(SuggestionDependencyClass.HARD_AVAILABILITY, _gt.GROUNDING_CANDIDATE_SET,
              "derives_from", {"resolved_object_refs": list(derives)})
    if not derives:
        return _reject(RejectCode.UNGROUNDED, "ungrounded")
    pairs: list[tuple[str, str]] = []
    for d in derives:
        srcs = src_of.get(d, set())
        if len(srcs) != 1:
            return _reject(RejectCode.AMBIGUOUS_CATALOG, f"ambiguous catalog for {d}")
        pairs.append((next(iter(srcs)), d))
    # The TEMPLATE's own declaration of what each operand IS — a template-authored input to the
    # unit/currency narrowing below, pinned where that narrowing will read it.
    trace.pin(SuggestionDependencyClass.VALIDATION, _gt.TEMPLATE_OPERAND_ROLES, template_id or "",
              {"operand_roles": [[ref, role] for ref, role in operand_roles]})
    declared_role_of: dict[str, str] = {}
    for ref, role in operand_roles:
        declared_role_of.setdefault(ref, role)
    # ORDERED by the gauntlet's own operand order, which is the template's binding order (`derives`
    # preserves it). An operand the template declared nothing for carries an EMPTY role: an absent
    # declaration is never a licence to guess one.
    trace.record_operand_roles(
        tuple((src, d, declared_role_of.get(d, "")) for src, d in pairs))

    meta = _column_meta(conn, pairs)
    for src, d in pairs:
        if d not in meta or meta[d]["catalog_source"] != src:
            trace.pin(SuggestionDependencyClass.HARD_AVAILABILITY, _gt.COLUMN_EXISTENCE,
                      _gt.column_dependency_key(src, d),
                      {"catalog_source": src, "object_ref": d, "exists": False})
            return _reject(RejectCode.UNKNOWN_COLUMN, f"unknown column {d} in catalog {src}")
        # ONE read (`_column_meta`) feeding three checks: the M4 existence check here, and the
        # unit / currency hints the hard rejects and the needs-checks both consume below. Pinned
        # where the read happened, not where each consumer sits.
        trace.pin(SuggestionDependencyClass.HARD_AVAILABILITY, _gt.COLUMN_EXISTENCE,
                  _gt.column_dependency_key(src, d),
                  {"catalog_source": src, "object_ref": d, "exists": True})
        trace.pin(SuggestionDependencyClass.VALIDATION, _gt.COLUMN_UNIT_HINT,
                  _gt.column_dependency_key(src, d), {"unit": meta[d]["unit"]})
        trace.pin(SuggestionDependencyClass.VALIDATION, _gt.COLUMN_CURRENCY_HINT,
                  _gt.column_dependency_key(src, d), {"currency": meta[d]["currency"]})
    if target_ref and target_ref in derives:
        return _reject(RejectCode.LEAKAGE, "leaks target")
    if now is not None:
        for src in {p[0] for p in pairs}:
            wm = drift_watermark(conn, src)
            if wm is None or wm < now - fresh_within:
                return _reject(RejectCode.STALE, f"stale source: {src}")

    aggregation = raw.get("aggregation")
    operation = _norm_agg(aggregation)   # the normalized operation string (server-known, not the LLM's)
    grain_table = raw.get("grain_table")
    catalogs = {p[0] for p in pairs}
    requirements: list[Requirement] = []
    grain_operand: tuple[str, str] | None = None
    time_operand: tuple[str, str] | None = None

    # ── disposition: numeric type (a numeric op's measure must be numeric; declared_type is a HINT
    #    that may only reject/needs-check, never clear). Read the operational type through C1
    #    (read_operational_value) so its tamper gate protects the clear: C1 fails CLOSED with
    #    value=None exactly on a DRIFTED / ambiguous head (GATE 2 hash_mismatch — the graph type
    #    drifted from its approved decision — or GATE 1 fork), so such a value no longer clears. A
    #    genuinely governed (resolved, hash-verified) type clears; an UNGOVERNED type is a numeric
    #    HINT that clears exactly as before (logical_representation is often ungoverned on the upload
    #    path — consistent-state behavior is preserved; only the drifted case is newly fail-closed).
    #    projection_unavailable ABORTS (never serve a stale type). ──
    if _needs_numeric(aggregation):
        trace.record_rule("TYPE_IS_NUMERIC")
        for src, d in pairs:
            lref = logical_ref_of(conn, src, d)
            ov = _governed_read(conn, lref, "logical_representation")
            _pin_governed(trace, src, d, "logical_representation", _gt.GOVERNED_LOGICAL_REPRESENTATION, ov)
            if _is_numeric(ov.value):   # value is None on the C1 drift/fork fail-closed → won't clear
                continue
            facts = read_column_facts(conn, lref, "declared_type")
            declared = facts.value
            trace.pin(SuggestionDependencyClass.VALIDATION, _gt.DECLARED_TYPE_HINT,
                      _gt.column_dependency_key(src, d),
                      {"declared_type": declared, "authority": facts.authority},
                      current_revision_id=facts.provenance)
            if declared and not _is_numeric(declared):
                return _reject(RejectCode.NON_NUMERIC,
                               f"declared type {declared!r} of {d} is not numeric")
            requirements.append(build_requirement(
                code="TYPE_IS_NUMERIC", operand=(src, d),
                detail="operational type unknown; numeric declared hint", params=None))

    # ── disposition: additivity — only a GOVERNED (status=="resolved", hash-verified) semi/non-
    #    additive rejects; ANY other C1 status (no_decision/no_value/not_operational/conflict/fork/
    #    hash_mismatch/retired) is an honest needs-check (spec [F6]). THE FIX: a graph value that
    #    DRIFTED from its approved decision (e.g. mutated to "additive") now hash-mismatches → status
    #    != "resolved" → does NOT clear (emits ADDITIVITY_SUPPORTS_OPERATION), where the old permissive
    #    read_column_facts served it as governed-additive and wrongly cleared. ──
    if _is_additive_unsafe(aggregation):
        trace.record_rule("ADDITIVITY_SUPPORTS_OPERATION")
        for src, d in pairs:
            ov = _governed_read(conn, logical_ref_of(conn, src, d), "additivity")
            _pin_governed(trace, src, d, "additivity", _gt.GOVERNED_ADDITIVITY, ov)
            if ov.status == "resolved":
                if ov.value in ("semi_additive", "non_additive"):
                    return _reject(RejectCode.ADDITIVITY, f"unsafe additive aggregation of {d}")
            else:
                requirements.append(build_requirement(
                    code="ADDITIVITY_SUPPORTS_OPERATION", operand=(src, d),
                    detail="additivity not governed-confirmed", params={"operation": operation}))

    # ── disposition: unit / currency — DISTINCT hint fields (never folded): a hint may TIGHTEN
    #    (a positive contradiction rejects; absence needs-checks) but never CLEAR — matching
    #    non-empty hints add no requirement and promote nothing ──
    units = {meta[d]["unit"] for d in derives if meta.get(d, {}).get("unit")}
    currencies = {meta[d]["currency"] for d in derives if meta.get(d, {}).get("currency")}
    if len(units) > 1:
        return _reject(RejectCode.MIXED_UNITS,
                       f"mixed units {sorted(units)}; aggregation would be silently wrong")
    if len(currencies) > 1:
        return _reject(RejectCode.MIXED_CURRENCY, f"mixed currencies {sorted(currencies)}")
    # (the "unit unknown" NEEDS-CHECK is minted further down, after the temporal + grain
    #  dispositions resolve which operands are the GROUP BY key and the window boundary)

    # ── disposition: temporal — a windowed feature needs a governed-VERIFIED as-of column; a table
    #    with NO as-of column at all is still a hard reject (future-leakage risk) ──
    if _is_windowed(aggregation):
        trace.record_rule("TEMPORAL_IS_POPULATED")
        checked_tables: set[tuple[str, str]] = set()
        for src, d in pairs:
            if d.count(".") < 2 or (src, d.split(".")[-2]) in checked_tables:
                continue
            checked_tables.add((src, d.split(".")[-2]))
            table = d.split(".")[-2]
            aref = _as_of_column_ref(conn, src, table)
            # The STRUCTURAL question — does this table have a point-in-time basis at all — pinned
            # with the answer it got, including the honest "there is none" that rejects below.
            trace.pin(SuggestionDependencyClass.HARD_AVAILABILITY, _gt.AS_OF_COLUMN_LOOKUP,
                      _gt.table_dependency_key(src, table),
                      {"table": table, "as_of_object_ref": aref})
            if aref is None:
                return _reject(RejectCode.NO_POINT_IN_TIME,
                               f"no point-in-time basis for {d} (future-leakage risk)")
            time_operand = (src, aref)
            ov = _governed_read(conn, logical_ref_of(conn, src, aref), "is_as_of")
            _pin_governed(trace, src, aref, "is_as_of", _gt.GOVERNED_IS_AS_OF, ov)
            if ov.status != "resolved":
                requirements.append(build_requirement(
                    code="TEMPORAL_IS_POPULATED", operand=(src, aref),
                    detail="as-of column declared, not governed-verified", params=None))

    # ── disposition: grain — a grain feature needs a governed-VERIFIED grain column ──
    if grain_table and len(catalogs) == 1:
        trace.record_rule("GRAIN_IS_UNIQUE")
        gcat = next(iter(catalogs))
        gref = _grain_column_ref(conn, gcat, grain_table)
        trace.pin(SuggestionDependencyClass.HARD_AVAILABILITY, _gt.GRAIN_COLUMN_LOOKUP,
                  _gt.table_dependency_key(gcat, grain_table),
                  {"table": grain_table, "grain_object_ref": gref})
        if gref is not None:
            grain_operand = (gcat, gref)
            ov = _governed_read(conn, logical_ref_of(conn, gcat, gref), "is_grain")
            _pin_governed(trace, gcat, gref, "is_grain", _gt.GOVERNED_IS_GRAIN, ov)
            if ov.status != "resolved":
                requirements.append(build_requirement(
                    code="GRAIN_IS_UNIQUE", operand=(gcat, gref),
                    detail="grain declared, not governed-verified", params=None))

    # ── disposition: unit / currency NEEDS-CHECK — asked only where units can actually MIX.
    #    Placed HERE (not beside the hard rejects above) because it is defined in terms of the
    #    operands the two dispositions above just resolved.
    #
    #    `pairs` is every BOUND operand, the GROUP BY key and the window boundary included, so the
    #    old `len(pairs) >= 2` gate read `AVG(txn_amt) BY cif_id OVER 30d [as_of_dt]` — three pairs,
    #    ONE measure — as a "combining op" and asked for the unit of a customer id and a date.
    #    Measured on the real FTR sample: 21 of 28 UNIT_CONSISTENT named cif_id / as_of_dt / txn_ts /
    #    setl_stat, questions no one can ever answer, and nothing could reach DESIGN_CHECKED.
    #
    #    TWO STRUCTURAL corrections, never a concept- or role-based one (that would rest a safety
    #    gate on an AI-PROPOSED concept, and one wrong concept would wave a real dollars-vs-fils
    #    mismatch through):
    #      1. `grain_operand` / `time_operand` are excluded. They are the feature's own key and its
    #         point-in-time anchor — the very pair `_recipe_parts` already subtracts to render the
    #         measures — never summed, averaged or divided into the result.
    #      2. The gate counts MEASURES. With a single measure there is nothing to mix: the result
    #         INHERITS that column's unit and cannot be corrupted, which is the only harm this check
    #         prevents (its name is *consistency* — that needs two things).
    #    The MIXED_UNITS / MIXED_CURRENCY hard rejects above are untouched and still read EVERY
    #    derive, grain and as-of included: a positive contradiction still rejects outright. Only the
    #    unanswerable "unit unknown" question is narrowed. ──
    #
    #    E4a T3 — SURFACING THE AI's ANSWER. A question no reviewer can answer is as useless as an
    #    unanswerable one, so each requirement CARRIES the `llm/proposed` unit/currency the drafter
    #    wrote (T2) as a registry-typed, OPTIONAL `suggested_unit` / `suggested_currency` param: the
    #    card reads "unit not confirmed — AI suggests AED" and the confirm is one action away. This
    #    is SURFACING ONLY and changes NO disposition: the mint CONDITION is still an empty
    #    `graph_node.unit` (read above via `meta`), the requirement still FIRES, and the suggestion
    #    is read from `field_evidence` — a store the gauntlet's clear path never consults. ──
    from featuregen.overlay.upload.validation_requirements import (
        MEASURE_SUGGESTION_SCHEMA_VERSION,
    )

    structural = {op for op in (grain_operand, time_operand) if op is not None}
    # E4b — the THIRD narrowing, and the only one that is not structural: the role the TEMPLATE AUTHOR
    # DECLARED for each bound operand. `setl_stat` / `acct_id` / `txn_ts` ride along as SECOND operands,
    # so the measure count above still trips and the check fires on a column that can never carry a
    # unit. `Need.role` is hand-written in the recipe library and versioned in this repo, and
    # `need_metadata` already resolves it to a typed `JoinRole` — so `is_measure_need_role` asks the
    # governed machinery, not a duplicated string list. This is emphatically NOT a concept-based rule:
    # concepts are AI-PROPOSED, and one wrong concept would wave a real dollars-vs-fils mismatch
    # through. FAIL TOWARD ASKING at every step — an operand is excluded ONLY when the template
    # declared a role for it AND every role it declared resolves to a non-measure. An operand with no
    # declaration (and therefore an idea with NO declarations at all — every LLM candidate) is a
    # measure, so the E4a rule below is reached unchanged.
    declared_measures = {ref for ref, role in operand_roles if is_measure_need_role(role)}
    declared_non_measures = {ref for ref, _role in operand_roles} - declared_measures
    measures = [p for p in pairs
                if p not in structural and p[1] not in declared_non_measures]
    if len(measures) >= 2:   # a COMBINING op: an operand's unknown scale/currency is a fact to verify
        trace.record_rule("UNIT_CONSISTENT")
        trace.record_rule("CURRENCY_CONSISTENT")
        for src, d in measures:
            if not meta[d]["unit"]:
                suggested, suggestion_evidence = _ai_suggestion_with_evidence(
                    conn, logical_ref_of(conn, src, d), "unit")
                # SEMANTIC, not VALIDATION: this decorates the question, it never answers it. A
                # later reader must not suppress a readiness claim because an advisory hint moved.
                trace.pin(SuggestionDependencyClass.SEMANTIC, _gt.AI_UNIT_SUGGESTION,
                          _gt.column_dependency_key(src, d),
                          {"field_name": "unit", "suggested": suggested},
                          evidence=suggestion_evidence)
                requirements.append(build_requirement(
                    code="UNIT_CONSISTENT", operand=(src, d),
                    detail=("unit unknown across a combining op"
                            + (f"; AI suggests {suggested}" if suggested else "")),
                    params={"suggested_unit": suggested} if suggested else None,
                    schema_version=MEASURE_SUGGESTION_SCHEMA_VERSION))
            if not meta[d]["currency"]:
                # currency is UNKNOWN here (that is the mint condition), so no bound currency_ref is
                # available — pass none; currency_ref is OPTIONAL in the registry (C2C3-T1 tweak).
                suggested, suggestion_evidence = _ai_suggestion_with_evidence(
                    conn, logical_ref_of(conn, src, d), "currency")
                trace.pin(SuggestionDependencyClass.SEMANTIC, _gt.AI_CURRENCY_SUGGESTION,
                          _gt.column_dependency_key(src, d),
                          {"field_name": "currency", "suggested": suggested},
                          evidence=suggestion_evidence)
                requirements.append(build_requirement(
                    code="CURRENCY_CONSISTENT", operand=(src, d),
                    detail=("currency unknown across a combining op"
                            + (f"; AI suggests {suggested}" if suggested else "")),
                    params={"suggested_currency": suggested} if suggested else {},
                    schema_version=MEASURE_SUGGESTION_SCHEMA_VERSION))

    # ── disposition: cross-table join authority (spec §7). A measure in a different table than the
    #    grain needs a real path; UNVERIFIED -> JOIN_CONNECTIVITY, no-path / read-scope-denied -> reject ──
    if grain_table and len(catalogs) == 1:
        jcat = next(iter(catalogs))
        for src, d in pairs:
            if d.count(".") >= 2 and d.split(".")[-2] != grain_table:
                trace.record_rule("JOIN_CONNECTIVITY")
                to_table = d.split(".")[-2]
                outcome = classify_join_path(conn, jcat, grain_table, to_table, roles=roles)
                # The path the planner SELECTED, converted at its own seam and RETAINED here — the
                # legs, in order, in the direction travelled. Nothing downstream may search again.
                # This loop runs PER cross-table operand, so `record_path` accumulates a UNION of
                # chains (see its docstring); THIS operand's own chain is pinned below, which is
                # what keeps the operand→path assignment identity-bearing and verifiable.
                legs = join_outcome_relationship_path(outcome, catalog_source=jcat)
                trace.record_path(legs)
                trace.pin(SuggestionDependencyClass.VALIDATION, _gt.JOIN_PATH,
                          _gt.column_dependency_key(src, d),
                          _gt.join_path_pin_content(from_table=grain_table, to_table=to_table,
                                                    outcome_kind=outcome.kind, legs=legs),
                          # THIS operand's own ordered legs, readable — the same list the content
                          # above hashes. An identity builder needs the chain's LOGICAL shape, and
                          # a hash cannot be projected onto one.
                          path_realization_hashes=[leg.realization_content_hash for leg in legs])
                if outcome.kind == JoinOutcome.NO_PATH:
                    return _reject(RejectCode.NO_JOIN_PATH, f"no join path {grain_table} -> {d}")
                if outcome.kind == JoinOutcome.DENIED:
                    return _reject(RejectCode.JOIN_DENIED,
                                   f"join {grain_table} -> {d} crosses a read-scope-denied hop")
                if outcome.kind == JoinOutcome.UNVERIFIED:
                    requirements.append(build_requirement(
                        code="JOIN_CONNECTIVITY", operand=(src, d),
                        detail="join authorized but not verified", params=None))

    # ── finalize (tri-state) ──
    status = "NEEDS_EXTERNAL_VALIDATION" if requirements else "DESIGN_CHECKED"
    return FeatureIdea(
        name=str(raw.get("name", "")), description=str(raw.get("description", "")),
        derives_from=derives, aggregation=aggregation, grain_table=grain_table,
        derives_pairs=tuple(pairs), rationale=str(raw.get("rationale", "")),
        operation_kind=_norm_agg(aggregation), measure_refs=tuple(pairs),
        grain_ref=grain_operand, time_ref=time_operand, window=_window_of(aggregation),
        grouping_refs=(), validation_status=status, requirements=tuple(requirements),
        # PROVENANCE, from the gate that already ran above: the policy revisions that licensed this
        # candidate's personal-data operands, or () when it binds none.
        personal_data_policy_revision_ids=use.personal_data_policy_revision_ids,
        grounding_trace=trace.build(
            validation_status=status, requirements=tuple(requirements),
            validation_rule_content_hashes=evaluated_rule_content_hashes(
                trace.evaluated_rule_codes))), None


def _governed_read(conn, logical_ref: str, field_name: str):
    """The C1 authority read for a GOVERNED-clearing check on the customer feature path.

    Delegates to :func:`read_operational_value` — the tamper-gated read (fork / hash-verify vs the
    approved decision's ``load_bearing_value_hash`` / projection-health). ONLY ``status=="resolved"``
    is a governed, hash-verified value that may CLEAR a design check; every other status is a
    non-authoritative hint that can only tighten (needs-check) — so a graph value that DRIFTED from
    its approved decision (``hash_mismatch``), a forked head (``fork``), or a retired decision
    (``retired``) can no longer masquerade as governed and wrongly clear.

    ``projection_unavailable`` ABORTS generation: re-raise :class:`CatalogProjectionUnavailable`
    (which the feature-gen route maps to a retryable 503) so we NEVER serve a stale projected value."""
    ov = read_operational_value(conn, logical_ref, field_name)
    if ov.status == "projection_unavailable":
        raise CatalogProjectionUnavailable(
            CATALOG_PROJECTION_UNAVAILABLE,
            ov.conflict_status or "load-bearing catalog projection unavailable")
    return ov


def _norm_agg(aggregation: str | None) -> str:
    """Normalize an aggregation for dedup so 'SUM' / 'sum' / None / '' don't read as distinct."""
    return (aggregation or "").strip().lower()


def _sig(idea: FeatureIdea) -> tuple[frozenset, str]:
    return (frozenset(idea.derives_pairs), _norm_agg(idea.aggregation))


def _redundant_of(idea: FeatureIdea, accepted: list[FeatureIdea]) -> bool:
    """A candidate is redundant if an already-accepted feature derives from the SAME columns with the
    same aggregation — a re-proposal under a new name (`seen` only catches identical names). (item 1a)"""
    sig = _sig(idea)
    return any(_sig(a) == sig for a in accepted)


def _registered_signatures(conn) -> set[tuple[frozenset, str]]:
    """(frozenset of (catalog_source, object_ref), normalized aggregation) for every REGISTERED feature
    — so the loop skips a candidate that duplicates an already-confirmed feature (§7.5 dedup, item 2)."""
    rows = conn.execute(
        "SELECT f.feature_id, f.aggregation, d.catalog_source, d.object_ref FROM feature f "
        "LEFT JOIN feature_derives_from d ON d.feature_id = f.feature_id").fetchall()
    by_feat: dict[tuple[str, str | None], set] = {}
    for fid, agg, cs, ref in rows:
        entry = by_feat.setdefault((fid, agg), set())
        if cs and ref:
            entry.add((cs, ref))
    return {(frozenset(pairs), _norm_agg(agg)) for (fid, agg), pairs in by_feat.items()}


def _critique_candidates(conn, client: LLMClient, objective: str,
                         candidates: list[FeatureIdea], *,
                         actor: IdentityEnvelope | None = None) -> dict[str, str]:
    """LLM-2 critic (item 5): reviews the generator's gauntlet-passed candidates against the hypothesis
    and returns {feature_name: issue} for any with a QUALITY/FIT problem the deterministic gauntlet
    cannot express (weak hypothesis fit, semantic/proxy leakage, redundancy, vague grounding, wrong
    grain). Its findings are fed back to the GENERATOR to fix. ADVISORY: fails OPEN — if the critic
    provider errors or is absent, generation proceeds without it (never breaks the loop, like ingest
    enrichment)."""
    if not candidates:
        return {}
    summary = [{"name": f.name, "derives_from": f.derives_from, "aggregation": f.aggregation,
                "grain_table": f.grain_table} for f in candidates]
    try:
        # critique stays v1 (no feature_candidate_critique v2 registered — spec §8)
        out = _call_raw(
            conn, client, "overlay.feature.critique_candidates", "feature_candidate_critique_v1",
            "feature_candidate_critique", objective, {"candidates": summary}, actor=actor)
    except psycopg.Error:
        raise   # a DB error aborts the request tx — NEVER swallow it (would silently roll back writes)
    except Exception:  # noqa: BLE001 — advisory; a provider/dispatch failure must not break generation
        logger.warning("candidate critic unavailable; proceeding without it", exc_info=True)
        return {}
    return {str(i.get("name", "")): str(i.get("issue", ""))
            for i in out.get("issues", [])
            if isinstance(i, dict) and i.get("name") and i.get("issue")}


def _vet(conn, raw: dict, known: set[str], src_of: dict[str, set[str]], registered: set,
         accepted: list[FeatureIdea], seen: set[str], avoid: list[dict],
         target_ref, now, fresh_within, *, roles: Iterable[str] = ()) -> FeatureIdea | None:
    """Gauntlet + dedup for one raw candidate. Returns the FeatureIdea to accept, or None (recording a
    structured rejection in `avoid`). Shared by the generation loop and the single critic-fix pass."""
    if not isinstance(raw, dict):
        # The LLM occasionally returns a feature as a bare string instead of an object; treat it as a
        # structured rejection rather than letting `raw.get(...)` raise AttributeError and kill the run.
        logger.warning("feature idea was a %s, not an object: %r", type(raw).__name__, raw)
        avoid.append({"name": str(raw)[:80], "reason": "LLM returned a non-object feature item",
                      "code": RejectCode.MALFORMED_ITEM})
        return None
    idea, rej = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within, roles=roles)
    if rej is not None:
        avoid.append({"name": raw.get("name", ""), "reason": rej.message, "code": rej.code})
        return None
    if idea.name in seen:
        return None
    if _redundant_of(idea, accepted):
        avoid.append({"name": idea.name, "reason": "duplicates an accepted feature",
                      "code": RejectCode.REDUNDANT})
        return None
    if _sig(idea) in registered:
        avoid.append({"name": idea.name, "reason": "already a registered feature",
                      "code": RejectCode.ALREADY_REGISTERED})
        return None
    return idea


def _fix_pass(conn, client: LLMClient, objective: str, accepted: list[FeatureIdea],
              issues: dict[str, str], menu: list[dict], known: set[str],
              src_of: dict[str, set[str]], registered: set,
              target_ref, now, fresh_within, feedback: str | None = None, *,
              table_context: list[dict] | None = None,
              roles: Iterable[str] = (),
              actor: IdentityEnvelope | None = None) -> list[FeatureIdea]:
    """One LLM-1 revision pass: keep the critic-clean features; ask LLM-1 to revise the flagged ones
    given the critic's notes; gauntlet-validate the revisions. Returns the merged list. `feedback`
    is the HUMAN's round guidance (see recommend_features) and rides along here too, so a fix pass
    revises under the same instruction as the rounds it repairs."""
    keep = [f for f in accepted if f.name not in issues]
    seen = {f.name for f in keep}
    fix_hints = [{"name": f.name, "derives_from": f.derives_from, "aggregation": f.aggregation,
                  "issue": issues[f.name]} for f in accepted if f.name in issues]
    inputs: dict = {"columns": menu, "fix": fix_hints}
    if table_context:
        inputs["table_context"] = table_context
    if feedback:
        inputs["feedback"] = feedback
    out = _call_raw(conn, client, "overlay.feature.recommend", "feature_recommend_v1",
                    "feature_ideas", objective + _DERIVES_FROM_DIRECTIVE, inputs, actor=actor,
                    prompt_version=_feature_schema_version(),
                    schema_version=_feature_schema_version())
    for raw in out.get("features", []):
        idea = _vet(conn, raw, known, src_of, registered, keep, seen, [], target_ref, now,
                    fresh_within, roles=roles)
        if idea is not None:
            keep.append(idea)
            seen.add(idea.name)
    return keep


# The schema alone does NOT force the model to cite its source columns: Anthropic's structured output
# does not hard-enforce `required` on nested array items, so Opus silently omits `derives_from` and
# EVERY idea is then rejected UNGROUNDED (measured: 0/18 populated on the bare instruction). An explicit
# mandatory directive appended to the generation instruction flips this decisively (measured: 21/22
# populated, with correctly-formatted object_refs). Kept as a fixed system directive (no PII) appended
# after the redacted objective, so the egress guard still scans it and the llm_call audit records it.
_DERIVES_FROM_DIRECTIVE = (
    "\n\nMANDATORY: for EVERY feature you propose, the `derives_from` field MUST list the exact "
    "`object_ref` string(s) — format public.<table>.<column> — of the source columns it is computed "
    "from, copied verbatim from the provided columns list. A feature whose `derives_from` is empty or "
    "omitted cannot be grounded and is discarded, so never leave it blank.")


def _generate(conn, objective: str, client: LLMClient, *,
              catalog_source: str | None = None, roles: Iterable[str] = (),
              entity: str | None = None,
              scope: ConfirmedScope | None = None,
              target_ref: str | None = None, now: datetime | None = None,
              fresh_within: timedelta = timedelta(hours=24),
              target: int = 5, budget: int = 3, critic: bool = True,
              critic_reviews: int = 3,
              feedback: str | None = None,
              actor: IdentityEnvelope | None = None) -> tuple[list[FeatureIdea], list[dict]]:
    """The generate→critic loop body (phase docs on recommend_features). Returns BOTH the accepted
    ideas AND the final `avoid` list — every structured rejection ({name, reason, code}) recorded
    across the generation rounds — so callers can show the human WHAT was rejected and why.
    recommend_features returns just the ideas; recommend_features_report exposes both."""
    cols = _candidate_columns(conn, catalog_source, roles, entity)
    known = {c["object_ref"] for c in cols}
    src_of: dict[str, set[str]] = {}          # object_ref -> catalog_source(s) in the candidate context
    for c in cols:
        src_of.setdefault(c["object_ref"], set()).add(c["catalog_source"])
    registered = _registered_signatures(conn)
    try:
        menu, table_context = _build_menu(
            conn, cols, objective=objective, entity=entity, scope=scope, roles=roles)
    except ContextTooLarge as exc:
        logger.warning("feature context too large for %r: %s", objective, exc)
        return [], [{"name": "", "reason": str(exc), "code": RejectCode.CONTEXT_TOO_LARGE}]

    # ---- Phase 1: generation (LLM-1 only; deterministic refinement, budget-bounded) ----
    accepted: list[FeatureIdea] = []
    seen: set[str] = set()
    avoid: list[dict] = []
    for _ in range(budget):
        if len(accepted) >= target:
            break
        # `avoid` is the loop's own machine feedback; `feedback` is HUMAN guidance for the whole
        # round (Gate #3 "regenerate with feedback"). Omitted when unset so the call is unchanged.
        inputs: dict = {"columns": menu, "avoid": avoid}
        if table_context:
            inputs["table_context"] = table_context
        if feedback:
            inputs["feedback"] = feedback
        out = _call_raw(conn, client, "overlay.feature.recommend", "feature_recommend_v1",
                        "feature_ideas", objective + _DERIVES_FROM_DIRECTIVE, inputs, actor=actor,
                        prompt_version=_feature_schema_version(),
                        schema_version=_feature_schema_version())
        proposed = out.get("features", [])
        if not proposed:                       # stalled generator -> stop
            break
        for raw in proposed:
            idea = _vet(conn, raw, known, src_of, registered, accepted, seen, avoid,
                        target_ref, now, fresh_within, roles=roles)
            if idea is not None:
                accepted.append(idea)
                seen.add(idea.name)

    # ---- Phase 2: bounded critic loop (AT MOST `critic_reviews` reviews) ----
    issues: dict[str, str] = {}
    if critic:
        for i in range(max(0, critic_reviews)):
            issues = _critique_candidates(conn, client, objective, accepted, actor=actor)
            if not issues:
                break                          # critic satisfied — nothing to fix
            if i < critic_reviews - 1:         # not the last allowed review -> let LLM-1 fix, re-review
                accepted = _fix_pass(conn, client, objective, accepted, issues, menu, known, src_of,
                                     registered, target_ref, now, fresh_within, feedback,
                                     table_context=table_context, roles=roles, actor=actor)

    # ---- Phase 3: forward to the human; residual critic notes ride along as ADVISORY ----
    if issues:
        accepted = [f if f.name not in issues else replace(f, critic_note=issues[f.name])
                    for f in accepted]
    kept = accepted[:target]
    if kept or avoid:
        from collections import Counter as _Counter
        by_code = _Counter(r.get("code") for r in avoid)
        logger.info(
            "feature-gen free-form [%s]: %d kept, %d rejected%s",
            objective if len(objective) <= 80 else objective[:79] + "…",
            len(kept), len(avoid),
            (" (" + ", ".join(f"{c}×{n}" for c, n in by_code.most_common()) + ")") if by_code else "")
    return kept, avoid


def recommend_features(conn, objective: str, client: LLMClient, *,
                       catalog_source: str | None = None, roles: Iterable[str] = (),
                       entity: str | None = None,
                       scope: ConfirmedScope | None = None,
                       target_ref: str | None = None, now: datetime | None = None,
                       fresh_within: timedelta = timedelta(hours=24),
                       target: int = 5, budget: int = 3, critic: bool = True,
                       critic_reviews: int = 3,
                       feedback: str | None = None,
                       actor: IdentityEnvelope | None = None) -> list[FeatureIdea]:
    """Generate (LLM-1) → a BOUNDED critic loop (LLM-2), then forward to the human.

      Phase 1 — GENERATION (LLM-1): a budget-bounded generate-validate loop. Each round LLM-1 proposes;
        every candidate clears the deterministic gauntlet (the hard safety floor); survivors are
        de-duplicated (vs this run — item 1a — and the registry — item 2). Stops at `target` or `budget`.
      Phase 2 — CRITIC LOOP (LLM-2), AT MOST `critic_reviews` (default 3) reviews: the critic reviews the
        candidates; if it flags any, LLM-1 revises them (one fix pass) and the critic reviews again —
        UP TO the cap. The loop exits early the moment the critic is clean.
      Phase 3 — FORWARD TO HUMAN: whatever LLM-1 produced after the review cap goes forward; a still-
        flagged feature carries the critic's residual note as ADVISORY, and the HUMAN decides fit at
        Gate #1. Nothing is dropped for a critic note alone — only the deterministic gauntlet can drop.

    TERMINATION: the critic runs at most `critic_reviews` times and LLM-1 fixes at most `critic_reviews-1`
    times — a hard cap, so there is never an unbounded LLM-1↔LLM-2 loop. `budget` bounds only Phase 1.

    Pass `entity` to gather candidates CROSS-DOMAIN; `critic=False` skips the critic loop.
    `feedback` is HUMAN guidance for the round (never a data value): it is threaded into EVERY
    generation round's inputs as "feedback" alongside the machine "avoid" list, and it only steers
    what the LLM proposes — the gauntlet still validates every candidate exactly as without it."""
    ideas, _ = _generate(
        conn, objective, client, catalog_source=catalog_source, roles=roles, entity=entity,
        scope=scope,
        target_ref=target_ref, now=now, fresh_within=fresh_within, target=target, budget=budget,
        critic=critic, critic_reviews=critic_reviews, feedback=feedback, actor=actor)
    return ideas


@dataclass(frozen=True, slots=True)
class RecommendReport:
    """recommend_features plus the gauntlet's structured rejections — so the human sees WHAT was
    rejected and why (rejection transparency at Gate #3), not just the survivors. Each rejection is
    {"name", "reason", "code"} (code from RejectCode). Identical repeats from retry rounds are
    collapsed: the same candidate rejected the same way across rounds appears once."""
    ideas: list[FeatureIdea]
    rejections: list[dict]


def _dedupe_rejections(rejections: list[dict]) -> list[dict]:
    seen: set[tuple] = set()
    out: list[dict] = []
    for r in rejections:
        key = (r.get("name"), r.get("reason"), r.get("code"))
        if key not in seen:
            seen.add(key)
            out.append(r)
    return out


def recommend_features_report(conn, objective: str, client: LLMClient, *,
                              catalog_source: str | None = None, roles: Iterable[str] = (),
                              entity: str | None = None,
                              scope: ConfirmedScope | None = None,
                              target_ref: str | None = None, now: datetime | None = None,
                              fresh_within: timedelta = timedelta(hours=24),
                              target: int = 5, budget: int = 3, critic: bool = True,
                              critic_reviews: int = 3,
                              feedback: str | None = None,
                              actor: IdentityEnvelope | None = None) -> RecommendReport:
    """recommend_features with the same kwargs and semantics, returning a RecommendReport that also
    carries the final avoid list as structured rejections. The API layer uses this so the UI can
    show the rejected candidates honestly instead of silently omitting them."""
    ideas, avoid = _generate(
        conn, objective, client, catalog_source=catalog_source, roles=roles, entity=entity,
        scope=scope,
        target_ref=target_ref, now=now, fresh_within=fresh_within, target=target, budget=budget,
        critic=critic, critic_reviews=critic_reviews, feedback=feedback, actor=actor)
    return RecommendReport(ideas=ideas, rejections=_dedupe_rejections(avoid))


def refine_idea(conn, idea: dict, instruction: str, client: LLMClient, *,
                catalog_source: str | None = None, roles: Iterable[str] = (),
                entity: str | None = None, target_ref: str | None = None,
                now: datetime | None = None,
                fresh_within: timedelta = timedelta(hours=24),
                objective: str | None = None,
                actor: IdentityEnvelope | None = None,
                ) -> tuple[FeatureIdea | None, dict | None]:
    """One HUMAN-directed revision of a single candidate: the reviewer's `instruction` becomes a
    fix hint (the same shape the critic loop uses), the model proposes ONE revision, and the
    revision runs the full single-candidate gauntlet. Returns (revised_idea, None) on success or
    (None, rejection_dict) when the revision fails — a rejection is DATA for the human, not an
    error. The revision is still only a proposal: registering it remains a separate explicit
    confirm. `instruction` is user text and goes through the audited egress-guarded seam like every
    other feature-assist call; a blocked or empty model response returns code NO_REVISION.

    `objective` is the round's prediction goal; when present it rides in the LLM inputs alongside
    the fix hint (the same way `feedback` rides in the generation rounds), so the model revises
    against the goal the candidate was generated for, not the instruction alone.

    DEDUP PARITY with the generation loop: a revision that duplicates an already-REGISTERED
    feature (same (derives_pairs, aggregation) signature _vet checks) is rejected with
    ALREADY_REGISTERED. The loop's other dedup — REDUNDANT vs the current round's candidates —
    stays CLIENT-side: the server is stateless about the round, so only the UI knows that list."""
    cols = _candidate_columns(conn, catalog_source, roles, entity)
    known = {c["object_ref"] for c in cols}
    src_of: dict[str, set[str]] = {}
    for c in cols:
        src_of.setdefault(c["object_ref"], set()).add(c["catalog_source"])
    fix = [{"name": idea.get("name", ""), "derives_from": idea.get("derives_from", []),
            "aggregation": idea.get("aggregation"), "issue": instruction}]
    try:
        menu, table_context = _build_menu(conn, cols, objective=objective, entity=entity,
                                          roles=roles)
    except ContextTooLarge as exc:
        return None, {"name": str(idea.get("name", "")), "reason": str(exc),
                      "code": RejectCode.CONTEXT_TOO_LARGE}
    inputs: dict = {"columns": menu, "fix": fix}
    if table_context:
        inputs["table_context"] = table_context
    if objective:
        inputs["objective"] = objective
    out = _call_raw(conn, client, "overlay.feature.recommend", "feature_recommend_v1",
                    "feature_ideas", instruction, inputs, actor=actor,
                    prompt_version=_feature_schema_version(),
                    schema_version=_feature_schema_version())
    proposed = out.get("features", [])
    if not proposed:
        return None, {"name": str(idea.get("name", "")),
                      "reason": "no revision was produced", "code": RejectCode.NO_REVISION}
    raw = proposed[0] if isinstance(proposed[0], dict) else {}
    revised, rej = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within,
                                  roles=roles)
    if rej is not None or revised is None:
        rej = rej or Rejection(RejectCode.NO_REVISION, "no revision was produced")
        return None, {"name": str(raw.get("name", "")), "reason": rej.message, "code": rej.code}
    if (frozenset(revised.derives_pairs), revised.aggregation) in _registered_signatures(conn):
        return None, {"name": revised.name, "reason": "already a registered feature",
                      "code": RejectCode.ALREADY_REGISTERED}
    return revised, None


@dataclass(frozen=True, slots=True)
class Recipe:
    intent: str
    grain_table: str | None
    derives_from: list[str]           # grounded object_refs
    aggregation: str | None
    as_of_column: str | None
    join_path: list[JoinStep] = field(default_factory=list)   # deterministic, real edges


def feature_recipe(conn, nl_query: str, client: LLMClient, *, catalog_source: str,
                   roles: Iterable[str] = (),
                   actor: IdentityEnvelope | None = None) -> Recipe:
    cols = _candidate_columns(conn, catalog_source, roles)
    known = {c["object_ref"] for c in cols}
    try:
        menu, table_context = _build_menu(conn, cols, objective=nl_query, roles=roles)
    except ContextTooLarge as exc:
        logger.warning("feature-recipe context too large for %r: %s", nl_query, exc)
        return Recipe(intent=nl_query, grain_table=None, derives_from=[], aggregation=None,
                      as_of_column=None)
    recipe_inputs: dict = {"columns": menu}
    if table_context:
        recipe_inputs["table_context"] = table_context
    out = _call_raw(conn, client, "overlay.feature.recipe", "feature_recipe_v1", "feature_recipe",
                    nl_query, recipe_inputs, actor=actor,
                    prompt_version=_feature_schema_version(),
                    schema_version=_feature_schema_version())
    derives = [d for d in out.get("derives_from", []) if d in known]
    grain = out.get("grain_table")
    join_table = out.get("join_table")
    # The LLM says WHAT to compute; the join PATH is found deterministically (real edges only).
    path: list[JoinStep] = []
    if grain and join_table and grain != join_table:
        path = find_join_path(conn, catalog_source, grain, join_table, roles=roles) or []
    return Recipe(intent=nl_query, grain_table=grain, derives_from=derives,
                  aggregation=out.get("aggregation"), as_of_column=out.get("as_of_column"),
                  join_path=path)


@dataclass(frozen=True, slots=True)
class LeakageWarning:
    object_ref: str
    reason: str


def leakage_check(conn, derives_from: list[str], target_ref: str,
                  client: LLMClient, *,
                  actor: IdentityEnvelope | None = None) -> list[LeakageWarning]:
    used = set(derives_from)
    # leakage input does not widen under the flag — stays v1 (RF-I8/recon #6)
    out = _call_raw(conn, client, "overlay.feature.leakage", "feature_leakage_v1", "leakage",
                    "Flag columns that leak the prediction target.",
                    {"derives_from": list(derives_from), "target": target_ref}, actor=actor,
                    prompt_version=1, schema_version=1)
    return [LeakageWarning(object_ref=w["object_ref"], reason=str(w.get("reason", "")))
            for w in out.get("leaks", [])
            if isinstance(w, dict) and w.get("object_ref") in used]


@dataclass(frozen=True, slots=True)
class FeatureSet:
    lens: str                       # the strategy this set explores (behavioral, monetary, ...)
    features: list[FeatureIdea]     # all validated (each ran the gauntlet)


@dataclass(frozen=True, slots=True)
class SetRecommendation:
    recommended_lens: str
    reasoning: str                  # ADVISORY — grounded in hypothesis + metadata, not a performance claim
    # Product surface copy: plain declarative, no em dashes (frontend/PRODUCT.md voice).
    caveat: str = ("advisory only: a fit/coverage judgment over the metadata, not a performance "
                   "prediction; confirm the winner with a backtest once features are computed")


_NUMERIC_TYPES = ("numeric", "integer", "bigint", "int", "int4", "int8", "smallint", "float",
                  "double", "double precision", "decimal", "real", "money")


def _is_numeric(data_type: str | None) -> bool:
    base = (data_type or "").lower().split("(")[0].strip()   # numeric(10,2) -> numeric
    return base in _NUMERIC_TYPES


def route_strategies(conn, cols: list[dict]) -> list[tuple[str, str]]:
    """§14.8 Router: DETERMINISTICALLY pick which typed feature-strategy families APPLY to this
    candidate set, from the graph's shape — so generation never wastes a round proposing a feature the
    data can't support (which the gauntlet would only reject). `unary` always applies; the rest gate on
    structure: `ratio` needs >=2 numeric columns; `temporal` needs a point-in-time (as-of) column;
    `aggregation` needs a join key; `distributional` needs an entity to form a peer group. Returns
    (strategy_name, prompt_focus) pairs."""
    picks = [("unary", "single-column transforms — bucketing, flags, or log of one column")]
    refs = [c["object_ref"] for c in cols]
    sources = [c["catalog_source"] for c in cols]
    if not refs:
        return picks
    # Source-qualified: match the exact (catalog_source, object_ref) pairs, so a same-named column in
    # ANOTHER catalog can't contaminate strategy selection (wrong type / as-of / entity).
    rows = conn.execute(
        "SELECT data_type, is_as_of, entity, declared_type FROM graph_node WHERE kind = 'column' "
        "AND (catalog_source, object_ref) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
        (sources, refs)).fetchall()
    # A column is numeric-capable if OPERATIONAL data_type is numeric OR the FTR-declared_type hint is
    # (spec §2 [F10]): the hint ENABLES the numeric strategy so an FTR feature is proposed, while
    # operational data_type stays 'unknown' and the validator still returns NEEDS_EXTERNAL_VALIDATION.
    if sum(1 for dt, _, _, decl in rows if _is_numeric(dt) or _is_numeric(decl)) >= 2:
        picks.append(("ratio", "ratios / cross-features between two numeric columns (e.g. utilization)"))
    # aggregation applies if a candidate column is a join key (from_ref) OR the parent column that
    # children join to (to_ref) — the entity-grain "aggregate children up" case. graph_edge stores
    # BOTH endpoints COLUMN-level (public.table.column — declared edges in graph.py and Pass-C
    # projected edges alike), so both sides compare against the candidate column refs. Scoped to
    # the candidate catalogs so cross-catalog same-named refs don't spuriously enable it.
    # authority='operational' (Task 7): a governed-seam display-only edge must NOT enable a feature
    # strategy — the confirmed approved_join fact is the source of truth once the seam is on.
    # Governed edge filter (Pass C Task 8): a fact-LINKED edge enables a strategy only while its
    # approved_join fact is VERIFIED; a declared edge (fact_key NULL) is untouched.
    if conn.execute("SELECT 1 FROM graph_edge WHERE kind = 'joins' AND authority = 'operational' "
                    "AND (approved_join_fact_key IS NULL OR approved_join_status = 'VERIFIED') "
                    "AND catalog_source = ANY(%s) "
                    "AND (from_ref = ANY(%s) OR to_ref = ANY(%s)) LIMIT 1",
                    (list(set(sources)), refs, refs)).fetchone() is not None:
        picks.append(("aggregation", "aggregations (count/sum/avg) over related child rows via a join key"))
    if any(a for _, a, _, _ in rows):
        picks.append(("temporal", "recency / trend / velocity over a point-in-time (as-of) column"))
    if any(e for _, _, e, _ in rows):
        picks.append(("distributional",
                      "distributional features vs a peer group (z-score / percentile per entity)"))
    return picks


@dataclass(frozen=True, slots=True)
class SetsReport:
    """recommend_feature_sets plus the rejections aggregated across EVERY lens's loop (same
    {"name", "reason", "code"} shape as RecommendReport, deduplicated across lenses) — the Gate #3
    transparency the single-list report gives, for the multi-set flow."""
    sets: list[FeatureSet]
    rejections: list[dict]


def recommend_feature_sets_report(conn, objective: str, client: LLMClient, *,
                                  entity: str | None = None, catalog_source: str | None = None,
                                  roles: Iterable[str] = (), target_ref: str | None = None,
                                  now: datetime | None = None,
                                  fresh_within: timedelta = timedelta(hours=24),
                                  lenses: tuple[str, ...] | None = None,
                                  per_set: int = 3, budget: int = 2,
                                  feedback: str | None = None,
                                  actor: IdentityEnvelope | None = None) -> SetsReport:
    """recommend_feature_sets with the same kwargs and semantics, returning the sets AND the
    rejections every lens's loop recorded (the same per-round avoid lists, deduplicated). `feedback`
    is HUMAN guidance applied to every lens's generation rounds (see recommend_features)."""
    if lenses is None:
        strategies = route_strategies(conn, _candidate_columns(conn, catalog_source, roles, entity))
    else:
        strategies = [(lens, lens) for lens in lenses]
    sets: list[FeatureSet] = []
    rejections: list[dict] = []
    for name, focus in strategies:
        ideas, avoid = _generate(
            conn, f"{objective} (focus: {focus})", client, entity=entity,
            catalog_source=catalog_source, roles=roles, target_ref=target_ref, now=now,
            fresh_within=fresh_within, target=per_set, budget=budget, feedback=feedback,
            actor=actor)
        sets.append(FeatureSet(lens=name, features=ideas))
        rejections.extend(avoid)
    return SetsReport(sets=sets, rejections=_dedupe_rejections(rejections))


def recommend_feature_sets(conn, objective: str, client: LLMClient, *,
                           entity: str | None = None, catalog_source: str | None = None,
                           roles: Iterable[str] = (), target_ref: str | None = None,
                           now: datetime | None = None, fresh_within: timedelta = timedelta(hours=24),
                           lenses: tuple[str, ...] | None = None,
                           per_set: int = 3, budget: int = 2,
                           feedback: str | None = None,
                           actor: IdentityEnvelope | None = None) -> list[FeatureSet]:
    """Generate N DIVERSE, each-fully-validated feature sets — one per strategy — by running the loop
    once per strategy. When `lenses` is None (default) the §14.8 Router picks the APPLICABLE typed
    strategies from the data's shape (skipping e.g. temporal when there's no as-of column); pass explicit
    `lenses` to force a fixed set. Every feature in every set has passed the gauntlet, so the human only
    ever curates among SAFE options. `feedback` is HUMAN guidance threaded into every lens's
    generation rounds (see recommend_features)."""
    return recommend_feature_sets_report(
        conn, objective, client, entity=entity, catalog_source=catalog_source, roles=roles,
        target_ref=target_ref, now=now, fresh_within=fresh_within, lenses=lenses,
        per_set=per_set, budget=budget, feedback=feedback, actor=actor).sets


def set_signals(conn, feature_set: FeatureSet) -> dict:
    """Deterministic ranking signals for a set (item 1b) — computed WITHOUT data, BEFORE the LLM's
    advisory fit pick: size, distinct source columns, and domain coverage (distinct domains the set's
    features span). More domains covered + fewer duplicate columns = a broader, less redundant set."""
    pairs = {(cs, ref) for f in feature_set.features for cs, ref in f.derives_pairs}
    domains: set[str] = set()
    if pairs:
        sources = [cs for cs, _ in pairs]
        refs = [ref for _, ref in pairs]
        # Source-qualified: a same-named column in another catalog must not add a phantom domain.
        rows = conn.execute(
            "SELECT DISTINCT domain FROM graph_node WHERE domain IS NOT NULL "
            "AND (catalog_source, object_ref) IN (SELECT * FROM unnest(%s::text[], %s::text[]))",
            (sources, refs)).fetchall()
        domains = {r[0] for r in rows}
    return {"size": len(feature_set.features), "distinct_columns": len(pairs),
            "domains_covered": len(domains), "domains": sorted(domains)}


def recommend_set(conn, sets: list[FeatureSet], hypothesis: str,
                  client: LLMClient, *,
                  actor: IdentityEnvelope | None = None) -> SetRecommendation:
    """Advisory: the LLM reasons over the validated sets + the analyst's HYPOTHESIS (+ the metadata
    already in each feature) and recommends one, WITH reasons — a fit/coverage judgment, never a
    performance prediction (see SetRecommendation.caveat)."""
    # Deterministic signals FIRST (coverage/redundancy), so the LLM's advisory fit pick is informed by
    # them rather than judging on prose alone (item 1b — "rank on deterministic signals first").
    summary = [{"lens": s.lens, "signals": set_signals(conn, s),
                "features": [{"name": f.name, "derives_from": f.derives_from,
                              "aggregation": f.aggregation} for f in s.features]} for s in sets]
    # recommend_set input does not widen under the flag — stays v1 (RF-I8/recon #6)
    out = _call_raw(conn, client, "overlay.feature.recommend_set", "feature_set_v1",
                    "feature_set_rec", hypothesis, {"sets": summary}, actor=actor,
                    prompt_version=1, schema_version=1)
    default = sets[0].lens if sets else ""
    return SetRecommendation(recommended_lens=str(out.get("recommended_lens", default)),
                             reasoning=str(out.get("reasoning", "")))
