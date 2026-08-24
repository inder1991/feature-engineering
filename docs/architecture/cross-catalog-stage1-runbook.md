# Cross-catalog Stage 1 — operator runbook

How the Stage-1 governed-telemetry lane is deployed, switched on, watched, and read. The
acceptance numbers it feeds live in
[`cross-catalog-stage1-thresholds.md`](cross-catalog-stage1-thresholds.md) (DRAFT until an SME
signs). One rule frames everything below: **every action that changes live state is an explicit
user go** — the table at the end enumerates them.

## 1. Migrations: 1120 + 1121, backend-first

Two migrations, and only two — S1C-3's chooser shipped **no migration 1122**: it reuses the
shared `structured_result` store (migration 1039) with `result_type = "param_choice_shadow"`
(`PARAM_CHOICE_RESULT_TYPE`, `src/featuregen/overlay/upload/param_choice.py`).

* **`1120_governed_planning_observation.sql`** — three **append-only** tables (row-level
  BEFORE UPDATE/DELETE + statement-level BEFORE TRUNCATE raisers on each):
  `governed_planning_observation` (one row per planned request, every outcome, modes
  `live | telemetry`), `bridge_demand_observation` (the rejection child; queue is a CHECK-enforced
  function of the verdict: `bridge_demand` / `realization_gap` / `planner_capacity`), and
  `governed_plan_review_event` (append-only SME judgement stream; a changed mind is a new event
  that supersedes the old).
* **`1121_governed_telemetry_outbox.sql`** — `governed_telemetry_outbox`, the one table that is
  **mutable by design**: `status`/`lease_owner`/`lease_expires_at`/`lease_fence`/`attempt_count`/
  `last_error`/`completed_at` are worker-coordination state, not evidence. The evidence the worker
  writes is 1120's append-only rows.

Apply **backend-first** (schema before any image that reads it):

```
python -m featuregen migrate --dsn "$FEATUREGEN_DSN"
```

Idempotent; already-applied unchanged migrations are skipped; a changed already-applied file
raises on drift. Each application is recorded in the `schema_migrations` ledger (name + SHA-256).

**Standing operational fact: a green init container proves nothing.** The backend init container
has carried FEWER migrations than the live database before (live at 1099 while the image carried
1093). After applying, verify against the DB itself:

```sql
SELECT name FROM schema_migrations ORDER BY name DESC LIMIT 5;
-- must include 1120_governed_planning_observation and 1121_governed_telemetry_outbox
-- (schema_migrations.name stores the FILE STEM — no .sql suffix)
```

## 2. The flag: `FEATUREGEN_INTENT_SHADOW_TELEMETRY`

Read **at the route only** (`src/featuregen/api/routes/contract.py:879` and `:1092`; the builder
and planner stay pure and never read env — `gate1.py` pins this). Default **OFF** (`"0"`).

It has **two effects**, both on the considered-set request path:

1. **Telemetry queueing** (S1B-3 producer): the route threads `telemetry_enabled` into
   `build_considered_set`, which enqueues one outbox work item carrying the run's frozen inputs.
   Enqueue-only — the request path plans nothing extra. Pinned by test: the served response is
   **byte-identical** with the flag on, and the request-path cost is **a query delta of exactly
   two** (one scope-material read + one outbox INSERT) —
   `tests/featuregen/overlay/upload/contract/test_gate1_telemetry_enqueue.py` and
   `tests/featuregen/api/test_contract_telemetry_enqueue.py`
   (`test_the_served_response_does_not_move_when_telemetry_is_on`).
2. **Shadow-plan persistence** (3B.4): the same flag gates `run_shadow_planner(persist=...)` on
   the entity-scoped shadow pass.

**Evidence accrues ONLY while the flag is on.** With it off, the outbox receives nothing, the
worker has nothing to plan, every threshold's minimum-evidence floor starves, and Stage-2 entry
evidence never accumulates. Flipping it is the explicit operator go that starts evidence accrual
— **an explicit user go, never the assistant's**.

## 3. The worker is a deployment — and it has NO OWNER yet

`run_governed_telemetry_once(conn, *, owner: str, now=None, param_chooser=None)`
(`src/featuregen/overlay/upload/governed_telemetry_worker.py:153`) claims one outbox item,
restores its frozen inputs, replans, records observations + demand, completes. **Nothing in the
tree schedules it.** Choosing how it runs is an operator decision; the honest options:

