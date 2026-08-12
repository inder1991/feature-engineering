# Catalog ingestion workflow — production-readiness review (main @ `3c69796d`)

Scope: `POST /uploads` → parse → validate → brake → fact assertion → drift → Pass A LLM enrichment →
graph → Pass B/C → D4 semantic bindings → projections → quarantine → run manifest, plus the
connector twin (`POST /syncs/{id}/import`), the deployed flag set, and the frontend surfaces that
consume it.

Reviewed against `main` in a detached worktree, not the checked-out feature branch.

**Overall.** The pipeline itself is unusually disciplined — savepointed advisory stages, honest
per-stage manifests, fail-closed validation, a durable run record that survives a rolled-back
request transaction. The production-readiness risk is not in the ingest algebra. It is concentrated
in four places: **the LLM driver's retry semantics**, **the deployed flag/budget combination**,
**three governed loops that have a backend but no UI**, and **observability of failed provider
calls**. Severities below are mine.

---

## 1. LLM call layer

### 1.1 [HIGH] "Bounded repair" re-sends a byte-identical prompt — the validation errors never reach the model

`intake/llm.py:267` builds the repair request:

```python
request = replace(request, inputs={**request.inputs, "_repair_errors": list(errors)})
```

but the Claude adapter's wire renderer (`intake/llm_claude.py:98–112`, `_wire_prompt`) reads only
`INPUT_KEY_INTENT` and `INPUT_KEY_CATALOG`. `_repair_errors` is deliberately excluded from the
identity hash (`llm.py:98`) and is read by **nothing else in `src/`** — the only other references are
two tests. So a repair attempt sends the same model the same bytes and gets the same answer.

Effect: `DEFAULT_REPAIR_BUDGET = 2` buys two extra full-price Opus calls per malformed structure that
cannot, by construction, differ from the first. Fix: render the accumulated errors into the user turn
(a `_`-prefixed key can still stay out of the idempotency hash).

### 1.2 [HIGH] Truncation retry replays the same `max_tokens`

`intake/llm.py:276–282`:

```python
if ps in _RETRYABLE:          # PROVIDER_MAX_TOKENS is in this tuple
    ...
    resp = client.call(request)   # unchanged request, unchanged max_tokens
```

`stop_reason == "max_tokens"` maps to `PROVIDER_MAX_TOKENS` (`llm_claude.py:70`) and retries the
identical request. With sampling parameters removed on Opus 4.8, a truncated call re-truncates. Three
expensive calls, then `"max_tokens retry budget exhausted"`.

