# P2 — Independent AI Confidence as a Read-Time, Non-Blocking Overlay (design)

Date: 2026-07-22 · Status: design for review (revision 3.1 — survived adversarial review, fixes folded in) · Parent: `2026-07-22-scaled-ai-attestation-design.md` · Builds on: P0 signals + store (`overlay/upload/attest/`, migration 1018), P1a read-model honesty.

> **Revision history.** v1 tried to promote AI into the *governed* authority tier — inert & unsafe (dead
> influence leaf, cascade laundering, byte-identity contradiction). v2 kept the right goal but reused
> `field_evidence.confidence_band`, which `readiness.py` already reads (a `'low'` band silently drops a field
> *out* of the review queue — the opposite of our intent) and leaned on a "cheap deterministic band at ingest"
> that doesn't exist. v3 fixed the carrier with a new sidecar; review found the sidecar duplicated the P0
> observation store and dropped the scored concept value (stale bands) and that calibration had no substrate.
> **v3.1 collapses the sidecar onto the existing P0 observation store** — the confidence, the scored concept,
> and the risk tier already live there — resolving all three. Both review lenses now confirm the safety /
> non-blocking architecture and the standalone reuse of the signal functions. See
> `../../memory/p2-spec-not-buildable-split-recommended.md`.

## The decision this encodes (from the user)

- AI **fills gaps and adds detail**; its output is transparent ("AI-filled"), never disguised as human.
- Where an **independent** check finds the AI genuinely unsure, the column is flagged **"human verification
  needed"** — **louder** (surfaced for attention) but **rare** (extreme low-confidence only).
- That flag **never blocks anything — feature generation included.** A "verification needed" column
  participates in every workflow exactly as an unflagged one.
- Humans **correct in arrears**; a correction supersedes the AI value.

## What is already true (grounded — the reason this is small)

- **AI-filled metadata already participates in feature-gen, ungated:** the planner routes on the flat
  `graph_node.concept` value carrying `llm/proposed` concepts (`templates.py:139-147`, `candidates.py:58`);
  `concept` has no eligibility check in that path (`field_policies.py:80-84`).
- **Doubt already downgrades, never blocks:** an ungoverned/AI-derived `additivity`/`is_grain`/`is_as_of`
  yields a `NEEDS_EXTERNAL_VALIDATION` requirement, not a rejection (`feature_assist.py:648-712`). The only
  hard-stops — a table with no as-of column (`feature_assist.py:692-694`) and a leakage/protected concept
  (`templates.py:150-164`, `planner/safety.py:22-31`) — are correctness guards we keep unchanged.
- **P1a already renders "AI proposed"** per field from `(producer, strength)` (`asset_detail.py:142-185`).
- **The safety invariant holds by construction:** behavioural/safety fields gate on `(producer, strength)`
  only (`field_policies.py:120-131`); a confidence number can never confer safety authority.
- **The confidence already exists, per column, in the P0 store:** `attestation_shadow_observation` carries
  `confidence` (`fuse()` output), the scored `proposer_value`, and the frozen `risk_tier`, keyed by
  `(shadow_run_id, logical_ref, field_name)` (migration 1018; read at `report.py:136-152,188-190`).

So the only genuinely missing things are: (a) computing that confidence for **all** AI-filled concepts (P0
only samples the gold subset), (b) a **calibration** that turns the float into a rare "verification needed"
flag, and (c) **honest, non-blocking surfacing.** That is all P2 adds.

## Explicitly left alone (avoids the v2 collision)

`field_evidence.confidence_band` and its `readiness._is_low_confidence` consumer
(`readiness.py:376-383,546-558`) are **out of scope and untouched** — that is the enrich-LLM self-grade slot
with a *quieter* semantics, opposite to ours. P2 never writes or reads it.

## The confidence store — reuse the P0 observation store (no new table)

The confidence lives where it already is: `attestation_shadow_observation` (migration 1018). No new
confidence table. This is what makes calibration work (`report.py` reads exactly this store) and removes the
v3 duplicate-store / stale-key problems, because the observation already records the `proposer_value` the
confidence was computed against.

- **Runs are tagged with a purpose** (`measure` vs `confidence`) — a small additive column on
  `attestation_shadow_run` — so a full-coverage *confidence* run does not pollute the gold-sampled *measure*
  runs that `report.py`'s false-attest math depends on. `report.py` filters to the purpose it needs.
