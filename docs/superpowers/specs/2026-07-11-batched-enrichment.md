# Batched Enrichment — Design Spec

Date: 2026-07-11. Status: DESIGNED (not built). Purpose: cut upload-time LLM cost by batching the
concept / definition / domain enrichment calls, WITHOUT degrading enrichment quality. Quality is
the hard constraint; batching is the means. A batch that lowers per-item quality is a bug, not a
saving.

## The cost today

`ingest_upload` runs enrichment only when an LLM client is configured (`client is not None`,
`ingest.py`). When it does, `enrich.py` makes **one call per item**:
- `enrich_concepts`: one call per distinct uncached column (classify into the controlled vocabulary).
- `draft_definitions`: one call per distinct uncached column that has no declared definition (R3).
- `classify_domains`: one call per distinct uncached table.

A first-time 144-column, 8-table upload with no declared definitions is ~296 provider round-trips,
and the concept prompt re-sends the ~281-term controlled vocabulary on every one of the 144 concept
calls. That resent vocabulary is the dominant token cost, and it is pure waste.

## Principle: batch the uncached remainder, quality-preserving by construction

Everything load-bearing stays exactly as it is; only the transport changes.
- **Cache-first is unchanged.** `enrich.py` still checks the `enrichment_concept/definition/domain`
  tables before any call and batches ONLY the uncached remainder. Re-uploads stay free. Per-column
  content-hash keying is unchanged — the batch is just how the uncached set is sent.
- **Per-item validation is unchanged.** `is_known_concept` still maps any concept outside the
  vocabulary to `UNCLASSIFIED`; `_bounded` still rejects empty / over-long / multiline /
  list-stringified definitions. A degraded item is caught and skipped per item, batched or not.
- **Advisory semantics unchanged.** Enrichment never blocks the upload; a batch failure degrades
  search, never a fact.
- **Egress guard unchanged.** Inputs stay metadata-only (names / types, table+columns). The guard
  runs once per batch over the same clean material.

## Quality preservation (the load-bearing part)

Batching threatens quality in exactly one way: a model asked to produce many items in one shot can
get lazier or anchor on earlier items, and free-text generation degrades faster than constrained
classification. Six controls, in priority order:

### 1. Per-task batch sizes, not one global K

The batch a task can tolerate depends on how constrained its output is:

| Task | Output | Batch tolerance | Batch size |
|---|---|---|---|
| Concept | pick from a fixed ~281-term vocabulary | HIGH (constrained answer space, per-item validated) | `K_concept` default **40** |
| Definition | free-text one-liner | LOW (generation degrades across items) | `K_def` default **12** |
| Domain | one short label per table | MEDIUM, and volume is low anyway | `K_domain` default **20** |

Concept batches aggressively because the answer space is closed and every answer is validated;
definitions batch conservatively because that is where quality is genuinely at risk. Keep the tasks
as SEPARATE batched calls (do not fold concept+definition into one call — that would force the
definition's small K onto the concept task, or the concept's large K onto definitions and drop
definition quality). All three K's are config, tunable per deployment/model.

Resulting call count for the 144-column / 8-table example (no declared definitions):
concept `ceil(144/40)=4` + definition `ceil(144/12)=12` + domain `ceil(8/20)=1` = **17 calls**
(vs 296), and the vocabulary is sent 4 times instead of 144. If definitions are pre-filled: **5**.

### 2. A measured quality gate before any K becomes default

`K_*` are not guessed and shipped. A committed eval harness (`tests/eval/`, run manually / in a
nightly, not in unit CI) compares batched output against the single-item baseline on real schemas
(the demo deposits catalog + a larger bank-shaped fixture, e.g. a 200-column core-banking extract),
scoring:
- **Concept**: agreement rate with the single-item result (target >= 98%); UNCLASSIFIED rate must
  not rise.
- **Definition**: an LLM-judge rubric (accuracy, specificity, one-line) scored batched vs single;
  the batched mean must be within a small epsilon of single-item.
- **Domain**: exact-match agreement with single-item.

The chosen `K_*` is the LARGEST batch size that holds quality within tolerance. If a future model
or vocabulary change moves the numbers, the eval re-runs and the config is retuned. "Quality is not
impacted" is thereby a measured property, not a hope.

