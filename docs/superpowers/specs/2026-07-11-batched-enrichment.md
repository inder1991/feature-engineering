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

## Current design (as-is, with code)

The call site is advisory and fail-soft (`ingest.py`): a provider error degrades search, never the
facts.

```python
# ingest.py — enrichment is optional and non-fatal
concepts = definitions = domains = None
if client is not None:
    try:
        concepts    = enrich_concepts(conn, vr.good, client, actor)
        definitions = draft_definitions(conn, vr.good, client, actor)
        domains     = classify_domains(conn, vr.good, client, actor)
    except Exception:                      # ADVISORY — degrade search, never abort the upload's facts
        concepts = definitions = domains = None
build_graph(conn, catalog_source, vr.good, concepts, definitions, domains)
```

Each enrichment function is a **cache-first, one-call-per-item loop** (`enrich.py`). The concept
loop is representative — note the two cost drivers marked inline:

```python
def enrich_concepts(conn, rows, client, actor=None) -> dict[str, str]:
    by_hash = {content_hash(r): r for r in rows}
    result  = _cache_get(conn, "enrichment_concept", list(by_hash))   # skip already-cached
    for h, row in by_hash.items():                                    # (A) ONE LLM CALL PER COLUMN
        if h in result:
            continue
        raw = _call(conn, client, _TASK, "overlay_concept_v1", "overlay_concept",
                    {"table": row.table, "column": row.column, "type": row.type,
                     "vocabulary": _CONCEPT_VOCABULARY},              # (B) VOCAB RESENT EVERY CALL
                    "concept", "Classify this column into the controlled vocabulary ...", actor)
        if raw is None:
            continue                                                  # failure -> don't cache (M3)
        concept = raw if is_known_concept(raw) else UNCLASSIFIED      # per-item grounding validator
        _cache_put(conn, "enrichment_concept", h, concept)
        result[h] = concept
    return result
```

The governed single-item seam (`enrich_llm.py`) returns one string, validated against a
single-field output schema:

```python
def audited_enrich_call(conn, client, *, task, prompt_id, schema_id,
                        catalog_metadata, out_key, instruction, actor=None) -> str | None:
    out = audited_structured_call(conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
                                  catalog_metadata=catalog_metadata, instruction=instruction,
                                  actor=actor)                        # egress guard + audit record
    if not out:
        return None
    return str(out.get(out_key, "")).strip() or None

# _SCHEMAS (enrich_llm.py) — a single-value object per call
("overlay_concept", 1): {"type": "object", "additionalProperties": False,
                         "properties": {"concept": {"type": "string"}}, "required": ["concept"]}
```

`draft_definitions` and `classify_domains` are the same shape (definition per blank-def column;
domain per table). So: **calls = distinct uncached columns (concept) + distinct uncached blank-def
columns (definition) + distinct uncached tables (domain)**, each a separate round-trip, each
resending its fixed prompt context.

## Current vs proposed at a glance

| Aspect | Current (as-is) | Proposed (batched) |
|---|---|---|
| LLM calls, 144 cols / 8 tables / no declared defs | ~296 | ~17 |
| Concept-vocabulary sends | 144 | 4 (once per batch) |
| Call granularity | 1 per column (concept, def) / 1 per table (domain) | chunk of `K_task` items per call |
| Cache-first dedup | yes | yes (unchanged; only the uncached remainder is batched) |
| Per-item validation | `is_known_concept` / `_bounded` per call | same validators, applied per item in the batch |
| Failure unit | one column skipped, retried next ingest | one item skipped (partial salvage), retried next ingest |
| Cross-item contamination | impossible (1 item/call) | impossible (each item carries a `ref`, matched back by key) |
| Audit (`llm_call`) rows | 1 per column | 1 per batch, recording covered refs |
| Quality control | inherent to single-item | per-task `K` + measured eval gate + fallback-to-single |
| Kill switch | n/a | `K_task = 1` reproduces today's exact per-item behavior |
| Structural graph (nodes/edges/joins/grain) | deterministic, untouched | deterministic, untouched |

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

## Proposed design (with code)

The per-item loop becomes "compute the uncached remainder -> chunk by `K_task` -> one batched call
per chunk -> validate + salvage per item -> cache." Everything else in the function is unchanged.

```python
def enrich_concepts(conn, rows, client, actor=None) -> dict[str, str]:
    by_hash = {content_hash(r): r for r in rows}
    result  = _cache_get(conn, "enrichment_concept", list(by_hash))   # SAME cache-first
    todo    = [(h, r) for h, r in by_hash.items() if h not in result] # uncached remainder only
    for chunk in _chunks(todo, K_CONCEPT):                            # (A') BATCHED, not per-column
        items = [{"ref": h, "table": r.table, "column": r.column, "type": r.type} for h, r in chunk]
        got = _call_batch(conn, client, _TASK, "overlay_concept_batch_v1", "overlay_concept_batch",
                          items, {"vocabulary": _CONCEPT_VOCABULARY},  # (B') VOCAB ONCE PER BATCH
                          "concept", "Classify EACH column independently ...", actor)  # -> {ref: concept}
        for h, row in chunk:
            raw = got.get(h)                                          # missing ref -> skip (salvage)
            if raw is None:
                continue
            concept = raw if is_known_concept(raw) else UNCLASSIFIED  # SAME per-item validator
            _cache_put(conn, "enrichment_concept", h, concept)
            result[h] = concept
    return result
```

The new governed batched seam mirrors `audited_enrich_call` but returns `{ref: value}` for the
valid items only, over an ARRAY output schema:

```python
def audited_batch_call(conn, client, *, task, prompt_id, schema_id,
                       items, extra_metadata, out_key, instruction, actor=None) -> dict[str, str]:
    """Governed batch: same egress guard + audit + attached schema as the single call; one llm_call
    row per batch. Returns {ref: value} for the items that passed validation."""
    out = audited_structured_call(conn, client, task=task, prompt_id=prompt_id, schema_id=schema_id,
                                  catalog_metadata={"items": items, **extra_metadata},
                                  instruction=instruction, actor=actor)
    got = {}
    for entry in (out or {}).get("results", []):
        ref = entry.get("ref"); val = str(entry.get(out_key, "")).strip()
        if ref and val:                       # extra/mis-ordered/blank ref -> dropped, never misattributed
            got[ref] = val
    return got

# _SCHEMAS gains the array variant — additionalProperties:false, ref required per item
("overlay_concept_batch", 1): {"type": "object", "additionalProperties": False, "required": ["results"],
    "properties": {"results": {"type": "array", "items": {
        "type": "object", "additionalProperties": False, "required": ["ref", "concept"],
        "properties": {"ref": {"type": "string"}, "concept": {"type": "string"}}}}}}
```

### Files touched
- `enrich.py`: the three loops adopt the chunk-and-salvage shape above; add `_chunks` + `_call_batch`
  wrappers and the fallback-to-single branch. Cache helpers (`_cache_get`/`_cache_put`) and
  validators (`is_known_concept`, `_bounded`) are reused unchanged.
- `enrich_llm.py`: add `audited_batch_call`; add `overlay_concept_batch_v1` /
  `overlay_definition_batch_v1` / `overlay_domain_batch_v1` to `_SCHEMAS` (each `{results:[{ref,
  <value>}]}`, `additionalProperties:false`). `register_enrichment_schemas` picks them up unchanged.
- `ingest.py`: the call site does not change — it already calls the three functions and stays
  fail-soft.
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
