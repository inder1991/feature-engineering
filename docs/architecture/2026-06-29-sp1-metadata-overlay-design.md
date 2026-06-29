# SP-1 — Metadata Overlay: Design Spec

**Status:** Design (sub-project spec)
**Date:** 2026-06-29
**Sub-project:** SP-1 (Phase A — Foundations)
**Parent:** [Reference architecture §6](./2026-06-27-feature-engineering-platform-design.md) · [Roadmap](./2026-06-27-feature-engineering-platform-roadmap.md) · builds on [SP-0](./2026-06-27-sp0-foundations-design.md)
**Type:** Vendor-neutral design + a clearly-marked sample-stack appendix

> Implements the **Metadata Overlay** (architecture §6): a platform-owned layer that augments the
> existing enterprise catalog with the **ML-specific facts it does not record** (availability time,
> grain, SCD effective-dating, approved joins, use-case-scoped policy), each **human-confirmed,
> versioned, and freshness-managed**. SP-1 **builds on SP-0** — it reuses the event store, human-gate
> task model, identity/authz + structural SoD, durable timers, inbound signals, and audit.

---

## 1. Purpose and scope

### 1.1 In scope
- The **overlay fact store** (event-sourced on SP-0).
- The **`CatalogAdapter`** (interface + Postgres `information_schema` reference adapter + fixture).
- The **merged-view read API** (authoritative-catalog → VERIFIED-overlay → missing; fail-closed).
- The **confirmation workflow** + **authority resolver** (data owner / Compliance, via SP-0 gates + SoD).
- The **deterministic data-profiling proposer** (emits DRAFT facts + evidence).
- **Freshness/expiry** + a **minimal catalog-change detector** (fingerprint diff → STALE).

### 1.2 Out of scope (deferred)
- **LLM-suggestion + lazy "during a feature build" proposing** → SP-2 (intake provides the trigger).
- The visual **confirmation console UI** → the frontend sub-project (SP-1 ships the API).
- **Non-Postgres catalog adapters** (Snowflake/Unity/etc.) → just another `CatalogAdapter` later.
- The **richer Change-Impact Analyzer** (dependent-feature impact, revalidation orchestration) → SP-10.

### 1.3 Design decisions (ledger)
| Decision | Choice |
|---|---|
| Output | Vendor-neutral spec + sample-stack appendix (Python + PostgreSQL) |
| Foundation | **Build on SP-0** (events, human-gate, identity/SoD, timers, inbound signals, audit) |
| Event model | **Extend SP-0 with an `overlay_fact` aggregate**; `aggregate_id = fact_key` |
| Fact identity | normalized **`fact_key`** hash + human-readable `object_ref` |
| Lifecycle | DRAFT→VERIFIED→REVERIFY→VERIFIED; DRAFT→REJECTED; VERIFIED→STALE→VERIFIED |
| Catalog precedence | **authoritative** catalog fact → VERIFIED overlay → missing (fail-closed) |
| Profiler | proposes **DRAFT + evidence** (deterministic); never asserts truth |
| Authority | data owner → data facts; Compliance → policy facts; unknown owner → governance queue |

---

## 2. Foundation reuse (build on SP-0)

SP-1 is a thin domain layer over SP-0. It reuses:

| SP-0 primitive | SP-1 use |
|---|---|
| Event store (append, OCC, `global_seq`, provenance) | the audited history of each overlay fact |
| **Aggregate vocabulary** | **one additive change: register `overlay_fact`** as an aggregate type |
| Human-gate task model (`gates/`) | data-owner / Compliance confirmation tasks |
| Identity + authz + **structural SoD** | who may confirm which fact type |
| Durable timers | expiry → REVERIFY |
| Inbound signals / change-impact hook | catalog-change → STALE |
| Security-audit stream | denied/unauthorized confirmation attempts |
| Projections (checkpointed, fail-closed) | the current-state overlay read model |

**The only change to SP-0** is registering the `overlay_fact` aggregate in the event-store aggregate set and its state-machine table; everything else is new code under `src/featuregen/overlay/`.

---

## 3. Data model

### 3.1 Fact identity (`fact_key` + `object_ref`)
A fact is keyed by a **stable normalized hash**, not a dotted string (which is fragile to case/quoting/multi-part names):

