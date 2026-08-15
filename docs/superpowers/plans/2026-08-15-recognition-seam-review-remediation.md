# Recognition seam — review remediation

**Date:** 2026-08-15 · **Successor to:** `2026-08-15-recognition-repair-seam.md` (Tasks 0–6, all
landed, `73100d78`).

An adversarial review of the completed seam raised seven findings. **All seven were independently
validated against the code before this plan was written** — the evidence is in §0.1, and where the
review's line numbers had drifted the verified ones are given instead. None were dismissed.

The review's closing judgement is adopted verbatim as this plan's framing: the seam is
**"repair-loop and observability implementation complete, release qualification incomplete"** — not
end-to-end complete. §7 corrects that language wherever the repo currently overstates it.

## 0. What is broken, and why it survived seven tasks

### 0.1 The findings, as validated

| # | Finding | Verified at | Verdict |
|---|---|---|---|
| F1 | **The motivating failure is still possible.** The aggregate cap counts `relationship == "primary"` over ALL raw candidates before any validity check, so an invalid candidate votes on a rule it is not a member of | `taxonomy/recognition.py:523-540` | **CONFIRMED — blocker** |
| F2 | **The frontend does not build.** Three typed fixtures lack the now-required `recognition_quality` / `ambiguity_note` | `WorkbenchScreen.test.tsx:1979, 2636, 2667` → 3 × TS2739 under `npm run typecheck` | **CONFIRMED — blocker** |
| F3 | **Recorded request identity can describe a contract the provider never executed.** The hash is computed from the committed canonical schema; dispatch returns whatever the mutable registry holds, never comparing the two | `enrich_llm.py:1908-1927` (`_require_schema`), `documents/registry.py:29-49` | **CONFIRMED — major** |
| F4 | **The paid evaluation is neither durable nor spend-capped.** No `commit()` exists in the module (only a git-hash helper of that name); run creation is `with conn.transaction()` — a nested savepoint on an open transaction. The execution loop iterates all 100 cases with no budget consulted inside it | `recognition_release_eval.py:176`, `:406-444`, `:678-682` | **CONFIRMED — major, both halves** |
| F5 | **Release not provider-qualified.** The gate was never run; the motivating case is not in the release denominator; gold labels are authored-pending-review | the seam plan's own Task 6 row | **CONFIRMED — factual** |
| F6 | **A closed vocabulary that is documented but not enforced.** `CandidateDrop` states `reason_code` is always a member of `RECOGNITION_FAILURE_CODES`; the dataclass validates nothing and the reader accepts any string | `taxonomy/recognition.py:146-160`, `contract/scope_records.py:65-82`, `1071_*.sql:53-88` | **CONFIRMED — medium** |
| F7 | **Idempotency is sequential, not concurrent.** lookup → provider → `ON CONFLICT DO NOTHING`, with no lock or re-check between; two simultaneous identical requests both pay | `api/routes/contract.py:1202-1223` | **CONFIRMED — medium** |

### 0.2 F1's root cause is a composition, not a mis-ordering

Re-ordering the cap check alone **does not fix the motivating case.** Three individually-defensible
decisions compose into the failure, and two of them came from the *previous* review round:

1. **Task 1's enum** made `use_case_id: "x"` a *structural* violation rather than a semantic one —
   adopted because the earlier review argued, correctly, that it should be impossible to emit.
2. **Task 2** withholds the body on the `schema_invalid` arm — correct, and one of its four proven
   negatives: a body without accountability must never escape.
3. **Task 4** counts primaries before validating candidates.

Composed: after repair exhaustion the incident body is `schema_invalid`, so **partition never
receives it at all**. Making the schema stricter *reduced* recoverability.

**This plan therefore partially reverses the earlier amendment**, and says so rather than quietly
re-tuning: taxonomy membership moves OUT of the wire schema and INTO the semantic validator, where
it still drives repair (Task 2 already routes semantic failures into the bounded budget) but no
longer converts a recoverable answer into an unrecoverable one.

