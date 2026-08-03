"""``FeatureSuggestionV2`` — one typed suggestion carrying enough meaning for every UI surface.

This module is the ADAPTER between the grounding engine's result and the discovery contract. It
creates no grounding, no validation, no join safety and no authority: every field is either read
verbatim off the engine's own output, resolved through an existing controlled registry, or
explicitly absent.

**What it consumes, and what it refuses.** The gauntlet already emitted a
:class:`~featuregen.overlay.upload.grounding_trace.GroundingDecisionTraceV1` at the decision points
(Task 2A). This module CONSUMES it — for identity material, for the relationship dependencies, for
the exact rules that were evaluated and for the operand-level evidence pins — and never
reconstructs one, never re-runs path selection and never infers a validation dependency from the
final status or from prose. A ``DESIGN_CHECKED`` candidate whose trace is absent or incomplete is
INADMISSIBLE: it is a readiness claim nothing could justify or invalidate, so it is withheld and
counted, not softened into a card.

**Anchor independence.** The requested table, the join-neighbourhood bounds and page truncation
live on :class:`FeatureSuggestionPageV2` / :class:`SuggestionCollectionContextV2`. Nothing
anchor-shaped enters :class:`FeatureSuggestionV2`, so opening the same cross-table candidate from
either of its operand tables yields the identical suggestion and revision bytes.

**Read scope is revalidated, not trusted.** One bounded statement per catalog source re-reads every
bound operand and every relationship endpoint under the CALLER's own visibility classes. A ref that
does not come back is hidden or gone, and the WHOLE suggestion is withheld — never a partial card
with a caller-specific identity, and never a leak through the counts, the groups or the facets.

**What is explicitly unavailable in Release A** (D9; the semantic/profile plans own these):
``business_domains`` stays empty unless a CONTROLLED resolver is registered on the
``featuregen.contracts.resolvers`` seam; ``semantic_context_hashes`` and ``dataset_profile_hashes``
are empty; every :class:`SuggestionSourceDatasetV1` field but catalog/table is ``None`` behind an
explicit ``profile_status='unavailable'``. Free-text catalog domain/entity wording travels as
``AttributedTextV1`` — displayable and searchable, never lowercased or slugged into a facet id.
When the semantic plan registers its resolvers, the SAME call sites start yielding controlled
labels with no change here.
"""
from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime

from featuregen.contracts.evidence_axes import (
    AssertionStrength,
    AttributedLabelV1,
    AttributedTextV1,
    EvidenceAuthorityV1,
    EvidenceLifecycle,
    EvidenceProducer,
    attributed_label_to_json,
    attributed_text_to_json,
)
from featuregen.contracts.resolvers import resolve_or_text
from featuregen.overlay.upload.concepts import concept
from featuregen.overlay.upload.contract._serial import requirements_to_json
from featuregen.overlay.upload.grounding_trace import (
    CARDINALITY_UNKNOWN,
    REVIEW_FILE_DECLARED,
    SAFETY_CLEARING,
    GroundingDecisionTraceV1,
    SuggestionRelationshipDependencyV1,
    column_dependency_key,
    trace_completeness_gaps,
)
from featuregen.overlay.upload.join_path import JoinNeighbourhood, table_of_ref
from featuregen.overlay.upload.read_scope import allowed_classes
from featuregen.overlay.upload.suggestion_identity import (
    build_read_scope,
    dependency_content_hashes,
    join_path_assignment,
    suggestion_id,
    suggestion_revision_id,
)
from featuregen.overlay.upload.suggestion_taxonomy import (
    FEATURE_CATEGORY_REGISTRY,
    RECIPE_FAMILY_REGISTRY,
)
from featuregen.overlay.upload.taxonomy.use_cases import USE_CASE_REGISTRY
from featuregen.overlay.upload.template_discovery import (
    DISCOVERY_METADATA,
    FAMILY_MAPPING_CITATION_PREFIX,
    RECIPE_REVISION_CITATION_PREFIX,
    DiscoveryControlledAssignmentV1,
    DiscoveryTextAssignmentV1,
    discovery_metadata_revision_id,
)
from featuregen.overlay.upload.templates import ALL_TEMPLATES

__all__ = [
    "GENERATION_SOURCES",
    "MAX_CONTEXTUAL_DOMAIN_TERMS",
    "MAX_CONTEXTUAL_ENTITY_TERMS",
    "MAX_EVIDENCE_REFS",
    "MAX_KEYWORDS",
    "MAX_OPERANDS",
    "MAX_RELATIONSHIP_DEPENDENCIES",
    "MAX_USE_CASES",
    "OPERAND_CLASSIFICATIONS",
    "PROFILE_STATUS_UNAVAILABLE",
    "PROJECTION_STATES",
    "READ_MODES",
    "SCHEMA_VERSION",
    "V1_UNSAFE_OMISSION_KEYS",
    "WARNING_CODES",
    "FacetBucketV1",
    "FeatureSuggestionHitV2",
    "FeatureSuggestionPageV2",
    "FeatureSuggestionV2",
    "RecipePartsV2",
    "SuggestionBuildProvenanceV1",
    "SuggestionCollectionContextV2",
    "SuggestionGroupV2",
    "SuggestionOperandV1",
    "SuggestionProjectionStateV1",
    "SuggestionRejectionV2",
    "SuggestionSourceDatasetV1",
    "SuggestionSummaryV2",
    "SuggestionV1ReconstructionError",
    "SuggestionWarningV1",
    "build_page_v2",
    "is_family_derived_category",
    "page_to_json",
    "recipe_parts",
    "recipe_parts_to_json",
    "render_recipe",
    "to_table_suggestions_v1",
    "unknown_table_page_v2",
]

#: The contract label every V2 suggestion carries (0F-9).
SCHEMA_VERSION = "feature-suggestion-v2"

#: Release A serves ``on_demand``; Release B's projection serves ``projected``.
READ_MODES = frozenset({"on_demand", "projected"})

#: The existing SERVER-stamped generation vocabulary. This surface emits only ``recipe``: it has no
#: hypothesis, no LLM and no user-authored idea, and projecting a free-form or user-defined idea
#: into global discovery needs a separately designed lifecycle (plan scope).
GENERATION_SOURCES = frozenset({"recipe", "llm_freeform", "user_defined"})
PRODUCER_GENERATION_SOURCE = "recipe"

#: What an operand IS to the computation — read from the engine's typed operand refs, never guessed
#: from a name, a concept or a data type.
OPERAND_CLASSIFICATIONS = frozenset({"measure", "grain", "time", "grouping", "other"})

#: The CLOSED warning vocabulary (0F-9). Prose renders a code; it is never an alternative decision
#: field. Staleness is projection state and is deliberately absent here.
WARNING_CODES = frozenset({
    "NEAR_LABEL", "SENSITIVE_INPUT", "MISSING_TEMPORAL_EVIDENCE", "MISSING_UNIT",
    "MISSING_CURRENCY", "RELATIONSHIP_UNCONFIRMED", "RELATIONSHIP_SAFETY_UNPROVEN",
    "DIRECTIONAL_CARDINALITY_UNAVAILABLE", "PROFILE_PROPOSED",
})

#: The projection lifecycle (Release B). Release A always reports ``projection=None``.
PROJECTION_STATES = frozenset({"current", "stale", "pending", "partial", "failed", "retired"})

#: The explicit "the profile plan has not landed" marker for a source dataset (D9).
PROFILE_STATUS_UNAVAILABLE = "unavailable"

