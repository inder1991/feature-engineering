"""The target registry — register a rule, read it back, search by entity.

Content-addressed: an identical rule registered twice is ONE definition. That is what makes a
label reusable across models rather than re-invented per run, which is the difference between
"what does this bank mean by non-performing?" having one answer and having three.
"""
from __future__ import annotations

import json
import uuid

from featuregen.overlay.upload.target_contract import (
    TargetRuleV1,
    canonical_target,
    refs_read,
    target_content_hash,
)


class TargetNameTaken(ValueError):
    """This entity already has a label of this name, with a DIFFERENT rule. Raised rather than
    letting the `(entity, name)` unique index surface as a raw IntegrityError — someone iterating
    on a definition meets this routinely, and a typed refusal can name what is in the way."""


def register_target(conn, rule: TargetRuleV1, *, description: str, registered_by: str) -> str:
    """Persist a rule and its lineage; return the definition id.

    Idempotent on content: re-registering an identical rule returns the existing id rather than
    minting a second row. Verification is DESIGN-CHECKED and never higher — see the migration.
    """
    content_hash = target_content_hash(rule)
    existing = conn.execute(
        "SELECT definition_id FROM target_definition WHERE content_hash = %s",
        (content_hash,)).fetchone()
    if existing is not None:
        return existing[0]

    header = rule.header
    taken = conn.execute(
        "SELECT definition_id FROM target_definition WHERE entity = %s AND name = %s",
        (header.entity, header.name)).fetchone()
    if taken is not None:
        raise TargetNameTaken(
            f"{header.name} already exists for entity {header.entity} as {taken[0]} with a "
            "different rule — a changed rule is a new label, so give it its own name")

    definition_id = f"tdef_{uuid.uuid4().hex[:16]}"
    conn.execute(
        "INSERT INTO target_definition (definition_id, name, entity, shape, window_days,"
        " label_type, rule, content_hash, description, registered_by)"
        " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (definition_id, header.name, header.entity, rule.shape, header.window_days,
         header.label_type, json.dumps(canonical_target(rule)), content_hash,
         description, registered_by))
    for catalog_source, object_ref in refs_read(rule):
        conn.execute(
            "INSERT INTO target_derives_from (definition_id, catalog_source, object_ref)"
            " VALUES (%s, %s, %s)",
            (definition_id, catalog_source, object_ref))
    return definition_id


def _row(conn, definition_id: str, name: str, entity: str, shape: str, window_days: int,
         label_type: str, rule, verification: str, description: str) -> dict:
    derives = [(r[0], r[1]) for r in conn.execute(
        "SELECT catalog_source, object_ref FROM target_derives_from WHERE definition_id = %s"
        " ORDER BY catalog_source, object_ref", (definition_id,)).fetchall()]
    return {"definition_id": definition_id, "name": name, "entity": entity, "shape": shape,
            "window_days": window_days, "label_type": label_type, "rule": rule,
            "verification": verification, "description": description, "derives_from": derives}


_SELECT = ("SELECT definition_id, name, entity, shape, window_days, label_type, rule,"
           " verification, description FROM target_definition")


def target_by_name(conn, entity: str, name: str) -> dict | None:
    """Entity-scoped, because the unique index is `(entity, name)`. Looking up by name alone
    would return an arbitrary row once two entities both have a `tgt_churned_90d`."""
    row = conn.execute(f"{_SELECT} WHERE entity = %s AND name = %s", (entity, name)).fetchone()
    return None if row is None else _row(conn, *row)


def targets_for_entity(conn, entity: str) -> list[dict]:
    """Every label registered for this entity — the reuse surface. Ordered by name so a listing
    is stable."""
    rows = conn.execute(f"{_SELECT} WHERE entity = %s ORDER BY name", (entity,)).fetchall()
    return [_row(conn, *row) for row in rows]
