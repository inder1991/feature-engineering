# SP-2 — Phase 6 — Hypothesis flow (`CandidateGenerator` seam + stub + `select_candidate_doc`) (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Global Constraints + Shared Contract:** see [sp2-00-overview.md](2026-07-01-sp2-00-overview.md) (authoritative). Design spec §7 (the `CandidateGenerator` seam), §3.2 (hypothesis mode), §8.1/§8.3 (Gate #1 candidate selection). When a signature here and the overview disagree, the **overview wins**.

---

This phase builds SP-2's **hypothesis-mode** flow: the **`CandidateGenerator` seam** SP-12 later fills, the
deliberately-dumb single-call **`StubCandidateGenerator`**, the cheap **model-free candidate signals** (§7.3),
the candidate-role **staged-document freezing**, and the **`select_candidate_doc`** command built on the SP-0
**document `PRIMARY_SELECTED`** primitive (`documents/primary.py::new_primary_selected`) — **not** the
request-level `select_candidate` (`request_aggregate.py`, wrong granularity, §7.1). Everything is generator-agnostic:
**the document/selection machinery is identical for the stub and for SP-12 — only `generate`'s body changes**
(§7.2). SP-2 **must not import SP-12 scope** (no router, no specialists, no attempt/conceptual memory, no
symbolic synthesis, no diversity/islands, no few-shot — those are SP-12, design §14.6–14.9). Definition mode has
**no** generation (§3.1) — this phase is exercised only in hypothesis mode.

**The SP-12 boundary (load-bearing).** The stub makes **exactly one** `LLMClient` pass → **1–3** candidate
definitions, each compiled to a tagged `calculation_method` (§4.2) with a one-line rationale and **cheap,
model-free `signals`** (does it reference a known catalog concept? is the window sane? is it a duplicate of a
sibling on this run?). It carries **no** predictive scoring — **no IV/WoE, no AUC, no overfitting-guard result**
(those need a point-in-time labelled sample and live in SP-5/SP-7). It has **no** learning loop (stateless across
runs) and **no** gate bypass (candidates flow through the normal pipeline + gates; the generator *proposes only*).
Each candidate is a **candidate-role `DRAFT_CONTRACT` staged document** under the run's Draft stage; the Gate #1
selection is a **document-candidate `PRIMARY_SELECTED`** promotion recording **only the chosen** doc — the losing
sibling docs are **write-once and left untouched** (no per-doc reject event); their `doc_id`s are captured
**only** in the Gate #1 confirmation record (§8.3, wired in P7).

**Cross-phase Consumes (built earlier; used verbatim here):**
- **SP-0 documents:** `documents/store.py::append_document(conn, new_document, *, run_id=None, feature_id=None, request_id=None, actor) -> str` (single validated write path; validates stage/branch_role/classification vocab; `derived_from` must reference committed docs), `compute_content_hash(body: bytes) -> str` (`"sha256:<hex>"`), `get_document(conn, doc_id) -> dict | None`; `documents/primary.py::new_primary_selected(*, run_id, stage, doc_id, actor, provenance, caused_by=None) -> NewEvent` (payload `{doc_id, stage}`, aggregate `"run"`), `register_primary_selected(conn)`, `current_primary(conn, run_id, stage) -> str | None`, `StagePrimaryProjection`; `contracts/documents.py::Stage` (`Stage.DRAFT_CONTRACT`, published — not extended), `NewDocument(doc_id, stage, schema_version, branch_role, content_hash, body_classification, provenance, body_ref=None, derived_from=(), supersedes=(), reject_reason=None)`, `BRANCH_ROLES=("candidate","primary","rejected","repair")`, `BODY_CLASSIFICATIONS=("pii-erasable","governance-retained")`.
- **SP-0 append/OCC + provenance:** `aggregates/_append.py::current_version(conn, aggregate, aggregate_id) -> int`, `table_version_for(conn, aggregate, aggregate_id) -> int`, `provenance_for(artifact_type=..., **extra) -> ProvenanceEnvelope`; `events/store.py::append_event(conn, new_event, *, expected_version, table_version) -> EventEnvelope`, `load_stream(conn, aggregate, aggregate_id)`.
- **SP-0 run/request setup (tests only):** `aggregates/request_aggregate.py::create_request_command(conn, cmd)` (args `feature_concept`, `intake_mode?`), `create_run_command(conn, cmd)` (args `request_id`); `identity/build.py::build_human_identity`/`build_service_identity`; `idgen.py::mint_id(prefix) -> str` (R14 — the ONE id minter; NOT `ids.new_id`); `contracts::Command`/`CommandResult`/`DbConn`/`IdentityEnvelope`; `contracts.documents::NewDocument`.
- **SP-0 security (R15):** `security/audit.py::record_denial(conn, cmd, reason) -> str` — the single SP-2 denial helper (routes a `COMMAND_DENIED` to the tamper-evident security-audit stream with `decision="denied"`, `attempted_action=cmd.action`). SP-2 **never** calls `record_security_event(..., decision="deny")` directly.
- **P1:** `PRIMARY_SELECTED` is registered for candidate promotion (`register_primary_selected` wired in `seed_sp2_authz`); the additive `("select_candidate_doc","","data_scientist","human",None)` authz row is seeded there. Tests call `register_primary_selected(db)` directly (mirrors `tests/featuregen/documents/test_primary.py`).
- **P2:** `intake/contract.py` — the tagged `calculation_method` shape (§4.2 `CONFIRMED_CONTRACT` `$defs.calculation_method`): `{method_version:int, chosen:method_variant, considered:[method_variant]}`; closed `chosen.kind`/`considered[].kind` vocabulary `{rolling_aggregate, point_snapshot, ratio, distribution_divergence}`. `documents/draft.py::DRAFT_CONTRACT_SCHEMA_VERSION` (=1).
- **P3:** `intake/llm.py` — `LLMClient` Protocol (`call(request: LLMRequest) -> LLMResult`), `LLMRequest(task, prompt_id, prompt_version, inputs, output_schema_id, output_schema_version, generation_settings)`, `LLMResult(output: dict, self_reported_scores: dict, call_ref: str, status: str)`, and `call_llm(conn, client: LLMClient, request: LLMRequest, *, run_id: str, actor: IdentityEnvelope) -> LLMResult` (egress-guards, records the `llm_call`, emits `LLM_CALL_RECORDED`).
- **P4:** `intake/commands.py` exists with `_SP2_CATALOG` (tuple of `(action, handler)`) + idempotent `register_sp2_commands()`; `submit_intent` freezes the primary `DRAFT_CONTRACT` doc and, in hypothesis mode, calls this phase's `generate_candidate_docs(...)` after the Draft is frozen (its returned candidate `doc_id`s ride the `DRAFT_CONTRACT_PRODUCED` event). This phase **appends** `select_candidate_doc` to that catalog.
- **P2 (`intake/state.py`, R3/R4):** `fold_feature_contract_state(stream) -> FeatureContractState` + the ONE canonical request-owner predicate `actor_is_request_owner(state, actor) -> bool` (state-based; `state.requester` is the `INTENT_SUBMITTED` event `actor.subject`). P6 folds the `feature_contract` stream and calls `actor_is_request_owner(state, cmd.actor)` — **NOT** a `(conn, run_id, actor)` form and **NOT** from `intake/mcv.py`. Single-sourcing the owner predicate in `state.py` prevents drift with `confirm_contract`'s `confirmer_is_requester_human` guard (P7).
- **P1 (`intake/store.py`, R1):** `load_feature_contract(conn, run_id) -> list[EventEnvelope]` — the `feature_contract` load seam P6 folds for the owner guard (imported verbatim; never redefined).

