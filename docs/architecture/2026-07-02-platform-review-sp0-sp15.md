# Platform Review — SP-0 → SP-15: Issues, Gaps, and Missing Features Blocking World-Class Status

**Status:** Adversarial review (six parallel deep reviews: SP-0 code+design, SP-1 code+design, SP-2 branch+design, SP-3/4/5 designs, SP-6..12 designs with an MRM/regulatory lens, industry benchmark vs Tecton/Feast/Hopsworks/Databricks/bank-internal platforms)
**Date:** 2026-07-02
**Scope note:** The roadmap defines **SP-0 through SP-12 only** — SP-13/14/15 do not exist in any document. That numbering gap is itself finding #0: the platform's missing consumption/serving half (see Phase E recommendation) is exactly the shape of the missing SP-13..15.
**Finding count:** 112 (18 blocker-tier).

---

## 1. Executive verdict

The platform's **governance architecture is genuinely world-class on paper** — arguably ahead of every commercial feature platform and most bank-internal builds. The four-actor authority model, the temporal-vs-semantic leakage split, the fail-closed metadata overlay with confirmation authority, the staged verification stamp (DESIGN→DATA→USEFULNESS-checked), the search-overfitting guard, and structural SoD are exactly what a model-risk reviewer wants and are rare in LLM-era platforms. SP-2's schema/upcaster/closed-enum discipline sets an excellent precedent.

Three systemic problems currently block world-class status:

1. **It is a feature factory without a feature store product.** SP-0..12 ends almost exactly where Tecton/Feast/Hopsworks begin: no online serving, no streaming/event-time compute, no PIT-correct training-set generation API, no consumption SDK, no discovery/search UX, no environments/CI, no brownfield import of the bank's existing SAS/SQL feature estate. The highest-value bank workloads (card fraud at auth, real-time AML interdiction) are rejected at intake *by design*, and even batch consumers have no governed way to read the store — so scientists will hand-roll training joins and reintroduce the exact leakage the seven layers exist to prevent.

2. **The hardest correctness mechanisms are asserted, not designed.** The design's depth is inversely proportional to layer difficulty. The PIT/SCD "engine" is one predicate string (no as-of joins, no bitemporal stance, no reversals/backdated-transaction handling); the label/target pipeline that all IV/WoE scoring depends on is owned by no SP; batch materialization has no engine, scheduler, or restatement semantics; the "deterministic PIT gate over arbitrary LLM SQL" (SP-6) has no mechanism (it's a known-hard problem); the fairness gate (SP-7) requires protected-attribute data the platform's own policy engine blocks everywhere; and evaluation's data environment is an unresolved contradiction (masked sandbox data destroys the very distributions IV measures).

3. **The built code's bank-grade guarantees are convention, not mechanism.** Identity is fully self-asserted (any caller mints any principal); four-eyes/SoD is opt-in and forgeable via caller-supplied strings; the "tamper-evident" audit chain is keyless and TRUNCATE-able; the event table has no write-once trigger; `resolve_fact` fail-opens past fact expiry; the profiler's schema allowlist is caller-supplied; the intake compliance screen is a substring matcher (rejects "average mortgage balance", passes "ethnic background"); and **no production daemon exists anywhere** — outbox relay, timer poller, projection runner, expiry/drift pollers are invoked only from tests. Recurring theme: *a correct kernel mistaken for a working system.*

## 2. Cross-cutting patterns

- **Promised-in-design, owned-by-no-SP:** Domain/Use-Case Catalog service (three layers consume it; nobody builds it), label pipelines, real-data evaluation environment, fairness-testing enclave, model→feature consumer registration, monitoring executor before Phase D, three-party independent-validation workflow, consumption APIs, online serving landing zone.
- **Sequencing hazards:** features reach `PRODUCTION` in Phase B/C while the monitoring executor ships in Phase D; exposure/row-column enforcement (SP-11) lands *last* while Wave-2 AML data (SAR labels — criminal tipping-off exposure) arrives second; duplication/cost/backfill packs arrive (SP-7) after registration+materialization ship (SP-5).
- **Test-green ≠ working:** SP-0 runtime daemons, SP-1 freshness/drift/projection drivers, SP-2's real-LLM path and hypothesis mode all pass CI and cannot function in production (no wiring, no composition root, fixtures bypass the missing pieces).
- **Fail-open in a fail-closed system:** `resolve_fact` ignores `expires_at`; unknown LLM stop_reason → `PROVIDER_OK`; analytics projections silently skip poison events; four-eyes fires only when the caller sets a flag; handler exceptions bypass the DLQ and retry forever.
- **Threshold governance unassigned:** IV cutoffs, doubt-router env vars, PSI thresholds, overfitting bars, cost ceilings — none versioned, owned, or change-controlled.

## 3. Recommended roadmap amendments

1. **Declare Phase E and mint SP-13/14/15** (the numbering the roadmap is missing):
   - **SP-13 — Online serving + streaming:** low-latency online store, streaming ingestion with governed window aggregations compiled from the same DSL, on-demand/request-time transform class (Path-1-only), training/serving equivalence testing. Make three cheap design commitments **now** so this isn't a rewrite: dual-target-compilable DSL ops (SP-4), online-aware registry schema (`serving_mode`, latency class, freshness contract — SP-5), Path-2 SQL stays batch-only.
   - **SP-14 — Consumption:** PIT-correct training-set generation service (`get_training_set(spine, features[])` → versioned dataset), Python SDK, batch-lookup read path with use-case-scoped authz, REST registry API, model-consumer registration (also feeds SP-9/SP-10).
   - **SP-15 — Adoption:** discovery/search console (entity/use-case/data-class facets, usage, lineage), brownfield import mode (register-as-is with honest DESIGN-CHECKED stamp; LLM-assisted SAS/SQL→contract extraction = the intake agent pointed backwards), environments (dev/UAT/prod) + promotion, features-as-code for power users.
2. **Cap all Phase B/C output at `APPROVED_EXPERIMENTAL`** until SP-7 (overfitting guard, semantic leakage, fairness) and SP-10 (monitoring executor) exist — otherwise SP-5's milestone violates the design's own hard gates (§10, §14.5).
3. **Pull exposure enforcement out of SP-11 into Phase B/C** (before AML data is touched); leave only Path-3 authoring in Phase D. State where row/column enforcement executes.
4. **Add a Domain Catalog service owner** (extend SP-1 or a small SP): versioned store, owner/Compliance confirmation, read API. Add a **Label/Target service** to SP-5's scope with its own PIT validation.
5. **Hardening sprints before new construction:** SP-0.5 (real authn boundary, DLQ/poison fix, WORM/signed audit, daemons, migrations framework, observability, KMS) and SP-1.5 (read-time expiry enforcement, pre-expiry renewal, group ownership/delegation, bulk confirmation, adapter protocol for real catalogs) before SP-3 consumers freeze current interfaces into their assumptions.
6. **Write real specs for SP-3/4/5/6/7** making the same schema-and-mechanism commitments SP-2 made; downgrade the reference architecture's "🔭 Designed" status for SP-6..12 to "outlined" until per-SP specs exist.
7. **SP-2 branch merge blockers:** classifier rewrite (word-boundary/lemma + adversarial tests), working real-LLM path, raw-text/document-body persistence, hypothesis-mode wiring (or explicit command-denial), `risk_flags` derivation from the domain catalog.

---

## 4. SP-0 backbone — findings (code + design; built, ~906 tests)

**1. Identity is fully self-asserted — any caller can mint any principal with any roles** | BLOCKER | architecture
Evidence: `src/featuregen/identity/build.py:12-27` (string-shape checks only), `:47` (`authenticated=True` hardcoded), `:18` (service "attestation" = non-empty string); `src/featuregen/authz/policy.py:91`; `src/featuregen/commands/api.py:104` (consumes `cmd.actor` verbatim). No JWT/OIDC/JWKS/SAML dependency; zero token verification code.
Why: every control — authz, four-eyes, three-party validation, audit attribution — rests on forgeable `role_claims`/`subject`. Design §6.1 promises "attested, not self-asserted"; no mechanism exists.
Fix: real authn boundary (OIDC/JWKS for humans; SPIFFE/mTLS for services); `IdentityEnvelope` constructible only by the verifier.

