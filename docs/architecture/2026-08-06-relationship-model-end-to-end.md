# How Relationships Are Built: The Complete End-to-End Approach

Date: 2026-08-06
Status: review document — describes the shipped system as of main `061c1440`.
Audience: product/architecture review. Every claim below was verified against the code; file
references are given so a reviewer can spot-check.

---

## 1. The one-paragraph summary

The platform never compares every column against every other column. Instead it works in four
stages: **(1) label** every column with a concept from one shared vocabulary at upload,
**(2) pair** columns across catalogs only when their concepts share an identifier namespace,
**(3) measure** the surviving pairs against real data, per direction, and **(4) execute** only
what measured safe — with typed refusals everywhere else. Discovery is cheap, deterministic and
open; execution is evidence-gated. Human confirmation records review; it is never the thing that
makes a relationship usable, and it is never a substitute for measurement.

---

## 2. The foundation every relationship stands on: concepts and the three axes

At upload, each column is matched to a **concept** from the platform's own vocabulary
(`overlay/upload/concepts.py`, ~280 concepts). A concept carries three independent axes
(the "three-axis semantic model"):

| Axis | Question it answers | Example |
|---|---|---|
| **Entity** | What real-world thing is this about? | customer, account, transaction |
| **Identifier namespace** | Who issues these values — which numbering scheme? | the bank's customer-number scheme, SWIFT BIC |
| **Party role** | What role does the thing play here? | account holder, counterparty, beneficiary |

Concepts also form a hierarchy (`is_a` edges: `interest_income` *is a* `monetary_flow`), which
downstream consumers use for generalization ("any monetary flow works here").

Two things matter for everything below:

- **Namespace is the ONLY axis that gates join candidacy.** Entity and role are corroboration
  and display. This is deliberate: entity labels are often AI-proposed, and a safety decision
  must never rest on a guess.
- Concept assignment itself has authority levels: deterministic matches, AI-proposed matches
  (with evidence, honestly badged, correctable), and human-confirmed ones. An AI-proposed
  concept is usable immediately — the platform-wide "usable before confirmation" rule.

---

## 3. The complete taxonomy of relationships

