# Plan: Agent-Debate Concept Mapping + Compound-Key Bridge Discovery

Date: 2026-08-07
Status: FOR REVIEW — verified against main `3c304dc4` before writing; no code changed yet.
Origin: user direction 2026-08-07 — "bring agents to debate the mapping (proposer / refuter /
synthesizer producing the final proposed evidence)" + "introduce a discovery pass that analyzes
joint distinctness across sets of columns to propose multi-column bridge candidates."

---

## Part 0 — What already exists (verified, and it is more than expected)

**The debate is ~70% built.** The scaled-AI-attestation harness (`overlay/upload/attest/`,
merged earlier as a SHADOW-measurement system) already contains every debate role:

| User's role | Existing module | What it does today |
|---|---|---|
| Proposer | enrichment Pass A concept task | proposes a concept per column from metadata |
| Refuter | `attest/concept_critic.py` | a refute-oriented second pass ("asked to refute is structurally harder to please"); deterministic hard-conflicts refuse WITHOUT dispatching the model; closed verdict `supported / refuted / uncertain` |
| (bonus) Independent second opinion | `attest/reclassify.py` | a second LLM call blind to the first |
| (bonus) Deterministic grounding | `attest/grounding.py` | type/path/sibling checks, no LLM |
| Synthesizer | `attest/fusion.py` | pure, transparent confidence fusion: agreement raises, a deterministic grounding conflict caps LOW unconditionally (two agreeing LLMs cannot outvote a failed check), grounding coverage scales how much LLM agreement is trusted |

What is missing is exactly one thing: **today this machinery only MEASURES (shadow mode) — its
verdict does not decide what gets proposed.** The fusion score lands in the shadow report, not
in the proposed evidence.

**The compound-key contract layer already speaks tuples.** `IdentifierEndpointV1.members` is a
tuple of columns; realizations carry multiple `column_pairs`; `TupleKeyRole.COMPOSITE_MEMBER`
exists; `bridge_cardinality` already compares a realization's ordered tuple against the table's
complete governed grain (with "a scalar member of a composite key" honestly = unknown). What is
missing is exactly one thing: **discovery (`bridge_candidates.py`) only nominates single
columns.** Nothing downstream needs a data-model change.

Consequence for review: both features are *wiring and discovery* work on proven contracts, not
new subsystems.

---

## Part 1 — Debate-synthesized proposals (Feature 1)

### 1.1 The change in one sentence

Promote the attest chain from shadow-measurement to the production path of the concept task:
for contested columns, the proposal that lands as `llm/proposed` evidence is the SYNTHESIS of
proposer + refuter (+ grounding), carrying the debate trace as its evidence.

### 1.2 Design rules (these keep us inside the platform's standing invariants)

1. **Authority does not move.** The debate improves WHICH concept is proposed and how it is
   annotated — the result is still `proposed`-tier evidence. Promotion of AI above proposed
   remains gated exactly as before (disposition seam + human gold labels; the P2b gate is NOT
   touched by this plan).
2. **Deterministic checks outrank the debate.** `fusion.py`'s existing rule stands: a failed
   grounding check caps confidence regardless of how eloquently two models agree.
3. **Debate only where it pays.** Three-role debate runs for: (a) columns where the
   deterministic matcher abstained, (b) columns whose proposed concept carries an identifier
   NAMESPACE (these gate joins — the highest-stakes stickers), (c) columns where refuter and
   proposer disagree on re-run. Everything else keeps the single-pass path. All calls ride the
   existing CallLedger budget; exhaustion → `not_attempted`, never a silent skip.
4. **The trace is the evidence.** The stored proposal carries: proposer's answer + reasoning,
   refuter's verdict + reasoning, fusion inputs and score. The UI badge stays `AI PROPOSED`;
   the dossier can show "proposed after refutation attempt" — which is honest strength, not a
   new authority tier.

### 1.3 Tasks

- **T1** — a `debate_synthesis` module in `attest/`: orchestrates proposer→critic→fusion for
  one column batch; returns the winning concept + the trace. Pure orchestration of existing
  parts; no new LLM task types.
- **T2** — wire it into the concept task's accept path behind `OVERLAY_DEBATE_SYNTHESIS`
  (default OFF; D8-style flag row): when ON, contested columns route through T1 and the
  synthesized result is what `record_proposed_evidence` stores. Schema/version discipline per
  D10 (the evidence payload gains the trace — a REAL version bump, not an alias).
- **T3** — evidence/UI surfacing: the trace renders in Metadata & evidence (collapsed), and the
  three-family language stands (a refuted proposal that leaves no winner files
  "needs a data check" — never "blocked").
