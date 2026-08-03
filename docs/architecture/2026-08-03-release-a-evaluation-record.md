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
- **Revised 2026-08-03 after adversarial review.** Two of this evaluation's own instruments were
  unfalsifiable as first submitted; both are fixed and the corrections are itemised in **§11**. No
  measurement in §3 moved.

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

**What 22/22 does and does not say — read this before quoting the number.** The rich arm's perfect
score is not a surprising empirical result. It is *entailed* by how the gold guard and the oracle are
built: `test_every_discriminator_is_rich_only` requires every discriminating term to be present in
the rich payload and absent from the thin one, and the oracle answers the reviewer's concept exactly
when it finds that term in the bytes. So **given** that the pipeline assembles the v4 payload the
gold describes, 22/22 follows by construction — and a score below 22 would mean the guard and the
oracle contradict each other, not that a model got something wrong.

The information the measurement adds is therefore the antecedent, not the score: the **real**
`enrich_concepts` path, over real bundles, actually delivered those bytes for all 22 columns, and the
same machinery delivered almost none of them (2/22) when the bundle was withheld. It is a **payload
delivery** check with a matched negative arm. It is not a model-accuracy claim, and nothing in §3.1
or §3.2 may be quoted as one. Provider skill is Gate B (§8).

### 3.2 Unclassified precision

| | declined | correctly declined | precision | recall |
| --- | --- | --- | --- | --- |
| thin | 6 | 2 | 0.33 | 1.00 |
| rich | 2 | 2 | **1.00** | 1.00 |

The thin arm declines four columns it should have classified. Recall is 1.00 in both arms: neither
arm ever invents meaning for a column that has none, which is the property `requires_absent` exists
to check and the one a rich context could plausibly have broken.

### 3.3 False cross-namespace candidates

**Exactly what is checked.** Both gold catalog sources (`ftr.comp_fin_tran`, `cib.cust_master`) are
seeded with the reviewer-expected concepts and put through the real `derive_bridge_candidates`, whose
blocking key **is** the identifier namespace. Every graded control names two columns that sit in
**different catalog sources** and belong to **disjoint identifier namespaces**; a candidate carrying
such a pair is a violation.

| | gate as shipped | namespace gate collapsed |
| --- | --- | --- |
| candidates derived | 2 | **10** |
| graded (cross-source) must-not-pair controls | 8 | 8 |
| **false cross-namespace candidates** | **0** | **8 of 8** |
| positive controls offered | `counter_party_cif_id ↔ cust_cif_id`, `counter_party_bic ↔ cust_swift_cd` | both |
| intra-source pairs offered | 0 | 0 |

**Two controls, because a zero on its own is not a pass.**

*Positive.* Both same-namespace pairs must be **offered**. On the first run of this harness the
derivation produced zero candidates of any kind — every gold column resolved to type family `other`
because FTR's `type='unknown'` carried no attested type — and the zero was vacuous. That control is
what exposed it.

*Reachability (added 2026-08-03, after review).* The right-hand column above is the bar's denominator.
`Concept.namespace` is the blocking key, so collapsing it to one literal removes the gate and changes
nothing else — same grounding, same type families, same source distinctness, same hard-conflict
suppression. Under that injection **all eight** controls are offered as candidates. The zero on the
left is therefore the gate's doing and not the fixture's shape, and the bar is demonstrably capable of
failing — the same standard the mutation harness holds itself to. It is asserted, not merely
recorded, by `test_bar_one_controls_are_reachable_only_the_namespace_gate_stops_them`.

**The controls were reachable only after they were rewritten.** Until the review, all six controls
named two columns of the *same* table. `_derive_from_identifier_columns` enumerates
`combinations(sources, 2)` — it can only ever offer a **cross-source** pair — so those six were
refused by the topology before the namespace gate was ever consulted, and "zero violations" was
guaranteed regardless of what the gate did. The graded list is now eight cross-source wrong-scheme
pairs drawn from both fixture catalogs (`cust_swift_cd ↔ counter_party_cif_id`,
`cust_cif_id ↔ counter_party_bic`, and six more against `dr_acct_num`, `benef_acct_num`, `sol_id`).

