# Phase 2 — Table Facts (Pass B) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an LLM "Pass B" table-synthesis pass that PROPOSES per-table facts (grain, availability/as-of, primary entity, table role, event-vs-snapshot) as human-gated typed-fact *proposals* — never auto-confirmed — reusing the mature overlay fact substrate (propose → confirm → expire → reverify) and the Pass A batch engine end-to-end.

**Architecture:** Pass B is ~90% reuse. The genuinely new surface is: (1) register the *already-existing* `UploadCatalog` as the process catalog adapter so the governed `propose_fact` lifecycle runs in the upload flow (this also un-gates the Phase-1-deferred governed joins); (2) a per-table LLM synthesis stage (assembler + `run_batched` driver + a dict-shaped validator) that emits `grain`/`availability_time` **PROPOSED-only** typed facts via `propose_fact` and advisory `table_role`/`primary_entity` field evidence; (3) a SPECIALIZED_FACT projection bridge that reads a *confirmed* (VERIFIED) grain/availability fact and lands it on `graph_node`, re-applied at end-of-ingest; (4) readiness that reads fact state to flip a table's grain/availability requirement missing → proposed → confirmed; (5) a pending-proposal worklist reader. The existing declared/structural grain path (`_assert_fact`) is byte-for-byte unchanged — it is legitimate *source* attestation (§16 "structural or human"), a different producer from Pass B's LLM proposal.

**Tech Stack:** Python 3.12, `uv`, psycopg 3, PostgreSQL (ephemeral PG auto-provisioned by `postgresql_proc` in tests), pytest. Event-sourced overlay fact substrate; JSON-Schema-validated LLM output via `DocumentSchemaRegistry`.

## Revision log (v2 architectural + v3 code-verified review folded in)

