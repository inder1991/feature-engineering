# Release-A Evaluation Record — replay half (2026-08-03)

**Status: EXECUTION RECORD.** Not a controlling document. It states what was measured, on which
code, with which numbers, and what was deliberately not measured. It is the factual basis for the
Gate-A approval package; it does not grant, imply, or request that approval.

- **Branch:** `feature/relA-evaluation`, worktree `.claude/worktrees/relA-eval`
- **Base:** `05160fcc` — the full Release-A integration (semantic Tasks 1–9, profile Tasks 1–5,
  D13, migrations 1045/1046/1047/1051/1052)
- **Owner:** the joint evaluation step — semantic plan Task 10 + profile plan Task 6, one stream
- **Mode:** REPLAY / FIXTURE only. Per the verified-interfaces doc **D9**, the live same-model
  comparison and the token/call cost measurement are **Gate B**, behind a separate approval.
- **No live LLM call, no network egress, no migration, no deploy, no upload was made.**

---

## 1. What "replay mode" means here, precisely

The evaluation drives the **real** enrichment, candidate-derivation, retrieval, feature-generation
and profile paths. What is substituted is the provider, and only the provider.

The fixture provider is **not a scripted `FakeLLM`**. A scripted fake returns the same answer
regardless of what it was shown, so a thin-versus-rich comparison against one measures nothing: both
arms score identically by construction. The provider used here (`OracleConceptProvider`) is a
**function of the request** — it reads the payload each batched item actually carried and answers
the reviewer's concept only when the disambiguating evidence is present in those bytes.

The consequence is the honest reading of every number below:

> A hit means **the platform delivered the evidence**. A miss means **it did not**.
> Nothing here is a claim about how good a model is at using it. That is Gate B.

**The thin/rich seam cost zero source changes.** `enrich_concepts` already accepts caller-supplied
`bundles`; passing `{}` makes every per-column lookup miss, and `_classifier_payload` then degrades
to exactly the historical metadata-only payload — a property its own docstring guarantees. So the
two arms differ in one input and nothing else: same rows, same glossary, same provider type, same
acceptance path, same evidence writer.

**The mode seam is real, and unused.** `build_concept_provider(mode)` returns an `LLMClient`;
`FIXTURE` returns the oracle, `LIVE` builds the real Anthropic client. Both satisfy the same
Protocol and plug into the same argument, so Gate B flips one parameter and no code is restructured.
Nothing in this branch selects `LIVE`, and two independent guards say so: `assert_no_live_dispatch`
(type check plus a build ledger) and `no_network()`, which refuses every non-loopback socket for the
duration of the suite. Loopback stays open because the test database is a real PostgreSQL — blocking
it would have proved only that the harness cannot run.

---

## 2. The gold sets

| | Semantic | Profile |
| --- | --- | --- |
| File | `tests/eval/semantic_context_gold_v1.json` | `tests/eval/catalog_profile_gold_v1.json` |
| Grain | column | table |
| Families | 10 | 6 |
| Graded items | 22 columns (+2 peer-catalog columns) | 12 datasets |
| Live-catalog dependency | none | none |

Semantic families: BIC vs CIF, internal vs external account, amount vs identifier, branch id vs
branch description, event time vs ingestion time, currency vs amount, status code vs description,
sensitive party attributes, target/leakage labels, honestly unclassified. Every family carries at
least one case **and** one counterexample; the counterexamples matter as much as the cases, because
three families grade the over-flagging direction (an ordinary segment code must not become a
protected characteristic; an ordinary presentation flag must not become a leakage target; a column
whose meaning only the rich context carries must not be dismissed as opaque).

Profile families: event fact vs snapshot fact, customer dimension vs population, system-of-record vs
replica, SCD2 vs current-only, real crosswalk vs ordinary reference, honestly-unknown tables.

The six named CIB/FTR witnesses ride as **fixture columns**, not catalog reads: `counter_party_bic`,
`counter_party_cif_id`, `actual_counter_party_amt`, `tran_crncy`, `pstd_date` (the synthesized
temporal witness the joint step introduced — no repo witness exists), `sol_desc`.

