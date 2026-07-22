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

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.catalog_changes import drift_watermark
from featuregen.overlay.upload.column_authority import (
    logical_ref_of,
    read_column_facts,
)
from featuregen.overlay.upload.enrich_llm import audited_structured_call
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CATALOG_PROJECTION_UNAVAILABLE,
    CatalogProjectionUnavailable,
)
from featuregen.overlay.upload.join_path import (
    JoinOutcome,
    JoinStep,
    classify_join_path,
    find_join_path,
)
from featuregen.overlay.upload.operational_facts import read_operational_value
from featuregen.overlay.upload.planner.plan_envelope import PlanEnvelopeV1
from featuregen.overlay.upload.read_scope import allowed_sensitivities
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


@dataclass(frozen=True, slots=True)
class Rejection:
    code: str
    message: str

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
           "c.domain, c.definition, c.data_type, c.declared_type, c.semantic_terms, c.entity, "
           "c.additivity, c.unit, c.currency, c.is_grain, c.is_as_of, c.grain_fact_event_id, "
           "c.availability_fact_event_id, t.definition, t.primary_entity "
           "FROM graph_node c "
           "LEFT JOIN graph_node t ON t.catalog_source = c.catalog_source AND t.kind = 'table' "
           "AND t.table_name = c.table_name "
           "WHERE c.kind = 'column' "
           "AND (c.sensitivity IS NULL OR c.sensitivity = ANY(%s))")
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
             "concept": r[4], "domain": r[5], "definition": r[6], "data_type": r[7],
             "declared_type": r[8], "semantic_terms": r[9], "entity": r[10], "additivity": r[11],
             "unit": r[12], "currency": r[13], "is_grain": r[14], "is_as_of": r[15],
             "grain_fact_event_id": r[16], "availability_fact_event_id": r[17],
             "table_definition": r[18], "table_primary_entity": r[19]} for r in rows]


def _menu(cols: list[dict]) -> list[dict]:
    return [{k: c[k] for k in ("object_ref", "table", "column", "concept", "domain")} for c in cols]


FEATURE_CONTEXT_FLAG = "FEATUREGEN_FEATURE_CONTEXT"


def feature_context_enabled() -> bool:
    """The single env gate for the whole Slice-3 enrichment (menu widening, per-table context,
    relevance, versioned shape). Default OFF ⟹ the thin pre-Slice-3 menu, byte-for-byte.
    RF-C3: the ONE public definition — 3a-iv imports and reuses this; never redefine it."""
    return os.environ.get(FEATURE_CONTEXT_FLAG, "").strip().lower() in {"1", "true", "yes", "on"}


def _feature_schema_version() -> int:
    """2 when the feature-context flag is on (the widened INPUT contract egressed), else 1 — so the
    immutable llm_call stamps the real numeric version, not a hardcoded 1 masked by a `…_v1` prompt_id."""
    return 2 if feature_context_enabled() else 1


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
_MENU_DEFINITION_FIELDS = ("definition", "semantic_terms")
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


def _enriched_menu(conn, cols: list[dict]) -> list[dict]:
    """The flag-ON menu (feature_context_enabled()). When the flag is OFF, callers keep serving
    the thin `_menu` projection unchanged — flag-off byte-identity is a Slice-3 invariant."""
    return [_enriched_column(conn, c) for c in cols]


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
        blocks.append(block)
    return blocks


# One hard byte budget on the assembled feature-context batch (spec §6). Referenced at call time so
# tests can monkeypatch it; select_relevant_context reads this module global when byte_budget is None.
FEATURE_CONTEXT_BYTE_BUDGET = 60_000

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
    for k in ("object_ref", "table", "column", "concept", "domain", "semantic_terms", "entity"):
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
                            byte_budget: int | None = None) -> tuple[list[dict], list[dict], int]:
    """Deterministic relevance selection ([F13], spec §6). Returns
    (selected_enriched_columns, table_context, dropped_count). Mandatory columns (confirmed grain,
    as-of, entity-match) are ALWAYS included; the rest are added by descending shared-token score,
    stable (-score, object_ref asc), until the ONE hard byte budget on the assembled batch is
    reached. Raises ContextTooLarge when the mandatory set alone exceeds the budget (do NOT chunk).
    Logs the dropped count."""
    if byte_budget is None:
        byte_budget = FEATURE_CONTEXT_BYTE_BUDGET
    obj_tokens = _objective_tokens(objective, entity, scope)
    obj_entity = _objective_entity(entity, scope)
    enriched_by_ref = {(c["catalog_source"], c["object_ref"]): _enriched_column(conn, c)
                       for c in cols}

    def _enriched(rows: list[dict]) -> list[dict]:
        return [enriched_by_ref[(c["catalog_source"], c["object_ref"])] for c in rows]

    mandatory = [c for c in cols if _is_mandatory(c, obj_entity)]
    optional = [c for c in cols if not _is_mandatory(c, obj_entity)]
    scored = sorted(optional,
                    key=lambda c: (-len(_column_tokens(c) & obj_tokens), c["object_ref"]))

    selected = list(mandatory)
    if _assembled_bytes(_enriched(selected), _table_context(selected)) > byte_budget:
        raise ContextTooLarge(
            f"mandatory feature context ({len(mandatory)} columns) exceeds byte budget "
            f"{byte_budget}; not chunking")
    dropped = 0
    for i, c in enumerate(scored):
        trial = selected + [c]
        if _assembled_bytes(_enriched(trial), _table_context(trial)) > byte_budget:
            dropped = len(scored) - i
            break
        selected = trial
    if dropped:
        logger.info("feature-context relevance dropped %d of %d optional columns (byte budget %d)",
                    dropped, len(optional), byte_budget)
    return _enriched(selected), _table_context(selected), dropped


