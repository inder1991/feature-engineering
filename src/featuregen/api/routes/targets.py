"""Target authoring — propose a filled form, register what the person submits.

One model call, not a dialogue: the tool fills the fields the catalog justifies, leaves blank the
ones it cannot know, and a person edits and submits. A form of a dozen fields gets rubber-stamped,
so `describe_target` renders the rule as one sentence the person can actually check — available
BEFORE submitting, which is the whole point of it.
"""
from __future__ import annotations

from typing import Annotated

import psycopg
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from featuregen.api.deps import (
    get_conn,
    get_identity,
    get_llm,
    require_feature_generate,
)
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.target_catalog_check import (
    check_target_against_catalog,
    selectable_entities,
)
from featuregen.overlay.upload.target_contract import (
    EventFilterV1,
    EventWindowRuleV1,
    StateChangeRuleV1,
    TargetContractError,
    TargetHeaderV1,
    canonical_target,
    describe_target,
)
from featuregen.overlay.upload.target_draft import propose_target_draft
from featuregen.overlay.upload.target_search import near_duplicates, search_targets
from featuregen.overlay.upload.target_store import (
    TargetNameTaken,
    register_target,
    targets_for_entity,
)

router = APIRouter()

_Conn = Annotated[psycopg.Connection, Depends(get_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]
_LLM = Annotated[LLMClient, Depends(get_llm)]


class ProposeIn(BaseModel):
    hypothesis: str = Field(min_length=1)
    entity: str = Field(min_length=1)
    catalog_source: str = Field(min_length=1)


class DescribeIn(BaseModel):
    rule: dict


class RegisterIn(BaseModel):
    rule: dict
    description: str = ""
    proposed_draft: dict | None = None
    author_comment: str = ""
    adapted_from: str | None = None


def _rule_from_body(body: dict):
    """Build a typed rule from the submitted form. The contract's refusals are the caller's."""
    header = TargetHeaderV1(
        name=str(body.get("name", "")), entity=str(body.get("entity", "")),
        anchor_catalog=str(body.get("anchor_catalog", "")),
        grain_ref=str(body.get("grain_ref", "")), as_of_ref=str(body.get("as_of_ref", "")),
        window_days=int(body.get("window_days", 0)),
        as_of_frequency=str(body.get("as_of_frequency", "")),
        label_type=str(body.get("label_type", "")),
        require_full_window=bool(body.get("require_full_window", True)),
        direction=str(body.get("direction", "forward")),
        operator=body.get("operator"), threshold=body.get("threshold"))
    if body.get("shape") == "state_change":
        return StateChangeRuleV1(
            header=header, column_ref=str(body.get("column_ref", "")),
            from_values=tuple(body.get("from_values") or ()),
            to_values=tuple(body.get("to_values") or ()),
            population_filter=str(body.get("population_filter", "from_values")),
            exclude_null_at_as_of=bool(body.get("exclude_null_at_as_of", True)))
    return EventWindowRuleV1(
        header=header, event_catalog=str(body.get("event_catalog", "")),
        event_table=str(body.get("event_table", "")),
        event_date_ref=str(body.get("event_date_ref", "")),
        join_left=str(body.get("join_left", "")), join_right=str(body.get("join_right", "")),
        aggregate=str(body.get("aggregate", "count")),
        event_filters=tuple(
            EventFilterV1(column_ref=str(f.get("column_ref", "")), op=str(f.get("op", "")),
                          value=f.get("value"), values=tuple(f.get("values") or ()),
                          value_ref=f.get("value_ref"))
            for f in (body.get("event_filters") or ())),
        measure_ref=body.get("measure_ref"),
        population_lookback_days=int(body.get("population_lookback_days", 0)),
        population_having=str(body.get("population_having", "any")))


@router.get("/targets/entities", dependencies=[Depends(require_feature_generate)])
def entities(catalog_source: str, conn: _Conn, identity: _Identity) -> list[dict]:
    """What this catalog can anchor a label on. An EMPTY list means it cannot anchor one at all —
    the client must say that rather than render a blank dropdown, which reads as a bug."""
    return selectable_entities(conn, catalog_source, roles=identity.role_claims)


@router.post("/targets/propose", dependencies=[Depends(require_feature_generate)])
def propose(body: ProposeIn, conn: _Conn, identity: _Identity, client: _LLM) -> dict:
    """Search FIRST, then propose. The two travel in separate keys: an existing label is a decision
    the organisation already made, a draft is a draft."""
    # The person chose the entity; the SERVER looks up its spine rather than trusting the client
    # to echo one back, and refuses an entity this catalog cannot anchor.
    spine = next((e for e in selectable_entities(conn, body.catalog_source,
                                                 roles=identity.role_claims)
                  if e["entity"] == body.entity.lower()), None)
    if spine is None:
        raise HTTPException(status_code=422, detail={
            "code": "ENTITY_NOT_ANCHORABLE",
            "message": f"{body.catalog_source} has no keyed spine table for {body.entity!r}"})
    as_of = conn.execute(
        "SELECT object_ref FROM graph_node WHERE kind = 'column' AND catalog_source = %s"
        "   AND table_name = %s AND is_as_of LIMIT 1",
        (body.catalog_source, spine["spine_table"])).fetchone()
    if as_of is None:
        raise HTTPException(status_code=422, detail={
            "code": "NO_AS_OF_COLUMN",
            "message": f"{spine['spine_table']} has no as-of column, so a forward window cannot "
                       "be measured from it"})

    existing = search_targets(conn, entity=body.entity, hypothesis=body.hypothesis)
    draft = propose_target_draft(
        conn, client, hypothesis=body.hypothesis, entity=body.entity,
        catalog_source=body.catalog_source, grain_ref=spine["spine_ref"], as_of_ref=as_of[0],
        roles=identity.role_claims, actor=identity)
    return {
        "existing": [{"name": e["name"], "description": e["description"],
                      "window_days": e["window_days"], "match_terms": list(e["match_terms"])}
                     for e in existing],
        "draft": None if draft is None else {
            "shape": draft.shape, "fields": draft.fields,
            "needs_input": list(draft.needs_input), "notes": draft.notes},
    }


@router.post("/targets/describe", dependencies=[Depends(require_feature_generate)])
def describe(body: DescribeIn) -> dict:
    """The rule as one plain sentence, so the FORM can show it while the person edits.

    Returning it only from registration — as an earlier draft of this plan did — gets the whole
    argument backwards: the sentence exists so a person approves a statement of MEANING rather than
    twelve fields, and one produced after they have committed approves nothing. Deterministic and
    model-free, so it cannot drift from the rule it renders.

    A rule too incomplete to construct has no meaning to state yet, which is an ordinary answer
    here rather than an error — the form is still being filled in.
    """
    try:
        return {"reads_as": describe_target(_rule_from_body(body.rule)), "incomplete": None}
    except (TargetContractError, TypeError, ValueError) as exc:
        return {"reads_as": None, "incomplete": str(exc)}


@router.post("/targets", dependencies=[Depends(require_feature_generate)])
def create_target(body: RegisterIn, conn: _Conn, identity: _Identity) -> dict:
    try:
        rule = _rule_from_body(body.rule)
    except (TargetContractError, TypeError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    reasons = check_target_against_catalog(conn, rule, roles=identity.role_claims)
    if reasons:
        raise HTTPException(status_code=422, detail={"reasons": list(reasons)})
    twins = near_duplicates(conn, rule)
    try:
        definition_id = register_target(
            conn, rule, description=body.description, registered_by=identity.subject,
            proposed_draft=body.proposed_draft, author_comment=body.author_comment,
            adapted_from=body.adapted_from)
    except TargetNameTaken as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"definition_id": definition_id, "name": rule.header.name,
            "rule": canonical_target(rule),
            # The statement of MEANING, not the field list — what a person actually approved.
            "reads_as": describe_target(rule),
            # Reported, never blocking: a twin may be deliberate, and the person has submitted.
            "near_duplicates": [{"name": t["name"], "differs_in": list(t["differs_in"])}
                                for t in twins]}


@router.get("/targets", dependencies=[Depends(require_feature_generate)])
def list_targets(entity: str, conn: _Conn, identity: _Identity) -> list[dict]:
    return targets_for_entity(conn, entity)
