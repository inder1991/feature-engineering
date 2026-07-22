"""D2 — the frozen contracts for the deterministic semantic-binding shortlist.

Immutable, hashable dataclasses + the CLOSED disposition / binding-kind / reason-code registries.
A :class:`SemanticBindingCandidate` is a PROPOSED relationship (a column IS a business entity, or a
measure's currency is that column) enumerated PURELY from the server-supplied roster — never an LLM
invention. It maps 1:1 onto D1's ``CandidateInput`` (store) and E1's governed fact command (propose)
without carrying any mutable state.

The binding-kind / disposition string values MATCH D1's ``store_projection`` and E1's ``facts.py``
(cross-checked in the tests) so a candidate flows into both without translation.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace

from featuregen.overlay.field_evidence import canonical_hash

# ---- closed binding kinds (MATCH D1 store + E1 facts) -------------------------------------------
CURRENCY_BINDING = "currency_binding"
ENTITY_ASSIGNMENT = "entity_assignment"
BINDING_KINDS = frozenset({CURRENCY_BINDING, ENTITY_ASSIGNMENT})

# ---- closed dispositions (MATCH D1 store) ------------------------------------------------------
STRONG = "strong"
WEAK = "weak"
REJECTED = "rejected"
DISPOSITIONS = frozenset({STRONG, WEAK, REJECTED})

# ---- closed reason-code registry (durable — a rejected/weak candidate ALWAYS carries one) ------
RC_SUBJECT_NOT_IN_ROSTER = "subject_not_in_roster"
RC_TARGET_NOT_IN_ROSTER = "target_not_in_roster"
RC_SUBJECT_ROLE_MISMATCH = "subject_role_mismatch"
RC_TARGET_ROLE_MISMATCH = "target_role_mismatch"
RC_AMBIGUOUS_TARGET = "ambiguous_target"
RC_ENTITY_NOT_KNOWN = "entity_not_known"
RC_MISSING_ENTITY_VALUE = "missing_entity_value"
RC_OVER_BOUND = "over_bound"
RC_UNKNOWN_BINDING_KIND = "unknown_binding_kind"
REASON_CODES = frozenset({
    RC_SUBJECT_NOT_IN_ROSTER, RC_TARGET_NOT_IN_ROSTER, RC_SUBJECT_ROLE_MISMATCH,
    RC_TARGET_ROLE_MISMATCH, RC_AMBIGUOUS_TARGET, RC_ENTITY_NOT_KNOWN, RC_MISSING_ENTITY_VALUE,
    RC_OVER_BOUND, RC_UNKNOWN_BINDING_KIND,
})


@dataclass(frozen=True, slots=True)
class ColumnRef:
    """A reference to ONE column of the server roster. Carries exactly the identity a candidate
    needs to (a) render D1's ``graph_ref`` / ``logical_ref`` strings and (b) build E1's
    ``CatalogObjectRef`` — never a free-text / LLM FQN. ``graph_ref`` is the dotted display form
    (``schema.table.column``, matching ``overlay.identity.display_object_ref``); ``logical_ref`` is
    the source-scoped, round-trippable form (``source::schema.table.column``)."""

    catalog_source: str
    schema: str
    table: str
    column: str
    logical_ref: str

    @property
    def graph_ref(self) -> str:
        return f"{self.schema}.{self.table}.{self.column}"

    @classmethod
    def from_view(cls, col: object) -> ColumnRef:
        """Build a ColumnRef from a ``ColumnMetadataView`` (D2 never reads sample values — identity
        fields only)."""
        return cls(catalog_source=col.source, schema=col.schema, table=col.table,
                   column=col.column, logical_ref=col.logical_ref)


@dataclass(frozen=True, slots=True)
class Evidence:
    """The deterministic, hashable justification for a candidate — the ordered signal codes that
    fired (structural name / concept / declared facet), and the subject/target concepts. NO sample
    values, NO LLM output. Serialized verbatim into D1's immutable ``evidence_json``."""

    signals: tuple[str, ...] = ()
    subject_concept: str | None = None
    target_concept: str | None = None

    def to_json(self) -> dict[str, object]:
        return {"signals": list(self.signals), "subject_concept": self.subject_concept,
                "target_concept": self.target_concept}


@dataclass(frozen=True, slots=True)
class SemanticBindingCandidate:
    """One proposed semantic binding. IMMUTABLE + HASHABLE. ``target`` is the currency column for
    ``currency_binding`` and ``None`` for ``entity_assignment``; ``entity_id`` is the closed-vocabulary
    entity for ``entity_assignment`` and ``None`` for ``currency_binding`` (mirrors the D1 kind shape).
    ``input_hash`` is a deterministic hash of the candidate's identity — it backs D1's idempotent
    ``candidate_id`` minting, so the SAME candidate always mints the SAME id."""

    binding_kind: str
    subject: ColumnRef
    disposition: str
    input_hash: str
    evidence: Evidence = field(default_factory=Evidence)
    reason_codes: tuple[str, ...] = ()
    target: ColumnRef | None = None
    entity_id: str | None = None

    def sort_key(self) -> tuple[str, str, str, str, str]:
        """Total order for the deterministic candidate tuple — same inputs → identical ordering."""
        return (self.binding_kind, self.subject.graph_ref,
                self.target.graph_ref if self.target is not None else "",
                self.entity_id or "", self.disposition)

    def rejected_with(self, reason_code: str) -> SemanticBindingCandidate:
        """Return a REJECTED copy carrying ``reason_code`` (deduped) — the fail-closed transform.
        Never mutates; never drops the candidate (the reason is durable)."""
        codes = self.reason_codes if reason_code in self.reason_codes \
            else (*self.reason_codes, reason_code)
        return replace(self, disposition=REJECTED, reason_codes=codes)


def candidate_input_hash(*, binding_kind: str, subject_graph_ref: str,
                         target_graph_ref: str | None, entity_id: str | None) -> str:
    """The deterministic per-candidate ``input_hash`` — a pure function of the candidate's identity
    dims (never sample values / clocks / randomness)."""
    return canonical_hash({
        "binding_kind": binding_kind, "subject_graph_ref": subject_graph_ref,
        "target_graph_ref": target_graph_ref, "entity_id": entity_id,
    })
