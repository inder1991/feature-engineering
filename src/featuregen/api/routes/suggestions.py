"""The read-only suggested-features route, with per-table contract negotiation.

``GET /catalog/{catalog_source}/tables/{table}/suggestions`` exposes
:func:`overlay.upload.suggestions.suggest_features_page_v2` projected onto CONTRACT v4 — every
candidate this catalog can ground on the table with the gauntlet's own statuses, plus the
semantic engine's own verdicts for the same table. NO hypothesis, NO intent, NO LLM — and no
verb other than GET, because this surface WRITES NOTHING: there is deliberately no place here
from which a suggestion could be accepted, dismissed or governed.

Gated by ``catalog:read`` (``require_catalog_read``), like its sibling ``/catalog/...`` reads.

Why not ``feature:read``: this surface exists so a CURATOR can see what still needs curating on a
table they own — its empty states are data-owner to-dos ("these columns carry no business concept",
"this table has no confirmed as-of column"). ``data_owner`` holds ``catalog:read`` but deliberately
NOT ``feature:read``, a boundary pinned by ``test_data_owner_can_upload_but_not_read_features_or_
generate`` (``GET /features`` must 403 for them). Granting ``feature:read`` to reach this one page
would also hand over the feature registry, the contract reads and the lineage features layer — far
more than the problem needs. So the narrower change is here, on the route.

The trade, stated honestly: derived feature content is now readable via a ``catalog:read`` route.
That is defensible — these suggestions are DERIVED FROM the catalog and rendered on a catalog table
page, and nothing here can accept, dismiss or govern anything — but it does mean the route no longer
gates purely on content class. Revisit if a finer ``feature:suggest:read`` permission is ever added.

Read-scope is separate and mandatory: ``roles=identity.role_claims`` comes from the authenticated
session (never the request), so a column the caller may not see is not a grounding candidate and
cannot be suggested. The v2 response exposes ``read_scope_key`` — an opaque JCS hash of the caller's
visibility CLASSES — for diagnostics and later cursor binding; it never echoes role claims and is
never itself authorization.

**Contract negotiation (freeze 0F-12).** ``contract_version`` is deliberately UNBOUNDED at the
FastAPI layer: a ``le=4`` would make the framework reject an unsupported version before the
handler ran, so the typed ``error_code`` below could never be emitted and the body would be
FastAPI's list-``detail`` instead. The HANDLER validates membership and owns the refusal. A
NON-INTEGER value is a different thing — a type failure caught before any handler code runs — and
keeps FastAPI's native validation error, deliberately outside the typed contract, because faking a
code we never produced would be worse than an honest framework error.

**E4 cutover (2026-08-14): v4 is the ONLY contract.** Versions 1, 2 and 3 are DELETED, not
deprecated — asking for one earns the same typed 422 as asking for 99, and the message names the
set this deployment serves (``[4]``). Nothing is renumbered: v4 keeps its number and its shape, so
a client that already speaks it needs no change, and a client that spoke v1/v2/v3 gets a refusal
that tells it exactly what to ask for instead of a body it would silently mis-read. The default is
v4 for the same reason — an omitted version must resolve to something this deployment can serve.

**One snapshot.** The v2 page is a multi-statement read (resolve, neighbourhood, ground, context),
and its counts, groups, neighbourhood metadata and revision ids must all describe ONE catalog state.
It therefore runs on the app's existing ``REPEATABLE READ`` request connection, with one
transaction-scoped statement timeout bound before any read.

**Response models.** The Pydantic models below mirror the wire forms produced by
``overlay.upload.suggestion_contract`` — which remains the single source of field truth — so OpenAPI
publishes both contracts and the typed error. They are validated against REAL bodies with unknown
keys FORBIDDEN, so a contract field that never reaches the wire, or a wire field the schema never
published, fails a test rather than misleading a client.
"""

from __future__ import annotations

