"""Suggestion identity — the stable candidate id, the immutable revision id and the scope key.

Three content-addressed identities, all minted through the shared JCS hasher
(:func:`featuregen.canonical.contract_hash_v1`), all with their inputs frozen by 0F-8/0F-10:

* ``suggestion_id`` — WHICH LOGICAL CANDIDATE this is. Recipe, bound parameters, the bound
  operands in their declared roles, the resolved entity / ordered grain / time anchor, and the
  per-operand relationship-path assignment. Deliberately independent of the SCREEN it was opened
  from, of the validation OUTCOME and of every build observation, so opening the same cross-table
  candidate from either of its operand tables — or from a global page — yields one id.
* ``suggestion_revision_id`` — WHICH EXACT CONTENT produced this rendering of it. Everything the
  meaning rested on, including the grounding decision and the validation result. The decision
  enters as :func:`build_universe_independent_trace_hash`, NOT as the trace's own
  ``trace_content_hash``: that one folds in the candidate key, whose tie sets are a property of the
  universe the grounding pass ran over (anchor table + neighbourhood, bounded by ``max_hops`` and
  filtered by read scope), so hashing it gave one logical candidate two revisions.
* ``SuggestionReadScopeV1.scope_key`` — WHICH VISIBILITY PROFILE the read ran under, as the
  canonical class tuple, never a user id and never a raw role list.

**The relationship-path input, precisely.** The plan's original wording put "the ordered logical
relationship path" into ``suggestion_id``. ``GroundingDecisionTraceV1.ordered_relationship_path``
is, as amended in 0F-7, a DEDUPLICATED LEG SET across the candidate's per-operand paths — so two
candidates whose operands SWAPPED chains carry the identical field and an identity derived from it
would fuse them into one card, breaking rule 23. The identity-bearing material named by that
amendment is the per-operand ``JOIN_PATH`` pin assignment — but projected onto its LOGICAL identity,
``(relationship_kind, from_ref, to_ref)`` per leg, NOT the pin's ``content_hash``. That hash also
covers ``approved_join_fact_key`` / ``approved_join_status`` / ``edge_authority``, so hashing it made
an admin CONFIRMING a file-declared join re-key every suggestion crossing it — which rule 23
forbids in as many words ("the logical path enters ``suggestion_id``; exact realization/dependency
revisions enter the revision"). :func:`join_path_assignment` performs that projection and fails
closed if it cannot.

The alternative the amendment also permits — hashing ``trace_content_hash`` itself — is deliberately
NOT taken for ``suggestion_id``: that hash also covers the validation status, the requirements and
every governed read, so a candidate whose type attestation was re-read would become a DIFFERENT
LOGICAL CANDIDATE. That content belongs to the revision, and the revision does hash it (0F-10), so
nothing is lost.

**Exclusion by signature, not by discipline.** Producer commits, refresh ids, snapshot ids,
timestamps, evidence occurrence ids and registry-wide fencing hashes are not merely "not passed" —
they are not parameters at all, so a later careless edit cannot slip one in without changing a
signature that tests pin.
"""
from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from featuregen.canonical import contract_hash_v1
from featuregen.contracts.contract_versions import register_contract_version
from featuregen.overlay.upload.grounding_trace import (
    JOIN_PATH,
    GroundingDecisionTraceV1,
    build_universe_independent_content,
    build_universe_independent_pins,
)
from featuregen.overlay.upload.read_scope import allowed_classes

__all__ = [
    "PRODUCER_CONTRACT_VERSION",
    "READ_SCOPE_CONTRACT",
    "READ_SCOPE_SCHEMA_VERSION",
    "SUGGESTION_ID_CONTRACT",
    "SUGGESTION_REVISION_CONTRACT",
    "SUGGESTION_CONTRACT_VERSION",
    "TRACE_PROJECTION_CONTRACT",
    "SuggestionReadScopeV1",
    "UnresolvableRelationshipPath",
    "build_read_scope",
    "build_universe_independent_trace_hash",
    "dependency_content_hashes",
    "join_path_assignment",
    "suggestion_id",
    "suggestion_revision_id",
]

_OWNER = "featuregen.overlay.upload.suggestion_identity"

