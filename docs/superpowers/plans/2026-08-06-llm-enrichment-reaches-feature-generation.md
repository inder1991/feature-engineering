# LLM Enrichment Reaches Feature Generation — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Every field the LLM produces during ingestion reaches feature generation and the asset-detail UI, including facts a human has not yet confirmed.

**Architecture:** Five groups of work, in dependency order.

| Tasks | What |
|---|---|
| **1–3** | **The LLM driver.** A truncation retry raises the ceiling instead of replaying; a repair carries value-free validation errors to the wire; a timeout is named and not retried. |
| **4, 4b** | **Configuration and bounds.** v4 on, the call ceiling at 100, the per-call timeout at 300s; then every prose and item cap raised for zero truncation, with the byte budget moved to match. |
| **4c, 6d** | **Semantic reach.** 15–20 synonyms per column with the drafter reading the definition (widens the columns); the objective expanded with related business terms before matching (widens the question). The two meet in the middle. |
| **5–8** | **The payload.** Three narrowings currently drop LLM output before the model sees it — the bundle's field lists, the `for_feature_generation` dict literal, and `_table_context`'s VERIFIED-only filter. These widen all three, add adjudication signals at the payload layer, make the generator return its grounding, and add a coverage test so a future field cannot leak silently. |
| **9, 9b, 10** | **Surfaces and vocabulary.** The full column dossier on asset detail; the `is_a` backfill that takes ancestry coverage from 52 of 324 to most of it; end-to-end verification on a real catalog. |

The `governed | hint` authority axis and the `semantic_authority` producer/strength map already exist and already ship — nothing here invents a new labelling mechanism.

Tasks 1–3 come first because Task 4 raises `FEATUREGEN_LLM_TIMEOUT` from 60s to 300s, and the driver currently retries a failure **twice with byte-identical bytes**. Shipping the config change alone would take one doomed call from 180s of waste to 900s — against an 1800s stage deadline, two of them consume a whole stage. Tasks 1–3 make each retry differ from the attempt it follows, so the timeout raise is a fix rather than a multiplier.

**Order matters: do not ship Task 4 before Tasks 1–3.**

**Tech Stack:** Python 3.12 / FastAPI / psycopg3 / PostgreSQL; React + TypeScript + Vitest; pytest.

## Global Constraints

- **Do not change `graph_node`'s operational projection.** The VERIFIED gate on `semantic_bindings/projection.py` stays. This plan changes only what the *feature-generation payload* carries. Generation and execution are separate roads; only the first is in scope.
- **Never emit an unconfirmed value without its authority.** Any field added to the payload must be reachable through `semantic_authority` (evidence-backed fields) or carry an explicit status key (`_table_context` additions).
- **No new migrations.** Every column this plan reads already exists: `sub_domain` / `bian_path` / `process_path` (1051), `table_role` / `event_or_snapshot` (0986).
- **Know which changes are version-gated and which are not.** The v1 payload SHAPE stays byte-identical — every field addition (Tasks 5, 6, 6b, 6c) lives behind the v4 path, so `FEATUREGEN_FEATURE_CONTEXT` unset yields exactly today's thin menu. **But four tasks deliberately change behaviour for every version:** 4b (egress caps apply to all enrichment calls), 4c (the synonym prompt runs at upload time), 6d (relevance ranking is not version-gated), and 9b (the concept registry is global). Do not treat "flag-off is unchanged" as a blanket rule and block on those four — it is a statement about the payload shape only.
- Run backend tests with `uv run pytest <path> -v`; frontend tests with `npm test -- <path>` from `frontend/`.
- Commit after every task. Conventional-commit prefixes (`feat:`, `test:`, `chore:`).

---

### Task 1: A truncation retry raises the ceiling instead of replaying

**Files:**
- Modify: `src/featuregen/intake/llm.py:276-282` (`drive_structured_call`)
- Test: `tests/featuregen/intake/test_llm.py`

**Interfaces:**
- Consumes: `PROVIDER_MAX_TOKENS`, `LLMRequest.generation_settings`.
- Produces: `_escalated(request, provider_status) -> tuple[LLMRequest, int | None]`. Tasks 2 and 3 do not depend on it.

Sampling parameters are removed on current models, so a `max_tokens` truncation is **deterministic**: replaying identical bytes re-truncates, and the retry budget is spent proving it. Three calls, one outcome.

- [ ] **Step 1: Write the failing test**

Add to `tests/featuregen/intake/test_llm.py`:

```python
def test_a_truncation_retry_raises_max_tokens():
    """A max_tokens retry must DIFFER from the attempt it follows, or it cannot succeed."""
    seen: list[int] = []

    class _Truncating:
        def call(self, request):
            seen.append(request.generation_settings["max_tokens"])
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_MAX_TOKENS)

    request = LLMRequest(
        task="t", prompt_id="p", prompt_version=1, inputs={},
        output_schema_id="s", output_schema_version=1,
        generation_settings={"model": "m", "max_tokens": 4096},
        output_schema={"type": "object"})
    outcome = drive_structured_call(_Truncating(), request, lambda _out: None)

    assert seen == [4096, 8192, 16384], "each retry must raise the ceiling"
    assert outcome.status == STATUS_FAILED
    assert outcome.repair_attempts[0]["max_tokens"] == 8192


def test_a_transient_retry_is_replayed_unchanged():
    """Only truncation escalates — a transient fault is genuinely re-attemptable as-is."""
    seen: list[int] = []

    class _Transient:
        def call(self, request):
            seen.append(request.generation_settings["max_tokens"])
            return LLMResult(output={}, self_reported_scores={}, call_ref="",
                             status=PROVIDER_TRANSIENT)

    request = LLMRequest(
        task="t", prompt_id="p", prompt_version=1, inputs={},
        output_schema_id="s", output_schema_version=1,
        generation_settings={"model": "m", "max_tokens": 4096},
        output_schema={"type": "object"})
    drive_structured_call(_Transient(), request, lambda _out: None)

    assert seen == [4096, 4096, 4096]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_llm.py -k truncation_retry -v`
Expected: FAIL — `assert [4096, 4096, 4096] == [4096, 8192, 16384]`

- [ ] **Step 3: Add the escalation helper**

In `src/featuregen/intake/llm.py`, above `drive_structured_call`:

```python
#: A provider 400s a `max_tokens` above the model's real output ceiling, so escalation is bounded —
#: past the cap the retry budget is spent honestly rather than on a request that will be rejected.
_TRUNCATION_ESCALATION = 2.0
_MAX_TOKENS_CEILING = 64_000


def _escalated(request: LLMRequest, provider_status: str) -> tuple[LLMRequest, int | None]:
    """A retry that DIFFERS from the attempt it follows.

    A `max_tokens` truncation is deterministic — sampling parameters are removed on current
    models, so replaying identical bytes re-truncates. Raise the ceiling instead. Schema-fault and
    transient retries ARE genuinely re-attemptable as-is and pass through unchanged.

    Returns `(request, raised_to)`; `raised_to` is None when nothing changed, so the caller can
    record the escalation in `attempts` without inventing a value.
    """
    if provider_status != PROVIDER_MAX_TOKENS:
        return request, None
    gs = dict(request.generation_settings)
    current = int(gs.get("max_tokens") or 0)
    if current <= 0:
        return request, None
    raised = min(int(current * _TRUNCATION_ESCALATION), _MAX_TOKENS_CEILING)
    if raised <= current:
        return request, None       # already at the cap — do not burn a call re-proving it
    gs["max_tokens"] = raised
    return replace(request, generation_settings=gs), raised
```

- [ ] **Step 4: Use it in the retry branch**

Replace the `if ps in _RETRYABLE:` body:

```python
        if ps in _RETRYABLE:
            if retries_used < retry_budget:
                retries_used += 1
                request, raised = _escalated(request, ps)
                attempts.append({"attempt": retries_used, "class": "retry", "reason": ps,
                                 **({"max_tokens": raised} if raised else {})})
                resp = client.call(request)
                provider_calls += 1
                continue
            return _failed(resp, attempts, f"{ps} retry budget exhausted",
                           provider_calls=provider_calls)
```

The escalation rides `attempts`, which is already persisted to `llm_call.repair_attempts` — so the audit shows what changed. Note `record_llm_call` is called with the ORIGINAL request, so `generation_settings` on the row still shows the starting ceiling; the `attempts` entry is what makes the escalation visible.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/featuregen/intake/ -v`
Expected: PASS, including every pre-existing driver test.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/intake/llm.py tests/featuregen/intake/test_llm.py
git commit -m "fix(llm): escalate max_tokens on a truncation retry instead of replaying it"
```

---

### Task 2: Repair feedback reaches the model — value-free

**Files:**
- Modify: `src/featuregen/intake/llm.py` (the `SchemaValidationError` handler)
- Modify: `src/featuregen/intake/llm_claude.py:80-112` (`_wire_prompt`)
- Test: `tests/featuregen/intake/test_llm.py`, `tests/featuregen/intake/test_llm_claude.py`

**Interfaces:**
- Consumes: `SchemaValidationError`, whose `__cause__` is the underlying `jsonschema.ValidationError` (raised with `from exc` at `documents/registry.py:119`).
- Produces: `_safe_reason(exc) -> str`. `_wire_prompt` renders `request.inputs["_repair_errors"]` into the user turn.

`drive_structured_call` already attaches `_repair_errors` to the request — and **no adapter reads it**. `_wire_prompt` renders only `redacted_intent` and `catalog_metadata`, so today a repair sends byte-identical bytes and the budget buys nothing.

> ⚠️ **The trap that makes the obvious fix wrong.** `jsonschema.ValidationError.message` **embeds the offending instance value** (`"'Acme Corporation Ltd' is not of type 'integer'"`). Those values derive from catalog metadata, and `assert_llm_safe` scans only `redacted_intent` and `catalog_metadata` — **not** `_repair_errors`. Rendering the raw message re-egresses content past the PII guard. Carry the JSON pointer and the failed keyword instead: enough for the model to fix its output, structurally incapable of leaking a value.

- [ ] **Step 1: Write the failing tests**

Add to `tests/featuregen/intake/test_llm.py`:

```python
def test_repair_reasons_never_carry_the_offending_value():
    """jsonschema messages embed the instance value; the repair channel must not."""
    import jsonschema

    try:
        jsonschema.validate(instance={"ref": "Acme Corporation Ltd"},
                            schema={"type": "object",
                                    "properties": {"ref": {"type": "integer"}}})
    except jsonschema.ValidationError as ve:
        exc = SchemaValidationError(f"t@v1: {ve.message}")
        exc.__cause__ = ve

    reason = _safe_reason(exc)
    assert "Acme Corporation Ltd" not in reason
    assert "ref" in reason and "type" in reason
```

and to `tests/featuregen/intake/test_llm_claude.py`:

```python
def test_wire_prompt_renders_repair_errors():
    request = LLMRequest(
        task="t", prompt_id="p", prompt_version=1,
        inputs={"redacted_intent": "i", "catalog_metadata": {},
                "_repair_errors": ["$.items[3].ref: failed 'required'"]},
        output_schema_id="s", output_schema_version=1,
        generation_settings={}, output_schema={"type": "object"})
    _system, user_content = _wire_prompt(request)
    assert "$.items[3].ref: failed 'required'" in user_content
    assert "did not validate" in user_content


def test_wire_prompt_omits_the_repair_block_on_a_first_attempt():
    request = LLMRequest(
        task="t", prompt_id="p", prompt_version=1,
        inputs={"redacted_intent": "i", "catalog_metadata": {}},
        output_schema_id="s", output_schema_version=1,
        generation_settings={}, output_schema={"type": "object"})
    _system, user_content = _wire_prompt(request)
    assert "did not validate" not in user_content
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/featuregen/intake/test_llm.py tests/featuregen/intake/test_llm_claude.py -k repair -v`
Expected: FAIL — `NameError: _safe_reason` and the wire assertion.