- **T4** — evaluation: extend the existing gold-set harness — debate-on vs debate-off concept
  accuracy on the 22-column gold set + the mutation "the fusion never sees gold labels" stays
  killing. Bar: debate-on ≥ debate-off, and zero NEW namespace-bearing false positives.

### 1.4 What it costs / when it runs live

Fake-client tests now; live calls are Gate-B territory (this rides the same approval — the
debate multiplies calls only for the contested subset, estimate in the Gate-B package).

---

## Part 2 — Compound-key bridge discovery (Feature 2)

### 2.1 The change in one sentence

Teach discovery to nominate multi-column endpoints (e.g. `branch_code + account_type +
sequence_number`), using metadata-first joint-key evidence now and measured joint distinctness
when data probes are approved.

### 2.2 Where compound candidates come from (bounded, never combinatorial)

The pass never enumerates arbitrary column subsets. Candidate TUPLES come only from:

1. **Declared/governed grain tuples** — a table whose governed grain is
   `(branch, acct_type, seq)` nominates exactly that tuple (the strongest source: somebody
   already declared "these columns together identify a row").
2. **Declared composite keys / uniqueness constraints** in the upload metadata.
3. **`COMPOSITE_MEMBER` clusters** — columns already marked as members of a composite key that
   never appear alone.

Cap: tuples of 2–4 members, same caps-per-source-pair regime as single-column discovery, with
suppression counts reported (the "no silent caps" rule).

### 2.3 Cross-catalog pairing rule for tuples

A tuple pairs with a tuple in another catalog when the MEMBERS align by namespace
(order-independent, matched member-to-member: branch↔branch, acct_type↔acct_type, seq↔seq).
Partial alignment (2 of 3 members) is recorded as a WEAK candidate with the missing member
named — visible, never auto-suppressed. Entity disagreement stays a display note, exactly as
for single columns.

### 2.4 Joint distinctness — the two-phase honesty split

- **Phase A (now, metadata-only):** joint-key strength comes from declared evidence
  (`bridge_cardinality`'s complete-key assessment, extended from "the realization's tuple vs the
  grain" to "the candidate's tuple vs the grain"). Verdicts stay the honest three: complete-
  and-unique / complete-but-non-unique / unknown. NO claimed distinctness without a declaration.
- **Phase B (gated on the bounded Hive/ODS profiling approval):** a scoped data probe measures
  ACTUAL joint distinctness of the tuple (`COUNT(DISTINCT tuple) / COUNT(*)`) and directional
  fan-out of the tuple-join, through the same data-agent probe machinery and the same
  directional-observation contracts crosswalks use. Phase A shapes are written so Phase B fills
  them without a contract change.

### 2.5 Tasks

- **T5** — `compound_candidates` discovery step in `bridge_candidates.py`: tuple sources §2.2,
  pairing rule §2.3, caps + suppression counts; runs at ingest beside single-column discovery
  (same deterministic, read-only guarantees). Flag `OVERLAY_COMPOUND_BRIDGES`, default OFF.
- **T6** — extend the metadata assessment to candidate tuples (Phase A); rank compound
  candidates below any single-column candidate with equal evidence (a compound claim leans on
  more members being right).
- **T7** — surface in the link UI: members listed, per-member namespace alignment shown,
  missing-member weakness named; framing rules apply (an unmeasured tuple "needs a data check").
- **T8** — release-gate mutations: (a) a tuple candidate claiming distinctness with no
  declared grain must die; (b) member-order confusion (branch↔seq) must die; (c) partial
  alignment silently upgraded to full must die.
- **T9 (gated)** — the Phase-B joint-distinctness probe, lands with the Hive/ODS approval.

---

## Part 3 — Decisions I need from you

1. **Debate scope (§1.2.3)**: agree that debate runs only on the contested subset (abstentions +
   namespace-bearing + disagreements)? The alternative — debate every column — roughly triples
   Gate-B enrichment cost for little gain on the easy majority.
2. **Compound-candidate sources (§2.2)**: metadata-declared keys only, or also "AI suspects
   these form a key" as a fourth source? My recommendation: NOT in v1 — an AI-guessed key
   tuple entering discovery would put an LLM opinion at the head of a join pipeline; Phase B's
   measured distinctness is the honest way to find undeclared keys later.
3. **Sequencing**: build both now with fake-client/fixture proof (T1–T8), live debate calls and
   Phase B ride Gate B + the Hive approval respectively — or hold everything until after Gate B?

## Part 4 — What this plan deliberately does not do

- No authority promotion of AI output (P2b untouched, disposition seam untouched).
- No LLM in compound candidacy (deterministic sources only, v1).
- No arbitrary-subset distinctness mining (bounded sources only — the combinatorial version is
  both expensive and a false-positive machine).
- No migrations expected; if evidence-payload versioning needs one, D7 rules apply
  (next free number checked at commit time, reserved in the same commit).
