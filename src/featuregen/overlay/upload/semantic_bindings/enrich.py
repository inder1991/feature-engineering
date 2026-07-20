"""D3 — the OPTIONAL, audited LLM SELECTION layer for semantic bindings (``overlay.semantic_bindings``).

D2's shortlist is DETERMINISTIC (structural/term/facet enumeration). D3 adds an LLM task that only
SELECTS among D2's server-minted candidate ids and supplies rationale/confidence — it can NEVER
invent an identity or a target. It is a SEPARATE audited failure domain: a provider / schema /
deadline / egress failure loses only these semantic PROPOSALS (a ``failed``/``partial`` candidate set
with truthful counts), never Pass B grain/availability/table metadata, and NEVER fails core
ingestion.

The four hard invariants (mirroring the brief):

* **Select-only.** The model sees server-minted candidate ids + SAFE metadata (bare column names /
  concepts / entity ids / dispositions — NEVER a raw sample value, NEVER a raw FQN it could echo as a
  new target). Its response may SELECT known ids and adjust disposition (strong/weak) + confidence on
  those KNOWN candidates; a model id NOT in the presented shortlist is DROPPED with a durable reason
  code (:data:`RC_UNKNOWN_CANDIDATE_ID`) — never persisted as a candidate.
* **Bounded, fail-soft.** :func:`enrich_config.semantic_binding_bounds` caps candidates-per-table,
  provider calls, model-facing bytes, and a wall-clock deadline. A candidate-cap overflow → a
  ``partial`` set (the capped subset is still ranked). A byte / call / deadline overflow → a
  ``failed`` set with NO dispatch. Every path returns an :class:`EnrichResult`; the top-level entry
  NEVER raises, so core ingestion / Pass B are unaffected.
* **No egress bypass.** Every model-facing payload rides enrich_llm's field-aware egress policy
  (``_redact_free_text_meta`` → ``sanitize_feature_context`` → ``assert_llm_safe``); an egress refusal
  makes the stage ``failed`` WITHOUT dispatch (``EGRESS_BLOCKED`` audited). C5 pre-dispatch
  attribution rides on every physical attempt.
* **No persist before ``llm_call_ref``.** A candidate is persisted ONLY after the immutable
  ``llm_call`` outcome has committed and supplied its ``llm_call_ref`` (D1's candidate rows carry it).
  A provider / schema / egress failure persists a ``failed`` set with ZERO candidate rows — nothing
  derived from a provider response is ever stored without its committed ``llm_call_ref``.

Confidence is EVIDENCE about this inference event only — stored in ``evidence_json['llm']``, never in
``proposed_value`` (so D2's ``to_fact_command`` can never copy it onto a governed fact) and never a
promotion authority: the model may CONFIRM or DOWNGRADE a deterministic disposition, never UPGRADE
one (a weak candidate stays weak no matter how confident the model claims to be).
"""
from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime

from featuregen.contracts import DbConn
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
)
from featuregen.intake.redaction import (
    EgressViolation,
    RedactionResult,
    assert_llm_safe,
    build_llm_inputs,
)
from featuregen.overlay.upload import enrich_config
from featuregen.overlay.upload.dispatch_audit import (
    AuditingClient,
    DispatchAuditContext,
    link_llm_call,
)
from featuregen.overlay.upload.enrich_llm import (
    _ENRICH_ACTOR,
    _OWNER,
    _REDACTION_VERSION,
    ENRICHMENT_RUN_ID,
    _audit_egress_block,
    _generation_settings,
    _record_llm_call_durable,
    _redact_free_text_meta,
    sanitize_feature_context,
)
from featuregen.overlay.upload.semantic_bindings.store import (
    DEFAULT_CONFIG_VERSION as _D2_CONFIG_VERSION,
)
from featuregen.overlay.upload.semantic_bindings.store import (
    DEFAULT_SHORTLIST_VERSION as _D2_SHORTLIST_VERSION,
)
from featuregen.overlay.upload.semantic_bindings.store import table_fingerprint, table_graph_ref
from featuregen.overlay.upload.semantic_bindings.store_projection import (
    CandidateInput,
    mint_candidate_id,
    mint_candidate_set_id,
    persist_candidate_set,
)
from featuregen.overlay.upload.semantic_bindings.types import (
    CURRENCY_BINDING,
    REJECTED,
    STRONG,
    WEAK,
    SemanticBindingCandidate,
)