### 3. Deterministic per-item keying (no cross-contamination)

Each item carries a stable `ref` in BOTH the request and the required response shape. The code
matches every returned item back to its content-hash by `ref`; a missing, extra, or mis-ordered
`ref` is detected and that item is skipped (not cached, retried next ingest). This makes it
structurally impossible for one column to receive another column's concept — the failure mode a
naive positional batch would introduce.

### 4. Partial salvage

A batch response is validated item by item: valid items are cached, invalid/missing ones are left
uncached for the next upload. One degraded item never poisons the cache and never costs the whole
batch. This replaces `drive_structured_call`'s all-or-nothing repair with "validate the array,
cache the good, re-request the gaps" in a new batched entry point beside `audited_enrich_call`.

### 5. Fallback-to-single circuit breaker

If a batch returns materially fewer valid items than sent (a degradation signal), OR the uncached
remainder for a task is at or below a small threshold (e.g. <= 3 items — batching buys nothing
there), fall back to per-item calls for those items. Worst case the system is back to today's
per-item quality; it can never be worse than today.

### 6. Low, pinned generation settings

Classification tasks (concept, domain) pin low temperature to minimise variance; definitions allow
marginally more but stay low. `generation_settings` are pinned on the request so the audit record
and idempotency key are stable.

## What changes in code

- `enrich.py`: the three per-item loops become "collect uncached items -> chunk by `K_task` ->
  batched call -> validate + salvage -> cache." The chunking and salvage live here; the cache
  helpers (`_cache_get`/`_cache_put`) and validators (`is_known_concept`, `_bounded`) are reused
  unchanged.
- `enrich_llm.py`: a new `audited_batch_call(conn, client, *, task, items, schema_id, ...)` beside
  `audited_enrich_call`, running the same governed seam (attached output-schema, reserved input
  keys, `assert_llm_safe`, `record_llm_call`) but with an ARRAY output schema and per-item
  validation returning `{ref: value}` for the valid items only. One `llm_call` audit row per batch,
  recording the covered refs + cost.
- New registered output schemas: `overlay_concept_batch_v1`, `overlay_definition_batch_v1`,
  `overlay_domain_batch_v1` — each `{results: [{ref, <value>}]}`, `additionalProperties: false`.
- Config: `OVERLAY_ENRICH_BATCH_CONCEPT` (40), `_DEFINITION` (12), `_DOMAIN` (20),
  `OVERLAY_ENRICH_SINGLE_FALLBACK_THRESHOLD` (3). Setting a K to 1 disables batching for that task
  (exact current behavior) — a safe kill-switch.

## What stays invariant

Cache-first dedup; per-column content-hash keying; the `is_known_concept` + `_bounded` validators;
metadata-only egress; advisory (never blocks upload); the audited seam and its `llm_call` record.
The graph's STRUCTURE (nodes/edges/joins/grain/sensitivity) is deterministic and untouched —
enrichment only fills the advisory `concept/domain/definition` fields on `graph_node`.

## Complementary lever (out of scope here, noted)

Anthropic prompt caching on the concept vocabulary prefix cuts the resent-vocabulary cost even
further and composes with batching (cache the stable vocabulary prefix, 5-min TTL). And the bigger
architectural move — running enrichment ASYNC on the durable worker so upload latency is decoupled
entirely — is a separate follow-on; batching cuts cost, async cuts latency, they compose.

## Testing

- Unit: chunking math; salvage (a batch missing 2 of 10 refs caches 8, leaves 2 uncached);
  ref-mismatch detection (extra/mis-ordered ref skipped, never misattributed); cache-first (only
  uncached items batched); K=1 kill-switch reproduces per-item behavior; fallback-to-single below
  threshold; one `llm_call` per batch with correct covered refs; egress guard runs on the batch.
- Eval (manual/nightly, `tests/eval/`): the quality gate of section 2 — batched vs single-item on
  real fixtures, the pass bar being the tolerances above.

## Cost estimate

144-column / 8-table first upload: ~296 -> ~17 calls (~17x fewer round-trips) with definitions
blank; ~5 calls if declared. Token cost drops more than call count on the concept task because the
vocabulary is amortized across the batch. Re-uploads remain 0 (cache). No quality regression is the
acceptance bar, verified by the eval gate before the defaults ship.