**2. Handler exceptions bypass the DLQ — infinite poison-retry with head-of-line partition blocking** | BLOCKER | code-bug
Evidence: `src/featuregen/runtime/dispatch.py:135` (one outer tx wraps claim→handle→commit), `:146` (`registry.get` KeyError outside any try), `:152-159`/`:172-176` (only `HandlerTimeout`/`ConcurrencyError` caught); rollback discards `claim_one`'s `attempts+1` (`runtime/queue.py:71-85`). Verified line-by-line.
Why: a raising handler/unknown handler name retries forever, never DLQs, and wedges its aggregate partition with no operator signal.
Fix: top-level `except BaseException` → durably increment attempts in own committed tx → `fail_retryable`/`fail_permanent`; test a plain-raising handler.

**3. No production worker/relay/poller daemon exists — the durable runtime is library primitives only** | BLOCKER | gap
Evidence: no `src/` caller of `process_one`, `relay_publish_batch`, `poll_due_timers`, `fire_timer`, `recover_stuck`; no `__main__`/`[project.scripts]`/scheduler; no Dockerfile/k8s/IaC.
Why: outbox, queue, timers, blob-GC, cost-breaker exercised only by tests; nothing to deploy, health-check, or scale.
Fix: worker/relay/timer daemons with intervals, graceful shutdown, liveness, scaling policy; deploy artifacts.

