# Physical table configuration — making the §0 inventory a UI, not a YAML file

**Status: design note. Nothing here is built.** Written 2026-08-04 from a design dialogue, so the
reasoning survives the conversation. Every claim about current behaviour carries a `file:line` and
was verified by reading the code; everything else is explicitly marked as proposed.

---

## 1. The problem

Spec A's §0 cluster inventory (`conf/environments/*-inventory.yml`) is hand-edited YAML. Its
`tables:` block needs, per governed table: ordered partition columns, physical column types, storage
location, a `partition_mapping`, and `rewritten_in_place`.

For the two or three tables of the first slice that is fine. For a real rollout it is not, and the
failure mode is worse than tedium: **a wrong `partition_mapping` does not error.** It silently reads
the wrong partitions and the feature is quietly incorrect. A block that is boring to fill in and
catastrophic to get wrong is the exact shape that gets filled in carelessly.

The user's steer, already recorded for attestation, applies unchanged here: per-item human
confirmation at catalog scale is a non-starter; the answer is proposal + bulk by-exception review.

## 2. What is already solved (and must not be rebuilt)

Two things came out of the dialogue that materially shrink the problem. Both were initially
mis-stated by the assistant and corrected against the source; they are recorded here so the same
mistakes are not made again.

### 2.1 The real schema already arrives — via the GLOSSARY, not the column CSV

The column CSV's accepted headers (`overlay/upload/_headers.py:13-29`) carry `source`, `table`,
`column`, `type` and eleven others. There is **no schema header**, and graph object refs are built as
`public.{table}.{column}` from a fixed constant `_SCHEMA = "public"`
(`overlay/upload/graph.py:20,159-163`).

That `public` is a **uniform internal key prefix, not a schema claim.** Nothing resolves a physical
table by reading it.

The real schema comes from the **glossary / FTR upload**: `schema_by_ref()`
(`overlay/upload/graph.py:166-189`) parses each glossary record's `logical_ref`, extracts the declared
schema, and populates `graph_node.schema_name`. A worked example from the running system:

| ref | value |
|---|---|
| object ref / graph ref | `public.bo_cib_customer.cust_num` |
| logical ref | `cib::bo_dpl_cib.bo_cib_customer.cust_num` |

`bo_dpl_cib` is the real schema, and it is there because the glossary declared it.

**Consequence:** the inventory's `logical_schema_map` is consulted **only** when
`graph_node.schema_name` is NULL. For a catalog with a schema-bearing glossary it stays `{}`
permanently. It needs no UI and no capture. Do not build one.

### 2.2 The hardest declared fact is already collected — as `as_of_basis`

The CSV accepts `as_of` and `as_of_basis` (aliases include `availabilitybasis`), and ingest folds
them into a governed **table-level** fact (`overlay/upload/ingest.py:344-360`):

```python
yield table, "availability_time", {"column": as_of_row.column, "basis": basis}
```

where `basis ∈ {"posted_at", "ingested_at"}` (`ingest.py:358,3396`).

That is precisely the distinction `partition_mapping.kind` needs:

| `as_of_basis` | meaning | `partition_mapping.kind` |
|---|---|---|
| `posted_at` | when it happened | `event_time_partition` |
| `ingested_at` | when it landed | `availability_partition` |

So the field previously described as "a human must declare it" is, for the *kind*, **already in the
catalog**, already governed, and already has a confirmation flow around it (Pass B synthesises
grain/availability as PROPOSED-only, with an author-cannot-self-confirm rule at
`ingest.py:2532,2593`).

## 3. Correction to an earlier claim: the mapping IS measurable

It has been stated in several places that `partition_mapping` "is declared by a human, never
captured". **That is true of metadata and false of data.**

Inside one partition (`load_dt=2026-08-05`), compare the event column on the rows it contains:

* every row `tran_dt = 2026-08-05` → **event-time** partition;
* rows spread across the 2nd–5th → **arrival** partition, and the observed spread *is* the
  late-arrival window.

