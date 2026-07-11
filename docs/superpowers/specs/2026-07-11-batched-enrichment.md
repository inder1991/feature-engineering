# Batched Enrichment — Design Spec (v2)

Date: 2026-07-11. Status: DESIGNED, revised after architectural review (v2 incorporates 20 required
changes + the important issues from that review). Purpose: cut upload-time LLM cost by batching the
advisory concept / definition / domain enrichment, without regressing enrichment quality.

**Corrected central claim.** v1 said "quality-preserving by construction." That overclaims. Stable
refs and the existing validators prevent *misattribution* and *bad cache writes*; they do **not** by
themselves prevent *semantic degradation, retry amplification, or stale-cache behaviour*. The
accurate claim, and the bar this spec is written to:

> The design preserves the deterministic-ingestion invariants and isolates batch failures per item.
> Semantic quality is protected through a gold-set evaluation gate, bounded retry/fallback,
> production telemetry, and an explicit single-call rollback path — not by the batch shape alone.

## What v2 adds over v1

v1 treated batching as a request/response-shape change. Batching in fact changes failure isolation,
retry amplification, idempotency, cache-write atomicity, audit interpretation, prompt/token limits,
latency distribution, evaluation methodology, and determinism expectations. v2 gives each of those a
contract. The optimization target, the architectural boundary, task-separated batching, cache-first,
ref-matching, per-item salvage, and the advisory/fail-soft invariant are unchanged from v1 (they
were the parts the review affirmed).

## The optimization target (unchanged)

`enrich.py` makes one governed LLM call per item, and the concept call resends the ~281-term
controlled vocabulary every time (verified: `_CONCEPT_VOCABULARY` is in each per-column call). A
144-column / 8-table first upload with blank definitions is ~296 round-trips; the resent vocabulary
is the dominant, avoidable token cost. Batch the uncached remainder so the vocabulary is sent once
per batch, not once per column.

## Architectural boundary (unchanged, non-negotiable)

Batch **only the advisory enrichment transport.** Do not touch: deterministic validation, the
large-change brake, fact assertion, drift watermarks, quarantine, join/graph construction, or the
`content_hash` identity. The graph's structure and every load-bearing fact are computed without the
LLM and are unaffected. Enrichment fills only the advisory `concept/domain/definition` fields on
`graph_node`; a wrong value worsens search, never a fact.

---

# Contracts

## C1. Failure model: independent per-task fail-soft (ingest.py DOES change)

Today the call site wraps all three enrichments in one `try`, so a domain-batch failure discards
already-computed concepts and definitions for the current `build_graph` (even if their cache writes
committed). Batching raises the odds of a per-task structured-output/size failure, so this coupling
must go. Each task becomes an independent fail-soft block:

```python
concepts = definitions = domains = None
if client is not None:
    try:    concepts    = enrich_concepts(conn, vr.good, client, actor)
    except Exception:  logger.warning("advisory concept enrichment failed", exc_info=True)
    try:    definitions = draft_definitions(conn, vr.good, client, actor)
    except Exception:  logger.warning("advisory definition enrichment failed", exc_info=True)
    try:    domains     = classify_domains(conn, vr.good, client, actor)
    except Exception:  logger.warning("advisory domain enrichment failed", exc_info=True)
build_graph(conn, catalog_source, vr.good, concepts, definitions, domains)
```

Three independent failure domains. Within a task, the failure unit is one item (C4 salvage), not the
whole task.

## C2. Response validation: ref-set membership, not "any non-empty ref"

The v1 seam accepted any non-empty ref. That silently stores invented refs, overwrites duplicates,
and misses conflicting values. The batch seam MUST be given the expected ref set and classify every
returned entry:

```python
expected = frozenset(it["ref"] for it in items); seen: set[str] = set()
for entry in results:
    ref, val = entry.get("ref"), str(entry.get(out_key, "")).strip()
    if ref not in expected:      outcome(ref, "extra");     continue
    if ref in seen:              outcome(ref, "duplicate");  continue
    seen.add(ref)
    if not val:                  outcome(ref, "blank");      continue
    outcome(ref, "valid", val)
missing = expected - seen        # -> outcome(ref, "missing") each
```

Per-item outcome codes (persisted, C12): `valid | missing | extra | duplicate | blank |
invalid_value | egress_rejected | fallback_valid | fallback_failed`. Nothing is silently collapsed.

## C3. Cache-write policy: distinguish intentional UNCLASSIFIED from invalid output

Today `concept = raw if is_known_concept(raw) else UNCLASSIFIED` caches invalid/hallucinated output
as `UNCLASSIFIED` — a permanent false negative until the hash changes. v2 splits the two:

- The model returns the literal allowed value `unclassified` -> cache `UNCLASSIFIED` (a real
  classification).
- The model returns an unknown/hallucinated concept -> outcome `invalid_value`, **do not cache**,
  eligible for single-fallback (C4). Retried next ingest.

This is a deliberate behaviour change from production; it is documented as such here, and the
"validators unchanged" language of v1 is retracted — the validators run, but the *caching decision*
on invalid output changes.

## C4. Degradation ladder: bounded retry, adaptive split, single fallback

v1's "fall back to per-item on materially fewer valid items" was unbounded (a failed 40-item batch
-> 40 single calls, dearer than today). v2 defines a finite ladder with budgets:

```text
batch(K)
  ├─ valid ratio >= keep_threshold      -> salvage valid, stop
  ├─ transient provider error           -> retry batch once (max_batch_attempts = 2)
  ├─ structural/truncation/schema refusal-> adaptive split into batch(K/2) ... down to a floor
  └─ still-unresolved refs              -> single fallback, capped at max_single_fallback_items
                                           remaining refs left UNCACHED (retried next ingest)
```

Budgets per task (config): `max_batch_attempts`, `max_single_fallback_items`,
`max_total_provider_calls_per_task`, and an **upload-time enrichment wall-clock budget** (C-latency):
on exhaustion, stop enrichment, leave the remainder uncached, continue the upload. Adaptive split
(K -> K/2 -> …) recovers quality/cost better than jumping straight to N singles on a truncation.

## C5. Chunking: item-count AND token-budget aware

Batch size is bounded by quality (C-sizing) *and* by provider limits. The chunker takes both:

```python
chunk(items, max_items=K_task, max_input_tokens=..., max_output_tokens=...)
```

so one very wide table or a long-metadata item never makes an otherwise-valid item-count batch
exceed the model's input/output/array limits. Definitions are output-heavy: their chunker weights
estimated output tokens. Persist per batch: estimated input tokens, actual input tokens, output
tokens, item count (for tuning, C-telemetry). Production chunk order is **deterministic — items
sorted by content-hash** — so the same logical batch is reproducible (C8); evaluation deliberately
shuffles (C7).

## C6. Cache correctness: versioning, transactions, concurrency, task-specific keys

Verified: `content_hash` = `[source, table, column, type, definition]` (so cross-table `status`
collisions do NOT occur), and the domain key = `[source, table, sorted(columns)]`. But the cache
tables key on `content_hash` **alone** — no version column — so a concept cached under vocabulary v3
is served forever after the vocabulary moves to v4. v2 requires:

- **Version dimensions in the cache key** (add columns, or fold into the key): `task_version`,
  `prompt_version`, `schema_version`, and a `vocabulary_fingerprint` for concept. A vocabulary bump
  invalidates concept cache entries cleanly.
- **Task-specific keys.** Concept key: `source, table, column, type + vocabulary_fingerprint +
  prompt_version`. Definition key: `source, table, column, type + assigned_concept +
  definition_prompt_version` (a definition can depend on the concept assigned, so include it).
  Domain key: `source, table, columns_fingerprint + domain_taxonomy_version`.
