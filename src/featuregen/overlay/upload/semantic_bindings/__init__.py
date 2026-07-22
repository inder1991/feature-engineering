"""Delivery D — the semantic-binding candidate store (immutable candidate sets + the mutable
compare-and-swap current-projection). D1 owns the schema (migration 1014) and the persistence
contract in :mod:`store_projection`; D2 builds the deterministic shortlist / validate / store /
propose on top; D3/D4 add the LLM selection + wiring.

D2 public surface:

* :mod:`types` — frozen contracts (``SemanticBindingCandidate``, ``ColumnRef``, ``Evidence``) +
  the closed disposition / binding-kind / reason-code registries.
* :mod:`shortlist` — the PURE deterministic ``shortlist(table_view, pass_b, pass_c)`` (roster-only,
  no LLM, no sample-shape).
* :mod:`validate` — referent / role / ambiguity / bound checks (fail closed, never silent-drop).
* :mod:`store` — the thin wrapper over D1's ``store_projection`` (persist + CAS current projection).
* :mod:`propose` — map a strong candidate → E1's ``entity_assignment`` / ``currency_binding`` DRAFT
  fact command (never a VERIFIED fact); link the proposal only after ``propose_fact`` succeeds.
"""
from featuregen.overlay.upload.semantic_bindings.enrich import (
    SEMANTIC_BINDINGS_TASK,
    EnrichResult,
    enrich_semantic_bindings,
)
from featuregen.overlay.upload.semantic_bindings.propose import (
    ProposeOutcome,
    propose,
    to_fact_command,
)
from featuregen.overlay.upload.semantic_bindings.shortlist import (
    PassBColumn,
    PassCIdentifier,
    shortlist,
)
from featuregen.overlay.upload.semantic_bindings.store import (
    StoreResult,
    candidate_id_for,
    link_proposal,
    store_shortlist,
    table_fingerprint,
    table_graph_ref,
    to_candidate_input,
)
from featuregen.overlay.upload.semantic_bindings.types import (
    BINDING_KINDS,
    DISPOSITIONS,
    REASON_CODES,
    ColumnRef,
    Evidence,
    SemanticBindingCandidate,
)
from featuregen.overlay.upload.semantic_bindings.validate import (
    DEFAULT_CANDIDATE_CAP,
    ValidationOutcome,
    validate,
    validate_candidates,
)

__all__ = [
    "BINDING_KINDS",
    "DEFAULT_CANDIDATE_CAP",
    "DISPOSITIONS",
    "REASON_CODES",
    "SEMANTIC_BINDINGS_TASK",
    "ColumnRef",
    "EnrichResult",
    "Evidence",
    "PassBColumn",
    "PassCIdentifier",
    "ProposeOutcome",
    "SemanticBindingCandidate",
    "StoreResult",
    "ValidationOutcome",
    "candidate_id_for",
    "enrich_semantic_bindings",
    "link_proposal",
    "propose",
    "shortlist",
    "store_shortlist",
    "table_fingerprint",
    "table_graph_ref",
    "to_candidate_input",
    "to_fact_command",
    "validate",
    "validate_candidates",
]
