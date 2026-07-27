# Verified Interfaces — feature materialization

**Purpose.** Every API the materialization program depends on, with its **real** signature and behaviour, cited to `file:line`.

**Baseline.** Originally verified at `12bc26d0`; **re-verified at `9cc25e89`** (2026-07-27) after a parallel feature-generation bug-fix stream landed 27 commits. Re-verification method: diff the cited files, not re-read everything.

**Why this exists.** Two successive Spec-A plan revisions were rejected in review. Every defect in both sat in an API that had been *described from memory rather than read*; the one part that survived review was the part verified first. This file inverts the order of operations: **nothing may appear in a spec or plan unless it is verified here first.** A plan reference that cannot be traced to an entry below is a defect, not a detail.

**Maintenance.** When a behaviour here is found wrong, fix this file in the same change that fixes the code or plan relying on it. Entries record what the code *does*, not what it ought to do.

---

## 1. Join planning — `overlay/upload/join_path.py`

```python
classify_join_path(conn, catalog_source: str, from_table: str, to_table: str,
                   *, roles: Iterable[str] = ()) -> JoinOutcome        # :91
```

**⚠️ Table arguments are BARE table names, not schema-qualified.** `_table_of(object_ref)` (`:46-48`) splits on `.` and returns `parts[1]`, so `"public.transactions.account_id"` → `"transactions"`. The BFS destination is compared against that. Passing `"banking.accounts"` never matches. Existing callers pass bare names: `feature_assist.py:752` passes `d.split(".")[-2]`; `contract/author.py:112` passes `grain_table`.

**Consequence for us:** an adapter must parse schema-qualified logical refs, pass bare table names in, keep schema/source identity separately, and **refuse ambiguity** when the same table name exists in two schemas of one catalog source.

**Outcomes** (`JoinOutcome.kind`): `OPERATIONAL` · `UNVERIFIED` · `DENIED` · `NO_PATH`. Layered BFS: shortest clearing path → OPERATIONAL; else clearing+unverified → UNVERIFIED; else a path through a denied hop → DENIED; else NO_PATH. `from_table == to_table` returns OPERATIONAL with no steps (`:98-99`).

**Edge classification** (`:113-124`): an edge is *clearing* when `fact_key is None` (file-declared) **or** `status == 'VERIFIED'`; *unverified* when fact-linked but not VERIFIED; *denied* when either endpoint's `graph_node.sensitivity` is outside `allowed_sensitivities(roles)`.

**⚠️ Authority provenance is LOST on operational paths.** `clearing.append((from_ref, to_ref, card))` (`:121`) drops `approved_join_fact_key` and `approved_join_status`; only `unverified_fact` retains a key. The SQL *does* select both columns (`:105-106`), so the fix is to carry them through the clearing tuple — **inside this query**, not by a second, potentially-drifted read.

**`JoinStep` orientation** (`:80-88`): steps are oriented to traversal direction, and the reverse edge **inverts cardinality** — "a reverse N:1 hop is really 1:N". Cardinality is therefore load-bearing and must not be discarded.

**Façade:** `find_join_path(conn, catalog_source, from_table, to_table, *, roles)` (`:146`) is a backward-compatible wrapper returning steps or None; it collapses the four outcomes and is **not** suitable for us — we need the discriminated kind.

---

## 2. Sensitivity — TWO independent axes

These are different columns with different vocabularies. Conflating them is a governance error.

| Column | Migration | Vocabulary | Meaning |
|---|---|---|---|
| `graph_node.sensitivity` | `0954_graph_node_sensitivity.sql:5` | `pii`, `restricted`, … | **Read-scope tag** — a hard visibility gate |
| `graph_node.effective_restriction` | `0984_graph_node_field_decision_links.sql:20` | `public < internal < confidential < restricted < prohibited` | **Ordered restriction level** |

```python
SENSITIVITY_ORDER: tuple[str, ...] = (
    "public", "internal", "confidential", "restricted", "prohibited")   # safety_floor.py:19
```

