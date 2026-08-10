"""Overlay-owned audited LLM call for catalog enrichment.

The direct `client.call()` path works only against FakeLLM: a real adapter (ClaudeLLM) fails closed
without an attached output-schema, and going around `call_llm` skips the egress guard + audit record.
But `call_llm` itself is coupled to the SP-2 feature-contract aggregate (it emits LLM_CALL_RECORDED on
a feature_contract). Catalog enrichment is not a feature contract, so we COMPOSE the same governance
from the decoupled building blocks — registered output-schema, reserved input keys, `assert_llm_safe`,
`drive_structured_call`, `record_llm_call` — under our own run bucket.

Enrichment inputs carry schema METADATA (names/types) plus — for a GLOSSARY column — its curated
business-semantic sidecar; the "intent" is a fixed instruction, never uploader free text or data
values. The structural names/types are classified `clean`; the sidecar's FREE-TEXT values
(definitions/synonyms/taxonomy paths) are uploader-authored and therefore NEVER presumed clean —
each rides through `redact_free_text` (finding #19), so the classification on the wire is what the
scan actually established ('clean' only post-scan; 'contains_pii' + scrubbed spans otherwise).
"""
from __future__ import annotations

import logging
import os
from collections.abc import Callable
from contextvars import ContextVar
from dataclasses import dataclass

import psycopg

from featuregen.config import get_settings
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.contracts.identity import identity_to_jsonb
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.idgen import mint_id
from featuregen.intake.llm import (
    STATUS_FAILED,
    LLMClient,
    LLMRequest,
    compute_input_hash,
    drive_structured_call,
    record_llm_call,
)
from featuregen.intake.llm_claude import ClaudeConfig
from featuregen.intake.redaction import (
    EgressViolation,
    RedactionResult,
    assert_llm_safe,
    build_llm_inputs,
    redact_free_text,
)
from featuregen.overlay.upload.catalog_profiles import (
    CATALOG_NARRATIVE_KEYS,
)
from featuregen.overlay.upload.catalog_profiles import (
    MAX_BUSINESS_CONTEXT as MAX_CATALOG_BUSINESS_CONTEXT,
)
from featuregen.overlay.upload.catalog_profiles import (
    MAX_DESCRIPTION as MAX_CATALOG_DESCRIPTION,
)
from featuregen.overlay.upload.dispatch_audit import (
    AuditingClient,
    DispatchAuditContext,
    link_llm_call,
)
from featuregen.overlay.upload.enrich_batch import (
    EGRESS,
    BatchCallResult,
    BatchItem,
    BatchItemOutcome,
    validate_batch_results,
)
from featuregen.overlay.upload.sanitize import sanitize_definition
from featuregen.security.audit import record_security_event

logger = logging.getLogger(__name__)

_OWNER = "featuregen-overlay"
# The audit run bucket for catalog enrichment llm_call records. Exposed as ENRICHMENT_RUN_ID so the
# evidence layer can use it as the `producer_ref` on Pass A field_evidence — tying each proposal back
# to the immutable llm_call records recorded under this same run bucket.
ENRICHMENT_RUN_ID = "overlay-enrichment"
_RUN = ENRICHMENT_RUN_ID
_REDACTION_VERSION = "metadata-only"  # structural names/types only — nothing to redact. When the
# metadata carries glossary FREE-TEXT, _redact_free_text_meta supplies the ACTUAL redactor version
# instead (finding #19) — this constant is never stamped on a call whose free-text was scanned.


# Finding #19: glossary sidecar values (business definitions, synonyms, data domains, BIAN/FIBO
# taxonomy paths, the term name) are uploader-authored FREE TEXT — not structural names/types — so
# they are never presumable-clean. Phase-2 Slice 1 makes the boundary FIELD-AWARE: the curated
# NARRATIVE fields (`_DEFINITION_META_KEYS` — column/table definitions, the Pass-B table narrative,
# and the projected semantic terms) can EMBED raw sample values in prose, so they route through
# `sanitize.sanitize_definition` (sample-clause strip + fail-closed data-marker scan + PII
# redaction); every other free-text field is PII-only via `redaction.redact_free_text`. The deterministic scan (email/SSN/PAN/IBAN/phone/account/DOB/
# address) classifies + scrubs, and a REGISTERED IntentRedactor (`register_intent_redactor` — the
# NER seam redaction.py documents as the DEFERRED personal-NAMES closer) supersedes the default
# when present (sanitize_definition's PII step rides the same seam). A value that fails closed
# (redactor failure, or a definition the sanitizer blanks) blocks the ITEM (batch: excluded +
# audited; single: no dispatch).
# Definition-KIND is a statement about what a field CAN CARRY, not about which name it goes by:
# curated narrative prose, authored against real data, which can therefore embed real sample values
# in a sentence. `business_definition` (column) and `table_definition` (table) were the original
# two; the joint/profile work added three more of exactly that class and they now share the grade:
#
# * `table_description` / `business_context` — the Pass-B table narrative. Classifying them as
#   plain prose gave them the PII backstop ONLY: a probe of "ACCT-8891, 1234.56, Jane Roe" in a
#   canonical sample clause survived intact, while the SAME sentence under `table_definition` was
#   stripped. Same class of field, same pipeline.
# * `semantic_terms` — the platform's term expansion, built from the sidecar's uploader-authored
#   terms. It was ALREADY definition-kind in the feature-menu adapter
#   (`_FEATURE_COLUMN_DEFINITION_KEYS`) and prose here: one field, two grades, depending only on
#   which seam it rode. The two seams now agree.
#
# Definition grade = `sanitize_definition` (sample-clause strip to a fixed point + FAIL-CLOSED
# data-marker scan + PII redaction) plus a per-field `sample_strip` audit; prose grade is PII only,
# with no gate for a data marker at all. The strictly-stronger direction, so nothing that used to
# egress cleanly is newly leaked; a value carrying an unconsumable marker now BLOCKS its item
# (audited) instead of egressing.
# * `catalog_description` / `catalog_business_context` (Task 7b) — the CATALOG narrative a human
#   typed in the upload form (migration 1047). Same class again: uploader-authored narrative prose,
#   up to 4000 characters, written while looking at the real dataset — so it can embed a sample
#   clause in a sentence exactly as a table description can. Prose grade would give it the PII
#   backstop only; the definition grade adds the sample-clause strip and the fail-closed
#   data-marker scan, which is the strictly-stronger direction.
_DEFINITION_META_KEYS = frozenset({"business_definition", "table_definition",
                                   "table_description", "business_context", "semantic_terms",
                                   "catalog_description", "catalog_business_context"})
# [F6] `synonyms` is prose emitted as list[str] (enrich.py) — a LIST of prose values, each
# PII-scanned per item and audited at an indexed path (`synonyms[0]`).
# `source_attributes` joins `synonyms` here deliberately: these are uploader-authored values, so
# they are never presumable-clean and each entry is PII-scanned at an indexed path.
# `related_terms` (richness Task 3 Step 6c) is the sidecar's uploader-authored term list — same
# never-presumable-clean rule, per-item PII scan at an indexed path.
# `catalog_business_domains` (Task 7b) is the uploader's own list of business domains for the whole
# catalog — free text a person typed into a form, bounded at 32 items of 200 chars. It is here and
# NOT with the structural lists for the same reason `source_attributes` is: a list shape is not a
# safety property, and grading uploader text as structural gives it no redaction at all.
_LIST_PROSE_META_KEYS = frozenset({"synonyms", "source_attributes", "related_terms",
                                   "catalog_business_domains"})
# `term_type`/`process_path` are sidecar (uploader-authored) scalars; `ai_synonyms` is the AI's
# comma-joined synonym draft riding the tail summary payload — all PII-scanned as prose. The
# remaining Step-6c summary keys (`concept`, `party_role`, `grain_role`, `table_role`) are
# PLATFORM-derived closed-vocabulary tokens, allowlisted but deliberately not prose-scanned.
# `semantic_terms` (joint Task 4) and the `table_description`/`business_context` table narrative
# (profile Task 4) were classified here and are now DEFINITION-kind — see `_DEFINITION_META_KEYS`
# above for why. What stays prose is genuinely short, non-narrative field text: a term name, a
# domain, a taxonomy/process path, a term type, and the AI's comma-joined synonym draft. None of
# those is authored against real rows, so none carries a sample clause by contract.
# `catalog_display_name` (Task 7b) is the catalog's human-readable NAME (bounded at 200) — short,
# non-narrative uploader text, the same shape as `term_name`. Not definition-kind: a name is not
# authored against rows, so it carries no sample clause by contract.
_SCALAR_PROSE_META_KEYS = frozenset({"term_name", "data_domain", "bian_path", "fibo_path",
                                     "term_type", "process_path", "ai_synonyms",
                                     "catalog_display_name"})
_PROSE_META_KEYS = _SCALAR_PROSE_META_KEYS | _LIST_PROSE_META_KEYS
_FREE_TEXT_META_KEYS = _DEFINITION_META_KEYS | _PROSE_META_KEYS

# Task 0.6 (D10): the single-call path has no ``_ITEM_META_ALLOWED`` gate, and the old
# ``_FREE_TEXT_META_KEYS & out.keys()`` intersection silently egressed every OTHER top-level key
# UNSCANNED. Every key a current producer sends is now classified explicitly — free-text (the sets
# above, scanned), STRUCTURAL/identifier (below), or ROUND-TRIP PROSE (further below, unscanned by
# documented decision); an UNKNOWN top-level key fails the whole payload CLOSED (audited by the
# caller as an egress block), never a silent unscanned ride. A new metadata key must be classified
# in exactly one of the three before anything under it can egress. STRUCTURAL means genuinely
# platform-derived: tokens, refs, closed-vocabulary values and nested payloads whose own adapters —
# ``sanitize_feature_context``, ``assert_llm_safe`` — own their scanning. Inventoried from the live
# call sites:
_STRUCTURAL_META_KEYS = frozenset({
    # enrichment item identity + rosters (the _ITEM_META_ALLOWED structural half; the batch seam
    # feeds {**shared_metadata, **item.metadata} back through the single fallback)
    "table", "column", "type", "columns", "concept",
    "column_profiles", "chunk_summaries", "column_roster",
    "party_role", "grain_role", "table_role",
    # classification vocabulary + the reclassifier's shape hints (attest/reclassify.py)
    "vocabulary", "sample_shape", "sample_semantic_type",
    # joint Task 4 — the semantic-context purpose adapters' structural half. All platform-derived:
    # `declared_type`/`operational_type` are bounded type tokens (the dual-type split Pass B
    # already carries per column), `primary_entity` is a `known_entities()` member, `concept_path`
    # is a tuple of registry concept names, `identifier_namespace` is the closed
    # {scheme, issuer_scope, basis} projection, `entity` is a registry entity link.
    "declared_type", "operational_type", "primary_entity", "concept_path",
    "identifier_namespace", "entity",
    # Task 2b tie-break adjudication: `candidates` is a nested list whose free-text fields
    # (definition / ai_summary / semantic_terms) are graded BY THE CALLER (`tie_break.py` sanitizes
    # definitions and redacts prose before building the payload — the column_profiles precedent:
    # the owning adapter scans, this gate admits the scanned shape). Refs and the remaining keys
    # are platform-derived tokens.
    "candidates",
    # ...and its three sibling tokens: a registry template id, an authored need role, a registry
    # concept name — closed-vocabulary platform values, never uploader text.
    "template_id", "need_role", "need_concept",
    # Task 3 near-label critic: the signed label window in days — a bounded integer off
    # contract_intent (migration 1059), human-confirmed at intake; never text. The candidate card
    # rides the `candidates` class above; the hypothesis rides `objective` (already redacted).
    "label_window_days",
    # Task 4b param choice: the registry's own authored parameter tuples per template — repo text,
    # closed vocabulary by construction; the hypothesis rides `objective` (already redacted).
    "parameter_menu",
    # profile Task 4 — Pass-B v3 structural context (closed-vocabulary role tokens + the bounded
    # evidence-ref roster the model must cite from).
    "authority_role", "temporal_storage_model", "evidence_refs", "profile_vocabulary",
    # Task 7b — the catalog narrative's AUTHORITY label. `producer/strength` read from the
    # revision's own stored D2 axes (`human/proposed`), so it is a closed-vocabulary pair the
    # platform minted, never uploader text. The narrative PROSE beside it is free-text-classified.
    "catalog_narrative_authority",
    # concept critic (attest/concept_critic.py)
    "logical_ref", "proposed_concept", "shape_conflicts", "proposed_concept_meaning",
    # semantic Task 5 — targeted adjudication. Both are CLOSED-VOCABULARY token lists computed by
    # the platform, never uploader or model text: `selection_reasons` is the deterministic subset of
    # `semantic_context.REASON_CODES` that referred the column, and `missing_context` is the full
    # `MISSING_CONTEXT_CODES` vocabulary the model must answer WITH (it is the menu, not data about
    # this column). Neither can carry a sample clause or a PII span by construction.
    "selection_reasons", "missing_context",
    # bridge identifier critic (attest/bridge_critic.py)
    "candidate_id", "candidate_revision_id", "left", "right",
    "deterministic_namespace_verdict", "governed_population_relation",
    "evidence", "explanation_codes",
    # contract authoring/critique/refine structural half (contract/author.py, contract/review.py)
    "feature", "aggregation", "derives_from",
    # feature assist structural half (feature_assist.py) — `columns`/`table_context` are
    # additionally routed through sanitize_feature_context after this scan
    "target", "sets", "table_context",
    # formula authoring/critic (formula/author.py, formula/critic.py)
    "authoring_intent", "tool_trail", "recipe_authoring_context", "proposal", "operand_columns",
    # Suggestion-discovery Task 1 (classified at the main merge): REPO-AUTHORED recipe labels and
    # legacy tags — SME-authored closed-vocabulary values, never uploader/source-file data. The one
    # prose-shaped sibling (`recipe_intent`) is deliberately NOT here — see _ROUNDTRIP_PROSE_KEYS.
    "recipe_family", "recipe_aggregation", "funnel_stage", "legacy_use_case_tags",
})

# ROUND-TRIP PROSE: free text that is DELIBERATELY egressed unscanned because it is the caller's
# OWN text returning to the model — either the model's output from the immediately preceding call
# of the same flow (the LLM's drafted `definition` and critique `findings`, contract/review.py) or
# the operating human's instruction to the model (`objective`, `feedback`, `fix`, `avoid`,
# feature_assist.py). Nothing here is uploader/source-file data, which is why it does not ride the
# `_FREE_TEXT_META_KEYS` scrubbers; expanding the PII/data-marker scan over these round-trip fields
# is the deferred B-2 egress hardening (docs/DEFERRED-WORK.md §B-2). These keys are NOT structural
# — classifying them as such would misdocument the egress surface (the batch seam forbids plain
# `definition` for exactly this reason) — but they ARE known: the fail-closed gate treats both
# classes as classified.
_ROUNDTRIP_PROSE_KEYS = frozenset({
    "definition", "findings",            # model round-trip (contract critique/refine)
    "objective", "feedback", "fix", "avoid",   # human round-trip (feature assist)
    # Outbound-only SME-authored prose (suggestion-discovery Task 1, classified at the main
    # merge): a recipe's intent sentence is repo-authored and length-bounded (400), never
    # uploader/source-file data — but it IS prose, and calling it structural would misdocument
    # the egress surface for exactly the reason this class's charter names.
    "recipe_intent",
})


# ── DROPPABLE ADVISORY CONTEXT (Task 7b review, Important-1) ─────────────────────────────────────
#
# Free text whose content is CONTEXT ABOUT the subject rather than the subject itself. A value the
# sanitizer BLANKS is dropped from the payload; it does not fail the payload closed.
#
# The rule everywhere else — blanked means block — is right where the field IS the subject. A column
# `business_definition` the sanitizer blanks must block its item: silently egressing the column
# without the meaning the model was asked to reason about produces a confident answer to a question
# nobody could answer. The catalog narrative is not that. Losing it DEGRADES the payload; it cannot
# falsify it, and the model is never told a narrative exists.
#
# WHAT MADE THIS LOAD-BEARING: BLAST RADIUS. `sanitize_definition` fails closed on five literal
# phrases (`sample values`, `example values`, `observed values`, `representative values`, `values
# such as` — sanitize.py:55-62), and a business user types the narrative into a form that validates
# LENGTH AND TYPE ONLY (`catalog_profiles.parse_narrative_payload`). "The description gives example
# values for each payment type" is ordinary phrasing. The narrative rides EVERY table block and
# EVERY Pass-B item, so under the ordinary rule that one sentence would refuse every
# feature-generation call for the catalog AND exclude every table from Pass B — no grain, no
# table_role, no primary_entity, no event_or_snapshot, audited only as an egress block. One
# sentence, whole catalog. A bad phrase in one `table_definition` costs one table; this cost
# everything.
#
# NOT SOLVED BY DOWNGRADING TO THE PROSE GRADE. Prose has no such failure mode precisely because it
# has no sample-clause strip and no marker gate — so `values such as 3708484836801` would egress
# intact, which is the leak the definition grade exists to stop and which real FTR prose demonstrably
# contains. Dropping keeps the strong scan AND removes the kill switch: the marker-bearing value
# never egresses, and everything else still does.
#
# The drop is NOT silent: the `sample_audits` entry (state `suspected_unhandled`) is recorded exactly
# as before, so `llm_call.input_redaction` still says what was removed and why, and both seams log.
_ADVISORY_CONTEXT_KEYS = frozenset(CATALOG_NARRATIVE_KEYS)


def _meta_field_kind(key: str) -> str:
    """The egress KIND of one free-text metadata key: ``definition`` (sample-strip + PII via
    `sanitize_definition`), ``prose`` (PII-only via `redact_free_text`), or ``list_of_prose``
    (per-item PII). A free-text key with NO declared kind is a hard error ([F6] fail closed) —
    a new key must be classified before anything under it can egress, never silently routed
    down the weaker prose path."""
    if key in _DEFINITION_META_KEYS:
        return "definition"
    if key in _LIST_PROSE_META_KEYS:
        return "list_of_prose"
    if key in _SCALAR_PROSE_META_KEYS:
        return "prose"
    raise ValueError(f"free-text metadata key {key!r} has no declared egress kind (fail closed)")


