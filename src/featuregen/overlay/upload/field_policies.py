"""The upload field-policy registry (spec §4.2-4.4, §8): ``policy_for(field_name) -> FieldPolicy``.

This is the CONFIGURATION half of the authority kernel — one immutable :class:`FieldPolicy` per
resolvable object-field, naming HOW load-bearing the field may be (``influence_max``), the (lenient)
authority a value needs to be SHOWN (``display_rule``), and the (strict) authority the active
evidence must satisfy for a LOAD-BEARING value (``operational_rule``). The resolver
(:func:`overlay.field_authority.resolve_field_authority`) is pure and policy-driven; ALL the
field-specific judgement lives here.

The governing invariants encoded below:

* An LLM proposal is NEVER load-bearing on its own (§8). ``concept`` (and the other advisory meaning
  fields) are ``RECOMMENDATION`` — the influence ceiling alone bars a load-bearing value however
  strong the evidence, and their ``operational_rule`` additionally requires a source/human signal.
* A safety/structural field is never certified by an LLM alone (§6/§7). ``sensitivity`` and the
  behavioural fields (``additivity``/``temporal_role``) require a source/human/deterministic signal;
  a taxonomy derivation from a PROPOSED concept is ``taxonomy/proposed`` and does NOT gate (§3.2).
* Deterministic structural parsing MAY gate an OPERATIONAL-limited field (``logical_representation``/
  ``semantic_type``) — a ``parser/supported`` (or ``source/attested``) signal is load-bearing.

``policy_for`` returns ``None`` for a field with no policy (e.g. the ``sensitivity_floor`` evidence
field, which is an INPUT to the ``sensitivity`` decision, not a resolvable field of its own).
"""
from __future__ import annotations

from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
from featuregen.overlay.field_authority import (
    AnyOf,
    ConflictStrategy,
    Disqualifier,
    FieldPolicy,
    HasEvidence,
    InfluenceTier,
    ResolutionMode,
)
from featuregen.overlay.safety_floor import SENSITIVITY_ORDER

# The disqualifiers an OPERATIONAL, human-confirmable field honours: a source re-upload that changed
# the column's MATERIAL flags the field PENDING revalidation (overlay.upload.field_revalidation), and
# that flag must BLOCK the load-bearing value until a human re-confirms (spec §6.3, Task 10).
_OPERATIONAL_DISQUALIFIERS: tuple[Disqualifier, ...] = (
    Disqualifier.CONFIRMATION_PENDING_REVALIDATION,
)

# Short leaf aliases (producer, strength) for readable rules below.
_LLM_PROPOSED = HasEvidence(EvidenceProducer.LLM, AssertionStrength.PROPOSED)
_SOURCE_PROPOSED = HasEvidence(EvidenceProducer.SOURCE, AssertionStrength.PROPOSED)
_SOURCE_ATTESTED = HasEvidence(EvidenceProducer.SOURCE, AssertionStrength.ATTESTED)
_HUMAN_CONFIRMED = HasEvidence(EvidenceProducer.HUMAN, AssertionStrength.CONFIRMED)
_PARSER_SUPPORTED = HasEvidence(EvidenceProducer.PARSER, AssertionStrength.SUPPORTED)
_TAXONOMY_PROPOSED = HasEvidence(EvidenceProducer.TAXONOMY, AssertionStrength.PROPOSED)
_TAXONOMY_CONFIRMED = HasEvidence(EvidenceProducer.TAXONOMY, AssertionStrength.CONFIRMED)

# A source/human signal is what makes an advisory value load-bearing (never an LLM proposal, §8).
_SOURCE_OR_HUMAN = AnyOf((_SOURCE_ATTESTED, _HUMAN_CONFIRMED))


def _recommendation(display_rule, operational_rule) -> FieldPolicy:
    """An advisory RECOMMENDATION field: shown leniently, but the influence ceiling bars a
    load-bearing value regardless of the operational rule (belt AND braces — the ceiling is the hard
    guarantee, the operational_rule documents intent for a future promotion)."""
    return FieldPolicy(
        influence_max=InfluenceTier.RECOMMENDATION,
        display_rule=display_rule,
        operational_rule=operational_rule,
        disqualifiers=(),
        resolution_mode=ResolutionMode.GENERIC_FIELD,
        conflict_strategy=ConflictStrategy.PREFER_CONFIRMED,
    )


# concept — the classified concept. LLM-proposed is SHOWN; only a source-attested or human-confirmed
# concept is load-bearing (§8). RECOMMENDATION ceiling makes "LLM-alone is not operational" absolute.
_CONCEPT = _recommendation(
    display_rule=AnyOf((_LLM_PROPOSED, _SOURCE_PROPOSED, _SOURCE_ATTESTED, _HUMAN_CONFIRMED)),
    operational_rule=_SOURCE_OR_HUMAN,
)

# definition / domain / feature_role — advisory meaning fields; LLM or source proposed may be shown.
_MEANING = _recommendation(
    display_rule=AnyOf((_LLM_PROPOSED, _SOURCE_PROPOSED, _SOURCE_ATTESTED, _HUMAN_CONFIRMED)),
    operational_rule=_SOURCE_OR_HUMAN,
)