`SENSITIVITY_ORDER` is a **most-restrictive floor**, ordered least → most restrictive. Evidence may only *raise* toward `prohibited`; going below the floor needs a governed `SafetyOverride`. **Any label not in the tuple is UNKNOWN and fails closed to `prohibited`** (`safety_floor.py:82-84`) and is never returned verbatim.

```python
allowed_sensitivities(roles: Iterable[str]) -> list[str]      # read_scope.py:19
# = [s for s, required in SENSITIVITY_ROLES.items() if required in roles]
# NOTE: untagged nodes are always visible — that is handled in SQL, not here.
```

**Consequence for us:** `sensitivity_class` derives from `effective_restriction` using `SENSITIVITY_ORDER` (do **not** mint a parallel enum). `access_requirements` derive from `graph_node.sensitivity` via `SENSITIVITY_ROLES`. Unknown values fail closed to `prohibited`; a `prohibited` input refuses materialization outright rather than producing a "prohibited feature group".

---

## 3. Column facts — `overlay/upload/column_authority.py`

```python
read_column_facts(conn, logical_ref: str, field_name: str) -> OperationalColumnFacts   # :99

@dataclass(frozen=True, slots=True)
class OperationalColumnFacts:
    value: str | None       # from the flat graph_node column; the decision log stores only a HASH
    authority: str          # "governed" | "hint"
    provenance: str | None  # a *_decision_id / *_fact_event_id, else None
```

**It carries no sensitivity, access or retention.** Any classification adapter must read `graph_node` directly (§2), not this.

---

## 4. Governed facts — `overlay/facts.py`

```python
AVAILABILITY_TIME = "availability_time"      # :15
GRAIN             = "grain"                  # :16
APPROVED_JOIN     = "approved_join"          # :18
ENTITY_BRIDGE     = "entity_bridge"          # :19
ENTITY_ASSIGNMENT = "entity_assignment"      # :22
```

```jsonc
// AVAILABILITY_TIME value schema
{ "column": str, "basis": "posted_at" | "ingested_at" | "event_time_plus_lag",
  "lag_hours": number }        // lag_hours REQUIRED iff basis == event_time_plus_lag
// GRAIN value schema
{ "columns": [str, ...],       // minItems 1, unique
  "is_unique": bool }
```

**⚠️ There is no source-delivery SLA fact.** `AVAILABILITY_TIME` names *which column carries knowledge time*; it does not say when a source is ready. `OverlayConfig.drift_freshness_sla` (`overlay/config.py`) measures **catalog-scan** freshness, not business-data arrival. Availability promises cannot be derived today.

**⚠️ `ENTITY_ASSIGNMENT` + `GRAIN` do NOT prove a complete entity population.** Together they prove a column represents an entity and that some columns uniquely identify rows. They do **not** prove the table contains *every* member, retains inactive members, or is the authoritative population. Several tables can carry a unique customer id over incomplete populations. A spine source therefore requires its **own governed declaration**; inferring one from these two facts violates the no-inference rule.

---

## 5. Bridges — `overlay/upload/bridge_projection.py`

```python
active_bridges(conn) -> tuple[ActiveBridgeV1, ...]            # :57  VERIFIED-only, ordered

@dataclass(frozen=True, slots=True)
class ActiveBridgeV1:                                          # :18
    fact_key: str; entity_id: str
    left_catalog_source: str;  left_object_ref: str
    right_catalog_source: str; right_object_ref: str
```

Reads `entity_bridge_edge WHERE status = 'VERIFIED'`. Object refs are `"{schema}.{table}.{column}"` (`_obj_ref_str`). **This is the CROSS-CATALOG bridge projection** — for same-catalog joins use `classify_join_path` (§1), not this.

---

## 6. Child-1 formula schema — `formula/schema.py`

