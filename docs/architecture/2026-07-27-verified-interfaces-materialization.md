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

---

## 23. What Task 7 established

**Two helpers made PUBLIC, for the reason `filter_plain` and `table_of_ref` were before them** — an
adapter must ask the question the owning module already answers, or the two answers can drift:

```python
formula.schema.body_expressions(body) -> tuple[tuple[str, AggregateExpression], ...]  # was _body_expressions
materialize.expression_ir.join_key_ref(catalog_source, step_ref) -> str               # was _key_ref
```

`materialize.ir.compile_ir` compiles ONE `ExpressionExecutionIR` per body path, and a second
enumeration there could disagree about how many expressions a body has or what each is called — the
path names the staging output every later stage reads. `join_key_ref` is shared because Gate 2
authorizes each join endpoint as its own element class: an endpoint addressed by a second conversion
would authorize one node and read another. (`capability.py` and `critic.py` keep their own private,
differently-shaped `_body_expressions`; they return expressions without paths and are untouched.)

**Gate 2's union is derived from FOUR structural sources, deliberately overlapping.** Task 6 already
folds join endpoints and the availability column into `physical_read_set`, and Task 4 builds
`SpineSpec.read_set` as keys + availability + policy columns — so in a healthy artifact several
sources name the same node. §1.3 nevertheless names them as separate element classes, so each is
derived from its own source (`physical_read_set`, `join_plan.steps`, `pit.availability_ref`, the
spine's `source_table_ref`/`ordered_key_refs`/`read_set`/`availability_ref`). The overlap makes the
obvious test non-discriminating, so the element-class tests hand Gate 2 a DOCTORED artifact with the
element removed from the overlapping source — each with a control proving the doctored group is
otherwise authorizable. Without that, five of these derivations survived mutation.

**"No contract, plan or project is produced" is only testable with a positive control.** The plan's
own sketch counted derived contracts after calling `authorize_compilation` alone, which cannot
derive one under any implementation — a vacuous assertion. `test_ir.py` runs §2's chain end to end
against a downstream double that REFUSES anything but an `AuthorizedCompilation`, asserts the
untagged group really does reach it (2 contracts, 1 project), and only then asserts zero.

**Gate 2 owns the read-scope axis ONLY.** `graph_node.sensitivity` vs `allowed_sensitivities(roles)`
(§2/§13 above). `effective_restriction` is §5.2's axis and refuses with `PROHIBITED_INPUT` during
classification — a `prohibited` restriction with no read-scope tag therefore passes Gate 2, which is
asserted rather than assumed.

⚠️ **Migration `0993_graph_check_constraints.sql:22-24` constrains `graph_node.sensitivity` to
exactly `SENSITIVITY_ROLES`' keys** (`'pii' | 'restricted' | NULL`). The fail-closed branch for an
unknown tag is therefore unreachable through the database, and is a guard rather than a tested path;
the tested property is that each tag needs its OWN granting role.

**Two bounds and one spec ambiguity, recorded rather than worked around:**

1. **A ref with no `graph_node` row is treated as untagged, i.e. authorized.** It carries no tag to
   hide behind, so nothing sensitive passes; what it is, is a read of a column the catalog does not
   describe, which §11's L1 reports as `COLUMN_ABSENT` against the live metastore. Refusing it here
   would report a missing column as an insufficient role. Note the wider gap: **nothing in
   compilation verifies that a formula's column refs exist as catalog nodes** — Task 6 resolves the
   TABLE, not the columns — so L1 is the first place a typo'd column is caught.
2. **Group-assembly errors raise `ValueError`, not a §14 code**: an empty group, an IR compiled
   against a different spine declaration than the one supplied, and an IR carrying join steps with an
   empty read set. None is a governed verdict about an artifact (the line `plan_join` and
   `admission.FeatureNamePlanError` already draw), and the closed vocabulary has no member for them.
3. **§14 has no code for "the formula's grain entity is not the declared population's entity."**
   `GRAIN_PATH_NOT_GOVERNED` is used as the closest governed reading — there is no governed path from
   this feature's grain to that population — the same reading Task 6 took for a two-table grain.

**`compile_ir` validates the spine declaration on every call**, so a group of N features performs N
validations. §4's "declared once per materialization contract" is enforced where the group actually
exists: `authorize_compilation` refuses (`ValueError`) to authorize IRs whose spine identity payload
differs from the supplied spine's.

**`ir_hash` payload.** Identity: feature name, `formula_content_hash`, final operation,
`zero_denominator`, grain entity + ORDERED keys, every expression keyed by BODY PATH (sorted — never
tuple position, the same defect Task 6 found one level down), the spine's semantic payload, the
carried output policy. Excluded: `authoring_run_id`, the declaration's provenance, `roles_used`, and
every run-time value. The formula-side fields are also inside `formula_content_hash`; they are
repeated because the IR is read on its own, and an identity only interpretable by fetching the
formula would push every reader back to the object the IR summarizes.

---

## 24. What Task 8 established

> ⚠️ **The two paragraphs marked below are SUPERSEDED by Task 8.1 — see §26.** The formula's logical
> word is no longer read at all, the signature is now
> `resolve_physical_type(formula, *, operand_types=…)`, and the "invisible to this check"
> consequence is closed. Everything else in this section stands unchanged.

**The operation decides the physical type; the logical word is read as OPERAND EVIDENCE only.**
*(SUPERSEDED in part — §26: the word is now read for nothing.)* `resolve_physical_type` keys §6's
table on the body shape and, for a unary body, on `body.expr.aggregation` — never on
`FormulaOutputPolicyV1.output_type`. Under Task 8 the word was consulted for exactly one question —
is the operand an EXACT numeric? — because Child-1 resolves it to the operand's governed
`logical_representation` verbatim (`output_authority._numeric_output_type`), making it the only
visible statement about what is being summed. Task 8.1 replaced that single statement with
per-EXPRESSION evidence.

| body / aggregation | published `sql_type` | `rounding` / `overflow` |
|---|---|---|
| `COUNT_ROWS` · `COUNT_NON_NULL` · `COUNT_DISTINCT` | `BIGINT` | `None` / `None` |
| `SUM` | `DECIMAL(p,s)` from `DecimalPolicy` | carried from the policy |
| `RATIO` | `DECIMAL(p,s)` from `DecimalPolicy` | carried from the policy |
| `DIFFERENCE` | `DECIMAL(p,s)` from `DecimalPolicy` | carried from the policy |

**Nullability is part of the type**, from the formula's own policies (§8 rule 4) and never from the
SQL type — a `BIGINT` count over a window declared `NULL` when empty is a nullable column. Three
sources, ANY of which makes the column nullable: `EmptyWindowResult.NULL` or `NullInput.PROPAGATE`
on **any** expression (each `AggregateExpression` owns its own window, so a ratio has two), and
`ZeroDenominator.NULL` on a ratio. **`NullInput.PROPAGATE` is a third source §6 does not list**: a
null operand VALUE makes the aggregate null on a NON-empty window, and declaring such a column
non-null is the direction of that decision that produces a broken write rather than a refusal.

**The word describes ONE operand, and §6 does not say which.** *(The finding stands; its consequence
is CLOSED — §26.)* Child-1 derives it from the SUM's own operand, or from a DIFFERENCE's **minuend**
(`_resolve_difference`); a RATIO's word is the constant `"decimal"` and describes no operand at all.
A count has no operand type to read, so its word is `"unknown"` for a benign reason. That is exactly
why Task 8.1 stopped reading it: ~~a ratio's numerator/denominator and a difference's subtrahend are
invisible to this check~~ — they are now each checked against their own governed type.

**The decimal policy is validated exactly where it governs.** Refusals: `SATURATE` (a deferred NFR
— nothing in this slice clamps), precision outside `1..38`, scale outside `0..precision`. Note
`schema._check_decimal` permits `precision=0`, so a zero-width decimal is a real input rather than a
hypothetical. A count publishes `BIGINT`, so its `DecimalPolicy` reaches no rendered expression and
is **neither validated nor carried** — pinned by a test, with the obligation that the renderer takes
rounding/overflow from `PhysicalType`, never from `formula.decimal`.

**`RoundingMode` and `OverflowBehavior` are carried on `PhysicalType`**, because §6 requires both to
be explicit in generated code and a renderer cannot honour what it never receives — Spark's default
on decimal overflow is a NULL, so `ERROR` is deliberate work, not a default.

**`PHYSICAL_TYPE_POLICY_VERSION`** is a declared constant (`1` under Task 8; **`2` since Task 8.1**,
which changed the operand rule — §26). `PhysicalType.identity_payload()`
carries `sql_type`, `nullable`, `rounding`, `overflow` and deliberately NOT the policy version — the
version is a property of the whole plan, contributed once by the contract (§5.5), not once per
column.

**A body outside Child-1's union raises `SchemaError` from `schema.body_expressions`, not from a
second check here.** `resolve_physical_type` calls it first and every return depends on the
nullability it feeds, so the gate is structural rather than positional. (An earlier draft duplicated
the check locally; mutation testing showed the duplicate was unobservable.)

---

## 25. What Task 9 established

**The two axes are real, and both branches of §2 were verified against the shipped code rather than
remembered.**

| Question | Column | Read through | Verified |
|---|---|---|---|
| `sensitivity_class` | `graph_node.effective_restriction` | `safety_floor.apply_sensitivity_floor` | `safety_floor.py:78-90` — `_rank`/`_normalize` map any label outside `SENSITIVITY_ORDER` to `prohibited`, so an unrankable value can never sort below the floor and is never returned verbatim |
| `access_requirements` | `graph_node.sensitivity` | `read_scope.SENSITIVITY_ROLES` | `read_scope.py:13` — `{pii: pii_reader, restricted: restricted_reader}` |

**`restricted` is a legal value of BOTH columns**, and that overlap is what makes a conflation hard
to see: a resolver reading the TAG column for the class returns a legal-looking `restricted` where
the truth is `public`. `test_the_word_restricted_lives_in_BOTH_vocabularies_and_means_different_things`
is the discriminator, and independence is proved by a 2×2 that moves each axis with the other held
fixed rather than by one fixture where both happen to agree.

**The unknown-label path is REACHABLE, checked rather than assumed.** Migration `0993` constrains
`graph_node.sensitivity` to exactly `('pii','restricted')` (a test now demonstrates the
`CheckViolation`), but it puts **no** constraint on `effective_restriction` — nothing in the schema
stops an unrankable label reaching that column, so normalize-then-refuse is a live path.
`field_resolution._resolve_sensitivity` writes it already normalized, so the guard defends against
every OTHER writer, not against that one.

**`CLASSIFICATION_POLICY_VERSION = 1` states the missing-classification policy: an unclassified
element is `internal`.** A column with no `effective_restriction` (NULL, blank, or no `graph_node`
row at all) is not proven `public` — nobody attested it — and is not `prohibited` either, because
"nobody classified this" and "a governed decision forbids this" route an operator to two different
people. `Classification.unclassified_refs` carries which elements fell to the policy so the
assumption is visible; it is deliberately NOT identity-bearing, since a column later classified at
the value the policy already assumed describes the same artifact.

**A contract is derived per feature over `physical_read_set((ir,), ir.spine)`** — the §1.3 union,
now exposed from `ir.py` rather than re-walked, so the classification cannot see a narrower read set
than Gate 2 authorized. `authorize_compilation` and `physical_read_set` share one `_sorted_refs`
derivation. The spine and any join endpoint can therefore decide a feature's class.

**§5.5's hash, as built:** `entity`, `ordered_keys`, `pit_semantics`, `sensitivity_class`,
`access_requirements`, `retention_class`, `retention_policy_version`, `availability_class`,
`cadence`, `publication_policy`, `backfill_boundary`, `spine` (its `identity_payload()` only), and
all three policy versions — asserted as a key-set EQUALITY, never a superset. The landing keys are
the **spine's** `ordered_key_refs`, not the formula's grain keys: the published row is one per
population key per `business_dt`, and a feature's grain columns may be another table's spelling of
the same entity (`compile_ir` already refuses a different entity).

**Three exclusions that are decisions, not omissions.** The **calculation window** (§5.3 — a 30d and
a 90d feature share a group). The **resolved physical type** — only `PHYSICAL_TYPE_POLICY_VERSION`
is in the contract; a contract carrying `DECIMAL(38,6)` would give a `BIGINT` count and a `DECIMAL`
sum different contracts, and the "one group" of this slice could never contain both. The
**availability LAG** — the basis (`posted_at` / `ingested_at` / `event_time_plus_lag`) IS part of the
landing semantics because it changes what "known by the cutoff" means, but the lag's magnitude only
changes which rows qualify, which is an expression-IR fact already hashed there.

**Where §14 has no code, this task raised rather than borrowed one.** §14's closed vocabulary has no
member for a malformed *declaration*, so an unknown cadence trigger/period, a non-IANA timezone, a
cutoff that is not a wall-clock time (or that carries its own UTC offset), an override naming an
unrankable class, and an override that LOOSENS the derived classification all raise `ValueError` at
the declaration boundary — the line `authorize_compilation` already draws for a call assembled
wrongly. The one governed verdict is `PROHIBITED_INPUT`, returned whether the top rank came from the
catalog or from a declared override.

**Cadence and the landing semantics deliberately carry the same cutoff twice** (as the schedule and
as the key's meaning). Both are copied from one `CadenceDecl`, so they cannot disagree; each is
pinned by its own key-set test, because mutation testing showed that dropping either one alone left
the contract hash unchanged.

**`AvailabilityClass` is invented here** (`same_day` / `next_day` / `best_effort`): §5.4 requires it
declared and names no vocabulary, and deriving one needs the deferred source-delivery SLA. Recorded
as a spec gap rather than presented as governed.

---

## 26. What Task 8.1 established

**"Numeric" and "exact-numeric" are different questions, and the obvious fix is wrong.** Verified
against the source rather than remembered: `_NUMERIC_LOGICAL_TYPES` (`b_output_policy.py:65`,
`output_authority.py:59`) contains `float`, `double`, `double precision`, `real` **and** `money`. So
`_is_numeric_logical_type` passes a float ratio, which then publishes `DECIMAL(38,6)` — a fixed-point
column asserting a reproducibility float arithmetic does not have. Only an **exact** numeric may back
a decimal column, and a test pins each member of §6's REFUSED set as simultaneously *in* the
catalog's numeric set and *out* of the allowlist, so the wrong fix is a failing test.

```
_EXACT_NUMERIC_LOGICAL_TYPES = {numeric, decimal, integer, int, int2, int4, int8, bigint, smallint}
REFUSED (all in _NUMERIC_LOGICAL_TYPES)  = {float, double, double precision, real, money}
```

`int2` is the single addition to §6's spelled set — PostgreSQL's alias for `smallint`, as
`int4`/`int8` (which §6 does list) are for `integer`/`bigint`. Recorded in `DEFERRED-WORK.md` A.9.

**The evidence is per EXPRESSION, carried by the IR.** `ExpressionExecutionIR.operand_type` is an
`OperandTypeEvidence(operand_ref, status, logical_type, read_status)` read in `compile_expression`
through the shipped governed reader — `read_operational_value(operand, "logical_representation")` —
never off the flat `graph_node.data_type` that reader's hash gate exists to verify. **That module
records and never judges:** the read happens after every refusal (a refused plan performs no extra
catalog read) and no type condition refuses there.

| `OperandTypeStatus` | C1 read | `logical_type` | §6 verdict for an ARITHMETIC operand |
|---|---|---|---|
| `GOVERNED` | `resolved` | the governed word | exact ⇒ publish · otherwise ⇒ refuse |
| `UNGOVERNED` | `no_decision` / `no_value` / `not_operational` / `retired` / `conflict` | `None` | refuse — "nobody attested this type" |
| `UNAVAILABLE` | `fork` / `hash_mismatch` / `projection_unavailable` | `None` | refuse — "the type could not be READ" |
| `NO_OPERAND` | not attempted | `None` | n/a — `operand is None` IFF `COUNT_ROWS` [c9] |

**Only a governed read carries a type.** There is no slot for an unattested word, so the
"ungoverned but numeric" path that published fixed-point on a file declaration no longer exists.
This closes `DEFERRED-WORK.md` A.9's `external_type_required` row and is a real behaviour change.

**`resolve_physical_type(formula, *, operand_types: Mapping[str, OperandTypeEvidence])`** validates
**every** arithmetic operand — a ratio's numerator AND denominator, a difference's minuend AND
subtrahend — keyed by body path. Counts are exempt (a count is integral whatever its operand holds,
so the operand is not arithmetic); their evidence is carried and simply not consulted. The mapping
is **required, with no default**, and its key set must equal the body's paths with each entry's
`operand_ref` equal to that expression's operand — checked BEFORE the count short-circuit, or
mismatched evidence would pass unnoticed for every `BIGINT`. A mismatched call raises `ValueError`
(the line Task 9 drew for a malformed call); §14 has no member for it.

**One code, three diagnoses — and the separation is the point.** All three refusals carry
`PHYSICAL_TYPE_UNSUPPORTED`, because §14 is closed and holds exactly one member for a typing verdict
(`NOT_RESOLVED` is admission's terminal-disposition code; `AVAILABILITY_TIME_NOT_GOVERNED` is the
availability fact's — borrowing either would double-book a code and leave a handler unable to
route). The three branches are separate in code, in `OperandTypeStatus`, and in the refusal detail,
with a test asserting the messages do not overlap. **`UNGOVERNED` and `UNAVAILABLE` carry no type,
so they are also absent from the allowlist: a single check would refuse them anyway, blaming a type
nobody could read.** The remaining gap — a caller switching on `.code` alone cannot distinguish them
— is recorded in `DEFERRED-WORK.md` A.9 as a §14 decision for a human.

**`operand_type` is deliberately NOT in `identity_payload()`.** It decides whether the expression may
be PUBLISHED, not what it computes: a governed `numeric` and a governed `bigint` operand read the
same rows and add them the same way, so hashing the difference would split one computation into two
artifacts, and a projection blip would change `ir_hash` and fire §9's `IR_HASH_MISMATCH` against a
computation nobody touched. The consequence — the resolved `PhysicalType` — enters `group_plan_hash`
on its own (§6).

**`PHYSICAL_TYPE_POLICY_VERSION = 2`.** The operand rule changed, so formulas that resolved to a
`DECIMAL` under version 1 can refuse under version 2 (a float denominator was invisible to the word;
an ungoverned operand type was accepted). A contract keyed on the old version describes a column
decided by a rule that no longer applies.

## 27. What Task 9.1 established

**`AvailabilityPromiseV1` replaced Task 9's invented `AvailabilityClass`, and A.10's 🔴 is closed.**
The promise is a value — `(kind, calendar_days, plus_minutes)` — so `T+0`, `T+5` and `T+1 plus 30
minutes` are all expressible without adding a member to anything. The old vocabulary entered the
contract hash, which made an arbitrary member list load-bearing: every group already keyed under it
would have re-keyed the day someone needed a value it did not spell.

**Non-canonical input is refused at construction; `normalized()` is a SEPARATE entry point.**
`AvailabilityPromiseV1(calendar_days=0, plus_minutes=1560)` raises;
`AvailabilityPromiseV1.normalized(calendar_days=0, plus_minutes=1560)` returns `(1, 120)`, which
hashes identically to a directly constructed `(1, 120)` — asserted on both
`materialize_hash(promise.identity_payload())` and `contract_hash(...)`. Normalizing silently inside
the main constructor would make two spellings of one promise both "work", leaving no call site able
to show which was meant, and group equality would then compare spellings.

**The bound `0 <= plus_minutes < 1440` is not decoration.** It is what makes `(calendar_days,
plus_minutes)` orderable as a plain tuple: once minutes may reach a whole day, `(1, 0)` and
`(0, 1440)` are the same instant and the tuple comparison deciding a monotonic override starts lying.

**`kind` is in the canonical payload from v1**, with one member (`CALENDAR_OFFSET`). That is the
whole forward-compatibility property: adding `BUSINESS_DAY_OFFSET` later must leave every existing
payload byte-identical, and it cannot if v1 omitted the field. **`T+N` means CALENDAR days in v1,
explicitly** — a banking-day promise is a different kind and additionally requires a governed
holiday-calendar identifier and version, and neither may ever be silently read as the other.

**Incomparable is a fourth VERDICT, not the absence of "later".** `compare_availability_promises`
returns `PromiseComparison.{EARLIER, SAME, LATER, INCOMPARABLE}`, and returns `INCOMPARABLE`
whenever `_comparison_basis` — `(kind, cadence.timezone, cadence.business_date_cutoff)` — differs on
the two sides. "T+3 at 23:59 Asia/Dubai" and "T+3 at 18:00 UTC" are the same three digits on
different clocks; equal components do **not** make two promises the same promise. Collapsing that
into "not later" would let a monotonic override succeed on a comparison that means nothing, so
`override_availability_promise` refuses instead — with a message that never claims an ordering.

**An earlier override is a `ValueError`, not a governed code — the Task 9 boundary, now settled by
ruling.** §14's vocabularies describe valid requests rejected by the catalog or by data state;
asking to promise earlier than what was derived is a bad request. `test_an_earlier_override_is_a_
CALLER_ERROR` asserts both directions: the exception is not a `MaterializationRefused`, and no
member of `CompilationRefusalCode` appears in its message.

**The kind's contribution to comparability cannot be shown behaviourally in v1** — one member means
two promises of different kinds cannot be constructed — so `_comparison_basis` is pinned by value
instead. It is pinned at all because the day a second kind arrives is the day a calendar-day promise
could otherwise be declared "later" than a banking-day one.

**An override carries no cadence of its own.** `ContractOverrides.availability_promise` is read
against the ONE cadence the derivation was given, so a promise cannot be re-based without
re-declaring the cadence; the two-cadence form of the rule lives in the public
`override_availability_promise`, which a later stage comparing a new declaration against an
already-persisted contract must use.

---

## 28. What Task 10 established

`materialize/group_plan.py` + `materialize/binding.py` — spec §9, §10.1, §10.2. Both modules are
**pure functions over values**: the `group_binding` tables land with the rest of the control plane
(T14), so nothing here opens a connection.

```python
SYSTEM_COLUMNS = ("__generation_id", "__generated_project_hash", "__sandbox_execution_hash")
SYSTEM_COLUMN_SQL_TYPE = "STRING"

ColumnRole(StrEnum)        # ENTITY_KEY · BUSINESS_DT · FEATURE · SYSTEM
StagingStatus(StrEnum)     # completed · failed
PlannedColumn(name, role, sql_type: str | None, nullable)
PlannedFeature(column_name, ir_hash, physical_type: PhysicalType)
FeatureGroupPlanV1(logical_group_name, materialization_contract_hash, entity_key_columns,
                   business_dt_column, features, physical_type_policy_version)
StagingManifestV1(...)     # §9's field list, `status` typed as StagingStatus
GateFailure(code: ValidationGateCode, detail)

build_group_plan(group: ContractGroup, features, *, logical_group_name) -> FeatureGroupPlanV1
group_plan_hash(plan) · expected_schema(plan) · expected_schema_hash(plan)
check_completeness(plan, manifests, *, generation_id, run_id, business_dt) -> tuple[GateFailure,...]

SANDBOX_NAMESPACE = "sandbox_feature";  physical_target_for(logical_group_name)
GroupContractBinding(binding_id, logical_group_name, materialization_contract_hash, physical_target)
GroupPlanRevision(binding_id, group_plan_hash, generation_id, created_at)
bind_group(plan, *, binding_id, existing=None) -> GroupContractBinding | MaterializationRefused
plan_revision(plan, binding, *, generation_id, created_at) -> GroupPlanRevision
current_plan_revision(revisions, *, published_generation_ids) -> GroupPlanRevision | None
```

**Completeness is proven by MANIFEST, not by schema.** `check_completeness` returns EVERY failure,
not the first, and asks its questions in this order: duplicates over the whole staging area →
**staleness over every manifest present, planned or not** → per planned feature (present,
`completed`, `ir_hash`-matching) → strangers bound to this run. Staleness precedes the `ir_hash`
because a manifest bound to another generation/run/date is not evidence about this run at all, and
grading its computation would report a verdict nobody asked for. `generated_project_hash` and
`sandbox_execution_hash` are deliberately NOT compared here — they have their own §9 gates, and
checking them twice under a staging code would report one failure under two names.

**The landing KEY and `business_dt` carry `sql_type=None`.** §6 decides the type of a FEATURE
column; nothing governed states `cif_id`'s physical type at compile time (the inventory carries
**partition**-column types only). Declaring one would be an invention the §9 gate then enforced
against a real table. Their nullability *is* decided (`False`): the published shape is one row per
`(keys…, business_dt)`, and a NULL landing key is not a landing key. **T14's gate must therefore
check type only where `sql_type is not None`.**

**A feature can never collide with a system column, structurally.** `admission.hive_identifier`
requires a leading letter; every system column begins `__`. The collision check still lists them, so
the guarantee survives a change to the normalizer.

**`admission.hive_identifier` is now PUBLIC** (was `_hive_identifier`). One normalizer, not two: a
second regex in the plan would be a second chance to disagree about which column a feature occupies,
and the disagreement would surface as a schema gate failing on a name nobody chose. It is
idempotent, so re-applying it to an admitted `feature_name` is a validation.

**The binding has no field that moves, and "current plan" is DERIVED.** Adding a feature changes
`group_plan_hash`, appends a `GroupPlanRevision` and returns the SAME binding object; only a
different contract hash (or a physical target that disagrees with the derived one) is
`PublicationRefusalCode.GROUP_BINDING_CONFLICT`, **returned** rather than raised. Publication success
cannot live on an append-only revision row — it is known only afterwards, from §12's folded run
events — so `current_plan_revision` takes the published generation ids as EVIDENCE. It refuses an
ambiguous tie (two published revisions at one instant, or two plan hashes under one generation)
rather than picking, and orders by INSTANT: `2026-07-27T23:00:00+05:30` sorts after
`…T19:00:00+00:00` as text and is 90 minutes before it in fact, so `created_at` must be
offset-aware ISO 8601.

**A binding for a different logical name is a `ValueError`, not a refusal** — the binding is fetched
BY that name, so the wrong one is a lookup bug rather than a verdict about the plan. The same line
separates `plan_revision`'s cross-group check from `bind_group`'s conflict.

**A.11's two-cadence override rule does not reach here.** The binding compares contract HASHES,
never promises: a different promise is a different contract hash and therefore a conflict, not an
override. Its first real consumer is T14's control plane or an API accepting a re-declaration.

---

## 29. What Task 11 established

`materialize/identity.py` — spec §7. Pure functions over values; no connection, no `pyspark`.

```python
GENERATED_LOCK_FILENAME = "GENERATED.lock"

CompilationIdentity(formula_content_hashes, ir_hashes,          # PLURAL, positionally paired
                    materialization_contract_hash, group_plan_hash)
RenderedArtifactIdentity(compilation, generated_project_hash)
SealedProject(identity, files)                                  # `files` is a read-only mapping

build_compilation_identity(irs, plan) -> CompilationIdentity
generated_project_hash(files: Mapping[str, str]) -> str         # EXCLUDES the lock, always
seal_project(compilation, files) -> SealedProject               # the ONLY writer of the lock
read_lock(document: str) -> RenderedArtifactIdentity            # the ONLY parser of it
derive_namespace() -> str                                       # no parameters
sandbox_execution_hash(rendered, *, environment_id, parameters, business_dt,
                       input_snapshot_ids, compiler_version, renderer_version,
                       capability_attestation_id) -> str        # nothing defaulted
```

**The exclusion is the whole non-circularity argument.** `generated_project_hash` skips
`GENERATED.lock` **whether or not it is present**, so it answers the same value for the files about
to be sealed and for the COMPLETE project L0 later verifies (§11.2). Each file contributes
`sha256` of its UTF-8 bytes under its path, so an edit, a rename, an addition and a deletion all
move it. `seal_project` is the only path that writes a lock and **refuses one the renderer
supplied**: such a lock either states a hash the renderer could not yet know, or feeds an older one
back into the bytes being hashed. A seal-time scan for the computed hash was deliberately NOT
added — a single-pass renderer cannot embed a hash it was never given, so the branch would be dead
code. **T14 therefore keeps its own assertion that no generated source file carries the literal**,
and reads `__generated_project_hash` from the lock at run time (§10.2).

**The two tuples are paired, not sorted independently.** `formula_content_hashes[i]` and
`ir_hashes[i]` are one feature's, so `__post_init__` neither re-orders nor de-duplicates them (two
features may be authored from one formula, and collapsing them would report a two-feature group as
a one-feature group). Order-independence comes from `build_compilation_identity` instead, which
emits both in the PLAN's order — already sorted by column name.