import logging
import time
from typing import Annotated, Any

import psycopg
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import JSONResponse
from psycopg import sql
from pydantic import BaseModel, ConfigDict

from featuregen.api.deps import get_feature_gen_conn, get_identity, require_catalog_read
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.upload.feature_metadata_snapshot import (
    CatalogProjectionUnavailable,
)
from featuregen.overlay.upload.join_path import MAX_HOPS_CEILING, MAX_HOPS_DEFAULT
from featuregen.overlay.upload.suggestion_contract import (
    page_to_json_v3,
    unknown_table_page_v2,
)
from featuregen.overlay.upload.suggestions import suggest_features_page_v2

logger = logging.getLogger(__name__)

router = APIRouter()
_RRConn = Annotated[psycopg.Connection, Depends(get_feature_gen_conn, scope="function")]
_Identity = Annotated[IdentityEnvelope, Depends(get_identity)]

# One request grounds the WHOLE template registry (~157 recipes) against THIS TABLE's columns, so its
# cost is O(recipes × table columns) — constant in the number of tables the catalog holds. It used to
# ground catalog-wide and filter, which scaled with catalog WIDTH (a 3-table/19-column fixture issued
# ~1.7k SELECTs for one page; the same page is ~0.4k per-table). It is still hundreds of statements on
# a wide TABLE, and a slow one must fail FAST (a 500 the caller sees) rather than pin a connection for
# minutes. 30s is well above any measured run here and well below any sane client timeout; change this
# one constant to move it. Bound ONCE per request and transaction-scoped, so it covers every statement
# of the multi-query v2 assembly as well as the grounding pass.
SUGGESTIONS_STATEMENT_TIMEOUT_MS = 30_000

#: The contract versions this deployment serves — exactly one, since the E4 cutover.
SUPPORTED_CONTRACT_VERSIONS: tuple[int, ...] = (4,)

#: Closed machine-readable error codes emitted by HANDLER-level checks, where the body shape is
#: actually controllable. Framework validation errors are deliberately not in this vocabulary.
SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION = "SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION"


# ── error contract ──────────────────────────────────────────────────────────────────────────────
class SuggestionsErrorResponse(BaseModel):
    """The typed error body: FastAPI's ``detail`` envelope (the platform-wide convention) plus one
    machine field. Emitted only by handler-level checks."""

    model_config = ConfigDict(extra="forbid")

    detail: str
    error_code: str


class _Model(BaseModel):
    """Unknown keys are FORBIDDEN so the published schema and the real body cannot drift apart in
    either direction — an unpublished field fails validation, a published-but-absent one fails as a
    missing field."""

    model_config = ConfigDict(extra="forbid")


# ── shared wire mirrors ─────────────────────────────────────────────────────────────────────────
class JoinNeighbourhoodResponse(_Model):
    """What the bounded join widening left out. A screen showing a SUBSET must say so."""

    tables_considered: int
    tables_available: int
    truncated: bool
    max_hops: int
    limit_reason: str | None


class RequirementResponse(_Model):
    """``contract._serial.requirements_to_json``: the base three keys always, with ``params`` and a
    non-default ``schema_version`` emitted ADDITIVELY so a no-param requirement's JSON stays
    byte-identical to what it has always been."""

    code: str
    operand: list[str]
    detail: str
    params: list[list[Any]] | None = None
    schema_version: str | None = None


class RecipePartsResponse(_Model):
    operation: str
    measures: list[str]
    grain: str
    window: str
    time: str


# ── v2 wire mirror ──────────────────────────────────────────────────────────────────────────────
class EvidenceAuthorityResponse(_Model):
    producer: str
    strength: str
    lifecycle: str
    producer_ref: str | None
    evidence_id: str | None


class AttributedLabelResponse(_Model):
    """A CONTROLLED registry id with its provenance. Every evidence axis travels: a card that
    dropped them would render an LLM proposal and a human attestation identically."""

    id: str
    display_name: str
    basis: str
    evidence: list[EvidenceAuthorityResponse]
    operational_influence: str | None
    source_refs: list[str]


