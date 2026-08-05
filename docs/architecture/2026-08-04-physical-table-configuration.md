# Physical table configuration — what it would take to stop hand-editing the §0 inventory

**Status: design note, v2. Nothing here is built.**

v1 (commit `a3f0123c`) was reviewed adversarially on 2026-08-05 and graded **materially flawed —
7 Critical**. Three of its load-bearing claims were wrong in the codebase's own terms, and two of the
things it proposed to build already exist. This rewrite is driven by that review
(`.superpowers/sdd/2026-08-03-phase-g-execution-wiring/design-note-review.md`).

**What v1 got wrong, recorded so it is not re-derived:**

1. It claimed `as_of_basis` *is* `partition_mapping.kind`. **It is inverted.** Both CSV-expressible
   values mean *arrival*; see §3.
2. It proposed adding `source_type` with a new migration. **`catalog_engine` (migration 1041) already
   is `(source_type, source)`,** wired and API-exposed; see §4.
3. It proposed a queue as "catalog LEFT JOIN inventory". **The inventory has no database
   representation at all;** see §2, which is now the note's central finding.

---

## 1. The problem

Spec A's §0 inventory (`conf/environments/*-inventory.yml`) is hand-edited YAML. Its `tables:` block
needs, per governed table: ordered partition columns, physical column types, storage location, a
`partition_mapping`, and `rewritten_in_place`.

For two or three tables that is fine. At rollout scale it is not, and the failure mode is worse than
tedium: **a wrong `partition_mapping` does not error.** It reads the wrong partitions and the feature
is quietly incorrect — `inventory.py:199-201` and `hdfc-local-inventory.yml:80-88` say exactly this.
A block that is boring to fill in and catastrophic to get wrong is the shape that gets filled in
carelessly.

The standing steer for attestation applies unchanged: per-item human confirmation at catalog scale is
a non-starter; the answer is proposal plus by-exception review.

## 2. The blocker nobody had named: the inventory has no persistence

**This is the finding that reorders everything else.**

The inventory has **no database table and no API surface.** It is a YAML file whose path arrives via
`FEATUREGEN_MATERIALIZE_INVENTORY` (`queue_lane.py:205,585`), read by `load_inventory(path)`
(`queue_lane.py:599`) — an `open()` plus `yaml.safe_load` (`inventory.py:593-646`) — inside the
compile worker. There is no `inventory` table in `src/featuregen/db/migrations/`, no route under
`src/featuregen/api/`, and the only frontend matches for "inventory" are the unrelated *feature*
registry.

So a UI cannot be built on top of it as things stand. A screen writing to Postgres while
`load_inventory` keeps reading a file from disk **is** a second list, and file-versus-database is
precisely the drift such a design claims to abolish.

**Closing this is a subsystem, not a screen:** persistence for captured entries, plus either a
render-to-YAML step or a `load_inventory` that accepts a `DbConn`. Everything else in this note is
downstream of that decision, and no queue can be a join until it exists.

## 3. `partition_mapping` cannot be seeded from `as_of_basis` — v1 had this backwards

v1 claimed `posted_at` → `event_time_partition` and `ingested_at` → `availability_partition`. The
renderer that consumes the basis says the opposite (`render/nodes_compute.py:54-57`):

> `posted_at` and `ingested_at` **name the arrival instant itself**; under `event_time_plus_lag` the
> column holds the EVENT time and the declared `lag_hours` is when it could first have been read.

Corroborated at `expression_ir.py:47-49` ("which column carries **knowledge time**"). So:

* both CSV-expressible values denote **arrival** — `posted_at` is ledger-posting time, not
  transaction time;
* the only basis under which the `as_of` column holds event time is **`event_time_plus_lag`**
  (`expression_ir.py:227-233`, `overlay/facts.py:56-68`) — and `overlay/upload/canonical.py:18-19`
  states plainly that it **"is not expressible via the CSV basis column"**.

v1 therefore seeded `event_time_partition` — which reads only the window's own partitions
(`inventory.py:99-101`), the **data-dropping** direction — from the value most strongly indicating
the column is *not* event time.

