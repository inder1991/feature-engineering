# Child 1 — TypedFormula Authoring Implementation Plan (rev 2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Author a governed, content-hashed `TypedFormulaV1` from a feature intent — offline, deterministic, no execution — proven against curated gold + a key-gated real-provider evaluation.

**Architecture:** New `src/featuregen/formula/` package. Boundary order: **parse (strict JSON schema) → semantic validate → canonicalize/hash → capability classify → C1 output authority → critic → disposition**. The LLM never returns dataclasses — a strict parser bridges dict→typed. The author runs a **sequential turn protocol** (`AuthorTurnV1 = ToolCallV1 | FinalProposalV1`) over repeated `client.call()` structured requests (the adapter has no tool-use). All governed provider calls go through one audited-call wrapper; nothing compiles or runs against data.

**Tech Stack:** Python 3.11, frozen slotted dataclasses + `StrEnum`, a **vendored RFC-8785 JCS** implementation with the RFC test vectors, `jsonschema` (Draft 2020-12) for the strict parser, the existing `FakeLLM`/`audited_structured_call` seams, psycopg for write-once trace tables.

## Global Constraints
- Frozen slotted dataclasses + `StrEnum`; **NOT pydantic**. The LLM boundary is dict-in via `parse_proposal_v1` (strict JSON schema), never direct dataclass construction.
- **NO execution, NO Spark, NO durable formula/version artifact.** Durable writes = `authoring_run` + `authoring_trace_event` (both write-once) + `llm_call` (via the audited seam).
- Authoritative output ONLY from `resolve_formula_output_policy` over **C1 `read_operational_value`** — never from the LLM's advisory `expected_output`. Authoritative output is **mandatory** for a `TypedFormulaV1`; when unresolved, carry the `candidate_proposal`, never a fabricated `FormulaOutputPolicyV1`.
- All governed provider calls go through the **audited-call wrapper** (§Task 3); do NOT call `record_llm_call` directly or re-implement the egress/schema/repair boundary.
- `formula_content_hash = sha256(JCS(TypedFormulaV1))`, identity fields only; capability version + provenance OUTSIDE it.
- Operand identity `source::schema.table[.column]` (`object_ref._SOURCE_SEP="::"`), normalized before hashing.
- **Unsupported ≠ invalid**; `COUNT_DISTINCT` is NON_ADDITIVE (override `b_output_policy.py:126`); do not widen `b_operation.SupportedOperation`.
- Tests offline via `FakeLLM` EXCEPT the key-gated provider evaluation; C1 fixtures built through the **real evidence → decision → projection** path (not flat `graph_node` inserts).
- Migration number **1020** (verify with `git show origin/main:src/featuregen/db/migrations` version-aware, not `ls | tail`).
- Commit trailer: `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`. Run: `PYTHONPATH=src .venv/bin/python -m pytest <path> -p no:cacheprovider -q`.

---