The original six are **kept and relabelled** `unreachable_by_topology` rather than deleted or quietly
regraded: they are still forbidden pairs, they are simply refused one layer earlier than this bar is
about. Gold guards assert they really are intra-source, that no pair appears in both lists, and that
they stay absent even with the gate removed.

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
| 1 | zero BIC↔CIF candidates on the gold set | **PASS** (0 of 8 cross-source controls violated; both positive controls offered; reachability proven — §3.3) |
| 2 | zero physical facts attributed to an LLM producer | **PASS** (structural + empirical) |
| 3 | zero source/human evidence overwritten across a full re-enrichment replay | **PASS** (2 passes, byte-identical) |
| 4 | zero unsafe gold features accepted | **NOT MET — see §5** |
| 5 | no regression in grounded acceptance vs the thin baseline | **PASS** (per family and overall) |
| 6 | measurable retrieval lift | **PASS** on leg-3 contribution; hit rate non-regressed |
| 7 | no unexplained zero-output stage | **PASS** (4 stages, predicate proven able to fire) |
| 8 | zero reviewed-but-unsafe relationships displayed executable | **PASS** (both halves) |

Bar 1 carries a second named test rather than a footnote:
`test_bar_one_controls_are_reachable_only_the_namespace_gate_stops_them` collapses the namespace —
the blocking key — and requires all eight controls to appear. A bar whose controls cannot be reached
is not a passing bar; it is an unasked question, which is what bar 1 was until the review (§3.3,
§11).

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

`tests/eval/test_release_a_bars.py` — **15 passed, 1 xfailed** (the xfail is bar 4).

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
- **A kill is the EXPECTED failure, not any failure** (added 2026-08-03, after review). Each must-die
  entry records `expect_failure_contains` — the victim's own assertion message, or the failing source
  line as pytest renders it, taken from a real mutated run — and the mutated run must contain it AND
  report a NAMED victim among its failures. Scoring on exit code alone counted a broken import or a
  malformed query as a caught invariant. See §11 for what that immediately caught.
- **Nothing touches `src/`.** Every mutation is a runtime patch installed by a pytest plugin in a
  throwaway process.

### Results

**16 / 16 must-die mutations killed, each with the failure the registry expects. 1 / 1 must-survive
control survived. 0 dropped.**
Suite: `24 passed in 34.6s` (16 must-die + 1 control + 3 registry meta-tests + 3 per-eval-suite
counts + the release-gate count baseline).

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
| 14 | `retrieval_drops_profile_context` | leg 3 harvests the table profile with the flag on | yes ‡ |
| 15 | `graph_projection_read_as_authority` | eligibility comes from the decision log, not the projection | yes |
| 16 | `profile_hash_omits_business_context` | every meaning-bearing field moves `dataset_profile_hash` | yes † |
| — | `noop_reorder_registry_declarations` | *(must survive)* declaration/dict order is not meaning | survived |

Every "yes" above now means **killed by the failure the registry expects**, named per mutation and
taken from a real mutated run — not merely "the run went red". ‡ marks the one mutation that had to
be rewritten before it could earn that (§11).

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

| Suite set | Count | Asserted in code by |
| --- | --- | --- |
| The Task-0 seventeen (`RELEASE_GATE_SUITES`) | **273** (Task-0 baseline was 238; Release-A integration added 31 = 269; the Track-2 merge added 4 first-hop cardinality tests to `test_joins.py` — rebaselined deliberately 2026-08-03) | `test_the_named_release_gate_suites_hold_their_literal_count` |
| `tests/eval/test_gold_sets_are_consistent.py` | 63 | `test_the_eval_suites_hold_their_literal_count` |
| `tests/eval/test_release_a_eval.py` | 4 | ″ |
| `tests/eval/test_release_a_bars.py` | 16 (15 passed + 1 strict xfail — bar 4) | ″ |
| `tests/eval/mutation/` | 24 | — (it is the runner) |

Regression gates re-run after the §11 corrections, all green: the seventeen above (**269 passed** at the tagged candidate; **273** on the post-Track-2-merge tree, see the rebaseline note above),
the four eval suites (**63 + 43 passed, 1 xfailed**), and the whole `tests/featuregen/overlay/upload`
half (**3838 collected — 3827 passed, 11 skipped, exit 0**). This branch ADDS 15 files and modifies
none — `git diff --name-only 05160fcc..HEAD` is additions only, and `src/`, `frontend/`, `deploy/`
and every controlling document are untouched.

