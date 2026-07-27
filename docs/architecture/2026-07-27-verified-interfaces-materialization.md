# Verified Interfaces — feature materialization

**Purpose.** Every API the materialization program depends on, with its **real** signature and behaviour, cited to `file:line`.

**Baseline.** Originally verified at `12bc26d0`; **re-verified at `9cc25e89`** (2026-07-27) after a parallel feature-generation bug-fix stream landed 27 commits. Re-verification method: diff the cited files, not re-read everything.

**Why this exists.** Two successive Spec-A plan revisions were rejected in review. Every defect in both sat in an API that had been *described from memory rather than read*; the one part that survived review was the part verified first. This file inverts the order of operations: **nothing may appear in a spec or plan unless it is verified here first.** A plan reference that cannot be traced to an entry below is a defect, not a detail.

**Maintenance.** When a behaviour here is found wrong, fix this file in the same change that fixes the code or plan relying on it. Entries record what the code *does*, not what it ought to do.

---

## 1. Join planning — `overlay/upload/join_path.py`

```python
classify_join_path(conn, catalog_source: str, from_table: str, to_table: str,
                   *, roles: Iterable[str] = ()) -> JoinOutcome        # :137
```

**⚠️ Table arguments are BARE table names, not schema-qualified.** `table_of_ref(object_ref)` (`:69-77`, public since Task 3 — was the private `_table_of`) splits on `.` and returns `parts[1]`, so `"public.transactions.account_id"` → `"transactions"`. The BFS destination is compared against that. Passing `"banking.accounts"` never matches. Existing callers pass bare names: `feature_assist.py:752` passes `d.split(".")[-2]`; `contract/author.py:112` passes `grain_table`. Its other importers are `entity.py` and `lineage.py`.

**Consequence for us:** an adapter must pass bare table names in, keep schema/source identity separately, and **refuse ambiguity** — see §17.4 for what that check can actually discriminate (resolved physical schemas, never the refs).

**Outcomes** (`JoinOutcome.kind`): `OPERATIONAL` · `UNVERIFIED` · `DENIED` · `NO_PATH`. Layered BFS: shortest clearing path → OPERATIONAL; else clearing+unverified → UNVERIFIED; else a path through a denied hop → DENIED; else NO_PATH. `from_table == to_table` returns OPERATIONAL with no steps (`:145-146`). `DENIED` carries `endpoints` but **no steps**.

**Edge classification** (`:161-171`): an edge is *clearing* when `fact_key is None` (file-declared) **or** `status == 'VERIFIED'`; *unverified* when fact-linked but not VERIFIED; *denied* when either endpoint's `graph_node.sensitivity` is outside `allowed_sensitivities(roles)`.

**Authority provenance is CARRIED (Task 3, was lost).** `JoinStep` (`:17-44`) now has six fields — `from_ref`, `to_ref`, `cardinality`, and (defaulted, so the extension is additive) `approved_join_fact_key`, `approved_join_status`, `authority`. The fetched row travels as `_Edge` (`:86`) = `(from_ref, to_ref, cardinality, fact_key, status, authority)` through classification into the BFS, so the provenance comes from **the same query that planned the path** — a second read of `graph_edge` would be a different snapshot of a table the join projection mutates. A file-declared edge reports `authority='operational'` with `approved_join_fact_key=None`: that `None` is a meaningful answer (nobody approved this edge), not a missing one. The query still filters `authority = 'operational'`, so `display_only` edges never appear.

**`JoinStep` orientation** (`:117-134`): steps are oriented to traversal direction, and the reverse edge **inverts cardinality** — "a reverse N:1 hop is really 1:N". Authority is a property of the EDGE, so fact key / status / authority ride **both** orientations unchanged while cardinality flips. Cardinality is load-bearing and must not be discarded; `str | None` over a nullable column, so `None` means UNATTESTED, never "safe".