They are also facts about different objects. `as_of_basis` describes *a column's semantics*;
`partition_mapping.kind` describes *how a physical partition relates to an event*. The shipped
template is the counterexample: `hdfc-local-inventory.yml:98-105` declares `availability_partition`
with `time_ref: …tran_dt` **and** `partition_column: load_dt`.

And the arities do not match: `PartitionMappingKind` (`inventory.py:72-79`) has five members —
`EVENT_TIME_PARTITION`, `AVAILABILITY_PARTITION`, `STATIC_SNAPSHOT`, `FULL_SCAN`,
`VERIFIED_UNPARTITIONED`. A two-valued column cannot seed a five-member closed union, and none of its
values means "not a time-partitioned table at all".

`time_ref` cannot be seeded from the `as_of` column either (`inventory.py:102-105`): `time_ref` is the
**event** column, and the `as_of` column is the availability column.

**Also false in v1: "governed, already confirmed."** Two independent defects:

* **the basis is silently defaulted.** A blank `as_of_basis` passes validation
  (`canonical.py:198` guards with `elif r.as_of_basis and …`) and then defaults to `posted_at`
  (`ingest.py:357-359`, and again at `:3396`). Any table declaring `as_of` without a basis gets a
  value nobody wrote.
* **nothing confirms it.** `_assert_fact` (`ingest.py:401-411`) appends PROPOSED then immediately
  CONFIRMED with `authority_basis=AUTHORITY_SOURCE_DECLARED`; its own docstring (`:377-381`) says it
  "never fabricates a confirmer". v1's citation for a confirmation flow (`ingest.py:2532,2593`) is
  the **Pass B LLM-synthesis** path, a different producer the CSV never traverses.

**What survives:** `as_of_basis` may be *one input to a proposal* for `time_ref`. It determines
nothing.

## 4. `(source_type, source)` already exists — do not add a CSV column for it

Migration **`1041_catalog_engine.sql:27-33`** already ships:

```sql
CREATE TABLE IF NOT EXISTS catalog_engine (
    catalog_source text PRIMARY KEY,
    engine         text        NOT NULL CHECK (engine IN ('hive', 'oracle', 'postgres')),
    tier           text        NOT NULL, …);
```

`tier` is `edp`/`ods`, deliberately open-vocabulary (`1041:24-26`); `engine` is what the inventory's
`engines: edp/hive: kind: hive` declares. It is wired end to end — `declare_catalog_engine` /
`read_catalog_engine` (`data_agent/binding_store.py:127-147`), consumed by `resolve_table`
(`:183-220`), exposed at `api/routes/data_sources.py:144,163`.

**Per-feed declaration is already a decision made and shipped**, with this note's own argument in its
header (`1041:6-8`): *"Migration 1037 gave every table its own binding row, which is correct and
unmaintainable: 126 FTR columns across one table is fine, a real EDP is thousands."* The per-table
binding survives as the exception mechanism.

Three traps in adding CSV columns instead:

* **`source_type` is a taken name.** `1004_ingestion_run_source_profile.sql:14` already defines
  `ingestion_run.source_type` as the `SourceCapabilityProfile` identity —
  `technical_csv`/`ftr_glossary`/`connector`.
* **A per-row `source` that disagrees with the upload quarantines the row**
  (`canonical.py:169-172`), and `catalog_source` is always supplied (`ingest.py:1988`). A file
  carrying `source=hive` uploaded under any other catalog source quarantines **every row**.
* **Accepted rows carry it nowhere queryable** — `build_graph` inserts `catalog_source` only
  (`graph.py:250-254,267-272`), so per-row `source` survives only in `quarantine_row.raw`.

**So the change is much smaller than v1 said, and migration-free:** declare `(engine, tier)` through
the existing `PUT /data-sources` surface, and teach `load_inventory` to consume its `engines:` block —
which today sits in `_DECLARED_FOR_LATER` (`inventory.py:363`), tolerated by the key allowlist
(`:618`) and consumed by nothing. That is also what would finally let `SOURCE_ENGINE_UNSUPPORTED`
fire; it is currently defined (`codes.py:74`) and **never raised** anywhere in the repo.

*(Unrelated but worth one line: unknown CSV headers are silently ignored by design —
`_headers.py:41,52-55` — but on the **FTR/glossary** path `_source_attributes`
(`ftr_adapter.py:186-210`) captures every unrecognised header as free text and forwards it to the
enrichment LLM (`enrich.py:531-532`). That is an egress consideration. A `unrecognised_headers()`
helper exists at `ftr_adapter.py:241-256` with **no production caller**.)*

