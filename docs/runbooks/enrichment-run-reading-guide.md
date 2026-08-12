# Reading an enrichment run

**What this is.** After a catalog upload with LLM enrichment on, this is the exact query or grep
that answers each question the run raises. It is the deliverable a human uses at 3am; the
instrumentation behind it (Task 9c, 2026-08-06) is only its substrate.

**Where everything lands.** `ingestion_run_stage` (columns: `ingestion_run_id`, `stage`, `attempt`,
`state`, `reason_code`, `started_at`, `completed_at`, `detail jsonb`), `llm_call` +
`ingestion_run_llm_call`, or the application log. No table exists only for diagnostics.

**The rule that makes the whole account readable: an absent key means the thing never happened; it
never means zero.** A `provider_calls: 0` on a run that charged a call is worse than no field at
all, because it answers the reader's question wrongly instead of sending them to look elsewhere.

**Never a data value.** Every field here is an integer count, an integer LENGTH, a member of a
closed code set defined in this repo, a concept/namespace name from `CONCEPT_REGISTRY`, or a ref
this code minted. `test_no_diagnostic_ever_carries_a_value` plants a marker in every model-authored
field a run produces and asserts it reaches no stage detail and no log record at DEBUG or above.

---

## Start here, always

```sql
-- 0. The run, and every stage's headline. Run this first; it tells you which query below to run.
SELECT stage, state, reason_code,
       completed_at - started_at AS duration,
       jsonb_pretty(detail)
FROM   ingestion_run_stage
WHERE  ingestion_run_id = :run
ORDER  BY id;
```

A NULL `duration` is a MARKER state (`not_run` / `not_applicable` / `skipped_no_client` /
`disabled`) — the stage never began. Any other state with a NULL duration is an instrumentation
gap; report it.

---

## Q1 — "The features are different. Did the CONCEPTS move, or was it just more enrichment?"

The one that must be answered first, because every other difference is downstream of it.

```sql
-- Every column whose concept the critic MOVED, with the namespace (join-candidacy) it moved
-- between. NOTE: `jsonb_each` returns zero rows for BOTH "present and empty" (the critic moved
-- nothing) and "absent" (this code never ran) — it cannot tell them apart. Use query 0, or the
-- `detail ? 'verdict_changes'` test, to make that distinction.
SELECT c.key                          AS logical_ref,
       c.value->>'disposition'        AS verdict,
       c.value->>'from'               AS concept_before,
       c.value->>'to'                 AS concept_after,
       c.value->>'from_namespace'     AS namespace_before,
       c.value->>'to_namespace'       AS namespace_after,
       c.value->'conflict_codes'      AS why
FROM   ingestion_run_stage s,
       jsonb_each(s.detail->'verdict_changes') c
WHERE  s.ingestion_run_id = :run AND s.stage = 'enrich_concept_critic'
ORDER  BY 2, 1;
```

Read it like this:

- `verdict = 'refuted'` and `namespace_after = '-'` → **this column lost bridge candidacy.** Any
  feature that joined through it is gone, and that is the explanation, not a new bug.
- `verdict = 'revised'` and `namespace_before <> namespace_after` → the column is now a join
  candidate against a *different* set of columns. Different features, same data.
- `concept_before` is the CANONICALIZED assignment (aliases already resolved), i.e. the value that
  would have been stored had the critic not moved it.
- If the key `verdict_changes` is **absent** rather than empty, this run predates the instrument or
  the critic never ran — check `enrich_concept_critic.state` (`not_run` / `not_applicable` /
  `skipped_no_client` are all distinct and all meaningful).

Then the aggregate, which also covers columns the critic never looked at (only the high-impact
concept groups are criticised):

