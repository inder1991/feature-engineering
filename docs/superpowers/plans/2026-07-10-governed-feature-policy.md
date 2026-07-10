# Governed Feature Policy — Initiative Design & Phased Plan

> **Status:** Design + phased roadmap. This is a **separate initiative**, split out of "Phase 2 of intent-aware recipe selection" because a layer that can **block a recipe or authorize an exception is a governed decision system**, not a filter — it carries lifecycle state machines, an authority model, versioned evaluation, and an append-only audit that the recipe-selection sub-phases do not.
> **Requires a compliance partner** and its own detailed (task-by-task, TDD) planning pass per phase before implementation. This document captures the design and the non-negotiable invariants so none are lost.
> **Related:** `2026-07-10-phase2-ranking-dimensions.md` (which already leaves the pipeline slot `safety → policy → rank`).

## What it does

After universal safety, a recipe can be **structurally safe yet inappropriate** for a governed context (a geography proxy is fine in fraud, fair-lending-risky in US underwriting). This layer evaluates locally-ratified policy against each eligible candidate + the decision context and can **warn / require-justification / require-approval / exclude** — additive-only over universal safety, and shadow-measured before it ever enforces.

## Non-negotiable invariants (fold into every task)

**Placement & neutrality**
1. Pipeline: `applicability → grounding → universal safety → contextual policy → rank(eligible only)`. Policy NEVER rescues a `SAFETY_REJECTED` recipe (additive-only). A `POLICY_BLOCKED`/`POLICY_REVIEW_REQUIRED` recipe gets no canonical rank.
2. Every flag defaults off → byte-identical to pre-policy. **Shadow before enforce** (measure vs compliance-reviewed expectations before changing any disposition) — the same discipline as recognition's shadow-before-filter.

**Context**
3. `PolicyContextStatus ∈ {COMPLETE, NOT_REQUIRED, INCOMPLETE, CONFLICTING}`. **Incomplete/missing context NEVER means unrestricted.** The review trigger is a **policy-relevant bound concept**, not the presence of context.
4. `NOT_REQUIRED` must come from a **trusted governed source** (project governance profile / authorized admin / explicitly non-governed mode) — NEVER inferred from missing fields. Missing fields → `INCOMPLETE`.
5. **Required-context fields are rule/bundle-driven**, not a caller argument: the pinned bundle declares its minimum required context; a rule may add fields.

**Sensitivity**
6. A closed **sensitivity-control registry**: `SensitivityControlClass ∈ {UNIVERSAL_BLOCK, CONTEXT_REVIEW, INFORMATIONAL}`. `protected_attribute`/`special_category` are `UNIVERSAL_BLOCK` (already refused by safety — they never reach policy). Only `CONTEXT_REVIEW` concepts (e.g. `proxy_candidate`, `high_risk_geography`) trigger incomplete-context review.

**Predicate DSL**
7. A **closed DSL** only: `{all|any} of leaf {field, operator, value}`; enumerated fields + operators (`eq, in, contains, intersects, exists, not_exists`). **`not` / general negation is deferred** (unbounded "all except" is unsafe). No executable/model-generated logic.
8. **Typed validation** (a field→type schema + operator-compatibility table) — a wrong value type is rejected. Predicates are **canonicalized** (sorted keys, sorted unordered sets, UTF-8, schema version) before hashing/storage.

**Lifecycle (event-sourced, immutable)**
9. Immutable `policy_rule_version` + append-only `policy_rule_event`. Active-state is **derived** from events; no mutable status column.
10. A **validated state machine**: `DRAFTED→SUBMITTED→RATIFIED→ACTIVE↔SUSPENDED→RETIRED` (RETIRED terminal). The event writer rejects invalid transitions transactionally with an `expected_current_state` (optimistic concurrency).
11. **Ratification and activation are separate events** (ratified = approved; activated = enabled for an environment/effective period).
12. **A bundle version freezes its exact contents** via a `policy_bundle_rule_version` join of immutable rule-versions ↔ immutable bundle-version. Never rely on `rule_version.bundle_id` (that lets composition drift). Bundles have their own lifecycle events. A generation run **pins exactly one `policy_bundle_version_id`**; `active_rules` never infers "latest".
13. **`review_due_at` ≠ `effective_until`.** Past `review_due_at` → keep enforcing + emit a governance alert. Only `effective_until` expires a rule.

**Evaluation & auditability**
14. `PolicyDecision` keeps **ALL matched rules**; `effective_action` = strongest (`exclude > require_approval > require_justification > warn > allow`). **Every effective action is attributable to one or more persisted matched rules — including a versioned, pinned system baseline rule** (e.g. `system:policy-context-incomplete:v1`) for the synthesized incomplete-context action. `matched_rules` is never empty for a non-`allow` action.
15. **Versions pinned before evaluation** (bundle + `policy_evaluator_version` + `predicate_dsl_version` + `system_baseline_policy_version`), persisted on the decision. **Canonical hashing** (SHA-256 over versioned canonical JSON) for `policy_context_hash` / `binding_fingerprint` / `concept_set_hash` / `decision_fingerprint`.

