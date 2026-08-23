# Deep review: the two 2026-08-22 plans, against the code they describe

**Reviewed:** `docs/superpowers/plans/2026-08-22-four-stage-gating-and-production-certification.md`
(parent, 2,586 lines) and `docs/superpowers/plans/2026-08-22-recipe-to-code-llm-fallback.md`
(child, 1,338 lines), both as amended by revision four.

**Against:** `feature/asset-detail-reapply` at `3c52a9de`, and the live kind cluster (ledger 195,
image now aligned to 1099).

**Method:** every claim below was checked by opening the module or querying the database. Where a
plan claim was checked and found accurate, it is recorded as such rather than omitted — a review
that lists only defects gives no signal about what was verified.

---

## Summary

The plans' architecture is sound and revision four's corrections hold up. **What this pass found is
a different class of problem: four live defects that neither plan knows about, and which the plans'
own designs would inherit or depend upon.** Three of the four are in the exact machinery the
programme is built on — draft identity, authorization consumption, and certificate matching.

| | |
|---|---|
| **Blockers** — live defects the plans inherit or assume away | **4** |
| **Major** — plan defects: over-built, under-built, or missing a case | **6** |
| **Moderate** | **2** |
| Plan claims re-verified as ACCURATE | 6 |

▲ **The single most important finding is B1.** It is live, it explains the state of the cluster, and
both plans reproduce it in their corrected request order.

---

## BLOCKERS

### B1 — A failed draft poisons its candidate permanently, and the plans rebuild the trap at V2

**The chain, every link verified:**

| # | Fact | Where |
|---|---|---|
| 1 | `BLOCKED` and `FAILED` are terminal — their transition sets are literally `frozenset()` | `formula_draft_store.py:98-99` |
| 2 | `request_draft` returns an existing draft of **any** state as `(id, created=False)`. It checks retirement; it never checks state | `formula_draft_store.py:335-367` |
| 3 | the route enqueues **only** `if created` | `formula_drafts.py:172` |
| 4 | so a second request for the same identity spends nothing, queues nothing, and returns the dead draft | — |
| 5 | the escape is to move an identity-bearing input — but `authoring_config_hash` is a **constant** (`getattr` on a dict), so **no configuration change can move the identity** | `formula_drafts.py:279` |

**The only remaining escapes are a new considered revision, a new catalog snapshot, or an edited
definition.** A transient fault has none of those. There is no retry.

**The live cluster proves it.** All seven drafts are terminal — four `FAILED`, three `BLOCKED`, zero
`READY`. Two of the four say this in their own `failure_reason`:

> *"the authoring run could not complete (run far-596a06f071d09c273af791c171e2fbe2); **this is a
> platform or provider fault, not a problem with the candidate**"*

▲ **The system correctly identifies that the candidate is not at fault, writes a terminal state
anyway, and thereby bars that candidate for ever.** Written at `formula_draft_worker.py:277-281`,
where `technical_status == "technical_failure"` terminalizes to `FAILED`.

A third was `RuntimeError: formula drafting has no resolved principal` — a wiring fault. A fourth,
`REQUESTER_REVOKED — local user is missing or disabled`: a draft is permanently blocked because the
person who asked for it was later disabled, for every other user too.

**Why the plans inherit it.** The corrected request order (parent §11.1.1) reads:

```
4. exact V2 identity hit    -> return it, spend nothing
```

State-blind, exactly like `request_draft` today. **A `FAILED` V2 draft will be returned for ever**,
and the money guard becomes a permanent failure cache the moment the first provider timeout lands.

**Required correction.** The money guard's question is *"have we already BOUGHT this answer?"*, and a
technical failure did not buy an answer. Split the identity hit by state:

* `READY` → return it, spend nothing. The guard working as designed.
* `BLOCKED` → return it. A business refusal **is** an answer about the candidate; re-buying it
  changes nothing.
* ▲ `FAILED` / `CANCELLED` → **not an answer.** Permit exactly one re-attempt, under an explicit
  authorization (parent §11.2 already supplies the shape) and with the attempt recorded, so a
  poisoned identity is recoverable without pretending the failure did not happen.
* `REQUESTED` / in-flight → return it, queue nothing. Today's double-click answer, unchanged.

▲ **This also needs `worker.retryable` widened.** `formula_draft_worker.py:199` treats only
`LeaseFenceLost` and `RecoveryRequiresReconciliation` as transient; the bare `except Exception` at
:205 permanently terminalizes provider timeouts, billing exhaustion and connectivity faults alike.
`technical_failure` should route to the retryable arm, not to `FAILED`.

---

### B2 — Any caller may consume another actor's generation authorization

