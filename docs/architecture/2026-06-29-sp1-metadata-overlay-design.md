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
- The **`CatalogAdapter`** (interface + `PostgresCatalog` reference adapter over `information_schema` + `pg_catalog` + fixture).
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
2. **Recreates `events_aggregate_id_consistent`** with an **explicit** overlay branch
   `OR (aggregate = 'overlay_fact' AND request_id IS NULL AND feature_id IS NULL AND run_id IS NULL)` —
   for overlay facts, **`aggregate_id = fact_key` is the canonical key** and the typed mirror columns are
   *constrained* to `NULL` (so they can't be accidentally populated; the existing partial indexes already
   filter `… IS NOT NULL`; OCC via `UNIQUE(aggregate, aggregate_id, stream_version)` works unchanged).
3. **Adds an `overlay_fact` lifecycle to SP-0's transition engine.** SP-0's `_TABLE_NAMES` is hardcoded to
   `{run, feature}` with no generic table (`state_machine/transition_table.py:12`); SP-1 extends it with a
   new **`overlay_lifecycle_table`** plus an `"overlay_fact": "overlay_lifecycle_table"` entry, so the
   lifecycle (§3.4) is declared as transitions and validated by the **same** install/load/guard engine as
   run/feature. (Alternatively the validation could be command-level only; we reuse the engine for parity.)
4. Creates the overlay **projection** and **evidence** tables (§3.6, §5.1).

The **append/envelope layer** is updated to accept `aggregate = "overlay_fact"` with the typed mirror
columns `NULL`. This is an additive, backward-compatible migration (no existing rows change).

### 2.2 Required change to SP-0's gate model (P1)
SP-0's human-gate is keyed to run/feature only: `GateTaskSpec` carries just `run_id`/`feature_id`
(`contracts/envelopes.py`), `human_tasks` stores only those with a gate CHECK of
`{CLARIFICATION, DATA_STEWARD, COMPLIANCE, INDEPENDENT_VALIDATION, FINAL_APPROVAL}`
(`0070_identity_authz_gates.sql`), and `_task_aggregate(None, None)` defaults to `("feature", None)`
(`gates/tasks.py`). SP-1 extends the gate model:
1. Add **`fact_key`, `draft_event_id`, `evidence_ref`** columns to `human_tasks` (nullable, like run/feature)
   and the same fields to `GateTaskSpec`.
2. Add overlay gates **`OVERLAY_DATA_OWNER`** and **`OVERLAY_COMPLIANCE`** to the gate CHECK enum.
3. Extend `_task_aggregate` so a task carrying a `fact_key` maps to `("overlay_fact", fact_key)`.
4. SoD/authz policy rows (§6.5) keyed by `fact_key` + `fact_type`.

### 2.3 Required change to SP-0's runtime (P1)
1. **Outbox:** `partition_key_for` raises on unknown aggregates (`runtime/outbox.py`); add an `overlay_fact`
   branch → `overlay:{fact_key}` for per-fact ordering.
2. **Timers:** the timer `kind` CHECK (`0502_timers.sql`) excludes overlay; add **`overlay_expiry`** plus a
   handler route that emits `FACT_EXPIRED` (CAS, §8).

All of §2.1–§2.3 are additive, backward-compatible migrations to SP-0 and are sequenced first in the plan.

---

## 3. Data model

### 3.1 Fact identity (`fact_key` + `object_ref`)
A fact is keyed by a **stable normalized hash**, not a dotted string (fragile to case/quoting/multi-part names):
```
fact_key = sha256_hex(normalize(
    catalog_source,   # e.g. "pg:core"
    object_kind,      # "table" | "column" | "relation"
    schema, table, column?,
    fact_type,        # see §3.3
    use_case?,        # policy facts (use-case-scoped)
    relation?))       # RELATION facts (approved_join): the normalized tuple
                      #   (from_ref, sorted from_columns, to_ref, sorted to_columns)
                      #   so multiple distinct joins on one table get distinct keys
object_ref = "core.transactions.posted_at"   # human-readable, carried alongside (display/audit)
```
`fact_key` drives OCC, the projection key, and confirmation routing; `object_ref` is for humans.

