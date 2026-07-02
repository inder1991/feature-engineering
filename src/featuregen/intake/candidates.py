from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from featuregen.aggregates._append import provenance_for
from featuregen.contracts import DbConn, IdentityEnvelope
from featuregen.contracts.documents import NewDocument, Stage
from featuregen.documents.draft import DRAFT_CONTRACT_SCHEMA_VERSION
from featuregen.documents.store import append_document, compute_content_hash
from featuregen.idgen import mint_id
from featuregen.intake.llm import (
    LLMClient,
    LLMRequest,
    LLMResult,
    call_llm,
    current_llm_client,
)
from featuregen.intake.redaction import _first_pii, build_llm_inputs, current_intent_redactor

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
# advance_intake (P4, Task 9.5a) resolves it via `generate_candidates_for_run` (which calls
# current_candidate_generator()); the P1 conftest `candidate_generator` fixture and P9's `_wire`
# composition root register the concrete generator via register_candidate_generator(...) —
# register_sp2 (conn-less schema/catalog only) does NOT wire it.
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
            "(the _wire composition root does this in production)"
        )
    return _CANDIDATE_GENERATOR


CANDIDATES_PROMPT_ID = "sp2.generate_candidates"
CANDIDATES_PROMPT_VERSION = 1
CANDIDATES_OUTPUT_SCHEMA_ID = "sp2.generate_candidates.output"
CANDIDATES_OUTPUT_SCHEMA_VERSION = 1
STUB_GENERATOR_VERSION = "sp2-stub-candidate-generator@1"
MAX_CANDIDATES = 3
CANDIDATES_SCHEMA_OWNER = "featuregen-intake"

# The generate_candidates structured-output schema `call_llm` resolves + validates the ONE generation
# pass against (§9.1) — mirrors critique.py's CONTRACT_REVIEW_OUTPUT_SCHEMA. Lenient/additive on the
# item shape: the generator itself fail-closes per-item on a structurally-invalid `calculation_method`
# (`_as_tagged_method`), so the schema only asserts the envelope (a `candidates` array of objects) and
# leaves item-level enforcement to the deterministic normalizer. additionalProperties stays open.
CANDIDATES_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "required": ["candidates"],
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "definition_text": {"type": "string"},
                    "rationale": {"type": "string"},
                    "calculation_method": {"type": "object"},
                },
                "additionalProperties": True,
            },
        },
    },
    "additionalProperties": True,
}


def register_candidate_schemas(registry) -> None:
    """Register the generate_candidates structured-output schema in SP-0's document registry so
    call_llm can validate the hypothesis generation pass (§9.1). Idempotent (register_schema upserts).
    Mirrors register_critique_schemas; wire it into the SP-2 schema-registration path."""
    registry.register_schema(
        CANDIDATES_OUTPUT_SCHEMA_ID,
        CANDIDATES_OUTPUT_SCHEMA_VERSION,
        CANDIDATES_OUTPUT_SCHEMA,
        CANDIDATES_SCHEMA_OWNER,
    )

# Pinned, structured-output generation settings for the stub's single pass (part of the P3
# idempotency key). Structural-only — no PHI/PII in property names/enums/descriptions (§16 (c)).
_STUB_GENERATION_SETTINGS = {
    "provider": "fake",
    "model": "fake-structured",
    "thinking": "off",
    "max_tokens": 2048,
}


def _compose_intent(
    *, proposed_feature_name: object, target: object, entity: object
) -> str:
    """Render the draft's human-authored FREE-TEXT fields as ONE intent string for redaction (§9.4).
    Only these fields can carry PII; catalog concept NAMES ride `catalog_metadata` separately and
    never need scrubbing. The rendered text is what the redactor scrubs into the single LLM-safe
    `redacted_intent` — no un-redacted draft free-text is ever placed in LLMRequest.inputs."""
    parts = []
    if proposed_feature_name:
        parts.append(f"proposed feature name: {proposed_feature_name}")
    if target:
        parts.append(f"target: {target}")
    if entity:
        parts.append(f"entity: {entity}")
    return "; ".join(parts)


def _known_concepts(
    catalog_metadata: CatalogView, domain_context: DomainCatalogEntry | None
) -> set[str]:
    """The set of catalog concept NAMES the candidate may plausibly reference (§9.4: names only —
    never profiled values). Union of catalog object/column/concept names + the read-only per-use-case
    `DomainCatalogEntry.allowed_concepts` slice of the `BankingDomainCatalog` (§4.5, §7.2)."""
    names: set[str] = set()
    cm = catalog_metadata or {}
    for key in ("objects", "columns", "concepts"):
        for name in cm.get(key, ()) or ():
            if isinstance(name, str):
                names.add(name)
    if domain_context:
        for name in domain_context.get("allowed_concepts", ()) or ():
            if isinstance(name, str):
                names.add(name)
    return names


