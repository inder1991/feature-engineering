# Ingestion review — architectural findings design (v2)

**Revision note.** v1 was reviewed and found to have real blockers: the transaction-split mechanism was infeasible (`get_conn` holds the request tx open until the route returns, so "enrich before the transaction" and nested `conn.transaction()` savepoints can't be independently-committed phases); it missed Pass B, the enrichment receipt semantics, and the batching-vs-reservation conflict; and it overstated guarantees (exactly-once egress, "milliseconds" lock, audit durability) and under-specified the manifest/stage models. v2 rewrites the mechanisms accordingly. Every claim below was checked against the code.

Constraints unchanged: metadata-only egress; fail-closed MOST_RESTRICTIVE; VERIFIED `approved_join` is the only operational join path; **flag-off byte-for-byte** (now including response JSON — see #22); single-node.

---

## #1 + #26 — Enrichment out of the global event lock (one change, three phases, separate connections)

**Problem (confirmed).** `append_event` holds a transaction-scoped `global_seq` advisory lock until the top-level commit. `_assert_fact` takes it, then concept/definition/domain enrichment **and Pass B `synthesize_tables`** run inside that transaction — so one upload's provider I/O blocks every other event-producing upload system-wide.

**Why v1 didn't work.** `get_conn()` (deps.py:100) commits only after the route returns; the first SQL statement opens that transaction and `ingest_upload` immediately takes its source lock (ingest.py:734). Nested `conn.transaction()` are savepoints, not independent commits. So the split cannot live inside `ingest_upload` on one connection — it must be a **route-level coordinator over separate connections**.

**v2 mechanism — a coordinator (in the upload/connector routes, above `ingest_upload`) with three phases:**

- **Phase 0 — Preflight (request connection, no source lock, no provider).** Parse, run `validate_rows`, and compute a **baseline fingerprint** of the current catalog state for the source (the drift snapshot / graph state the commit will diff against). Only `vr.good` rows are eligible for compute — so rejected/quarantined/brake-held metadata is never sent to the provider (closes the leak the review flagged). No facts asserted yet, no lock held.

- **Phase 1 — Compute (dedicated side connection(s), unlocked, ALL provider paths).** Run every provider path — concept, definition, domain, **and Pass B table synthesis** — here. Each produces a **durable receipt** (not just a warm cache), because `enrich_concepts` records `resolved` only for misses classified *this invocation* and `_write_concept_evidence` consumes only `resolved` — so re-calling against a warm cache writes no evidence. The receipt is the durable, replayable source of truth:

  ```
  EnrichmentReceipt(item):
    task, cache_key, cache_version, prompt_id, schema_id, generation_settings_hash,
    outcome (classified|drafted|unclassified|failed), value, cache_status (hit|fresh_miss),
    llm_call_ref
  ```

  Persisted to a `enrichment_receipt` table keyed by `(ingestion_run_id, task, cache_key)`. Phase 2 consumes the receipt to write evidence + facts deterministically, independent of cache warmth.

- **Phase 2 — Commit (request connection, source lock, then global lock, fast — no provider I/O).** Acquire the source lock; **revalidate**: re-read the baseline fingerprint and abort-and-retry if another same-source ingest changed it (the review's race), and re-run `large_change_brake` under the lock. Then consume the receipt to write `field_evidence`, assert Pass A + Pass B facts, build the graph, and project. The global lock is now held only across DB work.

**#26 — reservation (ships with #1, not separately).** Removing the accidental serialization exposes the concurrent-identical-egress race. Fix with a **leased reservation table**, not a per-item advisory lock (a single batch call covers many content keys — enrich_batch.py:138 — so per-item locks either destroy batching or need globally-sorted multi-locks that deadlock on overlapping batches):

- Reservation key = the **full LLM identity**: `(task, cache_version, prompt_id, schema_id, generation_settings, task_specific_key)` — not merely content hash.
- A batch winner leases its whole key set (one `INSERT … ON CONFLICT DO NOTHING` per key, within one tx, before the call); losers on any key wait/poll and then read the winner's cached result.
- **Guarantee is concurrent-call *coalescing*, not exactly-once** — a process can egress and then crash before caching, and the next request legitimately re-calls. Use the existing full-identity `find_llm_call` lookup as the durable dedup; define **lease expiry + crash recovery** (an expired lease with no result is reclaimable). Describe it honestly as coalescing unless/until the provider offers an idempotency key.

**Audit durability (review #14, corrected).** The dispatched `llm_call` side-connection write currently *falls back to the rollback-prone request connection* on failure, and `EGRESS_BLOCKED` events stay on the request connection (enrich_llm.py:162). v2: the compute phase durably records **both completed and blocked** egress attempts on an independent committed connection, and defines an explicit `audit_degraded` outcome (surfaced in the run + stages) when that write itself fails — never a silent fallback into the rollback path.

**Lock target (review #13, corrected).** The global lock still spans graph reconstruction, Pass C, evidence, projection drains, and quarantine persistence — not "milliseconds." v2 sets a **measured lock-duration target** and a bulk-write plan for phase 2 (batch the evidence/fact/graph writes), plus a **concurrency acceptance test**: a different-source ingest must make progress while a first upload is blocked on provider I/O.

**Deployment (review #15).** #1 and #26 ship behind **one atomic rollout flag** — an old instance that doesn't honor the reservation would defeat coalescing, so it's all-or-nothing per deploy. Flag-off = today's behavior byte-for-byte.

**Acceptance tests:** different-source progress during blocked LLM I/O; same-source baseline revalidation (an interleaved ingest forces abort/retry, not a stale commit); overlapping batch reservations in reverse order (no deadlock); provider timeout/crash mid-compute (lease reclaimed, no orphaned reservation, no double-write); receipt replay writes identical evidence to the pre-change path.

---

## #3 — Durable ingestion-run manifest (generalized, independently committed)

**Problem (confirmed).** No record ties graph/facts back to the upload that made them; held/rejected/parse-failed uploads and connector imports aren't auditable/reproducible. `source_snapshot_id` is a partial precedent but is glossary-only and, critically, **not one seam** — glossary mints `ing_*` (ingest.py:832), Pass C mints `psc_*` (321), Pass B mints `tsy_*` (935).

**v2 model — `ingestion_run` (parent), not a single mutable stamp:**

- **Generalized origin.** `origin_type ∈ {upload, connector}` with origin-specific artifact fields: upload → `filename`, `file_sha256`; connector → a foreign key to the existing `integration_import` record (integrations.py:548 already writes one per attempt — link, don't duplicate). File parsing happens in the route *before* `ingest_upload` (uploads.py:89), so the run must be created **in the route, before parse**, so malformed/oversized/unsupported files still get a run row.
- **Independently committed, reconciled.** Insert the initial `in_progress` run on its **own connection and commit** (so it survives a later rollback), with a **lease/heartbeat**. Update to a terminal state **atomically inside the graph/fact commit tx** for `ingested` (so success can't be recorded for a tx that then fails). Terminal states: `ingested | held | rejected | failed | abandoned` (a lease that expires without terminalization is reconciled to `abandoned` by a sweep). Optionally `cancelled`.
- **Parent/child snapshots.** `ingestion_run_id` is the parent; the existing `ing_/psc_/tsy_` snapshot IDs become **children** stamped with `ingestion_run_id`.
- **Immutable provenance, not one FK on a mutable row (review #9).** `build_graph` deletes+rebuilds every node each run and facts may be unchanged/reused — so a single `upload_id` on a mutable node conflates three different things. Model immutable run↔object and run↔fact associations distinguishing **`observed_in_run`**, **`asserted_by_run`**, **`last_changed_by_run`**, and stamp `run_id` on overlay events.
- **Field spec (review #16).** `pre_source_fingerprint`, `post_source_fingerprint`, `origin_type`, `actor_subject`, `actor_role_claims[]` (array — see #10), `authorization_decision`, `started_at`, `completed_at`, `heartbeat_at`, `row_count`/`quarantined_count` (`CHECK >= 0`), `redacted_failure_code`, `effective_config` (schema-versioned, **allowlisted** flags only — never secrets), `status`, retention policy, and read-authorization (a run is `catalog_read`-gated). **Honesty:** checksum-without-retained-file supports **correlation**, not full reproducibility — stated as such; retaining canonical parsed metadata (not the raw file) is the follow-on if true replay is required.

**Acceptance tests:** parse failure / oversize / unsupported file still yields a queryable run; a commit failure leaves the run non-`ingested` and reconcilable; a connector import links its `integration_import`; a crash mid-run is swept to `abandoned`; unchanged facts on re-upload record `observed_in_run` but not `last_changed_by_run`.

---

## #10 — File-declaration as a first-class authority basis (not a confirmer role)

**Problem (confirmed).** `_assert_fact` stamps *any* uploader as `confirmers: [{subject, role: "data_owner"}]` — fabricating a data-owner vouch for what is really a file-declared auto-confirm.

**Why v1's "via marker" is invalid (review #11).** `_CONFIRMER` is `additionalProperties: False` (facts.py:175) — a `via` field is schema-rejected today. And identity carries **multiple `role_claims`** (envelopes.py) — there is no single "real role" — and the functional `platform_admin` claim is **not** the governance `platform-admin` role (_types.py:61). So neither "record the real role" nor "add a marker" is valid.

**v2 model — a first-class `file_declared` authority basis:**

- Represent an auto-confirmed file-declared fact with a distinct **authority basis** (`basis: "file_declared"`) rather than a human confirmer entry — a fact is authoritative *because a source file declared it and the upload catalog treats the file as the system of record*, which is a different thing from a data owner confirming it.
- This is an **event-schema change**, so it ships with **schema versioning + upcasting**: existing `OVERLAY_FACT_CONFIRMED` events with the legacy `data_owner` confirmer replay unchanged (upcast to `basis: file_declared` at read time, or a new event type/version). Record the uploader's `role_claims[]` array + the authority basis, never a synthesized single role.
- Re-scan every reader of `confirmers[].role` before shipping (the feasibility check found no *gate* on `role == "data_owner"`, but the authority/provenance readers must be updated to understand the new basis).

**Acceptance tests:** a platform-admin upload records `basis: file_declared` with the actor's `role_claims`, never `role: data_owner`; a genuine dual-owner human confirm is unchanged; legacy events replay/upcast identically; no consumer of the confirmer role regresses.

---

## #22 — Per-stage ingestion status (stages self-report; runs are queryable)

**Problem (confirmed).** `ingested` is returned regardless of what happened, and **failures return no `IngestResult` at all** (uploads.py:107). An outer accumulator can't see the truth: concept evidence catches per-item exceptions internally (enrich.py:230), batching discards detailed outcomes, Pass B catches proposal errors internally.

**v2 model — stages return typed reports; status lives on the run:**

- Every stage **returns a typed `StageReport`** (it is not observed from outside a `try/except`). The internally-caught per-item failures must be surfaced *in the returned report* — batch/evidence/Pass B stages return per-item outcomes, not just resolved values.
- **State taxonomy:** `disabled | not_applicable | skipped_no_client | not_run | succeeded | partial | failed | deferred | lagged`.
- **Stage coverage:** parse, validation, brake, fact-assertion, graph-persistence, quarantine, drift, glossary classification, glossary revalidation, each enrichment task (concept/definition/domain), Pass B, Pass C, governed joins, each projector/drain, and manifest finalization.
- **Where it's exposed:** the per-stage detail lives on the **`ingestion_run`** (so even a failed HTTP request has an `ingestion_run_id` + a queryable **`GET /ingestion-runs/{id}`** endpoint, `catalog_read`-gated). This also resolves the flag-off contradiction (review #15): the *response body* of `POST /uploads` does **not** unconditionally gain a `stages` field (that would change flag-off JSON) — the stage detail is retrievable from the run, and any inline `stages` on the response is behind **API versioning / negotiated fields / a rollout flag**, preserving byte-for-byte flag-off compatibility.

**Acceptance tests:** a partial enrichment (some items fail internally) reports `partial` with the failing items, not `succeeded`; a failed HTTP request is retrievable by `ingestion_run_id`; flag-off `POST /uploads` response JSON is byte-identical (no `stages` key) unless the new API version is negotiated.

---

## Cross-cutting: operability, migration, acceptance

- **Rollout flags:** #1+#26 behind one atomic flag; #22's inline response change behind an API version; #3 additive (safe on by default once migrated); #10 behind event-schema versioning.
- **Reconciliation job:** a sweep that terminalizes expired-lease `in_progress` runs to `abandoned` and reclaims expired enrichment reservations (crash recovery).
- **Retention & authz:** runs and receipts have a retention policy; both are `catalog_read`-gated; `effective_config` is allowlisted (no secrets); failure codes are redacted.
- **Mandatory acceptance suite (review #18):** different-source progress during blocked LLM I/O; same-source baseline revalidation; overlapping batch reservations reverse-order (no deadlock); provider timeout/crash recovery; parse-failure and commit-failure both retaining a queryable run; connector import path; multi-role-claim actors; legacy/new event replay; partial stage outcomes; projection poison/lag; exact flag-off API compatibility.

## Revised sequencing (dependencies made explicit)

1. **#3 `ingestion_run` first** — it is the substrate the other three record onto (receipts, provenance, stage reports, authority basis all reference `ingestion_run_id`). Independently-committed run + reconciliation + immutable provenance associations.
2. **#22 stage reports** — build on the run; make the pipeline observable before restructuring it.
3. **#1 + #26** — the coordinator/receipt/reservation restructure, with the run + stage reports already in place to measure and verify it (lock-duration target, concurrency acceptance test).
4. **#10** — the authority-basis event-schema change, on its own, with versioning/upcasting and its own review.

Each is a branch → build → review → merge; #1+#26 ship together behind one flag.

## Open decisions for you

- **#3 reproducibility scope:** checksum-only (correlation) as above, or also retain canonical parsed metadata (not the raw file) for true replay? The latter is more storage + a redaction pass.
- **#22 response contract:** keep stage detail *only* on `GET /ingestion-runs/{id}` (cleanest, zero flag-off risk), or also inline it on `POST /uploads` behind a new API version?
- **#1 compute connection:** a pool of short-lived side connections for the compute phase vs one dedicated connection per request — trade-off is pool pressure vs a connection pinned across compute.
