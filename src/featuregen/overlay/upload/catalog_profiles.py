"""Catalog narrative contracts + validation (profile plan §6.2, interface doc D1/D2/D12.10).

A catalog's OWN prose (display name / description / business context / business domains) is
genuinely new state — no object-level evidence key or current-pointer source exists for it
(correction 2) — persisted by :mod:`profile_store` as immutable revisions + a CAS current pointer.

**Identity (D1/D12.10):** ``content_hash`` = ``materialize_hash`` (RFC 8785 JCS) over the VALUES
plus the real D2 producer/strength/lifecycle axes. ``revision_id = "cpr_" + content_hash`` — a
DETERMINISTIC, CHECK-pinned id (the ``pbr_`` binding-revision precedent), so a byte-identical
re-authoring resolves to the SAME revision id: the current narrative revision id may therefore sit
inside every ``dataset_profile_hash`` (D12.10 — narrative EDITS re-key dataset profiles) without
identical re-authoring ever churning it. ``producer_ref`` / ``ingestion_run_id`` / ``created_at``
are persistence PROVENANCE, deliberately OUTSIDE identity (§6.2 — an upload run is provenance, not
an invented evidence ref).

**Bounds (field_correction ``_MAX_LEN`` precedent):** every text/list field is validated HERE,
before any persistence; the migration-1047 CHECKs are only the backstop. Catalog profile values
are descriptive — they never default a dataset's role, authority or temporal model.
"""
from __future__ import annotations

from dataclasses import dataclass

from featuregen.materialize.canonical import materialize_hash
from featuregen.overlay.evidence import (
    AssertionStrength,
    EvidenceLifecycle,
    EvidenceProducer,
)
from featuregen.overlay.upload.object_ref import normalize_source_name

#: Canonical-content contract token — bump only on a meaning-bearing schema change.
_NARRATIVE_CONTRACT = "catalog_profile_revision_v1"

# Bounds (code-enforced BEFORE persistence; the migration CHECKs mirror them).
MAX_DISPLAY_NAME = 200
MAX_DESCRIPTION = 4000
MAX_BUSINESS_CONTEXT = 4000
MAX_DOMAINS = 32
MAX_DOMAIN_LEN = 200

#: The closed key set of an authored narrative payload (upload part / PUT body).
NARRATIVE_KEYS = frozenset({"display_name", "description", "business_context",
                            "business_domains"})


class CatalogProfileError(ValueError):
    """A benign, PRE-WRITE validation refusal (unknown key / type / bound). The routes map it to
    a 400; nothing was written."""


@dataclass(frozen=True, slots=True)
class CatalogProfileRevisionV1:
    """One immutable catalog-narrative revision (§6.2). ``producer``/``strength``/``lifecycle``
    are the D2 axes VERBATIM (string values of the overlay enums)."""

    catalog_source: str
    display_name: str | None
    description: str | None
    business_context: str | None
    business_domains: tuple[str, ...]
    producer: str
    strength: str
    lifecycle: str
    producer_ref: str
    ingestion_run_id: str | None
    content_hash: str
    revision_id: str


@dataclass(frozen=True, slots=True)
class CatalogProfileCurrentV1:
    """The CAS current pointer for one catalog's narrative (§6.2)."""

    catalog_source: str
    revision_id: str
    pointer_version: int


