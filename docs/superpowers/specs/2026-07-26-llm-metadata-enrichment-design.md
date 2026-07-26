# Layer-1 LLM Metadata Enrichment — maximal, ontology-ready, non-blocking (design)

Date: 2026-07-26 · Status: design for review · Parent: `2026-07-22-scaled-ai-attestation-design.md` (§3.1 Layer 1) · Pairs with: P2 confidence overlay (`2026-07-22-p2-ai-attested-tier-design.md`) · Step 1 of the enrichment→ontology two-step arc.

## Goal

Get as much useful metadata as possible from the LLM onto every column, across every catalog, to make **feature generation** richer and to serve as the **backbone of a cross-catalog data ontology** (Step 2). Everything the LLM proposes is **usable immediately** by feature generation; where the LLM is unsure it is flagged **"human verification needed"** (via the P2 overlay) but **never blocked**; humans correct in arrears.

## The spine principle (the design's backbone)

**Get the `concept` right; let the rulebook cascade what it can; point the LLM at the gaps.**

The concept registry already encodes a lot of analytical structure per concept — `group`, `is_a` edges, `entity_link`, `additivity`, `pit_role`, `sensitivity`, `leakage_anchor` (`overlay/upload/concepts.py:29-43,55,70-82`). So:

1. The LLM's **highest-leverage job is `concept`** — get it right and broaden coverage (every column left `unclassified` is invisible to feature-gen and to the ontology).
2. The rulebook **cascades additivity / temporal(pit) role / entity link / leakage / sensitivity-floor** from the concept — but **with transitive provenance** (the crux the safety line depends on): from a **human-confirmed** concept the derived safety fields are a real `taxonomy/confirmed`-grade authority that clears; from an **AI-proposed, unconfirmed** concept the cascade still runs (non-blocking) but its derived safety fields **inherit the concept's verification-needed status** — they do NOT silently clear the gauntlet. A derived field is only as trusted as the concept it came from. The cascade *is* the AI→safety path, so it must carry the concept's provenance forward, never launder it. (Today `derive_concept_evidence` at `ingest.py:1196` emits `taxonomy/proposed` but does **not** carry the concept's provenance/verification; `graph.py:255-257` writes file-declared not concept-cascaded — both are gaps this spec must close.)
3. The LLM then **only** enriches what the rulebook cannot derive (the gap fields, §Scope). It does not waste calls (or risk) guessing what the concept already implies.

This keeps AI effort and AI risk pointed exactly where they add value; the cascade stays deterministic but **provenance-carrying**, not blindly "safe".

## The non-negotiable safety line

The LLM may **propose** any field — including safety/operational ones — and feature generation **uses** those proposals. But an LLM-proposed **safety/operational** value must stay **visibly "verification needed"** until a human confirms it: **used, not blocked, never silently treated as verified.** A wrong AI guess must never *silently* clear a safety check (e.g. mark a non-additive column summable, or a mixed-currency amount combinable).

This is **transitive**, and that is the subtle part: because the rulebook derives the safety fields *from* the concept (the spine), an **AI-proposed-but-unconfirmed concept makes its derived additivity/leakage verification-needed too**. So the gauntlet (§E4) must check the **root concept's** provenance, not only the immediate field's — otherwise the cascade silently launders an AI concept-guess into a cleared safety check (the exact laundering hole a prior review caught; it is re-created if E4 only looks at the derived field). Provenance-awareness = follow the derivation chain back to its author.

## Scope — what the LLM enriches (prioritised, lead-data-scientist lens)

Grouped by what the rulebook can/can't derive:

**Already-admitted advisory fields (policy allows `llm`; `_MEANING`, RECOMMENDATION ceiling — `field_policies.py:88`):**
- **`concept`** — broaden coverage; the spine. (Enriched today.)
- **`definition`** — promote from display-only (`graph.py:248`) to governed `llm/proposed` evidence.
- **`domain`** — promote from display-only (`graph.py:246`) to governed `llm/proposed` evidence.

**New gap fields the rulebook can't derive (LLM adds real value; all advisory, RECOMMENDATION-tier):**
- **Analytical role — `key` / `measure` / `dimension` / `time`.** The single most useful feature-engineering tag: what to sum, group by, window over, join on. *Ex: `tran_amt`→measure, `mcc_code`→dimension, `post_date`→time, `acct_no`→key.*
- **Value semantics for flags/categoricals** — what the codes mean, **extracted from the column's own description/definition** (the customer's text, e.g. a description reading "Y = statement account, N = non-statement"), **never guessed from raw data** (which the LLM never sees — sample values are stripped before egress). The LLM's job is to *structure* the description's code explanations into a machine-readable code→meaning map; where the description does not explain the codes, value-semantics is **left blank, not invented**. Must ride the sanitized-definition egress channel (preserves definitional text, strips sample values/PII). *Turns free-text descriptions into count/ratio/indicator-ready features.*
- **Synonyms / `semantic_terms`** — LLM-authored (the column + sanitizer path already exists: `graph_node.semantic_terms`, `enrich_llm.py:194` whitelists it; today glossary-projected only). Drives feature-gen menu + relevance (`feature_assist.py:140,160`).
- **Column-to-column semantic relationships** — typed intra-row links: *amount denominated_in currency*, *rate converts amount*, *value as_of date*. Unlocks derived features (currency-normalised spend, rate-adjusted exposure).
- **Entity as a first-class object-type (Palantir-Foundry framing)** — designate, per entity, its **primary-key** column (the identifier) and a **title/display** property; the LLM proposes both, reusing the existing `suggest_entity` flow (`entity.py:94-161`). Turns a free-text `entity` tag into a proper object with a key and a name — the anchor for links, grain, and the ontology.
- **Relationship cardinality** — Customer 1→N Account, so aggregate vs join-through. Governs aggregation correctness.
- **Event-vs-snapshot confirmation + recency/velocity suitability** — snapshot(balance) vs event(transaction); which columns are good recency/velocity signals. (Concept `pit_role` seeds it; LLM confirms/fills.)
- **Feature-affinity seeds (light)** — "good for sum/avg over entity", "good ratio numerator". Kept deliberately light (full feature ideation is a later phase).

