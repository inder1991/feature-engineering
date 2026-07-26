# E1a — LLM Metadata Enrichment (functional) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the LLM's metadata enrichment *real and governed* — promote its already-drafted `definition`/`domain` from display-only into `llm/proposed` `field_evidence`, add LLM-authored synonyms, broaden concept-evidence coverage, and make the concept→behavioural cascade carry the concept's provenance (the transitive-provenance safety fix). Feature-first: the enrichment is end-to-end testable by Task 2.

**Architecture:** Generalise the existing concept-evidence writer (`_write_concept_evidence`) into a field-parameterised `llm/proposed` evidence writer, then wire the existing `draft_definitions`/`classify_domains` outputs (today consumed display-only in `build_graph`) through it. `definition`/`domain` already admit `llm/proposed` in their field policy and asset-detail already labels `("llm","proposed") → "AI proposed"`, so no policy/schema/frontend change is needed for those. The cascade fix threads the concept's asserted strength into `derive_concept_evidence`.

**Tech Stack:** Python 3.12, psycopg (raw SQL), pytest with the `overlay_conn` ephemeral-Postgres fixture, `FakeLLM`/`FakeResponse` scripted by task-key.

## Scope & Deferrals (per the architectural steer)

- **IN (functional enrichment only):** the generalised writer; `definition`, `domain`, synonyms enrichment as governed/authored metadata; broadened concept-evidence coverage; the transitive-provenance cascade.
- **DEFERRED — NFRs, handled later (NOT in this plan):** the feature-gen-lift measurement harness / exit-gate instrumentation, the coverage/readiness dashboard, the review-queue/governance UI, bulk-by-convention correction, operational/cold-start/enablement-interlock/async-batching, compliance/audit reporting, ROI, and any per-field confidence signal (P2). E1a here = the feature works and is tested; proving the lift is a follow-on.
- **DEFERRED — reuse/caching of LLM results (pure optimization).** Do **not** build unchanged-detection or a result cache for the new fields. The writer **supersedes and rewrites** unconditionally (stale prior LLM evidence for the field, then insert fresh) — simple and correct. The *existing* concept writer keeps its own reuse (don't touch working code); we just don't add reuse to the new fields.

## Global Constraints

- **`llm/proposed`, never a *governed* authority — but concept IS influential via the direct path (be honest).** All writes use `producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED`; the `_MEANING`/`_CONCEPT` RECOMMENDATION ceiling bars them from the **governed/operational** resolution (don't change ceilings). **But do NOT claim "never load-bearing":** feature-gen template matching reads the flat `graph_node.concept` **directly**, bypassing the resolver, so an LLM-proposed concept already influences which features are generated. The honest rule (matches the flexibility we want): LLM concepts MAY drive template matching, but a generated feature must carry the concept's **producer + strength** so downstream consumers apply their own policy (permissive accepts `proposed`; strict requires `confirmed`) — never let template code read a concept value **without** its provenance. (Threading producer+strength into the generated-feature record is a feature-gen-side follow-on; E1a writes the provenance-bearing evidence so it's available — but the plan must stop calling LLM concepts non-load-bearing.)
- **Leave the existing concept writer alone.** `_write_concept_evidence` (with its existing reuse/cache) is **not** refactored or routed through the new writer — it stays byte-identical (its tests, `tests/featuregen/overlay/upload/test_pass_a_evidence.py`, must stay green). The new `_write_llm_field_evidence` is a *separate, simpler* writer for the new fields only. Modest duplication is accepted here in exchange for not touching working code and not building reuse.
- **Producer-scoped staleness only.** Only LLM rows are staled (`producer=EvidenceProducer.LLM`) — never human/taxonomy/source rows.
- **Reconcile the target universe — don't just process successes (correctness, not optimization).** The writer sees only successful results; that leaves stale AI evidence active whenever a column drops out of a run. After enrichment, **reconcile per field** against the columns that currently hold active LLM evidence, by disposition of each expected target:
  - **successful** → supersede prior + write fresh.
  - **deliberately withheld** (sanitizer-suppressed / egress-blocked / accept-gate-rejected) → **RETIRE** prior AI evidence — a suppressed field must not keep asserting an old AI value.
  - **no longer a target** (now source-provided / no longer blank / ineligible / table gone) → **RETIRE** prior AI evidence.
  - **transient failure** (provider timeout / egress error) → **KEEP** prior — do not discard good data on a blip.
  Concretely: `retire_refs = (refs with active LLM evidence for the field) − (successfully rewritten) − (transient-failed)`; stale those. The drafter MUST report transient failures distinctly from deliberate suppression (the egress audit already records `EGRESS_BLOCKED` vs a provider error) so only transient ones are preserved.
- **Fail-soft per item — wrap the WHOLE item, and propagate the count.** The **entire** per-item body (valid check, `ref_of`, binding lookup, hash, savepoint write) is inside one `try/except/continue`, so a throw in `ref_of`/hash for one column can't abort the batch (only the write being in the `try` was a bug — a `ref_of` exception would kill the loop). Real exceptions increment a returned `failures` int; skips (invalid/unattachable) just `continue` without counting. **The caller MUST propagate `failures`** through the existing `_enrichment_outcome`/`record_stage` path — status **`partial`** (reason_code **`items_failed`**), **not** `succeeded`, when some items wrote and some failed (verified: `STAGE_STATES`, `stage_report.py:40-43`; existing partial handling `ingest.py:244-247`). Losing metadata while reporting success is a functional bug. A required test proves the item *after* a failing item is still written.
- **Egress unchanged.** All LLM calls keep going through the existing `audited_structured_call` egress guard; no raw sample values leave (definition/semantic_terms ride the sanitized `sanitize_definition` path).
- **`draft_definitions`/`classify_domains` are reused as-is** (they already exist and run in ingest) — this plan adds the *evidence write*, it does not re-implement the drafters.
- **TWO identities per column — never collapse them (real-file correctness).** A glossary column has a **public-flattened** identity used to check *attachability* (`normalize_ref(source, None, table, column)` → `ftr::public.accounts.balance`) and a **schema-preserving** identity used to *store evidence* (`rec.logical_ref` → `ftr::dpl_eib_compliance.accounts.balance`). The original `_write_concept_evidence` looks up the binding under the **public** ref and stores under the **schema** ref. The generic writer MUST keep them separate: check `bindings.get(binding_ref)` but write/stale/hash/read under `evidence_ref`. Collapsing to one ref silently skips every non-`public`-schema FTR column (binding miss → `continue`), writing zero evidence and reporting zero failures — a silent no-op on real data.
- **Cascade provenance = ONE resolved record (anti-laundering, Task 6).** The concept→taxonomy cascade derives BOTH the value and the strength from the **same resolved concept evidence record** — never a value from the LLM `concepts` dict combined with a strength read from a separate row. A derived safety fact's value and its confirmed-ness must belong to each other; otherwise an LLM value can inherit a human's `confirmed` strength (laundering). "Latest active" is not the resolver's selected value — use the canonical resolution (PREFER_CONFIRMED).

## File Structure

- Modify: `src/featuregen/overlay/upload/enrich.py` — extract `_write_llm_field_evidence`; add `_write_definition_evidence` + `_write_domain_evidence`; LLM synonym drafting + evidence; broaden concept-evidence gate.
- Modify: `src/featuregen/overlay/upload/ingest.py` — call the new definition/domain/synonym evidence writes in the enrichment stage; call `derive_and_write_concept_cascade(...)` in place of the inline cascade (`~1181-1207`).
- Modify: `src/featuregen/overlay/upload/taxonomy_evidence.py` — no signature change (already takes `concept_strength`); the fix is entirely at the call site (derive value+strength from the resolved record).
- Modify: `src/featuregen/overlay/upload/field_correction.py` — in the `field=="concept"` branch of `_confirm_override`/`_confirm_existing`/`_reject`, after `resolve_and_project(..., fields=["concept"])`, call `derive_and_write_concept_cascade(...)` to recompute dependent taxonomy in the same tx (verified: `resolve_and_project` has no cascade hook; ingest's cascade is upstream). Also extend `_write_producer_field` (`ingest.py:954`) to forward `producer_item_ref`/`evidence_spans` (root links).
- Modify: the post-`build_graph` semantic-terms projection stage — **merge** active `llm/proposed` `semantic_terms` evidence with the glossary/source terms onto `graph_node.semantic_terms` (union, never overwrite) + `rebuild_search_doc`, AFTER `build_graph` so it survives the node rebuild (Task 4).
- Modify: the domain drafter (`classify_domains` → `{table_domain, column_domains}`), the graph domain projection (column `domain = column_domains.get(col) or table_domain`), and `asset_detail.py` (expose `origin` direct-vs-inherited + `inherited_from` on the domain field) — the Task 3 two-level model.
- Test: `tests/featuregen/overlay/upload/test_llm_field_evidence.py` (new), plus additions to the existing enrichment/asset-detail test modules.

---

### Task 1: A new, simple `llm/proposed` evidence writer (for the new fields; concept writer untouched)

**Files:**
- Modify: `src/featuregen/overlay/upload/enrich.py` (new `_write_llm_field_evidence`, modelled on `_write_concept_evidence:326-397` but **without** its reuse/cache — supersede-and-rewrite only). `_write_concept_evidence` is **not** changed.
- Test: `tests/featuregen/overlay/upload/test_llm_field_evidence.py`

**Interfaces:**
- Produces: `_write_llm_field_evidence(conn, *, field_name, items, ref_of, source_snapshot_id, valid_fn=None, producer_configuration_hash=None, bindings=None) -> int` where `items: dict[K, str]` (key→value), `ref_of: Callable[[K], tuple[str, str, dict] | None]` returns **`(evidence_ref, binding_ref, material)`** — the schema-preserving storage ref, the public-flattened attachability ref, and the input-hash material — or `None` to skip. **No reuse/cache params** (deferred optimization).
- Consumes: `record_field_evidence` (`field_evidence.py:86`), `stale_source_evidence` (`:178`), `field_input_hash` (`:49`), `read_active_field_evidence` (`:136`), `may_attach` (`object_identity.py:124`).

- [ ] **Step 1: Write the failing test**

```python
# tests/featuregen/overlay/upload/test_llm_field_evidence.py
from featuregen.overlay.upload.enrich import _write_llm_field_evidence
from featuregen.overlay.upload.graph import build_graph
from featuregen.overlay.upload.model import CanonicalRow
from featuregen.overlay.field_evidence import read_active_field_evidence

def test_writes_generic_llm_proposed_evidence(overlay_conn):
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "notional", "numeric")])
    ref = f"{src}::public.trades.notional"
    n = _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h1": "Notional amount of the trade"},
        ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")   # (evidence, binding, material)
    assert n == 0                      # zero failures
    ev = read_active_field_evidence(overlay_conn, ref, "definition")
    assert ev[0].producer == "llm" and ev[0].strength == "proposed"
    assert ev[0].proposed_value == "Notional amount of the trade"

def test_reenrich_supersedes_prior_llm_evidence(overlay_conn):
    # re-enrichment supersedes prior LLM evidence -> exactly one active row with the new value
    # (no reuse optimization; the writer stales-all then writes fresh)
    src = "ftr"; build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "ccy", "text")])
    ref = f"{src}::public.trades.ccy"
    _write_llm_field_evidence(overlay_conn, field_name="definition", items={"h": "Currency of the trade"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    _write_llm_field_evidence(overlay_conn, field_name="definition", items={"h": "Settlement currency"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")  # re-enrich
    active = read_active_field_evidence(overlay_conn, ref, "definition")
    assert len(active) == 1 and active[0].proposed_value == "Settlement currency"  # superseded, one active row

def test_binding_checked_at_public_ref_stored_at_schema_ref(overlay_conn):
    """The regression guard for the two-identity bug: a non-public-schema column must NOT be skipped.
    Binding lives under the PUBLIC ref; evidence must store under the SCHEMA-preserving ref."""
    src = "ftr"
    build_graph(overlay_conn, src, [CanonicalRow(src, "accounts", "balance", "numeric",
                                                 schema="dpl_eib_compliance")])
    ev_ref = f"{src}::dpl_eib_compliance.accounts.balance"   # schema-preserving storage
    bind_ref = f"{src}::public.accounts.balance"             # public-flattened attachability
    # An EXACT binding keyed by the PUBLIC ref (build via the real classify path, or construct
    # object_identity.ObjectBinding(status=ObjectIdentityStatus.EXACT, ...) for bind_ref).
    bindings = {bind_ref: _exact_binding(bind_ref)}
    n = _write_llm_field_evidence(
        overlay_conn, field_name="definition", items={"h": "Account balance"},
        ref_of=lambda k: (ev_ref, bind_ref, {"k": k}), source_snapshot_id="snap", bindings=bindings)
    assert n == 0
    # Stored under the SCHEMA ref, NOT skipped (the bug would write nothing here):
    assert read_active_field_evidence(overlay_conn, ev_ref, "definition")
    assert not read_active_field_evidence(overlay_conn, bind_ref, "definition")

def test_withheld_target_retires_prior_ai(overlay_conn):
    # a column that HAD AI evidence but is deliberately withheld this run -> prior AI is RETIRED
    src = "ftr"; build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "note", "text")])
    ref = f"{src}::public.trades.note"
    _write_llm_field_evidence(overlay_conn, field_name="definition", items={"h": "old AI def"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    _reconcile_llm_field_evidence(overlay_conn, field_name="definition", retire_refs={ref})  # withheld/suppressed
    assert read_active_field_evidence(overlay_conn, ref, "definition") == []  # retired, not lingering

def test_transient_failure_keeps_prior_ai(overlay_conn):
    # a transient failure must NOT retire prior AI (ref excluded from retire_refs)
    src = "ftr"; build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "memo", "text")])
    ref = f"{src}::public.trades.memo"
    _write_llm_field_evidence(overlay_conn, field_name="definition", items={"h": "keep me"},
                              ref_of=lambda k: (ref, ref, {"k": k}), source_snapshot_id="snap")
    _reconcile_llm_field_evidence(overlay_conn, field_name="definition", retire_refs=set())  # transient -> preserved
    assert read_active_field_evidence(overlay_conn, ref, "definition")  # kept

def test_fail_soft_item_after_failure_still_written(overlay_conn):
    # a ref_of that THROWS for one key must not abort the batch; the item after it is still written
    src = "ftr"; build_graph(overlay_conn, src, [CanonicalRow(src, "trades", "good", "text")])
    good = f"{src}::public.trades.good"
    def ref_of(k):
        if k == "bad":
            raise ValueError("boom")                    # throws where the OLD narrow try wouldn't catch it
        return (good, good, {"k": k})
    n = _write_llm_field_evidence(overlay_conn, field_name="definition",
                                  items={"bad": "x", "good": "kept"},  # dict preserves order: bad first, then good
                                  ref_of=ref_of, source_snapshot_id="snap")
    assert n == 1                                        # one failure counted, not raised
    assert read_active_field_evidence(overlay_conn, good, "definition")  # item AFTER the failure was written
```

- [ ] **Step 2: Run to verify failure** — `uv run pytest tests/featuregen/overlay/upload/test_llm_field_evidence.py -x -q` → FAIL (function missing).

- [ ] **Step 3: Implement the generic writer + delegate concept to it**

```python
# enrich.py — new SIMPLE writer (no reuse, no cache): supersede prior LLM evidence, write fresh.
def _write_llm_field_evidence(conn, *, field_name, items, ref_of, source_snapshot_id,
                              valid_fn=None, producer_configuration_hash=None, bindings=None) -> int:
    failures = 0
    for key, value in items.items():
        try:
            if valid_fn is not None and not valid_fn(value):
                continue                                     # skip — not a failure
            resolved = ref_of(key)
            if resolved is None:
                continue
            evidence_ref, binding_ref, material = resolved   # storage identity, attachability identity, material
            if bindings is not None:
                binding = bindings.get(binding_ref)          # PUBLIC-flattened attachability lookup
                if binding is None or not may_attach(binding):
                    continue
            input_hash = field_input_hash(logical_ref=evidence_ref, field_name=field_name, material=material)
            with conn.transaction():
                # Supersede ALL prior active LLM evidence for this field, then write fresh.
                # No unchanged-detection/reuse and no result cache — deferred optimization.
                stale_all_llm_field_evidence(conn, logical_ref=evidence_ref, field_name=field_name)
                record_field_evidence(
                    conn, logical_ref=evidence_ref, field_name=field_name, proposed_value=value,
                    producer=EvidenceProducer.LLM, strength=AssertionStrength.PROPOSED,
                    producer_ref=ENRICHMENT_RUN_ID, producer_item_ref=str(key),
                    producer_configuration_hash=producer_configuration_hash,
                    source_snapshot_id=source_snapshot_id, input_hash=input_hash)  # SCHEMA-preserving storage
        except Exception:  # noqa: BLE001 — fail-soft: the WHOLE item is wrapped, so one bad item never aborts the rest
            failures += 1
            logger.warning("advisory %s field_evidence write failed for item %s", field_name, key, exc_info=True)
            continue
    return failures
# stale_all_llm_field_evidence: thin wrapper over
#   stale_source_evidence(conn, logical_ref=..., field_name=..., producer=LLM, keep_input_hash=_STALE_ALL)
# where _STALE_ALL = "__field_absent_from_upload__" (ingest.py:772 — non-hex, never matches a real input_hash);
# the same stale-all sentinel the codebase already uses in _stale_absent_fields. (Verified.)
```
**Do NOT refactor `_write_concept_evidence`.** Leave it (and its existing reuse/cache) untouched — it stays byte-identical. It is only the *model* the new writer copies (its two-identity pattern: `bindings.get(normalize_ref(source, None, table, column))` for the binding, `rec.logical_ref` for storage). Only the NEW fields use `_write_llm_field_evidence`; concept's evidence path is unchanged.

Reconciliation companion (retires prior AI for dropped targets — the target-universe fix):
```python
def _reconcile_llm_field_evidence(conn, *, field_name, retire_refs) -> None:
    # Retire prior active LLM evidence for columns this run must drop: withheld/suppressed or no-longer-a-target.
    for evidence_ref in retire_refs:
        stale_all_llm_field_evidence(conn, logical_ref=evidence_ref, field_name=field_name)
```
Each field's stage computes `retire_refs = (refs with active LLM evidence for this field) − (successfully written) − (transient-failed)`. **Verified gap:** the drafters do NOT return the disposition today — a deliberate suppression and a transient provider error both collapse to `None`. So the stage must **surface it explicitly** (in its `stats`/return): the **withheld → retire** signal comes from `GlossaryRecord.definition_suppressed` (`glossary_reader.py:98`, already honored by `draft_definitions`) and the `EGRESS_BLOCKED` audit event (`enrich_llm.py:354`); a **transient → keep** is a miss with neither. Do not assume `transient_failed`/`withheld` already exists — build it. Runs once per field after the writes, in the enrichment tx.

- [ ] **Step 4: Run tests** — the new module PASSES and `uv run pytest tests/featuregen/overlay/upload/test_pass_a_evidence.py -q` (concept regression) stays green.

- [ ] **Step 5: Commit** — `git commit -m "refactor(e1a): field-parameterised llm/proposed evidence writer"`

---

### Task 2: Promote `definition` → `llm/proposed` evidence (the first working feature, end-to-end)

**Files:** Modify `enrich.py` (add `_write_definition_evidence`), `ingest.py` (call it). Test: extend `test_llm_field_evidence.py`.

**Interfaces:** Consumes `draft_definitions` output `{content_hash: definition}` (`enrich.py:532`), the `by_hash`/`rec_by_tc` mapping (column-grained, same as concept). Produces `llm/proposed` `definition` evidence surfaced by asset-detail as `"AI proposed"` (no asset-detail change — `_METADATA_FIELDS`/`_EVIDENCE_PROVENANCE_LABELS` already handle it).

- [ ] **Step 1: Write the failing end-to-end test**

```python
def test_ai_definition_becomes_governed_evidence(overlay_conn):
    # upload a glossary column with NO declared definition; script the AI drafter; assert evidence + provenance
    ...  # read a small glossary via read_glossary; FakeLLM script {"overlay.enrich.definition": FakeResponse(output={"results":[{"ref": h, "definition": "Customer full legal name"}]})}
    from featuregen.overlay.upload.asset_detail import build_asset_detail
    ev = read_active_field_evidence(overlay_conn, NAME_REF, "definition")
    assert ev and ev[0].producer == "llm" and ev[0].strength == "proposed"
    field = build_asset_detail(overlay_conn, NAME_REF, include=["effective_metadata"])["effective_metadata"]["fields"]["definition"]
    assert field["evidence_provenance"] == "AI proposed"
```
(Model the seeding on `test_pass_a_evidence.py:88-117` + the provenance assertion on `test_asset_detail_provenance.py:23-33`.)

- [ ] **Step 2: Run to verify failure** — no definition evidence exists today (display-only).

- [ ] **Step 3: Implement**

```python
# enrich.py
def _write_definition_evidence(conn, *, definitions, by_hash, rec_by_tc, meta_by_hash,
                               bindings, source_snapshot_id) -> int:
    def ref_of(h):
        row = by_hash.get(h)
        if row is None: return None
        rec = rec_by_tc.get((_norm(row.table), _norm(row.column)))
        if rec is None: return None
        binding_ref = normalize_ref(row.source, None, row.table, row.column)   # PUBLIC-flattened
        return rec.logical_ref, binding_ref, meta_by_hash.get(h, {"table": row.table, "column": row.column})
    return _write_llm_field_evidence(
        conn, field_name="definition", items=definitions, ref_of=ref_of,
        source_snapshot_id=source_snapshot_id, valid_fn=lambda v: bool(v and v.strip()),
        producer_configuration_hash=None, bindings=bindings)
```
Wire it in the definition enrichment path (mirror how `enrich_concepts` writes concept evidence when `glossary and source_snapshot_id`): after `draft_definitions(...)` in `ingest.py:1878-1883`, when `glossary is not None and snapshot_id is not None`, call `_write_definition_evidence(conn, definitions=definitions, by_hash=..., rec_by_tc=..., meta_by_hash=..., bindings=bindings, source_snapshot_id=snapshot_id)`. (Build `by_hash`/`rec_by_tc`/`meta_by_hash` the same way `enrich_concepts` does — extract that mapping into a shared helper if cleaner.)

- [ ] **Step 4: Run tests** — new test PASSES; upload → AI definition → governed evidence → "AI proposed" visible. **This is the feature working end-to-end.**

- [ ] **Step 5: Commit** — `git commit -m "feat(e1a): AI definitions become governed llm/proposed evidence"`

---

### Task 3: Domain evidence at BOTH table and column levels (default + override)

**Files:** `enrich.py` (extend the domain drafter to `{table_domain, column_domains}`; `_write_domain_evidence`), the graph domain projection, `asset_detail.py` (origin: direct vs inherited). Test: extend the module.

**The two-level model (per directive):** Today `classify_domains` returns `{table: domain}` and `build_graph` copies the table domain onto **every** column node (`graph.py:234,246`) — so graph *columns* carry a domain but the plan wrote only *table* evidence: a mismatch. Fix with a real two-level model:
- **Table domain = default context; column domain = optional explicit override.** Effective column domain = the column's own domain if present, else **inherited** from its table.
- **Evidence stays separate and honest:** write table-domain evidence on the **table** node, and column-domain evidence on a **column** node **only when that column overrides** the table default. **Never fabricate column evidence for an inherited domain.**
- **Provenance preserves origin:** an inherited column exposes `{domain, origin:"inherited", inherited_from:<table>}` (no column evidence); a direct column exposes `{domain, origin:"direct", producer:"llm", strength:"proposed"}` (column evidence exists).
- **LLM result shape:** `{table_domain: str, column_domains: {col: domain}}` — a table default plus explicit overrides *only* for columns needing a different/more specific domain.

- [ ] **Step 1: Failing tests**
  - Table domain → `llm/proposed` `domain` evidence on the **table** node; its columns render `origin="inherited"`, `inherited_from=<table>`, and have **no** column domain evidence.
  - A column override (e.g. `fraud_score → fraud_risk` under a `retail_banking` table) → `llm/proposed` `domain` evidence on the **column** node, `origin="direct"`, and the column's effective domain is the override, not the table default.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement**
  - Extend the domain drafter to return `{table_domain, column_domains}` (schema + prompt: "give the table's domain, and list only columns whose domain differs").
  - Write **table**-domain evidence via `_write_llm_field_evidence(field_name="domain", ...)` with a table two-identity `ref_of` (`bindings=None`, schema-preserving table ref — never default to `public`); write **column**-override evidence with a column `ref_of` — overrides only.
  - Graph domain projection: column node `domain = column_domains.get(col) or table_domain` (keep the inherited table-default copy for non-override columns).
  - `asset_detail` domain field: if the column has its own domain evidence → `origin="direct"` (+ evidence provenance); else → `origin="inherited"`, `inherited_from=<table>` (read the table node's domain) — no fabricated column evidence.
- [ ] **Step 4: Run → pass.**
- [ ] **Step 5: Commit** — `git commit -m "feat(e1a): two-level domain evidence — table default + column override, direct-vs-inherited provenance"`

---

### Task 4: LLM synonyms as first-class `llm/proposed` semantic evidence (available to feature-gen)

**Files:** `enrich.py` (a `draft_synonyms` drafter + the evidence write via the generalised writer), the post-`build_graph` projection stage. Test: new.

**Model (per directive):** LLM synonyms are **not** search-only and **not** human-confirmed — they are **first-class `llm/proposed` semantic evidence**. Flow: *LLM drafts synonyms → store durable `field_evidence` (field_name=`semantic_terms`, producer=`llm`, strength=`proposed`) → build the graph → **merge/project** active semantic_terms evidence onto `graph_node.semantic_terms` → available to search, ranking, and **feature generation**.* Requirements: an LLM synonym may be the **only** semantic signal that selects a column (no corroboration, no human approval, no `search_only` restriction, **no new synonym gate** — it flows through the existing menu/relevance path, `feature_assist.py:140,160,328`); the LLM origin is **preserved** (on the evidence row); the projection **merges** LLM terms with source/human terms (never overwrites); re-enrichment **stales only prior LLM** terms (the generalised writer's producer-scoped staleness handles this); the projection runs **after `build_graph`** so terms survive. Principle: *store provenance with the synonym; let downstream decide how much authority to give it.* (Ride the sanitized egress path — `semantic_terms` ∈ `_FEATURE_COLUMN_DEFINITION_KEYS`, `sanitize_definition`.)

- [ ] **Step 1: Failing tests**
  - Evidence: an AI-drafted synonym is stored as `field_evidence(field_name="semantic_terms", producer="llm", strength="proposed")` under the schema-preserving ref (via the generalised writer's two-identity `ref_of`).
  - Merge (not overwrite): after a full ingest of a column that has *both* glossary terms and AI synonyms, `graph_node.semantic_terms` contains **both** (union), not just one.
  - Feature-gen reach: a column whose *only* semantic signal is an AI synonym is selectable via that synonym in the candidate menu (no new gate; existing relevance path).
  - Re-enrich staleness: re-running enrichment with a changed synonym stales the prior **LLM** term but leaves glossary/human terms intact.
- [ ] **Step 2: Run → fail.**
- [ ] **Step 3: Implement**
  - `draft_synonyms(conn, rows, client, ...) -> {content_hash: terms}` (reuse `_call`/`run_batched`, `_SYN_TASK="overlay.enrich.synonyms"`, `accept=_accept_bounded(...)`; register the schema in `enrich_llm`).
  - **Pipeline wiring (verified — WITHOUT this the stage never runs):** (1) call `draft_synonyms` in the **client-present** block (~`ingest.py:1895`) inside a `with conn.transaction():` savepoint with its own `stats`; (2) route the LLM request through `audited_structured_call`/`audited_enrich_call`/`run_batched` — **sanitization is NOT automatic** for a raw `client.call`; (3) `state, reason, detail = _enrichment_outcome(...)` + `record_stage(recorder, "enrich_synonyms", state, reason_code=reason, detail=...)`; (4) add `enrich_synonyms` to `CANONICAL_STAGES` (`stage_report.py:50`) **and** the `skipped_no_client` loop (`ingest.py:1831`) so a no-LLM-client upload records `skipped_no_client` and **ingestion continues**; (5) add its default mode to `enrich_config`; (6) thread `ingestion_run_id`. Gate = provider presence (no dedicated flag), same as definition/domain.
  - **Store evidence** via `_write_llm_field_evidence(field_name="semantic_terms", items=synonyms, ref_of=<two-identity ref_of>, valid_fn=<non-empty>, bindings=bindings, ...)` — durable, `llm/proposed`, producer-scoped staleness (stales only prior LLM semantic_terms).
  - **Project AFTER `build_graph`** (core ordering bug — enrich runs before `build_graph`, which delete+recreates nodes, so an enrich-time write is wiped): extend the post-build glossary projection to **merge** the glossary/source terms with the active `llm/proposed` `semantic_terms` evidence (union + dedupe), write `graph_node.semantic_terms`, and `rebuild_search_doc(...)`. Never overwrite source/human terms.
  - No `_POLICIES` entry and no `resolve_and_project` is needed — `semantic_terms` is a merged bag of terms, not a single-value resolved field; the projection is a union of active evidence + glossary terms.
- [ ] **Step 4: Run → pass** (all four tests, incl. the full-ingest survival + feature-gen reach).
- [ ] **Step 5: Commit** — `git commit -m "feat(e1a): LLM synonyms as first-class llm/proposed semantic evidence, merged post-build, available to feature-gen"`

*Follow-on (not blocking approval):* surfacing the matched-term + producer in the *selection output* (so a feature shows "matched on 'available funds' · AI-proposed") is a richer traceability enhancement — the provenance is already stored on the evidence row; wiring it into the relevance/selection result is a later add.

---

### Task 5: Non-glossary / technical-upload concept coverage — DEFERRED (needs the complete call-path change; decision required)

**Why the earlier version was wrong (locked front door).** Relaxing the `rec is None: continue` gate *inside* `_write_concept_evidence` is insufficient: the evidence-write path is gated OFF for technical uploads a level up — `enrich_concepts` writes concept evidence only when `glossary is not None and source_snapshot_id is not None` (`enrich.py:501`), and the ingest call site passes neither for a technical-only upload, so the writer is never reached. The cascade (`_write_glossary_taxonomy_evidence`) is glossary-path-gated too, so additivity/temporal_role/sensitivity_floor/leakage_anchor also wouldn't derive for technical columns.

**The complete change (only if non-glossary uploads are in scope):**
1. Create/pass a **source snapshot** for technical uploads (today none is passed).
2. Construct the schema-preserving **evidence ref** + a **binding** for technical columns.
3. **Remove the glossary-only condition** around concept-evidence writing.
4. Invoke the **taxonomy cascade** for technical columns too.
5. **Update the existing test** that deliberately asserts a technical-only upload writes **zero** evidence (that is a current, intentional invariant — flipping it is part of the change).

**DEFERRED — confirmed safe by the verification pass + the user's scope decision.** The first release ingests only the sample CSV `tests/featuregen/overlay/upload/fixtures/ftr_sample_synthetic.csv`, which is a **glossary** upload (routed via `is_ftr_glossary`→`read_ftr_glossary`); every resolvable column becomes a `GlossaryRecord` and gets concept/definition/domain evidence via the glossary path (Tasks 1–4, 6). So non-glossary coverage is genuinely out of scope. Three existing tests pin the "technical-upload writes zero evidence" invariant that keeping Task 5 out **preserves**: `test_glossary_reupload.py:178`, `test_pass_a_evidence.py:139`, `test_technical_source_evidence.py:147`. **If technical-only uploads later become a requirement, this task is rewritten as the complete call-path change above** (snapshot + bindings + cascade + flipping those tests) — not a gate relaxation. (No code in E1a; documented deferral, so Task 6 keeps its number.)

---

### Task 6: Transitive-provenance cascade (the safety fix)

**Files:** `overlay/upload/ingest.py` (`_write_glossary_taxonomy_evidence:1181-1207`), `overlay/upload/taxonomy_evidence.py` (no signature change — already takes `concept_strength`), `overlay/upload/field_correction.py` (wire the recompute). Test: new.

**Two defects to fix (both are the laundering the design forbids):**

1. **Value AND strength must come from ONE resolved record — never mixed.** Today the value is `concepts.get(content_hash(row))` (the *current LLM* result) and the strength was to be read from a *separate* "latest active" row. If a human confirmed `customer_id` but the latest LLM proposed `account_identifier`, that mix derives from `account_identifier` (LLM value) at `confirmed` (human strength) → a `taxonomy/confirmed` safety fact from an unconfirmed value. **Fix:** resolve the concept field to its **selected evidence record** via the canonical resolver (the same `field_authority._select`/projection resolution that sets `graph_node.concept`, PREFER_CONFIRMED) and derive from **that record's value AND strength together** — a matched `(value, producer, strength, evidence_id)`. Do NOT read the value from the `concepts` dict. ("Latest active" ≠ the selected value — there may be multiple producers, conflicts, stale/rejected rows.)
2. **A concept-decision change must regenerate the dependent taxonomy — as a first-class projection, in the same transaction.** Today `field_correction` re-resolves only the *corrected field* (`resolve_and_project(... [field])`, `field_correction.py:409,451`) and the cascade only runs during glossary ingest, so a correction leaves derived additivity/leakage stale until (maybe) a later re-upload. A single call after one path is NOT enough. **Fix:** register the concept→taxonomy cascade as a **projection triggered whenever the concept's resolved value/strength changes** — hook it into `resolve_and_project` for the `concept` field so it fires uniformly on **confirm, override, reject, staleness, AND ingest** (ingest also calls it directly), all in the same transaction. `derive_and_write_concept_cascade(conn, logical_ref)` is the shared body; the projection hook is what makes it cover every path, not just confirm-override.

3. **Persist explicit root links (traceability) — cheap, no migration.** `material` only feeds the input hash; it is not stored, so the taxonomy row would have empty `evidence_spans` and nothing could trace it to its parent. **Fix (using existing columns, no schema change):** on each derived row set `evidence_spans = [winner.evidence_id]` (the root concept evidence) and `producer_item_ref =` the concept decision id if one exists (concept is RECOMMENDATION-tier and may have only an evidence id — then use `winner.evidence_id`), and include `value + producer + strength + evidence/decision id + resolver/policy version` in the **input hash**. This makes the chain queryable ("which concept evidence produced this taxonomy", "LLM or human", "stale because parent changed") and distinguishes two same-valued concepts of different origin — which value+strength alone cannot. (A dedicated `parent_evidence_id` FK column is the only part left as later hardening; the functional chain ships here.)

- [ ] **Step 1: Failing tests**

```python
def test_cascade_derives_from_resolved_ai_concept_as_proposed(overlay_conn):
    # AI-proposed concept -> derived additivity is taxonomy/PROPOSED (does NOT clear behavioural)
    ...  # write llm/proposed concept, run derive_and_write_concept_cascade(overlay_conn, REF)
    add = read_active_field_evidence(overlay_conn, REF, "additivity")
    assert add[0].producer == "taxonomy" and add[0].strength == "proposed"

def test_cascade_never_launders_llm_value_into_confirmed(overlay_conn, human_actor):
    # human confirms concept "customer_id"; a later LLM proposes a DIFFERENT concept "account_identifier".
    # The cascade MUST derive from the resolved human value+strength, never the LLM value @ confirmed.
    ...  # confirm human concept="customer_id"; then write llm/proposed concept="account_identifier"
    derive_and_write_concept_cascade(overlay_conn, REF)
    add = read_active_field_evidence(overlay_conn, REF, "additivity")
    assert add[0].strength == "confirmed"
    assert add[0].proposed_value == _expected_additivity_for("customer_id")   # derived from customer_id, NOT account_identifier

def test_human_correction_regenerates_taxonomy_immediately(overlay_conn, human_actor):
    # correcting the concept recomputes dependent taxonomy in the SAME flow (not stale until re-ingest)
    ...  # seed llm concept "account_balance"; apply_field_correction(field="concept",
         #   action="confirm_override", replacement_value="available_balance", actor=human_actor, ...)
    add = read_active_field_evidence(overlay_conn, REF, "additivity")
    assert add[0].strength == "confirmed"
    assert add[0].proposed_value == _expected_additivity_for("available_balance")  # NOT the prior "account_balance"
```

- [ ] **Step 2: Run → fail** (today both are `proposed`).

- [ ] **Step 3: Implement** — one cascade body that derives value **and** strength from the *resolved* concept record AND persists the root links:
```python
def derive_and_write_concept_cascade(conn, logical_ref, *, producer_ref, snapshot_id) -> None:
    winner = resolve_concept_evidence(conn, logical_ref)   # canonical _select (PREFER_CONFIRMED) -> winning record or None
    present: set[str] = set()
    if winner is not None:
        root_id = winner.evidence_id                          # anchor on the concept EVIDENCE id (decision is non-load-bearing for LLM)
        for field_name, value, strength in derive_concept_evidence(
                winner.proposed_value, AssertionStrength(winner.strength)):    # VALUE + STRENGTH from ONE record
            present.add(field_name)
            _write_producer_field(                                            # NOTE: extend _write_producer_field to accept+forward these
                conn, logical_ref=logical_ref, field_name=field_name, value=value,
                producer=EvidenceProducer.TAXONOMY, strength=strength, producer_ref=producer_ref,
                snapshot_id=snapshot_id,
                producer_item_ref=root_id,                   # STORED, queryable: root concept evidence id
                evidence_spans=[root_id],                    # STORED, queryable: root concept evidence id
                material={"root_value": winner.proposed_value, "root_producer": winner.producer,
                          "root_strength": winner.strength, "root_evidence_id": root_id,
                          "resolver_version": RESOLVER_VERSION})   # input hash covers value+producer+strength+id+version
    _stale_absent_fields(conn, logical_ref=logical_ref, producer=EvidenceProducer.TAXONOMY,
                         all_fields=_TAXONOMY_FIELDS, present=present)
```
Wiring (**verified against the code**): (a) `_write_glossary_taxonomy_evidence` (`ingest.py:1181`) calls this instead of the inline `concepts.get(...)` value + hard-coded `PROPOSED` — the ingest cascade already runs upstream of `resolve_and_project`. (b) **On human correction, wire the recompute into the `field=="concept"` branch of `field_correction._confirm_override`/`_confirm_existing`/`_reject`** (`field_correction.py:410,451`). *Correction from the verification pass:* `resolve_and_project` (`field_resolution.py:331`) has a per-field seam but **no taxonomy-cascade hook**, and those correction paths re-resolve only the corrected field — so taxonomy stays stale. `concept` is already in `_SHORTLIST_INPUT_FIELDS` (`field_correction.py:91`) (the command already treats it as cascade-bearing), so the concept branch is the natural insertion point: after `resolve_and_project(..., fields=["concept"])`, call `derive_and_write_concept_cascade(...)` in the same tx. (c) **Extend `_write_producer_field` (`ingest.py:954`) to ADD + forward `producer_item_ref` and `evidence_spans`** — verified it does **not** forward them today; `record_field_evidence` already accepts both (`evidence_spans` = list of ids). (d) Anchor the root link on the concept's **`evidence_id`** — an LLM-only concept's decision is non-load-bearing (`load_bearing_value_hash=None`), so the evidence_id (what `derive_concept_evidence` consumes) is the right parent, not the decision_id. `resolve_concept_evidence` returns the winning record's value+producer+strength+evidence_id as one matched tuple. `RESOLVER_VERSION` bumps when the resolver/policy changes so stale derivations re-compute.

- [ ] **Step 4: Run → pass**, and the behavioural-clears suite stays green (a human/governed concept still clears via `taxonomy/confirmed`, now carrying the resolved value, not the LLM value).

- [ ] **Step 5: Commit** — `git commit -m "fix(e1a): cascade derives value+strength from one resolved concept record + recomputes on correction (no laundering)"`

---

## Self-Review (author checklist — completed)

- **Feature-first:** Task 2 is the first end-to-end working feature (upload → AI definition → governed evidence → "AI proposed"); the writer (Task 1) is its only prerequisite. No NFR/scaffolding precedes the feature.
- **NFRs deferred:** no measurement harness, dashboard, governance UI, ops, or compliance in any task — per the steer.
- **Byte-identity guard:** `_write_concept_evidence` is NOT touched (keeps its reuse/cache); `test_pass_a_evidence.py` stays green trivially. The new simple writer is for the new fields only.
- **Safety:** Task 6 is the transitive-provenance fix — an AI concept's cascade stays `taxonomy/proposed` and cannot silently clear the behavioural rule; only a human/confirmed concept yields `taxonomy/confirmed`.
- **No policy/schema/frontend churn for definition/domain:** confirmed `_MEANING` already admits `llm/proposed` and asset-detail already labels it; only the writer + call sites change.
- **Type consistency:** `_write_llm_field_evidence(field_name, items, ref_of, source_snapshot_id, valid_fn, producer_configuration_hash, bindings)` (no reuse/cache params) used identically by the new fields (definition/domain/synonyms).

## Follow-on (deferred, not this plan)
The feature-gen-lift **measurement** (baseline + before/after over `ground_all`/`_validate_idea`), the coverage dashboard, the review/governance surface, bulk-by-convention correction, the operational/cold-start model, compliance, and per-field confidence (P2) — all deferred as NFRs, to be planned after this functional slice is built and tested.