The deployed config confirms this was hit in production — `deploy/kind/k8s/20-backend.yaml:19–25`
raises `FEATUREGEN_LLM_MAX_TOKENS` to `32000` and the comment names the symptom ("the truncation retry
re-bills the whole (expensive) call and exhausts the retry budget"). That is a mitigation, not a fix,
and it created §1.3.

Fix: on `PROVIDER_MAX_TOKENS`, escalate — raise `generation_settings["max_tokens"]` (up to the model
ceiling), and/or drop `effort` a tier so thinking consumes less of the budget, and/or have the batch
ladder split the chunk instead of retrying it whole.

### 1.3 [HIGH] 60 s per-call timeout against a 32 000-token non-streaming call

`ClaudeConfig.timeout` defaults to `60.0` (`llm_claude.py:44,54`) and `FEATUREGEN_LLM_TIMEOUT` is
**not set** in the ConfigMap. The adapter calls `client.messages.create(...)` non-streaming with
`max_tokens=32000`, `thinking={"type":"adaptive"}`, `effort="high"` (`llm_claude.py:192–203`).

Anthropic's guidance is to stream anything above ~16 000 `max_tokens`; adaptive thinking at
`effort=high` on Opus 4.8 regularly runs past a minute. The explicit `timeout` also suppresses the
SDK's own large-`max_tokens` guard, so there is no error telling you this is misconfigured — you just
get `APITimeoutError`, which reaches `except anthropic.APIConnectionError` only *incidentally* (it is
a subclass; the adapter never names it) → `PROVIDER_TRANSIENT` → two more 60-second attempts.

Fix: switch the adapter to `client.messages.stream(...)` + `.get_final_message()`, or set
`FEATUREGEN_LLM_TIMEOUT` well above the expected generation time. Either way, add an explicit
`except anthropic.APITimeoutError` arm so the mapping is intentional and testable.

### 1.4 [MEDIUM] Failed calls are the least observable ones

LLM responses **are** persisted — `record_llm_call` writes an immutable `llm_call` row
(`intake/llm.py:362–401`) on a separate committing connection so it survives an upload rollback
(`enrich_llm.py:609–643`), and it is written on failure too (`enrich_llm.py:1409–1416`). But:

- `_parse_structured` returns `{}` when the first text block will not parse (`llm_claude.py:249–261`)
  — which is exactly what a truncated response does. The partial text is discarded, so
  `raw_output` is `{"output": {}}`.
- `_failed(...)` hard-codes `cost_metadata={}` (`llm.py:208`) — the tokens were billed, the usage is
  thrown away. Cost accounting under-reports precisely the expensive failures.
- `latency_ms=None` is hard-coded at the only call site (`enrich_llm.py:1416`) — the column is always
  NULL.

Fix: persist the raw text on a parse failure (truncated or not), thread `resp.cost_metadata` through
`_failed`, and time the call.

### 1.5 [LOW] Prompt-cache hit rate is unverified and easy to lose silently

`_wire_prompt` renders the cached vocabulary as `f"...: {cached}"` where `cached` is a Python dict
(`llm_claude.py:104–106`). Caching is a byte-prefix match; if that dict's iteration order ever varies
between chunks, `cache_read_input_tokens` silently goes to zero and the ~23 K-token vocabulary is
re-billed on every chunk with no error. Serialize it deterministically
(`json.dumps(..., sort_keys=True)`) and assert `usage.cache_read_input_tokens > 0` on chunk 2 in a
key-gated test.

### 1.6 [LOW] No server-side refusal fallback

`refusal` maps to fail-closed (`llm_claude.py:69`) with no `fallbacks` parameter and no
`stop_details` capture. Fine for metadata enrichment; worth revisiting if enrichment ever touches
content that trips a classifier.

---

## 2. Deployed configuration — the flag set is internally inconsistent

**Do not "enable all flags."** Two of them are deliberately, correctly off:

- `FEATUREGEN_CROSSWALK_EXECUTION=1` without `FEATUREGEN_SOURCE_TEMPORAL_SELECTION=1` is a **boot
  refusal**, by design (`api/app.py:127–147`).
- `FEATUREGEN_MATERIALIZE_ENABLED=1` strands requests permanently — the only §0 inventory in the repo
  is a template `load_inventory` refuses, and a stranded `requested` row has no legal terminal edge
  (documented at `20-backend.yaml:80–123`, DEFERRED-WORK A.35).

The flags that *are* on have two coherence problems:

### 2.1 [HIGH] The run lease can expire under a live ingest, permanently mislabelling a committed upload

`runtime/worker.py:338–348` states the invariant explicitly: the lease default (7200 s) *must* exceed
the maximum in-request ingest duration, "because NOTHING heartbeats a run mid-ingest" — and indeed
`heartbeat_at` is written only at `open_run` and at `terminalize_run`
(`overlay/upload/ingestion_run.py:126–133, 210–220`).

The deployed `OVERLAY_ENRICH_STAGE_DEADLINE_S: "1800"` is a **per-stage** ceiling, threaded at 8 call
sites (`enrich.py:1954, 2180, 2238, 2338, 2430, 2525`; `table_synth.py:801, 901`). A worst-case ingest
is therefore bounded near 8 × 1800 s ≈ 4 h against a 2 h lease.

Consequence chain, all in existing code: the sweep flips a still-running run to `abandoned` →
the request's own `terminalize_run` matches `WHERE id = %s AND status = 'in_progress'`, finds nothing,
returns `False` → **a successfully committed ingest is recorded `abandoned` forever**, with no status
event for what actually happened.

Fix (either): add a mid-ingest heartbeat (per enrichment batch is the natural granularity — the
comment already proposes it), or set `FEATUREGEN_INGESTION_RUN_LEASE_SECONDS` above the sum of the
stage deadlines. The heartbeat is the real fix; the lease bump is the one-line unblock.

### 2.2 [MEDIUM] Raising the stage deadline did nothing for wide catalogs — the call ledger binds first

`OVERLAY_ENRICH_MAX_PROVIDER_CALLS` is left at its default of 32 (`enrich_config.py:148`), enforced
per task run by `CallLedger` (`enrich_batch.py:42–75`). The `summary` stage runs over **every** column
at `max_items=8`, so a 300-column catalog needs 38 chunks before a single retry, split, or fallback.
The 7.5× deadline increase is largely inert; the ceiling truncates first.

This is reported honestly (`report["not_attempted"]`, `enrich_batch.py:362–365`, surfaced as a
`truncated` stage) — it is a capacity gap, not a silent one — but an operator only sees it by reading
run manifests. Size the ledger against the largest catalog you intend to onboard, and alert on
`overlay.enrich.*.batch.budget_exhausted` / `.timed_out`.

### 2.3 [MEDIUM] The per-source advisory lock is held across every LLM stage

`ingest.py:2000` takes `pg_advisory_xact_lock` at the top and the Pass A/B/D4 stages run inside it
(the trade-off is documented at `ingest.py:1987–1999`). At 1800 s per stage, a single upload can block
re-uploads of the same source for hours. Acceptable for a demo; not for concurrent ingestion.

Related: the upload is a synchronous request that may legitimately run for hours. Any real ingress
will kill it, and FastAPI sync routes occupy a threadpool worker for the duration — enough concurrent
uploads and `/health` stops answering, which the k8s `livenessProbe` (`20-backend.yaml:169–172`) will
read as a dead pod and restart mid-ingest.

### 2.4 [LOW] Two idempotency and efficiency gaps

- `draft_synonyms` and `draft_units` have **no enrichment cache** (`enrich.py:2401, 2482` — "call
  reuse is a deferred optimization"). An unchanged re-upload re-bills 2 of the 6 Pass-A stages in
  full. `content_hash`-keyed caches exist for concept/definition/domain/summary.
- Every durable audit write opens its own connection: `_record_llm_call_durable`
  (`enrich_llm.py:633–637`) does one `psycopg.connect(dsn)` **per LLM call**, and `open_run` /
  `terminalize_run_durable` do the same. A wide upload is 50–100 connect/handshake cycles inside one
  request. The isolation rationale is sound (I-3 self-deadlock); the implementation wants a small
  dedicated pool.

### 2.5 [LOW] Test-seam inconsistency in the batch ladder

`run_batched` injects `now` for the stage deadline (`enrich_batch.py:347,352`) but `over_budget()`
reads `time.monotonic()` directly (`:235,256`). The injected clock does not control the wallclock
budget path, so that branch is not deterministically testable.

---

## 3. Frontend ↔ backend mapping

### 3.1 [HIGH] Semantic bindings: enabled in production, zero UI

`OVERLAY_SEMANTIC_BINDING_CANDIDATES` and `..._PROPOSALS` are both `"1"` (`20-backend.yaml:42–43`).
The stage runs, spends provider calls from its own budget, and writes DRAFT `entity_assignment` /
`currency_binding` proposals.

Backend surface: `GET /sources/{source}/governance/semantic-bindings` plus five actions
(`confirm`, `reject`, `correct`, `reverify`, `withdraw`).

Frontend: `grep -r "semantic-bindings" frontend/src` → **no matches**, tests included. And
`GET /governance/queue` merges only three kinds — entity bridges, approved joins, table facts
(`overlay/upload/governance_queue.py`, `ENTITY_BRIDGE` / `APPROVED_JOIN` / table-fact listings).

So the loop is open: proposals accumulate as DRAFT and nothing in the product can ever act on them.
Either wire the surface (add the kind to the queue + actions to the review screen) or turn the two
flags off until it is wired.

### 3.2 [HIGH] The catalog narrative is silently discarded

`FEATUREGEN_DATASET_PROFILES: "0"` (`20-backend.yaml:69`). On the server,
`_validated_profile_fields` returns `None` with a **server-side log line only**
(`api/routes/uploads.py:88–91`).

On the client there is **no flag gate**: `UploadScreen.tsx` renders the full structured narrative
editor unconditionally, and `api.ts:413–414` appends `catalog_profile_json` whenever it is non-empty.
The upload then returns `ingested`. The user authored a narrative, the UI reported success, and the
narrative went nowhere — with no signal anywhere the user can see.

The UI cannot self-gate today because `/health` exposes no feature flags (`api/app.py:257–281`
returns only status/schema/scope-mode). Fix: expose the flag set on `/health` (or a `/config`
surface) and hide the section, or have the route 422 on a present-but-disabled part instead of
warning to a log nobody reads.

### 3.3 [MEDIUM] Bridge-realization governance has a client but no screen

`bridge-realizations` appears in `frontend/src/api.ts` and in no screen. Backend has
`GET /sources/{s}/governance/bridge-realizations` + `POST /governance/bridge-realizations/{id}/confirm|reject`.
Dead client code, or an unfinished screen — decide which.

### 3.4 [LOW] Backend endpoints with no frontend consumer

`GET /sources/{s}/readiness` (only `/readiness/relationships` is called), `GET /learning/gaps`,
`GET /contract/scope-mode`, `POST /entity/suggest`, `GET /columns/{ref}/feature-impact`,
`GET /columns/{ref}/joins`. `materialization-runs` is expected (flag off). Each is either a gap or
should be deleted.

### 3.5 [GOOD] What *is* well wired

The run manifest is properly surfaced: `X-Ingestion-Run-Id` rides success **and** every post-open
failure (`uploads.py:198, 341, 360`), the client lifts it onto `ApiError` (`api.ts:76`), and
`IngestResultCallout` fetches `GET /ingestion-runs/{id}` and folds the stage report into readable
segments for held/rejected runs too. The connector import route has full parity — same `open_run`,
same `StageRecorder`, same durable terminalize (`api/routes/integrations.py:562–701`).

---

## 4. Data model

### 4.1 [MEDIUM] Duplicate migration numbers on main

`0973`, `0974`, `1034`, `1036`, `1037`, `1038`, `1040` each have **two** files. The ledger keys on
full filename and applies in lexical order (`db/migrations.py:269, 309–322`), so both apply and
nothing breaks today — but the relative order of two same-numbered migrations is decided by whichever
description sorts first, not by anyone's intent. If one ever depends on the other, the ordering is
accidental. Reconcile the numbering and add a CI check that rejects a duplicate prefix.

### 4.2 [MEDIUM] Technical uploads get LLM values with no evidence behind them

`ingest.py:2238`:

```python
snapshot_id = mint_id("ing") if is_glossary else None
```

`field_evidence` requires a non-NULL `source_snapshot_id`, so on the **technical CSV/XLSX path** the
LLM's concept/definition/domain/synonym/unit values reach `graph_node` through the in-memory maps and
`build_graph` with no evidence row, no producer-scoped staleness, and no decision link. The code
documents this as pre-existing and deliberate (`ingest.py:2228–2237`) and the adjudicator counts the
items as `evidence_skipped`. It is still a governance hole on the most common upload shape: those
values are displayed but unbacked, and nothing retires them when the column changes.

### 4.3 [LOW] `llm_call` columns that are structurally always empty

`latency_ms` (always NULL, §1.4) and `cost_metadata` (NULL on every failure). Either populate them or
drop them from the contract.

---

## 5. Edge cases — what is already handled well

Recorded so a future reviewer does not re-derive it. `validate_rows` (`overlay/upload/canonical.py:107–313`)
fails **closed** on: whitespace-only required fields; foreign-source rows; unrecognized
sensitivity/cardinality/additivity/as_of_basis; `.` in a table/column name (object-ref corruption);
case-variant duplicates of one physical column (dedup keys on the same `_norm` as object identity, so
a `pii`-tagged `SSN` cannot be shadowed by an untagged `ssn`); conflicting metadata for one column
(**all** rows quarantined, with the sensitivity floor stamped across the group); tables over
`MAX_COLUMNS_PER_TABLE = 200`; and multiple `as_of` columns per table.

Also correct: reserved `__…__` source names rejected at both the route and the validator; `source`
normalized strip+lower and refused if it is not a single path segment (percent-decoded `%2F` cannot
cross the route boundary); a 25 MiB read cap checked one byte past the limit; a held or all-quarantined
upload returns **before** `build_graph` so garbage never wipes an existing graph; `persist_quarantine`
is skipped when the hold is clean so a reviewer's queue is not wiped; every advisory stage owns a
savepoint so a poisoned enrichment transaction cannot roll back asserted facts; and the
projection-lag guards refuse to advance drift/table-fact/join/semantic-binding projections against a
stale read model.

---

## 6. Recommended order of work

**Before production:**

1. §2.1 — mid-ingest heartbeat (or raise the lease past Σ stage deadlines). A committed ingest
   recorded `abandoned` is unrecoverable state corruption in the manifest.
2. §1.3 — stream the provider call, or raise `FEATUREGEN_LLM_TIMEOUT`. Today's 60 s/32 K pairing
   means a slow call costs 3 minutes and 3 billings to fail.
3. §1.2 + §1.1 — make retry and repair actually differ from the attempt they follow.
4. §3.2 — flag-gate the narrative editor (and expose flags on `/health`).
5. §3.1 — wire the semantic-binding governance surface, or turn the two flags off.

**Before scale:**

6. §2.2 — size `OVERLAY_ENRICH_MAX_PROVIDER_CALLS` to the target catalog; alert on the truncation
   counters.
7. §2.3 — move enrichment out of the request, or off the source lock.
8. §1.4 — persist truncated bodies, usage on failure, and latency.
9. §2.4 — cache synonyms/units; pool the durable-audit connections.

**Housekeeping:**

10. §4.1 migration numbering + CI guard; §4.2 decide whether technical uploads deserve evidence;
    §3.3/§3.4 wire or delete.

---

# Round 2 — second pass over the modules the first pass skipped

The first pass read the ingest driver, the LLM layer, the batch engine, `validate_rows` and the run
manifest. This pass covers the readers, the brake, the drift detector, the stage recorder, the
quarantine mutation path, the snapshot writer, and the deployment topology. **The largest finding in
the whole review is here (§R1), and it re-frames §2.1.**

## R1 [CRITICAL] No worker is deployed — the entire asynchronous half of the platform never runs

`python -m featuregen worker` is a **separate daemon** (`__main__.py:37, 153` → `runtime.worker.run_forever`).
The API process does not start it — `create_app`'s lifespan registers event schemas and the overlay
config and nothing else (`api/app.py:150–172`).

`deploy/kind/k8s/` contains exactly four manifests: `00-namespace`, `10-postgres`, `20-backend`,
`30-frontend`. **There is no worker Deployment.** The only match for "worker" under `k8s/` is comment
prose inside the backend ConfigMap.

So on the deployed cluster none of this runs:

| Never runs | Consequence |
|---|---|
| drift scanner | Nothing re-verifies that a VERIFIED fact still matches the catalog. This is *why* `OVERLAY_DRIFT_FRESHNESS_SLA_MIN` is set to 43200 (30 days) — the ConfigMap comment says "Real deploys run a scanner instead," and no deploy here does. |
| `reconcile_ingestion_runs` | A run from a crashed process stays `in_progress` forever. |
| timers / TTL / expiry / renewal | Every `OVERLAY_TTL_*` and `renewal_grace` knob is configured and inert. Facts never expire. |
| reverify tasks | Opened and never worked. |
| projection catch-up | The overlay projection only advances inside an upload — see §R2. |
| relay / queue | Anything queued is never delivered. |

**This corrects §2.1.** The lease sweep that would mislabel a slow ingest as `abandoned` is not
running either, so today's live symptom is *stuck* `in_progress` runs, not corrupted ones. §2.1 fires
the day someone deploys a worker — which is exactly what "make this production ready" means. Fix both
together, in that order.

## R2 [HIGH] Projection-lag ratchet — with no worker, a backlog is permanent and self-reinforcing

`_drain_projection` (`ingest.py:195–212`) is capped at `_DRAIN_MAX_EVENTS = 5000` per upload. Every
lag-guarded stage then checks `projection_lag(conn, "overlay") > 0` and skips if non-zero: drift
detection (`ingest.py:2178`), table-fact projection (`:3088`), approved-join re-projection (`:3218`),
semantic-binding re-projection (`:3246`).

Two things make this worse than it looks:

1. **`projection_lag` is global, not per-source** (`projections/runner.py:250–261`): it is
   `head_seq - checkpoint_seq`, where `head_seq` is the whole system's event sequence. Events from
   features, contracts, governance confirms, or a *different* catalog all inflate the number an
   upload must drain through, and can spend its 5,000-event budget on events the overlay projection
   does not care about.
2. **`build_graph` has already run by then.** It deletes and rebuilds every node and edge for the
   source. The skipped re-projections are precisely the ones that restore previously-VERIFIED
   approved joins, confirmed grain/as-of stamps, and governed semantic bindings onto the fresh graph.

So once the global backlog exceeds one upload's drain budget, every subsequent upload also skips —
and with no worker there is nothing to ever catch up. Confirmed joins and grain stay unprojected
indefinitely, and feature construction goes dark on those paths. The guards are correct; the thing
that is supposed to relieve them isn't deployed.

## R3 [HIGH] The large-change brake has no escape hatch

`large_change_brake(conn, catalog_source, upload)` (`ingest.py:2105`) is called with defaults only.
The thresholds — 30% removed, 60% overlap minimum — are hardcoded function parameters
(`brake.py:22–24`) with **no env override, no force flag on `POST /uploads`, and no admin bypass
anywhere in `src/`** (grep confirms the only callers are ingest, the connector preview, and the
resolution path, all with defaults).

A catalog that legitimately drops more than 30% of its objects — a deprecation, a consolidation, a
table split — is **permanently un-uploadable through the product**. The only ways out are direct DB
surgery or renaming the source, which orphans every fact, decision and governance record attached to
the old name. Needs an audited, role-gated override (`force=true` + a reason, recorded on the run).

## R4 [MEDIUM] On the upload path, a rename silently destroys the column's governance state

Rename detection is keyed entirely on `native_oid` (`catalog_changes.py:151–153`), and
`UploadCatalog` sets `native_oid=None` for every object it emits (`upload_catalog.py:43, 52`). So
`dropped_by_oid` and `added_by_oid` are always empty on the upload path and `renamed` is always
empty: **every column rename is recorded as a `drop` plus an `add`.** The old column's dependent
facts are STALEd and the new column is onboarded fresh — confirmed grain membership, availability
basis, entity bindings and any human governance work attached to the old name are gone.

That is unavoidable from a CSV (there is no stable identifier in the file). What is missing is any
surface that says so: no "these two look like a rename, confirm?" review item, no warning on the run,
nothing in the ingest callout. A one-character typo in a column name is indistinguishable from a real
drop, and costs the same.

## R5 [MEDIUM] Quarantine resolution takes no advisory lock

`ingest_upload` takes `pg_advisory_xact_lock(ingest_source_lock_key(catalog_source))` at the very top
(`ingest.py:2000`) precisely because `build_graph` is a whole-source delete-then-reinsert.
`resolve_quarantine_row` (`ingest.py:3454–3590`) mutates the same source's graph — `add_column_row`,
`_assert_fact` for grain and availability — and takes **no lock at all**.

Concurrent resolve + upload interleavings are real: the resolve checks "column not already in the
graph", the upload's `build_graph` then deletes and reinserts the whole source, and the resolve's
insert lands either as a duplicate or as a survivor with attributes from a superseded file. The
docstring's "a re-upload supersedes the resolution" describes the *sequential* case only. Take the
same lock.

## R6 [MEDIUM] Quarantine resolution leaves no run manifest

The function's own docstring calls resolution "an ingestion path" — it takes the brake, and asserts
facts with `origin_type="resolution"`. But it opens no `ingestion_run`, records no stages, and writes
no `ingestion_run_object` / `ingestion_run_fact` provenance. A governed mutation that adds a column
and asserts grain/availability facts is invisible to the audit surface that design #3 promises covers
"every ingestion attempt."

## R7 [MEDIUM] `_save_snapshot` is a per-object round-trip loop

`catalog_changes.py:96–110` executes one `INSERT … ON CONFLICT DO UPDATE` **per object**, in a Python
loop, inside the request and inside the source advisory lock. A 5,000-object catalog is 5,000
round-trips. `executemany` is already the pattern used by `record_run_objects`
(`ingestion_run.py:300`).

## R8 [LOW] `columns_fingerprint` is dead

`_save_snapshot` writes the literal `""` for it on every row (`catalog_changes.py:109`) and nothing
in `src/` ever reads it. The column has existed since migration `0507_overlay_tables.sql:66`. Either
a lost table-level change signal or cruft — decide and act.

## R9 [LOW] Multi-sheet Excel workbooks silently read sheet 1 only

`read_excel_rows` accepts a `sheet` parameter, and the upload route never passes it
(`api/routes/uploads.py:136`), so `ws = wb.worksheets[0]` always wins (`excel_reader.py:43`). A
workbook whose schema sits on the second tab — behind a "Read Me" or a cover sheet — parses to
garbage or nothing, with no warning naming the sheet that was read.

## R10 [LOW] Excel formula cells can quarantine a whole file with a misleading reason

`load_workbook(..., data_only=True)` (`excel_reader.py:42`) returns the *cached* value of a formula
cell. A file generated programmatically and never opened in Excel has no cached values, so every such
cell reads `None` → `""` → the row quarantines as "missing required field(s)". Correct fail-closed
behaviour, useless diagnostic.

## R11 [LOW] Quarantine resolution is O(n) per row, O(n²) per queue

The sibling-sensitivity check (`ingest.py:3526–3533`) selects **every** other quarantined row for the
source and filters in Python. Working through the queue left by an all-quarantined wrong-source
upload (thousands of rows) is quadratic.

---

## Checked in this pass and found correct

Recorded so the coverage is legible and nobody re-derives it:

- **Duplicate and aliasing header collisions are rejected**, not last-write-wins — `field_map`
  (`_headers.py:40–66`) raises on both a repeated normalized header and two distinct headers aliasing
  to one canonical field, naming the sensitivity-drop hazard explicitly. I expected a bug here and
  there isn't one.
- **The stage recorder cannot double-write.** `flush` drains the buffer and `flush_durable` returns
  early on an empty buffer, so a success-path flush followed by an exception-path durable flush is
  safe; a contained flush failure keeps the buffer and writes an `audit_degraded` marker.
- **Excel decompression bombs are bounded** across both the header scan and the data read
  (`excel_reader.py:19–36`), fail-closed with a parse error rather than silent truncation.
- **The quarantine-resolution refusals are genuinely thorough**: an FTR-adapter row cannot be
  repaired inline; a case-variant of an existing column resolves to that column's ref and is refused;
  the sensitivity floor is read from the row's own stored record so a reviewer edit cannot strip it
  *and* dismissing the tagged sibling cannot lift it; a second `as_of` column cannot silently flip a
  table's availability basis.
- **Drift is not laundered on a concurrent-confirm conflict** — `detect_catalog_changes` returns
  without advancing the snapshot or the watermark, and counts the truncated scan.

## Still unread

For honesty about coverage, these remain unreviewed: the six Pass-A stage bodies in `enrich.py`
(2,549 lines — only the call sites and cache keys were read), `templates.py` (4,720),
`feature_assist.py` (2,303), `semantic_context.py` (1,844), `suggestion_contract.py` (1,594),
`concepts.py` (1,428), `table_synth.py` internals, `build_graph`, the Pass-C candidate scorer, the
bridge assessment/realization modules, and the crosswalk family.