**Safety/operational fields (LLM proposes, provenance-guarded — §E4):**
- `additivity`, `temporal_role`, `leakage_anchor` (mostly cascade from concept; LLM proposal is advisory/verification-needed), `unit`, `currency`, `logical_representation`. LLM may propose; the gauntlet treats an LLM-proposed value as **verification-needed**, never a silent clear.

**Explicitly NOT LLM-fillable from metadata (honest gap):** distribution/quality signals — null rate, cardinality counts, skew, outliers. These need **data profiling**, which this system deliberately doesn't do (facts come from the upload). Hugely useful for feature engineering, but out of scope here; flagged as a separate decision (profiler or upload-provided stats).

## Mechanics (grounded)

The enrichment call/schema/prompt/egress/cache/batch stack is **already field-generic** — `draft_definitions`/`classify_domains` prove three fields share it (`enrich.py:532,604`). The one concept-specific piece is the evidence **write** (`_write_concept_evidence`, `enrich.py:326-397`, hardcodes `field_name="concept"` + `is_known_concept`).

- **Generalise the writer:** a field-parameterised `llm/proposed` evidence writer (field_name + acceptor + config-hash), reusing the egress guard (`audited_structured_call`, `enrich_llm.py:640`), per-field acceptors (closed-set for `analytical_role`, bounded free-text for definition/value-semantics, open-vocab for domain), the vocab cache, and the batch ladder. Definition/domain/synonyms/role/value-semantics all route through it.
- **New per-column advisory fields** (`analytical_role`, `value_semantics`, `feature_affinity`) need: a `graph_node` column (or JSON facet), a `field_evidence` field_name, and an advisory policy (RECOMMENDATION ceiling, `llm` admitted, never load-bearing — mirror `_MEANING`, `field_policies.py:88`).
- **Relational enrichment** = the ontology's **link types** (Foundry framing): typed relationships **between entities** with **direction + cardinality** (1:1 / 1:N / N:M) backed by a join/bridge, plus intra-row column-to-column links (amount↔currency, rate↔amount). These are **edges**, not column fields — a new typed advisory edge store beside `graph_edge` (`0945`) / `entity_bridge_edge` (`0989`), LLM-proposed + confidence-flagged, VERIFIED-by-human before it's operational (mirrors the existing join/bridge governance).
- **Entity** already has an LLM-propose→human-confirm mechanism (`entity.py:94-161`, `suggest_entity`, `entity_suggestion` table) — reuse it; do not rebuild.
- **The registry cascade** (spine step 2): project the concept registry's `additivity/pit_role/entity_link/leakage/sensitivity` onto each classified column deterministically (extend `derive_concept_evidence`), so these are concept-derived (`taxonomy/proposed`) rather than only file-declared.