def _as_tagged_method(cm: object) -> dict | None:
    """Normalize an LLM-proposed calculation method into the tagged §4.2 shape, or None if its
    variant kind is not in the closed vocabulary (fail-closed per-item — never fabricate a method).
    A bare variant `{kind, ...}` is wrapped as `{method_version:1, chosen, considered:[chosen]}`."""
    if not isinstance(cm, Mapping):
        return None
    if "chosen" in cm:
        chosen = cm.get("chosen")
        method_version = cm.get("method_version", 1)
        considered = cm.get("considered")
    else:
        chosen = cm
        method_version = 1
        considered = None
    if not isinstance(chosen, Mapping) or chosen.get("kind") not in _METHOD_KINDS:
        return None
    chosen_d = dict(chosen)
    if isinstance(considered, list) and considered:
        considered_d = [dict(c) for c in considered if isinstance(c, Mapping)]
    else:
        considered_d = [chosen_d]
    return {"method_version": method_version, "chosen": chosen_d, "considered": considered_d}


class StubCandidateGenerator:
    """The deliberately-dumb SP-2 hypothesis generator (§7.2): ONE `LLMClient` structuring pass →
    1–3 candidate definitions, each with a one-line rationale, a tagged `calculation_method`, and
    cheap model-free `signals`. It has NO router, NO specialists, NO attempt/conceptual memory, NO
    symbolic synthesis, NO diversity/islands, and NO few-shot — those are SP-12 (design §14.6–14.9).
    It is domain-AWARE only via the read-only per-use-case `DomainCatalogEntry` allowed-concepts slice
    (§4.5), never the full generation prior. SP-2 MUST NOT import SP-12 scope. The `CandidateGenerator`
    seam is IDENTICAL for the stub and SP-12 — only this `generate` body changes."""

    def __init__(
        self, client: LLMClient, *, generator_version: str = STUB_GENERATOR_VERSION
    ) -> None:
        self._client = client
        self._generator_version = generator_version

    def generate(
        self,
        draft: DraftContract,
        catalog_metadata: CatalogView,
        domain_context: DomainCatalogEntry | None = None,
    ) -> list[Candidate]:
        known = _known_concepts(catalog_metadata, domain_context)
        semantics = draft.get("feature_semantics") or {}
        # §9.4 no-PII egress backstop. The candidate-gen free-text is composed from the draft's
        # ALREADY-STRUCTURED fields (proposed name / target / entity), which are clean-BY-CONSTRUCTION:
        # `_produce_draft` (commands.py) redacts the RAW intent BEFORE the LLM structures it, so the
        # LLM never saw raw PII. We therefore decide egress safety by scanning the ACTUAL composed
        # text being sent — NOT by inheriting the raw intent's stale origin classification. (Inheriting
        # it would send a `contains_pii`-ORIGIN draft whose composed fields are already clean into the
        # redactor's "contains_pii but nothing locatable to scrub → fail closed" branch: a FALSE
        # fail-closed that yields zero candidates for a legitimate class of runs.)
        #
        # Fail SAFE on an origin we cannot trust as clean-by-construction (`unscanned` / missing /
        # unknown): only `clean` and `contains_pii` origins went through _produce_draft's redact-then-
        # structure path. Anything else → no LLM pass, no candidates (never send unscanned/unknown content).
        if draft.get("raw_input_classification") not in ("clean", "contains_pii"):
            return []  # fail closed: untrusted origin → no candidates (the run stays in clarification)
        composed = _compose_intent(
            proposed_feature_name=draft.get("proposed_feature_name"),
            target=draft.get("target"),
            entity=semantics.get("entity"),
        )
        # Scan the ACTUAL composed text with redaction's OWN scanner (the same `_first_pii` that
        # assert_llm_safe / critique use). The fields are clean-by-construction, so residual PII here is
        # an UPSTREAM redaction breach — fail closed, never dispatch it (even redacted) to the LLM.
        if _first_pii(composed) is not None:
            return []  # fail closed: PII in composed draft text → no LLM pass, no candidates
        # Composed text is clean-by-construction → build the reserved LLM-safe request classified
        # `clean`; call_llm's own `_first_pii` egress backstop still re-guards the whole payload (DiD).
        redaction = current_intent_redactor().redact(composed, "clean")
        if redaction.text is None or redaction.disposition != "ok":
            return []  # fail closed: no LLM-safe intent → no candidates (the run stays in clarification)
        inputs = build_llm_inputs(
            redaction,
            catalog_metadata={"allowed_concepts": sorted(known)},  # catalog NAMES only (§9.4)
            raw_input_classification="clean",
        )
        inputs["intake_mode"] = draft.get("intake_mode")  # structural enum context (no data values)
        request = LLMRequest(
            task="generate_candidates",
            prompt_id=CANDIDATES_PROMPT_ID,
            prompt_version=CANDIDATES_PROMPT_VERSION,
            inputs=inputs,
            output_schema_id=CANDIDATES_OUTPUT_SCHEMA_ID,
            output_schema_version=CANDIDATES_OUTPUT_SCHEMA_VERSION,
            generation_settings=_STUB_GENERATION_SETTINGS,
        )
        result = self._client.call(request)  # THE single LLM pass (§7.2)
        if result.status == "failed_into_clarification":
            return []  # fail closed: no candidates → the run stays in clarification (never fabricate)
        raw = list((result.output or {}).get("candidates", []))[:MAX_CANDIDATES]
        call_refs = [result.call_ref] if result.call_ref else []
        candidates: list[Candidate] = []
        sibling_methods: list[dict] = []
        for item in raw:
            if not isinstance(item, Mapping):
                continue
            method = _as_tagged_method(item.get("calculation_method"))
            if method is None:
                continue  # skip a structurally-invalid variant (fail-closed per-item)
            signals = candidate_signals(
                method,
                item.get("definition_text", ""),
                known_concepts=known,
                sibling_methods=sibling_methods,
            )
            candidates.append(
                Candidate(
                    candidate_id=mint_id("cand"),
                    definition_text=item.get("definition_text", ""),
                    rationale=item.get("rationale", ""),
                    calculation_method=method,
                    signals=signals,
                    provenance={
                        "llm_call_refs": list(call_refs),
                        "generator_version": self._generator_version,
                    },
                )
            )
            sibling_methods.append(method)
        return candidates