### The guard that keeps the measurement from being a fiction

`test_every_discriminator_is_rich_only` requires every discriminating term to appear in the rich
payload and **not** in the thin one. A term present in both makes every case hit in both arms; one
present in neither makes every case miss. Either way the reported number is invented.

It earned its keep on first run: `Portfolio` and `Statement` were leaking into the thin payload
through `bian_path`, which would have reported two families as measuring something when they measured
nothing. Both were repaired at source.

---

## 3. Per-family results

### 3.1 Selected-concept accuracy (thin v1 / flag-off → rich v4 / flag-on)

| Family | thin | rich | forbidden selections thin → rich |
| --- | --- | --- | --- |
| bic_vs_cif | 0/2 | 2/2 | 1 → 0 |
| internal_vs_external_account | 0/2 | 2/2 | 2 → 0 |
| amount_vs_identifier | 0/2 | 2/2 | 1 → 0 |
| branch_id_vs_description | 0/2 | 2/2 | 1 → 0 |
| event_time_vs_ingestion_time | 0/2 | 2/2 | 2 → 0 |
| currency_vs_amount | 0/2 | 2/2 | 2 → 0 |
| status_code_vs_description | 0/2 | 2/2 | 2 → 0 |
| sensitive_party_attributes | 0/3 | 3/3 | 3 → 0 |
| target_leakage_labels | 0/2 | 2/2 | 2 → 0 |
| honestly_unclassified | 2/3 | 3/3 | 0 → 0 |
| **overall** | **2/22 (9%)** | **22/22 (100%)** | **16 → 0** |

A *forbidden selection* is not merely a miss: it is the specific wrong answer the family exists to
catch — a BIC read as a CIF, a business date read as a load timestamp, a protected characteristic
read as an ordinary geography code. Sixteen of them in the thin arm, none in the rich arm.

### 3.2 Unclassified precision

| | declined | correctly declined | precision | recall |
| --- | --- | --- | --- | --- |
| thin | 6 | 2 | 0.33 | 1.00 |
| rich | 2 | 2 | **1.00** | 1.00 |

The thin arm declines four columns it should have classified. Recall is 1.00 in both arms: neither
arm ever invents meaning for a column that has none, which is the property `requires_absent` exists
to check and the one a rich context could plausibly have broken.

### 3.3 False cross-namespace candidates

Driven through the real `derive_bridge_candidates`, whose blocking key **is** the identifier
namespace, over both gold catalog sources seeded with the reviewer-expected concepts.

| | |
| --- | --- |
| candidates derived | 2 |
| must-not-pair controls checked | 6 |
| **false cross-namespace candidates** | **0** |
| positive control offered | `counter_party_cif_id ↔ cust_cif_id` (same `cif` registry) |

The positive control is load-bearing. On the first run the derivation produced **zero** candidates
of any kind — every gold column resolved to type family `other` because FTR's `type='unknown'` was
never accompanied by an attested type — and the "zero violations" number was therefore vacuous. The
control is what exposed it.

### 3.4 Grounded retrieval, and where the lift actually is

| | thin (profiles off) | rich (profiles on) |
| --- | --- | --- |
| grounded hits | 8/8 | 8/8 |
| forbidden columns offered | 4 | 5 |
| controlled expansion terms | 80 | **142** |
| leg-3 (semantic expansion) offers | 56 | **68** |

**Grounded hits saturate, and no budget makes them stop.** Re-run at `max_columns` 60 / 6 / 4 / 3,
the answer is 8/8 in both arms every time: on two tables and ~26 columns the lexical leg already
reaches every expected column, so a hit-rate lift has nowhere to come from. Asserting one would have
been asserting an artifact of the fixture size.

What the flag genuinely changes is leg 3's own contribution — the controlled expansion harvests the
table profile projections only when `FEATUREGEN_DATASET_PROFILES` is on. The flag-on term set
visibly gains the profile vocabulary (`compliance`, `event`, `fact`, `monitoring`, `posting`,
`restated`, `feeds`) that the flag-off set does not contain. That is where the release bar asserts
the direction, with the hit rate checked for non-regression beside it.