### Task 1: Schema + FULL semantic validation `[fixes review#7]`
**Files:** Create `src/featuregen/formula/__init__.py`, `src/featuregen/formula/schema.py`; Test `tests/featuregen/formula/test_schema.py`.
**Produces:** all enums + frozen dataclasses (spec §A, exact string values); `SchemaError`; `validate_semantics(p: TypedFormulaProposalV1) -> None`.
Validation MUST cover (each its own asserting test): predicate invariants + filter limits; **ref arity** (table ref has no `.column`, operand ref has one); **same-table source containment** (every operand/filter/`event_time_ref` shares the expression's `source_relation.table_ref` — cross-table reachability is explicitly deferred to governed planning, Child 3, and noted as such); body discriminator (`aggregation` ∈ `AggregateFunction`, never a `FinalOperation`); version pins (the four identity versions are known ints); decimal `precision≥scale≥0`; typed-literal parse (value string parses to its `LiteralType`); parameter bounds (`allowed_min≤allowed_max`, `allowed_set` non-empty if present, name regex + uniqueness); **predicate-operator type compatibility** (`GREATER_THAN` etc. only on numeric/date literal/param); window `length≥1` + IANA `timezone` + inclusivity present; `COUNT_ROWS` ⟺ `operand is None`.
Steps: write the failing tests (one per rule) → run-fail → implement enums+dataclasses+`validate_semantics` → run-pass → commit `feat(formula): schema + full semantic validation`.

### Task 2: Strict dict→typed parser `[fixes review#1 — BLOCKER]`
**Files:** Create `src/featuregen/formula/parse.py` (+ `src/featuregen/formula/proposal_v1.schema.json`); Test `tests/featuregen/formula/test_parse.py`.
**Produces:** `parse_proposal_v1(raw: Mapping) -> TypedFormulaProposalV1` — validates `raw` against a registered **strict** JSON Schema (`additionalProperties:false` everywhere, discriminated `oneOf` on `body.final_operation` and `filter.kind`, enum + version constraints) BEFORE constructing dataclasses, then calls Task-1 `validate_semantics`. Raises `SchemaError` on unknown fields, malformed unions, wrong versions, invalid refs, over-limits.
Steps: failing tests (unknown field rejected; `final_operation:"ratio"` requires numerator+denominator; a dropped required field fails; `aggregation:"avg"` fails enum; extra `right_set` on `EQUAL` fails) → run-fail → write the JSON Schema + `parse_proposal_v1` → run-pass → commit `feat(formula): strict proposal parser`.

### Task 3: Audited-call wrapper `[fixes review#3 — BLOCKER]`
**Files:** Create `src/featuregen/formula/audited.py`; Test `tests/featuregen/formula/test_audited.py`.
**Produces:** `@frozen AuditedCallResult{output: dict|None, llm_call_ref: str|None, provider_calls: int, usage: dict}`; `audited_formula_call(conn, client, *, authoring_run_id, task, prompt_id, schema_id, instruction, catalog_metadata, actor, ...) -> AuditedCallResult` — a thin wrapper delegating to the existing `audited_structured_call` (egress sanitizer + schema registry + durable fresh-connection audit + repair) that ALSO threads `authoring_run_id` and surfaces the `llm_call_ref`/usage. Does NOT re-implement the security boundary.
Steps: failing test (a FakeLLM call returns output + a non-null `llm_call_ref` linked to `authoring_run_id`; an egress-blocked call returns `output=None` but still an audited ref) → run-fail → implement wrapper (extend/adapt `audited_structured_call` to return the ref + accept the run id; if it must change signature, do so additively) → run-pass → commit `feat(formula): audited-call wrapper carrying run id + llm_call_ref`.

### Task 4: Canonicalization + hashing (vendored JCS) `[fixes review#8]`
**Files:** Create `src/featuregen/formula/_jcs.py` (vendored RFC-8785 + `tests/featuregen/formula/test_jcs_vectors.py` running the RFC vectors), `src/featuregen/formula/canonical.py`; Test `tests/featuregen/formula/test_canonical.py`; `tests/featuregen/formula/factories.py`.
**Produces:** `canonical_json(f: TypedFormulaV1) -> str`, `formula_content_hash(f) -> str`. Uses `overlay.upload.object_ref` to normalize refs; applies §E ordering (ordered slots + grain order preserved; associative AND/OR flatten+sort-by-child-hash; `IN`/`allowed_set` sort+dedup; params sort). **Fix the broken review tests:** build variants with real factory constructors (no `f.__dict__` on slotted classes); the capability-invariance test hashes a formula vs. one carrying a *different capability version in the envelope* (not the same object twice).
Tests (each non-tautological): RFC vectors pass; NFC normalization; normalized refs; ordered-slot swap ≠ hash; associative-AND reorder = hash; grain-key reorder ≠ hash; each identity version independently changes the hash; capability version (envelope) does NOT; duplicate `parameter` name rejected.
Steps: failing tests → run-fail → vendor JCS + implement canonical.py → run-pass → commit `feat(formula): vendored JCS canonicalization + formula_content_hash`.

### Task 5: C1 fixtures via evidence→decision→projection `[fixes review#5 — BLOCKER, fixtures]`
**Files:** Create `tests/featuregen/formula/c1_fixtures.py`; Test `tests/featuregen/formula/test_c1_fixtures.py`.
**Produces:** helpers that seed a column through the REAL governed path so `read_operational_value` returns each status: `resolved`, `no_value`, `conflict`/`fork`, `hash_mismatch`, `projection_unavailable`, retired, `not_operational` (mirror the seeding used by existing operational-facts tests — evidence rows → decision → projection, NOT flat `graph_node` inserts).
Steps: failing test asserting each helper yields the intended `read_operational_value(...).status` → run-fail → implement helpers over the real commands → run-pass → commit `test(formula): C1 status fixtures via governed path`.

### Task 6: Operation map + additivity + output-authority resolver `[fixes review#5 — BLOCKER, interface]`
**Files:** Create `src/featuregen/formula/operations.py`, `src/featuregen/formula/output_authority.py`; Test `tests/featuregen/formula/test_output_authority.py` (uses Task-5 fixtures).
**Produces:** `to_path_aggregation(fn) -> PathAggregation|None`; `formula_additivity(body, *, per_expr_facts, partition_proof) -> AdditivityClass` (takes resolved facts + a disjointness/path proof object — cannot prove additivity from the body alone); `resolve_formula_output_policy(proposal: TypedFormulaProposalV1, *, per_expr_facts, grain_facts, now) -> FormulaOutputPolicyV1 | NeedsAuthority | ExternalRequirement | InvalidOutput`. Consumes the **validated proposal** + a `per_expr_facts` mapping (each expr's C1 `OperationalValue` bundle) + grain facts — NOT a bare `body`.
Tests (via Task-5 fixtures): COUNT_DISTINCT → NON_ADDITIVE; SUM(amount) resolves without demanding hint-only unit (§C matrix); DIFFERENCE incompatible units → `InvalidOutput`; RATIO non-cancelling → `ExternalRequirement("UNIT_PROVISIONING_REQUIRED")`; C1 `hash_mismatch`/`fork`/`projection_unavailable` on a required field → `NeedsAuthority`.
Steps: failing tests → run-fail → implement map + corrected additivity + the §C required-field matrix → run-pass → commit `feat(formula): operation map + additivity + C1 output-authority`.

### Task 7: Capability classifier `[fixes review#6 — BLOCKER]`
**Files:** Create `src/featuregen/formula/capability.py`; Test `tests/featuregen/formula/test_capability.py`.
**Produces:** `CAPABILITY_POLICY_VERSION=1`; `classify_formula_capability(proposal) -> Literal["ok","unsupported_capability"]` — v1 allows a single catalog source across all operands + a single trailing/calendar window per expression; multi-source or an out-of-v1 capability → `unsupported_capability`. Distinguishes valid-but-unsupported from structural invalidity (structural is Task 1/2).
Steps: failing tests (single-source → ok; two-source operands → unsupported_capability; NOT an INVALID) → run-fail → implement classifier → run-pass → commit `feat(formula): versioned capability classifier`.

### Task 8: Multi-axis AuthoringResult + disposition fold `[fixes review#4, #12]`
**Files:** Create `src/featuregen/formula/result.py`; Test `tests/featuregen/formula/test_result.py`.
**Produces:** `@frozen AuthoringResult` with: the 6 status axes; `authoring_disposition`; `disposition_policy_version`; `authoring_run_id`; `candidate_formula: TypedFormulaV1|None`; `candidate_formula_hash: str|None`; `candidate_proposal: TypedFormulaProposalV1|None` (when output authority is unresolved — no authoritative formula exists); `output_requirements: tuple[ExternalRequirement,...]`; `authority_failures: tuple[AuthorityFailure,...]` (reason + affected operand/field); `capability_reason: str|None`; `critic_findings_hash: str|None`. `derive_disposition(axes, *, authoring_run_id, candidate_formula=None, candidate_proposal=None, ...) -> AuthoringResult` — pure fold, **single signature** (fixes the review#12 `candidate` mismatch): technical→TECHNICAL_FAILURE; invalid_formula/invalid_output→REJECTED; unsupported_operation/unsupported_capability→UNSUPPORTED; needs_authority/external_requirement/blocking-critic/expectation-mismatch→NEEDS_REVIEW (carry candidate_formula OR candidate_proposal + reasons); else RESOLVED.
Steps: failing tests (unsupported_operation→UNSUPPORTED; reviewable NEEDS_REVIEW carries the candidate + reasons + run id; unresolved output → candidate_proposal not a fake FormulaOutputPolicyV1) → run-fail → implement → run-pass → commit `feat(formula): multi-axis AuthoringResult + disposition fold`.

### Task 9: Sequential-turn author + 7 tools `[fixes review#2 — BLOCKER]`
**Files:** Create `src/featuregen/formula/turns.py` (`AuthorTurnV1 = ToolCallV1 | FinalProposalV1`, provider-compatible schemas), `src/featuregen/formula/tools.py`, `src/featuregen/formula/author.py`; Test `tests/featuregen/formula/test_author.py`.
**Produces:** `TOOLS` (7 read/validate, read-scoped, metadata-only egress; wrap `graph_node`/`resolve_fact`/`object_ref`); `author_formula(conn, intent, client, *, roles, max_turns, actor, authoring_run_id) -> tuple[dict|None, list[turn]]` — a loop that issues ONE `audited_formula_call` per turn requesting an `AuthorTurnV1`; on `ToolCallV1` it runs the tool and feeds the **canonical tool result** into the next turn's `catalog_metadata`; on `FinalProposalV1` it returns the raw proposal dict (→ Task-2 parser). Bounded by `max_turns` + budget (exceed → surfaced as technical).
Tests (FakeLLM scripting a 3-turn run: search → get_metadata → final proposal): the flow completes; each turn is one audited call with an `llm_call_ref`; a run that never emits `FinalProposalV1` within `max_turns` → technical; tool egress carries metadata only.
Steps: failing tests → run-fail → implement turn schemas + tools + loop → run-pass → commit `feat(formula): sequential-turn author + read/validate tools`.

### Task 10: Independent critic (fail-closed) `[fixes review#9]`
**Files:** Create `src/featuregen/formula/critic.py`; Test `tests/featuregen/formula/test_critic.py`.
**Produces:** `CRITIC_FINDING_CODES` (closed enum + fixed severity, §G), `CRITIC_POLICY_VERSION=1`, `critique(conn, intent, proposal, client, *, roles, actor, authoring_run_id) -> tuple[list[CriticFinding], str, bool]` (findings, `critic_findings_hash`, `is_technical_failure`). The critic gets an **independently-assembled, read-scoped metadata context** (intent + the proposal's operands' metadata re-fetched under the caller's scope) — but NOT the author's reasoning/tool trace. A **malformed/unparseable** critic response → `is_technical_failure=True` (**fail closed**, never a clean critic → never auto-RESOLVED); unknown/duplicate codes dropped with a recorded note.
Steps: failing tests (blocking finding → surfaced; malformed critic response → technical_failure, not clean; author reasoning never passed) → run-fail → implement → run-pass → commit `feat(formula): independent fail-closed critic`.

### Task 11: Write-once, crash-safe trace `[fixes review#10]`
**Files:** Create `src/featuregen/db/migrations/1020_formula_authoring_trace.sql`, `src/featuregen/formula/trace.py`; Test `tests/featuregen/formula/test_trace.py`.
**Migration:** `authoring_run` AND `authoring_trace_event` **both** reject UPDATE/DELETE/TRUNCATE (write-once triggers like `0060`); `authoring_trace_event` has: `(authoring_run_id, seq)` unique, closed `kind` CHECK, one-terminal-event constraint (partial unique on terminal kinds), FK `llm_call_ref → llm_call`, `idempotency_key` unique, and a trigger rejecting any event after a terminal event.
**Produces:** `open_authoring_run(conn, *, intent_hash, versions, actor) -> str`; `append_event(conn, run_id, kind, *, seq, idempotency_key, llm_call_ref=None, payload)`; `run_status(conn, run_id) -> Literal["incomplete","completed","failed"]` (incomplete = no terminal event). Manifest + events use the **durable fresh-connection** pattern so provider audits can't survive a rolled-back manifest.
Steps: failing tests (no terminal → incomplete; event-after-terminal rejected; UPDATE on manifest rejected; duplicate seq/idempotency rejected) → run-fail → write migration + trace.py → run-pass → commit `feat(formula): write-once crash-safe authoring trace`.

### Task 12: Orchestrator + gold gate (3-way) `[fixes review#11]`
**Files:** Create `src/featuregen/formula/authoring.py`, `src/featuregen/formula/gold.py`; `tests/featuregen/formula/gold_fixtures/*.json` (immutable curated `(intent, expected TypedFormulaV1)`, hand-authored — NOT from the code factories); Tests `tests/featuregen/formula/test_authoring.py`, `test_gold_gate.py`, `test_provider_eval.py`.
**Produces:** `run_authoring(conn, intent, author_client, critic_client, *, roles, actor) -> AuthoringResult` wiring open_run → author (T9) → parse (T2) → validate (T1) → capability (T7) → C1 output authority (T6) → critic (T10, separate context) → derive_disposition (T8) → append terminal event (T11). `score_gold(...)` + `GOLD_GATE_V1` thresholds.
Split the gate three ways: (a) **deterministic plumbing/conformance** tests (FakeLLM) proving wiring; (b) **curated-fixture** tests loading the immutable gold JSON and asserting a correctly-authored formula's hash equals the fixture's (fixtures independent of factories → not tautological); (c) a **key-gated real-provider evaluation** (`skipif` no `ANTHROPIC_API_KEY`) covering all six operations, adversarial cases, and repeated runs against `GOLD_GATE_V1` (false-resolve 0, operand-preservation 1.0, blocking-critic recall 1.0, technical-failures-in-clean 0). FakeLLM proves plumbing, never quality.
Steps: failing tests (a)+(b) → run-fail → implement orchestrator + gold scorer + write curated fixtures → run-pass → write (c) as an enablement test → commit `feat(formula): authoring orchestrator + 3-way gold gate`.

---

## Self-Review
**Review-finding coverage:** #1→T2; #2→T9; #3→T3; #4→T8; #5→T5+T6; #6→T7; #7→T1; #8→T4; #9→T10; #10→T11; #11→T12; #12→T8 (single `derive_disposition` signature) + T11 (migration 1020) + T9-before-nothing (the author no longer consumes a T11 `AuthorTrace`; it returns raw turns, and the orchestrator T12 owns trace writes).
**Spec coverage:** §A→T1/T2; §E→T4; §B/C/D→T6; §F→T8; §I→T9; §G→T10; §H→T11; §J→T12; capability policy→T7; audited seam→T3; C1 authority→T5/T6.
**Placeholder scan:** no TBDs; every task has concrete asserting tests + exact interfaces. The two spots that defer to governed state (cross-table reachability, additivity partition proof) are explicitly named as consuming external proof material / deferred to Child 3, not hand-waved.
**Type consistency:** `TypedFormulaProposalV1` produced by T2, consumed by T6/T7/T9/T12; `AuditedCallResult` (T3) used by T9/T10; `AuthoringResult` (T8) returned by T12; `formula_content_hash` (T4) is the single hasher; `read_operational_value` (via T5 fixtures) is the only authority read; `derive_disposition` has ONE signature used by T12.