| # | Relationship | Between | Executable? | Where defined |
|---|---|---|---|---|
| 1 | **Structural** (catalog → table → column) | objects in one catalog | n/a (it *is* the graph) | `graph.py`, built at upload |
| 2 | **Concept assignment** (column → concept) | a column and the vocabulary | n/a (it's the label) | enrichment + `concepts.py` |
| 3 | **Concept hierarchy** (`is_a`) | concepts | n/a | `concepts.py`, repo-authored |
| 4 | **Governed join** (`approved_join`) | tables in ONE catalog | YES — the only operational join path inside a catalog | Pass C discovery + 2-admin confirm |
| 5 | **Entity bridge** (direct equality) | identifier columns in TWO catalogs, same namespace | YES, after directional measurement | `bridge_*` modules |
| 6 | **Crosswalk** (mapping-table mediated) | two catalogs via a mapping dataset | YES in principle; compiled + gated, flag OFF today | `crosswalk_*` modules |
| 7 | **Transformed** | columns whose values relate through a declared transformation | not yet — labelled honestly, not executable | frozen display vocabulary |
| 8 | **Semantic-only link** | columns/tables related by MEANING only | NO, by definition — context, search, retrieval | context graph / semantic context |

The four cross-catalog *display* kinds are a frozen vocabulary — `direct_equality`, `crosswalk`,
`transformed`, `semantic_only` — pinned by a contract test so no fifth kind (and no relabeling of
a semantic link as something joinable) can appear silently.

---

## 4. When each relationship is created — the trigger map

| Trigger event | What runs | What it produces |
|---|---|---|
| **Catalog upload** (`ingest.py`) | graph build; deterministic concept matching; **Pass C join discovery** (`OVERLAY_PASS_C=1`, live); **entity-bridge candidate discovery** (always) | the structural graph; first concept stickers; within-catalog join *proposals*; cross-catalog bridge *candidates* (with considered / retained / suppressed / truncated counts recorded) |
| **Batched LLM enrichment** (Gate-B style run, paid) | Pass A (per-column concepts), Pass B (table synthesis + narratives), domain/sub-domain tasks | AI-proposed concept assignments + enriched metadata, as governed *proposed* evidence |
| **Human review** (governance screens) | 2-admin join confirm; bridge confirm; field corrections; PII policy approvals | review RECORDS (visible provenance) — for joins, the VERIFIED `approved_join` state; for bridges, a review note — usability does not wait for these except where noted in §6 |
| **Data probes** (data-agent, needs cluster access) | bridge scoped probes; **crosswalk composed measurement** (`data_agent/crosswalk_measurement.py`) | directional cardinality observations — the evidence production execution actually depends on |
| **Feature compile / analysis run** | join planning (`materialize/joins.py`), read-scope authority, refusal codes | an executable plan, or a typed refusal naming exactly what is missing |

Key point for review: **discovery costs nothing and runs at every upload**; the expensive/risky
stages (LLM, data probes, execution) are each behind their own flag or approval.

---

## 5. Layer by layer

### 5.1 Upload: the structural graph and the candidates (deterministic, no LLM)

When a catalog CSV lands, `ingest.py` builds the graph (catalog → tables → columns, each with a
stable `logical_ref` that survives schema differences), attaches deterministic concept stickers,
and then runs two discovery passes **from metadata alone**:

- **Pass C — within-catalog join discovery** (`OVERLAY_PASS_C`, ON live): reads the declared
  keys/metadata in the upload and proposes table-to-table joins *inside* the catalog. No LLM.
  A proposal is not operational until two admins confirm it (below).
- **Entity-bridge candidate discovery** (`bridge_candidates.py`): finds identifier columns,
  resolves their concepts, and pairs columns **across** catalogs whose concepts share an
  identifier namespace. Bounded (max 20 candidates per endpoint per source-pair, pool cap 80),
  suppressions are counted and reported, entity disagreement becomes a display note. Read-only
  and deterministic — the same upload always yields the same candidates.

On the real catalogs this yields 9 cross-catalog candidates, of which 1 (CIB `customer_number`
↔ FTR `CIF`) is the real customer link and 8 are weak branch-code pairs — which is why ranking
exists, and why an AI corroboration pass over candidate *pairs* is a proposed follow-up
(decision pending; see §8).

### 5.2 Enrichment: the AI improves the stickers (paid, Gate-B gated)

The batched LLM passes read column names, types, glossary sidecars and table context, and
*propose* concepts and narrative metadata where the deterministic matcher couldn't decide.
Everything AI-produced lands as **proposed evidence with the reasoning attached** — badged in
the UI (`AI PROPOSED`), usable, correctable without re-upload, and never silently promoted
above proposed. Better stickers → better candidate discovery on the next pass.

### 5.3 Human surfaces: review is recorded, not load-bearing

- **Within-catalog joins are the one exception where confirmation gates use**: a Pass C
  proposal becomes operational only as a VERIFIED `approved_join` after **two admins** confirm
  (`proposal_commands` / `confirmation_commands`). This is deliberate: these joins run inside
  every catalog query surface, so the bar is highest. `join_drift` then watches confirmed joins
  against later uploads.
- **Cross-catalog bridges/crosswalks**: confirmation records that a human reviewed the link.
  It does NOT make the link executable (measurement does), and withholding it does NOT hide
  the link (usable-before-confirmation). A mutation test pins that a reviewer's confirm
  changes no admission answer — "review substitutes for safety" is a release-gate kill.

### 5.4 Measurement: the evidence execution actually depends on

Two levels, kept honestly separate:

- **Metadata-level** (`bridge_cardinality.py`): compares a directional realization's key tuple
  against the table's governed grain — complete-and-unique / complete-but-non-unique / unknown.
  Explicitly "metadata evidence only"; it can never assert production safety.
- **Data-level** (data-agent probes): the real measurement. For bridges: scoped probes per
  direction. For crosswalks: the **composed observation** — the two-leg shape
  (source → mapping → target) measured as one join, in BOTH named directions, never inferred
  from the legs' individual maxima; the mapping's temporal row rule ("which row is current")
  and physical bindings are pinned into the observation, so evidence can never license a
  traversal over a different schema or a different edition of the mapping than was measured.

Directionality is the heart of it: CIB→FTR can be 1:1 while FTR→CIB fans out. Every verdict is
per-direction, and admission distinguishes a production tier (strict) from a sandbox tier
(plannable without approval, for exploration — DoD 16).

### 5.5 Execution: refusal-heavy planning

- **Direct bridge** (`plan_cross_catalog_join`): re-checks structurally even though the caller
  validated — realization active + deterministically validated, physical identity matches the
  pinned bindings on both sides, cardinality known, direction correct. Any miss is a typed
  refusal.
- **Crosswalk** (`plan_crosswalk_join`): accepts only the fused, cross-checked bundle
  (definition + execution + decision + row-selection + observation + three bindings). The
  rendered pipeline enforces: mapping row filter → per-frame uniqueness gate → join → composed
  amplification check spanning both legs. **Fan-out refuses; nothing deduplicates silently.**
  Every pinned revision reaches the generated project's provenance and README.
- The old rule that one plan cannot state two independent fan-out verdicts still stands for two
  unrelated joins; a crosswalk plans only because its *composition* is itself measured.
- Startup enforces the flag chain `CROSSWALK → SOURCE_TEMPORAL → DATASET_PROFILES`: an illegal
  combination refuses to boot the API rather than logging and continuing.

### 5.6 Consumption: where relationships surface

- **Feature generation**: the gauntlet's join authority reads governed joins and bridge
  realizations; read-scope decides who may even compile over a table (Gate 2, conjunctive
  table floor).
- **Asset Detail / Context dossier**: `RelationshipContextV1` shows each relationship with its
  kind label, both crosswalk legs, the mapping dataset, row policy, direction-specific
  cardinality and safety, "what already depends on this" (honest `not_tracked_yet`, never a
  fake zero), and the three unresolved families — *nobody decided yet* / *needs a data check* /
  *structurally unsuitable*. Never "blocked", never "approval unblocks it".
- **Learning**: a missing business decision (e.g. no temporal row policy chosen) files an open
  gap; fan-out/duplicates/overlap are data-quality or safety refusals and deliberately do NOT
  auto-file as ontology gaps.
- **Semantic-only links** feed search and retrieval expansion — related-by-meaning, clearly
  labelled non-executable.

---

## 6. The governing invariants (the rules a reviewer should hold us to)

1. **Namespace-only candidacy** — meaning corroborates, only the numbering scheme nominates.
2. **Usable before confirmation** — AI-proposed and discovered relationships show and serve
   immediately; confirm records review. (Exception: within-catalog operational joins, §5.3.)
3. **Review never substitutes for measurement** — pinned by release-gate mutations.
4. **Direction-specific, current, deterministic evidence** gates production execution.
5. **Fan-out refuses; nothing silently deduplicates.**
6. **Every pin travels** — definition, execution, bindings, temporal policy, observation, leg
   realizations reach the artifact's provenance; an auditor reads exactly what executed.
7. **No failure framing for undecided things** — the three-family vocabulary, enforced by
   forbidden-phrase scans over the rendered UI.

---

## 7. The Case-A decision (recorded business fact)

CIB `customer_number` and FTR `CIF` are **one numbering scheme** — the same numbers in both
systems (user decision, 2026-08-04). Therefore they share one namespace, the direct-equality
bridge between them is the correct join, and no registry split exists. Recorded in the
`crosswalk_discovery` docstring so nobody "fixes" the direct bridge into a crosswalk. The
alternative (Case B — two schemes, direct match invalid, crosswalk required) was rejected as
factually wrong for this bank.

---

## 8. Current live state, and what turns on when

| Capability | State on the cluster today |
|---|---|
| Structural graph, concept stickers, Pass C proposals, bridge candidates | LIVE (run at every upload; `OVERLAY_PASS_C=1`) |
| LLM enrichment of the current catalogs | code deployed; the paid re-enrichment run is **Gate B** (awaiting user trigger) |
| Feature-context + dataset-profile serving | ON (`FEATUREGEN_FEATURE_CONTEXT=1`, `FEATUREGEN_DATASET_PROFILES=1`) |
| Cross-catalog grounding in live feature generation | interlock OFF (boot log confirms) |
| Crosswalk execution | flag OFF everywhere; compiled + proven through fixtures; the store-side assembler has no production caller yet (recorded: DEFERRED-WORK A.44/A.45) |
| Real crosswalk measurement | needs Hive/mapping data — arrives with the bounded Hive/ODS profiling approval |
| AI pair-corroboration ranking of bridge candidates (§5.1) | PROPOSED, not built — natural rider on Gate B |

---

## 9. Worked example, end to end

1. You upload FTR and CIB. Ingest builds both graphs; `CIF` and `cust_num` both deterministically
   sticker as `customer_number` (entity: customer; namespace: the bank's customer-number scheme).
2. Bridge discovery pairs them (same namespace, Case A) — 1 strong candidate among 9; the link
   shows on both asset pages immediately, ranked, with entity notes.
3. Gate B enrichment sharpens weak stickers elsewhere; a proposed AI corroboration pass would
   demote the 8 branch-code pairs with stated reasons.
4. A data probe measures both directions: CIB→FTR 1:1 ✓, FTR→CIB 1:1 ✓ → directional
   realizations become production-eligible.
5. A feature spanning both catalogs compiles: the planner re-checks the pinned bindings and
   cardinality, the artifact carries every pin, and a reviewer can read exactly which link, which
   measurement and which mapping edition the number was built on.
6. If instead FTR→CIB had measured 1:50, that direction refuses with the fan-out named — shown
   as a data-quality fact to fix, not a "blocked" failure and not an ontology gap.
