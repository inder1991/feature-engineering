# Serving-Quality Remediation — the Owner Package

*Closing document, 2026-08-25. Branch `feature/serving-quality-remediation`, head `7f339ce5`,
base `463498ed` (main). The program plan is
`docs/superpowers/plans/2026-08-24-serving-quality-remediation.md`; every verdict and ruling is in
`.superpowers/sdd/2026-08-24-serving-quality-remediation/progress.md`. This package is what an
owner needs to decide the merge, run the deploy, and work the decision backlog.*

---

## 1. What this program was

Your AML brief ran and produced 135 feature cards so poor that a subject-matter review would keep
zero of them — planned over the wrong catalog, wearing design-review badges nothing had earned,
with empty rationales, a proxy target proposed as if it were the label, and all eight LLM intents
rejected by a schema/parser contract bug. Eleven tasks (T1–T9, T11) fixed the distinct causes;
T10 replays your exact journey as a permanent test. Every task was adversarially reviewed by a
fresh reviewer with execution-verified findings and fix rounds; nothing was accepted on argument.

The one law under all of it: **the platform must never present confidence it does not have.**

## 2. What each task fixed, in one line each

| Task | In plain English |
| --- | --- |
| T1 | The LLM's feature ideas were all thrown away on a technicality (schema said "any words", parser demanded exact vocabulary). Now the vocabulary is declared on the wire, near-misses are normalized, and genuine gaps are recorded as `INTENT_VOCABULARY_GAP` instead of silently binned. |
| T2 | Recipes that can't compute on your catalog are no longer served as cards anyway. They move to a `needs_setup` lane where each one says, per operand and in the binder's own words, exactly what is missing or refused and how to fix it. |
| T3 | The `DESIGN-CHECKED` badge now appears only when the registry actually certifies a reviewed formula (3 of 317 recipes today). Everything else honestly reads `UNVERIFIED`. The badge returns by itself as recipes earn review. |
| T4 | Cards carry a real rationale — the recipe's own authored business definition and decision context — instead of the empty field the old code read. |
| T5 | A brief aimed at a catalog that cannot satisfy it is refused **before** anything is minted, and the refusal gives directions: it names the concepts required and the catalog in your estate that carries them (your run would have been told "aim at ftr"). If *no* catalog carries a concept, the run proceeds and the absence is reported per card — a refusal with nowhere to point is not issued. |
| T6 | 135 cards collapse to at most one per recipe (43). Parameter siblings are named on the card (`window: 30/[90]/180`) instead of being minted as near-duplicate cards. |
| T7 | A proxy target (like `cust_susp_flg`, which the registry marks label-*adjacent*) is never silently confirmed. The platform abstains, names the proxy for what it is, types the window contradiction, and confirming requires an explicit acknowledgment that is then recorded. |
| T8 | A recipe can no longer bind its own population key onto a counterparty leg — "a party is never its own counterparty" — refused structurally, with the refused column named. |
| T9 | The frontend stops masking every 503 as "AI assist is not configured" and renders the server's real sentences verbatim, including the new needs-setup lane and refusal directions. |
| T10 | Your journey, replayed as 14 permanent tests: the wrong-catalog refusal, the broaden path, the honest cards, the target story, the depth-blocked case — and the one known-open gap pinned (§5). |
| T11 | The provider outage (HTTP 400 on every formula-authoring call) is fixed: 46 bare enums across three schema generations now declare their types, an independent audit walks what the old ratchet never covered, and the whole thing is pinned as an identity-moving act. |

## 3. Final numbers (independently verified by the T10 reviewer's own runs)

- **Backend:** 14,059 passed / 20 skipped / 0 failed = 14,079 selected, from a clean checkout,
  reconciled chunk-by-chunk against a whole-tree collection of 14,079 (82 deselected = `tests/eval`).