#: The frozen contract version of the two v2 suggestion identities (0F-10).
SUGGESTION_CONTRACT_VERSION = "2"
SUGGESTION_ID_CONTRACT = "feature-suggestion-id"
SUGGESTION_REVISION_CONTRACT = "feature-suggestion-revision"
#: The grounding decision, projected onto the part of it that does not depend on WHICH COLUMNS were
#: in scope when it was built (see :func:`build_universe_independent_trace_hash`). Its own contract
#: name because it is a different claim from ``trace_content_hash``, not a re-spelling of it.
TRACE_PROJECTION_CONTRACT = "grounding-decision-trace-projection"
READ_SCOPE_CONTRACT = "suggestion-read-scope"
READ_SCOPE_CONTRACT_VERSION = "1"

#: The read-scope contract's own schema label, carried on the value object (0F-8).
READ_SCOPE_SCHEMA_VERSION = "suggestion-read-scope-v1"

#: The explicit version of THE SUGGESTION BUILDER CONTRACT — what this producer means by the
#: fields it assembles (0F-10 requires it in the revision; the freeze left the literal to the
#: first implementer, so it is pinned here and owned here). It is NOT a Git commit, a deployment
#: id or a build observation: those are provenance and are excluded from every hash below. Bump it
#: when the MEANING of an assembled field changes, which correctly re-revisions every suggestion.
PRODUCER_CONTRACT_VERSION = "suggestion-producer-v1"


# ── the visibility scope (0F-8) ─────────────────────────────────────────────────────────────────
@dataclass(frozen=True, slots=True)
class SuggestionReadScopeV1:
    """The canonical visibility profile a suggestion read ran under.

    ``allowed_classes`` is the sorted output of :func:`read_scope.allowed_classes`, which ignores
    every claim that is not one of the three reader roles — so functional roles and user ids
    provably cannot mint scope variants, and ``{feature_engineer, pii_reader}`` and
    ``{data_owner, pii_reader}`` are ONE scope. ``tenant`` stays ``None`` until Task 0P declares
    otherwise (shared rule 22). ``scope_key`` is an opaque diagnostics/cursor key — never itself
    an authorization.
    """

    schema_version: str
    tenant: str | None
    allowed_classes: tuple[str, ...]
    scope_key: str


def build_read_scope(roles: Iterable[str], *, tenant: str | None = None) -> SuggestionReadScopeV1:
    """The caller's canonical scope. Derived from role CLAIMS only — never from a client-supplied
    scope key, a user id or a raw role list echoed back onto the wire."""
    classes = tuple(allowed_classes(roles))
    payload = {"schema_version": READ_SCOPE_SCHEMA_VERSION, "tenant": tenant,
               "allowed_classes": list(classes)}
    return SuggestionReadScopeV1(
        schema_version=READ_SCOPE_SCHEMA_VERSION, tenant=tenant, allowed_classes=classes,
        scope_key=contract_hash_v1(READ_SCOPE_CONTRACT, READ_SCOPE_CONTRACT_VERSION, payload))


# ── reading the identity-bearing material off a trace ───────────────────────────────────────────
class UnresolvableRelationshipPath(RuntimeError):
    """A ``JOIN_PATH`` pin names a realization the trace's own path does not contain.

    The logical projection below cannot be completed, so there is no honest identity to mint. The
    caller REFUSES the candidate and counts the refusal; it must never fall back to the physical
    hash, which would silently reintroduce the churn that projection exists to prevent.
    """


#: One traversed leg's LOGICAL identity: what kind of relationship, from where, to where. Direction
#: is preserved (a reverse hop is a different traversal); attestation state deliberately is not.
_LogicalLeg = tuple[str, str, str, str, str]


