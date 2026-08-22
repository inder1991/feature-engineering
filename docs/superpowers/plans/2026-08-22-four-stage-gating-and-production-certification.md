# Six gated actions, one decision service, and a real production boundary

**Authority:** the product owner's rulings of 2026-08-22, quoted inline, as amended by their reviews
of this plan's first and second revisions. Where this plan and a ruling disagree, the ruling wins.
The three decisions this document used to leave open (D1/D2/D3) are **resolved below** — they are no
longer inputs to be gathered, they are constraints to be built.

> *"If the backend can safely and honestly render the selected formula, show the user the code. Gold
> evaluation decides whether the generation method is certified for production — not whether code can
> be inspected."*

> *"A missing reviewed expectation changes the AUTHORING METHOD. It must not automatically prevent
> code preview."*

> *"Certification must be checked BEFORE production materialization begins, not only before its
> results become visible."*

---

## 0. Where we actually are

**Done and green** (tree clean at `d4023825`):

| | |
|---|---|
| Certification machinery | evaluation contract (migration 1097), 12-case corpus, V2/V3 evaluation lane (1098), current-evaluation validity reader |
| Method matching | per-member provenance table (1099) **and its sealing writer** — `derive_member_provenance` / `record_member_provenance`, called from `seal_v2` and reached from the real lane at `generation_lane.py:543` |
| Blockers removed | `grain_refs` reaches authoring; `build_set_revision.declaration_json` typed |
| Live schema | **1097 + 1098 + 1099 all APPLIED to kind (ledger 195)** |

▲ **Migration 1099 is APPLIED. Every "1099 is files only" statement in earlier revisions of this
plan was true when written and is now STALE.** `record_member_provenance`
(`src/featuregen/materialize/authoring_provenance.py:236`) issues an unguarded
`INSERT INTO sealed_artifact_member_provenance`; the migration is now in front of the image, so the
deploy-ordering hazard that revision two raised is **discharged**, not outstanding. It stays recorded
here because the ordering rule it produced still applies to every migration after it: **a governance
table's writer and its migration are one deployment unit, migration first.**

▲ **Phase A of revision one is DONE.** The owner's first review was written against `d4072f08` and
correctly said `derive_authoring_method` had no caller and `sealed_artifact_member_provenance` had no
writer. Both were fixed at `364cd7fa`. The *standing rule* those examples produced (§14) stands
regardless.

**Not started:** all six gated actions, the single decision service, the exhaustive disposition
table, gold's relocation, the production materialization and publication boundary (endpoint, attempt
record, namespace, per-member certificate bindings), method identity, formula pinning, server-derived
action authorization, the deterministic certification programme, the operator UI journey, and the
end-to-end journey tests.

---

## 0.1 The prerequisite that cannot wait — the worker cannot ask "who?" today

▲ **The build API takes its authorization roles from the client, and they reach the read-scope
predicate.** This is not a design smell to tidy during cutover. It is a live seam, verified end to
end:

```
POST /build-sets/generations  body.roles: list[str] = Field(default_factory=list) build_sets.py:124
   → GenerationJob.roles                            roles=tuple(body.roles)       build_sets.py:258
   → validate_spine_declaration(..., roles=job.roles)                    generation_lane.py:496
   → compile_generation_v2(..., roles=job.roles)                         generation_lane.py:512
   → _resolve_nodes(conn, located, roles)                                        spine.py:562
   → allowed_classes(roles) → visibility_predicate()                             spine.py:586
```

`allowed_classes` (`overlay/upload/read_scope.py:155`) is the shipped read-scope fold — the one that,
since migration 1032, decides which governed-`restricted` columns a reader may see. **The caller
supplies the role set that decides which restricted columns its own build may read.**
`require_feature_generate` gates the ROUTE; the list inside the body gates the DATA.

▲ **And the authorization record cannot supply the missing actor.**
`GenerationAuthorizationV1.identity_payload()` (`generation_authorization.py:62`) says so in its own
words — *"No actor, no timestamp — those are provenance"*. `authorized_by` is written at
`generation_authorization.py:94` and `load_generation_authorization` reconstructs
`environment_id, logical_group_name, build_set_revision_id, target_mode, target_ref` and **nothing
else**. The worker has no actor to recheck against.

So a worker asked to re-evaluate the decision has exactly three options today, and none is
acceptable:

| Option | Why it fails |
|---|---|
| Trust `job.roles` | today's behaviour — the thing being forged is the input to the check |
| Use the worker's own identity | the worker becomes an authority nobody granted; every build runs at daemon privilege |
| Have no actor | every read-scope question fails closed, so nothing builds at all |

**Required, as a PREREQUISITE — before the decision service, not during cutover:**

1. **Delete `roles` from `GenerationIn`.** The route already resolves `identity: _Identity`
   (`build_sets.py`, `Depends(get_identity)`). Role claims come from there or from nowhere.
2. **Persist a server-derived, immutable `action_authorization_revision`** — append-only,
   content-addressed the way `generation_authorization` already is:

   | Field | Why |
   |---|---|
   | `action` | an authorization authorizes ONE act (§3); a generic one authorizes the strongest |
   | `actor_subject` + `actor_role_claims` | **derived from `IdentityEnvelope`, never from a body** |
   | `permission_result` | the governed permission decision, recorded, not re-asked later |
   | `read_scope_result` | the resolved `allowed_classes` fold, frozen as a value |
   | `policy_version` | which policy produced the answer |
   | `evidence_hash` | so the answer is re-derivable rather than asserted |
   | `decided_at` | provenance, outside `revision_id` — same rule as 1099's writer |

3. **`generation_request` references it by FOREIGN KEY, NOT NULL** (new migration ≥ 1100). A request
   that names no authorization is not a weaker request; it is not a request.
4. **The worker loads it and REVALIDATES** — re-derives the read-scope fold against current
   `graph_node` state and refuses on drift. Revalidation against a frozen record is the whole point;
   revalidation against a caller's claim is theatre.

▲ `validate_spine_declaration(..., roles=)` and `pilot_v2.compile_generation_v2(..., roles=)` keep
their signatures. **What changes is the SOURCE of that tuple** — it becomes the frozen
`read_scope_result` of an authorization revision rather than a field of the HTTP body. That keeps the
diff small and the invariant large.

---

## 0.2 Four facts that shape the sequence

1. **The readiness ladder has exactly ONE enforcing comparison** in production
   (`activation_policy.py:176`, `effective_readiness != MATERIALIZATION_READY`), gating
   `execute_materialization`. Everything else that reads readiness displays, sorts or reports it.
   **Removing gold from the ladder is measured to change ZERO served states** — only the three
   `gold_evaluation_unproven` blockers disappear.
