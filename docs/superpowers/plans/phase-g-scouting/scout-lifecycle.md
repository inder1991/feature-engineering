# Phase G scout — run lifecycle, durability, publish pointer, tiers

Worktree: `/Users/ascoe/Projects/ai/feature-engineering/.claude/worktrees/phase-g` (branch `feature/phase-g`, HEAD `3b0b7b01`).
All paths below are relative to that root unless absolute.

**Headline fact that colours every answer below.** Nothing in `src/` calls
`record_generation`, `append_run_event`, `record_run_manifest`, `record_group_binding`,
`record_plan_revision`, `record_validation_report`, `record_attestation`, `select_publisher`,
`prepare_run` or `LocalClusterSubmitter.submit`. Verified by grep across `src/`: the only
call sites are tests. `RunManifestV1(...)` is constructed in exactly one place in the entire
repo — `tests/featuregen/materialize/test_control_plane.py:83`. `src/featuregen/materialize/compile/`
contains only `__init__.py` holding `COMPILER_VERSION` (`__all__ = ["COMPILER_VERSION"]`,
`compile/__init__.py:23`); the `compile/chain.py` orchestrator DEFERRED-WORK A.24 promises does not
exist. **There is no orchestrator. The lifecycle is a set of correct, individually-tested parts
with no caller.**

---

## 1. THE CONTROL PLANE AS BUILT

### 1.1 Tables (migration `src/featuregen/db/migrations/1034_materialization_control_plane.sql`, 256 lines)

Seven tables. The header (1034:5) says "all seven tables reject UPDATE, DELETE and TRUNCATE".