@dataclass(frozen=True)
class RecordingLLMClient:
    """Binds SP-2's event-sourced `call_llm` envelope (P3) to the pure `LLMClient.call` seam so a
    `CandidateGenerator` — which only ever sees `client.call` — still writes the immutable `llm_call`
    record + emits `LLM_CALL_RECORDED` for its ONE generation pass. Constructed per-run (conn/run_id/
    actor captured here) so the generator stays db-agnostic and the seam stays stable SP-2 → SP-12.
    `call` returns `call_llm`'s `LLMResult`, whose `call_ref` is the real event-sourced record id.

    This is a GENERIC bridge: it forwards the request UNCHANGED. Making the request LLM-safe
    (reserved-keyed, redacted per §9.4) is the caller/generator's job — `StubCandidateGenerator`
    builds the reserved shape via `build_llm_inputs` above; `call_llm` then re-guards egress."""

    conn: DbConn
    inner: LLMClient
    run_id: str
    actor: IdentityEnvelope

    def call(self, request: LLMRequest) -> LLMResult:
        return call_llm(self.conn, self.inner, request, run_id=self.run_id, actor=self.actor)


# Candidates are candidate-role documents UNDER the run's Draft stage (§7.1) — the stage enum is
# not extended; `branch_role` distinguishes a candidate from the primary Draft.
_CANDIDATE_STAGE = Stage.DRAFT_CONTRACT.value


def _persist_contract_body(conn: DbConn, *, body: dict) -> tuple[str, str]:
    """Freeze a governance-retained contract body BY REFERENCE (§3.4, §4.3): canonical-JSON
    content-hash + a live `blob_index` row. The document row stores `body_ref` + `content_hash`
    only (opaque-by-reference) — the body itself lives in the object store keyed by `body_ref`.
    Governance-retained bodies are needed for MRM reproduction / adverse-action explainability."""
    raw = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    content_hash = compute_content_hash(raw)
    body_ref = mint_id("blob")
    conn.execute(
        "INSERT INTO blob_index "
        "  (blob_id, object_key, content_hash, classification, referenced, status, size_bytes) "
        "VALUES (%s, %s, %s, 'governance-retained', true, 'live', %s)",
        (body_ref, f"contracts/{body_ref}.json", content_hash, len(raw)),
    )
    return body_ref, content_hash