- Writing observations is **not** an authority write (they are WORM measurement rows, not
  `field_evidence`/`decision`/`graph_node`), so the P0 measure-only invariant (`attest/runner.py:13-14`) is
  preserved. (A **new** test must assert the confidence pass adds zero authority rows; the existing
  `test_run_shadow_writes_no_authority_state` wraps `run_shadow` only and does not cover the new writer.)

## The confidence pass — the independent triangulation, in arrears (no LLM in ingest)

A new async/batch pass computes confidence for AI-filled `concept`s by reusing the P0 signal functions
standalone (review-confirmed callable outside the runner): deterministic `ground_concept(conn, logical_ref,
concept)` (pure over `graph_node`+`field_evidence`) + blind independent `reclassify_concept(conn, client,
logical_ref, *, column_ctx, …)` + pure `fuse(...)`. It writes one observation per column into a fresh
`confidence`-purpose run.

- At **ingest**, an AI-filled field is just **"AI-filled, confidence pending"** — no band. (There is no honest
  cheap-at-ingest band; grounding alone is thin and, at enrich time, mis-timed vs `build_graph` — v2's error.)
  This "compute it later" shape *is* the enrich-in-arrears model.
- The pass **must** run **after** `build_graph`/resolve (so `ground_concept`'s sibling/type/path inputs
  exist), **stands up an `LLMClient` + `ColumnContext`**, and **is O(columns) LLM calls** — so it is batched,
  rate-limited, and **incremental**: it re-scores only concepts whose value changed since their last
  observation (the recorded `proposer_value` differs). `reclassify_concept` also emits its own audited
  `llm_call`/`document_schema` telemetry (as in P0) — intended, non-authority.
- Scope is **`concept`** — the only field the P0 signals cover. `definition`/`domain` render "AI-filled" with
  **no band** until they get their own signal.

## Calibration — what makes "verification needed" mean something (and stay rare)

`fuse` yields a float; the **low threshold** at which `verification_needed` fires (and any high/medium/low cut
points) is a **new calibration output added to `report.py`**, chosen against the human gold set per
source/field. It does not exist today (`report.py:42` is a single auto-attest sweep, not band cut points) —
it is explicit new work, and it reads the same `attestation_shadow_observation JOIN attestation_gold_label`
substrate `report.py` already uses. Until real gold labels exist, the artifact and every flag it drives are
`provisional` and rendered as such. The threshold is set deliberately low so the flag stays **rare**; on
thin-grounding columns the default is **"AI-filled, no band"**, never "low" — the flag is never manufactured
by absence of signal.

## Surfacing (all read-time, all non-blocking)

### S1 — Asset detail
Extend `_effective_metadata_section` (`asset_detail.py:162-185`) to also read the **latest `confidence`-run
observation** for the field and render **"AI-filled · <band>"** or **"AI-filled · verification needed"** —
but **only when both**: the field's active evidence author is `llm` (so a corrected field shows nothing AI),
**and** the observation's `proposer_value` equals the currently-displayed concept (so a stale score for a
superseded concept is suppressed). These two guards are what deliver S3's supersession without any extra
write. Optionally add a `('human','corrected')`-style label; note the existing fallback already yields
`"human confirmed"`, so this is cosmetic.

### S2 — The disposer (honest, column-level, draft only)
For each **input column** of a proposed feature (`derives_pairs`, the `(catalog_source, object_ref)` operands
— `feature_assist.py:478`, `templates.py:103`, serialized at `gate1.py:578`), resolve
`logical_ref_of(conn, catalog_source, object_ref)` (`column_authority.py:56`), read its latest `concept`
observation under the same two guards, and annotate the option: **"input `<col>`: meaning is AI-filled ·
verification needed — its derived additivity/timing rests on that."** This is honest about the provenance
chain (the safety property was *derived from* the AI concept) without hanging a concept band on an additivity
requirement.
- It is **advisory text only** on the **considered-set/draft response** (which is shaped separately from the
  persisted C0 snapshot; `_idea_from_json` ignores unknown keys) — it changes no requirement, no validation
  status, no acceptance, and does not touch the frozen `Requirement` value object or the persisted snapshot.
- It surfaces at **`/contract/considered-set` (draft) only** — `/contract/confirm` re-runs server-side and
  renders no per-operand requirements (`contract.py:665-708`).