class AttributedTextResponse(_Model):
    """Attributed free text — displayable and searchable, never a facet id."""

    value: str
    basis: str
    evidence: list[EvidenceAuthorityResponse]
    operational_influence: str | None
    source_refs: list[str]


class SuggestionOperandResponse(_Model):
    catalog_source: str
    logical_ref: str
    graph_object_ref: str
    table_ref: str
    recipe_role: str
    classification: str
    visibility_requires_current: list[str]
    evidence_refs: list[str]


class SuggestionSourceDatasetResponse(_Model):
    """``profile_status`` is the explicit unavailability marker while the profile plan is unlanded:
    the descriptive roles are ``null`` because nobody has decided them, not because the dataset has
    none."""

    catalog_source: str
    table_ref: str
    data_role: AttributedLabelResponse | None
    authority_role: AttributedLabelResponse | None
    temporal_storage_model: AttributedLabelResponse | None
    primary_entity: AttributedLabelResponse | None
    dataset_profile_hash: str | None
    profile_status: str


class SuggestionRelationshipDependencyResponse(_Model):
    relationship_ref: str
    relationship_kind: str
    from_ref: list[str]
    to_ref: list[str]
    realization_content_hash: str | None
    cardinality: str
    safety_status: str
    review_status: str


class SuggestionWarningResponse(_Model):
    """A CLOSED code plus the operands it concerns. ``detail`` renders the code; it is never an
    alternative decision field."""

    code: str
    operand_refs: list[list[str]]
    detail: str


class FeatureSuggestionV2Response(_Model):
    schema_version: str
    suggestion_id: str
    suggestion_revision_id: str
    generation_source: str
    template_id: str | None
    recipe_revision_id: str | None
    discovery_metadata_revision_id: str | None
    validation_rule_content_hashes: list[str]
    read_scope_rule_content_hashes: list[str]
    name: str
    display_name: str
    business_interpretation: AttributedTextResponse | None
    business_value: AttributedTextResponse | None
    feature_category: AttributedLabelResponse | None
    #: The provenance-badge signal a consumer must NOT try to read off ``basis``, which says
    #: ``template_authored`` for a taxonomy-derived category as well as for an authored value.
    feature_category_derived_from_family_mapping: bool
    discovery_disposition: str
    recipe_family: AttributedLabelResponse | None
    business_domains: list[AttributedLabelResponse]
    contextual_domain_terms: list[AttributedTextResponse]
    use_cases: list[AttributedLabelResponse]
    keywords: list[AttributedTextResponse]
    entity: AttributedLabelResponse | None
    contextual_entity_terms: list[AttributedTextResponse]
    grain_refs: list[list[str]]
    operation_kind: str
    window: str | None
    time_ref: list[str] | None
    recipe: str
    recipe_parts: RecipePartsResponse
    #: The remaining AUTHORED recipe declarations. `null` means the SME wrote none — silence, not
    #: an empty value.
    recipe_stage: AttributedTextResponse | None
    eligibility_note: AttributedTextResponse | None
    authoring_notes: list[AttributedTextResponse]
    output_additivity: AttributedTextResponse | None
    point_in_time_declaration: AttributedTextResponse | None
    source_datasets: list[SuggestionSourceDatasetResponse]
    operands: list[SuggestionOperandResponse]
    relationship_dependencies: list[SuggestionRelationshipDependencyResponse]
    validation_status: str
    requirements: list[RequirementResponse]
    warnings: list[SuggestionWarningResponse]
    binding_quality: str
    semantic_context_hashes: list[str]
    dataset_profile_hashes: list[str]
    grounding_trace_content_hash: str


