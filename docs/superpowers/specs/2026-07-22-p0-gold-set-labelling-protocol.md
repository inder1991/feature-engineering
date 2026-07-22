# P0 — Gold-Set Labelling Protocol

Date: 2026-07-22 · Status: ready for the labelling team · Feeds: P0 shadow measurement (spec §6, §8-P0)

## Why this exists

P0 measures whether AI attestation can be trusted — as a **number**: the false-attest rate at a given
confidence threshold. That number is only meaningful against **human ground truth**. This protocol
produces that ground truth for a stratified sample of columns. It is the one input the harness cannot
generate itself, and it gates the entire attestation program (P2 onward). The measurement is only as
honest as this sample is representative and un-anchored.

## What a "gold label" is

For a sampled column, the **correct value a human believes is true**, decided **independently of the
AI's proposal**. We label two fields per column (the two that drive everything else):

1. **`concept`** — the single best-fitting concept from the controlled vocabulary (276 concepts;
   the labelling sheet lists them), or the literal **`unclassifiable`** when none genuinely fits.
   *(The behavioural fields — additivity, temporal_role, leakage_anchor — are not labelled: they are
   deterministically derived from a confirmed concept by the taxonomy, so labelling the concept covers
   them. Measuring the concept measures the cascade.)*
2. **`sensitivity`** — one of `pii` / `restricted` / `none`. Labelled separately because sensitivity is
   never AI-derivable (spec §3.4.3) and is a high-risk field the gate always escalates.

## The sampling plan (stratified, not first-N)

A threshold measured on a biased sample is worthless. Draw a **stratified random sample**, not the
first N columns:

- **Strata:** cross `domain` × `risk` × `type-family`. For the FTR table use at least:
  domains {Payments, Compliance, Customer, Channel, Technical};
  risk {looks-like-PII, money/amount, identifier, descriptive, technical/ETL};
  type-family {numeric, date/time, code/enum, free-text, identifier}.
- **Size:** **120 columns** for the FTR source (of 126 — so nearly a census here, which is ideal for the
  first source; later, wider sources sample ~15–20% per stratum). 120 labels give a usable confidence
  interval on a single **global** false-attest rate (per-domain thresholds are deferred — spec §3.3).
- **Draw:** the harness emits the stratified sample as a worksheet (`P0` command, below). Do **not**
  hand-pick columns.

## The independence rules (this is what makes it valid)

1. **Label blind.** The worksheet shows the column name, its file-declared definition, its BIAN/FIBO
   path, and 5 sample values — but **NOT** the AI's proposed concept. Anchoring on the AI's guess
   destroys the measurement.
2. **Two labellers per column**, working independently. A third person adjudicates disagreements; the
   adjudicated value is the gold label. Record all three.
3. **Use `unclassifiable` honestly.** A technical/ETL column (`etl_batch_id`, `dummy1`) with no real
   business concept is a *correct* `unclassifiable`, not a failure to think harder.
4. **Sensitivity is about the data, not the name.** A column named `notes` that contains card numbers
   is `pii`. Label from the sample values.

## The worksheet format (what the team fills in)

One row per (column × field). The harness produces columns 1–6 pre-filled; the team fills 7–9:

| # | column | given (blind) | field | 1st label | 2nd label | adjudicated | labeller ids | notes |
|---|--------|---------------|-------|-----------|-----------|-------------|--------------|-------|
| 1 | `cif_id` | def + bian/fibo + 5 sample values | concept | | | | | |

The filled worksheet is ingested into `attestation_gold_label` (one row per adjudicated label) by the
harness (`P0 ingest <worksheet.csv>`). Schema: `(catalog_source, logical_ref, field_name, gold_value,
labeller_ids, adjudicated_by, labelled_at, notes)`.

## Effort (state it plainly — spec §6)

~120 columns × 2 fields × 2 labellers + adjudication ≈ **480 label decisions + ~50 adjudications** for
the FTR source. At ~30s/label that is roughly **half a day of two people's time** for the first source.
This is the honest cost of the measurement, and it is one-time per source (later same-convention sources
reuse the learned rules and need far less). It is *not* the per-column bottleneck the program abolishes —
it is a fixed, bounded calibration cost.

## What P0 does with it

Once labels are ingested, the harness runs the triangulation signals over the sampled columns and, by
joining to the gold labels, computes — at a sweep of confidence thresholds — the **false-attest rate**,
the **grounding coverage**, and the **auto-attestable fraction**, with confidence intervals. The output
is the go/no-go artifact: *"at threshold T, auto-attesting the low-risk bulk has a measured false-attest
rate of X% (95% CI …)."* Nothing is written to the authority tier. See the P0 implementation plan.