**`build_compilation_identity` closes a gap nothing else covered:** it recomputes `ir_hash(ir)` and
compares it with the `ir_hash` the plan carries for that column. `PlannedFeature.ir_hash` is a value
Task 10's *caller* supplies, and §9 gates every staging manifest against it, so a plan naming a hash
no IR produced would only have surfaced at run time as an `IR_HASH_MISMATCH` against a computation
nobody performed. The feature closure is the same one `build_group_plan` draws.

**`sandbox_execution_hash` takes the RENDERED identity**, not a compilation identity plus a project
hash, so a caller cannot pair one compilation with another project's bytes. **No parameter has a
default** — a defaulted `capability_attestation_id` would let an execution be identified without
naming the attestation §10.3 requires before anything may publish. `input_snapshot_ids` keeps the
caller's ORDER (§3.4 calls it *the exact ordered partition set read*; sorting would declare two
different read orders equivalent, which is run preparation's judgement, not this module's), while
`parameters`' key order is not identity because RFC 8785 sorts object keys. A bare `str` for
`input_snapshot_ids` — or for either hash tuple — is a `TypeError`, since it would be read one
character at a time.

**`derive_namespace()` reads `binding.SANDBOX_NAMESPACE` through the MODULE**, so the test can move
the constant and see the answer move. `is` would prove nothing: CPython interns identifier-shaped
literals, so a re-spelled `"sandbox_feature"` is the very same object. The test therefore also scans
the module's AST for code string literals (docstrings excluded) and asserts none names a namespace.