class SuggestionBuildProvenanceResponse(_Model):
    """The exact ids a reader compares for currentness — and which are excluded from every semantic
    hash for exactly that reason."""

    scope_set_id: str | None
    metadata_snapshot_ids: list[str]
    dependency_revision_ids: list[str]
    evidence_event_ids: list[str]
    relationship_realization_revision_ids: list[str]
    producer_commit: str | None
    refresh_id: str | None
    generated_at: str | None


class SuggestionProjectionStateResponse(_Model):
    state: str
    scope_set_id: str | None
    read_scope_key: str
    scope_epoch: int
    target_fingerprint: str
    current_fingerprint: str | None
    generated_at: str | None
    stale_reason: str | None
    omitted_counts: dict[str, int]


class FeatureSuggestionHitResponse(_Model):
    suggestion: FeatureSuggestionV2Response
    projection: SuggestionProjectionStateResponse | None
    provenance: SuggestionBuildProvenanceResponse


class SuggestionSummaryV2Response(_Model):
    """``design_checked`` replaces the misleading V1 ``clean_ready``: design checking is not proof
    of predictive usefulness or production readiness."""

    suggested: int
    design_checked: int
    needs_external_validation: int
    groups: int


class SuggestionGroupV2Response(_Model):
    entity: AttributedLabelResponse | None
    contextual_entity_terms: list[AttributedTextResponse]
    grain_refs: list[list[str]]
    suggestion_ids: list[str]


class SuggestionRejectionV2Response(_Model):
    template_id: str | None
    candidate_name: str
    code: str
    explanation: str


class SuggestionCollectionContextV2Response(_Model):
    """Everything about THIS READING — anchor, counts, refusals and what was left out. None of it
    enters a suggestion revision."""

    anchor_catalog_source: str | None
    anchor_table_ref: str | None
    anchor_column_ref: str | None
    table_known: bool | None
    summary: SuggestionSummaryV2Response
    groups: list[SuggestionGroupV2Response]
    rejections: list[SuggestionRejectionV2Response]
    neighbourhood: JoinNeighbourhoodResponse | None
    omitted_counts: dict[str, int]


class FacetBucketResponse(_Model):
    id: str
    display_name: str
    count: int


class ReadinessBlockerV3Response(_Model):
    """One named fact about why a suggestion is not further up the execution ladder. ``group`` is
    BR-8's display taxonomy (data_meaning / time / currency / relationship / formula_capability /
    governance / execution); ``code`` is the machine vocabulary the audit drawer keeps."""

    code: str
    group: str


class ReplacementReadinessV3Response(_Model):
    """One atomic V2 replacement's own execution state on a v3 card."""

    recipe_id: str
    execution_readiness: str
    computation_kind: str


class ExecutionBlockV3Response(_Model):
    """Contract v3's additive truthfulness block: what this card IS, execution-wise. UNASSESSED
    is the legacy adapter's honest word for "nobody decided yet" — an idea, never a failure and
    never readiness. Design checking (``validation_status``) stays a SEPARATE axis."""

    recipe_contract_version: str
    computation_kind: str
    execution_readiness: str
    readiness_blockers: list[ReadinessBlockerV3Response]
    binding_ambiguity: bool
    #: BR-17: the atomic V2 recipe ids replacing this suggestion's legacy template — the split
    #: made visible; empty only on the legacy fallback.
    v2_replacements: list[str]
    #: True when the legacy template maps to SEVERAL atomic outputs the user has not chosen
    #: between: ``execution_readiness`` is then the best atom's state — a CEILING, not a
    #: property of this card — and the choice belongs to the user, never the serializer.
    output_selection_required: bool
    #: Each replacement's OWN readiness (alias-map order), so no atom's state is inherited
    #: silently by its siblings.
    replacement_readiness: list[ReplacementReadinessV3Response]


class FeatureSuggestionV3Response(FeatureSuggestionV2Response):
    """The v2 suggestion plus its execution block — strictly additive by construction."""

    execution: ExecutionBlockV3Response


