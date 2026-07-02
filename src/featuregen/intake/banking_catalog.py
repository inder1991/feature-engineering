from __future__ import annotations

import json
import os
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from enum import Enum
from functools import lru_cache

# Surface terms per blocked data class — the deterministic lexical expansion the intake screen
# matches against raw intent text (§5.4). `protected_attribute` is the platform-wide blocked class
# in the seed (every use-case blocks it); these are the protected characteristics it covers.
_PROTECTED_ATTRIBUTE_TERMS: tuple[str, ...] = (
    "race", "ethnicity", "ethnic", "gender", "sex", "religion", "religious", "creed",
    "age", "disability", "marital status", "national origin", "nationality",
    "sexual orientation",
)
_CLASS_SURFACE_TERMS: dict[str, tuple[str, ...]] = {
    "protected_attribute": _PROTECTED_ATTRIBUTE_TERMS,
}
# Data classes that are sensitive PROXIES (route to clarification / compliance review, NOT a block,
# §4.5, §6.2), and their raw-text surface terms.
_PROXY_TERMS_BY_CLASS: dict[str, tuple[str, ...]] = {
    "geolocation": ("zip code", "zipcode", "postal code", "postcode", "postal",
                    "neighbourhood", "neighborhood"),
    "demographics": ("age band", "age bracket", "income bracket", "income band"),
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
    def from_seed(cls, seed: Mapping) -> BankingDomainCatalog:
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


class IntakeOutcome(str, Enum):
    """The deterministic intake-classification outcomes (§4.5, §5.4). Exactly one is produced per
    intent (most-restrictive-wins). OUT_OF_SCOPE / PROHIBITED_DATA_CLASS / NEEDS_USE_CASE_ONBOARDING
    share their string values with FeatureContractStatus so the fold can map them directly."""

    OUT_OF_SCOPE = "OUT_OF_SCOPE"                             # TERMINAL reject → reject_intent / RUN_REJECTED (banking boundary)
    PROHIBITED_DATA_CLASS = "PROHIBITED_DATA_CLASS"          # TERMINAL reject → reject_intent / RUN_REJECTED (blocked class)
    SENSITIVE_PROXY_CLARIFY = "SENSITIVE_PROXY_CLARIFY"      # non-terminal → clarification / review
    AMBIGUOUS_CLARIFY = "AMBIGUOUS_CLARIFY"                  # non-terminal → clarification
    NEEDS_USE_CASE_ONBOARDING = "NEEDS_USE_CASE_ONBOARDING"  # in-scope, unknown use-case → HOLD (onboarding; the only hold, NOT a terminal reject)
    CLEAR = "CLEAR"                                          # pass


@dataclass(frozen=True)
class IntakeClassification:
    """One deterministic classification outcome + its audit/MRM provenance. `catalog_version` is
    stamped on EVERY outcome incl. CLEAR (§4.5(c)); it is None only when the catalog was unavailable
    (the fail-closed case, §4.5(b))."""

    outcome: IntakeOutcome
    catalog_version: str | None
    reason: str
    matched_class: str | None = None
    matched_use_case: str | None = None

    @property
    def is_clear(self) -> bool:
        return self.outcome is IntakeOutcome.CLEAR

    @property
    def blocks(self) -> bool:
        return self.outcome in (IntakeOutcome.OUT_OF_SCOPE, IntakeOutcome.PROHIBITED_DATA_CLASS)

    @property
    def needs_clarification(self) -> bool:
        return self.outcome in (
            IntakeOutcome.SENSITIVE_PROXY_CLARIFY,
            IntakeOutcome.AMBIGUOUS_CLARIFY,
        )

    def as_mapping(self) -> dict:
        """R9 — the compact provenance mapping submit_intent (P4) persists on INTENT_SUBMITTED (§4.5);
        MCV / not_prohibited_intent / refine read it back. Emits the outcome VALUE (not the enum),
        the stamped catalog_version, and matched_class."""
        return {
            "outcome": self.outcome.value,
            "catalog_version": self.catalog_version,
            "matched_class": self.matched_class,
        }


@lru_cache(maxsize=2048)
def _term_pattern(term: str) -> re.Pattern[str]:
    r"""A word/token-bounded, case-insensitive matcher for a single surface term (N1). The term is
    anchored on both sides by `\b`, so it matches only as a whole word — a genuine standalone
    "age"/"race"/"religion" — never as a raw substring of a larger word ("aver-age", "mort-gage",
    "t-race"). A trailing simple plural ("customer"→"customers", "balance"→"balances") is still
    allowed so scope detection is not weakened; that extension is on the right only, so it never
    re-opens the suffix false-positives (the left `\b` still fails inside "average"/"trace")."""
    return re.compile(rf"\b{re.escape(term)}(?:es|s)?\b", re.IGNORECASE)


def _term_in(text: str, term: str) -> bool:
    """True iff `term` occurs in `text` as a whole word/token (word-bounded, not a raw substring)."""
    return bool(term) and _term_pattern(term).search(text) is not None


def _first_match(text: str, terms: Iterable[str]) -> str | None:
    """First (deterministically ordered) term that occurs in the lowercased intent as a whole
    word/token, or None. Word-bounded (N1): never a raw substring match."""
    for term in sorted(terms):
        if _term_in(text, term):
            return term
    return None


def _match_use_case(text: str, catalog: BankingDomainCatalog) -> str | None:
    """The first (deterministically ordered) known use-case any of whose keyword terms occurs as a
    whole word/token (word-bounded, consistent with _first_match — N1)."""
    for use_case in sorted(catalog.use_case_terms):
        if any(_term_in(text, term) for term in catalog.use_case_terms[use_case]):
            return use_case
    return None


def classify_intent(
    intent: str,
    *,
    product: str | None = None,
    region: str | None = None,
    catalog: BankingDomainCatalog | None,
) -> IntakeClassification:
    """Deterministic intake banking-boundary classifier over the read-only BankingDomainCatalog
    (§4.5, §5.4) — NOT the LLM's call. TOTAL and fail-closed: it returns exactly one outcome for any
    input under most-restrictive-wins precedence (PROHIBITED_DATA_CLASS > OUT_OF_SCOPE >
    sensitive-proxy > ambiguous), and stamps the catalog `version` on every outcome incl. CLEAR
    (§4.5 a/c). Completeness rules: (b) an unavailable/unversioned catalog fails closed to
    AMBIGUOUS_CLARIFY (never CLEAR); (e) a scoped use-case missing product/region → AMBIGUOUS_CLARIFY."""
    # (b) fail-closed on an absent / unversioned catalog — never auto-pass.
    if catalog is None or not catalog.available:
        return IntakeClassification(
            IntakeOutcome.AMBIGUOUS_CLARIFY, None, "catalog_unavailable_fail_closed"
        )
    version = catalog.version
    text = f" {intent.lower()} "

    # 1. PROHIBITED_DATA_CLASS — most restrictive; dominates everything.
    hit = _first_match(text, catalog.blocked_terms)
    if hit is not None:
        return IntakeClassification(
            IntakeOutcome.PROHIBITED_DATA_CLASS, version,
            f"blocked data class matched: {hit}", matched_class=catalog.blocked_terms[hit],
        )

    use_case = _match_use_case(text, catalog)

    # 2. OUT_OF_SCOPE — explicit example term, an out-of-scope use-case, or no banking concept at all.
    oos_term = _first_match(text, catalog.out_of_scope_terms)
    if oos_term is not None:
        return IntakeClassification(
            IntakeOutcome.OUT_OF_SCOPE, version, f"out-of-scope example matched: {oos_term}"
        )
    if use_case is not None and use_case in catalog.out_of_scope_use_cases:
        return IntakeClassification(
            IntakeOutcome.OUT_OF_SCOPE, version, f"use-case out of scope: {use_case}",
            matched_use_case=use_case,
        )
    if _first_match(text, catalog.banking_terms) is None:
        return IntakeClassification(
            IntakeOutcome.OUT_OF_SCOPE, version, "no banking entity / data / concept"
        )

    # 3. SENSITIVE_PROXY_CLARIFY — a proxy hint is a doubt to review, never a standalone block.
    proxy = _first_match(text, catalog.sensitive_proxy_terms)
    if proxy is not None:
        return IntakeClassification(
            IntakeOutcome.SENSITIVE_PROXY_CLARIFY, version,
            f"sensitive-proxy hint matched: {proxy}",
        )

    # 4. AMBIGUOUS_CLARIFY — (e) a scoped use-case whose product/region context is missing.
    if use_case is not None and use_case in catalog.scoped_use_cases and (
        product is None or region is None
    ):
        return IntakeClassification(
            IntakeOutcome.AMBIGUOUS_CLARIFY, version,
            f"missing product/region for scoped use-case {use_case}", matched_use_case=use_case,
        )

    # 5. CLEAR (known use-case) / NEEDS_USE_CASE_ONBOARDING (in-scope banking, unknown use-case).
    if use_case is not None:
        return IntakeClassification(
            IntakeOutcome.CLEAR, version, f"in banking scope: {use_case}", matched_use_case=use_case
        )
    if _first_match(text, catalog.predictive_markers) is not None:
        return IntakeClassification(
            IntakeOutcome.NEEDS_USE_CASE_ONBOARDING, version, "in-scope banking, unknown use-case"
        )
    return IntakeClassification(IntakeOutcome.CLEAR, version, "in banking scope: feature definition")
