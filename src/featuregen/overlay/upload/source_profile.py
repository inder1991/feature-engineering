"""Source capability profiles — the abstraction that unifies technical-CSV and glossary uploads.

Under the unified-ingestion model (spec §U), every file upload reduces to
``(rows, SourceCapabilityProfile)``. The profile — not the code path — is what differs between a
technical CSV and a glossary: it declares, per field, HOW strongly *this kind of source* vouches for
that field's value. A field the profile ``attests`` enters the evidence machinery at
``AssertionStrength.ATTESTED`` (fast to load-bearing); every other field enters as ``PROPOSED`` (a
candidate awaiting confirmation, never overriding a taxonomy floor or structural restriction).

- **Technical CSV** — attests structure (`type/grain/joins/cardinality`) and safety/semantic fields
  (`sensitivity/additivity/unit/currency/entity/definition`). *Structure-vouched.*
- **FTR glossary** — attests semantics (`definition/business_term/BIAN/FIBO`) but only *proposes*
  structure and `domain/sample_profile/sensitivity`. *Semantics-vouched, structure-incomplete* — a
  glossary does NOT attest a physical `type`, which is what drives profile-aware validation (Task 4).

Pure module: no DB, no I/O. Depends on ``overlay.evidence`` (the strength axis) and the reader's
``_headers._ALIASES`` (so glossary detection matches exactly what the technical reader accepts).
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.overlay.evidence import AssertionStrength
from featuregen.overlay.upload._headers import _ALIASES


@dataclass(frozen=True, slots=True)
class SourceCapabilityProfile:
    """Per-source, per-field trust: which fields this kind of source vouches for vs. merely proposes.

    ``attested_fields`` — governed/vouched: enter the evidence machinery at strength ``ATTESTED``.
    ``proposed_fields`` — declared-but-unverified: enter at strength ``PROPOSED``.
    ``structural_fields`` — attested structure a *structural* source supplies (physical type, grain,
    joins, cardinality); empty for a glossary. Structural fields are attested too — ``attests`` and
    ``strength_for`` treat them exactly like ``attested_fields``; the split only records WHY a field
    is vouched for (semantic assertion vs. real structure), which downstream policy may care about.
    """

    source_type: str
    attested_fields: frozenset[str]
    proposed_fields: frozenset[str]
    structural_fields: frozenset[str]

    def attests(self, field: str) -> bool:
        """True iff this source vouches for ``field`` — i.e. it is attested or structural (not merely
        proposed, and not unknown). This is the single test callers use to decide whether a field's
        declared value is load-bearing evidence or just a proposal."""
        return field in self.attested_fields or field in self.structural_fields


def strength_for(profile: SourceCapabilityProfile, field_name: str) -> AssertionStrength:
    """The assertion strength a value for ``field_name`` earns under ``profile``: ``ATTESTED`` when the
    profile vouches for the field (attested or structural), else ``PROPOSED``. Evidence-writing
    producers and profile-aware validation call this — never a hardcoded strength literal."""
    return AssertionStrength.ATTESTED if profile.attests(field_name) else AssertionStrength.PROPOSED


# ── The two file-ingestion profiles (spec §U.1). ────────────────────────────────────────────────
# A glossary vouches for meaning, not physical structure; the technical CSV vouches for both.

FTR_GLOSSARY_PROFILE = SourceCapabilityProfile(
    source_type="ftr_glossary",
    attested_fields=frozenset({"definition", "business_term", "bian_path", "fibo_path"}),
    proposed_fields=frozenset({"domain", "sample_profile", "sensitivity"}),
    structural_fields=frozenset(),
)

TECHNICAL_CSV_PROFILE = SourceCapabilityProfile(
    source_type="technical_csv",
    attested_fields=frozenset(
        {"definition", "sensitivity", "additivity", "unit", "currency", "entity"}),
    proposed_fields=frozenset(),
    structural_fields=frozenset({"type", "grain", "joins_to", "cardinality"}),
)


# ── Header-signature dispatch: which profile a raw upload's headers select. ──────────────────────
# NOTE (reuse): as of Task 3 there is no `glossary_reader.is_glossary_csv` to reuse, so the
# glossary-signature check lives HERE and Task 4's glossary reader must reuse `profile_for_upload`
# (or `_is_glossary_headers`) rather than re-deriving detection. Importing `_headers` here is
# cycle-safe: `canonical.py` imports this module only under TYPE_CHECKING, so the runtime chain
# `source_profile -> _headers -> canonical` never loops back.


def _norm_header(h: str) -> str:
    """Normalize a header for signature matching: drop a UTF-8 BOM (Excel prefixes the first header
    with one), lowercase, and collapse spaces/underscores — mirrors the reader's header aliasing so
    "Business Term", "business_term", and "businessterm" all match."""
    return h.lstrip("﻿").strip().lower().replace(" ", "").replace("_", "")


# A glossary is keyed on a business term and carries taxonomy paths; it never carries the physical
# column/table identity a technical extract is keyed on. Require a glossary signal AND the absence of
# the technical row-key headers so a stray column can't misclassify a technical upload as a glossary.
_GLOSSARY_HEADER_SIGNATURE = frozenset(
    _norm_header(h) for h in ("business_term", "bian_path", "fibo_path"))
# The technical row-keys are the SAME alias sets the technical reader accepts for `column` and
# `table` (M-7): a technical CSV keyed on `attribute`/`columnname`/`tablename` must never flip to
# the glossary profile just because a stray glossary-signal header rides along — the glossary
# profile has no FQN row key, so every row would quarantine.
_TECHNICAL_KEY_HEADERS = frozenset(
    _norm_header(h) for h in (_ALIASES["column"] | _ALIASES["table"]))


def _is_glossary_headers(headers: list[str]) -> bool:
    """True iff ``headers`` look glossary-shaped: at least one glossary-distinctive header present and
    no technical row-key header (any `column`/`table` alias the reader accepts) present."""
    norm = {_norm_header(h) for h in headers}
    return bool(_GLOSSARY_HEADER_SIGNATURE & norm) and not (_TECHNICAL_KEY_HEADERS & norm)


def profile_for_upload(headers: list[str]) -> SourceCapabilityProfile:
    """Pick the source profile for an upload from its header row: glossary-shaped headers select the
    FTR glossary profile; everything else is a technical CSV (the default — structure-vouched)."""
    return FTR_GLOSSARY_PROFILE if _is_glossary_headers(headers) else TECHNICAL_CSV_PROFILE