class FeatureSuggestionHitV3Response(FeatureSuggestionHitResponse):
    suggestion: FeatureSuggestionV3Response  # type: ignore[assignment]


class FeatureSuggestionPageV2Response(_Model):
    """Release A serves ``read_mode='on_demand'`` with no projection state and no facets; Release B
    adds the projected mode, the search facets and the cursor."""

    read_mode: str
    read_scope_key: str
    projection: SuggestionProjectionStateResponse | None
    collection: SuggestionCollectionContextV2Response
    hits: list[FeatureSuggestionHitResponse]
    facets: dict[str, list[FacetBucketResponse]]
    next_cursor: str | None


class FeatureSuggestionPageV3Response(FeatureSuggestionPageV2Response):
    """The v3 page: the v2 page plus its own declared version, execution blocks on every hit and
    the page-level readiness tally — additive, never a reshuffle."""

    contract_version: int
    hits: list[FeatureSuggestionHitV3Response]  # type: ignore[assignment]
    readiness_counts: dict[str, int]
    #: How many hits carry a ceiling readiness pending an output choice (multi-output aliases).
    output_selection_required_count: int


class SemanticEngineVerdictV4Response(_Model):
    role: str
    status: str
    selected_ref: str | None
    reason_codes: list[str]
    resolution: str


class SemanticEngineCorroborationV4Response(_Model):
    origin: str
    source_definition_id: str


class SemanticEngineEntryV4Response(_Model):
    """One engine candidate anchored to this table. ``card`` is the SHARED option carrier (D4,
    UI-05) — the same projected FeatureIdea the Workbench renders, serialized by gate1's own
    function — so it is published as a free-form object rather than re-declared here, which
    would be a second copy of the card model and could drift from the one that ships."""

    card: dict[str, Any] | None
    recipe_id: str
    binding_state: str
    readiness: str
    review_current: bool
    planning_request_hash: str
    verdicts: list[SemanticEngineVerdictV4Response]
    corroborations: list[SemanticEngineCorroborationV4Response]


class SuggestionSemanticBlockV4Response(_Model):
    """SE-13's parity block: the engine's own answer for this table, over the same frozen
    context the hypothesis Workbench plans against."""

    semantic_context_hash: str
    table: str
    ranked: list[SemanticEngineEntryV4Response]
    actionable: list[SemanticEngineEntryV4Response]
    order_basis: str


class FeatureSuggestionPageV4Response(FeatureSuggestionPageV3Response):
    """The ONLY contract this deployment serves: the v3 execution-truthfulness page plus the
    engine's verdicts. ``contract_version`` is redeclared as 4 — the number is kept, never
    renumbered, so a client already speaking v4 is unaffected by v1–v3's retirement."""

    contract_version: int
    semantic: SuggestionSemanticBlockV4Response


#: Documentation-only: the handler returns plain dicts, so declaring these here publishes the
#: contract and the typed error in OpenAPI WITHOUT letting FastAPI re-serialize the body through a
#: model (which would put the payload's byte stability at the mercy of a schema edit).
_RESPONSES: dict[int | str, dict[str, Any]] = {
    200: {"model": FeatureSuggestionPageV4Response,
          "description": "The v4 page: the execution-truthfulness page plus the semantic "
                         "engine's own verdicts for this table."},
    422: {"model": SuggestionsErrorResponse,
          "description": "An integer `contract_version` outside the supported set — including "
                         "the retired 1, 2 and 3. A non-integer value keeps FastAPI's own "
                         "validation error, whose `detail` is a list."},
}


@router.get("/catalog/{catalog_source}/tables/{table}/suggestions",
            dependencies=[Depends(require_catalog_read)],
            response_model=None, responses=_RESPONSES)