* **A Kubernetes CronJob** invoking a management command. Beware the name collision:
  `python -m featuregen worker` EXISTS, but it runs the durable-runtime queue daemon — it does
  **not** drain the governed telemetry outbox. No subcommand for THIS worker exists today;
  adding one is a (small) code change that must ride a reviewed branch, not this runbook.
* **A sidecar loop** in the backend deployment, calling `run_governed_telemetry_once` until it
  returns `None`, then sleeping.
* **Manual invocation** during the evaluation phase — the only option available with zero code
  changes:

```python
import psycopg
from featuregen.overlay.upload.governed_telemetry_worker import run_governed_telemetry_once

with psycopg.connect(dsn) as conn:          # the worker never commits — the invoker owns it
    while (summary := run_governed_telemetry_once(conn, owner="eval-<your-name>")) is not None:
        conn.commit()                        # persist this item's observations + completion
        print(summary)                       # {"status": "done"|"failed"|"lease_lost", counts...}
```

Semantics worth knowing before running it: default lease 300 s (`claim_telemetry_work`),
fence-guarded completion (a reclaimed item's late results are discarded as `lease_lost`, not
double-written); an item-scoped failure is **terminal** for that item (`status='failed'`, no
silent retry — a failure worth re-running is re-enqueued deliberately); `attempt_count` rises on
every claim including reclaim-after-expiry, so a poison item shows as a rising count; requests
per item are capped at `MAX_REQUESTS_PER_ITEM = 60` — drops are counted in the invocation's
returned summary (its `dropped` key) and logged, and they never reach the report.

**The chooser is injected, evaluation-only.** `param_chooser=None` (the default everywhere — no
production caller wires one yet) is chooser-off, byte-identical to the pre-chooser worker. The
worker reads NO environment: whatever scheduler constructs a real chooser resolves the flag and
API key outside and hands the client in. Provider spend is content-address-bounded via the shared
`structured_result` store — a repeated (parameter, menu, hypothesis, prompt-version) address
replays free;
`chosen` and `invalid_pick` are cached, **`unavailable` is never cached** (a billing outage must
not poison an address forever). Wiring a real chooser starts provider spend: explicit user go.

**Watching it:** the outbox's own columns —
`SELECT status, attempt_count, last_error FROM governed_telemetry_outbox ORDER BY recorded_at DESC`
— plus the report's `volumes` section (`by_mode` counts; `outbox` = `queued`/`leased`/`done`/
`failed`/`retried_items`) and `worker_latency` (p50/p95 `enqueue_to_complete_seconds`, queue wait
included).

## 4. Mode semantics: production evidence is telemetry-mode

`observation_mode` is CHECK-constrained to `live | telemetry` (1120). **Production Stage-1
evidence is `telemetry`-mode** — rows written by the worker from frozen inputs, off the request
path.

