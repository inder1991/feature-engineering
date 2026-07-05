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

import re
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from featuregen.intake.llm import LLMClient
from featuregen.overlay.catalog_changes import drift_watermark
from featuregen.overlay.upload.enrich_llm import audited_structured_call
from featuregen.overlay.upload.join_path import JoinStep, find_join_path
from featuregen.overlay.upload.read_scope import allowed_sensitivities

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


def _call_raw(conn, client: LLMClient, task: str, prompt_id: str, schema_id: str,
              instruction: str, catalog_metadata: dict) -> dict:
    """Every feature-assist LLM call goes through the AUDITED seam (M6): the egress guard scans the
    user text (`instruction`) + metadata before dispatch, and the call is recorded in llm_call. Was a
    raw client.call() that skipped both — a real leak against a non-fake provider."""
    out = audited_structured_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, instruction=instruction)
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
        return None, "ungrounded"
    # B3: resolve each derive to exactly one catalog_source from the candidate context. If a bare
    # object_ref maps to >1 catalog we cannot know which the LLM meant -> fail closed.
    pairs: list[tuple[str, str]] = []
    for d in derives:
        srcs = src_of.get(d, set())
        if len(srcs) != 1:
            return None, f"ambiguous catalog for {d}"
        pairs.append((next(iter(srcs)), d))
    # M4: verify each resolved (catalog_source, object_ref) pair actually EXISTS as a graph node.
    # `src_of` may be client-supplied over HTTP (the MCV path) — a fabricated catalog must fail closed,
    # not sail through freshness on a catalog the column doesn't live in. _column_meta is pair-scoped.
    meta = _column_meta(conn, pairs)
    for src, d in pairs:
        if d not in meta or meta[d]["catalog_source"] != src:
            return None, f"unknown column {d} in catalog {src}"
    if target_ref and target_ref in derives:
        return None, "leaks target"
    if now is not None:   # freshness — every RESOLVED source must be fresh
        for src in {p[0] for p in pairs}:
            wm = drift_watermark(conn, src)
            if wm is None or wm < now - fresh_within:
                return None, f"stale source: {src}"
    if _is_additive_unsafe(raw.get("aggregation")):   # aggregation safety (additivity) — M2 widened
        for d in derives:
            if meta.get(d, {}).get("additivity") in ("semi_additive", "non_additive"):
                return None, f"unsafe additive aggregation of {d}"
    # unit/currency safety: combining columns of mixed scale (dollars vs cents) / currency is
    # silently wrong (migration 0957). Reject when the derives span >1 distinct non-empty unit/currency.
    units = {meta[d]["unit"] for d in derives if meta.get(d, {}).get("unit")}
    currencies = {meta[d]["currency"] for d in derives if meta.get(d, {}).get("currency")}
    if len(units) > 1:
        return None, f"mixed units {sorted(units)} — aggregation would be silently wrong"
    if len(currencies) > 1:
        return None, f"mixed currencies {sorted(currencies)}"
    if _is_windowed(raw.get("aggregation")):   # point-in-time: a windowed feature needs an as-of column
        for src, d in pairs:
            # object_ref is "[catalog.]schema.table.column"; table is the second-to-last segment.
            if d.count(".") >= 2 and not _table_has_as_of(conn, src, d.split(".")[-2]):
                return None, f"no point-in-time basis for {d} (future-leakage risk)"
    return FeatureIdea(
        name=str(raw.get("name", "")), description=str(raw.get("description", "")),
        derives_from=derives, aggregation=raw.get("aggregation"),
        grain_table=raw.get("grain_table"), derives_pairs=tuple(pairs)), "ok"


def recommend_features(conn, objective: str, client: LLMClient, *,
                       catalog_source: str | None = None, roles: Iterable[str] = (),
                       entity: str | None = None,
                       target_ref: str | None = None, now: datetime | None = None,
                       fresh_within: timedelta = timedelta(hours=24),
                       target: int = 5, budget: int = 3) -> list[FeatureIdea]:
    """Bounded generate-validate-refine loop. Each round the LLM proposes; every candidate runs the
    deterministic gauntlet; rejections feed back as `avoid` hints to the next round; stops at `target`
    accepted or `budget` rounds. The LLM only proposes — code owns the loop, the checks are deterministic.
    Pass `entity` to gather candidates CROSS-DOMAIN (every catalog containing that entity)."""
    cols = _candidate_columns(conn, catalog_source, roles, entity)
    known = {c["object_ref"] for c in cols}
    src_of: dict[str, set[str]] = {}          # object_ref -> catalog_source(s) in the candidate context
    for c in cols:
        src_of.setdefault(c["object_ref"], set()).add(c["catalog_source"])
    accepted: list[FeatureIdea] = []
    seen: set[str] = set()
    avoid: list[dict] = []
    for _ in range(budget):
        if len(accepted) >= target:
            break
        out = _call_raw(conn, client, "overlay.feature.recommend", "feature_recommend_v1",
                        "feature_ideas", objective, {"columns": _menu(cols), "avoid": avoid})
        for raw in out.get("features", []):
            idea, reason = _validate_idea(conn, raw, known, src_of, target_ref, now, fresh_within)
            if idea is None:
                avoid.append({"name": raw.get("name", ""), "reason": reason})   # refine
                continue
            if idea.name in seen:
                continue
            accepted.append(idea)
            seen.add(idea.name)
    return accepted[:target]


