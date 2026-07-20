"""D2 — the PURE, deterministic candidate shortlist (NO LLM, NO sample-shape, NO randomness).

``shortlist(table_view, pass_b, pass_c)`` enumerates semantic-binding candidates from the SERVER
ROSTER alone — every subject and every target is a column of ``table_view.columns``; a raw or
LLM-invented FQN can never appear. The seven rules from the brief, as implemented:

1. Targets come ONLY from ``table_view.columns`` (the explicit server roster). The function never
   accepts a bare string ref; a currency target is picked FROM a roster column, an entity value is
   a closed-vocabulary member.
2. No raw / LLM-generated FQN is accepted (there is no LLM here) — the roster is the only source.
3. **Currency** candidates are shortlisted from structural column names, curated business
   concepts (the ``monetary`` / ``currency`` taxonomy groups), and declared semantic facets. A
   measure with EXACTLY ONE currency-eligible target → ``strong``; a measure with several equally
   plausible currency targets → each pairing is ``weak`` (never ``strong``) — the ambiguity is
   preserved for a reviewer, never resolved by guessing.
4. **Event-time is NOT a binding kind here** — it is owned by the Pass B availability fact
   path/lifecycle. This function NEVER emits an event-time candidate.
5. **Entity** candidates target identifier-eligible columns (Pass C identifier metadata) whose
   resolved entity is a member of ``known_entities()``. An eligible column whose entity is NOT
   known is emitted ``rejected`` (never silently dropped); a non-identifier column is not an entity
   candidate at all.
6. ``term_type == 'measure'`` excludes a column from entity candidacy; an OPEN-vocabulary
   term_type never classifies a column by itself (it is used only to EXCLUDE, never to assert).
7. NO sample-value shape inference — only declared metadata (names / concepts / facets) is read.

Determinism: the roster is iterated in its given order, and the full result is sorted by
:meth:`SemanticBindingCandidate.sort_key`, so the SAME ``(table_view, pass_b, pass_c)`` always
returns an identical candidate tuple (order + content).
"""
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY
from featuregen.overlay.upload.semantic_bindings.types import (
    CURRENCY_BINDING,
    ENTITY_ASSIGNMENT,
    RC_ENTITY_NOT_KNOWN,
    STRONG,
    WEAK,
    ColumnRef,
    Evidence,
    SemanticBindingCandidate,
    candidate_input_hash,
)
from featuregen.overlay.upload.taxonomy.dimensions import known_entities

# Concepts that DENOTE a currency (the monetary UNIT itself) — a currency_binding TARGET. FX rates
# (fx_conversion_rate / cross_rate) are rates, NOT currency codes, so they are deliberately excluded.
_CURRENCY_CODE_CONCEPTS = frozenset({"currency_code", "base_currency", "local_currency"})

# Structural currency column-name tokens (exact) + suffixes — the "structural column names" signal.
_CURRENCY_NAME_EXACT = frozenset({"ccy", "curr", "currency", "currency_code", "ccy_code",
                                  "iso_currency", "iso_ccy", "currency_iso"})
_CURRENCY_NAME_SUFFIXES = ("_ccy", "_curr", "_currency", "_currency_code", "_ccy_code")

# Declared semantic-facet values (view.semantic_type) that mark a currency column.
_CURRENCY_FACETS = frozenset({"currency", "currency_code", "iso_currency_code"})

# Structural measure/amount name tokens — the "structural column names" signal for the subject.
_MEASURE_NAME_TOKENS = frozenset({
    "amount", "amt", "balance", "bal", "notional", "price", "value", "val", "fee", "principal",
    "cost", "revenue", "charge", "premium", "exposure", "pnl", "proceeds",
})


@dataclass(frozen=True, slots=True)
class PassBColumn:
    """Validated Pass B synthesis for one column, as the shortlist consumes it. Optional per-column
    override of the view-derived currency roles (``is_measure`` / ``is_currency``); ``is_grain`` /
    ``is_as_of`` are carried for completeness (event-time is Pass B's, never a candidate here)."""

    is_measure: bool = False
    is_currency: bool = False
    is_grain: bool = False
    is_as_of: bool = False


@dataclass(frozen=True, slots=True)
class PassCIdentifier:
    """Pass C identifier metadata for one column: whether it may anchor an identifier binding
    (``is_join_key_eligible`` — measures / unrecognized term_types excluded, per Pass C), and the
    RESOLVED entity it denotes (from ``column_entity`` / the concept's ``entity_link`` — server
    metadata, no LLM). ``entity`` is validated against ``known_entities()`` by the shortlist."""

    join_key_eligible: bool = False
    entity: str | None = None


def _tokens(name: str) -> set[str]:
    return {t for t in (name or "").lower().replace("-", "_").split("_") if t}


def _concept_group(concept: str | None) -> str | None:
    c = CONCEPT_REGISTRY.get((concept or "").strip().lower())
    return c.group if c is not None else None