**The framing error was mine, and it is recorded so the reasoning is not repeated.** Task 4's brief
instructed the implementer to refuse the incident and to test that refusal, on the argument that
dropping a junk sibling would "launder a cap violation". That is wrong: an invalid candidate has no
standing to vote on a cap it is not a member of. The agent implemented the instruction faithfully.

### 0.3 Done-bar

> The 2026-08-15 incident body — one valid churn primary beside a placeholder — ends as a scope over
> the valid candidate **whether or not repair succeeds**, with the drop named by a closed code; two
> genuinely valid primaries still refuse; the frontend builds under the project's own script; a
> recorded request identity always describes the contract that actually executed; and no paid gate
> can be started without durability and an enforced spend ceiling.

## 1. Tasks

### R1 — the frontend builds (½ day) · **F2**

Add the missing `recognition_quality: null` / `ambiguity_note: null` to the three fixtures — `null`
is the honest legacy value, not a placeholder. **Extract ONE typed fixture factory** for recognition
responses so the next required field cannot break three call sites again.

**The gate itself is part of the fix.** `tsc --noEmit` passes while `tsc -b` fails; the seam's
acceptance rows record "typecheck clean" on the wrong command. Every frontend gate in this repo's
plans and briefs must name **`npm run typecheck`**, `npm test` and `npx oxlint` — the project's own
scripts, run from `frontend/`.

**Acceptance:** `npm run typecheck` clean; `npm run build` succeeds; `npm test` unchanged; a test
that constructs `RecognitionResp` through the factory and would fail to compile if a required field
were added without updating it.

### R2 — membership is semantic, and invalid candidates do not vote (2½ days) · **F1** — **schema v3**

**Two changes, and the second is only correct because of the first.**

1. **Schema v3** keeps `use_case_id` structural (a bounded string) and drops the closed enum;
   membership is validated by `validate_recognition_output` inside the repair loop. v2 stays
   registered and byte-frozen; `technical_failure` stays out of the provider schema; the frozen-bytes
   + import-time digest discipline from Task 1 carries forward unchanged.
2. **`partition_candidates` drops candidate-local invalids FIRST, then applies the aggregate rules to
   what remains.** Two genuinely valid primaries still refuse — that is real ambiguity and the reason
   the cap exists. A valid primary beside an invalid one is not ambiguity; it is one answer and one
   piece of noise.

**Explicitly preserved:** no secondary is ever promoted; duplicate ids, over-count, and
status/candidate incompatibility still refuse on the survivors; every drop carries a closed code.

**Acceptance:**
- `test_the_live_incident_recovers_when_repair_fails` — the verbatim §0 body, repair exhausted →
  `CLASSIFIED` on `customer.relationship_attrition.churn`, one drop recorded. **This is the done-bar,
  and it currently asserts the opposite** (`test_recognizer.py:605-616`); that test is replaced and
  the replacement is justified in the commit message.
- `test_two_valid_primaries_still_refuse` — the cap survives.
- `test_an_unknown_id_still_drives_repair` — membership failure reaches the loop via the semantic
  arm, so the enum's removal costs no repair pressure.
- `test_v2_stays_registered_and_frozen`; `test_v3_bytes_match_their_pinned_hash`.
- Task 2's four exposure negatives re-asserted unchanged.

### R3 — recorded identity is the executed contract (1½ days) · **F3**

- `register_immutable_schema` for `use_case_recognition` specifically (**not** a blanket conversion
  of every enrichment schema — Task 1 recorded why: a drifted stored body would take every
  enrichment call down at bootstrap).
- **At dispatch, compare the resolved registry digest with the canonical digest** and refuse on
  mismatch, naming both. This is the actual fix; immutability alone does not cover a row that was
  already drifted.