- [ ] **Step 3: Add the sanitiser**

In `src/featuregen/intake/llm.py`:

```python
#: Bound on the rendered repair feedback — a pathological error list must not grow the payload.
_MAX_REPAIR_FEEDBACK_CHARS = 2000


def _safe_reason(exc: SchemaValidationError) -> str:
    """A VALUE-FREE description of why the structure failed.

    `jsonschema.ValidationError.message` embeds the offending INSTANCE VALUE, which derives from
    the catalog metadata this call egressed and has NOT been through `assert_llm_safe` (that guard
    scans `redacted_intent` and `catalog_metadata` only). Feeding the raw message back into the
    repair prompt would re-egress content past the PII guard. Carry only the JSON pointer and the
    failed keyword — enough for the model to fix its output, structurally incapable of leaking a
    value. `registry.validate` raises `from exc`, so the structured cause is always available.
    """
    cause = exc.__cause__
    path = getattr(cause, "json_path", None)
    validator = getattr(cause, "validator", None)
    if path and validator:
        return f"{path}: failed '{validator}'"
    return "the structure did not match the required schema"
```

- [ ] **Step 4: Use it where the errors are collected**

In `drive_structured_call`, replace `errors.append(str(exc))` with:

```python
            except SchemaValidationError as exc:
                ps = PROVIDER_INVALID
                errors.append(_safe_reason(exc))
```

- [ ] **Step 5: Render it on the wire**

In `src/featuregen/intake/llm_claude.py`, append to `_wire_prompt` before the `return`:

```python
    # A repair re-call carries the errors that refuted the previous answer. Without this the
    # repair sends byte-identical bytes and the budget buys nothing. The key is `_`-prefixed so it
    # stays OUT of `compute_input_hash` — the repair keeps its parent's identity while differing
    # on the wire, which is exactly the intent. The values are already value-free (`_safe_reason`).
    errors = request.inputs.get("_repair_errors")
    if errors:
        rendered = "; ".join(str(e) for e in errors)[:_MAX_REPAIR_FEEDBACK_CHARS]
        user_content += (
            "\n\nYour previous answer did not validate against the required output schema. "
            f"Correct exactly these problems and return the fixed structure: {rendered}"
        )
```

Import `_MAX_REPAIR_FEEDBACK_CHARS` from `featuregen.intake.llm` alongside the existing imports.

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/featuregen/intake/ -v`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/intake/llm.py src/featuregen/intake/llm_claude.py tests/featuregen/intake/
git commit -m "fix(llm): send value-free validation errors on a repair re-call"
```

---

### Task 3: A timeout is named, and is not retried

**Files:**
- Modify: `src/featuregen/intake/llm_claude.py:204-219` (the exception arms)
- Test: `tests/featuregen/intake/test_claude_timeout.py`

**Interfaces:**
- Consumes: `anthropic.APITimeoutError`.
- Produces: nothing later tasks depend on.

`APITimeoutError` currently reaches `except anthropic.APIConnectionError` **by inheritance, not by intent** — the adapter never names it — and is classified `PROVIDER_TRANSIENT`, so it is retried twice. Two things make that wrong: the SDK has *already* retried transient network faults internally (`max_retries=2`) before raising, and a request that genuinely needs longer than the ceiling will need longer on every attempt.

With Task 4 raising the timeout to 300s, this is what keeps one doomed call at 300s instead of 900s.

- [ ] **Step 1: Write the failing test**

Add to `tests/featuregen/intake/test_claude_timeout.py`:

```python
def test_a_timeout_is_non_retryable_not_transient(monkeypatch):
    """A timeout under a fixed ceiling is deterministic — retrying it spends budget proving that."""
    import anthropic

    class _Timeout:
        def create(self, **_kwargs):
            raise anthropic.APITimeoutError(request=None)

    llm = build_claude_llm(ClaudeConfig(enabled=True, model="m", max_tokens=4096))
    llm._client = type("C", (), {"messages": _Timeout()})()

    result = llm.call(_a_request())
    assert result.status == PROVIDER_NON_RETRYABLE
```

> `_a_request()` is whatever minimal `LLMRequest` builder the neighbouring tests in this file already use — reuse it rather than adding a second one.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/intake/test_claude_timeout.py -k non_retryable -v`
Expected: FAIL — `assert 'transient' == 'non_retryable'`

- [ ] **Step 3: Add the arm, BEFORE its parent class**

In `ClaudeLLM.call`, insert immediately above `except anthropic.APIConnectionError:`:

```python
        except anthropic.APITimeoutError:
            # NOT transient, and ordered BEFORE APIConnectionError because it is a subclass. The
            # SDK has already retried genuine network faults internally (max_retries=2) before
            # raising, so what reaches here is a request that needs longer than the configured
            # ceiling — deterministic, and re-attempting it identically spends the budget proving
            # it. Fail closed; the stage reports it and the operator raises FEATUREGEN_LLM_TIMEOUT
            # or switches this adapter to streaming.
            logger.warning("anthropic call timed out after %.0fs (task=%s) — non-retryable; "
                           "raise FEATUREGEN_LLM_TIMEOUT or stream",
                           self._config.timeout, request.task)
            return _fail(PROVIDER_NON_RETRYABLE)
```

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/featuregen/intake/ -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/intake/llm_claude.py tests/featuregen/intake/test_claude_timeout.py
git commit -m "fix(llm): classify a provider timeout as non-retryable, not transient"
```

---

### Task 4: Deployment configuration

**Files:**
- Modify: `deploy/kind/k8s/20-backend.yaml`

**Interfaces:**
- Consumes: nothing.
- Produces: `FEATUREGEN_FEATURE_CONTEXT=1` makes `_feature_schema_version()` return 4, which is what activates every payload change in Tasks 5–8.

- [ ] **Step 1: Set the three values**

In the `backend-config` ConfigMap, change `FEATUREGEN_FEATURE_CONTEXT` from `"0"` to `"1"` and add two new keys. Replace the existing line and its comment block:

```yaml
  # Richer feature-generation context (v4 payload). ON: feature generation receives the full
  # semantic dossier — concept, definition, domain, ai_summary, semantic_terms, party_role,
  # concept ancestry, identifier namespace, cross-catalog links — plus `semantic_authority`,
  # the {field: "producer/strength"} map that makes an LLM proposal legible AS a proposal.
  # Rollback lever: FEATUREGEN_FEATURE_CONTEXT_VERSION="3" drops to the shipped v3 payload
  # WITHOUT falling back to the thin v1 menu.
  FEATUREGEN_FEATURE_CONTEXT: "1"
  # Per-stage physical-provider-call ceiling for batched enrichment. The 32 default is exactly
  # break-even at 126 columns (summary/synonyms both run over EVERY column at 8 per chunk, so a
  # 256-column catalog exhausts it with zero retries). 100 covers ~500 columns with headroom for
  # the degradation ladder while still tripping on a runaway loop.
  OVERLAY_ENRICH_MAX_PROVIDER_CALLS: "100"
  # Per-CALL wall-clock ceiling, seconds. The 60s default is mismatched with
  # FEATUREGEN_LLM_MAX_TOKENS=32000 + adaptive thinking at effort=high: a slow call dies at 60s,
  # is classified transient, and is retried twice identically — three billings, no result. 300s
  # covers the heaviest task (feature generation), not just enrichment.
  FEATUREGEN_LLM_TIMEOUT: "300"
```

- [ ] **Step 2: Verify the YAML parses and the keys are present**

Run:
```bash
python -c "import yaml,sys; d=[x for x in yaml.safe_load_all(open('deploy/kind/k8s/20-backend.yaml')) if x['kind']=='ConfigMap'][0]['data']; print({k:d[k] for k in ('FEATUREGEN_FEATURE_CONTEXT','OVERLAY_ENRICH_MAX_PROVIDER_CALLS','FEATUREGEN_LLM_TIMEOUT')})"
```
Expected: `{'FEATUREGEN_FEATURE_CONTEXT': '1', 'OVERLAY_ENRICH_MAX_PROVIDER_CALLS': '100', 'FEATUREGEN_LLM_TIMEOUT': '300'}`

- [ ] **Step 3: Commit**

```bash
git add deploy/kind/k8s/20-backend.yaml
git commit -m "chore(deploy): enable v4 feature context; raise enrichment call ceiling and LLM timeout"
```

---

### Task 4b: Remove truncation from the LLM path — raise every cap, bound it offline

> **Live measurement is NOT authorised (human decision, 2026-08-06).** Steps 2 and 8 were before/after catalog ingests; both are now offline derivations. Do not run an upload, `deploy.sh`, `kubectl`, or anything that spends LLM budget anywhere in this task.

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` — `MAX_DEFINITION_LEN`, `_MAX_LEN_DEFAULT`, `_TABLE_ADVISORY_MAX_LEN`, `_FEATURE_STRUCTURAL_MAX_LEN`, `_FEATURE_COLLECTION_MAX_ITEMS`, `_MAX_COLUMN_PROFILES`, `_MAX_SOURCE_ATTRIBUTES`
- Modify: `src/featuregen/overlay/upload/enrich.py` — `_MAX_META_LEN`, `_MAX_SYNONYMS_LEN`, `_MAX_UNIT_LEN`
- Modify: `src/featuregen/overlay/upload/semantic_context.py` — `NEIGHBOUR_LIMIT`, `ADAPTER_LIST_LIMIT`
- Modify: `src/featuregen/overlay/upload/feature_assist.py` — `FEATURE_CONTEXT_BYTE_BUDGET`, `_MAX_SUGGESTION_LEN`
- Modify: `src/featuregen/overlay/upload/enrich_config.py` — `_DEFAULT_MAX_INPUT_TOKENS`
- Test: `tests/featuregen/overlay/upload/test_feature_context_budget.py`, `test_feature_context_egress.py`

**Interfaces:**
- Consumes: nothing.
- Produces: wider egress windows. No signature changes; Tasks 5–9 are structurally unaffected.

**Goal: nothing the platform knows is cut on the way to the model, and nothing the model returns is cut on the way back.**

**Raise, do not delete.** These constants are the egress control surface — `_FEATURE_STRUCTURAL_MAX_LEN` in particular is an *acceptance gate*, not a formatter: a value over it is EXCLUDED from egress and audited, not truncated. Removing bounds entirely means unbounded uploader-authored text leaving the system with nothing to catch a pathological file. A bound at ~10× the real worst case is functionally zero truncation and still catches a 5,000-column table or a 50 KB "definition".

**The interaction that decides the numbers.** More context per item ⟹ fewer items per chunk ⟹ more provider calls. `chunk_items` packs by BOTH item count and estimated tokens, so raising `NEIGHBOUR_LIMIT` multiplies every concept item and shatters the chunking. Measured baseline from `enrich_config.py`: a 40-entry resolved roster is ~971 tokens/item against a 24,000-token concept chunk budget. At `NEIGHBOUR_LIMIT=512` an item could exceed that budget alone, taking the concept stage from ~8 calls to ~72 for 144 columns. **Step 8 tests this offline against the real `chunk_items`, and Step 2 raises the ceiling so that even the degenerate outcome cannot truncate enrichment.**

- [ ] **Step 1: Measure the real data first**

Do not set a single constant from a guess. Against `CIB_Customer_Column_Mapping_final.csv`:

```bash
python3 - <<'PY'
import csv, collections
rows = list(csv.DictReader(open("CIB_Customer_Column_Mapping_final.csv")))
print("rows (catalog columns):", len(rows))
widest = {h: max(len(r.get(h) or "") for r in rows) for h in rows[0]}
for h, n in sorted(widest.items(), key=lambda kv: -kv[1]):
    print(f"{n:6d}  {h}")