**There is no `production_execution_hash` and no bare `execution_hash`**, and `__all__` is pinned
with `==` in the tests so neither can be added quietly.

---

## 30. What Task 12 established — the renderer, and the Kedro surface it renders against

### 30.1 ⚠️ `ClusterInventoryV1.engine_versions` did not exist

The plan's Task-12 sketch says *"pinned dependency versions come from `ClusterInventoryV1.engine_versions`"*, and there was no such field: **Task 0 is unstarted**, and `inventory.py`'s own docstring records that Task 0 still owes `EngineVersions`, `load_inventory` and `MetastoreInventoryAdapter`. Task 12 added the type and the required field to `inventory.py` — extending the module Task 0 owns rather than restating it, as that docstring asks — and left the loader and the adapter to Task 0.

```python
@dataclass(frozen=True, slots=True)
class EngineVersions:      # every field required and non-blank
    hive: str; spark: str; metastore: str; python: str; java: str
    pyspark: str; kedro: str; kedro_datasets: str

ClusterInventoryV1(environment_id, tables, logical_schema_map, engine_versions, captured_at)
```

**`kedro_datasets` is not in the plan's field list, and it must be.** Kedro moved its dataset implementations into a separately versioned distribution: `spark.SparkHiveDataset` and `spark.SparkDataset` resolve out of **`kedro-datasets`**, not out of `kedro`. A lock naming only `kedro` leaves the class that reads every governed source table unpinned.