## 5. Schema resolution — mostly solved, but not "no UI needed"

The real schema arrives via the **glossary**, not the column CSV. `schema_by_ref()`
(`graph.py:166-189`) parses each glossary record's `logical_ref` and populates
`graph_node.schema_name`. `public` in an object ref is a fixed internal prefix (`_SCHEMA`,
`graph.py:20`), **not** a schema claim — nothing resolves a physical table by reading it.

v1 concluded "`logical_schema_map` needs no UI. Do not build one." **That is too strong.**
`resolve_physical_identity` (`inputs.py:155`) reads `inventory.declared_schema_for(table_ref)`
**every time**, and when the catalog attests one schema and the map declares a different one it
**hard-refuses** with `AMBIGUOUS_TABLE_NAME` (`inputs.py:167-173`). A stale entry blocks compilation.

And the map is genuinely required where a glossary does not cover the catalog: technical-CSV uploads
attest no schema at all (`1000_graph_node_schema_declared.sql:8-9`); partial glossary coverage leaves
the rest NULL (`graph.py:166-189` skips schema-less records); reader-quarantined columns never graph
(`glossary_reader.py:112,224-228`).

**Correct statement:** no capture is needed where the glossary attests a schema; the map remains
required for technical-CSV catalogs, and a wrong entry refuses compilation.

## 6. Sampling can propose the mapping — with failure modes that decide the design

Inside one partition, comparing the event column to the partition value distinguishes event-time from
arrival partitioning, and the spread estimates the late-arrival window. The clean case works. These
do not:

1. **Zero spread is the unsafe direction.** One day of history, a partition still being written, or a
   feed that has had no late rows yet all propose `event_time_partition` — the narrow read.
   `inventory.py:132-134`: *"an inferred widening is a guess about a specific bank's specific feed,
   and the failure mode of guessing low is invisible."*
2. **Backfill and reprocessing** put historic events in a current partition; the spread is the
   backfill span, not the SLA. `rewritten_in_place: true` (`inventory.py:235`) destroys the evidence
   entirely.
3. **Null event columns** bias the sample — and are exactly the rows an event-time predicate drops.
4. **Timezone skew.** If the partition value is cluster-local and the event column UTC, a true
   `event_time_partition` shows ±1 day and is proposed as `availability_partition`.
5. **Three kinds where the question is meaningless.** `static_snapshot` partitions are declared
   vintages (`inventory.py:155-161`) and would sample as a wide spread; `full_scan` and
   `verified_unpartitioned` have nothing to sample.
6. **Composite partition keys are silently under-specified — which is worse than unrepresentable.**
   Both time-partition variants name exactly one `partition_column` (`inventory.py:119-120,152-153`)
   while `partition_columns` is an ordered list, and `_refuse_incoherent_layout`
   (`inputs.py:259-267`) only requires the named column to be a **subset**. So a `(year, month, day)`
   table compiles happily against a mapping naming just `day`. Nothing refuses; the sampling
   comparison is simply undefined. (`StaticSnapshot` does name many columns, so the type system is
   not the limit here — the two time-partition variants are.)

**The consequence v1 missed entirely.** `partition_mapping` is **identity-bearing**:
`TableLayout.semantic_payload` (`inventory.py:245-251`) → `layout_fingerprint` (`inputs.py:319`) →
`PhysicalInputRequirement.identity_payload` (`inputs.py:92-102`) → `ir_hash`. As
`inventory.py:133-135` puts it, *"two widenings read two different partition sets, so they are two
different computations."* **So correcting a proposal invalidates every artifact sealed against it.**

**This breaks the "proposed values are usable before confirmation" rule — for this field only.** A
wrong `unit` is visible; a wrong partition set is not, and it silently drops rows. This field needs a
human gate before compilation, unlike every other proposal in the system. That is a genuine exception
to a standing principle and should be decided deliberately.

