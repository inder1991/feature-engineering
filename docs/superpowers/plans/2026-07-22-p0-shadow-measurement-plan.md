# P0 — Shadow Measurement Harness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** A measure-only harness that runs the triangulation signals over a human-labelled gold set and produces the go/no-go number (false-attest rate + grounding coverage + auto-attestable fraction at a threshold sweep). Writes NOTHING to the authority tier.

**Architecture:** Mirrors the planner shadow store (`overlay/upload/planner/shadow_store.py`, migration 0999) — WORM/append-only telemetry with CHECK constraints + payload hashes. Grounding + fusion are pure functions (provider-free tests); re-classification reuses the enrichment `LLMClient` seam with a new prompt. Design: `docs/superpowers/specs/2026-07-22-p0-shadow-measurement-design.md`. Gold set: `docs/superpowers/specs/2026-07-22-p0-gold-set-labelling-protocol.md`.

**Tech Stack:** Python 3.12/3.11, psycopg, pytest, `uv`. New module dir `src/featuregen/overlay/upload/attest/`.

## Global Constraints

- **Measure-only:** no write to `field_evidence`/`field_decision_event`/`graph_node`/any authority store; no `ai/attested`; no gate that changes catalog behaviour. Only the three new `attestation_*` tables are written.
- Design choices (locked): re-classification = **same model, different prompt** (`prompt_id=overlay_concept_reclassify_v1`); grounding floor = **unset** (the report sweeps it); gold-set size = **120** for FTR.
- Tables are **WORM**: app role INSERTs only, never UPDATE/DELETE/TRUNCATE (mirror `0971_worm_truncate_revoke.sql` grants). Every enum column has a CHECK; jsonb columns carry a `jsonb_typeof='object'` CHECK + a stored payload hash.
- Migration number **1018** (highest existing is 1017). Backend tests via `.venv/bin/python -m pytest <path> -q`; ruff line-length 100. Stage only touched files (never `git add -A` — worktree has unrelated dirt).
- Re-verify the shadow-store pattern in `planner/shadow_store.py` and the migration format in `db/migrations/1010_*.sql` before writing each task.

## File Structure

- `src/featuregen/db/migrations/1018_attestation_shadow_store.sql` — the 3 WORM tables + grants.
- `src/featuregen/overlay/upload/attest/shadow_store.py` — row dataclasses + INSERT-only writers + reconciliation (mirror `planner/shadow_store.py`).
- `src/featuregen/overlay/upload/attest/grounding.py` — deterministic grounding signal (pure).
- `src/featuregen/overlay/upload/attest/reclassify.py` — independent re-classification signal (LLM seam).
- `src/featuregen/overlay/upload/attest/fusion.py` — confidence fusion (pure).
- `src/featuregen/overlay/upload/attest/runner.py` — the shadow runner + gold worksheet emit/ingest.
- `src/featuregen/overlay/upload/attest/report.py` — threshold-sweep metric (Wilson CI).
- Tests under `tests/featuregen/overlay/upload/attest/`.

---

### Task 1: Migration 1018 + the WORM shadow store

**Files:**
- Create: `src/featuregen/db/migrations/1018_attestation_shadow_store.sql`
- Create: `src/featuregen/overlay/upload/attest/shadow_store.py`, `src/featuregen/overlay/upload/attest/__init__.py`
- Test: `tests/featuregen/overlay/upload/attest/test_shadow_store.py`

**Interfaces (Produces):**
- `write_gold_label(conn, *, catalog_source, logical_ref, field_name, gold_value, labeller_ids: list[str], adjudicated_by, notes=None) -> None` (INSERT; PK `(logical_ref, field_name)`; idempotent via `ON CONFLICT DO NOTHING`).
- `write_shadow_run(conn, rec: ShadowRunV1) -> None` and `write_observation(conn, obs: ObservationV1) -> None` (append-only).
- `reconcile(conn, shadow_run_id) -> ReconcileV1` with `.complete` = every sampled (logical_ref,field_name) in the run has an observation.
- Dataclasses `ShadowRunV1`, `ObservationV1` (fields per the design §Persistence).

**Schema (the migration):** three tables per the design — `attestation_gold_label`, `attestation_shadow_run`, `attestation_shadow_observation`. Follow the `planner_shadow_*` DDL idiom: enum CHECKs, `jsonb_typeof(...)='object'` CHECKs, `payload_hash text NOT NULL`, WORM grants. `attestation_shadow_observation` stores NO gold value (correctness is a read-time join).