2. ▲ **…and removing gold on its own helps almost nobody.** `recipe_readiness.py:83` sends any recipe
   without a reviewed expectation straight to `FORMULA_BLOCKED`, which is **295 of 317** recipes.
   *(Measured against the real registry: `V2_RECIPES` is 317 — 298 `deterministic_formula`, 11
   `conceptual_pattern`, 8 `governed_model_output` — and exactly 3 deterministic recipes satisfy
   `has_reviewed_expectation`: `merchant_mcc_diversity`, `obligor_facility_count`,
   `posted_debit_amount`. 298 − 3 = 295.)*
   ▲ **295 is the count of POTENTIAL LLM-FALLBACK RECIPES, not a set that becomes previewable.**
   Each of the 295 must still pass binding completeness, grain, currency/reversal/status policy
   resolution and renderer dispatch before a preview exists. **How many of the 295 clear all four is
   NOT MEASURED, and this plan does not claim it.** Phase B's first output should be that
   measurement, because it is the only honest denominator for "did this work".
   The 3 that reach `FORMULA_AUTHORABLE` become `FORMULA_VALIDATED` when gold is removed
   (`recipe_readiness.py:89`) — and `FORMULA_VALIDATED != MATERIALIZATION_READY`, so the equality
   check at `activation_policy.py:176` **keeps blocking them too**. **The ladder change alone moves
   nothing.**
3. **Candidate origin is NOT authoring method.** `formula_draft_worker` passes no
   `reviewed_blueprint` — the parameter exists only on `replay_authoring_v2.py:486` and no production
   caller supplies it — so it ALWAYS drives the LLM author and critic. Every formula today is
   `LLM_AUTHORED` whatever the recommendation's origin, and `formula_drafts.py:269` hard-codes
   `"formula_source": "llm_authored"` to match.
4. ▲ **Therefore "mixed-method" is not testable today.** A build combining a recipe-origin
   recommendation and an LLM-origin recommendation is mixed-**origin**; both members are
   LLM-authored. Phase F's mixed-method acceptance test is **impossible without D1**. That is the
   argument that decides D1, not a preference.

---

## 1. The three decisions, resolved

* **D1 — the deterministic reviewed-blueprint lane is IN SCOPE**, with LLM fallback. When a current
  reviewed executable blueprint exists, instantiate the formula deterministically with **zero
  provider calls**. When none exists, **explicitly** ask the LLM, then validate the result
  deterministically, and allow preview/sandbox if it is valid. Deferring D1 would leave the
  recipe-compiler certification programme certifying a path nothing uses, and would make the
  mixed-method journey test unwritable (fact 4). Task-level design lives in the sibling plan
  `2026-08-22-recipe-to-code-llm-fallback.md`; this plan owns the gating contract it must satisfy.
  ▲ **Its production ceiling is sandbox until §12's eight pieces exist.** Stated up front so nobody
  discovers it at Phase F.
* **D2 — DELETE the old materialization route. Do not build an adapter.** The product is pre-live and
  the migration surface is measured, not guessed:

  | Caller | What it is |
  |---|---|
  | `frontend/src/api.ts:4257` | `GET /materialization-runs/{id}` — the run sheet, behind `VITE_MATERIALIZATION_RUNS` (`nav.ts:41`) |
  | `POST /materialization-runs` | **no frontend caller at all** |
  | `tests/featuregen/api/test_materialization_runs.py` · `test_materialization_e2e.py` · `test_seam_walkthrough.py` | the three suites that drive it |

  So D2 is: migrate the run sheet onto a build-set request read, migrate those three suites, delete
  the route. **If an adapter is kept anyway, §8.3 states the only shape that is honest.**
* **D3 — sandbox IS allowed while certification is pending, and the warning is part of the
  contract.** It MUST be returned by the API and **DISPLAYED in the UI**. A warning that is computed
  and dropped is worse than no warning: it teaches the platform to believe it warned. Today the
  nearest surface, `SuggestionCard.tsx:210`, renders `gold_evaluation_unproven` as a *blocker* line —
  that copy has to become warning-shaped, not disappear.

---

## 2. Terminology — the conflation this plan must not reproduce

The word "materialization" currently covers six different things. Each is a separate act with a
separate gate, and the rest of this document uses only these names:

| Term | What it means | Its action (§3) |
|---|---|---|
| **Formula authoring** | deciding *what to calculate* — may legitimately show an incomplete formula with unresolved policy requirements | `AUTHOR_FORMULA` |
| **Preview generation** | producing the Kedro/PySpark project — requires a safe compile and render | `GENERATE_PREVIEW` |
| **Sandbox execution** | running the generated code against test data — touches a cluster | `EXECUTE_SANDBOX` |
| **Sandbox publication** | making test results available to look at | `PUBLISH_SANDBOX` |
| **Production materialization** | running the certified calculation and **storing production values** | `MATERIALIZE_PRODUCTION` |
| **Production publication** | making those values available downstream | `PUBLISH_PRODUCTION` |

▲ *"The older materialization path"* is the **historical name of a pipeline that conflated several of
these**, not a synonym for any one of them. When this plan says a gate belongs to sandbox
publication, that is a statement about which of the six acts it guards.

---

## 3. SIX actions, not five

```
AUTHOR_FORMULA · GENERATE_PREVIEW · EXECUTE_SANDBOX · PUBLISH_SANDBOX
              · MATERIALIZE_PRODUCTION · PUBLISH_PRODUCTION
```

▲ **The sixth is the owner's P0 correction, and the argument is the glossary's own.** §2 already
defines production materialization and production publication as separate acts. With five actions —
certification checked only at publication — **code could execute against production, read production
data and STORE PRODUCTION VALUES, stop before publication, and never have met the certification
gate.** Values written by an uncertified method are already a fact about the bank's data, whether or
not anything downstream can read them yet.

> **The rule:** certification is checked **BEFORE production materialization begins**, not only
> before its results become visible.

**If the product instead intends ONE ATOMIC production command**, this plan must say so explicitly
and then discharge all three of these — no partial version counts:

1. **No API, queue message, CLI or worker entry point can trigger production execution without its
   publication in the same governed act.** Enumerated and tested, not asserted.
2. **The production namespace is unreachable by the sandbox executor** — otherwise the sandbox path
   becomes the independent production-execution path.
3. **A partial failure leaves nothing readable** in the production namespace.

Until all three are demonstrated by test, **SIX actions**. The atomic design is a legitimate product
choice; it is not a way to skip building the gate.

### What exists today, and what has to be added

`EvaluatorAction` (`src/featuregen/overlay/upload/evaluator_contracts.py:43`) has exactly three
members — `GENERATE`, `VERIFY`, `PUBLISH_SANDBOX` — and its docstring says naming three is deliberate
because they are *"the three an execution chain gates"*. **There is no `PUBLISH_PRODUCTION` and no
`MATERIALIZE_PRODUCTION` anywhere in `src/`.**