Adding the field changes **no hash**: only `TableLayout.semantic_payload()` is hashed (per table), and `ClusterInventoryV1` itself never enters an identity payload.

### 30.2 The Kedro API surface, VERIFIED (kedro 1.5.0 · kedro-datasets 9.5.0)

Neither package is a repository dependency and neither is importable from `.venv`, so THE RULE could not be satisfied from the repo. Both were installed into a scratch virtualenv and inspected. What the renderer emits is written against this surface, chosen for being stable across 0.19.x and 1.x:

| Emitted | Verified |
|---|---|
| `from kedro.pipeline import Pipeline, node` | `node(func, inputs, outputs, *, name=None, tags=None, …)`; `Pipeline(nodes, *, inputs=…, outputs=…, …)` |
| `settings.HOOKS = (…)` · `CONFIG_LOADER_ARGS` | the project template's own `settings.py` |
| `default_run_env: "base"` | `OmegaConfigLoader` skips the run env when it equals `base_env`, so a project shipping only `conf/base` needs no `conf/local` |
| `config_patterns={"spark": [...]}` then `context.config_loader["spark"]` | `OmegaConfigLoader.__init__(config_patterns=…)` |
| `${runtime_params:staging_root}` in `catalog.yml` | `OmegaConfigLoader._register_runtime_params_resolver` (allowed in catalog/parameters, **banned in globals**) |
| `@hook_impl def after_context_created(self, context)` | `kedro.framework.hooks.specs.KedroContextSpecs` |
| `@hook_impl def before_pipeline_run(self, run_params, pipeline, catalog)` | `PipelineSpecs`; the prepared `--params` arrive as **`run_params["runtime_params"]`** |
| `conf/base/logging.yml` | selected by `KEDRO_LOGGING_CONFIG`; Kedro's own default is `conf/logging.yml`, which is why §7's location needs the env var |
| `spark.SparkHiveDataset` | `__init__(*, database, table, write_mode="errorifexists", table_pk=None, save_args=None, metadata=None)`; write modes are `append/error/errorifexists/upsert/overwrite` |
| `spark.SparkDataset` | `__init__(*, filepath, file_format="parquet", load_args=None, save_args=None, version=None, credentials=None, metadata=None)` |
| `json.JSONDataset` | `kedro_datasets/json/json_dataset.py` |
| `metadata: {kedro-viz: {layer: …}}` | `metadata` is documented as *"ignored by Kedro"* — safe to carry §7's layer |