```sql
-- The catalog's join-candidacy shape, before and after acceptance, plus WHICH registry
-- generation produced it. Compare `vocab_fingerprint` across two runs: if it changed, a registry
-- edit is a candidate explanation for everything else that changed.
SELECT detail->>'vocab_fingerprint'          AS registry_generation,
       detail->'namespaces'->'before_critic' AS ns_before,
       detail->'namespaces'->'after_critic'  AS ns_after
FROM   ingestion_run_stage
WHERE  ingestion_run_id = :run AND stage = 'enrich_concept';
```

If `ns_before = ns_after` the critic changed nothing structural, and a different feature set has to
be explained by enrichment coverage, not by verdicts.

> ⚠️ **Expect this to fire on the first run after the `is_a` parent expansion** (DEFERRED-WORK
> A.53). `concept_critic._registry_fingerprint()` folds `is_a`, so adding parents moves it and
> EVERY stored identifier verdict re-rolls — non-deterministically, since the verdict comes from a
> model. Nothing is wrong when that happens; but a different feature set on that run is explained
> here, not by the enrichment coverage in Q4.

---

## Q2 — "A stage says `truncated` / `partial`. WHICH bound did it, and what did the run cost?"

```sql
SELECT stage, state, reason_code,
       detail->>'stopped_by'      AS headline_bound,
       detail->'bounds'           AS every_bound_hit,
       detail->>'provider_calls'  AS calls_spent,
       detail->>'fallback_calls'  AS per_item_calls,
       detail->>'chunks_planned'  AS planned,
       detail->>'chunks_issued'   AS issued,
       detail->>'not_attempted'   AS never_dispatched
FROM   ingestion_run_stage
WHERE  ingestion_run_id = :run AND detail ? 'stopped_by';
```

- `call_ceiling` → a CONFIG problem (`OVERLAY_ENRICH_MAX_PROVIDER_CALLS`), not a provider problem.
  Columns were silently not enriched. Raise it and re-run. The code default and the deployed value
  are pinned equal by `test_deployment_llm_bounds.py`, so this should only appear where an
  environment overrides it downward.
- `wallclock_budget` / `stage_deadline` → the provider was slow. Different fix entirely. These two
  were indistinguishable before Task 9c.
- `fallback_cap` → only a few leftover items were not individually retried. Usually benign — **but
  check `bounds` for a `call_ceiling` alongside it**, which is the severe case it used to mask.
- `exception` → the ladder raised. `provider_calls` is still truthful: the run spent that much
  before dying.
- `unattributed` → items went undispatched and no bound owns it. That is a hole in the instrument;
  report it.
- `planned <> issued` → chunks were abandoned mid-loop (deadline or raise).

---

## Q3 — "The model answered but the value was thrown away. WHY?"

Counts, per stage:

```sql
SELECT stage,
       detail->'outcomes' AS by_disposition,   -- missing | blank | duplicate | invalid_value | egress_rejected
       detail->'rejects'  AS by_rule           -- over_length | list_prefix | enumeration | multiline | task_echo | off_vocabulary | unit_shape | blank
FROM   ingestion_run_stage
WHERE  ingestion_run_id = :run AND detail ? 'rejects';
```

**These are rejection EVENTS, not distinct columns** — the ladder re-accounts a chunk it retries or
splits, so a column rejected on two attempts contributes two. `detail->>'unresolved'` is the
per-column number. If `rejects` exceeds `unresolved`, that is retries, not a bug.

> **Coverage limit (DEFERRED-WORK A.56, F9):** this account covers the BATCH path only.
> `enrich_batch._fallback` calls `_single_fallback` and keeps the value while discarding the
> acceptor's status, so a per-item retry that the acceptor refuses appears in neither `rejects` nor
> `outcomes`. A stage with a high `fallback_calls` and an unexplained `unresolved` is that gap.

Which columns, and how long each value was:

```bash
grep 'enrich_reject' <logs> | grep 'reason=over_length'
# enrich_reject stage=enrich_definition ref=<hash|table> status=invalid_value reason=over_length len=41233
```

`len=` is the ONLY thing recorded about the value. Reading it:

- `reason=over_length` with a huge `len` → the model wrote past the cap and the whole value was
  discarded. Raise the cap or shorten the instruction.