def _redact_free_text_meta(metadata: dict) -> tuple[dict | None, list[dict], list[dict], str | None]:
    """Route every glossary free-text value in `metadata` (top-level keys + each column_profiles
    descriptor's business_definition) through its FIELD-KIND sanitizer (`_meta_field_kind`).
    Returns ``(redacted_metadata, pii_spans, sample_audits, redaction_version)``:

    * ``redacted_metadata`` — the metadata with sanitized free-text, or ``None`` when any value
      failed closed (the caller must not egress the item): a prose value the redactor returned
      ``None`` for, or a definition `sanitize_definition` BLANKED (``suspected_unhandled`` marker
      or ``pii_redaction_failed``);
    * ``pii_spans`` — ``{"key", "type", "start", "end"}`` per scrubbed PII span (types/positions,
      NEVER values) for ``input_redaction["redacted_spans"]``. Definition fields contribute their
      `sanitize_definition` spans at the same granularity ([F3]); list items are keyed at their
      indexed path (``synonyms[0]``);
    * ``sample_audits`` — ``{"path", "sanitizer_version", "state", "removed_count"}`` per
      DEFINITION field processed (prose fields never emit one) for
      ``input_redaction["sample_strip"]``;
    * ``redaction_version`` — the redactor/sanitizer version that scanned the free-text, or
      ``None`` when the metadata carried no free-text at all (pure structural names/types)."""
    out = dict(metadata)
    pii_spans: list[dict] = []
    sample_audits: list[dict] = []
    version: str | None = None
    dropped = False

    # D10: fail CLOSED on any top-level key with NO declared classification — the intersection loop
    # below only visits the free-text keys, so an unclassified key would otherwise egress unscanned
    # through the single-call path (the batch path's _ITEM_META_ALLOWED never let one this far).
    # Same disposition as a redactor fail-closed: the caller blocks dispatch + audits EGRESS_BLOCKED.
    # Round-trip prose is KNOWN (classified, documented unscanned), so it never trips this gate.
    unknown = sorted(k for k in out
                     if k not in _FREE_TEXT_META_KEYS and k not in _STRUCTURAL_META_KEYS
                     and k not in _ROUNDTRIP_PROSE_KEYS)
    if unknown:
        logger.warning("metadata key(s) with no declared egress classification: %s — failing "
                       "closed (no egress kind)", unknown)
        return None, pii_spans, sample_audits, version

    def _definition(text: str, path: str) -> str | None:  # None ⟹ fail closed (blanked field)
        nonlocal version
        d = sanitize_definition(text)
        version = version or d.redaction_version or d.sanitizer_version
        sample_audits.append({"path": path, "sanitizer_version": d.sanitizer_version,
                              "state": d.state, "removed_count": d.removed})
        if d.reason:
            # The sanitizer blanked the field (unhandled data marker, or its PII redaction failed
            # closed): nothing provably safe — block the whole item, matching the prose contract.
            return None
        # [F3]: the definition's PII spans keep reaching input_redaction["redacted_spans"] at the
        # same granularity as prose fields — the sample audit above is IN ADDITION, not instead.
        pii_spans.extend({"key": path, **dict(s)} for s in d.redacted_spans)
        return d.clean

    def _prose(text: str, path: str) -> str | None:        # None ⟹ fail closed
        nonlocal version
        res = redact_free_text(text)
        version = version or res.redaction_version
        if res.text is None:
            return None
        pii_spans.extend({"key": path, **dict(s)} for s in res.redacted_spans)
        return res.text

    for key in sorted(_FREE_TEXT_META_KEYS & out.keys()):
        kind = _meta_field_kind(key)                       # ValueError on an unclassified key
        scrub = _definition if kind == "definition" else _prose
        # Advisory context DROPS rather than blocking — see `_ADVISORY_CONTEXT_KEYS`. The audits and
        # spans already appended by the scrubber are kept: the record must say what was removed.
        droppable = key in _ADVISORY_CONTEXT_KEYS
        val = out[key]
        if droppable and not isinstance(val, (str, list)):
            # UNSCANNABLE. Neither branch below can run on it, so leaving it in place would egress
            # advisory free text that NO scrubber ever looked at — the batch path is protected by
            # `_item_shape_ok`, the single-call path is not. Dropping is the fail-closed direction
            # and costs only context. (Deliberately scoped to the advisory keys: the same shape
            # under an ordinary free-text key is pre-existing behaviour this task does not own.)
            logger.warning("advisory context %r is not scannable free text (%s) — DROPPED from the "
                           "item", key, type(val).__name__)
            del out[key]
            dropped = True
            continue
        if isinstance(val, str):
            redacted = scrub(val, key)
            if redacted is None:
                if not droppable:
                    return None, pii_spans, sample_audits, version
                logger.warning("advisory context %r failed its egress scan — DROPPED from the item "
                               "(the item still egresses)", key)
                del out[key]
                dropped = True
                continue
            out[key] = redacted
        elif isinstance(val, list):
            new_list = []
            for i, v in enumerate(val):
                nv = scrub(v, f"{key}[{i}]") if isinstance(v, str) else v
                if nv is None:
                    if not droppable:
                        return None, pii_spans, sample_audits, version
                    logger.warning("advisory context %r[%d] failed its egress scan — that ITEM "
                                   "dropped", key, i)
                    dropped = True
                    continue
                new_list.append(nv)
            # An advisory list scrubbed empty carries nothing; a fabricated `[]` is not context.
            if droppable and not new_list:
                del out[key]
                dropped = True
                continue
            out[key] = new_list
    profiles = out.get("column_profiles")
    if isinstance(profiles, list):
        new_profiles = []
        for desc in profiles:
            if not isinstance(desc, dict):
                new_profiles.append(desc)
                continue
            # [F4] Same D10 fail-closed rule the top-level loop applies, one level down: this scan
            # used to name `business_definition` and let every sibling key ride unscanned, so the
            # descriptor namespace had no gate at all. The batch path's `_item_shape_ok` rejects an
            # unknown descriptor key, but the SINGLE-CALL path has no such gate — exactly the
            # asymmetry D10 closed above.
            unknown_desc = sorted(k for k in desc if k not in _COLUMN_PROFILE_KEYS)
            if unknown_desc:
                logger.warning("column_profiles descriptor key(s) with no declared egress "
                               "classification: %s — failing closed", unknown_desc)
                return None, pii_spans, sample_audits, version
            updated = dict(desc)
            for key in sorted(desc):
                if key in _COLUMN_PROFILE_DEFINITION_KEYS:
                    scrub = _definition
                elif key in _COLUMN_PROFILE_PROSE_KEYS:
                    scrub = _prose
                else:
                    continue                                   # structural: no scan by contract
                if not isinstance(desc[key], str):
                    continue
                nv = scrub(desc[key], f"column_profiles.{key}")
                if nv is None:
                    # A descriptor field IS the subject the model reasons about, so a blanked value
                    # blocks the item rather than dropping (the `_ADVISORY_CONTEXT_KEYS` argument
                    # does not reach here) — the contract `business_definition` already had.
                    return None, pii_spans, sample_audits, version
                updated[key] = nv
            new_profiles.append(updated)
        out["column_profiles"] = new_profiles
    # `dropped` guards the passthrough (review round 2). A value the scrubber REFUSED without ever
    # setting `version` — a non-`str` under a free-text key, so no scrubber body ran — would
    # otherwise be handed back inside the ORIGINAL `metadata`, egressing the exact value the log
    # line above says was dropped. "Never silent" is the contract this drop mechanism rests on; a
    # warning that is not true is worse than no warning.
    if version is None and not dropped:
        return metadata, [], [], None                      # no free-text — metadata untouched
    return out, pii_spans, sample_audits, version


# The feature MENU's nested field-aware egress classification (spec §5, [F4]). Distinct top-level
# keys (`columns` of DICTS, `table_context`) from the enrichment payload, so this adapter is inert
# on every enrichment call (a `columns` list of STRINGS is left untouched). RF-I6 (BINDING): the
# adapter fires on EVERY audited_structured_call, including `overlay.contract.draft`, whose
# `columns` is contract/author.py `_column_defs(...)` — {object_ref, column, concept, definition},
# with `concept`/`definition` NULLABLE straight from graph_node. Those keys are all classified
# below (identity/definition-kind), and a None value in a definition-kind field passes through as
# content-free — so the legitimate draft path is IMPROVED (its definitions now sample-stripped),
# never fail-closed. Only a genuinely-unknown key blocks.
# `ai_summary` is definition-KIND prose: it egresses under the same sample-strip + PII scan as
# `definition`. Without it here an ai_summary key is genuinely-unknown and blocks the WHOLE column
# descriptor — wiring the query alone would have removed columns from the menu, not enriched them.
_FEATURE_COLUMN_DEFINITION_KEYS = frozenset({"definition", "ai_summary", "semantic_terms"})
# Identity strings: `catalog_source` is deliberately NOT emitted by the enriched menu (RF-I7
# handoff) and `concept` may be None — absence/None is structural-optional, never a block.
# `party_role` (feature-context v4) is a closed-vocabulary token from `party_vocab`, the same grade
# of structural identity as `concept`.
#
# TWO of the five D13.1/D13.2 CLASSIFICATION AXES (Task 6) join them here:
#
#   * `table_role` / `event_or_snapshot` are platform-derived `_TABLE_ADVISORY` tokens — the same
#     grade `_TABLE_CONTEXT_IDENTITY_KEYS` gives `data_role`/`primary_entity`, and `table_role` is
#     already `_STRUCTURAL_META_KEYS` on the enrichment seam.
#
# `domain` / `sub_domain` / `bian_path` / `process_path` are DELIBERATELY NOT here — see
# `_FEATURE_COLUMN_PROSE_KEYS`.
#
# THE RULE, RESTATED (2026-08-09, final branch review — it used to read "the grade follows who
# TYPED the value", and `sub_domain` was graded identity on that reading). Who typed the value is
# not the question; whether the value is drawn from a CLOSED SET is. A closed-vocabulary token
# (`concept`, `party_role`, `confidence_band`) cannot carry an arbitrary span, so a type-and-length
# check is the whole of its safety. Free text cannot be made safe that way whoever authored it —
# and a model drafting from a payload that carries uploader definitions can echo an uploader's
# account number as readily as the uploader can type one. See `_FEATURE_COLUMN_PROSE_KEYS` for the
# failure-direction argument that decides it.
#
# These are ACCEPTANCE-gated at `_FEATURE_STRUCTURAL_MAX_LEN`, and the gate is WHOLE-PAYLOAD
# FAIL-CLOSED, not per column: an over-long value makes `sanitize_feature_context` return None, the
# caller blocks dispatch and audits EGRESS_BLOCKED, and NO column is enriched on that call. It is
# never truncated into a different classification token, and never quietly dropped either. `_refuse`
# logs which element and which key did it, because on a 237-column catalog that is the difference
# between a diagnosis and an opaque failure. (An earlier version of this comment claimed the value
# "excludes the column and is audited" — it excluded the whole payload and logged nothing.)
#
# `confidence_band` (Task 6b) is the ADJUDICATOR's grade — a member of the closed
# `semantic_adjudication.CONFIDENCE_BANDS` vocabulary (`high|medium|low`), validated on the way in
# (`adjudication_from_output` invalidates the whole result for an off-vocabulary band) and AGAIN on
# the re-validating current-pointer read. It is PLATFORM-generated, never uploader-typed, so the
# two-origin argument that moved `domain` to prose does not reach it and the prose grade's redactor
# would have nothing to scrub. It is EXPLANATION, never authority: nothing branches on it.
_FEATURE_COLUMN_IDENTITY_KEYS = frozenset({"object_ref", "table", "column", "concept",
                                           "party_role",
                                           "table_role", "event_or_snapshot",
                                           "confidence_band"})
# PROSE grade for the feature menu: PII-redacted via `redact_free_text` (no sample-clause strip and
# no data-marker gate — that is the DEFINITION grade), then length-bounded like any structural
# value. This is the grade `_SCALAR_PROSE_META_KEYS` (line ~125) already gives these exact three
# keys on the ENRICHMENT seam (as `data_domain`, `bian_path`, `process_path`), and the two seams now
# agree — the principle stated in this file's egress header. Three reasons they are prose and not
# identity, in the order they were established:
#
#   1. `redact_free_text`'s own docstring names its subject as "an uploaded glossary's curated
#      business definitions / synonyms / TAXONOMY PATHS". These are the values it was written for.
#   2. "They are already redacted at ingest" is NOT true. `bian_path` has TWO origins:
#      `ftr_adapter` builds it through `_redact` (`ftr_adapter.py:482`), but the generic
#      `glossary_reader.read_glossary` — a live upload branch via `api/routes/uploads.py:155` —
#      builds the same `GlossaryRecord` with a bare `_cell(...)` and NO redaction, and
#      `ingest._write_glossary_source_evidence` persists either one identically. `domain` is the
#      same shape (`glossary_reader.py:242`), and from there it is projected onto
#      `graph_node.domain` (`field_resolution.py`) and emitted by
#      `semantic_context.for_feature_generation`. (`process_path` IS single-origin — only the FTR
#      adapter populates it — but grading the trio apart on that difference would be a trap for the
#      next reader.)
#   3. Identity grade applies `_structural_ok` ONLY: a type-and-length check, no redaction and no
#      audit trail.
#
# WHAT THE PROSE GRADE ACTUALLY BUYS, stated precisely so nobody reads more into it. It does NOT add
# detection: `redact_free_text`'s `_scan` and `assert_llm_safe`'s `_first_pii` walk the SAME
# `_PII_PATTERNS` tuple, and NO `IntentRedactor` is registered anywhere in `src/` (only the
# `register_intent_redactor` helper exists), so `redact_free_text` falls back to
# `DefaultIntentRedactor` and the pattern set is identical on both seams. A personal NAME in a
# `domain` still egresses today, at either grade — closing that needs a registered NER redactor, not
# this list. What it buys is three concrete things:
#
#   a. FAILURE DIRECTION. At identity grade a detectable PII token sails past `_structural_ok` and
#      then trips `assert_llm_safe`, which raises `EgressViolation` and kills the WHOLE feature-
#      generation call. Prose grade scrubs the span and the call proceeds.
#   b. AUDIT. The scrub is reported as a `pii_spans` entry keyed `columns[N].domain` with a
#      redaction_version, so `llm_call.input_redaction` records what left and under which version.
#      Identity grade records nothing.
#   c. The DI SEAM. When a NER-backed `IntentRedactor` IS registered, prose-graded values route
#      through it; identity-graded values never would. This is the line that makes (3) closeable
#      later without touching this list again.
#
# `business_term` (Task 7b) joins them: the glossary's curated business NAME for the column
# ("Counterparty Exposure Amount" for `CPTY_EXPSR_AMT`). Uploader-authored through the SAME
# unredacted `glossary_reader` branch reason (2) names for `domain`, and already prose-graded on the
# enrichment seam as `term_name` (`_SCALAR_PROSE_META_KEYS`) — so the two seams agree, which is this
# file's stated principle.
#
# `sub_domain` (D13.2) joins them too — DECIDED 2026-08-09 at the final branch review, reversing
# Task 6, which added it at identity grade in the very commit that moved `bian_path`/`process_path`
# OUT of identity grade for this exact reason. The argument it rested on ("the grade follows who
# TYPED the value"; `sub_domain`'s only producer is `enrich._write_sub_domain_evidence`) is true and
# does not decide the question:
#
#   * Reason (a) below is ORIGIN-INDEPENDENT. It is about what happens when a detectable token
#     reaches the wire, not about who put it there. At identity grade one such token on one column
#     kills the whole call, for all 237 columns; at prose grade it is scrubbed and the call
#     proceeds. Availability is the whole of the difference, and Task 6 accepted precisely that
#     argument for the other two axes.
#   * `sub_domain` is NOT a closed vocabulary. It is the model's free-text refinement of `domain`,
#     admitted by `accept_label` — a length-and-shape gate, not a member check — and it is drafted
#     from a payload carrying uploader definitions, so an uploader's account number can arrive in
#     it by echo. That is what separates it from `concept` / `party_role` / `confidence_band`,
#     which are validated against registries and cannot carry an arbitrary span at all.
#   * It carries the SAME `field_policies._MEANING` policy as `domain`, which is prose-graded by an
#     explicit human decision (2026-08-07). Grading a parent and its refinement differently is a
#     trap for the next reader, and the pair reads identically at every other seam.
#
# Cost of the move: one `redact_free_text` pass per column. The wire is byte-identical for a clean
# value (the redactor returns unchanged text), and the bound is the same `_FEATURE_STRUCTURAL_MAX_LEN`
# — applied AFTER redaction, so a near-budget value carrying PII can now be refused where an
# un-scrubbed one of the same length was admitted. That is the fail-closed direction.
# `fibo_path` joins its two sidecar siblings here (readiness wave). Uploader-authored, unredacted
# at origin (`glossary_reader`), so it takes the same prose grade `domain` does — a length bound
# is not a safety property.
_FEATURE_COLUMN_PROSE_KEYS = frozenset({"domain", "sub_domain", "bian_path", "process_path",
                                        "fibo_path", "business_term"})
# The LIST form of the same grade (Task 7b): each item PII-redacted at its own indexed path
# (`related_terms[1]`) and bounded per TERM, mirroring `_LIST_PROSE_META_KEYS` on the enrichment
# seam. `related_terms` is uploader-authored curated vocabulary, so the token-list grade its SHAPE
# suggests would be wrong twice over — no redaction, and no audit of what left.
_FEATURE_COLUMN_PROSE_LIST_KEYS = frozenset({"related_terms"})
_FEATURE_COLUMN_FACT_KEYS = frozenset({
    "data_type", "declared_type", "entity", "additivity", "unit", "currency",
    "is_grain", "is_as_of"})
# `proposed_value` (Task 6) is the LLM's answer for a fact it could not win — present ONLY where
# `value` is null, and a bounded token exactly like `value`. It is a SUBKEY of the wrapper rather
# than a sibling column key so it cannot be read as an operationally-resolved fact by anything that
# reads `.value`; classifying it here is what stops the whole column failing closed.
# [F7] `proposed_authority` is the `producer/strength` of the row `proposed_value` came from —
# a platform-minted closed-vocabulary pair, exactly like `authority`, never model or uploader
# text. Classified here or the whole column fails closed on an unknown subkey.
_FEATURE_FACT_SUBKEYS = frozenset({"value", "authority", "proposed_value",
                                   "proposed_authority"})