logger = logging.getLogger(__name__)

# ── task / prompt / schema identity (stamped on the immutable llm_call + the D1 provenance). ─────
SEMANTIC_BINDINGS_TASK = "overlay.semantic_bindings"
PROMPT_ID = "overlay_semantic_bindings_v1"
PROMPT_VERSION = 1
# The document-registry output-schema (int-versioned) that structures the SELECTION response.
SELECTION_SCHEMA_ID = "overlay_semantic_bindings_select"
SELECTION_SCHEMA_VERSION = 1
DISPATCH_STAGE = "semantic_bindings"

# D1 candidate-row provenance versions (strings) — distinct from D2's deterministic ones so a D3
# LLM set is a SEPARATE immutable candidate set (its own candidate_set_id), never a mutation of D2's.
TASK_VERSION = "d3-select-v1"
D3_PROMPT_VERSION = "d3-prompt-v1"
D3_SCHEMA_VERSION = "d3-schema-v1"
CONFIG_VERSION = "d3-config-v1"

# Durable drop reason codes (surfaced on EnrichResult; the model's invented ids also survive verbatim
# in the immutable llm_call.raw_output, so the drop is doubly auditable).
RC_UNKNOWN_CANDIDATE_ID = "llm_unknown_candidate_id"       # a model id not in the presented shortlist
RC_LLM_DISPOSITION_OFF_VOCAB = "llm_disposition_off_vocab"  # a selection whose disposition ∉ strong/weak

# The instruction is a FIXED string — never uploader free text / data values (mirrors enrich_llm's
# metadata-only "intent"). The closed disposition vocab is enumerated here, enforced code-side.
_INSTRUCTION = (
    "You are re-ranking a server-enumerated shortlist of candidate semantic bindings for one table. "
    "You may ONLY select from the given candidate_id values and set each selected candidate's "
    "disposition to 'strong' or 'weak' with a confidence in [0,1] and a short rationale. You cannot "
    "add a candidate, invent a column, or change a subject/target. Return only known candidate_ids."
)

# The SELECTION response schema. `disposition` is deliberately NOT a schema enum (mirrors table_synth
# [F1]): reg.validate rejects the WHOLE response on one enum violation, so an off-vocab disposition is
# code-checked per selection instead (that selection keeps its deterministic disposition). No array
# maxItems (the Anthropic structured-output API rejects it); the per-table candidate cap is the real
# input bound.
_SELECTION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "properties": {"selections": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "properties": {
            "candidate_id": {"type": "string", "maxLength": 128},
            "disposition": {"type": "string", "maxLength": 16},
            "confidence": {"type": "number"},
            "rationale": {"type": "string", "maxLength": 500},
        },
        "required": ["candidate_id", "disposition"]}}},
    "required": ["selections"],
}

_MAX_TOKEN_LEN = 128  # per-value egress bound for the structural candidate tokens

# The CLOSED allowlist of per-candidate keys that may egress (a probe the egress tests assert
# against): a server-minted id + bounded STRUCTURAL tokens only — never a logical_ref / sample value.
_ITEM_ALLOWED_KEYS_PROBE = frozenset({
    "candidate_id", "binding_kind", "subject_column", "subject_concept",
    "target_column", "target_concept", "entity_id", "disposition",
})


@dataclass(frozen=True, slots=True)
class EnrichResult:
    """The truthful outcome of ONE table's semantic-binding LLM stage. ``completion_status`` mirrors
    D1's ``complete | partial | failed``; ``persisted`` is how many candidate rows the D3 set carries;
    ``dropped_unknown`` is ``(model_candidate_id, reason_code)`` per invented id the code refused."""

    completion_status: str
    candidate_set_id: str | None
    llm_call_ref: str | None
    presented: int
    selected: int
    persisted: int
    dropped_unknown: tuple[tuple[str, str], ...] = ()
    reason: str | None = None


# ==================================================================================================
# Safe model-facing payload (SELECT-only, egress-classified)
# ==================================================================================================
def _bounded(value: str | None) -> str | None:
    return value[:_MAX_TOKEN_LEN] if value else value


