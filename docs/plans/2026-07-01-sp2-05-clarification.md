# SP-2 — Phase 5 — Layer-2 clarification & the Doubt Router (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax. **Global Constraints + Shared Contract:** see [sp2-00-overview.md](2026-07-01-sp2-00-overview.md) (authoritative — every shared symbol here is defined there; do NOT redefine or drift one).

---

This phase builds **Layer 2 (Contract control & human clarification)** — spec §6. It turns the Draft
Feature Contract produced by Layer 1 (P4) into an MCV-passing, gate-eligible contract, entirely on
deterministic machinery with an **auditable, challenger-only** LLM. It ships, in dependency order:

- `scoring.py` — per-field **ambiguity + confidence** on a 0.0–1.0 scale: the LLM self-report **combined**
  (cautious-max) with a deterministic **catalog-cardinality** check (Decision 3: the LLM can never *lower*
  a deterministic doubt), plus the read-only `CatalogView` scoring seam (spec §6.1).
- `doubt_router.py` — the deterministic **Doubt Router** (`auto-resolve iff ambiguity ≤ 0.30 ∧ confidence
  ≥ 0.70 ∧ safe source ∧ not policy-sensitive ∧ not a calc-method choice`; config-gated; biased toward
  asking — Decision 4, spec §6.2).
- `critique.py` — the Critique **`CONTRACT_REVIEW`** mode (SP-2 owns this one mode): a single **challenger,
  never a gate** LLM pass that can only *raise* doubts / *add* open questions and **feeds the Doubt Router**
  (each `blocks_progress:true` finding ORs its field to must-ask, spec §6.4).
- `mcv.py` — **Minimum Contract Validation** — **R5** BOTH the pure `minimum_contract_validated(...)`
  6-check checklist AND the DB-backed `run_minimum_contract_validation(conn, run_id, *, actor) ->
  CommandResult` (folds status via the P2 fold → the `MINIMUM_CONTRACT_VALIDATED` event, spec §6.7) + the
  SP-2 lifecycle-guard predicates (`open_fields_empty`, `not_prohibited_intent`,
  `calculation_method_available`, `confirmer_is_requester_human(state, actor)`) built on the **R4**
  `intake.state.actor_is_request_owner` predicate (imported from P2, never redefined in mcv).
- `commands.py` (extended) — the **Human Clarification tasks** (`open_clarification_task`: SP-0
  `CLARIFICATION` gate, `delegation_allowed=False`, eligible = the request owner), the bounded **Contract
  Refinement Loop** (`refine_contract`: renormalize → rescore → re-critique → re-route → auto-resolve /
  open must-ask tasks → MCV; exhausted → auto-park), and the **`answer_clarification`** command with the
  SP-2-built **request-owner guard** (**R4** `actor_is_request_owner(state, actor)` else **deny +
  security-audit**) — the guard SP-0 does **not** provide (spec §6.5, §2.1).

**Cross-phase Consumes (built earlier; used verbatim here):**

- **SP-0 (verbatim):** `gates/tasks.py::open_task(conn, spec, actor) -> task_id` /
  `submit_human_signal(conn, task_id, *, response, actor, expected_task_version, on_behalf_of=None) ->
  SignalResult` (role/scope/quorum only — **never** subject membership, so SP-2 adds the owner guard);
  `contracts/envelopes.py::{GateTaskSpec (delegation_allowed defaults True — SP-2 passes False), Command,
  CommandResult, SignalResult}`; `aggregates/run_lifecycle.py::park_command(conn, cmd)` (RUN_PARKED,
  payload `{run_id, owner, waiting_on_fact}`); `security/audit.py::record_denial(conn, cmd, reason)` (routes
  a denial to the tamper-evident security-audit stream, §6.2); `events/store.py::load_stream`;
  `documents/draft.py::UNKNOWN`; `contracts.{DbConn, IdentityEnvelope}`; `identity/build.py::{build_human_identity,
  build_service_identity}`.
- **P1 (sp2-01):** `intake/events.py::register_sp2_event_types(registry)`;
  the **R1** single append/load seam **`intake/store.py::append_feature_contract_event(conn, *, run_id,
  type, payload, actor, request_id=None, provenance=None, expected_version=None, caused_by=None) ->
  EventEnvelope`** — appends on the **`feature_contract`** aggregate (sets `aggregate="feature_contract"`,
  `aggregate_id == feature_contract_id == run_id`), threading the typed mirror-id (§2.1 #1, the SP-1
  `append_overlay_event` recipe); **`intake/store.py::load_feature_contract(conn, run_id) ->
  list[EventEnvelope]`** (mirrors `overlay/store.py::load_fact`). P5 IMPORTS both verbatim from
  `intake.store` (the short alias `append_feature_contract_event as append_fc_event` is the only
  permitted rename — never a local `append_fc_event`/`intake.events` redefinition, R1). Event-type
  names (string constants, all registered `schema_version=1`): `INTENT_SUBMITTED`,
  `DRAFT_CONTRACT_PRODUCED`, `CONTRACT_CRITIQUED`,
  `FIELD_AUTO_RESOLVED`, `CLARIFICATION_REQUESTED`, `CLARIFICATION_ANSWERED`, `CONTRACT_REFINED`,
  `MINIMUM_CONTRACT_VALIDATED`, `LLM_CALL_RECORDED`. Plus the `llm_call` store + the `0508`/`0509` migrations.
- **P2 (sp2-02):** `intake/contract.py::register_contract_schemas(registry)` (the `DRAFT_CONTRACT`/
  `ASSUMPTION_LEDGER`/`CONFIRMED_CONTRACT` document content-schemas **and** the four structural LLM
  output-schemas `structure_intent`/`contract_review`/`generate_candidates`/`renormalize` — P2's
  output-schema-registration remit — resolved by `call_llm`); the closed enum vocabularies + `UNKNOWN`
  reuse. **R3/R4** `intake/state.py::{fold_feature_contract_state(stream) -> FeatureContractState,
  FeatureContractStatus, actor_is_request_owner(state, actor) -> bool}` (P2 Task 2.5 — the ONE
  `feature_contract` fold + the ONE owner predicate; `state.requester` = the `INTENT_SUBMITTED` event
  `actor.subject`) — P5 CONSUMES these (never a duplicate `state.py`), and P8 consumes the same
  unchanged. **R9** the **recorded classification** carried on `INTENT_SUBMITTED.payload["classification"]
  = classify_intent(...).as_mapping() = {outcome, catalog_version, matched_class?}` (field is
  `.catalog_version`, NOT `.version`; from `banking_catalog.classify_intent`, outcomes as the string
  values of `IntakeOutcome`).
- **P3 (sp2-03):** `intake/llm.py::{LLMClient (Protocol: .call(request) -> LLMResult), LLMRequest(task,
  prompt_id, prompt_version, inputs, output_schema_id, output_schema_version, generation_settings),
  LLMResult(output, self_reported_scores, call_ref, status), FakeLLM, call_llm(conn, client, request, *,
  run_id, actor) -> LLMResult}` (call_llm egress-guards, records the `llm_call`, emits `LLM_CALL_RECORDED`);
  `intake/redaction.py::{IntentRedactor (Protocol: .redact(raw, classification) -> RedactionResult),
  DefaultIntentRedactor, RedactionResult(text, redaction_version, redacted_spans, disposition),
  assert_llm_safe}`.
- **P4 (sp2-04), same `commands.py` module:** `submit_intent(conn, cmd)`, the `_SP2_CATALOG` tuple +
  `register_sp2_commands()` (idempotent — skips already-registered actions; P5 **appends**
  `("answer_clarification", answer_clarification)`); and the Draft/Ledger **body-persistence seam**
  **`freeze_draft(conn, *, run_id, request_id, body, ledger_body, actor, supersedes=()) -> tuple[str, str]`**
  (returns `(draft_doc_id, ledger_doc_id)`; freezes the `DRAFT_CONTRACT` + `ASSUMPTION_LEDGER` documents on
  the DAG) and **`read_contract_body(conn, doc_id) -> dict`**. **R12** `DRAFT_CONTRACT_PRODUCED.payload =
  {draft_doc_id, assumption_ledger_ref, open_fields}` (NEVER `ledger_doc_id`); `INTENT_SUBMITTED.payload =
  {request_id, run_id, intake_mode, raw_input_ref, raw_input_classification, classification}` (issued by
  the human requester, so the event `actor.subject` IS the request owner the fold reads, R4 — P4 may also
  mirror `requester` into the payload, but no owner lookup reads it).

> **The `feature_contract` fold is P2 (R3), consumed here.** `fold_feature_contract_state` /
> `FeatureContractState` / `actor_is_request_owner(state, actor)` are owned by **P2 `intake/state.py`**
> (Task 2.5) — **not** P8, which merely consumes the same fold unchanged (R3). P5 CONSUMES the P2 fold:
> `run_minimum_contract_validation` folds the status before appending `MINIMUM_CONTRACT_VALIDATED`, and
> `answer_clarification` resolves the request owner via `actor_is_request_owner(state, actor)` (R4 —
> `state.requester` = the `INTENT_SUBMITTED` event `actor.subject`; **never** a `payload.get("requested_by")`
> lookup). The small P5 stream-readers (`_first`, `_answered_fields`, `_current_draft_doc_id`) are local
> loop helpers over the same stream — not a second fold and never a duplicate `state.py`.

---

### Task 5.1: `scoring.py` — per-field ambiguity/confidence (LLM self-report ⊕ catalog cardinality)

**Files:**
- Create: `src/featuregen/intake/scoring.py`
- Test: `tests/featuregen/intake/test_scoring.py`

