# Ingestion first-release hardening — design

**Scope decision.** #1 (enrichment out of the global event lock) and #26 (duplicate-egress coalescing) are **out of first release** — they're a performance/cost concern on a single-node system and their correct form is a distributed-coordination project. This spec covers the three bounded, audit/governance-grade items: **#3 durable ingestion-run manifest**, **#22 per-stage status**, **#10 honest authority attribution**. Every requirement below folds in the two prior review rounds' valid findings.

Constraints: metadata-only egress; fail-closed; VERIFIED `approved_join` is the only operational join path; **flag-off byte-for-byte, including response JSON**; single-node.

Because #1 is out, there is **no compute/commit phase split** — these three record onto the *existing* `ingest_upload` flow as it runs today.

---

## #3 — Durable `ingestion_run` manifest

**Goal.** Every ingestion attempt — upload or connector, accepted, held, rejected, or parse-failed — leaves a durable, queryable record of *who* ingested *what*, *when*, under *what settings*, with *what outcome*, and *which catalog objects/facts it touched*.

**Table `ingestion_run` (read-model, not event-sourced).**
- `id` (the run id; a minted `ing_run_*`), `origin_type ∈ {upload, connector}`, `catalog_source`.
- Upload origin: `filename` (sanitized, length-capped), `file_sha256` **nullable** (a rejected oversized/unreadable file is never fully hashed — a `CHECK` ties nullability to status).
- Connector origin: no filename/checksum; linked from `integration_import` (see FK direction below).
- `actor_subject`, `actor_role_claims` (text[] — identity carries multiple claims, there is no single "role"), `authorization_decision`.
- `pre_source_fingerprint`, `post_source_fingerprint`, each with `fingerprint_algo_version` (the fingerprint contract is defined once — the exact tables/columns, normalization, ordering, and hash — and versioned; see Fingerprint below).
- `effective_config` (jsonb) — an **allowlisted, schema-versioned** snapshot of the flags that governed this run (OVERLAY_PASS_C / OVERLAY_GOVERNED_JOINS / OVERLAY_TABLE_SYNTH / provider on-off / model). Never secrets. Pinned once at run start and passed through (not re-read from env late).
- `row_count`, `quarantined_count` (`CHECK >= 0`).
- `status ∈ {in_progress, ingested, held, rejected, failed, abandoned}` (`cancelled` reserved).
- `started_at`, `completed_at`, `heartbeat_at`, `redacted_failure_code`.
- `catalog_read`-gated for reads.

**Lifecycle (the durability model the review corrected).**
- **Create in the route, before parse, on an INDEPENDENT connection that commits immediately** (so it survives the request transaction rolling back, and so a parse/oversize/unsupported failure still has a run row). Status `in_progress`, `heartbeat_at` set.
- **Terminalize success atomically inside the main graph/fact transaction** (write `status=ingested` in the same tx that commits the facts/graph — so `ingested` can never be recorded for a tx that then fails).
- **Commit-failure and crash:** `get_conn` commits after the route returns, so route code cannot mark a failed commit. A **reconciliation sweep** terminalizes `in_progress` runs whose `heartbeat_at` lease expired to `abandoned`. (`held`/`rejected` are terminalized on their own independent update, since those paths return before the main commit.)
- **Status history is append-only** (a child `ingestion_run_status_event` row per transition) — never an in-place mutable status that loses a concurrent update.