def _candidate_item(candidate: SemanticBindingCandidate, candidate_id: str) -> dict:
    """The ONLY per-candidate metadata that egresses: the server-minted ``candidate_id`` + SAFE
    STRUCTURAL tokens — bare column names (NEVER the ``source::schema.table.column`` logical_ref),
    curated concepts, the closed-vocabulary entity id, the deterministic disposition. No sample value
    can appear (the shortlist never reads one); no raw FQN the model could echo as a new target."""
    item: dict[str, object] = {
        "candidate_id": candidate_id,
        "binding_kind": candidate.binding_kind,
        "subject_column": _bounded(candidate.subject.column),
        "disposition": candidate.disposition,
    }
    if candidate.evidence.subject_concept:
        item["subject_concept"] = _bounded(candidate.evidence.subject_concept)
    if candidate.binding_kind == CURRENCY_BINDING and candidate.target is not None:
        item["target_column"] = _bounded(candidate.target.column)
        if candidate.evidence.target_concept:
            item["target_concept"] = _bounded(candidate.evidence.target_concept)
    elif candidate.entity_id:
        item["entity_id"] = _bounded(candidate.entity_id)
    return item


def _safe_payload(table_view: object, presented: list[tuple[str, SemanticBindingCandidate]]) -> dict:
    """The metadata-only catalog payload for the model: a bare table NAME (never an FQN) + the
    per-candidate safe items. Distinct top-level keys from the enrichment/feature payloads, so
    enrich_llm's glossary + feature-menu egress adapters are inert here and ``assert_llm_safe`` is the
    active backstop that scans every string for a data marker / forbidden key."""
    return {
        "table": _bounded(getattr(table_view, "table", None)),
        "candidates": [_candidate_item(c, cid) for cid, c in presented],
    }


def _dispatch_subjects(catalog_source: str,
                       presented: list[tuple[str, SemanticBindingCandidate]]) -> list[dict]:
    """C5 attribution grain: one ``{catalog_source, object_ref, logical_ref, field_names}`` per unique
    catalog column the dispatch is about (subjects + currency targets). The logical_ref/object_ref ride
    the INTERNAL audit trail (llm_dispatch_subject), never the model payload."""
    by_ref: dict[str, dict] = {}
    for _cid, cand in presented:
        cols = [cand.subject]
        if cand.target is not None:
            cols.append(cand.target)
        for col in cols:
            by_ref.setdefault(col.logical_ref, {
                "catalog_source": catalog_source, "object_ref": col.graph_ref,
                "logical_ref": col.logical_ref, "field_names": [col.column]})
    return [by_ref[k] for k in sorted(by_ref)]


# ==================================================================================================
# The audited SELECT call — mirrors enrich_llm.audited_structured_call, returns the llm_call_ref
# ==================================================================================================
def _register_selection_schema(conn: DbConn) -> None:
    DocumentSchemaRegistry(conn).register_schema(
        SELECTION_SCHEMA_ID, SELECTION_SCHEMA_VERSION, _SELECTION_SCHEMA, _OWNER)