**APIs take a structured `CatalogObjectRef`** (`{catalog_source, object_kind, schema, table, column?, relation?}`),
never a raw dotted string — both the `fact_key` and the display `object_ref` are **derived** from it, so the
case/quoting/multi-catalog ambiguity the `fact_key` exists to avoid cannot re-enter through the API surface.

### 3.2 The `overlay_fact` aggregate (on SP-0's event store)
Events (`aggregate="overlay_fact"`, `aggregate_id=fact_key`):
```
FACT_PROPOSED   (DRAFT)     catalog_object_ref, object_ref, fact_type, use_case?, proposed_value,
                            proposal_fingerprint, evidence_ref?, provenance, proposed_by(service|human)
FACT_PARTIALLY_CONFIRMED (PARTIALLY_CONFIRMED)  by_owner, draft_event_id   # approved_join only: first of two owners
FACT_CONFIRMED  (VERIFIED)  value, confirmed_by, authority_role, expires_at, confirms_event_id
FACT_REJECTED   (REJECTED)  rejected_by, reason, rejects_event_id
FACT_EXPIRED    (REVERIFY)  expires_confirmed_event_id    # CAS: no-op if a newer FACT_CONFIRMED superseded it
FACT_STALED     (STALE)     catalog_change_ref, stales_confirmed_event_id   # CAS: targets the current confirmed version
```

### 3.3 Fact types and value schemas (P1)
Four **data facts** (data-owner authority) and one **policy fact** (`policy_tag`, Compliance authority).
Every `proposed_value`/`value` is validated against a per-type schema at append time:

| fact_type | authority | value schema |
|---|---|---|
| `availability_time` | data owner | `{ column: str, basis: "posted_at"\|"ingested_at"\|"event_time_plus_lag", lag_hours?: number }` |
| `grain` | data owner | `{ columns: [str, …], is_unique: bool }` |
| `scd_effective_dating` | data owner | `{ valid_from: str, valid_to: str\|null, current_flag?: str }` |
| `approved_join` | **both** owners | `{ from_columns: [str,…], to_ref: CatalogObjectRef, to_columns: [str,…], cardinality: "1:1"\|"1:N"\|"N:1" }` — `object_kind="relation"`; identity (`fact_key`) includes the **full normalized relation** (§3.1) so multiple joins per table don't collide; dual-confirmation (§6.4) |
| `policy_tag` | Compliance | `{ decision: "allow"\|"deny"\|"restricted", sensitivity?: str, basis: str }` — `use_case` lives in the `fact_key`, **not** duplicated in the value |

Schemas are registered with SP-0's schema registry (versioned) so event validation, the projection
shape, merged-view consumers, and confirmation editing all share one definition.

**Scope of `resolve_fact`/`get_fact` — ML facts only.** These operate over the **five fact types above**.
*Structural* catalog facts — object existence, column list, column type — are **not** overlay fact types;
they are fields of `CatalogObject` (via `list_objects`), consumed directly by the profiler and grounding.
A catalog is *authoritative for an ML fact* only when it genuinely records one (e.g. a governance catalog
holding `policy_tag`/PII or `availability_time`); `PostgresCatalog` records none of the five, so
its `get_fact` returns `None` for all of them while still supplying structural metadata via `CatalogObject`.