def _bounded_text(value: object, *, key: str, max_len: int) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise CatalogProfileError(f"{key} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > max_len:
        raise CatalogProfileError(f"{key} exceeds the {max_len}-character bound")
    return text


def parse_narrative_payload(payload: object) -> dict:
    """Validate an authored narrative payload (upload ``catalog_profile_json`` part / PUT body)
    into normalized kwargs — the FULL validation that runs before any write. Closed key set,
    typed values, every bound enforced; anything else raises :class:`CatalogProfileError`."""
    if not isinstance(payload, dict):
        raise CatalogProfileError("catalog profile must be a JSON object")
    unknown = set(payload) - NARRATIVE_KEYS
    if unknown:
        raise CatalogProfileError(
            f"unknown catalog profile keys: {', '.join(sorted(unknown))} "
            f"(allowed: {', '.join(sorted(NARRATIVE_KEYS))})")
    domains_raw = payload.get("business_domains", [])
    if not isinstance(domains_raw, list):
        raise CatalogProfileError("business_domains must be a list of strings")
    if len(domains_raw) > MAX_DOMAINS:
        raise CatalogProfileError(f"business_domains exceeds the {MAX_DOMAINS}-item bound")
    domains: list[str] = []
    for item in domains_raw:
        text = _bounded_text(item, key="business_domains item", max_len=MAX_DOMAIN_LEN)
        if text is None:
            raise CatalogProfileError("business_domains items must be non-empty strings")
        domains.append(text)
    return {
        "display_name": _bounded_text(payload.get("display_name"), key="display_name",
                                      max_len=MAX_DISPLAY_NAME),
        "description": _bounded_text(payload.get("description"), key="description",
                                     max_len=MAX_DESCRIPTION),
        "business_context": _bounded_text(payload.get("business_context"),
                                          key="business_context", max_len=MAX_BUSINESS_CONTEXT),
        "business_domains": tuple(domains),
    }


def narrative_content_payload(
    *, catalog_source: str, display_name: str | None, description: str | None,
    business_context: str | None, business_domains: tuple[str, ...],
    producer: str, strength: str, lifecycle: str,
) -> dict:
    """The canonical JCS content (§6.2): values + the D2 axes. NO provenance, NO timestamps."""
    return {
        "contract": _NARRATIVE_CONTRACT,
        "catalog_source": catalog_source,
        "display_name": display_name,
        "description": description,
        "business_context": business_context,
        "business_domains": list(business_domains),
        "producer": producer,
        "strength": strength,
        "lifecycle": lifecycle,
    }


def build_catalog_profile_revision(
    *, catalog_source: str, display_name: str | None = None, description: str | None = None,
    business_context: str | None = None, business_domains: tuple[str, ...] = (),
    producer: EvidenceProducer | str = EvidenceProducer.HUMAN,
    strength: AssertionStrength | str = AssertionStrength.PROPOSED,
    lifecycle: EvidenceLifecycle | str = EvidenceLifecycle.ACTIVE,
    producer_ref: str, ingestion_run_id: str | None = None,
) -> CatalogProfileRevisionV1:
    """Build one fully-validated revision with its deterministic identity. Bounds re-run here so
    NO construction path (route, upload, test) can bypass them; the enum axes are normalized to
    their string values."""
    source = normalize_source_name(catalog_source)
    fields = parse_narrative_payload({
        "display_name": display_name, "description": description,
        "business_context": business_context, "business_domains": list(business_domains)})
    producer_v = EvidenceProducer(producer).value
    strength_v = AssertionStrength(strength).value
    lifecycle_v = EvidenceLifecycle(lifecycle).value
    if not producer_ref or not producer_ref.strip():
        raise CatalogProfileError("producer_ref is required")
    content_hash = materialize_hash(narrative_content_payload(
        catalog_source=source, producer=producer_v, strength=strength_v, lifecycle=lifecycle_v,
        **fields))
    return CatalogProfileRevisionV1(
        catalog_source=source,
        display_name=fields["display_name"],
        description=fields["description"],
        business_context=fields["business_context"],
        business_domains=fields["business_domains"],
        producer=producer_v,
        strength=strength_v,
        lifecycle=lifecycle_v,
        producer_ref=producer_ref.strip(),
        ingestion_run_id=ingestion_run_id,
        content_hash=content_hash,
        revision_id=f"cpr_{content_hash}",
    )


# ── the narrative as MODEL CONTEXT (Task 7b) ────────────────────────────────────────────────────
#
# The upload form promises this prose is "used by search and by the AI when it interprets tables".
# It was used by neither model-facing seam: the bundle carried `catalog_profile_revision_id` (the
# id, not the prose) and the ingest path read that id only as a BOOLEAN, to decide whether the
# `authority_claim_without_source_context` contradiction could fire at all.
#
# ONE builder, because two renderings of the same narrative would drift. Two seams consume it, both
# at TABLE grain — never per column, where a 4000-character description multiplied by a 144-column
# table would dominate the payload:
#
#   * `table_synth` — the per-table Pass-B synthesis item. Highest leverage: Pass B decides grain,
#     `table_role`, `primary_entity` and `event_or_snapshot` from column names and profiles alone,
#     and a sentence like "all outbound SWIFT/RTGS payments; Compliance-owned" answers three of the
#     four outright.
#   * `feature_assist._table_context` — the feature-generation table block.
#
# KEY NAMES ARE PREFIXED `catalog_` on purpose. The table block ALREADY carries a `business_context`
# (migration 1052 — the per-table narrative Pass B writes) and a `table_definition`. An unprefixed
# `business_context` here would collide with one and be read as the other; the prefix also tells the
# model which GRAIN it is reading about, which is the whole reason the block is worth sending.
CATALOG_NARRATIVE_AUTHORITY_KEY = "catalog_narrative_authority"

#: Every key the block can emit, in emission order. Frozen as data so the two seams' egress
#: allowlists can be checked against it by test rather than by eye.
CATALOG_NARRATIVE_KEYS: tuple[str, ...] = (
    "catalog_display_name", "catalog_description", "catalog_business_context",
    "catalog_business_domains", CATALOG_NARRATIVE_AUTHORITY_KEY,
)


def catalog_narrative_block(revision: CatalogProfileRevisionV1) -> dict:
    """One catalog's authored narrative, as bounded model context with its authority LABELED.

    AUTHORITY. The block publishes the revision's OWN stored D2 axes as ``producer/strength`` — the
    same wire form `semantic_context`'s `semantic_authority` uses (D10: the wire carries the pair,
    never a derived display label). For an authored narrative that is ``human/proposed``: prose a
    person typed, which INFORMS the model and overrides nothing. It must not default any dataset's
    role, authority or temporal model — :func:`put_catalog_profile` states that constraint for the
    write side, and reading the stored axes rather than asserting a stronger one preserves it here.
    Nothing downstream branches on this value; it is context, exactly like the prose beside it.

    An unauthored field is OMITTED, never sent blank: a fabricated empty string is a value the model
    can reason about, and "nobody wrote one" is honestly the absence of the key.
    """
    block: dict = {}
    if revision.display_name:
        block["catalog_display_name"] = revision.display_name
    if revision.description:
        block["catalog_description"] = revision.description
    if revision.business_context:
        block["catalog_business_context"] = revision.business_context
    if revision.business_domains:
        block["catalog_business_domains"] = list(revision.business_domains)
    if block:
        # Only where there is prose to attribute. A block that is nothing but an authority label
        # would announce a narrative that does not exist.
        block[CATALOG_NARRATIVE_AUTHORITY_KEY] = f"{revision.producer}/{revision.strength}"
    return block