# ── feature-context v4 (semantic Task 8, D10) ────────────────────────────────────────────────────
# Every NEW key ships with an explicit classification here plus a golden egress test; unclassified
# stays BLOCKED, and now loudly. All four groups carry closed-vocabulary tokens or platform-minted
# ids — never uploader prose, so none of them is sample-bearing and none routes through the
# definition sanitizer.
#
#   * `concept_path` / `missing_context` — lists of closed-vocabulary tokens (registry concept
#     names; `semantic_context.MISSING_CONTEXT_CODES`).
#   * `concept_alternatives` (Task 6b) — the adjudicator's shortlist: at most
#     `semantic_adjudication.MAX_ALTERNATIVES` (3) names, each filtered to `CONCEPT_REGISTRY`
#     membership on the way in and again on the re-validating read, so it is the SAME closed
#     vocabulary `concept_path` carries and takes the same grade. It is CONTEXT for the model to
#     weigh, never a classification and never an authority claim of its own.
#   * `identifier_namespace` — {scheme, issuer_scope, basis}: the value space an identifier lives
#     in. `scheme` is a registry namespace, `basis` is the closed NAMESPACE_BASES vocabulary and
#     `issuer_scope` is a catalog-scope token.
#   * `semantic_authority` — {field: "producer/strength"}. The D2 triple ON THE WIRE, per D10: the
#     wire carries (producer, strength), never the derived `llm_proposed` display label. This is
#     what lets the model see that a concept is an AI PROPOSAL while the governed|hint fact wrapper
#     keeps meaning what it always meant.
#   * `relationships` — the current cross-catalog links: platform-minted refs plus the closed
#     availability/review vocabularies. NEVER a claim of executability (that answer needs a
#     revalidating reader and does not belong in a prompt).
_FEATURE_COLUMN_TOKEN_LIST_KEYS = frozenset({"concept_path", "missing_context",
                                             "concept_alternatives"})
_FEATURE_COLUMN_OBJECT_KEYS: dict[str, frozenset[str]] = {
    "identifier_namespace": frozenset({"scheme", "issuer_scope", "basis"}),
}
_FEATURE_COLUMN_MAPPING_KEYS = frozenset({"semantic_authority"})
_FEATURE_COLUMN_DICT_LIST_KEYS: dict[str, frozenset[str]] = {
    # `outbound_cardinality` (Task 6) is the closed `Cardinality` vocabulary or None — a token like
    # its siblings, so the existing `_dict_list_ok` shape still holds. It is DIRECTIONAL (the hop
    # away from the anchor's own table) and named for its direction; a bare `cardinality` here
    # would read as a verdict on the pair, which `semantic_context` explicitly refuses to publish.
    "relationships": frozenset({"relationship_ref", "kind", "availability", "review_status",
                                "outbound_cardinality"}),
}
# `business_context` is PROSE (a data owner wrote it), so it routes through the definition
# sanitizer exactly like `table_definition` — sample-clause stripped, data-marker scanned, PII
# redacted. Classifying it as an identity string would have let an uploader's paragraph egress
# unscanned, which is the whole reason the definition grade exists.
#
# Task 7b adds the two long CATALOG-narrative fields at the same grade and for the same reason —
# see `_DEFINITION_META_KEYS`. Note the DELIBERATE key names: `catalog_business_context` is the
# narrative a human typed about the CATALOG (migration 1047); the unprefixed `business_context`
# beside it is the per-TABLE narrative Pass B writes (migration 1052). Two different values, two
# grains, one block — the prefix is what keeps them apart on the wire.
#
# There is no length ceiling on this grade, which is why the 4000-character fields take it: an
# acceptance gate that refused a legitimately-authored narrative would refuse the WHOLE payload,
# not the field.
_TABLE_CONTEXT_DEFINITION_KEYS = frozenset({"table_definition", "business_context",
                                            "catalog_description", "catalog_business_context"})
# The Release-A profile classifications are CLOSED-vocabulary tokens (profile_vocab), not prose.
# `catalog_narrative_authority` is the `producer/strength` pair read off the narrative revision's
# own stored D2 axes — platform-minted, never uploader text.
#
# Task 8 adds `grain_status` / `as_of_status` at the SAME grade and for the same reason: each is one
# of a CLOSED set of platform-minted tokens that `_table_context` computes. Nothing uploader-typed
# and nothing model-authored reaches them, so the prose grade's redactor would have nothing to scrub
# and would misdocument the egress surface. They are LABELS, never permission: see `_table_context`.
#
# TASK 8b WIDENED THE VALUE SET AND ADDED NO KEY, which is why this list is unchanged. The tokens are
# now `feature_assist.TABLE_FACT_STATUSES` — human_confirmed | source_declared | ai_proposed, an
# AUTHORITY axis naming who asserted the value — replacing Task 8's "confirmed" | "declared", which
# conflated a human endorsement with ingest's source-declared auto-confirm. Named here rather than
# left stale because this file is the first thing a reviewer of the egress surface reads, and a
# retired vocabulary documented at the classifier is how the next reader learns the wrong one.
_TABLE_CONTEXT_IDENTITY_KEYS = frozenset({"table", "as_of_column", "primary_entity",
                                          "data_role", "authority_role",
                                          "temporal_storage_model",
                                          "catalog_narrative_authority",
                                          "grain_status", "as_of_status"})
# The table block's PROSE grade (Task 7b) — the missing half of this seam. The column side has had
# `_FEATURE_COLUMN_PROSE_KEYS` since `domain` moved off identity grade in `c62ab49d`; the table side
# had only DEFINITION (sample-strip + marker gate) and LIST/IDENTITY (no redaction at all), so a
# short uploader-typed label had nowhere honest to go. `catalog_display_name` is bounded at 200 by
# `catalog_profiles.MAX_DISPLAY_NAME`, well inside `_FEATURE_STRUCTURAL_MAX_LEN`, with room for
# redaction to LENGTHEN it.
_TABLE_CONTEXT_PROSE_KEYS = frozenset({"catalog_display_name"})
# The list form: each item redacted and bounded on its own, at its own indexed audit path. NOT
# `_TABLE_CONTEXT_LIST_KEYS`, which is for PLATFORM-authored sentences (`advisories`) and structural
# refs (`grain_columns`) and applies no redaction — grading uploader text there was the exposure
# `c62ab49d` fixed one seam over.
_TABLE_CONTEXT_PROSE_LIST_KEYS = frozenset({"catalog_business_domains"})
# `advisories` are PLATFORM-authored sentences from a closed table in this repo — never uploader
# text and never model output — so they are length-bounded like any other structural list.
_TABLE_CONTEXT_LIST_KEYS = frozenset({"grain_columns", "advisories"})
#: Advisory sentences are longer than a ref; bounded so a mutated table cannot smuggle a payload.
#: ZERO-TRUNCATION RAISE (2026-08-06): 400 -> 2000, ~10x the longest advisory this repo's closed
#: advisory table emits. Still a bound — a mutated table cannot smuggle an unbounded payload.
_TABLE_ADVISORY_MAX_LEN = 2000
#: ZERO-TRUNCATION RAISE (2026-08-06): 200 -> 1000. This is an ACCEPTANCE gate, not a formatter: a
#: value over it is EXCLUDED from egress and audited, never truncated. So values of 201–1000 chars
#: are now ADMITTED where they were previously refused — a deliberate widening of the egress
#: SURFACE. Redaction (`_redact_free_text_meta` / `sanitize_feature_context`) still runs first, so
#: this is a size decision, not a PII one.
_FEATURE_STRUCTURAL_MAX_LEN = 1000
#: Bound on a v4 list/mapping so one column cannot carry an unbounded payload past the byte budget
#: into the egress scanner. ZERO-TRUNCATION RAISE (2026-08-06): 40 -> 256, above the widest real
#: table this platform ingests (144 columns) so a per-column collection is never clipped.
_FEATURE_COLLECTION_MAX_ITEMS = 256


def _token(v: object) -> bool:
    """A bounded, non-prose token: a short string with no room for a sample value."""
    return isinstance(v, str) and len(v) <= _FEATURE_STRUCTURAL_MAX_LEN


def _token_list_ok(v: object) -> bool:
    return (isinstance(v, list) and len(v) <= _FEATURE_COLLECTION_MAX_ITEMS
            and all(_token(x) for x in v))


def _token_object_ok(v: object, allowed: frozenset[str]) -> bool:
    """A flat object whose keys are exactly allowlisted and whose values are tokens or None."""
    if v is None:
        return True
    if not isinstance(v, dict) or any(k not in allowed for k in v):
        return False
    return all(x is None or _token(x) for x in v.values())


def _mapping_ok(v: object) -> bool:
    """A ``{field_name: token}`` mapping — bounded in both directions. Keys are field names the
    platform minted, values are short closed-vocabulary tokens (``producer/strength``)."""
    if not isinstance(v, dict) or len(v) > _FEATURE_COLLECTION_MAX_ITEMS:
        return False
    return all(_token(k) and _token(x) for k, x in v.items())


def _dict_list_ok(v: object, allowed: frozenset[str]) -> bool:
    if not isinstance(v, list) or len(v) > _FEATURE_COLLECTION_MAX_ITEMS:
        return False
    return all(_token_object_ok(entry, allowed) and isinstance(entry, dict) for entry in v)


def _fact_wrapper_ok(v: object) -> bool:
    """A governed/hint fact wrapper: {value, authority} plus the optional `proposed_value`; both
    values a bounded str or None (RF-I7: is_grain/is_as_of booleans arrive RENDERED as
    "true"/"false" strings); authority in {governed, hint}. Enum-ish tokens, never sample-bearing
    prose. `proposed_value` is bounded on the SAME gate as `value` — it is a model-authored token,
    so an unbounded one would be the one place a wrapper could smuggle a payload."""
    if not isinstance(v, dict) or any(k not in _FEATURE_FACT_SUBKEYS for k in v):
        return False
    for key in ("value", "proposed_value"):
        val = v.get(key)
        if val is not None and not (isinstance(val, str)
                                    and len(val) <= _FEATURE_STRUCTURAL_MAX_LEN):
            return False
    return v.get("authority") in ("governed", "hint")


def sanitize_feature_context(
        metadata: dict) -> tuple[dict | None, list[dict], list[dict], str | None]:
    """Nested field-aware egress adapter for the feature menu ([F4], spec §5). Traverses
    ``columns[*]`` (DICT elements only — a list-of-strings `columns` from enrichment is left
    untouched) and any ``table_context[*]`` block. Definition-kind fields (`definition`,
    `semantic_terms`, `table_definition`) route through `sanitize_definition` (sample-clause strip +
    fail-closed data-marker scan + PII redaction); a ``None`` there is content-free and passes
    through (the contract-draft `_column_defs` shape carries NULL graph definitions — RF-I6).
    Structural fields (identity strings, {value, authority} fact wrappers, grain-column ref lists)
    are exact-key allowlisted + length-bounded, never sample-stripped. FAIL CLOSED: any unclassified
    key anywhere, or a definition the sanitizer BLANKS, returns (None, ...) — the caller blocks
    dispatch + audits EGRESS_BLOCKED. Returns the SAME ``metadata`` object (untouched) when no
    feature menu is present or only structural passthrough occurred, so non-feature and flag-off
    payloads stay byte-identical."""
    columns = metadata.get("columns")
    table_context = metadata.get("table_context")
    has_cols = isinstance(columns, list) and any(isinstance(c, dict) for c in columns)
    has_ctx = isinstance(table_context, list) and any(isinstance(b, dict) for b in table_context)
    if not has_cols and not has_ctx:
        return metadata, [], [], None

    pii_spans: list[dict] = []
    sample_audits: list[dict] = []
    version: str | None = None
    dropped = False

    def _defn(text: object, path: str) -> str | None:  # None ⟹ fail closed
        nonlocal version
        if not isinstance(text, str):
            return None
        d = sanitize_definition(text)
        version = version or d.redaction_version or d.sanitizer_version
        sample_audits.append({"path": path, "sanitizer_version": d.sanitizer_version,
                              "state": d.state, "removed_count": d.removed})
        if d.reason:
            return None
        pii_spans.extend({"key": path, **dict(s)} for s in d.redacted_spans)
        return d.clean

    def _prose(text: object, path: str) -> str | None:  # None ⟹ fail closed
        """PII-only redaction for the uploader-authored short labels — the taxonomy paths and the
        business `domain`. The `_prose` half of the enrichment seam's grade, applied here. Bounded
        AFTER redaction, like the enrichment seam's own length gate, so the cap applies to what
        would actually egress rather than to what arrived.

        Redaction is NOT length-preserving in the shrinking direction: `DefaultIntentRedactor`
        substitutes a `[REDACTED:LABEL]` placeholder, which is LONGER than several of the tokens it
        replaces (a 6-char email becomes 16 chars). So a near-budget value carrying PII can cross
        `_FEATURE_STRUCTURAL_MAX_LEN` after scrubbing and be refused where an un-scrubbed one of the
        same length was admitted. That is the fail-closed direction and it is deliberate — bounding
        BEFORE redaction would bound a string that is not the one egressing."""
        nonlocal version
        if not isinstance(text, str):
            return None
        res = redact_free_text(text)
        version = version or res.redaction_version
        if res.text is None or len(res.text) > _FEATURE_STRUCTURAL_MAX_LEN:
            return None
        pii_spans.extend({"key": path, **dict(s)} for s in res.redacted_spans)
        return res.text

    def _prose_list(v: object, path: str, *, droppable: bool = False) -> list | None:
        """A LIST of uploader-authored prose — `_prose` per item, at that item's own indexed path
        (`related_terms[1]`), so the audit keeps per-term granularity and one long term is bounded
        on its own rather than as part of a joined blob. A non-list, or a list over the collection
        cap, fails closed like any other shape violation (``None``).

        ``droppable`` (the `_ADVISORY_CONTEXT_KEYS` contract): a single item that fails its scan is
        DROPPED and the rest of the list still egresses, rather than the failure escalating to the
        whole payload. The shape violations above still fail closed either way — a non-list is a
        producer bug, not a scrubbing outcome."""
        if not isinstance(v, list) or len(v) > _FEATURE_COLLECTION_MAX_ITEMS:
            return None
        cleaned: list[str] = []
        for i, item in enumerate(v):
            clean = _prose(item, f"{path}[{i}]")
            if clean is None:
                if not droppable:
                    return None
                _drop_advisory(f"{path}[{i}]")
                continue
            cleaned.append(clean)
        return cleaned

    def _drop_advisory(path: str) -> None:
        """Record an advisory-context value removed by its own scan. Never silent: the
        `sample_audits` entry the scrubber already appended says WHAT was removed and why, and
        `dropped` keeps the structural-passthrough below from handing the original back."""
        nonlocal dropped
        dropped = True
        logger.warning("advisory context %s failed its egress scan — DROPPED from the block (the "
                       "payload still egresses)", path)

    def _structural_ok(v: object) -> bool:  # identity strings may be None (`concept` is nullable)
        return v is None or (isinstance(v, str) and len(v) <= _FEATURE_STRUCTURAL_MAX_LEN)

    def _refuse(path: str, rule: str) -> tuple[None, list[dict], list[dict], str | None]:
        """Refuse the WHOLE payload, and say which element and which key did it.

        Every branch below fails closed to the same `(None, ...)`, and the caller turns that into a
        blocked call with no dispatch — so ONE over-long `sub_domain` on ONE column costs feature
        generation for every column in the catalog. That is the intended direction (the alternative
        is egressing a value nobody scanned), but until now it was also OPAQUE: a 237-column run
        returned nothing with no record of which key tripped it, and re-running does not narrow it.

        VALUE-FREE by construction, exactly like the `enrich_reject` line: the `path` is a key name
        and a list index this code minted, `rule` is a member of the closed set below, and the value
        itself is never read here. Lengths, counts, codes, refs — never content."""
        logger.warning("feature-context egress REFUSED the whole payload at %s (rule=%s) — the "
                       "call is blocked and NO column is dispatched", path, rule)
        return None, pii_spans, sample_audits, version

    new_columns = columns
    if has_cols:
        rebuilt: list = []
        for idx, col in enumerate(columns):
            if not isinstance(col, dict):
                rebuilt.append(col)              # enrichment roster token — untouched
                continue
            out: dict = {}
            for k, v in col.items():
                path = f"columns[{idx}].{k}"
                if k in _FEATURE_COLUMN_DEFINITION_KEYS:
                    if v is None:                # NULL graph definition (_column_defs) — no content
                        out[k] = v
                        continue
                    clean = _defn(v, path)
                    if clean is None:
                        return _refuse(path, "definition_scan")
                    out[k] = clean
                elif k in _FEATURE_COLUMN_PROSE_KEYS:
                    if v is None:                # absent taxonomy path — no content to scan
                        out[k] = v
                        continue
                    clean = _prose(v, path)
                    if clean is None:
                        # [A.55] DROP the key, do not refuse the catalog. These are FACETS —
                        # context ABOUT the column, beside the `definition` that IS its meaning —
                        # so this is the same argument `_ADVISORY_CONTEXT_KEYS` made for the
                        # catalog narrative: losing one degrades the payload and cannot falsify
                        # it, and the model is never told a facet exists. Refusing cost feature
                        # generation for all 237 columns over one column's `domain`.
                        #
                        # The safety property is UNCHANGED — the value that failed its scan never
                        # egresses. Only the blast radius narrows, and the drop is audited exactly
                        # like the advisory one, so it is degradation the record can name.
                        _drop_advisory(path)
                        continue
                    out[k] = clean
                elif k in _FEATURE_COLUMN_PROSE_LIST_KEYS:
                    cleaned = _prose_list(v, path)
                    if cleaned is None:
                        return _refuse(path, "prose_list_scan")
                    out[k] = cleaned
                elif k in _FEATURE_COLUMN_IDENTITY_KEYS:
                    if not _structural_ok(v):
                        return _refuse(path, "identity_shape")
                    out[k] = v
                elif k in _FEATURE_COLUMN_FACT_KEYS:
                    if not _fact_wrapper_ok(v):
                        return _refuse(path, "fact_shape")
                    out[k] = v
                elif k in _FEATURE_COLUMN_TOKEN_LIST_KEYS:
                    if not _token_list_ok(v):
                        return _refuse(path, "token_list_shape")
                    out[k] = v
                elif k in _FEATURE_COLUMN_OBJECT_KEYS:
                    if not _token_object_ok(v, _FEATURE_COLUMN_OBJECT_KEYS[k]):
                        return _refuse(path, "object_shape")
                    out[k] = v
                elif k in _FEATURE_COLUMN_MAPPING_KEYS:
                    if not _mapping_ok(v):
                        return _refuse(path, "mapping_shape")
                    out[k] = v
                elif k in _FEATURE_COLUMN_DICT_LIST_KEYS:
                    if not _dict_list_ok(v, _FEATURE_COLUMN_DICT_LIST_KEYS[k]):
                        return _refuse(path, "dict_list_shape")
                    out[k] = v
                else:
                    return _refuse(path, "unclassified_key")     # fail closed
            rebuilt.append(out)
        new_columns = rebuilt

    new_ctx = table_context
    if has_ctx:
        rebuilt_ctx: list = []
        for idx, block in enumerate(table_context):
            if not isinstance(block, dict):
                return _refuse(f"table_context[{idx}]", "block_not_a_dict")
            out = {}
            for k, v in block.items():
                path = f"table_context[{idx}].{k}"
                if k in _TABLE_CONTEXT_DEFINITION_KEYS:
                    if v is None:
                        out[k] = v
                        continue
                    clean = _defn(v, path)
                    if clean is None:
                        if k not in _ADVISORY_CONTEXT_KEYS:
                            return _refuse(path, "definition_scan")
                        _drop_advisory(path)
                        continue
                    out[k] = clean
                elif k in _TABLE_CONTEXT_PROSE_KEYS:
                    if v is None:                # unauthored label — no content to scan
                        out[k] = v
                        continue
                    clean = _prose(v, path)
                    if clean is None:
                        if k not in _ADVISORY_CONTEXT_KEYS:
                            return _refuse(path, "prose_scan")
                        _drop_advisory(path)
                        continue
                    out[k] = clean
                elif k in _TABLE_CONTEXT_PROSE_LIST_KEYS:
                    advisory = k in _ADVISORY_CONTEXT_KEYS
                    cleaned = _prose_list(v, path, droppable=advisory)
                    if cleaned is None:
                        return _refuse(path, "prose_list_scan")
                    if advisory and not cleaned:
                        # Scrubbed empty — a fabricated `[]` is not context, so drop the key.
                        _drop_advisory(path)
                        continue
                    out[k] = cleaned
                elif k in _TABLE_CONTEXT_IDENTITY_KEYS:
                    if not _structural_ok(v):
                        return _refuse(path, "identity_shape")
                    out[k] = v
                elif k in _TABLE_CONTEXT_LIST_KEYS:
                    cap = (_TABLE_ADVISORY_MAX_LEN if k == "advisories"
                           else _FEATURE_STRUCTURAL_MAX_LEN)
                    if not (isinstance(v, list) and len(v) <= _FEATURE_COLLECTION_MAX_ITEMS
                            and all(isinstance(x, str) and len(x) <= cap for x in v)):
                        return _refuse(path, "list_shape")
                    out[k] = v
                else:
                    return _refuse(path, "unclassified_key")
            rebuilt_ctx.append(out)
        new_ctx = rebuilt_ctx

    # `dropped` guards the passthrough (review round 2): a refused value that never ran a scrubber
    # body — a non-`str` under a prose/definition key — leaves `version` None, and returning the
    # ORIGINAL `metadata` would egress the very value the log line said was dropped.
    if version is None and not dropped:
        return metadata, [], [], None            # structural passthrough only — untouched
    safe = dict(metadata)
    safe["columns"] = new_columns
    if has_ctx:
        safe["table_context"] = new_ctx
    return safe, pii_spans, sample_audits, version