def _audited_select_call(
    conn: DbConn, client: LLMClient, *, catalog_metadata: dict,
    actor: IdentityEnvelope, dispatch_audit: DispatchAuditContext | None,
) -> tuple[dict | None, str | None, str | None, str]:
    """Run ONE governed metadata-only SELECT call. Step-for-step MIRROR of
    ``enrich_llm.audited_structured_call`` — SAME field-aware egress policy, SAME
    ``assert_llm_safe`` backstop, SAME C5 ``AuditingClient`` + ``link_llm_call`` wiring, SAME durable
    ``_record_llm_call_durable``. Returns ``(output|None, llm_call_ref|None, dispatch_ref|None,
    status)``: ``llm_call_ref`` is the committed outcome record (the no-persist-before-ref ordering
    signal); ``dispatch_ref`` is the C5 pre-dispatch authorization the D1 candidate rows FK-link to
    (the 1014 ``llm_call_ref`` column → ``llm_dispatch.dispatch_ref``), ``None`` when no durable
    dispatch store is configured (no-DSN dev/test). NEVER bypasses the egress guard: an egress
    refusal returns ``(None, None, None, 'egress_blocked')`` with ``EGRESS_BLOCKED`` audited and NO
    dispatch."""
    reg = DocumentSchemaRegistry(conn)
    schema = reg.schema_for(SELECTION_SCHEMA_ID, SELECTION_SCHEMA_VERSION)
    if schema is None:
        _register_selection_schema(conn)
        schema = reg.schema_for(SELECTION_SCHEMA_ID, SELECTION_SCHEMA_VERSION)

    # Field-aware egress policy (reused verbatim): glossary free-text scrub, then the nested
    # feature-menu adapter. Both are inert for D3's structural-only payload, but present so no path
    # can ever skip the egress guard; a fail-closed either way blocks dispatch.
    safe_metadata, spans, sample_audits, free_text_version = _redact_free_text_meta(
        dict(catalog_metadata))
    if safe_metadata is None:
        _audit_egress_block(conn, task=SEMANTIC_BINDINGS_TASK, actor=actor,
                            reason="semantic-binding free-text redaction failed closed")
        return None, None, None, "egress_blocked"
    ctx_meta, ctx_spans, ctx_sample_audits, ctx_version = sanitize_feature_context(safe_metadata)
    if ctx_meta is None:
        _audit_egress_block(conn, task=SEMANTIC_BINDINGS_TASK, actor=actor,
                            reason="semantic-binding feature-context adapter failed closed")
        return None, None, None, "egress_blocked"
    safe_metadata = ctx_meta
    spans = spans + ctx_spans
    sample_audits = sample_audits + ctx_sample_audits
    redaction_version = free_text_version or ctx_version or _REDACTION_VERSION
    redaction = RedactionResult(text=_INSTRUCTION, redaction_version=redaction_version,
                                redacted_spans=(), disposition="ok")
    inputs = build_llm_inputs(redaction, catalog_metadata=safe_metadata,
                              raw_input_classification="contains_pii" if spans else "clean")
    req = LLMRequest(
        task=SEMANTIC_BINDINGS_TASK, prompt_id=PROMPT_ID, prompt_version=PROMPT_VERSION,
        inputs=inputs, output_schema_id=SELECTION_SCHEMA_ID,
        output_schema_version=SELECTION_SCHEMA_VERSION,
        generation_settings=_generation_settings(), output_schema=schema)

    try:
        assert_llm_safe(req)                     # §9.4 egress backstop — scans every model-facing str
    except EgressViolation as exc:
        _audit_egress_block(conn, task=SEMANTIC_BINDINGS_TASK, actor=actor, reason=str(exc))
        return None, None, None, "egress_blocked"  # hard fail closed — no dispatch, no candidate

    # C5-T3: pre-dispatch attribution on EVERY physical attempt (fail-closed on AuditUnavailable).
    dispatch_client: LLMClient = client
    auditing_client: AuditingClient | None = None
    if dispatch_audit is not None:
        auditing_client = AuditingClient(client, dispatch_audit, logical_call_ref=mint_id("lc"),
                                         redaction_version=redaction_version)
        dispatch_client = auditing_client
    outcome = drive_structured_call(
        dispatch_client, req,
        lambda o: reg.validate(SELECTION_SCHEMA_ID, SELECTION_SCHEMA_VERSION, o))
    llm_call_ref = _record_llm_call_durable(     # #20: evidence survives an upload-tx rollback
        conn, run_id=ENRICHMENT_RUN_ID, request=req, input_hash=compute_input_hash(req.inputs),
        redaction_version=redaction_version,
        input_redaction=({"redacted_spans": spans, "sample_strip": sample_audits}
                         if (spans or sample_audits) else {}),
        raw_output={"output": outcome.output, "self_reported_scores": outcome.self_reported_scores},
        validation_result=outcome.validation_result, repair_attempts=list(outcome.repair_attempts),
        latency_ms=None, cost_metadata=outcome.cost_metadata, created_by=identity_to_jsonb(actor))
    dispatch_ref: str | None = None
    if dispatch_audit is not None and auditing_client is not None:
        # C5-T4/T6 eligibility ordering: record → link → return. A link failure DISCARDS the result.
        if not link_llm_call(llm_call_ref=llm_call_ref,
                             dispatch_refs=auditing_client.dispatch_refs,
                             ingestion_run_id=dispatch_audit.ingestion_run_id,
                             stage=dispatch_audit.stage):
            return None, None, None, "audit_degraded"
        # The pre-dispatch authorization the D1 candidate rows FK-link to (1014 llm_call_ref column →
        # llm_dispatch.dispatch_ref). Empty when no durable dispatch store was configured (no-DSN).
        refs = auditing_client.dispatch_refs
        dispatch_ref = refs[-1] if refs else None
    if outcome.status == STATUS_FAILED:
        # provider/repair/schema failure — no usable output; the recorded call proves egress happened.
        return None, llm_call_ref, dispatch_ref, outcome.status
    output = outcome.output if isinstance(outcome.output, dict) else None
    return output, llm_call_ref, dispatch_ref, outcome.status


