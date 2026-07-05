# Upload-Driven Catalog Pivot — design

Date: 2026-07-04. Status: proposed direction, pending build.

> **Amended by the [architecture-review resolutions](2026-07-04-upload-catalog-review-resolutions.md).**
> Key corrections: v1 is **catalog/column search, not feature search** (feature nodes/lineage → phase-2);
> facts use a single **`FACT_ASSERTED`/`FACT_RETRACTED`** event (diff-appended), not propose+confirm;
> a small **read-scope authz stays** (the PII map must not be world-readable); enrichment is persisted as
> events; the join/grain contract is **composite-capable**. Read that doc alongside this one.

## The pivot in one paragraph

The platform was built as a **human-governed metadata overlay**: facts about data (grain, join keys,
time columns, sensitivity) are *proposed*, *routed to the data owner*, *confirmed under four-eyes*,
and only then served — with a live database introspected for schema and profiled for facts.
The deployment reality is different: **no database connection, no defined ownership, no approval
step.** So we pivot to an **upload-driven schema + facts catalog with drift detection**: users upload
schema files (Excel/CSV/JSON), the facts ride *in* the upload, ingested facts are served
immediately, and re-uploading a changed file drives drift (stale the facts whose objects changed).
The governance layer (ownership, routing, four-eyes, gate tasks, dual-owner joins) and the
data-scanning profiler are **retired**.

## Target architecture — the funnel

```
  upload file(s)             one canonical shape             the surviving core
  ┌──────────┐   parse+     ┌───────────────────┐  ingest   ┌────────────────────────┐
  │ .xlsx    │──validate──▶ │ rows:              │─(atomic)─▶│ overlay_fact_state     │ (served facts)
  │ .csv     │   per        │  (source, table,   │           │ overlay_fact_dependency│ (drift index)
  │ .json    │   format     │   column, type,    │           │ overlay_catalog_object │ (drift snapshot)
  └──────────┘              │   is_grain, join,  │           │ overlay_drift_watermark│ (freshness)
     one parser             │   as_of, sens.)    │           └────────────────────────┘
     per format             └───────────────────┘                     │
                             large-change brake                  resolve_fact → pipelines
                                                                 (fail-closed on drift)
```

Everything downstream of the canonical shape is format-blind. Adding a format = one new parser.

## Canonical template (the contract)

One row per column. The fact columns are OPTIONAL — blank = pure schema, filled = schema+facts.

| source | table | column | type | is_grain | joins_to | as_of_column | sensitivity |
|--------|-------|--------|------|----------|----------|--------------|-------------|
| deposits | accounts | account_id | integer | Y | | | |
| deposits | accounts | posted_at | timestamp | | | as_of | |
| deposits | transactions | acct_id | integer | | accounts.account_id | | |
| deposits | accounts | ssn_hash | text | | | | pii |