#: Which typed requirement raises which warning. Derived from the CODE — never from the detail
#: prose, which is a rendering.
_WARNING_BY_REQUIREMENT: dict[str, str] = {
    "TEMPORAL_IS_POPULATED": "MISSING_TEMPORAL_EVIDENCE",
    "UNIT_CONSISTENT": "MISSING_UNIT",
    "CURRENCY_CONSISTENT": "MISSING_CURRENCY",
}

# ── bounds. Every one reports what it left out in `collection.omitted_counts` (plan: report omitted
#    counts rather than silently truncating). The operand bound is the one that would make the V1
#    reconstruction a LIE, so it is also listed in V1_UNSAFE_OMISSION_KEYS below. ────────────────
MAX_BUSINESS_DOMAINS = 8
MAX_CONTEXTUAL_DOMAIN_TERMS = 8
MAX_USE_CASES = 8
MAX_KEYWORDS = 8
MAX_CONTEXTUAL_ENTITY_TERMS = 8
MAX_OPERANDS = 32
MAX_RELATIONSHIP_DEPENDENCIES = 16
MAX_EVIDENCE_REFS = 16

#: Omission keys whose presence means a V1 payload rebuilt from this page would be FALSE — a
#: shortened ``uses`` list, or a card the V1 producer would have emitted and this page withheld.
#: :func:`to_table_suggestions_v1` refuses rather than emit a quietly different V1 body.
V1_UNSAFE_OMISSION_KEYS: tuple[str, ...] = (
    "operands",
    "withheld_read_scope",
    "withheld_missing_trace",
    "withheld_incomplete_trace",
    "withheld_non_recipe_generation_source",
)


class SuggestionV1ReconstructionError(RuntimeError):
    """This page cannot be rendered as V1 without lying about what it contains."""


# ── value objects ───────────────────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class RecipePartsV2:
    """The rendered recipe line's pieces, structured — field-identical to V1's parts block (0F-9).
    V2 adds nothing here: operands, grain, window and time anchor travel TYPED at the suggestion
    level, so duplicating them as strings would create two answers to one question."""

    operation: str
    measures: tuple[str, ...]
    grain: str
    window: str
    time: str


@dataclass(frozen=True, slots=True)
class SuggestionOperandV1:
    """One bound column, in the engine's own binding order.

    ``recipe_role`` is the TEMPLATE AUTHOR's declared role, read off the grounding context's need
    bindings — never inferred from a concept (concepts are AI-proposed), a column name or a type.
    ``visibility_requires_current`` is re-read at request time under the caller's scope, which is
    what makes a hidden operand withhold its whole suggestion instead of quietly vanishing from a
    card. ``evidence_refs`` are the exact provenance pins the gauntlet recorded for this operand.
    """

    catalog_source: str
    logical_ref: str
    graph_object_ref: str
    table_ref: str
    recipe_role: str
    classification: str
    visibility_requires_current: tuple[str, ...]
    evidence_refs: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SuggestionSourceDatasetV1:
    """The datasets this suggestion reads, with their descriptive roles.

    These are DISCOVERY/EXPLANATION fields. Source selection, "latest master" row policy and join
    safety are owned by the profile and link plans and are never derived from anything here. In
    Release A every field but catalog/table is honestly unavailable (D9): the profile plan has not
    landed, and a plausible guess would be indistinguishable from a governed answer.
    """

    catalog_source: str
    table_ref: str
    data_role: AttributedLabelV1 | None
    authority_role: AttributedLabelV1 | None
    temporal_storage_model: AttributedLabelV1 | None
    primary_entity: AttributedLabelV1 | None
    dataset_profile_hash: str | None
    profile_status: str


@dataclass(frozen=True, slots=True)
class SuggestionWarningV1:
    """One limitation, as a CLOSED code plus the operands it concerns. ``detail`` renders the code;
    a reader never parses truth out of it."""

    code: str
    operand_refs: tuple[tuple[str, str], ...]
    detail: str

    def __post_init__(self) -> None:
        if self.code not in WARNING_CODES:
            raise ValueError(
                f"unknown suggestion warning code {self.code!r}; the closed vocabulary is "
                f"{sorted(WARNING_CODES)}")


@dataclass(frozen=True, slots=True)
class FacetBucketV1:
    id: str
    display_name: str
    count: int


@dataclass(frozen=True, slots=True)
class FeatureSuggestionV2:
    """One suggested feature, complete enough to explain itself without opening source code.

    Anchor-free by construction: nothing here names the screen it was read from. Projection state
    and timestamps are deliberately outside it, so the SAME revision can move from current to stale
    without its canonical bytes changing.
    """

    schema_version: str
    suggestion_id: str
    suggestion_revision_id: str
    generation_source: str

    template_id: str | None
    recipe_revision_id: str | None
    discovery_metadata_revision_id: str | None
    validation_rule_content_hashes: tuple[str, ...]
    read_scope_rule_content_hashes: tuple[str, ...]
    name: str
    display_name: str
    business_interpretation: AttributedTextV1 | None
    business_value: AttributedTextV1 | None

    feature_category: AttributedLabelV1 | None
    discovery_disposition: str
    recipe_family: AttributedLabelV1 | None
    business_domains: tuple[AttributedLabelV1, ...]
    contextual_domain_terms: tuple[AttributedTextV1, ...]
    use_cases: tuple[AttributedLabelV1, ...]
    keywords: tuple[AttributedTextV1, ...]

    entity: AttributedLabelV1 | None
    contextual_entity_terms: tuple[AttributedTextV1, ...]
    grain_refs: tuple[tuple[str, str], ...]
    operation_kind: str
    window: str | None
    time_ref: tuple[str, str] | None
    recipe: str
    recipe_parts: RecipePartsV2

    source_datasets: tuple[SuggestionSourceDatasetV1, ...]
    operands: tuple[SuggestionOperandV1, ...]
    relationship_dependencies: tuple[SuggestionRelationshipDependencyV1, ...]
    validation_status: str
    requirements: tuple[object, ...]           # feature_assist.Requirement (D1)
    warnings: tuple[SuggestionWarningV1, ...]
    binding_quality: str

    semantic_context_hashes: tuple[str, ...]
    dataset_profile_hashes: tuple[str, ...]
    grounding_trace_content_hash: str


@dataclass(frozen=True, slots=True)
class SuggestionBuildProvenanceV1:
    """The exact ids a reader needs to compare CURRENTNESS — and which are excluded from every
    semantic hash for exactly that reason (rule 24)."""

    scope_set_id: str | None
    metadata_snapshot_ids: tuple[str, ...]
    dependency_revision_ids: tuple[str, ...]
    evidence_event_ids: tuple[str, ...]
    relationship_realization_revision_ids: tuple[str, ...]
    producer_commit: str | None
    refresh_id: str | None
    generated_at: datetime | None


@dataclass(frozen=True, slots=True)
class SuggestionProjectionStateV1:
    """Release-B projection currentness. Release A has no projection and reports ``None`` rather
    than inventing a ``current`` it cannot prove."""

    state: str
    scope_set_id: str | None
    read_scope_key: str
    scope_epoch: int
    target_fingerprint: str
    current_fingerprint: str | None
    generated_at: datetime | None
    stale_reason: str | None
    omitted_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class FeatureSuggestionHitV2:
    suggestion: FeatureSuggestionV2
    projection: SuggestionProjectionStateV1 | None
    provenance: SuggestionBuildProvenanceV1