**Beyond `ast.parse`, and short of L0.** The rendered project was written to disk, `pyspark` was stubbed, and `configure_project("sandbox_feature_cif_daily")` + `register_pipelines()` were run under the real Kedro: a real `Pipeline` object with **10 nodes**, free inputs exactly `{raw_banking__customers, raw_banking__transactions}`, and one terminal output `feature_cif_daily`. `OmegaConfigLoader` then loaded all 15 catalog entries with `${runtime_params:staging_root}` interpolated. This is **not** L0 (§11.2) — L0 installs into an isolated environment and is Task 15's — but it is evidence a parse test cannot give.

### 30.3 `RENDERER_VERSION` — owned, and unpassable

A.13 recorded that nothing owned the compiler/renderer versions §7 puts in the execution hash. The renderer half is closed:

- the constant lives in **`featuregen/materialize/render/__init__.py`**, not in `project.py`;
- **`sandbox_execution_hash` no longer takes `renderer_version`.** It reads `render.RENDERER_VERSION` *through the module*, the same shape `derive_namespace` uses for `binding.SANDBOX_NAMESPACE`, so the drift test moves the constant and watches the hash move.

The placement is forced by the import graph: `render.project` imports `identity` (for `seal_project`), so `identity` cannot import `render.project` back — but it can import the package `__init__`, **provided that file imports nothing**. A test asserts exactly that, because an `from .project import …` added there closes the loop and surfaces as an `ImportError` in whichever module is imported first.