def _build_menu(conn, cols: list[dict], *, objective: str | None = None,
                entity: str | None = None, scope=None) -> tuple[list[dict], list[dict]]:
    """The menu + per-table context for one generation call. Flag-OFF ⟹ the thin pre-Slice-3 menu
    and NO context (byte-identical). Flag-ON ⟹ the enriched, relevance-selected menu + context
    (may raise ContextTooLarge)."""
    if not feature_context_enabled():
        return _menu(cols), []
    columns, table_context, _dropped = select_relevant_context(
        conn, cols, objective=objective, entity=entity, scope=scope)
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


def _column_meta(conn, pairs: list[tuple[str, str]]) -> dict[str, dict]:
    """Additivity/catalog for each (catalog_source, object_ref) pair — scoped to the EXACT pair, so a
    same-named column in another catalog cannot contaminate the reading (M3), and a fabricated pair is
    simply absent from the result (used for the M4 existence check)."""
    if not pairs:
        return {}
    refs = [ref for _, ref in pairs]
    rows = conn.execute(
        "SELECT catalog_source, object_ref, additivity, unit, currency FROM graph_node "
        "WHERE kind = 'column' AND object_ref = ANY(%s)", (refs,)).fetchall()
    wanted = set(pairs)
    return {ref: {"catalog_source": cs, "additivity": add, "unit": unit, "currency": cur}
            for cs, ref, add, unit, cur in rows if (cs, ref) in wanted}


# Aggregation words that REQUIRE a numeric measure (ratio/mean/sum/…); count/count_distinct do not.
_NUMERIC_OP_WORDS = ("sum", "total", "avg", "average", "mean", "ratio", "rate", "net_",
                     "percent", "pct", "std", "variance", "median")


def _needs_numeric(aggregation: str | None) -> bool:
    a = (aggregation or "").lower()
    return any(w in a for w in _NUMERIC_OP_WORDS)


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


