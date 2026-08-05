"""Shared evidence-axis contracts — the frozen home (Task 0S; freeze 0F-4/0F-6, ledger §2).

One definition, cross-plan: ``EvidenceAuthorityV1``, ``SemanticValueV1``,
``AttributedLabelV1`` and ``AttributedTextV1`` live HERE and are imported by every later
plan (semantic Task 1, suggestion Tasks 1/2, projections) — never copied. The three axis
vocabularies are the existing ``featuregen.overlay.evidence`` enums, imported and
re-exported; no five-value authority enum exists or may be created (ledger §2). One value
may carry source, LLM and human evidence simultaneously; collapsing to one "best
authority" is forbidden (0F-6).

Hash canonicalization (0F-4 rule 3): inside any semantic hash only
``(producer, strength, lifecycle)`` plus value content enter — ``producer_ref`` and
``evidence_id`` are OCCURRENCE provenance. :func:`canonical_evidence_axes` embodies the
rule (sorted, deduplicated), so replaying identical evidence under a new event ID changes
no revision.

``AttributedLabelV1`` is only for controlled registry IDs; free-text catalog domain/entity
wording travels as ``AttributedTextV1`` and never becomes a facet key (0F-6). ``basis``
and ``operational_influence`` vocabularies are frozen below; ``operational_influence`` is
READ from governed state, never inferred, and is ``None`` for every Release-A discovery
value.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from featuregen.contracts.contract_versions import register_contract_version
from featuregen.overlay.evidence import (  # one owner — imported, never copied (ledger §2)
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
)

__all__ = [
    "ATTRIBUTED_BASIS_VALUES",
    "OPERATIONAL_INFLUENCE_VALUES",
    "AssertionStrength",
    "AttributedLabelV1",
    "AttributedTextV1",
    "EvidenceAuthorityV1",
    "EvidenceLifecycle",
    "EvidenceProducer",
    "SemanticValueV1",
    "attributed_label_to_json",
    "attributed_text_to_json",
    "canonical_evidence_axes",
    "evidence_to_json",
    "semantic_value_to_json",
]

_OWNER = "featuregen.contracts.evidence_axes"

#: Frozen basis vocabulary for attributed values (0F-6). The discovery-assignment basis
#: (``template_authored | human | llm_proposed``, 0F-5) is owned by the Task-1 discovery
#: registry, not here.
ATTRIBUTED_BASIS_VALUES = frozenset(
    {"template_authored", "catalog_resolved", "human", "llm_proposed"})

#: The existing operational-influence vocabulary (ledger §2: ``governed | hint``); ``None``
#: means "no operational influence" and is the only Release-A discovery value.
OPERATIONAL_INFLUENCE_VALUES = frozenset({"governed", "hint"})


def _check_operational_influence(value: str | None) -> None:
    if value is not None and value not in OPERATIONAL_INFLUENCE_VALUES:
        raise ValueError(
            f"operational_influence must be one of {sorted(OPERATIONAL_INFLUENCE_VALUES)} or "
            f"None (read from governed state, never inferred), got {value!r}")


def _check_basis(value: str) -> None:
    if value not in ATTRIBUTED_BASIS_VALUES:
        raise ValueError(
            f"basis must be one of {sorted(ATTRIBUTED_BASIS_VALUES)}, got {value!r}")


@dataclass(frozen=True, slots=True)
class EvidenceAuthorityV1:
    """One evidence occurrence on the real three-axis vocabulary (ledger §2 sketch, verbatim)."""

    producer: EvidenceProducer
    strength: AssertionStrength
    lifecycle: EvidenceLifecycle
    producer_ref: str | None
    evidence_id: str | None

    def __post_init__(self) -> None:
        # Coerce through the owner enums so a raw axis string is validated, and garbage
        # ("oracle", "verified") fails loudly instead of traveling as pseudo-evidence.
        object.__setattr__(self, "producer", EvidenceProducer(self.producer))
        object.__setattr__(self, "strength", AssertionStrength(self.strength))
        object.__setattr__(self, "lifecycle", EvidenceLifecycle(self.lifecycle))


@dataclass(frozen=True, slots=True)
class SemanticValueV1:
    """An evidence-bearing semantic value — the real axes, not a lossy authority label."""

    field_name: str
    value: object | None
    evidence: tuple[EvidenceAuthorityV1, ...]
    resolution_status: str
    operational_influence: str | None  # governed | hint | None; read, never inferred

    def __post_init__(self) -> None:
        _check_operational_influence(self.operational_influence)


@dataclass(frozen=True, slots=True)
class AttributedLabelV1:
    """A CONTROLLED registry ID with provenance. Never minted from free text (0F-6)."""

    id: str
    display_name: str
    basis: str  # template_authored | catalog_resolved | human | llm_proposed
    evidence: tuple[EvidenceAuthorityV1, ...]
    operational_influence: str | None  # governed | hint | None; read, never inferred
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _check_basis(self.basis)
        _check_operational_influence(self.operational_influence)


@dataclass(frozen=True, slots=True)
class AttributedTextV1:
    """Attributed free text: displayable and text-searchable, never a facet ID (0F-6)."""

    value: str
    basis: str
    evidence: tuple[EvidenceAuthorityV1, ...]
    operational_influence: str | None
    source_refs: tuple[str, ...]

    def __post_init__(self) -> None:
        _check_basis(self.basis)
        _check_operational_influence(self.operational_influence)


def canonical_evidence_axes(evidence: Sequence[EvidenceAuthorityV1]) -> list[dict[str, str]]:
    """The 0F-4 rule-3 hash payload for an evidence tuple: sorted, deduplicated
    ``(producer, strength, lifecycle)`` triples ONLY.

    ``producer_ref``/``evidence_id`` are occurrence provenance and are excluded, and the
    output is order-independent and replay-idempotent: appending an identical occurrence
    under a new event ID yields the identical payload, so no revision hash moves.
    """
    triples = sorted({(e.producer.value, e.strength.value, e.lifecycle.value)
                      for e in evidence})
    return [{"producer": producer, "strength": strength, "lifecycle": lifecycle}
            for producer, strength, lifecycle in triples]


def evidence_to_json(evidence: EvidenceAuthorityV1) -> dict[str, Any]:
    """The wire/provenance form — ALL five fields. Never feed this to a semantic hash;
    that is :func:`canonical_evidence_axes`'s job."""
    return {
        "producer": evidence.producer.value,
        "strength": evidence.strength.value,
        "lifecycle": evidence.lifecycle.value,
        "producer_ref": evidence.producer_ref,
        "evidence_id": evidence.evidence_id,
    }