### 3.4 Lifecycle (with REJECTED and proactive entry)
```
                       ┌─ reject ─▶ REJECTED
DRAFT ─ confirm ─▶ VERIFIED ─ expiry ─▶ REVERIFY ─ confirm ─▶ VERIFIED
                       └─ catalog change ─▶ STALE ─ confirm ─▶ VERIFIED
```
- **Canonical status values:** `DRAFT, PARTIALLY_CONFIRMED, VERIFIED, REJECTED, STALE, REVERIFY` (display `REVERIFY` as `RE-VERIFY`). `PARTIALLY_CONFIRMED` applies **only** to two-party facts (`approved_join`).
- **Two-party facts (`approved_join`):** `DRAFT → (first owner confirms) PARTIALLY_CONFIRMED → (second owner confirms) VERIFIED`; either owner's rejection → REJECTED. Single-authority facts go `DRAFT → VERIFIED` directly.
- **REJECTED is sticky by `proposal_fingerprint`:** dedup keys on a **stable fingerprint = hash(canonical `proposed_value` + profiler version + thresholds)** — *not* the evidence id/timestamp (which change every profiling run). A rejected candidate therefore cannot return merely because a new snapshot produced fresh evidence; only a **materially different proposed value** yields a new DRAFT.
- **Proactive (human-entered) facts (P1):** not everything is profiler-inferable, and §6 of the parent allows proactive verified entry. Two supported paths:
  - **Propose-then-confirm:** a human submits `FACT_PROPOSED(proposed_by=human)`, then the resolved authority confirms (normal gate).
  - **Direct entry:** when the **acting principal *is* the resolved authority** for that fact (data owner for a data fact; Compliance for a policy fact), the command may emit `FACT_PROPOSED`+`FACT_CONFIRMED` atomically (a self-confirmation that still records actor + provenance and respects SoD — a data owner can never self-confirm a `policy_tag`). **This self-confirm is an explicit, audited exception to four-eyes (§6.5):** permitted only for a **human resolved authority** asserting an owner-known fact; a **service/profiler proposal can never be self-confirmed** — four-eyes always applies there.

**Lifecycle wiring (P1).** The overlay lifecycle is pinned to an `OVERLAY_TABLE_VERSION` constant, stamped on
every `FACT_*` event (SP-0 events require `table_version`). The projection stores the current `status` **and**
the `table_version` that produced it. Every `FACT_*` command path **runs the transition engine before
appending**: `load_transition_table("overlay_fact", OVERLAY_TABLE_VERSION).matches(current_status, trigger)`,
evaluates guards, and only then appends the resulting event on the same connection (SP-0 OCC) — so an illegal
transition is rejected before it is written.

### 3.5 Evidence vs. fact (separation)
Profiler output is **evidence**, not truth. `FACT_PROPOSED` carries the **proposed_value** (the business
assertion to confirm/edit/reject) and an **`evidence_ref`** → an immutable evidence record (§5.1). Only a
**human-confirmed value** becomes a VERIFIED fact; evidence informs the decision and is never served as a fact.

### 3.6 Projection (the read model)
Per `fact_key`: `status, value, source, confirmed_by, authority_role, confirmed_at, expires_at,
evidence_ref, provenance, object_ref, fact_type, use_case`. Rebuildable from the event stream (SP-0
fail-closed projection rules apply).

**Proposal/task read model (P2).** A *separate* projection serves in-flight confirmation/re-verify tasks and
`get_task_proposal` (§7.2): per `fact_key` it carries `status, proposed_value, proposal_fingerprint,
draft_event_id, evidence_ref, prior_value, partial_confirmers[], object_ref, fact_type, use_case`. The
VERIFIED-fact projection above stays lean for the merged-view hot path; this one holds the workflow detail.

---

## 4. Catalog adapter (P1 — per-fact authority)

```python
@dataclass(frozen=True)
class CatalogObjectRef:
    catalog_source: str          # e.g. "pg:core"
    object_kind: str             # "table" | "column" | "relation"
    schema: str
    table: str
    column: str | None = None
    relation: str | None = None
    # fact_key(ref, fact_type, use_case?) and the display object_ref are derived from this.

class CatalogAdapter(Protocol):
    def list_objects(self) -> Iterable[CatalogObject]: ...                    # tables/columns/types
    def get_fact(self, ref: CatalogObjectRef, fact_type, use_case=None) -> CatalogFact | None: ...
    def owner_of(self, ref: CatalogObjectRef) -> Principal | None: ...        # ownership if recorded
    def fingerprint(self) -> CatalogFingerprint: ...                          # for change detection (§8)

@dataclass(frozen=True)
class CatalogFact:
    value: object
    authoritative: bool        # is the catalog the source of truth for THIS object/fact/use_case?
```
Authoritativeness is a **property of each returned `CatalogFact`**, not a global set — a catalog may be
authoritative for PII tags on some columns but not others, or for type on a column but not its grain.