**Live, and neither plan nor the verdict flags it.** Both plans treat `roles` as the client-supplied
authority input. `GenerationIn` carries a second one:

```python
class GenerationIn(BaseModel):                       # build_sets.py:112
    build_set_revision_id: str
    generation_authorization_revision_id: str        # <- the CLIENT names the approval
    ...
    roles: list[str] = Field(default_factory=list)   # <- the known one
```

The route validates that the authorization covers the **build set** (`build_sets.py:202`, 409 if
not). **Nothing compares the authorization's grantee to `identity.subject`.** Migration 1095's chain
proves the approval covers this build set in this environment; it says nothing about who may spend
it.

So a caller holding `feature:generate` may name another user's approval for that build set and build
under it, in the environment and logical group **that approval** names. `requested_by` records who
ran it, so the audit shows the actor — while the approval consumed belongs to somebody else.

▲ **Parent §0.1.1 does not close this either.** It lists `actor_subject` as a binding and requires
the *worker* to revalidate current entitlement, but never states the request-time rule:

> **The caller must BE the authorization's grantee** (or hold a recorded delegation from them,
> §0.1.1's durable-delegation branch). An authorization is presented, not merely referenced.

---

### B3 — The per-member certificate binding points at a table that does not exist

Parent §10 and §10.2 require, per artifact member, `certificate_revision_id` — *"the EXACT
revision"*. **There is no certificate table in the schema.** The certification tables are:

```
recipe_formula_eval_run · recipe_formula_eval_case(_v2) · recipe_formula_eval_attempt(_v2)
recipe_formula_evaluation_contract
```

No `..._certificate` anywhere. What plays the role of a certificate today is a **derivation**:
`current_evaluation_validity` (`current_evaluation_validity.py:76`) computes the current contract
hash from the deployed configuration, selects the newest `eval_run` matching it **for a given
`expectation_ref`**, re-scores it via `evaluate_persisted_run_v2`, and returns a verdict object that
is never stored.

**Three consequences:**

1. **`certificate_revision_id` has no parent to FK to.** The binding as specified is unbuildable.
   Either freeze a certificate row (a `method_certificate_revision` carrying method identity,
   contract hash, corpus hashes, outcome and issued-at), or bind `eval_run_id` and accept a
   re-derived verdict — but the plans must say which. Migration 1114 plans a *compiler* certificate
   record while assuming the LLM-side one already exists. It does not.
2. **The verdict is recomputed from live deployment settings.** §9.1 requires *"the same certificate
   bindings across both production acts"* — but a re-derivation can legitimately differ between
   materialization and publication if anything in the deployed configuration moved. The binding must
   therefore store the contract hash and **compare**, never re-derive, at publication.
3. The plans are right that this reader cannot answer for an LLM feature (§9 piece 4): the query is
   keyed on `subject_ref = expectation_ref`, and a novel feature has none.

---

### B4 — `derive_authoring_method` can seal the strongest method claim on the weakest evidence

`authoring_provenance.py:85-105`:

```python
llm_evidence       = bool(author_refs) and bool(critic_refs) and reconciled
blueprint_evidence = bypass > 0
if llm_evidence and blueprint_evidence:  raise AuthoringMethodUndecidable(...)
if llm_evidence:                         method = LLM_AUTHORED
elif blueprint_evidence:                 method = REVIEWED_RECIPE_BLUEPRINT
```

The contradiction guard is gated on `reconciled`. **A run with provider dispatches that failed to
reconcile, plus a `REVIEW_BYPASSED` event, sets `llm_evidence = False` and therefore falls through to
`REVIEWED_RECIPE_BLUEPRINT`.** The guard never fires, because the very unreliability that should make
the run undecidable is what disqualifies its LLM evidence.

That writes the strongest possible method claim — the one §10 mints a `method_identity_hash` from,
naming a blueprint revision and expectation hash — into an **append-only** table, from a run whose
provider dispatches could not be accounted for.

Not reachable today only because no production caller supplies `reviewed_blueprint`. **Step 4 makes
both paths live simultaneously**, which is precisely when it becomes reachable.

**Fix:** the contradiction check must use raw dispatch presence, not the reconciled-gated boolean:

```python
if (author_refs or critic_refs) and blueprint_evidence:  raise AuthoringMethodUndecidable(...)
```

An unreconciled dispatch alongside a bypass is the definition of a trace that disagrees with itself.

---

## MAJOR

### M1 — The prescribed money-guard fix is redundant; the correct primitive already exists