def _validate_idea(conn, raw: dict, known: set[str], src_of: dict[str, set[str]],
                   target_ref: str | None, now: datetime | None, fresh_within: timedelta,
                   *, roles: Iterable[str] = ()):
    """The deterministic TRI-STATE gauntlet (spec §2). Returns (FeatureIdea, None) for DESIGN_CHECKED
    or NEEDS_EXTERNAL_VALIDATION — the returned idea carries validation_status + typed requirements +
    resolved operands — or (None, Rejection) for REJECTED (deterministically invalid / unauthorized).
    `roles` gates cross-table join authority (a read-scope-DENIED hop rejects). `src_of` maps
    object_ref -> the candidate catalog source(s), used to resolve each derive's catalog (B3)."""
    derives = _ground_refs(raw.get("derives_from", []), known)
    if not derives:
        return None, Rejection(RejectCode.UNGROUNDED, "ungrounded")
    pairs: list[tuple[str, str]] = []
    for d in derives:
        srcs = src_of.get(d, set())
        if len(srcs) != 1:
            return None, Rejection(RejectCode.AMBIGUOUS_CATALOG, f"ambiguous catalog for {d}")
        pairs.append((next(iter(srcs)), d))
    meta = _column_meta(conn, pairs)
    for src, d in pairs:
        if d not in meta or meta[d]["catalog_source"] != src:
            return None, Rejection(RejectCode.UNKNOWN_COLUMN, f"unknown column {d} in catalog {src}")
    if target_ref and target_ref in derives:
        return None, Rejection(RejectCode.LEAKAGE, "leaks target")
    if now is not None:
        for src in {p[0] for p in pairs}:
            wm = drift_watermark(conn, src)
            if wm is None or wm < now - fresh_within:
                return None, Rejection(RejectCode.STALE, f"stale source: {src}")

    # C2-C3: every requirement below is minted through the SANCTIONED, registry-validated factory
    # (validation_requirements.build_requirement) — the deterministic code picks code + typed params
    # from server-known refs; a bad code/param is a PROGRAMMER error (raises), never swallowed. Imported
    # here (function-local) because validation_requirements imports REQUIREMENT_CODES/Requirement from
    # this module — a module-top import would be a circular import at load time.
    from featuregen.overlay.upload.validation_requirements import build_requirement

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
        for src, d in pairs:
            lref = logical_ref_of(conn, src, d)
            ov = _governed_read(conn, lref, "logical_representation")
            if _is_numeric(ov.value):   # value is None on the C1 drift/fork fail-closed → won't clear
                continue
            declared = read_column_facts(conn, lref, "declared_type").value
            if declared and not _is_numeric(declared):
                return None, Rejection(RejectCode.NON_NUMERIC,
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
        for src, d in pairs:
            ov = _governed_read(conn, logical_ref_of(conn, src, d), "additivity")
            if ov.status == "resolved":
                if ov.value in ("semi_additive", "non_additive"):
                    return None, Rejection(RejectCode.ADDITIVITY,
                                           f"unsafe additive aggregation of {d}")
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
        return None, Rejection(RejectCode.MIXED_UNITS,
                               f"mixed units {sorted(units)}; aggregation would be silently wrong")
    if len(currencies) > 1:
        return None, Rejection(RejectCode.MIXED_CURRENCY, f"mixed currencies {sorted(currencies)}")
    if len(pairs) >= 2:   # a COMBINING op: an operand's unknown scale/currency is a fact to verify
        for src, d in pairs:
            if not meta[d]["unit"]:
                requirements.append(build_requirement(
                    code="UNIT_CONSISTENT", operand=(src, d),
                    detail="unit unknown across a combining op", params=None))
            if not meta[d]["currency"]:
                # currency is UNKNOWN here (that is the mint condition), so no bound currency_ref is
                # available — pass none; currency_ref is OPTIONAL in the registry (C2C3-T1 tweak).
                requirements.append(build_requirement(
                    code="CURRENCY_CONSISTENT", operand=(src, d),
                    detail="currency unknown across a combining op", params={}))

    # ── disposition: temporal — a windowed feature needs a governed-VERIFIED as-of column; a table
    #    with NO as-of column at all is still a hard reject (future-leakage risk) ──
    if _is_windowed(aggregation):
        checked_tables: set[tuple[str, str]] = set()
        for src, d in pairs:
            if d.count(".") < 2 or (src, d.split(".")[-2]) in checked_tables:
                continue
            checked_tables.add((src, d.split(".")[-2]))
            aref = _as_of_column_ref(conn, src, d.split(".")[-2])
            if aref is None:
                return None, Rejection(RejectCode.NO_POINT_IN_TIME,
                                       f"no point-in-time basis for {d} (future-leakage risk)")
            time_operand = (src, aref)
            ov = _governed_read(conn, logical_ref_of(conn, src, aref), "is_as_of")
            if ov.status != "resolved":
                requirements.append(build_requirement(
                    code="TEMPORAL_IS_POPULATED", operand=(src, aref),
                    detail="as-of column declared, not governed-verified", params=None))

    # ── disposition: grain — a grain feature needs a governed-VERIFIED grain column ──
    if grain_table and len(catalogs) == 1:
        gcat = next(iter(catalogs))
        gref = _grain_column_ref(conn, gcat, grain_table)
        if gref is not None:
            grain_operand = (gcat, gref)
            ov = _governed_read(conn, logical_ref_of(conn, gcat, gref), "is_grain")
            if ov.status != "resolved":
                requirements.append(build_requirement(
                    code="GRAIN_IS_UNIQUE", operand=(gcat, gref),
                    detail="grain declared, not governed-verified", params=None))

    # ── disposition: cross-table join authority (spec §7). A measure in a different table than the
    #    grain needs a real path; UNVERIFIED -> JOIN_CONNECTIVITY, no-path / read-scope-denied -> reject ──
    if grain_table and len(catalogs) == 1:
        jcat = next(iter(catalogs))
        for src, d in pairs:
            if d.count(".") >= 2 and d.split(".")[-2] != grain_table:
                outcome = classify_join_path(conn, jcat, grain_table, d.split(".")[-2], roles=roles)
                if outcome.kind == JoinOutcome.NO_PATH:
                    return None, Rejection(RejectCode.NO_JOIN_PATH,
                                           f"no join path {grain_table} -> {d}")
                if outcome.kind == JoinOutcome.DENIED:
                    return None, Rejection(RejectCode.JOIN_DENIED,
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
        grouping_refs=(), validation_status=status, requirements=tuple(requirements)), None


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
            conn, cols, objective=objective, entity=entity, scope=scope)
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
        menu, table_context = _build_menu(conn, cols, objective=objective, entity=entity)
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
        menu, table_context = _build_menu(conn, cols, objective=nl_query)
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