- **`PostgresCatalog`** (reference): reads structural metadata (existence/columns/types) from
  `information_schema` and the **stable native object id (`oid`) from `pg_catalog`** (used to *detect* renames, §8), exposed via
  `list_objects` → `CatalogObject` — **not** as ML facts. Its `get_fact` (ML fact types) returns `None`
  for all five, so the overlay owns them. `owner_of` returns `None` unless ownership is recorded.
- **`FixtureCatalog`**: in-memory adapter for tests (can mark facts authoritative per object).

A Snowflake / Unity Catalog adapter is just another implementation with its own per-fact authority and `owner_of`.

---

## 5. Data-profiling proposer (deterministic, bounded)

Inspects the actual data (read-only) and emits **DRAFT facts + evidence** for human confirmation:
- **grain** — uniqueness ratio of candidate key sets; **availability_time** — candidate timestamp columns
  (name/type/monotonicity vs an event-time column); **scd_effective_dating** — candidate `valid_from/valid_to`.
- evidence-only metrics: null-rate, distinct count, sample size.

Each proposal carries a **`proposal_fingerprint`** = hash(canonical `proposed_value` + profiler version +
thresholds); the profiler dedups against REJECTED/DRAFT **by fingerprint** (§3.4) before proposing — fresh
evidence alone never revives a rejected candidate.

### 5.1 Evidence store (P1)
Evidence is an **immutable record** in an `overlay_evidence` table (an SP-0-style append-only artifact,
written once, never updated; referenced by `evidence_ref`). Schema:
```
{ evidence_id, fact_key, table_snapshot_at, row_count, sample_size, profile_version,
  thresholds_used, metric_values, created_by, created_at }
```
**No raw values** are stored (SP-0 privacy rule) — only aggregate metrics. **Classification: sensitive
operational metadata** — aggregate metrics over sensitive tables (small `row_count`, distinct counts,
min/max dates, uniqueness over identifying columns) can themselves be revealing — so evidence is
**governance-retained and service-internal / read-controlled** (no raw values, but *not* assumed non-sensitive).

### 5.2 Profiler safety limits (P1)
The profiler runs only under bounded, auditable access:
- **read-only DB role**; **schema allowlist** (only onboarded schemas);
- **aggregate-only queries** (COUNT/DISTINCT over columns; **extrema only as bucketed/derived metrics** — e.g. date→month, numeric→coarse range — never raw `MIN`/`MAX` of identifiers, balances, or dates) — never row exports;
- **sampling policy** (TABLESAMPLE / `LIMIT` above a configurable row-count threshold; full scan only below it);
- **statement timeout**, **max columns**, and **max column-combinations** per run (cap combinatorial key-set search);
- every run records its provenance (§5.1) so a proposal can be explained and reproduced.

---

## 6. Confirmation workflow + authority resolver (P1)

1. A DRAFT — or a `REVERIFY`/`STALE` re-verification (§8) — opens an SP-0 **human-gate task** routed to the
   **resolved authority** (a re-verify task carries `prior_value` + the target `confirmed_event_id` for CAS):
   - **Data facts** → the **data owner**, resolved via `CatalogAdapter.owner_of(ref)`. **If the owner is
     unknown → route to a data-governance / platform-admin queue** (never to whoever submitted the request).
     The governance queue's **default action is to repair ownership and re-route** to the real owner; it may
     **confirm directly only as an explicit, separately-audited authority override (break-glass)** when
     ownership genuinely cannot be established.
   - **Policy facts (`policy_tag`)** → **Compliance**, *always*; a data owner attempting to confirm a policy fact
     is rejected by SP-0's structural SoD.