Parent §11.1.1 and Phase 0 prescribe fixing `_authoring_config_hash` as *"a typed projection with
explicit absence … read by subscription-with-default, never getattr"*, folding `model_controls`
(provider · model · max_tokens · thinking · effort) **beside** `provider_contract_hash`.

▲ **`freeze_provider_contract` (`frozen_configuration.py:177-215`) already does this, better.** It
takes the same settings mapping and hashes into the contract:

```
generation_settings   <- THE WHOLE DICT, whatever keys the provider supplies
prompt_id · prompt_version · instruction_sha256
output_schema_id · output_schema_version · output_schema_sha256
```

It also **validates** `provider` and `model` are non-empty strings, raising rather than hashing `""`
— which is the exact failure mode the constant exhibits.

So `provider_contract_hash` **already subsumes** `model_controls` entirely, plus prompt and schema
identity. Carrying both puts two representations of the same facts in one identity: if they ever
diverge — a new provider key lands in the dict but not in the hand-written projection — the identity
moves through one path and not the other, and which one is right becomes unanswerable.

**Correction:** identity V2's payload is `{provider_contract_hash, formula_strategy,
strategy_identity_hash}`. **Delete `model_controls`.** This also disposes of the plan's `prompt_id`
problem, which it correctly identifies and then solves the long way round: the contract already
carries prompt identity because `prompt_id` is a parameter to the freeze.

### M2 — The legacy-preview rule is one-shot, but the problem recurs on every contract change

Once identity V2 folds `provider_contract_hash`, **any** change to a prompt, an output schema, a
model or a provider setting moves every future identity. The V1→V2 machinery — legacy drafts
preserved as auditable previews, regeneration only under explicit cost-confirmed approval — is
written as a one-time migration. **It is the general case.** The next prompt edit re-poses the same
question for the entire draft corpus.

**Correction:** state the rule over *superseded identities* generally, not over V1 specifically.
`formula_draft_authoring_identity` already carries `identity_version` and `config_payload_json`; the
preview/regeneration rule keys off *"the current composition differs from this draft's"*, which V1 is
merely the first instance of.

### M3 — `restore_formula` has a second failure mode the plan does not describe

`restore_formula_v3.py:78-90` joins drafts on `(considered_revision_id, option_id)` and takes
`ORDER BY d.updated_at DESC LIMIT 1` — **then** checks `READY`. It selects the newest draft and
demands it be ready; it does not select the newest ready draft.

So a candidate with a good `READY` formula, re-drafted later under a moved snapshot that then
`FAILED`, becomes **unbuildable** — the newest row shadows the usable one. Parent §11 describes only
the *"a newer draft builds different code"* failure. This is the same query, a different and (given
B1) more likely outcome. Pinning fixes both; the plan should say so, because the acceptance test for
one does not exercise the other.

### M4 — §7.1 conflates asking with deciding, and the codebase already distinguishes them

`GET /feature-execution/{artifact_id}/verify-eligibility` (`feature_execution.py:190`) exists so a
workspace can enable a button, and its docstring is explicit: *"A QUESTION — it records nothing.
Recording an attempt every time a screen rendered would fill the history with things nobody did."*

My §7.1 makes every decision a durable `action_decision_revision` and has the worker refuse when none
is found. Applied to eligibility queries that is write amplification per render and an audit trail
full of decisions nobody acted on.

**Correction:** `evaluate_action` needs two entry points over one implementation — `ask` (pure,
records nothing) and `decide` (records the revision, returns its id). Only an act that will be
enqueued calls `decide`. The parent's own §7 already implies this by separating request-time from
worker-time, but never says the request-time call for a *button* is not a decision.

### M5 — An unlisted fifth evaluator call site, and it has no user actor

The plans enumerate the routes and the lane. `corpus_generation.py:143` also calls
`evaluate_generate` — and is the **only** caller that supplies `activation_blockers`. It is a
platform coverage job, not a user act, so it has no actor to authorize with. Under §0.1.1's
one-authorization model it either needs a recorded platform delegation or must be excluded
explicitly. Either way it is migration surface the plans do not list.

### M6 — The composite binding I specified is asymmetric

Parent §11.0.1's `selection_formula_binding` FKs `planning_request_hash` to `formula_draft` but not
to `feature_selection_revision` — which **also has that column** (verified). So a binding may pair a
selection and a draft whose planning-request hashes differ: the formula was authored under a
different question than the selection was made under, and the constraint permits it.

**Correction:** include it in both composite keys, forcing all three to agree through the binding's
single column:

```
FOREIGN KEY (selection_revision_id, considered_revision_id, option_id, planning_request_hash)
    REFERENCES feature_selection_revision (revision_id, considered_revision_id, option_id,
                                           planning_request_hash)