```
fact_key = sha256_hex(normalize(
    catalog_source,   # e.g. "pg:core"
    object_kind,      # "table" | "column" | "relation"
    schema, table, column?, relation?,
    fact_type,        # see §3.3
    use_case?         # only for policy facts (use-case-scoped)
))
object_ref = "core.transactions.posted_at"   # human-readable, carried alongside (display/audit)
```
`fact_key` drives OCC, the projection key, and confirmation routing; `object_ref` is for humans.

### 3.2 The `overlay_fact` aggregate (on SP-0's event store)
Each fact is an event-sourced aggregate (`aggregate = "overlay_fact"`, `aggregate_id = fact_key`). Events:

```
FACT_PROPOSED   (status DRAFT)     payload: proposed_value, evidence_ref, provenance, proposed_by(service|human)
FACT_CONFIRMED  (status VERIFIED)  payload: value, confirmed_by, authority_role, expires_at
FACT_REJECTED   (status REJECTED)  payload: rejected_by, reason
FACT_EXPIRED    (status REVERIFY)  payload: by timer
FACT_STALED     (status STALE)     payload: catalog_change_ref
```

### 3.3 Fact types (§6.3)
`availability_time` · `grain` · `scd_effective_dating` · `approved_join` · `policy_tag` (use-case-scoped). The first four are **data facts** (data-owner authority); `policy_tag` is a **policy fact** (Compliance authority).

### 3.4 Lifecycle (with REJECTED)
```
DRAFT ── confirm ──▶ VERIFIED ── expiry ──▶ REVERIFY ── confirm ──▶ VERIFIED
  │                      │
  └── reject ─▶ REJECTED └── catalog change ─▶ STALE ── confirm ─▶ VERIFIED
```
**REJECTED is sticky for the proposed value:** the profiler dedups against REJECTED (and in-flight DRAFT) so a weak candidate is **not re-proposed / re-tasked**. A genuinely new proposal (different value/evidence) is a new DRAFT.

### 3.5 Evidence vs. fact (separation)
Profiler output is **evidence**, not truth. A `FACT_PROPOSED` carries:
- the **proposed_value** (the business assertion the human will confirm/edit/reject), and
- an **`evidence_ref`** → an evidence record (metrics + provenance, §5).

Only a **human-confirmed value** becomes a VERIFIED fact. Evidence informs the decision; it is never served as a fact.

### 3.6 Projection (the read model)
Per `fact_key`: `status, value, source, confirmed_by, authority_role, confirmed_at, expires_at, evidence_ref, provenance, object_ref, fact_type, use_case`. Rebuildable from the event stream (SP-0 fail-closed projection rules apply).

---

## 4. Catalog adapter

```python
class CatalogAdapter(Protocol):
    def list_objects(self) -> Iterable[CatalogObject]: ...            # tables/columns/types
    def get_fact(self, object_ref, fact_type) -> CatalogFact | None: ...
    def authoritative_fact_types(self) -> frozenset[str]: ...          # which it can be trusted for
    def owner_of(self, object_ref) -> Principal | None: ...            # ownership if recorded
    def fingerprint(self) -> CatalogFingerprint: ...                   # for change detection (§6)
```

- **`InformationSchemaCatalog`** (reference): reads real table/column/type/existence from Postgres `information_schema`. It is **authoritative for `existence`, `column`, `type`** — and explicitly **not** for `grain`, `availability_time`, `scd_effective_dating`, `approved_join`, `policy_tag` (those come from the overlay). `owner_of` returns None unless ownership is recorded.
- **`FixtureCatalog`**: in-memory adapter for tests.

Vendor-neutrality: a Snowflake / Unity Catalog adapter is just another implementation that declares its own `authoritative_fact_types()` and `owner_of`.

---

## 5. Data-profiling proposer (deterministic)

Inspects the actual data (read-only) and emits **DRAFT facts + evidence** for human confirmation:
- **grain** — uniqueness ratio of candidate key sets (e.g. `customer_id` 99.9% unique → propose grain).
- **availability_time** — candidate timestamp columns (by name/type/monotonicity vs an event-time column).
- **scd_effective_dating** — candidate `valid_from/valid_to` pairs.
- evidence-only metrics: null-rate, distinct count, sample size.

**Provenance contract** (on every proposal — for audit and to explain *why* proposed):
```
{ table_snapshot_at, row_count, sample_size, profile_version, thresholds_used, metric_values }
```
**No raw values** are stored (PII rule, §9 of SP-0). The profiler dedups against REJECTED/DRAFT (§3.4) before proposing.

---

## 6. Confirmation workflow + authority resolver

