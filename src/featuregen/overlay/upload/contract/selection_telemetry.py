"""TASK 7 phase 1 — what did humans actually KEEP? A join and a report, not new capture.

The product review's largest finding: the plan optimised what is SHOWN and never asked what happens
next. The data was already durable — every Gate-1 choice row carries the FULL considered snapshot
(each feature with its origin fields and option id) plus the chosen option — so selection rate by
template / origin / parameterisation is a query, not a pipeline. This module is that query.

ORIGIN ON EVERY SELECTION (owner decision, 2026-08-10): the platform runs TWO proposer engines into
one gauntlet, and until this report nobody could answer which one humans keep, per question type.
`generation_source` is the SERVER-assigned path label (recipe | llm_freeform | user_defined) — the
measured answer to every future "invest in templates vs invest in the LLM path" debate.

Phase 2 (feeding selection history back into ordering) is EXPLICITLY deferred until there is
volume — tens of contracts, not three. Telemetry first; the north-star metric exists from today.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SelectionRowV1:
    """One offered candidate identity across all Gate-1 rounds. The feature NAME is the
    parameterisation-bearing identity (Task 4b puts the chosen window in it), so the same recipe
    under two parameterisations is honestly two rows."""
    generation_source: str          # recipe | llm_freeform | user_defined
    recipe_id: str | None
    feature_name: str
    offered: int                    # rounds where this candidate was on the menu
    chosen: int                     # rounds where the human picked it
    use_cases: tuple[str, ...]      # registry taxonomy for recipe rows; () for LLM/user rows


@dataclass(frozen=True, slots=True)
class SelectionReportV1:
    rows: tuple[SelectionRowV1, ...]        # sorted: most-chosen, then most-offered, then name
    by_origin: dict                         # generation_source -> {"offered": n, "chosen": n}
    rounds: int                             # Gate-1 choice rows consulted


def _features_of(snapshot: dict):
    anchor = snapshot.get("anchor")
    if anchor:
        yield "anchor", anchor
    for feature_set in snapshot.get("alternatives") or []:
        for feature in feature_set.get("features") or []:
            if feature:
                yield "alternative", feature


def _is_chosen(source: str, feature: dict, chosen_source: str, chosen_option_id: str) -> bool:
    # Confirmation mode records the opaque option id; the legacy mode recorded the feature NAME
    # (+ which side it came from). Match either — both are exact identities within their round.
    if feature.get("option_id") == chosen_option_id:
        return True
    return source == chosen_source and feature.get("name") == chosen_option_id


def selection_report(conn) -> SelectionReportV1:
    """Selection rate by candidate identity and by origin, over every recorded Gate-1 choice."""
    from featuregen.overlay.upload.templates import ALL_TEMPLATES

    use_cases_by_template = {t.id: tuple(t.use_cases) for t in ALL_TEMPLATES}
    offered: dict[tuple, int] = {}
    chosen: dict[tuple, int] = {}
    by_origin: dict[str, dict[str, int]] = {}
    rows = conn.execute(
        "SELECT chosen_source, chosen_option_id, considered FROM contract_gate1_choice").fetchall()
    for chosen_source, chosen_option_id, considered in rows:
        # Counts are PER ROUND: one candidate identity may occupy several menu slots in a single
        # round (the same name across lenses, or beside a same-named anchor), and the legacy
        # name-based choice record cannot say which slot was picked — so both `offered` and
        # `chosen` count rounds, never slots. A round contributes at most 1 to each.
        round_offered: set[tuple] = set()
        round_chosen: set[tuple] = set()
        for source, feature in _features_of(considered or {}):
            generation_source = feature.get("generation_source") or (
                "user_defined" if source == "anchor" else "llm_freeform")
            key = (generation_source, feature.get("recipe_id"), feature.get("name") or "")
            round_offered.add(key)
            if _is_chosen(source, feature, chosen_source, chosen_option_id):
                round_chosen.add(key)
        for key in round_offered:
            offered[key] = offered.get(key, 0) + 1
            by_origin.setdefault(key[0], {"offered": 0, "chosen": 0})["offered"] += 1
        for key in round_chosen:
            chosen[key] = chosen.get(key, 0) + 1
            by_origin.setdefault(key[0], {"offered": 0, "chosen": 0})["chosen"] += 1
    report_rows = tuple(sorted(
        (SelectionRowV1(
            generation_source=generation_source, recipe_id=recipe_id, feature_name=name,
            offered=count, chosen=chosen.get(key, 0),
            use_cases=use_cases_by_template.get(recipe_id, ()))
         for key, count in offered.items()
         for generation_source, recipe_id, name in [key]),
        key=lambda r: (-r.chosen, -r.offered, r.feature_name)))
    return SelectionReportV1(rows=report_rows, by_origin=by_origin, rounds=len(rows))