def semantic_value_to_json(value: SemanticValueV1) -> dict[str, Any]:
    return {
        "field_name": value.field_name,
        "value": value.value,
        "evidence": [evidence_to_json(e) for e in value.evidence],
        "resolution_status": value.resolution_status,
        "operational_influence": value.operational_influence,
    }


def attributed_label_to_json(label: AttributedLabelV1) -> dict[str, Any]:
    return {
        "id": label.id,
        "display_name": label.display_name,
        "basis": label.basis,
        "evidence": [evidence_to_json(e) for e in label.evidence],
        "operational_influence": label.operational_influence,
        "source_refs": list(label.source_refs),
    }


def attributed_text_to_json(text: AttributedTextV1) -> dict[str, Any]:
    return {
        "value": text.value,
        "basis": text.basis,
        "evidence": [evidence_to_json(e) for e in text.evidence],
        "operational_influence": text.operational_influence,
        "source_refs": list(text.source_refs),
    }


# Bullet 5 (Task 0S brief): every new serialized contract version is registered with the
# real registry by its owner module at import; a competing owner fails at ITS import.
register_contract_version("evidence-authority", "1", owner=_OWNER)
register_contract_version("semantic-value", "1", owner=_OWNER)
register_contract_version("attributed-label", "1", owner=_OWNER)
register_contract_version("attributed-text", "1", owner=_OWNER)