**Façade:** `find_join_path(conn, catalog_source, from_table, to_table, *, roles)` (`:193`) is a backward-compatible wrapper returning steps or None; it collapses the four outcomes and is **not** suitable for us — we need the discriminated kind.

**Adapter:** `materialize/joins.py` `plan_join(...) -> JoinPlan | MaterializationRefused` is the only materialization entry point to this planner; it owns no traversal.

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

**Output TYPES, entered during Task 2** (the reference previously recorded only the additivity half, which is not enough to hand-author a `FormulaOutputPolicyV1`):

| body | `output_type` | `unit`/`currency` | `external_type_required` |
|---|---|---|---|
| SUM / DIFFERENCE | the operand's C1 `logical_representation` verbatim (`_numeric_output_type`, `:206-217`); `"unknown"` if absent or non-numeric | C1 hints, carried | `False` **only** when that C1 read is `status="resolved"` |
| COUNT_* | `"integer"` (`_COUNT_OUTPUT_TYPE`, `:65`) — dimensionless | `None` | `False` |
| RATIO | `"decimal"` (`_RATIO_OUTPUT_TYPE`, `:66`) | `None` (they cancel) | `False` |

A RATIO whose operand units/currencies do NOT cancel is an `ExternalRequirement`
(`UNIT_PROVISIONING_REQUIRED` / `CURRENCY_PROVISIONING_REQUIRED`), never a policy; a DIFFERENCE with
incompatible units/currency is `InvalidOutput`. `resolve_formula_output_policy` always passes
`_NO_PROOF` (`:194`), so no partition proof exists at resolve time — which is why the additivity
table above collapses to `NON_ADDITIVE` for every first-slice feature.

**Verified end to end in `tests/featuregen/materialize/test_fixtures.py`**: the three worked features
are hand-authored AND re-derived by driving the real orchestrator, so a wrong fixture is a failing
test rather than a silent forgery.

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

**ADDED IN TASK 2** — the public event reader §14 said was missing now exists:

```python
@dataclass(frozen=True, slots=True)
class TerminalEvent:
    kind: TraceEventKind; payload: Mapping[str, Any]; payload_hash: str   # payload is read-only

read_terminal_event(conn, run_id) -> TerminalEvent | None    # the run's one terminal event
read_run_intent_hash(conn, run_id) -> str | None             # authoring_run.intent_hash
```

