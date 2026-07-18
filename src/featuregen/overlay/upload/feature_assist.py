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

import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

import psycopg

from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.catalog_changes import drift_watermark
from featuregen.overlay.upload.enrich_llm import audited_structured_call
from featuregen.overlay.upload.join_path import JoinStep, find_join_path
from featuregen.overlay.upload.read_scope import allowed_sensitivities

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
    AMBIGUOUS_CATALOG = "AMBIGUOUS_CATALOG"
    UNKNOWN_COLUMN = "UNKNOWN_COLUMN"
    LEAKAGE = "LEAKAGE"
    STALE = "STALE"
    ADDITIVITY = "ADDITIVITY"
    MIXED_UNITS = "MIXED_UNITS"
    MIXED_CURRENCY = "MIXED_CURRENCY"
    NO_POINT_IN_TIME = "NO_POINT_IN_TIME"
    REDUNDANT = "REDUNDANT"                 # near-duplicate of an already-accepted candidate (item 1a)
    ALREADY_REGISTERED = "ALREADY_REGISTERED"   # duplicates a confirmed/registered feature (item 2)
    CRITIC = "CRITIC"                       # LLM-2 critic flagged a quality/fit issue (item 5)
    NO_REVISION = "NO_REVISION"             # refine_idea: the model produced no revision to validate


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


def _call_raw(conn, client: LLMClient, task: str, prompt_id: str, schema_id: str,
              instruction: str, catalog_metadata: dict, *,
              actor: IdentityEnvelope | None = None) -> dict:
    """Every feature-assist LLM call goes through the AUDITED seam (M6): the egress guard scans the
    user text (`instruction`) + metadata before dispatch, and the call is recorded in llm_call. Was a
    raw client.call() that skipped both — a real leak against a non-fake provider. `actor` is the
    HUMAN subject the route threaded in, so the llm_call attribution names who asked (not the fallback
    service enrichment actor); absent, the seam falls back to that service identity."""
    out = audited_structured_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, instruction=instruction, actor=actor)
    return out if isinstance(out, dict) else {}


def _candidate_columns(conn, catalog_source: str | None, roles: Iterable[str],
                       entity: str | None = None) -> list[dict]:
    # Read-scope: never feed a sensitivity-tagged column the caller can't see to the LLM (M6).
    sql = ("SELECT catalog_source, object_ref, table_name, column_name, concept, domain, definition "
           "FROM graph_node WHERE kind = 'column' "
           "AND (sensitivity IS NULL OR sensitivity = ANY(%s))")
    params: list = [allowed_sensitivities(roles)]
    if entity:
        # Cross-domain gather: candidates from EVERY catalog that contains this entity, not one source.
        sql += (" AND catalog_source IN "
                "(SELECT DISTINCT catalog_source FROM graph_node WHERE entity = %s)")
        params.append(entity)
    elif catalog_source:
        sql += " AND catalog_source = %s"
        params.append(catalog_source)
    rows = conn.execute(sql, params).fetchall()
    return [{"catalog_source": r[0], "object_ref": r[1], "table": r[2], "column": r[3],
             "concept": r[4], "domain": r[5], "definition": r[6]} for r in rows]


def _menu(cols: list[dict]) -> list[dict]:
    return [{k: c[k] for k in ("object_ref", "table", "column", "concept", "domain")} for c in cols]


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


def _table_has_as_of(conn, catalog_source: str, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM graph_node WHERE catalog_source = %s AND table_name = %s "
        "AND is_as_of = true LIMIT 1",
        (catalog_source, table)).fetchone()
    return row is not None