**The sampling home already exists.** `data_agent/` ships a governed profiling subsystem with Hive
and Postgres dialects (`sql_hive.py`, `sql_postgres.py`), sample-vs-census honesty
(`sql_hive.py:82-96`), declared partition scope and `RowCoverage`
(`relationship_observation.py:220-224`), and connection authorization (`binding_store.py:118-121`).
Not a new capability — an extension of that one. (`MetastoreInventoryAdapter` is "Metadata only",
`inventory.py:717-718`, and has **no caller in `src/`**.)

## 7. Scoping by use — derivable logically, circular physically

"A table becomes blocking only when a governed feature reads it" requires feature → table.

**Feature → LOGICAL table is derivable today, no compile needed.** `lineage.py` already models
`("table", catalog_source, table_name)` nodes (`:87`) and walks `feature_derives_from` from column to
feature (`:571`); `feature_current_contract` ⋈ `contract_metadata_dependency`
(`1011_contract_pointer_model.sql:127-140,162-169`) is the same query from the contract side.
**The queue is buildable now on logical `(catalog_source, table_name)`.**

Three limits, all open:

* **Feature → PHYSICAL `(schema, table)` is circular for schema-less catalogs.** Blocking is a
  physical predicate (`inputs.py:301-307`), and resolving the pair needs `graph_node.schema_name` or
  `logical_schema_map` — which lives in the same document as `tables`. For a technical CSV you need
  an inventory entry to learn which tables need inventory entries. It is also not total: two attested
  schemas for one bare name refuses `AMBIGUOUS_TABLE_NAME` (`inputs.py:158-163`).
* **Persisted lineage is incomplete for direct confirms** — `contract/govern.py:296-299`:
  *"Lineage based only on the draft's derives/grain/as_of/join is INCOMPLETE."* The full read set
  persists only when `plan_envelope is not None` (`:576-579`). A direct-confirm feature can read a
  join-key table appearing in no lineage row: listed as quiet, then refusing at compile.
* **Materialize-lane features have no persisted per-feature READ SET** — inputs come from the formula
  AST at compile time (`expression_ir.py:782-802`). The sealed artifact is not empty of table refs
  (`contract.py:549` and `spine.py:322-324` put `source_table_ref` and `ordered_key_refs` into the
  contract hash stored at `1054:65-67`), but there is no queryable "which tables does this feature
  read" to drive a queue from.

## 8. What each fact can come from

Corrected from v1. "Fact" means read, not inferred.

| Field | Source | Status |
|---|---|---|
| `columns`, `partition_columns`, `location` | metastore — **but the reader is not built**: `MetastoreInventoryAdapter.capture()` exists and is callerless, and its `MetastoreTableMetadata` is a Protocol with **no implementation**. `data_agent` reads schemas with plain `DESCRIBE` only (`sql_hive.py:161-163`), which does not yield partitions or location. | fact **once something implements the Protocol** |
| `transform` (`date_iso`/`date_compact`) | actual partition values | fact |
| cadence, retention, gaps | partition listing | **observation, not fact** — a gap is a bank holiday or a missed load |
| `kind` | **sampling proposal only** (§6) | not derivable from the catalog |
| `time_ref` | proposal; `as_of` column is an input, not an answer (§3) | needs confirmation |
| `late_arrival_days` | sampling | proposal; guessing low is invisible |
| `timezone` | **per-mapping required field** (`inventory.py:111,142`) | per-environment is a *default to propose*, not where it lives — an overseas branch breaks it |
| `rewritten_in_place` | human | declared |

**v1 claimed "the expected number of typed fields is zero."** That was a claim about a proposed
future presented as arithmetic. Today `MetastoreInventoryAdapter._declaration`
(`inventory.py:762-772`) **refuses** any table with no `TableDeclaration` — which is exactly
`partition_mapping` + `rewritten_in_place` (`:690-695`). Zero depends entirely on feed-level
inheritance, which is not built.

## 9. What this does not block — corrected

**Code generation needs no cluster.** Verified: nothing on the **trigger→seal path** contacts a
metastore, and that path imports no metastore, Thrift, JDBC, Spark or Kedro client. (Scoped
deliberately: `submit.py:93-94` *does* import a Kedro client — but `submit` has no importer in `src/`
at all, it is G-2 code, so it sits off the shipped path.) `PARTITION_IDENTITY_UNKNOWN` is a
*CompilationRefusalCode* raised on a missing inventory **entry** (`inputs.py:301-307`), not a missing
cluster table.