- `reason=enumeration` / `list_prefix` → the model answered with a list where prose was asked for.
  A prompt problem.
- `reason=multiline` on `enrich_synonyms` or `enrich_domain` → the model ignored "ONE
  comma-separated line". These two gates deliberately did not inherit the newline relaxation.
- `reason=off_vocabulary` on `enrich_concept` → the model named a word not in the registry.
- `status=egress_rejected` → the redaction/egress gate excluded that column's metadata; the model
  never saw it. Not a model problem at all.

### Q3b — "Feature generation returned NOTHING, with no rejections at all"

Not a stage failure — an EGRESS refusal, and it is whole-payload. One over-long or unscannable
value on ONE column makes `sanitize_feature_context` return `None`, the caller blocks dispatch and
audits `EGRESS_BLOCKED`, and no column is dispatched. The log line names the element and the key:

```bash
grep 'feature-context egress REFUSED' <logs>
# feature-context egress REFUSED the whole payload at columns[57].sub_domain (rule=prose_scan) —
# the call is blocked and NO column is dispatched
```

`rule` is a closed set: `definition_scan`, `prose_scan`, `prose_list_scan`, `identity_shape`,
`fact_shape`, `token_list_shape`, `object_shape`, `mapping_shape`, `dict_list_shape`, `list_shape`,
`block_not_a_dict`, `unclassified_key`. `unclassified_key` means a NEW payload key reached the
adapter without a grade — a code change, not a data problem. See DEFERRED-WORK A.55.

---

## Q4 — "The stage says `succeeded`. Was anything actually STORED?"

The divergence that used to be invisible: `resolved` counts what the MODEL produced,
`detail->'evidence'` counts what reached `field_evidence`.

```sql
SELECT stage, state, reason_code,
       detail->>'resolved'                     AS drafted,
       detail->'evidence'->>'written'          AS rows_written,
       detail->'evidence'->>'reused'           AS rows_reused,
       detail->'evidence'->>'skipped'          AS not_written,
       detail->'evidence'->>'skipped_no_evidence_ref'      AS no_glossary_ref,
       detail->'evidence'->>'skipped_unattachable_binding' AS unattachable,
       detail->'evidence'->>'skipped_not_a_proposal'       AS unclassified,
       detail->'evidence'->>'skipped_invalid_value'        AS empty_value,
       detail->'evidence'->>'failed'           AS write_failures,
       detail->'evidence'->>'writer'           AS writer_never_ran,
       detail->'evidence'->>'rolled_back'      AS counts_are_stale
FROM   ingestion_run_stage
WHERE  ingestion_run_id = :run AND detail ? 'evidence';
```

- `writer = 'not_run:no_glossary'` → a technical upload. Every drafted value was paid for and
  discarded **by design** (DEFERRED-WORK A.52). This is the single most expensive silent behaviour
  in the pipeline and it now appears on every affected stage.
- `writer = 'not_run:no_source_snapshot'` → a glossary run that could not key evidence. Investigate.
- `rolled_back = true` → the stage's savepoint went down after the writer ran. **The counts above
  it describe rows that no longer exist.** Do not use them.
- `drafted` high and `rows_written` zero with no `writer` key → the writer ran and skipped
  everything; the `skipped_*` breakdown says which of the three designed non-writes it was.
- `no_glossary_ref` large → columns with no glossary record. Not a failure; there is no
  schema-preserving ref to key evidence on.

---

## Q5 — "Did anything degrade the audit itself?"

The first thing to check if numbers look impossible:

```sql
SELECT stage, detail->>'audit_degraded' FROM ingestion_run_stage
WHERE ingestion_run_id = :run AND detail ? 'audit_degraded';
```

---

## Q6 — "What did the CONCEPT CRITIC cost?"