tables = collections.Counter(r["schema.table.column"].rsplit(".", 1)[0] for r in rows)
print("\ncolumns per table:", tables.most_common(5))
PY
```

Record: the widest value per header, and the largest table's column count. **Every constant below is set to ≥ 2× the measured maximum**, so replace the suggested values with `2 × measured` wherever the measurement is larger.

- [ ] **Step 2: Derive the call-count ceiling instead of measuring it — NO LIVE RUN**

> **Decision (2026-08-06, human):** the before/after live ingests are NOT authorised. Do not run a catalog upload, do not spend LLM budget. Replace the measurement with a ceiling derived from the code's own constants and guarded by a test. Steps 2 and 8 are both offline.

The reason a measurement was wanted: raising the caps means more context per item, so `chunk_items` packs fewer items per chunk, so each stage makes more provider calls. `enrich_config.py:69-73` states the consequence exactly:

> *"an item that alone exceeds the token budget still forms its own chunk — nothing is ever dropped for size. So an under-budgeted bound degrades PROPORTIONALLY (fewer items per chunk → more provider calls), never into a lost item. The real backstop is `budget(short).max_provider_calls`."*

So the ceiling is the only thing that can turn "more calls" into **lost enrichment**. Crossing it does not slow the stage down; it stops enriching columns. Set it above any legitimate chunking outcome and let the wall clock be the operational bound.

**`max_provider_calls` is PER STAGE, not per upload.** `enrich_batch.py:236` builds one `CallLedger(b.max_provider_calls)` per `run_batched` invocation, and `budget(short)` is keyed by task. Verify this before trusting the arithmetic below.

Derive the worst case rather than guessing it. The degenerate case is one item per chunk, so for a column-scoped stage the chunk count equals the column count:

```
worst_case_calls = largest_catalog_items × max_batch_attempts + max_single_fallback
```

With the shipped `max_batch_attempts=2` and `max_single_fallback=8` (`enrich_config.py:146-148`), and the 237-column `wide_catalogs` fixture as the largest catalog this repo exercises:

```
237 × 2 + 8 = 482
```

Set the ceiling above that, in `deploy/kind/k8s/20-backend.yaml` (Task 4 shipped `"100"` — this supersedes it):

```yaml
# Raised from 100 (2026-08-06). PER STAGE, not per upload. This is a RUNAWAY BACKSTOP, not a
# throughput limiter: crossing it does not slow enrichment down, it silently stops enriching
# columns (enrich_config.py:69-73 — an over-budget item forms its own chunk, so a tight bound
# degrades into more calls, and the ceiling turns more calls into lost work). Derived from the
# degenerate one-item-per-chunk case over the largest catalog this repo exercises:
# 237 items x max_batch_attempts(2) + max_single_fallback(8) = 482. 512 clears it.
# The operational cost bound is now OVERLAY_ENRICH_STAGE_DEADLINE_S, not this number.
OVERLAY_ENRICH_MAX_PROVIDER_CALLS: "512"
```

Pin the derivation so a future cap change that outgrows it fails a test rather than silently truncating:

```python
def test_the_call_ceiling_cannot_bind_before_the_wall_clock_does():
    """The ceiling is a runaway backstop. If it can bind on a legitimate catalog, raising the
    caps silently STOPS ENRICHING COLUMNS rather than merely costing more calls."""
    b = enrich_config.budget("concept")
    worst_case = _LARGEST_CATALOG_ITEMS * b.max_batch_attempts + b.max_single_fallback
    assert b.max_provider_calls > worst_case, (
        f"{b.max_provider_calls} can bind at {worst_case} worst-case calls — enrichment would "
        f"truncate. Raise OVERLAY_ENRICH_MAX_PROVIDER_CALLS or lower the item caps.")
```

`_LARGEST_CATALOG_ITEMS = 237` with a comment naming `wide_catalogs` as its source. Read the ceiling through `enrich_config.budget(...)` under the deployed environment, not from a literal, so the test tracks the manifest.

**State the cost trade in your report.** 512 per stage across the enrichment stages is a much larger worst-case bill than 100 was, and the ceiling is no longer what stops a runaway — `OVERLAY_ENRICH_STAGE_DEADLINE_S` is. Say so plainly; do not present this as free.

- [ ] **Step 3: Raise the length caps**

`enrich_llm.py`:

```python
_TABLE_ADVISORY_MAX_LEN = 2000        # was 400
_FEATURE_STRUCTURAL_MAX_LEN = 1000    # was 200 — ACCEPTANCE gate: values 201–1000 chars are now
                                      # ADMITTED to egress where they were previously excluded.
                                      # Deliberate widening of the egress surface; redaction
                                      # (_redact_free_text_meta / sanitize_feature_context) still
                                      # runs first, so this is a size decision, not a PII one.
MAX_DEFINITION_LEN = 4000             # was 600 — a full bank definition is never clipped
_MAX_LEN_DEFAULT = 1000               # was 200 — the per-value egress cap for every other scalar
```

`enrich.py`:

```python
_MAX_META_LEN = 1000                  # was 200
_MAX_SYNONYMS_LEN = 1000              # was 200
_MAX_UNIT_LEN = 64                    # was 32
```

`feature_assist.py`:

```python
_MAX_SUGGESTION_LEN = 256             # was 64 — an ACCEPTED value, i.e. OUTBOUND truncation
```

- [ ] **Step 4: Raise the item caps that are dropping content today**

`_MAX_COLUMN_PROFILES = 64` is the one biting now: `BO_CIB_CUSTOMER` has 144 columns, so Pass B decides the table's **grain**, **table_role**, **primary_entity** and **event_or_snapshot** while seeing 64 of them.

`enrich_llm.py`:

```python
_FEATURE_COLLECTION_MAX_ITEMS = 256   # was 40
_MAX_COLUMN_PROFILES = 512            # was 64 — Pass B now sees every column of a real table
_MAX_SOURCE_ATTRIBUTES = 256          # was 40
```

`semantic_context.py`:

```python
ADAPTER_LIST_LIMIT = 256              # was 40
NEIGHBOUR_LIMIT = 512                 # was 64 — see Step 5, this one has a call-count cost
```

- [ ] **Step 5: Raise the chunk token budgets so the bigger items do not shatter the chunking**

A bigger item with an unchanged token budget means fewer items per chunk and proportionally more calls. Raise the per-task input budgets in `enrich_config.py` in step with the item growth:

```python
_DEFAULT_MAX_INPUT_TOKENS = {"concept": 200_000, "definition": 60_000, "domain": 200_000,
                             "synonyms": 60_000, "unit": 60_000, "summary": 100_000,
                             "table_synth": 60_000}
```

These sit far below Opus's 1M-token window; the binding constraint is cost per call, not the model. **Do not raise `_DEFAULT_MAX_ITEMS`** — those are cross-item CONTAMINATION boundaries (MF-8a), not size limits, and trading them for bytes would swap an accuracy control for a budget.

- [ ] **Step 6: Raise the assembly byte budget, and re-measure**

`feature_assist.py` — replace the constant and its measurement comment together; the file's convention is that this comment carries measurements, not estimates:

```python
# CAP INCREASE (2026-08-06): every prose and item cap raised for zero-truncation. The previously
# measured v4 figure was ~1_048 bytes/column (237 columns -> 248_601) at the old caps. Re-measure
# with `test_feature_context_budget.py` after this change and REPLACE this number; the budget below
# is set to hold the largest real catalog at the new caps with ~40% headroom.
FEATURE_CONTEXT_BYTE_BUDGET = 1_500_000
```

**You will break `test_measured_mandatory_bytes_for_v3_and_v4` — that is the point, and you must re-pin it, not delete it.** That test measures the 237-column `wide_catalogs` fixture and pins BOTH versions to tolerance ranges taken at the OLD caps:

```python
    assert 150_000 < v3 < 200_000, f"v3 mandatory bytes moved: {v3}"
    assert 215_000 < v4 < 285_000, f"v4 mandatory bytes moved: {v4}"
```

Raising `MAX_DEFINITION_LEN` 600 → 4000 (and the rest) moves both numbers well above their upper bounds. The ranges exist so a payload change has to come back and re-argue the budget — this task IS that change.

Do it in this order, and do not guess the numbers:

1. Apply Steps 1–5 and the constant above.
2. Run `uv run pytest tests/featuregen/overlay/upload/test_feature_context_budget.py::test_measured_mandatory_bytes_for_v3_and_v4 -v` and read the two failure messages — they print the real measured `v3` and `v4`.
3. Re-pin both ranges around the MEASURED values at ±15%, keeping the same message format.
4. Put the measured v4 figure into the comment above `FEATURE_CONTEXT_BYTE_BUDGET`, replacing `~1_048 bytes/column (237 columns -> 248_601)`. The comment must state what was measured, not what was expected.
5. Confirm `assert v4 < FEATURE_CONTEXT_BYTE_BUDGET` (already in the test) still holds, and add the headroom assertion below it.

Add this to the same test, using the file's real helper — `_mandatory_bytes(conn, version, monkeypatch)`. **There is no `_build_v4_columns` helper in this file; do not invent one.** The fixture is `wide_catalogs` and the catalog is 237 columns, not 144:

```python
def test_the_raised_caps_leave_headroom_on_the_worst_realistic_catalog(wide_catalogs, monkeypatch):
    """237 mandatory columns at the zero-truncation caps must still clear the budget with room.

    Headroom is the point: the budget is not a target to fill. If this fails, the caps grew faster
    than the budget and one of the two is wrong — decide which, do not just raise the budget."""
    v4 = _mandatory_bytes(wide_catalogs, 4, monkeypatch)
    assert v4 < FEATURE_CONTEXT_BYTE_BUDGET * 0.6, (
        f"{v4} bytes leaves under 40% headroom against {FEATURE_CONTEXT_BYTE_BUDGET}")
```

- [ ] **Step 7: Run the suites — the egress one is the one that matters**

Run: `uv run pytest tests/featuregen/overlay/upload/test_feature_context_budget.py tests/featuregen/overlay/upload/test_feature_context_egress.py tests/featuregen/overlay/upload/test_enrich_egress.py -v`

Expected: PASS. The egress suites pin what is admitted to leave the system; `_FEATURE_STRUCTURAL_MAX_LEN` is one of their gates and this task moves it. If an egress test fails, **do not relax the test** — the cap change has admitted a shape the guard was built to refuse, and that is the finding.

- [ ] **Step 8: Prove offline that the chunking did not shatter — NO LIVE RUN**

> Same decision as Step 2: no ingest, no LLM spend. Replace the re-measurement with a chunking assertion that runs against the real `chunk_items` and the real caps.

The risk Step 8 existed to catch is `NEIGHBOUR_LIMIT=512` multiplying every concept item until items no longer share a chunk. That is measurable without a provider: build items at the NEW caps, run the REAL `chunk_items` against the REAL token budget, and assert the packing did not collapse to one item per chunk.

```python
def test_the_concept_stage_still_packs_multiple_items_per_chunk_at_the_new_caps():
    """The cap raise must degrade packing PROPORTIONALLY, not shatter it. One item per chunk
    means the concept stage's call count equals its column count — the shape that made the old
    ceiling bind."""
    items = [_a_concept_item_at_the_new_caps(i) for i in range(60)]
    chunks = chunk_items(items, short="concept")
    assert len(chunks) < len(items), "every item formed its own chunk — packing collapsed"
    assert max(len(c) for c in chunks) > 1
