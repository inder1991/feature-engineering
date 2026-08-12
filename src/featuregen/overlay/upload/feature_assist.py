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
from collections.abc import Iterable, Mapping
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
from featuregen.overlay.field_evidence import canonical_hash, read_active_field_evidence
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
from featuregen.overlay.upload.enrich_llm import (
    audited_structured_call,
    drive_audited_structured_call,
)
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
from featuregen.overlay.upload.object_ref import normalize_ref
from featuregen.overlay.upload.operational_facts import read_operational_value
from featuregen.overlay.upload.pii_policy_store import active_pii_use_policies
from featuregen.overlay.upload.planner.plan_envelope import PlanEnvelopeV1
from featuregen.overlay.upload.profile_store import current_catalog_narrative_block
from featuregen.overlay.upload.read_scope import (
    allowed_sensitivities,
    read_scope_rule_content_hash,
)
from featuregen.overlay.upload.semantic_context import (
    bundle_from_store,
    for_feature_generation,
)
from featuregen.overlay.upload.structured_results import (
    find_structured_result,
    record_structured_result,
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
    # The model's own `grounding` array named a column that matches NOTHING the catalog offered
    # (Task 6c). The feature may still compute — its `derives_from` grounded — but the account it
    # gives of itself cites something that does not exist, so the proposal is discarded rather than
    # shown with a fabricated explanation attached. Narrow by construction: an AMBIGUOUS name, a
    # missing one and an unreadable role all cost their own entry and never the feature
    # (`_ground_notes`), because none of those is a false claim about the catalog.
    UNKNOWN_GROUNDING_COLUMN = "UNKNOWN_GROUNDING_COLUMN"
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
    # Same family as UNGROUNDED / UNKNOWN_COLUMN: the proposal, AS STATED, cannot be audited. No
    # setting a human could change would make a citation of a non-existent column true.
    RejectCode.UNKNOWN_GROUNDING_COLUMN: STRUCTURALLY_UNSUITABLE,
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
           "t.data_role, t.authority_role, t.temporal_storage_model, t.business_context, "
           # Task 7b: `schema_name` is not payload — it is the key `_business_terms` rebuilds each
           # row's SCHEMA-PRESERVING logical ref from, because `field_evidence` is keyed by that ref
           # while `graph_node.object_ref` is the public-flattened one. Selected here so the term
           # lookup needs no second graph query.
           "c.schema_name "
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
    out = [{"catalog_source": r[0], "object_ref": r[1], "table": r[2], "column": r[3],
            "concept": r[4], "domain": r[5], "definition": r[6], "ai_summary": r[7],
            "data_type": r[8], "declared_type": r[9], "semantic_terms": r[10], "entity": r[11],
            "additivity": r[12], "unit": r[13], "currency": r[14], "is_grain": r[15],
            "is_as_of": r[16], "grain_fact_event_id": r[17], "availability_fact_event_id": r[18],
            # The LEFT JOIN's table fields sit AFTER the column fields — inserting `ai_summary`
            # shifted every index past it, and these two were the tail I missed first time.
            "table_definition": r[19], "table_primary_entity": r[20],
            "table_data_role": r[21], "table_authority_role": r[22],
            "table_temporal_storage_model": r[23], "table_business_context": r[24],
            "schema_name": r[25]}
           for r in rows]
    terms = _business_terms(conn, out)
    for row in out:
        row["business_term"] = terms.get(_logical_ref_of_row(row))
    return out


def _logical_ref_of_row(row: dict) -> str:
    """The row's SCHEMA-PRESERVING logical ref — the key every evidence store uses. `object_ref` on
    the menu is the PUBLIC-FLATTENED graph ref (see `_context_v4_column`), and looking evidence up
    by that spelling matches no row: the field would populate for a public-schema catalog and
    silently never populate for a glossary catalog, which is the harder half of that bug to notice.
    """
    return normalize_ref(row["catalog_source"], row.get("schema_name") or None,
                         row["table"], row["column"])


def _business_terms(conn, rows: list[dict]) -> dict[str, str]:
    """`{logical_ref: business_term}` for these candidates, in ONE batched read (Task 7b).

    The glossary's curated business NAME for a column has no `field_resolution._DISPLAY_COLUMN`
    entry, so it reaches no flat `graph_node` column and the candidate query cannot join it. It
    lives as SOURCE evidence, which is exactly where `semantic_context.bundle_from_store` reads it
    from for the payload — this read exists because `_column_tokens` scores the CANDIDATE row, not
    the (lazily built, budget-gated) enriched payload, so a payload-only fix would leave the
    objective/column intersection as blind to the bank's own vocabulary as it was.

    PRODUCER-SCOPED TO `source`, and that is not caution — it is what keeps the ranking and the
    PAYLOAD looking at the same value. `bundle_from_store` builds `source_semantics` from SOURCE
    rows only, so that is exactly the `business_term` `for_feature_generation` emits; an unscoped
    read here would let the ranking match a term the model is never shown. (A HUMAN-corrected
    business term reaches neither surface today — the bundle carries no resolved projection for a
    field with no `_DISPLAY_COLUMN` entry. That is a pre-existing gap in the bundle, and closing it
    HERE alone would create the divergence this scope exists to prevent.)

    Read scope needs no predicate here: `rows` are already the scoped candidates, and this reads
    only their own refs. Fail-soft — a term is ranking colour, never a gate."""
    if not rows:
        return {}
    refs = sorted({_logical_ref_of_row(r) for r in rows})
    try:
        found = conn.execute(
            "SELECT DISTINCT ON (logical_ref) logical_ref, proposed_value FROM field_evidence "
            "WHERE field_name = 'business_term' AND lifecycle = 'active' AND producer = 'source' "
            "AND logical_ref = ANY(%s) ORDER BY logical_ref, created_at DESC, evidence_id DESC",
            (refs,)).fetchall()
    except Exception:  # noqa: BLE001 — advisory: a curated term never fails the request
        logger.warning("advisory business_term read failed for %d candidates", len(rows),
                       exc_info=True)
        return {}
    return {ref: value for ref, value in found if isinstance(value, str) and value}


def _menu(cols: list[dict]) -> list[dict]:
    return [{k: c[k] for k in ("object_ref", "table", "column", "concept", "domain")} for c in cols]


FEATURE_CONTEXT_FLAG = "FEATUREGEN_FEATURE_CONTEXT"


def feature_context_enabled() -> bool:
    """ALWAYS ON — the FEATUREGEN_FEATURE_CONTEXT gate retired with the pre-live
    simplification (2026-08-11): the rich Slice-3 context (menu widening, per-table context,
    relevance, versioned shape) is the platform's core capability and was already ON in every
    deployment; a dark thin-menu fallback nobody serves is complexity, not safety. The
    function remains (7 call sites; RF-C3's one-public-definition rule) but no longer reads
    env. Version fallback stays a separate concern (FEATUREGEN_FEATURE_CONTEXT_VERSION)."""
    return True


#: The input contract's numeric version, stamped on the immutable llm_call record.
#:   1 — the base menu.
#:   2 — the widened feature-context menu (definition + semantic_terms prose).
#:   3 — v2 plus `ai_summary`.
#:   4 — the shared `SemanticContextBundleV1` contract (semantic Task 8): v3 plus concept ancestry,
#:       identifier namespace/issuer, party role, the D2 (producer, strength) axes per semantic
#:       field, current cross-catalog links and the closed missing-context codes.
#:   5 — Task 6c. The first version whose OUTPUT contract moved rather than its input: every
#:       proposed feature returns `grounding`, its own account of which offered column it used.
#: Bumped because the record must identify WHICH contract egressed. Adding a field to the payload
#: while leaving the version at 2 makes a v2 record ambiguous — with or without summaries — which
#: defeats the reason the version is stamped at all.
_FEATURE_CONTEXT_SCHEMA_VERSION = 5

#: The version from which the OUTPUT carries `grounding`. Below it the wire item is CLOSED without
#: a `grounding` key, so the generation instruction must not ask for one — a model is never asked
#: for output its schema forbids.
_GROUNDING_SCHEMA_VERSION = 5

#: The D8 ROLLBACK LADDER, in one place:
#:   flag off                              -> v1, the thin pre-Slice-3 menu, byte-for-byte;
#:   flag on + FEATUREGEN_FEATURE_CONTEXT_VERSION=3 -> today's SHIPPED v3 behaviour;
#:   flag on + FEATUREGEN_FEATURE_CONTEXT_VERSION=4 -> v4, the rich menu with NO returned grounding;
#:   flag on (default)                     -> v5.
#: The env override exists precisely so v3 stays REACHABLE after Task 8 — a rollback that dropped
#: to the v1 thin menu would be a functional regression dressed as a safety valve. Only versions
#: this module can actually render are honoured; anything else falls back to the default and warns,
#: because a typo in a deploy manifest must not silently downgrade the contract.
FEATURE_CONTEXT_VERSION_ENV = "FEATUREGEN_FEATURE_CONTEXT_VERSION"
_SELECTABLE_CONTEXT_VERSIONS = (3, 4, 5)


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
#: context that dropped them to fit would be smaller and less safe. The adjudication signals
#: (`confidence_band` / `concept_alternatives`, Task 6b) are absent for the same reason and one
#: more: they exist ONLY on the columns the platform could not settle, they cost ~27-92 bytes
#: there, and shedding them would remove the uncertainty marker from exactly the columns whose
#: uncertainty the model most needs to see.
_V4_TRIM_ORDER: tuple[str, ...] = ("semantic_terms", "ai_summary", "definition", "relationships",
                                   "concept_path")


#: The TABLE-context field names the v4 payload emits per column (`table_role`,
#: `event_or_snapshot` — Task 6). Their authority is folded into `semantic_authority` below;
#: `table_context`'s OTHER fields (`definition`, `domain`, `ai_summary`, `semantic_terms`) share
#: their names with the COLUMN's own fields, and folding those in would label the column's value
#: with the table's authority. Hence an explicit list, not "everything on table_context".
_V4_TABLE_AUTHORITY_FIELDS: frozenset[str] = frozenset({"table_role", "event_or_snapshot"})

#: The SOURCE-only semantic fields the v4 payload emits (Task 7b). Both are curated glossary values
#: that reach no `graph_node` display column, so they ride `bundle.source_semantics` alone and the
#: `resolved_semantics` loop below never sees them — a value would have egressed with nothing beside
#: it saying who vouched for it. Named EXPLICITLY, exactly like `_V4_TABLE_AUTHORITY_FIELDS`, and not
#: "everything on source_semantics": most source fields share a name with the column's own resolved
#: field (`definition`, `domain`, `entity`…), and folding those in would label the resolved value
#: with the DECLARED value's authority.
_V4_SOURCE_AUTHORITY_FIELDS: frozenset[str] = frozenset({"business_term", "related_terms"})


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
    # The two TABLE axes the payload also emits live on `table_context`, which the loop above never
    # walks — without this they would egress with nothing beside them saying who proposed them. A
    # column field of the same name always wins: the column's own authority is never overwritten.
    for value in bundle.table_context:
        if value.field_name not in _V4_TABLE_AUTHORITY_FIELDS or value.field_name in out:
            continue
        if not value.evidence:
            continue
        lead = value.evidence[0]
        out[value.field_name] = f"{lead.producer}/{lead.strength}"
    # The two SOURCE-only glossary fields (Task 7b), same rule and same precedence: a resolved field
    # of the same name always wins, so a column's own authority is never overwritten.
    for value in bundle.source_semantics:
        if value.field_name not in _V4_SOURCE_AUTHORITY_FIELDS or value.field_name in out:
            continue
        if not value.evidence:
            continue
        lead = value.evidence[0]
        out[value.field_name] = f"{lead.producer}/{lead.strength}"
    return out


def _context_v4_column(conn, c: dict, *, roles: Iterable[str]) -> dict:
    """One v4 column payload: `SemanticContextBundleV1.for_feature_generation` plus the D2 axes.

    The bundle is the ONE assembly (semantic Task 1) — read-scoped and batched at that seam — so
    this path cannot drift from the Context tab or from data-agent retrieval, which read the same
    contract. Everything the adapter emits is already egress-classified (D10); the keys added here
    (`semantic_authority`, `party_role` promoted out of the adapter's identity block, and the
    adjudication's `confidence_band` / `concept_alternatives`) are classified alongside them.

    The two ADJUDICATION keys are joined here rather than in the bundle on purpose — see the note
    at the join. They ride the anchor's own read scope: a column whose bundle `roles` refuses never
    reaches that read at all, because the fallback below returns first.

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
    # The adjudicator's JUDGEMENT signals. Deliberately joined HERE and NOT in the bundle:
    # `semantic_context`'s own docstring draws that line — an adjudication is a judgement ABOUT the
    # semantics, not one of them, so it is served BESIDE the bundle — and this keeps it. Both are
    # ADVISORY: nothing here or downstream branches on `confidence_band` (the Task-5 contract:
    # explanation, never authority — it cannot turn an `llm/proposed` concept into a stronger
    # assertion), and `concept_alternatives` is context for the model to weigh, never a
    # classification. Neither carries or implies an authority of its own; the concept's authority is
    # `semantic_authority` above, unchanged.
    #
    # READ IT BY `bundle.object_ref`, NOT `c["object_ref"]`. The 1046 adjudication subject pointer is
    # keyed by the SCHEMA-PRESERVING logical ref, which is what the bundle carries; `c` and the
    # emitted payload carry the PUBLIC-FLATTENED graph ref (see the note on `out["object_ref"]`
    # above). Passing the flattened form matches no pointer row, so every column would silently come
    # back unadjudicated — the keys would simply never appear and the feature would look implemented.
    #
    # Deferred import, matching `asset_detail._semantic_adjudication_section`, to keep the
    # module-import cycle that module already avoids (`semantic_adjudication` imports `enrich_llm`).
    from featuregen.overlay.upload.semantic_adjudication import load_current_adjudication

    adjudication, _result_id = load_current_adjudication(conn, bundle.object_ref)
    if adjudication is not None:
        out["confidence_band"] = adjudication.confidence_band
        out["concept_alternatives"] = list(adjudication.alternatives)
    # The adapter emits `null` for anything the bundle does not hold; a null in a prompt is noise
    # the egress scanner still has to walk. Drop the empties — absence IS the honest signal, and it
    # is what a never-adjudicated column (the NORMAL case: adjudication is the exception path) and
    # an adjudication with no shortlist both carry.
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


# ── the table-fact AUTHORITY axis (Task 8b) ──────────────────────────────────────────────────────
#
# WHO ASSERTED THIS VALUE. Task 8 shipped `confirmed` | `declared`, and its review proved the pair
# was wrong in both halves: `ingest._assert_fact` AUTO-CONFIRMS every file-declared grain/as-of with
# `authority_basis=source_declared`, so `confirmed` was worn by an unreviewed CSV flag and by a human
# endorsement alike; and a Pass B AI proposal never sets `is_grain` at all, so the value this whole
# plan exists to surface reached nothing. These three tokens are the axis the CODE actually knows.
#
# THIS IS A LABEL, NEVER A PERMISSION. None of the three admits anything to the execution path: a
# feature that runs against the warehouse still needs a VERIFIED fact, and the guards that say so
# (`resolve_fact`, `read_governed_grain`, `semantic_bindings/projection.py`, `materialize/spine.py`)
# read the fact stream and none of them reads this block. `resolve_fact` refusing to serve a PROPOSED
# fact is what keeps that honest and is deliberately untouched here — this reads the proposed state
# BESIDE the resolved one and labels the difference.
STATUS_HUMAN_CONFIRMED = "human_confirmed"
STATUS_SOURCE_DECLARED = "source_declared"
STATUS_AI_PROPOSED = "ai_proposed"
#: The CLOSED vocabulary. Every member must also be defined for the model in
#: `_TABLE_CONTEXT_STATUS_DIRECTIVE` — a token whose meaning lives only in a docstring is not
#: labelling, which is the finding Task 8's review raised about its own two.
TABLE_FACT_STATUSES = frozenset({STATUS_HUMAN_CONFIRMED, STATUS_SOURCE_DECLARED,
                                 STATUS_AI_PROPOSED})


#: Folds on which a HUMAN confirmation, if the stream has one, HAS NOT BEEN WITHDRAWN — so the label
#: follows the signature stamped on the row rather than the fact's current servability.
#:
#: NAMED FOR "NOT WITHDRAWN", NOT "STANDS", and the rename is the finding: round 2 called this
#: `_ENDORSEMENT_STANDS` and wrote "that sign-off still stands" into the model-facing sentence, which
#: is FALSE for the two lapsed folds this very tuple admits. A constant whose name asserts more than
#: its members support is the same defect as a token whose meaning lives only in a docstring — one
#: level down, where the next reader copies it into prose without re-deriving it.
#:
#: THE STAMP OUTLIVES VERIFIED, BY DESIGN. `graph_node.grain_fact_event_id` is written by
#: `table_fact_projection` from the confirmed event, and NOTHING clears it when the fact leaves
#: VERIFIED: `expiry` demotes join edges and semantic bindings, `catalog_changes._stale_one` appends
#: the STALED event and calls no table-fact projection, and the overlay projection touches only
#: `overlay_fact_state`/`overlay_proposal`. `stamp_reconcile` states the intent outright — a fact
#: `resolve_fact` will not serve is "left drifted-and-visible rather than force-stamped or, worse,
#: wiped". So `_grain_block`'s confirmed branch keeps firing off a human's own signature long after
#: the fact stops being servable, and testing `status == "VERIFIED"` here answered
#: `source_declared` — "no human review" — for a grain whose reviewer's event id is stamped on the
#: very row the block was built from.
#:
#: EXPIRY AND STALING ARE LAPSES; REJECTION IS A REPUDIATION. A TTL firing or a drift scan retires a
#: signature by the clock or by the catalog moving underneath it — nobody withdrew it. A human
#: rejecting the re-verification is a person saying THIS VALUE IS WRONG, and `_AWAITING_CONFIRMATION`
#: admits exactly REVERIFY and STALE to that command, so it is reachable. REJECTED is therefore
#: excluded: the endorsement must be UNWITHDRAWN, not merely have existed. It need NOT still be in
#: force — a lapsed signature is admitted here on purpose, and the model-facing sentence claims
#: exactly that much and no more.
#:
#: EVERY FOLD THAT CAN REACH `_grain_block`'s CONFIRMED BRANCH (i.e. the row is still stamped), and
#: what it emits:
#:
#:   VERIFIED             -> human_confirmed | source_declared   (who authored the confirm)
#:   REVERIFY             -> human_confirmed | source_declared   (TTL lapse; not a withdrawal)
#:   STALE                -> human_confirmed | source_declared   (drift lapse; not a withdrawal)
#:   REJECTED             -> source_declared                     (signature withdrawn)
#:   PARTIALLY_CONFIRMED  -> source_declared                     (UNREACHABLE for a table fact —
#:                                                                see below; NOT "no stamp to read")
#:   DRAFT (re-proposed)  -> source_declared                     (PROPOSED clears confirmed_event_id)
#:   no stream at all     -> source_declared                     (nothing to evidence)
#:
#: PARTIALLY_CONFIRMED IS UNREACHABLE HERE, and the reason matters because this table is the
#: checklist the next change to this surface works from. It would NOT be safe by the stamp being
#: absent: reached from REVERIFY/STALE its fold branch touches only `status` and `partial_confirmers`
#: (`state.py`), so `confirmed_event_id` — and the row's stamp — survive it exactly as they survive
#: REJECTED. It is moot only because `OVERLAY_FACT_PARTIALLY_CONFIRMED` has exactly ONE emitter,
#: `join_confirmation._confirm_approved_join`, which is dispatched for `approved_join` alone (the
#: dual-owner path). Grain and availability_time are single-confirmer facts and never enter it. If a
#: table fact ever gains dual authority, this row stops being moot and must be decided on its merits.
#:
#: which is why `source_declared`'s sentence is that no sign-off STANDS — the one claim true of every
#: state in its column — while `human_confirmed` claims only that the sign-off has NOT BEEN
#: WITHDRAWN, which is the one claim true of every state in ITS column. They are deliberately
#: different predicates: REVERIFY and STALE have a signature that is not withdrawn but no longer
#: stands, and saying "still stands" there was the third breach of the clause-truth rule.
_ENDORSEMENT_NOT_WITHDRAWN = ("VERIFIED", "REVERIFY", "STALE")


@dataclass(frozen=True, slots=True)
class _FactEvent:
    """The four fields the overlay lifecycle fold and the human-review rule read off an event.

    `fold_overlay_state` states its own input contract in its docstring — "Each item exposes
    `.type`, `.event_id`, `.payload`" — and `bridge_assessment._human_reviewed` adds
    `.actor.actor_kind`. Selecting four columns rather than the nineteen `row_to_event` needs keeps
    this a narrow read on the assembly path. The fold and the human-review rule themselves are
    REUSED, never reimplemented: a second copy of an identity-bearing rule is a rule that drifts.
    """

    type: str
    event_id: str
    payload: Mapping[str, object]
    actor: IdentityEnvelope


def _machine_proposal(stream: list[_FactEvent], draft_event_id: str | None,
                      fact_type: str) -> list[str] | str | None:
    """The value of an OPEN, MACHINE-authored proposal — the grain's sorted column list or the
    as-of column name — or None when this DRAFT is not one, or its value will not read.

    THE ACTOR CHECK IS THE POINT. `ai_proposed` claims a MODEL inferred the value. Every grain
    DRAFT this codebase writes today comes from Pass B under the service actor `_ENRICH_ACTOR`
    (`table_synth._propose_table_facts`), and `table_fact_governance` already hard-codes that
    assumption for its own queue (`origin = "llm_proposed_not_profiled"`). But `propose_fact` is a
    generic governed command: a human-actor DRAFT would wear a label that is simply false about who
    wrote it, so it is not surfaced at all — which is exactly what shipped before this task.

    Mirrors `_human_reviewed`'s shape deliberately, including its failure direction: anything this
    cannot positively establish returns the weaker answer rather than the flattering one.
    """
    draft = next((e for e in stream if e.event_id == draft_event_id), None)
    if draft is None or draft.actor.actor_kind == "human":
        return None
    value = draft.payload.get("proposed_value")
    if not isinstance(value, Mapping):
        return None
    if fact_type == "grain":
        columns = value.get("columns")
        if not (isinstance(columns, list) and columns
                and all(isinstance(c, str) and c for c in columns)):
            return None
        return sorted(columns)
    column = value.get("column")
    return column if isinstance(column, str) and column else None


def _table_fact_authority(conn, cols: list[dict]) -> dict[tuple[str, str], dict]:
    """Who asserted each candidate table's grain / availability fact, read ONCE per assembly.

    Returns ``{(catalog_source, table): {fact_type: {"human": bool, "proposed": value|None}}}`` —
    ``human`` says the VERIFIED fact carries a real human endorsement rather than ingest's
    source-declared auto-confirm; ``proposed`` carries an open MACHINE proposal's value (a sorted
    column list for ``grain``, a column name for ``availability_time``) and is None whenever a fact
    is confirmed, rejected, expired, human-authored or absent.

    ONE QUERY, AND IT MUST STAY ONE. `_table_context` is called once for the mandatory set and then
    once per optional column as the budget loop searches for a fit, so this is resolved by
    `select_relevant_context` BEFORE that loop — the same hoist, for the same reason, that Task 7b
    applied to the catalog narrative. `fact_key` is a pure sha256 over the identity tuple, so the
    2xN keys are computed with no query at all and the read is a single `aggregate_id = ANY(...)`
    probe of `events_stream_idx (aggregate, aggregate_id, stream_version)`.

    WHY THE EVENT STREAM AND NOT A READ MODEL. `overlay_proposal` would answer the proposal half in
    one statement, but it carries no index on `catalog_source`/`fact_type` (a seq scan), it lags the
    async projector, and it cannot answer the human-vs-source half at all — that needs the CONFIRMED
    event's own actor and payload, which is precisely what `_human_reviewed` reads. One stream read
    answers both, using the platform's own rules for both.

    FAIL-SOFT. An unreadable stream returns an empty map, never an exception: this is prompt CONTEXT,
    and a catalog must not lose feature generation over it (the same rule the catalog narrative was
    given after it took a catalog down). `_table_context` then falls back to the weaker claim.
    """
    tables = sorted({(c["catalog_source"], c["table"]) for c in cols})
    if not tables:
        return {}
    try:
        from featuregen.events.serde import identity_from_jsonb
        from featuregen.overlay.identity import fact_key
        from featuregen.overlay.state import fold_overlay_state
        from featuregen.overlay.upload.bridge_assessment import _human_reviewed

        # The closed fact-type vocabulary READ FROM ITS OWNER rather than re-typed here — a second
        # copy would let this surface and the projection that writes the flags disagree about what a
        # table fact is.
        from featuregen.overlay.upload.table_fact_projection import _TABLE_FACT_TYPES
        from featuregen.overlay.upload.upload_catalog import table_ref

        out = {t: {ft: {"human": False, "proposed": None} for ft in _TABLE_FACT_TYPES}
               for t in tables}
        keyed: dict[str, tuple[tuple[str, str], str]] = {}
        for source, table in tables:
            ref = table_ref(source, table)
            for fact_type in _TABLE_FACT_TYPES:
                keyed[fact_key(ref, fact_type)] = ((source, table), fact_type)
        streams: dict[str, list[_FactEvent]] = {}
        rows = conn.execute(
            "SELECT aggregate_id, type, event_id, payload, actor FROM events "
            "WHERE aggregate = 'overlay_fact' AND aggregate_id = ANY(%s) "
            "ORDER BY aggregate_id, stream_version", (list(keyed),)).fetchall()
        for aggregate_id, etype, event_id, payload, actor in rows:
            streams.setdefault(aggregate_id, []).append(
                _FactEvent(etype, event_id, payload, identity_from_jsonb(actor)))
        for key, stream in streams.items():
            table_key, fact_type = keyed[key]
            state = fold_overlay_state(stream)
            if state.status in _ENDORSEMENT_NOT_WITHDRAWN:
                out[table_key][fact_type]["human"] = _human_reviewed(
                    stream, state.confirmed_event_id)
            elif state.status == "DRAFT":
                # DRAFT ONLY — the same test `table_fact_governance._build_view` applies to its own
                # queue. REJECTED is a value a human REFUSED, and REVERIFY/STALE are a once-confirmed
                # value that lapsed; re-floating either as "the AI's proposal" would put an answer
                # governance already adjudicated back in front of the model wearing the wrong label.
                out[table_key][fact_type]["proposed"] = _machine_proposal(
                    stream, state.draft_event_id, fact_type)
            # THE REMAINING FOLDS FALL THROUGH ON PURPOSE, leaving `human=False, proposed=None`.
            # REJECTED is the one that matters: its `confirmed_event_id` SURVIVES the fold (the
            # REJECTED branch sets only status and value), so following the stamp blindly would
            # answer `human_confirmed` for a value a person explicitly REFUSED — the strongest claim
            # in the vocabulary attached to the weakest evidence for it. PARTIALLY_CONFIRMED is
            # UNREACHABLE for a table fact rather than safe-by-construction — its one emitter is the
            # dual-owner `approved_join` path — full reasoning on `_ENDORSEMENT_NOT_WITHDRAWN`,
            # which is the table to update if that ever changes. Neither may read `ai_proposed`
            # either: they are not open proposals.
        return out
    except Exception:  # noqa: BLE001 — prompt context must never take down feature generation
        logger.warning("table-fact authority unreadable for %d table(s) — the grain/as-of status "
                       "falls back to source_declared", len(tables), exc_info=True)
        return {}


def _visible(columns: Iterable[str], members: list[dict]) -> list[str]:
    """`columns` if EVERY one of them is a column the caller was actually offered, else ``[]``.

    M6 READ SCOPE, and the one way this task could leak. The confirmed and file-declared values are
    built FROM `members`, which `_candidate_columns` already read-scoped, so their names can only be
    names the caller may see. An AI proposal is the only value arriving from OUTSIDE that set: it
    was validated against the table at PROPOSE time (`table_synth.make_ref_accept` drops a grain
    column the table lacks), but a sensitivity tag added since — or simply a thinner caller — can
    have removed one of its columns from this menu.

    ALL OR NOTHING. A compound grain half-emitted is a different table, and a name the model was not
    offered is also a reference `_ground_refs` cannot resolve, so the feature resting on it would be
    discarded UNGROUNDED after the call rather than never proposed.
    """
    wanted = list(columns)
    offered = {m["column"] for m in members}
    return wanted if wanted and all(c in offered for c in wanted) else []


def _grain_block(members: list[dict], authority: Mapping | None) -> dict:
    """The grain half of one table block: ``{}``, or ``grain_columns`` + ``grain_status``.

    PRECEDENCE, strongest assertion first, and NEVER two values for one field. A confirmed grain
    wins over a file declaration, which wins over the AI's proposal; the loser is not mentioned at
    all. If a human confirmed something the AI disagrees with, the human's value is what ships —
    the model is choosing FEATURES, not adjudicating governance, and a block carrying both would
    invite it to pick.

    A CONFIRMATION IS NEVER WIDENED BY A DECLARATION (Task 8). `project_table_facts_for_ref` SPARES
    file-declared columns from its clear, so a table whose file declares (a, b) while the governed
    grain is (a) genuinely carries `is_grain` on both, one stamped and one not. Emitting the UNION
    would assert a grain nobody attested — not the file's and not the fact's.
    """
    confirmed = sorted(m["column"] for m in members if m["is_grain"] and m["grain_fact_event_id"])
    declared = sorted(m["column"] for m in members if m["is_grain"])
    if confirmed:
        return {"grain_columns": confirmed,
                "grain_status": (STATUS_HUMAN_CONFIRMED if (authority or {}).get("human")
                                 else STATUS_SOURCE_DECLARED)}
    if declared:
        # Task 8's `declared`: the file's flag survived a drift-STALEd / expired / never-projected
        # fact. It collapses onto source_declared because the axis is WHO ASSERTED the value, and
        # the answer is the same source either way. Whether the fact is currently SERVABLE is an
        # execution question, answered on the execution path from the fact stream, never from here.
        return {"grain_columns": declared, "grain_status": STATUS_SOURCE_DECLARED}
    # Sorted HERE, not only in the resolver that normally supplies it: the confirmed and declared
    # sets above are sorted at this same emit site, and a compound grain must read identically
    # whichever branch produced it — two spellings of one grain are two tables to a reader.
    proposed = _visible(sorted(((authority or {}).get("proposed")) or ()), members)
    return ({"grain_columns": proposed, "grain_status": STATUS_AI_PROPOSED} if proposed else {})


def _as_of_block(members: list[dict], authority: Mapping | None) -> dict:
    """The time half, under the same precedence. The ordering makes it bite: a plain "first as-of
    column" pick would hand the model an UNCONFIRMED `a_ts` over a confirmed `z_ts` and call it the
    anchor. The governed availability fact names exactly ONE column, so the confirmed branch is at
    most one candidate anyway."""
    by_name = sorted(members, key=lambda m: m["column"])
    confirmed = next((m["column"] for m in by_name
                      if m["is_as_of"] and m["availability_fact_event_id"]), None)
    if confirmed:
        return {"as_of_column": confirmed,
                "as_of_status": (STATUS_HUMAN_CONFIRMED if (authority or {}).get("human")
                                 else STATUS_SOURCE_DECLARED)}
    declared = next((m["column"] for m in by_name if m["is_as_of"]), None)
    if declared:
        return {"as_of_column": declared, "as_of_status": STATUS_SOURCE_DECLARED}
    proposed = (authority or {}).get("proposed")
    visible = _visible([proposed] if isinstance(proposed, str) else (), members)
    return ({"as_of_column": visible[0], "as_of_status": STATUS_AI_PROPOSED} if visible else {})


def _table_context(cols: list[dict], *,
                   narratives: Mapping[str, Mapping] | None = None,
                   authority: Mapping[tuple[str, str], Mapping] | None = None) -> list[dict]:
    """One context block per TABLE, assembled ONLY from the already-authorized candidate rows
    (spec §5): a table whose columns were all read-scope-excluded has no rows here and gets no
    block. Grain and as-of are emitted whether a human endorsed them, the uploaded file declared
    them, or the AI merely proposed them — each with a `grain_status` / `as_of_status` naming WHICH
    (`TABLE_FACT_STATUSES`), so the model can weigh it. Omitting an unconfirmed grain made the model
    invent one; primary_entity is ADVISORY.

    THE STATUS IS A LABEL, NOT PERMISSION. It admits nothing to the execution path: a feature that
    runs against the warehouse still requires a VERIFIED fact, and the guards that enforce that
    (`resolve_fact`, `read_governed_grain`, `semantic_bindings/projection.py`, `materialize/spine`)
    read the fact stream — none of them reads this block. Task 8b did not weaken `resolve_fact`; it
    reads the PROPOSED state BESIDE the resolved one and labels the difference.

    `authority` is `{(catalog_source, table): {fact_type: {"human", "proposed"}}}` from
    `_table_fact_authority`, RESOLVED BY THE CALLER in one query before the budget loop — this
    function is called once per optional column as that loop searches for a fit, so a read in here
    would be an N+1 on the hot path. Passing nothing is the honest degraded mode, not an error: the
    block then falls back to the WEAKER claim (`source_declared` for a confirmed fact, and no AI
    proposal at all), never to the flattering one. That is the same direction `_human_reviewed`
    itself fails in — unknown is not human.

    `narratives` (Task 7b) is `{catalog_source: catalog_narrative_block}` — the prose a human typed
    about the CATALOG, which the upload form promises is "used by the AI when it interprets tables"
    and which reached no model-facing payload at all. RESOLVED BY THE CALLER, and keyed by catalog
    because an entity-scoped gather legitimately spans several: `select_relevant_context` resolves
    it once per catalog rather than once per table. Passing nothing means the caller resolved no
    narrative, and the block is then byte-identical to its pre-Task-7b shape — which is also what a
    catalog with no authored narrative gets, honestly, with `catalog_profile_absent` already saying
    so in the column payload's missing-context codes.

    It rides the TABLE block and not the column payload on purpose: it is one fact about the whole
    catalog, and a 4000-character description repeated across 144 columns would crowd out the column
    context it exists to interpret."""
    by_table: dict[tuple[str, str], list[dict]] = {}
    for c in cols:
        by_table.setdefault((c["catalog_source"], c["table"]), []).append(c)
    blocks: list[dict] = []
    for (_catalog, table), members in sorted(by_table.items()):
        block: dict = {"table": table}
        block.update((narratives or {}).get(_catalog) or {})
        tdef = next((m["table_definition"] for m in members if m.get("table_definition")), None)
        if tdef:
            block["table_definition"] = tdef
        # Grain and as-of reach the model whether a human endorsed them, the file declared them, or
        # only the AI proposed them — an unconfirmed grain is better information than a missing one,
        # PROVIDED the model is told which it is. The precedence rule and the read-scope guard live
        # in `_grain_block` / `_as_of_block`; the two axes are labelled INDEPENDENTLY, because a
        # table can carry a human-endorsed grain and only a machine's guess at its time anchor.
        table_authority = (authority or {}).get((_catalog, table)) or {}
        block.update(_grain_block(members, table_authority.get("grain")))
        block.update(_as_of_block(members, table_authority.get("availability_time")))
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
    # A SNAPSHOT table that also carries an as-of column is the mismatch worth naming: the time
    # column reads like an event stream and the storage model says it is not one.
    #
    # TRACKS WHAT THE BLOCK EMITS (Task 8), which is why the confirmation test that used to be here
    # is gone. `_table_context` now emits a merely-DECLARED as-of column too, and suppressing the
    # warning for exactly those tables would silence it where the anchor is least examined. The
    # sentence is true of the storage model, not of the confirmation: a snapshot's time column marks
    # when the snapshot was taken whether or not a human has signed for it.
    if out.get("data_role") == "snapshot_fact" and any(m["is_as_of"] for m in members):
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
#   v4 mandatory bytes, 237 columns:  268_902   (~1_135 bytes/column)
#   v4 with every trimmable field shed: 223_930   (~945 bytes/column)
#
# ...and the SATURATED shape — every field the branch added actually populated, which the fixture
# above does NOT do (see below):
#
#   v4 saturated, 237 columns:          405_863   (~1_712 bytes/column)
#   v4 saturated, fully shed:           360_891   (~1_523 bytes/column)
#
# THESE NUMBERS ARE PINNED, NOT DESCRIBED: `test_the_floor_rose_by_exactly_what_the_payload_rose_by`
# asserts `(floor, untrimmed) == (223_930, 268_902)` and
# `test_the_SATURATED_catalog_is_measured_and_still_clears_the_budget` asserts the saturated pair,
# so this comment cannot drift from the measurement without a red test. Read them there, not here,
# if the two ever disagree.
#
# WHY TWO PAIRS. `wide_catalogs` leaves 8 of the 11 fields this branch added at exactly 0, so the
# first pair measures a catalog thinner than any real one. That was not theoretical: a new
# `proposed_authority` subkey grew the payload and the pinned test passed COMPLETELY UNCHANGED,
# because the field it sits beside was empty in the fixture. `saturated_catalogs` populates each
# field through its real writer and costs ~51% more. Two fields stay structurally low and that is
# a product fact, not a gap: adjudication (`confidence_band`/`concept_alternatives`) is capped at
# `adjudication_bounds().max_provider_calls` = 12 columns per run because it is the EXCEPTION path,
# and `relationships` needs cross-catalog link rows the fixture does not stand up.
#
# The v4 figure has been RE-MEASURED four times (248_601 when v4 landed -> 241_491 on 2026-08-06 ->
# 250_982 with the Task-6 axes -> 259_405 with Task 7b's curated vocabulary -> 268_902 when
# `fibo_path` finally reached the payload, migration 1058). Twice the recorded number had quietly
# stopped being true while staying inside the test's tolerance band — which is exactly how a
# "measured" number stops being one, and why the pins above exist.
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
# sends ~1.5x the v3 prompt bytes for the same 237 columns (268_902 vs 175_520), and a mid-size
# catalog that previously refused now succeeds at ~4x the bytes it used to attempt. Input tokens are
# the cheaper half of a call and this is metadata, not data — but "cheaper" is not "free", and the
# number belongs beside the constant that produces it rather than in a review nobody re-reads.
# `FEATUREGEN_FEATURE_CONTEXT_VERSION=3` is the lever that takes it back (the D8 ladder above).
#
# ── ZERO-TRUNCATION RAISE, 300_000 -> 1_500_000 (2026-08-06) ─────────────────────────────────────
#
# STATED HONESTLY, because the measurement did not say what the change expected it to say: raising
# every prose and item cap did NOT move these numbers. The assembled `definition` is read straight
# out of `graph_node` (see `_candidate_columns`' query), so it never passes through
# `enrich_llm.MAX_DEFINITION_LEN` at all — that cap governs the ENRICHMENT egress path, not this
# assembly. v4 measured 241_491 both before and after the raise (it has since moved to 268_902 for
# reasons unrelated to the caps — the Task-6 axes, Task 7b's vocabulary and `fibo_path`; see the
# ladder above).
#
# The raise is still the right change, for a reason that is about CATALOG SIZE rather than per-value
# length. At ~1_135 bytes/column, 300_000 bytes is roughly 264 columns — so a catalog past ~265
# mandatory columns starts shedding prose through `_V4_TRIM_ORDER` (definition first) and, past the
# trimmed floor, refuses outright. That trim IS truncation on the feature-generation path, arriving
# by a different door than the caps.
#
# At 1_500_000 the two rungs are, DERIVED from the pinned per-column rates above:
#   first shed  ~1_322 mandatory columns  (1_500_000 / ~1_135 B per column, untrimmed)
#   refusal     ~1_588 mandatory columns  (1_500_000 /   ~945 B per column, fully shed)
# On the SATURATED rate those rungs come in to ~876 and ~985 columns — still unreachable, and the
# honest pair to quote for a richly-enriched catalog.
# A 144-column catalog therefore assembles ~163_000 B sparse / ~247_000 B saturated (144 x the
# rates above, DERIVED — not measured; the pinned fixtures are the 237-column pairs, and no
# 144-column shape is measured anywhere in this repo). NEITHER RUNG IS REACHABLE ON ANY REALISTIC CATALOG, which is the honest
# reading of this constant: it is a runaway backstop, not a working constraint. Both rungs assume
# the mandatory set scales linearly at the measured per-column rate; a catalog with markedly longer
# prose per column reaches them sooner. (An earlier revision of this paragraph put the first shed at
# ~1_470 columns and quoted a 203_629 floor — that floor matched no rung of any ladder and was never
# true. Both numbers understated cost while overstating headroom.)
#
# The cost is the same cost as before, five times over: nothing downstream bounds the prompt, so on
# a catalog large enough to use this headroom the request is correspondingly larger. It buys the
# absence of silent shedding, not free context.
FEATURE_CONTEXT_BYTE_BUDGET = 1_500_000

_TOKEN_RE = re.compile(r"[a-z0-9]+")


class ContextTooLarge(Exception):
    """The mandatory feature-context set alone exceeds the single-call byte budget — surfaced as
    RejectCode.CONTEXT_TOO_LARGE. We do NOT chunk: one audited_structured_call is one audited
    llm_call, so chunking would need N calls + cross-chunk dedup and defeat the single fail-open
    audit; relevance ordering already floats the highest-relevance items into the one bounded call
    ([F13])."""


def _tokenize(text: str | None) -> set[str]:
    return set(_TOKEN_RE.findall((text or "").lower()))


def _objective_source(objective: str | None, entity: str | None, scope) -> str:
    """The TEXT the objective tokens come from, by source priority (spec §6): the GOVERNED confirmed
    scope (leaf ids + target_entity + modelling_contexts) when present and not unscoped; else the
    DIRECT-ASSIST objective free-text + explicit entity; else the LEXICAL objective alone.

    ONE function decides the priority, and both the literal tokenisation and the Task-6d expansion
    read it. That is the whole reason it exists: expanding the raw `objective` instead would put the
    free text back into the governed route through the side door — a governed scope of
    `retail_churn` with a stale `"weather forecast"` objective would start matching weather columns
    via LLM-derived synonyms, which is precisely the substitution the source priority forbids
    (`test_tokenize_and_objective_source_priority` pins the literal half of that rule)."""
    if scope is not None and not scope.unscoped:
        parts = [
            *([scope.primary] if scope.primary else []),
            *scope.secondary,
            scope.target_entity,
            *scope.modelling_contexts,
        ]
    else:
        parts = [objective, entity]
    return " ".join(p for p in parts if p)


def _literal_tokens(objective: str | None, entity: str | None, scope) -> set[str]:
    """Today's tokenisation, byte-for-byte: the tokens of `_objective_source`. NO LLM call."""
    return _tokenize(_objective_source(objective, entity, scope))