**Recorded honestly: rich offered one MORE forbidden column than thin (5 vs 4).** Widening the
expansion widens what is reachable, including columns a question should avoid. At this scale it is a
single item and the grounded hits did not move, but it is a real directional cost of the profile
harvest and it belongs in front of the Gate-A approver rather than in a footnote.

### 3.5 Profile table-selection quality

Asked of the **real, flag-sensitive consumer** (`feature_assist._profile_advisories`), which returns
`{}` with the flag off — no key, not an empty one — rather than modelled.

| | resolved | of |
| --- | --- | --- |
| profiles off | 0 | 6 |
| profiles on | **6** | 6 |

A question is *resolved* when the table it should prefer is distinguishable on at least one profile
axis from every table it must avoid. With profiles off, no question resolves: `acct_bal_eod` and
`comp_fin_tran` are indistinguishable, so "what was each account's balance at the end of last month"
has nothing to prefer on.

### 3.6 Pass-B replay through the structured-results store

11 of 11 datasets with a reviewer-accepted synthesis were recorded into the real `structured_results`
store under the real result type `table_profile_synthesis` and read back **by recomputed input
hash**, byte-identical. Zero absent, zero mismatched.

The recomputation is not a shortcut: Pass B calls `record_structured_result` and never sets a
`structured_result_current` pointer, so "the current synthesis for table X" does not exist as a
readable pointer today and a replay must go through `find_structured_result`. That is what the plan
review's "replayed structured results" item was asking for, and it is now proven end to end with no
client in the process.

---

## 4. Release bars

`tests/eval/test_release_a_bars.py` — each bar a named test, run against the real path it is a bar
on. Every zero-violations bar carries its own positive control in the same test.

| # | Bar | Result |
| --- | --- | --- |
| 1 | zero BIC↔CIF candidates on the gold set | **PASS** (0 violations, control offered) |
| 2 | zero physical facts attributed to an LLM producer | **PASS** (structural + empirical) |
| 3 | zero source/human evidence overwritten across a full re-enrichment replay | **PASS** (2 passes, byte-identical) |
| 4 | zero unsafe gold features accepted | **NOT MET — see §5** |
| 5 | no regression in grounded acceptance vs the thin baseline | **PASS** (per family and overall) |
| 6 | measurable retrieval lift | **PASS** on leg-3 contribution; hit rate non-regressed |
| 7 | no unexplained zero-output stage | **PASS** (4 stages, predicate proven able to fire) |
| 8 | zero reviewed-but-unsafe relationships displayed executable | **PASS** (both halves) |

Bar 2 is asserted twice over: **structurally**, the fields Pass B may propose are disjoint from every
physical assertion the gold names, so an LLM cannot reach one even in principle; **empirically**, no
stored synthesis and no `field_evidence` row with `producer='llm'` names one.

Bar 8 is likewise two halves: an APPROVED link whose stored realization claims `production_eligible`
must still read `executable_now = false`, and a revalidating reader that *raises* must degrade to
"nothing is executable" while leaving the transaction usable.

Three further bars were added **after** the mutation harness proved their invariants untested
(§6 †). They are bars in their own right and are listed here for completeness:

| Bar | Result |
| --- | --- |
| `test_bar_data_role_and_authority_role_stay_two_questions` | **PASS** |
| `test_bar_a_catalog_narrative_never_defaults_a_dataset_authority` | **PASS** |
| `test_bar_business_context_moves_the_dataset_profile_hash` | **PASS** |

`tests/eval/test_release_a_bars.py` — **14 passed, 1 xfailed** (the xfail is bar 4).

---

## 5. Bar 4 — the finding

**Of the five unsafe gold classes, the platform refuses exactly one.**

