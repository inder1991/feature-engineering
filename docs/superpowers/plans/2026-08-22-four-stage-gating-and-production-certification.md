# Six gated actions, one decision service, and a real production boundary

**Authority:** the product owner's rulings of 2026-08-22, quoted inline, as amended by their reviews
of this plan's first, second and third revisions. Where this plan and a ruling disagree, the ruling
wins. The three decisions this document used to leave open (D1/D2/D3) are **resolved below** — they
are no longer inputs to be gathered, they are constraints to be built.

▲ **The third review closed the last five open questions, and this revision is what "architecturally
ready" means.** Nothing below is now waiting on an owner input. The five:

| Ruling | What it settled | Where it lives |
|---|---|---|
| **1 — legacy formula pinning** | no fabricated pin, no deleted history: unpinned sets stay readable and unbuildable, every NEW member pins, enforced by a **versioned** constraint — and the live measurement says zero legacy rows, so the columns land `NOT NULL` today | §11 |
| **2 — what "the compiler produced the right thing" means** | **TWO** comparisons, IR **and** executed values; never generated source bytes; failure of either fails the case | §12 piece 4, §12.1 |
| **3 — the money guard** | neither earlier option: legacy identity **V1** preserved, corrected identity **V2** introduced, retirement **decoupled** from identity and checked **before** provider work | §11.1 |
| **4 — six actions stand** | production materialization and publication are separate **product** decisions; proving atomicity makes consolidation possible, not chosen | §3 |
| **5 — delete the legacy route** | no adapter, no both-route equivalence; both **methods**, one canonical route, route-absence and queue-bypass tests instead | §1 D2, §8.3 |

> *"If the backend can safely and honestly render the selected formula, show the user the code. Gold
> evaluation decides whether the generation method is certified for production — not whether code can
> be inspected."*

> *"A missing reviewed expectation changes the AUTHORING METHOD. It must not automatically prevent
> code preview."*

> *"Certification must be checked BEFORE production materialization begins, not only before its
> results become visible."*

---

## ▲ Revision four — the principal-architect review, and what it changed

**Authority:** the principal architect's verdict of 2026-08-22, delivered against `3c52a9de` with the
suite green (**13,154 passed / 20 skipped / 82 deselected / 0 failed**, 9m57s) and the live cluster
inspected. Its finding: **the direction is right and these documents were NOT executable** — ten
stop-ship findings, three completeness gaps, four rulings. This revision folds all of them.

▲ **What the verdict confirmed was already right is preserved in §22** — six separate actions, gold
gating production rather than preview or sandbox, one server-owned decision service, recipe and LLM
formulas sharing validation/compilation/rendering, formula drafts pinned before generation, the
legacy route ultimately deleted, method provenance and certification immutable. **Those are not
re-opened.** What follows is only what was wrong.

### The ten stop-ship findings, and where each is now answered

| # | Finding | Now lives in |
|---|---|---|
| **P0-1** | the canonical sandbox execution lane **does not exist** — this plan built on a substrate that is four links of dead code | **§9.0** (the durable sandbox worker) · §12.2 (the certification-runner ruling) |
| **P0-2** | step 2 activates identity V2 from strategy facts step 4 creates — **a cycle** | **§17** (the strategy contract is PROMOTED into the foundation) · child §3.3 |
| **P0-3** | the retirement **exception is unreachable** — steps 1 and 3 of the request order always return first | **§11.1.1 (1)** |
| **P0-4** | retirement has a **concurrency race**, and silently **widens its scope** to every future configuration | **§11.1.1 (1)** |
| **P0-5** | **two authorization models** that can disagree about the same resource | **§0.1.1** |
| **P0-6** | the formula pin does not prove the formula **belongs to the selection** | **§11.0.1** |
| **P0-7** | **one certificate per member** cannot carry the two independent proofs production needs | **§10**, §10.2 |
| **P0-8** | the blocker matrix **contradicts its own routing rule** — origin overrides the sealed method | **§4.1** · §5.1 |
| **P0-9** | *"Try AI formula"* contradicts **server-owned method selection**, with no durable mechanism | **§11.3** |
| **P0-10** | ordinary **LLM spend authorization is not designed** — a modal is not a money guard | **§11.2** |

### The three completeness gaps

| Gap | Now lives in |
|---|---|
| production materialization and publication need **real state machines** — lease, fencing, idempotent retry, output-revision identity, CAS publication, crash recovery | **§9.1** |
| Comparison B needs **one governed case revision**, not separately approved parts | **§12.2** |
| decision results need a **durable request-time record** the worker re-checks | **§7.1** |

### ▲ Two choices the verdict left open, and how they were taken

The verdict resolved eight findings and deliberately left two to the owner. Both were taken here, and
**both are reversible by a single ruling** — each is marked at its site, so a reversal is an edit
rather than an archaeology exercise.

| Open choice | Taken | Why | Reverse by |
|---|---|---|---|
| **P0-5** — replace `generation_authorization`, or reference it | **REPLACE**, in the pre-live cutover | every table it constrains is measured **empty** (§0.3), so nothing migrates and no second record can disagree; referencing keeps two records a future change can drift apart, which is the representable disagreement the finding objects to | §0.1.1's replacement becomes a parent FK, and 1095's chain stays |
| **P0-2** — split step 2 into 2A/4A/4B, or promote the strategy contract | **PROMOTE** into the foundation | an identity that cannot be composed without strategy facts **is telling you the strategy contract is foundation**; the split needs an atomic mid-programme activation, which is a deploy-ordering hazard the promotion simply does not have | §17 restores 2A/2B and moves 1104 back to step 4 |

▲ **The promotion has a cost, and it is stated here rather than discovered at step 2: that step gets
bigger.** It now carries six migrations and the strategy resolver. That is one large step instead of
three coupled ones — and the coupling was the defect.

---

## ▲ Revision five — two independent reviews, folded

Revision five folds **two** reviews conducted against `3c52a9de`: a second principal-architect pass
(10 P0s + contract gaps) and a code-level deep review
(`docs/architecture/2026-08-22-plans-and-code-deep-review.md`, 4 blockers + 6 major). ▲ **They
overlap almost nowhere**, which is the useful fact about them: one read the plans against the
data model and the deployment, the other read them against the runtime. Both were verified
claim-by-claim before folding; where a claim contradicted this document, this document was wrong.

### ▲ Three things revision four asserted that are FALSE

| Asserted | Actually |
|---|---|
| *"there is no authorization row to migrate"* (§0.1.1) | **`generation_authorization` holds 1 row** — `user:ops`, `hdfc-local`, `customer_txn_features`, `bs-1`, 2026-08-17. ▲ **And its build set `bs-1` DOES NOT EXIST**: the row survives only because 1095's FK was added `NOT VALID`. It is an orphan approval for a set that was never created |
| *"consumption is recorded in the SAME transaction that records the dispatch"* (§11.2) | **impossible.** `record_dispatch` deliberately opens its **own** connection and commits independently (`dispatch_audit.py:133,155`) so the audit survives a caller rollback. Spend must reserve/settle, not co-commit |
| the compiler storage lands at *"migrations 1108 / 1109"* (§12.1) | **collides.** §17 assigns 1108 to the method override and 1109 to `code_generation_job`. Compiler storage is **1113 / 1114** |

### The consolidated finding set

| # | Finding | Now answered in |
|---|---|---|
| **R1** | a `FAILED` draft poisons its candidate for ever, and the corrected order rebuilds the trap at V2 | **§11.1.2** |
| **R2** | any caller may consume another actor's authorization — the route checks approval↔build-set, never caller↔approval | **§0.1.1** |
| **R3** | the per-member certificate binds `certificate_revision_id`, and **no certificate table exists** | **§10.3** |
| **R4** | `derive_authoring_method` seals `REVIEWED_RECIPE_BLUEPRINT` on unreconciled provider evidence | **§10.4** |
| **R5** | the legacy `generation_authorization` row is real, orphaned, and cannot be silently dropped | **§0.3**, §0.1.1 |
| **R6** | authorization and decision are not RELATIONALLY bound to action + resource | **§0.1.2** |
| **R7** | one job, one authorization, but the journey performs several actions | **§0.1.3** |
| **R8** | `AUTHOR_FORMULA` has two contradictory subject models — selections vs inspection-before-selection | **§0.1.4** |
| **R9** | `selection_formula_binding` omits the hashes that would make it bind | **§11.0.1** |
| **R10** | the tombstone key cannot represent both retirement scopes | **§11.1.1** |
| **R11** | spend authorization is "append-only" with mutable counters, and cannot co-commit with the audit | **§11.2** |
| **R12** | certificates are bound to an authoring identity even when they certify the runtime | **§10.3** |
| **R13** | *"rollback is the previous image"* is disproven by this cluster | **§20.1** |
| **R14** | migration numbers collide (1108/1109) | **§12.1**, §17 |
| **R15** | the money-guard fix is over-built — `provider_contract_hash` already subsumes it | **§11.1** |
| **R16** | declared tolerance is not representable in the current grammar | **§12.1** |
| **R17** | `ask` and `decide` are conflated; eligibility queries would write decision rows | **§7.1** |
| **R18** | Kind cannot execute the planned journeys — materialization is disabled | **§17 step 0b** |
| **R19** | §13's code-retrieval choice was never made | **§13** |
| **R20** | unlisted evaluator caller `corpus_generation.py:143`, a platform job with no actor | **§7.2** |

▲ **Two of the second review's recommendations are adopted with a correction, not verbatim:**

* **The legacy authorization row should NOT be preserved as "audit evidence" uncritically.** It
  approves a build set that does not exist, so preserving it as a record of an approved build
  records something that never happened. Disposition in §0.3.
* Its *"ran 187 focused backend tests"* is not restated here as a fact of this document — the
  suite's authoritative number at `3c52a9de` is **13,154 passed**, and a subset run is not a
  baseline.

---

## ▲ 0.3 The deployment baseline, and eight measurements taken against it

**Ruling 3 of the verdict, and it is the first thing that happens — before any step-2 migration:**

> *Do not deploy this plan, because it has no implementation. First align the running backend/worker
> image with migrations 1094–1099 and smoke-test that baseline. The current DB-at-1099 /
> image-at-1093 state should not remain the starting point for another migration programme.*

**Verified 2026-08-22, independently of the verdict:**

| | |
|---|---|
| live ledger | **195 rows**, high-water `1099_sealed_artifact_member_provenance`, applied 12:30 UTC |
| running `featuregen-backend:local` | ships migrations **through 1093** — listed inside the running container |
| backend and worker | the **same** image; `deploy.sh backend` rebuilds it and rolls both |
| checksum drift, all 180 `.sql` migrations against the ledger | **zero** — so the init container skips cleanly, and a green init proves the schema matches and **nothing else** |

### ▲ The cluster is not STALE. It is BROKEN at sealing — verified

**A migration gap is dangerous in proportion to how much it touches EXISTING tables, not how many
numbers it spans.** Five of the six are new tables and cost nothing: old code cannot miss a table it
never heard of. **1095 is the exception, because it constrains tables the running code already
writes:**

```sql
ALTER TABLE sealed_artifact_v2
    ADD COLUMN IF NOT EXISTS generation_authorization_revision_id text;
ALTER TABLE sealed_artifact_v2
    ALTER COLUMN generation_authorization_revision_id SET NOT NULL;
```

| Verified | |
|---|---|
| live schema | `generation_authorization_revision_id` is **NOT NULL with NO DEFAULT** on **both** `sealed_artifact_v2` and `generation_request` |
| the running image | `seal_v2.py:156` is the **only** module in it that touches `sealed_artifact_v2`, and its INSERT names ten columns — **that one is not among them** |
| triggers | the only trigger on the table is `BEFORE DELETE OR UPDATE` (write-once). **Nothing fills the column on insert** |
| consequence | the 1093-era image sends an INSERT omitting a NOT NULL column with no default. ▲ **Every seal attempt raises a not-null violation.** `sealed_artifact_v2` and `generation_request` both standing at **0** rows is consistent with exactly that |

▲ **So every journey test in both plans is unrunnable on this cluster today**, because all of them end
at a sealed artifact — §19's decisive test asserts one exists, step 2's proof requires sealing to
work, and §9.0's worker consumes one. **And the failure presents as a not-null violation deep inside
a worker**, which reads like a defect in whatever is being built rather than the deployment gap it is.

▲ **The second reason stands independently:** the init container only carries migrations to 1093, so
1094–1099 were all applied by hand from a local checkout through a port-forward. **A green init
therefore proves nothing today.** Step 2 adds six more migrations whose writers must ship with them —
§15's rule, *a governance table's writer and its migration are one deployment unit, migration first* —
and that rule cannot be verified from an image that has never carried a migration past 1093.

### The eight measurements, and the four open items they close

Live kind cluster, 2026-08-22. **Four of these were recorded as NEVER MEASURED in one or both
plans**, and each decides a migration branch rather than merely informing one:

| Table | Count | What it decides |
|---|---:|---|
| `build_set_revision` · `build_set_member` · `generation_request` · `sealed_artifact_v2` | **0 · 0 · 0 · 0** | §11.0's immediate-`NOT NULL` branch |
| ▲ `feature_selection_revision` | **0** | **NEW — §11.0.1's `selection_formula_binding` lands `NOT NULL`, no backfill** |
| ▲ `formula_draft` | **7** — 4 `FAILED`, 3 `BLOCKED`, 7 distinct identities | **CLOSES child step 1's first unmeasured item.** ▲ **No draft is in a previewable state**, so `LEGACY_CONFIG_UNPROVEN` has **zero** live subjects today — which changes §11.1.1 step 5 |
| ▲ `formula_draft_retirement` | **0** | **CLOSES the second.** §11.1.1's tombstone backfill is **EMPTY**: the ORDER still governs, the work is nil |
| ▲ `authoring_work_item` | **0** | **CLOSES child §3.4's audit.** Its migration takes the *"empty → replace the old constraint directly"* branch |
| `formula_authoring_run` | 3 | sizes nothing; recorded for completeness |
| ▲ **`generation_authorization`** | ▲ **1 — NOT zero** | **§0.1.1's replacement. Revision four said there was nothing to migrate; there is** |

#### ▲ The one legacy authorization row, and what happens to it — R5

```
revision_id 7360292c...   environment hdfc-local   group customer_txn_features
build set   bs-1          authorized_by user:ops   authorized_at 2026-08-17
```

▲ **Its build set does not exist.** `SELECT count(*) FROM build_set_revision WHERE revision_id =
'bs-1'` returns **0**. The row is only representable because
`generation_authorization_covers_a_real_build_set` was added **`NOT VALID`** (verified in
`pg_constraint`), which exempts pre-existing rows from the check.

**So this is an approval for a build set that was never created** — or one created in a scratch
database and lost. That decides its disposition, and the second review's *"preserve it as legacy
evidence"* is refused **in this specific form**:

| Option | Verdict |
|---|---|
| silently `DROP` the table with the row in it | ▲ **refused** — deleting an authorization record without saying so is exactly the audit loss the review names |
| preserve it in a legacy evidence table, unqualified | ▲ **refused** — it would stand as a record of an approved build, and **no such build exists**. Preserving evidence of nothing is worse than deleting it, because the next reader believes it |
| ▲ **preserve it, explicitly marked ORPHANED** | ▲ **ADOPTED.** Migration 1100 copies the row into `legacy_generation_authorization` with `orphaned boolean NOT NULL` set from a re-checked existence test, plus the reason. No runtime path may reference that table, so it creates no second authority (§0.1.1) |

▲ **And migration 1100 must drop 1095's constraints in a proven order** — `sealed_artifact_v2` and
`generation_request` both carry composite FKs into `generation_authorization`, so the table cannot be
dropped before they are re-pointed. Write the order down and test it on a scratch restore; a
migration that fails halfway through a `DROP TABLE` chain leaves a schema no image matches.

▲ **Every one of these is a fact about a moment.** Re-measure inside the transaction that applies the
migration each one decides — §11.0 already states that rule, and it now governs **five** branches
rather than one.

---

### This is the PARENT plan, and it has a child

`2026-08-22-recipe-to-code-llm-fallback.md` is a **CHILD IMPLEMENTATION PLAN of this document**, not
a separate programme executed afterward. The owner's ruling of 2026-08-22:

> *"The gating plan defines WHETHER each action is allowed; the recipe-to-code plan defines HOW a
> selected feature obtains a formula and generated code. … recipe-to-code-llm-fallback.md should
> become a CHILD IMPLEMENTATION PLAN of the amended gating architecture — not a separate plan
> executed afterward."*

> *The gating plan defines the traffic rules; the recipe-to-code plan builds the road and the
> vehicle.*

| | Owns |
|---|---|
| **This plan (PARENT) — the traffic rules** | WHETHER an act may proceed: the six actions (§3), the ONE blocker matrix (§4), the exhaustive `(code, action)` disposition table (§5), the shared decision service (§7), the bypasses (§8), the production boundary (§9), method identity and per-member certificate binding (§10), formula pinning and draft identity (§11), the certification programmes (§12), code-retrieval authorization (§13) |
| **The CHILD — the road and the vehicle** | HOW a selected feature obtains a formula and generated code: strategy resolution from evidence, the deterministic reviewed-blueprint lane, the LLM fallback lane, the durable code-generation coordinator, preview generation, the frontend journey, and the reviewed-expectation growth programme |

▲ **There is exactly ONE action matrix in this repository, and it is §4 of this document.** The
child's competing *"D4 — four actions, four decisions"* matrix — which named four actions, omitted
`PUBLISH_SANDBOX` and `MATERIALIZE_PRODUCTION`, and carried its own twelve rows — is **deleted**. The
child now points here. Rows that existed only in the child have been **folded into §4** and are
marked `(child)`. A second matrix is not a convenience; it is the mechanism by which two routes give
two answers, which §8.3 exists to prevent.

▲ **The circularity is real and it decides the order.** This plan's deterministic lane (§1 D1), its
support for the other 295 recipes (§0.2 fact 2), its mixed-**method** artifacts (§0.2 fact 4, §10)
and its decisive journey test (§19) all depend on the child. **Finishing this plan first is
impossible.** §17 is the single ten-step order that supersedes both plans' internal phase lists.

▲ **Neither plan may begin implementation until step 1 of §17 is complete** — the owner:
*"As currently written, neither should begin implementation until their conflicting action matrices
and the P0 findings are corrected."* This revision IS step 1.

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
action authorization, the retirement tombstones and the V1/V2 authoring identity (§11.1.1), both
certification programmes and the two-comparison compiler runner (§12.1), the operator UI journey,
the deletion of the legacy route (§8.3), and the end-to-end journey tests.

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

3. **`generation_request` references it by FOREIGN KEY, NOT NULL** (migration 1100, §17). A request
   that names no authorization is not a weaker request; it is not a request.
4. **The worker loads it and REVALIDATES** — re-derives the read-scope fold against current
   `graph_node` state and refuses on drift. Revalidation against a frozen record is the whole point;
   revalidation against a caller's claim is theatre.

▲ `validate_spine_declaration(..., roles=)` and `pilot_v2.compile_generation_v2(..., roles=)` keep
their signatures. **What changes is the SOURCE of that tuple** — it becomes the frozen
`read_scope_result` of an authorization revision rather than a field of the HTTP body. That keeps the
diff small and the invariant large.

### ▲ 0.1.1 ONE authorization model — `generation_authorization` is REPLACED, not joined — P0-5

**The finding.** The system already has `generation_authorization`, binding build set + environment +
logical group + target mode/ref, and **migration 1095 already enforces the referential chain in the
database** — a generation request cannot name an artifact created under another authorization
(`1095_authorization_referential_chain.sql:126`). Revision three proposed
`action_authorization_revision` **beside** it and never said whether the new record replaces,
extends, references, or may disagree with the old one.

▲ **Two records that can describe different resources make disagreement REPRESENTABLE again** — the
precise failure §8.3 exists to prevent, re-created one layer down. "Which authorization is
authoritative" is not a question a running system should be able to ask.