#: The replay identity of one expansion. Bump to retire every stored expansion explicitly — a
#: reworded instruction, a different accept gate, or a changed subject derivation all reach the
#: model with a DIFFERENT question, and replaying a v1 answer against a v2 question is the trap
#: `CONCEPT_CRITIC_VERSION` documents. The version is the ONLY lever: the prompt text is
#: deliberately NOT hashed, so an editorial re-wording is a decision somebody makes here.
OBJECTIVE_EXPANSION_VERSION = 1
OBJECTIVE_EXPANSION_RESULT_TYPE = "objective_expansion"
OBJECTIVE_EXPANSION_TASK = "overlay.feature.objective_expansion"
OBJECTIVE_EXPANSION_PROMPT_ID = "objective_expansion_v1"
OBJECTIVE_EXPANSION_SCHEMA_ID = "objective_expansion"

#: BOTH bounds are CODE-ONLY, and the expansion schema carries NEITHER — see the long note beside
#: `_SCHEMAS[("objective_expansion", 1)]`. Short version: `maxItems` is repo-wide forbidden (the
#: provider 400s on it), and a `maxLength` there is stripped from the wire yet validated on the
#: response, so it would fire before this gate and let ONE long term destroy the whole expansion
#: — the resolution `feature_ideas`' grounding array already reached. Over-bound terms are
#: DROPPED here, one at a time. `test_enrich_output_bounds.py` pins the schema's silence.
_MAX_EXPANSION_TERMS = 40
_MAX_EXPANSION_TERM_LEN = 64
#: What we SEND. The objective is user-typed and otherwise unbounded on this seam (the per-item
#: `_MAX_LEN_BY_KEY` gate governs the BATCH path, not a single call's metadata), so it is bounded
#: here — and bounded BEFORE the cache key is computed, so the key always names exactly the bytes
#: that egressed.
_MAX_EXPANSION_SUBJECT_LEN = 2_000