- **Write semantics.** Each cache write is idempotent (`INSERT ... ON CONFLICT DO NOTHING`); a
  duplicate never fails the batch; valid items of a batch are written in one bounded transaction; a
  cache failure never invalidates the uploaded facts; concurrent enrichment of the same item
  converges (idempotent upsert). If cache and graph build share the ingest transaction, an upload
  rollback discards cache writes too — document this and prefer committing cache writes on their own
  so a late brake/rollback does not waste the provider spend (decision to confirm at build).

## C7. Idempotency and audit identity

A batch's logical identity must be stable regardless of input iteration order. Canonicalize the
idempotency key over: `task, prompt_id, schema_id, model_id, generation_settings, ordered item refs,
per-item metadata hashes, vocabulary_fingerprint`. Do not hash the raw 281-term vocabulary per call
— reference it by **version/fingerprint** (C24). Record two distinct ids so retries are explainable:
`logical_batch_id` (the stable batch) and `provider_attempt_id` (each provider round-trip).

## C8. Audit: item-level outcomes, not just "covered refs"

One `llm_call` per batch is insufficient. Persist a batch record plus per-item outcomes
(`llm_call` + `llm_call_item`, or a structured summary on the batch row):

```json
{"requested_refs": [...], "valid_refs": [...], "missing_refs": [...],
 "invalid_refs": [...], "extra_refs": [...], "duplicate_refs": [...],
 "input_tokens": N, "output_tokens": M, "provider_attempt_ids": [...]}
```

Refs + outcome codes + token counts are appropriate to persist; sensitive values are not persisted
beyond what today's `llm_call` already retains. This is the substrate for production quality
telemetry (C-telemetry).

## C9. Egress: per-item AND batch-level

The guard is not automatically equivalent on a batch (aggregate size; many names in one request;
vocabulary + metadata together). v2: run **per-item** egress validation first, then a **batch-level**
payload check. A single unsafe item is **excluded** and the remainder batched (advisory path favours
progress over blocking 39 valid items), and the excluded ref is audited (`egress_rejected`) before
any provider egress. If governance requires whole-batch rejection on any unsafe item, that is a
config, defaulting to exclude-and-proceed.

## C10. Config and the real kill switch (not "K=1")

`K=1` is NOT the current code path — it still uses the batch prompt id, array schema, instruction
wording, and response wrapper, so it is not "exact current behaviour." The true kill switch invokes
the existing `audited_enrich_call` single path:

```text
OVERLAY_ENRICH_<TASK>_MODE = single | batch     # single -> today's exact prompt/schema/code, proven by test
OVERLAY_ENRICH_BATCH_<TASK>_MAX_ITEMS = 40 / 12 / 20   # sizing (C-sizing), also token-bounded (C5)
OVERLAY_ENRICH_<TASK>_SINGLE_THRESHOLD = ...    # task-specific (C-thresholds)
OVERLAY_ENRICH_MAX_BATCH_ATTEMPTS / _MAX_SINGLE_FALLBACK / _WALLCLOCK_BUDGET_MS
```

Default `single` everywhere at first; batch is enabled per task, per rollout stage (C-rollout).

---

# Batch sizing, grouping, and prompting

## C-sizing. Task-specific sizes, bounded by quality and tokens

| Task | Output | Batch tolerance | Start size |
|---|---|---|---|
| Concept | closed ~281-term vocabulary, per-item validated | HIGH | 40 |
| Domain | short label per table, low volume | MEDIUM | 20 |
| Definition | free text, highest quality + output-token risk | LOW | 12 |

Sizes are ceilings, further capped by the token budget (C5). They are not shipped as defaults until
the eval gate (C-eval) sets them to the largest size holding quality within tolerance. Keep the three
tasks as separate calls (do not combine concept+definition — it would force one task's size onto the
other).

## C-thresholds. Single-fallback threshold is task-specific, not a flat "<= 3"