# ==================================================================================================
# Apply the model's selection to the deterministic candidates (SELECT-only, confidence-as-evidence)
# ==================================================================================================
def _bounded_disposition(deterministic: str, model_disposition: str) -> tuple[str, str | None]:
    """Fold a model disposition onto a deterministic one. Confidence is NEVER promotion authority:
    the model may CONFIRM ``strong`` or DOWNGRADE to ``weak``, but can NEVER UPGRADE (a weak candidate
    stays weak). An off-vocab disposition keeps the deterministic value + a durable reason code."""
    if model_disposition not in (STRONG, WEAK):
        return deterministic, RC_LLM_DISPOSITION_OFF_VOCAB
    if deterministic == STRONG and model_disposition == STRONG:
        return STRONG, None
    return WEAK, None            # any weak involved → weak; never upgrade weak→strong


def _to_candidate_input(candidate: SemanticBindingCandidate, *, disposition: str,
                        llm_evidence: dict | None, extra_reason: str | None,
                        dispatch_ref: str | None) -> CandidateInput:
    """Map ONE candidate → D1's ``CandidateInput`` with the D3 provenance versions, the (possibly
    adjusted) disposition, the ``llm`` evidence overlay, and the pre-dispatch authorization link
    (``dispatch_ref`` → the 1014 ``llm_call_ref`` column's ``llm_dispatch`` FK; ``None`` in no-DSN
    mode). Confidence lives in ``evidence_json['llm']`` ONLY — never in ``proposed_value`` — so D2's
    ``to_fact_command`` can never copy it onto a governed fact."""
    if candidate.binding_kind == CURRENCY_BINDING:
        target = candidate.target
        target_graph = target.graph_ref if target is not None else None
        target_logical = target.logical_ref if target is not None else None
        proposed_value: object | None = None
    else:
        target_graph = None
        target_logical = None
        proposed_value = {"entity_id": candidate.entity_id}
    evidence = candidate.evidence.to_json()
    if llm_evidence is not None:
        evidence = {**evidence, "llm": llm_evidence}
    reason_codes = candidate.reason_codes
    if extra_reason and extra_reason not in reason_codes:
        reason_codes = (*reason_codes, extra_reason)
    return CandidateInput(
        binding_kind=candidate.binding_kind, subject_graph_ref=candidate.subject.graph_ref,
        subject_logical_ref=candidate.subject.logical_ref, input_hash=candidate.input_hash,
        disposition=disposition, model_version=_generation_settings().get("model", "unknown"),
        prompt_version=D3_PROMPT_VERSION, schema_version=D3_SCHEMA_VERSION,
        config_version=CONFIG_VERSION, target_graph_ref=target_graph,
        target_logical_ref=target_logical, proposed_value=proposed_value, reason_codes=reason_codes,
        evidence_json=evidence, llm_call_ref=dispatch_ref)


def _clamp_confidence(raw: object) -> float | None:
    if isinstance(raw, bool) or not isinstance(raw, (int, float)):
        return None
    return max(0.0, min(1.0, float(raw)))