Both baselines are now **asserted**, not merely written down. `EVAL_SUITES` carried its three counts
as an unread literal until the review — a claim nobody checked — and is now held to the same rule as
the seventeen, one parametrized case per suite. A **drop** is a deleted guard and must be explained;
a rise is fine but must be rebaselined deliberately.

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
6. **Two of this evaluation's own instruments were unfalsifiable when first submitted** (§11): bar 1
   graded controls the derivation could not offer, and the mutation gate counted any failure as a
   kill. Both are fixed and both zeros are now backed by a control that makes them capable of
   failing. The general point is worth carrying into Gate B: for every "zero" in this record, ask
   what was shown to produce a non-zero.

## 10. Reproducing this record

```sh
uv run --extra dev pytest -q -m eval tests/eval/test_release_a_eval.py -s   # the numbers in §3
uv run --extra dev pytest -q -m eval tests/eval/test_release_a_bars.py      # §4
uv run --extra dev pytest -q -m eval tests/eval/mutation/                   # §6 and the §7 baseline
uv run --extra dev pytest -q tests/eval/test_gold_sets_are_consistent.py    # the gold-set guards
```

The eval run writes a timestamped JSON report to `tests/eval/reports/` (a run artifact, git-ignored).
Its `cross_namespace` and `cross_namespace_reachability_control` blocks are the two columns of §3.3,
produced by the same execution.

---

## 11. Corrections after review (2026-08-03)

The first submission of this record was reviewed adversarially. Five items came back; two of them
were findings about the **evaluation itself** rather than about the platform, and both are the kind
that make a green result mean less than it appears to. They are recorded here rather than folded
away, because a Gate-A reader is entitled to know which numbers changed after someone pushed on them.

**1. Bar 1 could not fail (major).** All six must-not-pair controls named two columns of the same
table, and `derive_bridge_candidates` only ever enumerates pairs across *distinct* catalog sources.
The controls were therefore refused by the topology, not by the namespace gate, and
`false_cross_namespace_candidates == 0` was guaranteed whatever the gate did — including if it were
deleted. The reviewer demonstrated it by collapsing every namespace: candidates rose 2 → 10 and the
violation count stayed 0.

Fixed by regrading the bar on eight **cross-source** controls drawn from both fixture catalogs, adding
a second positive control, keeping the original six as an honestly-labelled `unreachable_by_topology`
list, and adding a reachability control that reproduces the reviewer's injection as a named test:
with the gate collapsed, **all eight** controls are offered. §3.3 states what is checked; the bar's
verdict did not change, but it now means something.

**2. The mutation gate scored any failure as a kill (minor).** `test_the_mutation_is_caught` required
only that the mutated run go red. A mutation that broke an import, malformed a query or upset an
unrelated fixture would have been reported as a caught invariant. Each must-die entry now records the
failure it expects, taken from a real mutated run, and a named victim must be among the failures.

It caught one immediately. `retrieval_drops_profile_context` emptied `_TABLE_PROFILE_COLUMNS`, which
turned leg 3's harvest into `SELECT catalog_source, table_name,  FROM graph_node` — both victims died
of `psycopg.errors.SyntaxError`, having never reached the question. The mutation now projects a
literal `NULL`: the statement stays well-formed, the rows are read, and the victims fail on the
invariant itself. **The 16/16 kill count is unchanged; what changed is that it is now 16/16 for the
right reasons.**

**3. `EVAL_SUITES` was an unread literal (minor).** Three per-suite counts sat in the mutation module
with nothing asserting them. Now asserted, and rebaselined (§7).

**4. This record said the branch adds 14 files (minor).** It adds 15. Corrected in §7.

**5. The 22/22 could be over-read (minor).** §3.1 now states plainly that the rich arm's perfect score
is *entailed* by the leak-guard and the oracle construction, and that the measurement's actual content
is the antecedent — that the real pipeline delivered the payload — with the thin arm's 2/22 as the
matched negative.

All numbers in §3 were re-measured after these changes and are unchanged: thin 2/22, rich 22/22,
16 → 0 forbidden selections, unclassified precision 0.33 → 1.00, grounded hits 8/8 both arms,
expansion terms 80 → 142, leg-3 offers 56 → 68, table selection 0 → 6 of 6, Pass-B replay 11/11.