| Action (service vocabulary) | Closest existing evaluator | Status |
|---|---|---|
| `AUTHOR_FORMULA` | none | new; not a chain gate |
| `GENERATE_PREVIEW` | `EvaluatorAction.GENERATE` | exists, misgated (§5, §6) |
| `EXECUTE_SANDBOX` | `EvaluatorAction.VERIFY` | exists; verify is the artifact/environment/permission half — widen it or confirm it is the whole sandbox-execution question |
| `PUBLISH_SANDBOX` | `EvaluatorAction.PUBLISH_SANDBOX` | exists; **leave unchanged** |
| `MATERIALIZE_PRODUCTION` | — | **does not exist** (§9) |
| `PUBLISH_PRODUCTION` | — | **does not exist** (§9) |

▲ Adding the two production members **requires rewriting that enum's docstring claim**, because both
ARE chain gates and the "deliberately three" sentence becomes false. `AUTHOR_FORMULA` and
`EXECUTE_SANDBOX` do not join the enum on that reasoning: the six-name vocabulary is the *service's*
`Action`, and `EvaluatorAction` is the subset the chain enforces. Do not silently widen one into the
other.

---

## 4. The blocker matrix — one column PER ACTION

▲ **The previous revision merged EXECUTE_SANDBOX and PUBLISH_SANDBOX into one "Sandbox" column, and
the merge produced a lie: "Artifact not verified → Sandbox: Depends".** It does not depend.
**EXECUTE_SANDBOX does not require verification — execution is what PRODUCES verification.
PUBLISH_SANDBOX requires a current passing one** — `VERIFICATION_NOT_CURRENT`,
`evaluate_execution.py:183`. One column per action makes the difference statable; the merged column
made it evasive.

| Condition | AUTHOR_FORMULA | GENERATE_PREVIEW | EXECUTE_SANDBOX | PUBLISH_SANDBOX | MATERIALIZE_PRODUCTION | PUBLISH_PRODUCTION |
|---|---|---|---|---|---|---|
| **No reviewed expectation, valid LLM-authored formula exists** | **Allow** | **Allow after deterministic validation** | **Allow + warn** | **Allow + warn** | **Require LLM-method certification + feature governance** | **Same certificate, re-derived** |
| **Reviewed executable blueprint exists** | **Deterministic, zero provider calls** | Allow | Allow | Allow | **Require deterministic-method certification (§12)** | **Same certificate, re-derived** |
| Method certificate pending / never run | Allow | Allow + warn | Allow + warn | Allow + warn | **BLOCK** | **BLOCK** |
| Method certificate stale | Allow | Allow + warn | Allow + warn | Allow + warn | **BLOCK** | **BLOCK** |
| Certificate exists but **method-mismatched** | Allow | Allow | Allow | Allow | **BLOCK** | **BLOCK** |
| Any member lacks method identity (§10) | Allow | Allow | Allow | Allow | **BLOCK** | **BLOCK** |
| Mixed-method artifact, one member's certificate stale | Allow | Allow | Allow | Allow | **BLOCK — all-must-pass** | **BLOCK — all-must-pass** |
| Missing customer relationship / grain | Block | Block | Block | Block | Block | Block |
| Target leakage | business formula visible | Block | Block | Block | Block | Block |
| Unsupported renderer operation | business formula visible | Block | Block | Block | Block | Block |
| Missing currency / reversal / status policy | business formula visible | Block | Block | Block | Block | Block |
| Caller lacks read authorization for the physical columns | **business-level formula only, no physical refs (§13)** | Block | Block | Block | Block | Block |
| No sealed artifact yet | n/a | n/a — preview produces it | Block | Block | Block | Block |
| Artifact sealed, **not yet verified** | Allow | Allow | **Allow — this is what produces verification** | **Block** | Block | Block |
| Verification stale | Allow | Allow | **Allow — re-execute** | **Block** | Block | Block |
| Artifact not servable (subgraph check refused) | Allow | Allow | Block | Block | Block | Block |
| Environment incompatible | Allow | Allow | Block | Block | Block | Block |
| Formula draft not pinned to the build set (§11) | Allow | **Block** | Block | Block | Block | Block |

**The rule, stated so it can be quoted back at a future change:**

> *A missing reviewed expectation changes the AUTHORING METHOD. It must not automatically prevent
> code preview.*

Which resolves to two routes and no third:

* **Reviewed blueprint exists** → instantiate the formula **deterministically. NO LLM calls.**
* **No blueprint** → **explicitly** ask the LLM, **validate deterministically**, and allow preview
  and sandbox if the result is valid.

There is no automatic silent fallback from a failed deterministic instantiation to the LLM: the
method is chosen from evidence before authoring, and a deterministic failure is a deterministic
refusal.

▲ **This table is illustrative, and it is not the contract.** The contract is §5's exhaustive
reason-code × action table. A matrix of representative conditions is exactly how a code with no
disposition gets shipped.

---

## 5. The EXHAUSTIVE disposition table — `reason code × action`

**Measured, not estimated:**

| | |
|---|---|
| Reason codes defined in `overlay/upload/semantic_eligibility_reasons.py` | **51** |
| Codes with a disposition in `ACTIVATION_BLOCKER_DISPOSITIONS` (`evaluator_contracts.py:61`) | **22** |
| Codes emitted by the shipped activation policy | **16** |
| Codes with **no disposition anywhere** | **29** |

The 29 are not an oversight in that table — its exhaustiveness test
(`tests/featuregen/overlay/upload/test_evaluator_contracts.py:51`) asserts equality against *codes
the activation policy emits* ∪ `EVALUATOR_ONLY_BLOCKERS`, and the 29 are emitted by the option and
eligibility layers instead. They include `TARGET_LEAKAGE_BLOCKED`, `CURRENCY_POLICY_MISSING`,
`STATUS_POLICY_UNRESOLVED`, `JOIN_PATH_DENIED`, `SOURCE_GRAIN_MISMATCH`,
`PROTECTED_CHARACTERISTIC_BLOCKED`, `OUTPUT_POLICY_INCOMPLETE` — **every row of §4's lower half.**
The decision service folds all six layers, so its table's domain is the union.

**Build:**

```python
class Disposition(StrEnum):
    BLOCK = "block"   # this action may not proceed
    WARN  = "warn"    # proceeds, and the caller MUST be told (D3)
    DROP  = "drop"    # this action is not the gate for this fact — never "ignored"

ACTION_DISPOSITIONS: Mapping[tuple[str, Action], Disposition]   # 51 × 6 = 306 cells
```

* **`DROP` keeps its existing meaning** (`evaluator_contracts.py` module docstring): *"this evaluator
  is not the gate for it"*. Anything that decides whether a computation is LEGAL or POSSIBLE stays
  `BLOCK`.
* **CI FAILS if any `(code, action)` pair is missing.** Extend the pattern already proved at
  `test_evaluator_contracts.py:51` — enumerate the reason module's codes by reflection, enumerate
  `Action`, assert the product is covered. Do not invent a second exhaustiveness mechanism.