def _generation_settings() -> dict:
    """Generation settings for the audit record + idempotency key, read from the SAME env that
    configures the client (ClaudeConfig.from_env). So a real ClaudeLLM is audited as
    anthropic/<model> WITH the settings the adapter actually applies — model + max_tokens +
    thinking + effort (#24), which the adapter also treats as pinned — never just provider/model
    (and never the old hard-coded {"provider":"fake","model":"test"}, which made a production
    Claude call request model "test"). Defaults to fake/test with no provider set."""
    provider = os.environ.get("FEATUREGEN_LLM_PROVIDER", "fake")
    if provider == "anthropic":
        cfg = ClaudeConfig.from_env()
        return {"provider": "anthropic", "model": cfg.model, "max_tokens": cfg.max_tokens,
                "thinking": cfg.thinking, "effort": cfg.effort}
    return {"provider": "fake", "model": "test"}


def current_enrichment_generation_settings() -> dict:
    """Return the exact provider controls used by the audited structured-call seam."""
    return dict(_generation_settings())


def _audit_egress_block(conn, *, task: str, actor, reason: str) -> None:
    """A blocked egress is a security event (content was about to reach the LLM) — record it on the
    tamper-evident chain, not just the log (the redaction contract requires hard failures be audited).
    Best-effort: an audit failure (e.g. HMAC key unset) must never turn a safe block into a crash."""
    if actor is None:
        return
    try:
        record_security_event(conn, event_type="EGRESS_BLOCKED", actor=actor,
                              attempted_action=f"llm.{task}", decision="denied",
                              reason=reason or "egress guard blocked")
    except Exception:  # noqa: BLE001 — never let an audit failure mask the (correct) egress block
        logger.exception("failed to record EGRESS_BLOCKED security event for %s", task)


# #13 gap D: how many durable llm_call audit writes DEGRADED to the request connection in this
# request context. A ContextVar, not a module global: FastAPI runs each sync request in its own
# copied context, so counts can never bleed across concurrent requests. Incremented ONLY on the
# genuine degrade (DSN configured but the fresh connection failed) — the DSN-less harness writing
# on the request conn is the designed test path, not a degradation.
_AUDIT_DEGRADED: ContextVar[int] = ContextVar("overlay_llm_audit_degraded", default=0)


def consume_audit_degradations() -> int:
    """Return-and-reset the count of durable llm_call audit writes that degraded to the request
    connection since the last consume (#13 gap D). Ingest calls this at each enrichment stage
    boundary so the stage that carried the degraded call reports it in its detail."""
    n = _AUDIT_DEGRADED.get()
    if n:
        _AUDIT_DEGRADED.set(0)
    return n


def _record_llm_call_durable(conn, **record_kwargs) -> str:
    """Finding #20: by the time this runs, content has ALREADY egressed to the provider — so the
    immutable llm_call record must NOT share the upload transaction's fate (a later graph/DB
    failure in the same request would erase the evidence that data left the system). Mirror of the
    api.deps.audit_access_denied separate-connection pattern: gated on the production DSN being
    configured (unset ⟹ tests / no-DB harness, where a separately-committing connection would
    pollute the rolled-back test DB — same reasoning as that gate), and connecting with
    ``get_settings().dsn`` — the FULL configured DSN, exactly as audit_access_denied does. NOT
    ``conn.info.dsn``: psycopg3's ConnectionInfo.dsn strips the password, so in a password-auth
    deployment that connect fails and the record silently degrades to the request conn.

    The fresh connection performs ONE bare INSERT (record_llm_call) and NEVER takes an advisory
    lock — it cannot re-acquire the upload's ``pg_advisory_xact_lock`` and self-deadlock the way a
    second chain-locking security-audit connection did (program-audit I-3). NOT used for
    ``_audit_egress_block``: record_security_event's tamper-evident chain lock is exactly that
    hazard, so egress-block events stay on the request conn.

    Best-effort fallback: if the separate connection cannot be opened/committed, the record is
    written on the request conn — transactional evidence beats none — and the failure is logged.

    Returns the ``llm_call_ref`` of the row written (record_llm_call mints + returns it) so the
    caller can associate the logical call to its dispatches/run (C5-T4 ``link_llm_call``)."""
    dsn = get_settings().dsn
    if dsn:
        try:
            with psycopg.connect(dsn) as audit_conn:  # own tx, committed on `with` exit
                ref = record_llm_call(audit_conn, **record_kwargs)
            return ref
        except Exception:  # noqa: BLE001 — degraded audit must never fail the (done) provider call
            logger.exception(
                "durable llm_call audit write failed; falling back to the request connection")
            # #13 gap D: the degradation is COUNTED (per request context) so the enrichment stage
            # that carried this call can report audit_degraded instead of a log-only trace.
            _AUDIT_DEGRADED.set(_AUDIT_DEGRADED.get() + 1)
    return record_llm_call(conn, **record_kwargs)