**Package layout:** all new code lives in `src/featuregen/intake/candidates.py` (new) and an append to `src/featuregen/intake/commands.py`; tests under `tests/featuregen/intake/`.

---

### Task 6.1: `candidates.py` — the `Candidate`/`CandidateGenerator` seam + cheap model-free signals

**Files:**
- Create: `src/featuregen/intake/candidates.py`
- Create: `tests/featuregen/intake/__init__.py` (empty, if absent)
- Test: `tests/featuregen/intake/test_candidates_seam.py`

**Interfaces:**
- Consumes: nothing beyond stdlib (`collections.abc.Mapping`, `dataclasses`, `typing`). Pure — **no db, no LLM**.
- Produces:
  - `Candidate` — frozen dataclass `{candidate_id: str, definition_text: str, rationale: str, calculation_method: dict, signals: dict, provenance: dict}` (the §7.1 candidate schema; `calculation_method` is the tagged `{method_version, chosen, considered}` dict of §4.2 that SP-3 consumes deterministically by switching on `chosen.kind`).
  - `CandidateGenerator` — a `@runtime_checkable` `Protocol` with `generate(self, draft: DraftContract, catalog_metadata: CatalogView, domain_context: DomainCatalogEntry | None) -> list[Candidate]` (the **stable seam** SP-12 binds its real engine to; **only** the `generate` body changes SP-2→SP-12).
  - Type aliases `DraftContract = Mapping[str, Any]`, `CatalogView = Mapping[str, Any]`, `DomainCatalogEntry = Mapping[str, Any]` (structural-only metadata views — names/types/grain + declared enum/code metadata, **never** data values, §9.4).
  - `candidate_signals(calculation_method: dict, definition_text: str, *, known_concepts: set[str], sibling_methods: list[dict]) -> dict` — **cheap, model-free** plausibility signals ONLY (§7.3): `references_known_concept`, `window_sane`, `duplicate_of_sibling`, a heuristic `heuristic_rank ∈ [0,1]`, and `scored_by="cheap_model_free_heuristic"`. **Never** IV/WoE/AUC/overfitting.
  - `_METHOD_KINDS` (closed method-variant vocabulary, mirrors §4.0), and helpers `_variant_concept`, `_window_days`, `_window_is_sane`, `_same_variant`.
  - **R10** the module-global collaborator DI seam **owned by P6**: `register_candidate_generator(generator) -> None` / `current_candidate_generator() -> CandidateGenerator` (fail-closed `RuntimeError` if unset). Mirrors SP-0's `overlay/catalog.py::register_catalog_adapter`/`current_catalog_adapter`. This is the ONLY holder — `submit_intent` (P4) resolves the generator via `current_candidate_generator()`; the P1 conftest `candidate_generator` fixture and P9's `register_sp2` wire it via `register_candidate_generator(...)`.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_candidates_seam.py
import dataclasses

import pytest

from featuregen.intake.candidates import (
    Candidate,
    CandidateGenerator,
    candidate_signals,
    current_candidate_generator,
    register_candidate_generator,
)


def _method(chosen: dict) -> dict:
    return {"method_version": 1, "chosen": chosen, "considered": [chosen]}


def test_candidate_is_frozen_with_the_seam_fields():
    c = Candidate(
        candidate_id="cand_1",
        definition_text="count of distinct MCCs, last 30d minus prior 30d",
        rationale="category churn precedes financial distress",
        calculation_method=_method({"kind": "ratio", "numerator": "a", "denominator": "b"}),
        signals={},
        provenance={},
    )
    assert c.candidate_id == "cand_1"
    assert dataclasses.is_dataclass(c)
    with pytest.raises(dataclasses.FrozenInstanceError):
        c.definition_text = "mutated"  # write-once seam object


def test_signals_are_cheap_model_free_only_no_predictive_power():
    method = _method(
        {"kind": "rolling_aggregate", "aggregation": "count", "window": "90d",
         "filter": {"concept": "declined_auth"}}
    )
    s = candidate_signals(method, "count of declined auths",
                          known_concepts={"declined_auth"}, sibling_methods=[])
    assert s["references_known_concept"] is True
    assert s["window_sane"] is True
    assert s["duplicate_of_sibling"] is False
    assert 0.0 <= s["heuristic_rank"] <= 1.0
    assert s["scored_by"] == "cheap_model_free_heuristic"
    # §7.3 boundary: NO measured-predictive-power keys may ever appear
    keys = {k.lower() for k in s}
    for banned in ("iv", "woe", "auc", "information_value", "gini", "ks", "overfitting"):
        assert banned not in keys


def test_unknown_concept_and_insane_window_lower_signals():
    method = _method(
        {"kind": "rolling_aggregate", "aggregation": "count", "window": "9999d",
         "filter": {"concept": "mystery_concept"}}
    )
    s = candidate_signals(method, "", known_concepts={"declined_auth"}, sibling_methods=[])
    assert s["references_known_concept"] is False
    assert s["window_sane"] is False  # 9999d > 3y ceiling


def test_duplicate_of_a_sibling_is_flagged():
    chosen = {"kind": "distribution_divergence", "measure": "jensen_shannon",
              "window": "30d", "baseline_window": "180d"}
    method = _method(chosen)
    s = candidate_signals(method, "JS divergence of category spend",
                          known_concepts=set(), sibling_methods=[_method(chosen)])
    assert s["duplicate_of_sibling"] is True


def test_candidategenerator_is_a_runtime_checkable_protocol():
    class _G:
        def generate(self, draft, catalog_metadata, domain_context=None):
            return []

    assert isinstance(_G(), CandidateGenerator)  # structural conformance = the stable seam
    assert not isinstance(object(), CandidateGenerator)