| Gold feature | Class | Disposition |
| --- | --- | --- |
| `sar_filed_rate_90d` | target leakage | **rejected**, `LEAKAGE` |
| `customer_dob_bucket` | PII as an input | accepted, `DESIGN_CHECKED`, 0 requirements |
| `citizenship_default_propensity` | protected characteristic as an input | accepted, `DESIGN_CHECKED`, 0 requirements |
| `total_amount_all_currencies` | currency-blind sum of a monetary flow | accepted, `DESIGN_CHECKED`, 0 requirements |
| `branch_description_join_key` | free-text description as a join key | accepted, `DESIGN_CHECKED`, 0 requirements |
| `settled_amount_sum_by_currency_30d` | *safe control* | accepted, `NEEDS_EXTERNAL_VALIDATION`, 4 requirements |

Two things worth separating:

- **Read scope behaved correctly.** `cust_dob` is tagged `pii` and `cust_ctzn_ctry_cd` `restricted`;
  they were visible because the caller held `pii_reader` and `restricted_reader`. Sensitivity gates
  **visibility**. It does not gate **use** — nothing stops a visible protected characteristic from
  becoming a model input.
- **No Release-A task owns that gate.** Neither plan scopes feature-generation safety beyond target
  leakage, so this is not a regression introduced by Release A; it is a pre-existing gap that this
  evaluation is the first thing to measure.

**How it is encoded, and why.** The bar is split. The leakage half is a real, passing bar
(`test_bar_zero_unsafe_gold_features_accepted_leakage_class`). The full bar is a **strict xfail**
naming all four classes. Strict is the point: the day the gate lands, that test *fails* and someone
must promote it. The alternative — relabelling the gold to match what the platform does — would have
turned an evaluation into a mirror.

**This is a decision for the Gate-A approver, not for this document.**

---

## 6. Mutation harness

Built here; it did not exist anywhere on main. Bridge Task 12 owed it and never landed, and the
plan review (§A9) records that neither plan allocated it. Nobody may cite it as pre-existing.

**One command:** `uv run --extra dev pytest -q -m eval tests/eval/mutation/`

Design, and the reason for each choice:

- **Subprocess, not in-process monkeypatch.** A mutation must be installed before the victim module
  is imported and gone afterwards. A subprocess gives both and cannot leak a patched module into a
  later test.
- **The parent's DSN is handed down** (`FEATUREGEN_TEST_DSN`). The root conftest boots an ephemeral
  PostgreSQL cluster per session and applies migrations; letting 17 subprocesses repeat that would
  cost minutes. Victims still roll their own writes back.
- **The collected count is asserted on every run.** `addopts` carries `-m 'not eval'`, so a renamed,
  moved or eval-marked victim would be *deselected* and exit 0 — and a mutation would then look like
  it survived nothing while the harness reported a pass. `assert_ran` refuses a run that collected
  nothing, and the child clears the marker filter with `-m ""` because some victims (the release
  bars) are deliberately eval-marked.
- **Mutations patch the CONSUMER, not the producer,** wherever a symbol travels by
  `from X import Y`. Patching the producer would be a mutation that mutates nothing and would then
  kill its victims for no reason — or, worse, kill nothing and read as a survival.
- **Nothing touches `src/`.** Every mutation is a runtime patch installed by a pytest plugin in a
  throwaway process.

### Results

**16 / 16 must-die mutations killed. 1 / 1 must-survive control survived. 0 dropped.**
Suite: `20 passed in 27.84s` (16 must-die + 1 control + 2 registry meta-tests + the count baseline).