def join_path_assignment(trace: GroundingDecisionTraceV1 | None
                         ) -> tuple[tuple[str, tuple[_LogicalLeg, ...]], ...]:
    """The per-operand relationship-path ASSIGNMENT, projected onto its LOGICAL identity.

    For each ``JOIN_PATH`` pin: its ``dependency_key`` (the operand) paired with that operand's own
    ordered legs, each reduced to ``(relationship_kind, from_ref, to_ref)``. The legs are recovered
    by joining the pin's ``path_realization_hashes`` against ``ordered_relationship_path``, whose
    entries carry kind and endpoints alongside their realization hash. Sorted by operand, because
    the assignment is a mapping, not a sequence.

    **Why the projection, and not the pin's ``content_hash``.** That hash covers
    :func:`~featuregen.overlay.upload.grounding_trace.join_path_pin_content`, whose per-leg
    realization content includes ``approved_join_fact_key``, ``approved_join_status`` and
    ``edge_authority``. Hashing it into ``suggestion_id`` meant that an admin CONFIRMING a
    file-declared join — same recipe, same columns, same endpoints, same direction, pure governance
    provenance — minted a brand-new stable identity for every suggestion crossing it. Plan rule 23
    says the opposite in as many words: "The logical path enters ``suggestion_id``; exact
    realization/dependency revisions enter the revision." Those realization hashes DO travel, in
    ``suggestion_revision_id``, via :func:`dependency_content_hashes`.

    **Rule 23 is still satisfied**, on both of its limbs: a different ordered path changes the leg
    tuple, and two candidates whose operands SWAPPED chains differ because the tuple is keyed per
    operand.

    Raises:
        UnresolvableRelationshipPath: a pinned realization has no matching path entry. Fail closed
            — an identity built from a partial chain is not the identity of anything.
    """
    if trace is None:
        return ()
    legs_by_hash = {leg.realization_content_hash: leg
                    for leg in trace.ordered_relationship_path
                    if leg.realization_content_hash is not None}
    assignment: list[tuple[str, tuple[_LogicalLeg, ...]]] = []
    for pin in trace.dependency_pins:
        if pin.dependency_kind != JOIN_PATH:
            continue
        legs: list[_LogicalLeg] = []
        for realization_hash in pin.path_realization_hashes:
            leg = legs_by_hash.get(realization_hash)
            if leg is None:
                raise UnresolvableRelationshipPath(
                    f"JOIN_PATH pin {pin.dependency_key!r} names realization "
                    f"{realization_hash!r}, which the trace's ordered_relationship_path does not "
                    f"contain; the logical chain cannot be projected and no identity is minted")
            legs.append((leg.relationship_kind, leg.from_ref[0], leg.from_ref[1],
                         leg.to_ref[0], leg.to_ref[1]))
        assignment.append((pin.dependency_key, tuple(legs)))
    return tuple(sorted(assignment, key=lambda entry: entry[0]))


def build_universe_independent_trace_hash(trace: GroundingDecisionTraceV1) -> str:
    """The grounding decision as the REVISION hashes it: the trace's content with everything that
    depends on WHICH COLUMNS WERE IN SCOPE WHEN IT WAS BUILT projected out — the ``candidate_key``
    and the caller-scoped dependency pins (see
    :func:`~featuregen.overlay.upload.grounding_trace.build_universe_independent_content`).

    **Why the revision cannot hash ``trace_content_hash`` directly.** That hash covers
    ``candidate_key``, i.e. ``recipe_candidate_key``, one of whose inputs is
    ``binding_resolution_hash`` — a fold of each role's ``tied_candidate_set_hash``, the set of
    columns that TIED for that role in the universe the grounding pass happened to run over. That
    universe is the anchor table plus its join neighbourhood, bounded by the caller's ``max_hops``
    and filtered by the caller's read scope. So the SAME winning binding of the SAME recipe over the
    SAME operands yields two different trace hashes when opened from two different operand tables of
    an asymmetric join graph — and would have yielded two ``suggestion_revision_id`` values under one
    ``suggestion_id``. The plan's Identity rules forbid exactly that: "The canonical suggestion
    payload is also anchor-independent. Requested table, neighbourhood limits, page truncation and
    search cursor belong to ``FeatureSuggestionPageV2``." Invisible in Release A, where both surfaces
    read one anchor and nothing is persisted; permanent in Release B, where rule 26 / DoD 17 would
    withhold a suggestion whose revision bytes disagree with themselves.

    **Why not fix ``trace_content_hash`` instead.** That hash is the TRACE's own identity — the
    identity of one grounding decision, made in one universe, over one read scope — and the tie set
    genuinely is part of what that decision was. It is pinned by its own tests and by
    ``recompute_trace_content_hash``'s tamper check. The suggestion asks a different question
    ("which content produced this rendering of this logical candidate"), so it gets its own
    projection and its own contract name rather than redefining the trace's.

    The tie set is not discarded, it is RECLASSIFIED as BUILD provenance: it still reaches a reader
    through ``FeatureSuggestionV2.grounding_trace_content_hash``, the unprojected trace hash, which
    is how two builds of the same candidate are compared. Everything the plan requires the revision
    to move on still moves: the ordered relationship path, the candidate's dependency pins, the
    requirements, the validation status and the evaluated rule hashes are all inside this
    projection, and the recipe/discovery/semantic/profile content is hashed alongside it by
    :func:`suggestion_revision_id`.
    """
    return contract_hash_v1(TRACE_PROJECTION_CONTRACT, SUGGESTION_CONTRACT_VERSION,
                            build_universe_independent_content(trace))


