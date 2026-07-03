# SP-2 — Phase 3 — Auditable-LLM envelope (Implementation Plan)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Global Constraints + Shared Contract:** see [sp2-00-overview.md](2026-07-01-sp2-00-overview.md) (authoritative). The `LLMClient` / `FakeLLM` / `LLMRequest` / `LLMResult` / `IntentRedactor` / `RedactionResult` / `assert_llm_safe` / `call_llm` signatures and the full-identity idempotency key below are taken verbatim from the overview's **Shared Contract — Key signatures** (§9.1–§9.4) and the SP-2 design spec §9. Do not drift them.

---

This phase builds SP-2's most consequential contribution — the platform's **first and permanent auditable-LLM boundary** (spec §9). Three modules:

- `src/featuregen/intake/redaction.py` — the **`IntentRedactor` seam** (Protocol + `DefaultIntentRedactor` + `RedactionResult`), the reserved LLM-safe `inputs` key vocabulary, the `build_llm_inputs(...)` assembler, the fail-closed **egress guard** `assert_llm_safe(request)` (refuses `unscanned` / data-values / un-redacted PII → hard failure), and the **R10** collaborator DI seam `register_intent_redactor` / `current_intent_redactor` (module-global, fail-closed if unset).
- `src/featuregen/intake/llm.py` — the **`LLMClient` Protocol**, `LLMRequest` / `LLMResult`, the deterministic scriptable **`FakeLLM`** (**R19** `FakeLLM(script={task_key: FakeResponse(...)})` construction form + task-key fallback), the **R10** collaborator DI seam `register_llm_client` / `current_llm_client` (module-global, fail-closed if unset), the structured-output **bounded-repair / bounded-retry / fail-closed taxonomy** (`drive_structured_call`), the append-only **`llm_call` record store** (`record_llm_call` / `read_llm_call` / `find_llm_call`) with the **full-identity idempotency key**, and the event-sourced wrapper **`call_llm(conn, client, request, *, run_id, actor)`** — which **imports the R1 store seam** `append_feature_contract_event` (from `intake.store`, P1) and the **R17** `LLM_CALL_RECORDED` constant (from `intake.events`, P1) — egress-guards → dedups → validates against the registered output-schema → records the call → emits `LLM_CALL_RECORDED` on the `feature_contract` aggregate.
- `src/featuregen/intake/llm_claude.py` — the **config-gated real Claude adapter** (Anthropic SDK, `anthropic` imported lazily, never in CI; model config-driven, default `claude-opus-4-8`; no-PHI-in-schema; **no silent fallback to `FakeLLM`**), with the SDK syntax in an Adapter Appendix.

**Authority-model invariants this phase enforces (verbatim, load-bearing):** the LLM *structures/suggests*, the platform *validates/enforces*. A malformed LLM structure is a **doubt, not a value** — bounded repair → fail into the clarification/manual path (no silent bad structure). A refusal fails into clarification directly (not repair). No PII / no data values ever reach the model (`IntentRedactor` redacts; `assert_llm_safe` is the hard egress backstop → security-audit). Every call is one immutable, **replayable** (`redacted_input` stored, not hash-only) record + one `LLM_CALL_RECORDED` event. **No silent production fallback** — an enabled-but-unavailable real adapter fails closed into clarification, never swaps in `FakeLLM` (Decision D5).