* ▲ **This is the thing that stops a newly added code silently becoming a sandbox blocker.** Today an
  unknown code raises through `_explained` (`feature_execution.py:119`) at DISPLAY time — the wrong
  layer and the wrong moment: the build already ran.

**Codes that do not exist yet and this programme must add** (each needs a disposition on all six
actions from the day it lands):

| New code | Raised when |
|---|---|
| `METHOD_CERTIFICATE_MISSING` | no certificate covers this member's method identity |
| `METHOD_CERTIFICATE_STALE` | one exists, and is not current |
| `METHOD_CERTIFICATE_MISMATCHED` | one is current, and covers a different method identity |
| `METHOD_IDENTITY_UNRECORDED` | the member predates §10's companion table |
| `PROVIDER_UNAVAILABLE` | LLM authoring is required and the provider cannot be reached |
| `COST_AUTHORIZATION_MISSING` | LLM authoring is required and no approved cost ceiling covers it |
| `FORMULA_DRAFT_NOT_PINNED` | a build-set member names no `formula_draft_id` (§11) |
| `ACTION_AUTHORIZATION_MISSING` | the request references no `action_authorization_revision` (§0.1) |
| `FORMULA_REVIEW_UNMEASURED` | see §6 — the honest half of today's `FORMULA_SCHEMA_UNSUPPORTED` |

Families the table must cover by name, per the owner: conceptual patterns
(`CONCEPTUAL_PATTERN_NOT_AUTHORABLE`), governed model outputs, stale recipe review
(`RECIPE_REVIEW_NOT_CURRENT`), provider unavailable, cost authorization missing, legacy readiness
(`READINESS_NOT_MATERIALIZATION_READY`), method certification missing/stale, artifact states
(`ARTIFACT_NOT_SERVABLE`, `ENVIRONMENT_INCOMPATIBLE`), verification states
(`VERIFICATION_NOT_CURRENT`).

---

## 6. `GENERATE_PREVIEW` never consults readiness — and one blocker is mislabelled

> ▲ *"`GENERATE_PREVIEW` never consults `effective_readiness == MATERIALIZATION_READY`. It evaluates
> the formula, bindings, leakage, policies, permissions and renderer directly."*

Recipe readiness may remain a **discovery/maturity projection** — a way to sort and explain a
registry — but it must **STOP AUTHORIZING executable actions**. Every fact preview genuinely needs is
available without it: the formula and its bindings, the leakage check, the currency/reversal/status
policies, the caller's read authorization, and the renderer's capability for each operation.

▲ **The materialization rung has THREE enforcing blockers, not two** — verified in
`overlay/upload/activation_policy.py::_materialization_blockers`:

| Line | Blocker | Condition |
|---|---|---|
| :176 | `READINESS_NOT_MATERIALIZATION_READY` | `current.effective_readiness != MATERIALIZATION_READY` |
| :182 | `FORMULA_NOT_REVIEWED` | `not frozen.has_reviewed_formula_expectation` — **its own `if`**, independent of the readiness comparison |
| :187 | `FORMULA_SCHEMA_UNSUPPORTED` | `not current.formula_schema_supported` |

So even a hypothetical `MATERIALIZATION_READY` recipe still collects `FORMULA_NOT_REVIEWED`. All
three must be re-homed, not just the first.

▲ **And the third is a FALSE STATEMENT to the operator.**
`semantic_option_decision.py:403::_formula_schema_supported` begins:

```python
if not has_reviewed_formula_expectation(recipe_id):
    return False
```

…so an unreviewed recipe is told *"the execution engine does not advertise this formula schema —
engine support not implemented; never downgrade to an older schema"*. **That is false. Engine support
was never measured.** It is missing review wearing an engine-capability code, and it sends an
operator to the engine team for a governance problem. The same function's blanket
`except Exception: return False` folds "unknown engine" into the identical code.

**Fix, in Phase B:** `_formula_schema_supported` returns a tri-state —
`SUPPORTED | UNSUPPORTED | UNMEASURED` — and:

* `UNMEASURED` **because unreviewed** surfaces as `FORMULA_REVIEW_UNMEASURED` (§5), never as an
  engine claim. `FORMULA_NOT_REVIEWED` is already emitted separately at :182, so the governance fact
  is not lost.
* `UNMEASURED` **because the engine is unknown or the classification raised** surfaces as its own
  code with its own text.
* `FORMULA_SCHEMA_UNSUPPORTED` is reserved for the case it names: `classify_demands_for_engine`
  actually ran and actually said no.

▲ **Sequencing dependency:** **Phase C only works if Phase B has already removed the ladder from
preview authorization.** Relocating gold while `MATERIALIZATION_READY` equality still authorizes
preview would leave the same 295 recipes blocked by a different sentence, and §17's journey test
would pass its production assertion while failing its preview assertion for an unrelated reason.

---

## 7. The shared decision service — discriminated, typed, server-loaded

Revision two proposed one generic signature with an optional `artifact_id`. ▲ **That shape cannot
carry the evidence the six actions need**, because the evidence differs per action and an optional
field is how a required one goes missing. Replace it with **discriminated request types**:

| Action | The immutable evidence its request MUST carry |
|---|---|
| `AUTHOR_FORMULA` | selections · authoring strategy (`deterministic_blueprint` \| `llm_authored`) · provider contract ref *(required iff LLM)* · approved cost-ceiling authorization id *(required iff LLM)* |
| `GENERATE_PREVIEW` | selections · **exact `formula_draft_id` + content hash per member** (§11) · build declaration revision · renderer id + version · environment id |
| `EXECUTE_SANDBOX` | sealed artifact id · inventory observation id |
| `PUBLISH_SANDBOX` | sealed artifact id · verified output revision id · staging identity · publication capability attestation |
| `MATERIALIZE_PRODUCTION` | sealed artifact id · **production** environment id · production target ref · per-member certificate bindings (§10) |
| `PUBLISH_PRODUCTION` | sealed artifact id · materialized output revision id · per-member certificate bindings (§10) |

```python
def evaluate_action(
    conn, request: ActionRequestV1, *, action_authorization_revision_id: str,
) -> BuildActionDecisionV1: ...
```

* `ActionRequestV1` is the discriminated union of the six; **a request whose type does not match its
  action is a refusal, not a coercion.**
* The actor arrives ONLY through `action_authorization_revision_id` (§0.1). There is no `actor`
  parameter and no `roles` parameter — a parameter is a thing a caller can pass.

**It loads its own evidence, server-side, from immutable identities:**

```
considered_revision_id + option_id + decision_id
   → frozen semantic_option_decision
   → current activation state
   → pinned formula draft + content hash          (§11)
   → artifact + per-member provenance (1099) + method identity  (§10)
   → per-member certificate bindings              (§10)
```

**It returns:**

* one decision **PER MEMBER**
* a group-level verdict, **all-must-pass**
* blockers, each with its `(code, action) → Disposition` from §5
* warnings, which D3 requires the UI to render
* the policy version that produced them
* an evidence hash
* current-state revision pins