```

Build `_a_concept_item_at_the_new_caps` from the same assembly the stage uses, at a fully-populated `NEIGHBOUR_LIMIT` roster — an item smaller than production would make this test pass vacuously. If packing HAS collapsed, that is the real finding: lower `NEIGHBOUR_LIMIT` to the largest real table's column count (from Step 1) rather than 512. Report the measured items-per-chunk either way.

**What this does NOT prove, and must be said so in the report:** the true per-stage call count against a real catalog is still unmeasured. Steps 2 and 8 now bound the failure mode (the ceiling cannot bind; packing has not collapsed) without observing the actual number. A live before/after remains the only way to know it, and it is deferred by explicit human decision — record that in `docs/DEFERRED-WORK.md` so it is not mistaken for verified.

- [ ] **Step 9: Commit, with the measurements in the message**

```bash
git add src/featuregen/overlay/upload/enrich_llm.py src/featuregen/overlay/upload/enrich.py \
        src/featuregen/overlay/upload/semantic_context.py \
        src/featuregen/overlay/upload/feature_assist.py \
        src/featuregen/overlay/upload/enrich_config.py \
        tests/featuregen/overlay/upload/test_feature_context_budget.py
git commit -m "feat(egress): raise every prose and item cap for zero-truncation LLM context

Ceiling derived (not measured): 237 items x 2 attempts + 8 fallbacks = 482 worst case, ceiling 512.
Packing at the new caps: <items-per-chunk from Step 8>. Assembled bytes: <from Step 6>.
A live before/after call count is DEFERRED by human decision - see DEFERRED-WORK."
```

---

### Task 4c: Richer synonyms — the cheapest quality win in the plan

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich.py` (`_SYN_INSTRUCTION`, and the synonyms item payload if it omits the definition)
- Test: `tests/featuregen/overlay/upload/test_enrich_synonyms.py` (or the nearest existing synonyms test)

**Interfaces:** none changed. Same stage, same call, richer answer.

**Depends on Task 4b** — `_MAX_SYNONYMS_LEN` must already be 1000, or a longer answer is truncated on arrival.

Synonyms are the **only semantic handle an unclassified column has**. It has no concept to be found by, so its aliases are the entire basis on which anyone can search for it. Today's instruction undermines that twice:

```python
_SYN_INSTRUCTION = ("List the business SYNONYMS and common aliases for EACH column — the other names "
                    "a business user would search for it by. Return ONE comma-separated line per "
                    "item, terms only, no explanation. Treat each item independently: use only that "
                    "item's table/column/type/concept; return exactly one result per input ref.")
```

1. **No count is asked for**, so the model returns whatever it feels like — typically three or four.
2. **It is told to ignore the definition.** For your row it must generate aliases from `create_user_nm` and `varchar(100)` alone, while forbidden from reading the bank's own sentence describing the column. That definition is the single richest source of aliases available.

- [ ] **Step 1: Check what the item payload actually carries**

```bash
uv run python -c "
from featuregen.overlay.upload.enrich import _SYN_INSTRUCTION
print(_SYN_INSTRUCTION)"
```

then read the synonyms `BatchItem` construction in `draft_synonyms` and record whether the per-item metadata includes the business definition. If it does, Step 3 is instruction-only. If it does not, Step 3 must also add it — the instruction cannot use evidence the payload never sends.

- [ ] **Step 2: Write the failing test**

```python
def test_the_synonym_instruction_asks_for_a_count_and_permits_the_definition():
    assert "15" in _SYN_INSTRUCTION and "20" in _SYN_INSTRUCTION
    assert "use only that item's table/column/type/concept" not in _SYN_INSTRUCTION
    assert "definition" in _SYN_INSTRUCTION.lower()


def test_an_unclassified_column_still_gets_synonyms(db, unclassified_column):
    """Synonyms are the ONLY search handle an unclassified column has — never skip it."""
    out = draft_synonyms(db, [unclassified_column.row], _client_returning_terms(
        "audit user, created by, record creator, entry user, data steward"),
        actor=_ACTOR, concepts={unclassified_column.hash: UNCLASSIFIED})
    assert out[unclassified_column.hash].count(",") >= 4
```

- [ ] **Step 3: Rewrite the instruction**

```python
_SYN_INSTRUCTION = (
    "List the business SYNONYMS and common aliases for EACH column — the other names a business "
    "user would search for it by. Give 15 to 20 terms where the evidence supports them; give fewer "
    "only when the column is genuinely narrow. Use the item's table, column name, type, concept AND "
    "its business definition — the definition is usually the richest source of aliases, so read it. "
    "If the concept is the literal value 'unclassified', ignore it: it is the absence of a "
    "classification, not a hint. Return ONE comma-separated line per item, terms only, no "
    "explanation. Treat each item independently; return exactly one result per input ref.")
```

If Step 1 found the definition absent from the item payload, add it there too — bounded by `MAX_DEFINITION_LEN`, the same window every other stage uses.

- [ ] **Step 4: Run the tests**

Run: `uv run pytest tests/featuregen/overlay/upload/ -k synonym -v`
Expected: PASS. The egress suite must stay green — the definition is already an approved egress field for other stages, so this widens which stage sends it, not what may be sent.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/enrich.py tests/featuregen/overlay/upload/
git commit -m "feat(enrich): ask for 15-20 synonyms and let the drafter read the definition"
```

---

### Task 5: Carry the five leaked fields in the semantic bundle

**Files:**
- Modify: `src/featuregen/overlay/upload/semantic_context.py`
- Test: `tests/featuregen/overlay/upload/test_semantic_context.py`

**Interfaces:**
- Consumes: `graph_node` columns `sub_domain`, `bian_path`, `process_path` (column nodes); `table_role`, `event_or_snapshot` (table nodes).
- Produces: `bundle.resolved_semantics` gains `SemanticValueV1` entries named `sub_domain`, `bian_path`, `process_path`; `bundle.table_context` gains `table_role` and `event_or_snapshot`. Task 6 reads these by exactly those `field_name` strings.

**Verified names — do not guess these:** the bundle's table field is `table_context` (a `tuple[SemanticValueV1, ...]`), NOT `table_semantics`. The column field is `resolved_semantics`. Both are declared on `SemanticContextBundleV1` at `semantic_context.py:741-758`.

**Test fixture convention:** DB-backed tests in this file take a `db` fixture — see `test_emitted_missing_context_codes_are_closed(db)` at line 189 and mirror its setup exactly. Most other tests in the file are pure and take no fixture. The two tests below are written against a helper that seeds one visible column plus its table node; if no such helper exists, build it from the `db` fixture following the line-189 test, and adjust the parameter names in the two tests to match.

- [ ] **Step 1: Write the failing test**

Add to `tests/featuregen/overlay/upload/test_semantic_context.py`:

```python
def test_bundle_carries_the_column_classification_axes(conn, seeded_column):
    """sub_domain / bian_path / process_path are on graph_node and must ride the bundle."""
    conn.execute(
        "UPDATE graph_node SET sub_domain = %s, bian_path = %s, process_path = %s "
        "WHERE catalog_source = %s AND lower(object_ref) = %s",
        ("Sanctions Screening", "BIAN>Party>Reference", "Onboarding>KYC",
         seeded_column.source, seeded_column.flat_ref))
    bundle = bundle_from_store(conn, seeded_column.source, seeded_column.object_ref, roles=())
    got = {v.field_name: v.value for v in bundle.resolved_semantics}
    assert got["sub_domain"] == "Sanctions Screening"
    assert got["bian_path"] == "BIAN>Party>Reference"
    assert got["process_path"] == "Onboarding>KYC"