## The provenance-aware, non-blocking gauntlet (§E4 — the safety line, made real)

**Honest scope (from the engineering review): this is bigger than "extended to read provenance", and it is only `unit`/`currency`.** additivity/temporal_role/is_grain/is_as_of/logical_representation already read through the governed path (`_governed_read`→`read_operational_value`, `feature_assist.py:631,650,696,708`), so an ungoverned llm value there *already* fails to clear (non-`resolved`) — no change needed, and the **transitive-concept** check (above) handles the AI-concept→cascade case. The real silent-clear risk is **only** `unit`/`currency`, which the gauntlet reads as a flat `graph_node` presence (`_column_meta`, `feature_assist.py:531-543,663-681`) with **no** producer/authority axis, **no** decision-link column (`field_resolution.py:87-114` omits them), and **no** resolver projection (`graph.py:256-257` file-declared only). So making these provenance-aware requires, as first-class E4 work:
- admit `_LLM_PROPOSED` into `unit`/`currency`'s display_rule (today `_MEASURE_ANNOTATION`, `field_policies.py:182-189`, is source/human-only — an llm unit isn't even displayable);
- add `unit`/`currency` to the resolver's projection + decision-link maps + a migration for `*_decision_id` columns;
- replace the flat `_column_meta` unit/currency reads with a **producer-level** read (`status=="resolved"` can't separate a source-declared unit that never had a decision from an llm-proposed one — keying on status would *regress* file-declared units; we must read the winning evidence's **producer**).