_EXPANSION_INSTRUCTION = (
    "You are widening a search over a data catalog's COLUMN METADATA. Given the analyst's "
    "objective below, return the related business terms that a bank's catalog might use for the "
    "same ideas — synonyms, standard industry vocabulary, and the words another house style would "
    "use for the same thing (for example 'obligor' for 'counterparty', 'facility' for 'credit "
    "line'). Return single words or short noun phrases only: no sentences, no explanations, no "
    "column names, and nothing invented about this particular catalog. Repeating the objective's "
    "own words is wasted — they are already searched for."
)


def _expansion_input_hash(subject: str) -> str:
    """THE CACHE KEY. Content-addressed on the expansion version and the NORMALIZED subject text,
    and on nothing else.

    What is deliberately absent is the point: no catalog, no catalog revision, no role scope, no
    entity beyond what the subject already carries. The expansion is about the QUESTION, not the
    corpus — "counterparty" relates to "obligor" whichever catalog is loaded — so folding a catalog
    identity in would mint a fresh provider call per upload for an answer that cannot vary. The
    same question is the same answer, forever, until the version moves."""
    return canonical_hash({
        "expansion_version": OBJECTIVE_EXPANSION_VERSION,
        "subject": subject,
    })


def _expansion_subject(objective: str | None, entity: str | None, scope) -> str:
    """The bounded, normalized text we both KEY on and SEND. Normalizing (whitespace-collapsed,
    lower-cased) before hashing is what makes "Total Counterparty Exposure" and "total counterparty
    exposure" one cached question rather than two: the terms are consumed through `_tokenize`,
    which lower-cases anyway, so the two can never want different answers."""
    return " ".join(_objective_source(objective, entity, scope).split()).lower()[
        :_MAX_EXPANSION_SUBJECT_LEN]