For concept, batching 2-3 items still sends the vocabulary once vs three times — worth it. For
domain, single calls are often fine. The threshold is per task and is what the cost/eval harness
tunes, not a global constant.

## C-grouping + anti-contamination

- **Concept:** global, hash-sorted chunks; vocabulary shared once.
- **Definition:** group by table, then bounded chunks — table context helps quality and is sent
  once — but this raises cross-column contamination risk, so the prompt is explicitly isolating:
  "treat each item independently; use only that item's table/column/type/approved metadata; do not
  infer relationships between items; do not reuse another item's facts; return exactly one result
  per input ref." Present items as isolated objects, not a comparative narrative. Eval includes
  adversarial batches of similarly-named columns from different tables (C-eval).
- **Domain:** tables only, global chunk.

---

# Evaluation methodology (C-eval — significantly strengthened)

v1 used single-item output as ground truth; that measures *behavioural equivalence*, not
*correctness*, and an LLM judge comparing to the same model reinforces its bias. v2 uses a
three-layer hierarchy, gold-first:

1. **Gold correctness set (primary).** Human-reviewed expected values on representative and hard
   columns/tables: expected concept + acceptable alternatives, expected domain, definition rubric /
   reference facts.
2. **Single-item parity (secondary).** Regression vs current production behaviour.
3. **Batch stability (tertiary).** Repeated runs measuring variance, missing-item rate, and quality
   **by batch position** (models degrade on middle/late items) — evaluation shuffles order to detect
   positional bias while production stays hash-sorted.

Per-task metrics:
- **Concept:** exact accuracy vs gold; acceptable-alternative/hierarchical accuracy; `UNCLASSIFIED`
  precision/recall; invalid-output rate; missing-output rate; **per-family and rare-concept
  accuracy** (a 98% aggregate hides rare-but-critical degradation). Gates are **stratified**:
  zero regression on critical concepts, an agreed floor on common, minimum recall on rare, no
  statistically meaningful rise in `UNCLASSIFIED`.
