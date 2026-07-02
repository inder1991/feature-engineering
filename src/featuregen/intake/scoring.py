from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# The closed `source` vocabulary (spec §4.0). A value is safe to auto-resolve only if it came from
# one of these (the field carries a concrete reading), never from the UNKNOWN sentinel.
SAFE_SOURCES: tuple[str, ...] = ("llm", "default", "catalog")


@dataclass(frozen=True, slots=True)
class FieldScore:
    """A per-field score on the 0.0–1.0 scale (spec §6.1). `ambiguity` = how many plausible readings
    (0 = one reading, 1 = many incompatible); `confidence` = how sure of the CHOSEN reading."""

    ambiguity: float
    confidence: float
    source: str  # llm | default | catalog


def combine_scores(llm: FieldScore, catalog: FieldScore) -> FieldScore:
    """Combine the LLM self-report with the deterministic catalog-cardinality check by taking the
    MORE CAUTIOUS value on each axis (Decision 3): higher ambiguity, lower confidence. The LLM can
    never *lower* a doubt the deterministic check raised. The source that set the (winning, more
    cautious) ambiguity is recorded; a tie keeps the LLM's source."""
    ambiguity = max(llm.ambiguity, catalog.ambiguity)
    confidence = min(llm.confidence, catalog.confidence)
    source = catalog.source if catalog.ambiguity > llm.ambiguity else llm.source
    return FieldScore(ambiguity=ambiguity, confidence=confidence, source=source)


def catalog_cardinality_score(n_bindings: int) -> FieldScore:
    """Deterministic doubt from catalog cardinality: how many catalog objects / catalog-declared
    codes a concept could bind to. One binding is unambiguous; two is genuinely doubtful; three or
    more reads as high-ambiguity (several incompatible readings). This is the doubt the LLM cannot
    talk the platform out of."""
    if n_bindings <= 1:
        return FieldScore(ambiguity=0.05, confidence=0.95, source="catalog")
    if n_bindings == 2:
        return FieldScore(ambiguity=0.50, confidence=0.55, source="catalog")
    return FieldScore(ambiguity=0.85, confidence=0.35, source="catalog")


def score_fields(
    llm_scores: Mapping[str, Mapping[str, Any]],
    concept_of: Mapping[str, str | None],
    cardinality: Callable[[str], int],
) -> dict[str, dict]:
    """Produce the `field_scores` block: for every LLM-scored field, combine its self-report with the
    catalog-cardinality score of its bound concept (concept-bearing fields only). A field with no
    bound concept keeps the LLM's self-report unchanged."""
    out: dict[str, dict] = {}
    for field, raw in llm_scores.items():
        llm = FieldScore(
            float(raw["ambiguity"]), float(raw["confidence"]), str(raw.get("source", "llm"))
        )
        concept = concept_of.get(field)
        if concept:
            combined = combine_scores(llm, catalog_cardinality_score(cardinality(concept)))
        else:
            combined = llm
        out[field] = {
            "ambiguity": combined.ambiguity,
            "confidence": combined.confidence,
            "source": combined.source,
        }
    return out


@runtime_checkable
class CatalogView(Protocol):
    """The read-only SP-1 merged-view scoring seam (spec §4.4): names/types/grain + how many candidate
    bindings a concept has. NEVER profiled values / rows / samples (the no-column-values-to-LLM
    boundary, §9.4). SP-6's CandidateGenerator binds to this same seam."""

    def candidate_count(self, concept: str) -> int: ...

    def metadata(self) -> Mapping[str, Any]: ...


_CATALOG_VIEW: CatalogView | None = None


def register_catalog_view(view: CatalogView) -> None:
    """Single-source registration of the merged-view scoring adapter (mirrors SP-1's
    `register_catalog_adapter`). P9 bootstrap wires the production SP-1 adapter; tests register a stub."""
    global _CATALOG_VIEW
    _CATALOG_VIEW = view


def current_catalog_view() -> CatalogView:
    if _CATALOG_VIEW is None:
        raise RuntimeError("no CatalogView registered; call register_catalog_view() first")
    return _CATALOG_VIEW