The danger and its fix are **both created by this spec** (today an llm unit can't reach `graph_node.unit`). Guarantee: flag-off and source-declared cases stay byte-identical (no new requirements on file-declared units); only an llm-proposed unit/currency drives `NEEDS_EXTERNAL_VALIDATION`. The LLM's guesses are still *used* (the feature is proposed on them) and flagged for the human — never a silent clear, never a block.

## Ontology-readiness (Step-2 enabler)

The grounding confirms the **make-or-break fact**: the concept vocabulary is **one global 281-concept registry shared across all catalogs** (`concepts.py:842,885`, no per-source variant) — so `concept` is a true cross-catalog join key, and identifier concepts carry a canonical `entity_link` that already keys the governed cross-catalog bridge (`entity_bridge_edge`, `0989`, cross-catalog by construction). This spec keeps everything **ontology-ready** by emitting each signal as a **governed, source-qualified** value (`logical_ref` embeds `catalog_source`, `object_ref.py:33`):
- `concept`, `domain`, `entity`, `synonyms`, `analytical_role`, `value_semantics`, column-relationships, cardinality — all become clean cross-catalog signals the Step-2 ontology view reuses (node model = `graph_node`; edges = `entity_bridge_edge` + `graph_edge` + the new relationship store; traversal = `lineage_graph`, `lineage.py:73`, already cross-catalog). Step 2 then adds only a source-agnostic entry point + a concept-hierarchy (`group`/`is_a`) exposure + the visual view.

## Prior art — Palantir Foundry ontology (reference model)

The Step-2 ontology target maps cleanly onto Foundry's proven model, and this spec is deliberately built so those pieces fall out of the enrichment:
- **Object types** ≈ our **entities** (now with a primary-key + title property, this spec's entity item) — backed by physical columns via `logical_ref`.
- **Properties (typed, semantic)** ≈ our enriched per-column fields (concept, analytical role, value semantics, unit) — the "rich property metadata" *is* this enrichment.
- **Link types (with cardinality)** ≈ our **E3** typed entity relationships.
- **Datasource decoupling / shared semantic layer** ≈ our shared concept registry as the cross-catalog join key (`concepts.py:842`) — already decoupled from storage.
- **Governance / digital twin** ≈ our governed evidence + WORM audit + four-eyes confirm.
- **Actions / Functions (Foundry's "kinetic" write-back layer)** — **deliberately NOT adopted.** We are a catalog + feature platform, not an operational app builder; **governed feature generation is our kinetic layer** — the ontology's operational payoff is features, not write-back apps. (This is also the concrete answer to "does the ontology help feature-gen": the ontology is the object/property/link model feature-gen reasons over.)

## Composition with P2 (the confidence flag)

Every new `llm/proposed` field composes with the P2 confidence overlay exactly as `concept` does: P2 scores confidence and renders **"AI-filled · verification needed"** (rare, loud, non-blocking). Fields whose confidence signals P2 doesn't yet cover render **"AI-filled" with no band** (per P2's own design) until a per-field signal is added — a P2-side follow-on, not a blocker here.

## Product coverage — the wrapper around the engine (from the head-of-product review)

The enrichment engine is only half the product; without these it produces a flood of AI metadata that can't be *proven*, *governed*, or *trusted*. These ship **with** the engine, not in a later phase.

### Prove it — outcome measurement (the E1a exit gate)
Success is **feature-gen lift**, not "fields populated". This is a **net-new measurement harness** (the existing `attest/` shadow harness measures concept-classification *confidence*, not lift — no baseline/delta instrument exists): a baseline snapshot store + counting queries over `ground_all` (`templates.py:332-343`) and `_validate_idea` (`feature_assist.py:733`) + a before/after runner. Metrics: # columns leaving `unclassified` (groundable), # templates grounding to buildable candidates, # relevant menu candidates, # cross-catalog candidates. Three requirements that make the gate real:
- **A numeric bar agreed with the funder** (e.g. ≥X% fewer unclassified, ≥Y more buildable templates) — "a positive delta" is not a gate.
- **An honest baseline = today's system *with* current concept enrichment** (`concept` is enriched already), so E1a's true increment (cascade + 3 fields) isn't inflated by already-shipped work.
- **A hold-out / second corpus** so lift isn't proven only on the one FTR CSV.
Passing this gate is what justifies funding E1b/E2–E4.

### Govern it at scale — review surface + coverage dashboard (ships with E1/E2)
P2's confidence flag covers `concept` only, so every new field would otherwise flow in unreviewed. First-class deliverables:
- A **metadata review queue** grouping AI-enriched fields by field-type **and by convention** (all `analytical_role=measure`; all value-semantics for `*_flg` codes), with **bulk approve/correct by convention** — fix a whole pattern at once, so the human unit of work stays *conventions, not columns*.
- An honest **"AI-filled · unreviewed"** state, distinct from "human-confirmed", on every new field.
- **Triage by business value:** rank the queue by feature-usage (columns feeding the most/accepted features) and entity centrality (keys/titles of high-traffic entities), reusing the live-usage signal the program already computes.
- **Reviewer role routing:** value-semantics → data owner/SME; analytical role & link types → data modeler; sensitivity → compliance. Segregation of duties on AI metadata.
- A per-catalog **Enrichment Coverage / readiness dashboard:** coverage % per field, verification-needed count, entities-with-key+title, unclassified count, and a single **"feature-gen-ready / ontology-ready"** rollup with the specific blockers. The multi-catalog onboarding spine.

### Compound it — corrections feed back (learning hook)
A correction seeds a reusable example (the Proposer only, per the parent's validator-firewall rail) so the same mistake is not re-served on the next column or catalog. Success criterion: **a same-convention re-upload needs measurably fewer corrections.** (The full rule/cluster layer is P3; this spec adds the hook so E-phase corrections aren't thrown away.)

### Trust it — usage doubles as correction
In the feature builder, an AI-filled field carries a one-click **accept/reject in flow** — using it *is* the confirmation, rejecting *is* a correction — which gives the data scientist a reason to engage and cheaply feeds the learning hook.

### Cross-catalog conflict detection (ontology integrity)
Split honestly by what's knowable when (engineering review): **concept-keyed** divergence *is* detectable at enrichment time — `concept` is a true cross-catalog key on `graph_node` (single global registry), so "the same concept enriched with conflicting analytical_role/value-semantics across catalogs" surfaces now, in a reconciliation queue. **Entity-keyed** divergence (same *entity* given a different primary-key/cardinality across catalogs) requires first knowing two catalogs' entities are the same — which is the human-VERIFIED cross-catalog entity resolution in **E5**. So entity/PK/cardinality conflict detection rides with E5's resolution, not "now"; only concept-keyed conflict is an E2 deliverable.

### Compliance & audit (bank-grade, pulled forward)
Ship-blocking before a bank turns this on: a per-field **provenance + confidence + plain-English "why"** (capability C, pulled forward to ride with E2 — *not* deferred to P5), and an **enrichment-model-governance record** (model + prompt version, calibration/eval evidence, who-confirmed, revocation). Without the "why", stewards can only rubber-stamp.

### First visible increment (thin vertical slice) + ROI
The demo-able moment, tied to the parent's feature-ideation demonstrator: enrich a source → a data scientist opens a column → sees its analytical role + synonyms + a code meaning → **generates a feature that wasn't buildable before**, with the coverage dashboard showing the lift. Track 2–3 buyer-facing ROI metrics from E1 (% columns auto-enriched, analyst-hours saved per catalog, % more features buildable) plus a back-of-envelope $/catalog (new LLM calls + P2's O(columns) pass) vs hours-saved, so the economics are demonstrably net-positive.

### Operational model + cold-start (the onboarding reality)
Not covered by the engine alone, and it bites at the first enterprise onboarding:
- **Trigger / enablement:** per-catalog opt-in behind an enablement interlock (like `FEATUREGEN_INTENT_LIVE`), not automatic-on-every-upload.
- **Async, not blocking:** the enrichment LLM pass runs **off the ingest transaction** (a 150K-column catalog cannot block ingestion on a batch LLM pass); state the expected latency and the incremental (changed-columns-only) steady state.
- **Cold-start:** a new catalog's first pass produces thousands of AI-filled, unreviewed fields at once — the dashboard opens near-zero-confirmed with a backlog wall. The plan: feature-gen is usable immediately (non-blocking), **usage-as-correction** is the primary funnel that pulls stewards to what matters, and **bulk-by-convention** (E2) clears the rest — with an honest "unreviewed but usable" state throughout.

### Honest build cost (from the engineering review — these are first-class, not "small adds")
The measurement harness, the coverage/readiness dashboard, the bulk-by-convention correction primitive, and the E4 unit/currency provenance scaffolding are each **net-new build items** with real cost, not extensions of existing surfaces (`readiness.py` is a safety-blocker gate, not a coverage roll-up; `field_correction.py` is single-target CAS/four-eyes; there is no lift harness today). Each gets its own scope in the plan.

## Phased decomposition (build order — value up, risk concentrated last)

- **E1a — Prove the lift (backend, no customer UI — the small provable slice that funds the rest):** promote `definition`/`domain`/`synonyms` to governed `llm/proposed` evidence via the generalised writer (a real ~70-line-per-field lifecycle: staleness/reuse/attach-gate/savepoint — not a signature change); broaden `concept` coverage; project the registry cascade **with transitive provenance**. Then stand up the **feature-gen-lift measurement harness** (baseline snapshot + before/after deltas — *net-new infra*) and run it on the FTR corpus **plus a hold-out**. Exit gate = a **numeric** lift vs the **honest baseline** (today's system *with* current concept enrichment, so E1a's true increment isn't inflated). All advisory, no gauntlet change.
  - **Build order — FEATURE-FIRST (architectural directive):** the enrichment *itself* ships and is end-to-end testable **first** — generalised writer → `definition` → `domain` → `synonyms` → broaden `concept` → cascade — each a thin vertical slice provable on its own (upload → AI enriches → governed `llm/proposed` evidence → visible with honest provenance). The **measurement harness is the LAST step** (it measures what already works, and can be a lean counting script over `ground_all`/`_validate_idea`, not an over-built platform). Prior phases over-invested in scaffolding and left the actual feature to the end; invert that here — the feature is testable by task 2, the scaffolding rides at the tail.
- **E1b — Govern at scale (customer-facing; after lift is proven, before customer enablement):** the per-catalog coverage/readiness dashboard (*net-new* catalog-wide roll-up, distinct from `readiness.py`'s safety-blocker gate) and the review surface. Ship the honest **"AI-filled vs confirmed"** state; **defer** the bulk-by-convention correction primitive (a genuinely new multi-target CAS + per-target four-eyes mechanism — its own design surface) and reviewer-role routing to land with E2, alongside the "why" and per-field confidence that make review meaningful (don't ship a review queue that can only rubber-stamp).
- **E2 — Analytical fields + entity object-types + full governance surface:** `analytical_role`, `value_semantics` (extracted from descriptions), light `feature_affinity`, and **entity primary-key/title** (reusing `suggest_entity`) — new advisory fields, LLM-enriched. **Ships with:** the bulk-by-convention correction primitive (its own design surface), review-queue triage + role routing, the plain-English "why", accept/reject-in-flow in the feature builder, **concept-keyed** cross-catalog conflict detection, and the learning-hook (correction → reusable example — net-new: example store + Proposer-only injection). *Value-semantics is a conditional win, not a headline — pre-measure what % of flag/categorical descriptions actually explain their codes on the FTR corpus first.*
- **E3 — Relational enrichment (Foundry-style link types):** typed **link types between entities** (direction + cardinality 1:1/1:N/N:M + backing join/bridge) plus intra-row column-to-column links — new advisory edge store, LLM-proposed, human-VERIFIED before operational. These *are* the ontology's link types.
- **E4 — Provenance-aware non-blocking gauntlet:** let the LLM propose safety/operational fields; make the gauntlet provenance-aware so LLM-proposed → verification-needed (never silent-clear, never block). The safety line. Biggest/riskiest of E1–E4 — do last.
- **E5 — Cross-catalog data ontology view** (own spec: `2026-07-26-cross-catalog-ontology-view-design.md`): assemble the E1–E4 ontology-ready signals into the three ontology forms (queryable semantic map, entity-relationship model, visual navigable graph) via cross-catalog entity resolution. Gated on E1a's proven feature-gen lift; needs E3's link types + the **entity primary-key/title** work (assigned to **E2**, reusing `suggest_entity`).

Each phase is its own spec→plan→impl cycle (following the program's phasing discipline). E1 delivers a richer, ontology-ready catalog with zero trust risk; E4 is the safety-sensitive gauntlet change; E5 is the ontology payoff, gated on E1's proven value.

## Out of scope / deferred
- Data profiling / distribution-quality signals (null rate, cardinality, skew) — needs a profiler or upload-provided stats; separate decision.
- Full feature ideation (per-column feature proposals) — a later program phase (P4); E2's affinity seeds stay light.
- The Step-2 ontology view itself — now **E5**, with its own spec (`2026-07-26-cross-catalog-ontology-view-design.md`); gated on E1's proven lift.
- AI certifying any safety field as authority — never; safety stays taxonomy/source/human. LLM only *proposes*, verification-needed.

## Risks
- **LLM over-reach on safety fields** → the provenance-aware gauntlet (E4) + P2 verification flag; a proposal is never a silent clear.
- **Field sprawl / cost** → the spine principle (LLM only fills gaps the rulebook can't derive) bounds the call surface; batch + cache reused.
- **Value-semantics accuracy** (E2) → advisory only, confidence-flagged; wrong value-semantics degrade the menu, never a safety gate.
- **Relationship/cardinality errors** (E3) → human-VERIFIED before operational (mirrors join/bridge governance), advisory otherwise.
- **Ontology drift across catalogs** → the single shared concept registry is the anchor; new fields are source-qualified.

## Success criteria (outcomes, not outputs)
- **E1 exit gate (the one that funds the rest):** a **measured feature-gen lift** vs the captured baseline — *N* fewer `unclassified` columns, *M* more templates grounding to buildable candidates, more relevant menu candidates. "Fields populated" is *not* a pass on its own. Plus: definition/domain/synonyms render with honest `AI-filled` provenance; classified columns carry concept-cascaded additivity/pit-role/entity-link/leakage; the coverage dashboard shows per-catalog readiness; the review queue supports bulk-by-convention correction.
- **E2:** analytical role + description-sourced value semantics measurably improve the menu (more/more-relevant candidates); every new field carries an honest "unreviewed vs confirmed" state + a plain-English "why"; a correction seeds a reusable example so a same-convention re-upload needs **fewer** corrections; conflicting cross-catalog enrichment is detected.
- **E3:** entity link-types + cardinality proposed and human-verifiable; derived-feature paths (currency-normalised, rate-adjusted) appear that couldn't be built before.
- **E4:** an LLM-proposed unit/additivity makes a feature `NEEDS_EXTERNAL_VALIDATION` (used, flagged) — never a silent clear and never a block — proven by test; source/human values still clear.
- **Governance at scale:** the human unit of work is a *convention/entity*, not a column — provable by a same-convention re-upload needing far fewer human actions.
- **Compliance:** every AI-generated field is auditable — provenance + confidence + who-confirmed + "why" + revocation — and an enrichment-model-governance record exists.
- **Ontology-ready:** every new signal is governed + source-qualified, reused as-is by the Step-2 ontology backbone.
- **First visible increment:** a demo where enrichment makes a previously-unbuildable feature buildable, with the lift shown on the dashboard.