2. **Confirm** → `FACT_CONFIRMED` (VERIFIED, with `expires_at`); **reject** → `FACT_REJECTED`.
3. **Confirmation race semantics (P1):** a confirm/reject command carries the **`draft_event_id`** (or
   `expected_stream_version`) **and the gate `task_version`**. The command is **rejected** if the fact has
   advanced (a newer draft exists), expired, or been staled since the task was raised — i.e. a CAS on the
   stream (SP-0 OCC) plus task-version staleness. Stale confirmations never apply silently.
4. Unauthorized attempts are denied and recorded in the **security-audit stream** (SP-0).

### 6.4 `approved_join` dual-confirmation (P1)
An `approved_join` asserts cross-object data sharing, so its `value.to_ref` is a structured `CatalogObjectRef`
and it requires **confirmation from the data owners of *both* the source object and `to_ref`** via two human-gate
tasks. The **first** owner's confirm moves the fact `DRAFT → PARTIALLY_CONFIRMED` (`FACT_PARTIALLY_CONFIRMED`);
the **second** owner's confirm moves it `PARTIALLY_CONFIRMED → VERIFIED` (`FACT_CONFIRMED`). Either owner's
rejection → REJECTED. An unknown owner on either side routes that side to the governance queue (§6 step 1).

### 6.5 Command authorization (P1)
All commands run through SP-0 authz; the gate / SoD vocabulary:

| command | actor kind | authorization / SoD | denial |
|---|---|---|---|
| `propose_fact` | service (profiler) **or** human | `overlay.propose` capability; **a proposer may not confirm the same fact (four-eyes)** — *except* the §3.4 direct-entry self-confirm by a **human** resolved authority, audited as an override; a **service** proposal is never self-confirmable | → security-audit |
| `confirm_fact` / `reject_fact` | **human** authority | actor must equal the **resolved authority** for the fact_type (data owner; Compliance for `policy_tag`; **both** owners for `approved_join`); gate `overlay_fact_confirmation`; confirmation CAS (§6.3) | → security-audit |
| `enter_fact` (proactive/direct, §3.4) | **human** authority | actor must *be* the resolved authority; SoD still blocks a data owner entering a `policy_tag` | → security-audit |
| `run_profiler` | service / data-eng operator | `overlay.profile` capability + target schema on the allowlist; runs under the read-only role (§5.2) | → security-audit |

Service actions (`propose_fact` by the profiler, `run_profiler`) carry a **service attestation** (SP-0 service
principal); confirmations require a **human** principal. "Wrong-role rejected" = the resolved-authority check above.

## 7. Merged-view read API

### 7.1 Resolution
```python
def resolve_fact(ref: CatalogObjectRef, fact_type, use_case=None) -> ResolvedFact
```
Precedence: **authoritative catalog fact → VERIFIED overlay fact → missing** (catalog wins *only* when the
returned `CatalogFact.authoritative` is true, §4). STALE/REVERIFY/DRAFT/REJECTED overlay entries are **not**
served as VERIFIED. Return shape (P1):
```
ResolvedFact{ value, status, source('catalog'|'overlay'|'missing'),
              catalog_object, fact_type, use_case, provenance,
              confirmed_by, confirmed_at, expires_at, reason_if_missing,
              prior_value }
```
**Fail-closed:** `status == 'missing'` → the caller (grounding, Layer 3) blocks and routes to confirmation
(the §6.2 hard floor).

**REVERIFY/STALE serving (P2):** a fact in `REVERIFY` or `STALE` returns that `status` with **`value = null`
(not usable)** and **`prior_value` = the last VERIFIED value** as context — so a consumer can show
*"previous value … — re-confirmation required"* without ever treating it as current.

### 7.2 Read authorization posture (P2)
`resolve_fact` can reveal policy tags, table existence, ownership, and metadata about sensitive columns.
In SP-1 the read path is **service-internal only** — called by platform components, not exposed to end
users — and **write/confirm authz is enforced now** (SP-0 authz + SoD on the commands). **Human-facing read
authorization is deferred to SP-9/SP-11**; this is stated explicitly so no consumer assumes the read path is
end-user-safe.