def _validate_idea(conn, raw: dict, known: set[str], src_of: dict[str, set[str]],
                   target_ref: str | None, now: datetime | None, fresh_within: timedelta):
    """The deterministic gauntlet. Returns (FeatureIdea, 'ok') or (None, reason). Runs every pass so a
    leaky / stale / unsafe candidate can NEVER be returned. `src_of` maps object_ref -> the catalog
    source(s) it lives in within the candidate context, used to resolve each derive's catalog (B3)."""
    derives = [d for d in raw.get("derives_from", []) if d in known]
    if not derives:
        return None, Rejection(RejectCode.UNGROUNDED, "ungrounded")
    # B3: resolve each derive to exactly one catalog_source from the candidate context. If a bare
    # object_ref maps to >1 catalog we cannot know which the LLM meant -> fail closed.
    pairs: list[tuple[str, str]] = []
    for d in derives:
        srcs = src_of.get(d, set())
        if len(srcs) != 1:
            return None, Rejection(RejectCode.AMBIGUOUS_CATALOG, f"ambiguous catalog for {d}")
        pairs.append((next(iter(srcs)), d))
    # M4: verify each resolved (catalog_source, object_ref) pair actually EXISTS as a graph node.
    # `src_of` may be client-supplied over HTTP (the MCV path) — a fabricated catalog must fail closed,
    # not sail through freshness on a catalog the column doesn't live in. _column_meta is pair-scoped.
    meta = _column_meta(conn, pairs)
    for src, d in pairs:
        if d not in meta or meta[d]["catalog_source"] != src:
            return None, Rejection(RejectCode.UNKNOWN_COLUMN, f"unknown column {d} in catalog {src}")
    if target_ref and target_ref in derives:
        return None, Rejection(RejectCode.LEAKAGE, "leaks target")
    if now is not None:   # freshness — every RESOLVED source must be fresh
        for src in {p[0] for p in pairs}:
            wm = drift_watermark(conn, src)
            if wm is None or wm < now - fresh_within:
                return None, Rejection(RejectCode.STALE, f"stale source: {src}")
    if _is_additive_unsafe(raw.get("aggregation")):   # aggregation safety (additivity) — M2 widened
        for d in derives:
            if meta.get(d, {}).get("additivity") in ("semi_additive", "non_additive"):
                return None, Rejection(RejectCode.ADDITIVITY, f"unsafe additive aggregation of {d}")
    # unit/currency safety: combining columns of mixed scale (dollars vs cents) / currency is
    # silently wrong (migration 0957). Reject when the derives span >1 distinct non-empty unit/currency.
    units = {meta[d]["unit"] for d in derives if meta.get(d, {}).get("unit")}
    currencies = {meta[d]["currency"] for d in derives if meta.get(d, {}).get("currency")}
    if len(units) > 1:
        return None, Rejection(RejectCode.MIXED_UNITS,
                               f"mixed units {sorted(units)}; aggregation would be silently wrong")
    if len(currencies) > 1:
        return None, Rejection(RejectCode.MIXED_CURRENCY, f"mixed currencies {sorted(currencies)}")
    if _is_windowed(raw.get("aggregation")):   # point-in-time: a windowed feature needs an as-of column
        for src, d in pairs:
            # object_ref is "[catalog.]schema.table.column"; table is the second-to-last segment.
            if d.count(".") >= 2 and not _table_has_as_of(conn, src, d.split(".")[-2]):
                return None, Rejection(RejectCode.NO_POINT_IN_TIME,
                                       f"no point-in-time basis for {d} (future-leakage risk)")
    return FeatureIdea(
        name=str(raw.get("name", "")), description=str(raw.get("description", "")),
        derives_from=derives, aggregation=raw.get("aggregation"),
        grain_table=raw.get("grain_table"), derives_pairs=tuple(pairs),
        rationale=str(raw.get("rationale", ""))), None   # §14.2 one-line causal rationale (opportunistic)


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
         target_ref, now, fresh_within) -> FeatureIdea | None:
    """Gauntlet + dedup for one raw candidate. Returns the FeatureIdea to accept, or None (recording a
    structured rejection in `avoid`). Shared by the generation loop and the single critic-fix pass."""
    idea, rej = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within)
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
    if feedback:
        inputs["feedback"] = feedback
    out = _call_raw(conn, client, "overlay.feature.recommend", "feature_recommend_v1",
                    "feature_ideas", objective, inputs, actor=actor)
    for raw in out.get("features", []):
        idea = _vet(conn, raw, known, src_of, registered, keep, seen, [], target_ref, now, fresh_within)
        if idea is not None:
            keep.append(idea)
            seen.add(idea.name)
    return keep


def _generate(conn, objective: str, client: LLMClient, *,
              catalog_source: str | None = None, roles: Iterable[str] = (),
              entity: str | None = None,
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
    menu = _menu(cols)

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
        if feedback:
            inputs["feedback"] = feedback
        out = _call_raw(conn, client, "overlay.feature.recommend", "feature_recommend_v1",
                        "feature_ideas", objective, inputs, actor=actor)
        proposed = out.get("features", [])
        if not proposed:                       # stalled generator -> stop
            break
        for raw in proposed:
            idea = _vet(conn, raw, known, src_of, registered, accepted, seen, avoid,
                        target_ref, now, fresh_within)
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
                                     registered, target_ref, now, fresh_within, feedback, actor=actor)

    # ---- Phase 3: forward to the human; residual critic notes ride along as ADVISORY ----
    if issues:
        accepted = [f if f.name not in issues else replace(f, critic_note=issues[f.name])
                    for f in accepted]
    return accepted[:target], avoid


def recommend_features(conn, objective: str, client: LLMClient, *,
                       catalog_source: str | None = None, roles: Iterable[str] = (),
                       entity: str | None = None,
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
    inputs: dict = {"columns": _menu(cols), "fix": fix}
    if objective:
        inputs["objective"] = objective
    out = _call_raw(conn, client, "overlay.feature.recommend", "feature_recommend_v1",
                    "feature_ideas", instruction, inputs, actor=actor)
    proposed = out.get("features", [])
    if not proposed:
        return None, {"name": str(idea.get("name", "")),
                      "reason": "no revision was produced", "code": RejectCode.NO_REVISION}
    raw = proposed[0] if isinstance(proposed[0], dict) else {}
    revised, rej = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within)
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
    out = _call_raw(conn, client, "overlay.feature.recipe", "feature_recipe_v1", "feature_recipe",
                    nl_query, {"columns": _menu(cols)}, actor=actor)
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
    out = _call_raw(conn, client, "overlay.feature.leakage", "feature_leakage_v1", "leakage",
                    "Flag columns that leak the prediction target.",
                    {"derives_from": list(derives_from), "target": target_ref}, actor=actor)
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
    out = _call_raw(conn, client, "overlay.feature.recommend_set", "feature_set_v1",
                    "feature_set_rec", hypothesis, {"sets": summary}, actor=actor)
    default = sets[0].lens if sets else ""
    return SetRecommendation(recommended_lens=str(out.get("recommended_lens", default)),
                             reasoning=str(out.get("reasoning", "")))