@dataclass(frozen=True, slots=True)
class SuggestionSummaryV2:
    """V1's counts, with ``clean_ready`` renamed to what it actually means. ``groups`` keeps V1's
    ``entities`` semantics exactly: the number of NAMED grain buckets — the label-less bucket is
    not an entity, and counting it would claim the catalog attested one."""

    suggested: int
    design_checked: int
    needs_external_validation: int
    groups: int


@dataclass(frozen=True, slots=True)
class SuggestionRejectionV2:
    """A refused candidate, carrying the template the engine already knew — never recovered by
    matching on the rendered name."""

    template_id: str | None
    candidate_name: str
    code: str
    explanation: str


@dataclass(frozen=True, slots=True)
class SuggestionGroupV2:
    entity: AttributedLabelV1 | None
    contextual_entity_terms: tuple[AttributedTextV1, ...]
    grain_refs: tuple[tuple[str, str], ...]
    suggestion_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SuggestionCollectionContextV2:
    """Everything about THIS READING of the collection: which anchor was asked for, what was
    counted, what was refused and what was left out. None of it enters a suggestion revision."""

    anchor_catalog_source: str | None
    anchor_table_ref: str | None
    anchor_column_ref: str | None
    table_known: bool | None
    summary: SuggestionSummaryV2
    groups: tuple[SuggestionGroupV2, ...]
    rejections: tuple[SuggestionRejectionV2, ...]
    neighbourhood: JoinNeighbourhood | None
    omitted_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class FeatureSuggestionPageV2:
    read_mode: str
    read_scope_key: str
    projection: SuggestionProjectionStateV1 | None
    collection: SuggestionCollectionContextV2
    hits: tuple[FeatureSuggestionHitV2, ...]
    facets: Mapping[str, tuple[FacetBucketV1, ...]]
    next_cursor: str | None


# ── the recipe line (ONE producer, shared by V1 and V2) ─────────────────────────────────────────
def render_recipe(idea, entity_ref: str) -> str:
    """The one-line recipe this feature computes, e.g. ``trend_90d(bal_amt) BY cif_id OVER 90d
    [as_of_dt]``.

    ``operation_kind`` is printed AS BOUND. It is a DOMAIN label (``trend``, ``inflow_outflow``,
    ``frequency_trend`` — 156 of them), NOT a SQL verb: there is no label -> verb map here, because
    inventing one would print ``AVG(...)`` for an operation the system calls ``trend``. Clauses that
    do not apply are omitted, never emitted empty."""
    parts = recipe_parts(idea, entity_ref)
    measures = ", ".join(parts.measures)
    line = f"{parts.operation}({measures})" if parts.operation else measures
    if parts.grain:
        line += f" BY {parts.grain}"
    if parts.window:
        line += f" OVER {parts.window}"
    if parts.time:
        line += f" [{parts.time}]"
    return line


def recipe_parts(idea, entity_ref: str) -> RecipePartsV2:
    """The rendered line's pieces. ``measure_refs`` carries EVERY bound pair — the grain and
    point-in-time columns included — so both are subtracted here: a card listing the grain column as
    a measure would claim the feature aggregates its own key. Order is the engine's binding order
    (deduped), so the same idea always renders the same line.

    ``entity_ref`` is the recipe's OWN bound entity when one resolved. The ``BY`` clause must name
    the column the card's HEADING names: ``idea.grain_ref`` is the table's single ``is_grain``
    column, so an account-grained card otherwise read "per account" above a line saying
    ``BY cif_id``. It is subtracted from the measures for the same reason the grain is — it is the
    feature's key, not a quantity it aggregates."""
    dropped = {ref for ref in (idea.grain_ref, idea.time_ref) if ref is not None}
    measures = [ref for ref in dict.fromkeys(idea.measure_refs)
                if ref not in dropped and ref[1] != entity_ref]
    grain = entity_ref or (idea.grain_ref[1] if idea.grain_ref else "")
    return RecipePartsV2(
        operation=idea.operation_kind,
        measures=tuple(_column(ref) for _src, ref in measures),
        grain=_column(grain) if grain else "",
        window=idea.window or "",
        time=_column(idea.time_ref[1]) if idea.time_ref else "",
    )


def recipe_parts_to_json(parts: RecipePartsV2) -> dict:
    """The wire block — the SAME five keys, in the same order, with ``measures`` as a list. V1's
    payload is this dict verbatim."""
    return {"operation": parts.operation, "measures": list(parts.measures), "grain": parts.grain,
            "window": parts.window, "time": parts.time}


def _column(object_ref: str) -> str:
    """The column name — the ref's last segment. A full ``schema.table.column`` ref is unreadable on
    a card, and nothing is invented by taking the name the catalog already holds."""
    return object_ref.rsplit(".", 1)[-1]


# ── attributed values ───────────────────────────────────────────────────────────────────────────
_TEMPLATES_BY_ID = {template.id: template for template in ALL_TEMPLATES}


def _recipe_evidence(recipe_revision: str | None) -> tuple[EvidenceAuthorityV1, ...]:
    """A value READ OUT OF the authored recipe cites the recipe revision — the same citation shape
    Task 1's discovery registry uses, so one prefix identifies recipe-authored provenance
    everywhere."""
    return (EvidenceAuthorityV1(
        producer=EvidenceProducer.TAXONOMY, strength=AssertionStrength.ATTESTED,
        lifecycle=EvidenceLifecycle.ACTIVE,
        producer_ref=(None if recipe_revision is None
                      else f"{RECIPE_REVISION_CITATION_PREFIX}{recipe_revision}"),
        evidence_id=None),)


def is_family_derived_category(label: AttributedLabelV1 | None) -> bool:
    """Whether a ``feature_category`` was DERIVED by the family->category mapping rather than read
    out of the recipe (Task 1 handoff (b)).

    ``basis`` alone says ``template_authored`` for both, because the derivation is deterministic and
    controller-sanctioned — so a provenance badge that reads ``basis`` would present a taxonomy
    derivation as an SME-authored objective. The positive signal is the mapping citation, which
    Task 1 puts FIRST in the assignment's evidence and this adapter carries through verbatim.
    """
    if label is None:
        return False
    return any((e.producer_ref or "").startswith(FAMILY_MAPPING_CITATION_PREFIX)
               for e in label.evidence)


def _controlled_label(assignment: DiscoveryControlledAssignmentV1, *, display_name: str,
                      source_refs: tuple[str, ...]) -> AttributedLabelV1:
    """A discovery assignment as an attributed label. The per-value basis, evidence and operational
    influence are carried VERBATIM: an LLM-proposed use case beside a derived category must stay
    distinguishable, and collapsing either into one unqualified string is exactly what rule 4
    forbids."""
    return AttributedLabelV1(
        id=assignment.controlled_id, display_name=display_name, basis=assignment.basis,
        evidence=assignment.evidence, operational_influence=assignment.operational_influence,
        source_refs=source_refs)


def _text_value(assignment: DiscoveryTextAssignmentV1, *,
                source_refs: tuple[str, ...]) -> AttributedTextV1:
    return AttributedTextV1(
        value=assignment.value, basis=assignment.basis, evidence=assignment.evidence,
        operational_influence=assignment.operational_influence, source_refs=source_refs)


def _bounded(values: Sequence, limit: int, omitted: Counter, key: str) -> tuple:
    """Keep ``limit`` values and COUNT the rest. Silent truncation would let a card claim it lists
    every domain it has."""
    if len(values) <= limit:
        return tuple(values)
    omitted[key] += len(values) - limit
    return tuple(values[:limit])