- **Baseline reconciliation, by test name:** true pre-program baseline `89364fac` = 13,990 selected
  → head 14,079: **+91 new / −2 removed = +89 net**, journey exactly +14, every file attributed.
  (An earlier report anchored this at the wrong SHA; the reviewer found and corrected the real
  cause — the ledger's figure had been measured on the two-commit T11 set. Details in the ledger.)
- **Frontend:** typecheck clean; 1,115 tests passing (unchanged — the program added its frontend
  pins inside existing suites).
- **Ruff:** 56 pre-existing repo findings at both ends; the program adds zero.
- **Zero new migrations. Zero new flags. All 20 skips environment-gated (Hive driver, local CSVs,
  live-LLM config), none functional.**
- **Review depth on the closer:** T10's reviewer re-ran every gate, ran the api suite with test
  randomization on, and executed **11 mutations** against the platform to prove every journey pin
  actually discriminates — including a simulated grain-law fix that turns the known-open pins red
  with their inversion message on screen.

Reproduction trap, for anyone re-counting: one planner test runs `git merge-base HEAD origin/main`
at module import — a checkout without `origin/main` fetched silently collects 14 fewer tests.

## 4. The deploy plan, with its written obligations

**Sequence:** (1) your merge word for `feature/serving-quality-remediation` → main; (2) your
explicit deploy approval; (3) one `deploy/kind/deploy.sh` run. No migrations run, no flags change.
The script ships backend and frontend images together, which satisfies the one ordering obligation
(T7's refusal names a field only T9's frontend knows how to render — they must land together, and
one deploy run does that by construction).

**What operators will see change, day one:**

1. **Badges read humbler.** `DESIGN-CHECKED` is reachable for exactly 3 of 317 recipes; essentially
   every engine card reads `UNVERIFIED`. This is the audit's headline defect rendered honestly, not
   a regression, and it self-heals as recipes earn reviewed formula expectations.
2. **Mis-aimed briefs get a 422 with directions** (`CATALOG_CANNOT_SATISFY_SCOPE`) naming the
   catalog to aim at, and nothing is written behind the refusal. An estate-wide absence is *not*
   refused — the run proceeds and `needs_setup` reports it per card.
3. **Card counts drop.** One card per recipe, siblings named on the card. The honest cost, written
   in the plan: when a hypothesis names a window the source's declared history cannot cover, the
   platform now serves **zero** cards where the old code served a silently-different sibling
   window. The refusal names the clock and both remedies. Making the named alternatives clickable
   is the chartered-but-unbuilt answer (backlog item 9).
4. **T11's identity move has operator steps** (the only non-benign consequence set in the program):
   - Sealed shadow work items report `ConfigurationDrifted` — **re-seal them**.
   - In-flight regeneration coupons are permanently unredeemable (fail-closed, uses unburned) —
     **re-approve the spend**.
   - **Exact-draft tombstones silently stop covering the LLM lane** (by 1103's design). An operator
     who retired an exact draft will find it re-draftable with no notification — this note is the
     notification. Candidate-wide tombstones hold.
   - Existing FAILED drafts become re-mintable (their failures were caused by the defect now
     fixed) — a real spend-authorization consequence to be aware of.
5. **T2-T4's and T6's option-identity moves are benign** — option ids are minted per generation
   run and never span runs; old revisions verify against their own stored identities. No action.

**Rollback:** no schema changes means image-level rollback suffices; the standing snapshots at
`~/featuregen-backups/` are untouched by this program.

## 5. The one known-open defect, pinned as known-open

`new_counterparty_flag` (an account-anchored recipe) still binds the customer master's own grain
key onto its counterparty leg and serves a card — a feature that would compute "new counterparty"
against the bank's own customer list. T8 correctly declines (different entities), T2 correctly
declines (nothing is unbound); the missing axis is **grain** — nothing compiles a table's shape
(per-customer master vs per-event table) into something the binder can consult, so
`SOURCE_GRAIN_MISMATCH` exists as a code that cannot fire. Measured: 6 account-anchored
`customer_id` legs share this exposure, 2 recipes serve on the journey's fixture.

Two `test_KNOWN_OPEN_*` tests pin today's wrong behaviour positively. The reviewer *executed* a
faithful grain-law fix and both went red with their instruction on screen: **invert the pin, never
relax it**. Building the grain law ("compile the `table_shape` axis") is backlog item 8, and the
mutation run established that only the grain law closes the serving path — widening T8's role
protection alone provably does not.

## 6. Your decision backlog

Nothing here blocks the merge. Ordered roughly by cost.

**Zero-cost / minutes:**
1. **"capture the inventory"** — run the capture against the cluster so the materialization worker
   stops erroring once per second (`FEATUREGEN_MATERIALIZE_INVENTORY`; the refused template sits at
   `conf/environments/hdfc-local-inventory.yml`).
2. **Flip the telemetry flag** (`FEATUREGEN_INTENT_SHADOW_TELEMETRY` — tables exist, 0 rows).
3. **Re-run your AML brief aimed at `ftr`** — best done post-merge; the refusal will now aim you
   there anyway.

**Small backend/product decisions:**
4. `target_is_proxy` as a durable column (needs a migration — the 0976 CHECK can't carry it).
5. The `_ACKNOWLEDGE` wire wording (75-char instruction) — soften or keep.
6. `intent_normalizations` surfaced to the UI (one-line backend emission + a render follow-up),
   plus the small recorded seam items: outcome-union 403 detail threading, five 409 CAS masks, the
   unguarded OUTCOMES case on MaterializationRunScreen (latent), month-to-days convention.
7. `counterparty_id` alias registry decision (retired alias currently canonicalizes to
   `customer_id` — the honest fix interacts with item 8).

**Roadmap builds:**
8. **The grain law** — compile `table_shape`; closes §5, flips the two pins to refusals.
9. **Make `param_alternatives` actionable** — the chartering reason is written in the plan's T6
   consequences (the zero-cards-where-a-sibling-worked case).
10. **Cross-catalog Stage 2** — gated charter exists; Stage 1 is live.
11. **Vocabulary extension** (categorical / rating_scale / monetary_change operand classes).

**SME / data work:**
12. SME concept session — `booking_status` alone unlocks ~25 recipes; corridor + alert families.
13. Re-review of the 94 bulk-confirmed concepts (three proven poison cases came from that batch).
14. Real AML review labels (until then, every target in this domain is a proxy and will say so).
15. Money-law ratification and the stray 949-line document's authorship (both standing from the
    run-spine program).

## 7. What closes the program

Your word **"merge"** for `feature/serving-quality-remediation` → main (fast-forward-clean from
`463498ed`; if main has moved I'll rebase-verify first, per the origin/main law). Deploy remains a
separate explicit approval afterwards, per standing rule.