▲ **RULED: replace it — and the pre-live measurement is what makes replacing it cheap.**
`generation_request` is **0** and `build_set_revision` is **0** (§0.3), so there is no authorization
row to migrate and no in-flight build to strand. *(This is one of the two choices the verdict left
open. **To reverse it:** give `action_authorization_revision` a `generation_authorization_revision_id`
FK instead of replacing, and leave 1095's chain as it is. Everything else below is unchanged.)*

**Migration 1100 therefore does three things in one file:** creates `action_authorization_revision`,
re-points 1095's referential chain at it, and drops `generation_authorization`. ▲ **1095 is an
APPLIED migration and is immutable** — the re-point is a NEW file altering the constraint, never an
edit to 1095 (§15, and the ledger's checksum guard would refuse it anyway).

**Every authorization binds all of this. A NULL in any of it is not a weaker authorization; it is not
an authorization:**

| Binding | Why it cannot be optional |
|---|---|
| `action` | an authorization authorizes ONE of the six acts (§3); a generic one authorizes the strongest |
| `actor_subject` + tenant / authentication context | derived from `IdentityEnvelope`, never from a body |
| **exact resource / request identity** | what 1095 already enforces for generation, now enforced for all six |
| **build-set and formula pins where applicable** | so an authorization cannot outlive the selection changing under it (§11.0.1) |
| environment + target ref | the production/sandbox distinction is part of the grant, not ambient context |
| `policy_version` | which policy produced the answer |
| `permission_result` + `read_scope_result` | the governed decision and the resolved `allowed_classes` fold, frozen as values |
| **entitlement revision or expiry** | see the revocation ruling below |
| `evidence_hash` | so the answer is re-derivable rather than asserted |
| `decided_at` | provenance, and therefore OUTSIDE `revision_id` — 1099's rule |

▲ **Every durable job for ALL SIX actions references its authorization by NOT NULL FK — not only
`generation_request`.** Revision three required it of one table, which would have left five acts
authorized by nothing. The tables: `generation_request` (1100), the verification lifecycle (§9.0,
1110), the two production attempts (§9.1, 1111/1112), and `code_generation_job` (child §3.5, 1109).

#### ▲ Frozen roles and revocation — the question revision three did not ask

Freezing `actor_role_claims` makes a decision **re-derivable**. It does not make it **current**: a
worker re-deriving read scope from yesterday's frozen roles is validating an entitlement the actor
may no longer hold. The two readings are different products, and the plan must pick one.

> **RULED: roles are REVOCABLE PERMISSIONS, not durable delegation.** The frozen `read_scope_result`
> is the *record of what was decided*. The worker **revalidates the actor's CURRENT entitlement**
> before the act and refuses on revocation with `ACTION_AUTHORIZATION_REVOKED` (§5). The frozen value
> is what a refusal is *compared against*, never what it is *derived from*.

▲ **This is strictly stronger than "re-derive the fold against current `graph_node` state"**, which
revision three already required: that catches **the data** moving; this catches **the person**
moving. Both run, and a difference in either is a refusal rather than a quiet re-decision.

**If a future ruling wants durable delegation instead** — a long-running build that must survive its
requester's offboarding — then the authorization records `issuer`, `delegation_scope` and `expiry`
explicitly, and the worker checks the delegation rather than the person. That is a different product
answer. It must be written down, not inferred from an authorization that happens to still parse.

▲ **Revocation is TRI-STATE, and the abstraction already exists.** Revision four added only
`ACTION_AUTHORIZATION_REVOKED`. `PrincipalResolutionStatus`
(`identity/current_principal.py:14`) already distinguishes **`CURRENT` · `REVOKED` ·
`UNVERIFIABLE`**, and the third is the one a two-state model gets wrong: an identity provider that
cannot be reached is not a revoked actor, and treating it as one turns an outage into a wave of
governance refusals nobody can act on. Add `ACTION_AUTHORIZATION_UNVERIFIABLE` (§5) — **fail closed,
with a different operator remedy** — and reuse `WorkerIdentityResolver` rather than writing a second
resolver. ▲ Note its built-in resolver handles `local user:` subjects only; a non-local/OIDC
deployment needs an adapter, and that is an operator prerequisite (§17 step 0b), not a code detail.

### ▲ 0.1.0 THE DEVELOPMENT AUTHORIZATION POLICY — owner ruling, 2026-08-22, and it SUPERSEDES much of what follows

> *"Because the tool is still under development, we do not need segregation of duties now. That
> would be premature complexity."*

**The policy, in full:**

```
any authenticated DEVELOPMENT user
        -> may trigger any IMPLEMENTED, NON-PRODUCTION stage
        -> and the server records WHO triggered it
```

| Action | Development policy |
|---|---|
| `AUTHOR_FORMULA` | **Allow** |
| `GENERATE_PREVIEW` | **Allow** |
| `EXECUTE_SANDBOX` | **Allow once a worker exists** (§9.0) |
| `PUBLISH_SANDBOX` | **Allow once implemented** |
| `MATERIALIZE_PRODUCTION` | ▲ **UNAVAILABLE** |
| `PUBLISH_PRODUCTION` | ▲ **UNAVAILABLE** |

▲ **The safeguards are NOT relaxed, and this is the whole point of the ruling:**

1. **Roles are never accepted from the request body.** Development users get broad permissions
   **server-side** (§0.1's `roles` deletion stands, unchanged and undiminished).
2. **The triggering user is recorded**, so *"my runs"* and audit history work.
3. ▲ **A client may not supply an authorization belonging to somebody else.** The server
   **creates or resolves** it from the run and the current user.
4. **Production actions stay unavailable** until production governance exists.
5. **Kind is a DEVELOPMENT/SANDBOX environment**, whatever an action happens to be called.

> ▲ **The principle, and it is the sentence to quote back at any future shortcut:**
> *"'Allow everyone in development' must be an EXPLICIT SERVER POLICY — not permission inferred
> from client-supplied roles."*
>
> The permissive half is temporary. The **server-owned** half is the production path, and it is
> being built correctly now precisely so that tightening the policy later is a change of values
> rather than a change of architecture.

#### ▲ 0.1.0.1 What this SIMPLIFIES — migration 1100 shrinks

```
action_authorization_revision
    authorization_id · action · resource_identity_hash
    actor_subject · environment_id
    policy_version    = 'development-v1'
    permission_result = 'allowed'
    evidence_hash
```

▲ **No approver, grantee, delegation, environment-entitlement or segregation-of-duties tables are
needed now.** The following, specified in earlier revisions, are **DEFERRED to production
readiness** and must not be built into the development path:

| Deferred | Where it was specified |
|---|---|
| approver ≠ executor, and any four-eyes rule | §0.1.1.1 |
| delegation records — issuer, scope, expiry | §0.1.1 |
| per-environment / per-group entitlement checks | §0.1.1.1 |
| revocation tri-state and `ACTION_AUTHORIZATION_UNVERIFIABLE` | §0.1.1 |
| the legacy-authorization grantee question | §0.3 |

▲ **IMPLEMENTED 2026-08-22** — migration **1100** and `materialize/action_authorization.py`, with
`ActionV1` (the six-name service vocabulary, deliberately distinct from `EvaluatorAction`'s three),
`authorize_action`, `load_action_authorization`, and the append-only guard. 19 tests.

▲ **AND IT IS EXPAND-ONLY, which is a correction to this plan rather than a detail of it.** 1100
**drops nothing**: `generation_authorization` and 1095's chain are untouched, so the running image
keeps working and **an image-only rollback stays safe**. Earlier revisions had 1100 create, re-point
and drop in one file — which is precisely what makes §20.1 say rollback must restore the database
too. **Splitting expand from contract removes that requirement for this migration**, and the contract
half (re-point 1095, drop the old table) becomes a later migration once all six acts have callers
here. ▲ **Apply the same split to 1101–1105 wherever it is available** — it is the cheapest way to
shrink the maintenance window §20.1 describes.

▲ **Production actions raise `ActionUnavailable` and record NOTHING.** An authorization table holds
authorizations; a refusal is a decision, and decisions belong to `action_decision_revision` (§7.1).
This also means the two production acts have **no authorized code path at all**, which is strictly
stronger than a certificate gate — a gated action still has a path a bypass could reach.

▲ **`policy_version` is what makes the deferral safe rather than a hole.** Every development
authorization is stamped `development-v1`, so the day production governance lands, *"which
authorizations were issued under the permissive policy"* is a query rather than an archaeology
exercise — and none of them can be mistaken for a governed approval.

▲ **This becomes a RELEASE-READINESS requirement, not a blocker for current development** — and it
belongs in §21 beside the corpus obligations, because both are things that must be true before live
and neither gates the build.

#### ▲ 0.1.1.1 A client may not spend an authorization it was not issued — R2, and it is LIVE

▲ **SUPERSEDED IN FRAMING by §0.1.0, and RETAINED as safeguard 3.** Earlier revisions argued this as
*"the caller must BE the grantee"*, on a segregation-of-duties reading. **That reading is refused for
now** — the owner's ruling is that approving and running are one act in development. What survives,
and what the check below actually enforces, is narrower and permanent: **permission is server-owned,
so a caller cannot spend an authorization the server did not issue to them.**

**Verified: any caller with `feature:generate` may spend another actor's approval.** `GenerationIn`
(`build_sets.py:112`) carries `generation_authorization_revision_id` **from the client**, and the
route validates only that the approval covers the **build set** (`build_sets.py:202`, 409
otherwise). ▲ **Nothing compares the approval's grantee to `identity.subject`.** 1095's chain proves
coverage; it says nothing about who may consume it. `requested_by` then records the caller — so the
audit shows one person spending another's approval, and reads as if that were intended.

> **The rule: an authorization is PRESENTED, not merely REFERENCED.** The requesting actor must be
> the authorization's `actor_subject`. Refuse otherwise with `ACTION_AUTHORIZATION_NOT_HELD` (§5).
> ▲ **Under §0.1.0 this is not a duties rule — it is the "server owns permission" safeguard**, and
> the delegation branch is deferred with the rest of the production governance.

▲ **IMPLEMENTED 2026-08-22** (`build_sets.py`, `authorization_grantee`): the route refuses 403
`ACTION_AUTHORIZATION_NOT_HELD` and enqueues nothing. ▲ **The grantee had to be read separately**,
because `load_generation_authorization` reconstructs only the five identity-bearing columns —
`GenerationAuthorizationV1.identity_payload()` excludes the actor by design and `revision_id` is
content-addressed over it, so folding the actor in would re-mint every authorization id.
▲ **Five existing route tests failed on the change, and the failure was the finding**: they seeded
`authorized_by="user:ops"` and ran as `user:sam`, so the suite had been asserting that spending
somebody else's authorization succeeds.

**The FULLER form of this safeguard, per §0.1.0 point 3:** the server **resolves or creates** the
authorization from the run and the current user, and `generation_authorization_revision_id` stops
being a request field at all. The equality above is the interim, deliberately strict — a request
naming another user's approval is refused rather than silently re-pointed, so nothing comes to depend
on the leniency.

### ▲ 0.1.2 Authorization and decision are bound RELATIONALLY to action and resource — R6

A plain `job.action_authorization_revision_id → action_authorization_revision(id)` is **not enough**:
a preview job could reference an authorization issued for production, or for another build, and every
FK would pass. ▲ **Migration 1095 already solved this shape with COMPOSITE keys** — its whole point is
that *"a request may only point at an artifact produced under the SAME approval in the SAME
environment"* (`1095:126`). **The replacement must be at least as strong as the thing it replaces.**

```
action_authorization_revision   authorization_id · action · resource_identity_hash · ...
action_decision_revision        decision_id · authorization_id · action · resource_identity_hash
<every durable attempt>         action · resource_identity_hash · authorization_id · decision_id

    FOREIGN KEY (action, resource_identity_hash, authorization_id)
        REFERENCES action_authorization_revision (action, resource_identity_hash, authorization_id)
    FOREIGN KEY (action, resource_identity_hash, decision_id, authorization_id)
        REFERENCES action_decision_revision
                   (action, resource_identity_hash, decision_id, authorization_id)
```

▲ **`authorization_id` is in the DECISION key on purpose (C2).** Without it an attempt could cite
decision **D** — issued under authorization **A** — while itself naming authorization **B**, and every
foreign key would still pass. Carrying it in both keys forces the three to agree transitively. **This
is the same defect §11.0.1 fixes for formulas**, and it was reintroduced here in the first draft of
this very section; a composite key that omits the linking column is a join, not a constraint.

* ▲ **`action_decision_revision_id` must be a NOT NULL column on every request/attempt table**, and
  revision four never named one. A decision table nothing references is §15's failure again.
* ▲ **`resource_identity_hash` is a hash, so it cannot itself FK to a typed parent.** Where an action
  names a typed resource — a build set, a sealed artifact, a formula draft — use a **typed child
  table** per action carrying the real FK, and derive the hash from it. An unrestricted JSON payload
  hashed into a column is not a relational binding; it is a promise.

### ▲ 0.1.3 One job performs SEVERAL actions, so it needs several authorizations — R7

`code_generation_job` (child §3.5) carries one `requested_action` and one authorization link, while
its journey performs `AUTHOR_FORMULA` → `GENERATE_PREVIEW`, and its workspace then shows sandbox
execution and publication. ▲ **One authorization cannot truthfully cover all of those**, and §3's
whole ruling is that each is a separately governed act.

```
code_generation_job_action
    job_id · action · resource_identity_hash
    authorization_revision_id · decision_revision_id · state
    PRIMARY KEY (job_id, action)
```

**Every worker claims ONE action stage and revalidates that stage's own authorization and decision.**

▲ **And there is a live bypass to close in the same change.** `POST
/considered-revisions/{id}/options/{id}/formula-drafts` (`formula_drafts.py:117`) creates a draft and
an outbox message with **no action authorization, no decision and no spend authorization** — the
worker then trusts `formula_draft.requested_by` (`formula_draft_worker.py:239`). Direct formula
drafting is `AUTHOR_FORMULA`; it must bind all three **before the outbox row is written**, on the same
before-the-queue discipline as retirement (§11.1.1) and spend (§11.2).

### ▲ 0.1.4 `AUTHOR_FORMULA`'s subject is a CANDIDATE, not a selection — R8

Two rules, both deliberate, currently contradictory:

| | |
|---|---|
| §7's evidence table | `AUTHOR_FORMULA` carries **selections** |
| child step 5b item 1, and the live route | per-row drafting **without** selection — *"This route never selects"* (`formula_drafts.py:125`) |

▲ **Inspection before selection is a product property, not an oversight**, and fabricating a
selection in order to authorize an inspection would destroy it. **The subject of `AUTHOR_FORMULA` is
an immutable authoring subject** — precisely the five facts that already constitute draft identity:

```
authoring_subject_revision
    considered_revision_id · option_id · planning_request_hash
    catalog_snapshot_hash  · definition_revision
```

▲ **These are the SAME FIVE FIELDS as §11.1.1's `retirement_scope_key` — so they are ONE concept and
must not carry two names (C4).** `retirement_scope_key` **is** the authoring subject's identity hash.
Stating it that way is not tidying; it says something true that the two-name version hides:
**retirement withdraws a SUBJECT, authorization authorizes a SUBJECT, and the money guard's
non-configuration half IS that subject.** One tuple, one hash, three uses.

`AUTHOR_FORMULA` authorizes **that subject**. A later `feature_selection_revision` binds the
resulting READY formula through §11.0.1's binding. **§7's evidence table is corrected accordingly:
`AUTHOR_FORMULA` carries an authoring-subject revision, never selections.**

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
   NOT MEASURED, and this plan does not claim it.**
   ▲ **The owner's third review: the unmeasured 295 / 90 / 3 counts are NOT a design blocker — but
   Phase B MUST measure and record them, as a VERSIONED FUNNEL, before ANY coverage claim.** Not
   before the work starts; before anyone says how much of the registry this programme reached.
   The funnel's shape and version stamp are specified in Phase B.
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
  mixed-method journey test unwritable (fact 4). Task-level design lives in the **child plan**
  `2026-08-22-recipe-to-code-llm-fallback.md`; this plan owns the gating contract it must satisfy.
  ▲ **"Reviewed" means the reviewed EXPECTATION REGISTRY at generation V2 — not a derived
  blueprint** (§10.1). On today's evidence that is exactly ONE recipe, `posted_debit_amount`.
  ▲ **Its production ceiling is sandbox until §12's eight pieces exist.** Stated up front so nobody
  discovers it at Phase F.
* **D2 — DELETE the old materialization route and its execution entry points. Build NO adapter, not
  even a temporary one.** ▲ **Re-confirmed by the owner in the third review, and it SUPERSEDES the
  child's earlier "keep it briefly as a thin adapter" commitment.** The product is pre-live and the
  migration surface is enumerated, not guessed:

  | Surface | What it is | Disposition |
  |---|---|---|
  | `POST /materialization-runs` (`materialization_runs.py:197`) | enqueues compile → render → seal → L0 → **publish** | **delete** |
  | `GET /materialization-runs/{request_id}` (`:260`) | the run sheet's status read | **delete** |
  | `api/app.py:252` | the router registration | **delete** |
  | `enqueue_materialization` (`queue_lane.py:526`) + the legacy queue handler | the producer and the worker for that job | **delete** — a route removed while its producer survives is a bypass, not a deletion (§8.3) |
  | `frontend/src/api.ts:4257` + `VITE_MATERIALIZATION_RUNS` (`nav.ts:37,41`) | the run-sheet read and its flag | **migrate onto a build-set request read, then delete the flag** |
  | `test_materialization_runs.py` (`_PATH` :71, schema assertion :222) · `test_materialization_e2e.py` (`_PATH` :144) · `test_seam_walkthrough.py` (:10, :380) | the three suites that drive it | **migrate onto the canonical lane** |
  | `tests/featuregen/materialize/test_chain.py:1402` | a docstring naming the old status read | **re-word** |

  ▲ **REVISION FIVE — what follows is a REPLACEMENT CHECKLIST, and revision four read it as proof of
  present equivalence. §9.0 disproves that:** `POST /feature-execution/verifications` records a row
  **no worker claims**, so sandbox execution does not exist on the canonical lane today. The table
  names where each act *will* live; the deletion's entry condition is that those replacements
  **execute**, not that they are named (§17 step 10).

  ▲ **Every act it performed has a NAMED canonical home — which is not the same as a working one:** `POST /feature-execution/generations`
  (`feature_execution.py:141`) authorizes, the build-set generation lane compiles/renders/seals,
  `POST /feature-execution/verifications` (`:307`) is sandbox EXECUTION, `POST
  /feature-execution/publications` (`:424`) is sandbox PUBLICATION, and §9 adds the two production
  acts. The legacy route's only unique property was doing all of them under one decision — which is
  the conflation §2 exists to name, not a capability.
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

### ▲ The separation is a PRODUCT ruling, and atomicity does not overturn it

The owner, third review, 2026-08-22 — record this reasoning, because a future change will re-derive
the technical argument and think it has found a simplification:

> *In banking, writing production customer values is already a production act, even before making
> them visible.*

**MATERIALIZE_PRODUCTION and PUBLISH_PRODUCTION remain two separately governed decisions.** The
three properties below are therefore **not a route back to five actions**. They are the engineering
price of a *different* product answer, not an argument for it:

1. **No API, queue message, CLI or worker entry point can trigger production execution without its
   publication in the same governed act.** Enumerated and tested, not asserted.
2. **The production namespace is unreachable by the sandbox executor** — otherwise the sandbox path
   becomes the independent production-execution path.
3. **A partial failure leaves nothing readable** in the production namespace.

▲ **Proving all three makes consolidation technically POSSIBLE. It does not make it the product's
decision.** Consolidating the two acts requires BOTH the three proofs AND a new owner ruling that
supersedes the sentence quoted above. Until both exist, **SIX actions** — and a change that merges
them on the strength of the proofs alone is a product change made by engineering.

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

Rows marked `(child)` were folded in from the child plan's deleted D4 matrix and from the blocker
codes its §3.2 proposed. They are here, and only here.

| Condition | AUTHOR_FORMULA | GENERATE_PREVIEW | EXECUTE_SANDBOX | PUBLISH_SANDBOX | MATERIALIZE_PRODUCTION | PUBLISH_PRODUCTION |
|---|---|---|---|---|---|---|
| **No reviewed expectation, valid LLM-authored formula exists** | **Allow** | **Allow after deterministic validation** | **Allow + warn** | **Allow + warn** | **Require LLM-method certification + feature governance** | **Same certificate, re-derived** |
| **Reviewed REGISTRY entry at generation V2, current, and it binds** (§10.1) | **Deterministic, zero provider calls** | Allow | Allow | Allow | **Require deterministic-method certification (§12)** | **Same certificate, re-derived** |
| ▲ **Blueprint DERIVABLE but NOT reviewed** `(child)` | **LLM route — a derivation is not a review (§10)** | Allow after deterministic validation | Allow + warn | Allow + warn | ▲ **Require LLM-method certification + feature governance** — the SEALED method is `LLM_AUTHORED`; origin does not override it (§4.1) | ▲ **Same certificate, re-derived** |
| Reviewed expectation exists but is **Formula V1** `(child)` | **LLM route + warn** — V1 is not a V3-producible method (§10) | Allow after deterministic validation | Allow + warn | Allow + warn | ▲ **Require LLM-method certification + feature governance** (§4.1) | ▲ **Same certificate, re-derived** |
| Deterministic instantiation of a reviewed blueprint **refused** `(child)` | **Block by name — NO silent LLM fallback**; an explicit new draft identity is a new user act | Block | Block | Block | Block | Block |
| Method certificate pending / never run | Allow | Allow + warn | Allow + warn | Allow + warn | **BLOCK** | **BLOCK** |
| Method certificate stale | Allow | Allow + warn | Allow + warn | Allow + warn | **BLOCK** | **BLOCK** |
| Certificate exists but **method-mismatched** | Allow | Allow | Allow | Allow | **BLOCK** | **BLOCK** |
| Any member lacks method identity (§10) | Allow | Allow | Allow | Allow | **BLOCK** | **BLOCK** |
| Mixed-method artifact, one member's certificate stale | Allow | Allow | Allow | Allow | **BLOCK — all-must-pass** | **BLOCK — all-must-pass** |
| **Recipe business review not current** (`RECIPE_REVIEW_NOT_CURRENT`) `(child)` | Allow + warn | Allow + warn | Allow + warn | Allow + warn | **BLOCK** | **BLOCK** |
| Missing customer relationship / grain / **unbound operand** | Block | Block | Block | Block | Block | Block |
| **No frozen catalog snapshot** `(child)` | **Block** — there is nothing to author against | Block | Block | Block | Block | Block |
| **Formula validation failed** `(child)` | Block — the proposal is the output, the refusal is the answer | Block | Block | Block | Block | Block |
| Target leakage | ▲ **WARN** — authoring proceeds and the formula is visible; §13 decides at which altitude | Block | Block | Block | Block | Block |
| Unsupported renderer operation | ▲ **WARN** (§5.1) | Block | Block | Block | Block | Block |
| Missing currency / reversal / status policy | ▲ **WARN** (§5.1) | Block | Block | Block | Block | Block |
| Caller lacks read authorization for the physical columns | ▲ **WARN** — and §13's `next_step` serves the business-level formula with no physical refs | Block | Block | Block | Block | Block |
| **Caller lacks a data-USE licence** (`PERSONAL_DATA_POLICY_REQUIRED`) `(child)` | Block — a licence is not a read scope, and no formula review makes the use lawful | Block | Block | Block | Block | Block |
| **LLM authoring required, provider unreachable** `(child)` | **Block THIS member only** — a reviewed-blueprint member in the same set is unaffected | ▲ **DROP** — the gate for "no formula yet" is `FORMULA_NOT_AUTHORED`, not provider reachability (§4.1) | ▲ DROP | ▲ DROP | ▲ DROP | ▲ DROP |
| **LLM authoring required, no approved cost ceiling** `(child)` | **Block this member** — spend is an authorized act, not a side effect (§11.2) | ▲ **DROP** (§4.1) | ▲ DROP | ▲ DROP | ▲ DROP | ▲ DROP |
| **Conceptual pattern** (`CONCEPTUAL_PATTERN_NOT_AUTHORABLE`) `(child)` | ▲ **BLOCK** — `next_step` is "Save idea / specify computation"; never a deterministic Generate Code affordance (§5.1) | Block | Block | Block | Block | Block |
| **Governed model output** `(child)` | ▲ **BLOCK** — not a formula. `next_step` routes to the model workflow, which is a **different set of acts** governed by its own certification programme, not a cell in this table (§5.1) | Block | Block | Block | Block | Block |
| **Draft authored under legacy identity V1** (`LEGACY_CONFIG_UNPROVEN`) `(§11.1)` | n/a — the draft exists | **Allow + warn** — a legacy draft is an auditable PREVIEW artifact | Allow + warn | Allow + warn | **BLOCK** — an unprovable configuration cannot be certificate-matched | **BLOCK** |
| **A retirement tombstone covers this candidate** (`FORMULA_DRAFT_RETIRED`) `(§11.1)` | **Block — checked BEFORE any provider work is enqueued** | Block | Block | Block | Block | Block |
| **Identity V2 active, only a legacy V1 draft exists, no regeneration approval** (`LEGACY_REGENERATION_NOT_APPROVED`) `(§11.1)` | **Block — re-authoring is an approved, cost-confirmed act, never an automatic re-spend** | ▲ **DROP** — there is no V2 draft to preview; the legacy draft is governed by the row above (§5.1) | ▲ DROP | ▲ DROP | Block | Block |
| No sealed artifact yet | n/a | n/a — preview produces it | Block | Block | Block | Block |
| Artifact sealed, **not yet verified** | Allow | Allow | **Allow — this is what produces verification** | **Block** | Block | Block |
| Verification stale | Allow | Allow | **Allow — re-execute** | **Block** | Block | Block |
| Artifact not servable (subgraph check refused) | Allow | Allow | Block | Block | Block | Block |
| Environment incompatible | Allow | Allow | Block | Block | Block | Block |
| Formula draft not pinned to the build set (§11) | Allow | **Block** | Block | Block | Block | Block |

▲ **Two child rows were folded as CORRECTIONS rather than copied.**

* The child's *"Artifact not verified → Sandbox: Depends on execution policy"* is the same evasion
  the merged sandbox column produced (see the ▲ above). It does not depend. It splits into the two
  sandbox rows already in the table.
* The child's *"User lacks read/use authority → Formula visible only if already authored"* made
  visibility depend on whether the platform had already spent money, which is not an authorization
  rule. It splits into the read-scope row (§13's business-level formula) and the new data-use
  licence row, which blocks outright.

▲ **`AUTHOR_FORMULA` is an EXPLICIT, COSTED act, never a side effect of selection.** The child's D5
is a rule this matrix assumes rather than states: selecting a candidate updates client state and
spends nothing; polling, reload and double-click create no provider work. The money guard (§11.1) is
the durable half of that promise, and it is currently broken.

**The rule, stated so it can be quoted back at a future change:**

> *A missing reviewed expectation changes the AUTHORING METHOD. It must not automatically prevent
> code preview.*

Which resolves to two routes and no third:

* **A reviewed expectation-registry entry exists, at generation V2, and it binds** → instantiate the
  formula **deterministically. NO LLM calls.**
* **Anything else — including a merely DERIVED blueprint (§10.1)** → **explicitly** ask the LLM,
  **validate deterministically**, and allow preview and sandbox if the result is valid.

There is no automatic silent fallback from a failed deterministic instantiation to the LLM: the
method is chosen from evidence before authoring, and a deterministic failure is a deterministic
refusal.

▲ **This table is illustrative, and it is not the contract.** The contract is §5's exhaustive
reason-code × action table. A matrix of representative conditions is exactly how a code with no
disposition gets shipped.

### ▲ 4.1 Production eligibility follows the SEALED METHOD, never the candidate's origin — P0-8

**The finding is a contradiction inside this table, not a preference.** Three rows routed a candidate
to the LLM and then blocked its production categorically:

| Row | It routes to | It then said | Which means |
|---|---|---|---|
| no reviewed expectation, valid LLM formula | `LLM_AUTHORED` | require LLM certification → **production reachable** | correct |
| blueprint derivable but **unreviewed** | `LLM_AUTHORED` | **BLOCK** | the same sealed method, a different answer |
| reviewed expectation at **Formula V1** | `LLM_AUTHORED` | **BLOCK** | the same sealed method, a different answer |

All three seal `LLM_AUTHORED`. ▲ **Once a formula is authored, sealed and immutable, the recipe
maturity it started from is history, not authority.** A rule that says otherwise makes production
eligibility depend on a fact about a *recipe registry* that the sealed artifact does not contain —
while §10's certificate is matched against the **method identity**, which is identical in all three
rows. The platform would be refusing an artifact for something the artifact does not say.

> **The rule, stated so it can be quoted back at a future change:** production eligibility is decided
> by **sealed method provenance** (1099 + §10's method identity) **+ the current required certificate
> SET for that method** (§10) **+ feature-specific governance** (§14). **Candidate origin and recipe
> maturity are WARNINGS and independent business-review facts. They are never production gates.**

▲ **This does not make unreviewed recipes easier to ship.** All three rows still require a current
LLM-method certificate, and §21 measures how far away that is. What changes is that the *reason* for
the refusal becomes the one that is true — `METHOD_CERTIFICATE_MISSING` — rather than a permanent
mark against a candidate's ancestry that no evidence in the artifact supports.

▲ **The same correction applies to the two authoring-time blockers.** `PROVIDER_UNAVAILABLE` and
`COST_AUTHORIZATION_MISSING` are conditions of **buying** a formula. Once a formula exists, they are
facts about the world with nothing to say about previewing, executing or publishing an immutable
artifact that is already paid for. They are `DROP` on the five downstream actions — not because they
do not matter, but because **the gate for "there is no formula" is `FORMULA_NOT_AUTHORED`, and a
blocker that answers a question nobody asked is how a provider outage takes production down.**

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
(`tests/featuregen/overlay/upload/test_evaluator_contracts.py:50`) asserts equality against *codes
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
  `test_evaluator_contracts.py:50` — enumerate the reason module's codes by reflection, enumerate
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
| `LLM_AUTHORING_REQUIRED` `(child)` | **a WARNING, never a blocker** — the route selector, surfaced |
| `REVIEWED_EXPECTATION_LEGACY_VERSION` `(child)` | a reviewed expectation exists and is Formula V1, so it cannot select the V3 producer (§10) |
| `REVIEWED_BLUEPRINT_NOT_EXECUTABLE` `(child)` | deterministic instantiation of a reviewed blueprint refused — **and does not fall back** |
| `BLUEPRINT_DERIVED_NOT_REVIEWED` `(child, new)` | a blueprint was *derived* structurally and no human reviewed it (§10) |
| `FORMULA_VALIDATION_FAILED` `(child)` | the authored proposal did not pass deterministic validation |
| `CATALOG_SNAPSHOT_NOT_FROZEN` `(child)` | there is no frozen snapshot to author or bind against |
| `LEGACY_CONFIG_UNPROVEN` `(§11.1)` | the draft's historical authoring configuration cannot be proven — it is identity **V1**, whose config hash is a constant. Previewable, never certifiable |
| `FORMULA_DRAFT_RETIRED` `(§11.1)` | a retirement tombstone covers this candidate's retirement scope. ▲ Today this refusal exists only as an HTTP body (`formula_drafts.py:161`) raised **after** an identity collision; §11.1 makes it a reason code checked **before** provider work |
| `LEGACY_REGENERATION_NOT_APPROVED` `(§11.1)` | identity V2 is active, a legacy V1 draft covers this candidate, and no approved, cost-confirmed regeneration authorizes buying the answer again |
| ▲ `ACTION_AUTHORIZATION_REVOKED` `(§0.1.1)` | the authorization is well-formed and the actor's CURRENT entitlement no longer covers it |
| ▲ `ACTION_DECISION_MISSING` `(§7.1)` | the act references no request-time decision revision — a queue bypass by definition |
| ▲ `DECISION_DRIFT` `(§7.1)` | the worker recomputed the decision's evidence and a pin moved. **A refusal a person re-requests, never a re-decision** |
| ▲ `FORMULA_NOT_AUTHORED` `(§4.1)` | no formula exists for this member yet — the honest downstream gate that `PROVIDER_UNAVAILABLE` was standing in for |
| ▲ `COST_AUTHORIZATION_EXHAUSTED` `(§11.2)` | a spend authorization exists and its calls, tokens or cost are spent |
| ▲ `SELECTION_FORMULA_BINDING_MISSING` `(§11.0.1)` | a build-set member references no `selection_formula_binding` — the relational proof that the formula belongs to the selection |

▲ **Three codes the child proposed are NOT added, and the reason matters.**
`PRODUCTION_METHOD_CERTIFICATE_MISSING` / `_STALE` and `PRODUCTION_RECIPE_REVIEW_NOT_CURRENT`
duplicate `METHOD_CERTIFICATE_MISSING`, `METHOD_CERTIFICATE_STALE` and the existing
`R.RECIPE_REVIEW_NOT_CURRENT`. A code whose name embeds the ACTION cannot have a per-action
disposition — the action is already a column. `LLM_AUTHORING_UNAVAILABLE` becomes
`PROVIDER_UNAVAILABLE`, above, for the same reason. `FORMULA_NOT_READY` is a STATE, not a blocker.

### ▲ Adding any code is a THREE-PART commit — P0, folded from the child's B3

**Verified**: `tests/featuregen/overlay/upload/test_evaluator_contracts.py:50` regex-scans
`activation_policy.py`'s **source text** for `R.([A-Z_]+)` and asserts

```python
assert len(emitted) == 16, sorted(emitted)
assert emitted | EVALUATOR_ONLY_BLOCKERS == set(ACTIVATION_BLOCKER_DISPOSITIONS)
```

Both are **EQUALITIES**, deliberately — the test's own docstring says a superset check *"would let a
genuinely lost code back in"*. So a seventeenth code mentioned anywhere in that module — even in a
comment the regex happens to match — fails CI until the literal moves.

▲ **And the second failure mode is worse than a red build.** `ACTIVATION_BLOCKER_DISPOSITIONS`
(`evaluator_contracts.py:61`) is **18 CARRIED / 4 DROPPED** — verified by counting the rows.
`evaluate_publish_sandbox` (`evaluate_execution.py:165`) folds `carried_blockers(...)` as its **FIRST
act**, before it reads a single row of evidence. There is no literal default: `carried_blockers`
indexes the mapping and **raises `KeyError`** on an unknown code, which is a 500 out of the sandbox
publish path rather than a refusal. So a new production-certificate code has exactly two ways to go
wrong, and both violate the owner's ruling that gold must not gate sandbox:

| What you do | What happens |
|---|---|
| Add the code, no disposition row | `carried_blockers` raises — sandbox publication **crashes** |
| Add the code with `CARRIED` (the table's norm, 18 of 22) | sandbox publication **is blocked by a production certificate** |

**Therefore every new code lands with all three in the SAME commit:** its
`semantic_eligibility_reasons` entry · an explicit `CARRIED`/`DROPPED` row **with a written reason**
(the `len(reason) > 40` test enforces that a decision was made, not copied) · the `== 16` literal
updated. The §5 `ACTION_DISPOSITIONS` table does not replace this one — until §7's service is the
only caller, both exist, and both must be exhaustive.

Families the table must cover by name, per the owner: conceptual patterns
(`CONCEPTUAL_PATTERN_NOT_AUTHORABLE`), governed model outputs, stale recipe review
(`RECIPE_REVIEW_NOT_CURRENT`), provider unavailable, cost authorization missing, legacy readiness
(`READINESS_NOT_MATERIALIZATION_READY`), method certification missing/stale, artifact states
(`ARTIFACT_NOT_SERVABLE`, `ENVIRONMENT_INCOMPATIBLE`), verification states
(`VERIFICATION_NOT_CURRENT`).

### ▲ 5.1 A disposition cell contains a DISPOSITION — P0-8, second half

`Disposition` has exactly three members, and every cell must contain one of them. §4's illustrative
matrix carried cells reading *"business formula visible"*, *"Model workflow"*, *"Save idea / specify
computation only"* and *"n/a — there is no V2 draft"*. **Those are UI behaviours and narrative. A cell
containing one cannot be folded by code**, which means the exhaustive table would have been
exhaustive in shape and unusable in fact.

* **What the UI DOES with a `BLOCK`** — serve the business-level formula (§13), offer *"Save idea"*,
  route to the model workflow — is the **rendering of a refusal**, not a fourth disposition. It lives
  in the blocker's `next_step` string, which §5's contract already requires of every code.
* ▲ **`DROP` is the honest cell for "this action is not the gate for this fact"**, and it already
  means exactly that (`evaluator_contracts.py`'s module docstring). Use it instead of `n/a`, which
  looks like an omission and reads like one in a diff.
* ▲ **`WARN` is neither "allowed" nor "blocked".** It proceeds **and the caller must be told** (D3).
  The three policy rows — leakage, renderer, currency — are `WARN` at `AUTHOR_FORMULA` because
  authoring genuinely proceeds and the formula is genuinely visible; the refusal arrives at preview.

**The CI exhaustiveness test asserts each cell is a `Disposition` MEMBER**, not merely that a cell
exists. That is what makes this mechanical rather than editorial — and it is the check that would
have caught all four of the cells above.

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

▲ **TWO VOCABULARIES, and the child was naming the wrong one — P0, folded from the child's B2.**
The child's §3.2 and D3 proposed to keep `no_reviewed_formula_expectation` in the "legacy fold-owned
set" as though that were the enforcing blocker. It is not. **Verified — these are two different
codes in two different modules, and only one of them refuses anything:**

| Code | Where | What it does |
|---|---|---|
| `"no_reviewed_formula_expectation"` | `recipe_readiness.py:37` (`BLOCKER_NO_REVIEWED_EXPECTATION`) | a **readiness-fold** string. Displays, sorts, explains. Enforces nothing |
| `R.FORMULA_NOT_REVIEWED` = `"FORMULA_NOT_REVIEWED"` | `semantic_eligibility_reasons.py:93` | the **activation-policy** code emitted at `activation_policy.py:184`. This is the refusal |

Leaving `no_reviewed_formula_expectation` fold-owned is correct *and changes nothing about the
gate*. `R.FORMULA_NOT_REVIEWED` is the code the ladder actually raises, and it is what Phase B must
re-home. ▲ The same distinction applies to the gold code: `BLOCKER_GOLD_UNPROVEN`
(`"gold_evaluation_unproven"`, `recipe_readiness.py:39`) is a fold string with **no counterpart in
`semantic_eligibility_reasons`** — which is precisely why §0.2 fact 1 could measure that removing
gold changes zero served states.

▲ And note where `R.FORMULA_NOT_REVIEWED` already sits in the disposition table:
**`DROPPED`** — *"this evaluator is not the gate for it"*. The evaluator chain had already decided
review is not its question. The activation ladder never got the message.

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
preview would leave the same 295 recipes blocked by a different sentence, and §19's journey test
would pass its production assertion while failing its preview assertion for an unrelated reason.

---

## 7. The shared decision service — discriminated, typed, server-loaded

Revision two proposed one generic signature with an optional `artifact_id`. ▲ **That shape cannot
carry the evidence the six actions need**, because the evidence differs per action and an optional
field is how a required one goes missing. Replace it with **discriminated request types**:

| Action | The immutable evidence its request MUST carry |
|---|---|
| `AUTHOR_FORMULA` | ▲ **an `authoring_subject_revision`, NEVER selections (§0.1.4)** · ▲ **the resolved strategy from the persisted authoring plan** (child §3.3 — never a client label) · provider contract ref *(required iff LLM)* · ▲ **`llm_spend_authorization_id`** *(required iff LLM, §11.2)* · ▲ `formula_method_override_revision` id *(iff this is a retry, §11.3)* |
| `GENERATE_PREVIEW` | selections · ▲ **the `selection_formula_binding_id` per member** (§11.0.1 — **not** a loose draft id and hash, which cannot prove the formula belongs to the selection) · build declaration revision · renderer id + version · environment id |
| `EXECUTE_SANDBOX` | sealed artifact id · inventory observation id |
| `PUBLISH_SANDBOX` | sealed artifact id · verified output revision id **from §9.0's worker** · staging identity · publication capability attestation |
| `MATERIALIZE_PRODUCTION` | sealed artifact id · **production** environment id · production target ref · per-member **per-kind** certificate bindings (§10) |
| `PUBLISH_PRODUCTION` | ▲ **the materialization ATTEMPT id — never a client-supplied output id** (§9.1); the server resolves its output · per-member **per-kind** certificate bindings (§10) |

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

▲ **Two things this rule already excluded, which revision three then let back in elsewhere, and both
are now closed:** the child's *"Try AI formula"* was a client-supplied METHOD (→ §11.3's
server-verified override), and `PUBLISH_PRODUCTION`'s materialized-output id was a client-supplied
IDENTITY (→ §9.1's attempt-resolved output). **A rule stated in one section and contradicted in
another is not a rule**, which is why both now name this line explicitly.

### ▲ ONE canonical route, and the same service at BOTH moments

**This line used to read "Route BOTH paths through it", and that was a contradiction of D2.** It
quoted an earlier ruling — *"Both old and new APIs must receive the same answer"* — written while two
APIs were still expected to survive. **D2 deletes the old one** (§8.3), so "both APIs agree" is not a
property this system can have, or needs. What that ruling was protecting is its second half — *"do
not keep two independent readiness implementations"* — and the canonical shape satisfies it exactly:

* **one canonical route** per action (§1 D2's replacement table, §9's two additions);
* **`evaluate_action` is the only implementation** of the question, with no second readiness fold
  anywhere;
* **called at request time AND at worker time** (§8.2) — the same service, the same request type,
  answering at two moments about a state that can move;
* **both recipe and LLM METHODS** flow through it, which is the axis that genuinely has two values
  (§10, child D1). Method is not route.

▲ **"Both-route equivalence" is retired as a requirement and must not be reintroduced** — see §8.3
for what replaces it and why a test over a deleted route proves nothing.

### ▲ 7.1 The decision is a DURABLE RECORD, not a returned value — completeness gap 3

§8.2 requires the decision be checked twice, at request time and again in the worker. ▲ **Revision
three returned an `evidence_hash` and specified nothing that PERSISTS** — so the worker's "second
check" had nothing to compare against and could only ask the question again. Two independent answers
to the same question is not a drift check; it is a coin flipped twice.

**Migration 1106 — `action_decision_revision`, append-only:**

```
decision_id                        -- content-addressed over the payload below
action                             resource_identity     -- the discriminated request's identity
action_authorization_revision_id   NOT NULL FK           -- §0.1.1
per_member_verdicts_json           -- one verdict per member, with the all-must-pass fold recorded
blockers_json                      warnings_json
policy_version                     evidence_pins_json    -- the revision pins §7 already returns
evidence_hash
decided_at                         -- provenance, and therefore OUTSIDE decision_id
```

**At worker time: recompute, then COMPARE.**

| Outcome | What happens |
|---|---|
| the recomputed evidence hash **equals** the stored one | proceed — the state did not move |
| it **differs** | ▲ **refuse with `DECISION_DRIFT` (§5), recording WHICH pin moved** |
| there is **no stored decision** for this act | refuse — `ACTION_DECISION_MISSING`. An act with no request-time decision is a queue bypass (§8.3) |

▲ **The refusal is the whole point, and it is the easiest thing here to get backwards.** A worker
that finds moved evidence and simply **re-evaluates** will usually get "allowed" again and proceed —
silently executing under an answer no human was ever shown. **Drift is a refusal that a person
re-requests. It is never a re-decision the platform makes on their behalf.**

#### ▲ ASK and DECIDE are different calls over one implementation — R17

Revision four made every evaluation durable, which would write an `action_decision_revision` **every
time a screen rendered a button**. ▲ **The codebase already draws this line and says why.** `GET
/feature-execution/{artifact_id}/verify-eligibility` (`feature_execution.py:190`) exists so a
workspace can enable a control, and its docstring is explicit: *"A QUESTION — it records nothing.
Recording an attempt every time a screen rendered would fill the history with things nobody did."*

| Call | Records | Used by |
|---|---|---|
| `ask(...)` | **nothing** — pure | eligibility reads, button state, the plan endpoint's estimate |
| `decide(...)` | an `action_decision_revision`, returning its id | **only** an act that will be enqueued |

**One implementation, two entry points**, so the two can never answer differently — which is the
property §7 exists for. The worker's re-check (§8.2) compares against a `decide`; an act that carries
only an `ask` carries nothing, and refuses with `ACTION_DECISION_MISSING`.

### ▲ 7.2 An evaluator caller with no actor — R20

`corpus_generation.py:143` calls `evaluate_generate` and is **the only caller in the codebase that
supplies `activation_blockers`**. Neither plan lists it. It is a platform coverage job, not a user
act, so under §0.1.1's one-authorization model **it has no actor to authorize with** — the exact
"daemon privilege" problem §12.2 refuses for certification runs.

**Decide it explicitly rather than discovering it during the migration:** either it runs under a
recorded platform delegation (§0.1.1's delegation branch, with an issuer and an expiry), or it is
excluded from `evaluate_action` by name and keeps a separate, clearly non-authorizing evaluator. ▲ **A
third option — letting it keep calling `evaluate_generate` after that function becomes the governed
path — is the bypass, and it is the one that happens by default if nobody chooses.**

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

### 8.3 ONE canonical route — the legacy one is DELETED, and nothing adapts it

`materialization_runs.py:486` documents a keyless bypass in its own words: *"No option key is not a
refusal."* A work-item-driven request with no `considered_revision_id` and no `option_id` returns
`{}` and proceeds.

▲ **And the route is not a preview route.** `POST /materialization-runs` enqueues a job that
compiles, renders, seals, runs L0 and **can publish** — the status read reports `published_object`,
`published_generation_id` and `published_at` (`materialization_runs.py:305-324`) and its terminal
state `PUBLICATION_REFUSED` is a *publication* verdict. The build-set lane, by contrast, stops at a
sealed artifact. **There is no "old API preview" operation to map.**

Therefore — **and this is the owner's third-review ruling, which supersedes every earlier "adapter"
sentence in EITHER document:**

* **Mapping the old endpoint to `GENERATE_PREVIEW` alone is a BYPASS**, not a simplification: its
  later execution and publication would proceed under a decision made for a weaker act.
* **An adapter that orchestrated the stages correctly would BE the canonical lane** — with a legacy
  keyless entrance bolted to its front. Pre-live, that is strictly worse than the lane itself.
* ▲ **DELETE the route and its execution entry points. Build no adapter, not even temporarily.** The
  deletion surface is enumerated in §1 D2 — and it includes `enqueue_materialization` and the legacy
  queue handler, because **a route deleted while its producer survives is a renamed bypass**.
* **Rollback is the previous image** (child D9). There is no dark gate and no dual-write window.

**The three tests that replace "both-route equivalence":**

| Test | What it asserts | Where the pattern already exists |
|---|---|---|
| **Route absence** | the OpenAPI schema contains **no** path beginning `/materialization-runs`, and a live request to it is **404** | invert `test_materialization_runs.py:222`, which today asserts those paths are present |
| **Direct-queue bypass** | work submitted straight to the queue does not execute: the legacy handler is gone, so a legacy-shaped message dead-letters rather than running — and a canonical `generation_request` carrying no `action_authorization_revision_id` **refuses at the worker** (§0.1's NOT NULL FK) rather than building | `enqueue_materialization` (`queue_lane.py:526`) is the producer to delete; `enqueue_generation` (`generation_lane.py:261`) is the one that stays |
| **Both METHODS** | one build set seals a reviewed-blueprint member and an LLM-authored member; each carries its own per-member decision and its own certificate (§10) | Phase F, child step 10 test 4 |

▲ **Both-route equivalence tests are OUT OF SCOPE and must not be reintroduced.** After D2 there is
no second route to compare against; a test that drove one would be exercising a fixture and
reporting it as coverage. What must still agree is **request-time and worker-time** (§8.2) — the same
service, the same evidence, two moments.

---

## ▲ 9.0 The sandbox execution lane DOES NOT EXIST — P0-1

**The most serious finding in the verdict, and the one this plan most needed to be false.** Revision
three treated `EXECUTE_SANDBOX` as a working substrate to be re-gated, and §17 step 10 deletes the
legacy route on the strength of it. **Verified independently — four links, and every one is dead:**

| # | Link | What is actually there |
|---|---|---|
| 1 | `POST /feature-execution/verifications` (`feature_execution.py:307`) writes a `verification_attempt` row, and its own docstring promises *"a worker executes it"* | it **records**; it creates no durable lifecycle request |
| 2 | **no worker does** | `MATERIALIZATION_QUEUE_HANDLERS` (`runtime/queue.py:40`) is `frozenset({"materialization.compile.v1"})` — **one handler, and it is compile.** There is no verification lane in the durable worker at all |
| 3 | `verification_request` (migration **1094**, applied) and `verification_request_store.py` exist | **no production writer** |
| 4 | `authorize_publication_v2.py`, the one module that reads that store | ▲ **zero importers anywhere in `src/`** — only its own `__all__` and a single test. It is not under-consumed; it is **unreachable** |

▲ **The codebase already knew.** Migration 1092's own comment, explaining why `generation_request`
carries an explicit status column, names this exact gap: *"'still running' is distinguishable from
'the worker died' and from 'nothing consumes this table' — the gap S11's `verification_attempt` has
and this deliberately does not."* Somebody saw it, wrote it down, and designed around it.

▲ **The only concrete data execution in the codebase is still inside the LEGACY chain** — `run_l1`
→ submit generated project → `publish_generation` (`compile/chain.py:985`). **So §17 step 10 as
previously written would have deleted the only working execution path and replaced it with a
promise.** That is the sequencing error this finding prevents, and it is why the sandbox worker is
now **step 6** and the deletion stays at **step 10**.

### What has to be built — eight responsibilities, none optional

A durable sandbox verification worker that:

1. **creates a `verification_request`** — the lifecycle row, not a bare attempt;
2. **claims it with lease and fencing semantics** — the pattern `claim_one` already implements for
   the materialization lane, **reused rather than reinvented**;
3. **prepares frozen inputs** — the pinned formula through its binding (§11.0.1), the sealed
   artifact, and the frozen read scope from its authorization (§0.1.1);
4. ▲ **runs `run_l1` OUTSIDE the database transaction** — a cluster job inside a transaction is the
   crash window §9.1 exists to close, not a simplification;
5. **stores the exact output revision and its findings** — an identity, so publication can bind to
   it rather than to a name;
6. **stops before any publication** — `PUBLISH_SANDBOX` is a separate act (§3) with its own decision;
7. **marks `PASSED` / `REFUSED` / `FAILED`** — three outcomes, and ▲ **`REFUSED` is not `FAILED`**:
   one is the platform working correctly and the other is the platform broken;
8. **supports idempotent retry and cleanup** — a redelivered message re-attaches to the existing
   request instead of starting a second cluster job.

#### ▲ 9.0.1 The wedge this worker would otherwise ship — C1, and it is ALREADY LIVE elsewhere

**A lifecycle state only a live worker can leave, plus a uniqueness guard on the live states, is a
permanent wedge.** It exists today on the production lane, and §9.0's worker reproduces it unless
built against this section.

**Live, on `generation_request`:**

| # | Fact | Where |
|---|---|---|
| 1 | `generation_request_one_live_attempt` is UNIQUE on `(build_set_revision_id, environment_id)` `WHERE status IN ('REQUESTED','CLAIMED','RUNNING')` | live index |
| 2 | on redelivery the lane refuses anything not `REQUESTED` and not terminal — `fail_generation(permanent=False)`, *"another worker holds it"* | `generation_lane.py:416-422` |
| 3 | the queue reclaimer returns the **queue** row to `ready` and touches **nothing else** | `queue.py:659-666` |
| 4 | the only `FAILED` terminalization is **inside the lane** (`:442`) and needs a live worker to run | no sweep, no expiry, no operator path |

▲ **So a worker that dies between `CLAIMED` and terminal wedges that build set permanently**: the
message retries until `max_attempts` and the row never moves, so no new attempt can be created for
that set in that environment, ever. The comment at `:409` correctly argues that "another worker holds
it" must be retryable — **and the crash case is the one it cannot distinguish**. A held request and an
abandoned one look identical to that code.

▲ **`verification_request` has the same shape and less machinery**: `verification_request_one_live`
on `(sealed_artifact_id, environment_id) WHERE status IN ('REQUESTED','CLAIMED','RUNNING')`, and **no
lease, fence, attempts column or trigger**. Building §9.0's worker without a reconciler ships this
defect a second time, in the lane the whole programme depends on.

> **The rule, and it belongs in §15 beside the governance-function rule: EVERY LIFECYCLE TABLE HAS A
> RECONCILER.** The domain row and the queue row have separate lifetimes, and today only the queue
> row has an expiry. A reconciler moves an abandoned `CLAIMED`/`RUNNING` row out of the live set, so
> that a crash costs an attempt rather than the resource.

##### ▲ DO NOT WRITE A NEW ONE — PORT THE EXISTING ONE. This is worse than "missing"

**`src/featuregen/materialize/reconcile.py` already IS this reconciler**, it is **wired** into the
worker (`runtime/worker.py:582`), and it is careful in ways a fresh implementation would not be. It
reconciles **`materialization_request`** — the **LEGACY** chain — and contains **zero** references to
`generation_request`.

▲ **So the legacy lane has crash recovery and its replacement does not** — and §17 step 10 deletes
the legacy route. **Carried out naively, this programme ends with strictly LESS crash recovery than
the platform has today, on the lane everything runs through.** That is the opposite of the intent,
and it is invisible unless someone notices the reconciler was never ported.

▲ **And its prior art contains the trap that a naive port walks straight into.** The obvious
detection — *"request is `CLAIMED`/`RUNNING` and its queue row is not leased"* — is **WRONG**, and
`reconcile.py`'s own header says why: a request healthily awaiting redelivery after
`fail_generation(permanent=False)` is byte-for-byte indistinguishable from an abandoned one on that
signal. Terminalizing it does QUIET damage: *"the redelivery arrives, hits the terminal
short-circuit, and the lane reports `replayed` — 'already done' — for a compile that never
happened."*

**The correct predicate is the one that module already derived: an abandoned request necessarily has
an UNREACHABLE MESSAGE** (dead or absent), not merely an unleased one — plus its three-class analysis
(dead worker + dead message · dead message + live claim, which only TIME resolves · stranded at
`REQUESTED` behind a dead message, structurally invisible to a lease-based query).

**Port it, with its classes, its ranking and its verdicts, onto `generation_request` and
`verification_request`. Do not re-derive it** — the second derivation will omit the trap, because the
trap is only obvious once it has bitten.

▲ **DONE for `generation_request`, 2026-08-22** — `materialize/reconcile_generation.py`, wired into
the worker tick beside the legacy sweep and gated on the GENERATION switch (not the materialization
one: separate lanes, separate flags). It reuses `UNREACHABLE_MESSAGE_STATUSES` from the legacy module
rather than restating it, judges `REQUESTED` as well as `CLAIMED`/`RUNNING` so the
stranded-behind-a-dead-message class is not structurally invisible, writes `FAILED` and never
`REFUSED` (nothing was decided about the build set), and carries a separate gauge so watching the
number cannot be the reason rows get terminalized.

▲ **The trap test is the one to keep**: `test_a_RELEASED_MESSAGE_IS_NOT_ABANDONED` — **verified to
bite**. Injecting the naive predicate (treating a `ready` message as unreachable) terminalizes a
request that was healthily awaiting redelivery, which is precisely the quiet damage the legacy
module's header warns about.

▲ **STILL OWED: the same port for `verification_request`**, which cannot be written until §9.0's
worker exists — that table has no lease, fence or attempts column today (migration 1110).

#### ▲ 9.0.2 REUSE the right half of the claim pattern — C6

`claim_one`'s `partition_key NOT IN (SELECT ... WHERE status='leased')` is evaluated under the
statement snapshot and is **not** serialized by the `FOR UPDATE SKIP LOCKED` on the candidate row —
two claimers can both see no leased row for one partition. ▲ **The guarantee is
`queue_one_inflight_per_partition`, a PARTIAL UNIQUE INDEX**, and `except
psycopg.errors.UniqueViolation: return None` is what turns the race into a clean miss.

**A worker that copies the predicate and not the index inherits the race.** And
**`verification_request_one_live` ALREADY EXISTS** (migration 1094) — 1110 adds lease, fence, attempts
and the reconciler, and must **not** redefine it.

▲ **Decide in the plan, not in the patch:** whether the verification handler joins
`MATERIALIZATION_QUEUE_HANDLERS` — in which case the partition exclusion serializes a verification
against a compile sharing its partition key, which is probably wrong, they are different acts — or
takes its own poller and partition namespace.

**Migration 1110** adds the lease/fence/attempts columns to `verification_request`, the reconciler,
and the sandbox output revision identity. ▲ **`authorize_publication_v2` gets its caller in the same change, or it is
DELETED** — §15's standing rule, and it is currently the clearest live example of the failure that
rule names.

▲ **Only after this worker and sandbox publication genuinely work may the legacy execution handler be
removed** (§17 step 10, §8.3). The deletion surface in §1 D2 is unchanged; what changes is its
**entry condition**, which was "the canonical lane executes" — previously assumed, and false.

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

### ▲ 9.1 Both production acts need real STATE MACHINES — completeness gap 1

§9 above describes two endpoints, two attempt records and per-member certificates. ▲ **That is the
GATE, not the MACHINE.** A production act that writes customer values needs everything §9.0's sandbox
worker needs, and more — because its partial failures are visible to the bank:

| Requirement | Why |
|---|---|
| durable **claim / lease / fencing** | two workers must never both materialize one target |
| **idempotent retry** | a redelivered message re-attaches; it never writes twice |
| **exact target locking** | the target ref is locked *for the act*, not checked and released |
| **materialized-output revision identity** | so publication binds to a **thing**, not to a name |
| ▲ **composite FK from publication to the exact materialization output** | so `PUBLISH_PRODUCTION` cannot publish an output some other act produced |
| the **same** artifact / environment / target / certificate bindings across both acts | a certificate satisfied at materialization must be the one checked at publication |
| **partial-failure policy** for multi-member builds | all-must-pass at the gate is not the same as all-or-nothing at the write |
| **quarantine and cleanup** | a half-written namespace must not be left readable |
| **CAS publication** | compare-and-set, so a concurrent publish cannot interleave |
| **rollback / reconciliation** | and a way to answer *"what is actually out there right now"* |
| ▲ **crash recovery between external Spark work and the database commit** | the one window where the cluster has done work the database does not know about |

> ▲ **`PUBLISH_PRODUCTION` must NEVER accept a client-supplied materialized-output id.** §7 already
> rules that anything a caller can pass is something a caller can forge — and an output id is the
> single most valuable thing in this system to forge, because it is exactly the difference between
> publishing the values that were certified and publishing some others. **The publication request
> names the materialization ATTEMPT, and the server resolves its output.**

**Migrations 1111 and 1112** carry the two attempt state machines and the output revision identity.
The per-member certificate binding lands in 1112 and changes shape — see §10 and §10.3.

▲ **REVISION FIVE — the list above is a set of REQUIREMENTS, and step 7 cannot begin from it.**
Naming leases, fencing, CAS and reconciliation is not a state machine. Before implementation, step 7
must first WRITE:

* the **exact states and the legal transitions** between them, per act;
* the **external operation identity** — what names the Spark job, so a reconciler can ask about it;
* **fence propagation** into Spark, staging and the target, since a fence the external system does
  not carry cannot stop a zombie writer;
* **retry attachment rules** — which redeliveries re-attach to an existing attempt, and which are new;
* an explicit **UNKNOWN-OUTCOME state**, because *"the cluster did something and we do not know
  what"* is the state crash recovery actually finds, and a machine without it will guess;
* the **reconciliation algorithm** that resolves that state;
* **quarantine and cleanup ownership** — which component deletes a half-written namespace, and when;
* the **multi-member partial-write policy** — all-must-pass at the gate is not all-or-nothing at the
  write, and §9's all-members ruling does not by itself say what happens after member three of five.

▲ **Write production PUBLICATION first**, because it is the act whose partial failure is visible to
the bank.

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

▲ **WHERE THE LLM PAYLOAD ACTUALLY COMES FROM — verified 2026-08-23, and it is not where this
section implies.** The frozen author/critic configuration is persisted by exactly ONE writer,
`recipe_formula_shadow.py:1178`, which is the **shadow evaluation** lane. **The production authoring
lane freezes no configuration per run**, so a sealing-time derivation cannot read one.

**It does not need to.** `llm_dispatch` carries, per call: `provider`, `model`,
`provider_contract_hash`, `prompt_content_hash`, `schema_content_hash`, `authoring_run_id` and
`call_role` — **live: 877 of 890 rows carry a contract hash.** That is the same evidence
`derive_authoring_method` already reads, and it is strictly better than a frozen configuration for
this purpose: **it records what was ACTUALLY SENT, not what configuration happened to be current.**

▲ **This matters because the obvious alternative is the §10 backfill error moved to sealing time.**
Deriving the identity from `current_formula_generation_settings()` at seal would record *today's*
deployed configuration against a run authored under a different one — today's evidence, yesterday's
bytes. **Derive from the run's dispatches, by role.** The version axis comes from
`formula_authoring_run.versions` (`formula_schema`, `operation_grammar`, `disposition`, `critic`),
which is already stored per run.

▲ **One live consequence worth knowing before writing the migration:** of 890 dispatches only **3**
carry an `authoring_run_id` and a `call_role`, and all three are `formula.author` — **there are no
critic dispatches at all**. Consistent with all seven drafts being `FAILED`/`BLOCKED` (§11.1.2): the
runs never reached the critic. So a fixture for this table cannot be harvested from live data, and
`LLM_AUTHORED` identity requires both roles by §10's own payload.

▲ **Expectation generation is load-bearing, not decoration** — `has_reviewed_expectation()`
(`recipe_formula_expectations_v2.py:53`) unions two registries and **two of the three reviewed
recipes are Formula V1**, so "a reviewed expectation exists" does not by itself identify a
V3-producible method.

### ▲ 10.1 A DERIVED blueprint is not a REVIEWED blueprint — P0, folded from the child

**Verified, and this is a governance hole rather than a modelling nicety.** The child's D2 selects
`REVIEWED_RECIPE_BLUEPRINT` when *"a current executable V2 blueprint exists and binds"*, and its
§3.1 supplies `blueprint_derivable` and `blueprint_bindable` as the facts that answer it. Those two
facts do not know whether a human ever looked:

| | |
|---|---|
| `derive_blueprint_v2` (`recipe_formula_blueprint_derivation.py:152`) | **pure — definition only.** Its own docstring: *"Pure: definition only."* It derives a blueprint for **90** of the 317 recipes |
| `RECIPE_FORMULA_V2_EXPECTATIONS` (`recipe_formula_expectations_v2.py`) | **one entry**: `posted_debit_amount`. Its docstring: *"A2 derives a blueprint for 90 of the 317 recipes, but* which *of those 90 a human has actually reviewed is a governance answer no derivation can supply, and no `recipe_review_event` row exists yet"*. ▲ **That docstring's last clause is now FALSE — 996 approved rows exist (§21).** Its ARGUMENT is untouched and is the reason this section exists: none of them binds a formula expectation, so no derivation and no recipe approval can supply the governance answer. **Correct the docstring in step 2; do not let the correction be read as removing the rule** |
| `bypass_for` / `proposal_from_bound_expectation` (`deterministic_producer.py:138,173`) | **contain no review check.** `grep` for `has_reviewed`, `recipe_review_event` or `review_validity` across `deterministic_producer.py` and `replay_authoring_v2.py` returns **nothing** |

So a lane that routes on derivability would seal `REVIEWED_RECIPE_BLUEPRINT` — and, under §10, mint
a `method_identity_hash` naming a *blueprint revision and expectation hash* — for up to **90**
recipes **no reviewer approved**. That is a sealed method claim the evidence does not support, and
it is the one failure this whole programme cannot audit its way out of afterwards, because the seal
is append-only.

**The rule:**

> `REVIEWED_RECIPE_BLUEPRINT` requires **membership in the reviewed expectation registry**, at
> **expectation generation V2**, **and** a successful binding. Derivability is a fourth,
> independent fact and **grants nothing**.

A derived-but-unreviewed blueprint is a **proposal**. It may be shown, it may seed the child's
review queue, and it selects the **LLM** route with `BLUEPRINT_DERIVED_NOT_REVIEWED` (§5) recorded —
because on today's evidence one recipe qualifies for the deterministic lane, not ninety.

**Storage — 1099 is APPLIED and append-only, so extend, never alter.** The table carries the trigger
`sealed_artifact_member_provenance_no_change` (BEFORE UPDATE OR DELETE, raises). `ADD COLUMN` would
succeed and then be unfillable, because the backfill is an `UPDATE`. So:

```
sealed_artifact_member_method_identity      -- migration 1102, append-only, same guard as 1099
    artifact_id, member_name                -- FK → sealed_artifact_member_provenance
    method_identity_hash                    -- 64 chars
    method_identity_json                    -- inspectable, so the hash is checkable
    derived_at
    PRIMARY KEY (artifact_id, member_name)
```

Written by the same sealing act that writes 1099's rows, in the same transaction, with the same
all-members-or-none refusal.

**Certificate binding is PER MEMBER *and* PER KIND — P0-7:**

```
production_attempt_member_certificate       -- migration 1112
    attempt_id, member_name
    certificate_kind                        -- AUTHORING_METHOD | COMPILER_RUNTIME
    certificate_revision_id                 -- the EXACT revision
    method_identity_hash                    -- what it was matched against
    PRIMARY KEY (attempt_id, member_name, certificate_kind)
```

▲ **A mixed artifact needs SEVERAL certificates** — an LLM certificate for one column and a compiler
certificate for the next — and one certificate on the attempt could only ever be right about one of
them. This is the same shape argument migration 1099 already made for the method itself, applied one
level further.

▲ **And revision three then keyed the table `(attempt_id, member_name)`, which permits exactly ONE
certificate per member — so it made the same mistake one level further down.** An LLM-authored
production member requires **at least two independent proofs**, and neither can stand in for the
other:

| Kind | What it proves | Why the other cannot substitute |
|---|---|---|
| `AUTHORING_METHOD` | this model, under these prompts and this frozen contract, reliably produces the expected formula | says nothing about whether the accepted formula **executes** correctly |
| `COMPILER_RUNTIME` | accepted formulas compile, render and execute correctly on the certified engine and runtime | says nothing about whether the formula that was accepted was **the right one** |

▲ **A current sandbox verification is NOT a third certificate.** It is feature-specific evidence that
*this artifact* ran — required by §14, and genuinely valuable — but it is not a platform-wide claim
about a method. **Do not let it satisfy `COMPILER_RUNTIME`**; that substitution would let one
successful run of one feature certify the compiler for every feature.

**The required certificate SET is DERIVED from the sealed method, and the derivation is the gate:**

| Sealed method | Required kinds |
|---|---|
| `LLM_AUTHORED` | `AUTHORING_METHOD` **and** `COMPILER_RUNTIME` |
| `REVIEWED_RECIPE_BLUEPRINT` | `COMPILER_RUNTIME` — the deterministic producer's own contract stands in for the authoring half, because there is no provider to certify |

▲ **A member whose bound set is INCOMPLETE is refused by `METHOD_CERTIFICATE_MISSING`, naming the
missing KIND.** "A certificate exists" is not the question the gate asks; "is the required set
complete for this sealed method" is.

### ▲ 10.2 A `COMPILER_RUNTIME` certificate must name the whole execution stack — P0-7, second half

§10's `method_identity_hash` names the **authoring** inputs. A compiler/runtime certificate is about
**execution**, and revision three's identity omitted nearly all of it. The certified identity carries:

```
renderer version · Spark / Kedro / Python runtime versions · relevant engine configuration
timezone and ANSI-mode behaviour · decimal and rounding policy IMPLEMENTATION version
compiler version · canonicalization version · grammar version
certification dataset / corpus content hashes
```

### ▲ 10.3 There is NO certificate table, and the binding certifies the wrong subject — R3, R12

**Verified, and it invalidates the binding as specified.** §10 binds `certificate_revision_id`, *"the
EXACT revision"*. **No certificate table exists.** The certification schema is
`recipe_formula_eval_run` · `recipe_formula_eval_case(_v2)` · `recipe_formula_eval_attempt(_v2)` ·
`recipe_formula_evaluation_contract`, and nothing else. What plays the part of a certificate is a
**derivation**: `current_evaluation_validity` (`current_evaluation_validity.py:76`) computes the
current contract hash from the **deployed configuration**, picks the newest `eval_run` matching it
for a given `expectation_ref`, re-scores it through `evaluate_persisted_run_v2`, and returns a verdict
that is **never stored**.

**Three consequences, and each needs a decision this plan previously did not make:**

1. ▲ **`certificate_revision_id` has no parent to reference.** Migration **1112** must therefore
   create `method_certificate_revision` — append-only, carrying the certified subject, the contract
   hash, the corpus hashes, the outcome and `issued_at`. Migration 1114 planned a *compiler*
   certificate record while assuming the LLM-side one existed; it does not.
2. ▲ **A derived verdict can legitimately change between the two production acts**, because it is
   recomputed from live deployment settings. §9.1 requires the same bindings at both acts — so the
   attempt stores the **contract hash and the certificate revision**, and publication **compares**.
   It must never re-derive and proceed on the new answer (the §7.1 drift rule, one level down).
3. The plans are right that this reader cannot answer for an LLM feature (§9 piece 4): its query is
   keyed on `subject_ref = expectation_ref`, and a novel feature has none.

**And the subject is typed, because the two kinds certify different things:**

```
production_attempt_member_certificate       -- migration 1112
    attempt_id · member_name · certificate_kind
    certificate_revision_id                 -- FK -> method_certificate_revision
    subject_identity_kind                   -- AUTHORING_METHOD | EXECUTION_STACK
    subject_identity_hash                   -- what THIS kind was matched against
    PRIMARY KEY (attempt_id, member_name, certificate_kind)
    -- ▲ C5: the kind and its subject must AGREE, or an authoring certificate can carry an
    -- execution-stack subject and the typing buys nothing:
    CHECK ((certificate_kind = 'AUTHORING_METHOD') = (subject_identity_kind = 'AUTHORING_METHOD'))
```

▲ **And where the subject has a parent row, FK to it** — `AUTHORING_METHOD` →
`sealed_artifact_member_method_identity` (§10). A hash with no parent is an assertion; a hash with one
is a binding.

▲ **Revision four matched BOTH kinds against `method_identity_hash`, which is an AUTHORING identity —
so a `COMPILER_RUNTIME` certificate was being matched against a subject it does not describe.** The
constraint: `AUTHORING_METHOD` → the member's authoring method identity (§10); `EXECUTION_STACK` →
§10.2's stack identity. The exact-artifact verification is bound separately on the attempt, and is
not a certificate (§10's note).

### ▲ 10.4 `derive_authoring_method` can seal the strongest claim on the weakest evidence — R4

`authoring_provenance.py:85-105`:

```python
llm_evidence       = bool(author_refs) and bool(critic_refs) and reconciled
blueprint_evidence = bypass > 0
if llm_evidence and blueprint_evidence:  raise AuthoringMethodUndecidable(...)
```

▲ **The contradiction guard is gated on `reconciled`.** A run with provider dispatches that failed to
reconcile, plus a `REVIEW_BYPASSED` event, sets `llm_evidence = False`, so the guard never fires and
the run falls through to **`REVIEWED_RECIPE_BLUEPRINT`** — the strongest method claim, minted into an
**append-only** table, from a run whose provider calls could not be accounted for.

Unreachable today only because no production caller supplies `reviewed_blueprint`. ▲ **Step 4 is what
makes it reachable**, so the fix lands in step 2, before the lane exists:

```python
if (author_refs or critic_refs) and blueprint_evidence:  raise AuthoringMethodUndecidable(...)
```

**Raw dispatch presence, not the reconciled-gated boolean.** An unreconciled dispatch alongside a
bypass is the definition of a trace that disagrees with itself — which is what the exception is for.

▲ **Timezone and ANSI mode are on that list for a reason that would otherwise be discovered in
production.** The same IR, the same data and the same renderer produce **different window
boundaries** under a different session timezone, and **different overflow behaviour** under a
different ANSI setting. A certificate that does not name them certifies a run nobody can reproduce —
which is the one property a certificate exists to have.

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
   (**migration 1101**), plus the versioned constraint `build_set_member_formula_pinned_v1` — §11.0
   owns which branch of `NOT NULL` / `NOT VALID` applies, and the measurement that decides it.
   ▲ **These two columns are NECESSARY AND NOT SUFFICIENT** — on their own they pin *a* formula
   without proving it is *this member's* formula. **§11.0.1 is the rest of the fix**, and the columns
   land as part of the binding it defines.
2. **Both enter `content_hash`.** Pinning that does not change identity is not pinning.
3. `restore_formula_v3` selects **BY draft id**, and refuses a content-hash mismatch — it already has
   the vocabulary (`INTENT_HASH_MISMATCH`).
4. The §0.1 action-authorization revision covers the pinned pair, and the worker re-checks it.
5. `GENERATE_PREVIEW` requests carry the pinned pair per member (§7).

### ▲ 11.0 Legacy rows — RULED, and MEASURED

The owner's third-review ruling, which closes the judgement call this section used to carry:

> **Do NOT fabricate a formula choice by selecting the latest draft, and do NOT delete audit
> history.** Existing unpinned build sets stay **READABLE** but **UNBUILDABLE**, refused by name
> with `FORMULA_DRAFT_NOT_PINNED`. Every **newly created** build-set member must carry both
> `formula_draft_id` and `formula_content_hash`, enforced by a **versioned database constraint**.
> **If deployment-time measurement proves zero legacy rows, make the columns `NOT NULL`
> immediately.**

Both halves of the earlier text are refused: a `ORDER BY updated_at DESC` backfill fabricates a
choice nobody made, and deleting the rows destroys the record of builds that really happened.

**▲ THE MEASUREMENT EXISTS. Live kind cluster, 2026-08-22:**

```
build_set_revision   0        generation_request   0
build_set_member     0        sealed_artifact_v2   0
```

**So the immediate-`NOT NULL` branch applies TODAY.** Migration 1101 lands the two columns `NOT
NULL`, with no nullable phase and no backfill, because there is nothing to grandfather.

**The RULE still governs, because the measurement is a fact about a moment.** Re-run it in the same
transaction that applies 1101 — a build set created between this measurement and the deploy would
turn a clean `NOT NULL` into a failed migration on a live cluster:

```sql
SELECT (SELECT count(*) FROM build_set_revision) AS revisions,
       (SELECT count(*) FROM build_set_member)   AS members,
       (SELECT count(*) FROM generation_request) AS requests,
       (SELECT count(*) FROM sealed_artifact_v2) AS artifacts;
```

| Measurement at deploy | What 1101 does |
|---|---|
| **all zero** (today's answer) | `ADD COLUMN … text NOT NULL` for both, plus the named constraint below |
| **any row present** | `ADD COLUMN … text NULL` for both, plus the **same named constraint** added `NOT VALID` — which binds every INSERT and UPDATE while exempting rows already there — and the request gate refuses `NULL` with `FORMULA_DRAFT_NOT_PINNED` (§5). **No backfill. No deletion.** |

**The versioned constraint, in both branches:**

```sql
ALTER TABLE build_set_member
  ADD CONSTRAINT build_set_member_formula_pinned_v1
  CHECK (btrim(formula_draft_id) <> '' AND btrim(formula_content_hash) <> '')
  -- NOT VALID  ← only on the "rows present" branch
```

▲ **The version is in the NAME on purpose.** A later rule — say, pinning the admission identity too
— is `…_pinned_v2`, added beside this one, so "which pinning rule did this row satisfy" stays a
question with one answer per constraint rather than a redefinition nobody can date. `NOT NULL` and
the constraint are not redundant: `NOT NULL` refuses absence, the constraint refuses a blank string,
and the constraint is the thing that survives if a future migration ever re-widens the column.

▲ **`FORMULA_DRAFT_NOT_PINNED` (§5) is built even though today's branch makes it unreachable.** It
is the disposition the RULE needs in the other branch, and a code invented later — under deploy
pressure, on a cluster that turned out to have rows — is a code with no disposition row, which §5
shows is a 500 out of the sandbox publish path.

### ▲ 11.0.1 The pin must prove the formula BELONGS to the selection — P0-6

Adding `formula_draft_id` and `formula_content_hash` to `build_set_member` stops *latest-draft-wins*.
▲ **It does not stop the pin naming somebody else's formula:**

```
build-set member selects                      Feature A
pinned formula is a valid READY formula for   Feature B
formula id exists · content hash matches · the build proceeds
```

`feature_selection_revision` and `formula_draft` both carry the candidate facts, but **nothing forces
them to agree** — the two proposed columns are independent values on one row, and worker-side
validation is exactly the kind of check that is correct on the day it is written and silently
bypassed by the next caller.

**Make disagreement UNREPRESENTABLE in the database. Migration 1101:**

```
selection_formula_binding                   -- immutable, append-only
    binding_id                              -- content-addressed
    selection_revision_id                   -- FK -> feature_selection_revision
    formula_draft_id                        -- FK -> formula_draft
    formula_content_hash
    considered_revision_id   option_id   planning_request_hash
    recorded_at

    binding_plan_hash                       -- from the selection; see below
    -- COMPOSITE foreign keys to BOTH source identities, so the row can only exist when the
    -- draft and the selection ALREADY agree on EVERY fact the binding claims:
    FOREIGN KEY (selection_revision_id, considered_revision_id, option_id,
                 planning_request_hash, binding_plan_hash)
        REFERENCES feature_selection_revision (revision_id, considered_revision_id, option_id,
                                               planning_request_hash, binding_plan_hash)
    FOREIGN KEY (formula_draft_id, considered_revision_id, option_id,
                 planning_request_hash, formula_content_hash)
        REFERENCES formula_draft (formula_draft_id, considered_revision_id, option_id,
                                  planning_request_hash, formula_content_hash)
```

▲ **REVISION FIVE — revision four's version did not enforce what it claimed (R9).** Two omissions,
each of which left a hole the binding was created to close:

| Omitted | The hole it left |
|---|---|
| `formula_content_hash` from the **formula** key | the binding could store a content hash **disagreeing with the draft's** and both FKs would still pass — so the one value the build is pinned to was unchecked |
| `planning_request_hash` and `binding_plan_hash` from the **selection** key | the formula could be authored under a **different question** than the selection was made under, and the binding permitted it |

Both hashes are already immutable columns of `feature_selection_revision`
(`1072_selection_and_target_reading_revisions.sql:83`, verified), so including them costs nothing but
the index.

▲ **One Postgres subtlety that must not be missed: `formula_draft.formula_content_hash` is
NULLABLE** (it is filled when the draft reaches READY). Under the default `MATCH SIMPLE`, a composite
FK with **any** NULL column is **not enforced at all** — so a binding row with a null content hash
would silently skip the check it exists for. **Declare the binding's own `formula_content_hash NOT
NULL`**, which is correct independently: a binding may only be created for a draft that produced a
formula.

`build_set_member` then references **the binding**, never the two loose values:

```
build_set_member  +  selection_formula_binding_id  text NOT NULL
                     REFERENCES selection_formula_binding(binding_id)
```

* ▲ **The composite FKs need matching UNIQUE indexes on both parents** — `feature_selection_revision
  (revision_id, considered_revision_id, option_id)` and `formula_draft (formula_draft_id,
  considered_revision_id, option_id, planning_request_hash)`. Both are supersets of an existing
  primary key, so both are additive and neither can fail on existing data.
* **`feature_selection_revision` is measured at ZERO (§0.3)**, so this lands `NOT NULL` with no
  backfill — the same branch, and the same re-measure-in-the-transaction rule, as §11.0.
* **The BINDING enters `build_set_revision.content_hash`**, not the loose pair. Pinning that does not
  change identity is not pinning.
* `restore_formula_v3` resolves **through the binding** and still refuses a content-hash mismatch.
* A member with no binding is refused by `SELECTION_FORMULA_BINDING_MISSING` (§5).

▲ **Worker validation still runs.** It becomes defence in depth rather than the only defence — which
is the difference between a check and a constraint.

▲ **IMPLEMENTED 2026-08-23** — migration **1101**, `overlay/upload/selection_formula_binding.py`,
and `restore_formula_v3` resolving **through the pin**. `build_set_member` carries
`selection_formula_binding_id NOT NULL`, the build-set identity hashes **bindings rather than
selections**, and the API takes `selection_formula_binding_ids`.

▲ **THREE THINGS THE IMPLEMENTATION TAUGHT, none visible from the plan:**

1. **The composite FK is STRONGER than the runtime check it was paired with.** Because
   `formula_content_hash` is inside the key, a draft's contents **cannot move at all** while a
   binding references it — Postgres refuses the `UPDATE` outright. The pin-vs-draft comparison in
   `restore_formula` is therefore **defence in depth, not the guarantee** (restored dumps, disabled
   triggers, a future key change) — exactly the relationship the queue has between its partition
   predicate and `queue_one_inflight_per_partition`. **Say which half is load-bearing, or somebody
   simplifies away the wrong one.**
2. ▲ **`restore_formula` had a SECOND failure mode, and pinning fixes it silently.** It took the
   newest draft and *then* checked READY — so a newer FAILED draft **shadowed an older READY one**
   and the selection became unbuildable. §11 described only the "builds different code" failure.
   One test now pins each.
3. ▲ **A refusal MOVED EARLIER, which is a product improvement rather than a refactor.** "This
   candidate has no formula" used to surface when the worker tried to build; a pin cannot exist for
   an undrafted candidate, so it now surfaces when the build set is **declared**.

▲ **Fixture discipline matters more than usual here:** a READY draft must satisfy
`formula_draft_ready_carries_a_formula` (content hash **and** non-empty `formula_json`), so one
shared `bind_ready_formula` helper exists rather than a hand-rolled draft per suite.

### 11.1 The MONEY GUARD, its enforced retirements, and the constant nobody noticed — P0

Folded from the child's B1, **verified — and one step worse than the child reported.**

**The chain, as shipped:**

```
_authoring_config_hash()              formula_drafts.py:279      hashes {model, max_tokens, prompt_id}
   → formula_identity()               formula_draft_store.py:246 folds it with 5 other facts
   → formula_draft.formula_identity_hash
   → CREATE UNIQUE INDEX formula_draft_identity      1090_formula_draft.sql:101
```

Migration 1090 names that index **"THE MONEY GUARD"** in its own comment: *"One paid authoring run
per formula identity — a second request for the same candidate, snapshot and configuration finds
this row instead of buying the answer again."*

▲ **Retirement is enforced ONLY by identity collision.** `request_draft`
(`formula_draft_store.py:335-367`) inserts `ON CONFLICT (formula_identity_hash) DO NOTHING`, and
reads `formula_draft_retirement` **only when the INSERT loses the race**. Its own comment says so:
*"the identity is UNIQUE, so a new draft id alone does not defeat this conflict."* **If every insert
succeeds, no retirement is ever consulted.** A composition change is therefore not just a re-spend —
it is a silent, blanket un-retirement of every formula anyone has ever withdrawn.

**So the child's §3.3 — recompute `authoring_config_hash` as
`hash({formula_strategy, strategy_identity_hash})` — is refused.** Replacing the composition voids
the guard and every recorded retirement in one edit.

**Instead: FOLD, do not replace.**

```
authoring_config_hash = jcs_sha256({
    provider_contract_hash,     # NEW — the frozen author/critic contract (frozen_configuration.py)
    formula_strategy,           # from the child
    strategy_identity_hash,     # from the child
    ... the model/prompt facts, ACTUALLY READ (see the defect below)
})
```

▲ **That composition is identity V2, and it is not activated by writing it.** §11.1.1 owns how it
lands: legacy drafts keep identity V1 explicitly, retirement moves off the hash first, and no
existing draft is re-bought by the deploy.

▲ **Measured and mutable facts stay OUT.** The child's `blueprint_bindable` and
`semantic_inputs_ready` are *measurements of the world*, not descriptions of the build. A
measurement that flaps re-mints the identity and **buys the same answer twice** — which is the exact
expense the index exists to prevent. `formula_identity`'s own docstring already states this rule
about engine capabilities: *"folding them in would buy the same answer again every time an engine
gained an operator."*

#### ▲ The live defect: `_authoring_config_hash()` is a CONSTANT

Neither plan knew this, and it inverts B1's premise.

```python
settings = current_formula_generation_settings()   # returns a dict — audited.py:47
jcs_sha256({
    "model":      getattr(settings, "model", ""),        # dicts have no .model attribute
    "max_tokens": getattr(settings, "max_tokens", 0),    # → 0
    "prompt_id":  getattr(settings, "prompt_id", ""),    # → "" ; not even a key in the dict
})
```

`_generation_settings()` (`enrich_llm.py:1019`) returns a **plain dict**, and ▲ **its KEY SET DEPENDS
ON THE PROVIDER** — which matters to the fix, not just to the diagnosis:

| Deployment | What the dict actually contains |
|---|---|
| default / `FEATUREGEN_LLM_PROVIDER` unset (`fake`) | `{"provider": "fake", "model": "test"}` — **two keys. No `max_tokens`.** |
| `anthropic` | `{"provider", "model", "max_tokens", "thinking", "effort"}` |

`getattr` on a dict looks up an **attribute**, not a key, so all three defaults fire, every time —
and ▲ **naively "fixing" it to `settings["max_tokens"]` raises `KeyError` on the default deployment,
which is every test run and every unconfigured environment.** The corrected read is a typed
projection with explicit absence, never subscription. **Measured on this branch, under three
different provider/model configurations:**

```
fake/test            f5c34b84d694062755f4b88605f9fc8d67e2f4ac1699054f99f6ccd09bfdc3c8
anthropic/opus       f5c34b84d694062755f4b88605f9fc8d67e2f4ac1699054f99f6ccd09bfdc3c8
anthropic/haiku      f5c34b84d694062755f4b88605f9fc8d67e2f4ac1699054f99f6ccd09bfdc3c8
                     == jcs_sha256({"model": "", "max_tokens": 0, "prompt_id": ""})
```

**The money guard has been blind to model, token budget and prompt since it shipped.** Two drafts
authored under different models collide on identity and are treated as interchangeable — the precise
thing `_authoring_config_hash`'s docstring says it exists to notice: *"two drafts authored under
different model contracts are not interchangeable, and the identity has to notice."* It does not
notice. `prompt_id` is not a key of that dict at all, so even correcting `getattr` to subscription
does not produce one; the prompt identity has to come from the frozen provider contract instead.

#### ▲ 11.1.1 The ruling: NEITHER earlier option, because TWO CONCERNS WERE WRONGLY COUPLED

Revision three offered the owner a choice between a one-time re-spend and a hash-preserving
backfill. **The owner refused both**, and the reason is the sentence this whole section should have
opened with:

> **The MONEY guard means "do not pay twice for the same exact configuration."
> The RETIREMENT guard means "do not silently regenerate something deliberately withdrawn."
> They are two different rules, and today they ride on ONE mechanism.**

Retirement is enforced **only as a side-effect of identity collision**: `request_draft` inserts
`ON CONFLICT (formula_identity_hash) DO NOTHING` and reads `formula_draft_retirement` **only when
the INSERT loses** (`formula_draft_store.py:335-367`, verified above). So *any* correction to the
hash — however right — would have moved retirement behaviour with it. That is why "re-spend once" and
"preserve the constant" were both wrong answers: they are answers to the money question, and the
question that actually bites is the retirement one.

**Decouple them. Then fix each on its own terms.**

##### (1) RETIREMENT — the ORDER, the LOCK, and the SCOPE — P0-3 and P0-4

▲ **Verified enqueue order, and it is the whole bug:** `request_formula_draft`
(`formula_drafts.py:118-181`) calls `request_draft`, and enqueues the outbox message **only
`if created`** (`:172`). `created=True` is precisely the branch in which **no retirement row was
ever read.** A won INSERT is today's silent permission to spend.

So retirement moves off the identity hash and onto a key that survives every future composition
change — the **retirement scope key**, over the five identity fields that are not the authoring
configuration, all of which are already columns on `formula_draft` (migration 1090):

```
retirement_scope_key = jcs_sha256({
    considered_revision_id, option_id,
    planning_request_hash, catalog_snapshot_hash, definition_revision,
})                                  -- deliberately EXCLUDES authoring_config_hash
```

```
formula_draft_retirement_tombstone            -- migration 1103, append-only, same guard as 1096
    tombstone_id                text PRIMARY KEY
    scope                       text NOT NULL   -- EXACT_DRAFT | CANDIDATE_ACROSS_CONFIGURATIONS
    retirement_scope_key        text NOT NULL   -- the candidate scope, both kinds
    exact_formula_identity_hash text            -- NOT NULL iff scope = EXACT_DRAFT
    coverage_identity_hash      text NOT NULL   -- what this tombstone actually covers
    formula_draft_id            text NOT NULL REFERENCES formula_draft
    reason, detail, replacement_draft_id                            -- copied from 1096's row
    recorded_at
    UNIQUE (scope, coverage_identity_hash)
```

▲ **REVISION FIVE — revision four keyed this `retirement_scope_key PRIMARY KEY`, which CANNOT
represent the two scopes it then defines (R10).** Every exact draft of one candidate shares that
candidate's scope key, so a single-column primary key means:

* retiring exact configuration **A** consumes the key, and **B can never be retired**;
* a candidate-wide tombstone **cannot be recorded after** an exact one;
* an exception cannot say **which** tombstone it overrides, because there is at most one row to name.

**The coverage identity is what the tombstone covers** — the scope key alone for
`CANDIDATE_ACROSS_CONFIGURATIONS`, the exact formula identity for `EXACT_DRAFT` — so the unique
constraint stops a duplicate of the same act while permitting both kinds, and any number of exact
ones, to coexist. ▲ **And the exception references `tombstone_id`, not a scope key**, or "which
retirement did a person override" has no answer.

* **Every existing `formula_draft_retirement` row gets a tombstone, written and verified BEFORE
  identity V2 is activated** — the owner's *"compatibility aliases/tombstones for existing
  retirements"*. Same deployment unit, migration first (§15). ▲ It is computable today with no
  fabrication: all five inputs are stored columns. ▲ **And the live count is ZERO (§0.3), so the
  backfill is EMPTY** — the order still governs, the work is nil, and a future non-zero count must
  not find the order improvised.
* **`request_draft` consults tombstones FIRST**, before the INSERT and therefore before any
  enqueue, and refuses with `FORMULA_DRAFT_RETIRED` (§5).

###### ▲ The order revision three proposed CANNOT REACH ITS OWN EXCEPTION — P0-3

```
1. tombstone check            -> refuse                       <- ALWAYS returns first
2. exact V2 identity lookup   -> hit: return
3. legacy V1 draft?           -> hit: return preview, enqueue NOTHING   <- ALWAYS returns first
4. regeneration approval?     -> insert + enqueue                       <- UNREACHABLE
```

Step 4 exists to authorize regenerating a **legacy** draft. Step 3 returns unconditionally whenever a
legacy draft exists. ▲ **So step 4 can only ever be evaluated when there is no legacy draft to
regenerate** — the exact case it was written for is the one case that never reaches it. The same
defect hides `overrides_tombstone`: step 1 refuses before any exception is loaded, so that column
would ship with **no reachable code path**.

**The corrected order — LOAD the exception before DECIDING, and branch on it:**

```
1. LOCK the retirement scope key                        <- P0-4, below
2. LOAD the tombstone AND any exact exception for the REQUESTED V2 identity
3. tombstone present, no valid exception covering it    -> FORMULA_DRAFT_RETIRED, enqueue nothing
4. exact V2 identity hit                                -> return it, spend nothing
5. legacy V1 draft present:
       valid exact regeneration authorization           -> create V2 ONCE, consume the authorization
       no authorization                                 -> return the legacy draft as an auditable
                                                          preview marked LEGACY_CONFIG_UNPROVEN,
                                                          ONLY if its state is genuinely previewable
6. otherwise                                            -> INSERT + enqueue, once
```

▲ **Step 5's final clause is new, and the live measurement is what forced it.** Revision three
returned the legacy draft as a preview unconditionally. **All seven live drafts are `FAILED` or
`BLOCKED` (§0.3)** — so today that branch would hand back something that cannot be previewed and call
it an auditable preview artifact. A non-previewable legacy draft falls through to the authorization
question, not to a preview that does not exist.

###### ▲ The exception must BIND, not merely exist

Revision three keyed the approval `(retirement_scope_key, approved_at)` — *a scope and a timestamp*.
That authorizes **any** regeneration of that candidate, at **any** cost, under **any** configuration,
**for ever**. The exception binds all of this instead:

```
formula_draft_regeneration_exception          -- migration 1103, append-only
    exception_id
    tombstone_id                              -- ▲ THE EXACT tombstone it overrides (R10).
                                              -- NULLABLE: a legacy regeneration with no
                                              -- retirement is not an override of anything
    target_formula_identity_hash    NOT NULL  -- the EXACT V2 identity it authorizes creating
    provider_contract_hash          NOT NULL  -- under THIS contract
    strategy_identity_hash          NOT NULL  -- and THIS strategy
    llm_spend_authorization_id      NOT NULL  -- section 11.2: the money, BOUND rather than implied
    actor_subject                   NOT NULL
    overrides_tombstone             boolean NOT NULL  -- TRUE only when a person overrode a
                                                      -- retirement, so the exception is auditable
    max_uses                        smallint NOT NULL DEFAULT 1
    uses_consumed                   smallint NOT NULL DEFAULT 0
    expires_at                      NOT NULL
    approved_at
```

* ▲ **One-time consumption is enforced in the SAME TRANSACTION as the INSERT it authorizes** —
  `UPDATE ... SET uses_consumed = uses_consumed + 1 WHERE uses_consumed < max_uses` returning zero
  rows **is** the refusal. An exception that is checked and not consumed is a coupon that regenerates
  itself every time somebody clicks.
* **Expiry is not optional.** An approval granted inside a triage window must not still authorize a
  re-spend three months later, when nobody remembers granting it.

###### ▲ The LOCK — and why the existing one cannot serve — P0-4

"Check the tombstone before the INSERT" is not enough:

```
request checks: no tombstone
                                    operator retires the candidate and COMMITS
request inserts the draft and queues provider work
```

▲ **Verified: the existing draft lock cannot close this.** `_draft_locked`
(`formula_draft_store.py:227`) takes `pg_advisory_xact_lock(namespace, hashtext(formula_draft_id))`.
**A regeneration mints a NEW draft id**, so the two callers hash different keys and never contend.
*(Its surrounding comment already reasons correctly about why a `WHERE NOT EXISTS` predicate is
insufficient under snapshot isolation — the lock is well argued and keyed to the wrong noun.)*

**Both the request path and the retirement path take the SAME transaction-scoped lock on the
retirement scope key**, in a namespace distinct from the draft lock, **before either reads**.

▲ **IMPLEMENTED — and the lock must NOT COMMIT ON THE CALLER'S BEHALF, which is subtler than
`_draft_locked`'s version admits and cost a suite-wide leak to find.** `conn.transaction()` **nests
as a savepoint when a transaction is already open, and opens a real one — committing on exit — when
none is.** So wrapping unconditionally makes *"does this function commit your work?"* depend on
whether the caller happened to execute a statement first. Wrapping `request_draft`'s INSERT that way
committed drafts past the test teardown's rollback — and **the failure did not look like a leak**: it
surfaced as impossible state transitions in later tests (`ADMISSION → AUTHORING`), which reads like a
lifecycle bug rather than a transaction one.

**So the block is opened only for an AUTOCOMMIT connection** — the one case with no scope to borrow.
A transactional caller takes the lock inside its own transaction, where `pg_advisory_xact_lock`
already releases exactly when that caller commits or rolls back, which is the behaviour wanted.

▲ **The same hazard is latent in `_draft_locked`** (`formula_draft_store.py:227`), whose docstring
says it *"opens and commits its own"* for a connection with none. For `advance` that was deliberate —
the runbook's operator connection wants durability — so it is left alone, and recorded here because
the next person to copy that pattern into a path that INSERTS will reproduce the leak.

###### ▲ Retirement SCOPE is a governance decision, not a migration — P0-4, second half

Today *"retire this draft"* means an **exact draft identity**. Revision three's scope key silently
widened it to *"retire this candidate under every current and future model, prompt and formula
strategy"* — and buried the widening in a bullet. ▲ **That is a new governance power, and a migration
must never grant one.**

**Two explicit scopes:**

| Scope | Meaning | Authorization |
|---|---|---|
| `EXACT_DRAFT` | **the default**, and the meaning every historical row already carries | today's retirement permission |
| `CANDIDATE_ACROSS_CONFIGURATIONS` | the candidate under every configuration, present and future | ▲ **elevated, explicit, and recorded as the stronger act it is** |

* ▲ **Historical retirements are `EXACT_DRAFT`, and are NEVER reinterpreted as candidate-wide.** A
  tombstone backfill that widened them would retroactively withdraw formulas nobody withdrew.
* **The live count is ZERO (§0.3)**, so there is no backfill conflict today — *and the schema, the
  refusal path and the tests must still handle both scopes correctly*, because the first real
  retirement will not be a migration event and nobody will be watching for this then.
* The tombstone carries `scope`, and for `EXACT_DRAFT` the exact `formula_identity_hash` it covers. A
  request is refused when a tombstone covers **either** its exact identity **or** its candidate
  scope.

##### (2) MONEY — legacy identity V1 preserved, corrected identity V2 introduced

```
formula_draft_authoring_identity              -- migration 1103, append-only companion to 1090
    formula_draft_id        text PRIMARY KEY REFERENCES formula_draft
    identity_version        smallint NOT NULL CHECK (identity_version IN (1, 2))
    retirement_scope_key    text NOT NULL
    config_payload_json     jsonb NOT NULL     -- inspectable, so the hash is checkable
    config_hash             text NOT NULL      -- V1 rows: the constant, recorded as what it IS
    recorded_at
```

* **V1 is preserved explicitly, not re-derived.** Every existing draft is `identity_version = 1`
  with `config_hash = f5c34b84d694062755f4b88605f9fc8d67e2f4ac1699054f99f6ccd09bfdc3c8` —
  **verified on this branch by running it**, and the payload recorded as what it literally was:
  `{"model": "", "max_tokens": 0, "prompt_id": ""}`. ▲ **That is a record of the DEFECT, not a claim
  about the historical model configuration.** The owner ruled against pretending otherwise.
* ▲ **A backfill here is legal**, and this is worth verifying rather than assuming: `formula_draft`'s
  append-only trigger (`formula_draft_guard`, 1090:138) freezes a named ten-column tuple and permits
  UPDATE elsewhere — but the companion table means no UPDATE to `formula_draft` is needed at all.
  Extend, never alter, exactly as §10 does for 1099.
* **V2 is the corrected composition**, folded into `authoring_config_hash` — never replacing it:

  ```
  identity_version = 2
  config_payload = {
      provider_contract_hash,     # the FROZEN author/critic contract — where prompt identity
                                  # actually lives. `prompt_id` is not a key of the settings dict
                                  # at all, so no correction of the read can produce one
      formula_strategy,           # from the child
      strategy_identity_hash,     # from the child
  }
  ```
* ▲ **MEASURED facts stay OUT** — `blueprint_bindable`, `semantic_inputs_ready`. A measurement that
  flaps re-mints the identity and buys the same answer twice, which is the expense the index exists
  to prevent. `formula_identity`'s own docstring already rules this way about engine capabilities.
* **One commit, not two.** Fixing the read and folding the contract are ONE identity change.
* ▲ **REVISION FIVE — `model_controls` is DELETED from this payload (R15), and there is no "typed
  projection" to write.** `freeze_provider_contract` (`frozen_configuration.py:177-215`) already
  hashes into the contract: **the entire `generation_settings` dict** (whatever keys the provider
  supplies — so `max_tokens`, `thinking` and `effort` are covered without enumerating them),
  `prompt_id`, `prompt_version`, `instruction_sha256`, and the output schema's id, version and hash.
  It also **validates** that `provider` and `model` are non-empty strings, raising rather than
  hashing `""` — which is precisely the failure the constant exhibits.

  ▲ **So `provider_contract_hash` SUBSUMES the projection entirely, and carrying both is worse than
  carrying one**: two representations of the same facts in one identity, which diverge the first time
  a provider adds a settings key that the hand-written projection does not list. It also disposes of
  this section's own `prompt_id` problem — the contract already carries prompt identity, because
  `prompt_id` is an argument to the freeze.

  **What remains of the fix is smaller than the plan implied:** delete `_authoring_config_hash`'s
  `getattr` block and compose from `provider_contract_hash` plus the child's two facts.
* ▲ **CONSEQUENCE, and it is the general case rather than a one-off (R-M2):** once the contract hash
  is in the identity, **any** prompt, schema, model or provider-setting change moves every future
  identity. §11.1.1's legacy-preview-plus-explicit-regeneration rule is therefore **not a V1→V2
  migration**; it is the standing rule for **any superseded identity**. Key it off *"the current
  composition differs from this draft's"*, which V1 is merely the first instance of.

##### (3) What happens to the legacy drafts — preview yes, certification no

Because a V2 identity can never collide with a V1 row, a naive V2 deploy would win every INSERT and
**buy every existing answer again**. The owner ruled against that automatic re-spend.

▲ **The request order that used to sit here was UNREACHABLE at its own step 4, and it has moved** —
corrected, locked and scoped, to §11.1.1 (1) above. It is stated once, there, because a request order
written down twice is how the two copies come to differ. The regeneration approval it referenced is
now `formula_draft_regeneration_exception`, which **binds** the exact target identity, the provider
contract, the strategy, an explicit spend authorization (§11.2), an actor, an expiry and a one-time
consumption — rather than a scope key and a timestamp.

* **`LEGACY_CONFIG_UNPROVEN` drafts stay fully readable, previewable and sandbox-runnable** (§4's
  new row) — the owner's *"preserve legacy drafts as auditable preview artifacts"*.
* **They can never support production certification** (§4, §10): a certificate is matched against a
  method identity, and a constant that stands for "we did not record it" is not one.
* **Regeneration requires explicit user/operator approval AND cost confirmation.** Not a
  migration-time sweep, not a background job, not a "one-time" anything.

##### (4) Consequences for the sequence

1. **This is a §15 violation already on the branch** — a governance value with no enforcement, whose
   docstring asserts the enforcement. It is step 2 of §17, not the child's to fix in passing.
2. **Order within step 2 is load-bearing:** tombstones written and verified → V2 composition lands →
   only then may any V2 request be served. A window with V2 active and tombstones missing is a
   window in which every withdrawn formula is buyable.
3. **Nothing is re-spent by the deploy itself.** The bill this programme used to carry is now a
   sequence of individually approved acts, or no acts at all.
4. ▲ **MEASURED, 2026-08-22 — this item is CLOSED** (§0.3). `formula_draft` is **7** (4 `FAILED`,
   3 `BLOCKED`, 7 distinct identities) and `formula_draft_retirement` is **0**. So the tombstone
   backfill is **empty**, and — the part that changes a design rather than a estimate — **no live
   draft is in a previewable state**, which is why step 5 of the corrected order above will not
   return a preview that does not exist.

### ▲ 11.1.2 A FAILED draft is not an ANSWER — the money guard is currently a failure cache — R1

**The most consequential live finding of either review, and the corrected order in §11.1.1 rebuilds
it.** Five verified links:

| # | Fact | Where |
|---|---|---|
| 1 | `BLOCKED` and `FAILED` are terminal — their transition sets are literally `frozenset()` | `formula_draft_store.py:98-99` |
| 2 | `request_draft` returns an existing draft of **any state** as `(id, created=False)`. It checks retirement; it **never checks state** | `formula_draft_store.py:335-367` |
| 3 | the route enqueues **only** `if created` | `formula_drafts.py:172` |
| 4 | so a retry spends nothing, queues nothing, and returns the dead draft | — |
| 5 | the escape is to move an identity-bearing input — but `authoring_config_hash` is a **CONSTANT**, so **no configuration change can move the identity** | `formula_drafts.py:279` |

**The live cluster is the proof. All seven drafts are terminal — 4 `FAILED`, 3 `BLOCKED`, 0 `READY`.**
Two of them say it in their own `failure_reason`:

> *"the authoring run could not complete (run far-596a06f071d09c273af791c171e2fbe2); **this is a
> platform or provider fault, not a problem with the candidate**"*

▲ **The platform correctly identifies that the candidate is not at fault, writes a terminal state,
and thereby bars that candidate for ever** (`formula_draft_worker.py:277-281`). Another died on
`no resolved principal` — a wiring fault. Another is `REQUESTER_REVOKED`: permanently blocked for
everyone because the person who asked was later disabled.

▲ **§11.1.1's corrected order inherits this exactly** — *"4. exact V2 identity hit → return it, spend
nothing"* is state-blind, just like the code. **The money guard becomes a permanent failure cache the
first time a provider times out.**

**The rule: the guard asks "have we already BOUGHT this answer?" — and a technical failure bought
nothing.** So step 4 of the order splits by state:

| State on the identity hit | Answer |
|---|---|
| `READY` | return it, spend nothing. The guard working as designed |
| `BLOCKED` | return it. A business refusal **is** an answer about the candidate; re-buying cannot change it |
| `REQUESTED` / in flight | return it, queue nothing. Today's double-click answer, unchanged |
| ▲ `FAILED` / `CANCELLED` | **not an answer.** Permit a bounded re-attempt under an explicit spend authorization (§11.2), recorded as an attempt — so a poisoned identity is recoverable **without pretending the failure did not happen** |

▲ **"Exactly one" needs somewhere to count, and `formula_draft` has NO attempts column (C7).** Do not
add a mutable counter to an append-only table: **bind the re-attempt to the spend authorization's
`max_uses` / `uses_consumed`** (§11.2), which already enforces one-time consumption in the same
transaction as the work it authorizes. The bound then comes from the thing that also bounds the
money, which is the same question asked once.

▲ **IMPLEMENTED 2026-08-23, in part.** `request_draft` now raises `DraftNotAnAnswer` — a
`FormulaControlFlow`, so the worker's bounded poison guard cannot fold a considered refusal into
`TECHNICAL_FAILURE` — for `FAILED` and `CANCELLED`, instead of returning a dead draft as an existing
one. `BLOCKED` and `READY` are returned unchanged, because both ARE answers. **The bounded
re-attempt itself waits on 1105**: it binds to the spend authorization's `max_uses`, since
`formula_draft` has no attempts column and adding a mutable counter to an append-only table would be
the wrong fix (C7).

▲ **And `formula_draft_worker`'s retryable set is too narrow.** Only `LeaseFenceLost` and
`RecoveryRequiresReconciliation` are treated as transient (`:199`); the bare `except Exception` at
`:205` permanently terminalizes provider timeouts, billing exhaustion and connectivity faults alike.
**`technical_failure` must route to the retryable arm, not to `FAILED`** — the code already knows the
difference, and says so in the failure text it writes.

### ▲ 11.2 LLM spend authorization — a modal is not a money guard — P0-10

Both plans **require** an approved cost ceiling: §4 has a blocker row for its absence, §7 makes it
required evidence on `AUTHOR_FORMULA`, and the child's D5 and step 5a both quote it. ▲ **No migration,
table or durable contract owns it.** A confirmation modal in the browser is a UI event — it
authorizes nothing, survives nothing, and cannot be checked by a worker.

**Migration 1105 — `llm_spend_authorization_revision`, append-only:**

```
spend_authorization_id            -- content-addressed
action                            actor_subject
job_identity   member_identities  -- the EXACT job and members it covers
provider_contract_hash            -- the exact contract: a different contract is a different price
max_calls      max_tokens         -- BOTH, because either one alone is unbounded
currency       max_cost
expires_at
idempotency_identity              -- so a redelivered request cannot re-authorize
pricing_version                   -- ▲ the provider price list the ceiling was computed against
authorized_at
```

▲ **REVISION FIVE — revision four put MUTABLE COUNTERS on an APPEND-ONLY table, and required them to
co-commit with the dispatch audit. Both are wrong (R11).** The counters contradict the table's own
discipline, and the co-commit is **impossible**: `record_dispatch` deliberately opens its **own**
connection and commits independently (`dispatch_audit.py:133,155`), so that the audit survives a
caller rollback — *"the caller must then NOT dispatch to the provider"*. An outer worker transaction
cannot atomically update spend and audit the dispatch, because the audit is not in that transaction
by design.

**So: the authorization stays immutable, and consumption becomes append-only EVENTS.**

```
llm_spend_reservation      -- append-only. Written INSIDE the pre-dispatch transaction
    reservation_id · spend_authorization_id · dispatch_ref
    reserved_calls · reserved_tokens · reserved_cost      -- WORST CASE for this attempt
    reserved_at
llm_spend_settlement       -- append-only. Written after the provider responds
    reservation_id · actual_calls · actual_tokens · actual_cost · settled_at
```

| Rule | |
|---|---|
| **reserve WORST CASE, then settle actuals** | a reservation taken after the call cannot prevent an overspend it has already paid for |
| ▲ **reserve inside the SAME pre-dispatch transaction `AuditingClient` already opens** (`dispatch_audit.py:509`) | that connection is the **only** seam that sees every PHYSICAL attempt — `drive_structured_call` retries and repairs beneath it (`intake/llm.py:410`), so a reservation taken at the logical-call layer under-counts by exactly the retries |
| **row lock or CAS on the authorization** when summing reservations | two workers reading "remaining" concurrently otherwise both proceed and both are within budget |
| **each `llm_dispatch` binds its reservation** | so "what did this job actually spend" is a join, not an estimate |
| **no authorization → no outbox work is created** | the refusal happens before the queue, exactly as retirement does (§11.1.1) |
| **exhausted → the job STOPS** with `COST_AUTHORIZATION_EXHAUSTED` (§5) | it does **not** silently truncate the critic loop and present the result as final |
| ▲ **`pricing_version` is pinned** | a ceiling in currency is meaningless if the price list under it can move |
| ▲ **reservations EXPIRE and are SWEPT** (C3) | a crash between reserve and settle otherwise leaves worst-case cost reserved for ever, silently shrinking the budget until the authorization is exhausted by work that never happened. **The sweep reconciles against `llm_dispatch_outcome`** — it must not assume an unsettled reservation was unspent, which is the opposite error and buys the tokens twice |

▲ **That row is §9.0.1's rule applied to spend:** a reservation is a lifecycle state that only a live
worker can leave, so it needs a reconciler for exactly the reason `generation_request` does.

▲ **This is the durable half of the child's D5 promise** — *"selection never spends"* — and that
promise cannot be kept by a component a page refresh destroys.

### ▲ 11.3 "Try AI formula" — a server-authorized OVERRIDE, never a client-chosen method — P0-9

Two rules that are each right and, as written, contradictory:

| | |
|---|---|
| child D2 and step 4b item 5 | after a deterministic refusal, the user may **explicitly request an LLM retry** |
| parent §7 | *"The client must not supply readiness, blocker codes, **formula method**, certificate identity, or roles"* |

▲ **Both survive once the override is a durable, server-authored RECORD rather than a request field.**
The browser never sends `formula_strategy=LLM_AUTHORED`; it asks for an override, and the server
decides whether the deterministic refusal it names actually happened.

**Migration 1108 — `formula_method_override_revision`, append-only:**

```
override_id
selection_revision_id / formula_draft_id     -- what it applies to
original_refusal_code                        -- REVIEWED_BLUEPRINT_NOT_EXECUTABLE, and the server
                                             -- VERIFIES that refusal is recorded and current
requested_alternative                        -- LLM_AUTHORED
actor_subject    reason
llm_spend_authorization_id   NOT NULL        -- section 11.2
approved_at      expires_at
```

**The strategy resolver (child §3.1) consumes this revision as an INPUT FACT.** It remains the only
component that decides a method — the override changes the **evidence**, never the **authority**.

* ▲ **The server verifies the refusal.** An override naming a deterministic failure that did not
  happen is refused — otherwise "Try AI formula" is a client-chosen method with extra steps and a
  longer audit trail.
* **A new draft identity, always** (child D2). The override does not re-label an existing draft; it
  authorizes creating a different one.
* ▲ **Expiry, because a refusal AGES.** The blueprint may have been fixed in the meantime, and the
  correct answer then is the deterministic one — which an unexpiring override would quietly override.

---

## 12. The deterministic certification programme is only NAMED

Phase D ships a deterministic authoring lane. **Nothing certifies it.** Eight pieces, none of which
exist:

| # | Piece | What exists today |
|---|---|---|
| 1 | Evaluation-contract identity for the compiler programme | ▲ **verified unusable as-is**: `recipe_formula_evaluation_contract` (1097) requires `author_provider_contract_hash` and `critic_provider_contract_hash` **NOT NULL**, and a compiler run has neither. A sibling contract table, not that row with fabricated hashes |
| 2 | A reviewed case corpus for the compiler | the 12-case corpus is the LLM lane's — and ▲ a compiler case now needs **more** than it did (§12.1: an approved IR *and* reviewed test data) |
| 3 | The runner | nothing walks a compiler corpus |
| 4 | **Comparison rules** | ▲ **RULED by the owner, third review — §12.1.** No longer a blocker; now a build |
| 5 | The certification record | LLM-shaped only |
| 6 | Current/stale reader | `current_evaluation_validity.py:76` is expectation-specific |
| 7 | API + UI results | Phase E's page is scoped to one programme |
| 8 | Production certificate matching | §10 |

▲ **Until all eight exist, reviewed-blueprint formulas can PREVIEW and reach SANDBOX but can NEVER
satisfy production certification.** That is the honest ceiling of D1, and it is stated in §1 so it is
not discovered at Phase F. It is not a defect; it is the cost of D1 being in scope.

### ▲ 12.1 What "the compiler produced the right thing" MEANS — the owner's ruling

> **BOTH of these, every time:** the **normalized semantic IR** exactly matches the expert-approved
> IR; **AND** executing it against **reviewed test data** produces the expected rows and values.
> **Failure of either comparison fails the case.**

And an explicit anti-requirement, because it is the cheap test somebody will otherwise write:

> **Do NOT compare generated source bytes.** Harmless formatting or variable-name changes must not
> fail certification.

▲ **So no golden-file test of the rendered Kedro/PySpark project may ever become a certification
gate.** Rendered bytes are already identity-bearing elsewhere (the sealed artifact hashes them);
using them here would make a renderer whitespace change look like a compiler defect and would train
operators to re-approve fixtures to make red go green.

#### Comparison A — the normalized semantic IR

**The object already exists.** `FormulaExecutionIRV2.identity_payload()`
(`materialize/boundary_v2.py:317`) is the normalized form: it excludes `authoring_run_id` by explicit
design — *"the same governed artifact authored twice is one computation"* — and orders expressions
and row selections **by body path**, so tuple order is not a difference. `ir_hash_v2` (`:355`) is its
hash.

| | |
|---|---|
| **Compared** | the approved payload, stored in the case, against the payload the compiler produced — **structurally, field by field**, reporting the first differing path |
| **Summarized** | by `ir_hash_v2` equality. ▲ The hash is the summary and **not** the evidence: a hash mismatch tells an operator nothing about where, and "where" is the entire value of this comparison |
| **Not compared** | rendered source, file layout, node names, variable names |

▲ **One subtlety that will otherwise be discovered as a mystery failure.** `identity_payload`
contains `formula_content_hash` — the producer's own formula bytes. If the deterministic producer's
*serialization* changes without any semantic change, that field moves and the payloads differ while
every other field is identical. Report it as its own outcome, `IR_INPUT_IDENTITY_MOVED`, and **fail
the case** (the ruling admits no third verdict) — what the distinct outcome buys is the correct
remedy: **re-approve the fixture**, a governance act, rather than send somebody to debug a compiler
that is behaving.

#### Comparison B — executed values against reviewed test data

**This is the half that does not exist at all today, and it needs a cluster.** Verified: `run_l0`
(`compile/chain.py:272`) only *imports and constructs* the generated project in another interpreter —
it touches no data. The stage that runs against data is `run_l1` (`chain.py:279, :985`), reached
through G-2's `RunExecution` seam.

* ▲ **CORRECTED by ruling 1 — reuse the ADAPTERS and the AUTHORIZATION SEMANTICS, not the lane.**
  This bullet used to say *"reuse the canonical lane"*, and §9.0 shows that lane does not exist. A
  certification execution still carries its own `action_authorization_revision` (§0.1.1) and is still
  an operator-triggered act with a real actor rather than a daemon privilege — but it runs as a
  **dedicated certification job**. See §12.2.
* **Against a frozen certification dataset**, pinned by content hash, in its **own** namespace —
  never production data, and never a user's `sandbox_feature` output.
* ▲ **The reviewed test data is REVIEWED DATA, and it is governed like data.** Read scope and the
  data-use licence apply to it (§4, §13). A certification corpus assembled from real customer rows
  without those checks is an exfiltration path wearing a governance name.
* ▲ **A deployment with no execution seam CANNOT certify.** `l0=None` / `execution=None` is a
  posture, not a skip (`queue_lane.MaterializationLaneConfig`), so a run without L1 records
  **`UNMEASURED`** and produces no certificate. Absence of a result must never read as a pass.

**The comparison rules, for banking values:**

| Fact | Rule |
|---|---|
| grain keys, dates, window bounds, row counts | **EXACT.** Row count exact in **both** directions; grain keys compared as a multiset, so a duplicated grain row is a failure rather than an extra |
| null handling | **EXACT**, and `NULL` / `0` / *row absent* are **three different answers** — the zero-denominator and empty-window declarations are what say which one is right |
| currency and other decimal values | **EXACT after applying the DECLARED rounding policy** — `DecimalPolicy(precision, scale, rounding)` (`formula/schema_leaves.py:195`) applied to the expected value with `Decimal.quantize` under the matching mode, then compared exactly. The policy comes from the formula, never from the comparator's own choice |
| approximate operations | ▲ **NOT REPRESENTABLE TODAY, so the rule is: tolerances MUST BE EMPTY and every comparison is exact (R16).** `OperationRuleV1` (`operations_v2.py:31`) has **no approximate/exact field** — its axes are aggregation, operand, argument, additivity, result kind, order sensitivity and second operand — and Formula V2 states unsupported operations are never approximated (`schema_v2.py:12`). So *"declared approximate in the IR"* names a property the grammar cannot express, and a runner written to it would branch on a field that does not exist. **Introduce approximate semantics only in a future grammar/IR version**, which is when a per-case tolerance becomes meaningful. The `declared_tolerances_json` column stays, constrained empty, so the shape survives the change |
| either comparison fails | **the case fails.** No "IR matched, values close enough" |
| no execution result | **`UNMEASURED`** — not a pass, not a failure; it cannot certify |

▲ **Only two rounding modes can ever reach this comparator, and that is a renderer fact worth
knowing before writing a six-way mode table.** `_ROUNDING_CALLS` (`render/nodes_compute.py:334`)
implements `HALF_UP` → `F.round` and `HALF_EVEN` → `F.bround` **exactly**; the other four refuse at
render rather than approximate. A case declaring `DOWN`, `UP`, `FLOOR` or `CEILING` fails at
GENERATE_PREVIEW as an unsupported renderer operation (§4) — it is not a tolerance question.

#### Storage — a sibling programme, not nullable columns on the LLM one

**Verified constraints that decide this:** `recipe_formula_eval_case_v2` (1098) constrains
`subject_kind IN ('expectation_ref','gold_fixture')` and carries no dataset pin and no expected
rows; `recipe_formula_eval_attempt_v2` foreign-keys to it. So the compiler programme gets its own
append-only tables (▲ **migrations 1113 / 1114** — revision four's "1108 / 1109" here **collided**
with the method override and the code-generation job, R14; §17's table is authoritative):

```
recipe_compiler_evaluation_contract     compiler version · grammar version · producer version ·
                                        canonicalization version · corpus version + content hash ·
                                        expectation registry hash            -- NO provider hashes
recipe_compiler_eval_case               expectation_ref · blueprint revision + hash ·
                                        approved_ir_json + approved_ir_hash ·
                                        dataset_pin (content hash of the reviewed test data) ·
                                        expected_rows_json + expected_rows_hash ·
                                        declared_tolerances_json (usually empty)
recipe_compiler_eval_attempt            ir_comparison · value_comparison · first_difference_path ·
                                        outcome · UNMEASURED reason · zero provider dispatches
```

▲ **`zero provider dispatches` is an assertion, not a column that happens to be empty.** The whole
claim of this programme is that the deterministic lane spends nothing; a compiler attempt that
recorded a dispatch is a failed attempt regardless of its comparisons.

▲ **The supply cost of Ruling 2 is real and lands on the child.** A clean compiler case now needs an
approved IR **and** a reviewed dataset with expected rows — reviewed by the same roles, under the
same multi-person rule (§21). Child step 9 owns growing that; this section owns what the runner does
with it.

#### ▲ 12.2 One GOVERNED CASE REVISION, and a dedicated certification runner — ruling 1, gap 2

**Ruling 1 of the verdict, which supersedes revision three's "reuse the canonical lane" sentence:**

> *Reuse the lower-level execution adapters and the same `EXECUTE_SANDBOX` authorization semantics,
> but build a **dedicated certification execution job** using a certification dataset and an isolated
> namespace. **Never reuse a user's ordinary sandbox output as certification evidence.***

▲ **And the substrate revision three proposed to reuse DOES NOT EXIST (§9.0)** — so "reuse the
canonical lane" was a dependency on dead code, which is the same error in a second place. What is
genuinely reusable sits one layer down.

| | |
|---|---|
| **Reused** | the L1 execution adapters · `EXECUTE_SANDBOX` authorization semantics (§0.1.1) · §9.0's lease/fence pattern |
| **Dedicated** | the certification **job**, with its own lifecycle · the certification **dataset** · an **isolated namespace**, distinct from both `sandbox_feature` and production |
| **Forbidden** | ▲ **a user's ordinary sandbox output as certification evidence.** It ran under their data, their read scope and their moment — certifying from it makes the platform's certificate depend on what a user happened to run that afternoon |

**And a case is ONE governed revision, not separately approved parts.** A compiler evaluation case
carries a single immutable revision hash over:

```
approved semantic IR
+ frozen input dataset manifest / content hash
+ expected output rows / hash
+ tolerance declarations
+ runtime and execution profile      -- section 10.2's stack: renderer, runtimes, timezone,
                                     -- ANSI mode, decimal policy implementation
```

▲ **Reviewers approve THAT COMBINED REVISION**, and this is what preserves §21's arithmetic. If the
IR and the dataset are approved separately, a clean case needs **six** approvals rather than three
and the plan's cost estimate silently doubles. It also closes a real hole: **an approved IR paired
with a dataset nobody approved is a certification of arithmetic against unknown inputs.**

▲ **Synthetic datasets are the DEFAULT; approved masked extracts are the EXCEPTION** — and the
exception is a governance act carrying the same read-scope and data-use-licence checks as any other
governed data (§13). A certification corpus quietly assembled from production rows is an
exfiltration path wearing a governance name.

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

▲ **REVISION FIVE — revision four left this as "two acceptable answers, choose per surface", which
means the plan was not decision-complete (R19). It is decided here, per surface:**

| Surface | Ruling |
|---|---|
| **business-level formula** (`GET /formula-drafts/{id}`, and the formula shown in any workspace) | ▲ **(b) SERVE, with restricted physical references REDACTED.** Concept names, operators, windows, policies. No schema, table or column. This is what §4's *"formula visible"* has always meant |
| **generated physical code** (`GET /feature-execution/{id}/code`) and the artifact's read set | ▲ **(a) RECHECK CURRENT READ SCOPE for every referenced table and column, and refuse with `EXECUTION_AUTHORITY_UNMET`** when any is unauthorized. A generated project names schema, table and column; a general `feature:read` is not a read-set authorization |

**Why they differ rather than sharing one answer:** the business formula is a statement about a
computation, and the physical project is a map of the warehouse. Redaction is sufficient for the
first and meaningless for the second — you cannot redact a Kedro project into something still worth
serving.

▲ **The recheck is against CURRENT scope, not the scope frozen at generation.** A read set that was
authorized when the artifact was sealed may not be authorized today, and serving the code because it
was legal last month is the same error as re-deriving a decision instead of refusing on drift (§7.1).

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
| `_authoring_config_hash` asserts in its docstring that the identity notices a model change, and returns a constant | **still true — §11.1.** The enforcement point exists (migration 1090's unique index); the VALUE feeding it is inert |

### ▲ 15.1 The second standing rule: EVERY LIFECYCLE TABLE HAS A RECONCILER

Three instances of one defect were found across this codebase, and a fourth was about to be designed
in (§9.0.1):

| Table | The wedge |
|---|---|
| `formula_draft` | `FAILED`/`BLOCKED` are terminal and `request_draft` returns them for ever — **7 of 7 live drafts are dead** (§11.1.2) |
| `generation_request` | abandoned `CLAIMED`/`RUNNING` + `generation_request_one_live_attempt` = that build set is unbuildable for ever (§9.0.1) |
| `verification_request` | same shape, and it has **no lease or fence at all** — §9.0's worker ships it unless built against §9.0.1 |
| `llm_spend_reservation` | proposed in §11.2, and it had the same gap until C3 |

> ▲ **A lifecycle state that only a live worker can leave, combined with a uniqueness guard on the
> live states, is a permanent wedge with no operator remedy.**

**The rule: a table with a status column and a live-state uniqueness guard does not ship without the
thing that moves an abandoned row out of the live set.**

▲ **And the correction that makes this rule sharper than "nobody built one":** `materialize/
reconcile.py` **IS** such a reconciler — wired, thorough, and written against the **legacy**
`materialization_request`. **The machinery exists and was never carried onto the replacement lane.**
So the failure mode is not neglect; it is a capability that quietly does not migrate, on a programme
whose final step deletes the lane that has it. §9.0.1 owns the port.

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

## 17. The combined TEN-STEP sequence — this supersedes both plans' phase orders

The owner's order, for the parent and the child together. ▲ **Where a phase list in either document
disagrees with this, this wins.** Neither document's internal phase letters/numbers are an execution
order any more; they are names for bodies of work that these ten steps sequence.

▲ **REVISED by the principal-architect verdict.** Two things moved, and both were forced rather than
preferred: **the strategy contract is PROMOTED into step 2** (P0-2 — otherwise step 2 needs facts
step 4 creates), and **the sandbox worker is BUILT at step 6** (P0-1 — otherwise step 10 deletes the
only working execution path and replaces it with a promise). Step 0 is new and is not engineering.

| # | Step | Owner | Where it is specified |
|---|---|---|---|
| **0** | ▲ **Align the running image with the database, and smoke-test that baseline** — DB at 1099, image at 1093. **Not engineering; a prerequisite** | **OPERATOR** | §0.3 |
| **0b** | ▲ **Stand up the EXECUTION SUBSTRATE — R18. Kind cannot run the planned journeys today**: `FEATUREGEN_MATERIALIZE_ENABLED` is **`"0"`** and the eight-variable execution block is commented out, all-or-none (`deploy/kind/k8s/20-backend.yaml:148-238`; the worker manifest records the authorization limits at `25-worker.yaml:97`). Needs: an authoritative inventory · a Spark/Kedro runtime · persistent project and staging storage · a sandbox namespace · a submission/Thrift endpoint · a declared read-scope posture · **a smoke execution producing known rows** | **OPERATOR** | §9.0 · §12.2 |
| **1** | **Amend both plans; establish ONE authoritative, EXECUTABLE action matrix** — every cell a `Disposition` member | **SHARED** | this revision + the child's; matrix = §4, purity rule = §5.1 |
| **2** | ▲ **Foundations — enlarged.** One authorization model, replacing `generation_authorization` (§0.1.1) · LLM spend authorization (§11.2) · **relational** selection→formula binding (§11.0.1) · retirement: corrected order, scope-key lock, two explicit scopes, tombstones (§11.1.1) · identity **V1 preserved** · ▲ **the strategy contract and resolver, PROMOTED from step 4** · identity **V2 activated at the END of this step**, once the strategy facts it composes are persisted · per-member method identity (§10, §10.1) | **PARENT** *(child supplies §3.1's resolver)* | §0.1.1 · §10 · §10.1 · §11.0 · §11.0.1 · §11.1 · §11.1.1 · §11.2 · child §3.1, §3.3 |
| **3** | **Shared action-decision service, and DURABLE decision revisions** | **PARENT** | §7 · **§7.1** · §5 · §8 · §6 |
| **4** | **Recipe formula routing** — reviewed executable blueprint → deterministic authoring; otherwise → **explicit** LLM authoring; deterministic validation for BOTH · ▲ **plus the method-override revision** (§11.3) | **CHILD** *(contract from §4, §4.1, §10.1)* | child D1/D2/D3 · child steps 4a–4c · §11.3 |
| **5** | **Durable recipe-to-code coordinator and preview generation** | **CHILD** | child §3.5 · child steps 5a–5c |
| **6** | ▲ **BUILD the sandbox execution lane** — the durable verification worker (§9.0), then sandbox publication, as two SEPARATE actions. **This is construction, not re-gating** | **PARENT** | **§9.0** · §3 · §4's two sandbox columns · §5 |
| **7** | **Production materialization and publication — gates AND state machines** — lease/fencing, idempotent retry, output-revision identity, CAS publication, crash recovery · **multi-certificate bindings per member per kind** | **PARENT** | §9 · **§9.1** · §10 · **§10.2** |
| **8** | **Atomically move gold OUT of preview readiness INTO the production boundary** | **PARENT** | Phase C — **one commit with step 7** |
| **9** | **Both evaluation programmes**, and the **governed case revision** — one approval covering IR + dataset + expected rows + tolerances + runtime profile; a dedicated certification runner, dataset and namespace | **SHARED** | §12 · §12.1 · **§12.2** · Phase E · child step 9 |
| **10** | **Both-METHOD, concurrency, tamper, bypass and crash-recovery journey tests, then DELETE the legacy route, its producer and its handler** — ▲ **not "both-route"**: after D2 there is one route, and what is tested is route absence and queue-bypass refusal (§8.3) | **SHARED** | §19 · Phases F/G · child step 10 |

▲ **REVISION FIVE-b — §0.1.0's development policy RE-WEIGHTS this order.** Steps **7 and 8** build the
production boundary; the owner has ruled `MATERIALIZE_PRODUCTION` and `PUBLISH_PRODUCTION`
**UNAVAILABLE** until production governance exists. **They stay specified and they stop being
near-term**: the decision service answers "unavailable" for both from day one (which is stricter than
a certificate gate, and cheaper), and the boundary itself becomes **release-readiness work** (§21)
rather than a step the current programme is blocked behind.

**So the near-term path is 0 → 0b → 1 → 2 → 3 → 4 → 5 → 6**, ending at a working sandbox lane. Steps
7–10 remain the plan of record for going live. ▲ **This does not weaken anything**: an action that is
unavailable cannot be reached by a bypass either, and §8's closure work applies unchanged to the four
actions that ARE available.

▲ **Step 2 is now the largest step in the programme, and that is the accepted cost of breaking the
P0-2 cycle** — though §0.1.0 removes a real slice of it: no delegation, entitlement, revocation
tri-state or duties tables. The alternative the verdict offered — 2A, then the resolver, then a 2B that activates
identity V2 atomically — is recorded in the preamble as the reversal path. It trades one large step
for three coupled ones with a mid-programme activation, and the coupling was the defect.

▲ **Steps 6, 7 and 8 are the reason the child cannot own the decision service.** The child's original
Phase 5 proposed to build it — with four actions, no sandbox-publication split, and gold relocation
inside the same phase. That phase is **dissolved**: its decision-service half becomes parent step 3,
its gold-relocation half becomes parent step 8, and its certificate-reader half becomes parent
step 7. The child keeps none of it.

### What each existing phase becomes — nothing is orphaned

| Plan | Phase | → Step | Note |
|---|---|---|---|
| Parent | **A** — the sealing writer | — | ✅ **DONE at `364cd7fa`**, migration applied. Discharged before step 2 |
| Parent | **0** — server-derived authz + formula pinning | **2** | joined by §11.1's money-guard composition, which is the same identity change |
| Parent | **B** — six actions + one decision service | **3** and **6** | the six-action split IS step 6; the service IS step 3. Its "first output" measurement lands here |
| Parent | **C** — production boundary + gold relocation | **7** and **8** | **one commit**, as it already says |
| Parent | **D** — the deterministic reviewed-blueprint lane | **4** | the design moves to the child; the parent keeps the CONTRACT (§4, §10.1) |
| Parent | **E** — the operator journey | **9** | both programmes; the compiler one still needs §12's eight pieces |
| Parent | **F** — the journey tests | **10** | |
| Parent | **G** — cutover | **10** | after the run sheet and the three suites are migrated — ▲ **there is no equivalence gate**, because there is no second route to be equivalent to |
| Child | **0** — baseline + characterization measurements | **1** | its "finish or shelve the 1099 writer" item is **stale**: Phase A did it |
| Child | **1** — split recipe maturity from executable action | **3** | **SHARED**: the parent removes the ladder from authorization (§6); the child owns the capability projection, the wording and the client filters |
| Child | **2** — persist formula-strategy selection | **4** | |
| Child | **3** — wire reviewed deterministic authoring | **4** | now gated on §10.1: reviewed registry membership, not derivability |
| Child | **4** — harden the LLM fallback | **4** | |
| Child | **5** — "build the one action-decision service" | **3, 7, 8** | ▲ **DISSOLVED into the parent.** See above |
| Child | **6** — the durable coordinator | **5** | |
| Child | **7** — the frontend journey | **5** | |
| Child | **8** — reviewed-expectation growth | **9** | explicitly **not** a prerequisite for LLM preview |
| Child | **9** — end-to-end and adversarial tests | **10** | its 20 scenarios join §19's reintroduction table |
| Child | **10** — Kind deployment and cutover | **10** | ▲ its "thin adapter" item is **superseded by D2, confirmed explicitly in the owner's third review** — delete the route, its producer and its handler; **no adapter, not even temporarily**. Its test 15 (both-route decision equivalence) becomes §8.3's route-absence + direct-queue-bypass pair |

### ▲ Migration numbers — RESERVED across both plans

Live ledger is **195**; the highest file on this branch is **1099**, applied. Both plans independently
claimed 1100. Reserved:

▲ **RE-RESERVED.** The promotion of the strategy contract into step 2 and the four tables the verdict
requires (`selection_formula_binding`, `llm_spend_authorization_revision`, `action_decision_revision`,
`formula_method_override_revision`) change both the set and the order. **Numbers are write order and
still follow step order.**

| # | Owner | Table / change | Step |
|---|---|---|---|
| 1100 | **parent §0.1.0.1** | ▲ **DONE** — `action_authorization_revision`, **EXPAND-ONLY**: the table, its `(action, resource_identity_hash, authorization_id)` unique index for composite FKs, and the append-only guard. **Drops nothing, so an image-only rollback stays safe** | 2 |
| 1100b | **parent §0.1.2 / §0.3** | ▲ **the CONTRACT half, deferred**: typed per-action child tables · 1095's chain re-pointed · the one legacy row copied to `legacy_generation_authorization` **marked orphaned** · `generation_authorization` dropped **in a proven constraint order**. Runs once all six acts have callers on 1100 | 2 |
| 1101 | **parent §11.0 / §11.0.1** | ▲ **DONE** — `selection_formula_binding` with composite FKs into BOTH parents (including `formula_content_hash`, and the selection's `planning_request_hash` + `binding_plan_hash`), the parent unique indexes, the append-only guard, and `build_set_member.selection_formula_binding_id` **NOT NULL** under `build_set_member_formula_pinned_v1` — keyed on `(binding_id, selection_revision_id)` so the member's own selection cannot disagree with its pin | 2 |
| 1102 | **parent §10** | `sealed_artifact_member_method_identity` (append-only, same guard as 1099) | 2 |
| 1103 | **parent §11.1.1** | ▲ **DONE** — `formula_draft_authoring_identity` (composite FK to `formula_draft (formula_draft_id, authoring_config_hash)`, so a V2 companion cannot attach to a draft it does not describe) + `formula_draft_retirement_tombstone` keyed on `tombstone_id`, with `scope` and a `(scope, coverage_identity_hash)` unique key + `formula_draft_regeneration_exception` binding the exact target identity, provider contract, strategy, actor, expiry and one-time consumption. Plus `overlay/upload/retirement_scope.py` and the corrected order in `request_draft` | 2 |
| 1104 | **child §3.1, §3.3 / parent §0.1.4** | ▲ **DONE** — `formula_draft_authoring_plan`, **MOVED from step 4 to step 2** (P0-2), with database CHECKs that a reviewed plan names its blueprint at generation **v2** and carries **no** provider contract, and that an LLM plan names its contract and **cannot claim a reviewed blueprint**. Plus `overlay/upload/formula_strategy.py` — the PURE selector. ▲ **`authoring_subject_revision` is NOT a separate table (C4)**: its five fields are exactly `retirement_scope_key`, so `AUTHOR_FORMULA`'s `resource_identity_hash` IS that key — one tuple, three uses | **2** |
| 1105 | **parent §11.2** | ▲ **DONE** — `llm_spend_authorization_revision` (immutable, idempotent, with `pricing_version` pinned) + `llm_spend_reservation` and `llm_spend_settlement` as append-only events + **the CONTRACT half of 1103's expand**: `formula_draft_regeneration_exception.llm_spend_authorization_id` gains its FK and its `NOT NULL`, which is what makes "regeneration is an approved, cost-confirmed act" true rather than intended | 2 |
| 1106 | **parent §7.1 / §0.1.2** | ▲ `action_decision_revision` + ▲ **the NOT NULL `action_decision_revision_id` columns and composite FKs on every request/attempt table** (R6) | 3 |
| 1107 | **child §3.4** | `authoring_work_item` origin/strategy split — ▲ **empty (0 rows, §0.3), so the old constraint is replaced directly** | 4 |
| 1108 | **parent §11.3** | ▲ `formula_method_override_revision` | 4 |
| 1109 | **child §3.5 / parent §0.1.3** | `code_generation_job` + `_member` + `_event` + ▲ **`code_generation_job_action` — one authorization and decision PER ACTION** (R7) | 5 |
| 1110 | **parent §9.0** | ▲ `verification_request` lease/fence columns + the sandbox output revision identity | **6** |
| 1111 | **parent §9.1** | ▲ production **materialization** attempt state machine + `materialized_output_revision` | 7 |
| 1112 | **parent §9.1 / §10 / §10.3** | ▲ production **publication** attempt (composite FK to the exact materialization output) + ▲ **`method_certificate_revision` — the certificate parent that DOES NOT EXIST TODAY (R3)** + `production_attempt_member_certificate` keyed by `certificate_kind` with a **typed subject** (R12) | 7 |
| 1113 | **parent §12.1 / §12.2** | `recipe_compiler_evaluation_contract` + the **governed case revision** (IR + dataset pin + expected rows + tolerances + runtime profile, one hash) | 9 |
| 1114 | **parent §12.1** | `recipe_compiler_eval_attempt` + the compiler certificate record | 9 |
| 1115 | **run-spine spec §6/§13 (foundation)** | `feature_run_identity` (composite-FK chain, write-once) + `feature_run_profile` + `feature_run_state` + the three additive UNIQUE chain indexes on `contract_generation_input` / `contract_considered_revision` / `catalog_metadata_snapshot` | foundation |
| 1116 | **run-spine spec §9 (foundation)** | simple FKs: `formula_draft.considered_revision_id` and `feature_selection_revision.considered_revision_id` → `contract_considered_revision` (live-measured 0 orphans) | foundation |

▲ **Ordering note (run-spine foundation).** 1115/1116 may APPLY before 1104–1114 exist: the two
blocks are mutually independent (1115/1116's FKs reach only ≤1024 tables; nothing in 1100–1114
references a spine table), and `migrations.py` applies pending files deterministically by name with
a checksum ledger. This is a DOCUMENTED interleaving of two independent workstreams, not an
accidental out-of-order convention — the rule "numbers match apply order" holds within each
workstream.

▲ **Numbers are WRITE order, and they still follow step order** — which needs restating because this
table has now been renumbered twice. Revision three moved the production-attempt migration so that
everything step 2 needs sorted below everything steps 4–9 add; **revision four re-cut the range
again**, because promoting the strategy contract (1104) into step 2 and adding four verdict-required
tables (`selection_formula_binding`, `llm_spend_authorization_revision`, `action_decision_revision`,
`formula_method_override_revision`) changed both the membership and the order.

▲ **Every number from 1105 upward has MOVED since revision three.** Anything outside this table that
names one is stale by construction — check it against this table rather than against memory, and note
that the child's §3.3, §3.4 and §3.5 were re-pointed in the same pass.

▲ A reservation is not a commitment: **re-verify the ledger at the moment of writing each file**, and
apply §15's rule — *a governance table's writer and its migration are one deployment unit, migration
first*.

---

## 18. The phases

Ordered by dependency, not size. **Phase 0 is a prerequisite and Phase B is the spine**; the rest
attach to B, and doing it late means writing each gate twice. ▲ **Read §17 first** — these phase
letters are names for bodies of work, and §17 is the order they run in.

### Phase A — the sealing writer ✅ DONE at `364cd7fa`, migration applied
Per-member provenance is derived and written inside `seal_v2`, from the one place where
`RestoredFormulaV3` (which carries `selection_revision_id` and `formula_draft_id`) and
`AdmittedFeatureV2` (which carries `feature_name` and `proposal_content_hash`) meet. An undecidable
method refuses the seal by name — `MemberProvenanceRefused` → `AUTHORING_RUN_INCOMPLETE`, terminal,
not retried.

### Phase 0 — PREREQUISITE: server-derived authorization + formula pinning ▲ BEFORE the service
Neither can wait for cutover, because the decision service's worker-side check is meaningless without
them (§8.2).

▲ **This phase IS §17 step 2** — and after the verdict it is **bigger than four bullets**. Its
migrations are **1100 · 1101 · 1102 · 1103 · 1104 · 1105** and no others. ▲ **1104 is here because of
P0-2**: identity V2 composes `formula_strategy` and `strategy_identity_hash`, so the strategy
contract and its resolver are foundation, not step-4 work. **Identity V2 activates at the END of this
phase**, after the tombstones exist AND the strategy facts are persisted. Add to the four bullets
below: **§0.1.1** (one authorization model), **§11.0.1** (the relational binding), **§11.2** (spend
authorization), and the child's **§3.1 + §3.3** (the resolver and its plan table). Nothing in it waits
on an owner decision.

* **§0.1** — delete `roles` from `GenerationIn`; add `action_authorization_revision` (append-only,
  content-addressed, actor + permission result + read-scope result + policy version + evidence hash);
  `generation_request` FK, NOT NULL; worker loads and revalidates; `job.roles` becomes the frozen
  `read_scope_result`.
* **§11 / §11.0** — pin `formula_draft_id` + `formula_content_hash` per build-set member; both enter
  `content_hash`; `restore_formula_v3` selects by draft id and refuses hash mismatch. **Re-measure
  the four build tables inside the migration's transaction** and take the `NOT NULL` branch on zero
  (today's answer), the `NOT VALID` branch otherwise. **Never backfill, never delete.**
* **§11.1 / §11.1.1** — in this order, and the order is the design:
  1. **tombstones first** — one `formula_draft_retirement_tombstone` per existing retirement, keyed
     by retirement scope, written and verified **before** anything else in this bullet;
  2. **`request_draft` consults tombstones BEFORE the INSERT**, so retirement is checked before any
     provider work is enqueued rather than only when an INSERT loses;
  3. **one** identity change — fix `_authoring_config_hash`'s `getattr`-on-a-dict defect (as a typed
     projection, **not** subscription: the default deployment's dict has no `max_tokens` key) AND
     fold in `provider_contract_hash` + the child's `{formula_strategy, strategy_identity_hash}`,
     measured facts excluded;
  4. **record identity V1 explicitly** for every existing draft — the constant, and the payload that
     produced it, as a record of the defect;
  5. **no automatic re-spend**: a V2 request that finds only a legacy draft returns it as an
     auditable preview marked `LEGACY_CONFIG_UNPROVEN` and enqueues nothing.
* **§10 / §10.1** — `sealed_artifact_member_method_identity` (migration 1102), append-only under the
  same guard as 1099, written by the same sealing act in the same transaction, all-members-or-none.
  The identity payload carries **expectation generation**, and `REVIEWED_RECIPE_BLUEPRINT` requires
  **reviewed-registry membership**, never derivability. Existing 1099 rows stay
  `METHOD_IDENTITY_UNRECORDED`: no backfill, because a backfill would record today's evidence
  against yesterday's bytes.

**Proof:** a request that supplies elevated roles in its body gets exactly the read scope its
identity earns — asserted against a governed-`restricted` column. A build whose selection acquires a
newer draft between request and worker **refuses**, and its build-set identity is unchanged. Two
draft requests under different deployed models produce **different** identities — the assertion that
fails today. A request whose candidate carries a retirement tombstone **refuses without enqueuing an
outbox message** — asserted on the queue, not on the response. A V2 request against a candidate that
already has a V1 draft **spends nothing** and returns the legacy draft marked unprovable.

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
* ▲ **ONE canonical route, checked at BOTH moments** (§7, §8.2) — request time and worker time,
  through the same `evaluate_action`. **This bullet used to say "route both paths through it" and
  that contradicted D2.** There is one path; what is doubled is the moment, not the route.
* ▲ **First output: the VERSIONED FUNNEL fact 2 asks for.** Of the 295 potential LLM-fallback
  recipes, how many clear each stage — and it must be **recorded before any coverage claim is made
  anywhere**, including in a demo, a status note or a later section of this plan:

  ```
  funnel_version · registry_content_hash · code_revision · measured_at
  317 registry → 298 deterministic → 295 without a reviewed expectation
      → binding-complete → grain-resolved → currency/reversal/status resolved
      → renderer-dispatchable → PREVIEWABLE
  ```

  Each stage carries its own refusal-code histogram, so a small final number is diagnosable rather
  than merely disappointing. **The version stamp is what makes two runs comparable**; an unversioned
  count re-measured after a registry change is two numbers pretending to be a trend.

**Proof:** a table-driven test over §4's matrix, **all six columns**, asserted at **request time and
again at worker time** on the canonical route, plus the CI check that no `(code, action)` cell is
missing.

### Phase C — the production boundary + gold relocation ▲ ATOMIC, ONE COMMIT
> *"Do not merge 'gold removed from readiness' without simultaneously introducing the production
> publication gate; that would temporarily remove the protection instead of relocating it."*

* Remove gold from `recipe_readiness` — **keep `BLOCKER_GOLD_UNPROVEN` in the fold-owned set** so
  legacy rows strip it rather than re-entering it as a governed policy blocker (which would pin every
  legacy candidate at `FORMULA_BLOCKED`, strictly worse).
* Ship **everything in §9 and §10's PRODUCTION half in the same commit**: both evaluator actions,
  both endpoints, **both attempt STATE MACHINES (migrations 1111 and 1112, §9.1 — not merely attempt
  records)**, the production namespace, the method-level certificate reader, the per-member
  **per-kind** certificate bindings (§10), both worker re-checks.
  ▲ **The method-identity companion table and its sealing writer are NOT here — they are step 2**
  (§17, Phase 0, migration 1102). Earlier revisions listed them in both places. Sealing must begin
  recording method identity as early as possible: every artifact sealed before it exists is
  permanently `METHOD_IDENTITY_UNRECORDED` and must be regenerated, so deferring that table to
  Phase C manufactures more ineligible artifacts for no gain.
* Fold all artifact members **all-must-pass**, at **both** production acts.

### Phase D — the deterministic reviewed-blueprint lane (D1)
Resolve the method **server-side, from evidence, before authoring**: reviewed current executable
blueprint → `deterministic_producer` with **zero provider calls**; otherwise → explicit LLM authoring
with the cost shown first, then deterministic validation. Stop `formula_drafts.py:269` hard-coding
`llm_authored` and read the evidence instead. The resolver must return the **expectation generation**
too (§10), and must read the **reviewed registry**, never the derivation (§10.1).
**Detailed tasks live in the child plan** `2026-08-22-recipe-to-code-llm-fallback.md`, step 4.
▲ Ceiling: sandbox, until §12.

### Phase E — the operator journey (Governance → Formula quality)
UI page → `POST /formula-evaluations` → queue runner over the frozen cases → progress endpoint →
results report. Cost-confirmed before starting (calls, token budget, max cost); **the backend reads
the deployed configuration — the operator never types prompt hashes.** Outcomes:
`CERTIFIED_CURRENT · PASSED_NOT_CERTIFIABLE · FAILED_QUALITY · FAILED_TECHNICAL · STALE`.
**Two programmes now that D1 is in scope: LLM authoring and recipe compiler** — and the compiler one
needs §12's eight pieces first. ▲ **Piece #4 is no longer a pending ruling: §12.1 defines the two
comparisons**, so all eight are engineering. Piece #2 grew: a compiler case needs an approved IR
**and** reviewed test data, which is child step 9's supply problem and §21's cost.

**The corpus runner is the critical missing piece:** the backend can describe and score an
evaluation, but nothing walks the cases as a user-triggered job.

### Phase F — the journey tests ▲ BEFORE removing the old path
Both methods, end to end: hypothesis → recommendation → selection → formula → preview project →
inspect code → sandbox execution → sandbox publication → attempt production materialization → attempt
production publication. §19 is the decisive one.

Must also prove: certification pending does **not** block preview · it **does** block production
**materialization** · target leakage blocks preview · missing currency/reversal blocks preview · a
reviewed-blueprint member and an LLM-authored member build together in one artifact
(**mixed-METHOD**, which Phase D makes possible) and require **two different certificates** · stale
certificate refused · mismatched certificate refused · missing method identity refused · a
caller-supplied elevated role changes nothing · an unpinned formula draft refuses · a retirement
tombstone refuses **before** an outbox message exists · a legacy V1 draft previews and cannot reach
production · ▲ **the deleted route 404s and a direct queue submission executes nothing** (§8.3) —
this replaces the old "every API gives the same decision", which after D2 names a route that is not
there.

### Phase G — cutover
Make the build-set workflow canonical; **delete** the old materialization route, its producer
(`enqueue_materialization`) and its queue handler, once the run sheet and the three test modules are
migrated (D2, §8.3's surface table). ▲ **There is no equivalence gate to wait for** — the entry
condition is the migration of those callers, and the exit condition is the route-absence and
direct-queue-bypass tests passing. ▲ **The build-set authorization gaps are NOT here** — they moved
to Phase 0, because a cutover that carries them forward is a cutover onto an unauthorized lane.

---

## 19. The decisive journey test

Given a valid **"90-day incoming amount minus prior 90-day"** formula with complete grain and
bindings, resolved policies, a supported renderer, a pinned formula draft, and **NO current method
certificate**:

```
Request-time preview decision   = allowed + METHOD_CERTIFICATE_MISSING warning
Worker-time preview re-check    = allowed + IDENTICAL warning   ← same service, second moment
Legacy route                    = 404 — it does not exist       ← §8.3, not "the other answer"
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
| skip the worker's evaluation | second-check drift detection |
| ▲ **restore `POST /materialization-runs`** (or leave `enqueue_materialization` in place) | **route absence, and direct-queue submission executes nothing** — this replaces the old "skip the old path's evaluation" row, which assumed a second route still existed and was the plan's own contradiction of D2 |
| put certification on sandbox | sandbox execution allowed + warning |
| **gate production publication only** | **production MATERIALIZATION blocked** |
| remove the production certificate check | `METHOD_CERTIFICATE_MISSING` |
| **restore client-supplied `roles`** | **elevated body roles change nothing** |
| **unpin the formula draft** | build-set identity binds one formula |
| **bind ONE certificate per attempt instead of per member** | mixed-method artifact requires two |
| merge `EXECUTE_SANDBOX` and `PUBLISH_SANDBOX` decisions | unverified artifact executes but does not publish |
| **restore `getattr`-on-a-dict in `_authoring_config_hash`** (§11.1) | **two deployed models produce two draft identities** |
| **recompose `authoring_config_hash` without `provider_contract_hash`** (§11.1) | the identity does not notice a changed provider contract |
| ▲ **check retirement only when the INSERT loses** (§11.1.1) | **a withdrawn candidate refuses BEFORE an outbox message is written** |
| ▲ **activate identity V2 with the tombstones not yet written** (§11.1.1) | a retired formula is re-authored and re-billed |
| ▲ **auto-regenerate a legacy draft under V2** (§11.1.1) | **no spend without an approved, cost-confirmed regeneration** |
| ▲ **let a legacy `LEGACY_CONFIG_UNPROVEN` draft reach production** (§11.1.1) | production materialization blocked |
| ▲ **compare rendered source bytes instead of the IR** (§12.1) | a whitespace-only render change **passes** certification |
| ▲ **drop comparison B and certify on the IR alone** (§12.1) | a case whose executed values are wrong **fails** |
| ▲ **apply a global epsilon to currency comparisons** (§12.1) | an exact-arithmetic case fails when a cent moves |
| **route on `blueprint_derivable` instead of registry membership** (§10.1) | a derived, unreviewed blueprint seals `REVIEWED_RECIPE_BLUEPRINT` |
| **give a new blocker code a `CARRIED` disposition by default** (§5) | sandbox publication still allowed under a pending certificate |
| ▲ **restore the revision-three retirement order** (§11.1.1) | **a legacy draft WITH a valid regeneration exception is regenerated** — the assertion that proves step 4 is reachable at all |
| ▲ **key the retirement lock on `formula_draft_id`** (§11.1.1) | a retirement committing mid-request refuses the regeneration |
| ▲ **reinterpret a historical `EXACT_DRAFT` retirement as candidate-wide** (§11.1.1) | a differently-configured request for the same candidate still authors |
| ▲ **key `production_attempt_member_certificate` without `certificate_kind`** (§10) | **a mixed-method artifact requires TWO KINDS on one member**, not two rows on two members |
| ▲ **accept a client-supplied materialized-output id at `PUBLISH_PRODUCTION`** (§9.1) | publication resolves its output from the attempt, and a forged id publishes nothing |
| ▲ **pin `formula_draft_id` directly on `build_set_member`, skipping the binding** (§11.0.1) | **a valid READY formula belonging to ANOTHER selection is refused by the database**, not by a worker |
| ▲ **drop `action_decision_revision` and re-evaluate in the worker** (§7.1) | moved evidence produces `DECISION_DRIFT`, not a fresh "allowed" |
| ▲ **delete the legacy route before §9.0's worker exists** (§9.0) | **sandbox execution actually executes** — the assertion that fails silently today because nothing consumes `verification_attempt` |
| ▲ **let candidate origin block production for an `LLM_AUTHORED` seal** (§4.1) | a derivable-but-unreviewed member reaches production on a current LLM certificate |
| ▲ **check the spend ceiling once per job instead of before every call** (§11.2) | a repair loop exhausts its authorization and STOPS rather than silently truncating |

Verify the injection actually applied, and read pytest's summary line — `grep -c "^FAILED"` silently
matches nothing against coloured output and reports a false pass.

---

## ▲ 20.1 "Rollback is the previous image" is FALSE, and this cluster already disproved it — R13

Both plans say rollback is the previous image (§1 D2, child D9). ▲ **The deploy performed at the top
of this programme is the counter-example.** The database stood at 1099 while the image carried 1093,
and the older image **could not write `sealed_artifact_v2`** — migration 1095 had added
`generation_authorization_revision_id NOT NULL` with no default, and the 1093-era INSERT omits it.
Sealing raised a not-null violation on every attempt. **Rolling back to that image would not have
restored service; it is what broke it.**

▲ **It gets worse across 1100–1114**, which drop `generation_authorization`, add further NOT NULL
columns and delete routes. And the deployment shape makes the overlap real rather than theoretical:
migrations run in the **new pod's init container** (`deploy/kind/k8s/20-backend.yaml:312`) while the
**previous pod may still be serving** — old code against new schema, for the length of a rollout.

**So, and the product being pre-live is what makes this affordable:**

```
stop API intake  ->  stop workers  ->  back up and VERIFY the backup
  ->  apply migrations  ->  deploy backend and workers TOGETHER
  ->  smoke + journey tests  ->  reopen intake
```

▲ **"Stop API intake" has NO MECHANISM today (C8)** — there is no maintenance mode, drain or
read-only switch in the manifests. An unimplementable first step invalidates every step after it, so
name the mechanism: **scale the `backend` and `worker` deployments to zero replicas**, which is
available now and makes "intake stopped" observable rather than asserted. If a real maintenance mode
is wanted later it can replace the scaling; what it must not do is stay unstated.

> ▲ **Rollback means restoring the database snapshot AND the previous image together.** An image
> rollback alone is only safe once migrations are designed as backward-compatible expand/contract
> changes — which these are not, and which pre-live is the right moment not to pay for.

---

## 20. Risks

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
  Drive the real path; §19's reintroduction table is the check that the tests have teeth.
* **The 295 is a ceiling, not a forecast** (fact 2). If Phase B's measurement returns a small number,
  that is information about binding/grain/policy/renderer coverage, not a failure of this plan — but
  it must be reported rather than absorbed.
* **Migration ordering.** 1099 taught the rule and is now applied; §0.1, §10, §11 and §9 each add a
  migration whose writer must not ship ahead of it. ▲ **Both plans independently claimed 1100** —
  §17 reserves the range; re-verify the ledger at write time.
* ▲ **The money guard's danger is no longer a bill — it is the ORDER** (§11.1.1). After the ruling,
  nothing is automatically re-bought; what remains fatal is activating identity V2 before the
  retirement tombstones exist, which silently un-retires every withdrawn formula. Tombstones first,
  in the same deployment unit, migration first.
* ▲ **"Fix the hash" reads like a one-line change and moves two governance rules** (§11.1.1). Anyone
  who opens `_authoring_config_hash`, sees a `getattr` on a dict and corrects it in isolation has
  just changed retirement behaviour without touching retirement code. That is the specific edit this
  section exists to stop.
* ▲ **Comparison B needs a cluster, and a deployment without one must record `UNMEASURED`**
  (§12.1). The failure mode is not a red test; it is a certification run that quietly reports "no
  value differences" because it never executed anything.
* ▲ **"Reviewed" is the word most likely to drift back to "derivable"** (§10.1). Ninety derivations
  exist and one review does; a resolver written from the derivation module reads as correct, passes
  its own tests, and seals ninety unreviewed method claims into an append-only table.
* ▲ **The sandbox lane's absence is invisible from the route** (§9.0). `POST /verifications` returns
  202 and a docstring promising a worker. Nothing dead-letters, nothing errors, and the request looks
  serviced — the failure is that the row is never claimed. **Any plan step that says "sandbox already
  works" is reading the route, not the worker**, and step 10 was built on exactly that reading.
* ▲ **Step 2 is now large enough to be split by someone in a hurry** (§17). Splitting it re-creates
  the P0-2 cycle unless the split is precisely 2A / resolver / 2B with V2 activated atomically at the
  end. **An ad-hoc split that activates identity V2 before the strategy facts are persisted composes
  the identity from absent facts**, which is the same class of defect as the constant it replaces.
* ▲ **A measurement that closes an item can re-open it** (§0.3). Five migration branches are now
  decided by a live row count, and all five say zero today. Re-measure **inside the migration's
  transaction**; a build set, selection or draft created between the reading and the deploy turns a
  clean `NOT NULL` into a failed migration on a live cluster.

---

## 21. Not engineering, and not blocking these phases

### ▲ 21.0 The release-readiness list — deferred by §0.1.0, and owed before live

The owner's development policy defers real governance rather than deleting it. **Each of these must
be true before this tool goes live, and none of them blocks current development:**

| Owed | Deferred from |
|---|---|
| production-specific approval, and **segregation of duties** (approver ≠ executor) | §0.1.0, §0.1.1.1 |
| delegation records — issuer, scope, expiry — for work outliving its requester | §0.1.1 |
| per-environment and per-group entitlement checks | §0.1.1.1 |
| revocation as a tri-state, with `ACTION_AUTHORIZATION_UNVERIFIABLE` failing closed | §0.1.1 |
| `MATERIALIZE_PRODUCTION` and `PUBLISH_PRODUCTION` becoming available at all — the boundary, the state machines, the certificates | §9, §9.1, §10 |
| every `development-v1` authorization re-examined, since none is a governed approval | §0.1.0.1 |

▲ **`policy_version = 'development-v1'` is what makes this list actionable rather than aspirational.**
It is stamped on every authorization issued under the permissive policy, so the question *"what was
approved under the rules we no longer use?"* is one query. Without it, the deferral would be
indistinguishable from an omission on the day somebody has to answer for it.

### ▲ MEASURED, and the previous framing was wrong in BOTH directions — ruling 4

**Live kind, 2026-08-22 — `recipe_review_event`:**

| | |
|---|---:|
| rows, **all `approved`** | **996** |
| distinct recipes covered | **317** — every recipe in the registry |
| distinct reviewers | 6 |
| recipes reviewed by a **single identity** (a multi-person violation) | **0** |
| distinct roles per recipe | 2 roles → 11 recipes · **3 roles → 254** · 4 roles → 48 · 5 roles → 4 |
| ▲ rows carrying a **`formula_expectation_hash`** | ▲ **ZERO** |

▲ **So the honest statement is neither "no review events exist" nor "nine signatures are needed".**
Both plans quoted a docstring saying *"no `recipe_review_event` row exists yet"* — **that sentence is
now FALSE and both quotations are stale.** 996 approved rows exist, every recipe is covered, the role
requirement is already satisfied for the overwhelming majority, and the multi-person rule is already
being honoured everywhere.

> **The correct statement: there are no QUALIFYING FORMULA-EXPECTATION approvals.** Zero of the 996
> approvals bind a `formula_expectation_hash`. They approve **recipes**; certification needs
> approvals that bind a **formula expectation**, and later (§12.2) a **governed case revision**.

▲ **This changes the SHAPE of the remaining work, not merely its size.** The reviewer population is
established, the roles are covered and the multi-person rule is met — what is missing is the
artifact those reviewers have never been shown. **The bottleneck is producing formula expectations
and governed case revisions to review, not recruiting reviewers.** A plan that budgeted for
signatures was solving the wrong scarcity.

▲ **The per-case cost below still stands for each NEW clean case**, because a formula-expectation
approval is a new approval even where a recipe approval exists:

* **nine additional CLEAN CASES**, each with its reviewed fixture; and
* per case, approved `recipe_review_event` rows from **every** role
  `recipe_review_validity.required_reviewer_roles` names. For a `deterministic_formula` recipe that
  is at minimum **three** — `banking_sme`, `data_semantic_owner`, `formula_engineering` — and more
  when the recipe carries a `privacy_purpose:` ref (`privacy_compliance`), a `risk_corridor:` or
  `model_output:` ref (`treasury_regulatory_accounting`), or `near_label`/`outcome` leakage
  (`model_risk`);
* under the **multi-person rule** — `ReviewValidityV1.single_identity_violation` refuses one person
  signing a multi-role requirement.

**So: at least 27 formula-expectation approval events, plus nine reviewed fixtures — and none of the
996 existing rows counts toward them.** Until then no evaluation is *certifiable*, so production stays
blocked, which is the intended invariant rather than a defect.

▲ **Ruling 2 adds to this bill, and it should be said here rather than discovered at step 9.** A
compiler-programme clean case needs **an approved IR AND a reviewed dataset with expected rows**
(§12.1) — reviewed by the same roles, under the same multi-person rule, and the dataset itself is
governed data subject to read scope and the data-use licence. The nine cases are therefore nine
*fixtures plus nine datasets*, and the LLM programme's corpus does not supply them.

▲ Note the shape this gives the product once §4's rows are built: with certification uncertifiable
and 295 recipes lacking a reviewed expectation, **the LLM route with deterministic validation is the
only route to preview for almost every recipe**, and production is closed for all of them. That is
the honest state, and it is exactly why preview must not be gated on the thing that cannot yet
happen.

---

## 22. What this plan already gets right — confirmed by the owner, do not regress

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
| **Defect-injection** journey tests are required, not optional | §19 |
| There is **ONE action matrix** in the repo, and one decision service | preamble, §4, §7 |
| A **derived** blueprint is not a **reviewed** one | §10.1 |
| Identity composition **folds**, never **replaces** — and measured facts stay out | §11.1 |
| A new blocker code lands with its disposition **and** the `== 16` literal, same commit | §5 |
| **Money and retirement are two rules**, and retirement is checked before provider work | §11.1.1 |
| Legacy drafts are **preserved as auditable previews**, never re-bought automatically | §11.1.1 |
| Legacy build sets are **readable and unbuildable** — never latest-draft-pinned, never deleted | §11.0 |
| Certification compares **IR and executed values**, never generated source bytes | §12.1 |
| Production materialization/publication stay separate **by product ruling**, not by unproven atomicity | §3 |
| **ONE canonical route**; no adapter; both **methods**, not both routes | §1 D2, §7, §8.3 |
| ▲ **Production eligibility follows the SEALED method, never candidate origin** | §4.1 |
| ▲ **One authorization model** — two records that can disagree is the same defect one layer down | §0.1.1 |
| ▲ **The formula pin is RELATIONAL** — a pin that does not prove belonging is not a pin | §11.0.1 |
| ▲ **Two certificate KINDS per production member** — authoring and compiler/runtime prove different things | §10, §10.2 |
| ▲ **Retirement: load the exception before deciding, lock the scope key, and never widen scope by migration** | §11.1.1 |
| ▲ **Spend is a durable authorization, checked before every call** — a modal authorizes nothing | §11.2 |
| ▲ **The decision is a durable record; drift REFUSES rather than re-decides** | §7.1 |
| ▲ **The sandbox lane is BUILT before the legacy route is deleted** | §9.0, §17 step 6 |
| ▲ **A disposition cell contains a disposition** | §5.1 |
| ▲ **A failed draft is not a bought answer** — the money guard must not become a failure cache | §11.1.2 |
| ▲ **An authorization is PRESENTED, not referenced** — the caller must be its grantee | §0.1.1.1 |
| ▲ **Authorization and decision bind action + resource RELATIONALLY**, at least as strongly as 1095 | §0.1.2 |
| ▲ **One authorization per ACTION, not per job** | §0.1.3 |
| ▲ **`AUTHOR_FORMULA`'s subject is a candidate, so inspection never requires a selection** | §0.1.4 |
| ▲ **`ask` records nothing; only `decide` writes** | §7.1 |
| ▲ **A certificate's subject is TYPED** — authoring identity and execution stack are different claims | §10.3 |
| ▲ **Spend RESERVES worst case and settles actuals**, inside the pre-dispatch transaction | §11.2 |
| ▲ **Rollback is image + database together**, never the image alone | §20.1 |