# ── read-scoped catalog facts, in ONE bounded statement per catalog source ──────────────────────
@dataclass(frozen=True, slots=True)
class _NodeFacts:
    visible_requires: tuple[str, ...]
    domain: str | None
    entity: str | None
    table_name: str | None


def _read_node_facts(conn, pairs: Iterable[tuple[str, str]],
                     roles: Sequence[str]) -> dict[tuple[str, str], _NodeFacts]:
    """Every referenced node THIS CALLER MAY SEE, keyed ``(catalog_source, object_ref)``.

    ONE statement per distinct catalog source — one, on the on-demand path — for the WHOLE page,
    which is what keeps a fifty-card page from issuing fifty context reads. A ref that does not come
    back is hidden or gone under this caller's scope; the caller learns nothing about which, because
    the suggestion that named it is withheld entirely.

    The predicate is the migration-1032 one every other read-scope call site binds, so "visible to
    grounding" and "visible here" can never disagree.
    """
    by_source: dict[str, set[str]] = {}
    for catalog_source, object_ref in pairs:
        by_source.setdefault(catalog_source, set()).add(object_ref)
    facts: dict[tuple[str, str], _NodeFacts] = {}
    classes = allowed_classes(roles)
    for catalog_source in sorted(by_source):
        rows = conn.execute(
            "SELECT object_ref, coalesce(visible_requires, '{}'), domain, entity, table_name "
            "FROM graph_node WHERE catalog_source = %s AND object_ref = ANY(%s) "
            "AND visible_requires <@ %s",
            (catalog_source, sorted(by_source[catalog_source]), classes)).fetchall()
        for object_ref, visible, domain, entity, table_name in rows:
            facts[(catalog_source, object_ref)] = _NodeFacts(
                visible_requires=tuple(visible or ()), domain=domain, entity=entity,
                table_name=table_name)
    return facts


# ── one suggestion ──────────────────────────────────────────────────────────────────────────────
def _operand_refs(idea) -> tuple[tuple[str, str], ...]:
    """The bound operands, deduplicated ON THE REF in binding order — byte-for-byte the rule V1's
    ``uses`` uses, which is why the V1 adapter can rebuild that list from the operand tuple."""
    seen: dict[str, tuple[str, str]] = {}
    for catalog_source, object_ref in idea.derives_pairs:
        seen.setdefault(object_ref, (catalog_source, object_ref))
    return tuple(seen.values())


def _endpoint_refs(trace: GroundingDecisionTraceV1 | None) -> tuple[tuple[str, str], ...]:
    if trace is None:
        return ()
    refs: dict[tuple[str, str], None] = {}
    for leg in trace.ordered_relationship_path:
        refs.setdefault(leg.from_ref, None)
        refs.setdefault(leg.to_ref, None)
    return tuple(refs)


def _classification(idea, ref: tuple[str, str], binding, entity_ref: str) -> str:
    """What this operand IS to the computation, from GOVERNED inputs only.

    Grain and time are decided FIRST, because ``measure_refs`` carries every bound pair — the key
    and the clock included — so checking measure membership first would file the feature's own key
    as a quantity it aggregates.

    When the engine's typed refs do not name it, the fallback is the TEMPLATE-AUTHORED need concept
    (``Need.concept`` -> ``Concept.pit_role``), which is registry metadata about the recipe slot,
    not a guess about the column: an ``event_timestamp`` need is a time operand even on a recipe
    whose point-in-time anchor is the table's own as-of column. Nothing here reads a column name, a
    data type or the column's AI-proposed concept.
    """
    if ref[1] == entity_ref or (idea.grain_ref is not None and ref == idea.grain_ref):
        return "grain"
    if idea.time_ref is not None and ref == idea.time_ref:
        return "time"
    if ref in idea.grouping_refs:
        return "grouping"
    declared = None if binding is None else concept(binding.expected_concept)
    if declared is not None and declared.pit_role != "none":
        return "time"
    if ref in idea.measure_refs:
        return "measure"
    return "other"


def _evidence_refs(trace: GroundingDecisionTraceV1 | None, ref: tuple[str, str],
                   omitted: Counter) -> tuple[str, ...]:
    """The exact provenance pins the gauntlet recorded AT this operand — the decision/fact revisions
    it read and the evidence occurrences behind them. Provenance, deliberately: none of these enter
    a semantic hash, and they are what a reader compares against the current pointer."""
    if trace is None:
        return ()
    key = column_dependency_key(ref[0], ref[1])
    found: set[str] = set()
    for pin in trace.dependency_pins:
        if pin.dependency_key != key:
            continue
        if pin.current_revision_id:
            found.add(pin.current_revision_id)
        for evidence in pin.evidence:
            if evidence.evidence_id:
                found.add(evidence.evidence_id)
    return _bounded(sorted(found), MAX_EVIDENCE_REFS, omitted, "evidence_refs")


def _warnings(idea, template, trace: GroundingDecisionTraceV1 | None,
              operands: Sequence[SuggestionOperandV1]) -> tuple[SuggestionWarningV1, ...]:
    """Every warning derived from a TYPED input: a requirement code, an authored template
    declaration, a re-read visibility class or a traversed leg's own recorded state. Nothing is
    parsed out of prose, and the three relationship concerns stay SEPARATE codes because they are
    separate facts — an unconfirmed link may still be worth exploring, while absent directional
    safety evidence is a different, louder thing.
    """
    warnings: list[SuggestionWarningV1] = []
    if template is not None and template.near_label:
        warnings.append(SuggestionWarningV1(
            code="NEAR_LABEL", operand_refs=(),
            detail="the recipe declares this feature borders the outcome label"))
    sensitive = tuple((o.catalog_source, o.graph_object_ref) for o in operands
                      if o.visibility_requires_current)
    if sensitive:
        warnings.append(SuggestionWarningV1(
            code="SENSITIVE_INPUT", operand_refs=sensitive,
            detail="an input carries a visibility restriction"))
    by_code: dict[str, list[tuple[str, str]]] = {}
    for requirement in idea.requirements:
        code = _WARNING_BY_REQUIREMENT.get(requirement.code)
        if code is not None:
            by_code.setdefault(code, []).append(requirement.operand)
    for code in sorted(by_code):
        warnings.append(SuggestionWarningV1(
            code=code, operand_refs=tuple(by_code[code]),
            detail="the gauntlet raised the matching typed requirement"))
    legs = () if trace is None else trace.ordered_relationship_path
    join_needed = any(r.code == "JOIN_CONNECTIVITY" for r in idea.requirements)
    unconfirmed = tuple((leg.from_ref, leg.to_ref) for leg in legs
                        if leg.review_status == REVIEW_FILE_DECLARED)
    if unconfirmed:
        warnings.append(SuggestionWarningV1(
            code="RELATIONSHIP_UNCONFIRMED", operand_refs=unconfirmed,
            detail="a traversed relationship was declared by an upload and confirmed by nobody"))
    unproven = tuple((leg.from_ref, leg.to_ref) for leg in legs
                     if leg.safety_status != SAFETY_CLEARING)
    if unproven or join_needed:
        warnings.append(SuggestionWarningV1(
            code="RELATIONSHIP_SAFETY_UNPROVEN", operand_refs=unproven,
            detail="a traversed relationship has no governed-verified safety evidence"))
    unknown_cardinality = tuple((leg.from_ref, leg.to_ref) for leg in legs
                                if leg.cardinality == CARDINALITY_UNKNOWN)
    if unknown_cardinality:
        warnings.append(SuggestionWarningV1(
            code="DIRECTIONAL_CARDINALITY_UNAVAILABLE", operand_refs=unknown_cardinality,
            detail="a traversed relationship declares no cardinality in the direction travelled"))
    return tuple(warnings)


