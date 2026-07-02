from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# Structural-only metadata views (names/types/grain + catalog-DECLARED enum/code metadata ONLY —
# never profiled column values, rows, samples, or overlay metrics; §9.4 no-data-to-LLM boundary).
DraftContract = Mapping[str, Any]
CatalogView = Mapping[str, Any]
DomainCatalogEntry = Mapping[str, Any]

# Closed calculation-method-variant vocabulary — mirrors §4.0 / P2's CONFIRMED_CONTRACT
# `$defs.method_variant` `kind` enum (SP-3 switches on `chosen.kind` deterministically).
_METHOD_KINDS: tuple[str, ...] = (
    "rolling_aggregate",
    "point_snapshot",
    "ratio",
    "distribution_divergence",
)
_MAX_WINDOW_DAYS = 3 * 365  # a "sane" analytic window ceiling (3 years) for the cheap plausibility check


@dataclass(frozen=True)
class Candidate:
    """One hypothesis-mode candidate feature (§7.1). `calculation_method` is the versioned, tagged
    structure of §4.2 (`{method_version, chosen, considered}`, discriminated on `chosen.kind`) that
    SP-3 consumes deterministically. `signals` carries ONLY cheap, model-free plausibility hints
    (§7.3) — never measured predictive power. Frozen: a candidate document is write-once."""

    candidate_id: str
    definition_text: str
    rationale: str
    calculation_method: dict
    signals: dict
    provenance: dict


@runtime_checkable
class CandidateGenerator(Protocol):
    """The stable hypothesis-generation seam (§7.1). SP-2 ships `StubCandidateGenerator`; SP-12 binds
    its real engine to THIS SAME signature without touching Layer 1/2, the candidate schema, or the
    Gate #1 selection machinery. Only the `generate` body changes across SP-2 → SP-12."""

    def generate(
        self,
        draft: DraftContract,
        catalog_metadata: CatalogView,
        domain_context: DomainCatalogEntry | None,
    ) -> list[Candidate]: ...


def _window_days(window: object) -> int | None:
    """Parse a compact window label (`"90d"`/`"6m"`/`"1y"`/`"4w"`) to a day count, or None if
    unparseable. Deterministic, model-free — a cheap sanity check only."""
    if not isinstance(window, str):
        return None
    w = window.strip().lower()
    if len(w) < 2 or not w[:-1].isdigit():
        return None
    n = int(w[:-1])
    mult = {"d": 1, "w": 7, "m": 30, "y": 365}.get(w[-1])
    return n * mult if mult is not None else None


def _window_is_sane(variant: Mapping[str, Any]) -> bool:
    """A variant's window(s) are sane iff each present window parses to a positive count within the
    ceiling. A variant that legitimately carries NO window (e.g. a point_snapshot) is sane."""
    present = [variant.get("window"), variant.get("baseline_window")]
    days = [_window_days(w) for w in present if w is not None]
    if not days:
        return "window" not in variant and "baseline_window" not in variant
    return all(d is not None and 0 < d <= _MAX_WINDOW_DAYS for d in days)


def _variant_concept(variant: Mapping[str, Any]) -> str | None:
    """The primary catalog concept a variant references (best-effort, structural)."""
    kind = variant.get("kind")
    if kind == "rolling_aggregate":
        return (variant.get("filter") or {}).get("concept")
    if kind == "point_snapshot":
        return variant.get("field")
    if kind == "ratio":
        num = variant.get("numerator")
        return num if isinstance(num, str) else None
    if kind == "distribution_divergence":
        return variant.get("measure")
    return None


def _same_variant(a: Mapping[str, Any], b: Mapping[str, Any]) -> bool:
    """Cheap structural equality for duplicate-detection among sibling candidates on one run."""
    return (
        a.get("kind") == b.get("kind")
        and a.get("window") == b.get("window")
        and a.get("aggregation") == b.get("aggregation")
        and a.get("measure") == b.get("measure")
        and _variant_concept(a) == _variant_concept(b)
    )


def candidate_signals(
    calculation_method: dict,
    definition_text: str,
    *,
    known_concepts: set[str],
    sibling_methods: list[dict],
) -> dict:
    """Cheap, MODEL-FREE plausibility/quality signals ONLY (§7.3): does the candidate reference a
    known catalog concept? is its window sane? is it a duplicate of a sibling on this run? plus a
    heuristic rank in [0,1]. This is DELIBERATELY not measured predictive power — NO IV/WoE/AUC/
    overfitting-guard result (those need a point-in-time labelled sample and live in SP-5/SP-7)."""
    chosen = (calculation_method or {}).get("chosen", {}) or {}
    concept = _variant_concept(chosen)
    references_known_concept = bool(concept) and concept in known_concepts
    window_sane = _window_is_sane(chosen)
    duplicate_of_sibling = any(
        _same_variant(chosen, (m or {}).get("chosen", {}) or {}) for m in sibling_methods
    )
    has_definition = bool(definition_text and definition_text.strip())
    # Weighted heuristic — a transparent, cheap ranking hint, NOT a predictive score.
    rank = (
        (0.4 if references_known_concept else 0.0)
        + (0.3 if window_sane else 0.0)
        + (0.2 if has_definition else 0.0)
        + (0.1 if not duplicate_of_sibling else 0.0)
    )
    return {
        "references_known_concept": references_known_concept,
        "window_sane": window_sane,
        "duplicate_of_sibling": duplicate_of_sibling,
        "heuristic_rank": round(rank, 3),
        "scored_by": "cheap_model_free_heuristic",  # honestly NOT measured predictive power (§7.3)
    }


# --- R10 collaborator DI seam (module-global; mirrors overlay/catalog.py's
# register_catalog_adapter/current_catalog_adapter) -----------------------------------------
# The process-wide CandidateGenerator SP-2's hypothesis flow resolves. This is the ONLY holder:
# submit_intent (P4) calls current_candidate_generator(); the P1 conftest `candidate_generator`
# fixture and P9's register_sp2 register the concrete generator via register_candidate_generator(...).
_CANDIDATE_GENERATOR: CandidateGenerator | None = None


def register_candidate_generator(generator: CandidateGenerator) -> None:
    """Register the process-wide `CandidateGenerator` (last writer wins)."""
    global _CANDIDATE_GENERATOR
    _CANDIDATE_GENERATOR = generator


def current_candidate_generator() -> CandidateGenerator:
    """Return the registered `CandidateGenerator`. Fails closed: raises `RuntimeError` if none has
    been registered, so SP-2 never silently generates zero candidates on an unwired seam."""
    if _CANDIDATE_GENERATOR is None:
        raise RuntimeError(
            "no CandidateGenerator registered; call register_candidate_generator(...) "
            "(register_sp2() does this in production)"
        )
    return _CANDIDATE_GENERATOR