def _accept_expansion_terms(raw: object) -> tuple[str, ...]:
    """The CODE-side gate on the model's answer: strings only, non-blank, within the per-term
    length bound, case-insensitively de-duplicated, capped. Anything else is dropped ENTRY-wise —
    a malformed list costs the terms it malformed, never the expansion."""
    if not isinstance(raw, list):
        return ()
    out: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        term = item.strip()
        if not term or len(term) > _MAX_EXPANSION_TERM_LEN or term.lower() in seen:
            continue
        seen.add(term.lower())
        out.append(term)
        if len(out) >= _MAX_EXPANSION_TERMS:
            break
    return tuple(out)


def _expand_objective(conn, client: LLMClient, subject: str, *,
                      actor: IdentityEnvelope | None = None) -> tuple[str, ...]:
    """One tiny audited call returning the business vocabulary related to `subject`, replayed from
    the content-addressed 1039 store for a question already asked.

    REPLAY FIRST, ALWAYS: `find_structured_result` is consulted before any dispatch, so the second
    identical objective costs nothing. A VALIDATED answer is stored even when it is EMPTY — "this
    question has no useful expansion" is an answer, and not storing it would re-bill that question
    on every request forever, which is the cache-shaped-thing failure mode.

    `actor` is the HUMAN subject the route threaded in, exactly as `_call_raw` takes it. It is NOT
    optional in spirit: this call's payload is a BARE USER SENTENCE, so it is the one row in the
    whole feature-generation flow that most needs to name who typed it. Absent, the seam falls back
    to the unauthenticated service identity, which would attribute a human's question to
    `featuregen-overlay-enrichment` while the three sibling calls carrying the SAME sentence name
    the human.

    Nothing here is caught: the ONE containment site is `_objective_tokens`, so every failure —
    provider throw, egress block, repair exhaustion, store fault — lands on the same fallback."""
    input_hash = _expansion_input_hash(subject)
    stored = find_structured_result(
        conn, result_type=OBJECTIVE_EXPANSION_RESULT_TYPE,
        result_version=OBJECTIVE_EXPANSION_VERSION, input_content_hash=input_hash)
    if stored is not None:
        return _accept_expansion_terms(dict(stored.output).get("terms"))
    call = drive_audited_structured_call(
        conn, client, task=OBJECTIVE_EXPANSION_TASK, prompt_id=OBJECTIVE_EXPANSION_PROMPT_ID,
        schema_id=OBJECTIVE_EXPANSION_SCHEMA_ID,
        # `objective` is an ALREADY-CLASSIFIED key on the enrichment seam
        # (`enrich_llm._ROUNDTRIP_PROSE_KEYS`) — the same grade the generation call's own
        # `objective` rides under — so this adds NO new key to the egress surface and cannot trip
        # the fail-closed classifier. On the governed route the subject is platform-derived scope
        # vocabulary, which is strictly safer than the human text the class was written for.
        catalog_metadata={"objective": subject},
        instruction=_EXPANSION_INSTRUCTION, actor=actor)
    if call.output is None:
        # Egress block, provider failure, or a response that failed repair. The call is audited;
        # NOTHING is cached, so a transient fault does not freeze an empty expansion in place.
        return ()
    terms = _accept_expansion_terms(call.output.get("terms"))
    if call.llm_call_ref:
        record_structured_result(
            conn, result_type=OBJECTIVE_EXPANSION_RESULT_TYPE,
            result_version=OBJECTIVE_EXPANSION_VERSION, input_content_hash=input_hash,
            output={"terms": list(terms)},
            producer_kind="llm_call", producer_ref=call.llm_call_ref,
            authority={"authority": "llm_advisory", "subject_hash": input_hash})
    return terms