Both go through the private `_durable_read`, so they inherit `run_status`'s cross-connection
visibility (durable fresh connection UNION the caller's, absence-derived). `read_terminal_event`
deliberately does **not** validate `payload_hash` against `payload` — a reader that dropped a
mismatching event would report the run as incomplete and HIDE the tampering; verifying is the
caller's decision (`materialize.admission`, §1.2 check 2, refuses with `TERMINAL_PAYLOAD_TAMPERED`).

⚠️ **`authoring_run.intent_hash` — NOT the terminal payload — is where the intent hash lives.** The
terminal payload (§14 below) carries the disposition, the six axes and the two hashes; it carries no
`intent_hash`. Only the STARTED event's payload and the write-once manifest do, and the manifest is
the one written first, before any provider call. Spec §1.2 check 6 therefore needs a SECOND read,
which is why `read_run_intent_hash` exists alongside the event reader.

**Consequence for us:** `AuthoringResult` is a publicly constructible frozen dataclass, so an in-memory consistency check proves nothing about provenance. Admission must verify the supplied result against the **immutable terminal event** for its run: terminal exists, its disposition is `RESOLVED`, its recorded candidate hash matches, the supplied status axes match, and `payload_hash` validates.

---

## 9. Object refs — `overlay/upload/object_ref.py`

```python
parse_ref(logical_ref) -> tuple[source, schema, table, column | None]   # :87
normalize_ref(...)                                                       # :72
_SOURCE_SEP = "::"                                                       # :23
```

Canonical form is `source::schema.table[.column]`.

**⚠️ Entered at Task 2 — every ref an UPLOAD produces is under schema `public`.** `graph.build_graph`
builds object refs with `_table_ref`/`_column_ref` (`graph.py:148-155`), which do not take a schema:
the real (pre-flatten) schema is preserved only in the separate `graph_node.schema_name` column, via
the `schemas` dict. So a governed logical ref is `hdfc::public.transactions.txn_amt`, never
`hdfc::banking.transactions.txn_amt`, and `object_ref._DEFAULT_SCHEMA = "public"` is not a fallback
in practice — it is the value.

**Consequence for §1/§3.1.** The `AMBIGUOUS_TABLE_NAME` case (one catalog source, one table name, two
schemas) cannot arise from the upload path as it stands, and the schema segment of a logical ref
carries no cluster meaning. Mapping a logical ref onto the cluster's real `banking` schema is
therefore a physical-resolution decision Task 3/5 must make explicitly — it may **not** be read off
the ref.

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

**⚠️ `trace.py` exposed NO public event reader** — only `open_authoring_run` (`:156`), `append_event`
(`:173`) and `run_status` (`:212`). ~~A `read_terminal_event(conn, run_id)` must be ADDED~~ —
**DONE in Task 2**: `read_terminal_event` + `read_run_intent_hash`, both over the existing private
`_durable_read`. See §8.

`authoring_intent_hash(intent)` lives at `formula/authoring.py:253`.

**⚠️ CORRECTION (Task 2) — `authoring_intent_hash` does NOT cover the whole intent object.** It
hashes exactly four fields (`authoring.py:256-261`):

```python
{"name": …, "hypothesis": …, "target_entity": …, "target_grain_keys": [ … ]}
```

`AuthoringIntent.recipe_authoring_context` is **outside the digest**. §15 below asserted the
opposite; that claim is refuted here and struck through in place. The consequences invert:

- an intent reconstructed WITHOUT a populated `recipe_authoring_context` hashes IDENTICALLY and
  passes check 6 — it does **not** fail `INTENT_HASH_MISMATCH`;
- so Gate 1 **cannot** distinguish two intents that differ only in that field. That is a real (small)
  limit on what "the admitted intent is the intent that was authored" proves, and widening the digest
  would be a change to `authoring`'s identity contract, not to the gate. Pinned by
  `test_admission.py::test_check_6_cannot_see_recipe_authoring_context` so it cannot change silently.

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

**⚠️ ~~Consequence for Gate 1.~~ REFUTED at Task 2 — see the correction at the end of §14.**
~~`authoring_intent_hash` covers the intent object. A real run may populate
`recipe_authoring_context`, so an intent **reconstructed** by a caller without it hashes differently
and fails `INTENT_HASH_MISMATCH`.~~ The digest enumerates four fields and `recipe_authoring_context`
is not among them, so such an intent hashes the SAME and admits. Callers should still pass the real
object; the point is that Gate 1 does not enforce it.

**⚠️ Migration numbering moved.** `1021`–`1030` are now taken (`1021_contract_considered_revision` … `1030_recognition_evaluation`). **Next free: `1031`.** The plan's control-plane migration is renumbered accordingly.

**Not consumed by Spec A** (new modules from that stream, noted so they are not mistaken for dependencies): `formula/replay_authoring.py` · `replay_trace.py` · `recipe_authoring.py` · `recipe_egress.py` · `frozen_configuration.py` · `control.py`. Changed but not depended on: `audited.py`, `author.py`, `critic.py`.

**Re-verify again immediately before implementation begins** — that stream is active.

---

## 16. Canonicalization — `formula/_jcs.py` (entered during Task 1)

Task 1 cited `featuregen.formula._jcs.dumps` as already verified here; it was **not present**. Read and entered per the standing rule.

```python
dumps(obj) -> bytes                                # _jcs.py:195  — canonical UTF-8, NOT str
dump(obj, sink: IO[bytes]) -> None                 # _jcs.py:206
class CanonicalizationError(ValueError)            # _jcs.py:62   — base of all canonicalization errors
class IntegerDomainError(CanonicalizationError)    # _jcs.py:70   — |n| > 2**53 - 1
class FloatDomainError(CanonicalizationError)      # _jcs.py:83   — NaN / ±inf
```

The module is **vendored VERBATIM** from `trailofbits/rfc8785.py` v0.1.4 (Apache-2.0) and proven against the RFC 8785 test vectors in `tests/featuregen/formula/test_jcs_vectors.py`. `pyproject.toml` per-file-ignores its lint findings deliberately — **it must not be edited**.

**⚠️ `dumps` returns `bytes`.** Hash the bytes directly (`hashlib.sha256(dumps(x)).hexdigest()`); `.encode()` on the result is a `TypeError`.

**⚠️ Type dispatch is `isinstance(obj, dict)`, not `Mapping`** (`:245`). A `Mapping` that is not a `dict` (`MappingProxyType`, a custom mapping) falls through to the final `else` and raises `CanonicalizationError("unsupported type: …")` (`:272`). A function accepting `Mapping` must convert with `dict(payload)` first — `materialize.canonical` does.

**Accepted values** (`dump`, `:206-272`): `None` · `bool` · `int` within ±(2\*\*53 − 1) · `str` · finite `float` · `list`/`tuple` · `dict` with **string keys only** (a non-string key raises `CanonicalizationError("object keys must be strings")`, `:259`). `Enum` is not special-cased: a `(str, Enum)` member serializes through the `str` branch (the code deliberately avoids `str(...)` coercion, `:218-221`), but any other `Enum` hits the unsupported-type branch. Serialize `.value` explicitly.

**Object key order** is the UTF-16BE encoding of the key (`:256`), so mapping insertion order never affects the bytes.

**Precedent:** `formula/canonical.py:75-81` — `formula_content_hash` is `sha256(_jcs_dumps(plain)).hexdigest()`. Spec A's `materialize.canonical.materialize_hash` is the same construction over a plain mapping, and is the ONE hasher for `src/featuregen/materialize/`.

---

## 17. Logical refs are SCHEMA-FLATTENED — the physical schema is separate

Found during Task 2; invalidates an assumption that ran through spec revs 1–5.

```python
_SCHEMA = "public"            # graph.py:20 — EVERY object_ref is written under this
```

`build_graph` flattens every `object_ref` to `public.<table>[.<column>]`. **The real declared schema
survives only in `graph_node.schema_name`** (`1000_graph_node_schema_declared.sql:6,17` — "the REAL
(pre-flatten) schema the upload declared", **nullable**: a schema-less or generic glossary leaves it
`NULL`).

The existing resolver to REUSE rather than reinvent (`column_authority.py:66-77`):

```python
logical_ref_of(conn, catalog_source, object_ref) -> str
# splits object_ref -> (table, column), SELECTs schema_name from graph_node,
# falls back to "public" when it is NULL, then normalize_ref(source, schema, table, column)
```

**Consequences for materialization:**

1. A logical ref's schema segment is **catalog-side**, not necessarily the physical Hive schema. Refs
   may legitimately read `hdfc::public.transactions.amount` even though the table lives in `banking`.
2. **Physical schema resolution is an explicit step** — read it from the governed catalog, never parse
   it out of the ref, and never assume the ref segment is the Hive schema.
3. `schema_name` being nullable means resolution can fail; that must refuse
   (`PHYSICAL_SCHEMA_NOT_RESOLVED`), not silently fall back to `public` and read the wrong table.
4. `AMBIGUOUS_TABLE_NAME` is still needed but for a different reason than spec §3.1 claimed: the
   ambiguity is not "two schemas appear in the refs" (they cannot — everything is flattened to
   `public`) but "two resolved physical schemas contain the same table name". The upload path cannot
   currently produce the former, so a test written against it would be unreachable rather than
   discriminating.

---

## 18. Governed ENTITY + governed GRAIN — what a spine declaration can be validated against

Entered during **Task 4**. Spec §4 says `ENTITY_ASSIGNMENT`, `GRAIN` and `AVAILABILITY_TIME` may
*reject* a spine declaration; these are the reads that make that possible, and the one place the
reads cannot see what §4 assumes.

### 18.1 Governed entity — `overlay/upload/entity.py`

```python
GOVERNED_ENTITY      = "governed"                 # :39  a VERIFIED entity_assignment
LEGACY_FILE_DECLARED = "legacy_file_declared"     # :40  a legacy 'applied' suggestion
FILE_DECLARED        = "file_declared"            # :41  the raw file-declared entity

@dataclass(frozen=True, slots=True)
class EntityRead:                                  # :216
    entity: str | None
    authority: str | None      # one of the three above, or None

effective_entity(conn, catalog_source: str, object_ref: str) -> EntityRead   # :224
```

`object_ref` is the **public-flattened** graph_node key (`public.<table>.<column>`), not a
`logical_ref`. Governed **iff** `graph_node.entity IS NOT NULL AND entity_status = 'VERIFIED'`
(migration `1015_semantic_binding_projection.sql:58-64` adds `declared_entity`, `entity_fact_key`,
`entity_fact_event_id`, `entity_status` with `CHECK (entity_status IS NULL OR = 'VERIFIED')`).

**⚠️ `build_graph` writes `graph_node.entity` straight from the upload**, so a CSV column tagged
`entity=customer` reads back as `FILE_DECLARED` — a display tag, not an attestation. Only
`semantic_bindings.projection._apply_verified_entity` (`:144-186`) sets `entity_status='VERIFIED'`.
A spine validator that accepted a non-`GOVERNED_ENTITY` read would let an upload's own spreadsheet
column validate the definition of the customer population.

**⚠️ CASE-MATCHING ASYMMETRY between the two governed readers.** `effective_entity` matches
`object_ref = %s` **exactly** (`:230-233`), while `column_authority._scalar` (`:91-97`) matches
`lower(object_ref) = %s`. `graph.build_graph` writes `object_ref`, `table_name` and `column_name`
with the **upload's own casing** (`_table_ref`/`_column_ref`, `:148-153` — no `_norm`), whereas a
canonical `logical_ref` is case-folded by `object_ref._norm`. So for a catalog uploaded as
`Customers.CIF_ID`:

* `read_column_facts` / `read_operational_value` find the node (they fold);
* `effective_entity` does **not**, and returns `EntityRead(None, None)` — indistinguishable from
  "no governed entity".

A caller must therefore address `effective_entity` with the ref the catalog **stored**, not one it
rebuilt. `materialize/spine.py` reads `object_ref` back from `graph_node` in the same statement that
checks existence and read scope, and threads that value in; pinned by
`test_spine.py::test_a_MIXED_CASE_catalog_is_the_same_catalog`.

### 18.2 Governed grain / availability — `overlay/upload/operational_facts.py`

```python
read_operational_value(conn, logical_ref: str, field_name: str) -> OperationalValue   # :424
_FACT_FIELD_TYPE = {"is_grain": "grain", "is_as_of": "availability_time"}             # :118-121
```

For those two SPECIALIZED_FACT fields authority is **not** the decision log: governed iff the flat
flag is true **and** the `*_fact_event_id` link is non-null (`column_authority._FACT_EVENT_COLUMN`),
surfacing as `status == "resolved"`. The value is a **string**: `column_authority._render` renders
BOOLEAN columns as `"true"`/`"false"`, so the governed test is
`status == "resolved" and value == "true"`.

**GATE 3 runs first and fails closed.** `check_projection_readiness`
(`feature_metadata_snapshot.py:130-166`) raises when the `overlay` projection has no checkpoint, is
degraded, or sits below the event head; `read_operational_value` converts that to
`status="projection_unavailable"` with `value=None`. Every governed read in a database whose overlay
projection is not healthy therefore degrades — which is why the materialize fixtures seed the drift
watermark.

### 18.3 ⚠️ The GRAIN fact's `is_unique` is NOT projected anywhere

`facts.py` gives `GRAIN` the value schema `{"columns": [...], "is_unique": bool}` (§4 above), but
`table_fact_projection.project_table_facts_for_ref` (`:69-84`) reads **only** `grain.value["columns"]`
and sets `is_grain = true` on them. `is_unique` is never read, never stored, and no read reachable
from `graph_node` can distinguish a `is_unique=true` grain from a `is_unique=false` one.

Two consequences, both load-bearing for §4:

1. **Spine uniqueness must be derived from the SET of governed grain columns**, compared against the
   declared keys and what the `SnapshotPolicy` collapses — not from `is_unique`. That is what
   `materialize/spine.py` does.
2. A governed `GRAIN` fact asserting `is_unique=false` projects **identically** to a unique one. That
   is a fail-open in the projection, recorded here rather than worked around: recovering the raw fact
   would mean `resolve_fact(conn, adapter, …)`, which needs a registered `CatalogAdapter`
   (`catalog.current_catalog_adapter` raises `RuntimeError` when none is registered) and would be a
   second, unguarded opinion about authority — exactly what `materialize/joins.py` refuses to do.

### 18.4 Ref form in the Task 4 tests

Spec §4's examples write `hdfc::banking.customers`; per §3.5 and §17 that is shorthand for "the ref
for the customers table", never a physical schema. The real ref an upload produces is
`hdfc::public.customers`, and that is what `tests/featuregen/materialize/test_spine.py` declares.
Physical resolution onto `banking` is Task 5's explicit governed step.

---

## 19. Catalog state — `overlay/catalog_changes.py` + the planner's stamp vocabulary

Entered during **Task 5**. Spec §3.3 puts `catalog_state_stamp` INSIDE `PhysicalInputRequirement`'s
identity, so what a stamp is made of decides whether a compiled artifact is stable.

```python
drift_watermark(conn, catalog_source) -> datetime | None    # catalog_changes.py:24
drift_head_seq(conn, catalog_source)  -> int | None         # catalog_changes.py:34
```

Both read the single `overlay_drift_watermark` row for the source; **both return `None` when no
drift scan has ever completed** — absence is a real state, not zero. `head_seq` is
`COALESCE(max(global_seq), 0)` at scan completion (`_write_watermark`, `:44-56`), i.e. a **monotone
global event counter**, while `last_completed_at` is the wall-clock time the scan ran.

The vocabulary already exists and must not be re-minted (`planner/contracts.py:77-83`):

```python
class CatalogStateStampKind(StrEnum):   # :77
    drift_watermark = "drift_watermark"

class CatalogOmissionReason(StrEnum):   # :81
    no_usable_state_stamp = "no_usable_state_stamp"     # what resolve_catalog_scope records
    catalog_consideration_bound = "catalog_consideration_bound"
```

`CatalogStateStampV1` (`:323`) carries `catalog_source`, `head_seq`, `last_completed_at`,
`stamp_kind`, plus 3B.4's `compiler_input_fingerprint` / `projection_checkpoint`.
`scope.resolve_catalog_scope` (`scope.py:20-56`) OMITS a catalog with no watermark rather than
stamping it zero — the shipped precedent for "no watermark is not head_seq 0".

**Consequences for materialization.**

1. A materialization stamp may include `head_seq` (identity-bearing: the catalog moved) but **not**
   `last_completed_at` — re-running the projection over an unchanged catalog would otherwise change
   every compiled identity, the same defect as putting an inventory's `captured_at` in.
2. `CatalogStateStampV1` itself is therefore NOT the right shape to embed in
   `PhysicalInputRequirement`: two of its fields are observation provenance. §3.3 types the field as
   `tuple[tuple[str, str], ...]`, and `materialize/inputs.py` fills it with
   `(("stamp_kind", …), ("head_seq", …))`, reusing the enum values above rather than new strings.
3. `head_seq` is **catalog-wide**, not per table. An unrelated upload to the same catalog source
   moves it and therefore moves every requirement's identity. That is the conservative direction
   (recompile when the governed catalog moved) and is recorded here so it is a known cost rather
   than a surprise.

---

## 20. ⚠️ Task-5 gaps in the Spec-A plan's own Task-0 sketch

Two fields the plan's `TableLayout` / `PartitionMappingV1` sketch omits, both of which Task 5's
OWN stated tests require. `materialize/inventory.py` defines them; Task 0 must load and capture them.

1. **`TableLayout.columns: tuple[tuple[str, str], ...]`** — the data columns as (name, physical
   type). The plan's T5 test `test_changing_layout_or_a_physical_type_changes_the_fingerprint` and
   spec §3.3's "SEMANTIC: partition columns+types, **physical types**, mapping" both need them, and
   they cannot come from the catalog: `graph_node.data_type` is the LOGICAL representation a
   decision governed (interfaces §3), not the cluster's `decimal(18,2)`.
2. **`AvailabilityPartition.late_arrival_days: int`** — §3.4's prose requires an availability
   mapping to "extend the partition set beyond the event window", but its field parenthetical
   `(time_ref, partition_column, transform, timezone)` gives run preparation nothing to extend it
   BY. An inferred widening is exactly the inference §3.4 forbids, so the amount is declared and
   identity-bearing.

Also entered: `transform` is given a CLOSED `PartitionTransform` StrEnum (`date_iso`,
`date_compact`). The spec names the field without a vocabulary; an open string would be an
ungoverned format applied against a live metastore, whose wrong answer looks like an empty
partition rather than an error.

---

## 21. ⚠️ `AVAILABILITY_TIME`'s `basis` / `lag_hours` are NOT projected anywhere

Entered during **Task 6**. The same shape of gap §18.3 records for `GRAIN.is_unique`, and equally
load-bearing: spec §8 rule 1 renders the point-in-time gate *per* `AVAILABILITY_TIME.basis`, plus
`lag_hours` for `event_time_plus_lag`.

`table_fact_projection.project_table_facts_for_ref` (`:82-89`) reads **only** `avail.value["column"]`
and writes `is_as_of = true` + `availability_fact_event_id` on that one column. `graph_node` has no
basis and no lag column (`0945_graph.sql:15`, `0986_graph_node_table_fields.sql:10`), and
`read_operational_value(conn, ref, "is_as_of")` therefore returns only `"true"`/`"false"` plus the
fact key and event id. **The basis a governed availability fact declares is unreachable from every
projected read.**

Task 6 resolves it by **dereferencing the link the projection wrote**, not by re-deciding authority:

```python
read_operational_value(conn, ref, "is_as_of")     # AUTHORITY (incl. GATE 3 projection health)
overlay_fact_state WHERE fact_key = fact_key(table_ref(source, table), "availability_time")
#   -> status must be 'VERIFIED'                  (the only servable overlay status)
#   -> confirmed_event_id must EQUAL graph_node.availability_fact_event_id
#   -> value["column"] must be the flagged column (catches a stale projection either way)
#   -> validate_fact_value(AVAILABILITY_TIME, value)  (the overlay's own schema)
```

`resolve_fact` is deliberately NOT used: it requires a registered `CatalogAdapter`
(`catalog.current_catalog_adapter` raises when none is registered) and re-decides authority via
catalog precedence, expiry and drift — a second, possibly disagreeing opinion about a question the
projection already answered, which is what `materialize/joins.py` refuses to do for join paths.

Two consequences recorded rather than worked around:

1. An **authoritative `CatalogAdapter`** value cannot reach this path at all. `_catalog_verified`
   (`resolve.py`) sets `provenance={"catalog_source": …}` with no `confirmed_event_id`, so
   `table_fact_projection` writes NULL to `availability_fact_event_id`, `read_column_facts` reports
   `authority="hint"`, and compilation refuses `AVAILABILITY_TIME_NOT_GOVERNED`. Fail-closed, but it
   means a catalog-authoritative availability fact is unusable for materialization today.
2. **Expiry and drift-freshness are as strong as the shipped projection, no stronger.** An expired
   fact's flat `is_as_of` flag stays true until the projection re-runs, and `read_operational_value`
   grants "resolved" on flag + link alone. Task 6 inherits that posture unchanged rather than
   inventing a second expiry clock at generation time.

**Also entered: `formula.canonical.filter_plain(node, path)` is now PUBLIC** (it was the private
`_filter_plain`, `canonical.py:234`), for the reason `join_path.table_of_ref` was made public in
Task 3 — an adapter must ask the question the canonical form already answers.
`materialize/expression_ir.py` carries a per-expression `filter_tree` into `expression_ir_hash`, and
a second rendering there could disagree with `formula_content_hash` about what the filter is.

## 22. What Task 6 established

* **One `PitSpec` per EXPRESSION.** `compile_expression(conn, *, expr_path, expr, grain_keys, roles,
  inventory)` compiles ONE `AggregateExpression`; `ExpressionExecutionIR` is complete on its own, so
  a ratio's two halves may read two tables, two event-time columns, two windows and two availability
  bases (interfaces §6). Body paths come from `formula/schema.py::_body_expressions`' own vocabulary
  (`BODY_PATHS`), never re-minted.
* **`PitSpec` excludes `empty_window` / `null_input`** — spec §8 rule 4 assigns those to the
  formula's own policies, so a type named "PIT" must not carry them.
* **Child-1's containment rule is VERIFIED, not assumed.** `schema._require_contained_column`
  constrains the operand, the event-time ref and every filter `left` to the expression's own
  `source_relation`; that is the only reason per-column physical resolution is unnecessary, so
  `compile_expression` checks it and refuses `UNACCOUNTED_LOGICAL_REF` rather than recording an
  off-relation column under the SOURCE table's physical identity.
* **Reference completeness (§2.1)** is `logical_refs_in(node, path)` — an exhaustive dataclass /
  sequence walk yielding `(location, ref)` for every string `parse_ref` accepts. Non-physical slots
  are keyed by the dataclass they sit on: `TypedLiteral.value` (`COMPARISON_LITERAL` — a predicate
  has no `right_ref`, so a ref on the right is data) and `ParameterRef.name` (`RUN_PARAMETER`).
  Refusals name the LOCATION, never the string, because an unconsumed ref-shaped slot may hold data.
* **Identity excludes join provenance and roles.** `identity_payload()` takes the join plan's
  `(from_ref, to_ref, cardinality)` steps only; `approved_join_fact_key`, `approved_join_status`,
  `authority` and `roles_used` record why the traversal was allowed and who asked, and neither
  changes a row. Including them would split one computation into as many artifacts as there are
  approvers and readers. `non_physical_refs` is likewise recorded but not identity — it explains a
  decision about the formula rather than feeding the computation.
* **`input_requirements` order is stated by the plan**, not by resolution order: the join TARGET
  must be resolved before the path can be planned, so resolution order would put the destination
  ahead of the hops reaching it — and the order enters the hash.
* **Open (spec ambiguity, not resolved here):** §3's IR has ONE `join_plan`, so a grain whose keys
  sit on two tables off the source relation would need two traversals with two independent fan-out
  verdicts. Task 6 refuses that with `GRAIN_PATH_NOT_GOVERNED` (the closest governed reading: there
  is no single governed path to *the* grain) rather than widening the field. §8 rule 1 likewise
  specifies ONE availability gate per expression, so a joined dimension table's own availability is
  not gated in this slice — recorded as a known bound, not designed around.