- **Leakage is dropped** from surfacing: a leakage/protected concept is a hard reject
  (`feature_assist.py:596-597`) and never enters the considered set.

### S3 — Correction in arrears
A human field-correction appends `human/confirmed` evidence and re-resolves (`field_correction.py:404-411,
446-452`), so the field's active author flips to `human`; **S1/S2's "author is llm" guard then suppresses the
AI band automatically** — no confidence-store write is needed on correction. The next `confidence` run simply
finds `proposer_value` unchanged-by-AI and does not re-score a human-authored field.

## The flag
`OVERLAY_AI_CONFIDENCE` (default OFF) gates the confidence pass (writes) and the S1/S2 reads. OFF ⇒ no
`confidence`-purpose runs are written and no confidence is read ⇒ **byte-identical to today.** No calibration
interlock, no live-authority switch — nothing here confers authority.

## Non-blocking — the load-bearing guarantee (tested)
The confidence is read by **exactly two consumers**: S1 and S2, both advisory. Nothing in P2 reads it in any
eligibility, candidate-filter, readiness, or acceptance path. A **`verification_needed` column produces the
identical candidate set and the identical disposer outcome** it does today — a required test, and the whole
point.

## Explicitly NOT in this design
- No authority-tier change (no `corroborated` strength, no influence promotion, no
  `_GOVERNED_DECISION_FIELD`/projection edits, no cascade minting `taxonomy/confirmed`).
- No write to `field_evidence`; no touch of `confidence_band`/`readiness`.
- No blocking of any kind, anywhere. No cheap-at-ingest confidence.

## Risks / open points
- **Grounding thinness → sparse bands.** Many columns land "AI-filled, no band"; that is acceptable
  (transparency without a fake number) but means "confidence everywhere" is really "confidence where the
  signal supports it." Quantify coverage on real FTR in the plan.
- **Confidence-pass cost is O(changed concepts) in LLM calls.** Batched, incremental, in-arrears — never on
  the ingest path — but a real recurring cost to budget.
- **Calibration depends on the human gold set.** Bands/flags are `provisional` until real labels exist; the
  number is a *signal*, never a gate — so provisional is safe.
- **Decorrelation caveat (from P0):** `reclassify` is same-model-different-prompt, not a different family;
  calibration must be justified on that basis, not overclaimed.

## Decomposition (tasks, for the plan)
1. Add a `purpose` (`measure`/`confidence`) discriminator to `attestation_shadow_run` (additive migration);
   teach `report.py` to filter to `measure` for the false-attest math.
2. The confidence pass: over all AI-filled `concept`s (incremental on changed `proposer_value`), run
   `ground_concept` + `reclassify_concept` + `fuse`, write observations into a `confidence` run; provision the
   `LLMClient`/`ColumnContext`; **new test** asserts zero `field_evidence`/`decision`/`graph_node` writes.
3. Band/verification calibration output in `report.py` (low threshold + cut points, per source/field, against
   gold), `provisional` when labels absent.
4. S1 asset-detail: latest-`confidence`-observation read + the two guards (author=llm ∧ proposer_value match)
   + "AI-filled · <band>/verification needed".
5. S2 disposer: per-operand `logical_ref_of` → latest concept observation (same guards) → column-level
   advisory annotation on the draft response; drop leakage; draft-only.
6. `OVERLAY_AI_CONFIDENCE` flag wrapping writes + reads; flag-off byte-identity test; **non-blocking test** (a
   `verification_needed` column yields the identical candidate set + disposer outcome).
7. S3: verify a human correction flips the active author so S1/S2 suppress the AI band (no store write).

## Success criteria
- Flag-OFF: byte-identical to today.
- Flag-ON: an AI-filled `concept` scored low-confidence renders **"AI-filled · verification needed"** in asset
  detail and, when an input to a proposed feature, an advisory line at `/contract/considered-set` — and the
  feature is **still proposed and still buildable** (non-blocking test).
- The confidence pass writes **zero** `field_evidence`/`decision`/`graph_node` rows; a `confidence` run does
  not change `report.py`'s `measure` false-attest output.
- `readiness` output is unchanged by P2.
- A safety field derived from an AI concept never becomes a governed safety authority.
- A human correction suppresses the AI band/flag (author-guard), with no confidence-store write.
- The two hard-stops (no as-of column; leakage/protected concept) are unchanged.