▲ **The client must not supply readiness, blocker codes, formula method, certificate identity, or
roles.** Anything a caller can pass is something a caller can forge. The only client inputs are
*which selections*, *which action*, and the action's own immutable references.

**Route BOTH paths through it.** The ruling: *"Both old and new APIs must receive the same answer"*
and *"do not keep two independent readiness implementations."*

---

## 8. Closing the bypasses

### 8.1 The empty default is a silent bypass

```python
activation_blockers: Sequence[str] = ()
```

at `src/featuregen/materialize/generate_v2.py:91` and
`src/featuregen/materialize/evaluate_execution.py:50`. Supplying nothing means "no blockers", which
means generation proceeds. ▲ **This is not hypothetical: the real lane never passes it.**
`generation_lane._drive` calls `generate_v2` at `generation_lane.py:531-545` with twelve keyword
arguments and `activation_blockers` is not among them — so the production generation path runs today
with the empty default.

Remove the default. Require instead:

```python
action_decisions_by_member: Mapping[str, ActionDecisionV1]   # NO default
```

with:

* **EXACT coverage** of every compiled member — no extra keys, no missing keys;
* **one decision per member**;
* **the expected action on each** (a decision for a different action is a refusal, not a near-miss);
* **any refused member prevents rendering** — all-must-pass, not best-effort;
* unknown or missing decisions **FAIL CLOSED**.

### 8.2 Check the decision TWICE

* **At request time** — so a person gets an immediate, actionable answer.
* **In the worker, immediately before the act** — which prevents a direct queue or API bypass, and
  catches current-state drift between the request and the render.

Two checks are not redundancy; they answer at two different moments about a state that can move. ▲
The worker's check is only meaningful once §0.1 lands — before that it has no actor to check with.

### 8.3 ACTION-DECISION equivalence, not route equivalence

`materialization_runs.py:486` documents a keyless bypass in its own words: *"No option key is not a
refusal."* A work-item-driven request with no `considered_revision_id` and no `option_id` returns
`{}` and proceeds.

▲ **And the route is not a preview route.** `POST /materialization-runs` enqueues a job that
compiles, renders, seals, runs L0 and **can publish** — the status read reports `published_object`,
`published_generation_id` and `published_at` (`materialization_runs.py:305-324`) and its terminal
state `PUBLICATION_REFUSED` is a *publication* verdict. The build-set lane, by contrast, stops at a
sealed artifact. **There is no "old API preview" operation to map.**

Therefore:

* **Mapping the old endpoint to `GENERATE_PREVIEW` alone is a BYPASS**, not a simplification: its
  later execution and publication would proceed under a decision made for a weaker act.
* **Preferred (D2): delete the route** once the run sheet and the three test modules are migrated.
* **If an adapter is kept**, it must **orchestrate the canonical stages and call `evaluate_action`
  with the correct action before EACH stage** — preview, then sandbox execution, then publication —
  and must **REJECT the legacy keyless request with a typed deprecation error** when a member cannot
  be resolved to a real selection revision.
* **Equivalence tests compare DECISIONS, not routes**: for the same selections and the same action,
  both paths produce the same `BuildActionDecisionV1` — same per-member blockers, same warnings, same
  policy version. A test over a route that can skip the evaluation proves only that both routes ran.

---

## 9. A real production boundary — P0

Revision one described `evaluate_publish_production` with **no authoritative caller**. An evaluator
nothing calls is a description of a gate, not a gate. Add, explicitly, **both** production acts:

```
POST /feature-execution/production-materializations
    → evaluate_action(MaterializeProductionRequestV1)
    → record per-member certificate bindings on the attempt
    → enqueue production materialization
    → worker re-checks attempt-bound evidence, re-derives every method identity
    → materialize or refuse

POST /feature-execution/production-publications
    → evaluate_action(PublishProductionRequestV1)
    → record per-member certificate bindings on the attempt
    → enqueue production publication
    → worker re-checks attempt-bound evidence, re-derives every method identity
    → publish or refuse
```

(The `/feature-execution` prefix already hosts `/generations`, `/verifications` and `/publications`
in `src/featuregen/api/routes/feature_execution.py`; `/publications` is **sandbox** publication and
stays as it is.)

Plus, all in the same change:

1. the **two production evaluator actions** (§3);
2. a **production attempt record for each** — carrying the per-member certificate bindings, so the
   answer is re-checkable and cannot drift between decision and act;
3. a **production namespace / publisher path** distinct from `sandbox_feature`;
4. a **method-level certificate reader**. `current_evaluation_validity`
   (`overlay/upload/current_evaluation_validity.py:76`) is expectation-specific and cannot answer for
   a novel LLM feature that has no recipe expectation. The question it must answer is *"was this
   exact method identity certified by the platform corpus, and is that certificate current?"*

Both evaluators require: current artifact verification · the corresponding production permission ·
production capability attestation · data-use and read authorization · **method-matched** current
certificates · **the exact certificate revision recorded per member on the attempt** · all artifact
members passing.

▲ **Until both endpoints, the attempt records and the namespace exist, gold has not been RELOCATED —
it has only been REMOVED from one place and DESCRIBED in another.** Phase C is not complete when an
evaluator function merges.

▲ **The gate does NOT go on the current publish path.** That path is `PUBLISH_SANDBOX` on the
`sandbox_feature` namespace; putting certification there blocks sandbox testing. `chain.py:646`'s
pattern is publication-*mechanism capability*, not certification. **Leave verification and
`PUBLISH_SANDBOX` unchanged.**

**Hard-block from day one. No transitional "certification required only once a certificate exists"
rule** — absence would act as permission, and earning the first certificate would make the platform
stricter than before.

---

## 10. Method identity, and one certificate binding PER MEMBER — P0

▲ **Today's provenance cannot match a certificate exactly.** `derive_authoring_method`
(`authoring_provenance.py:85-92`) records exactly this evidence:

```python
{"authoring_run_id": ..., "author_dispatch_count": len(author_refs),
 "critic_dispatch_count": len(critic_refs), "dispatches_reconciled": reconciled,
 "review_bypassed_events": bypass}
```

Call **counts**, a reconciled **boolean**, and a bypass **count**. That answers *which of two
methods*. It does not name **which model**, **which author or critic contract**, **which blueprint
revision** or **which expectation** — and a certificate certifies exactly those. Matching a
certificate against a count is matching on the wrong thing.

**Add a `method_identity_hash` per artifact member**, over a canonical (JCS) payload:

| Method | The identity payload |
|---|---|
| `LLM_AUTHORED` | provider · model id · author contract ref + hash · critic contract ref + hash · formula schema version · grammar version · governing policy version |
| `REVIEWED_RECIPE_BLUEPRINT` | blueprint revision id · blueprint content hash · expectation hash · **expectation generation (V1/V2)** · producer version · compiler version · grammar version |