**v3** adds fixes from a code-verified adversarial review (34 confirmed defects). The load-bearing v3 corrections: `extract`/`ref_aware` threaded through the REAL chain `run_batched → audited_batch_call → validate_batch_results` (v1 missed the `audited_batch_call` intermediary → Pass B would've emitted zero proposals); `Command(...)` built with all 6 fields (not `Command(actor=,args=)`); advisory evidence via `_write_producer_field` (the v1 `record_field_evidence` call omitted two required NOT-NULL args); `UploadContextAdapter.catalog_source` sentinel (else the worker drift poller AttributeErrors every tick); the Task 5 egress leak closed (never egress `r.definition`, only the curated `definition`); Task 11 worklist fixed (`task_id` column, dict-key access, platform-admin reader); `event_time_plus_lag` dropped; fail-soft savepoints around the LLM call + the end-of-ingest projection; four-eyes (propose under `_ENRICH_ACTOR`); the confirm authority is the **platform-admin governance queue** and the `_confirm_grain` helper must **drain the projection** after confirm; **tests live under `tests/featuregen/overlay/upload/`** with a new conftest authoring all fixtures/helpers (Task 7 Step 0); `IngestResult.status == "ingested"`; `compute_readiness(..., scope=...)`.

**v2** revises v1 per a full architectural review. Load-bearing changes an implementer must not miss:
- **Task 9 projection is now idempotent** — clears every prior `is_grain`/`is_as_of` on the table's columns before re-applying verified facts (v1 only ever set `true`, leaving stale flags after a grain changed/expired/was rejected). **[must-fix #1]**
- **Task 7 skip logic reads folded fact state, not raw stream existence** — v1's `_fact_stream_absent` skipped *forever* once any stream existed (even `REJECTED`/`EXPIRED`). v2 skips quietly only when `VERIFIED` (respect stronger source/declared evidence) and otherwise lets `propose_fact` adjudicate — it already handles pending-duplicate, sticky-rejected, and re-propose-after-terminal. Denials are logged as conflict diagnostics. **[must-fix #2, #12]**
- **Task 4/6 structured validation happens *inside* the batch harness** (ref-aware `accept(raw, ref)`), so a grain naming a non-existent column is classified `INVALID` by `validate_batch_results` — not accepted-then-silently-dropped. **[must-fix #3]**
- **`is_unique=True` is the proposed *claim*, not proven fact** — the fact schema (`{columns, is_unique}`, `additionalProperties:false`) forbids a caveat field, so the "LLM-proposed, not profiled" origin is surfaced via the proposal/worklist (`proposed_by` = service actor), and human confirmation *is* the uniqueness attestation. Documented as a Phase-2 limitation (profiling corroboration = Phase 3). **[must-fix #4]**
- **Feature kill switch (`OVERLAY_TABLE_SYNTH`) is separate from batch mode (`mode("table_synth")`)** — Task 2's test asserts both distinctly. **[must-fix #5]**
- **`event_or_snapshot` is now projected** as a third advisory field (Task 8), not captured-and-dropped. **[must-fix #8]**
- **Readiness reflects the full fact lifecycle** (rejected/expired/stale/pending-reverify), mapped onto the 4 status values + distinct causes; only `VERIFIED` reads ready. **[must-fix #7]**
- Assembler enriched with column identifier/temporal/semantic/entity roles + table-level domain/BIAN/FIBO context (bounded, sample-stripped) for better grain proposals **[should-fix]**; `UploadCatalog` vs `UploadContextAdapter` distinction made explicit + fallback telemetry **[Task 1 clarity]**; single-table `project_table_facts_for_ref` helper for a future confirm hook; ownership-fallback limitation documented.
- **`availability_time.basis`** — the underlying `FACT_VALUE_SCHEMAS.AVAILABILITY_TIME` fact schema is UNCHANGED (`posted_at|ingested_at|event_time_plus_lag`; facts.py:39-54; a different vocabulary would fail `validate_fact_value`). But **Pass B only proposes the two lag-free bases** (`posted_at|ingested_at`): `event_time_plus_lag` requires a `lag_hours` (facts.py:52) that Pass B cannot infer, so offering it would guarantee a `validate_fact_value` denial. It is dropped from the synthesis schema/accept/instruction (v3 review fix). Carrying a lag end-to-end is out of Phase 2 scope.

## Global Constraints

- **Migrations start at `0986`** (last used: `0985_field_revalidation.sql`). Allocate sequentially: this plan uses **`0986`** only. Verify the slot is free before writing (`ls src/featuregen/db/migrations/`).
- **Every value the LLM emits is a PROPOSAL, never operational.** Grain/availability reach operational authority **only** through human `confirm_fact` (§16: grain/as-of = "structural or human"; profile supports, not alone). Pass B never appends `OVERLAY_FACT_CONFIRMED`.
- **Fail-soft / advisory.** Pass B NEVER aborts or fails an upload. Mirror `_propose_governed_joins` (ingest.py:122): a malformed candidate is skipped-loud with a counter; a `propose_fact` denial is logged and counted; the upload still returns `ok`.
- **Metadata-only egress.** Any glossary free-text that reaches the LLM MUST pass through `strip_sample_values(...)` first (the Phase-1 CRITICAL leak fix) and ride under a **distinct** key — never the forbidden plain `definition` key. The batch-level `assert_llm_safe` PII scan still applies on top.
- **Kill switch, default OFF.** Pass B is gated behind env `OVERLAY_TABLE_SYNTH` (default `0`/off). With it off, `ingest_upload` behaviour is byte-for-byte today's behaviour.
- **Reuse the shared identity helper.** Pass B, `_assert_fact`, and readiness MUST key a table's grain fact via the *same* `table_ref(catalog_source, table)` (`overlay/upload/upload_catalog.py:10`) so `fact_key(...)` is identical across producer, reader, and confirmer. Never hand-build a table `CatalogObjectRef`.
- **Grain/availability facts prohibit `use_case`** (`validate_fact_value` enforces this). Always pass `use_case=None`.
- **display ≠ authority.** Advisory Pass B fields (`table_role`, `primary_entity`) use a RECOMMENDATION influence ceiling → structurally impossible to become load-bearing. The load-bearing grain/as-of value comes *exclusively* from the specialized-fact projection (spec §5.3), never from the field resolver.
- **Tests:** `uv run pytest <path> -q`. New tests live under `tests/featuregen/overlay/upload/` mirroring the existing layout.

---

## Reuse Map (what already exists — do NOT rebuild)

| Need | Status | Home |
|---|---|---|
| PROPOSED-only typed fact + human gate task | REUSE | `propose_fact(conn, cmd)` `overlay/proposal_commands.py:34` — appends ONLY `OVERLAY_FACT_PROPOSED`, opens one gate task per authority side |
| PROPOSED not load-bearing until VERIFIED | REUSE | `resolve_fact(conn, adapter, ref, fact_type, use_case)` `overlay/resolve.py:183` — serves a value only on `VERIFIED` |
| grain / availability_time value schemas | REUSE | `FACT_VALUE_SCHEMAS` `overlay/facts.py:38` — `GRAIN={columns[],is_unique}`, `AVAILABILITY_TIME={column,basis,lag_hours?}` |
| Human confirm PROPOSED → VERIFIED (+ arms expiry) | REUSE | `confirm_fact(conn, cmd)` `overlay/confirmation_commands.py:47` — human-only; four-eyes satisfied by the service proposer; single-confirmer. **Authority = platform-admin governance queue** (owner_of→None routes there); the confirmer MUST hold `platform-admin` role_claims. Data-owner-subject routing arrives only with a richer adapter (Phase 3/4). |
| Reject a proposal | REUSE | `reject_fact` `overlay/confirmation_commands.py:184` |
| Reviewer reads a proposal | REUSE | `get_task_proposal(conn, task_id, actor)` `overlay/task_read.py:17` — adapter-free; free-form `proposed_value` carries the structured candidate |
| Confirmations expiring/scoped + reverify + renewal | REUSE | `schedule_expiry` / `fire_due_overlay_expiries` / `fire_due_overlay_renewals` / `open_reverify_task` `overlay/expiry.py`, `overlay/reverify_tasks.py` |
| Batch degradation ladder + chunking + item container | REUSE | `run_batched`, `chunk_items`, `estimate_tokens`, `BatchItem` `overlay/upload/enrich_batch.py`; `audited_batch_call` `overlay/upload/enrich_llm.py:265` |
| Audit / service actor / egress-block / run bucket | REUSE | `ENRICHMENT_RUN_ID`, `_ENRICH_ACTOR`, `_audit_egress_block`, `record_llm_call` `overlay/upload/enrich_llm.py` |
| Resolver shows a SPECIALIZED_FACT proposal, never load-bearing | REUSE | `resolve_field_authority` short-circuit `overlay/field_authority.py:286-287` |
| Per-table grouping → grain/availability shapes | REUSE | `_table_facts(rows)` `overlay/upload/ingest.py:78` |
| Stable table logical_ref / graph key | REUSE | `normalize_ref(...,column=None)` `overlay/upload/object_ref.py:33`; `_graph_key` handles `column=None` `overlay/upload/field_resolution.py:105` |
| Table advisory evidence store | REUSE | `record_field_evidence` / `read_active_field_evidence` `overlay/field_evidence.py` — keys on `(logical_ref, field_name)`, column not required |
| **Catalog adapter with `owner_of→None` (→ governance queue)** | **REUSE** | **`UploadCatalog` `overlay/upload/upload_catalog.py:23` already implements the full `CatalogAdapter` protocol** — the only gap is nobody calls `register_catalog_adapter()` |
| Sample-value stripping (leak fix) | REUSE | `strip_sample_values` `overlay/upload/sample_parser.py` |
| Readiness proposed/confirmed vocabulary | EXTEND | `_PHASE1_UNPROMOTED` + `CAUSE_PROPOSED_UNCONFIRMED` `overlay/upload/readiness.py:53,79` — static today; make it read fact state |
| Egress filter for a table item's column descriptors | EXTEND | `_item_egress_ok` `overlay/upload/enrich_llm.py:253` — rejects list-of-dict today |
| Structured (non-scalar) batch accept | EXTEND | `validate_batch_results` `overlay/upload/enrich_batch.py:54` — extracts one scalar `out_key` today |

---

## File Structure

**New files:**
- `src/featuregen/overlay/upload/table_synth.py` — Pass B: input assembler, synthesis driver, dict-shaped validator, `_propose_table_facts` emission. One responsibility: derive per-table fact *proposals* from Pass A output.
- `src/featuregen/overlay/upload/table_fact_projection.py` — the SPECIALIZED_FACT bridge: read VERIFIED grain/availability → land on `graph_node`; the pending-proposal worklist reader.
- `src/featuregen/db/migrations/0986_graph_node_table_fields.sql` — advisory `table_role`/`primary_entity` columns + their decision-link + grain/as-of provenance link on `graph_node`.
- Test files mirroring each under `tests/featuregen/overlay/upload/`.

**Modified files:**
- `src/featuregen/overlay/upload/enrich_llm.py` — `_SCHEMAS` (add two schemas), `_item_egress_ok` (admit bounded column-descriptor list).
- `src/featuregen/overlay/upload/enrich_config.py` — `table_synth` caps.
- `src/featuregen/overlay/upload/enrich_batch.py` — `validate_batch_results` optional `extract` param.
- `src/featuregen/overlay/upload/field_policies.py` — `table_role`/`primary_entity` policies.
- `src/featuregen/overlay/upload/field_resolution.py` — project advisory table fields' display.
- `src/featuregen/overlay/upload/readiness.py` — availability requirement + fact-state reads.
- `src/featuregen/overlay/upload/ingest.py` — register adapter at entry; wire Pass B + projection near line 619.
- `src/featuregen/runtime/worker.py` — register the upload adapter at worker startup (for the expiry/renewal pollers).

**Scope boundary (explicit):** Phase 2 delivers the *backend* proposal + confirmation-persistence surface. A human confirms via the existing `confirm_fact` command; there is **no confirm API route in the codebase today**, so the graph_node `is_grain` boolean lands via **end-of-ingest re-projection** (guaranteed on every re-upload). The load-bearing truth on confirm — `resolve_fact` (VERIFIED) and readiness — updates immediately regardless. A live confirm-time projection + the reviewer dashboard UI are **Phase 4** (prioritized HITL + dashboards). Pass C (joins) and Pass D (reconciliation) remain Phase 3/4.

---

## Task 1: Register the upload-context catalog adapter

**Why first:** `propose_fact`, `confirm_fact`, `reject_fact`, and the expiry pollers all call `current_catalog_adapter()`, which **fails closed** (RuntimeError) because nothing calls `register_catalog_adapter()` today. We register a stable, stateless adapter idempotently so every producer/confirmer/poller resolves an adapter whose `owner_of→None` routes facts to the governance queue (the documented fail-safe). This ALSO un-gates the Phase-1-deferred `_propose_governed_joins`.

**Two distinct components — do not conflate them:**

| Component | Lifetime | Purpose | `owner_of` |
|---|---|---|---|
| `UploadCatalog` (exists, `upload_catalog.py:23`) | **per-upload** (built from `vr.good`) | drift fingerprint / `large_change_brake` context | `→None` |
| `UploadContextAdapter` (**new**, this task) | **process-stable**, stateless | the registered adapter for propose/confirm/expiry, which run *outside* an upload | `→None` |

The per-upload `UploadCatalog` cannot be the registered adapter: confirm/expiry run later (a different request, possibly a different process) when no upload is in flight, so the adapter must be stateless and stable. Both return `owner_of→None`; the new adapter is deliberately *narrower* (empty `list_objects`/`fingerprint`) because propose/confirm/expiry never call those — only `owner_of`/`get_fact`.

**Ownership limitation (document, don't fix here):** `owner_of→None` routes every grain/availability confirmation task to the **platform-admin governance queue**, not a data-owner/table-steward. That is a correct fail-safe for a proof-of-concept HITL loop, but it is NOT production-grade owner routing. Data-owner-specific routing requires a richer adapter (structural-provider fusion, Phase 3/4). State this in the module docstring so no one mistakes the fallback for finished ownership wiring.

**Files:**
- Create: `src/featuregen/overlay/upload/upload_catalog.py` — add `UploadContextAdapter` + `ensure_upload_catalog_adapter()` (append to the existing file).
- Modify: `src/featuregen/overlay/upload/ingest.py` — call `ensure_upload_catalog_adapter()` at the top of `ingest_upload` (before any fact write).
- Modify: `src/featuregen/runtime/worker.py:524` — call it beside `register_overlay(registry)`.
- Test: `tests/featuregen/overlay/upload/test_upload_context_adapter.py`

**Interfaces:**
- Consumes: `register_catalog_adapter`, `current_catalog_adapter`, `CatalogAdapter` from `overlay/catalog.py`; `CatalogObjectRef` from `overlay/identity.py`.
- Produces: `UploadContextAdapter` (stateless, `owner_of→None`, `get_fact→None`, `list_objects→[]`, `fingerprint→{}`); `ensure_upload_catalog_adapter() -> None` (idempotent: registers `UploadContextAdapter` only if `current_catalog_adapter()` raises).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_upload_context_adapter.py
import pytest
from featuregen.overlay.catalog import (
    current_catalog_adapter, register_catalog_adapter, _clear_catalog_adapter,
)
from featuregen.overlay.identity import CatalogObjectRef
from featuregen.overlay.upload.upload_catalog import (
    UploadContextAdapter, ensure_upload_catalog_adapter,
)


@pytest.fixture(autouse=True)
def _reset_adapter():
    _clear_catalog_adapter()
    yield
    _clear_catalog_adapter()


def test_ensure_registers_when_absent():
    with pytest.raises(RuntimeError):
        current_catalog_adapter()
    ensure_upload_catalog_adapter()
    assert isinstance(current_catalog_adapter(), UploadContextAdapter)


def test_adapter_owner_of_is_none_routes_to_governance():
    ref = CatalogObjectRef("src", "table", "public", "txn", None)
    assert UploadContextAdapter().owner_of(ref) is None
    assert UploadContextAdapter().get_fact(ref, "grain") is None


def test_ensure_is_idempotent_and_yields_to_existing():
    sentinel = UploadContextAdapter()
    register_catalog_adapter(sentinel)
    ensure_upload_catalog_adapter()  # must NOT clobber an already-registered adapter
    assert current_catalog_adapter() is sentinel
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_upload_context_adapter.py -q`
Expected: FAIL — `ImportError: cannot import name 'UploadContextAdapter'`.

- [ ] **Step 3: Implement the adapter + idempotent ensure**

Append to `src/featuregen/overlay/upload/upload_catalog.py`:

```python
from featuregen.overlay.catalog import (
    CatalogAdapter, current_catalog_adapter, register_catalog_adapter,
)


class UploadContextAdapter(CatalogAdapter):
    """A stateless catalog adapter for the upload request/worker context.

    The upload flow has no external ownership registry, so ``owner_of`` returns ``None`` — which
    routes every governed fact (grain/availability proposals) to the platform-admin governance
    queue, the documented fail-safe (mirrors ``PostgresCatalog.owner_of``). ``get_fact`` returns
    ``None`` (the ML fact types are recorded in the overlay, not this catalog). ``list_objects`` /
    ``fingerprint`` are unused on the propose/confirm/expiry path, so they are empty here; the
    per-upload ``UploadCatalog`` still owns drift fingerprinting. Stateless ⇒ safe to register once
    process-wide with no clobber hazard."""

    # REQUIRED protocol member (catalog.py:48). Registering this adapter at worker startup un-skips
    # the drift poller (_run_drift_scan reads adapter.catalog_source every tick); without this it
    # would AttributeError each tick. A reserved sentinel source that no real UploadCatalog uses +
    # an empty fingerprint() means detect_catalog_changes diffs {} against an equally-empty prior
    # snapshot → zero changes → drift is INERT (no false stales, no per-tick error).
    catalog_source = "upload:context"

    def list_objects(self):
        return []

    def fingerprint(self):
        return {}

    def get_fact(self, ref, fact_type, use_case=None):
        return None

    def owner_of(self, ref):
        return None


def ensure_upload_catalog_adapter() -> None:
    """Register a process-wide :class:`UploadContextAdapter` iff none is registered yet.

    Idempotent and forward-safe: a deployment that registers a richer adapter (with real ownership)
    wins — this NEVER clobbers an already-registered adapter — and a second call is a no-op. Called
    at ``ingest_upload`` entry (the single upload chokepoint) and at worker startup (for the
    expiry/renewal pollers). Emits a counter/log when the fallback is installed so a missing
    production ownership adapter is visible, not silent."""
    from featuregen.observability import counters, logger
    try:
        current_catalog_adapter()
    except RuntimeError:
        register_catalog_adapter(UploadContextAdapter())
        counters.incr("overlay.catalog_adapter.upload_context_fallback_registered")
        logger.info("registered UploadContextAdapter fallback (owner_of->None; governance-queue "
                    "routing). Not production owner routing — see Phase 3/4.")
```

> **Implementer note:** confirm the `counters`/`logger` import path matches `ingest.py`'s.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_upload_context_adapter.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Wire it at both process entry points**

In `src/featuregen/overlay/upload/ingest.py`, add the import (top of file) and the call as the first line of `ingest_upload` (before `validate_rows`):

```python
from featuregen.overlay.upload.upload_catalog import (
    UploadCatalog, table_ref, ensure_upload_catalog_adapter,
)
# ...
def ingest_upload(conn, catalog_source: str, rows: list[CanonicalRow], *,
                  actor, now: datetime | None = None, client=None,
                  profile: SourceCapabilityProfile | None = None,
                  glossary: GlossaryUpload | None = None) -> IngestResult:
    ensure_upload_catalog_adapter()   # governed fact lifecycle needs an adapter (owner_of->None)
    if glossary is not None and profile is None:
        profile = FTR_GLOSSARY_PROFILE
    # ...unchanged...
```

In `src/featuregen/runtime/worker.py`, beside line 524:

```python
    register_overlay(registry)
    from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter
    ensure_upload_catalog_adapter()   # expiry/renewal pollers resolve grain/availability authority
```

- [ ] **Step 6: Run the surrounding suites to confirm no regression**

Run: `uv run pytest tests/featuregen/overlay/upload/test_ingest.py tests/featuregen/overlay/upload/test_upload_context_adapter.py -q`
Expected: PASS (existing ingest behaviour unchanged; adapter now registered).

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/overlay/upload/upload_catalog.py src/featuregen/overlay/upload/ingest.py src/featuregen/runtime/worker.py tests/featuregen/overlay/upload/test_upload_context_adapter.py
git commit -m "feat(overlay): register upload-context catalog adapter (un-gates governed fact lifecycle)"
```

---

## Task 2: Pass B output schemas + rollout config + kill switch

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_llm.py:81` — add two `_SCHEMAS` entries.
- Modify: `src/featuregen/overlay/upload/enrich_config.py:8-9` — add `table_synth` caps.
- Test: `tests/featuregen/overlay/upload/test_table_synth_schema.py`

**Interfaces:**
- Consumes: `_SCHEMAS`, `register_enrichment_schemas`, `DocumentSchemaRegistry` (`enrich_llm.py`); `_DEFAULT_MAX_ITEMS`, `_DEFAULT_MAX_INPUT_TOKENS`, `mode` (`enrich_config.py`).
- Produces: registered schemas `("overlay_table_synth_batch", 1)` (array of per-item objects) and `("overlay_table_synth", 1)` (single-item sibling for `_single_fallback`); config keys so `chunk_items` caps and the kill switch resolve without `KeyError`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_table_synth_schema.py
from featuregen.overlay.upload.enrich_config import max_items, max_input_tokens, mode
from featuregen.overlay.upload.enrich_llm import _SCHEMAS


def test_batch_and_single_schemas_registered():
    batch = _SCHEMAS[("overlay_table_synth_batch", 1)]
    item = batch["properties"]["results"]["items"]
    props = item["properties"]
    assert item["required"] == ["ref", "synthesis"]
    syn = props["synthesis"]["properties"]
    assert set(syn) == {
        "grain_columns", "as_of_column", "as_of_basis",
        "primary_entity", "table_role", "event_or_snapshot",
    }
    assert ("overlay_table_synth", 1) in _SCHEMAS  # single-call fallback sibling


def test_two_independent_switches(monkeypatch):
    # The FEATURE switch (OVERLAY_TABLE_SYNTH) and the BATCH MODE (OVERLAY_ENRICH_TABLE_SYNTH_MODE)
    # are ORTHOGONAL. Feature-off means Pass B never runs; mode only chooses batch-vs-single WHEN it
    # runs. Setting mode=single must NOT be read as "feature off".
    from featuregen.overlay.upload.ingest import table_synth_enabled
    monkeypatch.delenv("OVERLAY_TABLE_SYNTH", raising=False)
    monkeypatch.delenv("OVERLAY_ENRICH_TABLE_SYNTH_MODE", raising=False)
    assert table_synth_enabled() is False            # FEATURE kill switch default OFF
    assert mode("table_synth") == "single"           # batch mode default single (only matters if on)
    assert isinstance(max_items("table_synth"), int)
    assert isinstance(max_input_tokens("table_synth"), int)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_synth_schema.py -q`
Expected: FAIL — `KeyError: ('overlay_table_synth_batch', 1)`.

- [ ] **Step 3: Add the schemas**

In `src/featuregen/overlay/upload/enrich_llm.py`, add to `_SCHEMAS` (after the existing `_batch` schemas, ~line 123). Each per-item entry carries a nested `synthesis` object so the batch harness can treat `synthesis` as a single (structured) out-key:

```python
    ("overlay_table_synth_batch", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {"results": {"type": "array", "minItems": 0, "maxItems": 256,
            "items": {"type": "object", "additionalProperties": False,
                "properties": {
                    "ref": {"type": "string", "maxLength": 256},
                    "synthesis": {"type": "object", "additionalProperties": False,
                        "properties": {
                            "grain_columns": {"type": "array", "maxItems": 16,
                                              "items": {"type": "string", "maxLength": 128}},
                            "as_of_column": {"type": ["string", "null"], "maxLength": 128},
                            "as_of_basis": {"type": ["string", "null"],
                                            "enum": ["posted_at", "ingested_at", None]},
                            "primary_entity": {"type": ["string", "null"], "maxLength": 128},
                            "table_role": {"type": ["string", "null"], "maxLength": 64},
                            "event_or_snapshot": {"type": ["string", "null"],
                                                  "enum": ["event", "snapshot", None]},
                        }, "required": ["grain_columns"]}},
                "required": ["ref", "synthesis"]}}},
        "required": ["results"]},
    ("overlay_table_synth", 1): {
        "type": "object", "additionalProperties": False,
        "properties": {
            "grain_columns": {"type": "array", "maxItems": 16,
                              "items": {"type": "string", "maxLength": 128}},
            "as_of_column": {"type": ["string", "null"], "maxLength": 128},
            "as_of_basis": {"type": ["string", "null"],
                            "enum": ["posted_at", "ingested_at", None]},
            "primary_entity": {"type": ["string", "null"], "maxLength": 128},
            "table_role": {"type": ["string", "null"], "maxLength": 64},
            "event_or_snapshot": {"type": ["string", "null"],
                                  "enum": ["event", "snapshot", None]},
        }, "required": ["grain_columns"]},

# NOTE: `event_time_plus_lag` is intentionally EXCLUDED from as_of_basis. FACT_VALUE_SCHEMAS mandates
# a `lag_hours` when basis == event_time_plus_lag (facts.py:52), and Pass B has no way to infer a lag,
# so such a proposal would always be denied by validate_fact_value. Phase 2 offers only the two
# lag-free bases; adding event_time_plus_lag would require a lag_hours field end-to-end (out of scope).
```

- [ ] **Step 4: Add the config caps**

In `src/featuregen/overlay/upload/enrich_config.py`, add a `table_synth` entry to BOTH dicts (few tables per chunk, generous token budget per table since a table item is large):

```python
_DEFAULT_MAX_ITEMS = {
    # ...existing entries...
    "table_synth": 8,
}
_DEFAULT_MAX_INPUT_TOKENS = {
    # ...existing entries...
    "table_synth": 6000,
}
```

Also add the **feature kill switch** helper (distinct from batch mode) next to `governed_joins_enabled()` — find that (likely `overlay/upload/ingest.py` or a flags module) and mirror it:

```python
def table_synth_enabled() -> bool:
    """Feature switch for Pass B (default OFF). Orthogonal to OVERLAY_ENRICH_TABLE_SYNTH_MODE,
    which only selects batch-vs-single execution WHEN the feature is on."""
    return os.environ.get("OVERLAY_TABLE_SYNTH", "0") == "1"
```

Task 7 wires the ingest call behind this helper; Task 2 owns its definition so it exists before any consumer.

- [ ] **Step 5: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_synth_schema.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_llm.py src/featuregen/overlay/upload/enrich_config.py tests/featuregen/overlay/upload/test_table_synth_schema.py
git commit -m "feat(overlay): table_synth output schemas + rollout config (default off)"
```

---

## Task 3: Extend the per-item egress filter to admit a table item

**Why:** A table item must carry each column's `{column, type, concept, business_definition}` so the synthesis prompt can reason about which columns form a grain. `_item_egress_ok` today rejects any list whose elements are not strings — a list-of-dicts fails, EGRESS-excluding the whole table. Extend it to admit a **bounded** `column_profiles` list of fixed-shape descriptors, preserving the metadata-only guarantee and the forbidden plain-`definition`-key rule (the curated definition rides as `business_definition`, mirroring the glossary path).

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_llm.py:247-262` — `_ITEM_META_ALLOWED` + `_item_egress_ok`.
- Test: `tests/featuregen/overlay/upload/test_item_egress_table.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `_item_egress_ok` additionally admits a top-level key `column_profiles` whose value is a list (≤64) of dicts, each with keys ⊆ `{column, type, concept, business_definition}`, every value a `str` of len ≤ 200. Any other nested shape (a bare `definition` key, a non-str value, a list of non-dicts, an unknown descriptor key) → `False`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_item_egress_table.py
from featuregen.overlay.upload.enrich_llm import _item_egress_ok


def _cols(n=2):
    return [{"column": f"c{i}", "type": "int", "concept": "amount",
             "business_definition": "the posted amount"} for i in range(n)]


def test_table_item_with_column_profiles_passes():
    assert _item_egress_ok({"table": "txn", "column_profiles": _cols()}) is True


def test_descriptor_with_forbidden_definition_key_fails():
    bad = [{"column": "c0", "type": "int", "definition": "leaky free text"}]
    assert _item_egress_ok({"table": "txn", "column_profiles": bad}) is False


def test_descriptor_with_non_string_value_fails():
    bad = [{"column": "c0", "type": "int", "concept": ["not", "a", "string"]}]
    assert _item_egress_ok({"table": "txn", "column_profiles": bad}) is False


def test_oversized_descriptor_value_fails():
    bad = [{"column": "c0", "business_definition": "x" * 201}]
    assert _item_egress_ok({"table": "txn", "column_profiles": bad}) is False


def test_too_many_descriptors_fails():
    assert _item_egress_ok({"table": "txn", "column_profiles": _cols(65)}) is False


def test_existing_scalar_and_list_of_str_still_pass():
    assert _item_egress_ok({"table": "txn", "columns": ["a", "b"]}) is True
    assert _item_egress_ok({"table": "txn", "column": "c0"}) is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_item_egress_table.py -q`
Expected: FAIL — `test_table_item_with_column_profiles_passes` returns `False` (list-of-dict rejected).

- [ ] **Step 3: Implement the extension**

In `src/featuregen/overlay/upload/enrich_llm.py`, add `column_profiles` to the allow-list and teach the filter the descriptor shape:

```python
_ITEM_META_ALLOWED = frozenset({
    "table", "column", "type", "columns", "concept",
    "term_name", "business_definition", "synonyms", "data_domain", "bian_path", "fibo_path",
    "column_profiles",
})

# The ONLY keys a per-column descriptor may carry, each a short scalar. `definition` is deliberately
# ABSENT — a technical free-text definition can never ride this seam; a curated meaning rides as
# `business_definition` (already stripped of sample values upstream). The role fields
# (identifier_role/temporal_role/semantic_type/entity) come from Pass A evidence and sharpen grain
# proposals (an identifier-role column is grain-eligible; a temporal-role column is as-of-eligible).
_COLUMN_PROFILE_KEYS = frozenset({
    "column", "type", "concept", "business_definition",
    "identifier_role", "temporal_role", "semantic_type", "entity",
})
_MAX_COLUMN_PROFILES = 64


def _column_profile_ok(desc: object) -> bool:
    if not isinstance(desc, dict):
        return False
    if any(k not in _COLUMN_PROFILE_KEYS for k in desc):
        return False
    return all(isinstance(v, str) and len(v) <= 200 for v in desc.values())


def _item_egress_ok(metadata: dict) -> bool:
    if any(k not in _ITEM_META_ALLOWED for k in metadata):
        return False
    for k, v in metadata.items():
        if k == "column_profiles":
            if not isinstance(v, list) or len(v) > _MAX_COLUMN_PROFILES:
                return False
            if not all(_column_profile_ok(d) for d in v):
                return False
        elif isinstance(v, list):
            if not all(isinstance(x, str) and len(x) <= 200 for x in v):
                return False
        elif not isinstance(v, str) or len(v) > 200:
            return False
    return True
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_item_egress_table.py -q`
Expected: PASS (6 passed).

- [ ] **Step 5: Run the enrich egress regression suite**

Run: `uv run pytest tests/featuregen/overlay/upload/ -q -k "egress or enrich_llm"`
Expected: PASS (existing per-item egress behaviour preserved).

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_llm.py tests/featuregen/overlay/upload/test_item_egress_table.py
git commit -m "feat(overlay): admit bounded column-descriptor list in per-item egress filter"
```

---

## Task 4: Structured (serialize-through) batch accept

**Why:** The batch harness is string-typed (`validate_batch_results` extracts one scalar `str(entry.get(out_key,""))`). A table synthesis result is a structured object. Keep the harness string-typed at the OUTCOME level (`BatchItemOutcome`/`BatchCallResult` unchanged), but add an `extract(entry) -> str` hook (serialize the per-item `synthesis` object to canonical JSON) and a `ref_aware` flag (call `accept(raw, ref)` so per-table column validation runs in the harness). Both `validate_batch_results` **and** its two callers in the chain — `audited_batch_call` (enrich_llm.py) and `run_batched` (enrich_batch.py) — gain and forward these two kwargs; defaults keep Pass A byte-for-byte. `run_batched` is therefore NOT pure reuse (its signature grows two defaulted kwargs) — see the call graph in Files.

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich_batch.py` — `validate_batch_results` gains `extract`/`ref_aware`; `run_batched` (`:123`) forwards them; `_single_fallback` (`:108`) skips ref_aware tasks.
- Modify: `src/featuregen/overlay/upload/enrich_llm.py:265` — `audited_batch_call` (the REAL intermediary; `run_batched` calls it, not `validate_batch_results` directly) gains + forwards `extract`/`ref_aware`.
- Test: `tests/featuregen/overlay/upload/test_validate_batch_structured.py`

**Call graph (verify before editing):** `run_batched` (enrich_batch.py:123) → `audited_batch_call` (enrich_llm.py:265) → `validate_batch_results` (called at enrich_llm.py:302 and :313). `run_batched` does NOT call `validate_batch_results` directly. So the kwargs must be threaded through `audited_batch_call` — this is the load-bearing wiring the v1 plan missed. All three signature changes default to `extract=None, ref_aware=False`, keeping Pass A byte-for-byte.

**Interfaces:**
- Consumes: `Accept`, `BatchItemOutcome`, the `EXTRA/DUPLICATE/BLANK/MISSING/VALID/INVALID` markers.
- Produces: `validate_batch_results(items, results, out_key, accept, *, extract=None)`. When `extract` is `None`, behaviour is unchanged (`str(entry.get(out_key,"")).strip()`). When supplied, `raw = extract(entry)`; the ref/dup/extra/missing bookkeeping and the blank/`accept` classification are identical. `extract` must not raise on a well-formed entry; a `None`/empty return is treated as BLANK.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_validate_batch_structured.py
import json
from featuregen.overlay.upload.enrich_batch import BatchItem, validate_batch_results


def _accept_json(raw):
    obj = json.loads(raw)
    if not obj.get("grain_columns"):
        return None, "no_grain"
    return raw, "valid"


def test_structured_extract_flows_through_accept():
    items = [BatchItem("txn", {})]
    results = [{"ref": "txn", "synthesis": {"grain_columns": ["id"], "table_role": "fact"}}]
    outcomes = validate_batch_results(
        items, results, "synthesis", _accept_json,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True),
    )
    assert outcomes[0].status == "valid"
    assert json.loads(outcomes[0].value)["grain_columns"] == ["id"]


def test_structured_missing_and_invalid_still_classified():
    items = [BatchItem("txn", {}), BatchItem("dim", {})]
    results = [{"ref": "txn", "synthesis": {"grain_columns": []}}]  # invalid; dim missing
    outcomes = {o.ref: o.status for o in validate_batch_results(
        items, results, "synthesis", _accept_json,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True))}
    assert outcomes["txn"] == "invalid_value"
    assert outcomes["dim"] == "missing"


def test_default_scalar_path_unchanged():
    items = [BatchItem("txn", {})]
    results = [{"ref": "txn", "concept": "amount"}]
    outcomes = validate_batch_results(items, results, "concept", lambda r: (r, "ok"))
    assert outcomes[0].status == "valid" and outcomes[0].value == "amount"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_validate_batch_structured.py -q`
Expected: FAIL — `TypeError: validate_batch_results() got an unexpected keyword argument 'extract'`.

- [ ] **Step 3: Implement the `extract` hook**

In `src/featuregen/overlay/upload/enrich_batch.py`, change the signature and the one extraction line:

```python
def validate_batch_results(items: list[BatchItem], results: list[dict], out_key: str,
                           accept: Accept, *, extract=None, ref_aware: bool = False
                           ) -> list[BatchItemOutcome]:
    """Classify every returned entry against the expected ref-set (spec C2): valid / invalid_value /
    blank / duplicate / extra, and every unreturned ref as missing. Nothing is silently collapsed.

    ``extract(entry) -> str`` overrides scalar out-key extraction so a STRUCTURED per-item result
    (e.g. a nested ``synthesis`` object) can be serialized to a canonical string. When
    ``ref_aware`` is set, ``accept`` is called as ``accept(raw, ref)`` so per-item validation that
    depends on the item's identity (e.g. "grain columns must be columns OF THIS table") is done
    HERE and yields a proper ``INVALID`` outcome — never accepted-then-post-filtered. Defaults keep
    the scalar ``accept(raw)`` path byte-for-byte for Pass A."""
    expected = {it.ref for it in items}
    seen: set[str] = set()
    outcomes: list[BatchItemOutcome] = []
    for entry in results:
        ref = entry.get("ref")
        raw = (extract(entry) if extract is not None
               else str(entry.get(out_key, "")).strip())
        if ref not in expected:
            outcomes.append(BatchItemOutcome(str(ref), EXTRA, None, (EXTRA,)))
            continue
        if ref in seen:
            outcomes.append(BatchItemOutcome(ref, DUPLICATE, None, (DUPLICATE,)))
            continue
        seen.add(ref)
        if not raw:
            outcomes.append(BatchItemOutcome(ref, BLANK, None, (BLANK,)))
            continue
        value, reason = accept(raw, ref) if ref_aware else accept(raw)
        if value is None:
            outcomes.append(BatchItemOutcome(ref, INVALID, None, (reason,)))
        else:
            outcomes.append(BatchItemOutcome(ref, VALID, value, (VALID,)))
    for ref in expected - seen:
        outcomes.append(BatchItemOutcome(ref, MISSING, None, (MISSING,)))
    return outcomes
```

- [ ] **Step 3b: Thread `extract`/`ref_aware` through `audited_batch_call` and `run_batched`**

The kwargs are useless unless they reach `validate_batch_results`. The real caller is `audited_batch_call`, NOT `run_batched` directly.

In `enrich_llm.py`, `audited_batch_call` (`:265`) — add `*, ..., extract=None, ref_aware=False` to the signature and forward to BOTH `validate_batch_results` calls:

```python
def audited_batch_call(conn, client, *, task, prompt_id, schema_id, shared_metadata, items,
                       out_key, instruction, accept, actor=None, extract=None, ref_aware=False):
    ...
    if not included:
        return BatchCallResult(tuple(egress_outcomes), 0, 0, 0)   # (the [] branch, ~:302 — no results to validate)
    ...
    outcomes = validate_batch_results(included, results, out_key, accept,
                                      extract=extract, ref_aware=ref_aware)   # ~:313
```

In `enrich_batch.py`, `run_batched` (`:123`) — add `extract=None, ref_aware=False` to the signature and forward them at the `audited_batch_call` invocation (~:147). In `_single_fallback` (`:108`), the flat single schema has no `synthesis` wrapper and the ref-aware accept needs `(raw, ref)`; the simplest safe fix is to **skip the single-fallback path for ref_aware tasks** (return the MISSING outcomes unchanged) so Pass A behaviour is untouched and Pass B never hits the mismatched flat path:

```python
def _single_fallback(conn, client, *, ..., accept, ref_aware=False, ...):
    if ref_aware:
        return leftovers_as_missing   # structured tasks: no single-call fallback in Phase 2
    ...  # existing Pass A path unchanged
```

> Defaults (`extract=None, ref_aware=False`) keep every Pass A call byte-for-byte. Add a test that drives `run_batched` end-to-end with a fake client returning a nested `{"results":[{"ref":"txn","synthesis":{...}}]}` and asserts the extractor is actually invoked (a canonical JSON string, not `str(dict)`, reaches the accept).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_validate_batch_structured.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the batch harness regression suite**

Run: `uv run pytest tests/featuregen/overlay/upload/test_enrich_batch.py -q`
Expected: PASS (default scalar path unchanged).

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/enrich_batch.py tests/featuregen/overlay/upload/test_validate_batch_structured.py
git commit -m "feat(overlay): optional structured extractor in validate_batch_results"
```

---

## Task 5: Pass B per-table input assembler

**Why:** Pass B needs one `BatchItem` per table whose metadata carries the table's columns with their Pass A enrichment (concept + drafted definition), sanitized for egress. This joins `CanonicalRow` (table/column/type/definition) with `enrich_concepts{content_hash:concept}` and `draft_definitions{content_hash:definition}` by `content_hash`, and strips sample values.

**Files:**
- Create: `src/featuregen/overlay/upload/table_synth.py` — `assemble_table_items(...)`.
- Test: `tests/featuregen/overlay/upload/test_table_synth_assemble.py`

**Interfaces:**
- Consumes: `BatchItem` (`enrich_batch.py`); `content_hash` (`overlay/upload/enrich.py` / wherever Pass A keys — same helper `build_graph` uses at ingest.py:120); `strip_sample_values` (`sample_parser.py`); `CanonicalRow` (`canonical.py`).
- Produces: `assemble_table_items(rows, *, concepts, definitions) -> list[BatchItem]` — one `BatchItem(ref=table_name, metadata={"table": table, "column_profiles": [...]})`, each descriptor `{column, type, concept?, business_definition?}` with `business_definition = strip_sample_values(...)`; only non-empty keys included; egress-admissible per Task 3.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_table_synth_assemble.py
from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich_llm import _item_egress_ok
from featuregen.overlay.upload.table_synth import assemble_table_items
from featuregen.overlay.upload.enrich import content_hash  # the Pass A content-hash key


def _row(table, column, type_="string", definition=""):
    return CanonicalRow(table=table, column=column, type=type_, definition=definition,
                        sensitivity="", is_grain=False, as_of=False, as_of_basis="",
                        cardinality="", additivity="", unit="", currency="", entity="",
                        joins_to="")


def test_one_item_per_table_egress_admissible():
    rows = [_row("txn", "id"), _row("txn", "amt"), _row("cust", "cust_id")]
    concepts = {content_hash(rows[1]): "monetary_amount"}
    items = assemble_table_items(rows, concepts=concepts, definitions={})
    assert {it.ref for it in items} == {"txn", "cust"}
    txn = next(it for it in items if it.ref == "txn")
    assert txn.metadata["table"] == "txn"
    assert {d["column"] for d in txn.metadata["column_profiles"]} == {"id", "amt"}
    assert any(d.get("concept") == "monetary_amount" for d in txn.metadata["column_profiles"])
    assert _item_egress_ok(txn.metadata) is True   # <-- the egress contract from Task 3


def test_curated_definition_is_sample_stripped():
    # the CURATED definition (from the sidecar/draft) rides as business_definition, sample-stripped
    rows = [_row("txn", "acct")]
    curated = {content_hash(rows[0]):
               "account number; sample profile is NUMERIC, representative values such as 3708484836801"}
    items = assemble_table_items(rows, concepts={}, definitions=curated)
    desc = items[0].metadata["column_profiles"][0]
    assert "3708484836801" not in desc.get("business_definition", "")   # stripped


def test_uploader_raw_definition_never_egresses():
    # a TECHNICAL row's raw r.definition free-text (a name, a bare id) must NEVER reach the LLM (M4).
    rows = [_row("txn", "cust", definition="belongs to John Q. Public, ssn 123456789")]
    items = assemble_table_items(rows, concepts={}, definitions={})   # no curated definition
    desc = items[0].metadata["column_profiles"][0]
    assert "business_definition" not in desc                          # r.definition dropped entirely
    assert "123456789" not in str(desc) and "John" not in str(desc)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_synth_assemble.py -q`
Expected: FAIL — `ModuleNotFoundError: ...table_synth`.

- [ ] **Step 3: Implement the assembler**

Create `src/featuregen/overlay/upload/table_synth.py`:

```python
"""Pass B — per-table synthesis (spec §15.2). Proposes grain/availability as human-gated typed-fact
proposals and table_role/primary_entity as advisory field evidence. Never auto-confirms."""
from __future__ import annotations

from featuregen.overlay.upload.canonical import CanonicalRow
from featuregen.overlay.upload.enrich import content_hash
from featuregen.overlay.upload.enrich_batch import BatchItem
from featuregen.overlay.upload.sample_parser import strip_sample_values


def _descriptor(r: CanonicalRow, concept: str | None, definition: str | None) -> dict:
    desc: dict = {"column": r.column, "type": r.type or ""}
    if concept:
        desc["concept"] = concept
    # CRITICAL (M4 egress rule): source business_definition ONLY from the CURATED `definition` (the
    # glossary sidecar meaning / Pass A draft) — NEVER from `r.definition`, the uploader's raw
    # free-text cell. enrich.py:_concept_metadata forbids egressing a technical row's r.definition;
    # we mirror that exactly. Even the curated text is sample-value-stripped as defence-in-depth.
    if definition:
        cleaned = strip_sample_values(definition)
        if cleaned:
            desc["business_definition"] = cleaned[:200]
    return desc


def assemble_table_items(rows: list[CanonicalRow], *, concepts: dict[str, str] | None,
                         definitions: dict[str, str] | None) -> list[BatchItem]:
    """One BatchItem per table; metadata carries each column's enriched, egress-safe descriptor."""
    # Pass A stages are savepointed and may fail, leaving concepts/definitions None (ingest.py:581).
    # Degrade to empty enrichment rather than AttributeError on None.get(...).
    concepts = concepts or {}
    definitions = definitions or {}
    by_table: dict[str, list[CanonicalRow]] = {}
    for r in rows:
        by_table.setdefault(r.table, []).append(r)
    items: list[BatchItem] = []
    for table, trows in by_table.items():
        profiles = [
            _descriptor(r, concepts.get(content_hash(r)), definitions.get(content_hash(r)))
            for r in trows
        ]
        items.append(BatchItem(ref=table, metadata={"table": table, "column_profiles": profiles}))
    return items
```

> **Implementer note:** confirm the exact `CanonicalRow` field list and the `content_hash` import location by reading `overlay/upload/canonical.py` and `overlay/upload/enrich.py` (the same `content_hash` used at `build_graph`/`ingest.py:120`). If `strip_sample_values` returns the text unchanged when there is nothing to strip, the second test still holds. Adjust the `_row` test factory to the real `CanonicalRow` constructor.
>
> **Descriptor enrichment (should-fix — do it if the data is present):** grain/as-of proposals are much stronger with role signals. When `CanonicalRow` (or Pass A evidence in scope) carries them, add these SHORT scalars to each descriptor — the Task-3 egress filter already admits them: `identifier_role` (an id-like column is grain-eligible), `temporal_role` (a time column is as-of-eligible), `semantic_type`, `entity`. Add table-level context to `BatchItem.metadata` when the glossary sidecar has it — `data_domain`, `bian_path`, `fibo_path` are already in `_ITEM_META_ALLOWED`. Keep every value bounded (≤200) and run any free-text through `strip_sample_values`. If a signal is not available in Phase 1's output, omit it — do not fabricate a field.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_synth_assemble.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/table_synth.py tests/featuregen/overlay/upload/test_table_synth_assemble.py
git commit -m "feat(overlay): Pass B per-table input assembler (egress-safe descriptors)"
```

---

## Task 6: Pass B synthesis driver + dict-shaped validator

**Why:** Drive the governed batch call (`run_batched(short="table_synth", ...)`) over the assembled table items and validate each structured result into a canonical grain/availability candidate mapped to the fact value shapes. All LLM-proposed grain columns must be real columns of the table; `as_of_column` must be a real column; `as_of_basis` must be in the enum. Anything else → INVALID (dropped, counted — never a bad proposal).

**Files:**
- Modify: `src/featuregen/overlay/upload/table_synth.py` — add `make_ref_accept(...)` + `synthesize_tables(...)`.
- Test: `tests/featuregen/overlay/upload/test_table_synth_driver.py`

**Interfaces:**
- Consumes: `run_batched` (`enrich_batch.py`), `mode`/`budget`/`max_items`/`max_input_tokens` (`enrich_config.py`), `ENRICHMENT_RUN_ID`/`_ENRICH_ACTOR` (`enrich_llm.py`).
- Produces:
  - `make_ref_accept(columns_by_table) -> Callable[[str, str], tuple[str|None, str]]` — a **ref-aware** accept (`accept(raw, ref)`, `ref`=table) validating a serialized `synthesis` JSON against that table's real columns; returns canonical JSON string or `(None, reason)`. Used via `validate_batch_results(..., ref_aware=True)`.
  - `synthesize_tables(conn, client, items, *, columns_by_table, actor) -> dict[str, dict]` — returns `{table: synthesis_dict}` for every VALID result. `synthesis_dict` = `{"grain": {...}|None, "availability_time": {...}|None, "table_role": str|None, "primary_entity": str|None, "event_or_snapshot": str|None}`, where `grain`/`availability_time` are already in `FACT_VALUE_SCHEMAS` shape (`{columns, is_unique}` / `{column, basis}`). Passes `extract=`/`ref_aware=True` into `run_batched`.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_table_synth_driver.py
import json
from featuregen.overlay.upload.table_synth import make_ref_accept


def _syn(**kw):
    base = {"grain_columns": [], "as_of_column": None, "as_of_basis": None,
            "primary_entity": None, "table_role": None, "event_or_snapshot": None}
    base.update(kw)
    return json.dumps(base, sort_keys=True)


def test_valid_grain_maps_to_fact_shape():
    accept = make_ref_accept({"txn": {"id", "amt", "posted_at"}})
    val, reason = accept(_syn(grain_columns=["id"], as_of_column="posted_at",
                              as_of_basis="posted_at", table_role="fact"), "txn")
    out = json.loads(val)
    assert out["grain"] == {"columns": ["id"], "is_unique": True}   # the proposed CLAIM
    assert out["availability_time"] == {"column": "posted_at", "basis": "posted_at"}
    assert out["table_role"] == "fact"


def test_grain_column_not_in_table_is_rejected():
    accept = make_ref_accept({"txn": {"id"}})
    val, reason = accept(_syn(grain_columns=["ghost"]), "txn")
    assert val is None and reason == "grain_col_not_in_table"


def test_as_of_column_not_in_table_is_rejected():
    accept = make_ref_accept({"txn": {"id"}})
    val, reason = accept(_syn(grain_columns=["id"], as_of_column="ghost", as_of_basis="posted_at"),
                         "txn")
    assert val is None and reason == "as_of_col_not_in_table"


def test_abstention_empty_grain_is_skipped_not_guessed():
    accept = make_ref_accept({"txn": {"id"}})
    val, reason = accept(_syn(grain_columns=[]), "txn")
    assert val is None and reason == "empty_synthesis"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_synth_driver.py -q`
Expected: FAIL — `ImportError: cannot import name 'make_ref_accept'`.

- [ ] **Step 3: Implement `make_ref_accept` + `synthesize_tables`**

Append to `src/featuregen/overlay/upload/table_synth.py`. The accept is **ref-aware** (`accept(raw, ref)`, `ref`=table name) so column validation runs *inside* the batch harness — a grain naming a non-existent column becomes an `INVALID` outcome, never accepted-then-dropped:

```python
import json

from featuregen.overlay.upload.enrich_batch import BatchItem, run_batched

_VALID_BASIS = {"posted_at", "ingested_at"}  # lag-free bases only (event_time_plus_lag needs lag_hours)


def make_ref_accept(columns_by_table: dict[str, set[str]]):
    """A ref-aware accept for `validate_batch_results(..., ref_aware=True)`. `ref` is the table name;
    validate the serialized `synthesis` against THAT table's real columns."""
    def accept(raw: str, ref: str) -> tuple[str | None, str]:
        cols = columns_by_table.get(ref, set())
        try:
            s = json.loads(raw)
        except (ValueError, TypeError):
            return None, "unparseable"
        grain_cols = [c for c in (s.get("grain_columns") or []) if isinstance(c, str)]
        if any(c not in cols for c in grain_cols):
            return None, "grain_col_not_in_table"
        as_of_col = s.get("as_of_column")
        as_of_basis = s.get("as_of_basis")
        # `is_unique=True` is the CLAIM being proposed (these columns are asserted to identify a row),
        # NOT empirical proof — there is no profiling in Phase 2. Human confirmation IS the uniqueness
        # attestation; the proposal's LLM origin (proposed_by=service actor) is what a reviewer sees.
        # The fact schema {columns,is_unique} forbids a caveat field, so origin is surfaced via the
        # worklist, not the value. An empty grain_columns == the model ABSTAINING (skip, not error).
        grain = {"columns": grain_cols, "is_unique": True} if grain_cols else None
        availability = None
        if as_of_col is not None:
            if as_of_col not in cols:
                return None, "as_of_col_not_in_table"
            if as_of_basis not in _VALID_BASIS:
                return None, "as_of_basis_invalid"
            availability = {"column": as_of_col, "basis": as_of_basis}
        if grain is None and availability is None:
            return None, "empty_synthesis"    # abstention / nothing proposed -> skipped-loud
        out = {"grain": grain, "availability_time": availability,
               "table_role": s.get("table_role"), "primary_entity": s.get("primary_entity"),
               "event_or_snapshot": s.get("event_or_snapshot")}
        return json.dumps(out, sort_keys=True), "valid"
    return accept


def synthesize_tables(conn, client, items: list[BatchItem], *, columns_by_table, actor
                      ) -> dict[str, dict]:
    """Run the governed batch synthesis; return {table: synthesis_dict} for VALID results only.
    Validation is done INSIDE run_batched via the ref-aware accept — this function does no
    post-filtering (an INVALID synthesis never reaches here)."""
    accept = make_ref_accept(columns_by_table)
    resolved = run_batched(
        conn, client, short="table_synth", task="table_synth",
        prompt_id="overlay_table_synth_v1", schema_id="overlay_table_synth_batch",
        shared_metadata={}, items=items, out_key="synthesis",
        instruction=_INSTRUCTION, accept=accept, actor=actor,
        extract=lambda e: json.dumps(e.get("synthesis"), sort_keys=True), ref_aware=True,
    )
    return {table: json.loads(raw) for table, raw in resolved.items()}


_INSTRUCTION = (
    "For each table, identify: the grain (the minimal set of columns whose combination uniquely "
    "identifies one row) — RETURN AN EMPTY grain_columns list if you cannot determine it, do not "
    "guess; the as-of/availability column and its basis (posted_at|ingested_at); "
    "the primary business entity; the table role; and whether it is an event or snapshot table. "
    "Only name columns that appear in the provided column list."
)
```

> **Abstention (must-fix #5):** the instruction tells the model to return `[]` when grain is undeterminable; `make_ref_accept` maps an all-abstained result to `empty_synthesis` (an INVALID/skip, counted by reason — see Task 7 counters), NOT a hallucinated grain. Because validation is ref-aware and in-harness, `run_batched`'s own `INVALID` bucket carries `grain_col_not_in_table`/`as_of_col_not_in_table`/`as_of_basis_invalid`/`empty_synthesis` — inspect them via the batch outcome for the malformed-by-reason counters.
>
> **Implementer note:** confirm `run_batched` now accepts `extract`/`ref_aware` (Task 4 threaded them). The driver-level test (Step 5) uses a fake `client` returning a canned `{"results":[{"ref":"txn","synthesis":{...}}]}` to prove one full pass end-to-end, including that a canned `grain_columns:["ghost"]` yields an `INVALID` outcome and never appears in the returned dict.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_synth_driver.py -q`
Expected: PASS (4 passed).

- [ ] **Step 5: Add and run the end-to-end driver test (fake client)**

Write `test_synthesize_tables_end_to_end` using a fake LLM client returning a canned `{"results":[{"ref":"txn","synthesis":{...}}]}`; assert `synthesize_tables` returns `{"txn": {...}}` with fact-shaped grain. Run it.

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/table_synth.py tests/featuregen/overlay/upload/test_table_synth_driver.py
git commit -m "feat(overlay): Pass B synthesis driver + column-validated accept"
```

---

## Task 7: Propose-only fact emission + ingest wiring

**Why:** Turn each table's synthesis into governed proposals. Grain/availability → `propose_fact` (PROPOSED-only, human-gated), keyed via `table_ref(source, table)` so the fact_key matches `_assert_fact`/readiness. **Skip** any fact whose stream already exists (a declared/structural grain from `_assert_fact`, or an already-pending proposal — never propose over it). `table_role`/`primary_entity` → advisory `record_field_evidence` (LLM producer). Fail-soft, adapter-gated, behind `OVERLAY_TABLE_SYNTH`.

**Files:**
- Modify: `src/featuregen/overlay/upload/table_synth.py` — add `_propose_table_facts(...)`.
- Modify: `src/featuregen/overlay/upload/ingest.py:~619` — call it beside `_propose_governed_joins`, behind the flag.
- Test: `tests/featuregen/overlay/upload/test_table_synth_propose.py`

**Interfaces:**
- Consumes: `propose_fact` + `Command` (`overlay/commands.py` / `contracts.envelopes` — Command needs all 6 fields), `proposal_fingerprint` (`overlay/identity.py`), `table_ref` (`upload_catalog.py`), `fact_key`/`load_fact`/`fold_overlay_state` (`overlay/identity.py`,`store.py`,`state.py`), `_write_producer_field` (`overlay/upload/ingest.py` — the SAME producer-scoped-staleness helper Pass A uses), `normalize_ref` (`object_ref.py`), `EvidenceProducer`/`AssertionStrength` (`evidence.py`), `ENRICHMENT_RUN_ID`/`_ENRICH_ACTOR` (`enrich_llm.py`), `mint_id` (`overlay/identity.py`), `table_synth_enabled()` (Task 2).
- Produces: `_propose_table_facts(conn, source, syntheses, *, actor, source_snapshot_id) -> None` — fail-soft; proposes grain/availability, skipping QUIETLY only when the folded state is VERIFIED/pending; records advisory table-field evidence via `_write_producer_field` (producer-scoped staleness + snapshot reuse). `actor` must be the service `_ENRICH_ACTOR`.

- [ ] **Step 0: Author the shared test conftest + fixtures (first consumer)**

The tests across Tasks 7/9/10/11/12 depend on fixtures/helpers that DO NOT EXIST yet. Create `tests/featuregen/overlay/upload/conftest.py` now (it sits under the existing `tests/featuregen/overlay/conftest.py`, which autoregisters overlay commands/event types + the `catalog` fixture). Author:

```python
# tests/featuregen/overlay/upload/conftest.py
import pytest
from featuregen.contracts.envelopes import IdentityEnvelope
from featuregen.overlay.projection import OverlayProjection
from featuregen.projections.runner import run_projection
from featuregen.overlay.upload.upload_catalog import ensure_upload_catalog_adapter
# NOTE: confirm import paths — mint_test_identity in tests/featuregen/_helpers.py;
# confirm_fact/reject_fact commands; get open task via human_tasks SELECT.

@pytest.fixture
def overlay_conn(db):
    # `db` is the ephemeral-PG connection fixture (tests/featuregen/conftest.py). Register the
    # upload-context adapter so propose/confirm/expiry resolve one (else they RuntimeError/skip).
    ensure_upload_catalog_adapter()
    return db

@pytest.fixture
def service_actor():
    # a non-human service proposer (mirrors _ENRICH_ACTOR) so four-eyes holds vs a human confirmer
    return IdentityEnvelope(subject="featuregen-overlay-enrichment", actor_kind="service",
                            authenticated=True, auth_method="internal", role_claims=())

@pytest.fixture
def human_actor():
    # MUST hold platform-admin: grain/availability route to the platform-admin governance queue
    # (UploadContextAdapter.owner_of -> None), so a data_owner confirmer would be DENIED.
    from tests.featuregen._helpers import mint_test_identity
    return mint_test_identity(subject="user:admin", role_claims=("platform-admin",))

@pytest.fixture
def seeded_graph(db):
    # source "src", table "txn", columns id/amt (+ txn_id for the integration test), is_grain=false
    for col in ("id", "amt", "txn_id"):
        db.execute("INSERT INTO graph_node (catalog_source, object_ref, kind, table_name, "
                   "column_name, is_grain, is_as_of) VALUES ('src', %s, 'column', 'txn', %s, "
                   "false, false)", (f"public.txn.{col}", col))
    return db

def _open_grain_task(conn, source, table):
    # the gate task propose_fact opened for this table's grain (carries target_event_id)
    from featuregen.overlay.task_read import get_task_proposal
    ...  # SELECT task_id FROM human_tasks WHERE status='open'; match fact_type=='grain' & object_ref

def _confirm_grain(conn, source, table, columns, *, actor):
    from featuregen.contracts.envelopes import Command
    from featuregen.overlay.commands import confirm_fact  # confirm import path
    task_id, target_event_id, ref = _open_grain_task(conn, source, table)
    confirm_fact(conn, Command("confirm_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target_event_id,
         "value": {"columns": columns, "is_unique": True}}, actor, f"confirm-{target_event_id}"))
    run_projection(conn, OverlayProjection())   # CRITICAL: resolve_fact reads the projected read model

def _reject_grain(conn, source, table, *, actor):
    from featuregen.contracts.envelopes import Command
    from featuregen.overlay.commands import reject_fact
    task_id, target_event_id, ref = _open_grain_task(conn, source, table)
    reject_fact(conn, Command("reject_fact", "overlay_fact", None,
        {"ref": ref, "fact_type": "grain", "target_event_id": target_event_id}, actor,
        f"reject-{target_event_id}"))
    run_projection(conn, OverlayProjection())

def _reconfirm_grain(conn, source, table, columns, *, actor):
    # drive a VERIFIED fact to a NEW VERIFIED value (expiry/reverify override path). Read
    # confirmation_commands.py for the reverify value-override; then run_projection.
    ...
```

> **Implementer note:** this conftest is a Task-7 deliverable but consumed by Tasks 9–12. Confirm every import/signature against the real code before writing (the `Command` shape for confirm/reject; `mint_test_identity`'s params; the `db` fixture name; `human_tasks` columns `task_id`/`status`; `get_task_proposal`'s TypedDict). `fake_synth_client`/`glossary_rows`/`technical_rows` are authored in Task 6 (driver e2e) and Task 12 respectively; move them here if an earlier task needs them. Every `_confirm_grain`/`_reconfirm_grain`/`_reject_grain` MUST end with `run_projection(conn, OverlayProjection())` so `resolve_fact` (which reads `overlay_fact_state`, populated only by the projection) sees VERIFIED.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_table_synth_propose.py
# Uses the overlay conftest (ephemeral PG + registered test adapter/commands).
from featuregen.overlay.identity import fact_key
from featuregen.overlay.store import load_fact
from featuregen.overlay.state import fold_overlay_state
from featuregen.overlay.upload.upload_catalog import table_ref
from featuregen.overlay.upload.table_synth import _propose_table_facts


def test_grain_is_proposed_not_confirmed(overlay_conn, service_actor):
    syn = {"txn": {"grain": {"columns": ["id"], "is_unique": True},
                   "availability_time": None, "table_role": "fact", "primary_entity": "account"}}
    _propose_table_facts(overlay_conn, "src", syn, actor=service_actor, source_snapshot_id="snap-test")
    state = fold_overlay_state(load_fact(overlay_conn, fact_key(table_ref("src", "txn"), "grain")))
    assert state.status == "PROPOSED"   # never auto-confirmed


def test_existing_verified_grain_is_not_overwritten(overlay_conn, service_actor, human_actor):
    # simulate a declared/structural grain already VERIFIED (as _assert_fact would leave it)
    from featuregen.overlay.upload.ingest import _assert_fact
    _assert_fact(overlay_conn, "src", "txn", "grain",
                 {"columns": ["id"], "is_unique": True}, actor=human_actor)
    syn = {"txn": {"grain": {"columns": ["other"], "is_unique": True},
                   "availability_time": None, "table_role": None, "primary_entity": None}}
    _propose_table_facts(overlay_conn, "src", syn, actor=service_actor, source_snapshot_id="snap-test")
    state = fold_overlay_state(load_fact(overlay_conn, fact_key(table_ref("src", "txn"), "grain")))
    assert state.status == "VERIFIED" and state.value["columns"] == ["id"]  # untouched


def test_advisory_table_role_recorded_as_evidence(overlay_conn, service_actor):
    from featuregen.overlay.field_evidence import read_active_field_evidence
    from featuregen.overlay.upload.object_ref import normalize_ref
    syn = {"txn": {"grain": None, "availability_time": None,
                   "table_role": "fact", "primary_entity": "account"}}
    _propose_table_facts(overlay_conn, "src", syn, actor=service_actor, source_snapshot_id="snap-test")
    ref = normalize_ref("src", None, "txn")
    ev = read_active_field_evidence(overlay_conn, ref, "table_role")
    assert any(e.proposed_value == "fact" for e in ev)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_synth_propose.py -q`
Expected: FAIL — `ImportError: cannot import name '_propose_table_facts'`.

- [ ] **Step 3: Implement `_propose_table_facts`**

Append to `src/featuregen/overlay/upload/table_synth.py` (lazy imports of the command stack, mirroring `_propose_governed_joins`):

```python
from featuregen.observability import counters, logger  # match ingest.py's import path

# The folded fact states in which a Pass B proposal is SKIPPED QUIETLY — a stronger/active claim
# already governs this key: VERIFIED (a declared/structural or human-confirmed grain — Pass B must
# never contest it), or a still-pending proposal/partial (already in the queue). All OTHER states
# (REJECTED / EXPIRED / STALE / empty) are handed to propose_fact, which adjudicates: it duplicate-
# denies an identical pending fingerprint, sticky-denies a re-proposed rejected fingerprint, and
# ALLOWS a genuinely new value after a terminal state. We never skip on raw stream existence (that
# suppressed every future proposal once a stream existed, even after rejection/expiry).
_SKIP_QUIET_STATES = frozenset({"VERIFIED", "PROPOSED", "PARTIALLY_CONFIRMED"})


def _active_skip_state(conn, ref, fact_type) -> str | None:
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact
    stream = load_fact(conn, fact_key(ref, fact_type))
    if not stream:
        return None
    status = fold_overlay_state(stream).status
    return status if status in _SKIP_QUIET_STATES else None


def _propose_table_facts(conn, source: str, syntheses: dict[str, dict], *, actor,
                         source_snapshot_id: str) -> None:
    """Route Pass B grain/availability candidates into governed PROPOSED-only facts and advisory
    table-field evidence. Fail-soft (never aborts the upload). Skips QUIETLY only when a stronger
    active claim governs the key (VERIFIED / pending proposal); otherwise lets propose_fact
    adjudicate re-proposal after a terminal state, logging any denial as a conflict diagnostic.

    ``actor`` MUST be the service actor (``_ENRICH_ACTOR``) so a human confirmer later satisfies
    four-eyes. ``source_snapshot_id`` keys producer-scoped staleness for the advisory evidence (a
    NOT-NULL column)."""
    from featuregen.contracts.envelopes import Command
    from featuregen.overlay.catalog import current_catalog_adapter
    from featuregen.overlay.commands import propose_fact
    from featuregen.overlay.evidence import AssertionStrength, EvidenceProducer
    from featuregen.overlay.identity import proposal_fingerprint
    from featuregen.overlay.upload.enrich_llm import ENRICHMENT_RUN_ID
    from featuregen.overlay.upload.ingest import _write_producer_field
    from featuregen.overlay.upload.object_ref import normalize_ref
    from featuregen.overlay.upload.upload_catalog import table_ref

    try:
        current_catalog_adapter()
    except RuntimeError:
        counters.incr("overlay.table_synth.skipped_no_adapter")
        logger.warning("OVERLAY_TABLE_SYNTH on but no catalog adapter registered — skipping.")
        return

    for table, syn in syntheses.items():
        ref = table_ref(source, table)
        for fact_type in ("grain", "availability_time"):
            value = syn.get(fact_type)
            if value is None:
                continue
            skip_state = _active_skip_state(conn, ref, fact_type)
            if skip_state is not None:
                # a stronger/active claim governs this key — Pass B does not contest it
                counters.incr(f"overlay.table_synth.{fact_type}.skipped_{skip_state.lower()}")
                continue
            try:
                # Command needs ALL 6 fields (envelopes.py); mirror _propose_governed_joins exactly.
                result = propose_fact(conn, Command(
                    "propose_fact", "overlay_fact", None,
                    {"ref": ref, "fact_type": fact_type, "proposed_value": value},
                    actor, proposal_fingerprint(value)))
                if result.accepted:
                    counters.incr(f"overlay.table_synth.{fact_type}.proposed")
                else:
                    # propose_fact adjudicated a deny (duplicate fingerprint, sticky-rejected, or a
                    # non-terminal race) — a conflict DIAGNOSTIC, not a silent drop.
                    counters.incr(f"overlay.table_synth.{fact_type}.denied")
                    logger.info("table_synth %s proposal denied for %s.%s: %s",
                                fact_type, source, table, result.denied_reason)
            except Exception:   # noqa: BLE001 — advisory: a proposal error never fails an upload
                counters.incr(f"overlay.table_synth.{fact_type}.error")
                logger.exception("table_synth %s proposal errored for %s.%s", fact_type, source, table)
        # advisory table fields -> field evidence via the SAME helper Pass A uses (_write_producer_field:
        # producer-scoped staleness + snapshot reuse + all required args incl. source_snapshot_id/input_hash).
        # RECOMMENDATION-ceilinged in Task 8. _write_producer_field is INSERT-guarded internally; a write
        # error here is still contained by the Step-5 savepoint+except.
        logical_ref = normalize_ref(source, None, table)
        for field_name in ("table_role", "primary_entity", "event_or_snapshot"):
            v = syn.get(field_name)
            if v:
                _write_producer_field(
                    conn, logical_ref=logical_ref, field_name=field_name, value=v,
                    producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
                    producer_ref=ENRICHMENT_RUN_ID, snapshot_id=source_snapshot_id, material=v)
```

> **Implementer note:** confirm the exact `record_field_evidence` signature and the `Command` import path by reading `field_evidence.py` and the Pass A item-level evidence call in `enrich.py`; match them verbatim (the Phase-1 call already writes item-level concept evidence with producer-scoped staleness — reuse that exact call shape). Confirm `counters`/`logger` import path from `ingest.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_synth_propose.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Wire the ingest call behind the (Task 2) feature switch**

`table_synth_enabled()` was defined in Task 2. In `ingest.py`, after `build_graph(...)` and beside `_propose_governed_joins(conn, vr.good, actor=actor)` (~line 619), add — guarded by the flag AND a live `client` (Pass B needs the LLM):

```python
        if table_synth_enabled() and client is not None:
            from featuregen.overlay.upload.enrich_llm import _ENRICH_ACTOR
            from featuregen.overlay.upload.object_ref import normalize_ref
            from featuregen.overlay.upload.table_synth import (
                assemble_table_items, synthesize_tables, _propose_table_facts)
            try:
                # The savepoint CONTAINS the LLM + security-audit writes (exactly like the Pass A
                # stages above). A DB abort inside synthesize_tables/propose must not poison the tx and
                # roll back Pass A facts + the quarantine. try/except makes Pass B strictly advisory.
                with conn.transaction():
                    synth_snapshot = snapshot_id or mint_id("tsy")  # tech uploads have snapshot_id=None
                    items = assemble_table_items(vr.good, concepts=concepts, definitions=definitions)
                    cols = {t: {r.column for r in vr.good if r.table == t}
                            for t in {r.table for r in vr.good}}
                    syntheses = synthesize_tables(conn, client, items, columns_by_table=cols,
                                                  actor=actor)   # LLM-call attribution only
                    # Propose under the SERVICE actor so a human confirmer later satisfies four-eyes:
                    _propose_table_facts(conn, catalog_source, syntheses, actor=_ENRICH_ACTOR,
                                         source_snapshot_id=synth_snapshot)
                    # Project the advisory table fields' DISPLAY. resolve_and_project is otherwise
                    # called ONLY over glossary COLUMN refs (ingest.py:496); table refs need this
                    # explicit call or table_role/primary_entity/event_or_snapshot stay NULL forever.
                    table_refs = [normalize_ref(catalog_source, None, t)
                                  for t in sorted({r.table for r in vr.good})]
                    resolve_and_project(conn, source=catalog_source, logical_refs=table_refs, now=now)
            except Exception:  # noqa: BLE001 — advisory: Pass B never fails an upload; Pass A facts hold
                counters.incr("overlay.table_synth.error")
                logger.warning("advisory Pass B table synthesis failed for %r — Pass A facts + graph "
                               "intact", catalog_source, exc_info=True)
```

> **Implementer note:** `concepts`/`definitions`/`snapshot_id`/`now`/`mint_id`/`resolve_and_project` are all already in scope at this point in `ingest_upload` (Pass A stages at ~597/607; `snapshot_id` at :562; `resolve_and_project` imported at :31; `mint_id` used at :562) — reuse them, do NOT re-run Pass A. The fail-soft model is the **enrichment-stage pattern** (`try: with conn.transaction(): <stage> except Exception: log`, ingest.py:596-614) — NOT `_propose_governed_joins`, which has per-item try/except but **no savepoint** and would let a DB abort poison the tx. The LLM+audit call (`synthesize_tables`) MUST be inside the savepoint. Propose under `_ENRICH_ACTOR` (service actor → four-eyes holds when a human confirms). Confirm the exact `mint_id` import (`featuregen.overlay.identity` or wherever ingest.py:562 gets it).

- [ ] **Step 6: Run the propose + ingest suites**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_synth_propose.py tests/featuregen/overlay/upload/test_ingest.py -q`
Expected: PASS (flag default-off ⇒ ingest unchanged; propose path proven).

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/overlay/upload/table_synth.py src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_table_synth_propose.py
git commit -m "feat(overlay): Pass B propose-only table facts + fail-soft ingest wiring (default off)"
```

---

## Task 8: Advisory table-field policies + display projection (migration 0986)

**Why:** `table_role`/`primary_entity` must be SHOWN on the table graph_node but can NEVER be load-bearing (RECOMMENDATION ceiling). Register their policies, add the graph_node columns, and project their display via the existing `resolve_and_project` (which is ref-shape agnostic and already handles `column=None` table refs).

**Files:**
- Create: `src/featuregen/db/migrations/0986_graph_node_table_fields.sql`
- Modify: `src/featuregen/overlay/upload/field_policies.py` — add two `_POLICIES` entries.
- Modify: `src/featuregen/overlay/upload/field_resolution.py:86-102` — add to `_DISPLAY_COLUMN` + `_DECISION_LINK_COLUMN`.
- Test: `tests/featuregen/overlay/upload/test_table_advisory_fields.py`

**Interfaces:**
- Consumes: `_recommendation` helper (`field_policies.py:58`), `resolve_and_project` (`field_resolution.py:302`).
- Produces: `policy_for("table_role")`/`policy_for("primary_entity")` return RECOMMENDATION policies; `_DISPLAY_COLUMN`/`_DECISION_LINK_COLUMN` route them to new graph_node columns `table_role`,`primary_entity`,`table_role_decision_id`,`primary_entity_decision_id`.

- [ ] **Step 1: Write the migration**

`src/featuregen/db/migrations/0986_graph_node_table_fields.sql`:

```sql
-- Phase 2 (table facts): advisory table-level fields (display-only, RECOMMENDATION-ceilinged) and
-- the grain/as-of specialized-fact provenance link populated by the projection bridge (Task 9).
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS table_role text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS primary_entity text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS event_or_snapshot text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS table_role_decision_id text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS primary_entity_decision_id text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS event_or_snapshot_decision_id text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS grain_fact_event_id text;
ALTER TABLE graph_node ADD COLUMN IF NOT EXISTS availability_fact_event_id text;
```

- [ ] **Step 2: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_table_advisory_fields.py
from featuregen.overlay.field_authority import InfluenceTier, ResolutionMode
from featuregen.overlay.upload.field_policies import policy_for


def test_table_role_is_recommendation_ceilinged():
    p = policy_for("table_role")
    assert p is not None
    assert p.influence_max is InfluenceTier.RECOMMENDATION      # never load-bearing
    assert p.resolution_mode is ResolutionMode.GENERIC_FIELD


def test_primary_entity_is_recommendation_ceilinged():
    p = policy_for("primary_entity")
    assert p is not None and p.influence_max is InfluenceTier.RECOMMENDATION
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_advisory_fields.py -q`
Expected: FAIL — `policy_for("table_role")` returns `None`.

- [ ] **Step 4: Register the policies + display columns**

In `field_policies.py`, add to `_POLICIES` (reusing `_MEANING`'s lenient display rule via `_recommendation`):

```python
_TABLE_ADVISORY = _recommendation(
    display_rule=AnyOf((_LLM_PROPOSED, _SOURCE_PROPOSED, _SOURCE_ATTESTED, _HUMAN_CONFIRMED)),
    operational_rule=_SOURCE_OR_HUMAN,
)
_POLICIES: dict[str, FieldPolicy] = {
    # ...existing...
    "table_role": _TABLE_ADVISORY,
    "primary_entity": _TABLE_ADVISORY,
    "event_or_snapshot": _TABLE_ADVISORY,   # advisory: informs modelling, never load-bearing
}
```

In `field_resolution.py`, add the display + decision-link mappings:

```python
_DISPLAY_COLUMN: dict[str, str] = {
    "concept": "concept", "definition": "definition", "domain": "domain",
    "additivity": "additivity",
    "table_role": "table_role", "primary_entity": "primary_entity",
    "event_or_snapshot": "event_or_snapshot",
}
_DECISION_LINK_COLUMN: dict[str, str] = {
    # ...existing...
    "table_role": "table_role_decision_id",
    "primary_entity": "primary_entity_decision_id",
    "event_or_snapshot": "event_or_snapshot_decision_id",
}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_advisory_fields.py -q`
Expected: PASS (2 passed).

- [ ] **Step 6: Add a projection integration test**

Write a test that records `table_role="fact"` evidence for a table logical_ref, runs `resolve_and_project`, and asserts the table graph_node's `table_role` column shows `fact` while `is_feature_eligible` for the table returns False (display, never authority). Run it.

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/db/migrations/0986_graph_node_table_fields.sql src/featuregen/overlay/upload/field_policies.py src/featuregen/overlay/upload/field_resolution.py tests/featuregen/overlay/upload/test_table_advisory_fields.py
git commit -m "feat(overlay): advisory table_role/primary_entity fields (display-only) + migration 0986"
```

---

## Task 9: SPECIALIZED_FACT projection bridge (confirmed grain → graph_node)

**Why:** A confirmed (VERIFIED) grain/availability fact is the load-bearing truth (spec §5.3), but `confirm_fact` is catalog-agnostic and never touches graph_node. Build the bridge — modeled on `_resolve_sensitivity` (compute outside the generic resolver, write dedicated graph_node columns) — that reads `resolve_fact` (VERIFIED-only) per table and sets `is_grain`/`is_as_of` on the grain/as-of column nodes plus the provenance event id. Call it at end-of-ingest so a re-upload re-applies confirmed facts after `build_graph` wipes graph_node.

**Files:**
- Create: `src/featuregen/overlay/upload/table_fact_projection.py` — `project_table_facts(...)`.
- Modify: `src/featuregen/overlay/upload/ingest.py` — call `project_table_facts` at end of `ingest_upload`.
- Test: `tests/featuregen/overlay/upload/test_table_fact_projection.py`

**Interfaces:**
- Consumes: `resolve_fact` (`resolve.py:183`), `current_catalog_adapter` (`catalog.py`), `table_ref` (`upload_catalog.py`).
- Produces: `project_table_facts(conn, *, source, tables, now=None) -> None` — for each table, `resolve_fact(conn, adapter, table_ref(source,table), "grain")` (VERIFIED value or None); when present, `UPDATE graph_node SET is_grain=true, grain_fact_event_id=<confirmed_event_id> WHERE catalog_source=? AND table_name=? AND column_name = ANY(grain columns)`. Same for `availability_time` → `is_as_of` on the as-of column. A PROPOSED/absent fact projects nothing (fail-closed).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_table_fact_projection.py
from featuregen.overlay.upload.table_fact_projection import project_table_facts


def test_confirmed_grain_sets_is_grain_on_columns(overlay_conn, human_actor, seeded_graph):
    # seeded_graph: a source "src" with table "txn" columns id, amt (all is_grain=false)
    _confirm_grain(overlay_conn, "src", "txn", ["id"], actor=human_actor)   # helper -> VERIFIED
    project_table_facts(overlay_conn, source="src", tables=["txn"])
    rows = overlay_conn.execute(
        "SELECT column_name, is_grain FROM graph_node "
        "WHERE catalog_source='src' AND table_name='txn' AND kind='column'").fetchall()
    grain = {c for c, g in rows if g}
    assert grain == {"id"}


def test_proposed_but_unconfirmed_grain_projects_nothing(overlay_conn, service_actor, seeded_graph):
    from featuregen.overlay.upload.table_synth import _propose_table_facts
    _propose_table_facts(overlay_conn, "src",
                         {"txn": {"grain": {"columns": ["id"], "is_unique": True},
                                  "availability_time": None,
                                  "table_role": None, "primary_entity": None}},
                         actor=service_actor, source_snapshot_id="snap-test")
    project_table_facts(overlay_conn, source="src", tables=["txn"])
    rows = overlay_conn.execute(
        "SELECT is_grain FROM graph_node WHERE catalog_source='src' AND table_name='txn' "
        "AND kind='column'").fetchall()
    assert not any(g for (g,) in rows)   # PROPOSED is not load-bearing


def test_reprojection_clears_stale_grain_flags(overlay_conn, human_actor, seeded_graph):
    # THE idempotency guarantee: a confirmed grain that later CHANGES columns must not leave the old
    # column flagged. Confirm grain=[id], project; then confirm a replacement grain=[amt] (after the
    # first expires/re-verifies), re-project, and assert `id` is now false and `amt` is true.
    _confirm_grain(overlay_conn, "src", "txn", ["id"], actor=human_actor)
    project_table_facts(overlay_conn, source="src", tables=["txn"])
    _reconfirm_grain(overlay_conn, "src", "txn", ["amt"], actor=human_actor)  # helper -> new VERIFIED
    project_table_facts(overlay_conn, source="src", tables=["txn"])
    flags = dict(overlay_conn.execute(
        "SELECT column_name, is_grain FROM graph_node WHERE catalog_source='src' "
        "AND table_name='txn' AND kind='column'").fetchall())
    assert flags["id"] is False and flags["amt"] is True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_fact_projection.py -q`
Expected: FAIL — `ModuleNotFoundError: ...table_fact_projection`.

- [ ] **Step 3: Implement the bridge**

Create `src/featuregen/overlay/upload/table_fact_projection.py`:

```python
"""SPECIALIZED_FACT bridge: land a CONFIRMED (VERIFIED) grain/availability fact onto graph_node.
Modeled on field_resolution._resolve_sensitivity — computes outside the generic resolver and writes
dedicated graph_node columns. The load-bearing truth is the fact stream; this is its projection."""
from __future__ import annotations

from datetime import datetime

from featuregen.overlay.catalog import current_catalog_adapter
from featuregen.overlay.resolve import resolve_fact
from featuregen.overlay.upload.upload_catalog import table_ref


def project_table_facts_for_ref(conn, *, source: str, table: str,
                                now: datetime | None = None) -> None:
    """Project the CURRENT verified grain/availability for ONE table onto graph_node — IDEMPOTENTLY.

    CRITICAL: clears every prior is_grain/is_as_of + fact-event-id on this table's columns FIRST,
    then applies only what resolve_fact currently serves (VERIFIED). Without the clear, a grain that
    changed columns, expired, was rejected, or was replaced on re-verify would leave STALE true flags
    on old columns — a silent correctness rot. Set-only projection is not rebuild-safe; clear-then-set
    is. This single-table entry point is also what a future confirm-time hook calls (there is no
    confirm API today; see the scope boundary)."""
    adapter = current_catalog_adapter()
    # 1. Clear this table's specialized-fact projection (rebuild-safe reset).
    conn.execute(
        "UPDATE graph_node SET is_grain = false, grain_fact_event_id = NULL, "
        "is_as_of = false, availability_fact_event_id = NULL "
        "WHERE catalog_source = %s AND table_name = %s AND kind = 'column'",
        (source, table))
    ref = table_ref(source, table)
    # 2. Apply the CONFIRMED grain (VERIFIED only; PROPOSED/absent -> value None -> nothing set).
    grain = resolve_fact(conn, adapter, ref, "grain")
    if grain and grain.value is not None:
        cols = grain.value.get("columns", [])
        # ResolvedFact has NO confirmed_event_id attribute; a VERIFIED overlay fact carries it in
        # .provenance['confirmed_event_id'] (resolve.py _overlay_verified). getattr(...) would silently
        # write NULL — read provenance so the audit-link column is actually populated.
        conn.execute(
            "UPDATE graph_node SET is_grain = true, grain_fact_event_id = %s "
            "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
            "AND column_name = ANY(%s)",
            ((grain.provenance or {}).get("confirmed_event_id"), source, table, list(cols)))
    # 3. Apply the CONFIRMED availability.
    avail = resolve_fact(conn, adapter, ref, "availability_time")
    if avail and avail.value is not None:
        col = avail.value.get("column")
        conn.execute(
            "UPDATE graph_node SET is_as_of = true, availability_fact_event_id = %s "
            "WHERE catalog_source = %s AND table_name = %s AND kind = 'column' "
            "AND column_name = %s",
            ((avail.provenance or {}).get("confirmed_event_id"), source, table, col))


def project_table_facts(conn, *, source: str, tables, now: datetime | None = None) -> None:
    """Project every table's confirmed grain/availability. Idempotent per table (clear-then-set)."""
    for table in tables:
        project_table_facts_for_ref(conn, source=source, table=table, now=now)
```

> **Implementer note:** read `resolve_fact` (`resolve.py:183`): it serves a value ONLY on `VERIFIED`, and it reads the **`overlay_fact_state` read model** — which is written by `OverlayProjection`, NOT by `confirm_fact` (confirm only appends the event + arms expiry). So a test that does `_confirm_grain → project_table_facts` with no projection run sees `value=None` and projects nothing. **The `_confirm_grain`/`_reconfirm_grain` conftest helpers MUST drain the projection after `confirm_fact`** (`run_projection(conn, OverlayProjection())`, mirroring `tests/featuregen/overlay/_helpers.py`). Do NOT rewrite `project_table_facts` to fold the event stream — keeping it on `resolve_fact` preserves the VERIFIED-only/expiry-guarded semantics; the drain belongs in the confirm helper. If a synchronous confirm-then-project is ever wired in-process, a `run_projection` must precede `project_table_facts` there too. The confirmed event id lives in `resolve_fact(...).provenance['confirmed_event_id']` (NOT an attribute). `use_case` is `None` for grain/availability. The table-node's own `is_grain`/`is_as_of` stay hard-coded false at `graph.py:115` — grain lives on the COLUMN nodes, which is what feature-gen reads.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_fact_projection.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Call the bridge at end-of-ingest (re-apply on every upload)**

In `ingest_upload`, after the graph is built and Pass B has run (near the end, before `return IngestResult(...)`), re-project any already-confirmed facts onto the fresh graph_node — inside a **savepoint + except** so a projection DB fault can never poison the tx and roll back facts/quarantine (this runs even when the feature flag is off, so it must not be able to newly 500 a flag-off upload):

```python
        try:
            with conn.transaction():   # savepoint: a projection fault must not roll back facts
                project_table_facts(conn, source=catalog_source,
                                    tables=sorted({r.table for r in vr.good}))
        except Exception:  # noqa: BLE001 — advisory: re-projection never fails an upload
            counters.incr("overlay.table_fact_projection.error")
            logger.warning("advisory grain/as-of re-projection failed for %r — facts intact",
                           catalog_source, exc_info=True)
```

Import at top: `from featuregen.overlay.upload.table_fact_projection import project_table_facts`. This is unconditional (not flag-gated): a grain confirmed in a *prior* cycle must survive a `build_graph` rebuild even if `OVERLAY_TABLE_SYNTH` is off. `project_table_facts` itself resolves `current_catalog_adapter()`, which `ensure_upload_catalog_adapter()` (Task 1) registered at the top of `ingest_upload` — so it is always available here.

- [ ] **Step 6: Run the ingest + projection suites**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_fact_projection.py tests/featuregen/overlay/upload/test_ingest.py -q`
Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add src/featuregen/overlay/upload/table_fact_projection.py src/featuregen/overlay/upload/ingest.py tests/featuregen/overlay/upload/test_table_fact_projection.py
git commit -m "feat(overlay): SPECIALIZED_FACT bridge — confirmed grain/as-of -> graph_node, re-applied at ingest"
```

---

## Task 10: Readiness reads grain/availability fact state

**Why:** Today `_PHASE1_UNPROMOTED` is static (`grain`→always "missing"). Phase 2 adds `availability` and makes both requirements READ the table's fact state so the diagnostic flips missing → proposed → confirmed, using the existing status vocabulary (`CAUSE_PROPOSED_UNCONFIRMED` already exists).

**Files:**
- Modify: `src/featuregen/overlay/upload/readiness.py` — add `availability` to `_PHASE1_UNPROMOTED`; add `_table_fact_status(...)`; use it where the grain/availability requirement status is set.
- Test: `tests/featuregen/overlay/upload/test_readiness_table_facts.py`

**Interfaces:**
- Consumes: `fact_key` (`identity.py`), `load_fact` (`store.py`), `fold_overlay_state` (`state.py`), `table_ref` (`upload_catalog.py`), `CAUSE_NOT_PROMOTED`/`CAUSE_PROPOSED_UNCONFIRMED`.
- Produces: `_table_fact_status(conn, source, table, fact_type) -> Literal["missing","proposed","confirmed","conflicting"]`; the per-table grain/availability `ReadinessRequirement` now reflects it — VERIFIED→`confirmed`(non-blocking, satisfied), PROPOSED→`proposed`(non-blocking review, `CAUSE_PROPOSED_UNCONFIRMED`), none/terminal→`missing`(blocking, `CAUSE_NOT_PROMOTED`).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_readiness_table_facts.py
from featuregen.overlay.upload.readiness import _table_fact_status


def test_absent_grain_is_missing(overlay_conn):
    assert _table_fact_status(overlay_conn, "src", "txn", "grain")[0] == "missing"


def test_proposed_grain_is_proposed(overlay_conn, service_actor):
    from featuregen.overlay.upload.table_synth import _propose_table_facts
    _propose_table_facts(overlay_conn, "src",
                         {"txn": {"grain": {"columns": ["id"], "is_unique": True},
                                  "availability_time": None,
                                  "table_role": None, "primary_entity": None}},
                         actor=service_actor, source_snapshot_id="snap-test")
    status, cause = _table_fact_status(overlay_conn, "src", "txn", "grain")
    assert status == "proposed" and cause == "proposed_unconfirmed"


def test_confirmed_grain_is_confirmed(overlay_conn, human_actor):
    _confirm_grain(overlay_conn, "src", "txn", ["id"], actor=human_actor)  # helper -> VERIFIED
    assert _table_fact_status(overlay_conn, "src", "txn", "grain")[0] == "confirmed"


def test_rejected_grain_is_missing_but_distinct_cause(overlay_conn, service_actor, human_actor):
    from featuregen.overlay.upload.table_synth import _propose_table_facts
    from featuregen.overlay.upload.readiness import CAUSE_FACT_REJECTED
    _propose_table_facts(overlay_conn, "src",
                         {"txn": {"grain": {"columns": ["id"], "is_unique": True},
                                  "availability_time": None,
                                  "table_role": None, "primary_entity": None}},
                         actor=service_actor, source_snapshot_id="snap-test")
    _reject_grain(overlay_conn, "src", "txn", actor=human_actor)   # helper -> REJECTED
    status, cause = _table_fact_status(overlay_conn, "src", "txn", "grain")
    assert status == "missing" and cause == CAUSE_FACT_REJECTED   # not "never proposed"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_readiness_table_facts.py -q`
Expected: FAIL — `ImportError: cannot import name '_table_fact_status'`.

- [ ] **Step 3: Implement `_table_fact_status` + wire it**

In `readiness.py`:

```python
_PHASE1_UNPROMOTED: tuple[tuple[str, str], ...] = (
    ("grain", "structural_or_human"),
    ("availability", "structural_or_human"),   # Phase 2 addition
    ("join", "approved_join"),
)

_FACT_TYPE_BY_REQUIREMENT = {"grain": "grain", "availability": "availability_time"}

# Granular causes for the non-terminal lifecycle states (must not collapse to "missing"). The
# STATUS stays in the 4-value vocabulary (confirmed/proposed/missing/conflicting) the type allows;
# the CAUSE distinguishes WHY so the diagnostic is honest. Only VERIFIED is feature-ready.
CAUSE_FACT_EXPIRED = "fact_expired_awaiting_reverify"
CAUSE_FACT_STALE = "fact_staled_awaiting_reverify"
CAUSE_FACT_REJECTED = "proposal_rejected"


def _table_fact_status(conn, source, table, requirement) -> tuple[str, str]:
    """Map the table's overlay fact stream to (readiness_status, cause). readiness_status is one of
    the 4 allowed values; cause carries the granular lifecycle reason. Only VERIFIED is ready."""
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact
    from featuregen.overlay.upload.upload_catalog import table_ref
    fact_type = _FACT_TYPE_BY_REQUIREMENT.get(requirement)
    if fact_type is None:
        return "missing", CAUSE_NOT_PROMOTED
    stream = load_fact(conn, fact_key(table_ref(source, table), fact_type))
    if not stream:
        return "missing", CAUSE_NOT_PROMOTED
    status = fold_overlay_state(stream).status
    if status == "VERIFIED":
        return "confirmed", CAUSE_NOT_PROMOTED           # satisfied; not a blocker
    if status in ("PROPOSED", "PARTIALLY_CONFIRMED"):
        return "proposed", CAUSE_PROPOSED_UNCONFIRMED     # in the review queue
    if status == "REJECTED":
        return "missing", CAUSE_FACT_REJECTED             # NOT ready; distinct from never-proposed
    if status == "EXPIRED":
        return "proposed", CAUSE_FACT_EXPIRED             # prior confirmation lapsed -> re-verify
    if status in ("STALE", "REVERIFY"):
        return "proposed", CAUSE_FACT_STALE               # drift/expiry -> awaiting re-confirm
    return "proposed", CAUSE_PROPOSED_UNCONFIRMED
```

Then, where the grain/availability requirement is built from `_PHASE1_UNPROMOTED`, replace the static `"missing"` with `_table_fact_status(...)` and set `blocking`/`cause` from the returned `(status, cause)`:
- `confirmed` → `blocking=False` (satisfied; not a blocker).
- `proposed` → `blocking=False`, a **review** requirement (carries the granular cause: proposed-unconfirmed / expired / stale).
- `missing` → `blocking=True` (never proposed, or proposal rejected — the cause distinguishes them).

Only a `confirmed` (VERIFIED) fact is feature-ready; every other state is non-ready but the cause tells the reviewer whether to propose, confirm, or re-verify.

> **Implementer note:** read the exact loop in `readiness.py` that materializes `_PHASE1_UNPROMOTED` into `ReadinessRequirement`s (it has `source` + the per-table iteration in scope) and thread `_table_fact_status`, unpacking the `(status, cause)` tuple. Keep `join` static (Phase 3 owns approved_join state). Ensure a `confirmed`/`proposed` grain moves out of `blocking_requirements` into `review_requirements`/satisfied per the existing `blocking` partitioning. Add a test asserting a REJECTED grain reports `status="missing", cause=CAUSE_FACT_REJECTED` (not indistinguishable from never-proposed).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_readiness_table_facts.py -q`
Expected: PASS (3 passed).

- [ ] **Step 5: Run the full readiness suite**

Run: `uv run pytest tests/featuregen/overlay/upload/test_readiness.py -q`
Expected: PASS (existing diagnostics intact; grain no longer hard-coded missing).

- [ ] **Step 6: Commit**

```bash
git add src/featuregen/overlay/upload/readiness.py tests/featuregen/overlay/upload/test_readiness_table_facts.py
git commit -m "feat(overlay): readiness reads grain/availability fact state (missing->proposed->confirmed)"
```

---

## Task 11: Pending-proposal worklist reader

**Why:** A reviewer needs the list of open grain/availability proposals awaiting confirmation. No such reader exists (`review_queue.list_quarantine` is the wrong domain — ingest validation failures). This is a **read model over the existing `human_tasks` gate tasks** — NOT a new proposal queue or task lifecycle. Each open gate task already carries the fact key + proposed value + CAS target (opened by `propose_fact`); the reader just selects/filters them and surfaces the proposal *origin* (`proposed_by` = the service actor) so a reviewer sees the grain is an unprofiled LLM proposal, not a proven fact (must-fix #4 surfacing).

**Files:**
- Modify: `src/featuregen/overlay/upload/table_fact_projection.py` — add `list_open_table_fact_proposals(conn)`.
- Test: `tests/featuregen/overlay/upload/test_table_fact_worklist.py`

**Interfaces:**
- Consumes: the `human_tasks` table (mirror the inline SELECTs at `expiry.py:126`), `get_task_proposal` (`task_read.py`).
- Produces: `list_open_table_fact_proposals(conn) -> list[dict]` — `[{task_id, fact_type, object_ref, proposed_value, target_event_id}]` for open tasks whose fact_type ∈ {grain, availability_time}, most-recent first.

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_table_fact_worklist.py
from featuregen.overlay.upload.table_synth import _propose_table_facts
from featuregen.overlay.upload.table_fact_projection import list_open_table_fact_proposals


def test_open_grain_proposal_appears(overlay_conn, service_actor):
    _propose_table_facts(overlay_conn, "src",
                         {"txn": {"grain": {"columns": ["id"], "is_unique": True},
                                  "availability_time": None,
                                  "table_role": None, "primary_entity": None}},
                         actor=service_actor, source_snapshot_id="snap-test")
    work = list_open_table_fact_proposals(overlay_conn)
    assert any(w["fact_type"] == "grain" and w["proposed_value"]["columns"] == ["id"]
               for w in work)


def test_confirmed_proposal_drops_off(overlay_conn, human_actor):
    _confirm_grain(overlay_conn, "src", "txn", ["id"], actor=human_actor)  # helper -> VERIFIED
    assert all(w["object_ref"] != "public.txn" or w["fact_type"] != "grain"
               for w in list_open_table_fact_proposals(overlay_conn))
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_fact_worklist.py -q`
Expected: FAIL — `ImportError: cannot import name 'list_open_table_fact_proposals'`.

- [ ] **Step 3: Implement the worklist reader**

Append to `table_fact_projection.py`:

```python
from featuregen.contracts.envelopes import IdentityEnvelope

_TABLE_FACT_TYPES = ("grain", "availability_time")

# The worklist reads platform-admin governance-queue tasks (grain/availability route there because
# UploadContextAdapter.owner_of -> None). get_task_proposal authorizes on role_claims, so the reader
# MUST hold platform-admin or every read is denied. Subject-less system reader.
_WORKLIST_READER = IdentityEnvelope(
    subject="system:table-fact-worklist", actor_kind="service", authenticated=True,
    auth_method="internal", role_claims=("platform-admin",))


def list_open_table_fact_proposals(conn) -> list[dict]:
    """Open grain/availability proposals awaiting human confirmation — a READ MODEL over the existing
    human_tasks gate tasks (not a new queue). get_task_proposal returns a TaskProposal TypedDict, so
    access its fields by KEY, not attribute."""
    from featuregen.overlay.task_read import get_task_proposal
    rows = conn.execute(
        "SELECT task_id FROM human_tasks WHERE status = 'open' ORDER BY created_at DESC"
    ).fetchall()
    out: list[dict] = []
    for (task_id,) in rows:
        try:
            p = get_task_proposal(conn, task_id, _WORKLIST_READER)
        except Exception:   # noqa: BLE001 — a task the reader can't see is simply skipped
            continue
        if p["fact_type"] in _TABLE_FACT_TYPES:
            out.append({"task_id": task_id, "fact_type": p["fact_type"],
                        "object_ref": p["object_ref"], "proposed_value": p["proposed_value"],
                        "target_event_id": p["target_event_id"],
                        # origin so a reviewer sees this is an unprofiled LLM proposal, not proof:
                        "uniqueness_basis": "llm_proposed_not_profiled"})
    return out
```

> **Implementer note:** `human_tasks` PK is **`task_id`** (not `id`) — verify against `0070_identity_authz_gates.sql`. `get_task_proposal` (`task_read.py:17`) returns a **`TaskProposal` TypedDict** (a plain dict) — access `p["fact_type"]`/`p["object_ref"]`/`p["proposed_value"]`/`p["target_event_id"]` by KEY, never attribute. It authorizes on `role_claims`, and grain/availability route to the platform-admin governance queue (owner_of→None), so `_WORKLIST_READER` MUST carry `("platform-admin",)` or every read is denied. Confirm the `IdentityEnvelope` field names (`actor_kind`/`authenticated`/`auth_method`/`role_claims`) against `contracts/envelopes.py`. If a `fact_type` column exists on `human_tasks` (or a cheap join), filter in SQL for scale; keep the return contract identical.

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/featuregen/overlay/upload/test_table_fact_worklist.py -q`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add src/featuregen/overlay/upload/table_fact_projection.py tests/featuregen/overlay/upload/test_table_fact_worklist.py
git commit -m "feat(overlay): pending grain/availability proposal worklist reader"
```

---

## Task 12: Whole-phase integration test (FTR glossary → proposal → confirm → projection)

**Why:** Prove the full Pass B loop on a realistic multi-table glossary upload: Pass B proposes a grain, readiness reports it proposed, a human confirms, and re-ingest projects it load-bearing.

**Files:**
- Test: `tests/featuregen/overlay/upload/test_phase2_integration.py`

- [ ] **Step 1: Write the integration test**

```python
# tests/featuregen/overlay/upload/test_phase2_integration.py
# Uses a fake LLM client that returns a canned table_synth batch. OVERLAY_TABLE_SYNTH=1.
def test_glossary_upload_proposes_then_confirms_grain(overlay_conn, human_actor, monkeypatch,
                                                      fake_synth_client, glossary_rows):
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.readiness import compute_readiness, ReadinessScopeType

    r1 = ingest_upload(overlay_conn, "src", glossary_rows, actor=human_actor,
                       client=fake_synth_client)
    assert r1.status == "ingested"    # IngestResult.status ∈ {ingested, held, rejected}
    # readiness: grain proposed, not confirmed
    rd = compute_readiness(overlay_conn, source="src", scope=ReadinessScopeType.TABLE, subset="txn")
    assert any(x.status == "proposed" and "grain" in x.requirement_id
               for x in rd.review_requirements)

    _confirm_grain(overlay_conn, "src", "txn", ["txn_id"], actor=human_actor)  # human confirms

    # re-ingest projects the confirmed grain load-bearing onto graph_node
    ingest_upload(overlay_conn, "src", glossary_rows, actor=human_actor, client=fake_synth_client)
    row = overlay_conn.execute(
        "SELECT is_grain FROM graph_node WHERE catalog_source='src' AND table_name='txn' "
        "AND column_name='txn_id' AND kind='column'").fetchone()
    assert row[0] is True
    rd2 = compute_readiness(overlay_conn, source="src", scope=ReadinessScopeType.TABLE, subset="txn")
    assert any(x.status == "confirmed" and "grain" in x.requirement_id
               for x in (rd2.review_requirements + rd2.blocking_requirements)
               ) or all("grain" not in x.requirement_id for x in rd2.blocking_requirements)


def test_declared_structural_grain_beats_pass_b_proposal(overlay_conn, human_actor, monkeypatch,
                                                         fake_synth_client, technical_rows):
    # technical_rows: a TECHNICAL csv declaring is_grain on `id` -> _assert_fact auto-confirms it
    # (legitimate SOURCE attestation, §16). Pass B proposing a DIFFERENT grain must not touch it.
    monkeypatch.setenv("OVERLAY_TABLE_SYNTH", "1")
    from featuregen.overlay.identity import fact_key
    from featuregen.overlay.state import fold_overlay_state
    from featuregen.overlay.store import load_fact
    from featuregen.overlay.upload.ingest import ingest_upload
    from featuregen.overlay.upload.upload_catalog import table_ref
    # fake_synth_client returns grain=[a_different_col] for the same table
    ingest_upload(overlay_conn, "src", technical_rows, actor=human_actor, client=fake_synth_client)
    state = fold_overlay_state(load_fact(overlay_conn, fact_key(table_ref("src", "txn"), "grain")))
    assert state.status == "VERIFIED"                 # source-declared grain stands
    assert state.value["columns"] == ["id"]           # Pass B did NOT overwrite it
```

> **Implementer note:** `IngestResult.status` success value is `"ingested"` (∈ {ingested, held, rejected}), never "ok". Build `glossary_rows`/`technical_rows` from the existing Phase-1 glossary + technical-CSV row builders. The shared conftest (below) is authored in **Task 7** (first consumer), not here.

- [ ] **Step 2: Run it**

Run: `uv run pytest tests/featuregen/overlay/upload/test_phase2_integration.py -q`
Expected: PASS.

- [ ] **Step 3: Run the full overlay upload suite**

Run: `uv run pytest tests/featuregen/overlay/upload/ -q`
Expected: PASS (whole slice green; flag-off paths unchanged).

- [ ] **Step 4: Commit**

```bash
git add tests/featuregen/overlay/upload/test_phase2_integration.py tests/featuregen/overlay/upload/conftest.py
git commit -m "test(overlay): Phase 2 end-to-end — propose -> confirm -> project grain"
```

---

## Self-Review (spec coverage)

| Spec §15.2 Pass B output | Destination | Task |
|---|---|---|
| Table role | advisory field evidence | 7 (evidence) + 8 (policy/display) |
| Primary entity | field evidence / confirmation | 7 + 8 |
| Grain candidate | typed grain fact proposal (specialized_fact) | 6 (validate) + 7 (propose) + 9 (project) + 10 (readiness) |
| As-of candidate | typed availability fact proposal | 6 + 7 + 9 + 10 |
| Event/snapshot classification | advisory | 2 (schema) — captured; surfaced via synthesis dict (advisory, not yet projected — see below) |
| Time columns | evidence linked to availability proposal | 6/7 (as_of_column carried in the availability fact) |

**Phase 2 acceptance (spec §17 "Phase 2 — Table facts"):** Pass B (✓ Tasks 5-7); grain/availability candidates → typed fact proposals (✓ Task 7, PROPOSED-only); review queue (✓ Task 11 worklist + existing gate task); human confirmation persistence scoped/expiring (✓ reused `confirm_fact` arms `schedule_expiry`; TTL note below).

**Resolved from v1 review (now IN scope):**
- **`event_or_snapshot`** — now projected as a third advisory field (Tasks 2/7/8, migration 0986), consistent with §15.2 "advisory".
- **Projection idempotency** — Task 9 clears-then-sets (must-fix #1); `project_table_facts_for_ref` exposes a single-table entry point for a future confirm hook.
- **Proposal lifecycle** — Task 7 reads folded state; Task 10 reflects the full lifecycle with distinct causes.

**Deliberate deferrals (call out to the reviewer):**
- **grain/availability TTL horizon:** `resolve_ttl` falls back to `_DEFAULT_TTL` (180d) unless entries are added to `OverlayConfig.ttl_by_fact_type`. DATA-not-code. **Decide during Task 7 review**: if 180d is wrong for grain, add `grain`/`availability_time` entries to `ttl_by_fact_type` (a one-line config change, no new code). The plan does not hard-code a horizon.
- **Live confirm-time projection** (no re-upload lag) — deferred to Phase 4 (no confirm API/HITL surface exists yet; `confirm_fact` is worker/command-bus only). End-of-ingest re-projection (Task 9) is the guaranteed mechanism, `project_table_facts_for_ref` is the ready hook, and `resolve_fact`/readiness are load-bearing-correct immediately on confirm regardless.
- **UNCONFIRMED-proposal age-out** — the reuse map flagged this NEW + out of scope (the spec scopes only *confirmations* as expiring). Not built.
- **Data-owner routing** — Phase 2 routes confirmations to the platform-admin governance queue (`owner_of→None`); owner-specific routing needs a richer adapter (Phase 3/4). Documented in Task 1, not a gap.

**Placeholder scan:** the two "Implementer note" callouts in Tasks 5/6/7/9/10/11 point at exact files/lines to confirm signatures — they are grounding directives, not placeholders; every step carries concrete code. **Type consistency:** `table_ref`, `fact_key`, `synthesis_dict` shape (`{grain,availability_time,table_role,primary_entity}`), and the `{columns,is_unique}`/`{column,basis}` fact shapes are used identically across Tasks 6/7/9/10/11.

---

## Execution Handoff

**Plan complete and saved to `docs/superpowers/plans/2026-07-12-phase2-table-facts.md`. Two execution options:**

**1. Subagent-Driven (recommended)** — I dispatch a fresh subagent per task, review between tasks, fast iteration.

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.

**Which approach?**