**Justify vs approve**
16. **Justification ≠ approval.** A justification (author-supplied) satisfies `require_justification` per a **rule-declared closed justification schema** (required structured fields; records satisfaction, never erases the original action). An **approval** (for `require_approval`) requires an actor with the **rule-declared authority** (`required_approval_authorities` + `approval_policy{mode: any|all, min_approvers, distinct_authorities, self_approval: prohibited}`), integrated with RBAC via `ActorAuthorityGrant`.
17. Approvals are **append-only** with a decision lifecycle (`requested/approved/rejected` now; design admits `revoked/expired`) and **bound to a decision fingerprint** (recipe + binding-plan hash + rule-version + context hash + snapshot). A stale-fingerprint approval is rejected; pending requests go stale when the pinned bundle / rule state / binding / context / snapshot changes.
18. **Approval produces a new append-only projection + a rerank, never an in-place patch.** Explicit ID hierarchy: `GenerationRun → ConsideredSetSnapshot → PolicyEvaluationRevision → PresentationProjection`. Grounding + safety are NOT rerun if the fingerprint holds; only policy + ranking reproject.

**Operational**
19. WORM tables: no ordinary update/delete; the **documented correction path** is append-only (suspend/retire events, superseding rule/bundle versions, reject/revoke approval events).
20. **Migration numbers are placeholders — resolve against repo head at execution time.**

## Phased delivery (each phase = its own TDD planning pass)

- **PP-1 — Vocabulary & DSL:** the closed predicate DSL (typed validation + canonicalization + deferred negation), `PolicyContext` + `PolicyContextStatus` (+ trusted `NOT_REQUIRED`), the `SensitivityControlClass` registry. *Exit:* DSL rejects unknown field/operator/type + empty `all`/`any` + oversize/deep predicates; canonicalization deterministic; malicious strings stay plain data.
- **PP-2 — Event-sourced model:** immutable `policy_rule_version` + `policy_rule_event` (state machine + optimistic concurrency), separate ratify/activate, `policy_bundle_version` + `policy_bundle_event` + the `policy_bundle_rule_version` freeze-join, `review_due_at`/`effective_until`, WORM grants + correction path. Ship rules as **inactive drafts** (a ratified rule exists only as a test fixture). *Exit:* activation-before-ratification rejected, retirement terminal, suspension blocks evaluation, bundle freezes exact rule-versions, overdue-review enforces + alerts.
- **PP-3 — Evaluation:** `evaluate_policy` (all matched rules, strongest action, context asymmetry via the pinned system baseline rule, fingerprints + evaluator/DSL/baseline versions). *Exit:* multi-rule precedence, conflicting-context, `NOT_REQUIRED` from a trusted source, universal-safety skips policy, evaluator-version replay.
- **PP-4 — Shadow:** evaluate + **persist** decisions (append-only, versioned, fingerprinted) with **no disposition change**; a **privileged** review API/report (`GET /admin/policy/shadow-evaluation/{run}`) so compliance inspects matched rules / effective vs expected / mismatch class; a compliance expectation set. *Exit (the 2C→enforce gate):* zero false-allow on designated critical cases; 100% incomplete-context handling for sensitive candidates; 100% matched-rule preservation + fingerprint determinism; false-review/false-block below agreed thresholds; **recorded compliance sign-off** on the proof bundle + predicate rendering; coverage: min examples per action, conflicting-rule, missing-context, expired/suspended, multi-rule precedence.
- **PP-5 — Enforcement:** fold policy into `evaluate_dispositions` after safety (`POLICY_BLOCKED`/`POLICY_REVIEW_REQUIRED`; `warn`→`ELIGIBLE`+reason); pin bundle version before eval; ranking now runs over the post-policy eligible set (no ranker change — it already consumes a precomputed rankable set). Flag-gated; emergency rollback = disable enforcement while shadow telemetry stays on. *Exit:* additive-only proven; blocked/review recipes unranked; rollback tested; shadow telemetry survives enforcement.
- **PP-6 — Justify & approval:** `/contract/policy/justify` (rule-schema justification) + `/contract/policy/approvals` (authority-checked, no self-approval, separation-of-duties, fingerprint-bound); approval → append-only `PolicyEvaluationRevision` + `PresentationProjection` + rerank; reject/revoke; stale-request handling. Role-aware UI (author sees Request/Justify; authority sees Approve/Reject; read-only rules panel with human-readable + normalized predicate). *Exit:* no self-approval, expired-authority rejected, stale-request rejected, old projection immutable, rerank correct, approval lifecycle append-only.

## Why this is separate, not "Phase 2C/2D"
Ranking + dimensions are recipe-selection UX with a small deterministic surface. This layer authorizes exceptions and blocks features under regulatory framing — it needs lifecycle state machines, an authority/RBAC integration, a compliance-owned expectation set + sign-off gate, versioned+fingerprinted replay, and append-only projections. Treating it as a quick sub-phase is exactly the "policy debt that is hard to unwind" the review warns against. It ships when it can carry that weight — after Phase 2 (ranking + dimensions) and a compliance partnership.