def _domain_and_entity_context(operands: Sequence[SuggestionOperandV1],
                               facts: Mapping[tuple[str, str], _NodeFacts],
                               omitted: Counter
                               ) -> tuple[tuple[AttributedLabelV1, ...],
                                          tuple[AttributedTextV1, ...],
                                          tuple[AttributedTextV1, ...]]:
    """The catalog's own domain/entity WORDING for the bound operands, routed through the frozen
    resolver seam.

    When the semantic plan registers a controlled ``business_domain``/``entity`` resolver, the same
    call yields ``AttributedLabelV1`` facets and this adapter needs no change. Until then the seam
    resolves nothing and the wording stays ``AttributedTextV1``: displayable, text-searchable, never
    a facet id — never lowercased or slugged into one.

    ``evidence=()`` is deliberate and honest. This is the catalog's CURRENT projected wording; its
    evidence chain lives in ``field_evidence`` and reading it per operand is exactly the per-card
    N+1 this task forbids. ``basis='catalog_resolved'`` plus the source refs say precisely where the
    words came from and claim no attestation the adapter did not read.
    """
    domain_terms: dict[str, list[str]] = {}
    entity_terms: dict[str, list[str]] = {}
    for operand in operands:
        node = facts.get((operand.catalog_source, operand.graph_object_ref))
        if node is None:
            continue
        if node.domain:
            domain_terms.setdefault(node.domain, []).append(operand.graph_object_ref)
        if node.entity:
            entity_terms.setdefault(node.entity, []).append(operand.graph_object_ref)
    domains: list[AttributedLabelV1] = []
    texts: list[AttributedTextV1] = []
    for wording in sorted(domain_terms):
        resolved = resolve_or_text("business_domain", wording, basis="catalog_resolved",
                                   source_refs=tuple(domain_terms[wording]))
        (domains if isinstance(resolved, AttributedLabelV1) else texts).append(resolved)
    entity_texts: list[AttributedTextV1] = []
    for wording in sorted(entity_terms):
        resolved = resolve_or_text("entity", wording, basis="catalog_resolved",
                                   source_refs=tuple(entity_terms[wording]))
        if isinstance(resolved, AttributedLabelV1):
            # A CONTROLLED entity resolution is the entity facet's business, not free context.
            continue
        entity_texts.append(resolved)
    return (_bounded(domains, MAX_BUSINESS_DOMAINS, omitted, "business_domains"),
            _bounded(texts, MAX_CONTEXTUAL_DOMAIN_TERMS, omitted, "contextual_domain_terms"),
            _bounded(entity_texts, MAX_CONTEXTUAL_ENTITY_TERMS, omitted,
                     "contextual_entity_terms"))


def _entity_label(entity_id: str, recipe_revision: str | None) -> AttributedLabelV1:
    """The entity facet — the CONTROLLED ``Concept.entity_link`` vocabulary the grounding engine
    itself bound (0F-6). ``display_name`` is the id until the semantic plan lands a controlled
    entity registry with display names; inventing a prettier one here would be a second ontology."""
    return AttributedLabelV1(
        id=entity_id, display_name=entity_id, basis="catalog_resolved",
        evidence=_recipe_evidence(recipe_revision), operational_influence=None, source_refs=())


def _source_datasets(operands: Sequence[SuggestionOperandV1]
                     ) -> tuple[SuggestionSourceDatasetV1, ...]:
    """One record per dataset the suggestion reads, in first-appearance (binding) order. Every
    descriptive role is explicitly unavailable in Release A (D9) — never guessed from the catalog's
    shape, because "event fact" and "dimension" are governed answers the profile plan owns."""
    seen: dict[tuple[str, str], SuggestionSourceDatasetV1] = {}
    for operand in operands:
        key = (operand.catalog_source, operand.table_ref)
        seen.setdefault(key, SuggestionSourceDatasetV1(
            catalog_source=operand.catalog_source, table_ref=operand.table_ref,
            data_role=None, authority_role=None, temporal_storage_model=None,
            primary_entity=None, dataset_profile_hash=None,
            profile_status=PROFILE_STATUS_UNAVAILABLE))
    return tuple(seen.values())


def _build_suggestion(idea, *, entity_ref: str, entity_label_id: str, context, trace,
                      binding_quality: str, facts: Mapping[tuple[str, str], _NodeFacts],
                      omitted: Counter) -> FeatureSuggestionV2:
    template = _TEMPLATES_BY_ID.get(idea.recipe_id or "")
    recipe_revision = None if context is None else context.template_content_hash
    entry = DISCOVERY_METADATA.get(idea.recipe_id or "")
    discovery_revision = None if entry is None else discovery_metadata_revision_id(entry)
    discovery_refs = () if discovery_revision is None else (
        f"discovery-metadata-revision:{discovery_revision}",)
    recipe_refs = () if recipe_revision is None else (
        f"{RECIPE_REVISION_CITATION_PREFIX}{recipe_revision}",)

    bindings_by_ref = {} if context is None else {
        binding.graph_object_ref: binding for binding in context.need_bindings}
    operand_refs = _operand_refs(idea)
    operands = []
    for catalog_source, object_ref in _bounded(operand_refs, MAX_OPERANDS, omitted, "operands"):
        binding = bindings_by_ref.get(object_ref)
        node = facts.get((catalog_source, object_ref))
        operands.append(SuggestionOperandV1(
            catalog_source=catalog_source,
            logical_ref=object_ref if binding is None else binding.logical_ref,
            graph_object_ref=object_ref,
            table_ref=(node.table_name if node is not None and node.table_name
                       else table_of_ref(object_ref)),
            recipe_role="" if binding is None else binding.role,
            classification=_classification(idea, (catalog_source, object_ref),
                                           binding, entity_ref),
            visibility_requires_current=() if node is None else node.visible_requires,
            evidence_refs=_evidence_refs(trace, (catalog_source, object_ref), omitted)))
    operands = tuple(operands)

    business_domains, domain_terms, entity_terms = _domain_and_entity_context(
        operands, facts, omitted)
    use_cases = ()
    keywords = ()
    business_value = None
    feature_category = None
    disposition = "unclassified"
    if entry is not None:
        disposition = entry.disposition
        if entry.feature_category is not None:
            feature_category = _controlled_label(
                entry.feature_category,
                display_name=FEATURE_CATEGORY_REGISTRY[
                    entry.feature_category.controlled_id].display_name,
                source_refs=discovery_refs)
        use_cases = _bounded(
            [_controlled_label(a, display_name=_use_case_display(a.controlled_id),
                               source_refs=discovery_refs)
             for a in entry.canonical_use_cases], MAX_USE_CASES, omitted, "use_cases")
        keywords = _bounded([_text_value(k, source_refs=discovery_refs) for k in entry.keywords],
                            MAX_KEYWORDS, omitted, "keywords")
        if entry.business_value is not None:
            business_value = _text_value(entry.business_value, source_refs=discovery_refs)

    family = None
    if template is not None and template.family in RECIPE_FAMILY_REGISTRY:
        family = AttributedLabelV1(
            id=template.family, display_name=RECIPE_FAMILY_REGISTRY[template.family].display_name,
            basis="template_authored", evidence=_recipe_evidence(recipe_revision),
            operational_influence=None, source_refs=recipe_refs)

    grain_source = next((source for source, ref in idea.derives_pairs if ref == entity_ref),
                        operands[0].catalog_source if operands else "")
    grain_refs = ((grain_source, entity_ref),) if entity_ref else ()
    relationships = () if trace is None else _bounded(
        trace.ordered_relationship_path, MAX_RELATIONSHIP_DEPENDENCIES, omitted,
        "relationship_dependencies")

    identity = suggestion_id(
        template_id=idea.recipe_id,
        bound_params=() if context is None else context.semantic_parameters,
        operands=tuple((o.catalog_source, o.logical_ref, o.recipe_role) for o in operands),
        entity_id=entity_label_id or None, grain_refs=grain_refs, time_ref=idea.time_ref,
        relationship_path_assignment=join_path_assignment(trace))
    revision = suggestion_revision_id(
        suggestion_id=identity, recipe_revision_id=recipe_revision,
        discovery_metadata_revision_id=discovery_revision,
        semantic_context_hashes=(), dataset_profile_hashes=(),
        trace_content_hash=trace.trace_content_hash,
        dependency_content_hashes=dependency_content_hashes(trace),
        validation_rule_content_hashes=trace.validation_rule_content_hashes,
        read_scope_rule_content_hashes=trace.read_scope_rule_content_hashes,
        validation_status=idea.validation_status)

    return FeatureSuggestionV2(
        schema_version=SCHEMA_VERSION, suggestion_id=identity, suggestion_revision_id=revision,
        generation_source=idea.generation_source,
        template_id=idea.recipe_id, recipe_revision_id=recipe_revision,
        discovery_metadata_revision_id=discovery_revision,
        validation_rule_content_hashes=trace.validation_rule_content_hashes,
        read_scope_rule_content_hashes=trace.read_scope_rule_content_hashes,
        name=idea.name, display_name=idea.name,
        business_interpretation=AttributedTextV1(
            value=idea.description, basis="template_authored",
            evidence=_recipe_evidence(recipe_revision), operational_influence=None,
            source_refs=recipe_refs),
        business_value=business_value,
        feature_category=feature_category, discovery_disposition=disposition,
        recipe_family=family, business_domains=business_domains,
        contextual_domain_terms=domain_terms, use_cases=use_cases, keywords=keywords,
        entity=_entity_label(entity_label_id, recipe_revision) if entity_label_id else None,
        contextual_entity_terms=entity_terms, grain_refs=grain_refs,
        operation_kind=idea.operation_kind, window=idea.window, time_ref=idea.time_ref,
        recipe=render_recipe(idea, entity_ref), recipe_parts=recipe_parts(idea, entity_ref),
        source_datasets=_source_datasets(operands), operands=operands,
        relationship_dependencies=relationships, validation_status=idea.validation_status,
        requirements=tuple(idea.requirements),
        warnings=_warnings(idea, template, trace, operands), binding_quality=binding_quality,
        semantic_context_hashes=(), dataset_profile_hashes=(),
        grounding_trace_content_hash=trace.trace_content_hash)


