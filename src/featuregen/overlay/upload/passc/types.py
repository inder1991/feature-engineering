from __future__ import annotations
from dataclasses import dataclass
from enum import StrEnum
CONFIG_VERSION = "passc-config-v1"
ALGORITHM_VERSION = "passc-algo-v1"
# Keep in sync with ftr_adapter.KNOWN_TERM_TYPES; drives conservative Pass C eligibility — an
# UNRECOGNIZED non-blank glossary term_type is ineligible (could be a mistyped measure), while a
# BLANK term_type (technical CSV / non-glossary) stays eligible.
KNOWN_TERM_TYPES = frozenset({"measure", "dimension", "code_value", "reference_data",
                              "business_term", "regulatory_term"})
class NamespaceCompatibility(StrEnum):
    COMPATIBLE = "compatible"; POSSIBLE = "possible"; AMBIGUOUS = "ambiguous"; INCOMPATIBLE = "incompatible"
class CardinalityInferenceStatus(StrEnum):
    INFERRED_FROM_CONFIRMED_GRAIN = "inferred_from_confirmed_grain"; MISSING_GRAIN = "missing_grain"
    AMBIGUOUS_BOTH_GRAINS = "ambiguous_both_grains"; MANY_TO_MANY_RISK = "many_to_many_risk"
@dataclass(frozen=True, slots=True)
class SignalEvidence:
    signal_name: str; score_delta: int; evidence_refs: tuple[str, ...]; explanation: str
@dataclass(frozen=True, slots=True)
class JoinCandidateEvidenceV1:
    candidate_id: str; from_ref: str; to_ref: str; column_pairs: tuple[tuple[str, str], ...]
    proposed_direction: str | None; proposed_cardinality: str | None
    cardinality_status: CardinalityInferenceStatus; bucket: str; score: int
    positive_signals: tuple[SignalEvidence, ...]; negative_signals: tuple[SignalEvidence, ...]
    namespace_compatibility: NamespaceCompatibility; namespace_reason_codes: tuple[str, ...]
    grain_evidence: tuple[str, ...]; missing_requirements: tuple[str, ...]; llm_annotations: tuple[str, ...]
    explanation: str; producer: str; config_version: str; candidate_algorithm_version: str; source_snapshot_id: str
@dataclass(frozen=True, slots=True)
class PassCConfig:
    weights: dict[str, int]; negative_concepts: frozenset[str]
    strong_threshold: int = 80; weak_threshold: int = 50
    mixed_bian_leaves: frozenset[str] = frozenset({"customer and counterparty identification"})
DEFAULT_CONFIG = PassCConfig(
    weights={"same_identifier_concept": 40, "related_terms_key_link": 50, "same_column_name": 30,
             "same_term_name": 25, "same_column_entity": 25, "same_bian_leaf": 10, "same_fibo_leaf": 10,
             "compatible_phase2_entity": 15, "one_side_confirmed_grain": 10, "compatible_domain": 10},
    negative_concepts=frozenset({"amount", "balance", "rate", "date", "timestamp", "description", "name",
        "status", "free_text", "address", "phone", "email", "currency", "flag", "score"}))