def dependency_content_hashes(trace: GroundingDecisionTraceV1 | None) -> tuple[str, ...]:
    """The sorted, deduplicated content hashes of everything the decision read about the CANDIDATE —
    every dependency pin plus every traversed leg's selected realization (0F-10's
    "relationship/dependency content hashes"). Content only: ``current_revision_id`` and evidence
    occurrence ids are provenance and never appear here.

    The caller-scoped pins (:data:`~featuregen.overlay.upload.grounding_trace
    .BUILD_SCOPE_DEPENDENCY_KINDS` — today the read-scope pin, whose content is the caller's own
    visibility classes) are projected out for the same reason
    :func:`build_universe_independent_trace_hash` drops them: the revision of one logical candidate
    may not depend on WHO read it. That scope travels on the page, as
    ``SuggestionReadScopeV1.scope_key``."""
    if trace is None:
        return ()
    hashes = {pin.content_hash for pin in build_universe_independent_pins(trace)}
    hashes |= {leg.realization_content_hash for leg in trace.ordered_relationship_path
               if leg.realization_content_hash is not None}
    return tuple(sorted(hashes))


# ── the two identities ──────────────────────────────────────────────────────────────────────────
def _ref_json(ref: tuple[str, str] | None) -> list[str] | None:
    return None if ref is None else [ref[0], ref[1]]


def suggestion_id(*, template_id: str | None,
                  bound_params: Sequence[tuple[str, Any]],
                  operands: Sequence[tuple[str, str, str]],
                  entity_id: str | None,
                  grain_refs: Sequence[tuple[str, str]],
                  time_ref: tuple[str, str] | None,
                  relationship_path_assignment: Sequence[tuple[str, Sequence[_LogicalLeg]]]
                  ) -> str:
    """The stable logical-candidate identity (0F-10).

    **Which refs are logical and which are physical — exactly.** ``operands`` are
    ``(catalog_source, logical_ref, recipe_role)`` triples: the LOGICAL ref, read off the engine's
    own ``need_bindings``, so a physical re-spelling of the same logical column does not fork the
    OPERAND SET. That is NOT true of the identity as a whole, and claiming it would be a promise
    this function does not keep:

    * ``operands`` — LOGICAL (``GroundedNeedBinding.logical_ref``);
    * ``grain_refs`` and ``time_ref`` — PHYSICAL ``graph_object_ref``s, passed by
      ``suggestion_contract`` as ``(catalog_source, object_ref)``;
    * ``relationship_path_assignment`` — PHYSICAL on both limbs: each entry is keyed by the
      ``JOIN_PATH`` pin's ``dependency_key`` (built from the operand's physical ref) and its legs
      carry the edge's physical endpoint refs.

    So a physical re-spelling of a bound column DOES currently fork the id, through the grain/time
    anchors and through the path assignment. Converting those to logical refs is a Release-B
    decision — it re-keys every existing suggestion, and the logical mapping for a relationship
    ENDPOINT (which is a graph edge, not a bound need) does not exist yet — so it is recorded in the
    freeze doc's deviation log rather than taken here.

    Sorted: role bindings are a set. ``grain_refs`` is ORDERED (rule 25: composite grain is a
    sequence of key operands, and reordering it is a different grouping).
    ``relationship_path_assignment`` comes from :func:`join_path_assignment` and from nothing else.

    The requested anchor table, the join neighbourhood bounds, page truncation and cursors are
    absent BY SIGNATURE: they belong to ``FeatureSuggestionPageV2`` / ``SuggestionCollectionContextV2``
    and would otherwise give one logical candidate two ids (or, worse, one id with conflicting
    canonical bytes) depending on which of its operand tables the reader opened.
    """
    payload = {
        "template_id": template_id,
        "bound_params": [[name, value] for name, value in bound_params],
        "operands": sorted([source, logical_ref, role]
                           for source, logical_ref, role in operands),
        "entity": {
            "entity_id": entity_id,
            "grain_refs": [[source, ref] for source, ref in grain_refs],
            "time_ref": _ref_json(time_ref),
        },
        # Per-operand LOGICAL chains (see `join_path_assignment`): the ORDER of a chain is meaning,
        # the order of the operands is not. Governance attestation is deliberately absent — it
        # belongs to the revision (rule 23).
        "relationship_path_assignment": sorted(
            ([key, [list(leg) for leg in legs]] for key, legs in relationship_path_assignment),
            key=lambda entry: entry[0]),
    }
    return contract_hash_v1(SUGGESTION_ID_CONTRACT, SUGGESTION_CONTRACT_VERSION, payload)


