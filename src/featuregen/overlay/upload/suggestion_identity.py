"""Suggestion identity — the stable candidate id, the immutable revision id and the scope key.

Three content-addressed identities, all minted through the shared JCS hasher
(:func:`featuregen.canonical.contract_hash_v1`), all with their inputs frozen by 0F-8/0F-10:

* ``suggestion_id`` — WHICH LOGICAL CANDIDATE this is. Recipe, bound parameters, the bound
  operands in their declared roles, the resolved entity / ordered grain / time anchor, and the
  per-operand relationship-path assignment. Deliberately independent of the SCREEN it was opened
  from, of the validation OUTCOME and of every build observation, so opening the same cross-table
  candidate from either of its operand tables — or from a global page — yields one id.
* ``suggestion_revision_id`` — WHICH EXACT CONTENT produced this rendering of it. Everything the
  meaning rested on, including the grounding trace hash and the validation result.
* ``SuggestionReadScopeV1.scope_key`` — WHICH VISIBILITY PROFILE the read ran under, as the
  canonical class tuple, never a user id and never a raw role list.

**The relationship-path input, precisely.** The plan's original wording put "the ordered logical
relationship path" into ``suggestion_id``. ``GroundingDecisionTraceV1.ordered_relationship_path``
is, as amended in 0F-7, a DEDUPLICATED LEG SET across the candidate's per-operand paths — so two
candidates whose operands SWAPPED chains carry the identical field and an identity derived from it
would fuse them into one card, breaking rule 23. The identity-bearing material named by that
amendment is the per-operand ``JOIN_PATH`` pin assignment: ``(dependency_key, content_hash)`` pairs,
where the content is :func:`~featuregen.overlay.upload.grounding_trace.join_path_pin_content` — that
operand's endpoints, outcome and ordered leg hashes. :func:`join_path_assignment` reads exactly
those pins and nothing else.

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
from featuregen.overlay.upload.grounding_trace import JOIN_PATH, GroundingDecisionTraceV1
from featuregen.overlay.upload.read_scope import allowed_classes

__all__ = [
    "PRODUCER_CONTRACT_VERSION",
    "READ_SCOPE_CONTRACT",
    "READ_SCOPE_SCHEMA_VERSION",
    "SUGGESTION_ID_CONTRACT",
    "SUGGESTION_REVISION_CONTRACT",
    "SUGGESTION_CONTRACT_VERSION",
    "SuggestionReadScopeV1",
    "build_read_scope",
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
def join_path_assignment(trace: GroundingDecisionTraceV1 | None
                         ) -> tuple[tuple[str, str], ...]:
    """The per-operand relationship-path ASSIGNMENT: sorted ``(dependency_key, content_hash)``
    pairs of the trace's ``JOIN_PATH`` pins.

    This — not ``ordered_relationship_path`` — is what rule 23 rests on: the flat leg set cannot
    tell two candidates apart when their operands swapped chains, while the pins can, because each
    pin's content is that operand's own endpoints, outcome and ordered leg hashes. Sorted because
    the assignment is a mapping, not a sequence: which operand the gauntlet classified first is not
    meaning. Every OTHER pin kind (governed reads, structural lookups, read scope) is deliberately
    excluded — it describes the content a revision rests on, not which candidate this is.
    """
    if trace is None:
        return ()
    return tuple(sorted((pin.dependency_key, pin.content_hash)
                        for pin in trace.dependency_pins if pin.dependency_kind == JOIN_PATH))


def dependency_content_hashes(trace: GroundingDecisionTraceV1 | None) -> tuple[str, ...]:
    """The sorted, deduplicated content hashes of everything the decision read — every dependency
    pin plus every traversed leg's selected realization (0F-10's "relationship/dependency content
    hashes"). Content only: ``current_revision_id`` and evidence occurrence ids are provenance and
    never appear here."""
    if trace is None:
        return ()
    hashes = {pin.content_hash for pin in trace.dependency_pins}
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
                  relationship_path_assignment: Sequence[tuple[str, str]]) -> str:
    """The stable logical-candidate identity (0F-10).

    ``operands`` are ``(catalog_source, logical_ref, recipe_role)`` triples — the LOGICAL ref, read
    off the engine's own ``need_bindings``, so a physical re-spelling of the same logical column
    does not fork the candidate. Sorted: role bindings are a set. ``grain_refs`` is ORDERED (rule
    25: composite grain is a sequence of key operands, and reordering it is a different grouping).
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
        "relationship_path_assignment": sorted([key, content_hash]
                                               for key, content_hash in
                                               relationship_path_assignment),
    }
    return contract_hash_v1(SUGGESTION_ID_CONTRACT, SUGGESTION_CONTRACT_VERSION, payload)


def suggestion_revision_id(*, suggestion_id: str,
                           recipe_revision_id: str | None,
                           discovery_metadata_revision_id: str | None,
                           semantic_context_hashes: Sequence[str],
                           dataset_profile_hashes: Sequence[str],
                           trace_content_hash: str,
                           dependency_content_hashes: Sequence[str],
                           validation_rule_content_hashes: Sequence[str],
                           read_scope_rule_content_hashes: Sequence[str],
                           validation_status: str,
                           producer_contract_version: str = PRODUCER_CONTRACT_VERSION) -> str:
    """The immutable content revision (0F-10).

    Everything meaning-bearing enters: the logical identity, the referenced recipe and discovery
    revisions, the referenced semantic-context and dataset-profile content, the grounding decision
    (its trace hash, its dependency/realization content hashes, the exact rules it evaluated) and
    the validation result.

    Everything OBSERVATIONAL stays out, and cannot be passed: raw catalog snapshot ids, evidence
    occurrence/event ids, realization revision ids, refresh ids, timestamps, producer commit,
    deployment/job identity and the registry-wide fencing hashes. Byte-identical content that is
    re-uploaded, re-authored or rebuilt by a new commit therefore REUSES this revision (rule 24),
    while a genuinely different attestation moves it.
    """
    payload = {
        "suggestion_id": suggestion_id,
        "recipe_revision_id": recipe_revision_id,
        "discovery_metadata_revision_id": discovery_metadata_revision_id,
        # SETS: the collection order of these hashes is an implementation detail.
        "semantic_context_hashes": sorted(set(semantic_context_hashes)),
        "dataset_profile_hashes": sorted(set(dataset_profile_hashes)),
        "grounding_trace_content_hash": trace_content_hash,
        "dependency_content_hashes": sorted(set(dependency_content_hashes)),
        "validation_rule_content_hashes": sorted(set(validation_rule_content_hashes)),
        "read_scope_rule_content_hashes": sorted(set(read_scope_rule_content_hashes)),
        "validation_status": validation_status,
        "producer_contract_version": producer_contract_version,
    }
    return contract_hash_v1(SUGGESTION_REVISION_CONTRACT, SUGGESTION_CONTRACT_VERSION, payload)


register_contract_version(SUGGESTION_ID_CONTRACT, SUGGESTION_CONTRACT_VERSION, owner=_OWNER)
register_contract_version(SUGGESTION_REVISION_CONTRACT, SUGGESTION_CONTRACT_VERSION, owner=_OWNER)
register_contract_version(READ_SCOPE_CONTRACT, READ_SCOPE_CONTRACT_VERSION, owner=_OWNER)