**4. "Tamper-evident" audit is a keyless hash chain in the same DB — defeated by TRUNCATE or any privileged writer; events table has no write-once trigger** | BLOCKER | architecture
Evidence: `src/featuregen/security/audit.py:34-68` (unkeyed SHA-256, no HMAC/signature), `:205-250` (`verify_chain` returns True on empty table); `db/migrations/0071_security_audit_append_only.sql:20-22` (row trigger doesn't cover TRUNCATE); no `events` write-once trigger (grep-verified).
Why: both audit substrates physically editable; `TRUNCATE security_audit` + `verify_chain` → True. No WORM/signing/external anchor.
Fix: HMAC/sign entries with an external key; publish signed chain heads to WORM/off-box; revoke UPDATE/DELETE/TRUNCATE from app role; add events write-once trigger.

**5. Global-order correctness bought with a platform-wide single-writer lock** | MAJOR | architecture
Evidence: `events/store.py:41,105-106` (`pg_advisory_xact_lock` on every append across all aggregates, held to commit); shared `global_seq_seq` across events/documents/security_audit; projections read `WHERE global_seq > checkpoint`.
Why: hard throughput ceiling for thousands of concurrent requests; and if any allocator (e.g. documents) skips the lock, the commit-order gap bug returns.
Fix: per-partition sequences + gap-tolerant/low-watermark projection reader; verify all allocators share one discipline.

**6. Four-eyes/SoD dual-control is opt-in and forgeable via caller-supplied `requested_by`** | MAJOR | code-bug
Evidence: `authz/sod.py:96-104` (fires only if caller sets `compliance_sensitive` AND supplies `requested_by`; else allow), `:8-13` (bare string inequality; case-sensitive; `service:` vs `user:` identities of the same human pass); author vs responders resolved inconsistently (`:24` vs `:37`).
Why: a single insider approves and activates their own feature by omitting a flag or fabricating a string.
Fix: mandatory dual-control derived from attested identities of recorded actions; canonicalize/link subjects.

**7. Break-glass review independence checked against caller-supplied strings; grants no scoped/expiring capability** | MAJOR | code-bug
Evidence: `security/break_glass.py:105-116` (reviewer checked against *parameters*, not the stored `:56-63` invoker/co-signer); `break_glass` envelope flag never consulted by authorizer; review completion unenforced.
Why: invoker can self-approve their own emergency-access review; break-glass is a logging ceremony, not a control.
Fix: read parties from the stored event; mint scoped expiring capability; block/alert on unreviewed break-glass past SLA.

**8. No migration framework — no version ledger, rollback, or ALTER path** | MAJOR | operational
Evidence: `db/migrations.py:227-262` — `CREATE IF NOT EXISTS` strings re-executed wholesale; no `schema_migrations` table, checksums, or down-migrations; extensions require hand-rolled ALTERs against auto-named constraints.
Fix: Alembic-style versioned ledger, checksums, per-migration transactions, named constraints.

**9. No KMS implementation — crypto-shred unverifiable; blobs marked shredded even on no-op destroy** | MAJOR | gap
Evidence: `privacy/kms.py` is a 12-line Protocol with no implementation; `crypto_shred.py:126-131` ignores `destroy()` result then sets `status='shredded'`; `governance/replay.py:70-71` labels a *missing* blob "shredded".
Why: GDPR Art. 17 requires demonstrable destruction; data loss masquerades as compliant erasure.
Fix: real KMS binding + proof-of-destruction; distinguish "key destroyed" from "blob missing".

**10. Legal hold honored only at blob scope; erasure/hold ops ungated and unaudited** | MAJOR | code-bug
Evidence: `crypto_shred.py:112` checks only `("blob", blob_id)` though holds support subject/run/feature scopes (`0820_legal_holds.sql:5-6`); `crypto_shred`/`place_legal_hold`/`release_legal_hold` never validate identity, check authz, or write security_audit.
Why: spoliation exposure — anyone can lift a litigation hold then shred, silently.
Fix: resolve holds across all scopes; authz + dual-control + audit on erasure/hold lifecycle.

**11. Observability effectively absent** | MAJOR | operational
Evidence: one logger in all of `src/`; no metrics/tracing/health endpoints; DLQ `dead` rows, `stale_ignored`, `degraded` projections accrue with no alert.
Fix: structured logs + correlation IDs, queue/outbox/projection-lag/DLQ metrics, tracing, health, alerting.

**12. Audit and denial writes are in-band with the audited transaction — lost on rollback** | MAJOR | architecture
Evidence: `security/audit.py:176-184`, `privacy/audit_read.py:58-68`, `authz/authorizer.py:19-23` (denials recorded inside the command tx, then raise).
Why: failed access attempts — the highest-value security signal — can vanish.
Fix: autonomous transaction / separate connection / guaranteed outbox for security records.

**13. Declarative state machine not wired into the runtime; table-versioning inert** | MAJOR | architecture
Evidence: `evaluate_transition`/`TransitionTable` uncalled from `runtime/`; `state_machine/migrations.py:76-81` appends `*_VERSION_MIGRATED` but nothing updates `run_workflow_state.table_version` (only read, `aggregates/_append.py:31-39`).
Why: transition legality is ad-hoc guards; the headline "migrate in-flight instances" capability does nothing.
Fix: route transitions through the engine; make migration commands advance the pinned version.

**14. `supersede` performs unconditional overwrite when `expected_prior` omitted** | MAJOR | code-bug
Evidence: `aggregates/consumers.py:114` (CAS optional), `:147-155` (`ON CONFLICT DO UPDATE` replaces active slot); contrast mandatory CAS in `activation.py:104-111`.
Why: flagship no-silent-clobber property bypassable on one of two identical paths.
Fix: mandatory CAS on `supersede`.

**15. Near-zero concurrency/crash-recovery/load testing** | MAJOR | gap
Evidence: 906 tests; exactly one uses threads; no kill/restart/multi-worker/volume tests; no property-based tests.
Why: the outbox lease, one-inflight guard, timer poller, reclaim, crash recovery are unproven under the conditions they exist for (this is why the poison loop went unnoticed).
Fix: multi-worker contention, mid-tx kill, Postgres-restart, replay-at-scale benchmarks.

**16. No snapshotting — full-stream replay on every command** | MAJOR | operational
Evidence: `events/store.py:144-177` loads whole stream; no snapshot table; lifecycle predicates replay every sibling per command.
Fix: aggregate snapshots + tail replay; projection rebuild checkpoints.

**17. Unpartitioned unbounded core tables; churny queue tables share the event store instance; no retention enforcement** | MAJOR | operational
Evidence: zero `PARTITION BY`; `runtime/ddl.py:10-14` "partitioned" = a text column; nothing schedules `prune_processed_messages`; `retention_class` unenforced.
Fix: partition events/documents; separate/broker the queues; schedule pruning; enforce retention classes.

**18. Single Postgres for ~12 responsibilities; no DR/HA/multi-region, pooling, or timeouts anywhere** | MAJOR | architecture
Evidence: DR/HA/RPO/RTO/replica/failover unmentioned in design + all ten plans; no pool/`statement_timeout`/SSL config.
Fix: DR/HA topology with tested RPO/RTO; pooled connections + timeouts; capacity analysis for Postgres-as-queue.

**19. Schema-evolution gaps: deprecated document versions writable; document upcasters unwired on read; upcaster availability import-order-dependent** | MAJOR | gap
Evidence: `events/store.py:56` guards events only; document reads never upcast; `events/registry.py:101-121` checks only the in-memory registry at persist time.
Fix: `assert_writable` on document writes; upcast-on-read in `get_document`; verify upcaster coverage at startup, fail closed.

**20. Analytics projections silently skip poison events with no record** | MAJOR | code-bug
Evidence: `projections/runner.py:53-61` — `continue` + checkpoint advance, nothing written to `projection_degraded`.
Why: wrong numbers in analytics/regulatory read models with no signal (BCBS 239 accuracy/completeness).
Fix: durable skip ledger + monitored metric.

**21. Out-of-band projection mutations break deterministic rebuild** | MAJOR | code-bug
Evidence: `aggregates/run_lifecycle.py:172-184` (`resolve_degraded` clears state with no event — rebuild re-blocks); `aggregates/feature_lifecycle.py:111-115` (direct UPDATE of `feature_active_versions`).
Fix: all projection changes flow from events only.

**22. Experiment-expiry timers route to an unregistered handler and drop feature refs — expired experiments never auto-deactivate** | MAJOR | code-bug
Evidence: `aggregates/activation.py:53-74` schedules; `runtime/timers.py:205` routes to `"timer.experiment_expiry"` (registered nowhere), `:206-218` drops payload refs.
Why: design §5.8's "experimental approvals auto-expire" guarantee is false; unapproved versions stay live indefinitely (and poison-loop per #2).
Fix: register the handler; carry full payload; test auto-deactivation.

**23. Retry budget + jittered backoff are dead code; live paths use zero jitter** | MINOR | code-bug
Evidence: `runtime/retries.py` imported nowhere; live paths (`queue.py:111-129`, `outbox.py:143-160`) cap on attempts only, `jitter=0.0`.
Fix: wire or delete; enable jitter + elapsed-time cap.

**24. CI gates on neither types, coverage, nor security; `anthropic` dependency undeclared** | MINOR | operational
Evidence: `.github/workflows/ci.yml:29` (mypy `continue-on-error: true`); no coverage floor, bandit/pip-audit/SBOM; `intake/llm_claude.py` imports `anthropic`, absent from `pyproject.toml`.
Fix: blocking mypy, coverage, security scanning; declare runtime deps.

**25. Dead "unreachable in production" branches; non-unique `llm_call_idem_idx`** | MINOR | code-bug
Evidence: `intake/bootstrap.py:30`, `intake/commands.py:2190,498`; `0510_llm_call_store.sql:37`.
Fix: remove/assert dead branches; make the index UNIQUE or rename.

**SP-0 verdict:** the paper design is genuinely strong and much of the mechanism is careful — but four headline guarantees don't survive adversarial reading as implemented (identity, tamper-evidence, dual-control/break-glass, durable runtime), and the systemic absences (IdP, KMS, DR, migrations, snapshotting, observability, daemons) make it a rigorous single-node prototype substrate, not yet a bank-grade backbone.

---

## 5. SP-1 metadata overlay — findings (code + design; built, merged)

**1. The entire freshness/read-model machinery has no production driver** | BLOCKER | operational
Evidence: `overlay/bootstrap.py:27-36` registers no scheduler; `expiry.py:93`, `catalog_changes.py:67`, `projections/runner.py:28` invoked only from tests; no CLI/daemon/`[project.scripts]`.
Why: deployed, facts never expire, drift is never detected, the merged view never advances — §6.6 "can't silently rot" is inert.
Fix: operational runner (CronJob or in-process scheduler) driving projection + both pollers, with lag metrics and checkpoint-age alerts.

**2. No pre-expiry re-verification path — every fact gets a recurring fail-closed outage window** | BLOCKER | architecture
Evidence: `_lifecycle.py:27` (`_AWAITING_CONFIRMATION` excludes VERIFIED) + `confirmation_commands.py:63-68,253-255`; `_DEFAULT_TTL` hardcoded 180d (`_lifecycle.py:31`), unjittered, not per-fact-type.
Why: re-confirm is only possible *after* dependents are already blocked; onboarding-wave facts all expire the same day, stalling whole domains.
Fix: open re-verify at `expires_at - grace` while serving VERIFIED until expiry; per-fact-type/risk-tier TTL with jitter.

**3. `resolve_fact` fail-opens past expiry and under projection lag** | MAJOR | code-bug
Evidence: `resolve.py:190-226` — no `expires_at <= now()` check; reads an async poll-driven projection, so STALED/EXPIRED events already in the stream still serve as VERIFIED. No test covers expired-but-unpolled reads.
Why: fail-open in the one component whose contract is fail-closed; for availability-time facts this is the leakage vector the overlay exists to prevent.
Fix: read-time expiry (and checkpoint-staleness) enforcement, blocking with `expired_pending_reverify`.

**4. Ownership is a single human subject string — no teams, delegation, absence, or re-org handling** | MAJOR | gap
Evidence: `catalog.py:49` (`owner_of -> str | None`); `authority.py:140-157`; `task_read.py:43-46` (only the stamped subject may read); SP-0's `task_delegations` never consulted.
Why: owner on leave → confirmations stall; after re-org the new owner can confirm but can't find/read the task, the old owner can read but not confirm.
Fix: principal-set/group ownership; honor delegations; task re-route on ownership change.

**5. No bulk confirmation ergonomics — cannot onboard a real estate (100k+ columns)** | MAJOR | missing-feature
Evidence: `proposal_commands.py:33-154` (one command/stream/task per fact_key); `policy_tag` per column × use_case with no data-class inheritance; no batch APIs, no enumeration/worklist API.
Why: potentially millions of individual confirms; Compliance won't click a million times — fail-closed then means nothing launches.
Fix: data-class/column-group facts, bulk propose/confirm (one task per owner per batch), worklist API.

**6. Catalog adapter too thin for Collibra/Alation/DataHub/Purview** | MAJOR | architecture
Evidence: `catalog.py:42-51` — pull-only, whole-catalog `list_objects()`/`fingerprint()`, per-fact synchronous `get_fact`, no pagination/batching/change events/auth/retry; process-global single adapter (`catalog.py:223-243`) ignores `ref.catalog_source`.
Why: N+1 remote calls per grounding; drift detection pages 100k objects per poll instead of consuming change events; singleton bakes in single-catalog assumptions SP-2 consumers will code against.
Fix: batched `get_facts`, `changes_since(cursor)`, adapter registry keyed by source, read-through cache with staleness contract.

**7. Profiler schema allowlist is caller-supplied — a safety control that self-attests** | MAJOR | code-bug (fail-open)
Evidence: `profiler_command.py:55` (`allowed_schemas` from `cmd.args`); read-only role simulated via `SET LOCAL transaction_read_only` on the full-privilege connection (`:65-76`).
Why: any principal with `run_profiler` can profile HR/payroll/GL by naming it in their own allowlist.
Fix: server-side sealed allowlist; genuinely restricted read-only role.

**8. Governance-queue "repair ownership and re-route" — the designed default — does not exist** | MAJOR | missing-feature
Evidence: design §6 step 1 vs `commands.py:35-41` (no repair/re-route command); reference adapter `owner_of` always None (`catalog.py:206-213`) → every data fact routes to platform-admins, who become de facto confirmers of everything.
Why: break-glass is the only action, inverting the control and hollowing the data-owner SoD story; direct-confirms are indistinguishable from normal confirms in the events.
Fix: ownership registry + `repair_ownership`/`reassign_task`; audit direct-confirms as overrides.

**9. Confirming a STALE fact re-affirms values referencing dropped/renamed objects** | MAJOR | code-bug
Evidence: `confirmation_commands.py:96-106` (default = `prior_value`, validated only against JSON schema, never the live catalog); `catalog_changes.py:102` advances the snapshot so the dangling name never re-triggers.
Why: drift detection becomes drift laundering; grounding emits SQL against nonexistent columns.
Fix: on STALE/REVERIFY confirm, validate referents against the adapter; force explicit override when a referent is gone.

**10. Multi-catalog identity already broken in the persistence layer** | MAJOR | architecture
Evidence: `fact_key` embeds `catalog_source` (`identity.py:65-89`) but `display_object_ref` omits it (`identity.py:92-99`) — and that string keys the dependency index, fingerprint snapshot, and fixture maps.
Why: second catalog onboarded → `core.transactions` collisions; drift in catalog A stales facts about catalog B.
Fix: key `ref_object` + snapshots on `(catalog_source, object_ref)` now, while tables are small.

**11. Catalog-change detection doesn't scale and floods tasks unbatched** | MAJOR | operational
Evidence: `catalog_changes.py:42-64` (one round-trip per object per poll + full anti-join DELETE); staling + task-opening synchronous in one transaction (`:98-101`); `columns_fingerprint` written as `""` (`:57`) — dead column, detects nothing.
Why: a routine warehouse migration becomes one giant lock-holding transaction then thousands of ungrouped re-verify tasks.
Fix: batch writes, chunked staling, tasks grouped per (owner, change-set) with a summary.

**12. Fact vocabulary too narrow; `use_case` an unvalidated free string** | MAJOR | gap
Evidence: `facts.py:14-22` (five types; no DQ/lineage/retention/sensitivity facts); `use_case` never validated against the domain catalog — `"retail-churn"` vs `"retail_churn"` mint distinct fact_keys.
Why: policy surface fragments invisibly; SP-2/SP-3 need column↔data-class linkage the overlay can't express.
Fix: validate `use_case` against the loaded catalog; add `sensitivity_label`, `retention`, `data_class_membership` fact types.

**13. Overlay tasks have no SLA ladder and no notifications** | MINOR | operational
Evidence: `proposal_commands.py:140-152`, `reverify_tasks.py:29-38` omit `sla`, so SP-0's reminder/escalation/auto-park ladder never arms; §8's "owners are notified" unimplemented.
Fix: per-gate SLAs + outbox notification on open/escalation.

**14. Governance-side partial confirmation leaves its own task dangling open** | MINOR | code-bug
Evidence: `join_confirmation.py:93` closes by `subject`, but governance tasks are stamped `{"role","side"}` with no subject (`authority.py:80-89`; `_lifecycle.py:88-93`).
Fix: match partial-close on the `side` label.

**15. Same-owner `approved_join` value override can diverge from the fact's identity** | MINOR | code-bug
Evidence: `confirmation_commands.py:79-80` routes only dual-authority joins to the no-override path; generic path (`:101`) accepts an override changing `from_ref`/`to_ref`/`column_pairs`.
Fix: reject overrides whose identity fields don't re-derive to the same `fact_key` (or ban overrides for this type).

**16. Profiler inference is name-token cosmetics, not the promised analysis** | MINOR | gap
Evidence: `profiler.py:71-76` (basis = "post" substring), `:40-41` (SCD by name tokens); no monotonicity-vs-event-time check (design §5 promised it); pairs-only grain search (`profiler_heuristics.py:102-121`); `SELECT count(*)` full-scans (`profiler.py:92-96`); one candidate per run per fact_key.
Fix: `reltuples` estimates, the specified monotonicity/lag metrics, ranked candidates in one evidence record.

**17. Confirmation idempotency deviates from the spec** | MINOR | code-bug
Evidence: design §9 promises idempotence by `(fact_key, draft_event_id)`; a retried confirm returns `accepted=False "not awaiting"` (`confirmation_commands.py:63-68`).
Fix: detect already-applied and return original success.

**18. Evidence retention and read control asserted but not implemented** | MINOR | gap
Evidence: `read_evidence` (`evidence.py:62-68`) has no authorization; `overlay_evidence` has no retention/legal-hold/shred linkage despite §5.1's "governance-retained, read-controlled".
Fix: SP-0 audit-read pattern on evidence reads; retention schedule tied to fact lifecycle.

**SP-1 verdict:** the write side (event-sourced fact aggregate, CAS confirmation races, dual-owner join choreography, schema enforcement) is careful and well-tested; but nothing operates it, the freshness design guarantees recurring outages, the read path fail-opens, and the human layer models one immortal employee per table. Schedule SP-1.5 hardening before SP-2 consumers freeze these interfaces.

---

## 6. SP-2 intake — findings (in-flight branch + design)

**1. Prohibited-class/banking-boundary screen is naive substring matching — terminally rejects legitimate intents and is trivially bypassed** | BLOCKER | code-bug/safety
Evidence: `intake/banking_catalog.py:185-198,222-227` (`term in text`). Reproduced: "average mortgage balance per customer over 90 days" → `PROHIBITED_DATA_CLASS` ("age" ⊂ "average"/"mortgage"); "trace of failed transactions" → blocked ("race" ⊂ "trace"); "ethnic background", "religiosity" → CLEAR.
Why: the platform's only hard compliance block fires a no-regression-locked terminal reject on ordinary banking vocabulary while actual protected-attribute intents pass with trivial morphology. Indefensible in both directions.
Fix: word-boundary/lemma matching + curated synonym expansion; adversarial test corpus; borderline → clarification, not terminal reject.

**2. The real-LLM path is unimplemented and unusable as wired** | BLOCKER | gap/llm-ops
Evidence: `llm_claude.py:106-115` (output schema is a NOTE comment, never attached); repair errors never rendered into the re-prompt (`llm.py:241` vs `llm_claude.py:100-104`); every call site pins `provider:"fake", model:"fake"` (`commands.py:506,957`; `critique.py:41`; `candidates.py:220-225`); `build_claude_llm` has zero callers; no production composition root (`_wire` lives only in `test_e2e.py:129`).
Why: Decision D5's "config-gated real adapter" cannot function; audit columns would record lies; the branch has never had a viable path to a real model.
Fix: attach registry schema via structured outputs; render repair errors; config-driven provider/model; production composition root.

**3. Raw intent and document bodies never persisted — dangling `blob_` refs break the audit/MRM chain** | BLOCKER | code-bug/gap
Evidence: `commands.py:868` (`raw_input_ref = mint_id("blob")`, no blob write; `register_blob` never called from intake); `_emit_document` `commands.py:564` (body never stored) — while schemas assert "raw text is NEVER inline" and the ledger is "needed for MRM reproduction".
Why: cannot reproduce what the user asked or verify LLM fidelity; the design's retention claims are false in code.
Fix: persist raw intent + bodies encrypted/access-controlled to the blob store, or re-specify the retention model honestly.

**4. Hypothesis mode dead end-to-end in production: generation unwired, candidate bodies unreadable** | MAJOR | gap
Evidence: `generate_candidate_docs`/`current_candidate_generator`/`StubCandidateGenerator` have zero src callers; `mcv.calculation_method_available` (`mcv.py:90-93`) requires candidates → every hypothesis intent parks; `_persist_contract_body` (`candidates.py:417-431`) writes a `blob_index` row but never the bytes; tests seed candidate docs directly.
Why: one of SP-2's two headline modes can't reach Gate #1, and the "select from scored candidates" UX has no readable data. Suite is green because fixtures bypass the gap.
Fix: wire generation into submit/advance; persist candidate bodies before merge — or command-deny hypothesis submits until then.

**5. `requires_independent_validation` can never fire on the production path** | MAJOR | code-bug
Evidence: `commands.py:1878-1881` reads `draft_body["risk_flags"]`; nothing in src produces `risk_flags` (only test fixtures); `assemble_draft_body` (`commands.py:443-456`) drops it even if LLM-emitted; no catalog→risk mapping.
Why: the sole SP-2 hook routing credit-decisioning features to independent validation is constantly False in production.
Fix: derive `risk_flags` deterministically from the `BankingDomainCatalog` during `_produce_draft`; production-path test.

**6. PII boundary is three regexes plus trust in the caller; clarification answers inherit the wrong classification** | MAJOR | gap/safety
Evidence: `redaction.py:30-34` (EMAIL/SSN/PAN only), `:73` (`clean` → no scan); `commands.py:519-531` (trusts caller-supplied classification); `commands.py:1200-1207` (answers redacted under the original intent's classification). No residency/DLP statement; `read_llm_call` unauthorized.
Why: names, addresses, DOBs, IBANs, phone/account numbers in a "clean" intent or any answer flow verbatim to the external provider.
Fix: always scan; NER/DLP-grade detection; classify each answer independently; region-pinned endpoint documented; read-control the llm_call store.

**7. No prompt versioning binding, change control, eval harness, or model-upgrade gate** | MAJOR | llm-ops
Evidence: `prompt_version` a free int (`commands.py:502-503`); template an f-string with no registry/hash — editing it silently reuses stale idempotent records (`llm.py:382-420`); zero mention of golden sets/offline eval in all 10 plans; model swap = env var.
Why: prompt/model changes alter a regulated pipeline with no approval trail, no quality gate, broken audit linkage. Fails change control at the first tweak.
Fix: versioned prompt registry (content hash on each `llm_call`); change control; labeled golden intake set as a gated eval.

**8. Doubt scores are uncalibrated LLM self-reports; router thresholds ungoverned env vars** | MAJOR | llm-ops/architecture
Evidence: `scoring.py:45-68` (face-value ambiguity/confidence); deterministic cardinality check covers entity + first filter only (`commands.py:1131-1143`); thresholds env vars with silent fallback (`doubt_router.py:20-33`); `mcv.py:52` hardcodes a diverging 0.30.
Why: what a human must review vs what the machine silently settles is decided by a model grading its own homework, tunable via shell, unaudited.
Fix: stamp thresholds+version onto auto-resolve events; single-source constants; calibration eval before trusting self-reports.

**9. No SLA, abandonment handling, or resume path — runs hang or park forever** | MAJOR | operational
Evidence: every `GateTaskSpec` omits `sla` (`commands.py:579-586,1107-1114,1775-1786`) so SP-0's ladder never arms despite design §2's promise; no unpark/resume command for the three park owners.
Fix: SLAs on clarification/Gate #1 (machinery exists); governance unpark command.

**10. LLM network calls run inside the single command DB transaction** | MAJOR | architecture/operational
Evidence: `call_llm` (with retries/repairs) executes inside `execute_command`'s claim-holding transaction; SP-0's `external_commands` durable pattern exists, unused; no client timeout, no latency SLO.
Why: provider degradation → row locks and open transactions for minutes, blocked submitters, drained pools.
Fix: move provider calls to the external-command/outbox pattern; aggressive timeouts.

**11. Cost controls stubbed: `cost_metadata` always empty; cost breaker unwired** | MAJOR | llm-ops
Evidence: `llm.py:186-189,230` (hardcoded `{}`); usage never read (`llm_claude.py:126-130`); `runtime/cost_budget.py` has no intake caller.
Fix: capture usage tokens, wire `record_cost`, per-run ceiling via existing breaker.

**12. Prompt-injection surface unmitigated and structurally untestable with the current harness** | MAJOR | gap/safety
Evidence: raw interpolation, no system prompt/delimiting (`llm_claude.py:100-104`); LLM-derived text re-enters prompts (`commands.py:1266-1275`, `critique.py:122-136`); FakeLLM is task-keyed and content-blind (`llm.py:138-158`) so injection cannot be simulated; the deterministic backstop is finding #1's substring matcher.
Fix: delimited untrusted segments + system prompt; injection corpus in the gated real-LLM lane; treat LLM-echoed text as untrusted.

**13. E2E/FakeLLM proves plumbing and authz, not intake behavior; adapter runtime paths CI-untested** | MAJOR | gap/llm-ops
Evidence: FakeLLM replays verbatim fixtures keyed on task; repair-budget exhaustion never tested through `call_llm`+registry; `_map_stop_reason` fail-opens unknown stop_reason → `PROVIDER_OK` (`llm_claude.py:63-64`), untested; live smoke asserts only "some taxonomy token".
Fix: fail-closed stop_reason default; adapter unit tests; scripted-malformed-output E2E; meaningful gated smoke.

**14. Confirm-time §8.4 re-screen runs on narrower text and silently loses product/region** | MINOR | code-bug
Evidence: `commands.py:1884-1891` vs `:1904-1906` — `product`/`region` live on `INTENT_SUBMITTED`, not the draft body, so the re-screen always gets None.
Fix: thread product/region (and both raw-ref texts once #3 fixed) into the confirm screen.

**15. Fold drops `request_id`/`run_id` (payload-only read vs R2 typed columns)** | MINOR | code-bug
Evidence: `state.py:121-122` vs the correct `_request_id` helper (`commands.py:1620-1626`); correlation-less parks (`commands.py:1862-1867`).
Fix: fold from typed envelope attributes with payload fallback.

**16. Withdrawn run's contract can still be CONFIRMED; withdraw cancels no tasks** | MINOR | code-bug
Evidence: `confirm_contract` never checks `run_is_terminal` (contrast `reject_intent` `commands.py:264`); `run_lifecycle.py:16-33` cancels nothing. Mitigated: `read_model.py:75-100` fail-closes servability.
Fix: terminal-run deny on confirm/edit/answer; cancel tasks on withdraw.

**17. Idempotent reuse permanently caches transient failures; stale confirm denial leaves orphans** | MINOR | code-bug/operational
Evidence: `llm.py:474-484` (reuses `failed_into_clarification` forever for identical identity); `commands.py:2005-2093` (task consumed + doc frozen before CAS denial; no rollback on `accepted=False`).
Fix: exclude transient-exhaustion from reuse; reorder or clean up on denial.

**18. Contract model can't express a large share of real banking features; filter predicates are unparsed strings** | MINOR | architecture
Evidence: closed `METHOD_KINDS` of 4 (`contract.py:31`); single entity, no joins/time-since-event/sessionization/sequences/multi-window velocity; ratio numerator/denominator untyped `{}`; `filter.predicate` free SQL-ish string flowing to SP-3.
Fix: type ratio parts now; structured predicate AST before SP-3 consumes the string (also see SP-3/4/5 finding 7).

**19. Gate #1 evidence capture thin for an "audited intent lock"** | MINOR | gap
Evidence: confirm task carries only `required_inputs=(draft_doc_id,)` (`commands.py:1775-1786`); confirmation record (`commands.py:2041-2050`) omits scores, ledger, critique findings, candidate ordering — the addendum's R30 exists because of this.
Fix: snapshot the rendered evidence set (content-hashed) into the confirmation record now.

**SP-2 verdict:** deterministic skeleton (fold, OCC, no-regression locks, owner guards, egress guard) is genuinely strong and adversarially tested — but the branch is a plumbing proof with a hollow center. Honest status: "deterministic contract lifecycle: done; auditable-LLM surface: scaffolding only." Findings 1–5 are merge blockers for anything labeled banking-grade.

---

## 7. SP-3/4/5 vertical slice — findings (design-only; one roadmap row each)

**1. The PIT/SCD "engine" is one predicate string, not an executable mechanism** | BLOCKER | underspecified
Evidence: design §4.3 (L264-269) — the entire PIT spec is `"rule": "transaction_date >= as_of_date - interval '90 days' AND posted_at < as_of_date"`; §7.1 names packs with no check semantics; zero corpus hits for "as-of join", "bitemporal", "late-arriving", "reversal", "backdated", "value date" (grep-verified); §11.4 punts time-travel to "the data platform" and no SP verifies it.
Why: banking cores emit backdated transactions, reversals, re-posts; a single-column predicate cannot express "what was known at T" for restated data — historical values, IV scores, backfills, and regulator recompute are silently wrong. The roadmap says the slice exists to retire exactly this risk.
Fix: SP-3 spec must define as-of join semantics (events + SCD2), a bitemporal stance (reversals/backdates), spine generation, and a fail-closed time-travel precondition check.

**2. SP-5's IV/WoE scoring is unbuildable: no label pipeline exists; targets are prose** | BLOCKER | gap
Evidence: §14.3 (L716-729) requires PIT-correct `(feature_value, label)` samples; targets are free text everywhere (design §4.2, catalog L52/68/83; SP-2 `"target": {"type": ["object","null"]}`). No SP owns label compilation, PIT label joins, or label maturity/embargo (churn-90d undefined for recent as-of dates).
Why: no labels → no Tier-1 score → no USEFULNESS-CHECKED → per §10, no production feature. The milestone collapses to "predictive value unverified". Hand-rolled label joins are also the most common leakage source in banks.
Fix: Label/Target service in SP-5 (or its own SP): structured versioned target schema, compiled label query under the same PIT packs, maturity/embargo rules.

**3. "Batch materialization to the store" has no engine, scheduler, or store semantics** | BLOCKER | missing-feature
Evidence: roadmap L69 is the entire design. §3.1 defines the store as "keyed by entity + as-of time"; SP-0's runtime is a request-workflow engine, not a recurring-job orchestrator (§9.3). No incremental-vs-full policy, no restatement policy for late/reversed events, no retention, no upstream-freshness dependency handling.
Why: ~a third of SP-5's surface with zero design; a Tuesday reversal changes Monday's 90d count — are materialized values immutable, restated, or versioned? Without an answer the store and audit trail contradict.
Fix: store+materialization mini-spec: schema (entity × as_of × feature_version), spine/cadence, restatement semantics (append-only corrections), named orchestration capability, upstream gating.

**4. Where does evaluation run? Masked-sandbox vs honest-IV is an unresolved contradiction** | BLOCKER | architecture
Evidence: §12 (L651): sandbox = masked/tokenized/sampled/synthetic only. §14.3 (L729): scoring sample must be PIT-correct historical truth. No section names evaluation's environment. Masking destroys the flagship features' semantics (stddev of gaps, CoV of amounts); tokenized keys break feature↔label joins unless consistent — nowhere required.
Why: either Gate #2 approves on distorted evidence, or unapproved artifacts run on real production history violating §12. An MRM reviewer finds this in the first hour.
Fix: a third, governed **evaluation environment**: real (or format/statistics-preserving) historical data, read-only, PIT-correct, consistent tokenization, access-logged, distinct from sandbox and production; per-domain IV-safe masking rules.

**5. The feature store has no consumers: training-set API and serving/lookup interface absent from all 13 SPs** | BLOCKER | missing-feature
Evidence: §3.1 "served by lookup" — no component performs the lookup; §13.1 monitors "feature usage by models" — through what interface? Zero corpus hits for training-set/SDK. No PIT training-join API, no read path, in any layer or SP.
Why: a registry with a store nobody can read is a compliance artifact; model builders hand-rolling as-of joins reintroduce the leakage the platform exists to prevent.
Fix: consumption SP (or SP-5 extension): PIT training-set API (spine + as-of join reusing the same PIT engine), batch-lookup path with use-case-scoped authz, `serving_mode` registry attribute from day one.

**6. The vertical slice violates the design's own hard gates: USEFULNESS-CHECKED and monitoring-spec requirements depend on SP-7/SP-10** | MAJOR | architecture
Evidence: §14.5 requires the §14.4 guard (SP-7); §10 "no monitoring spec → no production feature" (executor = SP-10); Gate #2 review shows semantic-leakage/fairness (SP-7) — all after SP-5's "approved → registered → materialized" milestone (roadmap L71).
Fix: cap SP-5 features at `APPROVED_EXPERIMENTAL` with a minimal OOT re-check + monitoring-spec stub; production promotion gated on SP-7/SP-10.

**7. Free-text SQL predicates inside the Confirmed Contract undermine Path-1's trust model and pre-empt policy-aware grounding** | MAJOR | architecture
Evidence: SP-2 §4.2 (L443-446,545-546): Gate-#1-confirmed contract carries `"predicate": "card_authorizations.auth_result = 'D'"` — a raw string binding a physical column before SP-3's mapper runs; design §4.3/4.4 expects structured `{column, operator, value}`.
Why: a trusted compiler embedding an unparsed free-text predicate is an injection surface and a policy bypass (human anchors on a blocked column before the mapper reviews).
Fix: structured predicate AST in SP-2 now, or SP-3 treats the string as concept evidence only, re-derives against policy-exposed columns, fails closed on mismatch.

**8. DSL expressiveness ceiling contradicts SP-2's confirmed vocabulary; Router has no in-slice fallback** | MAJOR | gap
Evidence: SP-2 closes `calculation_method.kind` at 4; the Feature Plan (§4.4) supports one source table, one entity key, one calculation — no joins/multi-entity/cross-table ratios/sessionization; Path 2 arrives SP-6; no `NO_PATH_AVAILABLE` state in §9.1. SP-2's own worked example (Jensen-Shannon divergence of category spends) is confirmable then unimplementable. DSL/compiler versioning-on-change (recompute? new version?) unaddressed despite §11.4.
Fix: publish the op-catalog ↔ method-kind coverage matrix, surface implementability at Gate #1, add `NO_PATH_AVAILABLE → parked for SP-6`, define DSL semver + recompute policy.

**9. No compile target, dialect, or determinism spec for compiler/sandbox/materializer** | MAJOR | underspecified
Evidence: §16 "assumes a single primary compile target" — never named; zero hits for dialect/Spark-as-target/timezone; reference stack is Postgres. Window semantics ("90d" calendar vs rolling, EOD cutoff, business date), null semantics, float determinism unspecified.
Why: SP-4 can't write a compiler without a target; PIT joins over years of transactions won't run on the reference Postgres; timezone/EOD ambiguity is classic quiet leakage.
Fix: pick the primary target (compiler IR + one bound dialect) in SP-4's spec; one-page determinism contract with per-table timezone/business-date overlay facts.

**10. The Mapped Feature Contract — the SP-3→SP-4 handoff — has no authoritative schema; the design's example is stale** | MAJOR | underspecified
Evidence: design §4.3 is a worked example on the *old* Confirmed shape (`prediction_time`, string `calculation_method`) which SP-2's ratified schema replaced; SP-2's `ratio` variant has untyped `{}` parts; `observation_intent.kind: "as_of_event"` is a legal confirmed value with no PIT-mapping story anywhere.
Fix: publish authoritative versioned content-schemas for `MAPPED_CONTRACT` and `FEATURE_PLAN` before SP-3 speccing; specify or remove `as_of_event`.

**11. The Domain/Use-Case Catalog is orphaned — three layers consume a service no SP builds** | MAJOR | gap
Evidence: §15.2 wires it into L3/L6/L7; SP-2 defers "the full catalog" to "Layer-0 catalog work" which has no roadmap SP; the banking catalog is a markdown+seed-JSON, read-only, intake-only; §15.6 onboarding workflow unowned.
Fix: Domain Catalog service (store, versioning, owner/Compliance confirmation via SP-0 gates, read API) as an explicit SP or SP-1 extension.

**12. Sandbox and masking infrastructure is a clause, not a design** | MAJOR | underspecified
Evidence: §12 L651-653 + roadmap clause. Unspecified: masked-dataset production/refresh/ownership, cross-table tokenization consistency, sampling design (rare-event preservation — AML positives vanish under naive sampling), substrate, tenancy isolation, quota enforcement.
Why: in a bank the masked-data environment is often the longest-lead-time item in the slice; sampling bias propagates into DQ and IV silently.
Fix: sandbox mini-spec covering all of the above.

**13. Evaluation metric model is binary-classification-only, but the catalog promises `ks`, `precision_at_k`, and future regression domains** | MAJOR | gap
Evidence: §14.3 computes only IV/WoE (+MI/AUC); catalog declares `lift`/`ks`/`precision_at_k` with no computation; LGD/EAD/prepayment (named onboarding example) have no scoring path; Tier-0 covers no-label, not non-binary-label.
Why: §10 hard-gates production on USEFULNESS-CHECKED = IV + guard → every non-binary use case is architecturally barred from production, contradicting §15.5's "any banking feature".
Fix: metric registry with model-free computation per `primary_metric`; SP-5 scorer dispatches on it.

**14. Human Gate #2's evidence package is a UI aspiration, not a signed artifact** | MAJOR | underspecified
Evidence: §5.7 lists what the screen "must surface"; no evidence-bundle schema, no content-hash binding the approval to exact artifact versions, no approver options beyond approve, no supersession-invalidates-approval rule.
Fix: `EVIDENCE_BUNDLE` staged document (hashed refs to every input artifact + stamp + ledger + routing) bound to `FINAL_APPROVAL` via `required_inputs`; supersession invalidates open approvals.

**15. The SCD mapper is delivered into a slice with no consumer** | MINOR | gap
Evidence: SP-3 delivers "PIT + SCD mapper"; SP-4's ops are all event aggregations; the Feature Plan has no dimensional-join shape; §7.2's example shows `"scd_correctness": "NOT_APPLICABLE"`.
Fix: one SCD-consuming DSL op (`attribute_as_of`) in SP-4, or explicitly rescope SCD later.

**16. Duplication/cost/backfill-feasibility packs arrive (SP-7) after registration+materialization ship (SP-5)** | MINOR | gap
Fix: minimal duplication (plan-hash match) + crude cost ceiling in SP-4/5.

**17. Entity resolution is asserted, not designed** | MINOR | underspecified
Evidence: §15.5 lists seven entities; the mechanism is pairwise `approved_join` facts + one `entity_table`/`entity_key_column`. No party-vs-account hierarchy, no golden-ID mastering, no household/counterparty rollup.
Why: bank identity is multi-master; "grain = customer_id" is ambiguous the moment core banking and cards disagree.
Fix: overlay-confirmed "entity anchor" fact per entity (canonical table+key+mastering source); fail closed without one.

**SP-3/4/5 verdict:** the master design's depth is inversely proportional to layer difficulty — the four things that determine correctness and viability (PIT/SCD mechanism, label pipeline, materialization engine, consumption path) get a predicate string, a prose target, a clause, and nothing. Each SP needs a real spec making SP-2-grade schema-and-mechanism commitments; the milestone needs capping at `APPROVED_EXPERIMENTAL`.

---

## 8. SP-6..SP-12 coverage & hardening — findings (design-only, MRM/regulatory lens)

**1. Static PIT/policy validation of arbitrary LLM SQL is asserted, not designed** | BLOCKER | architecture/underspecified
Evidence: design §5.3 (L328-333), L134, §5.7 L362 ("the deterministic point-in-time check runs regardless of path"), §7.3 L455; roadmap L77.
Why: temporal leakage is only deterministically decidable when the compiler controls structure; on arbitrary SQL (CTEs, window functions, self-joins, correlated subqueries, `SELECT *`, UDFs) it requires full column-lineage + predicate semantic analysis — known-hard. The platform's most important safety claim for Paths 2/3 has no mechanism; an unsubstantiated "deterministic" claim reads worse to MRM than an honest heuristic.
Fix: "SafeSQL" subset (one dialect, AST-parseable, no UDFs/`SELECT *`, mandatory platform-injected PIT scaffold CTE verified structurally) + dynamic sandbox leakage canaries (shifted as-of recompute; future-dated poison rows must not move values). Real SP-6 spec before build.

**2. Fairness gate has no metrics, no protected-attribute data strategy, and an internal contradiction** | BLOCKER | regulatory/gap
Evidence: design L144 (the entire spec), L275, catalog L55/72/74, RA L97.
Why: (a) under Reg B banks generally can't collect protected attributes for non-mortgage credit — no source named (BISG proxies? HMDA subset?); (b) the policy engine blocks `protected_attribute` for all use-cases, yet proxy-correlation testing requires exactly that data — the gate needs an enclave the design forbids; (c) no metrics (AIR/SMD/proxy R²), no threshold owner; feature-level testing alone can't satisfy ECOA/Reg B which assesses decision level. Wave-3 credit use-cases cannot legally clear the gate as written.
Fix: segregated fairness-testing enclave (protected/proxy data visible only to the fairness service), named metrics + BISG-or-equivalent method, thresholds owned by fair-lending/compliance, gate repositioned as evidence feeding the bank's fair-lending program.

**3. Features reach production with no operating monitoring for all of Phase C** | MAJOR | gap/regulatory
Evidence: §10 L595 gate checks a spec exists; execution is SP-10, Phase D; SP-5 milestone registers/materializes production features.
Fix: minimal executor (freshness, null-rate, PSI) in SP-5, or `PRODUCTION` unreachable until SP-10 (cap at `APPROVED_EXPERIMENTAL`).

**4. Exposure/row-column enforcement deferred to SP-11 while sensitive-data features build from Phase B** | MAJOR | architecture/gap
Evidence: roadmap L88 vs L67; design §12 L633-649; catalog Wave 2 (AML: `sar_filed`, `kyc_documents`, `watchlists`).
Why: SP-3's "policy-aware mapper" (Phase B) vs SP-11's enforcement (Phase D) — what does SP-3 enforce without it? SAR-derived labels carry criminal tipping-off exposure; nothing anywhere specifies serving-time access control on the store (a materialized salary feature is readable by any consumer).
Fix: split SP-11 — exposure + store-read purpose checks to Phase B/C; Path-3 authoring stays Phase D; name the enforcement point.

**5. Search-overfitting guard statistically underspecified; attempt/conceptual memory creates cross-round adaptivity it doesn't cover** | MAJOR | underspecified/architecture
Evidence: §14.4 L731-737 ("bar scales" — no method), §14.2 L708, §14.9.
Why: memory + islands + distillation = a long-running adaptive search over the same fixed history; the holdout is progressively exhausted (reusable-holdout failure). No named correction (Bonferroni/FDR/thresholdout) → MRM can't assess it; a fluke-harvesting engine feeding credit models is the SR 11-7 nightmare.
Fix: selection-aware correction, cumulative comparison tracking per use-case, rotating/embargoed OOT windows, governed per-hypothesis candidate budgets.

**6. Model→feature dependency tracking has no mechanism and no SP** | MAJOR | missing-feature
Evidence: §13.1 L662-664, §13.2, roadmap L86 ("hooks", no schema).
Why: deprecation, change-impact, revalidation scoping, and "which models consume this feature?" all depend on consumer registration nobody builds; store reads are anonymous.
Fix: consumer registration (model-inventory ID ↔ feature version, purpose, environment) as a first-class SP-9 deliverable; required for store reads; SP-10 keys off it.

**7. Evaluation-data contradiction: IV/fairness scoring vs masked/synthetic sandbox data** | MAJOR | architecture/underspecified
(Same issue as SP-3/4/5 finding 4, viewed from SP-7: which artifacts are computed where is never stated.)
Fix: governed real-data evaluation zone as an explicit SP-7 prerequisite.

**8. Label/target pipelines are ungoverned dependencies** | MAJOR | gap
(Same root as SP-3/4/5 finding 2.) Label engineering is as leakage-prone as feature engineering; no SP, no validation pack, no owner. A leaky label corrupts every gate downstream while everything reports PASSED.
Fix: label contracts with their own PIT validation, versioning, owner confirmation (SP-5/SP-7).

**9. Reproducibility snapshot strategy punted, and conflicts with crypto-shred** | MAJOR | regulatory/underspecified
Evidence: §11.4 L622; roadmap L86; RA L218.
Why: retention horizon (7–10y), frozen-snapshot storage cost, and the direct GDPR-erasure vs "regulator can reproduce any feature" conflict are all unaddressed — shred a customer and PIT recompute of any feature touching them is unverifiable.
Fix: SP-9 chooses mechanism (time-travel vs snapshots), risk-tier retention, documented erasure-vs-reproducibility policy (aggregate-level evidence for shredded subjects).

**10. "Three-party independent-validation gate" referenced but designed nowhere** | MAJOR | regulatory/gap
Evidence: catalog L77 promises it for `credit_origination`; design §11.1 gestures; SP-9 delivers only four-eyes.
Why: SR 11-7/TRIM effective challenge requires validation independent of development; a second approver is not that. Cross-document inconsistency an MRM reviewer finds in minutes.
Fix: define the independent-validation role + workflow (reviewer reproduces feature + evaluation from registry artifacts) in SP-9; reconcile the catalog.

**11. Semantic/proxy leakage "detection" is a heuristic sentence, not a method** | MAJOR | underspecified
Evidence: §7.4 L457-466; roadmap L78 promises it as an SP-7 deliverable.
Why: no IV/AUC ceilings, no entity-overlap contamination check, no label-derivation lineage analysis (e.g. `days_since_last_transaction` vs a churn label *defined as* 90 transaction-free days — near-tautological and uncaught). "Mandatory human review" without procedure regresses to rubber-stamping.
Fix: concrete detectors (per-use-case suspicion thresholds, lineage check against the label contract, entity-overlap audit) + a written reviewer procedure.

**12. Drift response ownership and mechanics unspecified** | MAJOR | underspecified
Evidence: §13.1-13.2 metric lists only; §16 L886 leaves deprecation-racing-adoption open.
Fix: per-risk-tier monitoring template — baseline definition, thresholds, named responder, response SLA, automatic containment (pause materialization, flag consuming models).

**13. Harvest loop promotes LLM SQL into the trusted compiler without an SDLC/equivalence gate** | MAJOR | architecture
Evidence: §5.8 L364-366; §5.2 ("trusted, audited" compiler); roadmap L87.
Why: promotion is a trust-tier escalation with no equivalence testing, code review, or compiler release process — "trusted" erodes with each harvest.
Fix: promotion = a software release: differential testing on historical data, independent review, versioned compiler release gate.

**14. Critique Service: internal contradiction and sequencing wrinkle; a library, not an SP-sized system** | MINOR | architecture
Evidence: §8.1 L491 example emits `"blocks_progress": true` vs §8.2 L508 "never a gate"; Layer 2 uses `CONTRACT_REVIEW` in Phase B (SP-2) but SP-8 "formalizes" it in Phase C.
Fix: rename the field (`recommends_block`), document its consumer; deliver SP-8 as incremental extraction from SP-2's critique code.

**15. Threshold governance unassigned across every quantitative gate** | MINOR | regulatory/gap
Evidence: L722 ("IV > ~0.1 rule of thumb"), §14.4, §13, §7.1 cost ceilings.
Fix: all gate thresholds as versioned registry artifacts with named owners (MRM committee; fair-lending for fairness), change control, per-use-case overrides in the Domain Catalog.

**16. Cost/duplication packs for arbitrary SQL and search runs are mechanism-free; rejected-feature memory never expires** | MINOR | underspecified
Evidence: §7.1/§7.5, §14.2, §12.
Why: pre-execution cost estimation of arbitrary SQL is engine-specific and unaddressed; duplication method (name? AST? value-correlation?) unstated; no per-hypothesis search budget; stale rejections can wrongly block resubmission after upstream data changes.
Fix: EXPLAIN-based cost bounds; tiered name→AST→correlation dedup with stated false-positive posture; governed per-hypothesis budgets; rejection re-evaluation triggers.

**17. EU AI Act absent; "🔭 Designed" status of SP-6..12 overstated** | MINOR | regulatory/gap
Evidence: §15.5 L858-861 lists frameworks — no EU AI Act anywhere (grep-verified); RA §7 marks seven spec-less SPs "Designed" (directory-verified).
Fix: add the EU AI Act mapping (credit scoring = Annex III high-risk; Articles 10/12 map cheaply onto this design); downgrade RA status to "outlined" until per-SP specs exist.

**SP-6..12 verdict:** Phases C/D are capability inventories, not designs. The two hardest technical claims the safety story rests on — deterministic PIT over arbitrary SQL, and fairness testing without protected attributes — are one-line gates with no mechanism. Hold Wave-2/3 catalog expansion hostage to real SP-6/SP-7 specs; cap Phase B/C output at experimental; pull exposure enforcement forward.

---

## 9. Industry benchmark — missing capabilities vs world-class platforms

**1. Online feature serving (low-latency store)** | BLOCKER — explicitly excluded (§1.4, §16); the domain catalog's own highest-risk use case (`card_fraud_realtime`) is turned away at intake. → SP-13; make DSL/registry online-aware now.
**2. Training-set generation service (PIT joins at consumption)** | BLOCKER — PIT enforced at build, absent at consumption; scientists hand-write as-of joins and reintroduce leakage; SR 11-7 independent validation needs versioned training datasets. → SP-5 extension / SP-14.
**3. Consumption surfaces (Python SDK, notebook/training/batch-scoring integration, REST/gRPC)** | BLOCKER — only intake UI/API + confirmation console exist anywhere; ungoverned copy-paste of registered SQL is the inevitable workaround. → SP-14.
**4. Streaming/event-time compute (windowed aggregations, late data)** | BLOCKER — velocity features (10-min transaction counts, geo-jumps) are the backbone of fraud/AML models and can't exist at T+1. → SP-13, compiled from the same DSL.
**5. Training/serving consistency (one definition → both paths, equivalence-tested)** | MAJOR (BLOCKER once online exists) — §16 future-work only. → design decision in SP-4 now (dual-target-compilable ops; Path-2 SQL batch-only).
**6. Brownfield migration / bulk import of the existing estate** | MAJOR — nothing; Path 3 is a high-friction exception, the opposite of an on-ramp; adoption is greenfield-only, MRM inventory incomplete, dedup blind to reality. This is the most common way internal bank platforms die. → legacy import mode with honest DESIGN-CHECKED stamps; LLM-assisted SAS/SQL→contract extraction.
**7. Feature discovery, search, cross-team reuse UX** | MAJOR — duplicate *prevention* exists; discovery/browse/sharing doesn't; at 1,000+ features the registry becomes write-only. → registry search API + catalog UI.
**8. Environments (dev/UAT/prod), features-as-code, git/CI** | MAJOR — one implicit environment; bank change management requires demonstrable UAT-before-prod; power users need declarative repos. → environment attribute + promotion via Gate #2 machinery; contract-file format.
**9. Scale NFRs and backfill compute strategy** | MAJOR — feasibility packs exist as gates but no throughput/feature-count targets, no incremental strategy; 5-year PIT backfills over 10M customers is a large distributed-compute problem no SP owns. → NFR appendix now; incremental materialization in SP-5/SP-10.
**10. Platform console beyond two confirmation surfaces** | MAJOR — Gate #2's "augmented review" (the key safety claim) has no delivery surface; SP-10 computes PSI, nothing displays it. → thin console in Phase B (request tracker + Gate #2 screen), thicken Phase D.
**11. On-demand/request-time features** | MAJOR — amount-vs-limit, distance-from-home at auth are among the strongest fraud/credit predictors; inexpressible even after an online store exists. → on-demand transform class, Path-1-only.
**12. Multi-tenancy, namespacing, cost attribution/chargeback** | MAJOR — no team/project construct, no quotas, no ownership-transfer (owners who leave = a real audit finding), no chargeback (finance will require it). → SP-9/SP-11 extension.
**13. Derived/composite features (feature→feature inputs)** | MINOR — contracts take only source tables; change-impact tracks table edges only. → feature-reference DSL input + feature-level dependency edges.
**14. Observability productization (SLOs, paging, consumer notification)** | MINOR — SP-10's metric list is good; the operational wrapper (who is paged, incident severity, consumer fan-out) is absent. → SLOs in the monitoring spec + incident-management hooks.
**15. Concrete data-platform binding (open table formats, time travel, pushdown)** | MINOR — reproducibility depends on time-travelable sources; discover non-time-travelable sources in Phase B, not at the first exam. → reference stack for the walking skeleton + a time-travel capability check in SP-3's Catalog Quality Gate.
**16. Self-serve onboarding, docs, education** | MINOR — a platform adding two human gates must over-invest in explaining itself; fail-closed messages must name the missing fact and responsible human. → template gallery from `feature_templates`; error-UX requirement.

**Benchmark verdict:** the creation-side governance is durable, hard-to-copy differentiation — genuinely ahead of Tecton/Feast/Hopsworks and most internal bank builds. The consumption-and-serving side is well-trodden and buildable. Don't dilute Phase B; make the three cheap design commitments now (dual-compilable DSL, online-aware registry schema, training-set service in SP-5's scope) and schedule Phase E so "world-class" is a destination on the roadmap rather than an open question in §16.