So `kind` and `late_arrival_days` are **measurable by sampling**. The honest limit: sampling measures
what has happened, not what is contracted. Six quiet months then a quarter-end batch lands five days
late, and a feature built on the measured "3" silently drops rows. **The measurement is evidence, not
authority** — a human still confirms the SLA. This is the same propose-then-confirm split already
used for units.

Note this requires reading *data*; `MetastoreInventoryAdapter.capture()` is documented "Metadata
only" (`materialize/inventory.py:708-722`), so sampling is a new capability with a read-scope
implication, not an extension of the existing adapter.

## 4. Where each fact comes from

| Field | Source | Confidence |
|---|---|---|
| `columns` (names, physical types) | metastore | fact |
| `partition_columns` (ordered) | metastore | fact |
| `location`, file format, MANAGED/EXTERNAL | metastore | fact |
| `transform` (`date_iso` / `date_compact`) | **read off actual partition values** | fact |
| cadence, retention window, gaps, staleness | partition listing | fact |
| `kind` (event vs availability) | **catalog `as_of_basis`** (§2.2) | governed, already confirmed |
| `time_ref` (event column) | catalog `as_of` column | governed |
| `late_arrival_days` | **sampling** (§3) | measured — proposal |
| `timezone` | human, **once per environment** | declared |
| `rewritten_in_place` | human, **once per feed** | declared |

**Per newly-catalogued table, the expected number of typed fields is zero.** Everything is either
inherited from the feed, read from the cluster, or already in the catalog. Human input collapses to
confirming a proposal and to two settings that live above the table level.

## 5. Proposed design

### 5.1 The queue is DERIVED, never synced

The configuration list is a view over *catalog tables* LEFT JOIN *inventory entries*. There is no
second list to maintain, so it cannot drift. Uploading a CSV puts a table in the catalog and
therefore in the queue; nothing has to be remembered.

### 5.2 Listed automatically, configuration demanded only ON USE

**This is the constraint that keeps the design from collapsing.** If 400 uploaded tables all start
demanding partition mappings, this recreates the 150K-column confirmation problem already rejected.

A table becomes *blocking* only when a governed feature reads it. Everything else is listed and
quiet.

```
Needs configuring — 2 features are waiting
  🔵 COMP_FINANCIAL_TRAN_REPOS_DLY   used by "avg txn amt 30d"
  ⚪ BO_CIB_CUSTOMER                  used by "avg txn amt 30d"

Catalogued, nothing reads it yet — 398   [show]
```

### 5.3 Declare per FEED, not per table

Tables from one extract almost always share arrival behaviour. The declaration belongs on the
`(source_type, source)` feed, with per-table overrides for genuine exceptions. Forty tables becomes
one declaration plus the handful that differ — which matches how a bank negotiates interfaces rather
than tables.

### 5.4 States, following the existing "AI-proposed is usable" rule

| | |
|---|---|
| ✅ | a person confirmed it |
| 🔵 | **proposed — usable now**, not yet confirmed |
| 🟡 | needs a data check (cannot tell without sampling) |
| ⚪ | nobody has looked / not found on the cluster |
| 🔴 | conflict, or structurally unsupported |

A proposal is usable before confirmation — features compile against it. Confirming records that a
human took responsibility. A proposal later found wrong is **corrected, never silently cleared**.

### 5.5 Screens

**Queue**, grouped by feed, with a capture trigger. **Table detail** — a section on the existing
asset-detail screen, not a new app — showing observed / proposed / decide, with the evidence visible:

```
How dates map                                     🔵 proposed
  Partitioned by ARRIVAL date, not event date.
  Because
   • catalog says tran_dt is `ingested_at`, not `posted_at`
   • sampled 90 days: 12% of rows arrived after their event date
   • largest gap observed: 3 days
  ⚠ We measured 3 days. The feed owner may have agreed a different SLA.
         [Confirm]   [Change]   [Ask the feed owner]
```

**Feed detail** — the defaults, plus the exceptions surfaced for review.

## 6. `source_type` — the enabling change

The user intends to add `source_type` (and `source`) to their CSV.