# Structural output schemas for the three enrichment tasks (single string field each).
_SCHEMAS: dict[tuple[str, int], dict] = {
    ("overlay_concept", 1): {"type": "object", "additionalProperties": False,
                             "properties": {"concept": {"type": "string"}}, "required": ["concept"]},
    ("overlay_definition", 1): {"type": "object", "additionalProperties": False,
                                "properties": {"definition": {"type": "string"}},
                                "required": ["definition"]},
    ("overlay_domain", 1): {"type": "object", "additionalProperties": False,
                            "properties": {"domain": {"type": "string"}}, "required": ["domain"]},
    # E1a T4 — one comma-separated line of business synonyms/aliases for a column.
    ("overlay_synonyms", 1): {"type": "object", "additionalProperties": False,
                              "properties": {"synonyms": {"type": "string"}},
                              "required": ["synonyms"]},
    # The summary batch's per-item single FALLBACK shape (run_batched degrades non-ref-aware tasks
    # to `schema_id=f"overlay_{task}"`). Until Task 0.6 this pair was UNREGISTERED, so the fallback
    # dispatched with output_schema=None — structured output unenforced — exactly the trap the
    # fail-loud registry gate now refuses.
    ("overlay_summary", 1): {"type": "object", "additionalProperties": False,
                             "properties": {"summary": {"type": "string"}},
                             "required": ["summary"]},
    # E4a T2 — the MEASURE ANNOTATION a file did not declare. The flat (single / batch-fallback)
    # shape carries only the `unit`; `currency` rides the per-item batch schema below, exactly like
    # the domain task's two-level split. Both are EVIDENCE-only proposals: `_MEASURE_ANNOTATION`
    # excludes the LLM from display AND operational resolution, so neither can reach `graph_node`.
    ("overlay_unit", 1): {"type": "object", "additionalProperties": False,
                          "properties": {"unit": {"type": "string"}}, "required": ["unit"]},
    ("overlay_entity", 1): {"type": "object", "additionalProperties": False,
                            "properties": {"entity": {"type": "string"}}, "required": ["entity"]},
    ("overlay_contract", 1): {"type": "object", "additionalProperties": False,
                              "properties": {"definition": {"type": "string"}},
                              "required": ["definition"]},
    ("overlay_critique", 1): {"type": "object", "additionalProperties": False,
                              "properties": {"findings": {"type": "array",
                                                          "items": {"type": "string"}}},
                              "required": ["findings"]},
    # Batch array output-schemas (spec C18). NO `minItems`/`maxItems` on ANY array: the Anthropic
    # structured-output API rejects array `maxItems` with HTTP 400 ("For 'array' type, property
    # 'maxItems' is not supported"), which would fail EVERY batch call closed. The real per-batch
    # count cap is code-enforced by `validate_batch_results` (extra/duplicate refs against the
    # REQUESTED ref-set), and the input side is bounded by `chunk_items` / `_MAX_COLUMN_PROFILES`, so
    # dropping the schema cap loses no real bound. One {ref, <out_key>} object per requested item.
    ("overlay_concept_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "concept": {"type": "string", "maxLength": 128}},
                      "required": ["ref", "concept"]}}},
        "required": ["results"]},
    # `definition` raised 500 -> 32_000 (2026-08-06), PAIRED with `enrich.draft_definitions`'
    # `_accept_bounded(MAX_DEFINITION_LEN)`. This is the INBOUND half the first raise missed:
    # `MAX_DEFINITION_LEN` widened only what we SEND, while a drafted definition over 500 chars was
    # still refused coming back — below even the original 600. Every schema bound in this block is
    # equal-by-test to its code-side accept gate (`test_enrich_output_bounds.py`), and they must
    # move together: the schema rejects the WHOLE CHUNK where the code gate drops one item.
    ("overlay_definition_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "definition": {"type": "string", "maxLength": 32_000}},
                      "required": ["ref", "definition"]}}},
        "required": ["results"]},
    # One plain-English sentence per column, for a human reading the catalog. Raised 400 -> 1000
    # (2026-08-06): 400 REJECTED a legitimately long sentence outright rather than shortening it.
    # 1000 is `_MAX_LEN_DEFAULT`, the per-value egress cap `ai_summary` is graded under when it is
    # re-threaded — accepting above it would mint a value its own egress gate then EXCLUDES.
    # The CODE-side `_accept_bounded` is the per-item gate (a schema maxLength failure would reject
    # the WHOLE chunk over one long answer); the two are pinned equal by test.
    ("overlay_summary_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "summary": {"type": "string", "maxLength": 1000}},
                      "required": ["ref", "summary"]}}},
        "required": ["results"]},
    ("overlay_synonyms_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      # Raised 200 -> 1000 (2026-08-06) to REJOIN `enrich._MAX_SYNONYMS_LEN`, which
                      # had already gone 200 -> 1000. The pair had silently split: the code gate
                      # accepted 1000 while this schema still failed the whole chunk at 201.
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "synonyms": {"type": "string", "maxLength": 1000}},
                      "required": ["ref", "synonyms"]}}},
        "required": ["results"]},
    # E4a T2 — one measure annotation per column: the `unit` (required — it is the whole question)
    # plus an OPTIONAL ISO-4217 `currency` (absent from `required`, so a non-monetary measure like a
    # count or a percentage returns a unit alone rather than inventing a currency for it). Bounded
    # short tokens, never prose; the closed shape is enforced CODE-side in `enrich._accept_unit` /
    # `_accept_currency` (a schema enum would fail the WHOLE chunk on one off-vocabulary value).
    ("overlay_unit_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      # `unit` raised 32 -> 64 (2026-08-06) to REJOIN `enrich._MAX_UNIT_LEN`, which
                      # had already gone 32 -> 64; the pair had silently split the same way
                      # `synonyms` did. `currency` stays 8: ISO-4217 is exactly three ASCII letters
                      # and `_CURRENCY_CODE_RE` enforces that, so 8 is already slack, not a bound.
                      "properties": {"ref": {"type": "string", "maxLength": 128},
                                     "unit": {"type": "string", "maxLength": 64},
                                     "currency": {"type": "string", "maxLength": 8}},
                      "required": ["ref", "unit"]}}},
        "required": ["results"]},
    # E1a T3 — the domain result is TWO-LEVEL: `domain` is the TABLE's default (the context all of
    # its columns inherit) and `column_domains` lists ONLY the columns whose domain DIFFERS from it.
    # An ARRAY of closed {column, domain} objects, never a free-key map: a map with dynamic keys
    # cannot be expressed with `additionalProperties: false`, and an open object fails the provider's
    # structured-output contract. Additive + OPTIONAL (absent from `required`) so a result carrying
    # only the table domain — the flat single/fallback seam's shape, and the normal answer when no
    # column differs — stays valid.
    ("overlay_domain_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 256},
                                     "domain": {"type": "string", "maxLength": 64},
                                     "column_domains": {"type": "array",
                                         "items": {"type": "object", "additionalProperties": False,
                                                   "properties": {
                                                       "column": {"type": "string",
                                                                  "maxLength": 128},
                                                       "domain": {"type": "string",
                                                                  "maxLength": 64}},
                                                   "required": ["column", "domain"]}}},
                      "required": ["ref", "domain"]}}},
        "required": ["results"]},
    # D13.2 — the domain task's v2 body: a REAL new schema, not an alias. It adds the per-column
    # `column_sub_domains` array beside the existing table-first `domain` / `column_domains` shape,
    # so the FINER axis is produced by the SAME call (no second LLM request, D13.2's explicit rule).
    #
    # Two separate arrays on purpose. `column_domains` lists ONLY the columns whose COARSE domain
    # differs from the table default (an override — inheritance is never fabricated as evidence);
    # `column_sub_domains` may name ANY column, because a sub-domain is additive: it refines a
    # domain the column still inherits. Collapsing them into one array would force a model that
    # wants to say "this BIC is Correspondent Banking under the table's Compliance domain" to also
    # restate Compliance as an override, which the accept gate would then drop as equal-to-default.
    #
    # Bounded strings, never enums: `reg.validate` rejects the WHOLE response on one schema
    # violation, so an off-shape sub-domain must not be able to lose a valid table domain with it
    # (the [F1] per-field-salvage rule the Pass-B schemas already follow).
    ("overlay_domain_batch", 2): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {"ref": {"type": "string", "maxLength": 256},
                                     "domain": {"type": "string", "maxLength": 64},
                                     "column_domains": {"type": "array",
                                         "items": {"type": "object", "additionalProperties": False,
                                                   "properties": {
                                                       "column": {"type": "string",
                                                                  "maxLength": 128},
                                                       "domain": {"type": "string",
                                                                  "maxLength": 64}},
                                                   "required": ["column", "domain"]}},
                                     "column_sub_domains": {"type": "array",
                                         "items": {"type": "object", "additionalProperties": False,
                                                   "properties": {
                                                       "column": {"type": "string",
                                                                  "maxLength": 128},
                                                       "sub_domain": {"type": "string",
                                                                      "maxLength": 64}},
                                                   "required": ["column", "sub_domain"]}}},
                      "required": ["ref", "domain"]}}},
        "required": ["results"]},
    # The FLAT domain seam at v2 — a DELIBERATE byte-alias of v1, and honestly so: the flat
    # single/fallback shape carries only the table domain, gains no field at v2, and exists only so
    # a v2 batch that degrades to per-item fallback resolves a REGISTERED (id, version) pair instead
    # of dispatching unenforced (the `schema_for(id, N+1) -> None` trap D10 closes). The alias rule
    # D10 bans is aliasing a schema that must ADMIT NEW FIELDS; nothing new rides this one.
    ("overlay_domain", 2): {"type": "object", "additionalProperties": False,
                            "properties": {"domain": {"type": "string"}}, "required": ["domain"]},
    # Suggestion-discovery Task 1 — the CLOSED output schema of the bounded taxonomy-proposal
    # batch (template_discovery.run_discovery_proposal_batch). One {ref, proposal} object per
    # requested template; every proposal field is required so an abstention is EXPLICIT (the
    # "abstain" token / empty lists), never an absent key. Controlled-ID membership (feature
    # categories, selectable use-case leaves) is enforced CODE-side by the accept callback —
    # never a schema enum, which would fail the whole chunk on one off-vocabulary value.
    ("overlay_discovery_proposal_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                      "properties": {
                          "ref": {"type": "string", "maxLength": 128},
                          "proposal": {
                              "type": "object", "additionalProperties": False,
                              "properties": {
                                  "feature_category": {"type": "string", "maxLength": 64},
                                  "use_case_ids": {"type": "array",
                                                   "items": {"type": "string",
                                                             "maxLength": 128}},
                                  "business_value": {"type": "string", "maxLength": 400},
                                  "keywords": {"type": "array",
                                               "items": {"type": "string",
                                                         "maxLength": 40}}},
                              "required": ["feature_category", "use_case_ids",
                                           "business_value", "keywords"]}},
                      "required": ["ref", "proposal"]}}},
        "required": ["results"]},
    # Table-synthesis (Pass B) output schemas. `_batch` is an array of per-item {ref, synthesis}
    # objects (batch harness treats `synthesis` as one structured out-key); the flat sibling is the
    # `_single_fallback` shape. [F1] per-field salvage: `as_of_basis`, `table_role`, and
    # `event_or_snapshot` are BOUNDED STRINGS here, never schema enums — `reg.validate` validates
    # the WHOLE response envelope BEFORE the ref-aware accept, so a strict enum would fail the
    # ENTIRE synthesis on one case-variant/off-vocab value (losing a valid grain with it). The
    # closed vocabularies are enumerated in the PROMPT and enforced CODE-SIDE per field in
    # `table_synth.make_ref_accept` (strip/lower normalization; an off-vocab value drops THAT FIELD
    # ONLY, with `basis_not_allowed` / `role_off_vocab` / `event_or_snapshot_off_vocab` reason
    # codes). `event_time_plus_lag` remains intentionally OUTSIDE the accepted as_of_basis vocab
    # (`_VALID_BASIS`): FACT_VALUE_SCHEMAS mandates a `lag_hours` when basis == event_time_plus_lag
    # (facts.py), and Pass B has no way to infer a lag, so such a proposal would always be denied by
    # validate_fact_value. Phase 2 accepts only the two lag-free bases; adding event_time_plus_lag
    # would require a lag_hours field end-to-end (out of scope).
    ("overlay_table_synth_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string", "maxLength": 256},
                    "synthesis": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "grain_columns": {"type": "array",
                                              "items": {"type": "string", "maxLength": 128}},
                            "as_of_column": {"type": ["string", "null"], "maxLength": 128},
                            "as_of_basis": {"type": ["string", "null"], "maxLength": 64},
                            "primary_entity": {"type": ["string", "null"], "maxLength": 128},
                            "table_role": {"type": ["string", "null"], "maxLength": 64},
                            "event_or_snapshot": {"type": ["string", "null"], "maxLength": 64},
                        }, "required": ["grain_columns"]}},
                "required": ["ref", "synthesis"]}}},
        "required": ["results"]},
    # Pass B v3 (profile Task 4) — a REAL new body, not an alias. v2 is a byte-alias of v1 with
    # `additionalProperties: false` at every level, so it REJECTS every field below; adding them
    # under v2 would fail the whole synthesis closed. The four new suggestions sit BESIDE the
    # existing role/entity/event-snapshot fields (same call, same item, no second pass), and each
    # carries its citations in `evidence_refs`.
    #
    # `evidence_refs` is an ARRAY of closed `{field, refs}` objects, never a free-key map: a map
    # with dynamic keys cannot be expressed under `additionalProperties: false`, and an open object
    # fails the provider's structured-output contract (the same reason `column_domains` is an array).
    #
    # Every value is a BOUNDED STRING, never a schema enum — [F1] per-field salvage: `reg.validate`
    # rejects the WHOLE response on one schema violation, so an off-vocabulary `authority_role`
    # must not be able to take a valid grain down with it. The closed vocabularies are enumerated in
    # the PROMPT and enforced CODE-side per field in `table_synth.make_ref_accept`.
    ("overlay_table_synth_batch", 3): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string", "maxLength": 256},
                    "synthesis": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "grain_columns": {"type": "array",
                                              "items": {"type": "string", "maxLength": 128}},
                            "as_of_column": {"type": ["string", "null"], "maxLength": 128},
                            "as_of_basis": {"type": ["string", "null"], "maxLength": 64},
                            "primary_entity": {"type": ["string", "null"], "maxLength": 128},
                            "table_role": {"type": ["string", "null"], "maxLength": 64},
                            "event_or_snapshot": {"type": ["string", "null"], "maxLength": 64},
                            "table_description": {"type": ["string", "null"], "maxLength": 600},
                            "business_context": {"type": ["string", "null"], "maxLength": 600},
                            "authority_role": {"type": ["string", "null"], "maxLength": 64},
                            "temporal_storage_model": {"type": ["string", "null"],
                                                       "maxLength": 64},
                            "evidence_refs": {"type": "array",
                                "items": {"type": "object", "additionalProperties": False,
                                          "properties": {
                                              "field": {"type": "string", "maxLength": 64},
                                              "refs": {"type": "array",
                                                       "items": {"type": "string",
                                                                 "maxLength": 128}}},
                                          "required": ["field", "refs"]}},
                        }, "required": ["grain_columns"]}},
                "required": ["ref", "synthesis"]}}},
        "required": ["results"]},
    ("overlay_table_synth", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            "grain_columns": {"type": "array",
                              "items": {"type": "string", "maxLength": 128}},
            "as_of_column": {"type": ["string", "null"], "maxLength": 128},
            "as_of_basis": {"type": ["string", "null"], "maxLength": 64},
            "primary_entity": {"type": ["string", "null"], "maxLength": 128},
            "table_role": {"type": ["string", "null"], "maxLength": 64},
            "event_or_snapshot": {"type": ["string", "null"], "maxLength": 64},
        }, "required": ["grain_columns"]},
    # Table-synthesis PHASE 1 (#1 — wide tables): per-column-CHUNK summary. NO fact output — a compact
    # digest of one <=_MAX_COLUMN_PROFILES chunk (candidate grain/id + temporal/as-of columns, entity
    # signals, event/snapshot hint) that later feeds the SINGLE per-table synthesis (phase 2) alongside
    # a complete roster, so a table wider than the egress cap is never sent as one giant profile list.
    # Batch-only (ref_aware, no single seam), so only the `_batch` shape exists.
    ("overlay_table_synth_summary_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array",
            "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string", "maxLength": 256},
                    "summary": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "grain_candidates": {"type": "array",
                                                 "items": {"type": "string", "maxLength": 128}},
                            "temporal_candidates": {"type": "array",
                                                    "items": {"type": "string", "maxLength": 128}},
                            "entity_signals": {"type": "array",
                                               "items": {"type": "string", "maxLength": 128}},
                            "event_or_snapshot": {"type": ["string", "null"], "maxLength": 64},
                        }, "required": []}},
                "required": ["ref", "summary"]}}},
        "required": ["results"]},
    # Feature-assist output schemas (M6 — routed through the audited seam). Permissive object shapes:
    # the value is the LLM's proposal that the deterministic layer then grounds/validates.
    # The feature ITEM shape is DECLARED and `derives_from` is REQUIRED **on the wire** — merely
    # declaring it (no requirement) let Opus omit the column refs / return an empty array on every idea,
    # so all candidates were rejected UNGROUNDED (verified by capturing the raw output: derives_from=[]
    # for 29/29 ideas). ``x-wire-required`` is a wire-ONLY hint: schema_projection converts it to
    # ``required`` in the closed schema sent to Anthropic (forcing the model to fill the key), while the
    # CANONICAL here stays permissive so RESPONSE validation is lenient — a single incomplete item must
    # not fail the whole response; the deterministic ``_vet`` gauntlet filters per-item. A verbose
    # `description` tells the model to copy the exact object_ref from the columns menu; ``_validate_idea``
    # also resolves a bare column name to its object_ref so the model's ref FORMAT can't un-ground a
    # feature. (Array ``minItems`` can't help — Anthropic rejects it, so schema_projection strips it.)
    ("feature_ideas", 1): {
        "type": "object", "additionalProperties": True,
        "properties": {"features": {"type": "array", "items": {
            "type": "object", "additionalProperties": True,
            "properties": {"name": {"type": "string"},
                           "derives_from": {"type": "array", "items": {"type": "string"},
                                            "description": "REQUIRED, non-empty. The source column(s) "
                                            "this feature is computed from — copy the EXACT object_ref "
                                            "string(s) from the provided columns menu (format "
                                            "public.<table>.<column>). A feature with no source "
                                            "columns cannot be grounded and is discarded."},
                           "aggregation": {"type": "string"},
                           "grain_table": {"type": "string"},
                           "description": {"type": "string"},
                           "rationale": {"type": "string"}},
            "x-wire-required": ["name", "derives_from", "aggregation"]}}}},
    # Left permissive (like on main): the recipe (NL-query) path is NOT the considered-set flow, and
    # declaring typed properties here rejects a fake's null optional fields (grain/join/as_of) in
    # response validation. Its wire schema stays open — a pre-existing limit, not this fix's scope.
    ("feature_recipe", 1): {"type": "object", "additionalProperties": True},
    ("leakage", 1): {"type": "object", "additionalProperties": True,
                     "properties": {"leaks": {"type": "array"}}},
    # Properties declared (the keys `recommend_feature_sets`'s SetRecommendation reads) so the wire
    # projection can CLOSE the object — Anthropic rejects an open object. Canonical stays permissive.
    ("feature_set_rec", 1): {"type": "object", "additionalProperties": True,
                             "properties": {"recommended_lens": {"type": "string"},
                                            "reasoning": {"type": "string"}}},
    # LLM-2 candidate critic (SP-12 item 5): {"issues": [{"name","issue"}]} — advisory quality/fit notes.
    ("feature_candidate_critique", 1): {"type": "object", "additionalProperties": True,
                                        "properties": {"issues": {"type": "array"}}},
    # Intent-recognition (Phase-1A): the closed-shape recognition body. Structure only — the closed-
    # taxonomy semantics (id in registry, primary is a leaf) are a post-pass in recognizer.recognize.
    ("use_case_recognition", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            "status": {"type": "string",
                       "enum": ["classified", "ambiguous", "unscoped", "technical_failure"]},
            "candidates": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "use_case_id": {"type": "string"},
                    "relationship": {"type": "string", "enum": ["primary", "secondary"]},
                    "confidence": {"type": "string", "enum": ["high", "medium", "low"]},
                    "evidence_spans": {"type": "array", "items": {"type": "string"}},
                    "rationale": {"type": "string"}},
                "required": ["use_case_id", "relationship", "confidence", "evidence_spans",
                             "rationale"]}},
            # Phase-2B optional intent dimensions. STRUCTURAL only (array-of-string / string|null) —
            # the closed-vocabulary semantics are a per-dimension, non-fatal post-pass
            # (recognition.normalize_dimensions), so a value outside the vocab never fails the call.
            "modelling_contexts": {"type": "array", "items": {"type": "string"}},
            "target_entity": {"type": ["string", "null"]},
            "ambiguity_note": {"type": ["string", "null"]}},
        "required": ["status", "candidates"]},
    # Identifier-link semantic critic. Namespace and population are deliberately separate outputs:
    # neither field carries uniqueness/cardinality/operational eligibility, which remain deterministic.
    ("bridge_identifier_critique", 1): {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "namespace_recommendation": {
                "type": "string",
                "enum": ["same", "different", "possible", "unknown"],
            },
            "population_hypothesis": {
                "type": "string",
                "enum": [
                    "same",
                    "left_subset",
                    "right_subset",
                    "partial_overlap",
                    "disjoint",
                    "unknown",
                ],
            },
            "namespace_reason_codes": {
                "type": "array",
                "items": {"type": "string"},
            },
            "population_reason_codes": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": [
            "namespace_recommendation",
            "population_hypothesis",
            "namespace_reason_codes",
            "population_reason_codes",
        ],
    },
    # Enrichment concept critic (ingestion-richness Task 2). Refute-oriented: the model answers
    # whether an ALREADY-PROPOSED concept assignment is supported by the supplied metadata evidence
    # — a closed verdict, never a replacement concept (the revise pass below owns that), so an
    # off-vocabulary answer cannot ride this channel.
    # Task 2b: the tie-break adjudicator's closed output — a RANKING of the tied refs (validated
    # code-side as an exact permutation of the tied set; the schema cannot express that) plus a
    # bounded rationale. NO maxItems: the Anthropic structured-output API rejects it (HTTP 400) and
    # the permutation check is the real bound.
    # Intake build (#2 spec): the mandatory-read ticket. `target_ref` must be ∈ the sent
    # candidates or "" (validated code-side); `target_window_days` 0 ⟹ not stated (mapped to None
    # code-side — the schema cannot express nullable cleanly across providers).
    ("intake_ticket", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            "target_ref": {"type": "string", "maxLength": 512},
            "target_window_days": {"type": "integer", "minimum": 0},
            "target_type": {"type": "string",
                            "enum": ["binary_classification", "regression", "multiclass",
                                     "abstain"]},
            "business_domain": {"type": "array", "items": {"type": "string", "maxLength": 64}},
            "confidence": {"type": "string", "enum": ["high", "medium", "abstain"]},
        },
        "required": ["target_ref", "target_window_days", "target_type", "business_domain",
                     "confidence"]},
    # Task 4b — hypothesis-chosen recipe parameters. A flat triple list (template_id, param,
    # value-as-string): nested per-template objects would need open additionalProperties, which
    # this seam forbids. Every triple is re-validated code-side against the AUTHORED tuples — an
    # off-menu answer is dropped, and an omitted param means the safe default. NO maxItems (the
    # Anthropic structured-output API rejects it); the menu size is the real bound.
    ("param_choice", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            "choices": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "properties": {
                    "template_id": {"type": "string", "maxLength": 128},
                    "param": {"type": "string", "maxLength": 64},
                    "value": {"type": "string", "maxLength": 64},
                },
                "required": ["template_id", "param", "value"]}},
        },
        "required": ["choices"]},
    # Task 3 — the near-label critic's closed verdict vocabulary. Deliberately NO token that reads
    # as "cleared" (no_finding ≠ clearance); the enum is re-validated code-side and an off-enum
    # answer degrades to abstain.
    ("near_label_verdict", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            "verdict": {"type": "string", "enum": ["no_finding", "too_close", "abstain"]},
            "rationale": {"type": "string", "maxLength": 600},
        },
        "required": ["verdict", "rationale"]},
    ("overlay_tie_break", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            "ranking": {"type": "array", "items": {"type": "string", "maxLength": 512}},
            "rationale": {"type": "string", "maxLength": 1000},
        },
        "required": ["ranking", "rationale"]},
    ("concept_critique", 1): {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "verdict": {
                "type": "string",
                "enum": ["supported", "refuted", "uncertain"],
            },
            "reason_codes": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["verdict", "reason_codes"],
    },
    # Targeted semantic adjudication (semantic Task 5). A REAL new body, registered here BEFORE any
    # caller may request it (D10 — `_require_schema` raises on an unregistered requested version, so
    # a missing body is a loud dispatch refusal, never an unenforced structured call).
    #
    # The shape is the `SemanticAdjudicationV2` contract, closed at every level:
    # `selected_concept` is one name (a registry member or the literal `unclassified` — the code
    # side validates membership, since a 280-member enum would fail the WHOLE call on one stale
    # name); `alternatives` is capped in code at three (no array `maxItems` — the provider rejects
    # it, the same constraint every batch schema above documents); `confidence_band` IS an enum
    # because its three values are stable by definition and an off-vocabulary band must not be
    # silently interpretable. `reason_codes`/`missing_context` are bounded strings gated code-side
    # against the closed `semantic_context` vocabularies. `ontology_gap` is OPTIONAL (absent from
    # `required`) — a gap is the rare answer, and requiring it would manufacture one per call.
    ("semantic_adjudication", 1): {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "selected_concept": {"type": "string", "maxLength": 128},
            "alternatives": {"type": "array", "items": {"type": "string", "maxLength": 128}},
            "confidence_band": {"type": "string", "enum": ["high", "medium", "low"]},
            "reason_codes": {"type": "array", "items": {"type": "string", "maxLength": 64}},
            "missing_context": {"type": "array", "items": {"type": "string", "maxLength": 64}},
            "ontology_gap": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "proposed_label": {"type": "string", "maxLength": 64},
                    "parent_concept": {"type": "string", "maxLength": 64},
                    "definition": {"type": "string", "maxLength": 400},
                    "aliases": {"type": "array", "items": {"type": "string", "maxLength": 64}},
                },
                "required": ["proposed_label", "definition"],
            },
        },
        "required": ["selected_concept", "alternatives", "confidence_band", "reason_codes",
                     "missing_context"],
    },
    # Dataset-profile critic (profile Task 4). PROPOSAL-BLIND: the model returns its OWN closed
    # classification of the table evidence — never a verdict ON a proposal, which is what produces
    # agreement bias for an operational classification. The comparison happens code-side, outside
    # the prompt. `unknown` is a first-class member of the enum so an honest abstention is
    # expressible on the wire rather than smuggled in as a low-confidence guess.
    ("dataset_profile_critique", 1): {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "classification": {"type": "string", "maxLength": 64},
            "reason_codes": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["classification"],
    },
    # The critic's one revise pass: a single concept name from the provided controlled vocabulary
    # (or the literal 'unclassified'). Free-form on the wire by necessity — the vocabulary is data,
    # not schema — so the code-side registry + shape gate is what makes an off-registry or
    # shape-conflicted revision unemittable.
    ("concept_revision", 1): {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "concept": {"type": "string", "maxLength": 64},
            "reason_codes": {
                "type": "array",
                "items": {"type": "string"},
            },
        },
        "required": ["concept", "reason_codes"],
    },
    # Objective expansion (feature-gen Task 6d). The question is ten words and the catalog is
    # hundreds of columns, so the relevance intersection is widened by expanding the SMALL side:
    # one tiny call returns the related business vocabulary ("obligor" for "counterparty"), the
    # match afterwards stays a deterministic set intersection, and an identical question replays
    # from the 1039 store rather than re-billing.
    #
    # BOTH BOUNDS ARE CODE-ONLY (`feature_assist._MAX_EXPANSION_TERMS` = 40 and
    # `_MAX_EXPANSION_TERM_LEN` = 64), and this schema deliberately carries NEITHER. The task brief
    # asked for `maxItems: 40` and `items.maxLength: 64` here; both were tried and both are wrong,
    # for two different reasons that happen to point the same way:
    #
    #   * `maxItems` may not appear in ANY schema in this dict — the Anthropic structured-output
    #     API rejects it with HTTP 400, pinned repo-wide by
    #     `test_enrich_llm.test_no_output_schema_carries_array_minitems_or_maxitems`.
    #   * `maxLength` is legal but WRONG HERE, and the reason is `feature_ideas`' grounding array's
    #     reason verbatim: it is stripped from the wire by `project_for_anthropic` (so the model is
    #     never TOLD 64) yet still validated against the RESPONSE — so it fires FIRST, before the
    #     per-entry code gate, and one 65-char term destroys the whole expansion. The terms in this
    #     list are siblings exactly as grounding entries are, and the cost is worse than a lost
    #     answer: a failed call caches NOTHING, so a question whose answer reliably contains one
    #     long term would re-bill on every request forever. The code gate drops that ONE term,
    #     keeps its 39 siblings, and caches. `test_enrich_output_bounds.py` pins the absence.
    #
    # STRUCTURE is a different matter and IS validated: `items: {"type": "string"}` survives the
    # projection, so the model is told it and a null/number entry legitimately fails the response —
    # the same bargain `derives_from` already takes.
    ("objective_expansion", 1): {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "terms": {
                "type": "array",
                "description": "Related business terms for the analyst's objective — synonyms and "
                               "industry vocabulary naming the same ideas. Single words or short "
                               "noun phrases only.",
                "items": {"type": "string"},
            },
        },
        "required": ["terms"],
    },
}