- The evaluator verifies `schema_content_hash` in `_verify_run()`.
- **Request identity gains the repair/retry policy versions** — changing `DEFAULT_REPAIR_BUDGET`
  currently does not re-key cached results.
- Model identity and generation settings derive from the **bound client/configuration**, not an
  independent re-read of the environment.

**Acceptance:** the review's own reproduction as a test (register canonical → mutate the row →
dispatch refuses); a budget change re-keys; two independent env reads can no longer disagree.

### R4 — the paid gate is durable and actually capped (2 days) · **F4**

- **Commit the run and corpus before any provider call.** Commit each attempt, or in small
  checkpoints.
- **Resumable:** skip existing `(case_id, repeat_index)` attempts.
- **Check accumulated usage before starting the next logical recognition**, not after all of them.
- Persist and report `budget_exhausted` / `incomplete` distinctly from pass/fail.
- **If a field is a release threshold rather than a spend cap, rename it** so no operator can read it
  as a ceiling. Today the CLI help says enforced; the code does not enforce.

**Acceptance:** a second connection sees the run before execution; a simulated crash mid-run loses no
paid attempt; a budget set below the corpus stops early and reports `budget_exhausted`; help text and
behaviour agree.

### R5 — the closed vocabulary is enforced where it is claimed (1 day) · **F6**

Validate `reason_code` ∈ `RECOGNITION_FAILURE_CODES` and `index` at construction **and** at
persistence. On corrupt legacy data return `recognition_quality = null` with an internal diagnostic —
never silently accept, never silently drop part of a record. Consider a DB-level CHECK on the
`dropped_candidates` shape (next free migration; **verify the number at execution — 1071 is the
highest today**).

**Acceptance:** constructing a drop with a non-member code raises; the review's `<script>` probe can
no longer be reconstructed through the reader; a legacy row with a malformed payload reads as null
plus a diagnostic, and the API stays well-formed.

### R6 — one provider call per request, under concurrency (1 day) · **F7**

A transaction-scoped advisory lock over `(intent_id, recognition_request_hash)`, then a **second
lookup before dispatch**. The earlier intent lookup shares the same single-flight boundary — the
review notes it has the same fork risk.

**Acceptance:** two concurrent identical requests produce exactly one provider call and one row, both
callers receiving the same `recognition_id`; a slow first caller does not block an unrelated request.

### R7 — the status language matches reality (½ day) · **F5**

Correct every place the repo describes the seam as complete: the seam plan's status line, Task 6's
row, and this program's memory. Record that the motivating case is **not** in the release
denominator and that gold labels remain authored-pending-expert-review — then add the churn case to a
reviewed corpus so the eventual gate measures the thing that caused all of this.

**No provider call.** Running the gate stays a separate, explicitly-approved operator act.

## 2. Risks

- **R2 reverses a prior amendment.** Stated, argued, and bounded: the enum's only load-bearing job
  was forcing repair, which the semantic arm already does.
- **R2 changes a governed schema.** v3 ships under Task 1's frozen-bytes discipline; v2 is not
  touched.
- **R3 can refuse dispatch on a drifted deployment.** That is the point — but it is a fail-closed
  change to a live path, so its refusal must name both digests and the remedy.
- **R4 changes transaction structure around paid work.** Test the crash path explicitly rather than
  reasoning about it.
- **Not addressed here:** whether the deployed model is fit for this task. That needs the gate, which
  needs R3 and R4 first, and then the operator's go.

## 3. Sequencing

```
R1 (build)  ──►  R2 (recovery, schema v3)
                    │
                    ├──► R3 (identity)  ──┐
                    ├──► R5 (vocabulary)  ├──►  R4 (durable + capped gate)  ──►  R7 (status)
                    └──► R6 (single-flight)┘
```

**≈ 9 days.** R1 first because a broken build blocks everything. R2 next because it is the done-bar.
**R3 and R4 must both land before any paid gate runs** — R3 so the gate certifies the contract that
executed, R4 so a crash after paid calls does not lose the evidence.