def _use_case_display(use_case_id: str) -> str:
    node = USE_CASE_REGISTRY.get(use_case_id)
    return use_case_id if node is None else node.display_name


def _provenance(trace: GroundingDecisionTraceV1 | None, idea) -> SuggestionBuildProvenanceV1:
    """The exact ids, all of them EXCLUDED from the revision above (0F-10) and kept here so a reader
    can still compare currentness and audit the build."""
    dependency_revisions: set[str] = set()
    evidence_events: set[str] = set()
    realizations: set[str] = set()
    if trace is not None:
        for pin in trace.dependency_pins:
            if pin.current_revision_id:
                dependency_revisions.add(pin.current_revision_id)
            for evidence in pin.evidence:
                if evidence.evidence_id:
                    evidence_events.add(evidence.evidence_id)
        for leg in trace.ordered_relationship_path:
            if leg.realization_content_hash:
                realizations.add(leg.realization_content_hash)
    return SuggestionBuildProvenanceV1(
        scope_set_id=None,
        metadata_snapshot_ids=(() if idea.metadata_snapshot_id is None
                               else (idea.metadata_snapshot_id,)),
        dependency_revision_ids=tuple(sorted(dependency_revisions)),
        evidence_event_ids=tuple(sorted(evidence_events)),
        relationship_realization_revision_ids=tuple(sorted(realizations)),
        producer_commit=None, refresh_id=None, generated_at=None)


# ── the page ────────────────────────────────────────────────────────────────────────────────────
def _empty_summary() -> SuggestionSummaryV2:
    return SuggestionSummaryV2(suggested=0, design_checked=0, needs_external_validation=0,
                               groups=0)


def unknown_table_page_v2(*, catalog_source: str, requested_table: str,
                          neighbourhood: JoinNeighbourhood,
                          roles: Sequence[str] = ()) -> FeatureSuggestionPageV2:
    """The FOURTH state, as a V2 page: this catalog does not hold that table FOR THIS CALLER.

    ``anchor_table_ref`` echoes the caller's requested string VERBATIM (0F-11's two-case rule):
    there is no resolver output to carry, and substituting a normalized spelling would answer a
    question the caller did not ask. The zero neighbourhood is carried as a real value, not
    recomputed by the adapter, so the V1 bytes need no second derivation.
    """
    return FeatureSuggestionPageV2(
        read_mode="on_demand", read_scope_key=build_read_scope(roles).scope_key, projection=None,
        collection=SuggestionCollectionContextV2(
            anchor_catalog_source=catalog_source, anchor_table_ref=requested_table,
            anchor_column_ref=None, table_known=False, summary=_empty_summary(), groups=(),
            rejections=(), neighbourhood=neighbourhood, omitted_counts={}),
        hits=(), facets={}, next_cursor=None)