```

---

## MODERATE

**D1 — `engine_id` is client-supplied and unbound to the authorization.** `GenerationIn.engine_id`
flows unvalidated to admission (`generation_lane.py:491`, `:535`), and admission identity folds the
engine's capability set. Nothing ties it to the authorization's environment; there is no
environment→engine table. **Latent only because exactly one engine is registered**
(`kedro-pyspark`). A second engine makes the caller the chooser of which capability set gates their
own formula. *(Related measurement for the funnel: only **12 of 39** operator rows are
`renderer_dispatchable` — a hard ceiling on the renderer stage of parent Phase B's funnel.)*

**D2 — `request_draft` dereferences a possibly-`None` row.** `formula_draft_store.py:366` returns
`existing[0]` after the retirement check, with no `None` guard. Hard to reach — `ON CONFLICT DO
NOTHING` implies the row exists and `formula_draft` forbids `DELETE` — but the codebase is otherwise
rigorous about this, and the failure mode is a `TypeError` where a refusal belongs.

---

## Plan claims re-verified as ACCURATE

Checked against the code and confirmed; no action.

| Claim | Verdict |
|---|---|
| 51 reason codes · 22 with dispositions (18 `CARRIED` / 4 `DROPPED`) · 16 emitted · 29 undisposed | **accurate** — an initial re-count of 53/31 was my own regex counting `REASON_FAMILIES`, a dict |
| `carried_blockers` raises `KeyError` on an unknown code, out of the sandbox publish path | **accurate** (`evaluator_contracts.py:219-228`) |
| the real lane calls `generate_v2` without `activation_blockers`, so production runs on the empty default | **accurate** (`generation_lane.py:531-545`) |
| `_authoring_config_hash` is a constant via `getattr` on a dict | **accurate** (`formula_drafts.py:279-293`) |
| §9.0: the sandbox lane does not exist — route records, no worker claims, store unreachable | **accurate**, four links |
| `seal_v2` derives provenance read-only before recording, so an undecidable member refuses the seal without leaving an artifact | **accurate** (`seal_v2.py:212`, `:249`) |
| §11.0.1's composite FKs are buildable — all columns exist, both unique indexes are PK supersets | **accurate** |

---

## What this changes about the sequence

* **B1, B2 and B4 belong in step 2.** All three are defects in identity, authorization and
  provenance — the three things step 2 exists to fix — and each is cheaper to fix there than to
  work around later. B4 in particular must land before step 4 makes it reachable.
* **B3 belongs in step 7 and changes its migration set.** A `method_certificate_revision` table is
  new work the plans do not currently carry.
* **M1 makes step 2 smaller**, which is welcome given that promoting the strategy contract already
  made it the largest step.
* **M4 should be settled before step 3 is written**, because it decides the shape of the service's
  public surface rather than an internal detail.

---

# Appendix — validation of the second principal-architect review

A second review (10 P0s + contract gaps) was conducted independently against the same commit. **It
overlaps this one almost nowhere**: it read the plans against the data model and the deployment,
this one read them against the runtime. Each claim was checked before folding.

## Verified CORRECT — 12 of 12 checkable claims

| Claim | Verification |
|---|---|
| `generation_authorization` holds 1 row, so 1100 cannot assume an empty table | **confirmed** — `user:ops` / `hdfc-local` / `customer_txn_features` / `bs-1` / 2026-08-17 |
| the certificate binds an authoring identity even for `COMPILER_RUNTIME` | **confirmed**, and compounds with this review's B3: no certificate table exists at all |
| spend "append-only" with mutable counters, and the same-transaction rule is impossible | **confirmed** — `record_dispatch` opens its **own** connection and commits independently (`dispatch_audit.py:133,155`) by design |
| tombstone `retirement_scope_key PRIMARY KEY` cannot carry both scopes | **confirmed** — every exact draft of a candidate shares that key |
| `selection_formula_binding` omits `formula_content_hash` and the selection's planning/binding hashes | **confirmed** — supersedes and extends this review's M6 |
| `AUTHOR_FORMULA` has two contradictory subject models | **confirmed** — `formula_drafts.py:125` states *"This route never selects"* |
| one job, one authorization, several actions | **confirmed** — `code_generation_job.requested_action` is singular |
| migration numbers collide: storage says 1108/1109 | **confirmed** — parent line 1987 vs the §17 table |
| *"rollback is the previous image"* is unsafe | **confirmed** — the 1093/1099 gap fixed at the top of this session is the counter-example |
| declared tolerance is not representable | **confirmed** — `OperationRuleV1` (`operations_v2.py:31`) has no approximate/exact axis |
| revocation needs `UNVERIFIABLE` | **confirmed** — `PrincipalResolutionStatus` already has `CURRENT`/`REVOKED`/`UNVERIFIABLE` |
| Kind cannot execute the journeys | **confirmed** — `FEATUREGEN_MATERIALIZE_ENABLED: "0"`, execution block commented out |

## Two corrections to that review

**1. The legacy authorization row is an ORPHAN, which changes its disposition.** Its build set `bs-1`
does not exist (`build_set_revision` count = 0); the row is representable only because
`generation_authorization_covers_a_real_build_set` was added `NOT VALID`. So *"preserve it as legacy
evidence"* would preserve a record of an approved build **that never existed**, and the next reader
would believe it. Adopted instead: preserve it **explicitly marked orphaned**, with the reason.

**2. "Ran 187 focused backend tests: all passed" is not adopted as a baseline.** The suite's
authoritative figure at `3c52a9de` is **13,154 passed**; a subset run is evidence about a subset.

## What neither review found alone

This review's **B1** (terminal drafts wedge the money guard — with 7/7 live drafts dead), **B2**
(caller ≠ grantee), **B4** (mis-certification on unreconciled dispatch) and **M1** (the money-guard
fix is over-built) do not appear in the second review. The second review's **R5, R6, R7, R8, R10,
R11, R13, R16, R18** do not appear here. ▲ **The union is the finding set; neither is sufficient
alone** — which is the argument for running both a data-model pass and a runtime pass rather than one
"thorough" review.

---

# Third pass — the crash-recovery class, and a self-review of revision five

Two things changed after the second pass: revision five added **~1,645 lines of design nobody has
reviewed**, and the first two passes never examined the queue, the lease/fence machinery, or the
lifecycle stores. This pass covers both.

## C1 — A crashed worker permanently wedges a build set. LIVE, on the production path

**The same disease as B1, in a third place, and this one is on the lane every journey runs through.**

| # | Fact | Where |
|---|---|---|
| 1 | `generation_request_one_live_attempt` is UNIQUE on `(build_set_revision_id, environment_id)` `WHERE status IN ('REQUESTED','CLAIMED','RUNNING')` | live index |
| 2 | on redelivery the lane refuses anything not `REQUESTED` and not terminal: `fail_generation(permanent=False)` — *"another worker holds it"* | `generation_lane.py:416-422` |
| 3 | the queue reclaimer returns the **queue** row to `ready` and touches **nothing else** | `queue.py:659-666` |
| 4 | the only `FAILED` terminalization is **inside the lane** (`generation_lane.py:442`), so it needs a live worker to run | verified by grep — no sweep, no expiry, no operator path |

**So:** a worker dies between `CLAIMED` and terminal. The queue redelivers. The lane reads `RUNNING`,
concludes another worker holds it, and releases the message for retry — **for ever**, because nothing
will ever move that row. The message eventually exhausts `max_attempts` and the build silently never
happens. ▲ **And the one-live index means no NEW attempt can be created for that build set in that
environment, permanently.**

The comment at `:409` reasons carefully that "another worker holds it" must be retryable rather than
dead-lettered — correct while a worker is alive, and **the crash case is the one it does not
distinguish**. A held request and an abandoned one look identical to this code.

**Required:** a reconciler that moves an abandoned `CLAIMED`/`RUNNING` request to `FAILED` (or back to
`REQUESTED` with an attempt count), keyed off the queue lease it lost. ▲ **The domain row and the
queue row have separate lifetimes today, and only one of them has an expiry.**

▲ **§9.0's sandbox worker would ship a FOURTH instance of this.** `verification_request` has the same
shape — `verification_request_one_live` on `(sealed_artifact_id, environment_id) WHERE status IN
('REQUESTED','CLAIMED','RUNNING')` — and **no lease, fence, attempts column or trigger**. A crash
wedges that artifact's verification permanently.

## C6 — §9.0's "reuse the claim pattern" points at the wrong half

`claim_one`'s partition exclusion is a `partition_key NOT IN (SELECT ... WHERE status='leased')`
subquery, which is **not** serialized by the `FOR UPDATE SKIP LOCKED` on the candidate row: two
claimers can both see no leased row for one partition. ▲ **The actual guarantee is
`queue_one_inflight_per_partition`** — a partial unique index — and the `except
psycopg.errors.UniqueViolation: return None` is what turns the race into a clean miss.

**So a worker that copies the predicate but not the index inherits a race.** §9.0 must say which half
is load-bearing, and must also record that **`verification_request_one_live` ALREADY EXISTS**
(migration 1094) — 1110 adds lease/fence/attempts and the reclaimer, and must not redefine it.

▲ **One integration decision §9.0 leaves open:** whether the verification handler joins
`MATERIALIZATION_QUEUE_HANDLERS` (one poller, and the partition exclusion then serializes a
verification against a compile that shares its partition key — probably wrong, they are different
acts) or gets its own poller and partition namespace. Decide it in the plan, not in the patch.

## Self-review of revision five — five defects in the new material

**C2 — §0.1.2's own composite binding does not bind.** The attempt's FK to the decision is
`(action, resource_identity_hash, decision_id)`, which does **not** include `authorization_id`. So an
attempt may cite decision **D** — issued under authorization **A** — while itself naming
authorization **B**, and every FK passes. ▲ **This is the exact defect R9 and M6 fixed elsewhere,
reintroduced in the section written to prevent it.** The decision FK must carry
`authorization_id`, forcing agreement transitively.

**C3 — §11.2's reservation design reintroduces C1's gap.** A crash between `llm_spend_reservation`
and `llm_spend_settlement` leaves worst-case cost reserved for ever, silently shrinking the budget
until the authorization is exhausted by work that never happened. **Reservations need an expiry and a
sweep**, and the sweep must be reconciled against `llm_dispatch_outcome` rather than assumed.
▲ The criticism this review levels at the lifecycle stores applies to the design it proposed.

**C4 — two names for one tuple.** `authoring_subject_revision` (§0.1.4) is
`{considered_revision_id, option_id, planning_request_hash, catalog_snapshot_hash,
definition_revision}`. `retirement_scope_key` (§11.1.1) is **the same five fields**. The plan now
carries one concept under two names, which is how they drift apart. ▲ **Unify: the retirement scope
key IS the authoring subject's identity hash** — which also states something true and useful, that
retirement withdraws *a subject*, authorization authorizes *a subject*, and the money guard's
non-configuration half *is* that subject.

**C5 — §10.3's typed subject is unconstrained.** Nothing ties `certificate_kind` to
`subject_identity_kind`, so an `AUTHORING_METHOD` certificate may carry an `EXECUTION_STACK` subject.
Needs a CHECK pairing them, and — where a parent exists — an FK from `subject_identity_hash` to the
identity row it names.

**C7 — §11.1.2's "exactly ONE re-attempt" is unenforceable.** `formula_draft` has **no** attempts,
attempt or retry_count column (verified). Either add one, or bind the re-attempt to the spend
authorization's `max_uses`, which already counts consumption.

**C8 — §20.1's cutover has no mechanism.** *"Stop API intake"* is the first step, and there is no
maintenance mode, drain or read-only switch in the manifests. Either add one, or state that intake is
stopped by scaling the backend to zero and say so, because an unimplementable first step invalidates
the sequence that follows it.

## Verified CORRECT in this pass

| Claim | Verdict |
|---|---|
| the queue's partition exclusion is race-free | **correct** — guaranteed by `queue_one_inflight_per_partition`, with the `UniqueViolation` catch as the handler |
| `advance_request` is compare-and-set, not read-then-write | **correct** (`build_set_store.py:317-327`) — an older note calling this read-then-write is **stale** |
| migration 1094 distinguishes REFUSED (product) from FAILED (platform) | **correct, and exemplary** — it is the model the `formula_draft` fix in B1 should copy, written by the same hands that got the draft states wrong |
| `allowed_classes` fails closed | **correct** — empty roles grant only columns with empty `visible_requires` |
| lease fencing is sound | **correct** — reclaim leaves `lease_fence` untouched and the next claim increments it, so a resurrected worker's fence-guarded UPDATE cannot land |

## The pattern worth naming

▲ **B1, C1 and the latent verification case are one defect repeated three times:** a lifecycle state
that only a live worker can leave, plus a uniqueness guard on the live states, equals a permanent
wedge with no operator remedy. The codebase has the ingredients for the fix in each case — the queue
has leases and fences, 1094 has the right vocabulary — but **no domain table has a reconciler.**

**Recommendation: make "every lifecycle table has a reconciler" a standing rule in §15**, beside
*"never land a governance function without its enforcement point"*. Both are the same failure —
machinery whose absence is invisible until the day it is needed.

---

# Execution notes — corrections found while implementing

Two findings changed materially the moment implementation started. Both are recorded here because
each was *more* wrong in a direction the review had not considered.

## C1 is sharper AND partly inverted — the reconciler EXISTS

`src/featuregen/materialize/reconcile.py` **is** the reconciler C1 says is missing. It is wired into
the worker (`runtime/worker.py:582`), it is thorough, and it reconciles **`materialization_request`**
— the **legacy** chain. It contains **zero** references to `generation_request`.

▲ **So the correct statement is not "no domain table has a reconciler". It is: the LEGACY lane has
crash recovery and its REPLACEMENT does not — and §17 step 10 deletes the legacy route.** Carried out
naively, this programme ends with **strictly less** crash recovery than the platform has today.

▲ **And that module contains the trap my proposed fix would have walked into.** I was about to detect
abandonment as *"request is `CLAIMED`/`RUNNING` and its queue row is not leased"*. `reconcile.py`'s
header explains why that is wrong: a request healthily awaiting redelivery after a
`permanent=False` release is byte-for-byte identical to an abandoned one on that signal, and
terminalizing it does quiet damage — the redelivery hits the terminal short-circuit and reports
*"already done"* for work that never happened. The correct predicate is an **unreachable message**,
not an unleased one.

**Revised instruction: PORT it, with its three abandonment classes and its ranking. Do not re-derive
it** — a second derivation omits the trap, because the trap is only obvious after it has bitten.

## B2's recommended fix is probably the WRONG product rule

The defect is real: any `feature:generate` holder may consume any generation authorization. But the
review's fix — *"the caller must BE the grantee"* — collides with what the code and its tests model:

* `tests/featuregen/api/routes/test_build_sets.py` runs as `X-User: sam` against an approval seeded
  `authorized_by="user:ops"`, and asserts success;
* `record_generation_authorization`'s docstring calls `authorized_by` *"the act of a person taking
  responsibility for a target"* — an **approver**, not necessarily the executor.

▲ **That is segregation of duties, which in banking is frequently mandatory** — the approver must
*not* be the executor. Requiring caller == grantee would forbid it.

**So the finding stands and the remedy does not.** There is no rule at all today, and there are two
opposite candidate rules:

| Model | Rule | Consequence |
|---|---|---|
| **same-actor** | caller must be the grantee | simple; forbids the approve/execute split the fixtures model |
| **segregation of duties** | approver ≠ executor, and the executor is independently entitled to execute in that environment/group | matches the fixtures and the docstring; needs an entitlement check that **does not exist** — `require_feature_generate` gates the route globally, not per environment |

**This is an owner decision, not an engineering one.** §0.1.1.1 currently asserts the same-actor rule
and must not be implemented until it is settled.

## The owner's development-policy ruling, and what it removed

Mid-execution the owner ruled that segregation of duties is premature while the tool is under
development. **Any authenticated development user may trigger any implemented non-production stage,
and the server records who.** The safeguards that remain are the ones that are not temporary: roles
never come from a request body, a client cannot present an authorization the server did not issue to
it, production actions stay unavailable, and Kind is a development environment whatever an action is
called.

▲ **This settles B2 in a third way — neither of the two the review offered.** The check built for it
survives, but as the *"permission is server-owned"* safeguard rather than as a duties rule, and every
comment around it says so, so nobody later builds four-eyes on top of a foundation that was never
laying it.

**Removed from the near-term path**, deferred to release-readiness (§21.0): approver ≠ executor,
delegation records, per-environment entitlement, the revocation tri-state, and
`ACTION_AUTHORIZATION_UNVERIFIABLE`. ▲ **`policy_version = 'development-v1'` is what keeps that
deferral honest** — otherwise a deferral and an omission are indistinguishable in hindsight.

## A correction the implementation forced: expand before contract

Migration 1100 was specified to create the new table, re-point 1095's chain and drop
`generation_authorization` in one file. **It ships expand-only, dropping nothing.**

The reason is R13's own argument turned around: the plan says rollback must restore the database
because these migrations are not backward-compatible — **so make the ones that can be, be.** An
additive 1100 leaves the running image working, which means an image-only rollback stays safe and
this migration needs no maintenance window at all. The contract half becomes 1100b, once all six
acts have callers on the new table.

▲ **Worth applying to 1101–1105 wherever available.** It is the cheapest way to shrink the window
§20.1 describes, and it was not visible until someone tried to write the migration.

## Delivered in this pass

* **B4 fixed** — `derive_authoring_method`'s contradiction guard now uses raw dispatch presence, so
  unreconciled provider calls plus a reviewed bypass **refuse** instead of sealing
  `REVIEWED_RECIPE_BLUEPRINT`. Two tests, **verified to bite** (`DID NOT RAISE` with the defect
  reintroduced).
* **B2 closed as the server-owned safeguard** — `authorization_grantee` plus a 403
  `ACTION_AUTHORIZATION_NOT_HELD` at the route. The grantee had to be read separately because
  `load_generation_authorization` reconstructs only the five identity-bearing columns. **Five
  existing route tests failed, and that was the finding**: they had been asserting that spending
  somebody else's authorization succeeds.
* **C1 ported** — `materialize/reconcile_generation.py`, wired into the worker tick, gated on the
  GENERATION switch. Judges `REQUESTED` as well as `CLAIMED`/`RUNNING`; writes `FAILED`, never
  `REFUSED`; separate gauge. ▲ **The trap test is verified to bite**: under the naive
  "queue-not-leased" predicate it terminalizes a request that was healthily awaiting redelivery.
* **Migration 1100 + `action_authorization.py`** — the six-action vocabulary, the development
  policy, append-only storage. Production acts raise `ActionUnavailable` and record nothing. 19
  tests.

**Still owed on step 2:** the relational selection→formula binding (1101), method identity (1102),
retirement rework (1103), the strategy contract (1104), spend authorization (1105) — and B1's full
fix, whose bounded re-attempt binds to 1105.

---

# Fourth pass — an adversarial workflow over the day's own modules

The seven modules written during steps 2–3 had zero independent review — everything else on this
branch has had three passes. A 16-agent adversarial workflow (4 grouped finders → 1 refuting
verifier per finding, high effort) reviewed them: **12 findings, 7 confirmed, 5 refuted** — and the
refutations were sound, each naming the exact code that makes the scenario unreachable.

## The seven confirmed, all fixed same-day

| # | Sev | Where | The defect |
|---|---|---|---|
| **W1** | blocker | `llm_spend.py` | the expiry check compared `str(expires_at) <= str(now)` — a session-TZ datetime with a space separator against an ISO string with `'T'`. Same calendar date: a valid authorization refused (0x20 < 0x54). Positive-offset session TZ: **an expired authorization spends real money — the money guard failing OPEN**. Every other time comparison in the module already ran in SQL; only this one was Python-side. Fixed: compared in Postgres |
| **W2** | major | migration 1105 | the ledger trigger's own error text claimed reservations are append-only — **but the trigger was attached only to settlements**. A mutable reservation IS the overspend: shrink its `expires_at` mid-flight and the worst-case amount frees for a concurrent worker. Fixed: trigger attached |
| **W3** | blocker | `retirement_scope.py` | `record_tombstone` wrote only the 1103 table while the advance fence read only the LEGACY one — so a tombstone stopped future *requests* while every **in-flight draft kept spending to READY**. Fixed: the fence recomputes the scope key from the draft's frozen identity columns, so a candidate-wide withdrawal stops sibling configurations too, and an EXACT one stops only its own |
| **W4** | blocker | `formula_draft_store.py` | the regeneration exception binds the EXACT identity being re-requested — and 1090's unique index covered every row, so the terminal FAILED draft occupied the slot for ever and the authorized INSERT **lost unconditionally**. Worse: consumption preceded the refusal, so the operator presenting the approved exception had one use burned per click while being told to obtain the thing they were holding. Fixed: **migration 1107** narrows the guard to answers (`WHERE state NOT IN ('FAILED','CANCELLED')` — a failed draft bought nothing), the INSERT names the partial predicate, the exception is located first and consumed only in the transaction that mints |
| **W5** | moderate | `retirement_scope.py` | a disagreeing second retirement was silently discarded while its operator was told it took effect, and the returned tombstone described a row recording none of it — the exact defect `RetirementDisagreement` fixed on the legacy path, reintroduced. Fixed: read-back-and-compare |
| **W6** | major | `action_decision.py` | `decide()` with a missing/mismatched authorization leaked a bare `ForeignKeyViolation` (1106's composite FK makes the refused decision UNWRITABLE) while `ask()` returned clean typed blockers — an ask/decide divergence. Fixed: typed `AuthorizationUnusable` before the INSERT |
| **W7** | moderate | `method_identity.py` | `ON CONFLICT DO NOTHING` was first-writer-wins-silently on an append-only evidence table: after a version bump, sealing proceeds believing its derived identity was recorded while certificate matching for ever evaluates a hash that act never produced. Fixed: insert-or-VERIFY, the ledger's own checksum discipline |

## What this pass teaches

▲ **The worst defects were in the paths my own tests could not reach.** W4's existing test used a
*different* identity — because the same-identity case was structurally impossible, the test
accommodated the defect instead of exposing it. W1's tests used dates differing in the first ten
characters, where lexical order coincidentally matches instant order. **A test written by the code's
author inherits the author's blind spot**; the adversarial pass exists because of exactly that.

▲ **And the refuted five are as valuable as the confirmed seven** — each refutation cited the
specific guard (an idempotency key, a single production entry point, a DB-side read) that makes the
scenario unreachable, which is the documentation of WHY those guards must stay.