def _objective_tokens(objective: str | None, entity: str | None, scope, *,
                      conn=None, client: LLMClient | None = None,
                      actor: IdentityEnvelope | None = None) -> set[str]:
    """The objective's own words, plus LLM-derived related business terms.

    ADVISORY and additive: the literal tokens are always retained, so expansion can only widen the
    candidate set, never narrow it. `client=None` (every pure caller, and any degraded deployment)
    returns exactly today's literal tokenisation — byte-for-byte unchanged.

    WHAT CONSUMES THIS: `select_relevant_context`'s ranking key,
    `-len(_column_tokens(c) & obj_tokens)`, and nothing else. The tokens never reach the model, are
    never persisted against a column, and cannot promote a column past the MANDATORY set or past
    the byte budget — a wrong expansion re-orders the optional tail of one menu and costs nothing
    beyond that. That bounded blast radius is why a failed expansion is a shrug rather than an
    error."""
    toks = _literal_tokens(objective, entity, scope)
    subject = _expansion_subject(objective, entity, scope)
    if conn is None or client is None or not subject:
        return toks
    try:
        expanded = _expand_objective(conn, client, subject, actor=actor)
    except Exception:  # noqa: BLE001 — advisory: a failed expansion must never fail the request
        logger.warning("objective expansion failed; falling back to literal tokens", exc_info=True)
        return toks
    derived = {t for term in expanded for t in _tokenize(term)} - toks
    if derived:
        # The reviewer's answer to "why did `obligor` match my question about counterparties?" —
        # the derivation is otherwise invisible, because what the ranking consumes is a flat set.
        # The subject is elided at the same 80 chars `_generate` already elides the objective to.
        logger.info("objective expansion widened [%s] by %d term(s): %s",
                    subject if len(subject) <= 80 else subject[:79] + "…",
                    len(expanded), ", ".join(sorted(derived)))
    return toks | derived


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
    # `business_term` (Task 7b) is the GLOSSARY's curated business NAME — for a bank whose physical
    # columns are `CPTY_EXPSR_AMT`, it is the only readable English the column has, and without it
    # an objective phrased in the bank's own vocabulary could not match the bank's own term.
    for k in ("object_ref", "table", "column", "concept", "domain", "semantic_terms", "entity",
              "ai_summary", "business_term"):
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
                            roles: Iterable[str] = (),
                            client: LLMClient | None = None,
                            actor: IdentityEnvelope | None = None,
                            ) -> tuple[list[dict], list[dict], int]:
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
    feature.

    `client` (Task 6d) is OPTIONAL and ADVISORY: given one, the objective is expanded with related
    business terms before the intersection, so a question about "counterparty exposure" can reach a
    column whose vocabulary says "obligor". Omitted (every pure caller, and any degraded
    deployment) the ranking is byte-for-byte today's literal token intersection. `actor` rides with
    it for the same reason `_call_raw` takes one — that expansion call egresses the human's own
    sentence, so the immutable llm_call must name the human and not the service default."""
    if byte_budget is None:
        byte_budget = FEATURE_CONTEXT_BYTE_BUDGET
    obj_tokens = _objective_tokens(objective, entity, scope, conn=conn, client=client, actor=actor)
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

    # The catalog narrative, resolved ONCE per catalog present in the candidate set (Task 7b). Not
    # once per table and not once per column: it is one fact about the whole catalog, and
    # `_table_context` is called repeatedly below as the budget loop searches for a fit.
    narratives = {source: block for source in sorted({c["catalog_source"] for c in cols})
                  if (block := current_catalog_narrative_block(conn, source))}
    # Task 8b, hoisted for exactly the reason above: WHO asserted each table's grain / as-of, read
    # in ONE indexed query over the FULL candidate set before the loop starts, so the loop's
    # repeated `_table_context` calls are pure. Resolving it per call would be an N+1 over tables
    # multiplied by the number of optional columns the budget considers.
    authority = _table_fact_authority(conn, cols)

    def _context(rows: list[dict]) -> list[dict]:
        return _table_context(rows, narratives=narratives, authority=authority)

    mandatory = [c for c in cols if _is_mandatory(c, obj_entity)]
    optional = [c for c in cols if not _is_mandatory(c, obj_entity)]
    scored = sorted(optional,
                    key=lambda c: (-len(_column_tokens(c) & obj_tokens), c["object_ref"]))

    selected = list(mandatory)
    trim = 0
    while (_assembled_bytes(_enriched(selected, trim=trim),
                            _context(selected)) > byte_budget):
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
        if _assembled_bytes(_enriched(trial, trim=trim),
                            _context(trial)) > byte_budget:
            dropped = len(scored) - i
            break
        selected = trial
    if dropped:
        logger.info("feature-context relevance dropped %d of %d optional columns (byte budget %d)",
                    dropped, len(optional), byte_budget)
    return (_enriched(selected, trim=trim), _context(selected),
            dropped)


def _build_menu(conn, cols: list[dict], *, objective: str | None = None,
                entity: str | None = None, scope=None,
                roles: Iterable[str] = (),
                client: LLMClient | None = None,
                actor: IdentityEnvelope | None = None) -> tuple[list[dict], list[dict]]:
    """The menu + per-table context for one generation call. Flag-OFF ⟹ the thin pre-Slice-3 menu
    and NO context (byte-identical). Flag-ON ⟹ the enriched, relevance-selected menu + context
    (may raise ContextTooLarge).

    `roles` is the CALLER's scope, threaded down to the v4 bundle build (D11). The candidate rows
    were already read-scoped; the bundle re-applies the same scope at its own boundary rather than
    inheriting a clearance from a row that passed it earlier.

    `client`/`actor` are the SAME pair every generation call in this module already carries, handed
    on so relevance ranking can widen the question under the caller's own identity (Task 6d).
    Flag-OFF never reaches the ranking at all, so it never expands."""
    if not feature_context_enabled():
        return _menu(cols), []
    columns, table_context, _dropped = select_relevant_context(
        conn, cols, objective=objective, entity=entity, scope=scope, roles=roles, client=client,
        actor=actor)
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


#: What a column can CONTRIBUTE to a feature — a CLOSED vocabulary (Task 6c). Free text here would
#: become a second, ungoverned way of saying what `operand_roles` / `RoleBinding.role` already say,
#: and the six between them cover every way a column enters a calculation. Closed on the WIRE
#: (`x-wire-enum`) and again in `_ground_notes`; deliberately NOT closed on the response schema,
#: where an off-vocabulary answer would fail the whole call instead of costing one entry.
GROUNDING_ROLES: tuple[str, ...] = ("measure", "grain", "time_anchor", "filter", "currency",
                                    "dimension")

#: The model's evidence clause is DISPLAY TEXT on a review card, so it is bounded like one — and
#: the bound lives HERE, not in the response schema, because `maxLength` is stripped from the wire
#: (the model never learns it) yet still validated against the response: one long clause would fail
#: the whole call. Truncating, not refusing: an explanation that ran long must never cost a feature.
_MAX_GROUNDING_WHY_LEN = 200
#: And a ceiling on how many entries ride back, so an unbounded array cannot become an unbounded
#: response. Applied AFTER every entry is validated (see `_ground_notes`).
_MAX_GROUNDING_ENTRIES = 32


@dataclass(frozen=True, slots=True)
class GroundingNote:
    """ONE column the generator says it used, what it contributed, and the evidence it named.

    EXPLANATORY, NEVER AUTHORITY. Nothing in the gauntlet, the USE gate, the tri-state or any
    downstream disposition reads these values — a feature carrying a confident-sounding note is
    neither more trusted nor more complete than the identical feature carrying none
    (`test_grounding_changes_no_disposition` pins it). Its whole job is to let a reviewer see WHY a
    feature was proposed instead of guessing, and to let the operator tell whether the widened
    semantic context is being READ rather than merely delivered.

    `column` is a RESOLVED catalog object_ref, never the model's raw string: an entry that could not
    be resolved against the offered candidate set discarded the whole proposal before this object
    was built (`_ground_notes`), so nothing here can be a channel for an ungrounded ref.
    """
    column: str
    role: str                       # in GROUNDING_ROLES
    why: str

    def to_json(self) -> dict:
        return {"column": self.column, "role": self.role, "why": self.why}


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
    # ── Task 3 (router-quality plan): the near-label critic's ADVISORY verdict on this candidate.
    #    Closed vocabulary {no_finding | too_close | abstain} — deliberately no token that reads as
    #    "cleared", because this critic must be INCAPABLE of clearing anything (LLM output never
    #    clears a design check). None = the critic did not run (flag off / pre-Task-3 snapshot) —
    #    honest absence, rendered as today's warning chip. FLAG-ONLY: nothing reads these to remove
    #    a candidate; they annotate the card and the considered set.
    near_label_verdict: str | None = None
    near_label_rationale: str = ""
    # ── Task 4b: the recipe's UNTAKEN parameterisations, named on the card per the emission
    #    policy ("also available: 30/180-day windows") — one bounded human-readable line, "" when
    #    the recipe has no multi-value params. PRESENTATION ONLY: identity rides the bound params
    #    (name + semantic_parameter_binding_hash), never this string.
    param_alternatives: str = ""
    # ── Task 2A: the DECISION TRACE this candidate's validation produced (freeze 0F-7 P1) ──
    #    Defaulted last and never serialized: it is TRANSIENT CARRY from the gauntlet to the
    #    projection built in the same call. A persisted-and-reloaded idea has None here, which is
    #    correct — V2 assembly always consumes FRESH grounding output, never a reloaded snapshot,
    #    so a trace that survived a round trip could only ever describe a decision made elsewhere.
    #    None also on every path that threads no candidate identity (LLM / planner / confirm-time
    #    revalidation). Nothing in the V1 payload reads it.
    grounding_trace: GroundingDecisionTraceV1 | None = None
    # ── Task 6c: the GENERATOR's own account of what it used, per proposed feature ──
    #    Each note is one offered column, the role it played, and the evidence the model named.
    #    EXPLANATORY, NEVER AUTHORITY: no check, requirement, status, dedup signature or gate reads
    #    it, and a feature is not more trusted for carrying a confident one. Empty for every
    #    non-LLM path (recipes and the planner declare `operand_roles` instead), for every contract
    #    version below `_GROUNDING_SCHEMA_VERSION` (the wire item has no such key), and for a model
    #    that simply did not answer — all three are honest absence, never a refusal.
    #
    #    NOT the same axis as `grounding_trace`, which sits above it: that is the PLATFORM's
    #    verifiable record of what the gauntlet read; this is the MODEL's unverifiable claim about
    #    what it reasoned from. Only the columns are checked — they must exist in the offered
    #    candidate set — and nothing checks the claim itself, which is why nothing may rest on it.
    grounding: tuple[GroundingNote, ...] = ()


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


def _by_bare_column(known: set[str]) -> dict[str, str | None]:
    """Bare column name -> the ONE object_ref ending in it, or ``None`` when several do (AMBIGUOUS).

    ONE home for the suffix-resolution rule. `_ground_refs` and `_ground_notes` must agree about
    what a bare name means — a second copy of an identity-bearing rule is a rule that drifts — and
    the two need DIFFERENT answers from it (a dropped ref vs a dropped note), which they can only
    give from the same map. The ``None`` marker is what makes "ambiguous" distinguishable from
    "unknown"; collapsing them is what made an ambiguous name cost a whole feature.
    """
    by_col: dict[str, str | None] = {}
    for ref in known:
        col = ref.rsplit(".", 1)[-1]
        by_col[col] = None if col in by_col else ref   # 2nd occurrence -> None marks it AMBIGUOUS
    return by_col


def _ground_refs(raw_refs: object, known: set[str]) -> list[str]:
    """Resolve each LLM-proposed ``derives_from`` entry to a real catalog ``object_ref``. Exact match
    first; else a UNIQUE bare-column-name / suffix match, so a model that emits ``actual_tran_amt``
    (or ``public.t.actual_tran_amt`` verbatim) both ground to the same object_ref — the model's
    reference FORMAT must not silently un-ground an otherwise-valid feature. Ambiguous column names
    (same name in >1 table) and unknown refs are dropped. Order-preserving + de-duplicated."""
    by_col = _by_bare_column(known)
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


def _ground_notes(raw_grounding: object,
                  known: set[str]) -> tuple[tuple[GroundingNote, ...], str | None]:
    """Resolve the model's `grounding` array. Returns (notes, unresolved_column).

    EXACTLY ONE failure costs the whole feature, and the CALLER applies it: a `column` that names
    NOTHING in the offered candidate set. That is a false claim about the CATALOG — the account the
    feature gives of itself is fabricated — and left in place the array would be a second channel
    for ungrounded refs to re-enter beside the ones `_ground_refs` filters out of `derives_from`.

    EVERY other malformation costs THAT ENTRY and nothing else, because none of them is a claim
    about the catalog that is false:

      * an AMBIGUOUS bare name (the same column name in >1 table) — and the payload invites exactly
        this: `_table_context` presents grain and as-of columns as BARE NAMES, and the grounding
        directive asks the model to account for them. A model that copies the name it was SHOWN,
        for a column that really was offered, must not lose its feature over the spelling. Note the
        asymmetry that would otherwise exist: for `derives_from` the identical ambiguity drops one
        ref and the feature survives, so the EXPLANATION would have been strictly harsher than the
        thing it explains.
      * a MISSING or non-string `column` — an incomplete entry. `column` is wire-required and
        response-OPTIONAL by deliberate design (a canonical `required` would fail the whole call);
        treating the omission the schema tolerates as a fabrication would contradict that in the
        same breath. `None` in particular must never be stringified into a column literally named
        "None" and reported to a human as an invention.
      * an OFF-VOCABULARY ROLE — a limit of OUR taxonomy, not a fabrication.

    Resolution is `_ground_refs`' — exact ref, else a unique bare-name/suffix match, off the SAME
    `_by_bare_column` map — so the model's reference FORMAT cannot un-ground a feature, and the
    emitted `column` is the resolved object_ref so a reviewer follows a real catalog ref rather than
    the model's string. An ABSENT or empty array is honest absence: no notes, no rejection, no
    disposition changed.
    """
    if not isinstance(raw_grounding, list):
        # Absent (the normal case at v4 and below) or a shape we cannot read. Neither is a refusal:
        # `grounding` is optional on the response precisely so a model that skips it does not turn
        # every such response into a whole-call failure.
        return (), None
    by_col = _by_bare_column(known)
    notes: list[GroundingNote] = []
    for entry in raw_grounding:
        if not isinstance(entry, dict):
            continue
        column = entry.get("column")
        column = column.strip() if isinstance(column, str) else ""
        if not column:
            continue                                  # incomplete — dropped, never a fabrication
        tail = column.rsplit(".", 1)[-1]
        if column in known:
            resolved: str | None = column
        elif tail in by_col:
            resolved = by_col[tail]                   # None ⟹ the bare name is AMBIGUOUS
        else:
            return (), column                         # names NOTHING — the one fatal case
        if resolved is None:
            logger.info("dropping an ambiguous grounding column %r", column[:80])
            continue
        role = str(entry.get("role", "")).strip().lower()
        if role not in GROUNDING_ROLES:
            logger.info("dropping a grounding entry with an off-vocabulary role %r", role[:40])
            continue
        notes.append(GroundingNote(column=resolved, role=role,
                                   why=str(entry.get("why", ""))[:_MAX_GROUNDING_WHY_LEN]))
    # Capped LAST, so a fabricated column past the cap is still refused rather than trimmed away.
    return tuple(notes[:_MAX_GROUNDING_ENTRIES]), None


# The AI's suggestion is DISPLAY TEXT on a review card, so it is bounded like one. T2's drafter
# already caps a drafted unit at 64 chars (`enrich._MAX_UNIT_LEN`); this is the independent
# read-side ceiling (a suggestion from any future writer can never turn a requirement into an
# unbounded payload).
#
# ZERO-TRUNCATION RAISE (2026-08-06): 64 -> 256. Unlike the accept gates above, this one TRUNCATES
# an already-ACCEPTED value on its way out — so at 64 it was exactly co-located with the drafter's
# old bound and any future writer with a longer legitimate value would have been silently clipped
# on the card rather than refused at the door. It stays deliberately ABOVE `_MAX_UNIT_LEN` so the
# display ceiling can never be the thing that cuts a value the drafter accepted.
_MAX_SUGGESTION_LEN = 256


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
    # Task 6c — the model's own account of what it used, checked against the SAME offered candidate
    # set the returned refs are, and here beside them so the two grounding rules read as one. The
    # notes themselves are NOT pinned into the decision trace and NOT read by anything below: the
    # trace records what the PLATFORM verified, and an unverifiable model claim in it would be
    # indistinguishable from a verified dependency.
    notes, unresolved = _ground_notes(raw.get("grounding"), known)
    if unresolved is not None:
        return _reject(RejectCode.UNKNOWN_GROUNDING_COLUMN,
                       f"grounding names a column that does not exist: {unresolved[:80]}")
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

    # ── the USE gate (Bar 4). Sensitivity decided who may SEE these operands; this decides whether
    #    the feature may be BUILT from them. Placed with the other hard rejects, AFTER leakage and
    #    freshness — a leaky or stale candidate is refused for the reason it has always been
    #    refused, so no existing rejection changes code — and BEFORE any requirement is minted,
    #    because a refused feature must never reach the tri-state at all. The refusal rides
    #    `_reject` so a use-gate refusal carries the decision trace like every other refusal. ──
    use = _use_gate(conn, pairs, meta, raw.get("aggregation"))
    if use.rejection is not None:
        return _reject(use.rejection.code, use.rejection.message)

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
        # Task 6c: carried, never consulted. `status` and `requirements` above were both decided
        # before this line and neither reads `notes` — the ONE property that makes an explanation
        # safe to accept from a model is that nothing rests on it.
        grounding=notes,
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
                    "feature_ideas", _generation_instruction(objective, table_context=table_context),
                        inputs, actor=actor,
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

# Task 6c. Two things are being asked for that the schema alone cannot ask for. The first is
# COVERAGE — an entry per column used, including the ones that are in the menu because the
# calculation is wrong without them (the confirmed grain, the as-of column) rather than because they
# match the objective. The second is HONESTY ABOUT AUTHORITY: the widened context distinguishes a
# confirmed semantic from one the enrichment merely PROPOSED, and a reader who cannot tell which a
# feature rests on has been given confidence they did not earn. Neither makes the feature more or
# less trusted by the platform — this is for the human.
_GROUNDING_DIRECTIVE = (
    "\n\nFor EVERY feature you propose, return a `grounding` entry per column you used: the column, "
    "its role, and one short clause naming the evidence you relied on. Where a value you relied on "
    "is marked llm/proposed in `semantic_authority`, or a fact carries `proposed_value` rather than "
    "`value`, say so — a feature resting on an unconfirmed semantic is still worth proposing, and "
    "the reader needs to know which one it is.")


def _grounding_directive() -> str:
    """The grounding directive at the versions whose schema can carry the answer, else "".

    Version-gated in BOTH directions, and both directions are the same rule — the prompt and the
    schema must agree about shape:

      * below `_GROUNDING_SCHEMA_VERSION` the wire item is CLOSED with no `grounding` key, so
        asking for one would demand output the schema forbids the model to give;
      * AT it the key is wire-REQUIRED, so the model is compelled to answer — and compelling an
        answer while withholding the rules for it is the same inversion the other way up. That is
        why EVERY call stamping this version appends it, including `refine_idea`, whose human
        instruction otherwise carries no system directive at all.
    """
    return (_GROUNDING_DIRECTIVE
            if _feature_schema_version() >= _GROUNDING_SCHEMA_VERSION else "")


# Task 8b. THE TOKENS MUST BE DEFINED WHERE THEY ARE READ. Task 8's review found `grain_status` /
# `as_of_status` travelling as bare JSON with no prompt text defining them anywhere, so the model
# read the English word and took `confirmed` at face value — which is exactly the confidence nobody
# earned, since ingest auto-confirms an unreviewed CSV flag. A token whose meaning lives only in a
# Python docstring is not labelling.
#
# Every member of `TABLE_FACT_STATUSES` is named here; `test_every_status_token_is_defined_for_the_
# model_in_the_directive` fails if a fourth is ever added without its sentence. Like its two
# siblings above it is a fixed PII-free constant appended after the redacted objective, so the
# egress guard still scans it and the llm_call audit records exactly what was asked.
# EVERY CLAUSE MUST BE TRUE IN EVERY STATE THAT PRODUCES ITS TOKEN, which is a stricter test than
# "true in the normal case" and is where BOTH earlier drafts of this sentence failed. The full
# enumeration lives on `_ENDORSEMENT_NOT_WITHDRAWN`; the drafts and why each was false:
#
#   draft 1: "the uploaded catalog file asserted it, so NOBODY has reviewed it" — false for the
#            FAIL-SOFT state, where a grain a human really did endorse degrades to this token. Not a
#            weaker claim than the truth but a DIFFERENT one: it asserts a fact about a file that
#            never declared anything and denies a review that happened.
#   draft 2: "NO HUMAN REVIEW IS RECORDED for it" — fixed that, and was still false for a REJECTED
#            re-verification, where a human review is emphatically recorded: it is the refusal.
#   draft 3: `human_confirmed` gained "and that sign-off STILL STANDS" — false in the two states the
#            same change newly routed into that token. In REVERIFY/STALE the fold has moved the
#            signed value to `prior_value`, the status is in `_AWAITING_CONFIRMATION`, a re-verify
#            task is open and `resolve_fact` refuses to serve it: the platform can EVIDENCE that the
#            sign-off has lapsed, and "still stands" reads as "still in force". The first over-claim
#            of the three, and the task calls that the dangerous direction. The private definition
#            ("stands" = not withdrawn) lived in the comment below and never reached the model —
#            which is the ORIGINAL finding of this whole task, committed again one level down.
#
# So: `human_confirmed` claims only that the sign-off has NOT BEEN WITHDRAWN — true in all three of
# its producing states, and it keeps the lapse/repudiation distinction `_ENDORSEMENT_NOT_WITHDRAWN` is
# built on. `source_declared` claims that NO SIGN-OFF STANDS — true of never-signed, unreadable and
# withdrawn alike. Neither says anything about whether the fact can currently EXECUTE; that is the
# other axis and `resolve_fact` alone answers it.
_TABLE_CONTEXT_STATUS_DIRECTIVE = (
    "\n\nEach `table_context` block may carry `grain_status` and `as_of_status`. These name what the "
    "platform can EVIDENCE about that table's grain (what one row means) or its as-of column (when "
    "the row was true), and nothing else: `human_confirmed` — a person with authority over the "
    "table reviewed and signed it, and that sign-off has not been withdrawn; `source_declared` — NO "
    "HUMAN SIGN-OFF STANDS for it, which usually means the uploaded catalog file asserted it and "
    "the platform recorded it automatically; `ai_proposed` — a model inferred it from the schema "
    "and it is unconfirmed. Use all three: an unreviewed grain is better information than none, and "
    "a feature resting on one is still worth proposing — but only `human_confirmed` means a person "
    "checked it.")
# The one clause that mentions `grounding`, split out because a model must NEVER be asked for output
# its schema forbids: below `_GROUNDING_SCHEMA_VERSION` the wire item is CLOSED with no `grounding`
# key, and a sentence telling the model to write one there is the same prompt/schema inversion
# `_grounding_directive` exists to avoid. Gated off `_grounding_directive()`'s OWN emptiness rather
# than a second copy of the version test, so the two cannot drift apart.
#
# IT ASKS ABOUT ALL THREE, NOT ONLY THE WEAK ONES. The first version said "where a feature rests on
# a grain that is NOT `human_confirmed`, say so", which made `human_confirmed` the silent default —
# so a reader could not tell a signed grain from one whose status the model simply never mentioned,
# and the review's second-order finding lands here: a signature that has lapsed would vanish from
# the label AND from the audit trail in the same breath. Naming the status whichever it is costs one
# short phrase and makes the absence of a claim mean something again.
_TABLE_CONTEXT_STATUS_GROUNDING_CLAUSE = (
    " Where a feature rests on a table's grain or as-of column, name that column's status in its "
    "`grounding` entry — whichever of the three it is. The reader needs to know what the feature is "
    "standing on, and `human_confirmed` is worth stating explicitly rather than left as the "
    "assumption when nothing is said.")


def _table_context_directive(table_context: Iterable[Mapping] | None, *,
                             cites_grounding: bool) -> str:
    """The status vocabulary, or "" when no block in this payload carries a status.

    Gated on the PAYLOAD, not on a flag, and for the same reason `_grounding_directive` is gated on
    the schema version: the prompt and the payload must agree about what is there. Flag-off sends no
    `table_context` at all, and a catalog where every table abstained sends blocks with no status —
    defining tokens that do not appear is noise the egress guard still has to walk, and it invites
    the model to look for a key it will not find.

    `cites_grounding` says whether THIS CALL'S contract has a `grounding` array to cite the status
    in — TRUE for the two `feature_ideas` paths, FALSE for `feature_recipe`, whose response contract
    is `{grain_table, join_table, derives_from, aggregation, as_of_column}` and which `Recipe` reads
    key by key. Two gates, not one, and both are the same rule stated at two grains: the clause
    rides only where the schema HAS the key (`cites_grounding`) and where that key is live at this
    version (`_grounding_directive`). Asking a recipe call to write a `grounding` entry would spend
    its output on a field nothing reads — the same prompt/schema inversion as demanding output the
    wire item forbids, arriving by the other door.
    """
    if not any(b.get("grain_status") or b.get("as_of_status") for b in (table_context or ())):
        return ""
    return _TABLE_CONTEXT_STATUS_DIRECTIVE + (
        _TABLE_CONTEXT_STATUS_GROUNDING_CLAUSE
        if cites_grounding and _grounding_directive() else "")


def _generation_instruction(objective: str, *,
                            table_context: Iterable[Mapping] | None = None) -> str:
    """The generation instruction: the (already-redacted) objective plus the fixed system
    directives. All are PII-free constants appended AFTER the objective, so the egress guard still
    scans the whole string and the llm_call audit records exactly what was asked.
    """
    return (objective + _DERIVES_FROM_DIRECTIVE + _grounding_directive()
            + _table_context_directive(table_context, cites_grounding=True))


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
            conn, cols, objective=objective, entity=entity, scope=scope, roles=roles,
            client=client, actor=actor)
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
                        "feature_ideas", _generation_instruction(objective, table_context=table_context),
                        inputs, actor=actor,
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
                                          roles=roles, client=client, actor=actor)
    except ContextTooLarge as exc:
        return None, {"name": str(idea.get("name", "")), "reason": str(exc),
                      "code": RejectCode.CONTEXT_TOO_LARGE}
    inputs: dict = {"columns": menu, "fix": fix}
    if table_context:
        inputs["table_context"] = table_context
    if objective:
        inputs["objective"] = objective
    # The grounding directive rides here too — NOT the derives-from one, which this path has never
    # appended. The difference is that the v5 wire item makes `grounding` REQUIRED on this call:
    # the schema compels an answer, so the rules for it must travel with the request rather than
    # leaving a human's revision to be refused for a convention it was never told (see
    # `_grounding_directive`). A fixed PII-free constant appended after the human's instruction, so
    # the egress guard still scans the whole string and the llm_call records exactly what was asked.
    out = _call_raw(conn, client, "overlay.feature.recommend", "feature_recommend_v1",
                    "feature_ideas",
                    instruction + _grounding_directive()
                    + _table_context_directive(table_context, cites_grounding=True),
                    inputs, actor=actor,
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
        menu, table_context = _build_menu(conn, cols, objective=nl_query, roles=roles,
                                          client=client, actor=actor)
    except ContextTooLarge as exc:
        logger.warning("feature-recipe context too large for %r: %s", nl_query, exc)
        return Recipe(intent=nl_query, grain_table=None, derives_from=[], aggregation=None,
                      as_of_column=None)
    recipe_inputs: dict = {"columns": menu}
    if table_context:
        recipe_inputs["table_context"] = table_context
    # THE THIRD SURFACE THAT SENDS THESE TOKENS, and the one the first version of Task 8b missed.
    # This path puts `table_context` in the inputs exactly like the two generation paths, so with
    # the context flag on a recipe request for a table whose grain is `ai_proposed` hands the model
    # a token it has never been told the meaning of — precisely the defect Task 8's review raised,
    # surviving on one surface of three. `Recipe.grain_table` / `as_of_column` then come back to
    # `POST /features/recipe` looking grounded.
    #
    # `cites_grounding=False`: the recipe RESPONSE contract is
    # {grain_table, join_table, derives_from, aggregation, as_of_column}, which `Recipe` reads key
    # by key. There is no `grounding` array here at any version, so telling the model to write one
    # would spend its output on a field nothing reads.
    out = _call_raw(conn, client, "overlay.feature.recipe", "feature_recipe_v1", "feature_recipe",
                    nl_query + _table_context_directive(table_context, cites_grounding=False),
                    recipe_inputs, actor=actor,
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