`live`-mode rows come only from the entity-scoped branch, which is **route-dead today** — a zero
live-mode count in `volumes.by_mode` is **expected, not a defect**. Whenever that lane becomes
reachable: it writes one observation per **planned recipe** (not refusals-only — the S1B-4 fix
restored the store invariant), and its legacy Template branch stamps
`planning_request_hash = "legacy_template"` (gate1's `_LEGACY_TEMPLATE_REQUEST_HASH`, mirrored as
the report's `LEGACY_TEMPLATE_PLANNING_REQUEST_HASH` and pinned equal by test). The report's
origin split **depends on that sentinel**: rows carrying it are bucketed separately and never
fused into `recipe_v2`.

## 5. The report and the thresholds

* **`GET /governance/cross-catalog-report`** (confirmer-gated: `require_confirmer` = the raw
  `platform-admin` role claim) — the wave-1 evidence surface, `wave1_report` over the observation
  ledger + corpus. Sections: `resolution_by_domain`, `origin_coverage`, `hop_distribution`,
  `authority_floor`, `bridge_demand`, `refusal_taxonomy`, `param_divergence_rate`, `volumes`,
  `worker_latency`, `corpus_status`, `review_activity`, `not_computable_in_stage_1`.
* **The numbers** the SME signs against each section key live in
  [`cross-catalog-stage1-thresholds.md`](cross-catalog-stage1-thresholds.md).
* **The demand queues:** `GET /governance/bridge-demand` (same gate; the three queues + the
  store's `resolution_summary`) and the **`BridgeDemandPanel`**
  (`frontend/src/screens/BridgeDemandPanel.tsx`, embedded in `GovernanceReviewScreen`).

## 6. Known one-time effects and accepted residuals

Each verified in the program ledger and task reports before being written here.

* **The `declarations_output_hash` shift (one-time, at first deploy).** S1A-4a grew
  `TemporalDeclarationV1` (serialized via `asdict`) and `anchor_catalog_source` entered the shadow
  observation payload, so the FIRST deploy carrying this branch re-computes declaration output
  hashes once: a cross-deploy telemetry comparison reads **one round of false drift**. Identity is
  unaffected — `contract_input_hash`/`planner_input_hash` use explicit dicts and are proven stable
  by test; sealed artifacts are unaffected. Accepted residual: there is **no committed pin** for
  the shifted shadow payload (the probe was deleted), so a later payload regression in that lane
  would be caught by nothing but this note.
* **The no-UOA drift ruling (intended re-key, both directions pinned).** Options frozen before
  their intent's first unit-of-analysis confirmation now read as DRIFTED at the materialization
  trigger and **regenerate once** — intended (it aligns materialization with the create-contract
  ladder's live semantic). Absence-vs-absence stays free; absence-vs-later-confirmation is honest
  drift. Both directions are pinned by test.
* **Bridge proposal has no propose route.** `propose_bridge` is ingestion-reachable only (no API
  route, no client); the `BridgeDemandPanel` deliberately carries no propose button and points at
  the entity-bridge decisions list instead. Until the chartered operator propose surface exists,
  bridges are proposed by re-running catalog ingestion over declared join evidence.
* **No production writer for `governed_plan_review_event` yet.** The table, its append-only
  guards, and the report's `review_activity` section exist; only tests insert rows. Recording the
  first real SME review (including corpus `draft → reviewed` promotion) needs either a small
  reviewed write surface or a deliberate operator SQL insert — an explicit-go act either way.
* **Chooser accuracy accrues, it did not land computed.** S1C-3 merged the machinery;
  `chooser_accuracy` stays in `not_computable_in_stage_1` until a scheduler wires a real
  `param_chooser` and shadow rows accrue (§3) — **and** the report gains an accuracy
  aggregation of its own: `NOT_COMPUTABLE_IN_STAGE_1` is a static tuple, no accuracy section
  exists, so computing the number is a report-side code change, part of the gap.
* **Chartered follow-ups are Stage-2 material, not operational actions:** G3 realization
  attachment (identity-impacting — segment identity changes when a revision attaches; plus the G2
  measure-derivation residual and the output_grain-vs-entity_link vocabulary gap), and the
  `concept`-tier promotion that would let the D4-verbatim authority floors bind operationally.
  Nothing in this runbook asks an operator to act on them.

## 7. Explicit-go table

| Action | Changes live state | Requires explicit user go |
|---|---|---|
| Applying migrations 1120 + 1121 to the live DB (`python -m featuregen migrate`) | yes — schema | **yes** |
| Deploying backend/frontend images carrying this branch | yes — running code (and the one-time hash shift in §6 fires here) | **yes** |
| Flipping `FEATUREGEN_INTENT_SHADOW_TELEMETRY` to `1` (or back) | yes — starts/stops evidence accrual and outbox writes | **yes** |
| Choosing and installing a worker schedule (CronJob / sidecar / manual cadence) | yes — a new running workload | **yes** |
| Running the worker manually against the live DB (§3 snippet) | yes — writes observations + demand rows | **yes** |
| Wiring a real `param_chooser` into the worker | yes — starts LLM provider spend | **yes** |
| Recording SME review events / promoting corpus entries `draft → reviewed` | yes — append-only judgement rows the report reads | **yes** |
| Signing (or amending) the thresholds doc | yes — arms the Stage-2 gate numbers | **yes** |
| Reading `GET /governance/cross-catalog-report` / `GET /governance/bridge-demand` | no — read-only | no (needs the `platform-admin` claim) |