def write_candidate_docs(
    conn: DbConn,
    *,
    candidates: list[Candidate],
    draft_doc_id: str,
    run_id: str,
    request_id: str,
    actor: IdentityEnvelope,
) -> tuple[str, ...]:
    """Freeze each candidate as a candidate-role `DRAFT_CONTRACT` staged document under the run's
    Draft stage (§7.1): `branch_role="candidate"`, `derived_from=(draft_doc_id,)`,
    `body_classification="governance-retained"`, body opaque-by-reference. Returns the candidate
    `doc_id`s in generation order. Documents are write-once — the Gate #1 `PRIMARY_SELECTED`
    promotion (Task 6.5) later picks ONE; the losers are simply left in place."""
    doc_ids: list[str] = []
    for c in candidates:
        body = {
            "request_id": request_id,
            "candidate_id": c.candidate_id,
            "definition_text": c.definition_text,
            "rationale": c.rationale,
            "calculation_method": c.calculation_method,
            "signals": c.signals,
            "provenance": c.provenance,
        }
        body_ref, content_hash = _persist_contract_body(conn, body=body)
        doc_id = mint_id("doc")
        append_document(
            conn,
            NewDocument(
                doc_id=doc_id,
                stage=_CANDIDATE_STAGE,
                schema_version=DRAFT_CONTRACT_SCHEMA_VERSION,
                branch_role="candidate",
                content_hash=content_hash,
                body_classification="governance-retained",
                provenance=provenance_for(
                    artifact_type=_CANDIDATE_STAGE,
                    external_refs=tuple(c.provenance.get("llm_call_refs", ()) or ()),
                ),
                body_ref=body_ref,
                derived_from=(draft_doc_id,),
            ),
            run_id=run_id,
            request_id=request_id,
            actor=actor,
        )
        doc_ids.append(doc_id)
    return tuple(doc_ids)


def generate_candidate_docs(
    conn: DbConn,
    generator: CandidateGenerator,
    *,
    draft: DraftContract,
    catalog_metadata: CatalogView,
    domain_context: DomainCatalogEntry | None,
    draft_doc_id: str,
    run_id: str,
    request_id: str,
    actor: IdentityEnvelope,
) -> tuple[str, ...]:
    """Hypothesis-mode orchestrator `generate_candidates_for_run` (Task 9.5a) calls after the primary
    Draft is frozen: run the (event-sourced, `RecordingLLMClient`-wrapped) generator → freeze each
    candidate as a candidate-role Draft document → return the candidate `doc_id`s (recorded on the
    `CANDIDATES_GENERATED` shadow). Empty ⟹ generation failed closed → the run stays in clarification
    (§7.2). Generator-agnostic: this orchestration is IDENTICAL for the stub and SP-12."""
    candidates = generator.generate(draft, catalog_metadata, domain_context)
    if not candidates:
        return ()
    return write_candidate_docs(
        conn,
        candidates=candidates,
        draft_doc_id=draft_doc_id,
        run_id=run_id,
        request_id=request_id,
        actor=actor,
    )


def _with_recording_client(
    generator: CandidateGenerator, recording: RecordingLLMClient
) -> CandidateGenerator:
    """Rebind the registered generator's ONE LLM pass to the per-run, event-sourcing
    `RecordingLLMClient` so `generate_candidates` writes an auditable `LLM_CALL_RECORDED` (§9.1/§9.3).
    SP-2 ships exactly one concrete generator (`StubCandidateGenerator`); rebind preserves its pinned
    `generator_version`. A future (SP-12) generator that manages its own event-sourced client is
    returned unchanged (it already records)."""
    if isinstance(generator, StubCandidateGenerator):
        return StubCandidateGenerator(recording, generator_version=generator._generator_version)
    return generator


def generate_candidates_for_run(
    conn: DbConn,
    *,
    draft: DraftContract,
    catalog_metadata: CatalogView,
    domain_context: DomainCatalogEntry | None,
    draft_doc_id: str,
    run_id: str,
    request_id: str,
    actor: IdentityEnvelope,
) -> tuple[str, ...]:
    """The PRODUCTION hypothesis-mode candidate-generation entry point `advance_intake` (Task 9.5a)
    calls once a primary Draft exists but no candidates do (§7.2, closes gap B). Resolves the registered
    `CandidateGenerator` (fail-closed via `current_candidate_generator()` — never silently zero
    candidates on an unwired seam), rebinds its ONE LLM pass to the per-run event-sourcing
    `RecordingLLMClient(conn, inner=current_llm_client(), run_id, actor)` so the generation pass is an
    auditable `LLM_CALL_RECORDED`, then runs `generate_candidate_docs` to freeze 1–3 candidate-role
    Draft docs. Returns the candidate `doc_id`s (empty ⟹ generation failed closed → the caller
    fail-closed-parks; never a silent zero-candidate MCV pass)."""
    generator = current_candidate_generator()  # fail-closed if the seam is unwired (§7.2)
    recording = RecordingLLMClient(
        conn=conn, inner=current_llm_client(), run_id=run_id, actor=actor
    )
    return generate_candidate_docs(
        conn,
        _with_recording_client(generator, recording),
        draft=draft,
        catalog_metadata=catalog_metadata,
        domain_context=domain_context,
        draft_doc_id=draft_doc_id,
        run_id=run_id,
        request_id=request_id,
        actor=actor,
    )