**Task-scoped proposal read (P1).** Confirmation still requires the assignee to see what they're confirming, so
SP-1 exposes `get_task_proposal(task_id, actor) -> { object_ref, fact_type, use_case, proposed_value, evidence }`,
authorized to the **task's assignee** (or the governance-queue role) — a narrow, task-scoped read, distinct from
the deferred end-user `resolve_fact` authz, so confirmation is implementable now without the visual UI.

## 8. Freshness + catalog-change detection (P2 — stable identity)

- **Expiry:** on `FACT_CONFIRMED`, schedule an SP-0 **timer** for `expires_at` (configurable horizon); firing
  emits `FACT_EXPIRED` → REVERIFY **targeting that `confirmed_event_id`** — a **CAS no-op if a newer
  `FACT_CONFIRMED` has since superseded it**, so a stale timer can't downgrade a freshly re-confirmed value. On REVERIFY it **opens a re-verify confirmation
  task** (§6) for the resolved authority, carrying `prior_value` + the target `confirmed_event_id`.
- **Catalog change:** a minimal detector snapshots `CatalogAdapter.fingerprint()` and diffs it. Because `fact_key` is **name-based**, a **rename always yields a new `fact_key`**: the old fact is **STALEd**
  and the renamed object is onboarded afresh (no identity is carried across a rename). A stable native id
  (`oid`) is used **only to recognize** that a change was a rename (vs an unrelated drop+add) so the operator
  can be told *"looks renamed from X"* — it does **not** preserve fact identity. Detected changes (add/drop/rename/type-change) emit an inbound signal → `FACT_STALED` for the
  affected object's facts **(targeting the current `confirmed_event_id`; CAS no-op if already advanced)**; STALE **opens a re-verify task** (§6) for the resolved authority (carrying `prior_value` + target id), and owners are notified. (The richer dependent-feature Change-Impact Analyzer is SP-10.)

## 9. Error handling & concurrency

- Fail-closed merged-view (§7); wrong-role confirmation rejected (SP-0 SoD); STALE/expired never served as VERIFIED; profiler output is DRAFT-only.
- **OCC on `fact_key`** (SP-0): concurrent writers to one fact serialize.
- **CAS on lifecycle writes:** confirmations (§6.3) **and `FACT_EXPIRED`/`FACT_STALED` target a specific `confirmed_event_id`** — a stale expiry timer or late change-signal is a **no-op** if a newer `FACT_CONFIRMED` has superseded it.
- **Idempotent** proposals (**same `proposal_fingerprint`** on an existing DRAFT → no-op, §3.4/§5) and confirmations (idempotent by `(fact_key, draft_event_id)`).

## 10. Interfaces SP-1 exposes (for SP-2+ consumers)
- `resolve_fact(ref: CatalogObjectRef, fact_type, use_case?) -> ResolvedFact` — the read path Layer 3 grounding uses (service-internal, §7.2).
- Commands (all take a `CatalogObjectRef`): `propose_fact`, `confirm_fact`, `reject_fact`, `enter_fact` (proactive/direct, §3.4), `run_profiler(ref)`.
- The `CatalogAdapter` protocol (deployments bind their real catalog).

## 11. What SP-1 deliberately does NOT do
LLM-suggestion / lazy-during-build proposing (SP-2) · console UI (frontend) · non-Postgres catalog adapters (later) · richer Change-Impact Analyzer / revalidation orchestration (SP-10) · human-facing read authz (SP-9/SP-11).