# Pass B v2 (Phase-2 Slice 1, Task 4): the OUTPUT contract is byte-for-byte v1 — v2 exists because
# the INPUT item contract changed (dual `operational_type`/`declared_type` column profiles,
# STRUCTURED `column_roster` entries `{column, operational_type, declared_type}`, and the fenced
# `table_definition`), and the prompt/schema version stamped on the immutable llm_call record must
# identify WHICH item contract egressed. Same body objects as v1 (an intentional alias, not a copy
# — the two versions cannot drift), registered as real v2 rows via `register_enrichment_schemas`'s
# `_SCHEMAS` sweep so the Task-1 version seam (`schema_for(schema_id, 2)`) resolves them.
for _synth_schema_id in ("overlay_table_synth_batch", "overlay_table_synth",
                         "overlay_table_synth_summary_batch"):
    _SCHEMAS[(_synth_schema_id, 2)] = _SCHEMAS[(_synth_schema_id, 1)]

# Pass B v3 (profile Task 4) stamps ONE contract generation across the whole run, so the two
# companion schema ids follow the batch body's version even though their own shapes are unchanged:
# the flat single-fallback shape carries no `synthesis` wrapper at all (a ref_aware task has no
# single seam — `_single_fallback` returns MISSING) and the phase-1 chunk SUMMARY emits no
# suggestions, so neither gains a field. They are registered at v3 so a request that stamps v3
# resolves, instead of hitting the `schema_for(id, 3) -> None` trap D10 closes.
for _synth_alias_id in ("overlay_table_synth", "overlay_table_synth_summary_batch"):
    _SCHEMAS[(_synth_alias_id, 3)] = _SCHEMAS[(_synth_alias_id, 1)]

# Feature-assist v2 (Phase-2 Slice 3A-iv, spec §8): the OUTPUT contract is byte-for-byte v1 — the
# permissive `additionalProperties: True` shape already admits the new proposal fields, and semantic
# validation stays CODE-SIDE in `_validate_idea`. v2 exists only so the immutable `llm_call` record
# stamps WHICH input contract egressed (the widened menu / tri-state shape) instead of a hardcoded 1
# masked by a `…_v1` prompt_id string. Registered as real v2 rows via `register_enrichment_schemas`'s
# `_SCHEMAS` sweep so `schema_for(schema_id, 2)` resolves them; an intentional alias, never a copy.
# v3 = v2 plus `ai_summary` on the column descriptor. The OUTPUT schema is unchanged — the version
# identifies the INPUT contract — so the alias must extend to every version the request may stamp.
# Registering 2 but requesting 3 makes `schema_for(id, 3)` return None, structured output
# unenforced, and the response fail repair: feature generation returns NOTHING with the flag on.
# v4 = the SemanticContextBundleV1 feature-generation contract (semantic Task 8): concept ancestry,
# identifier namespace/issuer, party role, the D2 (producer, strength) axes, current links and the
# closed missing-context codes beside the v3 material. The OUTPUT schema is still unchanged — the
# version identifies the INPUT contract — but D10 makes registration a PRECONDITION, not a
# follow-up: `_feature_schema_version()` may not return 4 until this loop has run, or
# `_require_schema` refuses the dispatch and feature generation returns nothing with the flag on.
# v5 (Task 6c) is the FIRST feature-gen version whose OUTPUT contract differs: every proposed
# feature returns `grounding` — its own account of which offered column it used, in what role, and
# on what evidence. Everything the request may ALSO stamp (recipe / leakage / set-rec) keeps the
# v1 alias, because only `feature_ideas` gained a field and `_feature_schema_version()` stamps one
# number across all four.
for _feature_schema_id in ("feature_ideas", "feature_recipe", "leakage", "feature_set_rec"):
    _SCHEMAS[(_feature_schema_id, 2)] = _SCHEMAS[(_feature_schema_id, 1)]
    _SCHEMAS[(_feature_schema_id, 3)] = _SCHEMAS[(_feature_schema_id, 1)]
    _SCHEMAS[(_feature_schema_id, 4)] = _SCHEMAS[(_feature_schema_id, 1)]
    if _feature_schema_id != "feature_ideas":
        _SCHEMAS[(_feature_schema_id, 5)] = _SCHEMAS[(_feature_schema_id, 1)]

#: The v5 `grounding` array: WHY this feature, in the model's own words, against columns that
#: really exist. EXPLANATORY ONLY — `feature_assist` reads it for display and pins that nothing
#: branches on its content.
#:
#: WHAT IS *NOT* HERE IS THE POINT. `maxItems`, `maxLength` and `minItems` are stripped from the
#: wire by `project_for_anthropic` but STILL validated against the response, so a bound here would
#: fail the WHOLE call for one long clause the model was never told to shorten — the failure mode
#: `test_enrich_output_bounds` exists to describe. `required` and `enum` are carried WIRE-ONLY
#: (`x-wire-required` / `x-wire-enum`) for the same reason the feature item itself is: the canonical
#: stays permissive so one malformed entry cannot take its siblings' good answers with it, and the
#: deterministic gauntlet filters per entry. Every bound that matters lives in
#: `feature_assist._ground_notes`, where it drops ONE entry instead of one call.
_FEATURE_GROUNDING_SCHEMA: dict = {
    "type": "array",
    "description": "REQUIRED. One entry per column from the provided columns menu that you actually "
                   "used in this feature — what it contributed and the evidence you relied on.",
    "items": {
        "type": "object",
        # Open on the canonical (the projection closes it on the wire) — exactly as the feature
        # item above is, and for the same leniency reason.
        "additionalProperties": True,
        "properties": {
            "column": {"type": "string",
                       "description": "the EXACT object_ref from the provided columns menu "
                                      "(format public.<table>.<column>). A column that was never "
                                      "offered is not a valid answer, and naming one discards the "
                                      "whole feature."},
            "role": {"type": "string",
                     "x-wire-enum": ["measure", "grain", "time_anchor", "filter", "currency",
                                     "dimension"],
                     "description": "what this column contributes to the feature."},
            "why": {"type": "string",
                    "description": "ONE short clause naming the evidence you relied on."},
        },
        "x-wire-required": ["column", "role", "why"],
    },
}


def _feature_ideas_with_grounding() -> dict:
    """v5 = the v1 body plus one key. DERIVED from v1 rather than restated so the two cannot drift:
    every earlier version is an alias of v1, so a v1 edit that v5 did not inherit would silently
    split the contract in half."""
    import copy

    body = copy.deepcopy(_SCHEMAS[("feature_ideas", 1)])
    item = body["properties"]["features"]["items"]
    item["properties"]["grounding"] = copy.deepcopy(_FEATURE_GROUNDING_SCHEMA)
    # Wire-required for the reason `derives_from` is: measured on Opus, a merely-DECLARED key on a
    # nested array item is silently omitted. An absent array is still ACCEPTED by the code (honest
    # absence, never a call failure) — this asks, it does not enforce.
    item["x-wire-required"] = [*item["x-wire-required"], "grounding"]
    return body


_SCHEMAS[("feature_ideas", 5)] = _feature_ideas_with_grounding()

# Fallback service identity for when no real actor is threaded in. authenticated=False — a
# fabricated authenticated identity is forbidden outside sanctioned auth modules; production threads
# the real (authenticated) upload actor from ingest.
_ENRICH_ACTOR = IdentityEnvelope(
    subject="featuregen-overlay-enrichment", actor_kind="service",
    authenticated=False, auth_method="internal", role_claims=())


class SchemaUnregisteredError(ValueError):
    """A REQUESTED ``(schema_id, schema_version)`` pair does not resolve even after the owner's
    registration hook ran (D10). A ``ValueError`` subclass so existing raise-at-dispatch contracts
    hold, but TYPED so a containment site (the batch ladder's single fallback) can catch exactly
    this registration bug without swallowing any other failure."""


def _require_schema(conn, reg: DocumentSchemaRegistry, schema_id: str, schema_version: int,
                    register: Callable[..., None] | None = None) -> dict:
    """Resolve the REQUESTED output schema or RAISE (D10) — a call must never dispatch unenforced.

    Self-registers the owner's schema set on first use (idempotent; ``register`` defaults to the
    enrichment set, a non-enrichment owner passes its own hook) so a fresh database never fails a
    real provider for lack of a schema; but a pair STILL unresolved after that is a caller bug (a
    version bumped without registering its body — the ``schema_for(id, N+1) -> None`` trap), and
    proceeding would send the request with ``output_schema=None``: structured output unenforced and
    the response failing repair downstream. Fail loudly, naming the pair."""
    schema = reg.schema_for(schema_id, schema_version)
    if schema is None:
        (register or register_enrichment_schemas)(conn)
        schema = reg.schema_for(schema_id, schema_version)
    if schema is None:
        raise SchemaUnregisteredError(
            f"output schema ({schema_id!r}, v{schema_version}) is not registered — refusing to "
            "dispatch an unenforced structured call; register the schema version before "
            "requesting it")
    return schema


def register_enrichment_schemas(conn) -> None:
    """Register the enrichment output-schemas so the audited call can resolve/validate them.
    Idempotent (register_schema upserts). Called at overlay bootstrap.

    Fail-closed provider-compat guard (Phase-1 hardening): before touching the DB, assert every
    canonical schema PROJECTS to an Anthropic-compatible wire schema. A node that survives the
    projection still provider-incompatible (e.g. a bare `{maxLength}` leaving an empty node) raises
    ValueError HERE — at bootstrap — instead of failing every live structured-output call closed."""
    from featuregen.intake.schema_projection import (
        assert_schemas_provider_compatible,
        project_for_anthropic,
    )
    assert_schemas_provider_compatible(
        [(name, project_for_anthropic(schema)) for (name, _v), schema in _SCHEMAS.items()])
    reg = DocumentSchemaRegistry(conn)
    for (name, ver), schema in _SCHEMAS.items():
        reg.register_schema(name, ver, schema, _OWNER)


@dataclass(frozen=True, slots=True)
class AuditedStructuredResult:
    """The FULL disposition of one governed structured call (Child-1 Task 3).

    ``output`` is exactly what ``audited_structured_call`` returns (the validated dict, or None on
    egress block / provider failure / outcome-audit discard). ``llm_call_ref`` is the immutable
    audit row written for the call — None only on a pre-dispatch block the caller did not ask to
    have recorded (``record_egress_block=False``, the enrichment default). ``provider_calls``
    counts the PHYSICAL provider requests issued (initial + repairs/retries; 0 when blocked before
    dispatch). ``usage`` is the outcome's provider-reported cost metadata (input/output tokens…);
    {} when nothing was dispatched."""
    output: dict | None
    llm_call_ref: str | None
    provider_calls: int
    usage: dict


def _record_blocked_call(conn, *, run_id: str, task: str, prompt_id: str, prompt_version: int,
                         schema_id: str, schema_version: int, actor: IdentityEnvelope,
                         reason: str, generation_settings: dict | None = None) -> str:
    """Record the immutable llm_call row for a call BLOCKED before dispatch (the
    ``record_egress_block=True`` path of ``drive_audited_structured_call``). CONTENT-FREE by
    construction: the blocked payload failed the egress contract, so nothing from it may be
    persisted on the audit row — the stored request carries EMPTY inputs, and only the guard's
    REASON (a label/diagnostic, never the content itself) rides ``validation_result``. Durable via
    ``_record_llm_call_durable`` for the same #20 reason as a dispatched call: the evidence that a
    block happened must survive the request tx. IN ADDITION to (never instead of) the
    ``EGRESS_BLOCKED`` security event the block site already records."""
    redaction = RedactionResult(text="", redaction_version=_REDACTION_VERSION,
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata={}, raw_input_classification="clean")
    req = LLMRequest(task=task, prompt_id=prompt_id, prompt_version=prompt_version, inputs=inputs,
                     output_schema_id=schema_id, output_schema_version=schema_version,
                     generation_settings=(
                         dict(generation_settings)
                         if generation_settings is not None
                         else _generation_settings()))
    return _record_llm_call_durable(
        conn, run_id=run_id, request=req, input_hash=compute_input_hash(inputs),
        redaction_version=_REDACTION_VERSION, input_redaction={},
        raw_output={"output": None, "self_reported_scores": {}},
        validation_result={"result": "egress_blocked", "reason": reason},
        repair_attempts=[], latency_ms=None, cost_metadata=None,
        created_by=identity_to_jsonb(actor))


def audited_structured_call(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
                            catalog_metadata: dict, instruction: str,
                            actor: IdentityEnvelope | None = None,
                            prompt_version: int = 1, schema_version: int = 1,
                            dispatch_audit: DispatchAuditContext | None = None,
                            cacheable_metadata_keys: tuple[str, ...] = ()) -> dict | None:
    """Run one governed metadata-only call and return the VALIDATED output dict, or None on any egress
    block / non-success. Attaches the registered output-schema (so a real provider does NOT fail closed),
    runs the egress guard, and records one immutable llm_call. The single audited seam for every overlay
    LLM node — enrichment, contract authoring/refine, and contract critique.

    Output-only view over ``drive_audited_structured_call`` (which carries the full disposition —
    llm_call_ref/provider_calls/usage — for callers like formula authoring that need it); this
    signature and its ``dict | None`` return are unchanged for the MANY existing callers.

    ``prompt_version``/``schema_version`` pin the request's contract (default ``1`` — byte-for-byte the
    v1 behavior): the resolved output-schema, the stamped request versions, and the validation all use
    them, so a versioned enrichment call cannot silently egress under the v1 contract.

    ``dispatch_audit`` (C5-T3): the INGESTION call sites thread a ``DispatchAuditContext`` so every
    PHYSICAL provider attempt (initial + each repair/retry re-call inside ``drive_structured_call``)
    is pre-dispatch audited via the ``AuditingClient`` wrapper, fail-closed. ``None`` (contract
    authoring / feature generation) keeps today's behavior byte-identical — no dispatch audit."""
    return drive_audited_structured_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, instruction=instruction, actor=actor,
        prompt_version=prompt_version, schema_version=schema_version,
        dispatch_audit=dispatch_audit, cacheable_metadata_keys=cacheable_metadata_keys).output


def drive_audited_structured_call(
        conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
        catalog_metadata: dict, instruction: str,
        actor: IdentityEnvelope | None = None,
        prompt_version: int = 1, schema_version: int = 1,
        dispatch_audit: DispatchAuditContext | None = None,
        cacheable_metadata_keys: tuple[str, ...] = (),
        run_id: str = ENRICHMENT_RUN_ID,
        record_egress_block: bool = False,
        generation_settings: dict | None = None,
        logical_call_ref: str | None = None) -> AuditedStructuredResult:
    """The outcome-returning core of ``audited_structured_call`` (same governance, richer return —
    mirrors ``drive_structured_call`` naming). Two ADDITIVE knobs beyond that seam's parameters,
    both defaulting to the historical behavior so ``audited_structured_call`` stays byte-identical:

    * ``run_id`` — the audit run bucket the immutable llm_call is recorded under (default
      ``ENRICHMENT_RUN_ID``). Formula authoring threads its ``authoring_run_id`` so every provider
      call is queryable per authoring run.
    * ``record_egress_block`` — when True, a call blocked BEFORE dispatch (free-text redaction
      fail-closed, feature-context adapter block, or the ``assert_llm_safe`` backstop) still writes
      a CONTENT-FREE llm_call row (``_record_blocked_call``) so the result carries an audited
      ``llm_call_ref`` for the block itself. False (default) keeps the enrichment behavior:
      security-event only, no llm_call row, ``llm_call_ref=None``."""
    actor = actor or _ENRICH_ACTOR
    effective_generation_settings = (
        dict(generation_settings)
        if generation_settings is not None
        else _generation_settings()
    )

    def _blocked(reason: str) -> AuditedStructuredResult:
        ref = None
        if record_egress_block:
            ref = _record_blocked_call(
                conn, run_id=run_id, task=task, prompt_id=prompt_id,
                prompt_version=prompt_version, schema_id=schema_id,
                schema_version=schema_version, actor=actor, reason=reason,
                generation_settings=effective_generation_settings)
        return AuditedStructuredResult(output=None, llm_call_ref=ref, provider_calls=0, usage={})

    reg = DocumentSchemaRegistry(conn)
    schema = _require_schema(conn, reg, schema_id, schema_version)   # D10: raise, never unenforced

    # #19: glossary free-text in the metadata is scanned/scrubbed BEFORE the payload is built —
    # the classification below is what the scan established, never a hardcoded "clean".
    safe_metadata, spans, sample_audits, free_text_version = _redact_free_text_meta(
        dict(catalog_metadata))
    if safe_metadata is None:
        logger.warning("free-text redaction failed closed for %s (schema %s); no dispatch",
                       task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor,
                            reason="glossary free-text redaction failed closed")
        return _blocked("glossary free-text redaction failed closed")  # no dispatch, no cache
    # [F4] the nested feature-menu adapter (spec §5) — fires on EVERY call (RF-I6: including the
    # contract-draft `_column_defs` shape, which it sanitizes rather than blocks); inert (same
    # object) on payloads without a feature menu.
    ctx_meta, ctx_spans, ctx_sample_audits, ctx_version = sanitize_feature_context(safe_metadata)
    if ctx_meta is None:
        logger.warning("feature-context egress adapter blocked %s (schema %s); no dispatch",
                       task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor,
                            reason="feature-context egress adapter failed closed")
        return _blocked("feature-context egress adapter failed closed")  # no dispatch, no cache
    safe_metadata = ctx_meta
    spans = spans + ctx_spans
    sample_audits = sample_audits + ctx_sample_audits
    free_text_version = free_text_version or ctx_version
    redaction_version = free_text_version or _REDACTION_VERSION
    redaction = RedactionResult(text=instruction, redaction_version=redaction_version,
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=safe_metadata,
                              raw_input_classification="contains_pii" if spans else "clean")
    req = LLMRequest(
        task=task, prompt_id=prompt_id, prompt_version=prompt_version, inputs=inputs,
        output_schema_id=schema_id, output_schema_version=schema_version,
        generation_settings=effective_generation_settings,
        output_schema=schema,
        # perf (vocab-caching): the single-mode concept path (and the batch's per-item fallback) also
        # carry the static vocabulary; mark it as the cached shared prefix so it isn't re-billed per
        # call. Empty () (definition/domain, contract authoring, feature gen) is byte-for-byte today.
        cacheable_metadata_keys=cacheable_metadata_keys)

    try:
        assert_llm_safe(req)              # §9.4 egress backstop
    except EgressViolation as exc:
        logger.warning("egress guard blocked %s (schema %s); no dispatch", task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor, reason=str(exc))
        return _blocked(str(exc))         # hard fail closed — no dispatch, no cache

    # C5-T3: with an ingestion-audit context, wrap the client so EVERY physical attempt is audited
    # BEFORE egress (fail-closed on AuditUnavailable — the provider is never called). The
    # logical_call_ref is minted ONCE per logical call so its retry/repair attempts share it
    # (UNIQUE(logical_call_ref, attempt_no)). With no context, the client is used untouched.
    dispatch_client: LLMClient = client
    auditing_client: AuditingClient | None = None
    if dispatch_audit is not None:
        auditing_client = AuditingClient(
            client,
            dispatch_audit,
            logical_call_ref=logical_call_ref or mint_id("lc"),
                                         redaction_version=redaction_version)
        dispatch_client = auditing_client
    outcome = drive_structured_call(
        dispatch_client, req, lambda output: reg.validate(schema_id, schema_version, output))
    llm_call_ref = _record_llm_call_durable(   # #20: evidence survives an upload-tx rollback
        conn, run_id=run_id, request=req, input_hash=compute_input_hash(req.inputs),
        redaction_version=redaction_version,
        input_redaction=({"redacted_spans": spans, "sample_strip": sample_audits}
                         if (spans or sample_audits) else {}),
        raw_output={"output": outcome.output, "self_reported_scores": outcome.self_reported_scores},
        validation_result=outcome.validation_result, repair_attempts=list(outcome.repair_attempts),
        latency_ms=None, cost_metadata=outcome.cost_metadata, created_by=identity_to_jsonb(actor))
    usage = dict(outcome.cost_metadata or {})
    if dispatch_audit is not None and auditing_client is not None:
        # C5-T4 eligibility ordering: record → link → return. The llm_call is durable (above) and
        # the dispatch/run associations commit HERE — before the output is handed back as eligible
        # for cache/evidence. C5-T6: an outcome-audit (link) FAILURE DISCARDS the result — the
        # provider output is not eligible for cache/evidence until the logical outcome audit
        # committed. The pre-dispatch authorization + the immutable llm_call are already durable, so
        # this loses convenience joins + defers the enrichment, never evidence. Scoped to the
        # dispatch_audit-present (ingestion) path ONLY; with dispatch_audit=None this whole branch
        # is skipped and behavior is byte-identical.
        if not link_llm_call(llm_call_ref=llm_call_ref,
                             dispatch_refs=auditing_client.dispatch_refs,
                             ingestion_run_id=dispatch_audit.ingestion_run_id,
                             stage=dispatch_audit.stage):
            logger.warning("enrichment call %s (schema %s) audit-degraded: the logical outcome "
                           "audit did not commit — discarding the enrichment result", task,
                           schema_id)
            # discard — not eligible for cache/evidence. The llm_call is durable + the provider was
            # called, so the ref/provider_calls/usage are surfaced even though output is withheld.
            return AuditedStructuredResult(output=None, llm_call_ref=llm_call_ref,
                                           provider_calls=outcome.provider_calls, usage=usage)

    if outcome.status == STATUS_FAILED:
        logger.warning("enrichment call %s (schema %s) failed: %s", task, schema_id,
                       outcome.validation_result)
        # provider/repair failure -> don't cache the (unverified) output; the call is still audited.
        return AuditedStructuredResult(output=None, llm_call_ref=llm_call_ref,
                                       provider_calls=outcome.provider_calls, usage=usage)
    output = outcome.output if isinstance(outcome.output, dict) else None
    return AuditedStructuredResult(output=output, llm_call_ref=llm_call_ref,
                                   provider_calls=outcome.provider_calls, usage=usage)