- [ ] **Step 1: Write the failing test** — `test_shadow_store.py`: apply migration 1018 (the test DB harness auto-applies migrations); `write_gold_label(...)` then re-write same key is idempotent; `write_shadow_run` + two `write_observation`; `reconcile(run_id).complete is True`; a missing observation → `.complete is False`. Assert an UPDATE on `attestation_shadow_observation` raises (WORM grant).
- [ ] **Step 2: Run — expect FAIL** (`ModuleNotFoundError: attest.shadow_store`). Run: `.venv/bin/python -m pytest tests/featuregen/overlay/upload/attest/test_shadow_store.py -q`.
- [ ] **Step 3: Write the migration** — mirror `0999_planner_shadow_store.sql` structure + `0971_worm_truncate_revoke.sql` grants. (Read both first.)
- [ ] **Step 4: Write `shadow_store.py`** — mirror `planner/shadow_store.py` (`canonical_json`/`payload_hash`, dataclasses, INSERT-only writers, `reconcile`).
- [ ] **Step 5: Run — expect PASS.** ruff clean.
- [ ] **Step 6: Commit** — `feat(attest): migration 1018 + WORM shadow store for measurement telemetry`.

---

### Task 2: Deterministic grounding signal (pure, no provider)

**Files:**
- Create: `src/featuregen/overlay/upload/attest/grounding.py`
- Test: `tests/featuregen/overlay/upload/attest/test_grounding.py`

