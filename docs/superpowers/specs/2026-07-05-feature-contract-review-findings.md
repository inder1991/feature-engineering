# Feature-Contract Flow — deep-dive review findings

Date: 2026-07-05. 4 parallel adversarial reviewers (correctness, data-models, integration, plans) over the
built Phases 1–5 (`overlay/upload/contract/*`) + both plans; every finding **verified against the code**
before recording. Status: findings confirmed; fix campaign in progress.

## Framing
The contract flow is **built + tested but NOT wired to any live path** (`contract/__init__.py` is empty; no
orchestrator). These are **pre-integration defects** — real, must-fix-before-use, not live harm. Two root
causes explain most:
- **(A) No orchestrator** → safety kwargs (`target_ref`/`now`/`roles`/MCV) default to "skip"; omission
  silently downgrades safety.
- **(B) `object_ref` not catalog-qualified** (`"public.{table}.{column}"`, constant `_SCHEMA`; graph PK is
  `(catalog_source, object_ref)`) → `catalog_source` is lost at `FeatureIdea.derives_from: list[str]` and
  re-derived ambiguously from `graph_node`.

## BLOCKERs
- **B1 — `confirm_contract` governs without re-running MCV** (`govern.py:53-68`). Registers a
  leaky/stale/empty draft. *Fix: run `validate_minimum` in `confirm_contract`; refuse unless clean.*
- **B2 — MCV grounding tautological** (`review.py:26`, `known=set(draft.derives_from)`). A column dropped
  after Gate #1 passes MCV and is silently unwired at confirm (empty lineage → vacuously fresh).
  *Fix: source `known` from the live graph.*
- **B3 — Multi-catalog corruption (root cause B)** (`govern._derives_pairs:43-50`; `author._as_of_column`,
  `_column_defs`). One `object_ref` fans out to every catalog that has it → contract binds to sources it
  never read (false drift + fail-closed on unrelated data); catalog-blind as-of/definition. *Fix: carry
  `(catalog_source, object_ref)` through `FeatureIdea.derives_from` end-to-end.*
- **B4 — Version race + re-confirm proliferation** (`0960` has no `UNIQUE(feature_name, version)`;
  `govern.py:60-67` MAX+1 read-then-insert; `register_feature` mints a new feature every confirm).
  *Fix: unique constraint + atomic version; supersede/upsert the feature by name.*
- **B5 (retirement plan) — keep-set not self-contained** (`intake/llm.py:26` imports `intake.events`;
  `call_llm:558` appends `feature_contract`). Deleting `events.py` breaks `llm.py` → the overlay + all
  overlay tests. *Fix the plan: trim `call_llm` + the `events`/`store` FC imports from `llm.py` first.*

## MAJORs
- **M1 — Read-scope dropped at authoring** — `author._column_defs` has no sensitivity filter; a
  `restricted` column's definition reaches the LLM (M6 regression). *Fix: sensitivity filter + `roles`.*
- **M2 — `confirm_gate1` asymmetric** (`gate1.py:95-101`) — only `anchor` identity checked; an
  `"alternative"` choice with the anchor's name is accepted + mis-recorded. *Fix: symmetric membership.*
- **M3 — `target_ref`/`now` default to skip** — authoring leakage + freshness re-checks silently no-op.
  *Fix: carry them on the draft / require them at confirm.*
- **M4 — `intake._classify` hardcodes `_scan`** (`intake.py:32-34`) — an injected redactor never runs.
  *Fix: classify via the injected redactor.*
- **M5 — Critique node unaudited** (`review.py:33`, `_call_raw`, unregistered schema) — contradicts
  "every LLM node audited"; fails closed against a real provider. *Fix: route through the audited seam.*
- **M6 — Spec fidelity** — not event-sourced (relational tables, no lifecycle events); the **intent is
  never persisted** (the mandatory hypothesis isn't stored); `ContractDraft` drops spec fields (io_schema,
  join_path, unit, lineage, assumptions). Spec/plan/retirement-plan mutually contradict the "reuse
  `intake/events`" claim. *Decision needed: close the gaps, or update the spec to match the relational
  reality.*

## MINORs (verified)
Empty-definition contract governs; `anchor=None` silent on ungroundable definition; empty considered-set
un-passable with a misleading error; unresolved critique dropped after budget; **feeding MCV reasons into
`refine` is inert** (refine only re-authors narrative, can't clear a structural defect); `_actor_json`
stores `None`/dicts as junk jsonb; `_snapshot` drops `grain_table`; no FK `contract.feature_id→feature`
(orphan → fail-open fresh); point-in-time check mis-parses 4-part `object_ref`; `fresh_within` not threaded.

## Not defects (verified — do not "fix")
Loop termination, `refine`→None, redacts-to-empty are safe. Redaction NER/name gaps are a **documented
deferral** — a real free-text egress surface for the hypothesis/definition, recorded as a known limitation.

## Retirement plan corrections (verified)
- **B5 above** is the real blocker (was buried in Phase-1 "expected: no matches").
- `llm_claude` has **zero external callers** (`build_claude_llm` uncalled) — keeping it is defensible but
  the plan's "overlay needs it" reason is wrong.
- Event-backbone coupling is mostly a **leave-able** generic `feature_contract_id` column + one outbox
  case + dead CHECK arm — NOT the highest risk; the risk register was mis-ordered.
- Hypothesis-plan Phase-6 note wrongly lists `read_model` as a worker import (worker imports only
  `commands` + `bootstrap`).