- **Definition:** deterministic constraints (one line, non-empty, no list formatting, max length,
  references the column's own semantics, invents nothing unsupported) **plus** blinded pairwise
  review (single vs batch, judge not told which) on a sample with a concrete rubric scale, sample
  size, and acceptable mean difference / no-catastrophic-error threshold. "Within epsilon" is made
  numeric before implementation.
- **Domain:** gold accuracy with alias/hierarchy normalization before comparison; penalise
  false-specific over safe-generic.

Gold fixtures deliberately include hard cases: repeated generic names (`status`, `type`, `code`),
acronyms, banking abbreviations, ambiguous numeric columns, same column name across tables, rare
concepts, blank/malformed metadata, very wide tables, max-length identifiers.

The eval harness lives in `tests/eval/` (manual / nightly, not unit CI) and its pass bars are the
release gate for enabling batch mode per task.

---

# Output schema (C18 — bounded)

Each batched schema is `{results: [{ref, <value>}]}`, `additionalProperties: false`, `ref` and value
required per item, with `minItems`/`maxItems` matching the configured cap where generation permits,
and length limits on `ref` and value (definitions: `maxLength`, newline-excluding pattern). Schema
cannot enforce ref-set membership, so application validation (C2) remains mandatory; `_bounded` stays
as defense-in-depth.

# Target architecture (from the review)

Orchestration per task:
```text
cache lookup -> deterministic (hash-sorted) todo -> task grouping -> token-aware chunking
  -> per-item egress -> governed batch call -> batch-shape validation -> ref-set validation
  -> per-item semantic validation -> transactional cache of valid items
  -> adaptive retry / split / bounded single fallback for the unresolved
  -> emit audit outcomes + metrics
```

Result contracts:
```python
@dataclass(frozen=True)
class BatchItemOutcome:
    ref: str
    status: Literal["valid","missing","extra","duplicate","blank","invalid_value",
                    "egress_rejected","fallback_valid","fallback_failed"]
    value: str | None
    reason_codes: tuple[str, ...]

@dataclass(frozen=True)
class BatchCallResult:
    logical_batch_id: str
    provider_attempt_ids: tuple[str, ...]
    outcomes: tuple[BatchItemOutcome, ...]
    request_count: int
    valid_count: int
    output_truncated: bool
```

The task function decides which valid values to cache; `enrich_llm.audited_batch_call` owns the
governed provider call + egress + audit and returns a `BatchCallResult`.

# Files touched
- `ingest.py`: independent per-task fail-soft blocks (C1). (v1 wrongly said this file is unchanged.)
- `enrich.py`: the three loops adopt cache-lookup -> chunk -> batch -> validate -> salvage ->
  transactional cache -> ladder; task-specific keys (C6) and grouping (C-grouping); the real
  single-mode kill switch calls the existing path.
- `enrich_llm.py`: `audited_batch_call` returning `BatchCallResult`; bounded array schemas; per-item
  + batch egress (C9); logical/attempt ids (C7); item-level audit (C8).
- migrations: add version columns (`vocabulary_fingerprint`, `prompt_version`, `schema_version`,
  `task_version`; `domain_taxonomy_version`; definition `assigned_concept`) to the three cache
  tables, or new keyed tables (C6); `llm_call_item` (or a JSON outcome summary column) for C8.
- config: the `C10` keys.

---

# Rollout and observability (C-rollout, C-telemetry — new)

Do not flip defaults globally. Ship behind: a feature flag, a workspace allowlist, percentage
rollout, per-task enablement, and per-model configuration (a size safe on one model may exceed
another's output limit). Sequence with **shadow mode**: run batch alongside single, compare, and only
switch cache writes to batch output once the gold gate clears.

Production metrics (the eval gate is necessary but not sufficient; these catch provider/model drift):
calls per upload, items per batch, valid-item ratio, missing-item ratio, fallback rate, single-call
rate, cache hit rate, provider latency per batch, upload latency, tokens + cost per enriched item,
invalid-concept rate, `UNCLASSIFIED` rate, definition-validator rejection rate, domain-mismatch rate.

## Phased rollout
1. **Governed batch seam** — canonical refs, expected-ref validation, dup/extra/missing detection,
   per-item outcomes, audit records, deterministic ordering, explicit single|batch mode. No task
   switches yet.
2. **Concept** — lowest risk (closed vocabulary, strongest validation, largest cost win). Shadow-compare, then enable batch writes after the gate.
3. **Domain** — low volume, validates seam reuse.
4. **Definition** — last (highest semantic risk): conservative K, stronger eval, adaptive split,
   stricter latency budget.

## Latency and concurrency notes (C21, C22)
- Batching should cut latency if today's calls are sequential, but large structured-output calls
  (esp. definitions) have longer tails, and fallback can lengthen the request — hence the wall-clock
  budget (C4). Async-on-the-worker (decoupling upload latency entirely) stays a separate follow-on.
- The three tasks run sequentially today. If `draft_definitions` is independent of the assigned
  concept, task-level concurrency could cut latency; but the definition cache key (C6) suggests a
  dependency, so keep them sequential and document it rather than parallelising casually on one DB
  connection.

# Complementary lever
Anthropic prompt caching on the (versioned) vocabulary prefix compounds with batching and is cheaper
to build; the audit records the vocabulary by fingerprint regardless (C24).

# What stays invariant
Deterministic validation / brake / facts / drift / quarantine / joins / graph structure; cache-first
dedup; content-hash identity; advisory + fail-soft; the governed egress + audit seam. The graph is
built with or without enrichment.

# Readiness
Architecture: approved. Spec: ready to implement once the C-contracts above are the acceptance
criteria (they now are). The honest claim is the corrected one at the top — deterministic invariants
preserved and failures isolated per item by construction; semantic quality protected by the gold
gate, bounded fallback, telemetry, and the single-call rollback path.