* `source` is **already accepted** (`_headers.py:14`, aliases `source`/`system`). Nothing to do.
* `source_type` is **not**, and unknown headers are **silently ignored by design**
  (`_headers.py:41,54-55` — *"a repeated unrecognized column is harmless"*). **A CSV carrying
  `source_type` today produces no error, no warning, and no data.** The CSV change must land with the
  code change, not before it.

Note `_norm` strips underscores and lower-cases (`_headers.py:34-37`), so `source_type` normalises to
`sourcetype` — no collision with `source`.

### Three loose threads that are one piece of work

1. the inventory's `engines:` block is keyed by `(source_type, source)` but sits in
   `_DECLARED_FOR_LATER` — read past and never consumed (`materialize/inventory.py:363`);
2. `SOURCE_ENGINE_UNSUPPORTED` is **defined and never raised** — it appears only at its enum
   definition (`materialize/codes.py:74`) and in a comment (`inventory.py:359`);
3. the user wants configuration driven by `(source_type, source)`.

All three are "make `(source_type, source)` a concept the system consumes". Today an Oracle source
would **not** be refused at load; it would fail later and less clearly.

**Steps:** (1) alias entry; (2) carry through the parsed row; (3) persist on the catalog node —
**likely needs a migration, and migration numbers are coordinated across tracks, so the number must
be reserved rather than picked**; (4) wire the guard so `SOURCE_ENGINE_UNSUPPORTED` can fire.
Steps 1–3 make the column real; step 4 makes it useful.

## 7. What this does NOT block — verified

**Code generation does not require the table to exist on a cluster.**

| | needs | when |
|---|---|---|
| compile + render | the **declared layout** (inventory entry) | no cluster |
| L0 build proof | builds the project | no cluster |
| L1 | table/columns **actually exist** | cluster |
| run | cluster + data | — |

`PARTITION_IDENTITY_UNKNOWN` is a **CompilationRefusalCode** (`materialize/inputs.py:221,254,304`)
raised because the inventory has no *entry*, not because the cluster has no *table* — `runprep.py:215`
confirms such a run "refuses at compilation … and never reaches here". Whether the table exists is an
L1 finding (`COLUMN_ABSENT` / `PARTITION_ABSENT`, `codes.py:161-163`), after the project is sealed.

**And for G-1 it is moot:** the chain terminates at `PUBLICATION_REFUSED` after L0 and never reaches
L1. So hand-written inventory entries for tables that exist nowhere still produce a complete, sealed,
build-verified Kedro project. **A filled-in file, not a running cluster, is the short road to
exercising the whole generation path.**

## 8. Open decisions — for the user, not for an implementer

1. **Who may confirm a late-arrival SLA?** It is a claim about a feed's contract, not about data. It
   belongs to the feed owner or a data steward, not whoever is building the feature. This may require
   a role that does not exist in the current RBAC model.
2. **What happens when measurement and confirmation diverge?** Somebody confirms 3 days; a month
   later sampling observes 5. That is the system noticing its own governance has gone stale. Same
   shape as the existing drift concept — it must neither silently stand nor silently vanish.
3. **Capture trigger** — scheduled, or a button someone presses?
4. **Cluster tables absent from every CSV** — surface as "discovered, not governed"? A real metastore
   may hold thousands, so on request rather than in the main queue.
5. **Re-upload after a confirmed mapping.** Most column changes do not affect partitioning and the
   confirmation should survive; but if the `as_of` column itself changes, the mapping's event column
   is stale.
6. **Hive is the only supported engine.** ODS/Oracle needs a metastore adapter, a renderer that emits
   something Oracle executes (or Spark JDBC), and — the awkward part — Oracle partitioning is not
   Hive's folder partitioning, so `partition_mapping` would need a second dialect.

## 9. Suggested sequence

1. `source_type` steps 1–3 (self-contained, no cluster; needs a reserved migration number).
2. The derived queue with use-triggered scoping (§5.1–5.2) — read-only, no capture, immediately
   shows what is actually blocking.
3. Metastore capture wired to the queue (facts tier only).
4. Feed-level declarations (§5.3) — the step that changes the arithmetic.
5. Sampling and the proposal loop (§3) — needs the read-scope decision first.
6. `(source_type, source)` guard wiring (§6 step 4).