▲ **Expectation generation is load-bearing, not decoration** — `has_reviewed_expectation()` unions
two registries and **two of the three reviewed recipes are Formula V1**, so "a reviewed expectation
exists" does not by itself identify a V3-producible method.

**Storage — 1099 is APPLIED and append-only, so extend, never alter.** The table carries the trigger
`sealed_artifact_member_provenance_no_change` (BEFORE UPDATE OR DELETE, raises). `ADD COLUMN` would
succeed and then be unfillable, because the backfill is an `UPDATE`. So:

```
sealed_artifact_member_method_identity      -- NEW migration, ≥1100, append-only, same guard
    artifact_id, member_name                -- FK → sealed_artifact_member_provenance
    method_identity_hash                    -- 64 chars
    method_identity_json                    -- inspectable, so the hash is checkable
    derived_at
    PRIMARY KEY (artifact_id, member_name)
```

Written by the same sealing act that writes 1099's rows, in the same transaction, with the same
all-members-or-none refusal.

**Certificate binding is PER MEMBER, not per attempt:**

```
production_attempt_member_certificate       -- NEW migration
    attempt_id, member_name
    certificate_revision_id                 -- the EXACT revision
    method_identity_hash                    -- what it was matched against
    PRIMARY KEY (attempt_id, member_name)
```

▲ **A mixed artifact needs SEVERAL certificates** — an LLM certificate for one column and a compiler
certificate for the next — and one certificate on the attempt could only ever be right about one of
them. This is the same shape argument migration 1099 already made for the method itself, applied one
level further.

**At each production act:** re-derive every member's method identity from live evidence, compare to
the stored `method_identity_hash` AND to the identity the bound certificate covers. **All must pass.
Any mismatch refuses the whole act.**

▲ **Existing provenance rows are permanently INELIGIBLE for production and must be regenerated.**
Not "backfill later": the evidence a backfill would read — the deployed model, the live contracts —
may have moved since sealing, so a backfill records *today's* identity against *yesterday's* bytes.
That is a fabricated certificate match, which is worse than an absent one.
`METHOD_IDENTITY_UNRECORDED` (§5) is the honest answer, and regeneration is the honest remedy.

---

## 11. A build set must PIN its formula — P0

▲ **Verified: it does not.** `build_set_member` — migration 1092
(`1092_build_set_and_generation_request.sql:68`) — is `(revision_id, position,
selection_revision_id)` and nothing else. The migration's own comment explains why:

> *"the formula it resolves to is looked up through it rather than copied, so a set cannot drift from
> the choice it records"*

**That reasoning is inverted.** The selection is stable; the DRAFT is not. `restore_formula_v3.py:90`
resolves it with `ORDER BY d.updated_at DESC LIMIT 1` — *the newest draft for this selection,
whenever the worker happens to look*.

The failure, concretely:

```
t0  request-time decision evaluates formula A                    → allowed
t1  a new draft B lands for the same selection (retry, re-author, operator fix)
t2  worker restores B                                            → builds different code
```

`build_set_revision.content_hash` is over *(target reading, ordered members, declaration)*, so the
build-set identity **does not move**. Same identity, different feature. And a retry of a failed
build changes what the build MEANS — which is exactly the property an idempotency guard exists to
prevent.

**Fix:**

1. `build_set_member` gains `formula_draft_id text NOT NULL` and `formula_content_hash text NOT NULL`
   (new migration ≥ 1100).
2. **Both enter `content_hash`.** Pinning that does not change identity is not pinning.
3. `restore_formula_v3` selects **BY draft id**, and refuses a content-hash mismatch — it already has
   the vocabulary (`INTENT_HASH_MISMATCH`).
4. The §0.1 action-authorization revision covers the pinned pair, and the worker re-checks it.
5. `GENERATE_PREVIEW` requests carry the pinned pair per member (§7).

▲ **Legacy rows cannot be migrated in place, and this is a judgement call the owner should confirm.**
A `NOT NULL` backfill would have to run `ORDER BY updated_at DESC` — the exact read being removed —
freezing whatever happens to be newest at migration time as though someone had chosen it.
**Recommendation: add the columns NULLABLE, refuse `NULL` at the request gate with
`FORMULA_DRAFT_NOT_PINNED` (§5), and mark legacy build sets unbuildable.** Honest absence over
invented pinning. If the owner prefers a hard `NOT NULL`, existing rows must be deleted rather than
backfilled.

---

## 12. The deterministic certification programme is only NAMED

Phase D ships a deterministic authoring lane. **Nothing certifies it.** Eight pieces, none of which
exist:

| # | Piece | What exists today |
|---|---|---|
| 1 | Evaluation-contract identity for the compiler programme | migration 1097's contract describes **model and prompt identity** — a compiler contract is a different shape, not the same row with nulls |
| 2 | A reviewed case corpus for the compiler | the 12-case corpus is the LLM lane's |
| 3 | The runner | nothing walks a compiler corpus |
| 4 | **Comparison rules** | ▲ **undecided, and not decidable by engineering alone** — byte-identical IR? equivalent results within tolerance? the owner must rule |
| 5 | The certification record | LLM-shaped only |
| 6 | Current/stale reader | `current_evaluation_validity.py:76` is expectation-specific |
| 7 | API + UI results | Phase E's page is scoped to one programme |
| 8 | Production certificate matching | §10 |

▲ **Until all eight exist, reviewed-blueprint formulas can PREVIEW and reach SANDBOX but can NEVER
satisfy production certification.** That is the honest ceiling of D1, and it is stated in §1 so it is
not discovered at Phase F. It is not a defect; it is the cost of D1 being in scope.

---

## 13. Code retrieval needs its own authorization

▲ **Verified: it has none.**

| Surface | Its only gate | What it returns |
|---|---|---|
| `GET /feature-execution/{artifact_id}/code` (`feature_execution.py:244`) | `require_feature_read` = `feature:read` (`deps.py:77`) | the generated Kedro/PySpark project files |
| `GET /formula-drafts/{formula_draft_id}` (`formula_drafts.py:232`) | `require_permission("feature:generate")` | `"formula": draft.formula_json` (:272) |

Neither consults `allowed_classes` / `read_scope.visibility_predicate` for the physical columns the
artifact reads. **A generated project names schema, table and column.** A general read permission is
not a read-set authorization for the specific physical columns in this project.

**Two acceptable answers — choose per surface, and state which:**

* **(a) Recheck read scope at serve time** against the artifact's read refs, and refuse with
  `EXECUTION_AUTHORITY_UNMET`; or
* **(b) Serve a redacted, business-level formula with NO physical references** — concept names,
  operators, windows, policies; no schema, table or column.

The same rule applies to formula-detail retrieval. Preview code and formula detail are the same
question asked at two altitudes.