def is_currency_column(col: object, pass_b: PassBColumn | None = None) -> bool:
    """A roster column that DENOTES a currency (a currency_binding target). Declared metadata only:
    a validated Pass B facet, a currency taxonomy concept, a structural name, or a declared semantic
    facet. NEVER sample-value shape."""
    if pass_b is not None and pass_b.is_currency:
        return True
    concept = (getattr(col, "concept", None) or "").strip().lower()
    if concept in _CURRENCY_CODE_CONCEPTS:
        return True
    name = (col.column or "").lower()
    if name in _CURRENCY_NAME_EXACT or name.endswith(_CURRENCY_NAME_SUFFIXES):
        return True
    facet = (getattr(col, "semantic_type", None) or "").strip().lower()
    return facet in _CURRENCY_FACETS


def is_measure_column(col: object, pass_b: PassBColumn | None = None) -> bool:
    """A roster column that is a MEASURE needing a currency (a currency_binding subject). A monetary
    taxonomy concept, ``term_type == 'measure'`` (curated business term), a structural amount name,
    or a validated Pass B flag. A currency column is never itself a measure subject."""
    if is_currency_column(col, pass_b):
        return False
    if pass_b is not None and pass_b.is_measure:
        return True
    if _concept_group(getattr(col, "concept", None)) == "monetary":
        return True
    if (getattr(col, "term_type", None) or "").strip().lower() == "measure":
        return True
    return bool(_tokens(col.column) & _MEASURE_NAME_TOKENS)


def _currency_candidates(
    columns: tuple, pass_b: Mapping[str, PassBColumn] | None,
) -> list[SemanticBindingCandidate]:
    pb = pass_b or {}
    currency_cols = [c for c in columns if is_currency_column(c, pb.get(c.logical_ref))]
    out: list[SemanticBindingCandidate] = []
    for col in columns:
        if not is_measure_column(col, pb.get(col.logical_ref)):
            continue
        targets = [t for t in currency_cols if t.logical_ref != col.logical_ref]
        if not targets:
            continue
        # EXACTLY ONE currency target → strong; several equally plausible → each pairing weak.
        disposition = STRONG if len(targets) == 1 else WEAK
        subject_ref = ColumnRef.from_view(col)
        for tgt in targets:
            target_ref = ColumnRef.from_view(tgt)
            signals = ["currency_target_unique"] if disposition == STRONG \
                else ["currency_target_ambiguous"]
            out.append(SemanticBindingCandidate(
                binding_kind=CURRENCY_BINDING, subject=subject_ref, target=target_ref,
                disposition=disposition,
                input_hash=candidate_input_hash(
                    binding_kind=CURRENCY_BINDING, subject_graph_ref=subject_ref.graph_ref,
                    target_graph_ref=target_ref.graph_ref, entity_id=None),
                evidence=Evidence(signals=tuple(signals),
                                  subject_concept=(getattr(col, "concept", None) or None),
                                  target_concept=(getattr(tgt, "concept", None) or None)),
            ))
    return out


def _entity_candidates(
    columns: tuple, pass_c: Mapping[str, PassCIdentifier] | None,
) -> list[SemanticBindingCandidate]:
    if not pass_c:                      # no identifier metadata → no entity candidates (roster-safe)
        return []
    known = known_entities()
    out: list[SemanticBindingCandidate] = []
    for col in columns:
        # Rule 6: a measure term_type is never an entity key, regardless of what Pass C supplied.
        if (getattr(col, "term_type", None) or "").strip().lower() == "measure":
            continue
        pc = pass_c.get(col.logical_ref)
        if pc is None or not pc.join_key_eligible or not pc.entity:
            continue
        subject_ref = ColumnRef.from_view(col)
        input_hash = candidate_input_hash(
            binding_kind=ENTITY_ASSIGNMENT, subject_graph_ref=subject_ref.graph_ref,
            target_graph_ref=None, entity_id=pc.entity)
        if pc.entity in known:
            out.append(SemanticBindingCandidate(
                binding_kind=ENTITY_ASSIGNMENT, subject=subject_ref, entity_id=pc.entity,
                disposition=STRONG, input_hash=input_hash,
                evidence=Evidence(signals=("identifier_eligible", "entity_known"),
                                  subject_concept=(getattr(col, "concept", None) or None))))
        else:
            # An eligible identifier whose entity is OUTSIDE the closed vocabulary — never silently
            # dropped; emitted rejected with a durable reason code.
            out.append(SemanticBindingCandidate(
                binding_kind=ENTITY_ASSIGNMENT, subject=subject_ref, entity_id=pc.entity,
                disposition="rejected", input_hash=input_hash, reason_codes=(RC_ENTITY_NOT_KNOWN,),
                evidence=Evidence(signals=("identifier_eligible", "entity_unknown"),
                                  subject_concept=(getattr(col, "concept", None) or None))))
    return out


def shortlist(
    table_view: object,
    pass_b: Mapping[str, PassBColumn] | None = None,
    pass_c: Mapping[str, PassCIdentifier] | None = None,
) -> tuple[SemanticBindingCandidate, ...]:
    """Enumerate the deterministic semantic-binding candidates for one table. PURE: no DB, no LLM,
    no randomness, no sample-shape. ``pass_b`` / ``pass_c`` are keyed by column ``logical_ref``.
    Same ``(table_view, pass_b, pass_c)`` → identical candidate tuple (order + content)."""
    columns = table_view.columns
    candidates = _currency_candidates(columns, pass_b) + _entity_candidates(columns, pass_c)
    return tuple(sorted(candidates, key=SemanticBindingCandidate.sort_key))