@dataclass(frozen=True, slots=True)
class Recipe:
    intent: str
    grain_table: str | None
    derives_from: list[str]           # grounded object_refs
    aggregation: str | None
    as_of_column: str | None
    join_path: list[JoinStep] = field(default_factory=list)   # deterministic, real edges


def feature_recipe(conn, nl_query: str, client: LLMClient, *, catalog_source: str,
                   roles: Iterable[str] = ()) -> Recipe:
    cols = _candidate_columns(conn, catalog_source, roles)
    known = {c["object_ref"] for c in cols}
    out = _call_raw(conn, client, "overlay.feature.recipe", "feature_recipe_v1", "feature_recipe",
                    nl_query, {"columns": _menu(cols)})
    derives = [d for d in out.get("derives_from", []) if d in known]
    grain = out.get("grain_table")
    join_table = out.get("join_table")
    # The LLM says WHAT to compute; the join PATH is found deterministically (real edges only).
    path: list[JoinStep] = []
    if grain and join_table and grain != join_table:
        path = find_join_path(conn, catalog_source, grain, join_table) or []
    return Recipe(intent=nl_query, grain_table=grain, derives_from=derives,
                  aggregation=out.get("aggregation"), as_of_column=out.get("as_of_column"),
                  join_path=path)


@dataclass(frozen=True, slots=True)
class LeakageWarning:
    object_ref: str
    reason: str


def leakage_check(conn, derives_from: list[str], target_ref: str,
                  client: LLMClient) -> list[LeakageWarning]:
    used = set(derives_from)
    out = _call_raw(conn, client, "overlay.feature.leakage", "feature_leakage_v1", "leakage",
                    "Flag columns that leak the prediction target.",
                    {"derives_from": list(derives_from), "target": target_ref})
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
    caveat: str = ("advisory only — a fit/coverage judgment over the metadata, not a performance "
                   "prediction; confirm the winner with a backtest once features are computed")


def recommend_feature_sets(conn, objective: str, client: LLMClient, *,
                           entity: str | None = None, catalog_source: str | None = None,
                           roles: Iterable[str] = (), target_ref: str | None = None,
                           now: datetime | None = None, fresh_within: timedelta = timedelta(hours=24),
                           lenses: tuple[str, ...] = ("behavioral", "monetary", "engagement"),
                           per_set: int = 3, budget: int = 2) -> list[FeatureSet]:
    """Generate N DIVERSE, each-fully-validated feature sets — one per strategy lens — by running the
    validated loop once per lens. Every feature in every set has passed the gauntlet, so the human only
    ever curates among SAFE options."""
    return [
        FeatureSet(lens=lens, features=recommend_features(
            conn, f"{objective} (focus: {lens})", client, entity=entity,
            catalog_source=catalog_source, roles=roles, target_ref=target_ref, now=now,
            fresh_within=fresh_within, target=per_set, budget=budget))
        for lens in lenses
    ]


def recommend_set(conn, sets: list[FeatureSet], hypothesis: str,
                  client: LLMClient) -> SetRecommendation:
    """Advisory: the LLM reasons over the validated sets + the analyst's HYPOTHESIS (+ the metadata
    already in each feature) and recommends one, WITH reasons — a fit/coverage judgment, never a
    performance prediction (see SetRecommendation.caveat)."""
    summary = [{"lens": s.lens,
                "features": [{"name": f.name, "derives_from": f.derives_from,
                              "aggregation": f.aggregation} for f in s.features]} for s in sets]
    out = _call_raw(conn, client, "overlay.feature.recommend_set", "feature_set_v1",
                    "feature_set_rec", hypothesis, {"sets": summary})
    default = sets[0].lens if sets else ""
    return SetRecommendation(recommended_lens=str(out.get("recommended_lens", default)),
                             reasoning=str(out.get("reasoning", "")))