def build_page_v2(conn, *, catalog_source: str, anchor_table_ref: str, anchored: Sequence[tuple],
                  candidates, neighbourhood: JoinNeighbourhood, roles: Sequence[str] = (),
                  anchor_column_ref: str | None = None) -> FeatureSuggestionPageV2:
    """Assemble one on-demand V2 page from ONE grounding pass.

    ``anchored`` is ``(idea, entity_ref, entity_label)`` in the engine's order — the caller resolved
    the entity with the SAME rule V1 groups by, so the two surfaces can never disagree about which
    bucket a card belongs to.

    Admission happens before anything is read from the catalog, and each refusal is COUNTED rather
    than quietly dropped:

    * a candidate whose ``generation_source`` is not ``recipe`` — this producer has no hypothesis
      and no lifecycle for a free-form idea;
    * a candidate with NO decision trace — its ``grounding_trace_content_hash`` would be a
      fabrication and its identity would have no path material;
    * a ``DESIGN_CHECKED`` candidate whose trace cannot explain that decision (freeze 0F-7);
    * a candidate any of whose operands or relationship endpoints this caller may not currently
      see — withheld WHOLE, so nothing leaks through the counts, the groups or the facets.
    """
    omitted: Counter = Counter()
    admitted = []
    for idea, entity_ref, entity_label in anchored:
        trace = idea.grounding_trace
        if idea.generation_source != PRODUCER_GENERATION_SOURCE:
            omitted["withheld_non_recipe_generation_source"] += 1
            continue
        if trace is None:
            omitted["withheld_missing_trace"] += 1
            continue
        if idea.validation_status == "DESIGN_CHECKED" and trace_completeness_gaps(
                trace, validation_status=idea.validation_status,
                requirements=idea.requirements):
            omitted["withheld_incomplete_trace"] += 1
            continue
        admitted.append((idea, entity_ref, entity_label, trace))

    wanted: set[tuple[str, str]] = set()
    for idea, _ref, _label, trace in admitted:
        wanted.update(_operand_refs(idea))
        wanted.update(_endpoint_refs(trace))
    facts = _read_node_facts(conn, wanted, roles)

    hits: list[FeatureSuggestionHitV2] = []
    grouped: dict[str, list[FeatureSuggestionV2]] = {}
    for idea, entity_ref, entity_label, trace in admitted:
        required = set(_operand_refs(idea)) | set(_endpoint_refs(trace))
        if not required <= set(facts):
            omitted["withheld_read_scope"] += 1
            continue
        context = candidates.contexts.get(trace.candidate_key)
        suggestion = _build_suggestion(
            idea, entity_ref=entity_ref, entity_label_id=entity_label, context=context,
            trace=trace, binding_quality=candidates.binding_by_id.get(idea.recipe_id or "", ""),
            facts=facts, omitted=omitted)
        hits.append(FeatureSuggestionHitV2(
            suggestion=suggestion, projection=None, provenance=_provenance(trace, idea)))
        grouped.setdefault(entity_ref, []).append(suggestion)

    groups = tuple(
        SuggestionGroupV2(
            entity=members[0].entity,
            contextual_entity_terms=members[0].contextual_entity_terms,
            grain_refs=members[0].grain_refs,
            suggestion_ids=tuple(s.suggestion_id for s in members))
        # The unlabelled bucket sorts LAST — V1's own key, so the adapter needs no second rule.
        for _ref, members in sorted(grouped.items(), key=lambda kv: (kv[0] == "", kv[0])))
    design_checked = sum(1 for hit in hits
                         if hit.suggestion.validation_status == "DESIGN_CHECKED")
    summary = SuggestionSummaryV2(
        suggested=len(hits), design_checked=design_checked,
        needs_external_validation=len(hits) - design_checked,
        # V1's `entities` semantics exactly: a bucket with no grain ref is not an entity.
        groups=sum(1 for group in groups if group.grain_refs))
    rejections = tuple(
        SuggestionRejectionV2(template_id=record.template_id, candidate_name=record.candidate_name,
                              code=record.rejection.code, explanation=record.rejection.message)
        for record in candidates.rejection_records)
    return FeatureSuggestionPageV2(
        read_mode="on_demand", read_scope_key=build_read_scope(roles).scope_key, projection=None,
        collection=SuggestionCollectionContextV2(
            anchor_catalog_source=catalog_source, anchor_table_ref=anchor_table_ref,
            anchor_column_ref=anchor_column_ref, table_known=True, summary=summary, groups=groups,
            rejections=rejections, neighbourhood=neighbourhood, omitted_counts=dict(omitted)),
        hits=tuple(hits), facets={}, next_cursor=None)


# ── the explicit V1 adapter (0F-11) ─────────────────────────────────────────────────────────────
def _grain_table(suggestion: FeatureSuggestionV2) -> str | None:
    """V1's ``grain_table``, rebuilt from the carried operands — no re-query, no second rule.

    ``templates.py`` derives it as the SOURCE-ENTITY binding's table when one bound, else the first
    bound column's table. Those two branches are exactly the two states of ``entity`` here, because
    a source-entity role only ever resolves on an ENTITY-LINKED concept (``_is_entity_concept``
    requires ``Concept.entity_link``): so the role bound ⇔ the entity label resolved ⇔
    ``grain_refs[0]`` names that very column. When it did not bind, ``operands[0]`` is the first
    bound column in needs order — the same element ``templates.py`` takes.
    """
    if suggestion.entity is not None and suggestion.grain_refs:
        ref = suggestion.grain_refs[0][1]
        for operand in suggestion.operands:
            if operand.graph_object_ref == ref:
                return operand.table_ref
    return suggestion.operands[0].table_ref if suggestion.operands else None


def to_table_suggestions_v1(page: FeatureSuggestionPageV2) -> dict:
    """Today's per-table wire payload, rebuilt from the V2 page and nothing else.

    Every V1 byte has a V2 carrier (0F-11): the summary is a rename, the group refs and labels ride
    on the groups, the neighbourhood block is the SAME ``JoinNeighbourhood`` value re-serialized by
    its own producer, and the unknown-table state carries its synthesized zero block rather than
    having the adapter recompute one. New V2 fields are DROPPED, never folded in.

    It REFUSES rather than lie: if this page bounded away operands or withheld a card that the V1
    producer would have emitted, the V1 body rebuilt from it would silently differ from the one the
    V1 producer returns, and a migration that cannot tell the two apart is worse than none.
    """
    collection = page.collection
    unsafe = {key: collection.omitted_counts.get(key, 0) for key in V1_UNSAFE_OMISSION_KEYS
              if collection.omitted_counts.get(key)}
    if unsafe:
        raise SuggestionV1ReconstructionError(
            f"this page cannot be rendered as V1 without lying about it: {unsafe}. V1 has no "
            f"carrier for a bounded operand list or a withheld card, so the adapter refuses "
            f"instead of returning a quietly different body")
    neighbourhood = (collection.neighbourhood.as_metadata()
                     if collection.neighbourhood is not None else None)
    if not collection.table_known:
        return {"catalog_source": collection.anchor_catalog_source,
                "table": collection.anchor_table_ref, "table_known": False,
                "summary": {"suggested": 0, "clean_ready": 0, "needs_review": 0, "entities": 0},
                "groups": [], "rejections": [], "neighbourhood": neighbourhood}
    by_id = {hit.suggestion.suggestion_id: hit.suggestion for hit in page.hits}
    groups = []
    for group in collection.groups:
        groups.append({
            "entity_ref": group.grain_refs[0][1] if group.grain_refs else "",
            "entity_label": group.entity.display_name if group.entity is not None else "",
            "suggestions": [_card_v1(by_id[sid]) for sid in group.suggestion_ids],
        })
    return {
        "catalog_source": collection.anchor_catalog_source,
        "table": collection.anchor_table_ref,
        "table_known": True,
        "summary": {"suggested": collection.summary.suggested,
                    "clean_ready": collection.summary.design_checked,
                    "needs_review": collection.summary.needs_external_validation,
                    "entities": collection.summary.groups},
        "groups": groups,
        "rejections": [{"name": r.candidate_name, "reason": r.explanation, "code": r.code}
                       for r in collection.rejections],
        "neighbourhood": neighbourhood,
    }


def _card_v1(suggestion: FeatureSuggestionV2) -> dict:
    return {
        "name": suggestion.name,
        "description": (suggestion.business_interpretation.value
                        if suggestion.business_interpretation is not None else ""),
        "grain_table": _grain_table(suggestion),
        "validation_status": suggestion.validation_status,
        "requirements": requirements_to_json(suggestion.requirements),
        "uses": [operand.graph_object_ref for operand in suggestion.operands],
        "binding_quality": suggestion.binding_quality,
        "recipe": suggestion.recipe,
        "recipe_parts": recipe_parts_to_json(suggestion.recipe_parts),
    }


# ── wire serialization ──────────────────────────────────────────────────────────────────────────
def _ref_json(ref: tuple[str, str] | None) -> list[str] | None:
    return None if ref is None else [ref[0], ref[1]]


def _label_json(label: AttributedLabelV1 | None) -> dict | None:
    return None if label is None else attributed_label_to_json(label)


def _text_json(text: AttributedTextV1 | None) -> dict | None:
    return None if text is None else attributed_text_to_json(text)