**Interfaces:**
- Consumes: nothing (pure; `documents.draft.UNKNOWN` for the sentinel).
- Produces:
  - `FieldScore(ambiguity: float, confidence: float, source: str)` — frozen/slots; `source ∈ {llm, default, catalog}`.
  - `combine_scores(llm: FieldScore, catalog: FieldScore) -> FieldScore` — **cautious-max** (Decision 3): `ambiguity = max`, `confidence = min`; the source that *raised* the ambiguity wins (ties keep the LLM's). The LLM can never lower a deterministic doubt.
  - `catalog_cardinality_score(n_bindings: int) -> FieldScore` — the deterministic catalog-cardinality check (how many catalog objects / catalog-declared codes a concept could bind to): 0–1 → confident, 2 → doubtful, ≥3 → high-ambiguity.
  - `score_fields(llm_scores: Mapping[str, Mapping], concept_of: Mapping[str, str | None], cardinality: Callable[[str], int]) -> dict[str, dict]` — per field, combine the LLM self-report with the cardinality score of its bound concept (only for concept-bearing fields), returning the `field_scores` block shape (`{field: {ambiguity, confidence, source}}`).
  - `CatalogView(Protocol)` — the read-only SP-1 merged-view scoring seam: `candidate_count(concept: str) -> int` + `metadata() -> Mapping[str, Any]` (names/types/grain for the LLM inputs). `register_catalog_view(view)` / `current_catalog_view() -> CatalogView` — single-source accessor (mirrors SP-1's `overlay/catalog.py::current_catalog_adapter`; raises `RuntimeError` if unregistered). SP-6's `CandidateGenerator` binds to this same `CatalogView`.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_scoring.py
from featuregen.intake.scoring import (
    CatalogView,
    FieldScore,
    catalog_cardinality_score,
    combine_scores,
    current_catalog_view,
    register_catalog_view,
    score_fields,
)


def test_cautious_max_takes_higher_ambiguity_and_lower_confidence():
    llm = FieldScore(0.10, 0.90, "llm")
    catalog = FieldScore(0.80, 0.40, "catalog")
    c = combine_scores(llm, catalog)
    assert c.ambiguity == 0.80
    assert c.confidence == 0.40
    assert c.source == "catalog"  # the deterministic check raised the doubt → it owns the score


def test_llm_can_never_lower_a_deterministic_doubt():
    # The model is near-certain, but the concept binds to three candidate columns.
    llm = FieldScore(0.05, 0.99, "llm")
    catalog = FieldScore(0.85, 0.35, "catalog")
    c = combine_scores(llm, catalog)
    assert c.ambiguity == 0.85 and c.confidence == 0.35


def test_catalog_cardinality_scales_with_bindings():
    assert catalog_cardinality_score(1).ambiguity <= 0.30
    assert catalog_cardinality_score(1).confidence >= 0.70
    assert catalog_cardinality_score(2).ambiguity > 0.30
    assert catalog_cardinality_score(3).ambiguity >= 0.70  # many incompatible readings


def test_score_fields_only_combines_concept_bearing_fields():
    llm_scores = {
        "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},  # verbatim; no concept
        "filters": {"ambiguity": 0.40, "confidence": 0.70, "source": "llm"},  # binds a status concept
    }
    concept_of = {"windows": None, "filters": "declined card authorization"}
    scored = score_fields(llm_scores, concept_of, cardinality=lambda concept: 3)
    assert scored["windows"] == {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"}
    assert scored["filters"]["ambiguity"] >= 0.70  # cardinality(3) raised it above the LLM's 0.40
    assert scored["filters"]["source"] == "catalog"


class _View:
    def candidate_count(self, concept: str) -> int:
        return {"declined card authorization": 3}.get(concept, 1)

    def metadata(self):
        return {"objects": ["card_authorizations"]}


def test_catalog_view_single_source_accessor():
    register_catalog_view(_View())
    view = current_catalog_view()
    assert isinstance(view, CatalogView)  # runtime-checkable Protocol
    assert view.candidate_count("declined card authorization") == 3
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_scoring.py -v`
  - Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.intake.scoring'`.

- [ ] **Step 3 — minimal implementation**

```python
# src/featuregen/intake/scoring.py
from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

# The closed `source` vocabulary (spec §4.0). A value is safe to auto-resolve only if it came from
# one of these (the field carries a concrete reading), never from the UNKNOWN sentinel.
SAFE_SOURCES: tuple[str, ...] = ("llm", "default", "catalog")


@dataclass(frozen=True, slots=True)
class FieldScore:
    """A per-field score on the 0.0–1.0 scale (spec §6.1). `ambiguity` = how many plausible readings
    (0 = one reading, 1 = many incompatible); `confidence` = how sure of the CHOSEN reading."""

    ambiguity: float
    confidence: float
    source: str  # llm | default | catalog


def combine_scores(llm: FieldScore, catalog: FieldScore) -> FieldScore:
    """Combine the LLM self-report with the deterministic catalog-cardinality check by taking the
    MORE CAUTIOUS value on each axis (Decision 3): higher ambiguity, lower confidence. The LLM can
    never *lower* a doubt the deterministic check raised. The source that set the (winning, more
    cautious) ambiguity is recorded; a tie keeps the LLM's source."""
    ambiguity = max(llm.ambiguity, catalog.ambiguity)
    confidence = min(llm.confidence, catalog.confidence)
    source = catalog.source if catalog.ambiguity > llm.ambiguity else llm.source
    return FieldScore(ambiguity=ambiguity, confidence=confidence, source=source)


def catalog_cardinality_score(n_bindings: int) -> FieldScore:
    """Deterministic doubt from catalog cardinality: how many catalog objects / catalog-declared
    codes a concept could bind to. One binding is unambiguous; two is genuinely doubtful; three or
    more reads as high-ambiguity (several incompatible readings). This is the doubt the LLM cannot
    talk the platform out of."""
    if n_bindings <= 1:
        return FieldScore(ambiguity=0.05, confidence=0.95, source="catalog")
    if n_bindings == 2:
        return FieldScore(ambiguity=0.50, confidence=0.55, source="catalog")
    return FieldScore(ambiguity=0.85, confidence=0.35, source="catalog")


def score_fields(
    llm_scores: Mapping[str, Mapping[str, Any]],
    concept_of: Mapping[str, str | None],
    cardinality: Callable[[str], int],
) -> dict[str, dict]:
    """Produce the `field_scores` block: for every LLM-scored field, combine its self-report with the
    catalog-cardinality score of its bound concept (concept-bearing fields only). A field with no
    bound concept keeps the LLM's self-report unchanged."""
    out: dict[str, dict] = {}
    for field, raw in llm_scores.items():
        llm = FieldScore(
            float(raw["ambiguity"]), float(raw["confidence"]), str(raw.get("source", "llm"))
        )
        concept = concept_of.get(field)
        if concept:
            combined = combine_scores(llm, catalog_cardinality_score(cardinality(concept)))
        else:
            combined = llm
        out[field] = {
            "ambiguity": combined.ambiguity,
            "confidence": combined.confidence,
            "source": combined.source,
        }
    return out


@runtime_checkable
class CatalogView(Protocol):
    """The read-only SP-1 merged-view scoring seam (spec §4.4): names/types/grain + how many candidate
    bindings a concept has. NEVER profiled values / rows / samples (the no-column-values-to-LLM
    boundary, §9.4). SP-6's CandidateGenerator binds to this same seam."""

    def candidate_count(self, concept: str) -> int: ...

    def metadata(self) -> Mapping[str, Any]: ...


_CATALOG_VIEW: CatalogView | None = None


def register_catalog_view(view: CatalogView) -> None:
    """Single-source registration of the merged-view scoring adapter (mirrors SP-1's
    `register_catalog_adapter`). P9 bootstrap wires the production SP-1 adapter; tests register a stub."""
    global _CATALOG_VIEW
    _CATALOG_VIEW = view


def current_catalog_view() -> CatalogView:
    if _CATALOG_VIEW is None:
        raise RuntimeError("no CatalogView registered; call register_catalog_view() first")
    return _CATALOG_VIEW
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_scoring.py -v`
  - Expected: PASS (5 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/intake/scoring.py tests/featuregen/intake/test_scoring.py && git commit -m "feat(intake): per-field ambiguity/confidence scoring — LLM self-report ⊕ catalog cardinality (cautious-max)"`

---

### Task 5.2: `doubt_router.py` — the deterministic Doubt Router

**Files:**
- Create: `src/featuregen/intake/doubt_router.py`
- Test: `tests/featuregen/intake/test_doubt_router.py`

**Interfaces:**
- Consumes: `scoring.SAFE_SOURCES` (the closed source vocabulary).
- Produces:
  - `RouterThresholds(ambiguity_max: float = 0.30, confidence_min: float = 0.70)` — the config-gated thresholds (Decision 4), overridable via env (`FEATUREGEN_DOUBT_AMBIGUITY_MAX` / `FEATUREGEN_DOUBT_CONFIDENCE_MIN`) through `default_thresholds()`.
  - `route_field(*, ambiguity, confidence, source, has_value, policy_sensitive, is_calculation_method_choice, thresholds=default_thresholds()) -> str` — returns `"auto"` **iff** `ambiguity ≤ max ∧ confidence ≥ min ∧ has a safe value ∧ not policy-sensitive ∧ not a calc-method choice`, else `"human"` (spec §6.2; biased toward asking).
  - `route_draft(field_scores, open_fields, *, mode, policy_sensitive_fields=(), thresholds=default_thresholds()) -> dict[str, str]` — routes every scored field. `has_value` = the field path is not in `open_fields` (an `UNKNOWN` sub-path stales the whole field); the **calc-method choice** is must-ask only in **hypothesis** mode (definition mode has one faithful method, spec §6.3).

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_doubt_router.py
from featuregen.intake.doubt_router import RouterThresholds, route_draft, route_field


def _route(**kw):
    base = dict(
        ambiguity=0.05, confidence=0.98, source="llm", has_value=True,
        policy_sensitive=False, is_calculation_method_choice=False,
    )
    base.update(kw)
    return route_field(**base)


def test_auto_resolves_low_ambiguity_high_confidence_with_a_value():
    assert _route() == "auto"


def test_unknown_field_without_a_value_is_never_auto():
    assert _route(has_value=False) == "human"  # a safe source must exist


def test_policy_sensitive_is_always_human_regardless_of_score():
    assert _route(ambiguity=0.0, confidence=1.0, source="catalog", policy_sensitive=True) == "human"


def test_calc_method_choice_is_always_human():
    assert _route(ambiguity=0.0, confidence=1.0, is_calculation_method_choice=True) == "human"


def test_high_ambiguity_is_human():
    assert _route(ambiguity=0.80, confidence=0.40) == "human"


def test_low_confidence_is_human():
    assert _route(ambiguity=0.10, confidence=0.55) == "human"


def test_thresholds_are_config_gated():
    strict = RouterThresholds(ambiguity_max=0.10, confidence_min=0.90)
    assert route_field(
        ambiguity=0.20, confidence=0.80, source="default", has_value=True,
        policy_sensitive=False, is_calculation_method_choice=False, thresholds=strict,
    ) == "human"


def test_route_draft_definition_example():
    field_scores = {
        "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
        "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
        "calculation_method": {"ambiguity": 0.10, "confidence": 0.90, "source": "llm"},
        "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},
        "filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"},
    }
    d = route_draft(field_scores, ["filters.declined_status_encoding"], mode="definition")
    assert d["entity_grain"] == "auto"
    assert d["windows"] == "auto"
    assert d["calculation_method"] == "auto"  # definition mode: not a choice
    assert d["filters"] == "human"            # UNKNOWN sub-path + high ambiguity


def test_route_draft_hypothesis_calc_method_is_a_choice():
    d = route_draft(
        {"calculation_method": {"ambiguity": 0.10, "confidence": 0.90, "source": "llm"}},
        [], mode="hypothesis",
    )
    assert d["calculation_method"] == "human"  # picking the method IS Gate #1's job


def test_route_draft_policy_sensitive_target_is_human():
    d = route_draft(
        {"target": {"ambiguity": 0.10, "confidence": 0.90, "source": "llm"}},
        [], mode="hypothesis", policy_sensitive_fields=("target",),
    )
    assert d["target"] == "human"
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_doubt_router.py -v`
  - Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.intake.doubt_router'`.

- [ ] **Step 3 — minimal implementation**

```python
# src/featuregen/intake/doubt_router.py
from __future__ import annotations

import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass

from featuregen.intake.scoring import SAFE_SOURCES

# Config-gated defaults (Decision 4), deliberately conservative — fail toward asking.
_DEFAULT_AMBIGUITY_MAX = 0.30
_DEFAULT_CONFIDENCE_MIN = 0.70


@dataclass(frozen=True, slots=True)
class RouterThresholds:
    ambiguity_max: float = _DEFAULT_AMBIGUITY_MAX
    confidence_min: float = _DEFAULT_CONFIDENCE_MIN


def default_thresholds() -> RouterThresholds:
    """Env-overridable thresholds (config-gated, spec §6.2). Bad/absent env values fall back to the
    conservative defaults."""

    def _f(name: str, default: float) -> float:
        try:
            return float(os.environ[name])
        except (KeyError, ValueError):
            return default

    return RouterThresholds(
        ambiguity_max=_f("FEATUREGEN_DOUBT_AMBIGUITY_MAX", _DEFAULT_AMBIGUITY_MAX),
        confidence_min=_f("FEATUREGEN_DOUBT_CONFIDENCE_MIN", _DEFAULT_CONFIDENCE_MIN),
    )


def route_field(
    *,
    ambiguity: float,
    confidence: float,
    source: str,
    has_value: bool,
    policy_sensitive: bool,
    is_calculation_method_choice: bool,
    thresholds: RouterThresholds | None = None,
) -> str:
    """One deterministic decision per field (spec §6.2):

        auto-resolve iff ambiguity ≤ max AND confidence ≥ min
                     AND a safe value exists (source ∈ SAFE_SOURCES and has_value)
                     AND the field is NOT policy-sensitive
                     AND the field is NOT a calculation-method CHOICE
        otherwise → must-ask-human

    Policy-sensitive fields and calc-method choices are must-ask REGARDLESS of score — they may never
    be auto-resolved (§6.2). Biased toward asking."""
    t = thresholds or default_thresholds()
    if policy_sensitive:
        return "human"
    if is_calculation_method_choice:
        return "human"
    if not has_value or source not in SAFE_SOURCES:
        return "human"
    if ambiguity <= t.ambiguity_max and confidence >= t.confidence_min:
        return "auto"
    return "human"


def _has_value(field: str, open_fields: Iterable[str]) -> bool:
    # An UNKNOWN sub-path (e.g. "filters.declined_status_encoding") stales its whole scored field
    # ("filters"): the field has no safe value until the sub-path is resolved.
    return not any(of == field or of.startswith(field + ".") for of in open_fields)


def route_draft(
    field_scores: Mapping[str, Mapping],
    open_fields: Iterable[str],
    *,
    mode: str,
    policy_sensitive_fields: Iterable[str] = (),
    thresholds: RouterThresholds | None = None,
) -> dict[str, str]:
    """Route every scored field. In hypothesis mode the `calculation_method` field is always a
    must-ask CHOICE (§6.3); in definition mode it is a faithful translation and may auto-resolve."""
    t = thresholds or default_thresholds()
    open_list = list(open_fields)
    policy = set(policy_sensitive_fields)
    decisions: dict[str, str] = {}
    for field, sc in field_scores.items():
        decisions[field] = route_field(
            ambiguity=float(sc["ambiguity"]),
            confidence=float(sc["confidence"]),
            source=str(sc.get("source", "llm")),
            has_value=_has_value(field, open_list),
            policy_sensitive=field in policy,
            is_calculation_method_choice=(mode == "hypothesis" and field == "calculation_method"),
            thresholds=t,
        )
    return decisions
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_doubt_router.py -v`
  - Expected: PASS (10 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/intake/doubt_router.py tests/featuregen/intake/test_doubt_router.py && git commit -m "feat(intake): deterministic Doubt Router (auto-resolve vs must-ask; config-gated, biased to asking)"`

---

### Task 5.3: `critique.py` — the Critique `CONTRACT_REVIEW` mode (challenger, feeds the router)

**Files:**
- Create: `src/featuregen/intake/critique.py`
- Modify: `tests/featuregen/intake/conftest.py` (**R18** — CREATED by P1; P5 MERGES its fixtures in, never re-creates it)
- Test: `tests/featuregen/intake/test_critique.py`

**Interfaces:**
- Consumes: `intake.llm.{LLMRequest, call_llm}` (P3); `intake.store.append_feature_contract_event` (**R1**, P1); `contracts.{DbConn, IdentityEnvelope}`.
- Produces:
  - `CritiqueFinding(severity, category, evidence, recommendation, blocks_progress, field=None)` — frozen; the structured `CONTRACT_REVIEW` finding shape (spec §6.4).
  - `CritiqueResult(review_type, status, findings: tuple[CritiqueFinding, ...], call_ref)`.
  - `contract_review(conn, client, draft_semantics, *, run_id, actor, catalog_metadata=None, prompt_id="contract_review", prompt_version=1) -> CritiqueResult` — one **challenger-only** LLM pass over the PII-free structured Draft semantics via `call_llm` (event-sourced), then emits a `CONTRACT_CRITIQUED` domain shadow on the `feature_contract` aggregate. It only *raises* doubts — it never confirms, lowers a doubt, or rewrites the contract.
  - `apply_critique(routing: dict[str, str], critique: CritiqueResult) -> dict[str, str]` — each `blocks_progress:true` finding **ORs** its field to `"human"` (spec §6.4); a finding without a `field` never lowers anything.

- [ ] **Step 1 — MODIFY the shared intake test conftest (created by P1, R18)**

The ONE `tests/featuregen/intake/conftest.py` is **created by P1** and already provides the `db` alias
fixture, the **autouse SP-2 event-type registration**, and the four collaborator-seam fixtures
(`llm_client`, `intent_redactor`, `candidate_generator`, `intake_catalog`). P5 does **not** re-create it
(R18) — it **merges in** the Layer-2 additions below: the `register_sp2_commands()` line inside the
autouse fixture (so the command catalog is present once P4/P5 register commands), the `sp2_schemas`
content-schema fixture, and the `owner`/`agent` identity fixtures.

```python
# tests/featuregen/intake/conftest.py  — P5 MERGES these into the P1-created conftest (do NOT re-create it)
from featuregen.documents.registry import DocumentSchemaRegistry
from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.intake.commands import register_sp2_commands
from featuregen.intake.contract import register_contract_schemas


# P1's autouse `_register_sp2_runtime` already re-registers the SP-2 FC event schemas each test (the
# root harness resets the event registry). P5 ADDS the command-catalog registration (idempotent) to it:
#     register_sp2_commands()   # ← append inside P1's autouse _register_sp2_runtime fixture


@pytest.fixture
def sp2_schemas(db):
    # The DRAFT/LEDGER/CONFIRMED content-schemas + the four structural LLM output-schemas
    # (structure_intent/contract_review/generate_candidates/renormalize) that call_llm resolves.
    register_contract_schemas(DocumentSchemaRegistry(db))
    return db


@pytest.fixture
def owner():
    return build_human_identity(subject="user:raj", role_claims=("data_scientist",))


@pytest.fixture
def agent():
    return build_service_identity(
        subject="service:intake-agent", role_claims=("intake-agent",), attestation="sig"
    )
```

- [ ] **Step 2 — write the failing test**

```python
# tests/featuregen/intake/test_critique.py
from psycopg.rows import dict_row

from featuregen.contracts import LLMResult  # re-exported by intake.llm; see note below
from featuregen.intake.critique import CritiqueResult, apply_critique, contract_review
from featuregen.intake.store import append_feature_contract_event as append_fc_event, load_feature_contract


class ScriptedLLM:
    """A raw LLMClient double (spec §9.1). call_llm wraps it: it egress-guards, records the llm_call,
    stamps `call_ref`, emits LLM_CALL_RECORDED, and returns the LLMResult. Mirrors how SP-1 Phase-4
    tests use their own CatalogAdapter double rather than importing the Phase-3 fixture."""

    def __init__(self, output, *, self_reported_scores=None, status="ok"):
        self._output = output
        self._scores = self_reported_scores or {}
        self._status = status

    def call(self, request):
        from featuregen.intake.llm import LLMResult as _R
        return _R(output=self._output, self_reported_scores=self._scores, call_ref="", status=self._status)


def _seed_contract(db, agent, run_id="run_crit"):
    append_fc_event(
        db, run_id=run_id, type="INTENT_SUBMITTED",
        payload={"request_id": "req_crit", "run_id": run_id, "intake_mode": "definition",
                 "raw_input_ref": "blob_x", "raw_input_classification": "clean",
                 "classification": {"outcome": "CLEAR", "catalog_version": "bdc-1"}},
        actor=agent, expected_version=0,
    )
    return run_id


def test_contract_review_records_findings_and_emits_domain_shadow(db, sp2_schemas, agent):
    run_id = _seed_contract(db, agent)
    client = ScriptedLLM({
        "review_type": "CONTRACT_REVIEW", "status": "NEEDS_REVIEW",
        "findings": [{
            "severity": "HIGH", "category": "AMBIGUOUS_DEFINITION", "field": "filters",
            "evidence": "'declined' could mean issuer-declined, expired, or fraud-blocked.",
            "recommendation": "Ask the requester to confirm the declined-status encoding.",
            "blocks_progress": True,
        }],
    })
    result = contract_review(
        db, client, {"entity": "customer", "filters": [{"concept": "declined card authorization"}]},
        run_id=run_id, actor=agent,
    )
    assert isinstance(result, CritiqueResult)
    assert result.status == "NEEDS_REVIEW"
    assert result.findings[0].field == "filters"
    assert result.findings[0].blocks_progress is True
    assert result.call_ref  # call_llm stamped the llm_call reference
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CONTRACT_CRITIQUED" in types
    assert "LLM_CALL_RECORDED" in types  # call_llm event-sourced the call


def test_apply_critique_ors_blocking_findings_to_human():
    routing = {"filters": "auto", "windows": "auto"}
    crit = CritiqueResult(
        review_type="CONTRACT_REVIEW", status="NEEDS_REVIEW", call_ref="llmc_1",
        findings=(
            __import__("featuregen.intake.critique", fromlist=["CritiqueFinding"]).CritiqueFinding(
                severity="HIGH", category="AMBIGUOUS_DEFINITION", evidence="e",
                recommendation="r", blocks_progress=True, field="filters",
            ),
        ),
    )
    out = apply_critique(routing, crit)
    assert out["filters"] == "human"  # forced to must-ask
    assert out["windows"] == "auto"   # untouched


def test_apply_critique_never_lowers_a_doubt():
    routing = {"filters": "human"}
    crit = CritiqueResult("CONTRACT_REVIEW", "OK", (), "llmc_2")  # no findings
    assert apply_critique(routing, crit)["filters"] == "human"  # challenger can only raise doubts
```

> Note: if `LLMResult` is not re-exported from `featuregen.contracts`, import it from `featuregen.intake.llm` instead (the double already imports it there); it is the P3 `LLMResult`.

- [ ] **Step 3 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_critique.py -v`
  - Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.intake.critique'`.

- [ ] **Step 4 — minimal implementation**

```python
# src/featuregen/intake/critique.py
from __future__ import annotations

from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from featuregen.contracts import DbConn, IdentityEnvelope
from featuregen.intake.store import append_feature_contract_event as append_fc_event
from featuregen.intake.llm import LLMRequest, call_llm

# Pinned generation settings for the challenger pass (part of the llm_call idempotency key, §9.3).
_REVIEW_SETTINGS = {"provider": "fake", "model": "fake-structured", "max_tokens": 2048}


@dataclass(frozen=True, slots=True)
class CritiqueFinding:
    severity: str        # HIGH | MEDIUM | LOW
    category: str        # e.g. AMBIGUOUS_DEFINITION | CONTRADICTION | SCOPE
    evidence: str
    recommendation: str
    blocks_progress: bool
    field: str | None = None  # the field a blocking finding forces to must-ask (§6.4)


@dataclass(frozen=True, slots=True)
class CritiqueResult:
    review_type: str
    status: str
    findings: tuple[CritiqueFinding, ...]
    call_ref: str


def contract_review(
    conn: DbConn,
    client,
    draft_semantics: Mapping[str, Any],
    *,
    run_id: str,
    actor: IdentityEnvelope,
    catalog_metadata: Mapping[str, Any] | None = None,
    prompt_id: str = "contract_review",
    prompt_version: int = 1,
) -> CritiqueResult:
    """The Critique `CONTRACT_REVIEW` mode (spec §6.4). A single event-sourced LLM pass over the
    PII-free STRUCTURED Draft semantics (no raw intent text → no redaction needed; call_llm still
    egress-guards). It is a CHALLENGER, never a gate: it may only raise doubts / add open questions;
    it never confirms, lowers a doubt, or rewrites the contract. Emits a CONTRACT_CRITIQUED domain
    shadow on the feature_contract aggregate."""
    request = LLMRequest(
        task="contract_review",
        prompt_id=prompt_id,
        prompt_version=prompt_version,
        inputs={"draft_semantics": dict(draft_semantics), "catalog_metadata": dict(catalog_metadata or {})},
        output_schema_id="contract_review",
        output_schema_version=1,
        generation_settings=dict(_REVIEW_SETTINGS),
    )
    result = call_llm(conn, client, request, run_id=run_id, actor=actor)
    findings = tuple(
        CritiqueFinding(
            severity=str(f.get("severity", "LOW")),
            category=str(f.get("category", "")),
            evidence=str(f.get("evidence", "")),
            recommendation=str(f.get("recommendation", "")),
            blocks_progress=bool(f.get("blocks_progress", False)),
            field=f.get("field"),
        )
        for f in result.output.get("findings", [])
    )
    crit = CritiqueResult(
        review_type=str(result.output.get("review_type", "CONTRACT_REVIEW")),
        status=str(result.output.get("status", "NEEDS_REVIEW")),
        findings=findings,
        call_ref=result.call_ref,
    )
    append_fc_event(
        conn, run_id=run_id, type="CONTRACT_CRITIQUED",
        payload={
            "review_type": crit.review_type,
            "status": crit.status,
            "findings": [asdict(f) for f in findings],
            "llm_call_ref": crit.call_ref,
        },
        actor=actor,
    )
    return crit


def apply_critique(routing: dict[str, str], critique: CritiqueResult) -> dict[str, str]:
    """OR each `blocks_progress:true` finding into the routing: its field becomes must-ask (§6.4).
    A challenger can only RAISE a doubt — it never lowers a `human` back to `auto`."""
    out = dict(routing)
    for f in critique.findings:
        if f.blocks_progress and f.field:
            out[f.field] = "human"
    return out
```

- [ ] **Step 5 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_critique.py -v`
  - Expected: PASS (3 tests).

- [ ] **Step 6 — commit**
  - `git add src/featuregen/intake/critique.py tests/featuregen/intake/conftest.py tests/featuregen/intake/test_critique.py && git commit -m "feat(intake): Critique CONTRACT_REVIEW mode — event-sourced challenger that feeds the Doubt Router"`

---

### Task 5.4: `mcv.py` — Minimum Contract Validation + the SP-2 lifecycle-guard predicates

**Files:**
- Create: `src/featuregen/intake/mcv.py`
- Test: `tests/featuregen/intake/test_mcv.py`

**Interfaces:**
- Consumes: `documents.draft.UNKNOWN`; `contracts.{IdentityEnvelope, CommandResult, DbConn}`; **R4** the ONE owner predicate `intake.state.actor_is_request_owner` + **R3** `intake.state.{fold_feature_contract_state, FeatureContractStatus}` (P2); **R1** `intake.store.{append_feature_contract_event, load_feature_contract}` (P1) and P4's same-package `commands.read_contract_body` (lazily, for the DB-backed wrapper only). The pure checklist + guard predicates are DB-free; `run_minimum_contract_validation` is the one DB-backed symbol.
- Produces:
  - `MCVResult(passed: bool, failures: tuple[str, ...])`.
  - **R5** — the two MCV symbols. Pure: `minimum_contract_validated(draft_body, ledger_body, classification, *, mode="definition", candidate_count=0, confirmed_fields=()) -> MCVResult` — the 6-check deterministic pre-gate checklist (spec §6.7), fail-closed on an absent/unversioned classification (check 5); the canonical `minimum_contract_validated(draft_body, ledger, classification)` 3-arg call is valid (the extras are optional keyword-only). DB-backed: `run_minimum_contract_validation(conn, run_id, *, actor) -> CommandResult` — folds the `feature_contract` status (**R3** `fold_feature_contract_state`), loads the current draft/ledger/classification, calls the pure checklist, appends `MINIMUM_CONTRACT_VALIDATED` on a pass; **P7 Task 7.6 reads `.accepted`.**
  - Lifecycle-guard predicates (evaluated inline by later handlers, spec §11): `open_fields_empty(draft_body)`, `not_prohibited_intent(classification)`, `calculation_method_available(draft_body, *, mode, candidate_count)`, `confirmer_is_requester_human(state, actor) = actor_is_request_owner(state, actor) ∧ actor_kind=="human"` (built on the **R4** `intake.state` predicate — mcv.py does NOT redefine `actor_is_request_owner`, it imports it from `intake.state`).

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_mcv.py
from types import SimpleNamespace

from featuregen.identity.build import build_human_identity, build_service_identity
from featuregen.intake.mcv import (
    calculation_method_available,
    confirmer_is_requester_human,
    minimum_contract_validated,
    not_prohibited_intent,
    open_fields_empty,
    run_minimum_contract_validation,
)
# actor_is_request_owner is owned by P2 (intake.state), consumed here (R4) — never redefined in mcv.
from featuregen.intake.state import actor_is_request_owner

_CLEAR = {"outcome": "CLEAR", "catalog_version": "bdc-1"}


def _draft(**over):
    body = {
        "feature_semantics": {
            "entity": "customer",
            "entity_grain": ["customer_id", "as_of_date"],
            "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date"},
            "calculation_method": "rolling_count",
            "windows": [{"name": "lookback", "value": "90d"}],
            "filters": [{"concept": "declined card authorization",
                         "predicate": "card_authorizations.auth_result = 'D'"}],
        },
        "field_scores": {
            "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
            "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
            "filters": {"ambiguity": 0.10, "confidence": 0.90, "source": "catalog"},
        },
        "open_fields": [],
    }
    body.update(over)
    return body


def _ledger(fields=("entity_grain",)):
    return {"request_id": "req_1",
            "assumptions": [{"field": f, "value": "v", "rationale": "r", "source": "default"} for f in fields]}


def test_definition_contract_passes_all_six_checks():
    res = minimum_contract_validated(
        _draft(), _ledger(), _CLEAR, mode="definition", candidate_count=0,
        confirmed_fields={"filters"},
    )
    assert res.passed is True, res.failures


def test_open_fields_nonempty_fails():
    res = minimum_contract_validated(
        _draft(open_fields=["filters.declined_status_encoding"]), _ledger(), _CLEAR,
        mode="definition", candidate_count=0,
    )
    assert res.passed is False
    assert "open_fields_nonempty" in res.failures


def test_unresolved_grain_fails():
    d = _draft()
    d["feature_semantics"]["entity_grain"] = ["UNKNOWN"]
    res = minimum_contract_validated(d, _ledger(), _CLEAR, mode="definition", candidate_count=0)
    assert "grain_unresolved" in res.failures


def test_prohibited_class_blocks_mcv():
    res = minimum_contract_validated(
        _draft(), _ledger(), {"outcome": "PROHIBITED_DATA_CLASS", "catalog_version": "bdc-1", "matched_class": "race"},
        mode="definition", candidate_count=0, confirmed_fields={"filters"},
    )
    assert res.passed is False
    assert any(f.startswith("blocked:") for f in res.failures)


def test_unavailable_classification_fails_closed():
    res = minimum_contract_validated(_draft(), _ledger(), None, mode="definition", candidate_count=0,
                                     confirmed_fields={"filters"})
    assert "classification_unavailable" in res.failures
    res2 = minimum_contract_validated(_draft(), _ledger(), {"outcome": "CLEAR"}, mode="definition",
                                      candidate_count=0, confirmed_fields={"filters"})
    assert "classification_unavailable" in res2.failures  # no resolvable version


def test_hypothesis_requires_a_candidate_set():
    d = _draft()
    d["feature_semantics"]["calculation_method"] = "UNKNOWN"
    assert calculation_method_available(d, mode="hypothesis", candidate_count=3) is True
    assert calculation_method_available(d, mode="hypothesis", candidate_count=0) is False
    res = minimum_contract_validated(d, _ledger(), _CLEAR, mode="hypothesis", candidate_count=0,
                                     confirmed_fields={"filters"})
    assert "calculation_method_unavailable" in res.failures


def test_high_ambiguity_field_without_account_fails_check_3():
    d = _draft()
    d["field_scores"]["filters"] = {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"}
    # filters neither in the ledger nor human-confirmed → check 3 fails
    res = minimum_contract_validated(d, _ledger(("entity_grain",)), _CLEAR, mode="definition",
                                     candidate_count=0, confirmed_fields=set())
    assert any(f.startswith("high_ambiguity_unaccounted") for f in res.failures)


def test_platform_supplied_field_needs_a_ledger_entry_check_6():
    # entity_grain has source=default (platform-supplied) but NO ledger entry → check 6 fails
    res = minimum_contract_validated(_draft(), _ledger(fields=()), _CLEAR, mode="definition",
                                     candidate_count=0, confirmed_fields={"filters"})
    assert any(f.startswith("unaccounted:") for f in res.failures)


def test_owner_and_confirmer_guards():
    owner = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
    other = build_human_identity(subject="user:mallory", role_claims=("data_scientist",))
    svc = build_service_identity(subject="service:intake-agent", role_claims=("intake-agent",), attestation="s")
    # The ONE owner predicate is state-based (R4): actor_is_request_owner(state, actor); `state.requester`
    # is set by the P2 fold to the INTENT_SUBMITTED actor.subject. A folded state exposes `.requester`.
    raj_state = SimpleNamespace(requester="user:raj")
    svc_state = SimpleNamespace(requester="service:intake-agent")
    assert actor_is_request_owner(raj_state, owner) is True
    assert actor_is_request_owner(raj_state, other) is False
    assert confirmer_is_requester_human(raj_state, owner) is True
    assert confirmer_is_requester_human(raj_state, other) is False  # a different data scientist can't confirm
    assert confirmer_is_requester_human(svc_state, svc) is False    # a service can never confirm
    assert not_prohibited_intent(_CLEAR) is True
    assert not_prohibited_intent({"outcome": "OUT_OF_SCOPE", "catalog_version": "bdc-1"}) is False
    assert open_fields_empty(_draft()) is True


def test_run_minimum_contract_validation_folds_and_appends_the_event(db, sp2_schemas, agent):
    """R5 DB-backed MCV: fold the feature_contract status (P2 R3), load the current draft/ledger/
    classification, run the pure checklist, append MINIMUM_CONTRACT_VALIDATED on a pass; return a
    CommandResult whose `.accepted` is the boundary P7's open_gate1_task reads."""
    from featuregen.intake.commands import freeze_draft
    from featuregen.intake.store import append_feature_contract_event, load_feature_contract

    owner = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
    run_id = "run_mcv"
    # INTENT_SUBMITTED is appended by the HUMAN requester → the fold's state.requester == user:raj (R4).
    append_feature_contract_event(
        db, run_id=run_id, type="INTENT_SUBMITTED",
        payload={"request_id": "req_mcv", "run_id": run_id, "intake_mode": "definition",
                 "raw_input_ref": "blob_x", "raw_input_classification": "clean", "classification": _CLEAR},
        actor=owner, expected_version=0,
    )
    body = _draft()
    body.update({"request_id": "req_mcv", "intake_mode": "definition", "raw_input_ref": "blob_x",
                 "raw_input_classification": "clean", "proposed_feature_name": "f",
                 "assumption_ledger_ref": "", "provenance": {"schema_version": 1},
                 "status": "NEEDS_CLARIFICATION"})
    draft_doc_id, ledger_doc_id = freeze_draft(
        db, run_id=run_id, request_id="req_mcv", body=body,
        ledger_body=_ledger(("entity_grain", "filters")), actor=agent,
    )
    append_feature_contract_event(
        db, run_id=run_id, type="DRAFT_CONTRACT_PRODUCED",
        payload={"draft_doc_id": draft_doc_id, "assumption_ledger_ref": ledger_doc_id, "open_fields": []},
        actor=agent,
    )
    res = run_minimum_contract_validation(db, run_id, actor=agent)
    assert res.accepted is True, res.denied_reason
    assert "MINIMUM_CONTRACT_VALIDATED" in [e.type for e in load_feature_contract(db, run_id)]
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_mcv.py -v`
  - Expected: FAIL — `ModuleNotFoundError: No module named 'featuregen.intake.mcv'`.

- [ ] **Step 3 — minimal implementation**

```python
# src/featuregen/intake/mcv.py
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from featuregen.contracts import CommandResult, DbConn, IdentityEnvelope
from featuregen.documents.draft import UNKNOWN
# R4: the ONE owner predicate is owned by P2 (intake.state) — mcv IMPORTS it, never redefines it.
# R3: the DB-backed wrapper folds the feature_contract status through the P2 fold.
from featuregen.intake.state import (
    FeatureContractStatus,
    actor_is_request_owner,
    fold_feature_contract_state,
)

# Classification outcomes that terminally block a contract (string values of banking_catalog.IntakeOutcome).
_BLOCKING_OUTCOMES = ("OUT_OF_SCOPE", "PROHIBITED_DATA_CLASS")
# Sources whose value the PLATFORM supplied (not the intent/human) → must carry a ledger account (§5.3, check 6).
_PLATFORM_SOURCES = ("default", "catalog")
_HIGH_AMBIGUITY = 0.30


@dataclass(frozen=True, slots=True)
class MCVResult:
    passed: bool
    failures: tuple[str, ...]


def _ledger_fields(ledger_body: Mapping[str, Any]) -> set[str]:
    return {str(a.get("field")) for a in ledger_body.get("assumptions", [])}


def _is_unknown(value: Any) -> bool:
    if value == UNKNOWN:
        return True
    if isinstance(value, list):
        return not value or any(v == UNKNOWN for v in value)
    return value in (None, "")


def open_fields_empty(draft_body: Mapping[str, Any]) -> bool:
    """Guard `open_fields_empty` (§11): a Draft with any open field can never pass Gate #1 (§3.5)."""
    return not draft_body.get("open_fields")


def not_prohibited_intent(classification: Mapping[str, Any] | None) -> bool:
    """Guard `not_prohibited_intent` (§11): fail-closed if the classification is absent."""
    return classification is not None and classification.get("outcome") not in _BLOCKING_OUTCOMES


def calculation_method_available(
    draft_body: Mapping[str, Any], *, mode: str, candidate_count: int
) -> bool:
    """MCV #2 / guard `calculation_method_available` (§6.7): in definition mode the single faithful
    method is present and non-UNKNOWN; in hypothesis mode a NON-EMPTY scored candidate set exists
    pre-gate (the human selects one AT Gate #1 — this does NOT assert `chosen` is already set)."""
    if mode == "hypothesis":
        return candidate_count >= 1
    method = draft_body.get("feature_semantics", {}).get("calculation_method")
    return bool(method) and method != UNKNOWN


def confirmer_is_requester_human(state, actor: IdentityEnvelope) -> bool:
    """Guard `confirmer_is_requester_human` = actor_is_request_owner ∧ actor_kind=="human" (§8.2),
    built on the ONE state-based owner predicate P2 owns (R4 — `actor_is_request_owner(state, actor)`,
    where `state.requester` is the INTENT_SUBMITTED actor.subject). A service or the LLM can never
    confirm; a different data scientist can never confirm."""
    return actor.actor_kind == "human" and actor_is_request_owner(state, actor)


def minimum_contract_validated(
    draft_body: Mapping[str, Any],
    ledger_body: Mapping[str, Any],
    classification: Mapping[str, Any] | None,
    *,
    mode: str = "definition",
    candidate_count: int = 0,
    confirmed_fields: Iterable[str] = (),
) -> MCVResult:
    """The deterministic 6-check pre-gate checklist (spec §6.7). **R5** pure form — the canonical
    `minimum_contract_validated(draft_body, ledger, classification)` 3-arg call is valid (the extras are
    optional keyword-only; the DB-backed `run_minimum_contract_validation` supplies them). Pure and
    machine-checkable —
    evaluated INLINE by `open_gate1_task` (P7) against the folded status, NOT the state-machine
    engine. A failure keeps the run in the Refinement Loop; success emits MINIMUM_CONTRACT_VALIDATED.

    Accountable = has a ledger entry OR was human-confirmed (`confirmed_fields`). §5.3's no-silent-
    assumption rule."""
    failures: list[str] = []
    sem = draft_body.get("feature_semantics", {})
    ledger = _ledger_fields(ledger_body)
    accountable = ledger | set(confirmed_fields)
    field_scores = draft_body.get("field_scores", {})

    # 1) Grain resolved — entity + the grain the DRAFT carries (entity_grain), non-UNKNOWN.
    if _is_unknown(sem.get("entity")) or _is_unknown(sem.get("entity_grain")):
        failures.append("grain_unresolved")

    # 2) A calculation method is available for selection (mode-specific, §6.7 #2).
    if not calculation_method_available(draft_body, mode=mode, candidate_count=candidate_count):
        failures.append("calculation_method_unavailable")

    # 3) No unresolved high-ambiguity field: open_fields empty AND no ambiguity > 0.30 left unaccounted.
    if draft_body.get("open_fields"):
        failures.append("open_fields_nonempty")
    else:
        for field, sc in field_scores.items():
            if float(sc.get("ambiguity", 0.0)) > _HIGH_AMBIGUITY and field not in accountable:
                failures.append(f"high_ambiguity_unaccounted:{field}")

    # 4) Observation intent present (so SP-3 can bind point-in-time).
    oi = sem.get("observation_intent") or {}
    if _is_unknown(oi.get("kind")):
        failures.append("observation_intent_missing")

    # 5) In banking scope — fail-closed on absent/unversioned classification (§4.5(b)); else not blocked.
    if classification is None or classification.get("catalog_version") in (None, ""):
        failures.append("classification_unavailable")
    elif classification.get("outcome") in _BLOCKING_OUTCOMES:
        failures.append(f"blocked:{classification.get('outcome')}")

    # 6) Every PLATFORM-supplied field is accountable (§5.3): a default/catalog value MUST be in the
    #    ledger or human-confirmed. Verbatim (source=llm) fields are accounted by the intent itself.
    for field, sc in field_scores.items():
        if sc.get("source") in _PLATFORM_SOURCES and field not in accountable:
            failures.append(f"unaccounted:{field}")

    return MCVResult(passed=not failures, failures=tuple(failures))


def run_minimum_contract_validation(conn: DbConn, run_id: str, *, actor) -> CommandResult:
    """**R5** DB-backed MCV — the boundary guard P7's `open_gate1_task` reads via `.accepted`. Folds the
    `feature_contract` status (**R3** `fold_feature_contract_state`), loads the current draft/ledger/
    classification, runs the pure 6-check checklist, and appends `MINIMUM_CONTRACT_VALIDATED` on a pass.
    Reads the recorded classification mapping (**R9** `.catalog_version`) off the folded state. All
    appends go through the **R1** `intake.store` seam."""
    # Lazy import of the P4 body-read seam (same package) to avoid a commands↔mcv import cycle.
    from featuregen.intake.commands import _candidate_count, read_contract_body
    from featuregen.intake.store import append_feature_contract_event, load_feature_contract

    stream = load_feature_contract(conn, run_id)
    state = fold_feature_contract_state(stream)
    # No-regression guard (mirrors overlay/confirmation_commands.py): a fold already at/past MCV or
    # CONFIRMED does not re-append — idempotent accept.
    if state.status in (FeatureContractStatus.MINIMUM_CONTRACT_VALIDATED, FeatureContractStatus.CONFIRMED):
        return CommandResult(accepted=True, aggregate_id=run_id)

    draft_body = read_contract_body(conn, state.draft_doc_id)
    ledger_ref = state.assumption_ledger_ref or draft_body.get("assumption_ledger_ref")
    ledger_body = (
        read_contract_body(conn, ledger_ref) if ledger_ref
        else {"request_id": state.request_id, "assumptions": []}
    )
    # Human-answered fields are accountable (§5.3) — the fields the requester confirmed via clarification.
    confirmed_fields = {
        e.payload.get("field") for e in stream if e.type == "CLARIFICATION_ANSWERED"
    }
    candidate_count = _candidate_count(conn, run_id) if state.intake_mode == "hypothesis" else 0

    res = minimum_contract_validated(
        draft_body, ledger_body, state.classification, mode=state.intake_mode,
        candidate_count=candidate_count, confirmed_fields=confirmed_fields,
    )
    if not res.passed:
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason="mcv_failed: " + ",".join(res.failures),
        )
    append_feature_contract_event(
        conn, run_id=run_id, type="MINIMUM_CONTRACT_VALIDATED",
        payload={"draft_doc_id": state.draft_doc_id}, actor=actor,
    )
    return CommandResult(accepted=True, aggregate_id=run_id)
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_mcv.py -v`
  - Expected: PASS (10 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/intake/mcv.py tests/featuregen/intake/test_mcv.py && git commit -m "feat(intake): Minimum Contract Validation (6-check pre-gate checklist) + SP-2 lifecycle-guard predicates"`

---

### Task 5.5: `commands.py` — the Human Clarification task + the bounded Refinement Loop

**Files:**
- Modify: `src/featuregen/intake/commands.py` (append `open_clarification_task`, `refine_contract`, the `IntakeDeps` accessor, and the small `feature_contract`-stream readers)
- Test: `tests/featuregen/intake/test_refine_contract.py`

**Interfaces:**
- Consumes: SP-0 `gates/tasks.py::open_task`; `contracts.gates.GateTaskSpec` (`delegation_allowed=False`); `aggregates/run_lifecycle.py::park_command`; `contracts.Command`; **R1** `intake.store.{append_feature_contract_event, load_feature_contract}` (P1); **R3** `intake.state.fold_feature_contract_state` (P2 — refine derives the owner from `state.requester`); P3 `intake.llm.{LLMRequest, call_llm}` + `intake.redaction.IntentRedactor`; P4 same-module `freeze_draft`/`read_contract_body`; `scoring.score_fields`; `doubt_router.{route_draft, default_thresholds}`; `critique.{contract_review, apply_critique}`; `mcv.minimum_contract_validated`.
- Produces:
  - `open_clarification_task(conn, *, run_id, request_id, draft_doc_id, field, question, owner_subject, actor, candidate_readings=()) -> str` — opens an SP-0 `CLARIFICATION` gate task (`allowed_responses=[confirm,edit,reject]`, `required_inputs=[draft_doc_id]` so a re-normalized draft stales it, `eligible_assignees={role:data_scientist, subject:owner}`, **`delegation_allowed=False`**), then emits `CLARIFICATION_REQUESTED{task_id, field, question, routed_to:"human", draft_doc_id}` on the feature_contract aggregate.
  - `refine_contract(conn, run_id, *, client=None, redactor=None, catalog=None, actor, thresholds=None, max_rounds=MAX_REFINEMENT_ROUNDS) -> RefineResult` — one bounded refinement round (spec §6.6): renormalize (if there are unfolded answers) → rescore → re-critique → re-route → auto-resolve safe fields (ledger + `FIELD_AUTO_RESOLVED`) → freeze the revised Draft (`CONTRACT_REFINED`) → open must-ask tasks; when no open field remains → run MCV → `MINIMUM_CONTRACT_VALIDATED`; when the round budget is exhausted → **auto-park** (SP-0 `park`). Defaults resolve deps from the P5 accessors.
  - `RefineResult(status, draft_doc_id, open_fields, mcv)` (`status ∈ {clarifying, validated, mcv_failed, parked}`); `MAX_REFINEMENT_ROUNDS` (config-gated: `FEATUREGEN_MAX_REFINEMENT_ROUNDS`, default 5); `IntakeDeps` + `register_intake_deps(*, client, redactor, catalog)` / `current_intake_deps() -> IntakeDeps | None`.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_refine_contract.py
from psycopg.rows import dict_row

from featuregen.identity.build import build_human_identity
from featuregen.intake.commands import RefineResult, open_clarification_task, refine_contract
from featuregen.intake.store import append_feature_contract_event as append_fc_event, load_feature_contract
from featuregen.intake.redaction import DefaultIntentRedactor

# R4: the request owner is the INTENT_SUBMITTED event actor.subject (state.requester) — never a payload
# key. INTENT_SUBMITTED is issued by the HUMAN requester, so the P2 fold reads the owner from the event.
OWNER = build_human_identity(subject="user:raj", role_claims=("data_scientist",))


class ScriptedLLM:
    """LLMClient double: returns a canned structured output per task ("contract_review" / "renormalize")."""

    def __init__(self, by_task):
        self._by_task = by_task

    def call(self, request):
        from featuregen.intake.llm import LLMResult
        spec = self._by_task[request.task]
        return LLMResult(
            output=spec.get("output", {}),
            self_reported_scores=spec.get("self_reported_scores", {}),
            call_ref="", status="ok",
        )


class _View:
    def candidate_count(self, concept):
        return {"declined card authorization": 3}.get(concept, 1)

    def metadata(self):
        return {}


def _semantics(filter_predicate="UNKNOWN"):
    return {
        "entity": "customer",
        "entity_grain": ["customer_id", "as_of_date"],
        "observation_intent": {"kind": "point_in_time", "as_of_field": "as_of_date"},
        "calculation_method": "rolling_count",
        "windows": [{"name": "lookback", "value": "90d"}],
        "filters": [{"concept": "declined card authorization", "predicate": filter_predicate}],
    }


def _seed_draft(db, agent, *, run_id="run_ref", open_fields=("filters.declined_status_encoding",)):
    # R4: INTENT_SUBMITTED is appended by the HUMAN requester (OWNER) → the P2 fold sets state.requester
    # == "user:raj", the owner the Refinement Loop scopes clarification tasks to (never a payload key).
    append_fc_event(
        db, run_id=run_id, type="INTENT_SUBMITTED",
        payload={"request_id": "req_ref", "run_id": run_id, "intake_mode": "definition",
                 "raw_input_ref": "blob_x", "raw_input_classification": "clean",
                 "classification": {"outcome": "CLEAR", "catalog_version": "bdc-1"}},
        actor=OWNER, expected_version=0,
    )
    ledger = {"request_id": "req_ref", "assumptions": [
        {"field": "entity_grain", "value": ["customer_id", "as_of_date"], "rationale": "pit convention",
         "source": "default", "ambiguity": 0.30, "confidence": 0.72}]}
    body = {
        "request_id": "req_ref", "intake_mode": "definition", "raw_input_ref": "blob_x",
        "raw_input_classification": "clean", "proposed_feature_name": "declined_card_auth_count_90d",
        "feature_semantics": _semantics(),
        "field_scores": {
            "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
            "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
            "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},
            "filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"},
        },
        "open_fields": list(open_fields), "assumption_ledger_ref": "", "provenance": {"schema_version": 1},
        "status": "NEEDS_CLARIFICATION",
    }
    draft_doc_id, ledger_doc_id = freeze_draft(
        db, run_id=run_id, request_id="req_ref", body=body, ledger_body=ledger, actor=agent
    )
    append_fc_event(db, run_id=run_id, type="DRAFT_CONTRACT_PRODUCED",
                    payload={"draft_doc_id": draft_doc_id, "assumption_ledger_ref": ledger_doc_id,
                             "open_fields": list(open_fields)}, actor=agent)
    return run_id, draft_doc_id


def _no_review():
    return {"output": {"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []}}


def test_open_clarification_task_is_owner_scoped_and_delegation_off(db, sp2_schemas, agent):
    run_id, draft_doc_id = _seed_draft(db, agent)
    task_id = open_clarification_task(
        db, run_id=run_id, request_id="req_ref", draft_doc_id=draft_doc_id,
        field="filters", question="Which column marks a declined auth?", owner_subject="user:raj", actor=agent,
    )
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT gate, eligible_assignees, allowed_responses, delegation_allowed, required_inputs, run_id "
            "FROM human_tasks WHERE task_id=%s", (task_id,)
        )
        row = cur.fetchone()
    assert row["gate"] == "CLARIFICATION"
    assert row["eligible_assignees"] == {"role": "data_scientist", "subject": "user:raj"}
    assert sorted(row["allowed_responses"]) == ["confirm", "edit", "reject"]
    assert row["delegation_allowed"] is False        # author-owned intent lock (§6.5, §8.2)
    assert row["required_inputs"] == [draft_doc_id]   # a re-normalized draft stales the pending answer
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CLARIFICATION_REQUESTED" in types


def test_initial_refine_opens_a_must_ask_task_for_the_open_field(db, sp2_schemas, agent):
    run_id, _ = _seed_draft(db, agent)
    client = ScriptedLLM({"contract_review": _no_review()["output"] and {"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []}})
    res = refine_contract(db, run_id, client=client, redactor=DefaultIntentRedactor(),
                          catalog=_View(), actor=agent)
    assert isinstance(res, RefineResult)
    assert res.status == "clarifying"
    assert "filters.declined_status_encoding" in res.open_fields
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM human_tasks WHERE run_id=%s AND status='open'", (run_id,))
        assert cur.fetchone()["n"] == 1


def test_answered_field_renormalizes_to_mcv_validated(db, sp2_schemas, agent):
    run_id, _ = _seed_draft(db, agent)
    # a prior human answer is pinned on the stream (as answer_clarification would emit, Task 5.6)
    append_fc_event(db, run_id=run_id, type="CLARIFICATION_ANSWERED",
                    payload={"task_id": "task_x", "field": "filters",
                             "answer": "card_authorizations.auth_result = 'D'", "response": "confirm",
                             "answered_by": "user:raj"}, actor=agent)
    client = ScriptedLLM({
        "renormalize": {
            "output": {"feature_semantics": _semantics("card_authorizations.auth_result = 'D'"),
                       "open_fields": []},
            "self_reported_scores": {
                "entity": {"ambiguity": 0.05, "confidence": 0.97, "source": "llm"},
                "entity_grain": {"ambiguity": 0.30, "confidence": 0.72, "source": "default"},
                "windows": {"ambiguity": 0.05, "confidence": 0.98, "source": "llm"},
                "filters": {"ambiguity": 0.10, "confidence": 0.92, "source": "llm"},
            },
        },
        "contract_review": {"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []},
    })
    res = refine_contract(db, run_id, client=client, redactor=DefaultIntentRedactor(),
                          catalog=_View(), actor=agent)
    assert res.status == "validated", res
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CONTRACT_REFINED" in types
    assert "MINIMUM_CONTRACT_VALIDATED" in types


def test_refinement_loop_is_bounded_and_auto_parks(db, sp2_schemas, agent, monkeypatch):
    import featuregen.intake.commands as cmds
    monkeypatch.setattr(cmds, "MAX_REFINEMENT_ROUNDS", 1)
    run_id, _ = _seed_draft(db, agent)
    # An answer that does NOT resolve the open field (renormalize keeps it UNKNOWN) → the loop cannot
    # converge; with the round budget = 1 the SECOND refine auto-parks instead of looping forever.
    append_fc_event(db, run_id=run_id, type="CLARIFICATION_ANSWERED",
                    payload={"task_id": "t1", "field": "filters", "answer": "still unclear",
                             "response": "confirm", "answered_by": "user:raj"}, actor=agent)
    client = ScriptedLLM({
        "renormalize": {"output": {"feature_semantics": _semantics("UNKNOWN"),
                                   "open_fields": ["filters.declined_status_encoding"]},
                        "self_reported_scores": {"filters": {"ambiguity": 0.80, "confidence": 0.40, "source": "llm"}}},
        "contract_review": {"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []},
    })
    refine_contract(db, run_id, client=client, redactor=DefaultIntentRedactor(), catalog=_View(), actor=agent)
    res = refine_contract(db, run_id, client=client, redactor=DefaultIntentRedactor(), catalog=_View(), actor=agent)
    assert res.status == "parked"
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT count(*) AS n FROM events WHERE aggregate='run' AND run_id=%s AND type='RUN_PARKED'",
                    (run_id,))
        assert cur.fetchone()["n"] >= 1
```

> The test imports `freeze_draft` from `featuregen.intake.commands` (the P4 seam in the same module); add it to the test's imports alongside the P5 symbols.

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_refine_contract.py -v`
  - Expected: FAIL — `ImportError: cannot import name 'refine_contract' from 'featuregen.intake.commands'`.

- [ ] **Step 3 — minimal implementation** (append to `commands.py`)

```python
# --- append to src/featuregen/intake/commands.py -------------------------------------------------
from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import UTC, datetime

from featuregen.aggregates.run_lifecycle import park_command
from featuregen.contracts import Command, CommandResult, DbConn
from featuregen.contracts.gates import GateTaskSpec
from featuregen.documents.draft import UNKNOWN
from featuregen.gates.tasks import open_task
from featuregen.intake.critique import apply_critique, contract_review
from featuregen.intake.doubt_router import default_thresholds, route_draft
from featuregen.intake.store import append_feature_contract_event as append_fc_event, load_feature_contract
from featuregen.intake.llm import LLMRequest, call_llm
from featuregen.intake.mcv import minimum_contract_validated
from featuregen.intake.scoring import score_fields
# R3/R4: the ONE feature_contract fold + owner predicate are owned by P2 (intake.state); consumed here
# (refine derives the request owner from state.requester; answer_clarification calls actor_is_request_owner).
from featuregen.intake.state import actor_is_request_owner, fold_feature_contract_state
from featuregen.security.audit import record_denial

# Round budget for the Contract Refinement Loop (Decision 6, spec §6.6) — bounded by SP-0's durable
# hard-loop-limit posture, config-gated; on exhaustion the run auto-parks for human follow-up.
MAX_REFINEMENT_ROUNDS = int(os.environ.get("FEATUREGEN_MAX_REFINEMENT_ROUNDS", "5"))
_REFINEMENT_PARK_OWNER = "governance:intake-refinement"
_RENORM_SETTINGS = {"provider": "fake", "model": "fake-structured", "max_tokens": 2048}


@dataclass(frozen=True, slots=True)
class RefineResult:
    status: str                 # clarifying | validated | mcv_failed | parked
    draft_doc_id: str
    open_fields: tuple[str, ...]
    mcv: object | None          # MCVResult when a checklist ran, else None


@dataclass(frozen=True, slots=True)
class IntakeDeps:
    client: object      # LLMClient (§9.1)
    redactor: object    # IntentRedactor (§9.4)
    catalog: object     # CatalogView (scoring seam, §6.1)


_INTAKE_DEPS: IntakeDeps | None = None


def register_intake_deps(*, client, redactor, catalog) -> None:
    """Single-source registration of the Layer-2 runtime deps (LLM client / redactor / merged-view
    catalog). P9 bootstrap wires FakeLLM + DefaultIntentRedactor + the SP-1 merged-view adapter; the
    auto-drive in `answer_clarification` uses these when registered."""
    global _INTAKE_DEPS
    _INTAKE_DEPS = IntakeDeps(client=client, redactor=redactor, catalog=catalog)


def current_intake_deps() -> IntakeDeps | None:
    return _INTAKE_DEPS


# ── feature_contract stream readers (folded into P8's fold_feature_contract_state later) ──────────
def _first(stream, event_type: str):
    return next((e for e in stream if e.type == event_type), None)


def _current_draft_doc_id(stream) -> str | None:
    for e in reversed(list(stream)):
        if e.type in ("CONTRACT_REFINED", "DRAFT_CONTRACT_PRODUCED"):
            return e.payload.get("draft_doc_id")
    return None


def _answered_fields(stream) -> dict[str, object]:
    """Pinned answers: {field: answer} from every CLARIFICATION_ANSWERED (last write wins). Pinning
    answered fields is what makes the loop converge (§6.6)."""
    answers: dict[str, object] = {}
    for e in stream:
        if e.type == "CLARIFICATION_ANSWERED":
            answers[e.payload["field"]] = e.payload.get("answer")
    return answers


def _requested_field(stream, task_id: str) -> str | None:
    for e in stream:
        if e.type == "CLARIFICATION_REQUESTED" and e.payload.get("task_id") == task_id:
            return e.payload.get("field")
    return None


# ── clarification task ────────────────────────────────────────────────────────────────────────────
def open_clarification_task(
    conn: DbConn,
    *,
    run_id: str,
    request_id: str,
    draft_doc_id: str,
    field: str,
    question: str,
    owner_subject: str,
    actor,
    candidate_readings: tuple = (),
) -> str:
    """Open an SP-0 CLARIFICATION human-gate task for one must-ask field (spec §6.5). The eligible
    assignee is the REQUEST OWNER (author-owned intent lock) and `delegation_allowed=False` — the
    subject guard alone is necessary but not sufficient, since SP-0's GateTaskSpec.delegation_allowed
    defaults to True and a delegate could otherwise stand in (§8.2). `required_inputs=[draft_doc_id]`
    so a later re-normalization stales any pending answer (SP-0 task staleness)."""
    spec = GateTaskSpec(
        gate="CLARIFICATION",
        required_inputs=(draft_doc_id,),
        eligible_assignees={"role": "data_scientist", "subject": owner_subject},
        allowed_responses=("confirm", "edit", "reject"),
        run_id=run_id,
        delegation_allowed=False,
    )
    task_id = open_task(conn, spec, actor)
    append_fc_event(
        conn, run_id=run_id, type="CLARIFICATION_REQUESTED",
        payload={"task_id": task_id, "field": field, "question": question, "routed_to": "human",
                 "draft_doc_id": draft_doc_id, "candidate_readings": list(candidate_readings)},
        actor=actor,
    )
    return task_id


# ── contract semantics helpers ────────────────────────────────────────────────────────────────────
def _base(path: str) -> str:
    return path.split(".", 1)[0]


def _concepts(semantics) -> dict[str, str]:
    """Concept-bearing fields for the catalog-cardinality check (§6.1). Only fields whose meaning
    binds to a catalog object / declared code get a cardinality lookup."""
    concepts: dict[str, str] = {}
    entity = semantics.get("entity")
    if entity and entity != UNKNOWN:
        concepts["entity"] = entity
    filters = semantics.get("filters") or []
    if isinstance(filters, list) and filters and isinstance(filters[0], dict):
        concept = filters[0].get("concept")
        if concept:
            concepts["filters"] = concept
    return concepts


def _policy_fields(classification, semantics) -> set[str]:
    """Policy-sensitive fields that may NEVER auto-resolve (§6.2): any sensitive-proxy field the
    classifier flagged, plus a present `target` (credit-decisioning use-cases pin the label at Gate #1)."""
    fields = set((classification or {}).get("sensitive_fields", []) or [])
    target = semantics.get("target")
    if target not in (None, UNKNOWN):
        fields.add("target")
    td = semantics.get("target_definition")
    if isinstance(td, str) and td and td != UNKNOWN and not td.startswith("N/A"):
        fields.add("target")
    return fields


def _ledger_entry(field, semantics, score) -> dict:
    return {
        "field": field,
        "value": semantics.get(field),
        "source": score["source"],
        "rationale": f"auto-resolved: {field} is low-ambiguity ({score['ambiguity']}) from {score['source']}",
        "ambiguity": score["ambiguity"],
        "confidence": score["confidence"],
        "auto_resolved_at": datetime.now(UTC).isoformat(),
    }


def _open_questions(routing, question_by_field) -> list[dict]:
    return [
        {"field": f, "question": question_by_field.get(f, f"Please specify {f}."),
         "blocks_progress": True, "routed_to": "human"}
        for f, decision in routing.items() if decision == "human"
    ]


def _candidate_count(conn: DbConn, run_id: str) -> int:
    row = conn.execute(
        "SELECT count(*) FROM documents WHERE run_id=%s AND stage=%s AND branch_role='candidate'",
        (run_id, "DRAFT_CONTRACT"),
    ).fetchone()
    return int(row[0]) if row else 0


def _redact_answers(redactor, answers, classification: str) -> dict:
    """Belt-and-suspenders: a clarification answer is human free text — redact it before it enters an
    LLM renormalize request (§9.4). call_llm additionally egress-guards the whole request."""
    out: dict[str, str] = {}
    for field, answer in answers.items():
        red = redactor.redact(str(answer), classification)
        out[field] = red.text if red.text is not None else "[REDACTED]"
    return out


# ── the bounded Contract Refinement Loop (§6.6) ───────────────────────────────────────────────────
def refine_contract(
    conn: DbConn,
    run_id: str,
    *,
    client=None,
    redactor=None,
    catalog=None,
    actor,
    thresholds=None,
    max_rounds: int | None = None,
) -> RefineResult:
    """One bounded refinement round (spec §6.6): renormalize (only if there are unfolded answers) →
    re-score → re-critique → re-route → auto-resolve safe fields → freeze the revised Draft → open
    must-ask tasks; converge to MCV when no open field remains; auto-park when the round budget is
    exhausted. Each new Draft `supersedes` the prior on the DAG (full history retained)."""
    deps = current_intake_deps()
    client = client or (deps.client if deps else None)
    redactor = redactor or (deps.redactor if deps else None)
    catalog = catalog or (deps.catalog if deps else None)
    thresholds = thresholds or default_thresholds()
    budget = MAX_REFINEMENT_ROUNDS if max_rounds is None else max_rounds

    stream = load_feature_contract(conn, run_id)
    state = fold_feature_contract_state(stream)   # R3 — the P2 fold; `state.requester` is the owner (R4)
    intent = _first(stream, "INTENT_SUBMITTED")
    request_id = intent.payload["request_id"]
    mode = intent.payload["intake_mode"]
    classification = intent.payload.get("classification")   # the recorded R9 mapping (`.catalog_version`)
    raw_class = intent.payload["raw_input_classification"]
    draft_doc_id = _current_draft_doc_id(stream)
    draft_body = read_contract_body(conn, draft_doc_id)
    ledger_body = read_contract_body(conn, draft_body["assumption_ledger_ref"]) if draft_body.get(
        "assumption_ledger_ref") else {"request_id": request_id, "assumptions": []}
    answers = _answered_fields(stream)
    rounds = sum(1 for e in stream if e.type == "CONTRACT_REFINED")

    # 1) Re-normalize only when an answer targets a field still open on the current draft.
    unfolded = [f for f in answers if any(_base(of) == f for of in draft_body.get("open_fields", []))]
    if unfolded:
        request = LLMRequest(
            task="renormalize", prompt_id="renormalize", prompt_version=1,
            inputs={"prior_semantics": draft_body["feature_semantics"],
                    "answers": _redact_answers(redactor, {f: answers[f] for f in unfolded}, raw_class),
                    "catalog_metadata": catalog.metadata()},
            output_schema_id="draft_contract", output_schema_version=1,
            generation_settings=dict(_RENORM_SETTINGS),
        )
        result = call_llm(conn, client, request, run_id=run_id, actor=actor)
        semantics = result.output["feature_semantics"]
        llm_scores = result.self_reported_scores
        open_fields = [of for of in result.output.get("open_fields", []) if _base(of) not in answers]
        rounds += 1
        renormalized = True
    else:
        semantics = draft_body["feature_semantics"]
        llm_scores = {f: dict(s) for f, s in draft_body.get("field_scores", {}).items()}
        open_fields = [of for of in draft_body.get("open_fields", []) if _base(of) not in answers]
        renormalized = False

    # 2) Re-score (LLM self-report ⊕ catalog cardinality).
    field_scores = score_fields(llm_scores, _concepts(semantics), catalog.candidate_count)

    # 3) Re-run the challenger critique and 4) route, ORing blocking findings to must-ask.
    critique = contract_review(conn, client, semantics, run_id=run_id, actor=actor,
                               catalog_metadata=catalog.metadata())
    routing = apply_critique(
        route_draft(field_scores, open_fields, mode=mode,
                    policy_sensitive_fields=_policy_fields(classification, semantics), thresholds=thresholds),
        critique,
    )

    # 5) Auto-resolve safe fields → ledger + FIELD_AUTO_RESOLVED (never a field already in the ledger
    #    or already human-answered).
    ledger_fields = {a["field"] for a in ledger_body.get("assumptions", [])}
    additions = []
    for field, decision in routing.items():
        if decision == "auto" and field not in ledger_fields and field not in answers:
            entry = _ledger_entry(field, semantics, field_scores[field])
            additions.append(entry)
            append_fc_event(conn, run_id=run_id, type="FIELD_AUTO_RESOLVED",
                            payload={"field": field, "value": entry["value"], "source": entry["source"],
                                     "ambiguity": entry["ambiguity"], "confidence": entry["confidence"]},
                            actor=actor)

    # 6) Freeze the revised Draft + Ledger and emit CONTRACT_REFINED when anything changed.
    question_by_field = {e.payload["field"]: e.payload["question"]
                         for e in stream if e.type == "CLARIFICATION_REQUESTED"}
    new_ledger = {"request_id": request_id,
                  "assumptions": list(ledger_body.get("assumptions", [])) + additions}
    new_draft = {**draft_body, "feature_semantics": semantics, "field_scores": field_scores,
                 "open_fields": open_fields, "open_questions": _open_questions(routing, question_by_field),
                 "status": "NEEDS_CLARIFICATION"}
    changed = renormalized or additions or open_fields != draft_body.get("open_fields", []) \
        or field_scores != draft_body.get("field_scores", {})
    if changed:
        draft_doc_id, ledger_doc_id = freeze_draft(
            conn, run_id=run_id, request_id=request_id, body=new_draft, ledger_body=new_ledger,
            actor=actor, supersedes=(draft_doc_id,),
        )
        new_draft["assumption_ledger_ref"] = ledger_doc_id
        append_fc_event(conn, run_id=run_id, type="CONTRACT_REFINED",
                        payload={"draft_doc_id": draft_doc_id, "assumption_ledger_ref": ledger_doc_id,
                                 "open_fields": open_fields, "round": rounds}, actor=actor)

    must_ask = [f for f, d in routing.items() if d == "human"
                and any(_base(of) == f for of in open_fields)]

    # 7) Converge → MCV; or bounded-exhausted → auto-park; or open must-ask tasks and loop.
    if not open_fields and not must_ask:
        candidate_count = _candidate_count(conn, run_id) if mode == "hypothesis" else 0
        mcv = minimum_contract_validated(new_draft, new_ledger, classification, mode=mode,
                                         candidate_count=candidate_count, confirmed_fields=set(answers))
        if mcv.passed:
            append_fc_event(conn, run_id=run_id, type="MINIMUM_CONTRACT_VALIDATED",
                            payload={"draft_doc_id": draft_doc_id}, actor=actor)
            return RefineResult("validated", draft_doc_id, (), mcv)
        return RefineResult("mcv_failed", draft_doc_id, (), mcv)

    if rounds >= budget:
        # Bounded (§6.6): stop looping — auto-park the run for human follow-up.
        park_command(conn, Command(
            action="park", aggregate="run", aggregate_id=run_id,
            args={"owner": _REFINEMENT_PARK_OWNER, "waiting_on_fact": None},
            actor=actor, idempotency_key=f"refine-park:{run_id}:{rounds}",
        ))
        return RefineResult("parked", draft_doc_id, tuple(open_fields), None)

    owner = state.requester   # R4 — the INTENT_SUBMITTED actor.subject; never payload.get("requested_by")
    open_task_fields = {e.payload["field"] for e in stream
                        if e.type == "CLARIFICATION_REQUESTED"} & set(must_ask)
    for field in must_ask:
        if field in open_task_fields:
            continue  # a task for this field already exists on the stream (refresh handled by staleness)
        open_clarification_task(conn, run_id=run_id, request_id=request_id, draft_doc_id=draft_doc_id,
                                field=field, question=question_by_field.get(field, f"Please specify {field}."),
                                owner_subject=owner, actor=actor)
    return RefineResult("clarifying", draft_doc_id, tuple(open_fields), None)
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_refine_contract.py -v`
  - Expected: PASS (4 tests).

- [ ] **Step 5 — commit**
  - `git add src/featuregen/intake/commands.py tests/featuregen/intake/test_refine_contract.py && git commit -m "feat(intake): Human Clarification task + bounded Contract Refinement Loop (renormalize→rescore→reroute→MCV; exhausted→auto-park)"`

---

### Task 5.6: `commands.py` — the `answer_clarification` command (request-owner guard + drives the loop)

**Files:**
- Modify: `src/featuregen/intake/commands.py` (append `answer_clarification`; extend `_SP2_CATALOG`)
- Test: `tests/featuregen/intake/test_answer_clarification.py`

**Interfaces:**
- Consumes: SP-0 `gates/tasks.py::submit_human_signal`; `security/audit.py::record_denial`; **R1** `intake.store.{load_feature_contract, append_feature_contract_event}` (P1); **R3/R4** `intake.state.{fold_feature_contract_state, actor_is_request_owner}` (P2 — the request-owner guard); the Task-5.5 `refine_contract`/`current_intake_deps`.
- Produces:
  - `answer_clarification(conn, cmd) -> CommandResult` — reads `cmd.args = {task_id, response, expected_task_version, answer}`; resolves the run + request owner from the `feature_contract` stream; enforces the **SP-2-built request-owner guard** (`actor_kind=="human" ∧ actor.subject == request owner`), a mismatch **denied + written to the security-audit stream** (never counted); `submit_human_signal(CLARIFICATION, expected_task_version)` (task-version OCC); on a counted, quorum-met answer emits `CLARIFICATION_ANSWERED` (the domain shadow) and drives the Refinement Loop (`refine_contract`, when deps are registered).
  - `_SP2_CATALOG` gains `("answer_clarification", answer_clarification)`; `register_sp2_commands()` (idempotent) registers it.

- [ ] **Step 1 — write the failing test**

```python
# tests/featuregen/intake/test_answer_clarification.py
from psycopg.rows import dict_row

from featuregen.contracts import Command
from featuregen.identity.build import build_human_identity
from featuregen.intake.commands import (
    answer_clarification,
    open_clarification_task,
    register_intake_deps,
)
from featuregen.intake.store import append_feature_contract_event as append_fc_event, load_feature_contract
from featuregen.intake.redaction import DefaultIntentRedactor
from featuregen.security.audit import verify_chain

OWNER = build_human_identity(subject="user:raj", role_claims=("data_scientist",))
MALLORY = build_human_identity(subject="user:mallory", role_claims=("data_scientist",))


class _View:
    def candidate_count(self, concept):
        return 1

    def metadata(self):
        return {}


class _NoopLLM:
    def call(self, request):
        from featuregen.intake.llm import LLMResult
        return LLMResult(output={"review_type": "CONTRACT_REVIEW", "status": "OK", "findings": []},
                         self_reported_scores={}, call_ref="", status="ok")


def _seed_with_task(db, agent):
    from featuregen.intake.commands import freeze_draft
    run_id = "run_ans"
    # R4: INTENT_SUBMITTED is appended by the HUMAN requester (OWNER), so the P2 fold sets
    # state.requester == "user:raj" — the value the request-owner guard checks. (The service `agent`
    # still produces the downstream Draft/task events.)
    append_fc_event(db, run_id=run_id, type="INTENT_SUBMITTED",
                    payload={"request_id": "req_ans", "run_id": run_id, "intake_mode": "definition",
                             "raw_input_ref": "blob_x", "raw_input_classification": "clean",
                             "classification": {"outcome": "CLEAR", "catalog_version": "bdc-1"}},
                    actor=OWNER, expected_version=0)
    body = {"request_id": "req_ans", "intake_mode": "definition", "raw_input_ref": "blob_x",
            "raw_input_classification": "clean", "proposed_feature_name": "f",
            "feature_semantics": {"entity": "customer", "entity_grain": ["customer_id", "as_of_date"],
                                  "observation_intent": {"kind": "point_in_time"},
                                  "calculation_method": "rolling_count", "windows": [], "filters": []},
            "field_scores": {}, "open_fields": ["filters.declined_status_encoding"],
            "assumption_ledger_ref": "", "provenance": {"schema_version": 1}, "status": "NEEDS_CLARIFICATION"}
    ledger = {"request_id": "req_ans", "assumptions": []}
    draft_doc_id, _ = freeze_draft(db, run_id=run_id, request_id="req_ans", body=body,
                                   ledger_body=ledger, actor=agent)
    append_fc_event(db, run_id=run_id, type="DRAFT_CONTRACT_PRODUCED",
                    payload={"draft_doc_id": draft_doc_id, "assumption_ledger_ref": "",
                             "open_fields": ["filters.declined_status_encoding"]}, actor=agent)
    task_id = open_clarification_task(db, run_id=run_id, request_id="req_ans", draft_doc_id=draft_doc_id,
                                      field="filters", question="Which column?", owner_subject="user:raj",
                                      actor=agent)
    return run_id, task_id


def _answer_cmd(task_id, actor, *, response="confirm", version=1, answer="auth_result='D'"):
    return Command(action="answer_clarification", aggregate="feature_contract", aggregate_id=None,
                   args={"task_id": task_id, "response": response, "expected_task_version": version,
                         "answer": answer}, actor=actor, idempotency_key=f"ans:{task_id}:{actor.subject}")


def test_a_different_data_scientist_is_denied_and_security_audited(db, sp2_schemas, agent):
    run_id, task_id = _seed_with_task(db, agent)
    res = answer_clarification(db, _answer_cmd(task_id, MALLORY))
    assert res.accepted is False
    assert "request owner" in res.denied_reason
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute(
            "SELECT count(*) AS n FROM security_audit WHERE decision='denied' "
            "AND attempted_action='answer_clarification'"
        )
        assert cur.fetchone()["n"] == 1
    assert verify_chain(db) is True  # the tamper-evident chain stays intact
    # the task was NOT answered
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (task_id,))
        assert cur.fetchone()["status"] == "open"


def test_owner_answer_is_counted_and_shadowed(db, sp2_schemas, agent):
    run_id, task_id = _seed_with_task(db, agent)
    res = answer_clarification(db, _answer_cmd(task_id, OWNER))
    assert res.accepted is True, res.denied_reason
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CLARIFICATION_ANSWERED" in types
    with db.cursor(row_factory=dict_row) as cur:
        cur.execute("SELECT status FROM human_tasks WHERE task_id=%s", (task_id,))
        assert cur.fetchone()["status"] == "answered"


def test_stale_task_version_is_not_counted(db, sp2_schemas, agent):
    run_id, task_id = _seed_with_task(db, agent)
    res = answer_clarification(db, _answer_cmd(task_id, OWNER, version=99))  # wrong task_version
    assert res.accepted is False
    assert "not counted" in res.denied_reason


def test_owner_answer_drives_the_refinement_loop_when_deps_registered(db, sp2_schemas, agent):
    run_id, task_id = _seed_with_task(db, agent)
    register_intake_deps(client=_NoopLLM(), redactor=DefaultIntentRedactor(), catalog=_View())
    try:
        answer_clarification(db, _answer_cmd(task_id, OWNER))
    finally:
        register_intake_deps(client=None, redactor=None, catalog=None)
    types = [e.type for e in load_feature_contract(db, run_id)]
    assert "CONTRACT_REFINED" in types  # the loop ran a round on the answer
```

- [ ] **Step 2 — run it (fails)**
  - `uv run pytest tests/featuregen/intake/test_answer_clarification.py -v`
  - Expected: FAIL — `ImportError: cannot import name 'answer_clarification'`.

- [ ] **Step 3 — minimal implementation** (append to `commands.py`, then extend `_SP2_CATALOG`)

```python
from featuregen.gates.tasks import submit_human_signal  # add to the imports at the top of commands.py


def _deny_owner_guard(conn: DbConn, cmd: Command, run_id: str, reason: str) -> CommandResult:
    """A request-owner / SoD denial is a security event, not a benign validation error: route it to
    the tamper-evident security-audit stream (§6.2, §8.2), never the domain stream. Mirrors the
    overlay `_deny_audited`; the resolved run_id is recorded as the aggregate_id."""
    record_denial(conn, replace(cmd, aggregate_id=run_id), reason)
    return CommandResult(accepted=False, aggregate_id=run_id, denied_reason=reason)


def answer_clarification(conn: DbConn, cmd: Command) -> CommandResult:
    """Answer a Human Clarification task (spec §6.5). SP-2 adds the request-owner guard SP-0 does not
    provide: SP-0's `submit_human_signal` checks role/scope/quorum but NEVER that the acting subject
    is the task's requester (`gates/tasks.py`), so role-authz alone would let ANY data_scientist
    answer another author's clarification. A mismatch is DENIED + security-audited, never counted.
    On a counted, quorum-met answer it emits the CLARIFICATION_ANSWERED domain shadow and drives the
    Contract Refinement Loop (when the Layer-2 deps are registered)."""
    args = cmd.args
    task_id = args["task_id"]
    row = conn.execute("SELECT run_id FROM human_tasks WHERE task_id=%s", (task_id,)).fetchone()
    if row is None or row[0] is None:
        return CommandResult(accepted=False, aggregate_id="", denied_reason="unknown clarification task")
    run_id = row[0]
    stream = load_feature_contract(conn, run_id)
    state = fold_feature_contract_state(stream)   # R3 — the P2 fold; state.requester is the owner (R4)

    # ── SP-2 request-owner guard (subject-level; SP-0 authz is role-level only) ──────────────────
    # R4: the ONE owner predicate is actor_is_request_owner(state, actor) — never payload.get("requested_by").
    if cmd.actor.actor_kind != "human" or not actor_is_request_owner(state, cmd.actor):
        return _deny_owner_guard(
            conn, cmd, run_id, "answer_clarification denied: actor is not the request owner"
        )

    result = submit_human_signal(
        conn, task_id, response=args["response"], actor=cmd.actor,
        expected_task_version=args["expected_task_version"],
    )
    if not result.counted:
        # Benign non-count (stale task_version / already-closed) — NOT a security event.
        return CommandResult(
            accepted=False, aggregate_id=run_id,
            denied_reason=f"clarification not counted (status={result.status})",
        )

    field = _requested_field(stream, task_id)
    append_fc_event(
        conn, run_id=run_id, type="CLARIFICATION_ANSWERED",
        payload={"task_id": task_id, "field": field, "answer": args.get("answer"),
                 "response": args["response"], "answered_by": cmd.actor.subject},
        actor=cmd.actor,
    )

    # Drive the Refinement Loop once quorum is met (§6.6) — only when the runtime deps are wired
    # (P9 bootstrap / test registration). Absent deps, the loop is driven by the durable runtime.
    if result.quorum_met:
        deps = current_intake_deps()
        if deps is not None and deps.client is not None:
            refine_contract(conn, run_id, client=deps.client, redactor=deps.redactor,
                            catalog=deps.catalog, actor=cmd.actor)
    return CommandResult(accepted=True, aggregate_id=run_id)
```

Then extend the P4 command catalog (append to the existing `_SP2_CATALOG` tuple):

```python
_SP2_CATALOG = (
    # ... the P4 entries (submit_intent, ...) stay unchanged ...
    ("answer_clarification", answer_clarification),
)
```

- [ ] **Step 4 — run it (passes)**
  - `uv run pytest tests/featuregen/intake/test_answer_clarification.py -v`
  - Expected: PASS (4 tests).

- [ ] **Step 5 — run the whole Layer-2 slice green**
  - `uv run pytest tests/featuregen/intake/ -v`
  - Expected: PASS (all Task 5.1–5.6 suites).

- [ ] **Step 6 — commit**
  - `git add src/featuregen/intake/commands.py tests/featuregen/intake/test_answer_clarification.py && git commit -m "feat(intake): answer_clarification command — request-owner guard (deny+security-audit) drives the Refinement Loop"`

---

## Phase 5 exit criteria (all green)

- `scoring.py` combines the LLM self-report with the deterministic catalog-cardinality check by cautious-max — the LLM can never lower a deterministic doubt (Decision 3); `CatalogView` single-source seam registered.
- `doubt_router.py` auto-resolves **iff** `ambiguity ≤ 0.30 ∧ confidence ≥ 0.70 ∧ safe value ∧ not policy-sensitive ∧ not a calc-method choice`; thresholds config-gated; biased toward asking (Decision 4).
- `critique.py` runs the single `CONTRACT_REVIEW` challenger pass (event-sourced via `call_llm`), emits `CONTRACT_CRITIQUED`, and only ORs doubts into the router — it never confirms or lowers a doubt (spec §6.4).
- `mcv.py` enforces the 6-check pre-gate checklist (fail-closed on an absent/unversioned classification) and exposes the pure lifecycle-guard predicates (`open_fields_empty`, `not_prohibited_intent`, `calculation_method_available`, `actor_is_request_owner`, `confirmer_is_requester_human`) — evaluated inline by P7, never via the state-machine engine.
- The Human Clarification task rides SP-0's `CLARIFICATION` gate with `delegation_allowed=False`, eligible = the request owner, `required_inputs=[draft_doc_id]`; the bounded Refinement Loop renormalizes → rescores → re-critiques → re-routes → auto-resolves / opens must-ask tasks → MCV, and **auto-parks** on round-budget exhaustion (Decision 6).
- `answer_clarification` enforces the SP-2-built request-owner guard (`actor_kind=="human" ∧ subject == owner`): a different data scientist / service / the LLM is **denied and written to the tamper-evident security-audit stream** (chain stays valid), never counted; a valid owner answer is task-version-OCC'd, shadowed as `CLARIFICATION_ANSWERED`, and drives the loop.
- No raw data / PII reaches the LLM: only structured Draft semantics (critique) and redacted answers + catalog metadata (renormalize) enter `LLMRequest.inputs`, all behind `call_llm`'s egress guard; every LLM call is event-sourced (`LLM_CALL_RECORDED`).

**Produced for later phases:** `scoring.{FieldScore, combine_scores, catalog_cardinality_score, score_fields, CatalogView, register_catalog_view, current_catalog_view}`; `doubt_router.{RouterThresholds, default_thresholds, route_field, route_draft}`; `critique.{CritiqueFinding, CritiqueResult, contract_review, apply_critique}`; `mcv.{MCVResult, minimum_contract_validated, open_fields_empty, not_prohibited_intent, calculation_method_available, actor_is_request_owner, confirmer_is_requester_human}`; `commands.{open_clarification_task, refine_contract, RefineResult, answer_clarification, IntakeDeps, register_intake_deps, current_intake_deps, MAX_REFINEMENT_ROUNDS}` — consumed by **P6** (candidate `CatalogView` + hypothesis calc-method routing + MCV #2 `candidate_count`), **P7** (`minimum_contract_validated`/`confirmer_is_requester_human` guards, `refine_contract` re-entry on `request_edit`, `open_clarification_task` cancel-on-gate-open), and **P8** (the stream-readers `_first`/`_answered_fields`/`_current_draft_doc_id` folded into `fold_feature_contract_state`).