▲ **This also qualifies §4's "formula visible when the user lacks read permission".** It means the
**business-level** formula. Physical table and column names may themselves be restricted metadata —
that is what `graph_node_visibility_requires` (migration 1032) and the sensitivity display floor
(migration 1042) exist to enforce. **"Formula visible" never means "physical plan visible."**

---

## 14. Certification is not feature approval

A current platform certificate means the **METHOD** is trusted enough to be **CONSIDERED** for
production. It is a precondition, not an approval.

A production feature must **still** pass, every time:

formula validation · target-leakage checks · grain and join checks · currency/reversal/status
policies · data-use and read authorization · engine compatibility · artifact verification · sandbox
checks · production permission and capability · current, **method-matched** certification for
**every member**

Conflating the two produces both failure modes: a certified method waved through a leaking feature,
and a perfectly governed feature refused because the platform's evaluation job has not run this week.

---

## 15. Standing rule — a repeated failure in this codebase

> ▲ *"Never land a governance function without its enforcement point in the same change."*

| Example | Status |
|---|---|
| `derive_authoring_method` had no caller | **fixed at `364cd7fa`** — one commit after it landed |
| `sealed_artifact_member_provenance` (1099) had no writer | **fixed at `364cd7fa`**, migration applied at ledger 195 |
| `evaluate_publish_production` proposed with no endpoint | **still true — the function does not exist, and §9 is what stops it repeating** |
| `activation_blockers: Sequence[str] = ()` with no caller supplying it | **still true — §8.1** |

The rule: a merged governance function whose enforcement point is "next phase" is indistinguishable,
from the outside, from a governance function that does nothing.

---

## 16. The two journeys are separate

They share a vocabulary and nothing else. Documenting them as one is how gold ended up on the
readiness ladder.

**The NORMAL HYPOTHESIS JOURNEY** (an analyst):

```
submit hypothesis → recommendations → select → formulas → preview code → sandbox → production
```

**The PLATFORM EVALUATION JOURNEY** (an admin/SME):

```
open Governance → Formula quality → run the reviewed corpus
    → evaluate the DEPLOYED model / prompts / contract      (LLM programme)
    → evaluate the DEPLOYED compiler / grammar / producer   (deterministic programme, §12)
    → produce a certificate: current | stale | failed
    → consumed ONLY by production materialization and production publication
```

▲ **The hypothesis user neither triggers nor waits for certification.** Nothing in the first journey
may block on the second before its final two steps.

---

## 17. The phases

Ordered by dependency, not size. **Phase 0 is a prerequisite and Phase B is the spine**; the rest
attach to B, and doing it late means writing each gate twice.

### Phase A — the sealing writer ✅ DONE at `364cd7fa`, migration applied
Per-member provenance is derived and written inside `seal_v2`, from the one place where
`RestoredFormulaV3` (which carries `selection_revision_id` and `formula_draft_id`) and
`AdmittedFeatureV2` (which carries `feature_name` and `proposal_content_hash`) meet. An undecidable
method refuses the seal by name — `MemberProvenanceRefused` → `AUTHORING_RUN_INCOMPLETE`, terminal,
not retried.

### Phase 0 — PREREQUISITE: server-derived authorization + formula pinning ▲ BEFORE the service
Neither can wait for cutover, because the decision service's worker-side check is meaningless without
them (§8.2).

* **§0.1** — delete `roles` from `GenerationIn`; add `action_authorization_revision` (append-only,
  content-addressed, actor + permission result + read-scope result + policy version + evidence hash);
  `generation_request` FK, NOT NULL; worker loads and revalidates; `job.roles` becomes the frozen
  `read_scope_result`.
* **§11** — pin `formula_draft_id` + `formula_content_hash` per build-set member; both enter
  `content_hash`; `restore_formula_v3` selects by draft id and refuses hash mismatch; legacy rows
  refused, not backfilled.

**Proof:** a request that supplies elevated roles in its body gets exactly the read scope its
identity earns — asserted against a governed-`restricted` column. A build whose selection acquires a
newer draft between request and worker **refuses**, and its build-set identity is unchanged.

### Phase B — the six actions and ONE decision service ▲ THE SPINE
* Split the single `MATERIALIZATION_READY` authorization into the **six** action decisions of §3.
* Build the **discriminated request types** and `evaluate_action` exactly as §7 specifies —
  server-loaded evidence, per-member decisions, all-must-pass, warnings, policy version, evidence
  hash, revision pins.
* Build the **exhaustive `ACTION_DISPOSITIONS` table + its CI exhaustiveness test** (§5).
* **Split `_formula_schema_supported` into a tri-state** and stop the mislabel (§6).
* **Remove all three ladder blockers from preview authorization** (§6). This is the half of Phase B
  that Phase C depends on.
* Remove `activation_blockers`' empty default; require exact per-member coverage (§8.1).
* Route **both** paths through it (§8.3).
* ▲ **First output: the measurement fact 2 asks for** — of the 295 potential LLM-fallback recipes,
  how many clear binding, grain, policy and renderer. That number is the denominator for every later
  claim about this programme.

**Proof:** a table-driven test over §4's matrix, **all six columns**, asserted through BOTH routes,
plus the CI check that no `(code, action)` cell is missing.

### Phase C — the production boundary + gold relocation ▲ ATOMIC, ONE COMMIT
> *"Do not merge 'gold removed from readiness' without simultaneously introducing the production
> publication gate; that would temporarily remove the protection instead of relocating it."*

* Remove gold from `recipe_readiness` — **keep `BLOCKER_GOLD_UNPROVEN` in the fold-owned set** so
  legacy rows strip it rather than re-entering it as a governed policy blocker (which would pin every
  legacy candidate at `FORMULA_BLOCKED`, strictly worse).
* Ship **everything in §9 and §10 in the same commit**: both evaluator actions, both endpoints, both
  attempt records, the production namespace, the method-level certificate reader, the method-identity
  companion table and its sealing writer, the per-member certificate bindings, both worker
  re-checks.
* Fold all artifact members **all-must-pass**, at **both** production acts.

### Phase D — the deterministic reviewed-blueprint lane (D1)
Resolve the method **server-side, from evidence, before authoring**: reviewed current executable
blueprint → `deterministic_producer` with **zero provider calls**; otherwise → explicit LLM authoring
with the cost shown first, then deterministic validation. Stop `formula_drafts.py:269` hard-coding
`llm_authored` and read the evidence instead. The resolver must return the **expectation generation**
too (§10). Detailed tasks live in `2026-08-22-recipe-to-code-llm-fallback.md`.
▲ Ceiling: sandbox, until §12.

### Phase E — the operator journey (Governance → Formula quality)
UI page → `POST /formula-evaluations` → queue runner over the frozen cases → progress endpoint →
results report. Cost-confirmed before starting (calls, token budget, max cost); **the backend reads
the deployed configuration — the operator never types prompt hashes.** Outcomes:
`CERTIFIED_CURRENT · PASSED_NOT_CERTIFIABLE · FAILED_QUALITY · FAILED_TECHNICAL · STALE`.
**Two programmes now that D1 is in scope: LLM authoring and recipe compiler** — and the compiler one
needs §12's eight pieces first, of which #4 is a product ruling.