| # | Mutation | Invariant broken | Killed |
| --- | --- | --- | --- |
| 1 | `vocab_fingerprint_drops_hint` | a reworded concept meaning re-keys the classifier cache | yes |
| 2 | `issuer_leaves_namespace_identity` | a scheme names a value space only WITHIN an issuer | yes |
| 3 | `entity_reenters_the_blocking_key` | entity never gates identifier pairing | yes |
| 4 | `accept_off_registry_concept` | only a registry concept or `unclassified` is accepted | yes |
| 5 | `disable_stale_value_retirement` | a superseded LLM proposal is retired, not left live | yes |
| 6 | `feature_gen_reads_the_thin_menu` | flag on ⇒ v4, never the v1 thin menu | yes |
| 7 | `drop_per_kind_truncation_reporting` | the context section says WHICH kinds it omitted | yes |
| 8 | `review_status_implies_executable` | a review badge is never a permission | yes |
| 9 | `collapse_data_and_authority_role` | what a dataset IS ≠ how authoritative its copy is | yes † |
| 10 | `catalog_authority_inherits_onto_a_dataset` | a catalog narrative never defaults a dataset's authority | yes † |
| 11 | `llm_authority_becomes_load_bearing` | an LLM proposal is displayed, never load-bearing | yes |
| 12 | `search_drops_profile_context` | profile classifications are facetable with the flag on | yes |
| 13 | `feature_gen_drops_profile_context` | the feature-context block carries the profile advisories | yes |
| 14 | `retrieval_drops_profile_context` | leg 3 harvests the table profile with the flag on | yes |
| 15 | `graph_projection_read_as_authority` | eligibility comes from the decision log, not the projection | yes |
| 16 | `profile_hash_omits_business_context` | every meaning-bearing field moves `dataset_profile_hash` | yes † |
| — | `noop_reorder_registry_declarations` | *(must survive)* declaration/dict order is not meaning | survived |

**Nothing was dropped.** Every mutation the two plans named maps onto a symbol that exists in the
as-built code and was verified against it before the mutation was written. Where a plan named a
mutation the as-built code expresses differently, the registry records the adaptation rather than
silently substituting one: "remove description from the vocabulary fingerprint" is implemented as
dropping the **hint** because that is precisely what the fingerprint hashes (the first sentence of
`description`, truncated to 120 chars) — the full description is not in it, and a mutation claiming
otherwise would have been testing a fiction.

### † The three mutations that SURVIVED on first run — and what that revealed

These are the harness's real product. In each case the invariant was claimed by the plans and by the
module docstrings, and **nothing in the entire pre-existing suite noticed when it stopped holding.**

| Mutation | What the existing tests actually covered | The gap |
| --- | --- | --- |
| `collapse_data_and_authority_role` | that an EMPTY table is undecided everywhere, and that legacy `bridge` displays as `crosswalk` | nothing asserted that a table whose `table_role` **is** source-attested still reports its AUTHORITY as undecided — one axis answering both questions would have shipped unseen |
| `catalog_authority_inherits_onto_a_dataset` | that an UNKNOWN narrative key is refused | that says nothing about what happens once a key becomes *known*. The outcome — narrative authored, dataset authority still undecided — was untested |
| `profile_hash_omits_business_context` | `definition`, `authority_role`, a governed fact head, the narrative revision | every meaning-bearing input **except** `business_context` |

Each gap is now closed by a named bar in `tests/eval/test_release_a_bars.py`
(`test_bar_data_role_and_authority_role_stay_two_questions`,
`test_bar_a_catalog_narrative_never_defaults_a_dataset_authority`,
`test_bar_business_context_moves_the_dataset_profile_hash`), and each is that mutation's victim, so
the mutation now dies against a test that exists because it survived.

The bars live in this evaluation's own file rather than in another stream's test module: ownership
stays unambiguous and there is no cross-stream merge conflict. The four adjacent profile suites
(`test_dataset_profiles`, `test_catalog_profiles`, `test_profile_consumption`, `test_context_graph`)
were re-run untouched — **100 passed**.

One further mutation, `llm_authority_becomes_load_bearing`, initially failed to *apply* at all
(`AnyOf` exposes `conditions`, not `options`). The harness reported it as "collected no tests"
rather than as a pass — which is exactly the failure mode `assert_ran` exists for, since a mutation
that silently does not apply would otherwise report every victim as surviving and read as a green
gate.

---

## 7. Test-count baselines — NAMED suites only

Per D9, the literal-count gate is scoped to named focused suites and never to the whole repository,
whose ~82 order/environment-dependent failures are recorded in `DEFERRED-WORK` §C.

