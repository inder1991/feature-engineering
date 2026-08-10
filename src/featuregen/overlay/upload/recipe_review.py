"""BR-23 schema half (R1, beside BR-2) — revision-specific SME review evidence, append-only.

The store the family migrations (BR-11..BR-16) write their sign-offs into AS they migrate, and
the store BR-23 proper folds validity from. What ships here is exactly the schema layer:

* :func:`record_review_event` — append one immutable, attributable decision, validated against the
  closed vocabularies (decision, reviewer role) and the supersedes chain rules (the superseded
  event must exist and belong to the SAME recipe — an audit chain across recipes is a data error).
* :func:`review_events` / :func:`current_review` — reads. "Current" is a PROJECTION (the newest
  event for one recipe at one exact revision hash), never a mutable column: approval is
  revision-specific by construction, so a definition edit makes the old approval a lookup MISS,
  not a stale status someone must remember to flip.

What does NOT ship here (BR-23 proper): the review-validity fold over required roles, the decision
APIs and permissions, dependency invalidation, and batch reports. Nothing in the platform READS
this store yet — it exists so evidence lands durably from the first migrated family onward.
"""
from __future__ import annotations

import json
from dataclasses import dataclass

from featuregen.idgen import mint_id

REVIEW_DECISIONS = ("approved", "changes_required", "rejected", "retired")

# BR-23's review roles, closed. Which roles a recipe REQUIRES (near-label adds model_risk,
# sensitive adds privacy_compliance, ...) is the validity fold's law — BR-23 proper.
REVIEWER_ROLES = ("banking_sme", "data_semantic_owner", "formula_engineering",
                  "model_risk", "privacy_compliance", "treasury_regulatory_accounting")


class RecipeReviewError(ValueError):
    """An invalid review event — refused before anything is written."""


@dataclass(frozen=True, slots=True)
class RecipeReviewEventV1:
    event_id: str
    recipe_id: str
    recipe_revision_hash: str
    output_id: str
    decision: str
    reviewer: str
    reviewer_role: str
    reviewed_primary_objective: str
    reviewed_supporting_objectives: tuple[str, ...]
    formula_expectation_hash: str | None
    gold_corpus_refs: tuple[str, ...]
    policy_dependencies: tuple[str, ...]
    permitted_stages: tuple[str, ...]
    prohibited_stages: tuple[str, ...]
    rationale: str
    evidence_refs: tuple[str, ...]
    supersedes_event_id: str | None


_COLUMNS = ("event_id, recipe_id, recipe_revision_hash, output_id, decision, reviewer, "
            "reviewer_role, reviewed_primary_objective, reviewed_supporting_objectives, "
            "formula_expectation_hash, gold_corpus_refs, policy_dependencies, permitted_stages, "
            "prohibited_stages, rationale, evidence_refs, supersedes_event_id")


def _event(row) -> RecipeReviewEventV1:
    return RecipeReviewEventV1(
        event_id=row[0], recipe_id=row[1], recipe_revision_hash=row[2], output_id=row[3],
        decision=row[4], reviewer=row[5], reviewer_role=row[6],
        reviewed_primary_objective=row[7],
        reviewed_supporting_objectives=tuple(row[8] or ()),
        formula_expectation_hash=row[9],
        gold_corpus_refs=tuple(row[10] or ()),
        policy_dependencies=tuple(row[11] or ()),
        permitted_stages=tuple(row[12] or ()),
        prohibited_stages=tuple(row[13] or ()),
        rationale=row[14], evidence_refs=tuple(row[15] or ()),
        supersedes_event_id=row[16])


def record_review_event(conn, *, recipe_id: str, recipe_revision_hash: str, decision: str,
                        reviewer: str, reviewer_role: str, output_id: str = "",
                        reviewed_primary_objective: str = "",
                        reviewed_supporting_objectives: tuple[str, ...] = (),
                        formula_expectation_hash: str | None = None,
                        gold_corpus_refs: tuple[str, ...] = (),
                        policy_dependencies: tuple[str, ...] = (),
                        permitted_stages: tuple[str, ...] = (),
                        prohibited_stages: tuple[str, ...] = (),
                        rationale: str = "",
                        evidence_refs: tuple[str, ...] = (),
                        supersedes_event_id: str | None = None) -> str:
    """Append one review decision; returns the minted event id. Validation is schema-level only —
    whether THIS reviewer role satisfies THIS recipe's requirements is the BR-23 validity fold."""
    if decision not in REVIEW_DECISIONS:
        raise RecipeReviewError(f"decision {decision!r} not in {REVIEW_DECISIONS}")
    if reviewer_role not in REVIEWER_ROLES:
        raise RecipeReviewError(f"reviewer_role {reviewer_role!r} not in {REVIEWER_ROLES}")
    if not reviewer.strip():
        raise RecipeReviewError("a review event is attributable: reviewer is mandatory")
    if not recipe_id.strip() or not recipe_revision_hash.strip():
        raise RecipeReviewError("recipe_id and recipe_revision_hash are mandatory")
    if set(permitted_stages) & set(prohibited_stages):
        raise RecipeReviewError("a stage cannot be both permitted and prohibited")
    if supersedes_event_id is not None:
        prior = conn.execute(
            "SELECT recipe_id FROM recipe_review_event WHERE event_id = %s",
            (supersedes_event_id,)).fetchone()
        if prior is None:
            raise RecipeReviewError(f"supersedes unknown event {supersedes_event_id!r}")
        if prior[0] != recipe_id:
            raise RecipeReviewError(
                "an event may only supersede a review of the SAME recipe "
                f"({prior[0]!r} != {recipe_id!r})")
    event_id = mint_id("rre")
    conn.execute(
        f"INSERT INTO recipe_review_event ({_COLUMNS}) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s::jsonb, %s::jsonb, "
        "%s::jsonb, %s::jsonb, %s, %s::jsonb, %s)",
        (event_id, recipe_id, recipe_revision_hash, output_id, decision, reviewer,
         reviewer_role, reviewed_primary_objective,
         json.dumps(list(reviewed_supporting_objectives)), formula_expectation_hash,
         json.dumps(list(gold_corpus_refs)), json.dumps(list(policy_dependencies)),
         json.dumps(list(permitted_stages)), json.dumps(list(prohibited_stages)),
         rationale, json.dumps(list(evidence_refs)), supersedes_event_id))
    return event_id


def review_events(conn, recipe_id: str) -> list[RecipeReviewEventV1]:
    """Every recorded event for a recipe, oldest first — ordered by the append sequence
    (1061): same-transaction events share now(), so a timestamp is a coincidence, not an
    order."""
    rows = conn.execute(
        f"SELECT {_COLUMNS} FROM recipe_review_event WHERE recipe_id = %s "
        "ORDER BY recorded_seq", (recipe_id,)).fetchall()
    return [_event(r) for r in rows]


def current_review(conn, *, recipe_id: str,
                   recipe_revision_hash: str) -> RecipeReviewEventV1 | None:
    """The newest event for THIS recipe at THIS exact revision hash — None for any edited
    definition, which is precisely how revision-specific approval stales without a flag."""
    row = conn.execute(
        f"SELECT {_COLUMNS} FROM recipe_review_event "
        "WHERE recipe_id = %s AND recipe_revision_hash = %s "
        "ORDER BY recorded_seq DESC LIMIT 1",
        (recipe_id, recipe_revision_hash)).fetchone()
    return _event(row) if row else None