# ==================================================================================================
# The public D3 entry — fail-soft, never raises into ingestion
# ==================================================================================================
def enrich_semantic_bindings(
    conn: DbConn,
    client: LLMClient,
    *,
    table_view: object,
    candidates: Sequence[SemanticBindingCandidate],
    catalog_source: str,
    ingestion_run_id: str,
    attempt_no: int,
    pass_b=None,
    pass_c=None,
    actor: IdentityEnvelope | None = None,
    bounds: enrich_config.SemanticBindingBounds | None = None,
    calls_remaining: int | None = None,
    deadline: datetime | None = None,
    now: datetime | None = None,
) -> EnrichResult:
    """Run the OPTIONAL semantic-binding LLM SELECTION for one table over D2's ``candidates``, persist
    the resulting immutable D3 candidate set, and return its truthful :class:`EnrichResult`.

    FAIL-SOFT / SEPARATE FAILURE DOMAIN: every provider / schema / deadline / egress failure yields a
    ``failed``/``partial`` set (never a crash); a persist error is caught and reported. This function
    NEVER raises — so it can never fail core ingestion or Pass B. Passing ``project`` is intentionally
    absent: D3 only writes the immutable candidate set; making an LLM set current is a D4 decision."""
    actor = actor or _ENRICH_ACTOR
    bounds = bounds or enrich_config.semantic_binding_bounds()
    now = now or datetime.now(UTC)
    fingerprint = table_fingerprint(table_view, pass_b=pass_b, pass_c=pass_c,
                                    shortlist_version=_D2_SHORTLIST_VERSION,
                                    config_version=_D2_CONFIG_VERSION)
    tgr = table_graph_ref(table_view)

    def _persist(status: str, inputs: list[CandidateInput]) -> str | None:
        try:
            res = persist_candidate_set(
                conn, catalog_source=catalog_source, table_graph_ref=tgr,
                ingestion_run_id=ingestion_run_id, attempt_no=attempt_no,
                metadata_input_fingerprint=fingerprint, task_version=TASK_VERSION,
                prompt_version=D3_PROMPT_VERSION, schema_version=D3_SCHEMA_VERSION,
                config_version=CONFIG_VERSION, completion_status=status,
                candidates=inputs, created_at=now)
            return res.candidate_set_id
        except Exception:  # noqa: BLE001 — a persist fault must never fail core ingestion / Pass B
            logger.warning("semantic-binding D3 persist failed for %s/%s (status=%s)",
                           catalog_source, tgr, status, exc_info=True)
            return None

    def _failed(reason: str, *, presented: int, ref: str | None = None) -> EnrichResult:
        # A failure persists a set HEADER with ZERO candidate rows — nothing derived from a provider
        # response is stored without a committed llm_call_ref.
        set_id = _persist("failed", [])
        return EnrichResult(completion_status="failed", candidate_set_id=set_id, llm_call_ref=ref,
                            presented=presented, selected=0, persisted=0, reason=reason)

    try:
        ordered = sorted(candidates, key=SemanticBindingCandidate.sort_key)
        presentable = [c for c in ordered if c.disposition != REJECTED]
        cap = bounds.max_candidates_per_table
        over_cap = max(0, len(presentable) - cap)
        capped = presentable[:cap]
        passthrough = [c for c in ordered if c not in set(capped)]  # rejected + over-cap overflow

        if not capped:
            # Nothing rankable — no dispatch needed; persist the deterministic passthrough as complete.
            inputs = [_to_candidate_input(c, disposition=c.disposition, llm_evidence=None,
                                          extra_reason=None, dispatch_ref=None) for c in passthrough]
            set_id = _persist("complete", inputs)
            return EnrichResult(completion_status="complete", candidate_set_id=set_id,
                                llm_call_ref=None, presented=0, selected=0, persisted=len(inputs))

        # ---- bounds that BLOCK dispatch (failed, no provider call) --------------------------------
        if calls_remaining is not None and calls_remaining <= 0:
            return _failed("provider_call_budget_exhausted", presented=len(capped))
        if bounds.deadline_s <= 0 or (deadline is not None and now >= deadline):
            return _failed("deadline_exceeded", presented=len(capped))

        # server-minted candidate ids (deterministic — persist re-derives the SAME ids).
        set_id_precomputed = mint_candidate_set_id(
            ingestion_run_id=ingestion_run_id, attempt_no=attempt_no, catalog_source=catalog_source,
            table_graph_ref=tgr, metadata_input_fingerprint=fingerprint, task_version=TASK_VERSION,
            prompt_version=D3_PROMPT_VERSION, schema_version=D3_SCHEMA_VERSION,
            config_version=CONFIG_VERSION)
        presented: list[tuple[str, SemanticBindingCandidate]] = [
            (mint_candidate_id(candidate_set_id=set_id_precomputed, binding_kind=c.binding_kind,
                               subject_graph_ref=c.subject.graph_ref,
                               target_graph_ref=c.target.graph_ref if c.target is not None else None,
                               input_hash=c.input_hash), c)
            for c in capped]
        by_id = {cid: c for cid, c in presented}

        payload = _safe_payload(table_view, presented)
        payload_bytes = len(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode("utf-8"))
        if payload_bytes > bounds.max_input_bytes:
            return _failed(f"input_bytes_exceeded ({payload_bytes} > {bounds.max_input_bytes})",
                           presented=len(capped))

        dispatch_audit = (DispatchAuditContext(
            ingestion_run_id=ingestion_run_id, stage=DISPATCH_STAGE,
            subjects=tuple(_dispatch_subjects(catalog_source, presented)))
            if ingestion_run_id else None)

        output, llm_call_ref, dispatch_ref, status = _audited_select_call(
            conn, client, catalog_metadata=payload, actor=actor, dispatch_audit=dispatch_audit)

        if output is None or llm_call_ref is None:
            # Egress block / provider / schema failure — no candidate persisted from the response.
            return _failed(f"llm_stage_{status}", presented=len(capped), ref=llm_call_ref)

        # ---- SELECT-only application ---------------------------------------------------------------
        selections = output.get("selections") or []
        selected_by_id: dict[str, dict] = {}
        dropped: list[tuple[str, str]] = []
        for sel in selections:
            if not isinstance(sel, dict):
                continue
            cid = sel.get("candidate_id")
            if not isinstance(cid, str) or cid not in by_id:
                # A model id NOT in the server shortlist — DROPPED with a durable reason code (never
                # persisted as a candidate). The invented id also survives in llm_call.raw_output.
                if isinstance(cid, str):
                    dropped.append((cid, RC_UNKNOWN_CANDIDATE_ID))
                continue
            selected_by_id[cid] = sel                       # last selection wins for a duplicate id

        inputs: list[CandidateInput] = []
        selected_count = 0
        for cid, cand in presented:
            sel = selected_by_id.get(cid)
            if sel is None:
                inputs.append(_to_candidate_input(cand, disposition=cand.disposition,
                                                  llm_evidence={"selected": False},
                                                  extra_reason=None, dispatch_ref=dispatch_ref))
                continue
            selected_count += 1
            disp, reason = _bounded_disposition(cand.disposition, str(sel.get("disposition", "")))
            confidence = _clamp_confidence(sel.get("confidence"))
            rationale = sel.get("rationale")
            llm_ev: dict[str, object] = {"selected": True, "model_disposition": sel.get("disposition"),
                                         "confidence": confidence}
            if isinstance(rationale, str):
                llm_ev["rationale"] = rationale[:500]
            inputs.append(_to_candidate_input(cand, disposition=disp, llm_evidence=llm_ev,
                                              extra_reason=reason, dispatch_ref=dispatch_ref))
        # over-cap + rejected passthrough: persisted (never dropped), marked not-evaluated.
        for cand in passthrough:
            inputs.append(_to_candidate_input(cand, disposition=cand.disposition,
                                              llm_evidence={"evaluated": False},
                                              extra_reason=None, dispatch_ref=dispatch_ref))

        completion = "partial" if over_cap else "complete"
        set_id = _persist(completion, inputs)
        return EnrichResult(
            completion_status=completion, candidate_set_id=set_id, llm_call_ref=llm_call_ref,
            presented=len(capped), selected=selected_count, persisted=len(inputs),
            dropped_unknown=tuple(dropped),
            reason=(f"candidate_cap_exceeded (over={over_cap})" if over_cap else None))
    except Exception:  # noqa: BLE001 — the SEPARATE failure domain: never fail core ingestion / Pass B
        logger.warning("semantic-binding D3 stage failed for %s/%s — isolated, reporting failed",
                       catalog_source, tgr, exc_info=True)
        return _failed("d3_stage_exception", presented=0)