| Suite set | Count |
| --- | --- |
| The Task-0 seventeen (`RELEASE_GATE_SUITES`) | **269** (Task-0 baseline was 238; Release-A integration added 31) |
| `tests/eval/test_gold_sets_are_consistent.py` | 60 |
| `tests/eval/test_release_a_eval.py` | 4 |
| `tests/eval/test_release_a_bars.py` | 15 (14 passed + 1 strict xfail — bar 4) |
| `tests/eval/mutation/` | 20 |

Regression gates run for this step, all green with `pipefail`: the seventeen above (**269 passed**),
the four eval suites (**60 + 38 passed, 1 xfailed**), and the whole `tests/featuregen/overlay/upload`
half (**3838 collected, exit 0**). This branch ADDS 14 files and modifies none — `git diff
--name-only 05160fcc..HEAD` is additions only, and `src/`, `frontend/`, `deploy/` and every
controlling document are untouched.

`test_the_named_release_gate_suites_hold_their_literal_count` asserts the 269 in code. A **drop** is
a deleted guard and must be explained; a rise is fine but must be rebaselined deliberately.

---

## 8. Explicitly deferred to Gate B

Per D9 these are **not** pre-gate deliverables and were not attempted:

1. **Live same-provider/model thin-vs-rich comparison.** Everything in §3.1–3.2 measures context
   delivery, not provider skill. Whether a real model uses the delivered evidence correctly is
   unmeasured here.
2. **Real token and provider-call cost.** No call was made, so no cost exists to report. Note the
   standing concern recorded in `feature_assist`: v4 sends roughly 2.2× the v3 prompt bytes on the
   measured catalogs, and the 300 KB context budget bounds assembly, not spend.
3. **Live retrieval lift against a real index.** §3.4 saturates on a 2-table fixture; only a real
   catalog can show whether the profile harvest lifts grounded hits at scale.
4. **Ontology-gap usefulness judgement.** Requires a live adjudication run.
5. **The Gate-B witness reconciliation** (`counter_party_bic`, `counter_party_cif_id`,
   `actual_counter_party_amt`, `cust_swift_cd`, `cust_num`, `cif_id`, `sol_desc` in the UI) and the
   full-re-enrichment cost of the vocabulary-fingerprint change recorded in D12.1.

## 9. Findings for the Gate-A approver

1. **Bar 4 is not met** (§5). Four unsafe classes are accepted as design-checked. A decision is
   needed: accept as a known gap with a tracked follow-up, or block.
2. **The profile expansion widens what retrieval offers, including one column a question should
   avoid** (§3.4). Small at this scale; direction worth watching at catalog scale.
3. **Pass B sets no `structured_result_current` pointer.** Replay works by recomputed input hash
   (§3.6), which is sound, but "the current synthesis for this table" is not a readable concept
   today. Not a Release-A blocker; named so it is not discovered later as a surprise.
4. **Grounded retrieval cannot be measured for lift on a fixture this size** (§3.4). The Gate-B
   run is the first opportunity to measure it honestly.
5. **Three claimed invariants were untested until this step** (§6 †): data-role/authority-role
   independence, catalog→dataset authority non-inheritance, and `business_context` moving
   `dataset_profile_hash`. All three are now closed by named bars. No defect was found in the
   PRODUCT for any of them — the code was correct; only the evidence that it was correct was
   missing. Worth knowing which claims in the Gate-A package rested on nothing until today.

## 10. Reproducing this record

```sh
uv run --extra dev pytest -q -m eval tests/eval/test_release_a_eval.py -s   # the numbers in §3
uv run --extra dev pytest -q -m eval tests/eval/test_release_a_bars.py      # §4
uv run --extra dev pytest -q -m eval tests/eval/mutation/                   # §6 and the §7 baseline
uv run --extra dev pytest -q tests/eval/test_gold_sets_are_consistent.py    # the gold-set guards
```

The eval run writes a timestamped JSON report to `tests/eval/reports/` (a run artifact, git-ignored).