def test_candidate_generator_di_seam_registers_and_resolves():
    # R10 — P6 OWNS the module-global register/current CandidateGenerator seam (fail-closed if unset).
    class _G:
        def generate(self, draft, catalog_metadata, domain_context=None):
            return []

    g = _G()
    register_candidate_generator(g)
    assert current_candidate_generator() is g  # last-writer-wins, mirrors register_catalog_adapter
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_candidates_seam.py -v`
  - Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.intake.candidates'`.

- [ ] **Step 3 — minimal implementation**

```python
# src/featuregen/intake/candidates.py
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
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_candidates_seam.py -v`
  - Expected: PASS (6 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/intake/candidates.py tests/featuregen/intake/__init__.py tests/featuregen/intake/test_candidates_seam.py && git commit -m "feat(intake): CandidateGenerator seam + Candidate schema + cheap model-free signals"`

---

### Task 6.2: `candidates.py` — `StubCandidateGenerator` (deliberately-dumb single LLM pass)

**Files:**
- Modify: `src/featuregen/intake/candidates.py` (append imports + the stub)
- Test: `tests/featuregen/intake/test_stub_generator.py`

**Interfaces:**
- Consumes: `LLMClient`/`LLMRequest`/`LLMResult` (P3 `intake/llm.py`); `mint_id` (`featuregen.idgen`, R14); the seam + `candidate_signals`/`_METHOD_KINDS` from Task 6.1.
- Produces:
  - Module constants: `CANDIDATES_PROMPT_ID = "sp2.generate_candidates"`, `CANDIDATES_PROMPT_VERSION = 1`, `CANDIDATES_OUTPUT_SCHEMA_ID = "sp2.generate_candidates.output"`, `CANDIDATES_OUTPUT_SCHEMA_VERSION = 1`, `STUB_GENERATOR_VERSION = "sp2-stub-candidate-generator@1"`, `MAX_CANDIDATES = 3`.
  - `StubCandidateGenerator(client: LLMClient, *, generator_version: str = STUB_GENERATOR_VERSION)` implementing `CandidateGenerator.generate` — **exactly one** `client.call(...)` (task `"generate_candidates"`) → **1–3** `Candidate`s. Fail-closed: `status == "failed_into_clarification"` → `[]`; a structurally-invalid method variant (kind ∉ `_METHOD_KINDS`, or missing) is **skipped** per-item (never fabricated). Clamps to `MAX_CANDIDATES`. **No** router/specialists/memory/symbolic/diversity/few-shot (the SP-12 boundary, §7.2).
  - Helpers `_known_concepts(catalog_metadata, domain_context) -> set[str]`, `_as_tagged_method(cm) -> dict | None`.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_stub_generator.py
from featuregen.intake.candidates import (
    CANDIDATES_OUTPUT_SCHEMA_ID,
    CANDIDATES_PROMPT_ID,
    STUB_GENERATOR_VERSION,
    StubCandidateGenerator,
)
from featuregen.intake.llm import LLMResult


class _ScriptedLLM:
    """A minimal LLMClient test double (structurally an LLMClient) that returns a scripted output
    and counts calls — so the 'exactly one LLM pass' invariant is directly assertable."""

    def __init__(self, output, *, status="ok", call_ref="llmc_stub_1"):
        self.output = output
        self.status = status
        self.call_ref = call_ref
        self.calls = 0
        self.last_request = None

    def call(self, request):
        self.calls += 1
        self.last_request = request
        return LLMResult(
            output=self.output, self_reported_scores={}, call_ref=self.call_ref, status=self.status
        )


# The §3.2 / Appendix-B hypothesis example: abrupt spending-category shift → credit risk.
_THREE = {
    "candidates": [
        {"definition_text": "count of distinct MCCs, last 30d minus prior 30d",
         "rationale": "category churn precedes financial distress",
         "calculation_method": {"kind": "rolling_aggregate", "aggregation": "distinct_count",
                                "window": "30d", "filter": {"concept": "merchant_category_code"}}},
        {"definition_text": "share of spend in top-1 category vs 3-month average",
         "rationale": "concentration shift signals stress",
         "calculation_method": {"kind": "ratio", "numerator": "top_category_spend",
                                "denominator": "total_spend", "window": "30d"}},
        {"definition_text": "JS divergence of this month's category-spend vs trailing 6-month",
         "rationale": "whole-distribution shift is a richer signal",
         "calculation_method": {"kind": "distribution_divergence", "measure": "jensen_shannon",
                                "window": "30d", "baseline_window": "180d"}},
    ]
}
_DRAFT = {"intake_mode": "hypothesis", "proposed_feature_name": "abrupt_category_shift",
          "target": "higher credit risk", "feature_semantics": {}}
_CATALOG = {"concepts": ["merchant_category_code", "total_spend", "top_category_spend"]}
_DOMAIN = {"allowed_concepts": ["merchant_category_code"]}


def test_single_pass_yields_three_scored_candidates():
    llm = _ScriptedLLM(_THREE)
    cands = StubCandidateGenerator(llm).generate(_DRAFT, _CATALOG, _DOMAIN)
    assert llm.calls == 1  # deliberately ONE LLM pass (§7.2)
    assert len(cands) == 3
    assert [c.calculation_method["chosen"]["kind"] for c in cands] == [
        "rolling_aggregate", "ratio", "distribution_divergence"
    ]
    for c in cands:
        assert c.candidate_id.startswith("cand_")
        assert c.rationale  # surfaced at Gate #1 (§8.1)
        assert c.calculation_method["method_version"] == 1
        assert c.provenance["llm_call_refs"] == ["llmc_stub_1"]
        assert c.provenance["generator_version"] == STUB_GENERATOR_VERSION
        assert "heuristic_rank" in c.signals  # cheap model-free signals attached
    # the one pass is the registered, versioned generate_candidates call
    assert llm.last_request.task == "generate_candidates"
    assert llm.last_request.prompt_id == CANDIDATES_PROMPT_ID
    assert llm.last_request.output_schema_id == CANDIDATES_OUTPUT_SCHEMA_ID


def test_clamps_to_at_most_three_candidates():
    many = {"candidates": _THREE["candidates"] + [
        {"definition_text": "extra", "rationale": "x",
         "calculation_method": {"kind": "point_snapshot", "field": "balance"}}]}
    cands = StubCandidateGenerator(_ScriptedLLM(many)).generate(_DRAFT, _CATALOG, None)
    assert len(cands) == 3  # 1..3 (§3.2)


def test_failed_into_clarification_yields_no_candidates():
    llm = _ScriptedLLM(_THREE, status="failed_into_clarification")
    assert StubCandidateGenerator(llm).generate(_DRAFT, _CATALOG, None) == []
    assert llm.calls == 1  # still exactly one pass; it just failed closed


def test_structurally_invalid_variant_is_skipped_never_fabricated():
    bad = {"candidates": [
        {"definition_text": "good", "rationale": "r",
         "calculation_method": {"kind": "ratio", "numerator": "a", "denominator": "b"}},
        {"definition_text": "unknown kind", "rationale": "r",
         "calculation_method": {"kind": "neural_net"}},          # not a closed kind → dropped
        {"definition_text": "no method", "rationale": "r"},         # missing method → dropped
    ]}
    cands = StubCandidateGenerator(_ScriptedLLM(bad)).generate(_DRAFT, _CATALOG, None)
    assert len(cands) == 1
    assert cands[0].calculation_method["chosen"]["kind"] == "ratio"


def test_bare_variant_is_wrapped_into_the_tagged_shape():
    one = {"candidates": [
        {"definition_text": "declined auth count 90d", "rationale": "faithful",
         "calculation_method": {"kind": "rolling_aggregate", "aggregation": "count",
                                "window": "90d", "filter": {"concept": "declined_auth"}}}]}
    cands = StubCandidateGenerator(_ScriptedLLM(one)).generate(_DRAFT, {"concepts": ["declined_auth"]}, None)
    m = cands[0].calculation_method
    assert set(m) >= {"method_version", "chosen", "considered"}  # §4.2 tagged shape
    assert m["considered"] == [m["chosen"]]
    assert cands[0].signals["references_known_concept"] is True
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_stub_generator.py -v`
  - Expected: FAIL — `ImportError: cannot import name 'StubCandidateGenerator' from 'featuregen.intake.candidates'`.

- [ ] **Step 3 — minimal implementation** (append to `candidates.py`; add the two imports at the top)

Add to the imports:

```python
from featuregen.idgen import mint_id
from featuregen.intake.llm import LLMClient, LLMRequest
```

Append the stub:

```python
CANDIDATES_PROMPT_ID = "sp2.generate_candidates"
CANDIDATES_PROMPT_VERSION = 1
CANDIDATES_OUTPUT_SCHEMA_ID = "sp2.generate_candidates.output"
CANDIDATES_OUTPUT_SCHEMA_VERSION = 1
STUB_GENERATOR_VERSION = "sp2-stub-candidate-generator@1"
MAX_CANDIDATES = 3

# Pinned, structured-output generation settings for the stub's single pass (part of the P3
# idempotency key). Structural-only — no PHI/PII in property names/enums/descriptions (§16 (c)).
_STUB_GENERATION_SETTINGS = {
    "provider": "fake",
    "model": "fake-structured",
    "thinking": "off",
    "max_tokens": 2048,
}


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
        request = LLMRequest(
            task="generate_candidates",
            prompt_id=CANDIDATES_PROMPT_ID,
            prompt_version=CANDIDATES_PROMPT_VERSION,
            inputs={
                # Redacted, LLM-safe metadata ONLY (§9.4) — names, the proposed name, the (redacted)
                # target label, and the allowed concept NAMES. Never data values / profiled sets.
                "proposed_feature_name": draft.get("proposed_feature_name"),
                "intake_mode": draft.get("intake_mode"),
                "target": draft.get("target"),
                "entity": semantics.get("entity"),
                "allowed_concepts": sorted(known),
            },
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
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_stub_generator.py -v`
  - Expected: PASS (5 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/intake/candidates.py tests/featuregen/intake/test_stub_generator.py && git commit -m "feat(intake): StubCandidateGenerator — deliberately-dumb single-pass 1-3 candidates (SP-12 boundary)"`

---

### Task 6.3: `candidates.py` — `RecordingLLMClient` (bind the event-sourced envelope to the pure seam)

**Files:**
- Modify: `src/featuregen/intake/candidates.py` (append imports + the bridge)
- Test: `tests/featuregen/intake/test_recording_client.py`

**Interfaces:**
- Consumes: `call_llm(conn, client, request, *, run_id, actor) -> LLMResult`, `LLMClient`, `LLMRequest`, `LLMResult` (P3 `intake/llm.py`); `DbConn`/`IdentityEnvelope` (`featuregen.contracts`).
- Produces: `RecordingLLMClient(conn, inner, run_id, actor)` — a frozen dataclass implementing `LLMClient.call(request) -> LLMResult` by routing every call through P3's event-sourced `call_llm` (writes the `llm_call` record + emits `LLM_CALL_RECORDED`). This is how a `CandidateGenerator` — which only ever sees `client.call` — event-sources its ONE generation pass **without** the generator taking a db handle (the seam stays db-agnostic and stable SP-2→SP-12). `submit_intent` (P4) constructs `StubCandidateGenerator(RecordingLLMClient(conn, inner_client, run_id, actor))`.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_recording_client.py
from featuregen.identity.build import build_human_identity
from featuregen.intake.candidates import (
    CANDIDATES_OUTPUT_SCHEMA_ID,
    CANDIDATES_OUTPUT_SCHEMA_VERSION,
    CANDIDATES_PROMPT_ID,
    CANDIDATES_PROMPT_VERSION,
    RecordingLLMClient,
)
from featuregen.intake.llm import LLMRequest, LLMResult

OWNER = build_human_identity(subject="user:raj", role_claims=("data_scientist",))


class _Inner:
    def call(self, request):
        return LLMResult(output={"candidates": []}, self_reported_scores={},
                         call_ref="inner_ref_ignored", status="ok")


def test_recording_client_binds_run_context_and_routes_through_call_llm(db, monkeypatch):
    seen = {}

    def fake_call_llm(conn, client, request, *, run_id, actor):
        seen.update(conn=conn, client=client, request=request, run_id=run_id, actor=actor)
        # call_llm returns the LLMResult carrying the REAL, event-sourced llm_call ref
        return LLMResult(output=request.inputs, self_reported_scores={}, call_ref="llmc_real",
                         status="ok")

    monkeypatch.setattr("featuregen.intake.candidates.call_llm", fake_call_llm)

    inner = _Inner()
    rec = RecordingLLMClient(conn=db, inner=inner, run_id="run_hyp", actor=OWNER)
    req = LLMRequest(
        task="generate_candidates",
        prompt_id=CANDIDATES_PROMPT_ID,
        prompt_version=CANDIDATES_PROMPT_VERSION,
        inputs={"allowed_concepts": ["mcc"]},
        output_schema_id=CANDIDATES_OUTPUT_SCHEMA_ID,
        output_schema_version=CANDIDATES_OUTPUT_SCHEMA_VERSION,
        generation_settings={},
    )
    res = rec.call(req)

    assert res.call_ref == "llmc_real"          # the event-sourced ref, not the inner client's
    assert seen["conn"] is db                    # bound conn passed to call_llm
    assert seen["client"] is inner               # the inner provider client is the one recorded
    assert seen["request"] is req                # the exact request forwarded unchanged
    assert seen["run_id"] == "run_hyp"           # bound run context
    assert seen["actor"] is OWNER                # bound actor identity
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_recording_client.py -v`
  - Expected: FAIL — `ImportError: cannot import name 'RecordingLLMClient' from 'featuregen.intake.candidates'`.

- [ ] **Step 3 — minimal implementation** (append to `candidates.py`; extend the imports)

Extend the `intake.llm` import and add the contracts import at the top:

```python
from featuregen.contracts import DbConn, IdentityEnvelope
from featuregen.intake.llm import LLMClient, LLMRequest, LLMResult, call_llm
```

Append the bridge:

```python
@dataclass(frozen=True)
class RecordingLLMClient:
    """Binds SP-2's event-sourced `call_llm` envelope (P3) to the pure `LLMClient.call` seam so a
    `CandidateGenerator` — which only ever sees `client.call` — still writes the immutable `llm_call`
    record + emits `LLM_CALL_RECORDED` for its ONE generation pass. Constructed per-run (conn/run_id/
    actor captured here) so the generator stays db-agnostic and the seam stays stable SP-2 → SP-12.
    `call` returns `call_llm`'s `LLMResult`, whose `call_ref` is the real event-sourced record id."""

    conn: DbConn
    inner: LLMClient
    run_id: str
    actor: IdentityEnvelope

    def call(self, request: LLMRequest) -> LLMResult:
        return call_llm(self.conn, self.inner, request, run_id=self.run_id, actor=self.actor)
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_recording_client.py -v`
  - Expected: PASS (1 test).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/intake/candidates.py tests/featuregen/intake/test_recording_client.py && git commit -m "feat(intake): RecordingLLMClient binds the event-sourced call_llm envelope to the pure LLM seam"`

---

### Task 6.4: `candidates.py` — freeze candidate docs + the `generate_candidate_docs` orchestrator

**Files:**
- Modify: `src/featuregen/intake/candidates.py` (append imports + the freezing helpers)
- Test: `tests/featuregen/intake/test_candidate_docs.py`

**Interfaces:**
- Consumes: `append_document`/`compute_content_hash` (SP-0 `documents/store.py`); `NewDocument`/`Stage` (`contracts/documents.py`); `provenance_for` (`aggregates/_append.py`); `DRAFT_CONTRACT_SCHEMA_VERSION` (`documents/draft.py`); `mint_id` (R14, already imported in Task 6.2); the `blob_index` table (SP-0 migration `0010`). `stdlib json`.
- Produces:
  - `write_candidate_docs(conn, *, candidates: list[Candidate], draft_doc_id: str, run_id: str, request_id: str, actor: IdentityEnvelope) -> tuple[str, ...]` — freezes each `Candidate` as a **candidate-role `DRAFT_CONTRACT`** staged document under the run's Draft stage (`branch_role="candidate"`, `stage=Stage.DRAFT_CONTRACT.value`, `derived_from=(draft_doc_id,)`, `body_classification="governance-retained"`, body opaque-by-reference via `body_ref`+`content_hash`, §3.4/§4.3). Returns the candidate `doc_id`s in generation order.
  - `generate_candidate_docs(conn, generator: CandidateGenerator, *, draft, catalog_metadata, domain_context, draft_doc_id, run_id, request_id, actor) -> tuple[str, ...]` — the **hypothesis-mode entry point** `submit_intent` (P4) calls after the primary Draft is frozen: run the (event-sourced, via a `RecordingLLMClient`-wrapped) generator → freeze candidate docs → return their `doc_id`s (referenced by `DRAFT_CONTRACT_PRODUCED`). An **empty** result ⟹ generation failed closed → the run stays in clarification (§7.2).
  - `_persist_contract_body(conn, *, body: dict) -> tuple[str, str]` — freeze a governance-retained body by reference (canonical-JSON `content_hash` + a live `blob_index` row); returns `(body_ref, content_hash)`.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_candidate_docs.py
from featuregen.aggregates._append import provenance_for
from featuregen.contracts.documents import NewDocument, Stage
from featuregen.documents.store import append_document, get_document
from featuregen.identity.build import build_human_identity
from featuregen.idgen import mint_id
from featuregen.intake.candidates import (
    StubCandidateGenerator,
    generate_candidate_docs,
    write_candidate_docs,
)
from featuregen.intake.llm import LLMResult

OWNER = build_human_identity(subject="user:raj", role_claims=("data_scientist",))

_OUT = {"candidates": [
    {"definition_text": "distinct MCC delta 30d", "rationale": "churn",
     "calculation_method": {"kind": "rolling_aggregate", "aggregation": "distinct_count",
                            "window": "30d", "filter": {"concept": "mcc"}}},
    {"definition_text": "top-category share drift", "rationale": "concentration",
     "calculation_method": {"kind": "ratio", "numerator": "top", "denominator": "total", "window": "30d"}},
    {"definition_text": "JS divergence", "rationale": "distribution shift",
     "calculation_method": {"kind": "distribution_divergence", "measure": "jensen_shannon",
                            "window": "30d", "baseline_window": "180d"}},
]}


class _ScriptedLLM:
    def __init__(self, output, *, status="ok"):
        self.output = output
        self.status = status

    def call(self, request):
        return LLMResult(output=self.output, self_reported_scores={}, call_ref="llmc_1",
                         status=self.status)


def _draft_doc(db, run_id, request_id):
    doc_id = mint_id("doc")
    append_document(
        db,
        NewDocument(
            doc_id=doc_id,
            stage=Stage.DRAFT_CONTRACT.value,
            schema_version=1,
            branch_role="primary",
            content_hash="sha256:draft",
            body_classification="governance-retained",
            provenance=provenance_for(artifact_type="DRAFT_CONTRACT"),
            body_ref="blob_draft",
        ),
        run_id=run_id,
        request_id=request_id,
        actor=OWNER,
    )
    return doc_id


def test_write_candidate_docs_freezes_candidate_role_draft_docs(db):
    run_id, request_id = "run_h1", "req_h1"
    draft = _draft_doc(db, run_id, request_id)
    cands = StubCandidateGenerator(_ScriptedLLM(_OUT)).generate(
        {"intake_mode": "hypothesis"}, {"concepts": ["mcc", "total", "top"]}, None
    )
    doc_ids = write_candidate_docs(
        db, candidates=cands, draft_doc_id=draft, run_id=run_id, request_id=request_id, actor=OWNER
    )
    assert len(doc_ids) == 3
    rows = db.execute(
        "SELECT branch_role, stage, derived_from, body_classification, run_id "
        "FROM documents WHERE doc_id = ANY(%s)",
        (list(doc_ids),),
    ).fetchall()
    assert {r[0] for r in rows} == {"candidate"}             # candidate branch role (§7.1)
    assert {r[1] for r in rows} == {"DRAFT_CONTRACT"}        # under the run's Draft stage
    assert all(r[2] == [draft] for r in rows)                # DAG-linked derived_from the Draft
    assert {r[3] for r in rows} == {"governance-retained"}   # contract bodies are governance-retained (§4.3)
    assert {r[4] for r in rows} == {run_id}


def test_generate_candidate_docs_orchestrates_generate_then_freeze(db):
    run_id, request_id = "run_h2", "req_h2"
    draft = _draft_doc(db, run_id, request_id)
    gen = StubCandidateGenerator(_ScriptedLLM(_OUT))
    doc_ids = generate_candidate_docs(
        db, gen, draft={"intake_mode": "hypothesis"}, catalog_metadata={"concepts": ["mcc"]},
        domain_context=None, draft_doc_id=draft, run_id=run_id, request_id=request_id, actor=OWNER
    )
    assert len(doc_ids) == 3
    # each candidate body is opaque-by-reference (body_ref + content_hash), never inline (§3.4)
    d = get_document(db, doc_ids[0])
    assert d["body_ref"].startswith("blob_")
    assert d["content_hash"].startswith("sha256:")
    # the frozen blob is a live, governance-retained object-store row
    row = db.execute(
        "SELECT classification, status FROM blob_index WHERE blob_id = %s", (d["body_ref"],)
    ).fetchone()
    assert row == ("governance-retained", "live")


def test_generation_failed_into_clarification_writes_no_docs(db):
    run_id, request_id = "run_h3", "req_h3"
    draft = _draft_doc(db, run_id, request_id)
    gen = StubCandidateGenerator(_ScriptedLLM({}, status="failed_into_clarification"))
    doc_ids = generate_candidate_docs(
        db, gen, draft={}, catalog_metadata={}, domain_context=None,
        draft_doc_id=draft, run_id=run_id, request_id=request_id, actor=OWNER
    )
    assert doc_ids == ()  # fail closed → no candidate docs; the run stays in clarification (§7.2)
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_candidate_docs.py -v`
  - Expected: FAIL — `ImportError: cannot import name 'write_candidate_docs' from 'featuregen.intake.candidates'`.

- [ ] **Step 3 — minimal implementation** (append to `candidates.py`; add the imports)

Add to the imports:

```python
import json

from featuregen.aggregates._append import provenance_for
from featuregen.contracts.documents import NewDocument, Stage
from featuregen.documents.draft import DRAFT_CONTRACT_SCHEMA_VERSION
from featuregen.documents.store import append_document, compute_content_hash
```

Append the freezing helpers:

```python
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
    """Hypothesis-mode entry point `submit_intent` (P4) calls after the primary Draft is frozen:
    run the (event-sourced, `RecordingLLMClient`-wrapped) generator → freeze each candidate as a
    candidate-role Draft document → return the candidate `doc_id`s (referenced by
    `DRAFT_CONTRACT_PRODUCED`). Empty ⟹ generation failed closed → the run stays in clarification
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
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_candidate_docs.py -v`
  - Expected: PASS (3 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/intake/candidates.py tests/featuregen/intake/test_candidate_docs.py && git commit -m "feat(intake): freeze candidate-role DRAFT_CONTRACT docs + generate_candidate_docs orchestrator"`

---

### Task 6.5: `commands.py` — `select_candidate_doc` (document `PRIMARY_SELECTED` promotion, owner+human guarded)

**Files:**
- Modify: `src/featuregen/intake/commands.py` (append the handler; extend `_SP2_CATALOG`)
- Test: `tests/featuregen/intake/test_select_candidate_doc.py`

**Interfaces:**
- Consumes: `new_primary_selected(*, run_id, stage, doc_id, actor, provenance, caused_by=None) -> NewEvent` (SP-0 `documents/primary.py`); `append_event` (`featuregen.events`); `current_version`/`table_version_for`/`provenance_for` (`aggregates/_append.py`); `Stage` (`contracts/documents.py`); **`record_denial(conn, cmd, reason)` (`security/audit.py`, R15 — writes `decision="denied"`)**; **`fold_feature_contract_state` + `actor_is_request_owner(state, actor) -> bool` (P2 `intake/state.py`, R3/R4)** and **`load_feature_contract(conn, run_id)` (P1 `intake/store.py`, R1)** — the owner guard folds the `feature_contract` stream and calls the state-based predicate (NOT the `(conn, run_id, actor)` mcv form); `Command`/`CommandResult`/`DbConn` (`featuregen.contracts`); the P4-created `_SP2_CATALOG`/`register_sp2_commands`.
- Produces: `select_candidate_doc(conn, cmd) -> CommandResult` — the **document-level** candidate promotion for hypothesis mode (§7.1). `cmd.args`: `run_id`, `candidate_doc_id`, `stage` (default `Stage.DRAFT_CONTRACT.value`). Guards (fail-closed, in order): **human** (`actor_kind == "human"`, else DENY); **request-owner** (fold the `feature_contract` stream → `actor_is_request_owner(state, cmd.actor)`, else DENY **via `record_denial`** → security-audit `COMMAND_DENIED`, `decision="denied"`, R15); the doc must be a **candidate-role** doc for `(run_id, stage)` (else DENY). On pass: emit `PRIMARY_SELECTED` via `new_primary_selected` on the **run** aggregate (OCC on the run stream) — records **only the chosen** doc; the losing sibling docs are write-once and **untouched** (no per-doc reject event; their `doc_id`s live only in the Gate #1 confirmation record, §8.3). **NOT** the request-level `select_candidate` (wrong granularity). Appended to `_SP2_CATALOG` so `execute_command` routes it (used by `confirm_contract` in hypothesis mode, P7, and the P9 E2E).
- **X4 (CAS-on-folded-head) — this file's reconciliation:** `select_candidate_doc` folds the `feature_contract` stream **only** for the owner-guard; it appends **no** `feature_contract` domain transition, so there is **no** feature_contract folded head to CAS on. The `PRIMARY_SELECTED` promotion is a **document selection per SP-0** that rides the **run** aggregate, guarded by the **run stream's own OCC** (`expected_version=current_version(conn,"run",run_id)`) — do **NOT** thread the feature_contract folded head into this run-aggregate append (different aggregate). Candidate generation (Tasks 6.2/6.4) likewise appends no `feature_contract` event (docs + blob rows only; `DRAFT_CONTRACT_PRODUCED` is P4's). If any future revision of this file appends a `feature_contract` event **after** folding state, it MUST follow X4 verbatim: capture `head_version = stream[-1].stream_version` (or `0` for a brand-new stream) at fold time, pass it as `expected_version` to `append_feature_contract_event`, and catch `ConcurrencyError` → deny `"stale"`.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_select_candidate_doc.py
from psycopg.rows import dict_row

from featuregen.aggregates._append import provenance_for
from featuregen.aggregates.request_aggregate import create_request_command, create_run_command
from featuregen.contracts import Command
from featuregen.contracts.documents import NewDocument, Stage
from featuregen.documents.primary import register_primary_selected
from featuregen.documents.store import append_document
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.idgen import mint_id
from featuregen.intake.commands import select_candidate_doc
from featuregen.intake.store import append_feature_contract_event

OWNER = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
STRANGER = build_human_identity(subject="user:mallory", role_claims=("data_scientist",))
SERVICE = build_service_identity(
    subject="service:intake-agent", role_claims=("intake-agent",), attestation="sig"
)


def _open_run(db, owner, concept):
    """A real requester-owned run: create_request + create_run, then open the `feature_contract`
    aggregate with an INTENT_SUBMITTED event acted by `owner` (R1 store seam). R4's fold sets
    `state.requester` from THAT event's `actor.subject`, so the state-based request-owner guard
    resolves `owner` as the run's requester."""
    req = create_request_command(
        db,
        Command("create_request", "request", None,
                {"feature_concept": concept, "intake_mode": "hypothesis"}, owner, mint_id("ik")),
    )
    run = create_run_command(
        db,
        Command("create_run", "request", None, {"request_id": req.aggregate_id}, owner, mint_id("ik")),
    )
    # R1/R4: open the feature_contract stream so the fold has a requester. The fold reads the EVENT
    # actor.subject for `state.requester` — the payload content does not set ownership.
    append_feature_contract_event(
        db,
        run_id=run.aggregate_id,
        type="INTENT_SUBMITTED",
        payload={
            "intake_mode": "hypothesis",
            "classification": {"outcome": "IN_SCOPE", "catalog_version": "v0", "matched_class": None},
        },
        actor=owner,
        request_id=req.aggregate_id,
    )
    return req.aggregate_id, run.aggregate_id


def _candidate_doc(db, run_id, request_id, *, branch_role="candidate"):
    doc_id = mint_id("doc")
    append_document(
        db,
        NewDocument(
            doc_id=doc_id,
            stage=Stage.DRAFT_CONTRACT.value,
            schema_version=1,
            branch_role=branch_role,
            content_hash="sha256:c",
            body_classification="governance-retained",
            provenance=provenance_for(artifact_type="DRAFT_CONTRACT"),
            body_ref=mint_id("blob"),
        ),
        run_id=run_id,
        request_id=request_id,
        actor=OWNER,
    )
    return doc_id


def _cmd(run_id, doc_id, actor):
    return Command(
        "select_candidate_doc", "run", None,
        {"run_id": run_id, "candidate_doc_id": doc_id, "stage": "DRAFT_CONTRACT"}, actor, mint_id("ik")
    )


def test_owner_promotes_only_the_chosen_candidate(db):
    register_primary_selected(db)
    request_id, run_id = _open_run(db, OWNER, "abrupt category shift A")
    chosen = _candidate_doc(db, run_id, request_id)
    loser = _candidate_doc(db, run_id, request_id)

    res = select_candidate_doc(db, _cmd(run_id, chosen, OWNER))
    assert res.accepted is True, res.denied_reason

    # exactly one PRIMARY_SELECTED, for the CHOSEN doc, on the run aggregate
    rows = db.execute(
        "SELECT payload->>'doc_id' FROM events "
        "WHERE aggregate='run' AND aggregate_id=%s AND type='PRIMARY_SELECTED'",
        (run_id,),
    ).fetchall()
    assert [r[0] for r in rows] == [chosen]
    # both candidate docs remain (write-once); the loser is UNTOUCHED — no per-doc reject event
    n = db.execute("SELECT count(*) FROM documents WHERE doc_id = ANY(%s)", ([chosen, loser],)).fetchone()[0]
    assert n == 2
    loser_promotions = db.execute(
        "SELECT count(*) FROM events WHERE type='PRIMARY_SELECTED' AND payload->>'doc_id'=%s", (loser,)
    ).fetchone()[0]
    assert loser_promotions == 0


def test_non_owner_is_denied_and_security_audited(db):
    register_primary_selected(db)
    request_id, run_id = _open_run(db, OWNER, "abrupt category shift B")
    chosen = _candidate_doc(db, run_id, request_id)

    res = select_candidate_doc(db, _cmd(run_id, chosen, STRANGER))
    assert res.accepted is False
    assert "owner" in res.denied_reason
    # nothing promoted
    assert db.execute(
        "SELECT count(*) FROM events WHERE aggregate_id=%s AND type='PRIMARY_SELECTED'", (run_id,)
    ).fetchone()[0] == 0
    # the denial is recorded on the tamper-evident security-audit stream (§8.2)
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit "
            "WHERE attempted_action='select_candidate_doc' AND decision='denied'"
        )
        assert cur.fetchone()["n"] == 1


def test_service_principal_cannot_select(db):
    register_primary_selected(db)
    request_id, run_id = _open_run(db, OWNER, "abrupt category shift C")
    chosen = _candidate_doc(db, run_id, request_id)
    res = select_candidate_doc(db, _cmd(run_id, chosen, SERVICE))
    assert res.accepted is False
    assert "human" in res.denied_reason


def test_non_candidate_doc_is_rejected_fail_closed(db):
    register_primary_selected(db)
    request_id, run_id = _open_run(db, OWNER, "abrupt category shift D")
    primary = _candidate_doc(db, run_id, request_id, branch_role="primary")  # the Draft, not a candidate
    res = select_candidate_doc(db, _cmd(run_id, primary, OWNER))
    assert res.accepted is False
    assert "candidate" in res.denied_reason


def test_unknown_doc_for_run_is_rejected(db):
    register_primary_selected(db)
    _request_id, run_id = _open_run(db, OWNER, "abrupt category shift E")
    res = select_candidate_doc(db, _cmd(run_id, "doc_does_not_exist", OWNER))
    assert res.accepted is False
    assert "unknown" in res.denied_reason
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_select_candidate_doc.py -v`
  - Expected: FAIL — `ImportError: cannot import name 'select_candidate_doc' from 'featuregen.intake.commands'`.

- [ ] **Step 3 — minimal implementation** (append to `intake/commands.py`; add the imports; extend `_SP2_CATALOG`)

Add to the imports at the top of `intake/commands.py`:

```python
from featuregen.aggregates._append import current_version, provenance_for, table_version_for
from featuregen.contracts.documents import Stage
from featuregen.documents.primary import new_primary_selected
from featuregen.events import append_event
from featuregen.intake.state import actor_is_request_owner, fold_feature_contract_state
from featuregen.intake.store import load_feature_contract
from featuregen.security.audit import record_denial
```

Append the handler:

```python
def select_candidate_doc(conn: DbConn, cmd: Command) -> CommandResult:
    """Hypothesis-mode candidate selection (§7.1): a document-level `PRIMARY_SELECTED` promotion of
    the chosen candidate doc on the RUN aggregate (`new_primary_selected`) — records ONLY the chosen
    doc; the losing candidate docs are write-once and LEFT UNTOUCHED (no per-doc reject event; their
    `doc_id`s live only in the Gate #1 confirmation record, §8.3). This is NOT the request-level
    `select_candidate` command (which promotes *run* candidates on a *request* stream — the wrong
    granularity; SP-2's candidates are documents under a single run). Owner + human guarded; invoked
    by `confirm_contract` in hypothesis mode (P7). OCC on the run stream serializes concurrent
    selects. X4: the `feature_contract` fold here is OWNER-GUARD-ONLY — this handler appends NO
    `feature_contract` transition, so there is no FC folded head to CAS on; the `PRIMARY_SELECTED`
    append rides the RUN aggregate under the run stream's own OCC (per SP-0). Do NOT pass the
    feature_contract folded head as this append's `expected_version` (wrong aggregate)."""
    args = cmd.args
    run_id = args["run_id"]
    candidate_doc_id = args["candidate_doc_id"]
    stage = args.get("stage", Stage.DRAFT_CONTRACT.value)

    # Gate #1 is an author-owned intent lock: the confirmer MUST be the authenticated human requester
    # (never a service, never the LLM, never a different data scientist). SP-0 authz admits any
    # data_scientist human, so SP-2 enforces the fine owner-guard here (§8.2).
    if cmd.actor.actor_kind != "human":
        return CommandResult(
            accepted=False,
            aggregate_id=run_id,
            denied_reason="select_candidate_doc requires the human requester (not a service)",
        )
    # R3/R4: fold the feature_contract stream and call the state-based owner predicate owned by P2
    # (intake/state.py) — never the (conn, run_id, actor) mcv form. `state.requester` is the
    # INTENT_SUBMITTED event actor.subject.
    state = fold_feature_contract_state(load_feature_contract(conn, run_id))
    if not actor_is_request_owner(state, cmd.actor):
        record_denial(conn, cmd, "actor is not the request owner")  # R15 — writes decision="denied"
        return CommandResult(
            accepted=False,
            aggregate_id=run_id,
            denied_reason="actor is not the request owner (owner-guard, §8.2)",
        )

    row = conn.execute(
        "SELECT branch_role FROM documents WHERE doc_id=%s AND run_id=%s AND stage=%s",
        (candidate_doc_id, run_id, stage),
    ).fetchone()
    if row is None:
        return CommandResult(
            accepted=False,
            aggregate_id=run_id,
            denied_reason=f"unknown candidate doc {candidate_doc_id} for (run={run_id}, stage={stage})",
        )
    if row[0] != "candidate":
        return CommandResult(
            accepted=False,
            aggregate_id=run_id,
            denied_reason=f"doc {candidate_doc_id} is branch_role={row[0]!r}, not a candidate",
        )

    # X4 / SP-0 carve-out: PRIMARY_SELECTED is a document promotion on the RUN aggregate — its OCC is
    # the run stream's own head (`current_version(conn,"run",run_id)`), NOT the feature_contract folded
    # head. This handler appends no feature_contract transition, so there is no FC head to CAS on
    # (X4's folded-head expected_version rule is n/a here; the fold above is owner-guard-only).
    event = new_primary_selected(
        run_id=run_id,
        stage=stage,
        doc_id=candidate_doc_id,
        actor=cmd.actor,
        provenance=provenance_for(artifact_type=stage),
    )
    appended = append_event(
        conn,
        event,
        expected_version=current_version(conn, "run", run_id),
        table_version=table_version_for(conn, "run", run_id),
    )
    return CommandResult(
        accepted=True, aggregate_id=run_id, produced_event_ids=(appended.event_id,)
    )
```

Then extend the catalog (append the entry to the existing `_SP2_CATALOG` tuple — `register_sp2_commands()` picks it up idempotently):

```python
_SP2_CATALOG = (
    # ... existing P4/P5 entries ...
    ("select_candidate_doc", select_candidate_doc),
)
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_select_candidate_doc.py -v`
  - Expected: PASS (5 tests).

- [ ] **Step 5 — run the whole Phase-6 suite (no regression)**
  - `uv run pytest tests/featuregen/intake/test_candidates_seam.py tests/featuregen/intake/test_stub_generator.py tests/featuregen/intake/test_recording_client.py tests/featuregen/intake/test_candidate_docs.py tests/featuregen/intake/test_select_candidate_doc.py -v`
  - Expected: PASS (all Phase-6 tests green).

- [ ] **Step 6 — lint**
  - `uv run ruff check src/featuregen/intake/candidates.py src/featuregen/intake/commands.py`
  - Expected: no findings.

- [ ] **Step 7 — commit**
  - `git add src/featuregen/intake/commands.py tests/featuregen/intake/test_select_candidate_doc.py && git commit -m "feat(intake): select_candidate_doc — document PRIMARY_SELECTED promotion (owner+human guarded)"`

---

## Phase 6 completion checklist

- [ ] `CandidateGenerator` Protocol + `Candidate` schema are the **stable seam** SP-12 binds to — only `generate`'s body changes (§7.1, §7.2).
- [ ] `StubCandidateGenerator` makes **exactly one** `LLMClient` pass → **1–3** candidates; **no** router/specialists/memory/symbolic/diversity/few-shot (the SP-12 boundary held; SP-12 scope is **not** imported).
- [ ] `signals` are **cheap, model-free** only — **no IV/WoE/AUC/overfitting** (§7.3).
- [ ] Candidates are frozen as **candidate-role `DRAFT_CONTRACT`** docs under the run's Draft stage, DAG-linked to the Draft, `governance-retained`, opaque-by-reference.
- [ ] `RecordingLLMClient` event-sources the generation pass through P3's `call_llm` without the generator taking a db handle.
- [ ] `select_candidate_doc` is a **document `PRIMARY_SELECTED`** promotion (`new_primary_selected`, run aggregate) recording **only the chosen** doc — losers write-once untouched; it is **not** the request-level `select_candidate`.
- [ ] Owner + human guards fail-closed; a non-owner/service is **denied + security-audited**; a non-candidate/unknown doc is rejected.
- [ ] Definition mode has **no** generation (this phase is hypothesis-only).
- [ ] All tests are `FakeLLM`/scripted-double driven, deterministic, and DB-backed where relevant.
