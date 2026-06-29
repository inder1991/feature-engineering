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
- The **overlay fact store** (event-sourced on SP-0) + the **immutable evidence store**.
- The **`CatalogAdapter`** (interface + Postgres `information_schema` reference adapter + fixture).
- The **merged-view read API** (authoritative-catalog → VERIFIED-overlay → missing; fail-closed).
- The **confirmation workflow** + **authority resolver** + **proactive (human-entered) facts**.
- The **deterministic data-profiling proposer** (emits DRAFT facts + evidence; bounded/safe).
- **Freshness/expiry** + a **minimal catalog-change detector** (fingerprint diff → STALE).

### 1.2 Out of scope (deferred)
- **LLM-suggestion + lazy "during a feature build" proposing** → SP-2 (intake provides the trigger).
- The visual **confirmation console UI** → the frontend sub-project (SP-1 ships the API).
- **Non-Postgres catalog adapters** (Snowflake/Unity/etc.) → just another `CatalogAdapter` later.
- The **richer Change-Impact Analyzer** (dependent-feature impact, revalidation orchestration) → SP-10.
- **Human-facing read authorization** on `resolve_fact` → SP-9/SP-11 (SP-1's read path is service-internal, §7.2).

### 1.3 Design decisions (ledger)
| Decision | Choice |
|---|---|
| Output | Vendor-neutral spec + sample-stack appendix (Python + PostgreSQL) |
| Foundation | **Build on SP-0** (events, human-gate, identity/SoD, timers, inbound signals, audit) |
| Event model | **`overlay_fact` aggregate on SP-0's event store**, via a migration that extends both event CHECKs (§2.1) — *not* a registration-only change |
| Fact identity | normalized **`fact_key`** hash + human-readable `object_ref` |
| Lifecycle | DRAFT→VERIFIED→REVERIFY→VERIFIED; DRAFT→REJECTED; VERIFIED→STALE→VERIFIED; **proactive direct entry** by the authority (§3.4) |
| Status enum | canonical **persisted** value `REVERIFY`; **display** label `RE-VERIFY` (= parent §6 prose) |
| Catalog precedence | **authoritative** catalog fact → VERIFIED overlay → missing (fail-closed); authority is **per-fact** (§4) |
| Profiler | proposes **DRAFT + evidence** (deterministic, bounded); never asserts truth |
| Authority | data owner → data facts; Compliance → policy facts; unknown owner → governance queue |

---

## 2. Foundation reuse (build on SP-0)

SP-1 is a thin domain layer over SP-0. It reuses: the event store (append, OCC, `global_seq`,
provenance), the human-gate task model (`gates/`), identity + authz + **structural SoD**, durable
timers (expiry), the inbound-signal/change-impact hook (catalog-change → STALE), the security-audit
stream, and checkpointed fail-closed projections (the overlay read model).

### 2.1 Required change to SP-0's event store (P0)
SP-0's `events` table hard-constrains the aggregate set and the id-consistency invariant:
```sql
aggregate text NOT NULL CHECK (aggregate IN ('request','feature','run'))
CONSTRAINT events_aggregate_id_consistent CHECK (
    (aggregate='request' AND aggregate_id=request_id) OR
    (aggregate='feature' AND aggregate_id=feature_id) OR
    (aggregate='run'     AND aggregate_id=run_id))
```
`overlay_fact` has no typed mirror column, so it would violate **both** CHECKs. SP-1 therefore ships a
**migration** that:
1. **Recreates the `aggregate` CHECK** to include `'overlay_fact'`.
2. **Recreates `events_aggregate_id_consistent`** adding a branch `OR (aggregate = 'overlay_fact')` —
   for overlay facts, **`aggregate_id = fact_key` is the canonical key** and `request_id/feature_id/run_id`
   stay `NULL` (the existing partial indexes already filter `… IS NOT NULL`, so they're unaffected; OCC
   via `UNIQUE(aggregate, aggregate_id, stream_version)` works unchanged).
3. **Registers `overlay_fact`** in the state-machine table (the lifecycle, §3.4).
4. Creates the overlay **projection** and **evidence** tables (§3.6, §5.1).

The **append/envelope layer** is updated to accept `aggregate = "overlay_fact"` with the typed mirror
columns `NULL`. This is an additive, backward-compatible migration (no existing rows change).

---

## 3. Data model

### 3.1 Fact identity (`fact_key` + `object_ref`)
A fact is keyed by a **stable normalized hash**, not a dotted string (fragile to case/quoting/multi-part names):
```
fact_key = sha256_hex(normalize(
    catalog_source,   # e.g. "pg:core"
    object_kind,      # "table" | "column" | "relation"
    schema, table, column?, relation?,
    fact_type,        # see §3.3
    use_case?))       # only for policy facts (use-case-scoped)
object_ref = "core.transactions.posted_at"   # human-readable, carried alongside (display/audit)
```
`fact_key` drives OCC, the projection key, and confirmation routing; `object_ref` is for humans.

### 3.2 The `overlay_fact` aggregate (on SP-0's event store)
Events (`aggregate="overlay_fact"`, `aggregate_id=fact_key`):
```
FACT_PROPOSED   (DRAFT)     proposed_value, evidence_ref?, provenance, proposed_by(service|human)
FACT_CONFIRMED  (VERIFIED)  value, confirmed_by, authority_role, expires_at, confirms_event_id
FACT_REJECTED   (REJECTED)  rejected_by, reason, rejects_event_id
FACT_EXPIRED    (REVERIFY)  by timer
FACT_STALED     (STALE)     catalog_change_ref
```

### 3.3 Fact types and value schemas (P1)
Four **data facts** (data-owner authority) and one **policy fact** (`policy_tag`, Compliance authority).
Every `proposed_value`/`value` is validated against a per-type schema at append time:

| fact_type | authority | value schema |
|---|---|---|
| `availability_time` | data owner | `{ column: str, basis: "posted_at"\|"ingested_at"\|"event_time_plus_lag", lag_hours?: number }` |
| `grain` | data owner | `{ columns: [str, …], is_unique: bool }` |
| `scd_effective_dating` | data owner | `{ valid_from: str, valid_to: str\|null, current_flag?: str }` |
| `approved_join` | data owner | `{ from_columns: [str,…], to_object: str, to_columns: [str,…], cardinality: "1:1"\|"1:N"\|"N:1" }` |
| `policy_tag` | Compliance | `{ use_case: str, decision: "allow"\|"deny"\|"restricted", sensitivity?: str, basis: str }` |

Schemas are registered with SP-0's schema registry (versioned) so event validation, the projection
shape, merged-view consumers, and confirmation editing all share one definition.

### 3.4 Lifecycle (with REJECTED and proactive entry)
```
                       ┌─ reject ─▶ REJECTED
DRAFT ─ confirm ─▶ VERIFIED ─ expiry ─▶ REVERIFY ─ confirm ─▶ VERIFIED
                       └─ catalog change ─▶ STALE ─ confirm ─▶ VERIFIED
```
- **Canonical status values:** `DRAFT, VERIFIED, REJECTED, STALE, REVERIFY` (display `REVERIFY` as `RE-VERIFY`).
- **REJECTED is sticky for the proposed value:** the profiler dedups against REJECTED (and in-flight DRAFT) so a weak candidate is **not re-proposed / re-tasked**. A genuinely different proposal (new value/evidence) is a new DRAFT.
- **Proactive (human-entered) facts (P1):** not everything is profiler-inferable, and §6 of the parent allows proactive verified entry. Two supported paths:
  - **Propose-then-confirm:** a human submits `FACT_PROPOSED(proposed_by=human)`, then the resolved authority confirms (normal gate).
  - **Direct entry:** when the **acting principal *is* the resolved authority** for that fact (data owner for a data fact; Compliance for a policy fact), the command may emit `FACT_PROPOSED`+`FACT_CONFIRMED` atomically (a self-confirmation that still records actor + provenance and respects SoD — a data owner can never self-confirm a `policy_tag`).

### 3.5 Evidence vs. fact (separation)
Profiler output is **evidence**, not truth. `FACT_PROPOSED` carries the **proposed_value** (the business
assertion to confirm/edit/reject) and an **`evidence_ref`** → an immutable evidence record (§5.1). Only a
**human-confirmed value** becomes a VERIFIED fact; evidence informs the decision and is never served as a fact.

### 3.6 Projection (the read model)
Per `fact_key`: `status, value, source, confirmed_by, authority_role, confirmed_at, expires_at,
evidence_ref, provenance, object_ref, fact_type, use_case`. Rebuildable from the event stream (SP-0
fail-closed projection rules apply).

---

## 4. Catalog adapter (P1 — per-fact authority)

```python
class CatalogAdapter(Protocol):
    def list_objects(self) -> Iterable[CatalogObject]: ...                    # tables/columns/types
    def get_fact(self, object_ref, fact_type, use_case=None) -> CatalogFact | None: ...
    def owner_of(self, object_ref) -> Principal | None: ...                   # ownership if recorded
    def fingerprint(self) -> CatalogFingerprint: ...                          # for change detection (§8)

@dataclass(frozen=True)
class CatalogFact:
    value: object
    authoritative: bool        # is the catalog the source of truth for THIS object/fact/use_case?
```
Authoritativeness is a **property of each returned `CatalogFact`**, not a global set — a catalog may be
authoritative for PII tags on some columns but not others, or for type on a column but not its grain.

- **`InformationSchemaCatalog`** (reference): reads real table/column/type/existence from Postgres
  `information_schema`. Returns `authoritative=True` only for `existence`/`column`/`type`; for
  `grain`/`availability_time`/`scd_effective_dating`/`approved_join`/`policy_tag` it returns `None`
  (the overlay owns those). `owner_of` returns `None` unless ownership is recorded.
- **`FixtureCatalog`**: in-memory adapter for tests (can mark facts authoritative per object).

A Snowflake / Unity Catalog adapter is just another implementation with its own per-fact authority and `owner_of`.

---

## 5. Data-profiling proposer (deterministic, bounded)

Inspects the actual data (read-only) and emits **DRAFT facts + evidence** for human confirmation:
- **grain** — uniqueness ratio of candidate key sets; **availability_time** — candidate timestamp columns
  (name/type/monotonicity vs an event-time column); **scd_effective_dating** — candidate `valid_from/valid_to`.
- evidence-only metrics: null-rate, distinct count, sample size.

It dedups against REJECTED/DRAFT (§3.4) before proposing.

### 5.1 Evidence store (P1)
Evidence is an **immutable record** in an `overlay_evidence` table (an SP-0-style append-only artifact,
written once, never updated; referenced by `evidence_ref`). Schema:
```
{ evidence_id, fact_key, table_snapshot_at, row_count, sample_size, profile_version,
  thresholds_used, metric_values, created_by, created_at }
```
**No raw values** are stored (SP-0 privacy rule) — only aggregate metrics. **Classification:** internal
metadata, **non-PII**, governance-retained (retained with the fact's audit history, not crypto-shred class).

### 5.2 Profiler safety limits (P1)
The profiler runs only under bounded, auditable access:
- **read-only DB role**; **schema allowlist** (only onboarded schemas);
- **aggregate-only queries** (COUNT/DISTINCT/MIN/MAX over columns) — never row exports;
- **sampling policy** (TABLESAMPLE / `LIMIT` above a configurable row-count threshold; full scan only below it);
- **statement timeout**, **max columns**, and **max column-combinations** per run (cap combinatorial key-set search);
- every run records its provenance (§5.1) so a proposal can be explained and reproduced.

---

## 6. Confirmation workflow + authority resolver (P1)

1. A DRAFT opens an SP-0 **human-gate task** routed to the **resolved authority**:
   - **Data facts** → the **data owner**, resolved via `CatalogAdapter.owner_of(object_ref)`. **If the owner is
     unknown → route to a data-governance / platform-admin queue** (never to whoever submitted the request).
   - **Policy facts (`policy_tag`)** → **Compliance**, *always*; a data owner attempting to confirm a policy fact
     is rejected by SP-0's structural SoD.
2. **Confirm** → `FACT_CONFIRMED` (VERIFIED, with `expires_at`); **reject** → `FACT_REJECTED`.
3. **Confirmation race semantics (P1):** a confirm/reject command carries the **`draft_event_id`** (or
   `expected_stream_version`) **and the gate `task_version`**. The command is **rejected** if the fact has
   advanced (a newer draft exists), expired, or been staled since the task was raised — i.e. a CAS on the
   stream (SP-0 OCC) plus task-version staleness. Stale confirmations never apply silently.
4. Unauthorized attempts are denied and recorded in the **security-audit stream** (SP-0).

## 7. Merged-view read API

### 7.1 Resolution
```python
def resolve_fact(object_ref, fact_type, use_case=None) -> ResolvedFact
```
Precedence: **authoritative catalog fact → VERIFIED overlay fact → missing** (catalog wins *only* when the
returned `CatalogFact.authoritative` is true, §4). STALE/REVERIFY/DRAFT/REJECTED overlay entries are **not**
served as VERIFIED. Return shape (P1):
```
ResolvedFact{ value, status, source('catalog'|'overlay'|'missing'),
              catalog_object, fact_type, use_case, provenance,
              confirmed_by, confirmed_at, expires_at, reason_if_missing }
```
**Fail-closed:** `status == 'missing'` → the caller (grounding, Layer 3) blocks and routes to confirmation
(the §6.2 hard floor).

### 7.2 Read authorization posture (P2)
`resolve_fact` can reveal policy tags, table existence, ownership, and metadata about sensitive columns.
In SP-1 the read path is **service-internal only** — called by platform components, not exposed to end
users — and **write/confirm authz is enforced now** (SP-0 authz + SoD on the commands). **Human-facing read
authorization is deferred to SP-9/SP-11**; this is stated explicitly so no consumer assumes the read path is
end-user-safe.

## 8. Freshness + catalog-change detection (P2 — stable identity)

- **Expiry:** on `FACT_CONFIRMED`, schedule an SP-0 **timer** for `expires_at` (configurable horizon); firing
  emits `FACT_EXPIRED` → REVERIFY.
- **Catalog change:** a minimal detector snapshots `CatalogAdapter.fingerprint()` and diffs it. Where the
  adapter exposes a **stable native object id** (e.g. Postgres `oid`), renames are tracked by id; **otherwise a
  rename is treated as drop+add** — the old `fact_key`'s facts go **STALE** and a new object appears for
  onboarding. Detected changes (add/drop/rename/type-change) emit an inbound signal → `FACT_STALED` for the
  affected object's facts, and owners are notified. (The richer dependent-feature Change-Impact Analyzer is SP-10.)

## 9. Error handling & concurrency

- Fail-closed merged-view (§7); wrong-role confirmation rejected (SP-0 SoD); STALE/expired never served as VERIFIED; profiler output is DRAFT-only.
- **OCC on `fact_key`** (SP-0): concurrent writers to one fact serialize.
- **Idempotent** proposals (same proposed_value+evidence on an existing DRAFT → no-op) and confirmations (idempotent by `(fact_key, draft_event_id)`); confirmation CAS per §6.3.

## 10. Interfaces SP-1 exposes (for SP-2+ consumers)
- `resolve_fact(object_ref, fact_type, use_case?) -> ResolvedFact` — the read path Layer 3 grounding uses (service-internal, §7.2).
- Commands: `propose_fact`, `confirm_fact`, `reject_fact`, `enter_fact` (proactive/direct, §3.4), `run_profiler(object)`.
- The `CatalogAdapter` protocol (deployments bind their real catalog).

## 11. What SP-1 deliberately does NOT do
LLM-suggestion / lazy-during-build proposing (SP-2) · console UI (frontend) · non-Postgres catalog adapters (later) · richer Change-Impact Analyzer / revalidation orchestration (SP-10) · human-facing read authz (SP-9/SP-11).

## 12. Testing
- **Migration (P0):** `overlay_fact` appends succeed after the CHECK migration; existing request/feature/run appends + consistency CHECK still hold; OCC on `fact_key`.
- **Value schemas (P1):** each fact type validates its value; malformed value rejected at append.
- **Merged-view:** authoritative-catalog hit · overlay-fill · fail-closed-on-missing · not-served-when-STALE/REVERIFY · **per-fact authority** (catalog beats overlay only where `authoritative=true`).
- **Authority:** data-owner confirms data fact · Compliance confirms policy fact · wrong-role rejected · **unknown owner → governance queue** (not submitter) · **direct entry by the authority** (self-confirm) · data owner **cannot** self-confirm a `policy_tag`.
- **Lifecycle:** DRAFT→VERIFIED→EXPIRED→REVERIFY→VERIFIED · DRAFT→REJECTED · VERIFIED→STALE→VERIFIED · profiler **does not re-propose a REJECTED candidate**.
- **Evidence (P1):** evidence record is immutable; `evidence_ref` resolves; **no raw values**; provenance present.
- **Profiler (P1):** grain/availability-time inference correctness; **safety limits honored** (read-only role, allowlist, aggregate-only, timeout, max columns/combinations, sampling threshold).
- **Confirmation race (P1):** stale confirmation after a newer draft is **rejected**; expired/staled confirmation rejected; concurrent confirm/reject.
- **Freshness/change (P2):** timer expiry → REVERIFY; fingerprint diff (drop/type-change) → STALE; **rename via stable id** tracked, **rename without id** = drop+add → STALE.
- **Read posture (P2):** `resolve_fact` is reachable service-internally; (human-facing authz is out of scope, asserted by test doc).
- **Adapter:** `InformationSchemaCatalog` against real PG; per-fact `authoritative` honored.

## Appendix A — Sample stack (non-binding)
PostgreSQL (overlay events reuse SP-0's event store + the §2.1 migration; a projection table + an
`overlay_evidence` table; `information_schema` as the reference catalog; Postgres `oid` as the stable
native object id for rename tracking) · Python 3.11 · pytest. The `overlay_fact` aggregate is registered
with SP-0's event store and state-machine table via the canonical migrations module.