def _operand_json(operand: SuggestionOperandV1) -> dict:
    return {"catalog_source": operand.catalog_source, "logical_ref": operand.logical_ref,
            "graph_object_ref": operand.graph_object_ref, "table_ref": operand.table_ref,
            "recipe_role": operand.recipe_role, "classification": operand.classification,
            "visibility_requires_current": list(operand.visibility_requires_current),
            "evidence_refs": list(operand.evidence_refs)}


def _dataset_json(dataset: SuggestionSourceDatasetV1) -> dict:
    return {"catalog_source": dataset.catalog_source, "table_ref": dataset.table_ref,
            "data_role": _label_json(dataset.data_role),
            "authority_role": _label_json(dataset.authority_role),
            "temporal_storage_model": _label_json(dataset.temporal_storage_model),
            "primary_entity": _label_json(dataset.primary_entity),
            "dataset_profile_hash": dataset.dataset_profile_hash,
            "profile_status": dataset.profile_status}


def _relationship_json(leg: SuggestionRelationshipDependencyV1) -> dict:
    return {"relationship_ref": leg.relationship_ref, "relationship_kind": leg.relationship_kind,
            "from_ref": _ref_json(leg.from_ref), "to_ref": _ref_json(leg.to_ref),
            "realization_content_hash": leg.realization_content_hash,
            "cardinality": leg.cardinality, "safety_status": leg.safety_status,
            "review_status": leg.review_status}


def suggestion_to_json(suggestion: FeatureSuggestionV2) -> dict:
    """The V2 suggestion on the wire. Attributed values keep ALL five evidence fields (this is the
    provenance surface, not a hash payload), and the relationship legs deliberately do NOT echo
    their evidence occurrences — those ride on the hit's provenance block."""
    return {
        "schema_version": suggestion.schema_version,
        "suggestion_id": suggestion.suggestion_id,
        "suggestion_revision_id": suggestion.suggestion_revision_id,
        "generation_source": suggestion.generation_source,
        "template_id": suggestion.template_id,
        "recipe_revision_id": suggestion.recipe_revision_id,
        "discovery_metadata_revision_id": suggestion.discovery_metadata_revision_id,
        "validation_rule_content_hashes": list(suggestion.validation_rule_content_hashes),
        "read_scope_rule_content_hashes": list(suggestion.read_scope_rule_content_hashes),
        "name": suggestion.name,
        "display_name": suggestion.display_name,
        "business_interpretation": _text_json(suggestion.business_interpretation),
        "business_value": _text_json(suggestion.business_value),
        "feature_category": _label_json(suggestion.feature_category),
        "feature_category_derived_from_family_mapping": is_family_derived_category(
            suggestion.feature_category),
        "discovery_disposition": suggestion.discovery_disposition,
        "recipe_family": _label_json(suggestion.recipe_family),
        "business_domains": [attributed_label_to_json(d) for d in suggestion.business_domains],
        "contextual_domain_terms": [attributed_text_to_json(t)
                                    for t in suggestion.contextual_domain_terms],
        "use_cases": [attributed_label_to_json(u) for u in suggestion.use_cases],
        "keywords": [attributed_text_to_json(k) for k in suggestion.keywords],
        "entity": _label_json(suggestion.entity),
        "contextual_entity_terms": [attributed_text_to_json(t)
                                    for t in suggestion.contextual_entity_terms],
        "grain_refs": [_ref_json(ref) for ref in suggestion.grain_refs],
        "operation_kind": suggestion.operation_kind,
        "window": suggestion.window,
        "time_ref": _ref_json(suggestion.time_ref),
        "recipe": suggestion.recipe,
        "recipe_parts": recipe_parts_to_json(suggestion.recipe_parts),
        "source_datasets": [_dataset_json(d) for d in suggestion.source_datasets],
        "operands": [_operand_json(o) for o in suggestion.operands],
        "relationship_dependencies": [_relationship_json(leg)
                                      for leg in suggestion.relationship_dependencies],
        "validation_status": suggestion.validation_status,
        "requirements": requirements_to_json(suggestion.requirements),
        "warnings": [{"code": w.code, "operand_refs": [_ref_json(r) for r in w.operand_refs],
                      "detail": w.detail} for w in suggestion.warnings],
        "binding_quality": suggestion.binding_quality,
        "semantic_context_hashes": list(suggestion.semantic_context_hashes),
        "dataset_profile_hashes": list(suggestion.dataset_profile_hashes),
        "grounding_trace_content_hash": suggestion.grounding_trace_content_hash,
    }


def _provenance_json(provenance: SuggestionBuildProvenanceV1) -> dict:
    return {"scope_set_id": provenance.scope_set_id,
            "metadata_snapshot_ids": list(provenance.metadata_snapshot_ids),
            "dependency_revision_ids": list(provenance.dependency_revision_ids),
            "evidence_event_ids": list(provenance.evidence_event_ids),
            "relationship_realization_revision_ids": list(
                provenance.relationship_realization_revision_ids),
            "producer_commit": provenance.producer_commit, "refresh_id": provenance.refresh_id,
            "generated_at": (None if provenance.generated_at is None
                             else provenance.generated_at.isoformat())}


def _projection_json(projection: SuggestionProjectionStateV1 | None) -> dict | None:
    if projection is None:
        return None
    return {"state": projection.state, "scope_set_id": projection.scope_set_id,
            "read_scope_key": projection.read_scope_key, "scope_epoch": projection.scope_epoch,
            "target_fingerprint": projection.target_fingerprint,
            "current_fingerprint": projection.current_fingerprint,
            "generated_at": (None if projection.generated_at is None
                             else projection.generated_at.isoformat()),
            "stale_reason": projection.stale_reason,
            "omitted_counts": dict(projection.omitted_counts)}


def page_to_json(page: FeatureSuggestionPageV2) -> dict:
    """The whole page on the wire. ``read_scope_key`` is exposed for diagnostics and cursor binding
    only — it is an opaque hash of the caller's visibility classes, never the role claims and never
    itself an authorization."""
    collection = page.collection
    return {
        "read_mode": page.read_mode,
        "read_scope_key": page.read_scope_key,
        "projection": _projection_json(page.projection),
        "collection": {
            "anchor_catalog_source": collection.anchor_catalog_source,
            "anchor_table_ref": collection.anchor_table_ref,
            "anchor_column_ref": collection.anchor_column_ref,
            "table_known": collection.table_known,
            "summary": {"suggested": collection.summary.suggested,
                        "design_checked": collection.summary.design_checked,
                        "needs_external_validation":
                            collection.summary.needs_external_validation,
                        "groups": collection.summary.groups},
            "groups": [{"entity": _label_json(g.entity),
                        "contextual_entity_terms": [attributed_text_to_json(t)
                                                    for t in g.contextual_entity_terms],
                        "grain_refs": [_ref_json(ref) for ref in g.grain_refs],
                        "suggestion_ids": list(g.suggestion_ids)}
                       for g in collection.groups],
            "rejections": [{"template_id": r.template_id, "candidate_name": r.candidate_name,
                            "code": r.code, "explanation": r.explanation}
                           for r in collection.rejections],
            "neighbourhood": (collection.neighbourhood.as_metadata()
                              if collection.neighbourhood is not None else None),
            "omitted_counts": dict(collection.omitted_counts),
        },
        "hits": [{"suggestion": suggestion_to_json(hit.suggestion),
                  "projection": _projection_json(hit.projection),
                  "provenance": _provenance_json(hit.provenance)} for hit in page.hits],
        "facets": {name: [{"id": b.id, "display_name": b.display_name, "count": b.count}
                          for b in buckets] for name, buckets in page.facets.items()},
        "next_cursor": page.next_cursor,
    }