def audited_enrich_call(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
                        catalog_metadata: dict, out_key: str, instruction: str,
                        actor: IdentityEnvelope | None = None,
                        prompt_version: int = 1, schema_version: int = 1,
                        dispatch_audit: DispatchAuditContext | None = None,
                        cacheable_metadata_keys: tuple[str, ...] = ()) -> str | None:
    """Single-string convenience over `audited_structured_call`: returns the trimmed `out_key` field, or
    None on any egress block / non-success / empty output (so the caller never caches a failure).
    ``prompt_version``/``schema_version`` (default ``1``) thread straight to the structured seam.
    ``dispatch_audit`` (C5-T5) threads straight through too, so an INGESTION single/fallback call
    pre-dispatch audits every physical attempt; ``None`` (the default) is byte-identical to today."""
    out = audited_structured_call(
        conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
        catalog_metadata=catalog_metadata, instruction=instruction, actor=actor,
        prompt_version=prompt_version, schema_version=schema_version,
        dispatch_audit=dispatch_audit, cacheable_metadata_keys=cacheable_metadata_keys)
    if not out:
        return None
    val = str(out.get(out_key, "")).strip()
    return val or None


# Only metadata may egress per item (Global Constraint). Any other key (e.g. a data value, or a
# TECHNICAL upload's uploader-authored `definition` free text — M4 PII risk) means the item is
# excluded pre-egress and audited (spec C9 per-item egress). Besides the structural keys
# (table/column/type/columns/concept), a GLOSSARY column carries curated business-semantic metadata
# from its sidecar — the business term, its curated business definition, synonyms/aliases, data
# domain, and BIAN/FIBO taxonomy paths. These are MEANING (semantics about the column), not raw data
# values, so they pass the per-item egress filter under DISTINCT keys — deliberately NOT the plain
# `definition` key (which stays forbidden, so a technical free-text definition can never ride through
# this seam). Being MEANING does not make them CLEAN: each free-text value is then scanned/scrubbed
# by `_redact_free_text_meta` (finding #19), and the batch-level `assert_llm_safe` scan still
# applies on top.
_ITEM_META_ALLOWED = frozenset({
    "table", "column", "type", "columns", "concept",
    "term_name", "business_definition", "table_definition",
    "synonyms", "data_domain", "bian_path", "fibo_path",
    "column_profiles",
    # Pass B phase-2 (#1 — wide tables): the per-chunk summaries (bounded structured digests) and the
    # complete column ROSTER (short `name:type` strings). Both are MEANING/structure, not data values,
    # and are bounded field-by-field below — the phase-2 item carries these INSTEAD of a giant
    # column_profiles list, so a >64-col table's synthesis input still passes the per-item egress cap.
    "chunk_summaries", "column_roster",
    # Every column of the uploader's file this platform has no first-class slot for, as bounded
    # "header: value" strings. A mapping file's columns DESCRIBE columns — meaning, not customer
    # rows — and for a source that auto-fills its description column by bucket they are the only
    # per-column signal that varies. Carried as list-of-prose so they ride the SAME per-item PII
    # scan and length cap as `synonyms`, rather than opening an unscanned channel.
    "source_attributes",
    # Richness Task 3 Step 6c — the TAIL summary dossier. Sidecar fields (`term_type`,
    # `process_path`, `related_terms`) and the AI's own synonym draft (`ai_synonyms`) are
    # prose-classified above; `party_role`/`grain_role`/`table_role` are platform-derived
    # closed-vocabulary tokens (party_vocab / table_vocab / the grain flags), never uploader text.
    "term_type", "process_path", "related_terms", "ai_synonyms",
    "party_role", "grain_role", "table_role",
    # Joint Task 4 — the purpose adapters' widened per-item context. `semantic_terms` is
    # prose-classified above; the rest are platform-derived closed-vocabulary/bounded tokens
    # (`primary_entity` from `known_entities()`, the dual type tokens, the concept ancestry tuple).
    "semantic_terms", "primary_entity", "declared_type", "operational_type", "concept_path",
    # Profile Task 4 (Pass B v3) — the table-narrative context the synthesis reasons over and the
    # bounded evidence-ref roster every suggestion must cite from.
    "table_description", "business_context", "authority_role", "temporal_storage_model",
    "evidence_refs",
    # Suggestion-discovery Task 1 — the NAMED bounded template fields of the taxonomy-proposal
    # batch (template_discovery.discovery_proposal_items). These are REPO-AUTHORED recipe
    # metadata (SME-authored family/intent/aggregation/stage labels and legacy use-case tags),
    # never uploader data or sample values; each is length-bounded (`recipe_intent` below, the
    # 200 default otherwise) and the item builder normalizes/validates them before egress.
    "recipe_family", "recipe_intent", "recipe_aggregation", "funnel_stage",
    "legacy_use_case_tags",
    # Task 7b — the CATALOG narrative on the per-TABLE Pass-B synthesis item. Per table, never per
    # column: a 4000-character description multiplied across a 144-column table would dominate the
    # payload it is meant to inform. The four prose fields are free-text-classified above
    # (definition / prose / list-of-prose); the authority label is structural.
    *CATALOG_NARRATIVE_KEYS,
})

# The ONLY keys a per-column descriptor may carry, each a short scalar. `definition` is deliberately
# ABSENT — a technical free-text definition can never ride this seam; a curated meaning rides as
# `business_definition` (already stripped of sample values upstream). The role fields
# (identifier_role/temporal_role/semantic_type/entity) come from Pass A evidence and sharpen grain
# proposals (an identifier-role column is grain-eligible; a temporal-role column is as-of-eligible).
# The FTR-sidecar facets (term_type/domain/process_path — MF-2) come from the GlossaryRecord so the
# synthesizer reasons over the column's business classification, not just its physical name/type;
# they are bounded structural tokens (the default 200 cap via `_max_len_for`), never data values.
# Task 4 (Phase-2 Slice 1): `operational_type` (the row's physical type) and `declared_type` (the
# glossary-DECLARED SQL type — a HINT, not confirmation) are TWO separate keys; the CONFLATED
# `type` key stays allowlisted only for the v1 item shape (it is no longer emitted by the v2
# assembler and never combined with the dual keys by any producer).
_COLUMN_PROFILE_KEYS = frozenset({
    "column", "type", "operational_type", "declared_type", "concept", "business_definition",
    "identifier_role", "temporal_role", "semantic_type", "entity",
    "term_type", "domain", "process_path",
})

# [F4] The descriptor's OWN egress classification — the nested mirror of the three top-level classes,
# and the gate whose absence made this a leak. `_redact_free_text_meta` scanned exactly ONE nested
# key (`business_definition`) by name, so every OTHER admitted key rode to the provider unscanned,
# and widening `_COLUMN_PROFILE_KEYS` opened a new unscanned path with nothing to notice.
#
# WHY DESCRIPTOR-LOCAL AND NOT `_FREE_TEXT_META_KEYS`. The two namespaces are genuinely different:
# `_ITEM_META_ALLOWED` admits `data_domain` at the top level and never a bare `domain`, so folding
# these names into the top-level set would let a top-level `domain` egress as prose where it fails
# CLOSED today — a widening bought for no producer that exists. `_FEATURE_COLUMN_PROSE_KEYS` is the
# precedent: the feature MENU's nested column dicts already carry their own per-key grade, including
# `domain` and `process_path` under the same names.
#
# WHY PROSE AND NOT DEFINITION. `sanitize_definition` fails CLOSED on five literal sample-clause
# phrases, and these facets ride EVERY Pass-B item — so the definition grade would hand one ordinary
# uploader sentence ("Values Such As Payments" as a domain label) a kill switch over the whole
# table's synthesis. That is precisely the blast radius `_ADVISORY_CONTEXT_KEYS` was created to
# avoid. Prose scans for PII without the marker gate, which is the grade these already carry at the
# top level (`_SCALAR_PROSE_META_KEYS`) and on the feature seam.
_COLUMN_PROFILE_DEFINITION_KEYS = frozenset({"business_definition"})
# Uploader-authored sidecar text. `table_synth._descriptor` truncates each to 200 chars and its
# docstring called them "bounded structural tokens" — a length bound is not a safety property, and
# `_SCALAR_PROSE_META_KEYS` already grades `term_type`/`process_path` as prose one namespace up.
_COLUMN_PROFILE_PROSE_KEYS = frozenset({"term_type", "domain", "process_path"})
# Platform-derived: physical/declared type tokens, registry concept + entity links, and the closed
# `identifier_role`/`temporal_role`/`semantic_type` role vocabularies. None can carry uploader text.
_COLUMN_PROFILE_STRUCTURAL_KEYS = frozenset({
    "column", "type", "operational_type", "declared_type", "concept", "entity",
    "identifier_role", "temporal_role", "semantic_type",
})
#: ZERO-TRUNCATION RAISE (2026-08-06): 64 -> 512.
#:
#: READ THIS BEFORE MOVING IT AGAIN — it is not only an egress cap, it is Pass B's NARROW/WIDE
#: ROUTER (`table_synth.synthesize_tables`). At 64 every real bank table (126 FTR / 144 CIB
#: columns) took the TWO-PHASE path: its profiles were split into 64-column chunks, each chunk was
#: SUMMARIZED, and the single phase-2 synthesis then decided grain / table_role / primary_entity /
#: event_or_snapshot from those lossy summaries plus a names-and-types roster — never from the
#: columns' own definitions. The two-phase path exists BECAUSE of this cap (a wider item was
#: egress-REJECTED), not because summarizing is better. At 512 a real table synthesizes in ONE call
#: over its COMPLETE profiles, and the two-phase path remains as the backstop for a genuinely
#: pathological (>512-column) table. Fewer provider calls, strictly richer context.
_MAX_COLUMN_PROFILES = 512
#: Breadth is the point of `source_attributes`; unboundedness is not. A file with hundreds of columns
#: must not push the real signal out of a batch. ZERO-TRUNCATION RAISE (2026-08-06): 40 -> 256.
#: THIS IS THE EFFECTIVE LIMIT. An earlier revision of this note said the producer still bound first
#: at 40 — true when written, false since `ftr_adapter._MAX_SOURCE_ATTRIBUTES` was raised to 256 in
#: the same round to stop it making this cap inert. The two are now EQUAL BY INTENT and pinned equal
#: by `test_the_producer_caps_no_longer_bind_before_the_egress_caps_they_feed`: the producer must
#: never exceed this (a longer list is egress-REJECTED here) and must never sit below it (that is
#: silent truncation at the reader). No real mapping export comes near either — FTR has 17 headers
#: in total — so the binding constraint in practice is the file, not this number.
_MAX_SOURCE_ATTRIBUTES = 256

# A STRUCTURED wide-roster entry (Task 4): only the identity keys, short strings only —
# structured (never the old `name:type` flat string) because a column name may itself contain
# `:`/`/`, which the flat form conflated irrecoverably.
#
# Joint Task 4 widens it by TWO platform-derived closed-vocabulary tokens: `concept` (a registry
# concept name) and `party_role` (a `party_vocab` member). Both are the whole point of a SIBLING
# roster — a classifier that can see that its neighbours are `customer_id`/`counterparty` is being
# asked a different, answerable question from one shown bare names. Neither can carry uploader free
# text (the registry and the role vocabulary are closed), so they stay structural rather than
# prose-scanned; the length bound applies to them exactly as to the type tokens.
_ROSTER_ENTRY_KEYS = frozenset({"column", "operational_type", "declared_type",
                                "concept", "party_role"})

# A phase-2 chunk-summary (#1) carries ONLY column-name lists + an event/snapshot signal — bounded,
# egress-safe, and column-name-shaped (never a data value). `event_or_snapshot` is the lone scalar
# (nullable enum); the three `*_candidates`/`entity_signals` keys are short string lists. A summary
# with any other key (or a non-list/oversized value) is rejected pre-egress.
_CHUNK_SUMMARY_LIST_KEYS = frozenset({
    "grain_candidates", "temporal_candidates", "entity_signals",
})
_CHUNK_SUMMARY_KEYS = _CHUNK_SUMMARY_LIST_KEYS | {"event_or_snapshot"}
# A wide table produces ceil(ncols/_MAX_COLUMN_PROFILES) chunk summaries; the generous cap backstops a
# pathological column count without unbounding the phase-2 payload.
_MAX_CHUNK_SUMMARIES = 256

# The roster count cap is tied to the chunk caps BY CONSTRUCTION: the complete roster of the
# LARGEST summarizable wide table (`_MAX_CHUNK_SUMMARIES` chunks of `_MAX_COLUMN_PROFILES`
# profiles) always fits, so a table whose chunks all summarize can never have its phase-2 item
# egress-rejected for roster size — the two bounds cannot drift apart. (The OLD flat-string roster
# had NO count bound at all; any real table is far below this.)
_MAX_ROSTER = _MAX_CHUNK_SUMMARIES * _MAX_COLUMN_PROFILES


# The single source of truth for the SANITIZED business-definition length bound (DRY): re-exported by
# `enrich` (as `MAX_DEFINITION_LEN`, with the private `_MAX_DEFINITION_LEN` alias) and consumed by
# `table_synth._descriptor` — so `enrich.bounded_definition`'s window, the metadata-only egress cap,
# and Pass B's descriptor bound can never drift apart. Defined HERE (not in `enrich`) because `enrich`
# imports `enrich_llm`, so this module is the cycle-free home for the shared constant.
# ZERO-TRUNCATION RAISE (2026-08-06): 600 -> 4000 -> 32_000.
#
# MEASURED — and the two files are DIFFERENT measurements, kept apart here because an earlier
# revision of this comment merged them into one false sentence:
#   * committed fixture (`tests/.../fixtures/ftr_sample_synthetic.csv`, 127 rows): widest
#     `description_business_definition` 802 chars; **1** of the 127 exceeded the shipped 600.
#   * REAL FTR export (`FTR_Column_Mapping_final.csv`, 127 rows, gitignored): widest raw value 960
#     chars, 727 after the adapter's read-time sanitization; **41** of its 127 definitions exceeded
#     600 — i.e. roughly a third of a real bank catalog's definitions were reaching the model CUT
#     mid-sentence. That 41 is the real export's number, never the fixture's.
# 32_000 is ~33x the widest real value and 53x the original 600 — deliberately far past any observed
# input, and still a bound, so a 1 MB "definition" is caught rather than egressed.
#
# BOTH DIRECTIONS ARE INTENDED, AND BOTH ARE NOW WIRED. This constant is the egress cap on what we
# SEND (`_MAX_LEN_BY_KEY` -> `_item_len_ok`, and `enrich.bounded_definition`'s window), the
# admissibility bound on Pass-B/Pass-A material RE-THREADED into a later stage's item metadata, AND
# — since the inbound fix — the ACCEPT gate on what the model may WRITE back: `draft_definitions`
# passes exactly this constant to `_accept_bounded`, and `_SCHEMAS["overlay_definition_batch"]`
# carries it as `definition.maxLength`.
#
# (An earlier revision of this note said the opposite — "it is NOT an accept gate on model OUTPUT …
# so raising this does not by itself let a model write a longer definition". That was TRUE when
# written and is the exact half-finished state the inbound fix closed: the accept gate then read
# `_accept_bounded(500)`, below even the original 600. Raising this constant now moves both
# directions, which is why the schema/code pairs are pinned equal by `test_enrich_output_bounds.py`
# — the two fail differently, per-item vs whole-chunk, so they must never drift apart again.)
#
# WHAT HAD TO MOVE WITH IT — `enrich_config._DEFAULT_MAX_INPUT_TOKENS`. A definition at this cap is
# 8_000 estimated tokens (the estimator is exactly `len(json)//4`), so `max_items` of them no longer
# shared a chunk at the old budgets and the stage's call count rose. See the derivation block in
# `enrich_config`. Measured items/chunk at this cap, against the real `chunk_items`, are pinned by
# `test_enrichment_context_wiring.py`.
MAX_DEFINITION_LEN = 32_000