`compiler_version` is still a required parameter: §2's chain has no orchestrating module, and A.13's argument against `identity` inventing one is unchanged.

### 30.4 The renderer's shape

`render_project(authorized, plan, *, environment_id, engine_versions, spine_input, nodes) -> SealedProject`

- takes **Gate 2's `AuthorizedCompilation`** (§1.3: no contract, group plan *or project* from an unauthorized group) and derives the compilation identity rather than accepting one;
- takes the **spine's resolved `PhysicalInputRequirement`**, because `SpineSpec` carries only the schema-flattened logical ref and §3.5 resolution needs a connection the renderer must not have. It refuses a requirement naming another table;
- takes **`RenderedNode`** values — source, declared imports, explicit `inputs`/`outputs` — from Tasks 13/14. The catalog is the only place a storage location may be written, so the module that writes the catalog decides the dataset names, and `project_datasets(...)` is the public function Tasks 13/14 wire against;
- `_check_wiring` refuses six things Kedro would otherwise discover on the cluster: an undeclared output (becomes an in-memory dataset — run succeeds, output gone), a write to a `raw` governed source, two writers of one dataset, a declared dataset nobody writes, a governed source nobody reads, and a pipeline that publishes nothing;
- `materialize_to(project, root)` refuses a non-empty directory: a file left from an earlier render is part of the project on disk and not part of the project the lock was computed over.

**`feature_staging_assembled`** was added to §7's layers because §9's gates run *after* assembly and *before* publication and the assembled group needs a declared home. The published entry's `write_mode` is **`errorifexists`** — fail-closed, since §10 bans `INSERT OVERWRITE` and §10.3 forbids selecting any mechanism before the probe proves one. Task 16 replaces that entry.
