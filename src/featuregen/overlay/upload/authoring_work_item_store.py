"""S2 — the generalized authoring work item, and the compatibility reader over both shapes.

**Two tables, one reader, no migration of rows.** ``recipe_formula_shadow_work_item`` is write-once
by trigger with ``UPDATE``/``DELETE`` revoked from the app role, and its NOT NULL columns are
recipe-specific (``recipe_id``, ``recipe_candidate_key``, ``recipe_expectation_json``). A free-form
or user-defined authoring run has none of them and could not produce a legal legacy row, so
generalizing the shape in place is not merely hard — it is impossible without dropping constraints
that exist to hold the old rows honest.

So the new shape lives in its own table and :func:`read_work_items` unions the two, labelling every
item with the ``origin`` it came from. A caller can never mistake a legacy recipe-shaped item for a
generalized one, because the label is derived from WHICH TABLE the row was in rather than from a
column somebody might have set wrongly.

**``FeatureDefinitionV1`` is created OR resolved**, and those are one operation: the definition is
identified by its content, so two selections that author the same feature share one row and the
caller does not have to know which happened.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from featuregen.canonical import jcs_sha256
from featuregen.materialize.admission import hive_identifier

__all__ = [
    "AuthoringWorkItemV1",
    "FeatureDefinitionV1",
    "WorkItemOrigin",
    "link_selection_to_definition",
    "read_work_items",
    "record_work_item",
    "resolve_feature_definition",
]


class WorkItemOrigin(StrEnum):
    """What authored this item. Closed: an origin nothing recognises can be authored by no path,
    and an open vocabulary would let one arrive and sit unprocessed forever."""

    RECIPE = "recipe"
    LLM_INTENT = "llm_intent"
    USER_DEFINITION = "user_definition"

    @property
    def authors_from_reviewed_blueprint(self) -> bool:
        """Only a recipe item stands on a reviewed blueprint — which is what lets C-A5's
        deterministic producer take it without a provider call."""
        return self is WorkItemOrigin.RECIPE


@dataclass(frozen=True, slots=True)
class AuthoringWorkItemV1:
    """One unit of authoring work, whatever produced it."""

    work_item_id: str
    origin: WorkItemOrigin
    intent_id: str
    considered_revision_id: str
    option_id: str
    expectation: dict[str, Any]
    expectation_hash: str
    binding_plan_hash: str
    frozen_configuration_hash: str
    reviewed_blueprint_revision: str | None = None

    def __post_init__(self) -> None:
        if self.origin.authors_from_reviewed_blueprint:
            if not (self.reviewed_blueprint_revision or "").strip():
                raise ValueError(
                    f"{self.work_item_id}: a recipe work item names no reviewed blueprint, so it "
                    f"cannot take the deterministic path — and the deterministic path is the only "
                    f"reason its origin is distinguished")
        elif self.reviewed_blueprint_revision:
            raise ValueError(
                f"{self.work_item_id}: origin {self.origin.value} names a reviewed blueprint "
                f"({self.reviewed_blueprint_revision!r}), which would assert a review that never "
                f"happened — only a recipe item stands on one")


@dataclass(frozen=True, slots=True)
class FeatureDefinitionV1:
    """What a feature IS, content-addressed so create-or-resolve is one operation."""

    definition_id: str
    feature_name: str
    entity: str
    grain_keys: tuple[str, ...]

    def __post_init__(self) -> None:
        # Folded through the ONE normalizer, not a second one — `hive_identifier`'s docstring says
        # why: a second normalizer is a second chance to disagree about which column a feature
        # occupies. It raises on a name that cannot be expressed, which is a plan error.
        object.__setattr__(self, "feature_name", hive_identifier(self.feature_name))
        if not self.entity.strip():
            raise ValueError("a feature definition must name the entity it is computed for")
        if not self.grain_keys:
            raise ValueError("a feature definition with no grain keys describes no population")

    @property
    def content_hash(self) -> str:
        return jcs_sha256({"feature_name": self.feature_name, "entity": self.entity,
                           "grain_keys": list(self.grain_keys)})


def record_work_item(conn, item: AuthoringWorkItemV1, *, idempotency_key: str) -> str:
    """Append a generalized work item. Idempotent on ``idempotency_key``."""
    conn.execute(
        "INSERT INTO authoring_work_item (work_item_id, idempotency_key, origin, intent_id, "
        "considered_revision_id, option_id, expectation_json, expectation_hash, "
        "reviewed_blueprint_revision, binding_plan_hash, frozen_configuration_hash) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s) "
        "ON CONFLICT (idempotency_key) DO NOTHING",
        (item.work_item_id, idempotency_key, item.origin.value, item.intent_id,
         item.considered_revision_id, item.option_id, json.dumps(item.expectation),
         item.expectation_hash, item.reviewed_blueprint_revision, item.binding_plan_hash,
         item.frozen_configuration_hash))
    return item.work_item_id


def read_work_items(conn, *, considered_revision_id: str) -> tuple[AuthoringWorkItemV1, ...]:
    """Every work item for one considered revision, from BOTH shapes.

    The legacy rows are read as ``RECIPE`` origin because that is what their table means — the label
    comes from which table the row was in, not from a column somebody could have set wrongly. Their
    ``recipe_candidate_key`` is the reviewed blueprint they stood on, which is exactly the fact the
    generalized shape records explicitly.
    """
    items: list[AuthoringWorkItemV1] = []

    for row in conn.execute(
        "SELECT work_item_id, origin, intent_id, considered_revision_id, option_id, "
        "expectation_json, expectation_hash, reviewed_blueprint_revision, binding_plan_hash, "
        "frozen_configuration_hash FROM authoring_work_item "
        "WHERE considered_revision_id = %s ORDER BY created_at, work_item_id",
        (considered_revision_id,)).fetchall():
        items.append(AuthoringWorkItemV1(
            work_item_id=row[0], origin=WorkItemOrigin(row[1]), intent_id=row[2],
            considered_revision_id=row[3], option_id=row[4], expectation=row[5],
            expectation_hash=row[6], reviewed_blueprint_revision=row[7],
            binding_plan_hash=row[8], frozen_configuration_hash=row[9]))

    for row in conn.execute(
        "SELECT work_item_id, intent_id, considered_revision_id, recipe_id, "
        "recipe_expectation_json, recipe_expectation_hash, recipe_candidate_key, "
        "binding_plan_hash, frozen_configuration_hash FROM recipe_formula_shadow_work_item "
        "WHERE considered_revision_id = %s ORDER BY created_at, work_item_id",
        (considered_revision_id,)).fetchall():
        items.append(AuthoringWorkItemV1(
            work_item_id=row[0], origin=WorkItemOrigin.RECIPE, intent_id=row[1],
            considered_revision_id=row[2],
            # The legacy shape has no option id — it predates option-addressable selection. The
            # recipe id is what it identified work by, and saying so is better than inventing an
            # option nobody served.
            option_id=f"legacy-recipe:{row[3]}",
            expectation=row[4], expectation_hash=row[5],
            reviewed_blueprint_revision=row[6],
            binding_plan_hash=row[7] or "legacy-unrecorded",
            frozen_configuration_hash=row[8]))

    return tuple(items)


def resolve_feature_definition(conn, definition: FeatureDefinitionV1) -> str:
    """Create the definition, or resolve to the existing one with identical content.

    One operation because the definition is identified by its content: two selections that author
    the same feature share one row, and the caller does not have to know which happened.
    """
    conn.execute(
        "INSERT INTO feature_definition (definition_id, feature_name, content_hash, entity, "
        "grain_keys) VALUES (%s, %s, %s, %s, %s::jsonb) ON CONFLICT (content_hash) DO NOTHING",
        (definition.definition_id, definition.feature_name, definition.content_hash,
         definition.entity, json.dumps(list(definition.grain_keys))))
    row = conn.execute(
        "SELECT definition_id FROM feature_definition WHERE content_hash = %s",
        (definition.content_hash,)).fetchone()
    return row[0]


def link_selection_to_definition(conn, *, selection_revision_id: str, definition_id: str) -> None:
    """Link a selection to the definition authoring resolved for it — once, and append-only.

    Re-authoring a selection to a DIFFERENT definition would mean the feature a person chose became
    a different feature with nothing recording the change, so the primary key refuses it rather
    than the second write winning.
    """
    conn.execute(
        "INSERT INTO feature_selection_definition_link (selection_revision_id, definition_id) "
        "VALUES (%s, %s)", (selection_revision_id, definition_id))