# Per-value egress length cap. ZERO-TRUNCATION RAISE (2026-08-06): 200 -> 1000. MEASURED against the
# real FTR export, the widest non-definition value is 105 chars (`related_terms`; then 84 for the
# FQN, 54 for synonyms), so the old 200 sat at less than 2x the observed maximum — one wider export
# away from excluding items. 1000 is ~10x it. The two sanitized DEFINITION fields keep their own,
# larger window (`MAX_DEFINITION_LEN`) so a full definition is never cut mid-sentence.
#
# This is an ACCEPTANCE gate, not a formatter: an over-cap value has its whole ITEM excluded from
# egress and audited, never truncated. Raising it therefore ADMITS shapes previously refused; it
# does not silently lengthen anything already flowing.
#
# The three keys that joined `_DEFINITION_META_KEYS` later (`table_description`, `business_context`,
# `semantic_terms`) still take this default rather than the definition window — but the safety
# property that used to justify the tight 200 has INVERTED, and that is deliberate: Pass B bounds
# its own narrative OUTPUT at 600 (`table_synth._MAX_PROFILE_PROSE`), which at a 200 default meant a
# re-threaded `business_context` had its item EXCLUDED + audited. At 1000 it is ADMITTED instead,
# which is the outcome this task wants. `_MAX_PROFILE_PROSE` must stay BELOW this default; see the
# note beside it.
_MAX_LEN_DEFAULT = 1000
#: The CATALOG-narrative acceptance bound (Task 7b), on the two fields `catalog_profiles` accepts at
#: 4000 characters. Left to inherit `_MAX_LEN_DEFAULT` a legitimately-authored description would
#: EXCLUDE its whole Pass-B item — that table would lose its synthesis (grain, table_role,
#: primary_entity, event_or_snapshot), silently, audited only as an egress block. Set at TWICE the
#: authored bound rather than exactly at it, because this gate runs AFTER redaction and redaction is
#: not length-preserving in the shrinking direction: `DefaultIntentRedactor` substitutes a
#: `[REDACTED:LABEL]` placeholder that is LONGER than several tokens it replaces, so a
#: maximum-length narrative carrying PII would otherwise be refused for having been scrubbed.
#: DERIVED from the authored bounds, never re-typed, so raising them cannot leave this behind.
_CATALOG_NARRATIVE_MAX_LEN = 2 * max(MAX_CATALOG_DESCRIPTION, MAX_CATALOG_BUSINESS_CONTEXT)
_MAX_LEN_BY_KEY = {"business_definition": MAX_DEFINITION_LEN,
                   "table_definition": MAX_DEFINITION_LEN,
                   "catalog_description": _CATALOG_NARRATIVE_MAX_LEN,
                   "catalog_business_context": _CATALOG_NARRATIVE_MAX_LEN,
                   # Task 1: the authored one-line recipe intent (longest authored value is 323
                   # chars at the 0F-2 baseline). Kept as an EXPLICIT entry rather than left to
                   # inherit the default, so lowering `_MAX_LEN_DEFAULT` later cannot silently
                   # re-truncate an authored field; raised 400 -> 1000 with the default because a
                   # per-key cap BELOW the default is an incoherence, not a control.
                   "recipe_intent": 1000}


def _max_len_for(key: str) -> int:
    return _MAX_LEN_BY_KEY.get(key, _MAX_LEN_DEFAULT)


def _column_profile_shape_ok(desc: object) -> bool:
    if not isinstance(desc, dict):
        return False
    if any(k not in _COLUMN_PROFILE_KEYS for k in desc):
        return False
    return all(isinstance(v, str) for v in desc.values())


def _column_profile_len_ok(desc: dict) -> bool:
    return all(len(v) <= _max_len_for(k) for k, v in desc.items() if isinstance(v, str))


def _column_profile_ok(desc: object) -> bool:
    return _column_profile_shape_ok(desc) and _column_profile_len_ok(desc)


def _roster_entry_ok(entry: object) -> bool:
    """One structured `column_roster` entry: a dict of the three identity keys ONLY
    (`_ROSTER_ENTRY_KEYS`), short strings. Shape and length together — roster entries are
    structural names/types, never free text, so the sanitizer cannot shorten them and the [F7]
    shape/length split does not apply. The OLD flat `name:type` string is rejected."""
    if not isinstance(entry, dict) or any(k not in _ROSTER_ENTRY_KEYS for k in entry):
        return False
    return all(isinstance(v, str) and len(v) <= _MAX_LEN_DEFAULT for v in entry.values())


def _chunk_summary_shape_ok(summary: object) -> bool:
    if not isinstance(summary, dict):
        return False
    if any(k not in _CHUNK_SUMMARY_KEYS for k in summary):
        return False
    for k, v in summary.items():
        if k == "event_or_snapshot":
            if v is not None and not isinstance(v, str):
                return False
        elif not isinstance(v, list) or not all(isinstance(x, str) for x in v):
            return False
    return True


def _chunk_summary_len_ok(summary: dict) -> bool:
    for k, v in summary.items():
        if k == "event_or_snapshot":
            if isinstance(v, str) and len(v) > 64:
                return False
        elif isinstance(v, list) and any(isinstance(x, str) and len(x) > 128 for x in v):
            return False
    return True


def _chunk_summary_ok(summary: object) -> bool:
    return _chunk_summary_shape_ok(summary) and _chunk_summary_len_ok(summary)


def _item_shape_ok(metadata: dict) -> bool:
    """[F7] SHAPE/allowlist half of the per-item egress gate — runs BEFORE `_redact_free_text_meta`:
    allowlisted keys only, correct value types + list structure, count caps. Per-value LENGTH is
    deliberately NOT checked here: a long RAW definition may sanitize (sample-clause strip) to
    within its bound, so the length gate (`_item_len_ok`) runs AFTER sanitization instead — the
    old combined pre-redaction gate excluded such items before the sanitizer could shorten them."""
    if any(k not in _ITEM_META_ALLOWED for k in metadata):
        return False
    for k, v in metadata.items():
        if k == "column_profiles":
            if not isinstance(v, list) or len(v) > _MAX_COLUMN_PROFILES:
                return False
            if not all(_column_profile_shape_ok(d) for d in v):
                return False
        elif k == "chunk_summaries":
            if not isinstance(v, list) or len(v) > _MAX_CHUNK_SUMMARIES:
                return False
            if not all(_chunk_summary_shape_ok(s) for s in v):
                return False
        elif k == "column_roster":
            # Task 4: STRUCTURED entries only ({column, operational_type, declared_type} dicts) —
            # `_roster_entry_ok` checks shape AND length (structural tokens, never sanitized), so
            # the roster needs no `_item_len_ok` half. The old flat `name:type` strings now fail.
            if not isinstance(v, list) or len(v) > _MAX_ROSTER:
                return False
            if not all(_roster_entry_ok(e) for e in v):
                return False
        elif k == "source_attributes":
            if not isinstance(v, list) or len(v) > _MAX_SOURCE_ATTRIBUTES:
                return False
            if not all(isinstance(x, str) for x in v):
                return False
        elif isinstance(v, list):
            if not all(isinstance(x, str) for x in v):
                return False
        elif not isinstance(v, str):
            return False
    return True


def _item_len_ok(metadata: dict) -> bool:
    """[F7] Per-value LENGTH half of the per-item egress gate — the batch seam runs it AFTER
    `_redact_free_text_meta`, on the SANITIZED item, so the bound applies to what would actually
    egress (a stripped definition that now fits passes; one still over-bound is excluded +
    audited on the same egress path)."""
    for k, v in metadata.items():
        if k == "column_profiles":
            if isinstance(v, list) and not all(
                    _column_profile_len_ok(d) for d in v if isinstance(d, dict)):
                return False
        elif k == "chunk_summaries":
            if isinstance(v, list) and not all(
                    _chunk_summary_len_ok(s) for s in v if isinstance(s, dict)):
                return False
        elif isinstance(v, list):
            if not all(len(x) <= _max_len_for(k) for x in v if isinstance(x, str)):
                return False
        elif isinstance(v, str) and len(v) > _max_len_for(k):
            return False
    return True


def _item_egress_ok(metadata: dict) -> bool:
    """The FULL per-item egress contract (shape AND length) — the single predicate assemblers
    (`table_synth`) validate against. The batch seam applies the two halves at different times
    ([F7]): shape before sanitization, length after."""
    return _item_shape_ok(metadata) and _item_len_ok(metadata)


def audited_batch_call(conn, client: LLMClient, *, task: str, prompt_id: str, schema_id: str,
                       shared_metadata: dict, items: list[BatchItem], out_key: str, instruction: str,
                       accept, actor: IdentityEnvelope | None = None,
                       extract=None, ref_aware: bool = False,
                       prompt_version: int = 1, schema_version: int = 1,
                       dispatch_audit: DispatchAuditContext | None = None) -> BatchCallResult:
    """One GOVERNED batch call (spec C4/C9): per-item egress filter -> batch-level egress guard ->
    schema-validated array call -> one immutable llm_call with a per-item outcome summary. Returns a
    BatchCallResult whose outcomes classify every requested ref (via validate_batch_results).

    ``prompt_version``/``schema_version`` pin the request's contract (default ``1`` — byte-for-byte the
    v1 behavior): output-schema resolution, the stamped request versions, and validation all use them.

    ``dispatch_audit`` (C5-T5): the INGESTION batch call sites thread a ``DispatchAuditContext`` so
    every PHYSICAL provider attempt is pre-dispatch audited (fail-closed) and the recorded llm_call
    is linked back to its dispatches + run — the same C5-T3/T4 wiring ``audited_structured_call``
    carries (this seam drives the provider itself, so it cannot delegate there). The context's
    ``subjects`` describe the items SUBMITTED to this call; an item the per-item egress gate excludes
    is still listed — over-attribution is the conservative direction (an auditor sees every object
    the caller intended the dispatch to be about; nothing that egressed is ever missing). ``None``
    (non-ingestion callers) keeps today's behavior byte-identical — no dispatch audit."""
    actor = actor or _ENRICH_ACTOR
    # [F7] SHAPE gate only pre-redaction: an item with a forbidden key / wrong structure never
    # reaches the sanitizer, but a merely-long definition is NOT excluded here — sanitization may
    # bring it under its length bound, so the length gate runs after `_redact_free_text_meta`.
    excluded = [it for it in items if not _item_shape_ok(it.metadata)]
    included = [it for it in items if _item_shape_ok(it.metadata)]
    egress_outcomes = [BatchItemOutcome(it.ref, EGRESS, None, (EGRESS,)) for it in excluded]
    for _it in excluded:
        _audit_egress_block(conn, task=task, actor=actor, reason="item metadata not metadata-only")

    # #19: per-item free-text scan/scrub (spec C9 grain — a fail-closed value excludes ITS item,
    # never the batch). The classification below is what the scan established, never a hardcoded
    # "clean"; scrubbed span types/positions + per-definition sample-strip audits are recorded on
    # the llm_call audit row. Span/audit records of an item that is ultimately EXCLUDED never ride
    # the record — input_redaction describes only what actually egressed.
    safe_items: list[BatchItem] = []
    all_spans: list[dict] = []
    all_sample_audits: list[dict] = []
    free_text_version: str | None = None
    for it in included:
        meta, spans, sample_audits, version = _redact_free_text_meta(it.metadata)
        free_text_version = free_text_version or version
        if meta is None:
            egress_outcomes.append(BatchItemOutcome(it.ref, EGRESS, None, (EGRESS,)))
            _audit_egress_block(conn, task=task, actor=actor,
                                reason="glossary free-text redaction failed closed")
            continue
        if not _item_len_ok(meta):
            # [F7] LENGTH gate on the SANITIZED item: a value still over its egress bound after
            # sample-strip/PII redaction is excluded + audited on the same egress path.
            egress_outcomes.append(BatchItemOutcome(it.ref, EGRESS, None, (EGRESS,)))
            _audit_egress_block(conn, task=task, actor=actor,
                                reason="sanitized item metadata over egress length bound")
            continue
        safe_items.append(BatchItem(it.ref, meta))
        all_spans.extend({"ref": it.ref, **s} for s in spans)
        all_sample_audits.extend({"ref": it.ref, **a} for a in sample_audits)
    included = safe_items

    if not included:
        return BatchCallResult(tuple(egress_outcomes), 0, 0, 0)

    reg = DocumentSchemaRegistry(conn)
    schema = _require_schema(conn, reg, schema_id, schema_version)   # D10: raise, never unenforced

    catalog_metadata = {**shared_metadata,
                        "items": [{"ref": it.ref, **it.metadata} for it in included]}
    redaction_version = free_text_version or _REDACTION_VERSION
    redaction = RedactionResult(text=instruction, redaction_version=redaction_version,
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=catalog_metadata,
                              raw_input_classification="contains_pii" if all_spans else "clean")
    req = LLMRequest(task=task, prompt_id=prompt_id, prompt_version=prompt_version, inputs=inputs,
                     output_schema_id=schema_id, output_schema_version=schema_version,
                     generation_settings=_generation_settings(), output_schema=schema,
                     # perf (vocab-caching): the shared_metadata keys (the ~276-concept classification
                     # vocabulary) are a STATIC prefix identical across every chunk — mark them so the
                     # adapter sends the block ONCE under cache_control and chunks 2..N reuse it,
                     # instead of re-billing ~23K input tokens per chunk. Empty shared_metadata
                     # (definition/domain) -> () -> byte-for-byte today.
                     cacheable_metadata_keys=tuple(shared_metadata))

    try:
        assert_llm_safe(req)                      # batch-level egress backstop (spec C9)
    except EgressViolation as exc:
        logger.warning("egress guard blocked batch %s (schema %s); no dispatch", task, schema_id)
        _audit_egress_block(conn, task=task, actor=actor, reason=str(exc))
        missing = validate_batch_results(included, [], out_key, accept,
                                         extract=extract, ref_aware=ref_aware)
        return BatchCallResult(tuple(egress_outcomes) + tuple(missing), 0, 0, 0)

    # C5-T5: with an ingestion-audit context, wrap the client so EVERY physical batch attempt is
    # audited BEFORE egress (fail-closed on AuditUnavailable — the provider is never called),
    # exactly like audited_structured_call's seam. The logical_call_ref is minted ONCE per batch
    # call so its retry/repair attempts share it (UNIQUE(logical_call_ref, attempt_no)).
    dispatch_client: LLMClient = client
    auditing_client: AuditingClient | None = None
    if dispatch_audit is not None:
        auditing_client = AuditingClient(client, dispatch_audit, logical_call_ref=mint_id("lc"),
                                         redaction_version=redaction_version)
        dispatch_client = auditing_client
    outcome = drive_structured_call(dispatch_client, req,
                                    lambda o: reg.validate(schema_id, schema_version, o))
    # A repair-exhausted / truncated batch (STATUS_FAILED) carries an UNVERIFIED body — do not harvest
    # it. Treat it as empty so validate_batch_results marks every requested ref MISSING and the
    # orchestrator's fallback ladder recovers it. Mirrors audited_structured_call returning None on
    # STATUS_FAILED: otherwise a truncated definition/domain value (validated only by `accept`) would
    # be cached durably and never retried (whole-branch review, BLOCKING).
    results = (outcome.output.get("results", [])
               if outcome.status != STATUS_FAILED and isinstance(outcome.output, dict) else [])
    item_outcomes = validate_batch_results(included, results, out_key, accept,
                                           extract=extract, ref_aware=ref_aware)

    summary = {"requested": [it.ref for it in included],
               "outcomes": {o.ref: o.status for o in item_outcomes}}
    cost = dict(outcome.cost_metadata or {})
    llm_call_ref = _record_llm_call_durable(   # #20: evidence survives an upload-tx rollback
        conn, run_id=_RUN, request=req, input_hash=compute_input_hash(req.inputs),
        redaction_version=redaction_version,
        input_redaction=({"redacted_spans": all_spans, "sample_strip": all_sample_audits}
                         if (all_spans or all_sample_audits) else {}),
        raw_output={"output": outcome.output,
                    "self_reported_scores": outcome.self_reported_scores},
        validation_result=outcome.validation_result,
        repair_attempts=list(outcome.repair_attempts), latency_ms=None,
        cost_metadata={**cost, "batch": summary}, created_by=identity_to_jsonb(actor))
    if dispatch_audit is not None and auditing_client is not None:
        # C5-T4 eligibility ordering (mirrors audited_structured_call): record → link → return.
        # C5-T6: an outcome-audit (link) FAILURE DISCARDS the result — mark every INCLUDED ref
        # MISSING so nothing is harvested/cached (the same all-MISSING discard the STATUS_FAILED
        # path above uses), leaving the fallback ladder to recover it. The pre-dispatch
        # authorization + the immutable llm_call are already durable; only the convenience linkage
        # failed. Scoped to the dispatch_audit-present (ingestion) path ONLY; with
        # dispatch_audit=None this branch is skipped and behavior is byte-identical.
        if not link_llm_call(llm_call_ref=llm_call_ref,
                             dispatch_refs=auditing_client.dispatch_refs,
                             ingestion_run_id=dispatch_audit.ingestion_run_id,
                             stage=dispatch_audit.stage):
            logger.warning("batch %s (schema %s) audit-degraded: the logical outcome audit did not "
                           "commit — discarding the enrichment result", task, schema_id)
            discarded = validate_batch_results(included, [], out_key, accept,
                                               extract=extract, ref_aware=ref_aware)
            return BatchCallResult(
                outcomes=tuple(egress_outcomes) + tuple(discarded),
                provider_calls=outcome.provider_calls,
                input_tokens=int(cost.get("input_tokens", 0)),
                output_tokens=int(cost.get("output_tokens", 0)))

    return BatchCallResult(
        outcomes=tuple(egress_outcomes) + tuple(item_outcomes),
        # #21: the ACTUAL provider requests the driver issued (initial + repairs + retries) — never
        # a hardcoded 1, so the caller's provider-call budget reflects reality.
        provider_calls=outcome.provider_calls,
        input_tokens=int(cost.get("input_tokens", 0)), output_tokens=int(cost.get("output_tokens", 0)))
