# Charter corrections (verified against feature/phase-g @ 3b0b7b01)

## C1 — "ExecutionTier exists; the run path must honor it" (charter §Design-first, decision 4)

Half true, and the half that's false changes the decision.

- `ExecutionTier` DOES exist: `src/featuregen/overlay/upload/bridge_realization.py:81-83` (`StrEnum`, members `SANDBOX = "sandbox"`, `PRODUCTION = "production"`).
- But it is a **bridge-realization applicability** concept — it scopes whether a cross-catalog join realization is approved for sandbox or production use (`RealizationApplicabilityScopeV1`), consumed by `overlay/upload/bridge_store.py:47` and the bridge tests.
- `src/featuregen/materialize/` **never imports it** (grep: zero hits). The run path has no tier concept at all:
  - `binding.py:53-56` — "§7's ONE namespace. There is no production path in this slice" → `SANDBOX_NAMESPACE = "sandbox_feature"`.
  - `identity.py:28-34` — `derive_namespace()` takes no parameters *by design*; "There is no production execution hash, and the reduced identity is never recorded as..."
  - `physical_target_for()` (`binding.py:66`) hardcodes the sandbox namespace into every published target.

**Consequence for decision 4:** there are TWO different tier notions and the plan must not conflate them.
(a) *Realization applicability tier* — governance: is this bridge approved for production data? Exists, enforced, Track-1-adjacent.
(b) *Run execution tier* — does this run publish to a sandbox or a production namespace? Does NOT exist; the code states its absence deliberately and threads sandbox-ness into `sandbox_execution_hash` itself.

So decision 4 is not "honor the existing tier" — it is "does Phase G introduce a run execution tier at all, and if so, does introducing it fork the execution identity (because the sandbox name is baked into the hash) and does it need to be reconciled with the realization applicability tier?" That is a materially harder question than the charter frames, and it is why the recommendation lands where it does.

## C3 — "Branch FROM worktree-codegen-review-remediation, NOT origin/main (main lacks Track 2)" (charter §Baseline)

**True when the charter was written; false by the time it was handed over — because this session merged Track 2 to main minutes earlier, with the user's approval.**

- `main` is now `f3424c36` = "Merge branch 'worktree-codegen-review-remediation'" (pushed 2026-08-03, this session, after a 7588-green merged-tree suite).
- `feature/phase-g` @ `3b0b7b01` is therefore a **strict ancestor of main**: 13 commits behind, 0 ahead.
- So the two candidate baselines are no longer "with Track 2" vs "without" — both have Track 2. The real difference is that `main` ALSO carries the 12 Track-1 commits already merged there (attested data types, entity map v0 + its route/read-model, namespace-pairing bridges, the ULID occurred_at fix).

**This is a coordination decision for the user, not mine to take**, because it interacts with two things I do not own: (a) whether the Gate A tag is cut from main or from the integration branch — if from main, main now already contains Track 2, which may or may not be what "exactly the reviewed Release A work" was meant to mean; (b) the charter's rule that Track 1 owns the integration↔codegen merge (a Phase-G rebase onto main is not that merge, but it does change the base the charter specified).

## C4 — program sequencing (controlling doc D12.8)

The controlling doc sequences Phase G **after** Release B, as a Release-C predecessor. Starting it now is an amendment to a controlling doc owned by the Track-1 session. Flagging for explicit routing rather than assuming the handoff supersedes it — the handoff may well be the amendment, but that should be said out loud, not inferred.

## C2 — publish-pointer line reference (charter decision 5)

Charter cites `materialize/render/publish.py:22-24`. Lines 22-24 are the `errorifexists` rationale; the pointer's "not implemented" statement is at **`render/publish.py:26-31`**, and the deferral is DEFERRED-WORK **A.26** (`:484`), assigned to 16b/T17. Substance of the charter's claim is correct — only the citation moves.