```python
TypedLiteral(type: LiteralType, value: str)                          # `type`, NOT `literal_type`
FilterPredicate(op, left, right_literal=None, right_param=None, right_set=None)
    # `kind` is init=False. There is NO `right_ref` field —
    # a predicate's ONLY column reference is `left`.
FilterBool(op: FilterBoolOp, children: tuple[FilterNode, ...])       # `children`, NOT `operands`
SourceRelation(table_ref: LogicalRef)                                # TABLE ref, no column
Grain(entity: str, keys: tuple[LogicalRef, ...])                     # key ORDER IS SEMANTIC
WindowPolicy(event_time_ref, basis: WindowBasis, length: int, unit: WindowUnit,
             start_inclusive: Inclusivity, end_inclusive: Inclusivity,
             timezone: str, empty_window: EmptyWindowResult, null_input: NullInput)
AggregateExpression(aggregation, operand: LogicalRef | None,
                    source_relation: SourceRelation, filter: FilterNode | None,
                    window: WindowPolicy)          # operand is None IFF COUNT_ROWS
UnaryBody(expr) · RatioBody(numerator, denominator, zero_denominator) · DiffBody(minuend, subtrahend)
FormulaOutputPolicyV1(output_type: str, unit, currency,
                      output_additivity: AdditivityClass, external_type_required: bool)
```

**Each `AggregateExpression` owns its `source_relation`, `filter` AND `window`** — so a legal ratio may read two tables with two event-time columns and two windows. Any IR with one PIT spec per *formula* cannot represent Child-1's grammar.

Body-path vocabulary (reused everywhere): `body.expr` · `body.numerator` · `body.denominator` · `body.minuend` · `body.subtrahend`.

`AuthoringIntent(name, hypothesis, target_entity, target_grain_keys=())` — `formula/turns.py:146`. **`name` is the feature name; `AuthoringResult` does not carry one.**

---

## 7. Output authority — `formula/output_authority.py`

**What Child-1 actually resolves** (this invalidates hand-written fixtures that guess):

- **SUM** (`:180-186`) → `ADDITIVE` **only** when the operand's governed additivity is `ADDITIVE` **and** `partition_proof.path_additive`; `SEMI_ADDITIVE` passes through; otherwise **`NON_ADDITIVE`**.
- **COUNT_DISTINCT** (`:155`) → **`NON_ADDITIVE`** unless `disjoint_distinct_values` is proven.
- **COUNT_ROWS / COUNT_NON_NULL** (`:157`) → `ADDITIVE` only across `disjoint_row_partitions`.
- **RATIO / DIFFERENCE** (`:154`) → `NON_ADDITIVE`.

```python
ExprFacts(output_type=None, additivity=None, unit=None, currency=None)   # OperationalValue | None
PartitionProof(disjoint_row_partitions=False, disjoint_distinct_values=False,
               path_additive=False)
```

`unit`/`currency` are C1 **hints** and never force `NEEDS_AUTHORITY`. The advisory `expected_output` is never consulted.

**Consequence for us:** a fixture claiming `ADDITIVE` for a plain SUM is not a genuine Child-1 output. Fixtures must be produced by, or validated against, the real resolver.

---

## 8. Child-1 authoring trace — `formula/trace.py` + migration `1020`

```python
class TraceEventKind(StrEnum):
    STARTED · LLM_CALL_RECORDED · TOOL_CALLED · TOOL_RESULT_RECORDED
    CRITIC_RECORDED · COMPLETED · FAILED                      # :82-91
TERMINAL = {COMPLETED, FAILED}                                 # :94-97

append_event(conn, run_id, kind, *, seq, idempotency_key, ...)  # :173
```

Row columns (`:104-106`): `authoring_trace_event_id, authoring_run_id, seq, kind, llm_call_ref, idempotency_key, payload, payload_hash`.

**`payload_hash` is a sha256 over the canonical payload** — so a terminal event is tamper-evident, and the payload is canonical redacted metadata (JSON-primitive values only).

**Consequence for us:** `AuthoringResult` is a publicly constructible frozen dataclass, so an in-memory consistency check proves nothing about provenance. Admission must verify the supplied result against the **immutable terminal event** for its run: terminal exists, its disposition is `RESOLVED`, its recorded candidate hash matches, the supplied status axes match, and `payload_hash` validates.