- `source` → `catalog_source` (one per file/system; may be implied by the filename).
- `is_grain` / `joins_to` / `as_of_column` → produce grain / approved_join / availability_time facts.
- `sensitivity` → policy_tag fact.
- A file may carry many sources; a source may span several re-uploads over time (that's what drives drift).

## Ingest pipeline (fail-closed, atomic)

1. **Format detect** — by extension + a content sniff; unknown/corrupt → clean rejection, previous
   catalog untouched.
2. **Parse → canonical rows** — one parser per format, all emitting the same shape.
3. **Validate against the contract** — required cols present, types recognized, `joins_to` targets a
   table present in the same upload, no blank table/column. Report the specific failing rows.
4. **All-or-nothing** — validate the WHOLE file before committing; a rejected upload never partially
   applies (a half-applied upload is itself a false-drift source).
5. **Large-change brake** — if the upload would REMOVE more than a threshold of a source's objects
   (e.g. >30% of tables, or object count collapses), HOLD it and require an explicit
   "this large change is intended" before it drives drift. Distinguishes a real month of small
   changes from a truncated / wrong-source export that would otherwise stale the whole catalog.
6. **Emit facts + drive drift** — a clean upload appends the fact events (auto-active, no approval)
   and runs the drift diff against the source's prior snapshot, staling facts whose objects changed.

## Facts source: in the upload (no profiler)

No DB → the data-scanning profiler cannot run → it is retired. Facts are provided by the user as the
optional columns above. (Phase-2 optional: let the LLM *suggest* those columns against an uploaded
schema — not now.)

## Drift + read (kept, unchanged in spirit)

- **Drift:** each re-upload of a source is diffed against that source's last snapshot
  (`overlay_catalog_object`); dropped/renamed/retyped objects stale the dependent facts, and the
  `overlay_drift_watermark` (+ head_seq) records the scan so reads know the catalog is fresh.
- **Read:** `resolve_fact` serves a fact only when it is active AND its source's drift is fresh
  (within `drift_freshness_sla`) AND the projection has caught up — otherwise **fail closed**.
  Freshness now means "re-uploaded recently enough," an upload-discipline SLA.

## Keep / Build / Delete map (grounded in current modules)

### KEEP (the surviving core)
- `overlay/catalog.py` — the `CatalogAdapter` PROTOCOL + `FixtureCatalog` (test double). Keep the
  seam; `owner_of` becomes vestigial (returns None).
- `overlay/catalog_changes.py` — drift detect + snapshot + watermark. Keep; STRIP the
  reverify-task-opening (`_stale_dependents` just marks facts STALE, no owner task).
- `overlay/dependencies.py`, `overlay/projection.py`, `overlay/resolve.py` — dependency index, read
  models, read path. Keep.
- `overlay/config.py` — keep `drift_scan_interval` / `drift_freshness_sla`; TTL/renewal fields become
  optional/unused.
- `overlay/store.py`, `overlay/state.py`, `overlay/facts.py`, `overlay/identity.py` — event append,
  fold (fewer statuses without approval), fact-type validation, identity + `join_write_error`. Keep.
- The event-sourced backbone (`events`, `projections`, `commands`) — keep; gives drift history +
  rebuild-from-scratch for free.

### BUILD (new)
- `overlay/upload_catalog.py` — an `UploadCatalog` implementing the adapter over parsed rows
  (`list_objects` / `fingerprint`; `owner_of` → None; `get_fact` → served from the uploaded facts).
- `overlay/uploads/` parser layer — `excel.py` first, then `csv.py` / `json.py`, each → canonical rows.
- An **`ingest_catalog` command** — parse → validate → large-change brake → append fact events
  (auto-active) → drive drift. Replaces the propose/confirm workflow.
- The large-change brake + a "confirm large change" override.

### DELETE (governance + profiler — retire)
- `overlay/authority.py`, `overlay/reverify_tasks.py`, `overlay/task_read.py` — ownership/routing/tasks.
- `overlay/confirmation_commands.py`, `overlay/join_confirmation.py`, `overlay/proposal_commands.py` —
  the propose/confirm/reject/enter + dual-owner-join workflow (replaced by `ingest_catalog`).
- `overlay/expiry.py` — time-expiry + renewal pollers (facts don't re-attest without owners; drift
  is what stales them).
- `overlay/profiler*.py` (`profiler`, `profiler_command`, `profiler_heuristics`, `profiler_metrics`),
  `overlay/evidence.py` — the data-scanning profiler (no DB).
- `overlay/catalog.py::PostgresCatalog` — live-DB introspection (no DB).
- `overlay/_lifecycle.py` — keep only `referent_gap` (as ingest-time validation); delete the confirm
  lifecycle helpers.
- `overlay/freshness.py`, `overlay/commands.py` — collapse to the new ingest surface.
- The SP-2 intake layer (`intake/*`) — its LLM feature-definition + governance is out of the immediate
  pivot; fate TBD (kept dormant unless LLM-assisted fact suggestion is wanted).

## Impact on the 72 deep-dive findings

Re-scope, don't fix-all. A majority are in code being deleted (authority bypass, dual-owner join gate,
gate-task lifecycle, four-eyes, `request_edit`, unwired intake collaborators, profiler restricted-role,
clarification tasks, cost-breaker/escalation-ladder tied to the run workflow). **Those become moot.**
Survivors to fix: **BLOCKER #1 drift-scan lag (done)**, the read-path fail-closed items, and the small
backbone bugs that touch the kept core (projection poison-degraded, idempotency-replay action check,
replay-loop skip-count). Everything else is retired with the code it lives in.

## Sequencing

1. **This doc** (done) — direction + keep/delete map.
2. **Vertical slice** (new spine, prove end-to-end): upload one Excel → ingest as a `catalog_source`
   → serve its facts via `resolve_fact` → re-upload a changed version → correct facts go STALE →
   the large-change brake stops a truncated file. TDD, no governance code touched.
3. **Retire governance** — delete the DELETE list once the slice replaces it; this also clears most of
   the findings backlog in one stroke.
4. **Add formats + the brake polish** incrementally.