## 12. Testing
- **Migration (P0):** `overlay_fact` appends succeed after the CHECK migration; existing request/feature/run appends + consistency CHECK still hold; **an `overlay_fact` append with a non-null `request_id`/`feature_id`/`run_id` is rejected** by the tightened constraint; the `overlay_lifecycle_table` loads via the extended transition engine; OCC on `fact_key`.
- **SP-0 extensions (P1):** an overlay confirmation task opens with `fact_key`/`draft_event_id`/`evidence_ref` and an `OVERLAY_*` gate; `_task_aggregate` maps a `fact_key` task → `overlay_fact`; the outbox partitions `overlay:{fact_key}`; an `overlay_expiry` timer fires `FACT_EXPIRED`.
- **Self-describing events / rebuild (P1):** the projection (`object_ref`/`fact_type`/`use_case`/`status`) rebuilds purely from the event stream (FACT_PROPOSED carries them); the transition engine **rejects an illegal trigger before append** (lifecycle wiring, `OVERLAY_TABLE_VERSION` stamped).
- **Command authz (P1):** each command's actor/SoD enforced — service-attested `propose_fact`/`run_profiler`; human-only confirm/reject; proposer ≠ confirmer; **wrong-role denied → security-audit**; `get_task_proposal` returns proposal+evidence to the **assignee** and denies others.
- **Value schemas (P1):** each fact type validates its value; malformed value rejected at append.
- **Merged-view:** authoritative-catalog hit · overlay-fill · fail-closed-on-missing · not-served-when-STALE/REVERIFY · **per-fact authority** (catalog beats overlay only where `authoritative=true`) · **ML-fact scope** (`get_fact` returns `None` for all five on information_schema; structural metadata via `list_objects`) · **REVERIFY/STALE returns `prior_value` with `value=null`**.
- **Authority:** data-owner confirms data fact · Compliance confirms policy fact · wrong-role rejected · **unknown owner → governance queue** (not submitter) · **direct entry by the authority** (self-confirm) · data owner **cannot** self-confirm a `policy_tag` · **`approved_join` needs both source and `to_ref` owners** (first → PARTIALLY_CONFIRMED, second → VERIFIED; one confirmation insufficient) · **governance direct-confirm is audited as an override** (default = repair + re-route) · **service-proposed facts are never self-confirmable** (four-eyes; human direct-entry self-confirm is allowed and audited) · **two distinct `approved_join`s on one source table get distinct `fact_key`s** (no collision).
- **Lifecycle:** DRAFT→VERIFIED→EXPIRED→REVERIFY→VERIFIED · DRAFT→REJECTED · VERIFIED→STALE→VERIFIED · profiler **does not re-propose a REJECTED candidate even with fresh evidence** (dedup by `proposal_fingerprint`) · **EXPIRED/STALE opens a re-verify task** (`prior_value` + target `confirmed_event_id`).
- **Evidence (P1):** evidence record is immutable; `evidence_ref` resolves; **no raw values**; provenance present.
- **Profiler (P1):** grain/availability-time inference correctness; **safety limits honored** (read-only role, allowlist, aggregate-only, timeout, max columns/combinations, sampling threshold).
- **Confirmation race / lifecycle CAS (P1):** stale confirmation after a newer draft is **rejected**; **a stale expiry timer or late stale-signal is a no-op when a newer `FACT_CONFIRMED` exists**; concurrent confirm/reject.
- **Freshness/change (P2):** timer expiry → REVERIFY; fingerprint diff (drop/type-change) → STALE; **a rename yields a new `fact_key` and STALEs the old fact** (`oid` only labels it as a rename).
- **Read posture (P2):** `resolve_fact` is reachable service-internally; (human-facing authz is out of scope, asserted by test doc).
- **Adapter:** `PostgresCatalog` against real PG (`information_schema` + `pg_catalog` `oid`); per-fact `authoritative` honored.

## Appendix A — Sample stack (non-binding)
PostgreSQL (overlay events reuse SP-0's event store + the §2.1 migration; a projection table + an
`overlay_evidence` table; the `PostgresCatalog` reference adapter over `information_schema` + `pg_catalog`,
using the stable `oid` only to detect renames, §8) · Python 3.11 · pytest. The `overlay_fact` aggregate is registered
with SP-0's event store and state-machine table via the canonical migrations module.