1. A DRAFT fact opens an SP-0 **human-gate task** routed to the **resolved authority**:
   - **Data facts** → the **data owner**, resolved via `CatalogAdapter.owner_of(object_ref)`. **If the owner is unknown → route to a data-governance / platform-admin queue** (never to whoever submitted the request).
   - **Policy facts (`policy_tag`)** → **Compliance**, *always* — a data owner attempting to confirm a policy fact is rejected by SP-0's structural SoD.
2. On **confirm** → `FACT_CONFIRMED` (VERIFIED, with `expires_at`); on **reject** → `FACT_REJECTED`.
3. Unauthorized attempts are denied and recorded in the **security-audit stream** (SP-0).

## 7. Merged-view read API

```python
def resolve_fact(object_ref, fact_type, use_case=None) -> ResolvedFact
```
Precedence: **authoritative catalog fact → VERIFIED overlay fact → missing** (catalog wins *only* for fact types the adapter declares authoritative, §4). STALE/REVERIFY/DRAFT/REJECTED overlay entries are **not** served as VERIFIED. Return shape:
```
ResolvedFact{ value, status, source('catalog'|'overlay'|'missing'),
              catalog_object, fact_type, use_case, provenance,
              confirmed_by, confirmed_at, expires_at, reason_if_missing }
```
**Fail-closed:** `status == 'missing'` (no authoritative catalog fact and no VERIFIED overlay fact) → the caller (grounding, Layer 3) blocks and routes to confirmation (the §6.2 hard floor).

## 8. Freshness + catalog-change detection

- **Expiry:** on `FACT_CONFIRMED`, schedule an SP-0 **timer** for `expires_at` (default horizon, configurable); firing emits `FACT_EXPIRED` → REVERIFY.
- **Catalog change:** a minimal detector takes **`CatalogAdapter.fingerprint()`** snapshots and diffs them (table/column added/dropped/renamed, type changed). A change affecting an object emits an inbound signal → `FACT_STALED` for that object's facts, and notifies owners. (The richer dependent-feature Change-Impact Analyzer is SP-10.)

## 9. Error handling & concurrency

- Fail-closed merged-view (§7); wrong-role confirmation rejected (SP-0 SoD); STALE/expired never served as VERIFIED; profiler output is DRAFT-only.
- **OCC on `fact_key`** (SP-0): concurrent writers to one fact serialize; duplicates are no-ops.
- Idempotent proposals (same proposed_value+evidence on an existing DRAFT → no-op) and confirmations (idempotent by `(fact_key, task)`).

## 10. Interfaces SP-1 exposes (for SP-2+ consumers)

- `resolve_fact(object_ref, fact_type, use_case?) -> ResolvedFact` — the read path Layer 3 grounding uses.
- Commands: `propose_fact`, `confirm_fact`, `reject_fact` (the last two via the human-gate), `run_profiler(object)`.
- The `CatalogAdapter` protocol (so deployments bind their real catalog).

## 11. What SP-1 deliberately does NOT do
LLM-suggestion / lazy-during-build proposing (SP-2) · console UI (frontend) · non-Postgres catalog adapters (later) · richer Change-Impact Analyzer / revalidation orchestration (SP-10).

## 12. Testing
- **Merged-view:** authoritative-catalog hit · overlay-fill · fail-closed-on-missing · not-served-when-STALE/REVERIFY · authoritative-precedence (catalog beats overlay only where declared).
- **Authority:** data-owner confirms data fact · Compliance confirms policy fact · wrong-role rejected · **unknown owner → governance queue** (not submitter).
- **Lifecycle:** DRAFT→VERIFIED→EXPIRED→REVERIFY→VERIFIED · DRAFT→REJECTED · VERIFIED→STALE→VERIFIED · profiler **does not re-propose a REJECTED candidate**.
- **Profiler:** grain/availability-time inference correctness; evidence + provenance present; **no raw values**.
- **Freshness/change:** timer expiry → REVERIFY; fingerprint diff (drop/rename/type-change) → STALE.
- **Concurrency/idempotency (§9):** duplicate proposal · double confirmation · **stale confirmation after a newer draft** · concurrent confirm/reject.
- **Adapter:** `InformationSchemaCatalog` against real PG; `authoritative_fact_types()` honored.

## Appendix A — Sample stack (non-binding)
PostgreSQL (overlay events reuse SP-0's event store + a projection table; `information_schema` as the reference catalog) · Python 3.11 · pytest. The overlay adds its projection table via the canonical migrations module; the `overlay_fact` aggregate is registered with SP-0's event store and state-machine table.