| # | Table | Line | Key | Notable columns / constraints |
|---|---|---|---|---|
| 1 | `materialization_generation` | 1034:44 | `generation_id` PK | `logical_group_name`, `materialization_contract_hash`, `group_plan_hash`, `generated_project_hash`, `created_at` (text, caller-supplied ISO-8601), `recorded_at timestamptz DEFAULT now()`. Every text col `CHECK (btrim(...) <> '')`. Index on `logical_group_name` (1034:55). |
| 2 | `publication_capability_attestation` | 1034:63 | `attestation_id` PK | `environment_id`, `hive_version`, `spark_version`, `metastore_version`, `mechanism` (**deliberately NOT a closed CHECK**, 1034:69-72), `passed bool`, `covers_schema_evolution bool`, `evidence_hash NOT NULL` (added beyond §10.3's sketch, 1034:58-62), `attested_at`, `recorded_at DEFAULT now()`. Index `(environment_id, mechanism)` 1034:79. |
| 3 | `group_binding` | 1034:86 | `binding_id` PK, **`UNIQUE(logical_group_name)`** | `materialization_contract_hash`, `physical_target`. No `status`, no `updated_at`, no `current_group_plan_hash` — stated at 1034:84-85. |
| 4 | `group_plan_revision` | 1034:98 | PK `(binding_id, generation_id)` | FKs to `group_binding` and `materialization_generation`; `group_plan_hash`, `created_at` text, `recorded_at`. "Current plan" is DERIVED, never stored (1034:96-97). |
| 5 | `pipeline_validation_report` | 1034:111 | `report_id` PK | FK `generation_id`; `run_id text NULL` (NULL for L0, 1034:114); `level CHECK IN ('L0','L1','L2')` — **L3 deliberately excluded** (1034:109-110); `status CHECK IN ('passed','failed','error')`; `findings jsonb DEFAULT '[]' CHECK (jsonb_typeof = 'array')`; **`CONSTRAINT pipeline_validation_report_error_has_no_findings CHECK (status <> 'error' OR jsonb_array_length(findings) = 0)`** (1034:128-129). Index `(generation_id, level)` 1034:131. |
| 6 | `materialization_run_event` | 1034:139 | PK `(run_id, seq)` | `seq integer CHECK (seq >= 0)`; FK `generation_id`; `event_kind` closed CHECK naming all 8 kinds (`materialization_run_event_kind_is_closed`, 1034:143-152); `occurred_at` text; `detail text DEFAULT ''`; `recorded_at`. Index `(generation_id, event_kind)` 1034:160. **Partial unique index `materialization_run_event_one_terminal ON (run_id) WHERE event_kind IN ('GATES_FAILED','PUBLISHED','PUBLICATION_REFUSED','RUN_FAILED')`** (1034:165-167). |
| 7 | `materialization_run_manifest` | 1034:173 | **`run_id` PK** (= one manifest per run) | Full §12 column list; `business_dt CHECK (~ '^\d{4}-\d{2}-\d{2}$')`; FK `capability_attestation_id → publication_capability_attestation(attestation_id)` (1034:184-185) — the DB half of "no publication without an attestation"; `expected_feature_columns text[] CHECK (cardinality > 0)`; counts `bigint NULL CHECK (>= 0)`; `status CHECK IN ('rejected','published','refused','failed')` (`materialization_run_manifest_status_is_terminal`, 1034:199-201); **`materialization_run_manifest_published_is_complete CHECK (status <> 'published' OR (published_at IS NOT NULL AND publication_location IS NOT NULL AND published_row_count IS NOT NULL))`** (1034:205-209). Index on `generation_id` 1034:211. |

### 1.2 Triggers

- `materialization_control_plane_append_only()` (1034:218-226) — `RAISE EXCEPTION 'the materialization control plane is append-only: % is not allowed on %...'`, referencing neither OLD nor NEW so it works statement-level too.
- Installed by a `DO $$ ... FOREACH` loop over all seven table names (1034:230-256), giving each table **two** triggers: `<table>_no_mutation` (`BEFORE UPDATE OR DELETE ... FOR EACH ROW`) and `<table>_no_truncate` (`BEFORE TRUNCATE ... FOR EACH STATEMENT`). Measured PG-18 fact recorded at 1034:21-25 and DEFERRED-WORK A.22: a FOR EACH ROW trigger **does not fire on TRUNCATE at all**.
- Guarded `REVOKE UPDATE, DELETE, TRUNCATE ... FROM featuregen_app` per table (1034:252-254), no-op when the role is absent.

### 1.3 Migration `1044_run_event_ordering.sql` (30 lines) — what it added

```sql
CREATE OR REPLACE FUNCTION materialization_run_event_ordered() ...   -- 1044:6
  IF EXISTS (SELECT 1 FROM materialization_run_event
             WHERE run_id = NEW.run_id
               AND event_kind IN ('GATES_FAILED','PUBLISHED','PUBLICATION_REFUSED','RUN_FAILED'))
  THEN RAISE EXCEPTION 'materialization_run_event: run % already recorded a terminal event' ...  -- 1044:9-16
  IF EXISTS (SELECT 1 FROM materialization_run_event
             WHERE run_id = NEW.run_id AND seq >= NEW.seq)
  THEN RAISE EXCEPTION 'materialization_run_event: seq % does not extend run %' ...              -- 1044:17-23
CREATE TRIGGER materialization_run_event_ordered BEFORE INSERT ON materialization_run_event
    FOR EACH ROW EXECUTE FUNCTION materialization_run_event_ordered();                           -- 1044:28-30
```

Rationale in its own header (1044:1-5): *"fold_run_status raises forever if an event follows a
terminal one, and the append-only triggers from 1034 make that state unrepairable — so the database
must refuse the write, not merely the read."* **Consequence: `seq` must strictly increase. No
back-fill, no gap-filling, no out-of-order arrival is possible.** Within one session the ordering
trigger fires first; the two unique constraints remain the arbiter of the concurrent race the
trigger's reads cannot see (`control_plane.py:388-405`).

### 1.4 `RunEventKind` — the CLOSED vocabulary (`control_plane.py:68-95`)

| Member | Line | Meaning | Terminal? |
|---|---|---|---|
| `RUN_PREPARED` | 81 | preparation resolved snapshots and prepared parameters (§11.1) | no |
| `RUN_SUBMITTED` | 83 | prepared project + parameters submitted for execution | no |
| `COMPUTATION_COMPLETED` | 85 | staging and assembly finished; §9 gates have not yet run | no |
| `GATES_PASSED` | 87 | every §9 gate passed; the group may be published | no |
| `GATES_FAILED` | 89 | a §9 gate failed; previous partition untouched | **TERMINAL** |
| `PUBLISHED` | 91 | published atomically (§10) | **TERMINAL** |
| `PUBLICATION_REFUSED` | 93 | a `PublicationRefusalCode` decision stopped publication | **TERMINAL** |
| `RUN_FAILED` | 95 | failed outside the gates — execution, submission or environment | **TERMINAL** |

`RunStatus` (98-108): `prepared / submitted / computed / validated / rejected / published / refused / failed`.
`_STATUS_OF_KIND` maps kind→status 1:1 (115-124). `TERMINAL_RUN_EVENT_KINDS` is the ONE definition
(129-134) — the migration's partial index and `RunStatus.is_terminal()` are both compared against it
by the suite. `_TERMINAL_STATUSES` derived at 136-137.

Note the vocabulary has **no member for "the cluster ran, publication has not been attempted yet"
other than `GATES_PASSED`**, and **no non-terminal member expressing "publish started"**. There is
no `PUBLISH_STARTED` / `PUBLISH_IN_FLIGHT`.

### 1.5 Records

- `MaterializationGeneration` (180-204): `generation_id, logical_group_name, materialization_contract_hash, group_plan_hash, generated_project_hash, created_at`. All five identity fields must be non-blank; `created_at` must parse as offset-aware ISO-8601 (`_instant`, 147-164 — naive timestamps refused because "two naive timestamps written in two zones are not orderable").
- `MaterializationRunEvent` (207-249): `run_id, seq, generation_id, event_kind, occurred_at, detail=""`. `event_kind` is **coerced** to the enum (238). `is_terminal()` 245, `status()` 248.
- `RunManifestV1` (252-319) — exactly §12's 20 fields in §12's order, pinned as `RUN_MANIFEST_FIELDS` (324):
  `run_id, generation_id, group_plan_hash, materialization_contract_hash, generated_project_hash, sandbox_execution_hash, business_dt, publication_mechanism, capability_attestation_id, expected_feature_columns, staged_row_count, published_row_count, schema_hash, key_uniqueness_result, required_column_result, orphan_grain_key_count, publication_location, started_at, published_at, status`.
  Invariants: nine identity fields non-blank (286-292); `expected_feature_columns` normalized to tuple and must be non-empty (295-300); three counts whole/non-negative (301-302); timestamps offset-aware (303-306); **`status` must be terminal** (307-312); a `published` manifest must state `published_at`, `publication_location` and `published_row_count` (313-319).

### 1.6 `fold_run_status` (`control_plane.py:330-370`) — verbatim behaviour

Orders by `seq`, **never** by `occurred_at` (338-339). Raises `ValueError` in four cases:
1. **empty** event list (344-348) — "a run with nothing appended has no status";
2. events from **more than one run** (350-355);
3. two events sharing a **`seq`** (358-362);
4. a **terminal event followed by another event** (364-369) — "a terminal event ends the run, so an event after one is a corrupt record".

Returns `ordered[-1].status()` (370).

### 1.7 Writers and readers — verbatim signatures

```python
def record_generation(conn: DbConn, generation: MaterializationGeneration) -> None          # :376
def append_run_event(conn: DbConn, event: MaterializationRunEvent) -> None                  # :387
def read_run_events(conn: DbConn, run_id: str) -> tuple[MaterializationRunEvent, ...]       # :414  (ORDER BY seq)
def run_status(conn: DbConn, run_id: str) -> RunStatus                                      # :422
def published_generation_ids(conn: DbConn) -> frozenset[str]                                # :427  (SELECT DISTINCT ... WHERE event_kind='PUBLISHED')
def record_run_manifest(conn: DbConn, manifest: RunManifestV1) -> None                      # :440
def read_run_manifest(conn: DbConn, run_id: str) -> RunManifestV1 | None                    # :460
def record_group_binding(conn: DbConn, binding: GroupContractBinding) -> None               # :472
def read_group_binding(conn: DbConn, logical_group_name: str) -> GroupContractBinding|None  # :481
def record_plan_revision(conn: DbConn, revision: GroupPlanRevision) -> None                 # :489
def read_plan_revisions(conn: DbConn, binding_id: str) -> tuple[GroupPlanRevision, ...]     # :499  (ORDER BY recorded_at, NEVER created_at — :502-507)
```
The module's entire write surface is `INSERT` (docstring 3-5); a test asserts it issues no
UPDATE/DELETE/TRUNCATE (`test_control_plane.py:147`) and reads no table outside the plane (`:157`).
**Nothing here mints an id, a timestamp or a `seq`** (docstring 22-28) — all supplied, so a
duplicate `(run_id, seq)` is a `UniqueViolation` the caller must handle rather than a race.

### 1.8 What a crashed orchestrator can RESUME from — given only the DB

**Reconstructible:**
- Which generations exist and what they compiled (`materialization_generation`): the group name, the contract hash, the plan hash, the project hash. Enough to re-locate/re-render the artifact.
- The full ordered event stream per run (`read_run_events`) and therefore the run's status (`run_status`), including *how far it got*: `RUN_PREPARED` → `RUN_SUBMITTED` → `COMPUTATION_COMPLETED` → `GATES_PASSED`.
- Whether a run ended, and how (one of four terminal kinds; at most one, enforced twice — index 1034:165 and trigger 1044:9).
- Which generations published (`published_generation_ids`), hence the current plan via `binding.current_plan_revision` (`binding.py:229-277`).
- Every validation report per generation with its findings and the regeneration verdict (`read_validation_reports` / `may_regenerate_for`, §3 below).
- Every attestation per (environment, mechanism), oldest-recorded first (`read_attestations`, `publish.py:468`).
- The terminal manifest, if one was written (`read_run_manifest`).

**NOT reconstructible / unrecoverable:**
1. **No run row exists until somebody appends an event.** A crash between "spark-submit launched" and "the first `append_run_event`" leaves *no trace at all*: `fold_run_status` on an empty list raises rather than returning an "unknown" member (`control_plane.py:344-348`). There is no run registry, no lease, no heartbeat, no `recorded_at`-based staleness rule anywhere.
2. **There is no `seq` allocator.** A resuming orchestrator must know the next `seq`. `read_run_events` gives it `max(seq)`, but `append_run_event` deliberately refuses to compute it (`:388-392`); and 1044 refuses any `seq` that does not strictly extend the run. So a resumer must re-derive the caller-side numbering convention; it is nowhere written down in `src/`.
3. **The gap between the cluster and the plane is unbridged and unbridgeable from the DB.** `COMPUTATION_COMPLETED` / `GATES_PASSED` are appended by whoever *watched* the run. `SubmissionOutcome` (`submit.py:52-67`) is an in-memory value returned by `LocalClusterSubmitter.submit`; it is **never persisted**. If the orchestrator dies while the child process is running, the DB shows `RUN_SUBMITTED` and stays there forever. `RunStatus` has no `UNKNOWN`, no timeout, no reconcile.
4. **There is no way to tell "gates passed but publish never ran" from "gates passed and publish is in flight".** Both read `validated`. The only evidence that publication happened at all lives on the cluster (the metastore) — and no code reads the metastore for that purpose.
5. **The staging evidence lives on the filesystem, not in the DB.** Per-feature `StagingManifestV1` JSON is written to `${staging_root}/feature_staging/<column>/manifest.json` (`render/project.py:539-541`). `group_plan.check_completeness` (`group_plan.py:390`) can judge them, but **no `src/` code reads them off disk** — the only production consumer is the *rendered pipeline's own* re-implementation (`render/nodes_gate.py:285-380`). So after a crash the staged output and its manifests are on disk with nothing in the platform that will ever look at them again.
6. **Append-only means no repair.** A wrong or duplicate event cannot be deleted or updated (1034:230-256). 1044 makes a post-terminal or non-extending insert a `RaiseException`. So a mis-sequenced write bricks the run's fold permanently — 1044's own header says exactly this.
7. **The manifest is the only place `staged_row_count` / `published_row_count` / `schema_hash` / `key_uniqueness_result` / `required_column_result` / `orphan_grain_key_count` / `publication_location` could be recorded, and nothing produces one** (see §2).

---

## 2. RUN MANIFEST + EVIDENCE

### 2.1 `RunManifestV1` — which stage produces it

**None.** Grep across the repo: the only construction site is `tests/featuregen/materialize/test_control_plane.py:83`. The class docstring (`control_plane.py:252-262`) says it is "§12's terminal record of ONE run" inserted "once, at the end", but no `src/` code inserts one. Its seven evidence fields (`staged_row_count`, `published_row_count`, `schema_hash`, `key_uniqueness_result`, `required_column_result`, `orphan_grain_key_count`, `publication_location`) are all `| None` in the dataclass and `NULL`-able in the migration — i.e. the schema already anticipates that they may be unknown.

`record_run_manifest` (`control_plane.py:440-457`) INSERTs all 20 columns; `read_run_manifest`
(`:460-469`) SELECTs the same 20 and reconstructs the frozen dataclass, so it round-trips
field-for-field (`test_control_plane.py:366`).

### 2.2 What the GENERATED pipeline actually writes at run time

The rendered pipeline writes **per-feature staging manifests only**. `render/nodes_compute.py:3273-3328`
(`_manifest_lines`) emits, into `${staging_root}/feature_staging/<column>/manifest.json`:

| key | how produced | line |
|---|---|---|
| `intent_feature_name` | rendered literal | 3315 |
| `ir_hash` | rendered literal | 3316 |
| `generation_id` | run parameter | 3317 |
| `run_id` | run parameter | 3318 |
| `business_dt` | run parameter | 3319 |
| `generated_project_hash` | **read from `GENERATED.lock` at run time** (3286-3288) | 3320 |
| `sandbox_execution_hash` | run parameter | 3321 |
| `output_location` | `staging_root` + the catalog's own relative path (3306-3307) | 3322 |
| `schema_hash` | `sha256` over `{'columns': staged.columns, 'feature': [name, sql_type, nullable]}` (3295-3299) — **column NAMES + declared type, NOT the physical dtype** (3290-3294) | 3323 |
| `row_count` | `staged.count()` | 3324 |
| `status` | `StagingStatus.COMPLETED` | 3325 |

The Python-side type is `StagingManifestV1` (`group_plan.py:206-237`), same eleven fields.

The **gate node** (`render/nodes_gate.py:515-586`) runs §9's six shape gates over the assembled
group and **returns the frame it validated** (`:574`) into the published dataset. It writes **no
evidence document at all**: no row count, no key-uniqueness result, no schema hash is persisted
anywhere a reader outside the run can find. Its docstring (`:527-529`) is explicit: *"The node
returns the frame it validated. It contains no publication mechanism, no table name and no DDL."*

### 2.3 Which of that the control plane can read back

**None of it, today.** There is no reader in `src/` for either the staging manifests or the gate
verdicts (`grep check_completeness src/` → definition only). The `RunManifestV1` evidence fields are
exactly the values the pipeline computed at run time, and there is no code path that carries them
from the cluster into the plane. `SubmissionOutcome.detail` (`submit.py:63`) carries the last 1500
bytes of stderr and 500 of stdout — operator text, not structured evidence, and it is never stored.

**This is the single hardest fact for the Phase G decision:** everything the manifest promises to
record is produced on the cluster and dies there.

---

## 3. VALIDATION REPORTS (`src/featuregen/materialize/validation.py`, 897 lines)

### 3.1 Vocabularies

- `ValidationLevel` (`:87-92`): `L0`, `L1`, `L2`. "``L3`` is the real run and is not a validation."
- `ValidationStatus` (`:95-104`): `PASSED`, `FAILED`, `ERROR`. `ERROR` = *the validation could not run*.
- `FindingClass` (`:107-117`): `RENDERER_DEFECT` / `GOVERNED_FACT_MISMATCH` / `ENVIRONMENT_OR_DATA` / `UNCLASSIFIED`.
- `FindingSeverity` (`:120-130`): `ERROR`, `WARNING` (a fifth enum; only `ERROR` is emitted in this slice — DEFERRED-WORK A.25).

### 3.2 Classification and the blocking rule

`FINDING_CLASSES` (`:141-150`) is TOTAL over `ValidationFindingCode`:

| code | class |
|---|---|
| `PROJECT_DOES_NOT_BUILD` | `RENDERER_DEFECT` |
| `PIPELINE_NOT_CONSTRUCTIBLE` | `RENDERER_DEFECT` |
| `PROJECT_HASH_MISMATCH` | `ENVIRONMENT_OR_DATA` |
| `COLUMN_ABSENT` | `GOVERNED_FACT_MISMATCH` |
| `COLUMN_TYPE_MISMATCH` | `GOVERNED_FACT_MISMATCH` |
| `READ_DENIED` | `GOVERNED_FACT_MISMATCH` |
| `PARTITION_ABSENT` | `ENVIRONMENT_OR_DATA` |
| `UNKNOWN_FINDING` | `UNCLASSIFIED` |

```python
_BLOCKING_CLASSES = frozenset({FindingClass.GOVERNED_FACT_MISMATCH, FindingClass.UNCLASSIFIED})   # :153
def classify(code: ValidationFindingCode) -> FindingClass                                          # :156  (.get default UNCLASSIFIED — fails closed)
```
`ValidationFinding.classification` is a **derived property** (`:220-222`), never accepted, and
`_finding_from_payload` (`:337-350`) deliberately does not read `classification` back — so a stored
row cannot smuggle a routing its code contradicts.

`ValidationReportV1` (`:237-294`) — three coherence invariants: `error` ⇒ zero findings (`:281-285`),
`passed` ⇒ zero findings (`:286-290`), `failed` ⇒ at least one (`:291-294`). `run_id` is `None` for
L0 and never blank (`:270-273`).

### 3.3 Verbatim signatures

```python
def may_regenerate(report: ValidationReportV1) -> bool                                       # :300
def record_validation_report(conn: DbConn, report: ValidationReportV1) -> None               # :321
def read_validation_reports(conn: DbConn, *, generation_id: str
                            ) -> tuple[ValidationReportV1, ...]                              # :353
def may_regenerate_for(conn: DbConn, *, generation_id: str) -> bool                          # :381
def run_l0(root, *, generation_id, environment_id, report_id, python_executable, clock,
           env=None, timeout_seconds=300.0) -> ValidationReportV1                            # :548
def run_l1(rendered, snapshots, *, irs, inventory, metastore, roles, generation_id, run_id,
           report_id, clock) -> ValidationReportV1                                           # :771
```

**The blocking rule.** `may_regenerate` returns `False` when `report.status is ERROR` (`:316-317`)
— *"an unreachable cluster is not evidence that regeneration is the right move"* — and otherwise
`all(finding.classification not in _BLOCKING_CLASSES for finding in report.findings)` (`:318`). It
raises `TypeError` on a non-report (`:311-315`) because the verdict depends on status as well as
findings.

**`may_regenerate_for` (Task 17, `:381-394`) — the CROSS-PROCESS form.** It reads what was
RECORDED and applies `may_regenerate` to **the NEWEST report of each level**:

```python
newest: dict[ValidationLevel, ValidationReportV1] = {}
for report in read_validation_reports(conn, generation_id=generation_id):
    newest[report.level] = report        # ordered oldest-first, so the last one seen is newest
return all(may_regenerate(report) for report in newest.values())
```
Stated rules (`:386-389`): a re-validation supersedes the report it replaces, so a superseded
blocker no longer blocks; but **every level's newest verdict must permit regeneration**, because
"L1's clean re-run says nothing about L0's standing refusal"; and **no recorded reports block
nothing → `True`**. Ordering is `ORDER BY started_at, recorded_at, report_id` (`:369`) — ISO-8601
text, so lexicographic order is chronological; `recorded_at`/`report_id` only break ties.

Neither `read_validation_reports` nor `may_regenerate_for` has a caller in `src/`.

---

## 4. PUBLISH + THE POINTER

### 4.1 `src/featuregen/materialize/publish.py` (687 lines) — §10.3 capability

`PublishMechanism` (`:91-111`), CLOSED, three members; `INSERT OVERWRITE` deliberately absent (`:92-97`):

- `VERSIONED_POINTER = "VERSIONED_POINTER"` — "§10.3's preferred first-slice mechanism: immutable versioned physical outputs with ONE reader-visible pointer/view switch. Still requiring demonstration." (`:105-107`)
- `EXCHANGE_PARTITION = "EXCHANGE_PARTITION"` (`:109`)
- `SET_LOCATION = "SET_LOCATION"` (`:111`)

Evidence chain:
- `ProbeObservation` (`:120-178`): `reader_id, observed_at, generation_id, column_names, row_count, content_digest`. `state_payload()` `:171`, `identity_payload()` `:175`.
- `_derive_passed` (`:190-197`): ≥1 observation, **≥2 distinct generation markers**, and every observation carrying a marker agrees with every other on that marker's whole state payload.
- `_derive_covers_schema_evolution` (`:200-205`): passed AND one observed column set is a **strict superset** of another.
- `ProbeResult` (`:234-312`): `passed`, `covers_schema_evolution`, `evidence_hash` are all **re-derived in `__post_init__`** and a disagreement raises (`:289-312`).
- `assess_probe_observations(observations, *, probe_id, environment_id, mechanism, engine_versions, completed_at) -> ProbeResult` (`:315`).
- `record_attestation(conn: DbConn, probe_result: ProbeResult) -> PublicationCapabilityAttestation` (`:408`) — no `passed=`, no `mechanism=`, no `environment_id=` parameter; `TypeError` on a duck-typed stand-in (`:437-441`); `RETURNING recorded_at` so the returned object equals what will be read back (`:454-465`).
- `read_attestations(conn, *, environment_id, mechanism)` (`:468`) — scoped in the SQL, `ORDER BY recorded_at`.
- `PublicationCapabilityAttestation.matches(engine_versions)` (`:395-405`) — **exact string equality on hive/spark/metastore, no version ordering**.
- `adds_feature_for(group_plan, published_schema)` (`:523-549`) — `published_schema is None` ⇒ `True` (fail closed); comparison over the FULL `expected_schema(plan)`, case-folded.

**`select_publisher` decision tree** (`:552-687`), signature:
```python
def select_publisher(conn: DbConn, *, environment_id: str, engine_versions: EngineVersions,
                     mechanism: PublishMechanism, group_plan: FeatureGroupPlanV1,
                     published_schema: Sequence[str] | None
                     ) -> PublisherSelection | MaterializationRefused
```
1. type guards on `mechanism` / `engine_versions` / `group_plan`, `environment_id` non-blank (`:596-612`);
2. `adds_feature = adds_feature_for(...)` — **derived, never a parameter** (`:614`);
3. no attestations at all ⇒ `CAPABILITY_UNPROVEN` (`:616-622`);
4. attestations exist but none `matches()` the current versions ⇒ `CAPABILITY_UNPROVEN` (drift is *unproven*, not *failed*) (`:624-635`);
5. matching but none passed ⇒ `PUBLISH_MECHANISM_UNSUPPORTED` (`:637-645`);
6. **newest-evidence + tie guard (the amendment, `:647-665`)**: `newest_recorded_at = matching[-1].recorded_at`; any FAILED attestation whose `recorded_at == newest_recorded_at` is "undefeated" ⇒ `PUBLISH_MECHANISM_UNSUPPORTED`. A pass supersedes a failure only when **strictly** newer; a tie (two probes ingested in one transaction share `now()`) refuses;
7. `adds_feature` and no passing attestation covers schema evolution ⇒ `CAPABILITY_UNPROVEN` (`:667-677`);
8. otherwise `chosen = covering[-1]` or `passing[-1]`, and a `PublisherSelection(environment_id, mechanism, capability_attestation_id, engine_versions, adds_feature)` is returned (`:682-687`).

Refusals are **returned, not raised** (`:590-594`).

### 4.2 `src/featuregen/materialize/render/publish.py` (175 lines)

- `RENDERABLE_MECHANISMS = frozenset({PublishMechanism.VERSIONED_POINTER})` (`:62`) — what the *renderer* can write down, distinct from what a probe attests.
- `_PUBLISHED_PREFIX = "published"` (`:65`).
- `published_dataset_name(plan) -> f"feature_{plan.logical_group_name}"` (`:68-81`).
- `_check` (`:84-104`) raises `MaterializationRefused(PUBLISH_MECHANISM_UNSUPPORTED, ...)` for `EXCHANGE_PARTITION` / `SET_LOCATION`.
- `publish_entry_body(plan, *, selection) -> tuple[str, ...]` (`:107-150`) — the catalog body. Target from `physical_target_for(plan.logical_group_name)` (`:119`), split on the **last** dot (`:120-122`). Emitted YAML:
  ```yaml
    type: spark.SparkDataset
    filepath: ${runtime_params:staging_root}/published/<table>      # :142-143
    file_format: "parquet"
    save_args:
      mode: "errorifexists"                                          # :145
    metadata:
      kedro-viz:
        layer: "feature"
  ```
- `render_publish(plan, *, selection) -> str` (`:153-175`) — takes a `PublisherSelection`, never a `PublishMechanism`.

**The write_mode / `errorifexists` policy** (module docstring `:11-23`): under `VERSIONED_POINTER`
the write target is *generation-scoped* (`staging_root` = `<base>/<generation_id>`), so
`errorifexists` "stops being the thing that blocks a re-run and becomes the thing that protects a
generation's own output from being written over". Rendered comments restate it at `:136-138`.

### 4.3 THE POINTER

The prompt asked for `render/publish.py:22-24` verbatim. Those exact lines are the *write-mode*
rationale, not the pointer — worth correcting:

```
22  §9 wants, since the staged output is the evidence its gates were run against. The write mode did
23  not have to relax; the *target* had to stop being shared.
24
```

The pointer statement is at **`render/publish.py:26-31`**, verbatim:

```
26  That is the mechanism's own shape, not a workaround: §10.3's preferred first-slice mechanism is
27  "immutable versioned physical outputs with one reader-visible pointer/view switch". This module
28  renders the immutable versioned output. **The pointer switch is not rendered here and is not a
29  catalog entry** — it is a single metastore operation performed against the live cluster after the
30  run's gates pass, and it belongs with the live probe that has to demonstrate it is atomic.
31
```

Restated inside the emitted catalog comment, `render/publish.py:139-140`:
```python
f"  # The one reader-visible pointer switch onto {target} is a metastore operation run "
f"after §9's gates pass; it is not a catalog entry and is deliberately not rendered here.",
```
And in DEFERRED-WORK **A.26** (`docs/DEFERRED-WORK.md:484`): *"🟡 **The reader-visible POINTER
SWITCH is not rendered** ... **Task 16b / T17.**"*

**What is NOT implemented (exhaustive).** `grep -rni "alter table|set location|exchange partition|saveAsTable|INSERT OVERWRITE" src/ --include=*.py` returns **only docstrings and comments** — no DDL is emitted or executed anywhere in the repo. There is no metastore-write seam at all: `MetastoreMetadata` (`validation.py:653-673`) has exactly three read methods (`list_partitions`, `describe_table`, `can_read`) and, by design, no method that returns a row and none that writes. `physical_target_for` (`binding.py:59-66`) computes `sandbox_feature.<group>` but **nothing ever creates, alters or points that table at anything.**

**What a reader sees today after a "successful publish".** The gate node returns the validated frame
into the `feature_<group>` dataset, whose catalog entry is a `spark.SparkDataset` at
`${staging_root}/published/<table>` in Parquet with `mode: errorifexists`. So:
- the rows land in a **generation-scoped filesystem directory**, not in a Hive table;
- `sandbox_feature.<group>` — the name `physical_target_for` derives, the name written into
  `group_binding.physical_target`, the name printed in the catalog comment (`render/publish.py:125`)
  and in the rendered README (`render/project.py:1043`) — **does not exist as a queryable object**;
- a consumer has no stable path: the next generation writes to a different directory and nothing
  moves;
- the *only* pre-selection alternative is worse — with `selection=None` the renderer falls back to
  Task 12's fail-closed Hive entry (`render/project.py:547-552`), which is `errorifexists` against a
  shared table and therefore cannot run twice.

**What implementing the pointer would require.** Concretely, all of:
1. A **live metastore write seam** — none exists; the only metastore protocol in the platform is read-only by construction, and adding a write method to `MetastoreMetadata` would break the property `validation.py:653-663` is built on. It needs a *new* seam.
2. A **mechanism decision expressed as DDL**: for `VERSIONED_POINTER` the natural forms are (a) `CREATE OR REPLACE VIEW sandbox_feature.<group> AS SELECT * FROM parquet.`<generation dir>`` (a view swap), (b) `ALTER TABLE ... SET LOCATION` on an external table (already a `PublishMechanism` member, and one `RENDERABLE_MECHANISMS` excludes), or (c) an atomic directory rename. **None of the three is attested**, and §10.3's rule is that the mechanism must be probe-proven for the exact environment at the exact engine versions before it may be used.
3. A **live probe (Task 16b)** to produce the `ProbeResult` whose observations demonstrate atomic visibility — `record_attestation` has no back door (`publish.py:437-441`), so no attestation can exist without one. The probe itself does not exist in the repo.
4. A **publish step in the orchestrator** that runs after `GATES_PASSED`, performs the switch, and appends `PUBLISHED` / `PUBLICATION_REFUSED` — i.e. the very orchestrator that does not exist.
5. Probably a **table** or view catalog record: the control plane has `group_binding.physical_target` (the logical→physical name) but **no record of which generation the pointer currently points at**. `RunManifestV1.publication_location` is the closest thing and it is per-run, terminal, and never written. There is no "active revision" row anywhere — DEFERRED-WORK A (line 21) calls this out as *"Multi-write atomicity across data commit + run manifest + **active-revision pointer** + stats + callback"*, deferred.

---

## 5. DEFERRED-WORK — `docs/DEFERRED-WORK.md` section A (591 lines total; A runs 13→590)

### 5.1 The program-level table (A head, `:19-29`) and A.1 (`:35-43`) — the ones Phase G must restate

| Line | Item | Trigger (verbatim where short) |
|---|---|---|
| :19 | 🟡 Iceberg atomic revisions, commit/merge/time-travel, restatement | "First time two writers can commit to one feature table, or the first restatement request." |
| **:20** | 🟡 **Run state machine `REQUESTED→ACCEPTED→RUNNING→COMMITTED/FAILED/CANCELLED/STALE_INPUT`** | **"Scheduled/unattended runs, or any run we cannot just re-launch by hand."** |
| **:21** | 🟡 **Multi-write atomicity across data commit + run manifest + active-revision pointer + stats + callback** | "Same as above." Note: same shape as Child-1 T11, the most defect-dense task — "budget for mutation testing + live probes, not a prose review." |
| **:22** | 🟡 **Outbox / reconciliation when either side is down** | **"First external (non-in-process) executor."** |
| :23 | 🟡 External attestation round-trip (Child-6) | "An executor outside this process. Also gates `DATA-CHECKED` promotion." |
| :24 | ⚪ Execution-signature batching optimization | "Measured cost problem from running many features." |
| :25 | ⚪ Quarantine by bounded bisection | "Batch runs where one bad formula fails a group." |
| :26 | 🟡 Profiling privacy/read-scope hardening | "Before any profiling output leaves the platform..." |
| :27 | 🟡 Full Child-2 lifecycle (artifact→feature-version→binding, `materialization_eligibility`) | "Production activation, or more than one consumer of a frozen artifact." |
| :28 | 🟡 Full `TemporalPolicyV1` (SCD, reversal, late arrival) | "Bitemporal sources, or a late-arrival/reversal correctness question on real data." |
| :29 | 🟡 Delivery I — external-validation protocol (Ed25519 + RFC 8785) | "A partner willing to validate." |
| :35 | 🔴 Governed source-delivery SLA | "Before promising consumers an availability SLA, or before enabling the `dependencies_ready` trigger." |
| :36 | 🟡 `dependencies_ready` cadence trigger | Same as above. |
| :37 | 🟡 **Authenticated submission into a real bank environment** | "A cluster outside the local dev environment, or unattended/scheduled runs." |
| **:38** | 🟡 **Content-addressed input snapshots** | "Reproducibility/replay guarantees, or any restatement work. **Also one of the reasons Spec A publishes to sandbox.**" Detail: `input_snapshot_ids` = `(schema, table, partition)` + `catalog_state_stamp`; "two runs over the same partitions after an in-place source rewrite share the same execution identity." |
| :39 | 🟡 Multi-partition and backfill runs | "First backfill request." |
| :40 | 🟡 Multi-environment promotion | **"A second environment."** |
| :41 | 🟡 `CurrentSnapshot` vintage mismatch has no failure code | (CLOSED by A.25) |
| :42 | ⚪ Spec B — statistical profiling/EDA + UI | "After Spec A publishes on the cluster." |
| :43 | ⚪ Spec C — `model_input` assembly | "When a model needs features from more than one group." |

`:45` "**Not deferred, despite resembling NFRs**" names **atomic group publication ("a partially-published table is a correctness failure")** as explicitly non-deferrable.

### 5.2 A.12 → A.32, one line each

- **A.12** (`:204-214`, Task 10 group-plan/binding): landing-key & `business_dt` columns carry NO physical type (`sql_type is None`, gate must check presence/order/nullability only) · §14 has no code for an UNEXPECTED staging manifest (routed to `UNEXPECTED_COLUMN`) · a `failed` staging manifest reports `INCOMPLETE_COMPUTATION` · `expected_schema` order is the PLAN's not the engine's (**"T17's `pub.schema == expected_schema(result.plan)` must compare name→(type,nullability) rather than positions"**) · `entity_key_columns`/`business_dt_column` redundant in `group_plan_hash` (EQUIVALENT mutants) · A.11's two-cadence override rule does not reach the group binding · `bind_group` also conflicts on a differing PHYSICAL TARGET.
- **A.13** (`:216-223`, Task 11 identity): `RENDERER_VERSION` resolved, **`COMPILER_VERSION` was open then and now lives in `compile/__init__.py`** · `business_dt` only checked non-blank in the execution hash · "the project hash appears in no other file" is a test property not a runtime gate · `SealedProject.files` read-only, hash not recomputed at publish (that is L0's `PROJECT_HASH_MISMATCH`).
- **A.14** (`:225-236`, Task 12 renderer): Task 0 unstarted and T12 needed a slice of it (`EngineVersions`, `kedro_datasets`; every value in `conf/environments/hdfc-local-inventory.yml` still `null`) · ✅ errorifexists blocker CLOSED by 16a · `hooks.py` rendered by T12 · `generation_id` is a RUN parameter · `spark.sql.session.timeZone` pinned to UTC · a T12-only project has no compute · `_expression_requirements` sorts are EQUIVALENT · kedro/kedro-datasets are not repo dependencies.
- **A.15** (`:238-246`, T12 handoffs): ✅ rendered project could not run twice — CLOSED by 16a · `staging_root` must derive from `generation_id` (**Task 13**) · session timezone UTC (**Task 13**) · **`compiler_version` has no owner (Task 15)** · node bodies injected.
- **A.16** (`:248-254`, Task 13a spine): rendered gates raise `RuntimeError` with the code as first token (**Task 14** — answered in A.23, convention KEPT) · **the partition transform is applied in the node, not in run preparation (Task 0/15)** · `AvailabilityPartition` refused for spines.
- **A.17** (`:256-264`, Task 13b PIT): ✅ multi-hop projection CLOSED by 13e · **the window is anchored at MIDNIGHT of the business date in the window's zone — "A spec sentence. Both readings select different rows for every feature, and the difference is invisible at run time."** · projection carries no `business_dt` column · `fake_spark` had no join/agg · a sub-second availability lag refuses.
- **A.18** (`:266-279`, Task 13c calculation): 🔴 a feature whose grain columns are spelled differently from the landing keys refuses (needs a governed key mapping) · four of six `RoundingMode` members refuse · a governed filter comparing against a formula PARAMETER refuses · `OverflowBehavior.ERROR` rests on the rendered CHECK alone, no ANSI mode configured · **the staging manifest's `schema_hash` does not cover the physical dtype (Task 15/L0)** · `_LOCK_DEPTH` is a literal `4` · explicit casts on `F.lit` · the `empty_window/zero_denominator: error` aborts carry no `ValidationGateCode`.
- **A.19** (`:281-291`, Task 13d final op): operand literals carry the PUBLISHED type · **nothing models Spark's DECIMAL division precision (L0)** · `fake_spark` refuses ÷0 · the `null` ÷0 policy is right by accident under non-ANSI Spark · a DIFFERENCE has no worked fixture.
- **A.20** (`:293-302`, Task 13e traversal): **🔴 joined dimensions are read as the run sees them — NO point-in-time gate applies to them; trigger "Before a multi-hop feature is used for training data"** · nothing checks a hop's endpoint TYPES · a grain key the SOURCE relation also spells refuses · no filter is pushed onto a joined table.
- **A.21** (`:304-326`, real-Spark observations): **🔴 `spark.sql.ansi.enabled` is not pinned in the rendered conf and it changes results — "Must be settled before T17 publishes anything from a cluster whose Spark major version we did not choose."** · **🔴 decimal division silently collapses scale at high precision (`decimal(38,18)/decimal(38,18) → decimal(38,6)`)** · `joined_datasets` must name exactly the hops' tables · the `.drop` of each hop key is hygiene · `fake_spark.join` models NULL-key semantics.
- **A.22** (`:328-354`, Task 14a control plane): landed as migration **1034** not 1031, and 1032/1033 are also taken on unmerged branches · **`pipeline_validation_report` has a table and no writer (Task 15)** · **`publication_capability_attestation` likewise, plus `evidence_hash` (Task 16)** · `group_binding` naming · **"A run may hold at most ONE terminal event ... If a lifecycle later needs a second terminal event, the index is the thing to argue with"** · `published_generation_ids` closes §10.1's open seam · `CREATE TABLE IF NOT EXISTS` is fail-open against a same-named foreign table.
- **A.23** (`:389-425`, Task 14b rendered gates): `ValidationGateCode` has **EIGHTEEN** members, not fifteen · "every code is rendered" cannot be true of ONE project · `WRONG_NULLABILITY` judged on ROWS · `SCHEMA_HASH_MISMATCH` fires only when nothing more specific did · `FORBIDDEN_NUMERIC` stands the type gate down · `PROJECT_INTEGRITY` recomputes the hash on the cluster and skips three kinds of file · **"The gate node writes the publication dataset, and that is not a publish mechanism ... Task 16 replaces the entry and may replace this node."** · the `RuntimeError` + leading-token convention is KEPT · 14b collects findings and raises once · none of 14b's gates depends on ANSI · `fake_spark` models `dtypes`.
- **A.24** (`:426-444`, Task 15a run preparation): plan's Step-1 snippets are STALE · `PhysicalInputSnapshot` carries `feature_name`/`expr_path` · `snapshot_id` covers `business_dt` · mapping-zone widening ±1 day · a `FULL_SCAN` table with no listed partitions refuses · a run-time inventory whose layout FINGERPRINT moved refuses · the window arithmetic is a SECOND statement of the renderer's · **the partition TRANSFORM is applied in two places (→ Task 15b/16)** · **the SPINE has no `RunInputRequest` yet (→15b)** · `prepare_run` takes a `RenderedArtifactIdentity` not §11.1's `result` (**"When `compile/chain.py` exists"**) · `COMPILER_VERSION` lives in a package `__init__` · the execution hash excludes itself · a malformed `business_dt` RAISES · a windowed read never consults the metastore · `CurrentSnapshot` vintage still unanswered (→15b/16).
- **A.25** (`:446-467`, Task 15b validation loop): 🔴 `CurrentSnapshot` vintage mismatch ANSWERED (`SPINE_DECLARATION_REJECTED_BY_FACTS`) — A.1 closes · **🔴 L0 runs in the main suite; the RENDERED project's build is an explicitly-invoked GATE (`tests/featuregen/materialize/l0_gate.py`)** · `PROJECT_HASH_MISMATCH` is `ENVIRONMENT_OR_DATA` · `READ_DENIED` is `GOVERNED_FACT_MISMATCH` · an `error` report DISCARDS findings already collected · `may_regenerate` refuses on `error` · `FindingSeverity` is a fifth enum · a finding records an exception TYPE never its message · L1 refuses without the spine snapshot · an `AVAILABILITY_PARTITION` widening resolves FUTURE partitions L1 reports absent · a `LATEST_AVAILABLE_AS_OF` spine over a TIME-MAPPED table refuses · **L0 does not `pip install` the project ("Task 16's cluster acceptance, or a nightly gate")** · L1 compares live metastore against the §0 inventory · partition existence compared order-independently · only `ClusterUnreachable` becomes `error` · `kedro run` invoked as `KedroSession.create(runtime_params=…)` · **"A submission that never started carries `returncode is None`"**.
- **A.26** (`:469-489`, Task 16a — "Task 16 **step 1** only — everything §10.3 needs that does not need a live cluster. **Step 2 (the probe driver and `test_probe.py`) is 16b's.**"): 🔴 the plan types `CAPABILITY_UNPROVEN` as a `CompilationRefusalCode` (it is a `PublicationRefusalCode`) · 🔴 the probe's verdict is DERIVED from the observations · `ProbeResult`'s field sketch incomplete · `PublisherSelection` carries a fifth field `adds_feature` · `PUBLISH_MECHANISM_UNSUPPORTED` is scoped to the mechanism ASKED ABOUT · engine-version drift routes to `CAPABILITY_UNPROVEN` · `published_schema=None` reads as `adds_feature=True` · the renderer emits an entry for `VERSIONED_POINTER` only · **🟡 [A.26 row 9, `:484`] "The reader-visible POINTER SWITCH is not rendered" — Trigger: Task 16b / T17** · A.24's transform-in-two-places belongs with the LIVE probe (→16b) · `RENDERER_VERSION` deliberately NOT bumped · `_quote` moved to `render/_yaml.py` · rendering into a project built for another environment is refused twice · mutation-tested, 7 controls all caught.
- **A.27** (`:491-502`): 🟡 legacy fabricated-N:1 `approved_join` facts persist under authority-persists; correction route is reject-then-re-propose after a cardinality-bearing upload.
- **A.28** (`:504-514`): 🟡 `half_even` on a RATIO refuses (`PHYSICAL_TYPE_UNSUPPORTED`) — Spark's decimal `Divide` wraps in `CheckOverflow` with hard-coded HALF_UP before the emitted `F.bround` runs.
- **A.29** (`:516-526`): 🔴 `prepare_run` refuses any run whose `event_time_ref`/`availability_ref` resolves to a DATE-typed column under a non-UTC `window_timezone` (the IR carries no physical time type). Implemented at `runprep.py:776-828`.
- **A.30** (`:528-557`): 🔴 a real SCD-2 spine with distinct `effective_time_ref`/`availability_ref` cannot validate — one governed as-of per table, enforced at ingest/projection/read, and the refusal has **no fixed point**.
- **A.31** (`:559-570`): 🟡 **`input_snapshots` is prepared evidence, not an enforced read scope** — hashed into `sandbox_execution_hash`, but "no rendered node consumes the snapshot list ... nothing proves the run read precisely (or only) them." Trigger: "§3.4 identity is relied on for audit of what a run read; fix = render partition predicates onto raw sources, or **record actually-read partitions post-run**."
- **A.32** (`:572-590`): 🔴 `requirements.lock` installs an environment that cannot construct the rendered catalog (kedro-datasets 4.x hard-imports `hdfs`/`s3fs`; the `[spark]` extra is uninstallable against the captured cluster). Trigger: "Deploying the rendered project on a real 0.19-line cluster from its lock."

### 5.3 The entries whose trigger is effectively "when the chain is wired"

These are the ones Phase G must explicitly bring forward or restate:

1. **A head `:20` — the full run state machine.** Trigger: *"any run we cannot just re-launch by hand."* The moment a run is scheduled, or a crash means "re-launch by hand" is not obviously safe, this has fired.
2. **A head `:21` — multi-write atomicity across data commit + run manifest + active-revision pointer.** Fires the moment the pointer exists.
3. **A head `:22` — outbox / reconciliation when either side is down.** Trigger: *"First external (non-in-process) executor."* `LocalClusterSubmitter` is a subprocess, so arguably not yet — but the *reconcile* half is exactly the mid-chain-failure question.
4. **A.1 `:38` — content-addressed input snapshots.** Explicitly *"one of the reasons Spec A publishes to sandbox"* — i.e. it is a stated precondition for leaving sandbox.
5. **A.26 `:484` — the reader-visible pointer switch.** Trigger: *"Task 16b / T17"* = now.
6. **A.22 `:349`/`:350` — `pipeline_validation_report` and `publication_capability_attestation` had a table and no writer.** Both have writers now (Task 15b, Task 16a); the *residual* is that neither has a **caller**.
7. **A.24 `:439` — `prepare_run` takes a `RenderedArtifactIdentity`, not §11.1's `result`. Trigger: "When `compile/chain.py` exists."** It still does not.
8. **A.31 — `input_snapshots` is evidence, not scope.** Its own suggested fix ("record actually-read partitions post-run") is a *run-manifest* feature and therefore a Phase G decision.
9. **A.21 `:322` — ANSI mode: "Must be settled before T17 publishes anything from a cluster whose Spark major version we did not choose."** A publication precondition, not a renderer nicety.
10. **A.12 `:211` — `expected_schema` order: "T17's `pub.schema == expected_schema(result.plan)` must compare name→(type,nullability) rather than positions."** A direct instruction to whoever writes the post-publish verification.
11. **A.25 `:461` — L0 does not `pip install` the project. Trigger: "Task 16's cluster acceptance."**

---

## 6. TIERS — sandbox vs production

### 6.1 There is no `ExecutionTier`

`grep -rn "ExecutionTier|execution_tier" .` over the whole repo returns **nothing**. The only
`PRODUCTION` strings in the repo are the *feature-activation* lifecycle in `tests/featuregen/aggregates/`
(`approval_type='PRODUCTION'`, `activation_state='PRODUCTION'`) — a completely different subsystem
(feature versions / activation), not materialization. **The materialization slice has no tier
concept at all: there is exactly one namespace and one code path.**

### 6.2 Every site where "sandbox" is load-bearing

| Site | What it does |
|---|---|
| `binding.py:53-56` | `SANDBOX_NAMESPACE = "sandbox_feature"` — with the comment **"§7's ONE namespace. There is no production path in this slice, and Child-2 must later supply a factory that validates actual frozen bindings before one exists."** |
| `binding.py:59-66` | `physical_target_for(logical_group_name) -> f"{SANDBOX_NAMESPACE}.{hive_identifier(logical_group_name)}"` — "derived, never supplied". |
| `identity.py:81-88` | `derive_namespace() -> str` — **takes NO parameters**, returns `binding.SANDBOX_NAMESPACE` through the module. Docstring `:28-30`: *"Sandbox only, structurally. `derive_namespace` takes no parameters, so there is no argument on which another namespace could arrive."* |
| `identity.py:437-518` | `sandbox_execution_hash(...)`. Named with the prefix deliberately (`:35`): *"a name without the `sandbox_` prefix would read as one half of a pair whose other half does not exist."* Docstring inside: **"There is no production counterpart, and this value is never recorded as `execution_hash`."** |
| `runprep.py:754-773` | `staging_root_for(staging_base, *, generation_id) -> f"{base}/{generation_id}"` — generation-scoped, so a re-run cannot land on a prior run's evidence. `staging_base` is a caller parameter to `prepare_run` (`runprep.py:840`); nothing derives it from a tier. |
| `runprep.py:898` | `"staging_root": staging_root_for(staging_base, generation_id=generation_id)` — one of the six `REQUIRED_RUN_PARAMETERS`. |
| `render/project.py:106-107, 452-490` | `staging_root` in `REQUIRED_RUN_PARAMETERS`; every intermediate/staging/assembled dataset is `${runtime_params:staging_root}/<relative path>`. |
| `render/publish.py:142-143` | the **published** dataset is *also* under `${runtime_params:staging_root}/published/<table>`. |
| `render/project.py:547-552` | with `selection=None`, the published entry is a Hive `errorifexists` entry at `physical_target_for(...)`. |
| `1034:179` | `materialization_run_manifest.sandbox_execution_hash text NOT NULL` — the manifest column itself is sandbox-named. |
| `group_plan.py:76`, `render/nodes_gate.py:507` | `__sandbox_execution_hash` is one of §10.2's three rendered system columns. |

### 6.3 What actually differs today between a "sandbox run" and a "production run"

**Nothing, because there is no production run.** Precisely:

- There is **one** namespace and no factory, parameter, environment variable, config key or column anywhere that could select another. `derive_namespace()` has no argument (`identity.py:81`), `physical_target_for` has one argument and it is the group name (`binding.py:59`).
- The difference is therefore **not even naming-configurable** — it is *structural*: to add a production tier you must add a parameter to `derive_namespace`/`physical_target_for` (which `identity.py:28-30` explicitly says was designed out), change `sandbox_execution_hash`'s name and payload (which would move every execution identity), and add a `group_binding` migration path for targets already recorded under the sandbox namespace.
- **Governance does not differ by tier either** — every governance control in the slice is unconditional: Gate 2 read authorization, the classification policy, the physical-type refusals, the §9 gates, the §10.3 attestation requirement. None reads a tier.
- The things that *would* differ are recorded as deferrals rather than as code: **A.1 `:38`** names content-addressed input snapshots as *"one of the reasons Spec A publishes to sandbox"*; **A.10 `:192`** says the `internal` unclassified-input policy must be revisited *"Before a group is published to anyone outside the sandbox"*; **A.1 `:40`** defers multi-environment promotion entirely ("A second environment"); **A head `:27`** defers the Child-2 lifecycle until "Production activation".
- One asymmetry worth naming: today **the published dataset lives inside `staging_root` too** (`render/publish.py:143`) — so under `VERSIONED_POINTER` the "published" output is physically indistinguishable from staging except by path prefix (`published/` vs `feature_staging/`). Sandbox-ness is currently a *path convention*, and the governance that a production tier would need is entirely un-built.

---

## 7. Cross-cutting summary for the five Phase G decisions

**(a) What is genuinely durable today.** The append-only plane itself (7 tables, 14 triggers, 2 unique
guards, 2 ordering guards) plus five pure/DB-backed decision functions: `fold_run_status`,
`current_plan_revision`, `may_regenerate` / `may_regenerate_for`, `select_publisher`,
`check_completeness`. All of it is tested (39 tests in `test_control_plane.py` alone). None of it is
called.

**(b) Mid-chain failure (cluster ran, publish did not).** Today this is *representable but not
recorded and not recoverable*: the vocabulary has `GATES_PASSED` (non-terminal) and nothing between
it and `PUBLISHED`. There is no lease, no heartbeat, no `recorded_at` staleness rule, no
`SubmissionOutcome` persistence, no reader of the cluster's staging manifests. A crash after
`RUN_SUBMITTED` leaves the run permanently `submitted`, and 1044 forbids any repair of a
mis-sequenced stream. Adding a fifth terminal kind or a resume rule is cheap in code and **expensive
in the migration**: the terminal set is duplicated in `control_plane.py:129`, `1034:167`, `1034:201`
and `1044:12`, and the suite compares all four with `==`.

**(c) Tier semantics.** There is one tier. Adding a second is a structural change to
`derive_namespace`, `physical_target_for` and `sandbox_execution_hash` (name, payload and every
recorded identity), not a config flag.

**(d) Publish pointer.** Not implemented, by an explicit and well-argued deferral (A.26 `:484`,
`render/publish.py:26-31`), and gated behind a probe (16b) that does not exist. Nothing in the repo
emits or executes any DDL. The consequence, which is the real decision: **`sandbox_feature.<group>`
does not exist as a readable object — a "successful publish" today produces a per-generation Parquet
directory nobody can point a consumer at.**

**(e) Durability deferrals that come forward.** A head `:20` (run state machine), `:21` (multi-write
atomicity incl. the active-revision pointer), `:22` (outbox/reconciliation); A.1 `:38`
(content-addressed inputs) and `:37` (authenticated submission); A.26 `:484` (the pointer); A.31
(record actually-read partitions post-run); A.24 `:439` (`compile/chain.py`); A.21 `:322` (pin ANSI
before T17 publishes); A.12 `:211` (T17's schema comparison must be name-keyed, not positional).