**The corpus runner is the critical missing piece:** the backend can describe and score an
evaluation, but nothing walks the cases as a user-triggered job.

### Phase F — the journey tests ▲ BEFORE removing the old path
Both methods, end to end: hypothesis → recommendation → selection → formula → preview project →
inspect code → sandbox execution → sandbox publication → attempt production materialization → attempt
production publication. §18 is the decisive one.

Must also prove: certification pending does **not** block preview · it **does** block production
**materialization** · target leakage blocks preview · missing currency/reversal blocks preview · a
reviewed-blueprint member and an LLM-authored member build together in one artifact
(**mixed-METHOD**, which Phase D makes possible) and require **two different certificates** · stale
certificate refused · mismatched certificate refused · missing method identity refused · a
caller-supplied elevated role changes nothing · an unpinned formula draft refuses · **every API gives
the same decision for the same action**.

### Phase G — cutover
Make the build-set workflow canonical; **delete** the old materialization route once decision-
equivalence passes and the run sheet and three test modules are migrated (D2). ▲ **The build-set
authorization gaps are NOT here** — they moved to Phase 0, because a cutover that carries them
forward is a cutover onto an unauthorized lane.

---

## 18. The decisive journey test

Given a valid **"90-day incoming amount minus prior 90-day"** formula with complete grain and
bindings, resolved policies, a supported renderer, a pinned formula draft, and **NO current method
certificate**:

```
Old path preview decision       = allowed + METHOD_CERTIFICATE_MISSING warning
New path preview decision       = allowed + IDENTICAL warning
Generated artifact              = exists, every member carries a method_identity_hash
Code view                       = available (read scope rechecked, §13)
Sandbox execution decision      = allowed + warning        (verification does not exist yet)
Sandbox publication decision    = blocked until the execution produces a passing verification
Production MATERIALIZATION      = blocked by METHOD_CERTIFICATE_MISSING   ← the gate that must exist
Production publication decision = blocked by METHOD_CERTIFICATE_MISSING
```

▲ **And it must FAIL when each defect is reintroduced.** A test that only passes is a test that
proves nothing here. Reintroduce these one at a time and confirm the failure:

| Defect reintroduced | Which assertion must break |
|---|---|
| restore `MATERIALIZATION_READY` equality on preview | preview allowed |
| restore the empty `activation_blockers=()` default | worker refusal on a refused member |
| skip the new worker's evaluation | second-check drift detection |
| skip the old path's evaluation | "identical warning" through both paths |
| put certification on sandbox | sandbox execution allowed + warning |
| **gate production publication only** | **production MATERIALIZATION blocked** |
| remove the production certificate check | `METHOD_CERTIFICATE_MISSING` |
| **restore client-supplied `roles`** | **elevated body roles change nothing** |
| **unpin the formula draft** | build-set identity binds one formula |
| **bind ONE certificate per attempt instead of per member** | mixed-method artifact requires two |
| merge `EXECUTE_SANDBOX` and `PUBLISH_SANDBOX` decisions | unverified artifact executes but does not publish |

Verify the injection actually applied, and read pytest's summary line — `grep -c "^FAILED"` silently
matches nothing against coloured output and reports a false pass.

---

## 19. Risks

* **Phase 0 is a prerequisite people will want to reorder.** It looks like cleanup and it is the
  authorization model. Every worker-side recheck built before it is decorative.
* **Phase C's atomicity is the one that bites.** Landing the ladder change alone removes protection
  rather than relocating it. This has already been attempted once and reverted. §9's and §10's
  artefacts are part of the atom, not follow-ups.
* **Phase C also depends on Phase B's preview half** (§6). Getting the order wrong produces a green
  production assertion and a red preview assertion for an unrelated reason, which reads as a product
  question and is not one.
* **Placement.** A gate put on the sandbox path blocks sandbox testing. When a change makes many
  tests on ONE path fail, treat that as a placement signal before treating it as a product question.
* **Weak tests.** Tests that construct their own fixtures and assert them back prove nothing here.
  Drive the real path; §18's reintroduction table is the check that the tests have teeth.
* **The 295 is a ceiling, not a forecast** (fact 2). If Phase B's measurement returns a small number,
  that is information about binding/grain/policy/renderer coverage, not a failure of this plan — but
  it must be reported rather than absorbed.
* **Migration ordering.** 1099 taught the rule and is now applied; §0.1, §10 and §11 each add a
  migration (≥1100) whose writer must not ship ahead of it.

---

## 20. Not engineering, and not blocking these phases

▲ **"Nine expert sign-offs" understated it by roughly a factor of three.** What is actually needed to
grow the reviewed corpus past its single clean case:

* **nine additional CLEAN CASES**, each with its reviewed fixture; and
* per case, approved `recipe_review_event` rows from **every** role
  `recipe_review_validity.required_reviewer_roles` names. For a `deterministic_formula` recipe that
  is at minimum **three** — `banking_sme`, `data_semantic_owner`, `formula_engineering` — and more
  when the recipe carries a `privacy_purpose:` ref (`privacy_compliance`), a `risk_corridor:` or
  `model_output:` ref (`treasury_regulatory_accounting`), or `near_label`/`outcome` leakage
  (`model_risk`);
* under the **multi-person rule** — `ReviewValidityV1.single_identity_violation` refuses one person
  signing a multi-role requirement.

**So: at least 27 approval events, plus nine reviewed fixtures — not nine signatures.** Until then no
evaluation is *certifiable*, so production stays blocked, which is the intended invariant rather than
a defect.

▲ Note the shape this gives the product once §4's rows are built: with certification uncertifiable
and 295 recipes lacking a reviewed expectation, **the LLM route with deterministic validation is the
only route to preview for almost every recipe**, and production is closed for all of them. That is
the honest state, and it is exactly why preview must not be gated on the thing that cannot yet
happen.

---

## 21. What this plan already gets right — confirmed by the owner, do not regress

Recorded because each of these was a live defect once and a future change will be tempted by each:

| Ruling | Where it lives |
|---|---|
| Gold does **not** block formula or code inspection | §4, §6 |
| A missing reviewed expectation selects the **LLM path**, not a dead end | §1 D1, §4 |
| Candidate **origin** is distinct from authoring **method** | §0.2 fact 3, migration 1099's own header |
| Preview **stops depending** on `MATERIALIZATION_READY` | §6, Phase B |
| Missing action decisions **fail closed** | §8.1 |
| **Request AND worker** boundaries both enforce | §8.2 |
| Production certification is **method-matched** and **all-members-must-pass** | §9, §10 |
| Gold removal and the production gate **land together** | Phase C |
| **Defect-injection** journey tests are required, not optional | §18 |
