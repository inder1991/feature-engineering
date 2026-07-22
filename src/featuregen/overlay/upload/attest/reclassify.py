"""P0 shadow-measurement harness — Task 3: the INDEPENDENT RE-CLASSIFICATION signal
(design §Components/3, docs/superpowers/specs/2026-07-22-p0-shadow-measurement-design.md).

``reclassify_concept`` is a SECOND, BLIND classification of a column into the concept vocabulary —
a decorrelating second opinion, not a yes/no over the first proposal. Design choice locked: same
model, DIFFERENT prompt (``prompt_id=overlay_concept_reclassify_v1``). ``column_ctx`` carries ONLY
the column's own name/definition/sample-values — NEVER the proposer's earlier concept — so the call
cannot anchor on (or simply echo) the first proposal; blindness is what makes the two signals usable
for decorrelation (two correlated copies of the same anchored answer prove nothing).

Reuses the SAME governed seam every enrichment call goes through
(:func:`featuregen.overlay.upload.enrich_llm.audited_enrich_call` — attached schema, egress guard,
immutable ``llm_call`` audit record) and the IDENTICAL vocabulary-acceptance gate the proposer's own
classification uses (:func:`featuregen.overlay.upload.enrich._accept_concept`: a known concept or the
literal ``'unclassified'`` is accepted; anything else -> ``None``), so an off-vocabulary response is
never coerced into a false disagreement (or agreement) either way.

Raw sample VALUES never egress verbatim (the Global Constraint every enrichment call already
honors — see ``enrich_llm``'s module docstring: "never uploader free text or data values"; the
egress backstop ``redaction.assert_llm_safe`` only catches PII-*shaped* text, not arbitrary account
numbers/codes, so a raw value must never even reach that backstop). ``_value_shape`` derives a safe,
non-identifying shape/semantic hint from them instead, reusing
:func:`featuregen.overlay.upload.sample_parser.parse_sample_profile` — the EXACT extraction the FTR
sanitizer already applies to embedded sample clauses — rather than a second, divergent heuristic.

MEASURE-ONLY: this module writes nothing to ``field_evidence`` / ``field_decision_event`` /
``graph_node`` or any authority store. The audited seam's own dispatch/``llm_call`` audit record is
the only DB write on the happy path — telemetry, not attestation, exactly like every other
enrichment call.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

from featuregen.intake.llm import LLMClient
from featuregen.overlay.upload.concepts import classification_vocabulary
from featuregen.overlay.upload.enrich import _accept_concept, bounded_definition
from featuregen.overlay.upload.enrich_llm import MAX_DEFINITION_LEN, audited_enrich_call
from featuregen.overlay.upload.sample_parser import parse_sample_profile

PROMPT_ID = "overlay_concept_reclassify_v1"
# A DISTINCT task identifier from the proposer's "overlay.enrich.concept" (enrich.py) — separate
# audit-trail/cost attribution for the reclassifier's calls, and the FakeLLM task-key script for
# this module's tests keys on this string.
_TASK = "overlay.enrich.concept_reclassify"

# B1b: the SAME controlled vocabulary the proposer classifies into — built once, mirroring enrich.py.
_CONCEPT_VOCABULARY: list[dict] = list(classification_vocabulary())

# Deliberately NOT the proposer's instruction (enrich.py's "Classify this column..."): distinct
# wording that states the blindness contract to the model itself, so the framing — not just the
# prompt_id label — is genuinely a second, independent read rather than a relabeled duplicate call.
_INSTRUCTION = (
    "You are an INDEPENDENT second classifier reviewing this database column in isolation — you "
    "have not seen, and must not assume, any prior classification of it. Using only this column's "
    "name, its business definition (if given), and any derived sample-value shape, choose the "
    "single best-fitting concept from the provided controlled vocabulary, or 'unclassified' if none "
    "fits confidently."
)


@dataclass(frozen=True, slots=True)
class ColumnContext:
    """The BLIND classification input for one column: name/definition/sample-values ONLY. Must
    NEVER carry the proposer's prior concept — see the module docstring on why blindness matters."""

    name: str
    definition: str | None = None
    sample_values: Sequence[str] = field(default_factory=tuple)


@dataclass(frozen=True, slots=True)
class ReclassifyV1:
    """The independent reclassifier's result for one column. ``value`` is ``None`` when the call
    failed/egress-blocked or the provider returned an off-vocabulary answer (``_accept_concept``'s
    reject path). Comparing this to the proposer's value for agreement is the RUNNER's job (Task 5)
    — this module stays single-purpose: produce one independent classification, nothing else."""

    value: str | None


def _value_shape(sample_values: Sequence[str]) -> dict[str, str]:
    """Derive a SAFE, non-identifying shape/semantic hint from raw sample values — NEVER the raw
    values themselves (Global Constraint: only metadata may egress). Reuses
    ``sample_parser.parse_sample_profile`` — feeding it the identical "values such as ..." clause
    shape it already parses out of FTR glossary prose — so a fixed-length all-digit run classifies
    as an identifier (never a numeric measure) exactly as it would there; returns ``{}`` when no
    values are given or none carry a recognizable shape."""
    values = [v for v in sample_values if v]
    if not values:
        return {}
    synthetic = "representative values such as " + "; ".join(values)
    profile = parse_sample_profile(synthetic)
    out: dict[str, str] = {}
    if profile.logical_representation:
        out["sample_shape"] = profile.logical_representation
    if profile.semantic_type:
        out["sample_semantic_type"] = profile.semantic_type
    return out


def reclassify_concept(conn, client: LLMClient, logical_ref: str, *, column_ctx: ColumnContext,
                       actor=None) -> ReclassifyV1:
    """Run the SECOND, BLIND concept classification for ``logical_ref``'s column (design §3).

    ``logical_ref`` identifies the column being reclassified — carried for caller-side logging and
    future dispatch-audit attribution (mirrors ``enrich.py``'s per-column subject convention); this
    MEASURE-ONLY call makes no ingestion ``dispatch_audit`` today, matching every other DIRECT
    (no ``ingestion_run_id``) enrichment call in this codebase.

    Returns the independent concept, or ``None`` on any call failure/egress-block or an
    off-vocabulary response. Writes nothing beyond the audited seam's own telemetry."""
    metadata: dict = {"column": column_ctx.name, "vocabulary": _CONCEPT_VOCABULARY}
    if column_ctx.definition:
        # `business_definition` (not `definition`) rides the SAME sanitize_definition pipeline
        # (sample-clause strip + fail-closed data-marker scan + PII redaction) every glossary
        # definition already goes through (enrich_llm._redact_free_text_meta) — bounded the same
        # way enrich.py's own concept-classification input is.
        metadata["business_definition"] = bounded_definition(column_ctx.definition, MAX_DEFINITION_LEN)
    metadata.update(_value_shape(column_ctx.sample_values))

    raw = audited_enrich_call(
        conn, client, task=_TASK, prompt_id=PROMPT_ID, schema_id="overlay_concept",
        catalog_metadata=metadata, out_key="concept", instruction=_INSTRUCTION, actor=actor,
        # vocab-caching: the vocabulary is the same large static shared prefix the proposer's call
        # marks cacheable — no reason for the reclassifier's calls to re-bill it per column.
        cacheable_metadata_keys=("vocabulary",))
    if raw is None:
        return ReclassifyV1(value=None)
    concept, _reason = _accept_concept(raw)
    return ReclassifyV1(value=concept)