The critic is the one stage with **no call ceiling, no deadline and no `not_attempted`**
(DEFERRED-WORK A.54): `critique_concept_batch` is a plain per-item loop, and
`OVERLAY_ENRICH_MAX_PROVIDER_CALLS` does not bound it. It is also the stage that runs hardest on
the first run after a registry change (Q1). So it is the one to cost explicitly.

```sql
-- The stage's own wall clock. NULL duration on a `succeeded` critic means the run predates the
-- 2026-08-09 fix that passes `started_at`.
SELECT state, reason_code, started_at, completed_at,
       completed_at - started_at AS critic_wall_clock,
       detail->>'items'     AS criticised,
       detail->>'refuted'   AS refuted,
       detail->>'revised'   AS revised,
       detail->>'abstained' AS abstained
FROM   ingestion_run_stage
WHERE  ingestion_run_id = :run AND stage = 'enrich_concept_critic';
```

```sql
-- The physical calls behind it, by task, with their span. `llm_call.latency_ms` is NULL on every
-- enrichment path (no call site sets it), so per-call duration is NOT available — the span between
-- the first and last call, read against the stage wall clock above, is what there is.
SELECT c.task,
       count(*)                                        AS calls,
       min(c.created_at)                               AS first_call,
       max(c.created_at)                               AS last_call,
       max(c.created_at) - min(c.created_at)           AS span,
       count(*) FILTER (WHERE c.validation_result->>'result' <> 'ok') AS not_ok,
       sum(jsonb_array_length(c.repair_attempts))      AS repair_attempts
FROM   ingestion_run_llm_call r
JOIN   llm_call c ON c.llm_call_ref = r.llm_call_ref
WHERE  r.ingestion_run_id = :run
  AND  c.task IN ('overlay.enrich.concept_critique', 'overlay.enrich.concept_revision')
GROUP  BY c.task;
```

Reading it:

- `calls` ≈ the number of identifier-group columns → every verdict was re-rolled (a registry
  change, or a first run). `calls` near zero on a catalog with identifiers → the replay store
  served them, which is the cheap and expected steady state.
- `critic_wall_clock` approaching the ingest's total → the critic dominated the run. There is
  nothing to raise or lower; the bound does not exist. This is the measurement A.54 asks for
  before deciding whether to build one.
- `span` much shorter than `critic_wall_clock` → the time went somewhere other than the provider
  (replay-store reads, evidence writes, decision-trail appends).
- `overlay.enrich.concept_revision` calls are the SECOND question the critic asks (a refutation
  it tried to repair), so `revision calls > 0` with `revised = 0` means the repairs were rejected.

Same shape works for any stage — swap the `task` filter. The per-stage physical account in Q2 is
the cheaper answer where a stage has one.

---

## What each record contains on the failure path

| The stage… | `state` | what the account contains |
|---|---|---|
| ran clean | `succeeded` | counts only; `bounds`, `stopped_by`, `rejects`, `outcomes`, `not_attempted` all **absent** (absent = never happened, never "zero") |
| hit a bound | `partial` / `truncated` | `stopped_by` + `bounds` (all hits, counted) + `not_attempted` + full physical cost |
| **raised** | `failed` / `exception` | `provider_calls` = what was actually charged before dying, `stopped_by = exception`, `chunks_planned` vs `chunks_issued = 0`, `not_attempted` = the chunks that never got to dispatch |
| was skipped | `skipped_no_client` / `not_applicable` | no detail — correct; the state IS the finding |
| never ran | `not_run` | no detail; `enrich_concept_critic` gets `reason_code = enrich_concept_failed` |
| wrote evidence then rolled back | `failed` | `evidence.rolled_back = true` **beside the counts it invalidates** |
| had its writer gated out | any | `evidence.writer = not_run:no_glossary` \| `not_run:no_source_snapshot` — never silence |
| criticised and moved nothing | `succeeded` | `verdict_changes = {}` — present and empty, distinct from absent |
| criticised then rolled back | `succeeded` | `verdict_changes` + `rolled_back = true`, so the trail is never read as live |
