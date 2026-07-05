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

from collections.abc import Iterable
from dataclasses import dataclass, field

from featuregen.intake.llm import LLMClient, LLMRequest
from featuregen.overlay.upload.join_path import JoinStep, find_join_path
from featuregen.overlay.upload.read_scope import allowed_sensitivities


def _call_raw(client: LLMClient, task: str, prompt_id: str, schema_id: str, inputs: dict) -> dict:
    req = LLMRequest(
        task=task, prompt_id=prompt_id, prompt_version=1, inputs=inputs,
        output_schema_id=schema_id, output_schema_version=1,
        generation_settings={"provider": "fake", "model": "test"})
    out = client.call(req).output
    return out if isinstance(out, dict) else {}


def _candidate_columns(conn, catalog_source: str | None, roles: Iterable[str]) -> list[dict]:
    # Read-scope: never feed a sensitivity-tagged column the caller can't see to the LLM (M6).
    sql = ("SELECT catalog_source, object_ref, table_name, column_name, concept, domain, definition "
           "FROM graph_node WHERE kind = 'column' "
           "AND (sensitivity IS NULL OR sensitivity = ANY(%s))")
    params: list = [allowed_sensitivities(roles)]
    if catalog_source:
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


def recommend_features(conn, objective: str, client: LLMClient, *,
                       catalog_source: str | None = None,
                       roles: Iterable[str] = ()) -> list[FeatureIdea]:
    cols = _candidate_columns(conn, catalog_source, roles)
    known = {c["object_ref"] for c in cols}
    out = _call_raw(client, "overlay.feature.recommend", "feature_recommend_v1", "feature_ideas",
                    {"objective": objective, "columns": _menu(cols)})
    ideas: list[FeatureIdea] = []
    for raw in out.get("features", []):
        derives = [d for d in raw.get("derives_from", []) if d in known]   # drop hallucinated
        if not derives:
            continue
        ideas.append(FeatureIdea(
            name=str(raw.get("name", "")), description=str(raw.get("description", "")),
            derives_from=derives, aggregation=raw.get("aggregation"),
            grain_table=raw.get("grain_table")))
    return ideas


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
    out = _call_raw(client, "overlay.feature.recipe", "feature_recipe_v1", "feature_recipe",
                    {"query": nl_query, "columns": _menu(cols)})
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
    out = _call_raw(client, "overlay.feature.leakage", "feature_leakage_v1", "leakage",
                    {"derives_from": list(derives_from), "target": target_ref})
    return [LeakageWarning(object_ref=w["object_ref"], reason=str(w.get("reason", "")))
            for w in out.get("leaks", [])
            if isinstance(w, dict) and w.get("object_ref") in used]