**Connector FK direction (review #2).** `integration_import` is created *after* `ingest_upload` returns, and pull/ingest failures have no import row today. So the **run is created first** (before the connector pull) and `integration_import.ingestion_run_id` points to it — not the reverse. A failed pull still has its run.

**Immutable provenance (review #9), not one FK on a mutable node.** `build_graph` deletes+rebuilds every node each run and facts may be reused unchanged, so a single `upload_id` on a node conflates three different facts. Model three association child tables (or one with a `relation` enum):
- `observed_in_run` — this run saw this object/fact (even if unchanged).
- `asserted_by_run` — this run (re)asserted this fact.
- `last_changed_by_run` — this run changed this object/fact's value.
Stamp **`ingestion_run_id`** on overlay events — **not** `run_id`, which the schema requires to be NULL for overlay fact events (migration 0504). Add a dedicated `ingestion_run_id` provenance column/reference.

**Parent/child snapshots (review #10 v1).** `ingestion_run_id` is the parent; the existing `ing_*` (glossary), `psc_*` (Pass C), `tsy_*` (Pass B) snapshot ids become **children** stamped with the parent run id — not one generalized field.

**Fingerprint contract (review #16).** Define exactly: which tables/columns compose the source's pre/post state (graph_node rows for the source + the drift snapshot), a canonical normalization (the already-normalized refs), a stable ordering, and a versioned hash. For connector runs, a changed baseline forces a **re-preview** (not a silent retry against unapproved state).

**Retention (review #20).** Runs, associations, and status history are referenced by evidence/events. Define: FK/tombstone behavior, retention *classes* (audit rows kept longer than operational detail), and archival — before we claim "durable provenance." First release: keep everything; document the retention seam.

**Honest limit.** Checksum-without-retained-file supports **correlation**, not byte-level reproducibility. Stated in the run's own docs. (Retaining canonical parsed metadata for true replay is an explicit non-goal of first release.)

**Acceptance tests:** parse-failure / oversize / unsupported still yields a queryable run; a held/rejected upload records the right terminal status; a connector import links its `integration_import`; unchanged re-upload records `observed_in_run` but not `last_changed_by_run`; a simulated crash mid-run is swept to `abandoned`; concurrent status transitions don't lose each other (append-only history).

---

## #22 — Per-stage ingestion status

**Goal.** Replace the single opaque "ingested" with an honest, per-stage account of what actually ran — retrievable even when the HTTP request failed.

**Each stage returns a typed `StageReport`** (it is not inferred from an outer try/except). The stages that catch per-item failures internally today — concept-evidence (`enrich.py:230`), batching, Pass B — must **return** those per-item outcomes, not swallow them.

**State taxonomy:** `disabled | not_applicable | skipped_no_client | not_run | running | waiting | retrying | succeeded | partial | failed | deferred | lagged | cancelled | audit_degraded`.

**Stage coverage:** parse, validation, brake, fact-assertion, graph-persistence, quarantine, drift, glossary-classification, glossary-revalidation, enrichment (concept / definition / domain, each), Pass B, Pass C, governed-joins, each projector/drain, manifest-finalization.

**Storage (review #19).** Stage attempts live in a **child table `ingestion_run_stage` keyed by `(ingestion_run_id, stage, attempt)`** with `state`, `started_at`, `completed_at`, `reason_code`, `detail` — not a mutable JSON blob on the run (which loses concurrent updates).

**Exposure (flag-off compatibility, review #13/#15).**
- **`GET /ingestion-runs/{id}`** (`catalog_read`) returns the run + its stage reports. This is the primary surface, and it works for *failed* requests too.
- The **run id is returned via a stable response header** (`X-Ingestion-Run-Id`) on success and on every post-manifest error — so a caller whose request failed can still fetch the run. Headers don't change the JSON body, so **flag-off `POST /uploads` response bytes are unchanged**.
- The `POST /uploads` response body does **not** unconditionally gain a `stages` field. Any inline stage summary is behind a negotiated API version — deferred out of first release; the run endpoint suffices.
- Frontend: the ingest callout fetches the run by the header id and shows the per-stage summary ("enriched • Pass C on • projection lagged — drift will re-run").

**Acceptance tests:** a partial enrichment (some items fail internally) reports `partial` with the failing items, not `succeeded`; a failed HTTP request is retrievable by the header run id; flag-off `POST /uploads` body is byte-identical; concurrent stage writes don't clobber (child rows).

---

## #10 — Honest authority attribution (`source_declared` basis)

**Goal.** Stop recording file/connector-declared facts as if a *data owner confirmed* them. Record the honest basis: the fact is authoritative because a source declared it and the upload catalog treats the source as the system of record — which is different from a human owner vouching for it.

**A first-class authority basis, not a confirmer role (reviews #10/#11).**
- Name it **`source_declared`** with `origin_type` (upload / connector / resolution) — *not* `file_declared` (connectors and reviewer-edited quarantine resolutions aren't "file"-declared).
- It is **not** a confirmer entry (`_CONFIRMER` is `additionalProperties: False` — a `via` marker is schema-invalid) and **not** a synthesized single role (identity has `role_claims[]`; functional `platform_admin` ≠ governance `platform-admin`).

**Event representation — the decision the review demanded (review #12).** Choose the explicit path: **a new `OVERLAY_FACT_CONFIRMED` payload shape, schema-version 2**, that carries `authority_basis: "source_declared"` + `origin_type` + the actor's `role_claims[]`, in place of the fabricated `confirmers: [{role: data_owner}]` for the auto-confirm path only. Specify each layer:
- **Schema:** register a v2 payload variant; the `confirmers` array becomes optional when `authority_basis` is present.
- **Append API:** `_append` currently hardcodes `schema_version=1` — extend it to stamp the version the payload was validated against.
- **Fold:** `state.py:72` currently requires `payload["confirmers"]` — the fold learns to derive authority from `authority_basis` when present, `confirmers` otherwise.
- **Projector / replay:** unchanged operational effect (a `source_declared` fact still projects VERIFIED exactly as today — this changes the *recorded provenance*, not the operational outcome).

**Legacy events (review #10 — do NOT rewrite history).** Existing v1 events used the *same* shape for genuine human confirmations and upload auto-confirms, with no discriminator. A blanket upcast would relabel real governance decisions. So legacy v1 events are read as **`legacy_unspecified`** — never retroactively reclassified. Only *new* auto-confirms get `source_declared`.

**Genuine human confirmations are untouched** — the dual-owner join confirm, the grain/availability single-confirmer, etc. still write real `confirmers`. Only the upload/connector/resolution *auto*-confirm path changes.

**Acceptance tests:** a platform-admin upload records `authority_basis=source_declared` + the actor's `role_claims`, never `role: data_owner`; a genuine dual-owner human confirm is byte-unchanged; legacy events fold as `legacy_unspecified` (no reclassification); the operational projection (VERIFIED → edge/flag) is identical before/after; no consumer of `confirmers[].role` regresses.

---

## Build order

`ingestion_run` is the substrate the others record onto (stage reports are its children; the authority basis references the run's origin). So:

1. **#3 `ingestion_run`** — table, independent-commit lifecycle + reconciliation sweep, provenance associations, connector FK, response header.
2. **#22 stage reports** — the stage child table + typed `StageReport` returns + `GET /ingestion-runs/{id}` + frontend callout.
3. **#10 authority basis** — the v2 event payload + schema/append/fold/projector, legacy handling, its own review.

Each is a branch → build → adversarial review → merge. #10 gets extra review scrutiny (it touches the governance event schema).

## Two small decisions

- **#3 retention:** first release keeps everything and documents the seam — OK, or do you want a retention policy specified now?
- **#22 frontend depth:** just the per-stage summary line on the ingest callout, or a full run-detail view (a "why did this upload do that" screen) reading `GET /ingestion-runs/{id}`?