**Depends on P1** (`sp2-01`): the `feature_contract` aggregate admitted by the `0508_feature_contract_events.sql` aggregate-CHECK widening + the `feature_contract_id` typed mirror-id column + the `append(..., feature_contract_id=...)` keyword (mirroring SP-1's `overlay_fact_id`); the registered `LLM_CALL_RECORDED` event-type schema (`featuregen.intake.events.register_sp2_event_types`); and the append-only `llm_call` record-store **table** (DDL below). **Depends on P2** (`sp2-02`) only for the *concept* of a registered structural output-schema — P3's tests register their own tiny output-schema in `document_type_registry`, so P3 does **not** depend on P2's specific schema ids. All tests run on `FakeLLM` (hermetic, no network, required in CI). The real adapter is exercised only by an opt-in, config-gated smoke test never gated in CI.

**Task independence.** Tasks 3.1–3.4 (redaction, egress guard, `LLMClient`/`FakeLLM`, taxonomy) are **pure unit code** — no DB. Task 3.5 (record store) and Task 3.6 (`call_llm`) are DB-backed (the `db`/`conn` fixture, P1's migrations). Task 3.7 (real adapter) is structural + a skipped smoke test. Implement 3.1 → 3.7 in order; 3.1–3.4 can be reordered freely.

---

## The `llm_call` record-store table (shipped by P1 `sp2-01`; P3 reads/writes it)

P3's `record_llm_call` / `read_llm_call` / `find_llm_call` bind to this exact table (mirroring SP-1's `overlay_evidence` write-once artifact, `documents/../evidence.py`). P1 creates it (write-once, classified **sensitive / governance-retained / read-controlled**). Column names are the overview's §9.3 record-field list verbatim; P3 pins the payload shapes below:

```sql
-- (shipped in P1's migration; reproduced here as the binding schema contract for P3)
CREATE TABLE IF NOT EXISTS llm_call (
    llm_call_ref          text        PRIMARY KEY,      -- 'llmc_' id
    run_id                text        NOT NULL,
    task                  text        NOT NULL,         -- structure_intent | contract_review | generate_candidates | renormalize
    provider              text        NOT NULL,         -- from generation_settings["provider"]
    model                 text        NOT NULL,         -- from generation_settings["model"]
    prompt_id             text        NOT NULL,
    prompt_version        integer     NOT NULL,
    output_schema_id      text        NOT NULL,
    output_schema_version integer     NOT NULL,
    generation_settings   jsonb       NOT NULL,         -- pinned; part of the idempotency key
    redaction_version     text        NOT NULL,         -- which IntentRedactor policy produced the LLM-safe text
    input_hash            text        NOT NULL,         -- sha256 of the redacted (LLM-safe) input; dedup component
    redacted_input        jsonb       NOT NULL,         -- the STORED redacted input itself (replayable, NOT hash-only)
    input_redaction       jsonb       NOT NULL,         -- what was scrubbed (span types/positions, never values)
    raw_output            jsonb       NOT NULL,         -- {"output": <structured>, "self_reported_scores": {...}}
    validation_result     jsonb       NOT NULL,         -- {"result": <final status>, "reason"?: str}
    repair_attempts       jsonb       NOT NULL,         -- [{attempt, class: repair|retry, reason}]
    latency_ms            integer     NULL,
    cost_metadata         jsonb       NULL,
    created_at            timestamptz NOT NULL DEFAULT now(),
    created_by            jsonb       NOT NULL           -- identity_to_jsonb(actor); service:intake-agent
);
CREATE INDEX IF NOT EXISTS llm_call_identity_idx ON llm_call (run_id, task, input_hash);
```

**`LLM_CALL_RECORDED` event schema (registered by P1; constant owned by P1's `intake/events.py`, R17):** `schema_version=1`, `additionalProperties: true`, `required: ["llm_call_ref"]` (**R2** — id fields NOT in `required`), owner `featuregen-intake`. P3 **imports the `LLM_CALL_RECORDED` constant from `featuregen.intake.events` (never redeclares it, R17)**, is the **sole emitter**, and pins the payload to SEMANTIC fields only (R2 — no id fields): `{"llm_call_ref", "task", "status", "validation_result"}`. Emitted via the **R1 store seam** `append_feature_contract_event(conn, run_id=..., type=LLM_CALL_RECORDED, ...)` (from `intake.store`, P1), which sets `aggregate="feature_contract"`, `aggregate_id = feature_contract_id = run_id`, and — per the **one event-identity invariant (X3)** — the **`run_id` mirror column ALWAYS populated** (`= run_id`, non-null, for correlation), **`feature_id` ALWAYS NULL**, and `request_id` optional. `LLM_CALL_RECORDED` is **never appended on the `run` aggregate** — it rides the `feature_contract` stream like every other SP-2 domain event. The `0508` `feature_contract` branch requires `aggregate_id = feature_contract_id` and `feature_id IS NULL` (mirroring `0504`'s overlay branch). `fold_feature_contract_state` (P8) MUST ignore `LLM_CALL_RECORDED` (it never advances the folded status).

---

### Task 3.1: `IntentRedactor` seam + `DefaultIntentRedactor` + reserved-inputs vocabulary + `build_llm_inputs`

**Files:**
- Create `src/featuregen/intake/__init__.py` (empty) if it does not already exist (P1 may have created it).
- Create `src/featuregen/intake/redaction.py` — `IntentRedactor` Protocol, `RedactionResult`, `DefaultIntentRedactor`, the reserved `INPUT_KEY_*` constants, the shared `_PII_PATTERNS`, `EgressViolation`, `build_llm_inputs`, and the **R10** collaborator seam `register_intent_redactor` / `current_intent_redactor`.
- Create `tests/featuregen/intake/__init__.py` (empty) so the test package imports.
- Create `tests/featuregen/intake/test_redaction.py` — pure unit tests (no DB fixture).

**Interfaces:**
- Consumes (from `featuregen.documents.draft`, SP-0): `RAW_INPUT_CLASSIFICATIONS = ("contains_pii","clean","unscanned")` (the closed classification vocabulary the redactor branches on).
- Produces:
  ```python
  # reserved SP-2 keys that structure LLMRequest.inputs (the ONLY LLM-safe rendering + provenance)
  INPUT_KEY_INTENT = "redacted_intent"              # the redacted, LLM-safe intent text
  INPUT_KEY_CATALOG = "catalog_metadata"            # names/types/grain + catalog-declared enum/code metadata ONLY
  INPUT_KEY_CLASSIFICATION = "raw_input_classification"   # SP-0 classification (egress guard reads it)
  INPUT_KEY_REDACTION_VERSION = "redaction_version"       # IntentRedactor policy version (stamped on the llm_call)
  INPUT_KEY_REDACTION = "input_redaction"                 # {"redacted_spans": [...]} — types/positions, never values

  class EgressViolation(Exception): ...                   # raised by the redactor/egress guard; hard failure

  @dataclass(frozen=True)
  class RedactionResult:
      text: str | None            # the ONLY LLM-safe rendering placed in inputs; None ⟹ fail closed
      redaction_version: str
      redacted_spans: tuple
      disposition: str            # "ok" | "fail_into_clarification"

  class IntentRedactor(Protocol):
      def redact(self, raw_intent: str, raw_input_classification: str) -> RedactionResult: ...

  class DefaultIntentRedactor:    # IntentRedactor; fails closed on un-redactable PII / `unscanned`
      def redact(self, raw_intent: str, raw_input_classification: str) -> RedactionResult: ...

  def build_llm_inputs(redaction: RedactionResult, *, catalog_metadata: Mapping[str, Any],
                       raw_input_classification: str) -> dict: ...   # assembles the reserved-keyed inputs dict

  # R10 collaborator DI seam (module-global; owned by P3, imported verbatim by P4/P9). Fail-closed.
  def register_intent_redactor(redactor: IntentRedactor) -> None: ...
  def current_intent_redactor() -> IntentRedactor: ...   # raises RuntimeError if unset
  ```

Steps:

- [ ] **Write the failing test.** Create `tests/featuregen/intake/test_redaction.py`:
  ```python
  import pytest

  from featuregen.intake.redaction import (
      INPUT_KEY_CATALOG,
      INPUT_KEY_CLASSIFICATION,
      INPUT_KEY_INTENT,
      INPUT_KEY_REDACTION,
      INPUT_KEY_REDACTION_VERSION,
      DefaultIntentRedactor,
      EgressViolation,
      RedactionResult,
      build_llm_inputs,
  )


  def test_clean_intent_passes_through_stamped():
      r = DefaultIntentRedactor().redact(
          "90-day rolling count of declined card authorizations per customer", "clean"
      )
      assert r.disposition == "ok"
      assert r.text == "90-day rolling count of declined card authorizations per customer"
      assert r.redacted_spans == ()
      assert r.redaction_version == "default-redactor@1"


  def test_contains_pii_scrubs_located_spans():
      r = DefaultIntentRedactor().redact(
          "count logins for jane.doe@bank.example and SSN 123-45-6789", "contains_pii"
      )
      assert r.disposition == "ok"
      assert r.text is not None
      # the located PII is gone; placeholders are digit/at-free so a residual scan is clean
      assert "jane.doe@bank.example" not in r.text
      assert "123-45-6789" not in r.text
      assert "[REDACTED:EMAIL]" in r.text and "[REDACTED:SSN]" in r.text
      # spans record TYPE + position only (never the scrubbed value)
      kinds = {s["type"] for s in r.redacted_spans}
      assert kinds == {"EMAIL", "SSN"}
      assert all("start" in s and "end" in s and "value" not in s for s in r.redacted_spans)


  def test_unscanned_fails_closed_no_text():
      r = DefaultIntentRedactor().redact("anything at all", "unscanned")
      assert r.disposition == "fail_into_clarification"
      assert r.text is None


  def test_contains_pii_but_unlocatable_fails_closed():
      # SP-0 says contains_pii, but the default redactor finds no locatable span it can scrub:
      # it cannot prove the text is safe, so it fails closed rather than emit an unsafe payload.
      r = DefaultIntentRedactor().redact("the applicant's maiden name is on file", "contains_pii")
      assert r.disposition == "fail_into_clarification"
      assert r.text is None


  def test_build_llm_inputs_assembles_reserved_keys():
      red = RedactionResult(
          text="count declined auths per customer",
          redaction_version="default-redactor@1",
          redacted_spans=(),
          disposition="ok",
      )
      inputs = build_llm_inputs(
          red,
          catalog_metadata={"objects": ["card_authorizations"], "columns": {"auth_result": "text"}},
          raw_input_classification="clean",
      )
      assert inputs[INPUT_KEY_INTENT] == "count declined auths per customer"
      assert inputs[INPUT_KEY_CATALOG]["objects"] == ["card_authorizations"]
      assert inputs[INPUT_KEY_CLASSIFICATION] == "clean"
      assert inputs[INPUT_KEY_REDACTION_VERSION] == "default-redactor@1"
      assert inputs[INPUT_KEY_REDACTION] == {"redacted_spans": []}


  def test_build_llm_inputs_refuses_failed_redaction():
      red = RedactionResult(text=None, redaction_version="default-redactor@1",
                            redacted_spans=(), disposition="fail_into_clarification")
      with pytest.raises(EgressViolation):
          build_llm_inputs(red, catalog_metadata={}, raw_input_classification="unscanned")


  def test_intent_redactor_seam_registers_and_fails_closed_when_unset():
      # R10 module-global DI seam: current_ fails closed until register_ is called; then round-trips.
      from featuregen.intake import redaction as _rmod
      from featuregen.intake.redaction import current_intent_redactor, register_intent_redactor

      _rmod._INTENT_REDACTOR = None  # ensure unset for a deterministic fail-closed assertion
      with pytest.raises(RuntimeError):
          current_intent_redactor()
      register_intent_redactor(DefaultIntentRedactor())
      assert isinstance(current_intent_redactor(), DefaultIntentRedactor)
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/intake/test_redaction.py -v` — Expected: FAIL (`ModuleNotFoundError: No module named 'featuregen.intake.redaction'`).

- [ ] **Minimal implementation.** Create `src/featuregen/intake/__init__.py` (empty) if absent, then `src/featuregen/intake/redaction.py`:
  ```python
  """SP-2 no-PII boundary (spec §9.4): the IntentRedactor seam + default impl + the reserved
  LLM-safe `inputs` vocabulary + the fail-closed egress guard.

  Ownership split (spec §9.4): SP-0 CLASSIFIES the raw intent (raw_input_classification);
  SP-2 REDACTS here (fails closed on un-redactable / `unscanned`); SP-2 GUARDS EGRESS
  (`assert_llm_safe`, Task 3.2). The redactor produces the ONLY LLM-safe rendering of the intent
  ever placed in LLMRequest.inputs. `input_redaction` records span TYPES/POSITIONS, never values.
  """
  from __future__ import annotations

  import re
  from collections.abc import Mapping
  from dataclasses import dataclass
  from typing import Any, Protocol, runtime_checkable

  # Reserved keys that structure LLMRequest.inputs. The model-facing content is INTENT + CATALOG;
  # the rest is provenance the egress guard + call_llm read. Provenance keys carry no data values.
  INPUT_KEY_INTENT = "redacted_intent"
  INPUT_KEY_CATALOG = "catalog_metadata"
  INPUT_KEY_CLASSIFICATION = "raw_input_classification"
  INPUT_KEY_REDACTION_VERSION = "redaction_version"
  INPUT_KEY_REDACTION = "input_redaction"

  REDACTION_VERSION = "default-redactor@1"

  # Deterministic PII detectors shared by the redactor and the egress backstop (Task 3.2). PHONE is
  # deliberately excluded (false positives on window/date literals like "90-day"); the set is
  # conservative-and-testable, not exhaustive. Placeholders are digit/at-free so a residual scan of
  # a redacted string never re-matches.
  _PII_PATTERNS: tuple[tuple[str, "re.Pattern[str]"], ...] = (
      ("EMAIL", re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")),
      ("SSN", re.compile(r"\b\d{3}-\d{2}-\d{4}\b")),
      ("PAN", re.compile(r"\b\d{4}[ \-]?\d{4}[ \-]?\d{4}[ \-]?\d{1,4}\b")),
  )


  class EgressViolation(Exception):
      """A payload that must never reach the LLM (unscanned, data values, or un-redacted PII), or
      a redactor that failed closed. A HARD failure — call_llm routes it to the security-audit
      stream (§9.4); it is never a warning."""


  @dataclass(frozen=True)
  class RedactionResult:
      text: str | None            # the ONLY LLM-safe rendering placed in inputs; None ⟹ fail closed
      redaction_version: str      # stamped onto the llm_call record
      redacted_spans: tuple       # ({"type","start","end"}, ...) — types/positions, NEVER values
      disposition: str            # "ok" | "fail_into_clarification"


  @runtime_checkable
  class IntentRedactor(Protocol):
      def redact(self, raw_intent: str, raw_input_classification: str) -> RedactionResult: ...


  def _scan(text: str) -> list[dict[str, Any]]:
      spans: list[dict[str, Any]] = []
      for label, pat in _PII_PATTERNS:
          for m in pat.finditer(text):
              spans.append({"type": label, "start": m.start(), "end": m.end()})
      return spans


  class DefaultIntentRedactor:
      """Default IntentRedactor. `clean` passes through; `contains_pii` scrubs the located spans and
      fails closed if it cannot locate any (cannot prove safety); `unscanned` fails closed outright.
      Never emits text for an un-redactable or unscanned intent (§9.4)."""

      def redact(self, raw_intent: str, raw_input_classification: str) -> RedactionResult:
          if raw_input_classification == "unscanned":
              return RedactionResult(None, REDACTION_VERSION, (), "fail_into_clarification")
          if raw_input_classification == "clean":
              return RedactionResult(raw_intent, REDACTION_VERSION, (), "ok")
          if raw_input_classification == "contains_pii":
              spans = _scan(raw_intent)
              if not spans:
                  # classified PII but nothing locatable to scrub → cannot prove safe → fail closed
                  return RedactionResult(None, REDACTION_VERSION, (), "fail_into_clarification")
              redacted = raw_intent
              for label, pat in _PII_PATTERNS:
                  redacted = pat.sub(f"[REDACTED:{label}]", redacted)
              if _scan(redacted):  # defense in depth: residual PII ⟹ fail closed
                  return RedactionResult(None, REDACTION_VERSION, (), "fail_into_clarification")
              return RedactionResult(redacted, REDACTION_VERSION, tuple(spans), "ok")
          # unknown classification: fail closed (never guess)
          return RedactionResult(None, REDACTION_VERSION, (), "fail_into_clarification")


  def build_llm_inputs(
      redaction: RedactionResult,
      *,
      catalog_metadata: Mapping[str, Any],
      raw_input_classification: str,
  ) -> dict:
      """Assemble the reserved-keyed LLMRequest.inputs from a RedactionResult + catalog METADATA.
      Refuses (EgressViolation) when the redactor failed closed — no unsafe payload is ever built."""
      if redaction.text is None:
          raise EgressViolation(
              "redactor failed closed; no LLM-safe text to dispatch (fail into clarification)"
          )
      return {
          INPUT_KEY_INTENT: redaction.text,
          INPUT_KEY_CATALOG: dict(catalog_metadata),
          INPUT_KEY_CLASSIFICATION: raw_input_classification,
          INPUT_KEY_REDACTION_VERSION: redaction.redaction_version,
          INPUT_KEY_REDACTION: {"redacted_spans": [dict(s) for s in redaction.redacted_spans]},
      }


  # ---- R10 collaborator DI seam (module-global; mirrors overlay/catalog.py) --------------------
  # The ONE holder for the active IntentRedactor. P4 (submit_intent) resolves the redactor via
  # current_intent_redactor(); P9 registers it via register_intent_redactor(...). Fail-closed if
  # unset — the platform never silently redacts with a default the caller did not choose (§9.4).
  _INTENT_REDACTOR: IntentRedactor | None = None


  def register_intent_redactor(redactor: IntentRedactor) -> None:
      """Register the process-wide IntentRedactor (last writer wins). P9 wires DefaultIntentRedactor."""
      global _INTENT_REDACTOR
      _INTENT_REDACTOR = redactor


  def current_intent_redactor() -> IntentRedactor:
      """Return the registered IntentRedactor; fail closed (RuntimeError) if none is registered."""
      if _INTENT_REDACTOR is None:
          raise RuntimeError(
              "no IntentRedactor registered; call register_intent_redactor(...) "
              "(register_sp2()/_wire does this)"
          )
      return _INTENT_REDACTOR
  ```

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/intake/test_redaction.py -v` — Expected: PASS.

- [ ] **Commit.** `git add src/featuregen/intake/__init__.py src/featuregen/intake/redaction.py tests/featuregen/intake/__init__.py tests/featuregen/intake/test_redaction.py && git commit -m "feat(intake): IntentRedactor seam + DefaultIntentRedactor + LLM-safe inputs vocabulary"`

---

### Task 3.2: The egress guard `assert_llm_safe(request)` (fail-closed backstop)

The pinned signature is `assert_llm_safe(request: LLMRequest) -> None` — **no `conn`**. So the guard is a pure deterministic predicate that **raises `EgressViolation`**; the conn-holding caller (`call_llm`, Task 3.6) catches it and writes the security-audit record. This keeps the guard hermetic/unit-testable while still routing violations to security-audit (§9.4). The guard needs `LLMRequest`, which is defined in Task 3.3; to keep Task 3.2 pure and dependency-light, `assert_llm_safe` reads `request.inputs` **duck-typed** (any object exposing `.inputs`), so the redaction module never imports `llm.py` (avoids a cycle: `llm.py` imports `redaction.py`).

**Files:**
- Modify `src/featuregen/intake/redaction.py` — add `_FORBIDDEN_INPUT_KEYS`, `_iter_strings`, `_first_pii`, and `assert_llm_safe` (append after `build_llm_inputs`).
- Modify `tests/featuregen/intake/test_redaction.py` — add the egress-guard tests (extend the top-of-file import).

**Interfaces:**
- Consumes: the reserved `INPUT_KEY_*` constants + `_PII_PATTERNS` + `EgressViolation` (Task 3.1). A duck-typed `request` exposing `request.inputs: Mapping`.
- Produces:
  ```python
  def assert_llm_safe(request) -> None:   # raises EgressViolation on: unscanned/unclassified,
                                          # data-value keys, contains_pii without redaction_version,
                                          # or any un-redacted PII in the model-facing content.
  ```

Steps:

- [ ] **Write the failing test.** Add to `tests/featuregen/intake/test_redaction.py` (extend the import to include `assert_llm_safe`):
  ```python
  from dataclasses import dataclass as _dc

  from featuregen.intake.redaction import assert_llm_safe  # add to imports at top


  @_dc(frozen=True)
  class _Req:  # a duck-typed stand-in for LLMRequest (Task 3.3) — the guard reads .inputs only
      inputs: dict


  def _safe_inputs():
      return {
          INPUT_KEY_INTENT: "count declined auths per customer",
          INPUT_KEY_CATALOG: {"objects": ["card_authorizations"], "columns": {"auth_result": "text"}},
          INPUT_KEY_CLASSIFICATION: "clean",
          INPUT_KEY_REDACTION_VERSION: "default-redactor@1",
          INPUT_KEY_REDACTION: {"redacted_spans": []},
      }


  def test_egress_allows_clean_metadata_only_payload():
      assert_llm_safe(_Req(_safe_inputs()))  # no raise


  def test_egress_refuses_unscanned():
      i = _safe_inputs()
      i[INPUT_KEY_CLASSIFICATION] = "unscanned"
      with pytest.raises(EgressViolation):
          assert_llm_safe(_Req(i))


  def test_egress_refuses_missing_classification():
      i = _safe_inputs()
      del i[INPUT_KEY_CLASSIFICATION]
      with pytest.raises(EgressViolation):
          assert_llm_safe(_Req(i))


  def test_egress_refuses_data_value_keys():
      i = _safe_inputs()
      i["column_values"] = ["D", "A", "R"]  # profiled value set — SP-1/SP-3 territory, never to the LLM
      with pytest.raises(EgressViolation):
          assert_llm_safe(_Req(i))


  def test_egress_refuses_contains_pii_without_redaction_version():
      i = _safe_inputs()
      i[INPUT_KEY_CLASSIFICATION] = "contains_pii"
      del i[INPUT_KEY_REDACTION_VERSION]
      with pytest.raises(EgressViolation):
          assert_llm_safe(_Req(i))


  def test_egress_refuses_unredacted_pii_in_content():
      i = _safe_inputs()
      i[INPUT_KEY_INTENT] = "count logins for jane.doe@bank.example"  # slipped past redaction
      with pytest.raises(EgressViolation):
          assert_llm_safe(_Req(i))
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/intake/test_redaction.py -k egress -v` — Expected: FAIL (`ImportError: cannot import name 'assert_llm_safe'`).

- [ ] **Minimal implementation.** Append to `src/featuregen/intake/redaction.py`:
  ```python
  # Keys that carry DATA VALUES (rows / samples / profiled value-sets / extrema) rather than
  # METADATA. Actual value/status-code sets are SP-1 profiling + SP-3 grounding (§4.4) — they must
  # NEVER reach the LLM. Their presence in an outbound payload is a hard egress violation.
  _FORBIDDEN_INPUT_KEYS = (
      "raw_input", "data_values", "column_values", "value_set",
      "rows", "samples", "profile", "extrema", "min", "max",
  )


  def _iter_strings(value: Any):
      if isinstance(value, str):
          yield value
      elif isinstance(value, Mapping):
          for v in value.values():
              yield from _iter_strings(v)
      elif isinstance(value, (list, tuple)):
          for v in value:
              yield from _iter_strings(v)


  def _first_pii(*values: Any) -> str | None:
      for value in values:
          for s in _iter_strings(value):
              for label, pat in _PII_PATTERNS:
                  if pat.search(s):
                      return label
      return None


  def assert_llm_safe(request) -> None:
      """Egress hard-backstop (§9.4). Deterministic pre-send check on an LLMRequest: refuses
      `unscanned`/unclassified content, data-value keys, a `contains_pii` payload that never went
      through redaction, or any un-redacted PII in the model-facing content. Raises EgressViolation
      (a HARD failure) — the conn-holding caller (call_llm) records it in the security-audit stream.
      Never mutates; never a warning."""
      inputs = request.inputs
      cls = inputs.get(INPUT_KEY_CLASSIFICATION)
      if cls == "unscanned":
          raise EgressViolation("refusing to dispatch an `unscanned` intent to the LLM")
      if cls not in ("clean", "contains_pii"):
          raise EgressViolation(f"missing/invalid {INPUT_KEY_CLASSIFICATION}: {cls!r}")
      present = [k for k in _FORBIDDEN_INPUT_KEYS if k in inputs]
      if present:
          raise EgressViolation(f"payload carries data-value keys, not metadata: {present}")
      if cls == "contains_pii" and not inputs.get(INPUT_KEY_REDACTION_VERSION):
          raise EgressViolation("`contains_pii` payload lacks a redaction_version (never redacted)")
      hit = _first_pii(inputs.get(INPUT_KEY_INTENT), inputs.get(INPUT_KEY_CATALOG))
      if hit:
          raise EgressViolation(f"un-redacted {hit} detected in outbound payload")
  ```

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/intake/test_redaction.py -v` — Expected: PASS (all redaction + egress tests).

- [ ] **Lint.** `uv run ruff check src/featuregen/intake/redaction.py` — Expected: clean.

- [ ] **Commit.** `git add src/featuregen/intake/redaction.py tests/featuregen/intake/test_redaction.py && git commit -m "feat(intake): fail-closed egress guard assert_llm_safe (no PII/data values to the LLM)"`

---

### Task 3.3: `LLMClient` Protocol + `LLMRequest` / `LLMResult` + `compute_input_hash` + scriptable `FakeLLM`

`LLMClient.call(request) -> LLMResult` is the **provider seam**: adapters return a *single-shot* `LLMResult` whose `status` carries a **provider outcome token** (`PROVIDER_*`) and whose `call_ref` is the empty sentinel (the adapter does not write the record — `call_llm` stamps the real `call_ref` and maps the token to the final `STATUS_*` vocabulary). `FakeLLM` is the deterministic CI default: it keys on `(task, prompt_id, input_hash)` (input_hash via the shared `compute_input_hash`, which excludes transient `_`-prefixed keys so repair re-calls hit the same fixture) and consumes a **per-key sequence** so a script can drive `[invalid, valid]` → repaired, `[refusal]` → fail-into-clarification, etc.

**Files:**
- Create `src/featuregen/intake/llm.py` — `LLMRequest`, `LLMResult`, `LLMClient` Protocol, the `PROVIDER_*` / `STATUS_*` constants, `compute_input_hash`, `FakeResponse`, `FakeLLM` (**R19** `FakeLLM(script={task_key: FakeResponse(...)})` construction form + task-key fallback), and the **R10** collaborator seam `register_llm_client` / `current_llm_client`.
- Create `tests/featuregen/intake/test_llm.py` — pure unit tests (no DB fixture).

**Interfaces:**
- Consumes: nothing from other phases (pure). `compute_input_hash` is used by both `FakeLLM` and `call_llm` (Task 3.6) — single source.
- Produces (the authoritative shared-contract shapes — overview §9.1):
  ```python
  @dataclass(frozen=True)
  class LLMRequest:
      task: str                  # "structure_intent" | "contract_review" | "generate_candidates" | "renormalize"
      prompt_id: str
      prompt_version: int
      inputs: dict               # reserved-keyed, redacted (Task 3.1); NO data values (§9.4)
      output_schema_id: str
      output_schema_version: int
      generation_settings: dict  # provider/model + thinking/effort/max_tokens — pinned; part of the idempotency key

  @dataclass(frozen=True)
  class LLMResult:
      output: dict
      self_reported_scores: dict
      call_ref: str              # "" from a provider single-shot; the real llmc_ ref from call_llm
      status: str                # provider token (PROVIDER_*) single-shot; final (STATUS_*) from call_llm

  class LLMClient(Protocol):
      def call(self, request: LLMRequest) -> LLMResult: ...

  # provider single-shot outcome tokens (LLMClient.call -> LLMResult.status)
  PROVIDER_OK="ok"; PROVIDER_INVALID="invalid"; PROVIDER_REFUSAL="refusal"
  PROVIDER_MAX_TOKENS="max_tokens"; PROVIDER_SCHEMA_FAULT="schema_fault"
  PROVIDER_TRANSIENT="transient"; PROVIDER_NON_RETRYABLE="non_retryable"; PROVIDER_AUTH_ERROR="auth_error"

  # final wrapper statuses (drive_structured_call / call_llm -> LLMResult.status)
  STATUS_OK="ok"; STATUS_REPAIRED="repaired"; STATUS_RETRIED="retried"
  STATUS_FAILED="failed_into_clarification"

  def compute_input_hash(inputs: Mapping[str, Any]) -> str: ...   # sha256 over non-`_` keys

  @dataclass(frozen=True)
  class FakeResponse:
      output: dict
      self_reported_scores: dict = ...    # default {}
      provider_status: str = PROVIDER_OK

  class FakeLLM:                          # LLMClient; scriptable to invalid/refusal/ambiguous
      # R19 canonical construction form (owner P3; P9's `_wire` uses EXACTLY this): a task-keyed
      # script whose values are a FakeResponse or a Sequence[FakeResponse]; `.call` resolves via a
      # task-key fallback. Constructing with no script is allowed (register scripts via `.script`).
      def __init__(self, script: Mapping[str, "FakeResponse | Sequence[FakeResponse]"] | None = None) -> None: ...
      def script(self, *, task: str, prompt_id: str,   # finer-grained (task,prompt_id,input_hash) builder for unit tests
                 responses: Sequence[FakeResponse], input_hash: str | None = None) -> None: ...
      def call(self, request: LLMRequest) -> LLMResult: ...

  # R10 collaborator DI seam (module-global; owned by P3, imported verbatim by P4/P9). Fail-closed.
  def register_llm_client(client: LLMClient) -> None: ...
  def current_llm_client() -> LLMClient: ...     # raises RuntimeError if unset
  ```

Steps:

- [ ] **Write the failing test.** Create `tests/featuregen/intake/test_llm.py`:
  ```python
  import pytest

  from featuregen.intake.llm import (
      PROVIDER_OK,
      PROVIDER_REFUSAL,
      FakeLLM,
      FakeResponse,
      LLMRequest,
      LLMResult,
      compute_input_hash,
  )


  def _req(inputs=None, task="structure_intent", prompt_id="intake.v1"):
      return LLMRequest(
          task=task, prompt_id=prompt_id, prompt_version=1,
          inputs=inputs if inputs is not None else {"redacted_intent": "x", "catalog_metadata": {}},
          output_schema_id="TEST_STRUCT", output_schema_version=1,
          generation_settings={"provider": "fake", "model": "fake-1", "max_tokens": 1024},
      )


  def test_compute_input_hash_ignores_transient_underscore_keys():
      base = {"redacted_intent": "count", "catalog_metadata": {"o": ["t"]}}
      h1 = compute_input_hash(base)
      # a transient repair annotation must NOT change the identity hash (stable across repairs)
      h2 = compute_input_hash({**base, "_repair_errors": ["missing entity"]})
      assert h1 == h2
      # a change to model-facing content DOES change the hash
      assert compute_input_hash({**base, "redacted_intent": "different"}) != h1


  def test_fakellm_returns_scripted_provider_result():
      fake = FakeLLM()
      fake.script(
          task="structure_intent", prompt_id="intake.v1",
          responses=[FakeResponse(output={"entity": "customer"},
                                  self_reported_scores={"entity": {"ambiguity": 0.05, "confidence": 0.97}})],
      )
      out = fake.call(_req())
      assert isinstance(out, LLMResult)
      assert out.output == {"entity": "customer"}
      assert out.self_reported_scores["entity"]["confidence"] == 0.97
      assert out.status == PROVIDER_OK
      assert out.call_ref == ""  # single-shot: call_llm stamps the real ref


  def test_fakellm_consumes_sequence_across_calls():
      fake = FakeLLM()
      fake.script(
          task="structure_intent", prompt_id="intake.v1",
          responses=[FakeResponse(output={}, provider_status="invalid"),
                     FakeResponse(output={"entity": "customer"})],
      )
      r = _req()
      assert fake.call(r).status == "invalid"   # attempt 0
      assert fake.call(r).status == PROVIDER_OK  # attempt 1 (repair-driven re-call would land here)
      assert fake.call(r).status == PROVIDER_OK  # exhausted sequence repeats the last


  def test_fakellm_scriptable_to_refusal():
      fake = FakeLLM()
      fake.script(task="structure_intent", prompt_id="intake.v1",
                  responses=[FakeResponse(output={}, provider_status=PROVIDER_REFUSAL)])
      assert fake.call(_req()).status == PROVIDER_REFUSAL


  def test_fakellm_raises_on_unscripted_key():
      with pytest.raises(KeyError):
          FakeLLM().call(_req())


  def test_fakellm_task_key_constructor_form_with_fallback():
      # R19 canonical construction: task-keyed script + task-key fallback (P9's `_wire` uses EXACTLY
      # this). A request for the task resolves regardless of prompt_id / inputs.
      fake = FakeLLM(script={"structure_intent": FakeResponse(output={"entity": "customer"})})
      out = fake.call(_req(prompt_id="whatever.v9", inputs={"redacted_intent": "z"}))
      assert isinstance(out, LLMResult)
      assert out.output == {"entity": "customer"}
      assert out.status == PROVIDER_OK
      assert out.call_ref == ""


  def test_fakellm_constructor_accepts_sequence_value():
      # A task-key value may be a SEQUENCE consumed in order (drives repair/retry paths in the E2E).
      fake = FakeLLM(script={"structure_intent": [FakeResponse(output={}, provider_status="invalid"),
                                                  FakeResponse(output={"entity": "customer"})]})
      r = _req()
      assert fake.call(r).status == "invalid"
      assert fake.call(r).status == PROVIDER_OK


  def test_llm_client_seam_registers_and_fails_closed_when_unset():
      # R10 module-global DI seam: current_ fails closed until register_ is called; then round-trips.
      from featuregen.intake import llm as _lmod
      from featuregen.intake.llm import current_llm_client, register_llm_client

      _lmod._LLM_CLIENT = None  # ensure unset for a deterministic fail-closed assertion
      with pytest.raises(RuntimeError):
          current_llm_client()
      fake = FakeLLM(script={"structure_intent": FakeResponse(output={"entity": "customer"})})
      register_llm_client(fake)
      assert current_llm_client() is fake
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/intake/test_llm.py -v` — Expected: FAIL (`ModuleNotFoundError: No module named 'featuregen.intake.llm'`).

- [ ] **Minimal implementation.** Create `src/featuregen/intake/llm.py`:
  ```python
  """SP-2 auditable-LLM envelope (spec §9): LLMClient seam + FakeLLM + the structured-output
  bounded-repair/retry taxonomy + the event-sourced call wrapper + the append-only llm_call store.

  All agent code depends on the LLMClient INTERFACE, never on a provider (Decision D5). The provider
  reports a single-shot outcome via LLMResult.status using the PROVIDER_* vocabulary; call_llm maps
  it to the final STATUS_* vocabulary, stamps the real call_ref, and records the call. This module
  ships FakeLLM + the taxonomy + the store; the real Claude adapter lives in llm_claude.py.
  """
  from __future__ import annotations

  import hashlib
  import json
  from collections.abc import Mapping, Sequence
  from dataclasses import dataclass, field
  from typing import Any, Protocol, runtime_checkable

  # ---- shared-contract shapes (overview §9.1) -------------------------------------------------


  @dataclass(frozen=True)
  class LLMRequest:
      task: str
      prompt_id: str
      prompt_version: int
      inputs: dict                # reserved-keyed, redacted (redaction.py); NO data values (§9.4)
      output_schema_id: str
      output_schema_version: int
      generation_settings: dict   # provider/model + thinking/effort/max_tokens — pinned; idempotency key


  @dataclass(frozen=True)
  class LLMResult:
      output: dict
      self_reported_scores: dict
      call_ref: str               # "" from a provider single-shot; the real llmc_ ref from call_llm
      status: str                 # PROVIDER_* single-shot; STATUS_* from call_llm


  @runtime_checkable
  class LLMClient(Protocol):
      def call(self, request: LLMRequest) -> LLMResult: ...


  # provider single-shot outcome tokens (what LLMClient.call reports)
  PROVIDER_OK = "ok"
  PROVIDER_INVALID = "invalid"
  PROVIDER_REFUSAL = "refusal"
  PROVIDER_MAX_TOKENS = "max_tokens"
  PROVIDER_SCHEMA_FAULT = "schema_fault"
  PROVIDER_TRANSIENT = "transient"
  PROVIDER_NON_RETRYABLE = "non_retryable"
  PROVIDER_AUTH_ERROR = "auth_error"

  # final wrapper statuses (call_llm / drive_structured_call return these)
  STATUS_OK = "ok"
  STATUS_REPAIRED = "repaired"
  STATUS_RETRIED = "retried"
  STATUS_FAILED = "failed_into_clarification"


  def compute_input_hash(inputs: Mapping[str, Any]) -> str:
      """sha256 of the exact redacted (LLM-safe) input — the dedup/identity component (§9.3).
      Transient driver keys (`_`-prefixed, e.g. `_repair_errors`) are excluded so a repair re-call
      keeps the SAME identity as its parent (no double-charge, stable FakeLLM keying)."""
      material = {k: v for k, v in inputs.items() if not str(k).startswith("_")}
      canonical = json.dumps(material, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
      return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


  # ---- FakeLLM (deterministic CI default) -----------------------------------------------------


  @dataclass(frozen=True)
  class FakeResponse:
      output: dict
      self_reported_scores: dict = field(default_factory=dict)
      provider_status: str = PROVIDER_OK


  class FakeLLM:
      """Deterministic LLMClient for CI (mirrors SP-1's FixtureCatalog). Hermetic: no network,
      required in CI (§15).

      R19 canonical construction form (owner P3; P9's `_wire` uses EXACTLY this): a task-keyed
      script passed to the constructor — `FakeLLM(script={task_key: FakeResponse(...)})` — where each
      value is a single FakeResponse or a Sequence[FakeResponse]. `.call` resolves a request in
      priority order: (1) the exact `(task, prompt_id, input_hash)` entry, (2) the
      `(task, prompt_id, None)` wildcard, then (3) the **task-key fallback** keyed on `request.task`
      alone (the constructor script). A per-key SEQUENCE is consumed in order across calls (so a
      script drives repair/retry paths), repeating the last response once the sequence is exhausted.
      The finer-grained `.script(...)` builder registers `(task, prompt_id, input_hash)` entries for
      unit tests; the constructor task-key form is the one P9 wires."""

      def __init__(
          self,
          script: Mapping[str, "FakeResponse | Sequence[FakeResponse]"] | None = None,
      ) -> None:
          self._scripts: dict[tuple[str, str, str | None], list[FakeResponse]] = {}
          # R19 task-key fallback: {request.task -> [FakeResponse, ...]}, matched on task alone.
          self._task_fallback: dict[str, list[FakeResponse]] = {}
          self._calls: dict[tuple[str, str, str], int] = {}
          for task_key, responses in (script or {}).items():
              self._task_fallback[task_key] = (
                  [responses] if isinstance(responses, FakeResponse) else list(responses)
              )

      def script(
          self,
          *,
          task: str,
          prompt_id: str,
          responses: Sequence[FakeResponse],
          input_hash: str | None = None,
      ) -> None:
          self._scripts[(task, prompt_id, input_hash)] = list(responses)

      def call(self, request: LLMRequest) -> LLMResult:
          h = compute_input_hash(request.inputs)
          seq = (
              self._scripts.get((request.task, request.prompt_id, h))
              or self._scripts.get((request.task, request.prompt_id, None))
              or self._task_fallback.get(request.task)   # R19 task-key fallback
          )
          if not seq:
              raise KeyError(
                  f"FakeLLM has no script for {(request.task, request.prompt_id, h)}"
              )
          call_key = (request.task, request.prompt_id, h)
          idx = self._calls.get(call_key, 0)
          self._calls[call_key] = idx + 1
          resp = seq[min(idx, len(seq) - 1)]
          return LLMResult(
              output=dict(resp.output),
              self_reported_scores=dict(resp.self_reported_scores),
              call_ref="",
              status=resp.provider_status,
          )


  # ---- R10 collaborator DI seam (module-global; mirrors overlay/catalog.py) --------------------
  # The ONE holder for the active LLMClient. All SP-2 agent code depends on the INTERFACE, never a
  # provider (Decision D5). P4 resolves the client via current_llm_client(); P9 registers the FakeLLM
  # via register_llm_client(...). Fail-closed if unset — never a silent default provider.
  _LLM_CLIENT: LLMClient | None = None


  def register_llm_client(client: LLMClient) -> None:
      """Register the process-wide LLMClient (last writer wins). P9 wires the FakeLLM here."""
      global _LLM_CLIENT
      _LLM_CLIENT = client


  def current_llm_client() -> LLMClient:
      """Return the registered LLMClient; fail closed (RuntimeError) if none is registered."""
      if _LLM_CLIENT is None:
          raise RuntimeError(
              "no LLMClient registered; call register_llm_client(...) "
              "(register_sp2()/_wire does this)"
          )
      return _LLM_CLIENT
  ```

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/intake/test_llm.py -v` — Expected: PASS.

- [ ] **Commit.** `git add src/featuregen/intake/llm.py tests/featuregen/intake/test_llm.py && git commit -m "feat(intake): LLMClient seam + LLMRequest/LLMResult + scriptable FakeLLM + input-hash"`

---

### Task 3.4: The structured-output taxonomy — `drive_structured_call` (bounded repair / retry / fail-closed)

The provider-failure taxonomy (§9.2, Decisions D5/D16) is provider-agnostic and lives in `llm.py` — a pure driver over `LLMClient.call` + a schema-validator callable. It is unit-tested with `FakeLLM` and a trivial `validate_output`, no DB. **A `PROVIDER_OK` whose body fails schema validation is treated as malformed structure** (invalid → repair). **Refusal fails into clarification directly (never repair).** `max_tokens` / `schema_fault` / `transient` → bounded retry. `auth_error` → fail closed **+ security-audit signal**. Repair (default N=2) and retry budgets are separate and config-gated via keyword args.

**Files:**
- Modify `src/featuregen/intake/llm.py` — add `DEFAULT_REPAIR_BUDGET`, `DEFAULT_RETRY_BUDGET`, `StructuredCallOutcome`, `drive_structured_call`, `_failed` (append after `FakeLLM`; add `replace` + `Callable` to the imports; import `SchemaValidationError`).
- Modify `tests/featuregen/intake/test_llm.py` — add taxonomy tests.

**Interfaces:**
- Consumes: `featuregen.contracts.SchemaValidationError` (raised by `validate_output` on an invalid structure — the same error the document registry raises, so `call_llm` can pass `DocumentSchemaRegistry.validate` directly). `LLMClient`, `LLMRequest`, `FakeLLM`, the `PROVIDER_*`/`STATUS_*` constants, `compute_input_hash` (Task 3.3).
- Produces:
  ```python
  DEFAULT_REPAIR_BUDGET = 2   # config-gated (Decision D5); malformed-structure repairs
  DEFAULT_RETRY_BUDGET = 2    # config-gated; truncation/schema-fault/transient retries

  @dataclass(frozen=True)
  class StructuredCallOutcome:
      output: dict
      self_reported_scores: dict
      status: str                 # STATUS_OK | STATUS_REPAIRED | STATUS_RETRIED | STATUS_FAILED
      validation_result: dict     # {"result": status, "reason"?: str}
      repair_attempts: tuple      # ({attempt, class: "repair"|"retry", reason}, ...)
      cost_metadata: dict
      security_audit_reason: str | None   # set on an auth failure (call_llm security-audits it)

  def drive_structured_call(client: LLMClient, request: LLMRequest,
                            validate_output: Callable[[Mapping[str, Any]], None], *,
                            repair_budget: int = DEFAULT_REPAIR_BUDGET,
                            retry_budget: int = DEFAULT_RETRY_BUDGET) -> StructuredCallOutcome: ...
  ```

Steps:

- [ ] **Write the failing test.** Add to `tests/featuregen/intake/test_llm.py`:
  ```python
  from featuregen.contracts import SchemaValidationError
  from featuregen.intake.llm import (  # add to imports at top
      STATUS_FAILED,
      STATUS_OK,
      STATUS_REPAIRED,
      STATUS_RETRIED,
      drive_structured_call,
  )


  def _needs_entity(output):
      if "entity" not in output:
          raise SchemaValidationError("missing required field: entity")


  def test_ok_first_try():
      fake = FakeLLM()
      fake.script(task="structure_intent", prompt_id="intake.v1",
                  responses=[FakeResponse(output={"entity": "customer"})])
      out = drive_structured_call(fake, _req(), _needs_entity)
      assert out.status == STATUS_OK
      assert out.output == {"entity": "customer"}
      assert out.repair_attempts == ()
      assert out.validation_result == {"result": STATUS_OK}


  def test_provider_ok_but_schema_invalid_repairs_then_validates():
      fake = FakeLLM()
      fake.script(task="structure_intent", prompt_id="intake.v1",
                  responses=[FakeResponse(output={"wrong": 1}),                 # ok token, invalid body
                             FakeResponse(output={"entity": "customer"})])       # repair validates
      out = drive_structured_call(fake, _req(), _needs_entity)
      assert out.status == STATUS_REPAIRED
      assert out.output == {"entity": "customer"}
      assert len(out.repair_attempts) == 1 and out.repair_attempts[0]["class"] == "repair"


  def test_repair_budget_exhausted_fails_into_clarification():
      fake = FakeLLM()
      fake.script(task="structure_intent", prompt_id="intake.v1",
                  responses=[FakeResponse(output={}, provider_status="invalid"),
                             FakeResponse(output={}, provider_status="invalid"),
                             FakeResponse(output={}, provider_status="invalid")])
      out = drive_structured_call(fake, _req(), _needs_entity, repair_budget=2)
      assert out.status == STATUS_FAILED
      assert len(out.repair_attempts) == 2  # N=2 repairs attempted, then fail closed
      assert out.validation_result["result"] == STATUS_FAILED


  def test_refusal_fails_into_clarification_without_repair():
      fake = FakeLLM()
      fake.script(task="structure_intent", prompt_id="intake.v1",
                  responses=[FakeResponse(output={}, provider_status="refusal"),
                             FakeResponse(output={"entity": "customer"})])  # must NOT be consumed
      out = drive_structured_call(fake, _req(), _needs_entity)
      assert out.status == STATUS_FAILED
      assert out.repair_attempts == ()  # a decline is not a malformed structure — no repair


  def test_max_tokens_retries_then_validates():
      fake = FakeLLM()
      fake.script(task="structure_intent", prompt_id="intake.v1",
                  responses=[FakeResponse(output={}, provider_status="max_tokens"),
                             FakeResponse(output={"entity": "customer"})])
      out = drive_structured_call(fake, _req(), _needs_entity)
      assert out.status == STATUS_RETRIED
      assert out.repair_attempts[0]["class"] == "retry"


  def test_auth_error_fails_closed_and_flags_security_audit():
      fake = FakeLLM()
      fake.script(task="structure_intent", prompt_id="intake.v1",
                  responses=[FakeResponse(output={}, provider_status="auth_error")])
      out = drive_structured_call(fake, _req(), _needs_entity)
      assert out.status == STATUS_FAILED
      assert out.security_audit_reason is not None
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/intake/test_llm.py -k "ok_first or repair or refusal or max_tokens or auth" -v` — Expected: FAIL (`ImportError: cannot import name 'drive_structured_call'`).

- [ ] **Minimal implementation.** Update the imports at the top of `src/featuregen/intake/llm.py`:
  ```python
  from collections.abc import Callable, Mapping, Sequence
  from dataclasses import dataclass, field, replace

  from featuregen.contracts import SchemaValidationError
  ```
  Append after `FakeLLM`:
  ```python
  # ---- structured-output taxonomy (§9.2): bounded repair / bounded retry / fail-closed ---------

  DEFAULT_REPAIR_BUDGET = 2   # config-gated malformed-structure repairs (Decision D5)
  DEFAULT_RETRY_BUDGET = 2    # config-gated truncation/schema-fault/transient retries

  _RETRYABLE = (PROVIDER_MAX_TOKENS, PROVIDER_SCHEMA_FAULT, PROVIDER_TRANSIENT)


  @dataclass(frozen=True)
  class StructuredCallOutcome:
      output: dict
      self_reported_scores: dict
      status: str                 # STATUS_*
      validation_result: dict     # {"result": status, "reason"?: str}
      repair_attempts: tuple      # ({attempt, class, reason}, ...)
      cost_metadata: dict
      security_audit_reason: str | None


  def _failed(resp: LLMResult, attempts: list, reason: str, *, security_audit: bool = False) -> StructuredCallOutcome:
      return StructuredCallOutcome(
          output=dict(resp.output),
          self_reported_scores=dict(resp.self_reported_scores),
          status=STATUS_FAILED,
          validation_result={"result": STATUS_FAILED, "reason": reason},
          repair_attempts=tuple(attempts),
          cost_metadata={},
          security_audit_reason=reason if security_audit else None,
      )


  def drive_structured_call(
      client: LLMClient,
      request: LLMRequest,
      validate_output: Callable[[Mapping[str, Any]], None],
      *,
      repair_budget: int = DEFAULT_REPAIR_BUDGET,
      retry_budget: int = DEFAULT_RETRY_BUDGET,
  ) -> StructuredCallOutcome:
      """Drive one structured LLM call to a fail-closed disposition (§9.2). Provider-agnostic:
      re-invokes `client.call` for repairs/retries. `validate_output(output)` raises
      SchemaValidationError on an invalid structure. A `PROVIDER_OK` whose body fails validation is
      malformed structure → bounded repair. Refusal → fail into clarification directly (no repair).
      Truncation/schema-fault/transient → bounded retry. Auth → fail closed + security-audit signal.
      Nothing proceeds on an unresolved outcome; an invalid structure is a doubt, not a value."""
      attempts: list[dict] = []
      repairs_used = 0
      retries_used = 0
      errors: list[str] = []
      resp = client.call(request)
      while True:
          ps = resp.status
          if ps == PROVIDER_OK:
              try:
                  validate_output(resp.output)
              except SchemaValidationError as exc:
                  ps = PROVIDER_INVALID
                  errors.append(str(exc))
              else:
                  status = (
                      STATUS_REPAIRED if repairs_used
                      else STATUS_RETRIED if retries_used
                      else STATUS_OK
                  )
                  return StructuredCallOutcome(
                      output=dict(resp.output),
                      self_reported_scores=dict(resp.self_reported_scores),
                      status=status,
                      validation_result={"result": status},
                      repair_attempts=tuple(attempts),
                      cost_metadata={},
                      security_audit_reason=None,
                  )
          if ps == PROVIDER_INVALID:
              if repairs_used < repair_budget:
                  repairs_used += 1
                  reason = errors[-1] if errors else "structure did not validate"
                  attempts.append({"attempt": repairs_used, "class": "repair", "reason": reason})
                  # re-prompt with the accumulated validation error, via a transient (`_`-prefixed)
                  # key EXCLUDED from the identity hash so the repair keeps its parent's identity.
                  request = replace(request, inputs={**request.inputs, "_repair_errors": list(errors)})
                  resp = client.call(request)
                  continue
              return _failed(resp, attempts, "repair budget exhausted (malformed structure)")
          if ps == PROVIDER_REFUSAL:
              return _failed(resp, attempts, "provider refusal (policy decline)")
          if ps in _RETRYABLE:
              if retries_used < retry_budget:
                  retries_used += 1
                  attempts.append({"attempt": retries_used, "class": "retry", "reason": ps})
                  resp = client.call(request)
                  continue
              return _failed(resp, attempts, f"{ps} retry budget exhausted")
          if ps == PROVIDER_AUTH_ERROR:
              return _failed(resp, attempts, "provider auth failure", security_audit=True)
          # PROVIDER_NON_RETRYABLE and any unknown token → fail closed
          return _failed(resp, attempts, f"non-retryable provider outcome ({ps})")
  ```

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/intake/test_llm.py -v` — Expected: PASS (all FakeLLM + taxonomy tests).

- [ ] **Lint.** `uv run ruff check src/featuregen/intake/llm.py` — Expected: clean.

- [ ] **Commit.** `git add src/featuregen/intake/llm.py tests/featuregen/intake/test_llm.py && git commit -m "feat(intake): structured-output taxonomy (bounded repair/retry, fail-closed, refusal->clarify)"`

---

### Task 3.5: The append-only `llm_call` record store + full-identity idempotency key

DB-backed (mirrors SP-1's `overlay/evidence.py`). `record_llm_call` INSERTs one immutable row (never updated); `read_llm_call` resolves a `call_ref`; `find_llm_call` implements the **full-identity idempotency lookup** — it queries the `(run_id, task, input_hash)` candidate set and compares the remaining identity components (`provider, model, prompt_id, prompt_version, output_schema_id, output_schema_version, redaction_version, generation_settings`) in Python (canonicalizing `generation_settings`). The narrow `(run_id, task, input_hash, prompt_version)` key is insufficient — any provider/model/schema/prompt/redaction/settings change forces a fresh call.

**Files:**
- Modify `src/featuregen/intake/llm.py` — add DB imports, `LLMCallRecord`, `_canonical`, `_record_from_row`, `record_llm_call`, `read_llm_call`, `find_llm_call`, `_result_from_record` (append after `drive_structured_call`).
- Create `tests/featuregen/intake/test_llm_store.py` — DB-backed tests (the `db` fixture).

**Interfaces:**
- Consumes: the `llm_call` table (P1 DDL above); `featuregen.contracts.DbConn`; `featuregen.idgen.mint_id` (`llmc_` ids); `psycopg.rows.dict_row`; `psycopg.types.json.Jsonb`. `LLMRequest`, `LLMResult`, the `STATUS_*` constants (Task 3.3).
- Produces:
  ```python
  @dataclass(frozen=True)
  class LLMCallRecord:
      llm_call_ref: str; run_id: str; task: str; provider: str; model: str
      prompt_id: str; prompt_version: int; output_schema_id: str; output_schema_version: int
      generation_settings: dict; redaction_version: str; input_hash: str
      redacted_input: dict; input_redaction: dict; raw_output: dict; validation_result: dict
      repair_attempts: list; latency_ms: int | None; cost_metadata: dict | None
      created_at: object; created_by: dict

  def record_llm_call(conn, *, run_id, request: LLMRequest, input_hash, redaction_version,
                      input_redaction, raw_output, validation_result, repair_attempts,
                      latency_ms, cost_metadata, created_by) -> str: ...   # returns llm_call_ref

  def read_llm_call(conn, call_ref: str) -> LLMCallRecord: ...             # KeyError if unknown

  def find_llm_call(conn, *, run_id, task, input_hash, provider, model, prompt_id, prompt_version,
                    output_schema_id, output_schema_version, redaction_version,
                    generation_settings) -> LLMCallRecord | None: ...      # full-identity dedup

  def _result_from_record(rec: LLMCallRecord) -> LLMResult: ...            # rebuild LLMResult on reuse
  ```

Steps:

- [ ] **Write the failing test.** Create `tests/featuregen/intake/test_llm_store.py`:
  ```python
  import pytest

  from featuregen.contracts.identity import identity_to_jsonb
  from featuregen.idgen import new_run_id
  from featuregen.intake.llm import (
      STATUS_OK,
      LLMRequest,
      find_llm_call,
      read_llm_call,
      record_llm_call,
  )
  from tests.featuregen.intake._helpers import service_actor


  def _req(gen=None):
      return LLMRequest(
          task="structure_intent", prompt_id="intake.v1", prompt_version=1,
          inputs={"redacted_intent": "count declined auths", "catalog_metadata": {},
                  "raw_input_classification": "clean", "redaction_version": "default-redactor@1"},
          output_schema_id="TEST_STRUCT", output_schema_version=1,
          generation_settings=gen or {"provider": "fake", "model": "fake-1", "max_tokens": 1024},
      )


  def _record(db, run_id, req):
      return record_llm_call(
          db, run_id=run_id, request=req, input_hash="hash-abc",
          redaction_version="default-redactor@1", input_redaction={"redacted_spans": []},
          raw_output={"output": {"entity": "customer"}, "self_reported_scores": {}},
          validation_result={"result": STATUS_OK}, repair_attempts=[],
          latency_ms=3, cost_metadata={"input_tokens": 40}, created_by=identity_to_jsonb(service_actor()),
      )


  def test_record_and_read_round_trip(db):
      run_id = new_run_id()
      ref = _record(db, run_id, _req())
      assert ref.startswith("llmc_")
      rec = read_llm_call(db, ref)
      assert rec.run_id == run_id and rec.task == "structure_intent"
      assert rec.provider == "fake" and rec.model == "fake-1"
      assert rec.redacted_input["redacted_intent"] == "count declined auths"   # replayable
      assert rec.raw_output["output"] == {"entity": "customer"}
      assert rec.validation_result == {"result": STATUS_OK}


  def test_read_unknown_raises(db):
      with pytest.raises(KeyError):
          read_llm_call(db, "llmc_nope")


  def test_find_matches_full_identity(db):
      run_id = new_run_id()
      req = _req()
      ref = _record(db, run_id, req)
      hit = find_llm_call(
          db, run_id=run_id, task=req.task, input_hash="hash-abc",
          provider="fake", model="fake-1", prompt_id="intake.v1", prompt_version=1,
          output_schema_id="TEST_STRUCT", output_schema_version=1,
          redaction_version="default-redactor@1", generation_settings=req.generation_settings,
      )
      assert hit is not None and hit.llm_call_ref == ref


  def test_find_misses_on_any_identity_change(db):
      run_id = new_run_id()
      _record(db, run_id, _req())
      # a changed generation setting (max_tokens) must NOT reuse the stale record
      miss = find_llm_call(
          db, run_id=run_id, task="structure_intent", input_hash="hash-abc",
          provider="fake", model="fake-1", prompt_id="intake.v1", prompt_version=1,
          output_schema_id="TEST_STRUCT", output_schema_version=1,
          redaction_version="default-redactor@1",
          generation_settings={"provider": "fake", "model": "fake-1", "max_tokens": 2048},
      )
      assert miss is None
      # a changed model likewise misses
      assert find_llm_call(
          db, run_id=run_id, task="structure_intent", input_hash="hash-abc",
          provider="fake", model="fake-2", prompt_id="intake.v1", prompt_version=1,
          output_schema_id="TEST_STRUCT", output_schema_version=1,
          redaction_version="default-redactor@1",
          generation_settings={"provider": "fake", "model": "fake-1", "max_tokens": 1024},
      ) is None
  ```

- [ ] **Create the test helper.** Create `tests/featuregen/intake/_helpers.py`:
  ```python
  from featuregen.contracts import IdentityEnvelope


  def service_actor() -> IdentityEnvelope:
      """The platform/service principal SP-2's auditable-LLM calls run as (service:intake-agent)."""
      return IdentityEnvelope(
          subject="service:intake-agent",
          actor_kind="service",
          authenticated=True,
          auth_method="mtls",
          role_claims=("intake-agent",),
          source_of_authority="platform",
          attestation="sp2-intake-agent",
      )
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/intake/test_llm_store.py -v` — Expected: FAIL (`ImportError: cannot import name 'record_llm_call'`), or a missing-table error if P1's `llm_call` migration is not yet applied (this task depends on P1).

- [ ] **Minimal implementation.** Add to the imports at the top of `src/featuregen/intake/llm.py`:
  ```python
  from psycopg.rows import dict_row
  from psycopg.types.json import Jsonb

  from featuregen.contracts.db import DbConn
  from featuregen.idgen import mint_id
  ```
  Append after `drive_structured_call`:
  ```python
  # ---- the append-only llm_call record store (§9.3) -------------------------------------------


  @dataclass(frozen=True)
  class LLMCallRecord:
      llm_call_ref: str
      run_id: str
      task: str
      provider: str
      model: str
      prompt_id: str
      prompt_version: int
      output_schema_id: str
      output_schema_version: int
      generation_settings: dict
      redaction_version: str
      input_hash: str
      redacted_input: dict
      input_redaction: dict
      raw_output: dict
      validation_result: dict
      repair_attempts: list
      latency_ms: int | None
      cost_metadata: dict | None
      created_at: object
      created_by: dict


  def _canonical(obj: Any) -> str:
      return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


  def _record_from_row(row: Mapping[str, Any]) -> LLMCallRecord:
      return LLMCallRecord(
          llm_call_ref=row["llm_call_ref"], run_id=row["run_id"], task=row["task"],
          provider=row["provider"], model=row["model"], prompt_id=row["prompt_id"],
          prompt_version=row["prompt_version"], output_schema_id=row["output_schema_id"],
          output_schema_version=row["output_schema_version"],
          generation_settings=row["generation_settings"], redaction_version=row["redaction_version"],
          input_hash=row["input_hash"], redacted_input=row["redacted_input"],
          input_redaction=row["input_redaction"], raw_output=row["raw_output"],
          validation_result=row["validation_result"], repair_attempts=row["repair_attempts"],
          latency_ms=row["latency_ms"], cost_metadata=row["cost_metadata"],
          created_at=row["created_at"], created_by=row["created_by"],
      )


  def record_llm_call(
      conn: DbConn,
      *,
      run_id: str,
      request: LLMRequest,
      input_hash: str,
      redaction_version: str,
      input_redaction: Mapping[str, Any],
      raw_output: Mapping[str, Any],      # {"output": ..., "self_reported_scores": ...}
      validation_result: Mapping[str, Any],
      repair_attempts: list,
      latency_ms: int | None,
      cost_metadata: Mapping[str, Any] | None,
      created_by: Mapping[str, Any],      # identity_to_jsonb(actor)
  ) -> str:
      """Write ONE immutable llm_call record (§9.3) and return its `llm_call_ref`. Append-only: each
      call mints a fresh `llmc_` id and INSERTs — there is no update path. Stores the REDACTED input
      itself (`redacted_input`, replayable — never the raw intent, which stays in SP-0's encrypted
      raw_input_ref). `provider`/`model` are lifted from generation_settings into their own columns."""
      gs = dict(request.generation_settings)
      ref = mint_id("llmc")
      conn.execute(
          """
          INSERT INTO llm_call
              (llm_call_ref, run_id, task, provider, model, prompt_id, prompt_version,
               output_schema_id, output_schema_version, generation_settings, redaction_version,
               input_hash, redacted_input, input_redaction, raw_output, validation_result,
               repair_attempts, latency_ms, cost_metadata, created_by)
          VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
          """,
          (
              ref, run_id, request.task, gs.get("provider"), gs.get("model"),
              request.prompt_id, request.prompt_version, request.output_schema_id,
              request.output_schema_version, Jsonb(gs), redaction_version, input_hash,
              Jsonb(dict(request.inputs)), Jsonb(dict(input_redaction)), Jsonb(dict(raw_output)),
              Jsonb(dict(validation_result)), Jsonb(list(repair_attempts)), latency_ms,
              Jsonb(dict(cost_metadata)) if cost_metadata is not None else None, Jsonb(dict(created_by)),
          ),
      )
      return ref


  def read_llm_call(conn: DbConn, call_ref: str) -> LLMCallRecord:
      """Resolve an `llm_call_ref` to its immutable record. Raises KeyError if unknown."""
      with conn.cursor(row_factory=dict_row) as cur:
          cur.execute("SELECT * FROM llm_call WHERE llm_call_ref = %s", (call_ref,))
          row = cur.fetchone()
      if row is None:
          raise KeyError(f"unknown llm_call_ref {call_ref!r}")
      return _record_from_row(row)


  def find_llm_call(
      conn: DbConn,
      *,
      run_id: str,
      task: str,
      input_hash: str,
      provider: str,
      model: str,
      prompt_id: str,
      prompt_version: int,
      output_schema_id: str,
      output_schema_version: int,
      redaction_version: str,
      generation_settings: Mapping[str, Any],
  ) -> LLMCallRecord | None:
      """Full-identity idempotency lookup (§9.3, Decision D16): reuse a record ONLY when EVERY
      identity component matches. Queries the (run_id, task, input_hash) candidate set (indexed) and
      compares the rest — including a canonicalized generation_settings — in Python."""
      with conn.cursor(row_factory=dict_row) as cur:
          cur.execute(
              "SELECT * FROM llm_call WHERE run_id=%s AND task=%s AND input_hash=%s "
              "ORDER BY created_at ASC",
              (run_id, task, input_hash),
          )
          rows = cur.fetchall()
      target_gs = _canonical(dict(generation_settings))
      for row in rows:
          if (
              row["provider"] == provider
              and row["model"] == model
              and row["prompt_id"] == prompt_id
              and row["prompt_version"] == prompt_version
              and row["output_schema_id"] == output_schema_id
              and row["output_schema_version"] == output_schema_version
              and row["redaction_version"] == redaction_version
              and _canonical(row["generation_settings"]) == target_gs
          ):
              return _record_from_row(row)
      return None


  def _result_from_record(rec: LLMCallRecord) -> LLMResult:
      """Rebuild the caller-facing LLMResult from a stored record (idempotent reuse — no new call)."""
      return LLMResult(
          output=dict(rec.raw_output.get("output", {})),
          self_reported_scores=dict(rec.raw_output.get("self_reported_scores", {})),
          call_ref=rec.llm_call_ref,
          status=rec.validation_result.get("result", STATUS_FAILED),
      )
  ```

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/intake/test_llm_store.py -v` — Expected: PASS (requires P1's `llm_call` table migration applied by the test harness).

- [ ] **Commit.** `git add src/featuregen/intake/llm.py tests/featuregen/intake/test_llm_store.py tests/featuregen/intake/_helpers.py && git commit -m "feat(intake): append-only llm_call store + full-identity idempotency key"`

---

### Task 3.6: `call_llm` — the event-sourced wrapper (egress → dedup → taxonomy → record → `LLM_CALL_RECORDED`)

The wrapper ties it together (overview §9.1, §9.3). Order: (1) **egress guard** — on `EgressViolation`, security-audit + re-raise (hard failure, no dispatch); (2) **idempotency** — `find_llm_call` on the full identity; reuse ⟹ return the reconstructed `LLMResult` with **no new call, no new record, no new event**; (3) **drive** the taxonomy, validating the LLM output against the registered output-schema via `DocumentSchemaRegistry(conn).validate(output_schema_id, output_schema_version, output)`; (4) **record** the `llm_call`; (5) **security-audit** on an auth failure; (6) **emit `LLM_CALL_RECORDED`** on the `feature_contract` aggregate. Returns the final `LLMResult` (`STATUS_*`) with the real `call_ref`.

**Files:**
- Modify `src/featuregen/intake/llm.py` — add imports (`time`, the **R1** store seam `append_feature_contract_event` from `intake.store`, the **R17** `LLM_CALL_RECORDED` constant from `intake.events`, `DocumentSchemaRegistry`, `record_security_event`, `assert_llm_safe` + reserved keys from `redaction`) and `call_llm` (append at end).
- Create `tests/featuregen/intake/test_call_llm.py` — DB-backed tests.

**Interfaces:**
- Consumes:
  - From P1 (`sp2-01`): **R1** — `featuregen.intake.store.append_feature_contract_event(conn, *, run_id, type, payload, actor, request_id=None, provenance=None, expected_version=None, caused_by=None) -> EventEnvelope`, the ONE Feature-Contract append seam (sets `aggregate="feature_contract"`, `aggregate_id == feature_contract_id == run_id` internally — mirroring SP-1's `overlay_fact_id`). `call_llm` **IMPORTS this seam verbatim** and never calls the low-level `featuregen.aggregates._append.append` directly, nor redefines an `append_fc_event`. **R17** — the `LLM_CALL_RECORDED` constant is imported from `featuregen.intake.events` (never redeclared here); its registered `@v1` schema is provided by `featuregen.intake.events.register_sp2_event_types(event_registry())`. Also the `llm_call` table.
  - From SP-0: `featuregen.documents.registry.DocumentSchemaRegistry(conn).validate(type_name, schema_version, body)` (raises `SchemaValidationError` on an invalid structure — the output-schema resolver, per-connection, over `document_type_registry`); `featuregen.security.audit.record_security_event(conn, *, event_type, actor, attempted_action, decision, reason, aggregate, aggregate_id)`; `featuregen.contracts.IdentityEnvelope`; `featuregen.events.store.load_stream`.
  - From this module: `assert_llm_safe`, `EgressViolation`, `INPUT_KEY_REDACTION_VERSION`, `INPUT_KEY_REDACTION` (from `redaction.py`); `drive_structured_call`, `record_llm_call`, `find_llm_call`, `_result_from_record`, `compute_input_hash`, the `STATUS_*` constants.
- Produces:
  ```python
  def call_llm(conn, client: LLMClient, request: LLMRequest, *,
               run_id: str, actor: IdentityEnvelope) -> LLMResult:
      # egress-guards (→ security-audit + raise on violation), dedups on the full identity,
      # validates against the registered output-schema, records the llm_call, emits
      # LLM_CALL_RECORDED on the feature_contract aggregate. Returns the final LLMResult.
  ```

Steps:

- [ ] **Write the failing test.** Create `tests/featuregen/intake/test_call_llm.py`:
  ```python
  import pytest

  from featuregen.documents.registry import DocumentSchemaRegistry
  from featuregen.events.registry import event_registry
  from featuregen.events.store import load_stream
  from featuregen.idgen import new_run_id
  from featuregen.intake.events import register_sp2_event_types  # from P1 (sp2-01)
  from featuregen.intake.llm import (
      STATUS_FAILED,
      STATUS_OK,
      FakeLLM,
      FakeResponse,
      LLMRequest,
      call_llm,
      read_llm_call,
  )
  from featuregen.intake.redaction import (
      DefaultIntentRedactor,
      EgressViolation,
      build_llm_inputs,
  )
  from tests.featuregen.intake._helpers import service_actor

  _OUT_SCHEMA = {
      "type": "object",
      "required": ["entity"],
      "properties": {"entity": {"type": "string"}},
      "additionalProperties": True,
  }


  def _setup(db):
      register_sp2_event_types(event_registry())  # LLM_CALL_RECORDED@v1 (P1)
      DocumentSchemaRegistry(db).register_schema("TEST_STRUCT", 1, _OUT_SCHEMA, owner="test")


  def _req(gen=None, cls="clean"):
      red = DefaultIntentRedactor().redact("count declined auths per customer", cls)
      inputs = build_llm_inputs(
          red, catalog_metadata={"objects": ["card_authorizations"]}, raw_input_classification=cls
      )
      return LLMRequest(
          task="structure_intent", prompt_id="intake.v1", prompt_version=1, inputs=inputs,
          output_schema_id="TEST_STRUCT", output_schema_version=1,
          generation_settings=gen or {"provider": "fake", "model": "fake-1", "max_tokens": 1024},
      )


  def _fake_ok(seq=None):
      fake = FakeLLM()
      fake.script(task="structure_intent", prompt_id="intake.v1",
                  responses=seq or [FakeResponse(output={"entity": "customer"},
                                                 self_reported_scores={"entity": {"ambiguity": 0.05}})])
      return fake


  def test_ok_records_and_emits_event(db):
      _setup(db)
      run_id = new_run_id()
      res = call_llm(db, _fake_ok(), _req(), run_id=run_id, actor=service_actor())
      assert res.status == STATUS_OK
      assert res.output == {"entity": "customer"}
      assert res.call_ref.startswith("llmc_")
      rec = read_llm_call(db, res.call_ref)
      assert rec.run_id == run_id
      assert rec.redacted_input["redacted_intent"] == "count declined auths per customer"  # replayable
      stream = load_stream(db, "feature_contract", run_id)
      assert [e.type for e in stream] == ["LLM_CALL_RECORDED"]
      assert stream[0].payload["llm_call_ref"] == res.call_ref
      assert stream[0].payload["status"] == STATUS_OK


  def test_idempotent_reuse_no_double_charge(db):
      _setup(db)
      run_id = new_run_id()
      fake = _fake_ok()  # scripted ONCE — a reuse must not call the provider again
      r1 = call_llm(db, fake, _req(), run_id=run_id, actor=service_actor())
      r2 = call_llm(db, fake, _req(), run_id=run_id, actor=service_actor())
      assert r1.call_ref == r2.call_ref
      assert db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s", (run_id,)).fetchone()[0] == 1
      stream = load_stream(db, "feature_contract", run_id)
      assert len([e for e in stream if e.type == "LLM_CALL_RECORDED"]) == 1


  def test_settings_change_forces_fresh_call(db):
      _setup(db)
      run_id = new_run_id()
      fake = _fake_ok(seq=[FakeResponse(output={"entity": "customer"}),
                           FakeResponse(output={"entity": "customer"})])
      r1 = call_llm(db, fake, _req(), run_id=run_id, actor=service_actor())
      r2 = call_llm(db, fake, _req(gen={"provider": "fake", "model": "fake-1", "max_tokens": 2048}),
                    run_id=run_id, actor=service_actor())
      assert r1.call_ref != r2.call_ref
      assert db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s", (run_id,)).fetchone()[0] == 2


  def test_refusal_fails_into_clarification_and_is_recorded(db):
      _setup(db)
      run_id = new_run_id()
      fake = FakeLLM()
      fake.script(task="structure_intent", prompt_id="intake.v1",
                  responses=[FakeResponse(output={}, provider_status="refusal")])
      res = call_llm(db, fake, _req(), run_id=run_id, actor=service_actor())
      assert res.status == STATUS_FAILED
      rec = read_llm_call(db, res.call_ref)
      assert rec.validation_result["result"] == STATUS_FAILED   # the failure is audited, not swallowed
      assert load_stream(db, "feature_contract", run_id)[0].type == "LLM_CALL_RECORDED"


  def test_egress_violation_hard_fails_and_security_audits(db):
      _setup(db)
      run_id = new_run_id()
      bad = _req(cls="clean")
      bad.inputs["raw_input_classification"] = "unscanned"  # tamper past the redactor
      with pytest.raises(EgressViolation):
          call_llm(db, _fake_ok(), bad, run_id=run_id, actor=service_actor())
      # hard failure recorded in the security-audit stream; no llm_call, no domain event
      assert db.execute(
          "SELECT count(*) FROM security_audit WHERE event_type='LLM_EGRESS_BLOCKED'"
      ).fetchone()[0] == 1
      assert db.execute("SELECT count(*) FROM llm_call WHERE run_id=%s", (run_id,)).fetchone()[0] == 0
      assert load_stream(db, "feature_contract", run_id) == []
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/intake/test_call_llm.py -v` — Expected: FAIL (`ImportError: cannot import name 'call_llm'`).

- [ ] **Minimal implementation.** Add to the imports at the top of `src/featuregen/intake/llm.py`:
  ```python
  import time

  from featuregen.contracts import IdentityEnvelope
  from featuregen.contracts.identity import identity_to_jsonb
  from featuregen.documents.registry import DocumentSchemaRegistry
  from featuregen.intake.events import LLM_CALL_RECORDED  # R17 — IMPORTED, never redeclared here
  from featuregen.intake.redaction import (
      INPUT_KEY_REDACTION,
      INPUT_KEY_REDACTION_VERSION,
      EgressViolation,
      assert_llm_safe,
  )
  from featuregen.intake.store import append_feature_contract_event  # R1 — the ONE FC append seam (P1)
  from featuregen.security.audit import record_security_event
  ```
  Append at the end of `src/featuregen/intake/llm.py`:
  ```python
  # ---- the event-sourced wrapper (§9.1, §9.3) -------------------------------------------------
  # NOTE (R17): LLM_CALL_RECORDED is IMPORTED from featuregen.intake.events (P1) above — it is the
  # single source for the constant and is NEVER redeclared here.


  def call_llm(
      conn: DbConn,
      client: LLMClient,
      request: LLMRequest,
      *,
      run_id: str,
      actor: IdentityEnvelope,
  ) -> LLMResult:
      """The auditable-LLM entry point every SP-2 agent uses (§9.1). Egress-guards (hard-fails a
      violation into the security-audit stream, no dispatch), dedups on the full call identity
      (reuse ⟹ no new call/record/event), drives the §9.2 taxonomy validating against the registered
      output-schema, records ONE immutable llm_call, and emits LLM_CALL_RECORDED on the
      feature_contract aggregate. Returns the final LLMResult (STATUS_*) with the real call_ref."""
      # 1. Egress hard-backstop (§9.4). A violation is a hard failure recorded in the security-audit
      #    stream — never a value, never a warning; no payload is dispatched.
      try:
          assert_llm_safe(request)
      except EgressViolation as exc:
          record_security_event(
              conn,
              event_type="LLM_EGRESS_BLOCKED",
              actor=actor,
              attempted_action="call_llm",
              decision="denied",
              reason=str(exc),
              aggregate="feature_contract",
              aggregate_id=run_id,
          )
          raise

      input_hash = compute_input_hash(request.inputs)
      redaction_version = request.inputs.get(INPUT_KEY_REDACTION_VERSION, "unversioned")
      input_redaction = request.inputs.get(INPUT_KEY_REDACTION, {})
      gs = request.generation_settings

      # 2. Idempotency: a truly identical retry reuses its record (no double-charge, §9.3).
      existing = find_llm_call(
          conn,
          run_id=run_id, task=request.task, input_hash=input_hash,
          provider=gs.get("provider"), model=gs.get("model"),
          prompt_id=request.prompt_id, prompt_version=request.prompt_version,
          output_schema_id=request.output_schema_id,
          output_schema_version=request.output_schema_version,
          redaction_version=redaction_version, generation_settings=gs,
      )
      if existing is not None:
          return _result_from_record(existing)

      # 3. Drive the structured call, validating the LLM output against the REGISTERED output-schema
      #    (structural-only; server-compiled/cross-call-cached in the real adapter, §9.1).
      doc_registry = DocumentSchemaRegistry(conn)

      def validate_output(output: Mapping[str, Any]) -> None:
          doc_registry.validate(request.output_schema_id, request.output_schema_version, output)

      t0 = time.monotonic()
      outcome = drive_structured_call(client, request, validate_output)
      latency_ms = int((time.monotonic() - t0) * 1000)

      # 4. Record the immutable, replayable llm_call (redacted input stored, not hash-only, §9.3).
      call_ref = record_llm_call(
          conn,
          run_id=run_id, request=request, input_hash=input_hash,
          redaction_version=redaction_version, input_redaction=input_redaction,
          raw_output={"output": outcome.output, "self_reported_scores": outcome.self_reported_scores},
          validation_result=outcome.validation_result, repair_attempts=list(outcome.repair_attempts),
          latency_ms=latency_ms, cost_metadata=outcome.cost_metadata,
          created_by=identity_to_jsonb(actor),
      )

      # 5. Auth failures are additionally security-audited (§9.2), never silently swallowed.
      if outcome.security_audit_reason:
          record_security_event(
              conn,
              event_type="LLM_PROVIDER_AUTH_FAILURE",
              actor=actor,
              attempted_action="call_llm",
              decision="denied",
              reason=outcome.security_audit_reason,
              aggregate="feature_contract",
              aggregate_id=run_id,
          )

      # 6. Emit LLM_CALL_RECORDED on the feature_contract aggregate via the R1 store seam.
      #    append_feature_contract_event sets aggregate="feature_contract",
      #    aggregate_id == feature_contract_id == run_id, and the run_id mirror column ALWAYS
      #    populated (= run_id, non-null, for correlation) — feature_id ALWAYS NULL, request_id
      #    optional (X3 one event-identity invariant, mirrors 0504's overlay branch). This is NEVER
      #    appended on the `run` aggregate; call_llm never touches the low-level
      #    featuregen.aggregates._append.append. The redacted body lives in the store (referenced by
      #    call_ref), never inlined in the event. Payload is SEMANTIC-only (R2 — no id fields;
      #    feature_contract_id/run_id ride the typed columns).
      #    X4: LLM_CALL_RECORDED is a NON-lifecycle audit event — fold_feature_contract_state ignores
      #    it and call_llm makes no fold-based decision here, so the append rides current head
      #    (expected_version=None is correct) and is NOT subject to the folded-head CAS rule (that
      #    rule governs the lifecycle-transition commands in P4/P5/P7/P8, not this audit append).
      append_feature_contract_event(
          conn,
          run_id=run_id,
          type=LLM_CALL_RECORDED,
          payload={
              "llm_call_ref": call_ref,
              "task": request.task,
              "status": outcome.status,
              "validation_result": outcome.validation_result.get("result"),
          },
          actor=actor,
      )

      return LLMResult(
          output=outcome.output,
          self_reported_scores=outcome.self_reported_scores,
          call_ref=call_ref,
          status=outcome.status,
      )
  ```

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/intake/test_call_llm.py -v` — Expected: PASS (requires P1's `0508` migration + `feature_contract_id` append widening + `register_sp2_event_types`).

- [ ] **Run the whole intake LLM suite + lint.** `uv run pytest tests/featuregen/intake/ -v && uv run ruff check src/featuregen/intake/llm.py src/featuregen/intake/redaction.py` — Expected: all green, no lint findings.

- [ ] **Commit.** `git add src/featuregen/intake/llm.py tests/featuregen/intake/test_call_llm.py && git commit -m "feat(intake): call_llm event-sourced wrapper (egress→dedup→taxonomy→record→LLM_CALL_RECORDED)"`

---

### Task 3.7: The config-gated real Claude adapter (`llm_claude.py`) — lazy import, no CI, no fallback

The real adapter implements `LLMClient` over the Anthropic SDK, **imported lazily inside the adapter** (never at module import time — CI never imports `anthropic`). It is enabled only when configured; when enabled-but-unavailable it **fails closed** — it never swaps in `FakeLLM` (Decision D5, "no silent production fallback"). Model is config-driven (default `claude-opus-4-8`); the output-schema it sends carries **no PHI/PII in property names / enums / descriptions** (server-compiled + cross-call-cached, §9.1). It maps each provider outcome to the §9.2 provider vocabulary. The full SDK call syntax lives in the **Adapter Appendix** below; the module ships the mapping + the fail-closed gate. Its only CI test asserts (a) importing the module does **not** import `anthropic`, and (b) a disabled/unavailable adapter fails closed (raises), never returning a `FakeLLM`-shaped success. The live smoke test is `skip`-gated on an env var and never runs in CI.

**Files:**
- Create `src/featuregen/intake/llm_claude.py` — `ClaudeConfig`, `LLMAdapterUnavailable`, `ClaudeLLM`, `_map_stop_reason`, `build_claude_llm`.
- Create `tests/featuregen/intake/test_llm_claude.py` — the structural CI test + the skipped smoke test.

**Interfaces:**
- Consumes: `featuregen.intake.llm.LLMClient` / `LLMRequest` / `LLMResult` + the `PROVIDER_*` constants (Task 3.3/3.4); `featuregen.intake.redaction.INPUT_KEY_INTENT` / `INPUT_KEY_CATALOG` (the model-facing content). The `anthropic` SDK is imported **lazily**, never at module scope.
- Produces:
  ```python
  @dataclass(frozen=True)
  class ClaudeConfig:
      enabled: bool = False
      model: str = "claude-opus-4-8"
      max_tokens: int = 4096
      thinking: str = "adaptive"           # adaptive thinking (§9.5); no budget_tokens (400 on 4.8)
      effort: str = "high"

      @classmethod
      def from_env(cls) -> "ClaudeConfig": ...   # FEATUREGEN_LLM_* env, disabled by default

  class LLMAdapterUnavailable(Exception): ...    # enabled-but-unavailable → fail closed (no fallback)

  class ClaudeLLM:                               # LLMClient
      def __init__(self, config: ClaudeConfig) -> None: ...   # lazy: does NOT import anthropic here
      def call(self, request: LLMRequest) -> LLMResult: ...

  def _map_stop_reason(stop_reason: str) -> str: ...          # anthropic stop_reason -> PROVIDER_*
  def build_claude_llm(config: ClaudeConfig | None = None) -> ClaudeLLM: ...
  ```

Steps:

- [ ] **Write the failing test.** Create `tests/featuregen/intake/test_llm_claude.py`:
  ```python
  import pytest

  from featuregen.intake.llm import (
      PROVIDER_AUTH_ERROR,
      PROVIDER_MAX_TOKENS,
      PROVIDER_OK,
      PROVIDER_REFUSAL,
      PROVIDER_TRANSIENT,
  )
  from featuregen.intake.llm_claude import (
      ClaudeConfig,
      ClaudeLLM,
      LLMAdapterUnavailable,
      _map_stop_reason,
  )


  def test_importing_adapter_does_not_import_anthropic():
      # The real SDK must never load at import time — CI never depends on `anthropic` (D5, §15).
      import featuregen.intake.llm_claude as mod
      # the module holds no module-level `anthropic` symbol (it is imported lazily inside .call)
      assert not hasattr(mod, "anthropic")


  def _bare_request():
      from featuregen.intake.llm import LLMRequest

      return LLMRequest(
          task="structure_intent", prompt_id="intake.v1", prompt_version=1,
          inputs={"redacted_intent": "x", "catalog_metadata": {}, "raw_input_classification": "clean"},
          output_schema_id="S", output_schema_version=1,
          generation_settings={"provider": "anthropic", "model": "claude-opus-4-8"},
      )


  def test_disabled_adapter_fails_closed_not_fallback():
      # An unconfigured/disabled adapter must fail closed — never silently return a FakeLLM result.
      adapter = ClaudeLLM(ClaudeConfig(enabled=False))
      with pytest.raises(LLMAdapterUnavailable):
          adapter.call(_bare_request())


  def test_stop_reason_mapping_to_provider_taxonomy():
      assert _map_stop_reason("end_turn") == PROVIDER_OK
      assert _map_stop_reason("refusal") == PROVIDER_REFUSAL          # policy decline → clarify
      assert _map_stop_reason("max_tokens") == PROVIDER_MAX_TOKENS    # truncation → retry
      assert _map_stop_reason("tool_use") == PROVIDER_OK


  @pytest.mark.skipif(
      not __import__("os").environ.get("FEATUREGEN_LLM_SMOKE"),
      reason="config-gated live Claude smoke test; never gated in CI (D5, §15)",
  )
  def test_live_claude_structure_intent_smoke():  # pragma: no cover
      from featuregen.intake.llm import LLMRequest

      adapter = ClaudeLLM(ClaudeConfig.from_env())
      out = adapter.call(
          LLMRequest(
              task="structure_intent", prompt_id="intake.v1", prompt_version=1,
              inputs={"redacted_intent": "90-day rolling count of declined card authorizations",
                      "catalog_metadata": {"objects": ["card_authorizations"]},
                      "raw_input_classification": "clean"},
              output_schema_id="DRAFT_STRUCTURE", output_schema_version=1,
              generation_settings={"provider": "anthropic", "model": "claude-opus-4-8"},
          )
      )
      assert out.status in (PROVIDER_OK, PROVIDER_REFUSAL, PROVIDER_MAX_TOKENS,
                            PROVIDER_TRANSIENT, PROVIDER_AUTH_ERROR)
  ```

- [ ] **Run it — expect failure.** `uv run pytest tests/featuregen/intake/test_llm_claude.py -v` — Expected: FAIL (`ModuleNotFoundError: No module named 'featuregen.intake.llm_claude'`).

- [ ] **Minimal implementation.** Create `src/featuregen/intake/llm_claude.py`:
  ```python
  """Config-gated real Claude adapter (spec §9.5, Decision D12). Ships but is NEVER required in CI:
  `anthropic` is imported LAZILY inside `.call`, never at module scope. Default model
  `claude-opus-4-8`, adaptive thinking, structured outputs via output_config.format. Maps each
  provider outcome to the §9.2 PROVIDER_* taxonomy. NO production fallback to FakeLLM — an
  enabled-but-unavailable adapter fails closed (LLMAdapterUnavailable) into the clarification/manual
  path. The output-schema carries NO PHI/PII (server-compiled, cross-call-cached, §9.1).

  See the Adapter Appendix in docs/plans/2026-07-01-sp2-03-llm-envelope.md for the full SDK call.
  """
  from __future__ import annotations

  import os
  from dataclasses import dataclass

  from featuregen.intake.llm import (
      PROVIDER_AUTH_ERROR,
      PROVIDER_MAX_TOKENS,
      PROVIDER_NON_RETRYABLE,
      PROVIDER_OK,
      PROVIDER_REFUSAL,
      PROVIDER_TRANSIENT,
      LLMRequest,
      LLMResult,
  )
  from featuregen.intake.redaction import INPUT_KEY_CATALOG, INPUT_KEY_INTENT


  @dataclass(frozen=True)
  class ClaudeConfig:
      enabled: bool = False
      model: str = "claude-opus-4-8"       # config-driven; never hard-coded at a call site
      max_tokens: int = 4096
      thinking: str = "adaptive"           # adaptive thinking (§9.5); budget_tokens is a 400 on 4.8
      effort: str = "high"

      @classmethod
      def from_env(cls) -> "ClaudeConfig":
          return cls(
              enabled=os.environ.get("FEATUREGEN_LLM_PROVIDER") == "anthropic",
              model=os.environ.get("FEATUREGEN_LLM_MODEL", "claude-opus-4-8"),
              max_tokens=int(os.environ.get("FEATUREGEN_LLM_MAX_TOKENS", "4096")),
              thinking=os.environ.get("FEATUREGEN_LLM_THINKING", "adaptive"),
              effort=os.environ.get("FEATUREGEN_LLM_EFFORT", "high"),
          )


  class LLMAdapterUnavailable(Exception):
      """The real adapter is enabled but unavailable (disabled, missing SDK, or missing creds). The
      platform FAILS CLOSED into the clarification/manual path — it NEVER swaps in FakeLLM (D5)."""


  # Anthropic stop_reason (§9.5) -> the §9.2 PROVIDER_* taxonomy the driver acts on.
  _STOP_REASON_MAP = {
      "end_turn": PROVIDER_OK,
      "tool_use": PROVIDER_OK,
      "stop_sequence": PROVIDER_OK,
      "pause_turn": PROVIDER_OK,
      "refusal": PROVIDER_REFUSAL,       # policy decline → fail into clarification (NOT repair)
      "max_tokens": PROVIDER_MAX_TOKENS,  # truncation → bounded retry
  }


  def _map_stop_reason(stop_reason: str) -> str:
      return _STOP_REASON_MAP.get(stop_reason, PROVIDER_OK)


  class ClaudeLLM:
      """LLMClient over the Anthropic SDK. Construction is lazy — it does NOT import `anthropic`;
      the SDK loads inside `.call` only when enabled, so CI never imports it."""

      def __init__(self, config: ClaudeConfig) -> None:
          self._config = config
          self._client = None  # constructed lazily on first enabled call

      def _ensure_client(self):
          if not self._config.enabled:
              raise LLMAdapterUnavailable(
                  "Claude adapter is not enabled; failing closed (no FakeLLM fallback, D5)"
              )
          if self._client is None:
              try:
                  import anthropic  # lazy: only here, only when enabled — CI never reaches this
              except ImportError as exc:  # enabled but SDK absent → fail closed, never fall back
                  raise LLMAdapterUnavailable(
                      "anthropic SDK not installed; failing closed (no FakeLLM fallback, D5)"
                  ) from exc
              try:
                  self._client = anthropic.Anthropic()
              except Exception as exc:  # missing creds / config → fail closed
                  raise LLMAdapterUnavailable(f"Claude adapter unavailable: {exc}") from exc
          return self._client

      def call(self, request: LLMRequest) -> LLMResult:
          client = self._ensure_client()  # raises LLMAdapterUnavailable if disabled/unavailable
          import anthropic  # already importable if _ensure_client succeeded

          model = request.generation_settings.get("model", self._config.model)
          # Only the redacted, LLM-safe content reaches the model (§9.4). The output-schema is
          # referenced structurally; it carries no PHI/PII (§9.1). See the Adapter Appendix.
          user_content = (
              f"Structure the following intent for task '{request.task}'.\n"
              f"Intent (redacted, LLM-safe): {request.inputs.get(INPUT_KEY_INTENT, '')}\n"
              f"Catalog metadata (names/types/grain only): {request.inputs.get(INPUT_KEY_CATALOG, {})}"
          )
          try:
              resp = client.messages.create(
                  model=model,
                  max_tokens=request.generation_settings.get("max_tokens", self._config.max_tokens),
                  thinking={"type": self._config.thinking},
                  output_config={"effort": self._config.effort},
                  messages=[{"role": "user", "content": user_content}],
                  # NOTE: attach the registered structural output-schema via
                  # output_config={"format": {"type": "json_schema", "schema": <schema>}} — resolved
                  # from output_schema_id/version by the caller; see the Adapter Appendix.
              )
          except anthropic.APIStatusError as exc:  # map transport/status failures to the taxonomy
              status = getattr(exc, "status_code", 0)
              if status in (401, 403):
                  return _fail(PROVIDER_AUTH_ERROR)   # auth/permission → fail closed + security-audit
              if status == 429 or status >= 500:
                  return _fail(PROVIDER_TRANSIENT)    # rate-limit / transient 5xx → bounded retry
              return _fail(PROVIDER_NON_RETRYABLE)    # other non-retryable 4xx → fail closed
          except anthropic.APIConnectionError:
              return _fail(PROVIDER_TRANSIENT)        # network → bounded retry

          provider_status = _map_stop_reason(resp.stop_reason)
          output, scores = _parse_structured(resp)
          return LLMResult(output=output, self_reported_scores=scores, call_ref="", status=provider_status)


  def _fail(provider_status: str) -> LLMResult:
      return LLMResult(output={}, self_reported_scores={}, call_ref="", status=provider_status)


  def _parse_structured(resp) -> tuple[dict, dict]:
      """Extract the schema-constrained JSON body. output_config.format guarantees the first text
      block is valid JSON; a parse failure surfaces as an empty body (→ malformed → repair)."""
      import json

      for block in resp.content:
          if getattr(block, "type", None) == "text":
              try:
                  parsed = json.loads(block.text)
              except (ValueError, TypeError):
                  return {}, {}
              return parsed, dict(parsed.get("field_scores", {}))
      return {}, {}


  def build_claude_llm(config: ClaudeConfig | None = None) -> ClaudeLLM:
      return ClaudeLLM(config or ClaudeConfig.from_env())
  ```

- [ ] **Run it — expect pass.** `uv run pytest tests/featuregen/intake/test_llm_claude.py -v` — Expected: PASS (the live smoke test is `skip`ped; the structural + fail-closed tests pass). CI never imports `anthropic`.

- [ ] **Run the full intake suite + lint.** `uv run pytest tests/featuregen/intake/ -v && uv run ruff check src/featuregen/intake/` — Expected: all green, no lint findings.

- [ ] **Commit.** `git add src/featuregen/intake/llm_claude.py tests/featuregen/intake/test_llm_claude.py && git commit -m "feat(intake): config-gated Claude adapter (lazy import, adaptive thinking, no-fallback fail-closed)"`

---

## Adapter Appendix — the real Claude SDK call (spec §9.5, config-gated, never in CI)

The `ClaudeLLM.call` body above shows the mapping + fail-closed gate; the load-bearing SDK syntax (verified against the Anthropic SDK, model `claude-opus-4-8`, current as of 2026-07) is:

- **Model:** `claude-opus-4-8` (config-driven via `ClaudeConfig.model` / `FEATUREGEN_LLM_MODEL`). Never hard-code a call-site model string.
- **Adaptive thinking:** `thinking={"type": "adaptive"}`. **Do not** send `budget_tokens` — it returns a 400 on Opus 4.8. Depth is controlled by `output_config={"effort": "high"}` (`low`|`medium`|`high`|`xhigh`|`max`).
- **Structured outputs (schema-constrained at the source):** attach the *registered, versioned, structural-only* output-schema (resolved from `output_schema_id`/`output_schema_version`) via
  ```python
  resp = client.messages.create(
      model="claude-opus-4-8",
      max_tokens=4096,
      thinking={"type": "adaptive"},
      output_config={
          "effort": "high",
          "format": {"type": "json_schema", "schema": OUTPUT_SCHEMA},  # no PHI/PII in the schema (§9.1)
      },
      messages=[{"role": "user", "content": user_content}],  # redacted intent + catalog METADATA only
  )
  # output_config.format guarantees the first text block is valid JSON:
  text = next(b.text for b in resp.content if b.type == "text")
  output = json.loads(text)
  ```
  The schema must use `additionalProperties: false` + `required` and avoid unsupported constraints (`minLength`/`minimum`/recursion); the SDK's `client.messages.parse(..., output_format=<pydantic model>)` is the equivalent typed helper. The schema is **server-compiled and cross-call-cached** — its property names / `enum`s / `const`s / `description`s must carry no PHI/PII (§9.1); because SP-2's registered §4 schemas are structural-only and referenced by id/version, no per-call value can enter them, and the §9.4 egress guard scans the resolved outbound body as a second backstop.
- **`stop_reason` → §9.2 disposition** (mapped by `_map_stop_reason`): `end_turn`/`tool_use`/`stop_sequence`/`pause_turn` → `PROVIDER_OK`; `refusal` → `PROVIDER_REFUSAL` (**policy decline → fail into clarification, never repair**; `resp.stop_details.category`/`.explanation` are populated only on a refusal); `max_tokens` → `PROVIDER_MAX_TOKENS` (truncation → bounded retry).
- **Transport/status failures → taxonomy** (SDK typed exceptions; the SDK already auto-retries 408/409/429/5xx internally): `AuthenticationError`(401)/`PermissionDeniedError`(403) → `PROVIDER_AUTH_ERROR` (fail closed **+ security-audited** by `call_llm`); `RateLimitError`(429) / `APIStatusError` ≥500 → `PROVIDER_TRANSIENT` (bounded retry with backoff); `APIConnectionError` → `PROVIDER_TRANSIENT`; other non-retryable 4xx → `PROVIDER_NON_RETRYABLE` (fail closed).
- **No production fallback:** an enabled-but-unavailable adapter raises `LLMAdapterUnavailable` (missing SDK / missing creds) and the platform fails closed into clarification — it never swaps in `FakeLLM` (Decision D5).
- **For large `max_tokens` (>~16K)** stream via `client.messages.stream(...)` + `stream.get_final_message()` to avoid SDK HTTP timeouts; SP-2's structuring calls are small, so the non-streaming `messages.create` above is the default.