# logical_representation / semantic_type — OPERATIONAL-limited: a deterministic parser/supported (or
# source/attested) signal is load-bearing (structure, not opinion). NB (review #5): certifying a
# STRICTER computational_type=decimal from a proposed identifier is a finer distinction handled by a
# dedicated computational_type field — scoped out of the base logical_representation gate here.
_LOGICAL_REPRESENTATION = FieldPolicy(
    influence_max=InfluenceTier.OPERATIONAL,
    display_rule=AnyOf((_PARSER_SUPPORTED, _SOURCE_ATTESTED, _SOURCE_PROPOSED)),
    operational_rule=AnyOf((_PARSER_SUPPORTED, _SOURCE_ATTESTED)),
    disqualifiers=_OPERATIONAL_DISQUALIFIERS,
    resolution_mode=ResolutionMode.GENERIC_FIELD,
    conflict_strategy=ConflictStrategy.PREFER_CONFIRMED,
)

# sensitivity — OPERATIONAL, MOST_RESTRICTIVE over safety_floor.SENSITIVITY_ORDER. A source/human
# classification is what CERTIFIES (never an LLM alone); the taxonomy floor is fed separately through
# safety_floor.apply_sensitivity_floor by the resolver (it RESTRICTS but does not CERTIFY, §7).
_SENSITIVITY = FieldPolicy(
    influence_max=InfluenceTier.OPERATIONAL,
    display_rule=_SOURCE_OR_HUMAN,
    operational_rule=_SOURCE_OR_HUMAN,
    disqualifiers=_OPERATIONAL_DISQUALIFIERS,
    resolution_mode=ResolutionMode.GENERIC_FIELD,
    conflict_strategy=ConflictStrategy.MOST_RESTRICTIVE,
    severity_order=SENSITIVITY_ORDER,
)

# additivity / temporal_role / leakage_anchor — OPERATIONAL behavioural fields DERIVED from the
# concept. They gate only from a CONFIRMED concept's derivation (taxonomy/confirmed) or a direct
# source/human signal; a derivation from a PROPOSED concept is taxonomy/proposed and does NOT
# gate (§3.2).
_BEHAVIOURAL = FieldPolicy(
    influence_max=InfluenceTier.OPERATIONAL,
    display_rule=AnyOf((_TAXONOMY_PROPOSED, _TAXONOMY_CONFIRMED, _SOURCE_ATTESTED, _HUMAN_CONFIRMED)),
    operational_rule=AnyOf((_TAXONOMY_CONFIRMED, _SOURCE_ATTESTED, _HUMAN_CONFIRMED)),
    disqualifiers=_OPERATIONAL_DISQUALIFIERS,
    resolution_mode=ResolutionMode.GENERIC_FIELD,
    conflict_strategy=ConflictStrategy.PREFER_CONFIRMED,
)


# table_role / primary_entity / event_or_snapshot — advisory TABLE-level fields (Phase 2 Pass B).
# SHOWN on the table graph_node, NEVER load-bearing: the RECOMMENDATION ceiling structurally bars a
# load-bearing value however strong the evidence (display ≠ authority, must-prove #4/#5).
_TABLE_ADVISORY = _recommendation(
    display_rule=AnyOf((_LLM_PROPOSED, _SOURCE_PROPOSED, _SOURCE_ATTESTED, _HUMAN_CONFIRMED)),
    operational_rule=_SOURCE_OR_HUMAN,
)


# The registry: object-field name -> its policy. Keyed by the field_name written to field_evidence.
_POLICIES: dict[str, FieldPolicy] = {
    "concept": _CONCEPT,
    "definition": _MEANING,
    "domain": _MEANING,
    "feature_role": _MEANING,
    "logical_representation": _LOGICAL_REPRESENTATION,
    "semantic_type": _LOGICAL_REPRESENTATION,
    "sensitivity": _SENSITIVITY,
    "additivity": _BEHAVIOURAL,
    "temporal_role": _BEHAVIOURAL,
    # leakage_anchor is derived + lifecycle-managed exactly like temporal_role (taxonomy_evidence /
    # ingest._TAXONOMY_FIELDS); without a policy its evidence was dead — no decision, never
    # eligible. Decision-only (no graph_node display column); templates.py's registry-based
    # leakage check is unchanged (this field never weakens it).
    "leakage_anchor": _BEHAVIOURAL,
    "table_role": _TABLE_ADVISORY,
    "primary_entity": _TABLE_ADVISORY,
    "event_or_snapshot": _TABLE_ADVISORY,   # advisory: informs modelling, never load-bearing
}


def policy_for(field_name: str) -> FieldPolicy | None:
    """The :class:`FieldPolicy` for ``field_name``, or ``None`` when the field has no policy.

    ``None`` means "not a resolvable object-field" — e.g. ``sensitivity_floor`` (an INPUT to the
    ``sensitivity`` decision, fed through :func:`safety_floor.apply_sensitivity_floor`, never resolved
    as a field on its own). Callers skip a ``None`` field rather than fabricating a default policy."""
    return _POLICIES.get(field_name)