But v1's "needs" column was materially incomplete:

| | needs | cluster? |
|---|---|---|
| compile + render | the declared layout **plus a fully governed catalog** — `graph_node` rows for the table and every declared column (`spine.py:587-602`), a governed `entity_assignment` (`:640-653`), governed `GRAIN` facts (`:670-700`), a governed `availability_time` fact with C1 `status == "resolved"` (`:728-740`; an upload flag is explicitly rejected at `:660-663`), and one governed `is_as_of` column per expression (`expression_ir.py:505-527`). Gate 2 refuses any ref with no `graph_node` row (`ir.py:661-673`). | no |
| L0 | **plus a local interpreter matching `engine_versions` by exact string equality** (`validation.py:610-627`) — `validation.py:621`: *"A capture that writes `kedro: "1.5"` renders a lock that can never pass."* | no |
| L1 | the table and columns actually exist | yes |

**And the terminal was wrong.** `compile/chain.py:633-644`: `PUBLICATION_REFUSED` when L0 **passes**,
`RUN_FAILED` otherwise. The project is still sealed either way (`chain.py:583-587`: "A FAILING L0 IS
NOT A ROLLBACK").

**Trapdoor:** if a *passing* capability attestation is ever recorded, `select_publisher` returns a
selection and the chain raises `PublishStepMissing` (`chain.py:509-516`) **before** `render_project`
(`:518`) — no project is sealed at all. "Moot for G-1" holds only while publication capability stays
unproven.

**The consequence worth drawing:** `run_l1` has **zero call sites in `src/`**, and `ir.py:578-581,625`
say three times that "L1 sits on no production path". So on the shipped path **nothing ever checks a
hand-written entry against reality.** `TableLayout.columns` is read only by unreached G-2 code
(`runprep.py:808`); declared physical types are never compared to anything; `location` is validated
non-blank (`inventory.py:579`) and read by nothing. A fabricated inventory is confronted with a real
system in exactly one place: L0's engine-version comparison.

## 10. Open decisions — for the user, not an implementer

1. **Where does a captured inventory live?** (§2) Nothing else proceeds without this.
2. **Does `partition_mapping` get an exception to "proposals are usable before confirmation"?** (§6)
   It is identity-bearing and its wrong direction is silent. Every other proposal in the system is
   usable unconfirmed.
3. **Who may confirm a late-arrival SLA?** A claim about a feed's contract, not its data — the feed
   owner or a steward, which may need a role the RBAC model lacks.
4. **What happens when a confirmed mapping is later contradicted by sampling?** It invalidates sealed
   artifacts (§6), so this is not merely a notification.
5. **Composite partition keys** (§6.6) compile against a mapping that names only one of them, with
   nothing refusing. Extend the time-partition variants to an ordered list, or refuse a layout whose
   mapping does not name every partition column?
6. **Engine coverage.** Narrower gap than v1 stated: the *render* path is Hive-only
   (`render/project.py:440` emits `spark.SparkHiveDataset`), but `data_agent` already ships a Postgres
   dialect and `catalog_engine`'s CHECK admits `oracle` and `postgres` (`1041:29`). Oracle
   partitioning is not Hive's folders, so `partition_mapping` would still need a second dialect.

## 11. Suggested sequence

1. **Decide §10.1 and build inventory persistence.** Everything else is downstream.
2. **The read-only queue on logical `(catalog_source, table_name)`**, using `lineage.py:528-543`.
   Shows what is blocking; needs no capture.
3. **Declare `(engine, tier)` via the existing `PUT /data-sources`**, and teach `load_inventory` to
   consume `engines:`. Migration-free; lets `SOURCE_ENGINE_UNSUPPORTED` fire.
4. **Metastore capture** into that persistence — facts tier only. **Partly built:**
   `MetastoreInventoryAdapter.capture()` is written and callerless, but its `MetastoreTableMetadata`
   Protocol has no implementation, so the actual cluster read (partitions, location) does not exist
   yet. `data_agent`'s `DESCRIBE` (`sql_hive.py:161-163`) covers schema only.
5. **Feed-level declaration inheritance** — the step that changes the arithmetic.
6. **Sampling proposals**, as an extension of `data_agent/`, after §10.2 is answered.