---

## 9. Object refs — `overlay/upload/object_ref.py`

```python
parse_ref(logical_ref) -> tuple[source, schema, table, column | None]   # :87
normalize_ref(...)                                                       # :72
_SOURCE_SEP = "::"                                                       # :23
```

Canonical form is `source::schema.table[.column]`.

---

## 10. Migrations

Highest applied: **`1020_formula_authoring_trace.sql`**. Next free number: **1021**. Never renumber an existing migration.

**Write-once pattern** (`0060_aggregates_lifecycle.sql:31-42`): a `RAISE EXCEPTION` trigger function plus `CREATE OR REPLACE TRIGGER … BEFORE UPDATE OR DELETE … FOR EACH ROW`.

**⚠️ A `FOR EACH ROW` trigger does NOT fire on TRUNCATE.** Blocking TRUNCATE needs a separate `BEFORE TRUNCATE … FOR EACH STATEMENT` trigger. Migration 1020 does both; anything claiming append-only must do the same.

---

## 11. Durable audit writes — `overlay/upload/enrich_llm.py`

`_record_llm_call_durable` (`:387-415`) is the reference pattern: read **`get_settings().dsn`** — the full configured DSN — open `psycopg.connect(dsn)` as its own transaction, do ONE bare INSERT, take no advisory lock, and degrade when no DSN is configured.

**⚠️ Never use `conn.info.dsn`** — it is password-less and has caused a real defect in this codebase.

---

## Open questions this file does not answer

Recorded so they are not silently assumed:

1. **Physical type mapping.** `FormulaOutputPolicyV1.output_type` is *logical* (`numeric`, `integer`, `decimal`). No verified mapping to Spark/Hive physical types exists anywhere in the repo — a versioned adapter must be designed, and its version must enter group/project identity.
2. **Partition identity.** Nothing verified defines how a physical partition is identified for a source table, and **source tables must not be assumed partitioned by `business_dt`**.
3. **Entity population source.** No governed fact declares an authoritative, complete entity population (§4). A new declaration is required.
4. **Fan-out semantics.** No governed allocation policy exists for a `1:N` traversal toward the grain (e.g. a joint account's transaction attributable to two customers). Until one does, such a path must be **refused**, not repaired by `dropDuplicates` or pre-aggregation — those encode a business allocation decision as a technical one, and differ per operation (SUM vs COUNT DISTINCT vs RATIO).

---

## 12. Actor identity — `contracts/envelopes.py`

```python
@dataclass(frozen=True, slots=True)
class IdentityEnvelope:                       # :17  "Identity-at-time-of-action" (§6.1)
    subject: str; actor_kind: str; authenticated: bool; auth_method: str
    role_claims: tuple[str, ...]; groups: tuple[str, ...] = ()
    tenant: str | None = None; on_behalf_of: str | None = None
    impersonation: str | None = None; break_glass: bool = False
    source_of_authority: str | None = None; attestation: str | None = None
```

Use this for `SpineSourceDeclarationV1.declared_by`. **⚠️ Never construct one with
`authenticated=True` inside materialization** — minting authenticated identity outside the sanctioned
trust-root modules is a violation this codebase has already been bitten by (caught during the B-slice
merge). Thread the envelope from the request.

---

## 13. Read-scope roles — `overlay/upload/read_scope.py`

```python
SENSITIVITY_ROLES: dict[str, str] = {          # :13
    "pii": "pii_reader",
    "restricted": "restricted_reader",
}
```

`access_requirements` derive from this mapping over the `graph_node.sensitivity` tags present in the
read set. Note it is keyed by read-scope tag, NOT by `effective_restriction` (§2).

---

## 14. Authoring result + terminal event — what Gate 1 can actually verify

`AuthoringResult` (`formula/result.py:97`) fields — note it **already carries `authoring_run_id`**, so
`ResolvedFeatureInput` needs no separate run-id field:

```python
structural_status · capability_status · output_status · expectation_status
critic_status · technical_status                       # the six axes
authoring_disposition · disposition_policy_version · authoring_run_id
candidate_formula · candidate_formula_hash · candidate_proposal
output_requirements · authority_failures · capability_reason · critic_findings_hash
```

**The terminal trace event payload** (`formula/authoring.py:393-408`) contains:

```python
authoring_disposition · disposition_policy_version
structural_status · capability_status · output_status
expectation_status · critic_status · technical_status
candidate_formula_hash · critic_findings_hash
output_requirements: [requirement, ...]
authority_failures: [{reason, operand, field}, ...]
```

So Gate 1 can verify disposition, all six axes and the candidate hash against an immutable,
`payload_hash`-protected record. 

**⚠️ `_TERMINAL_FOR_DISPOSITION = {"TECHNICAL_FAILURE": FAILED}`** (`authoring.py:188`) — every other
disposition, INCLUDING `REJECTED` and `UNSUPPORTED`, writes a `COMPLETED` event. Gate 1 must therefore
check the **payload's `authoring_disposition`**, never merely that a `COMPLETED` event exists.

**⚠️ `trace.py` exposes NO public event reader** — only `open_authoring_run` (`:156`), `append_event`
(`:173`) and `run_status` (`:212`). A `read_terminal_event(conn, run_id)` must be ADDED, using the
existing private `_durable_read` (`:432`) so it inherits the same cross-connection visibility
semantics `run_status` relies on.

`authoring_intent_hash(intent)` lives at `formula/authoring.py:253`.

---

## 15. Re-verification at `9cc25e89` — parallel feature-gen bug-fix stream

A concurrent workstream fixing feature-generation bugs landed 27 commits. Diffing **only the files this reference cites** (cheaper and more precise than re-reading) gives:

**Unchanged — every interface Spec A depends on holds:**
`overlay/upload/join_path.py` · `overlay/facts.py` · `overlay/config.py` · `overlay/safety_floor.py` · `overlay/upload/read_scope.py` · `overlay/upload/column_authority.py` · `overlay/upload/bridge_projection.py` · `overlay/upload/object_ref.py` · `formula/schema.py` · `formula/result.py` · `formula/trace.py` · `formula/authoring.py` · `formula/output_authority.py` · `contracts/envelopes.py`.

Spot-confirmed against the new HEAD: the terminal payload still carries the disposition, all six axes and `candidate_formula_hash`; `_TERMINAL_FOR_DISPOSITION` still maps only `TECHNICAL_FAILURE` → `FAILED`; `AuthoringResult` still carries `authoring_run_id`.

**Changed — one field, additive:**

```python
AuthoringIntent(name, hypothesis, target_entity, target_grain_keys=(),
                recipe_authoring_context: dict[str, Any] | None = None)   # NEW, optional
AuthorTurnRecord(..., tool_context_hash: str = "")                        # NEW, optional
```

**⚠️ Consequence for Gate 1.** `authoring_intent_hash` covers the intent object. A real run may populate `recipe_authoring_context`, so an intent **reconstructed** by a caller without it hashes differently and fails `INTENT_HASH_MISMATCH`. That is the correct behaviour — the admitted intent must be *the intent that was authored*, not a look-alike — but callers must pass the real object, and the plan's fixtures must not assume the field is always absent.

**⚠️ Migration numbering moved.** `1021`–`1030` are now taken (`1021_contract_considered_revision` … `1030_recognition_evaluation`). **Next free: `1031`.** The plan's control-plane migration is renumbered accordingly.

**Not consumed by Spec A** (new modules from that stream, noted so they are not mistaken for dependencies): `formula/replay_authoring.py` · `replay_trace.py` · `recipe_authoring.py` · `recipe_egress.py` · `frozen_configuration.py` · `control.py`. Changed but not depended on: `audited.py`, `author.py`, `critic.py`.

**Re-verify again immediately before implementation begins** — that stream is active.
