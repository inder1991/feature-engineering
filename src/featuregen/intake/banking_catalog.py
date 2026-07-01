from __future__ import annotations

import json
import os
from collections.abc import Mapping
from dataclasses import dataclass, field

# Surface terms per blocked data class — the deterministic lexical expansion the intake screen
# matches against raw intent text (§5.4). `protected_attribute` is the platform-wide blocked class
# in the seed (every use-case blocks it); these are the protected characteristics it covers.
_PROTECTED_ATTRIBUTE_TERMS: tuple[str, ...] = (
    "race", "ethnicity", "gender", "sex", "religion", "age", "disability",
    "marital status", "national origin", "sexual orientation",
)
_CLASS_SURFACE_TERMS: dict[str, tuple[str, ...]] = {
    "protected_attribute": _PROTECTED_ATTRIBUTE_TERMS,
}
# Data classes that are sensitive PROXIES (route to clarification / compliance review, NOT a block,
# §4.5, §6.2), and their raw-text surface terms.
_PROXY_TERMS_BY_CLASS: dict[str, tuple[str, ...]] = {
    "geolocation": ("zip code", "postal code", "neighbourhood", "neighborhood"),
    "demographics": ("age band", "income bracket"),
    "device": (),
}
_DEFAULT_PREDICTIVE_MARKERS: tuple[str, ...] = (
    "predict", "prediction", "propensity", "likelihood", "more likely",
    "higher risk", "score for", "who will", "which customers",
)
_DEFAULT_OUT_OF_SCOPE_TERMS: tuple[str, ...] = (
    "netflix", "e-commerce", "cart abandonment", "streaming", "movie",
)


@dataclass(frozen=True)
class BankingDomainCatalog:
    """Read-only, SP-0-governed banking-boundary / blocked-class reference data (§4.5). SP-2 READS
    only — never writes, never grounding (Decision D8). Term-sets are the deterministic lexical
    surfaces classify_intent matches raw intent text against (§5.4)."""

    version: str | None
    banking_entities: frozenset[str] = frozenset()
    banking_terms: frozenset[str] = frozenset()
    allowed_use_cases: frozenset[str] = frozenset()
    out_of_scope_use_cases: frozenset[str] = frozenset()
    out_of_scope_terms: frozenset[str] = frozenset()
    blocked_data_classes: frozenset[str] = frozenset()
    blocked_terms: Mapping[str, str] = field(default_factory=dict)
    sensitive_proxy_terms: frozenset[str] = frozenset()
    use_case_terms: Mapping[str, tuple[str, ...]] = field(default_factory=dict)
    predictive_markers: frozenset[str] = frozenset()
    scoped_use_cases: frozenset[str] = frozenset()
    owner: str | None = None
    effective_date: str | None = None
    provenance: str | None = None

    @property
    def available(self) -> bool:
        """The fail-closed gate (§4.5(b)): an absent/unversioned catalog is UNAVAILABLE — the intake
        screen must never auto-pass against it."""
        return bool(self.version)

    @classmethod
    def from_seed(cls, seed: Mapping) -> "BankingDomainCatalog":
        """Map the SP-0-governed banking-domain-catalog seed shape → the reader dataclass. Optional
        seed keys override the built-in surface defaults: `sensitive_proxy_hints`,
        `out_of_scope_examples`, `predictive_markers`, `scoped_use_cases`."""
        use_cases = list(seed.get("use_cases") or ())
        allowed = frozenset(
            u["use_case"] for u in use_cases if u.get("status", "active") == "active"
        )
        oos_uc = frozenset(
            u["use_case"] for u in use_cases if u.get("status") == "out_of_scope"
        )
        blocked_classes = frozenset(
            c for u in use_cases for c in (u.get("blocked_data_classes") or ())
        )
        blocked_terms: dict[str, str] = {}
        for cls_name in blocked_classes:
            for term in _CLASS_SURFACE_TERMS.get(cls_name, (cls_name,)):
                blocked_terms[term] = cls_name

        entities = frozenset(seed.get("entities") or ())
        data_classes = frozenset(seed.get("data_classes") or ())
        use_case_terms: dict[str, tuple[str, ...]] = {}
        for u in use_cases:
            name = u["use_case"]
            terms = [name.replace("_", " ")]
            target_name = (u.get("target") or {}).get("name")
            if target_name:
                terms.append(target_name)
            use_case_terms[name] = tuple(dict.fromkeys(terms))

        banking_terms = frozenset(
            set(entities)
            | set(data_classes)
            | {t for terms in use_case_terms.values() for t in terms}
        )
        proxy_terms: set[str] = set(seed.get("sensitive_proxy_hints") or ())
        for c in data_classes:
            proxy_terms.update(_PROXY_TERMS_BY_CLASS.get(c, ()))

        return cls(
            version=seed.get("catalog_version") or seed.get("version"),
            banking_entities=entities,
            banking_terms=banking_terms,
            allowed_use_cases=allowed,
            out_of_scope_use_cases=oos_uc,
            out_of_scope_terms=frozenset(
                seed.get("out_of_scope_examples") or _DEFAULT_OUT_OF_SCOPE_TERMS
            ),
            blocked_data_classes=blocked_classes,
            blocked_terms=blocked_terms,
            sensitive_proxy_terms=frozenset(proxy_terms),
            use_case_terms=use_case_terms,
            predictive_markers=frozenset(
                seed.get("predictive_markers") or _DEFAULT_PREDICTIVE_MARKERS
            ),
            scoped_use_cases=frozenset(seed.get("scoped_use_cases") or ()),
            owner=seed.get("owner"),
            effective_date=seed.get("effective_date"),
            provenance=seed.get("source") or seed.get("provenance"),
        )


def load_banking_catalog(path: str | os.PathLike) -> BankingDomainCatalog:
    """Load the read-only banking-domain-catalog seed JSON at `path` into a BankingDomainCatalog.
    A thin, side-effect-free reader (Decision D8): open → json.load → from_seed."""
    with open(path, encoding="utf-8") as fh:
        seed = json.load(fh)
    return BankingDomainCatalog.from_seed(seed)