def suggestion_revision_id(*, suggestion_id: str,
                           recipe_revision_id: str | None,
                           discovery_metadata_revision_id: str | None,
                           semantic_context_hashes: Sequence[str],
                           dataset_profile_hashes: Sequence[str],
                           trace_projection_hash: str,
                           dependency_content_hashes: Sequence[str],
                           validation_rule_content_hashes: Sequence[str],
                           read_scope_rule_content_hashes: Sequence[str],
                           validation_status: str,
                           producer_contract_version: str = PRODUCER_CONTRACT_VERSION) -> str:
    """The immutable content revision (0F-10).

    Everything meaning-bearing enters: the logical identity, the referenced recipe and discovery
    revisions, the referenced semantic-context and dataset-profile content, the grounding decision
    (its build-universe-independent projection, its dependency/realization content hashes, the exact
    rules it evaluated) and the validation result.

    Everything OBSERVATIONAL stays out, and cannot be passed: raw catalog snapshot ids, evidence
    occurrence/event ids, realization revision ids, refresh ids, timestamps, producer commit,
    deployment/job identity and the registry-wide fencing hashes. Byte-identical content that is
    re-uploaded, re-authored or rebuilt by a new commit therefore REUSES this revision (rule 24),
    while a genuinely different attestation moves it.

    ``trace_projection_hash`` is :func:`build_universe_independent_trace_hash`, NOT the trace's own
    ``trace_content_hash``: the latter folds in the tie sets of the universe the pass ran over, so
    it moves with the anchor table, the ``max_hops`` bound and the caller's read scope — none of
    which a revision of one logical candidate may depend on. The parameter is named for what it is
    so a call site cannot pass the wrong hash by keyword without noticing.
    """
    payload = {
        "suggestion_id": suggestion_id,
        "recipe_revision_id": recipe_revision_id,
        "discovery_metadata_revision_id": discovery_metadata_revision_id,
        # SETS: the collection order of these hashes is an implementation detail.
        "semantic_context_hashes": sorted(set(semantic_context_hashes)),
        "dataset_profile_hashes": sorted(set(dataset_profile_hashes)),
        "grounding_trace_projection_hash": trace_projection_hash,
        "dependency_content_hashes": sorted(set(dependency_content_hashes)),
        "validation_rule_content_hashes": sorted(set(validation_rule_content_hashes)),
        "read_scope_rule_content_hashes": sorted(set(read_scope_rule_content_hashes)),
        "validation_status": validation_status,
        "producer_contract_version": producer_contract_version,
    }
    return contract_hash_v1(SUGGESTION_REVISION_CONTRACT, SUGGESTION_CONTRACT_VERSION, payload)


register_contract_version(TRACE_PROJECTION_CONTRACT, SUGGESTION_CONTRACT_VERSION, owner=_OWNER)
register_contract_version(SUGGESTION_ID_CONTRACT, SUGGESTION_CONTRACT_VERSION, owner=_OWNER)
register_contract_version(SUGGESTION_REVISION_CONTRACT, SUGGESTION_CONTRACT_VERSION, owner=_OWNER)
register_contract_version(READ_SCOPE_CONTRACT, READ_SCOPE_CONTRACT_VERSION, owner=_OWNER)