def table_suggestions(
    catalog_source: str, table: str, conn: _RRConn, identity: _Identity,
    max_hops: Annotated[int, Query(ge=1, le=MAX_HOPS_CEILING)] = MAX_HOPS_DEFAULT,
    contract_version: Annotated[int, Query()] = 4,
) -> Any:
    """This table's suggested features. A table with no suggestions returns the honest empty payload
    — that is a catalog-readiness fact, not a server error — and one this catalog does not hold is
    reported distinctly as ``table_known: false``, never as "no concepts".

    ``max_hops`` is the EXPLICIT opt-in for a wider join neighbourhood. Its default is the capped
    one an automatic page load gets (``MAX_HOPS_DEFAULT``); FastAPI's own bounds refuse anything
    above ``MAX_HOPS_CEILING`` with a 422, so a request cannot ask the server for an unbounded walk.
    Raising it changes which tables are ELIGIBLE to widen into, never how many are admitted — the
    table cap and column budget still apply — so the page's cost stays bounded either way. Choosing
    WHICH deeper join path to follow is a governed, explicit act that wants its own picker; that UI
    is DEFERRED, and this parameter is the surface it will use.

    ``contract_version`` selects the payload contract — there is exactly one to select since the
    E4 cutover — and the refusal below is the FIRST thing the handler does: an unsupported version
    must not ground a registry, bind a timeout or touch the catalog."""
    if contract_version not in SUPPORTED_CONTRACT_VERSIONS:
        return JSONResponse(status_code=422, content={
            "detail": f"unsupported contract_version {contract_version}; this deployment serves "
                      f"{list(SUPPORTED_CONTRACT_VERSIONS)}",
            "error_code": SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION})
    # E4 (2026-08-14): there is one contract left to serve, so the only gate above is membership
    # in the closed SUPPORTED set — no rollback lever, no per-version branch.
    # SET LOCAL is transaction-scoped (the request connection owns the txn), so the bound dies with
    # the request and covers every statement of the read — including the multi-query assembly.
    # SET takes no bound parameters, so the literal is composed — the profiler's own precedent.
    conn.execute(sql.SQL("SET LOCAL statement_timeout = {}")
                 .format(sql.Literal(SUGGESTIONS_STATEMENT_TIMEOUT_MS)))
    started = time.monotonic()
    # Suggestions are computed ON DEMAND, and the gauntlet reads GOVERNED values (grain, join
    # authority) via `_governed_read`, which fails closed on a lagged projection rather than reason
    # from a value it knows is stale. That refusal is correct; what it must NOT do is leave as an
    # opaque 500. Mapped to the same retryable 503 `contract.py` already uses — the mapping
    # `_governed_read`'s docstring promises — so the caller learns this is temporary and retryable.
    try:
        page = suggest_features_page_v2(conn, catalog_source=catalog_source, table=table,
                                        roles=identity.role_claims, max_hops=max_hops)
        collection = page.collection
        logger.info(
            "suggestions v4 for %s.%s took %.3fs (known=%s, hits=%s, hops=%s, omitted=%s)",
            catalog_source, table, time.monotonic() - started,
            collection.table_known, len(page.hits), max_hops,
            dict(collection.omitted_counts))
        body = page_to_json_v3(page)
        # SE-13: v4 = the execution-truthfulness page + the ENGINE's verdicts for this table —
        # the same lens the hypothesis Workbench serves from, over the same frozen context, so
        # the two surfaces cannot disagree about a binding's validity.
        from featuregen.overlay.upload.suggestions import semantic_parity_block

        body["contract_version"] = 4
        body["semantic"] = semantic_parity_block(
            conn, catalog_source=catalog_source, table=table, roles=identity.role_claims)
    except CatalogProjectionUnavailable as e:
        raise HTTPException(status_code=503, detail=e.detail) from e
    return body


__all__ = ["FeatureSuggestionPageV4Response", "SuggestionsErrorResponse", "router",
           "suggest_features_page_v2", "table_suggestions", "unknown_table_page_v2"]