**Interfaces:**
- Consumes: the concept vocabulary (`concepts.classification_vocabulary()` / `class Concept` in `concepts.py` — read it for the type-family + bian/fibo path metadata each concept carries) and the column's active `field_evidence` (`read_active_field_evidence(conn, logical_ref, field_name)`).
- Produces: `ground_concept(conn, logical_ref, proposed_concept) -> GroundingV1` with `{checks: dict[str,str], coverage: float, conflict: bool}` where each check ∈ {`pass`,`fail`,`absent`}. Checks: `type_consistency` (proposed concept's implied type-family vs the parser `logical_representation`/`semantic_type` evidence), `path_agreement` (proposed concept vs attested `bian_path`/`fibo_path`/`business_term`), `sibling_consistency` (e.g. `currency_code` expects an amount sibling in the table). `coverage = present_checks / 3`; `conflict = any check == 'fail'`.

- [ ] **Step 1: Failing test** — three cases: (a) a numeric column proposed `monetary_flow` with a parser `numeric` type + a matching BIAN path → all checks `pass`, coverage 1.0, conflict False; (b) a text column proposed `monetary_flow` with parser `text` type → `type_consistency='fail'`, conflict True; (c) a column with NO parser type and NO attested path → those checks `absent`, coverage < 1.0. Seed via `build_graph` + `record_field_evidence` (mirror `test_asset_detail_provenance.py`).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement `ground_concept`** — pure logic over the vocab metadata + the read evidence; no LLM, no writes.
- [ ] **Step 4: Run — expect PASS.** ruff clean.
- [ ] **Step 5: Commit** — `feat(attest): deterministic grounding signal with per-check coverage`.

---

### Task 3: Independent re-classification signal (LLM seam)

**Files:**
- Create: `src/featuregen/overlay/upload/attest/reclassify.py`
- Test: `tests/featuregen/overlay/upload/attest/test_reclassify.py`

**Interfaces:**
- Consumes: `LLMClient` (`featuregen.intake.llm`) and the enrichment call seam (`enrich_llm.audited_structured_call` — read its signature). A NEW `prompt_id='overlay_concept_reclassify_v1'` whose instruction classifies the column BLIND (given name/definition/samples, NOT the prior proposal) into the same vocabulary — a decorrelating second opinion, not a yes/no.
- Produces: `reclassify_concept(conn, client, logical_ref, *, column_ctx) -> ReclassifyV1{value: str|None, agrees_with: callable}` — returns the independent concept; the runner compares it to the proposer's value for `agrees`.

- [ ] **Step 1: Failing test** — with a FAKE `LLMClient` (mirror how existing enrich tests fake the client) returning a fixed concept, assert `reclassify_concept` returns that value; a client returning an out-of-vocabulary value is rejected to `None` (reuse the `_accept_concept` gate from `enrich.py`).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** — build the blind prompt, call via the audited seam, accept via the vocab gate. No writes to authority stores (dispatch audit is fine — that is telemetry, not attestation).
- [ ] **Step 4: Run — expect PASS.** ruff clean.
- [ ] **Step 5: Commit** — `feat(attest): independent blind re-classification signal (decorrelating)`.

---

### Task 4: Confidence fusion (pure)

**Files:**
- Create: `src/featuregen/overlay/upload/attest/fusion.py`
- Test: `tests/featuregen/overlay/upload/attest/test_fusion.py`

**Interfaces:**
- Produces: `fuse(*, proposer_value, reclassify_value, grounding: GroundingV1) -> FusionV1{confidence: float, agreement: dict}`. Monotone + transparent: agreement between proposer and reclassifier raises confidence; a grounding `conflict` caps it low; grounding `coverage` scales how much LLM-agreement is trusted. NO gold peeking (calibration is downstream in the report).

- [ ] **Step 1: Failing test** — proposer==reclassify + grounding all-pass → high confidence; proposer!=reclassify → low; grounding conflict → capped low regardless of agreement; zero grounding coverage → agreement contributes less than with full coverage (the decorrelation guard is visible in the number).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** the monotone fusion.
- [ ] **Step 4: Run — expect PASS.** ruff clean.
- [ ] **Step 5: Commit** — `feat(attest): transparent confidence fusion (grounding-gated)`.

---

### Task 5: Shadow runner + gold worksheet emit/ingest

**Files:**
- Create: `src/featuregen/overlay/upload/attest/runner.py`
- Test: `tests/featuregen/overlay/upload/attest/test_runner.py`

**Interfaces:**
- Consumes: Tasks 1-4 + the schema-aware `logical_ref_of` + `read_active_field_evidence`.
- Produces:
  - `emit_gold_worksheet(conn, catalog_source, *, size=120, seed) -> list[WorksheetRow]` — a STRATIFIED sample (domain × risk × type-family, per the protocol), each row carrying name/definition/bian-fibo/5-sample-values but NOT the AI concept. Deterministic given `seed` (Math.random is unavailable — pass a seed).
  - `ingest_gold_worksheet(conn, rows) -> int` — writes adjudicated labels to `attestation_gold_label` via Task 1.
  - `run_shadow(conn, catalog_source, *, client, shadow_run_id, gold_version) -> ReconcileV1` — for each gold-labelled column: read proposer concept, `ground_concept`, `reclassify_concept`, `fuse`, assign risk tier (intrinsic PII/leakage from taxonomy sensitivity/leakage evidence), `write_observation`. Writes the run manifest. NO authority-tier write.

- [ ] **Step 1: Failing test** — seed a small catalog + gold labels; `run_shadow` with a fake client writes one observation per gold (logical_ref,field), `reconcile.complete is True`, and asserts ZERO rows written to `field_evidence`/`field_decision_event`/`graph_node` during the run (measure-only invariant). `emit_gold_worksheet` returns a stratified sample of the requested size without the AI concept present in the payload.
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** the runner + worksheet emit/ingest.
- [ ] **Step 4: Run — expect PASS.** ruff clean.
- [ ] **Step 5: Commit** — `feat(attest): shadow runner + stratified gold worksheet emit/ingest (measure-only)`.

---

### Task 6: Report / metric (threshold sweep + Wilson CI)

**Files:**
- Create: `src/featuregen/overlay/upload/attest/report.py`
- Test: `tests/featuregen/overlay/upload/attest/test_report.py`

**Interfaces:**
- Produces: `shadow_report(conn, shadow_run_id) -> ReportV1` joining `attestation_shadow_observation` → `attestation_gold_label`. For a threshold sweep (e.g. 0.50→0.95 step 0.05), split by (all / grounding-covered / grounding-thin) and by field, compute: `false_attest_rate` (auto-attested [confidence≥T, low-risk] whose fused value ≠ gold) with a **Wilson 95% CI**, `auto_attestable_fraction`, `grounding_coverage` distribution, and `n` per cell. Headline: the (threshold, false-attest, CI, auto-attest%) table.

- [ ] **Step 1: Failing test** — seed observations + gold with known agreements (e.g. 10 auto-attested at T, 1 wrong) and assert `false_attest_rate == 0.1` with the exact Wilson CI bounds for n=10,k=1; assert the grounding-covered vs grounding-thin split partitions correctly; assert a gold correction (re-ingest a different gold value) re-scores WITHOUT re-running signals (the read-time-join property).
- [ ] **Step 2: Run — expect FAIL.**
- [ ] **Step 3: Implement** the sweep + Wilson CI (implement Wilson directly; no scipy dependency).
- [ ] **Step 4: Run — expect PASS.** ruff clean.
- [ ] **Step 5: Commit** — `feat(attest): shadow report — threshold sweep, Wilson CI, grounding split (the go/no-go metric)`.

---

## Self-Review

**Spec coverage (design §Components):** persistence→T1; grounding→T2; re-classification→T3; fusion→T4; runner+worksheet→T5; report→T6. Locked design choices (same-model reclassify, unset floor, size 120) appear in Global Constraints + T3/T5/T6. ✓
**Measure-only invariant:** enforced by the T5 test asserting zero authority-store writes. ✓
**Placeholder scan:** each task names files, interfaces, concrete test cases, and the exact pattern to mirror. Greenfield code that must match a neighbouring module (shadow_store, the LLM seam, the vocab) is specified by reference-to-mirror + interface + tests rather than transcription, since the exact code depends on reading that module — the implementer reads it (named) as directed. ✓
**Human dependency:** the real gold labels come from the protocol doc (your team); T5's `ingest_gold_worksheet` is the seam. The harness is fully testable with synthetic labels before real ones exist. ✓