def test_bundle_carries_the_table_classification_axes(conn, seeded_column):
    conn.execute(
        "UPDATE graph_node SET table_role = %s, event_or_snapshot = %s "
        "WHERE catalog_source = %s AND kind = 'table' AND table_name = %s",
        ("fact", "event", seeded_column.source, seeded_column.table))
    bundle = bundle_from_store(conn, seeded_column.source, seeded_column.object_ref, roles=())
    got = {v.field_name: v.value for v in bundle.table_context}
    assert got["table_role"] == "fact"
    assert got["event_or_snapshot"] == "event"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_semantic_context.py -k classification_axes -v`
Expected: FAIL with `KeyError: 'sub_domain'`

- [ ] **Step 3: Widen the anchor query**

In `bundle_from_store`, extend the column SELECT and its unpack. Replace:

```python
    anchor = conn.execute(
        "SELECT object_ref, schema_name, table_name, column_name, data_type, declared_type, "
        "definition, domain, concept, semantic_terms, ai_summary, additivity, unit, currency, "
        "entity, is_grain, is_as_of, party_role, grain_fact_event_id, availability_fact_event_id "
```

with:

```python
    anchor = conn.execute(
        "SELECT object_ref, schema_name, table_name, column_name, data_type, declared_type, "
        "definition, domain, concept, semantic_terms, ai_summary, additivity, unit, currency, "
        "entity, is_grain, is_as_of, party_role, grain_fact_event_id, availability_fact_event_id, "
        "sub_domain, bian_path, process_path "
```

and extend the unpack tuple:

```python
    (_ref, schema_name, table_name, column_name, data_type, declared_type, definition, domain,
     concept_name, semantic_terms, ai_summary, additivity, unit, currency, entity, is_grain,
     is_as_of, party_role, grain_event, availability_event,
     sub_domain, bian_path, process_path) = anchor
```

- [ ] **Step 4: Add the three names to the display field list**

Replace:

```python
_DISPLAY_FIELDS = ("ai_summary", "concept", "definition", "domain", "party_role",
                   "semantic_terms")
```

with:

```python
# `bian_path` / `process_path` are the SOURCE's own taxonomy paths and `sub_domain` is the LLM's
# finer axis beside `domain` — all three are display/recommendation tier (field_policies `_MEANING`
# / `_GLOSSARY_TERM`), so they ride the same list as `domain` itself.
_DISPLAY_FIELDS = ("ai_summary", "bian_path", "concept", "definition", "domain", "party_role",
                   "process_path", "semantic_terms", "sub_domain")
```

Then add the three values to the `display` mapping the loop reads, alongside the existing entries.

- [ ] **Step 5: Widen the table query and its value loop**

Replace:

```python
    table_row = conn.execute(
        "SELECT definition, domain, semantic_terms, ai_summary FROM graph_node "
```

with:

```python
    table_row = conn.execute(
        "SELECT definition, domain, semantic_terms, ai_summary, table_role, event_or_snapshot "
        "FROM graph_node "
```

and replace the unpack + loop:

```python
        (t_definition, t_domain, t_semantic_terms, t_ai_summary,
         t_table_role, t_event_or_snapshot) = table_row
        for field_name, raw in (("ai_summary", t_ai_summary), ("definition", t_definition),
                                ("domain", t_domain), ("semantic_terms", t_semantic_terms),
                                ("table_role", t_table_role),
                                ("event_or_snapshot", t_event_or_snapshot)):
```

- [ ] **Step 6: Run the tests**

Run: `uv run pytest tests/featuregen/overlay/upload/test_semantic_context.py -v`
Expected: PASS, including the two new tests and every pre-existing one.

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/overlay/upload/semantic_context.py tests/featuregen/overlay/upload/test_semantic_context.py
git commit -m "feat(context): carry sub_domain, bian_path, process_path, table_role, event_or_snapshot in the bundle"
```

---

### Task 6: Emit them in the feature-generation payload

**Files:**
- Modify: `src/featuregen/overlay/upload/semantic_context.py:1792` (`for_feature_generation`)
- Test: `tests/featuregen/overlay/upload/test_feature_context_v4.py`

**Interfaces:**
- Consumes: the `field_name` strings Task 2 produces.
- Produces: `for_feature_generation(bundle)` returns the additional keys `sub_domain`, `bian_path`, `process_path`, `table_role`, `event_or_snapshot`. Task 7's coverage test asserts against this key set.

- [ ] **Step 1: Write the failing test**

Add to `tests/featuregen/overlay/upload/test_feature_context_v4.py`:

```python
def test_feature_payload_carries_the_classification_axes(conn, seeded_column):
    conn.execute(
        "UPDATE graph_node SET sub_domain = %s, bian_path = %s, process_path = %s "
        "WHERE catalog_source = %s AND lower(object_ref) = %s",
        ("Sanctions Screening", "BIAN>Party>Reference", "Onboarding>KYC",
         seeded_column.source, seeded_column.flat_ref))
    conn.execute(
        "UPDATE graph_node SET table_role = %s, event_or_snapshot = %s "
        "WHERE catalog_source = %s AND kind = 'table' AND table_name = %s",
        ("fact", "event", seeded_column.source, seeded_column.table))
    bundle = bundle_from_store(conn, seeded_column.source, seeded_column.object_ref, roles=())
    payload = for_feature_generation(bundle)
    assert payload["sub_domain"] == "Sanctions Screening"
    assert payload["bian_path"] == "BIAN>Party>Reference"
    assert payload["process_path"] == "Onboarding>KYC"
    assert payload["table_role"] == "fact"
    assert payload["event_or_snapshot"] == "event"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_feature_context_v4.py -k classification_axes -v`
Expected: FAIL with `KeyError: 'sub_domain'`

- [ ] **Step 3: Add the keys**

In `for_feature_generation`, after the existing `out["party_role"] = _value_of(bundle, "party_role")` line, insert:

```python
    # D13.1/D13.2 axes. `sub_domain` refines `domain`; `bian_path` / `process_path` are the
    # SOURCE's own taxonomy, which the generator should weigh above anything the model inferred.
    out["sub_domain"] = _value_of(bundle, "sub_domain")
    out["bian_path"] = _value_of(bundle, "bian_path")
    out["process_path"] = _value_of(bundle, "process_path")
    # Table SHAPE. A windowed count is right on an event log and wrong on a snapshot; the
    # generator cannot make that call without being told which it is.
    out["table_role"] = _table_value(bundle, "table_role")
    out["event_or_snapshot"] = _table_value(bundle, "event_or_snapshot")
```

Then replace the relationships block to carry **cardinality** — the answer to "does this join fan out?", which is what separates a correct aggregate from a silently multiplied one. `None` is meaningful: it means nobody has established the cardinality, and `plan_join` will refuse the hop later, so the generator should treat it as a reason to avoid the link or flag the feature:

```python
    out["relationships"] = [
        {"relationship_ref": link.relationship_ref, "kind": link.kind,
         "availability": link.availability, "review_status": link.review_status,
         "cardinality": link.cardinality}          # None == UNKNOWN, and that IS the signal
        for link in bundle.relationship_context[:ADAPTER_LIST_LIMIT]
    ]
```

- [ ] **Step 4: Carry the LLM's proposed unit and currency**

`unit` and `currency` already ride the `{value, authority}` wrapper, but `value` comes from the operational read model — which `_MEASURE_ANNOTATION` bars the LLM from ever winning. So today the payload emits `{"value": null, "authority": "hint"}` while `semantic_authority` says `unit: "llm/proposed"` — it announces a proposal it does not include.

**Do not touch `_MEASURE_ANNOTATION` or `graph_node.unit`.** `_column_meta` clears `UNIT_CONSISTENT` from `graph_node.unit` alone, so a projected LLM value would clear a safety check on a guess. That line stays exactly where it is. Add a *separate* key instead, which nothing reading `.value` can confuse:

First, carry the proposed value on the bundle. `_bulk_active_evidence` already returns it (`values[(logical_ref, field_name, producer)]`) and only `source_values` uses it. Add a field to `SemanticValueV1` in `contracts/evidence_axes.py`:

```python
    proposed_value: object | None = None   # the LLM's value where it did not win resolution
```

Populate it in the `_OPERATIONAL_FIELDS` loop of `bundle_from_store`:

```python
        llm_proposed = evidence_values.get((logical_ref, field_name, EvidenceProducer.LLM.value))
        resolved_values.append(SemanticValueV1(
            field_name=field_name, value=value, evidence=entries,
            resolution_status="current" if value is not None else UNRESOLVED_PENDING_REVIEW,
            operational_influence=influence, proposed_value=llm_proposed))
```

Then emit it in `for_feature_generation`'s fact-key loop:

```python
    for fact_key in ("data_type", "declared_type", "entity", "additivity", "unit", "currency",
                     "is_grain", "is_as_of"):
        got = resolved.get(fact_key)
        value = _render(got.value) if got is not None else None
        authority = got.operational_influence if got is not None else "hint"
        entry = {"value": value, "authority": authority or "hint"}
        # The LLM's answer where it did not win resolution (unit/currency under
        # `_MEASURE_ANNOTATION`). A SEPARATE key: `value` still means "operationally resolved",
        # so no existing consumer changes behaviour and no safety check can be cleared by it.
        if got is not None and got.proposed_value is not None and value is None:
            entry["proposed_value"] = _render(got.proposed_value)
        out[fact_key] = entry
```

Add to the Step-1 test:

```python
    assert payload["relationships"][0]["cardinality"] == "N:1"
    assert payload["unit"] == {"value": None, "authority": "hint", "proposed_value": "AED"}
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/featuregen/overlay/upload/test_feature_context_v4.py tests/featuregen/overlay/upload/test_feature_context_egress.py -v`
Expected: PASS. The egress suite must stay green — every new key is a bounded scalar already classified by the D10 rules the adapter applies.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/semantic_context.py tests/featuregen/overlay/upload/test_feature_context_v4.py
git commit -m "feat(feature-gen): emit the column and table classification axes in the v4 payload"
```

---

### Task 6b: Join the adjudication signals at the payload layer

**Files:**
- Modify: `src/featuregen/overlay/upload/feature_assist.py` (`_context_v4_column`)
- Test: `tests/featuregen/overlay/upload/test_feature_context_v4.py`

**Interfaces:**
- Consumes: the adjudication current-pointer read `asset_detail.py` already uses.
- Produces: two additional keys on a v4 column payload — `confidence_band` and `concept_alternatives`.

**Why these do NOT go in the bundle.** `semantic_context.py`'s own module docstring draws the line:

> *"the bundle carries no adjudication/critic result projection… the adjudication is an LLM **JUDGEMENT ABOUT** the semantics, not one of them, so it is served **beside** the bundle."*

That is a deliberate boundary and this plan keeps it. `_context_v4_column` is the right home: it already has `conn`, and it already joins a non-bundle signal (`semantic_authority`) onto the payload. Two more of the same shape.

The adjudicator produces both signals on every column it reviews, and today neither reaches the model:

- **`confidence_band`** (`high|medium|low`) — *"nothing in this module or downstream branches on it."* Stored, displayed, never sent. For a generator asked to weigh proposals, this is the weighing input.
- **`alternatives`** — up to three registry concepts it seriously considered. *"probably `bic_code`, possibly `institution_id`"* is far more useful than a bare `unclassified`.

- [ ] **Step 1: Write the failing test**

```python
def test_the_v4_payload_carries_the_adjudication_signals(db, adjudicated_column):
    payload = _context_v4_column(db, adjudicated_column.candidate_row, roles=())
    assert payload["confidence_band"] == "medium"
    assert payload["concept_alternatives"] == ["bic_code", "institution_id"]


def test_a_column_never_adjudicated_carries_neither_key(db, plain_column):
    """Absence is the honest signal — `_context_v4_column` already drops empty values."""
    payload = _context_v4_column(db, plain_column.candidate_row, roles=())
    assert "confidence_band" not in payload
    assert "concept_alternatives" not in payload
```

- [ ] **Step 2: Run — expect FAIL** (`KeyError: 'confidence_band'`)

- [ ] **Step 3: Join them beside `semantic_authority`**

In `_context_v4_column`, immediately after the existing `out["semantic_authority"] = _semantic_authority(bundle)`:

```python
    # The adjudicator's JUDGEMENT signals. Deliberately joined HERE and not in the bundle —
    # semantic_context draws that line explicitly and this keeps it. Both are advisory: nothing
    # branches on `confidence_band`, and the alternatives are context for the model, never a
    # classification. `_context_v4_column` already strips empty values, so a column that was never
    # adjudicated simply carries neither key.
    #
    # READ IT BY `bundle.object_ref`, NOT `c["object_ref"]`. The adjudication subject pointer is
    # keyed by the SCHEMA-PRESERVING logical ref, which is what the bundle carries; `c` and the
    # emitted payload carry the PUBLIC-FLATTENED graph ref (see the note on `out["object_ref"]`
    # above). Passing the flattened form matches no pointer row, so every column would silently
    # come back unadjudicated — the keys would simply never appear and the feature would look
    # implemented. Read this BEFORE the `out["object_ref"]` overwrite line, or from `bundle`.
    adj, _result_id = load_current_adjudication(conn, bundle.object_ref)
    if adj is not None:
        out["confidence_band"] = adj.confidence_band
        out["concept_alternatives"] = list(adj.alternatives)
```

Import at the top of the function body, matching how `asset_detail.py:692` does it (deferred import, to keep the module-import cycle it already avoids):

```python
    from featuregen.overlay.upload.semantic_adjudication import load_current_adjudication
```

**The exact API — verified, do not substitute from memory:**

```python
def load_current_adjudication(conn, logical_ref: str) -> tuple[SemanticAdjudicationV2 | None, str | None]
```

It takes ONE ref (not `catalog_source` + `object_ref`) and returns a **2-tuple** of `(adjudication, structured_result_id)`. `SemanticAdjudicationV2` carries `selected_concept`, `alternatives: tuple[str, ...]`, `confidence_band: str`, `reason_codes`, `missing_context`, `ontology_gap`.

This is `asset_detail.py`'s existing read — the one that already resolves the migration-1046 current pointer. Call it; do not write a second query.

- [ ] **Step 4: Run the tests — expect PASS**

Run: `uv run pytest tests/featuregen/overlay/upload/test_feature_context_v4.py tests/featuregen/overlay/upload/test_feature_context_budget.py -v`

The budget suite matters: this adds a short string and a ≤3-item list per adjudicated column, and only unclear columns are adjudicated — but confirm rather than assume.

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/feature_assist.py tests/featuregen/overlay/upload/test_feature_context_v4.py
git commit -m "feat(feature-gen): carry adjudication confidence and alternatives on the v4 payload"
```

---

### Task 6c: Make the generator show its grounding

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` (the feature-generation output schema)
- Modify: `src/featuregen/overlay/upload/feature_assist.py` (the instruction)
- Test: `tests/featuregen/overlay/upload/test_feature_assist.py`

**Interfaces:**
- Produces: each returned feature idea carries a `grounding` array of `{column, role, why}`.

This is an **output** change, not a payload change — the one LLM call behind `POST /features/recommend` returns more, rather than receiving more.

**Why this rather than a reviewer call.** The alternative — an LLM pass that filters the menu before generation — is the wrong shape: a false drop is unrecoverable (the generator never learns the column existed) while a false include is nearly free (the model ignores it). Worse, `_is_mandatory` puts the confirmed grain and as-of columns in the menu *because the calculation is wrong without them*, not because they match the objective — so a reviewer asked "which columns are about exposure?" would reasonably delete the uniqueness key. Making the generator explain itself costs nothing and audits the thing that matters: the feature, not the menu.

**Register the schema version before enabling it.** `_require_schema` (D10) refuses to dispatch a `(schema_id, version)` pair it cannot resolve, so the new output shape needs a registered body at the bumped version. A version returned without a registered body is a loud failure at dispatch — which is the intended behaviour, not something to work around.

- [ ] **Step 1: Write the failing test**

```python
def test_each_generated_feature_carries_its_grounding():
    report = recommend_features_report(
        conn, "total counterparty exposure by customer", _client_returning_grounded_idea(),
        actor=_ACTOR, roles=())
    idea = report["feature_ideas"][0]
    assert idea["grounding"] == [
        {"column": "LIMIT_AMT", "role": "measure",  "why": "monetary, concept credit_limit_amount"},
        {"column": "CUST_ID",   "role": "grain",    "why": "confirmed grain column"},
    ]


def test_a_grounding_entry_naming_an_unoffered_column_is_refused():
    """The existing grounding check validates the refs; this proves the new array is covered too."""
    report = recommend_features_report(
        conn, "…", _client_returning_grounding_for("NOT_IN_THE_MENU"), actor=_ACTOR, roles=())
    assert report["feature_ideas"] == []
```

- [ ] **Step 2: Run — expect FAIL** (`KeyError: 'grounding'`)

- [ ] **Step 3: Extend the output schema at a new version**

Add `grounding` to the feature-generation schema body in `enrich_llm._SCHEMAS`, registered at the bumped `schema_version`:

```python
    "grounding": {
        "type": "array",
        "maxItems": 32,
        "items": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "column": {"type": "string", "maxLength": 200},
                "role":   {"type": "string",
                           "enum": ["measure", "grain", "time_anchor", "filter",
                                    "currency", "dimension"]},
                "why":    {"type": "string", "maxLength": 200},
            },
            "required": ["column", "role", "why"],
        },
    },
```

`role` is a closed enum deliberately: free text there would become an ungoverned second vocabulary, and the six values cover every way a column can enter a feature.

- [ ] **Step 4: Ask for it in the instruction**

```
"For EVERY feature you propose, return a `grounding` entry per column you used: the column, its "
"role, and one short clause naming the evidence you relied on. Where a value you relied on is "
"marked llm/proposed in `semantic_authority`, or a fact carries `proposed_value` rather than "
"`value`, say so — a feature resting on an unconfirmed semantic is still worth proposing, and the "
"reader needs to know which one it is."
```

- [ ] **Step 5: Extend the existing grounding check to the new array**

Every `grounding[].column` must be in the offered candidate set, exactly as the returned refs already are. A feature whose grounding names a column that was never offered is dropped by the same rule — otherwise the array becomes a channel for ungrounded refs to re-enter.

- [ ] **Step 6: Run the tests — expect PASS**

Run: `uv run pytest tests/featuregen/overlay/upload/test_feature_assist.py tests/featuregen/overlay/upload/test_feature_assist_hitl.py -v`

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_llm.py src/featuregen/overlay/upload/feature_assist.py \
        tests/featuregen/overlay/upload/test_feature_assist.py
git commit -m "feat(feature-gen): every proposed feature returns the grounding it rests on"
```

---

### Task 6d: Expand the question, not the corpus

**Files:**
- Modify: `src/featuregen/overlay/upload/feature_assist.py` (`_objective_tokens`)
- Modify: `src/featuregen/overlay/upload/enrich_llm.py` (register the expansion schema)
- Test: `tests/featuregen/overlay/upload/test_feature_assist.py`

**Interfaces:**
- Produces: `_objective_tokens` returns the tokenised objective **plus** LLM-derived related terms.

Column selection today is a set intersection:

```python
key=lambda c: (-len(_column_tokens(c) & obj_tokens), c["object_ref"])
```

Word-for-word. Ask for *"counterparty exposure"*, and a column whose synonyms say *"obligor limit"* shares no words and scores zero — despite meaning the same thing.

**Why expand the question and not the columns.** The question is ten words; the catalog is 144 columns. Expanding the small side is one tiny call instead of a large one, it is cacheable (the same objective expands identically every time), and the match stays a deterministic set intersection afterwards — so a reviewer can still be told *"matched on `obligor`, which we derived from your word `counterparty`."* Task 4c widens the columns from the other end; the two meet in the middle.

**This is not a substitute for the hierarchy.** Only 52 of 324 concepts declare an `is_a`, so ancestry expansion would fire on one concept in six (Task 9b fixes that). Query expansion works regardless of how flat the vocabulary is.

- [ ] **Step 1: Write the failing test**

```python
def test_the_objective_is_expanded_with_related_business_terms():
    toks = _objective_tokens("total counterparty exposure by customer", entity=None, scope=None,
                             conn=_conn, client=_client_returning(["obligor", "limit", "facility"]))
    assert {"counterparty", "exposure", "customer"} <= toks    # the literal words survive
    assert {"obligor", "limit", "facility"} <= toks            # and the derived ones join them


def test_expansion_is_replayed_for_an_identical_objective(db):
    """The same question must not re-bill. Second call issues no provider request."""
    calls = _counting_client(["obligor"])
    _objective_tokens("total counterparty exposure", None, None, conn=db, client=calls)
    _objective_tokens("total counterparty exposure", None, None, conn=db, client=calls)
    assert calls.count == 1


def test_no_client_degrades_to_literal_tokens(db):
    """Expansion is advisory: with no provider the search behaves exactly as it does today."""
    toks = _objective_tokens("counterparty exposure", None, None, conn=db, client=None)
    assert toks == {"counterparty", "exposure"}
```

- [ ] **Step 2: Run — expect FAIL** (`_objective_tokens` takes no `conn`/`client`)

- [ ] **Step 3: Add the expansion**

Register a small output schema (`{"terms": [...]}`, `maxItems: 40`, each `maxLength: 64`) and dispatch through `audited_structured_call` — the objective is user text and must go through the egress guard and land on an `llm_call` like every other call.

Cache with the existing content-addressed `structured_result` store, keyed on the objective — the same mechanism the concept critic and adjudication already replay through. An identical question then costs nothing.

```python
def _objective_tokens(objective, entity, scope, *, conn=None, client=None) -> set[str]:
    """The objective's own words, plus LLM-derived related business terms.

    ADVISORY and additive: the literal tokens are always retained, so expansion can only widen the
    candidate set, never narrow it. `client=None` (every pure caller, and any degraded deployment)
    returns exactly today's literal tokenisation — byte-for-byte unchanged.
    """
    toks = _literal_tokens(objective, entity, scope)      # today's body, extracted verbatim
    if conn is None or client is None or not objective:
        return toks
    try:
        expanded = _expand_objective(conn, client, objective)
    except Exception:  # noqa: BLE001 — advisory: a failed expansion must never fail the request
        logger.warning("objective expansion failed; falling back to literal tokens", exc_info=True)
        return toks
    return toks | {t for term in expanded for t in _tokenize(term)}
```

The `try` matters: expansion is a search-quality improvement, and a provider fault must degrade to today's behaviour rather than fail someone's feature request.

- [ ] **Step 4: Thread `conn`/`client` from the one caller**

`_objective_tokens` is called once, in the menu assembly, which already holds both. No other call site changes.

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/featuregen/overlay/upload/test_feature_assist.py -v`

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/feature_assist.py src/featuregen/overlay/upload/enrich_llm.py \
        tests/featuregen/overlay/upload/test_feature_assist.py
git commit -m "feat(feature-gen): expand the objective with related business terms before matching"
```

---

### Task 7: A coverage test so no future field can leak

**Files:**
- Test: `tests/featuregen/overlay/upload/test_feature_context_coverage.py` (create)

**Interfaces:**
- Consumes: `_DISPLAY_FIELDS`, `_OPERATIONAL_FIELDS` from `semantic_context`.
- Produces: nothing consumed by later tasks. This is the regression guard.

The root cause of the leak is that `for_feature_generation` is a hand-maintained dict literal: a new enrichment field is added to `graph_node` and the bundle, and nothing fails when the payload forgets it. This test makes forgetting loud.

- [ ] **Step 1: Write the test**

Create `tests/featuregen/overlay/upload/test_feature_context_coverage.py`:

```python
"""Every semantic field the bundle carries must be a deliberate decision at the feature payload.

The leak this catches: `for_feature_generation` is a hand-written dict literal. Migration 1051
added sub_domain/bian_path/process_path and nothing failed when the payload did not carry them,
so an LLM stage ran on every ingest and its output never reached the consumer it was for.
"""
from featuregen.overlay.upload.semantic_context import (
    _DISPLAY_FIELDS,
    _OPERATIONAL_FIELDS,
    for_feature_generation,
)

#: Fields the payload deliberately OMITS, each with the reason. Adding a name here is a decision a
#: reviewer can see and argue with; leaving one out of both this set and the payload is the bug.
DELIBERATELY_OMITTED = {
    # Display-only: a rendering label derived from `visible_requires`, never an input to a feature.
    "sensitivity_display",
}


def test_every_bundle_field_is_either_emitted_or_explicitly_omitted():
    emitted = set(_probe_payload_keys())
    carried = set(_DISPLAY_FIELDS) | set(_OPERATIONAL_FIELDS)
    unaccounted = carried - emitted - DELIBERATELY_OMITTED
    assert not unaccounted, (
        f"{sorted(unaccounted)} ride the semantic bundle but never reach feature generation. "
        "Add them to `for_feature_generation`, or add them to DELIBERATELY_OMITTED with a reason."
    )


def _probe_payload_keys() -> set[str]:
    """The key set `for_feature_generation` can emit, read off a bundle with every field set."""
    from tests.featuregen.overlay.upload.conftest import fully_populated_bundle

    return set(for_feature_generation(fully_populated_bundle()).keys())
```

> If `fully_populated_bundle` does not exist, add it to the nearest conftest: a `SemanticContextBundleV1` constructed in memory with one `SemanticValueV1` per name in `_DISPLAY_FIELDS | _OPERATIONAL_FIELDS` and one per table field, each with a non-empty `value`. No database required.

- [ ] **Step 2: Run it — it must PASS after Task 6**

Run: `uv run pytest tests/featuregen/overlay/upload/test_feature_context_coverage.py -v`
Expected: PASS.

- [ ] **Step 3: Prove the guard actually bites**

Temporarily delete the `out["sub_domain"] = ...` line from `for_feature_generation`, re-run the test, and confirm it FAILS naming `sub_domain`. Then restore the line and confirm it passes again. A guard that cannot fail is not a guard.

- [ ] **Step 4: Commit**

```bash
git add tests/featuregen/overlay/upload/test_feature_context_coverage.py
git commit -m "test(feature-gen): fail when a bundle field never reaches the feature payload"
```

---

### Task 8: Unconfirmed grain and as-of reach generation, labelled

**Files:**
- Modify: `src/featuregen/overlay/upload/feature_assist.py:514-542` (`_table_context`)
- Test: `tests/featuregen/overlay/upload/test_feature_assist.py`

**Interfaces:**
- Consumes: candidate rows already carrying `is_grain`, `grain_fact_event_id`, `is_as_of`, `availability_fact_event_id`.
- Produces: table blocks gain `grain_status` and `as_of_status`, each `"confirmed"` or `"declared"`. Nothing downstream branches on them yet; they are for the model.

Today `_table_context` requires a non-null `grain_fact_event_id` / `availability_fact_event_id` — *"governed-VERIFIED, not merely file-declared."* So a table whose grain the file declares but no human has confirmed reaches the generator with **no grain at all**, and the model invents one. Emitting the declared value with an honest status is strictly more information than emitting nothing.

**This does not touch the operational projection.** A `declared` grain still cannot compile — that guard lives on the execution path and stays.

- [ ] **Step 1: Write the failing test**

Add to `tests/featuregen/overlay/upload/test_feature_assist.py`:

```python
def test_table_context_carries_declared_grain_with_its_status():
    """A file-declared, unconfirmed grain reaches the model labelled — not omitted."""
    cols = [
        {"catalog_source": "cib", "table": "facility_limits", "column": "cust_id",
         "is_grain": True, "grain_fact_event_id": None,
         "is_as_of": False, "availability_fact_event_id": None,
         "table_definition": None, "table_primary_entity": None},
    ]
    block = _table_context(cols)[0]
    assert block["grain_columns"] == ["cust_id"]
    assert block["grain_status"] == "declared"


def test_table_context_marks_a_confirmed_grain_confirmed():
    cols = [
        {"catalog_source": "cib", "table": "facility_limits", "column": "cust_id",
         "is_grain": True, "grain_fact_event_id": "evt_1",
         "is_as_of": False, "availability_fact_event_id": None,
         "table_definition": None, "table_primary_entity": None},
    ]
    block = _table_context(cols)[0]
    assert block["grain_status"] == "confirmed"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_feature_assist.py -k table_context_carries_declared -v`
Expected: FAIL with `KeyError: 'grain_columns'` — the unconfirmed column is filtered out entirely today.

- [ ] **Step 3: Relax the filter and label the result**

Replace the grain and as-of blocks inside `_table_context`:

```python
        # Grain reaches the model whether or not a human confirmed it — an unconfirmed grain is
        # better information than a missing one, PROVIDED the model is told which it is. The
        # status is advisory context, not permission: a `declared` grain still cannot compile.
        grain_cols = sorted(m["column"] for m in members if m["is_grain"])
        if grain_cols:
            block["grain_columns"] = grain_cols
            block["grain_status"] = (
                "confirmed" if all(m["grain_fact_event_id"] for m in members
                                   if m["is_grain"]) else "declared")
        as_of = next((m["column"] for m in sorted(members, key=lambda x: x["column"])
                      if m["is_as_of"]), None)
        if as_of:
            block["as_of_column"] = as_of
            block["as_of_status"] = (
                "confirmed" if next(m["availability_fact_event_id"] for m in members
                                    if m["is_as_of"] and m["column"] == as_of) else "declared")
```

- [ ] **Step 4: Update the docstring**

Replace the docstring's third sentence — *"Confirmed grain columns require a non-null grain_fact_event_id and the as-of column a non-null availability_fact_event_id (governed-VERIFIED, not merely file-declared); primary_entity is ADVISORY."* — with:

```
    Grain and as-of are emitted whether governed-VERIFIED or merely file-declared, each with a
    `grain_status` / `as_of_status` of "confirmed" or "declared" so the model can weigh them.
    Omitting an unconfirmed grain made the model invent one; primary_entity is ADVISORY.
```

- [ ] **Step 5: Run the tests**

Run: `uv run pytest tests/featuregen/overlay/upload/test_feature_assist.py tests/featuregen/overlay/upload/test_feature_context_budget.py -v`
Expected: PASS. The budget suite must stay green — two short scalars per table block.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/feature_assist.py tests/featuregen/overlay/upload/test_feature_assist.py
git commit -m "feat(feature-gen): send declared grain/as-of with an honest confirmed|declared status"
```

---

### Task 9: The full column dossier on the asset-detail screen

**Files:**
- Modify: `src/featuregen/overlay/upload/asset_detail.py` (`_ANCHOR_COLUMNS`, the table-node join)
- Modify: `frontend/src/api.ts`
- Modify: `frontend/src/screens/AssetDetailScreen.tsx`
- Test: `tests/featuregen/overlay/upload/test_asset_detail.py`, `frontend/src/screens/AssetDetailScreen.context.test.tsx`

**Interfaces:**
- Produces: the asset-detail column payload gains `semantic_terms`, `table_role`, `event_or_snapshot`; the screen renders those plus `sub_domain`, and shows `cardinality` on each cross-catalog link.

**What is already there — do not rebuild it.** The screen already renders concept, definition, domain, `ai_summary`, `party_role`, `bian_path`, `process_path`, unit, currency, the adjudication block (`confidence_band`, alternatives, ontology gap) **and the per-field authority** (`producer` / `strength`). Authority display is the hard part and it is done.

**The gaps, verified against `_ANCHOR_COLUMNS`:**

| Field | Backend sends | Screen shows | This task |
|---|---|---|---|
| `semantic_terms` | ❌ **absent from the SELECT** | ❌ | add both |
| `sub_domain` | ✅ | ❌ | render |
| `table_role` | ❌ | ❌ | add both |
| `event_or_snapshot` | ❌ | ❌ | add both |
| link `cardinality` | ✅ (relationship rows) | ❌ | render |

`semantic_terms` is the one that matters most: after Task 4c a column carries 15–20 curated aliases, and today the asset page cannot show a single one of them. It is also the only search handle an unclassified column has, so a reviewer cannot see why a column is (or is not) findable.

- [ ] **Step 1: Write the failing backend test**

```python
def test_the_column_payload_carries_synonyms_and_the_table_shape(db, seeded_column):
    db.execute("UPDATE graph_node SET semantic_terms = %s, sub_domain = %s "
               "WHERE catalog_source = %s AND lower(object_ref) = %s",
               ("audit user, created by, record creator", "Operational Metadata",
                seeded_column.source, seeded_column.flat_ref))
    db.execute("UPDATE graph_node SET table_role = %s, event_or_snapshot = %s "
               "WHERE catalog_source = %s AND kind = 'table' AND table_name = %s",
               ("dimension", "snapshot", seeded_column.source, seeded_column.table))

    got = asset_detail(db, seeded_column.source, seeded_column.object_ref, roles=())
    assert got["semantic_terms"] == "audit user, created by, record creator"
    assert got["sub_domain"] == "Operational Metadata"
    assert got["table_role"] == "dimension"
    assert got["event_or_snapshot"] == "snapshot"
```

- [ ] **Step 2: Run — expect FAIL** (`KeyError: 'semantic_terms'`)

Run: `uv run pytest tests/featuregen/overlay/upload/test_asset_detail.py -k synonyms_and_the_table_shape -v`

- [ ] **Step 3: Add the missing columns to the read model**

In `_ANCHOR_COLUMNS`, add `semantic_terms` beside `ai_summary` — it is a column-node field and belongs with the other display prose:

```python
    "declared_type, definition, ai_summary, semantic_terms, is_grain, is_as_of, concept, domain, "
    "sub_domain, "
```

`table_role` and `event_or_snapshot` live on the **table** node, not the column node, so they come from the existing table-node join — the same one already supplying `table_definition`. Extend that join's select rather than adding a second query; one table node per `(catalog, table)`, so it cannot fan a column into duplicate rows.

- [ ] **Step 4: Write the failing frontend test**

```tsx
it('shows the AI synonyms, the sub-domain and the table shape', async () => {
  renderAssetDetail({
    ...baseFixture,
    semantic_terms: 'audit user, created by, record creator',
    sub_domain: 'Operational Metadata',
    table_role: 'dimension',
    event_or_snapshot: 'snapshot',
  })
  expect(await screen.findByTestId('context-semantic-terms')).toHaveTextContent('created by')
  expect(screen.getByTestId('context-sub-domain')).toHaveTextContent('Operational Metadata')
  expect(screen.getByTestId('context-table-role')).toHaveTextContent('dimension')
  expect(screen.getByTestId('context-event-or-snapshot')).toHaveTextContent('snapshot')
})

it('shows a link cardinality, and says so when it is unknown', async () => {
  renderAssetDetail({
    ...baseFixture,
    relationships: [
      { relationship_ref: 'r1', kind: 'approved_join', cardinality: 'N:1', review_status: 'confirmed' },
      { relationship_ref: 'r2', kind: 'entity_bridge', cardinality: null,  review_status: 'proposed'  },
    ],
  })
  expect(await screen.findByTestId('link-r1')).toHaveTextContent('N:1')
  expect(screen.getByTestId('link-r2')).toHaveTextContent(/not established/i)
})
```

The second test is the load-bearing one. A missing cardinality must read as **"not established"**, never as a blank cell — `plan_join` refuses an unknown-cardinality hop, so a reviewer needs to see that this link cannot yet be traversed rather than assume the value is merely not displayed.

- [ ] **Step 5: Run — expect FAIL**

Run: `cd frontend && npm test -- AssetDetailScreen`

- [ ] **Step 6: Add the types**

In `api.ts`, on the asset-detail column interface:

```ts
  semantic_terms: string | null
  sub_domain: string | null
  table_role: string | null
  event_or_snapshot: string | null
```

and on the relationship entry: `cardinality: string | null`.

- [ ] **Step 7: Render them**

Follow the markup of the neighbouring rows exactly — copy whatever wrapper and classes `party_role` and `process_path` already use rather than introducing a second row idiom on this screen.

Render `semantic_terms` as a **list of chips**, not one comma-joined string: after Task 4c there are 15–20 of them, and a run-on line is unreadable. Split on `,` and trim.

Group the four under the existing semantic section rather than creating a new panel — `table_role` and `event_or_snapshot` describe the column's table and belong beside the other context, labelled so it is clear they are table-level, not column-level (e.g. "Table role", "Table shape").

For cardinality, extend the existing links rows:

```tsx
<span className="adg-v" data-testid={`link-${link.relationship_ref}`}>
  {link.cardinality ?? 'cardinality not established'}
</span>
```

- [ ] **Step 8: Run both suites**

Run: `uv run pytest tests/featuregen/overlay/upload/test_asset_detail.py -v`
Run: `cd frontend && npm test`
Expected: PASS, including every pre-existing asset-detail test.

- [ ] **Step 9: Commit**

```bash
git add src/featuregen/overlay/upload/asset_detail.py frontend/src/api.ts \
        frontend/src/screens/AssetDetailScreen.tsx \
        frontend/src/screens/AssetDetailScreen.context.test.tsx \
        tests/featuregen/overlay/upload/test_asset_detail.py
git commit -m "feat(frontend): show synonyms, sub-domain, table shape and link cardinality on asset detail"
```

---

### Task 9b: Backfill the 272 missing `is_a` parents

**Files:**
- Create: `src/featuregen/overlay/upload/propose_concept_parents.py`
- Modify: `src/featuregen/__main__.py` (a new one-off subcommand)
- Modify: `src/featuregen/overlay/upload/concepts.py` (the generated `is_a=` additions)
- Test: `tests/featuregen/overlay/upload/test_propose_concept_parents.py`

**Interfaces:**
- Produces: `propose_parents(client, records) -> dict[str, str]` — `{concept_name: parent_name}`, already validated.

**This is a one-off, not a per-upload stage.** It runs once over the registry, emits a source diff, and is done.

Of 324 concepts, **52 declare an `is_a` parent and 272 do not.** So a general question cannot reach a specific column through the hierarchy — `exposure` does not find `credit_limit_amount`, because nothing records that one is a kind of the other. It also means `concept_path`, which the payload already carries, is usually a one-element list.

**No human approval gate, and here is why that is safe:** nothing gates on `is_a`. Join candidacy runs on `namespace`. Bridge discovery runs on `namespace`. Pass C runs on `namespace`. Ancestry feeds `concept_path`, which is display and context only. A wrong parent degrades search quality; it cannot corrupt a join, clear a safety check, or make two unrelated columns joinable.

**The automated gate that must stay.** `_validate_registry` already fails at import if any `is_a` does not resolve to a real concept or the graph contains a cycle. That is the safety net, it is automatic, and it must run over the generated parents before they land — a cycle would make `concept_path` walk forever.

- [ ] **Step 1: Write the failing test**

```python
def test_a_proposed_parent_outside_the_registry_is_dropped():
    got = propose_parents(_client_returning({"customer_id": "not_a_real_concept"}),
                          [CONCEPT_REGISTRY["customer_id"]])
    assert got == {}


def test_a_proposed_parent_that_would_create_a_cycle_is_dropped():
    """monetary_flow is_a interest_income, while interest_income is_a monetary_flow."""
    got = propose_parents(_client_returning({"monetary_flow": "interest_income"}),
                          [CONCEPT_REGISTRY["monetary_flow"]])
    assert got == {}


def test_self_parenting_is_dropped():
    got = propose_parents(_client_returning({"customer_id": "customer_id"}),
                          [CONCEPT_REGISTRY["customer_id"]])
    assert got == {}


def test_a_valid_parent_is_kept():
    got = propose_parents(_client_returning({"customer_id": "party_identifier"}),
                          [CONCEPT_REGISTRY["customer_id"]])
    assert got == {"customer_id": "party_identifier"}
```

> If `party_identifier` is not a registry member, substitute any real one — the point is a valid tier-1 name, and Step 1 should read the registry to pick one rather than assume.

- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError`)

- [ ] **Step 3: Implement the proposer**

Batch the 272 in chunks (the registry entries are small — 40 per call is ample), sending each concept's name, group and description plus the full list of candidate parents. Then validate every answer before keeping it:

```python
def propose_parents(client, records) -> dict[str, str]:
    """{concept: parent}, keeping ONLY answers that survive every registry rule.

    Validation is not advisory here. `_validate_registry` fails the whole import on a bad `is_a`,
    so an unvalidated proposal does not degrade the vocabulary — it stops the application booting.
    Drop silently and report the count; a rejected proposal is a normal outcome, not an error.
    """
    kept: dict[str, str] = {}
    for name, parent in _dispatch(client, records).items():
        if name not in CONCEPT_REGISTRY or parent not in CONCEPT_REGISTRY:
            continue
        if parent == name:
            continue
        if _would_cycle(name, parent, kept):
            continue
        kept[name] = parent
    return kept
```

`_would_cycle` walks the existing `is_a` chain **plus the parents accepted so far in this run** — two proposals can be individually acyclic and jointly form a loop.

- [ ] **Step 4: Add the one-off command**

`python -m featuregen propose-concept-parents --out concepts_parents.patch` — prints, for each accepted pair, the exact `is_a="…"` argument to add to that `Concept(...)` line, plus a count of how many were proposed, kept and dropped with reasons.

It **emits a diff; it does not edit the file.** The registry is source code and stays that way — this is a code change reviewed like any other, not a governance approval step.

- [ ] **Step 5: Run it, apply the diff, and let the import guard check the result**

```bash
uv run python -m featuregen propose-concept-parents --out concepts_parents.patch
# apply the additions to concepts.py, then:
uv run python -c "from featuregen.overlay.upload.concepts import CONCEPT_REGISTRY; print(len(CONCEPT_REGISTRY))"
```

A cycle or an unresolved parent makes that import raise. **That is the gate.** If it raises, the proposer's validation has a hole — fix the proposer, do not hand-edit the diff.

- [ ] **Step 6: Confirm the coverage moved**

```bash
grep -c 'is_a=' src/featuregen/overlay/upload/concepts.py
```
Expected: substantially above 52. Record the number in the commit message — it is the measure of what this task bought.

- [ ] **Step 7: Run the full concepts and search suites**

Run: `uv run pytest tests/featuregen/overlay/upload/test_concepts.py tests/featuregen/overlay/upload/test_semantic_context.py -v`

`concept_path` now returns longer chains, so any test asserting a one-element ancestry will need updating — that is the change working, not a regression.

- [ ] **Step 8: Commit**

```bash
git add src/featuregen/overlay/upload/propose_concept_parents.py src/featuregen/__main__.py \
        src/featuregen/overlay/upload/concepts.py tests/featuregen/overlay/upload/
git commit -m "feat(ontology): backfill is_a parents for the flat concepts

<N> of 272 flat concepts gained a parent; is_a coverage 52 -> <M> of 324. Validated against the
registry and for cycles before landing; _validate_registry is the import-time backstop."
```

---

### Task 10: End-to-end verification against a real catalog

**Files:** none — this is a verification gate, not a code change.

- [ ] **Step 1: Run the full backend suite**

Run: `uv run pytest tests/featuregen -q`
Expected: no new failures against the pre-change baseline. Record the baseline first if you do not have it.

- [ ] **Step 2: Run the full frontend suite**

Run: `cd frontend && npm test`
Expected: PASS.

- [ ] **Step 3: Capture a before/after payload diff**

Upload `CIB_Customer_Column_Mapping_final.csv` with `FEATUREGEN_FEATURE_CONTEXT=0`, then again with `=1`, and diff the `redacted_input` of the feature-generation `llm_call` rows:

```sql
SELECT llm_call_ref, prompt_version, output_schema_version,
       jsonb_pretty(redacted_input -> 'catalog_metadata')
FROM llm_call
WHERE task LIKE '%feature%'
ORDER BY created_at DESC LIMIT 2;
```

Confirm the v4 payload carries `semantic_authority`, `sub_domain`, `bian_path`, `process_path`, `table_role`, `event_or_snapshot`, and that table blocks carry `grain_status`.

**This diff is the evidence the work paid off** — keep it, it is what you show a governance reviewer.

- [ ] **Step 4: Establish that projection lag is not confounding the result**

**No worker is deployed on the kind cluster** — the manifests are namespace, postgres, backend, frontend only, and `python -m featuregen worker` is a separate daemon the API does not start. So nothing drains the overlay projection between uploads. Several ingest stages (`drift`, `table_fact_projection`, `join_projection`, `semantic_binding_projection`) skip themselves when `projection_lag > 0`, and if they report `lagged` in this run you cannot tell whether that is this plan's doing or the missing worker.

Before and after the upload:

```sql
SELECT p.projection_name, (SELECT max(global_seq) FROM events) - p.checkpoint_seq AS lag
FROM projection_checkpoints p WHERE p.projection_name = 'overlay';
```

Expected: `lag = 0` at both points. If it is non-zero, **stop and record it** — the run's stage report is not readable as evidence about this plan, and the missing worker is the thing to fix first.

- [ ] **Step 5: Confirm Task 4c actually produced richer synonyms**

```sql
SELECT round(avg(array_length(string_to_array(semantic_terms, ','), 1)), 1) AS avg_terms,
       min(array_length(string_to_array(semantic_terms, ','), 1))            AS fewest,
       count(*) FILTER (WHERE semantic_terms IS NULL OR semantic_terms = '') AS none_at_all
FROM graph_node
WHERE catalog_source = 'cib' AND kind = 'column';
```

Expected: `avg_terms` in the low-to-mid teens and `none_at_all = 0`. If the average is still 3–4, the prompt change did not take — check whether the item payload actually carries the definition (Task 4c Step 1).

- [ ] **Step 6: Confirm Task 6d expanded the objective, and cached it**

```sql
SELECT task, count(*) FROM llm_call
WHERE run_id = '<the feature-generation run id>' GROUP BY task ORDER BY 2 DESC;
```

Issue the **same** objective twice. The expansion task must appear **once**, not twice — the second request replays from the `structured_result` store. If it appears twice, the cache key is wrong and every question is being re-billed.

- [ ] **Step 7: Confirm Tasks 6b and 6c reached the wire and the answer**

```sql
SELECT jsonb_pretty(redacted_input -> 'catalog_metadata' -> 'columns' -> 0),
       jsonb_pretty(raw_output -> 'output' -> 'feature_ideas' -> 0)
FROM llm_call WHERE task LIKE '%feature%' ORDER BY created_at DESC LIMIT 1;
```

The **input** column must show `semantic_authority`, `confidence_band` and `concept_alternatives` on an adjudicated column, plus `sub_domain`, `bian_path`, `process_path`, `table_role`, `event_or_snapshot`, a `cardinality` on each relationship, and `proposed_value` on `unit`/`currency` where the file declared none.

The **output** column must show a populated `grounding` array on every feature idea. An empty or absent one means the prompt change (6c Step 4) did not land, even though the schema did.

- [ ] **Step 8: Confirm Task 9's dossier renders**

Open the asset-detail page for `BO_CIB_CUSTOMER.create_user_nm` and confirm all five are visible: the synonym chips, the sub-domain, the table role, the table shape, and a cardinality (or *"not established"*) on every cross-catalog link.

> Task 9b verifies itself in its own Step 6 (`grep -c 'is_a='`), so it needs no step here.

- [ ] **Step 9: Check the enrichment stages are no longer truncating**

```sql
SELECT stage, state, reason_code, detail
FROM ingestion_run_stage WHERE ingestion_run_id = '<the run id>' ORDER BY id;
```

Expected: no `not_attempted` in any `detail`, and `summary` / `synonyms` in state `succeeded` rather than `partial`. If `not_attempted` persists, `OVERLAY_ENRICH_MAX_PROVIDER_CALLS` is still under-sized for this catalog — read the real chunk count off the detail and raise it.

---

## Out of scope, deliberately

- **The VERIFIED gate on `graph_node`.** Task 5 delivers unconfirmed facts to *generation*; the *execution* path keeps its guard. An unconfirmed `grain` changes row counts and an unconfirmed `availability` risks target leakage — neither is a labelling problem, and neither is visible to a human reviewing the generated feature. If you later want unconfirmed facts to compile, that is a separate plan whose first task is enumerating every read site of `entity_status='VERIFIED'`.
- **The uncapped concept critic** — but read this before you skip it. It is ~70–100 **individually dispatched** calls on a 144-column catalog (one per column whose proposed concept lands in `identifier`/`monetary`/`temporal`/`label`), and it is the only LLM stage in the ingest with no provider-call ceiling and no `not_attempted` disposition — `critique_concept_batch` is a plain loop. Task 4 raises the per-call timeout to 300s, so after this plan a slow provider costs the concept stage ~5× more than it did. Tasks 1–3 defuse most of that (a timeout no longer costs 3 attempts), but a cap matching `adjudication_bounds`' pattern is the real fix and should follow closely.
- **Streaming the adapter.** `client.messages.stream(...)` + `.get_final_message()` supersedes Task 3, removes the idle-timeout problem entirely, and returns the **partial text** on failure — which also closes the gap where a truncated body is discarded and `llm_call.raw_output` is stored as `{}`. It is the right long-term answer for a 32,000-token ceiling. Deliberately not here: it changes the one function every LLM call in the platform passes through, and wants its own test pass rather than riding along with a config change.
- **`Confidential` sensitivity is silently dropped.** The FTR adapter has no sensitivity mapping at all, and the valid vocabulary is only `{"", "pii", "restricted"}` — so a glossary row declaring `Confidential` graphs with **no tag, readable by every role**. This is a live governance defect on real CIB data and is more urgent than anything in this plan, but it needs a design decision first (does `Confidential` map to the governed floor `public < internal < confidential < restricted < prohibited`, or to the read-scope tag?) — the two vocabularies are deliberately distinct, with a test enforcing the distinction. Its own plan.
- **Semantic-binding governance UI.** `entity_assignment` / `currency_binding` proposals still have no confirm surface. Separate plan.
- **`FEATUREGEN_DATASET_PROFILES`.** Leaving it at `"0"` keeps `data_role`, `authority_role`, `temporal_storage_model` and `business_context` out of the table block, and keeps the upload screen's catalog-narrative editor writing to nothing. Both are pre-existing and out of scope here.
