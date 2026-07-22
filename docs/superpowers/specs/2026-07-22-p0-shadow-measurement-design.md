# P0 — Shadow Measurement Harness (design)

Date: 2026-07-22 · Status: design for review (→ then a TDD plan) · Parent: scaled-AI-attestation spec §3.2, §6, §8-P0

## Goal

Produce the go/no-go **number** that gates the whole attestation program: at a confidence threshold T,
the measured **false-attest rate** for auto-attesting the low-risk bulk, with a confidence interval,
plus grounding coverage and the auto-attestable fraction. **Writes nothing to the authority tier** —
it is measure-only, mirroring the planner shadow store (`planner/shadow_store.py`, migration 0999,
WORM/append-only). Consumes the gold set from the labelling protocol.

## Principle it proves (and the trap it avoids)

Two of the three triangulation signals are LLMs; only grounding is non-AI. On a column-mapping upload
`operational_type` is often `unknown`, so grounding can be thin. P0 must therefore **measure grounding
coverage per field** and show what the gate looks like *when grounding is present vs absent* — so we
learn empirically whether the gate degenerates to "two correlated LLMs agreed" on this data, rather
than assuming it doesn't (the exact blocker the adversarial review raised).

## Components

### 1. Persistence (migration 1018, WORM, mirrors planner_shadow_*)
- **`attestation_gold_label`** — human ground truth (ingested from the worksheet):
  `(catalog_source, logical_ref, field_name, gold_value, labeller_ids jsonb, adjudicated_by, labelled_at,
  notes)`. PK `(logical_ref, field_name)`. Append-only.
- **`attestation_shadow_observation`** — one row per (column, field) per shadow run, append-only WORM
  (INSERT-only, CHECK constraints + payload hash, like `planner_shadow_plan_observation`):
  `(shadow_run_id, logical_ref, field_name, proposer_value, proposer_producer,
  reclassify_value, reclassify_agrees bool, grounding_checks jsonb, grounding_coverage numeric,
  grounding_conflict bool, confidence numeric, risk_tier text, payload_hash)`.
  **No gold value stored here** — correctness is a *read-time join* to `attestation_gold_label`, so the
  observation is never contaminated by the label and can be re-scored against a corrected gold set.
- **`attestation_shadow_run`** — one row per run (dispatch manifest): `(shadow_run_id, catalog_source,
  gold_version_hash, model_ids jsonb, signal_versions jsonb, started_at, column_count)`. Capture
  integrity = every sampled (column,field) has an observation (manifest↔observation reconciliation,
  the shadow-store discipline).

### 2. Deterministic grounding signal (non-AI, the trustworthy one)
Pure function over existing evidence (no LLM), so unit-testable without a provider. For a column's
proposed `concept`, compute independent checks and a **coverage** (how many checks had a signal at all):
- **type-consistency:** the concept's implied type-family (from the vocabulary metadata) vs the parser's
  `logical_representation`/`semantic_type` evidence. Signal present only if a parser type exists.
- **path-agreement:** the concept vs the file-attested `bian_path`/`fibo_path`/`business_term` (a
  lookup/similarity against the vocabulary's path mappings). Present only if the file attested a path.
- **cross-field / sibling consistency:** e.g. a `currency_code` concept expects a sibling amount column.
- Output: `{checks: {...pass/fail/absent}, coverage: n_present/n_possible, conflict: any_fail}`.
**Gate rule (spec §3.2):** where `coverage < floor`, the column is **not auto-attestable** regardless of
LLM agreement — P0 measures the false-attest rate *with this rule applied* and *without*, to show its value.

### 3. Independent re-classification signal (the decorrelating LLM)
A **second** classification of the column, blind to the first proposal, via a **different prompt framing
(and, if configured, a different model)** to decorrelate errors. Reuses the enrichment client seam
(`enrich.py` `LLMClient`, `audited_*` call path, egress guard) with a new `prompt_id`
`overlay_concept_reclassify_v1`. Output: `{value, agrees_with_proposer bool}`. This is the only new
provider cost in P0, and only over the sampled gold columns (~120), not the whole catalog.

### 4. Confidence fusion
P0 does **not** invent a calibrated score from thin air — it computes a transparent **agreement vector**
(proposer, reclassifier, grounding) and a simple monotone fusion into `[0,1]`. Calibration is empirical:
the *report* (component 6) sweeps the raw fused score against the gold labels to find the threshold that
yields an acceptable false-attest rate. The fused score is a ranking; the gold join makes it a
calibrated gate.

### 5. Shadow runner (writes nothing to the authority tier)
`run_shadow(conn, catalog_source, *, client, gold_version)`:
1. resolve the sampled columns (those with a gold label);
2. for each, read the proposer's existing `concept` evidence (via the now-fixed schema-aware
   `logical_ref_of` → `field_evidence`), run grounding (2) + re-classification (3), fuse (4), assign the
   risk tier (intrinsic PII/leakage from the taxonomy sensitivity/leakage evidence);
3. write one `attestation_shadow_observation` per (column, field) + the run manifest. **No authority-tier
   write, no `ai/attested` evidence, no decision** — pure telemetry.

### 6. Report / metric (the go/no-go artifact)
`shadow_report(conn, shadow_run_id)` joins observations → gold labels and computes, over a **threshold
sweep** and split by (all / grounding-covered / grounding-thin) and by field:
- **false-attest rate** = fraction of auto-attested (confidence ≥ T, low-risk) columns whose fused value
  ≠ the gold value, with a Wilson 95% CI;
- **auto-attestable fraction** = share that clears the gate at T;
- **grounding coverage** distribution.
Surfaced read-only (a CLI + a `/gate`-style authority-only route, mirroring the existing gate console).
The headline output: *"threshold T → false-attest X% (95% CI a–b), auto-attests Y% of low-risk columns,
grounding covered Z%."*

## Task breakdown (each its own TDD cycle, for the plan)
- **T1** migration 1018 + the three WORM tables + the store module (INSERT-only, CHECK, payload hash,
  reconciliation) — mirror `shadow_store.py`.
- **T2** deterministic grounding signal (pure function; unit-tested, no provider).
- **T3** independent re-classification signal (new prompt_id; a fake-client unit test + the real seam).
- **T4** confidence fusion (pure function; unit-tested).
- **T5** shadow runner + gold-set worksheet **emit** (stratified sample) + **ingest** (worksheet→
  `attestation_gold_label`).
- **T6** report/metric (threshold sweep + Wilson CI, split by grounding) + the read-only surface.

## Non-goals (kept out of P0, per the parent spec)
- No authority-tier write, no `ai/attested`, no gate that changes catalog behavior — measure only.
- No adversarial-refutation signal yet (deferred until re-classification+grounding are shown
  insufficient on the gold set).
- No per-domain thresholds (single global threshold first).
- No async/worker move (that is P1b); P0 runs as an offline/authority-only job over the sampled columns.

## Open design questions for review
1. **Re-classification model:** same model, different prompt (cheapest, weaker decorrelation) vs a
   different model family (stronger decorrelation, needs a second provider/config)? Recommend: start
   same-model/different-prompt, and have the report flag if proposer/reclassifier agreement is
   suspiciously high on gold-wrong columns (the correlated-error signature).
2. **Grounding floor value:** start unset (measure across all floors 0–1 in the sweep) and let the data
   pick it, rather than guessing.
3. **Gold-set size:** 120 for FTR (near-census) is generous; is that the right one-time cost, or start
   at 60 and widen if the CI is too loose?
