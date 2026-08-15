# Feature generation — post-submit workspace UX review

Status: design review and interaction proposal. No production UI or backend behavior has been
changed.

Artifacts:

- [Interactive high-fidelity concept](./feature-generation-post-submit-concept.html)
- Screen reviewed: Feature generation, post-generation “Compare” state, supplied 2026-08-15
- Implementation reviewed: `frontend/src/screens/WorkbenchScreen.tsx` and its tests

## Executive verdict

The current screen has the right workflow states and the wrong state hierarchy.

After generation, the state strip correctly advances to **Compare, mix, give feedback**, but the
page continues to render the complete **State the goal** form before the results. At the reviewed
viewport, the current work—2 proposed sets and 917 features—starts below the fold. The interface
therefore tells the user to compare while visually asking them to re-enter their request.

This should be fixed with a phase-aware workspace, not by shrinking margins. The full intake form
belongs to the draft state. Once submitted, it should become a compact, immutable **submitted
brief** at the top; beside it the UI should show the current stage, engine output, and the next human
decision. Results and feedback then occupy the primary viewport. Editing the brief is an explicit
action that begins a revised round.

Recommendation: approve the proposed information architecture. It preserves every existing
human-in-the-loop step and feature-generation capability; it changes what is visually primary in
each phase.

## What the current implementation is doing

`WorkbenchScreen` computes the four gate states from real application state. When candidates
arrive, Gate 2 becomes done and Gate 3 becomes active. That part is sound and is covered by tests.

The render tree does not follow that state transition:

1. The four expanded gate cards always render.
2. The complete hypothesis, goal, source, entity, target, and path-selector form always renders.
3. Optional recognition and ranking panels render after the form.
4. The generated result heading and set cards render only after all of the above.

The page is consequently a chronological log of components rather than a workspace for the current
task.

There is also a truthfulness risk. A successful run snapshots `roundHypothesis` and
`roundObjective`, and whole-round feedback correctly reruns that snapshot. However, the visible
hypothesis and goal inputs remain directly editable without clearing the generated round. The user
can be looking at new text beside results produced for old text. The correct post-submit summary
must use the submitted snapshot, not the live draft fields.

## Findings

| Severity | Finding | User impact | Design response |
| --- | --- | --- | --- |
| Critical | The active phase and dominant viewport disagree. | Users do not know where the current work starts and may think generation did not finish. | Switch the page shell by phase; results/decision state becomes the main view after submission. |
| Critical | Editable request text can visually diverge from the submitted round. | A user can make decisions against output while believing it was generated for a different hypothesis. | Show an immutable submitted-brief snapshot; edit through a revised-round flow. |
| Major | Output begins below the fold. | The value-producing part of the workflow is effectively hidden after the primary action. | Put output counts, recommended set, results and the next action in the first viewport. |
| Major | Four full-width stage cards consume height after their explanatory purpose is over. | Progress is visible, but at the cost of the task itself. | Use a compact status rail after submission; expand stage details only on demand. |
| Major | Guidance is repeated in the page subtitle, gate cards, field help and path cards. | Scanning cost is high and important warnings have no distinct visual weight. | Keep instructional prose in draft; use factual state and action copy in the workspace. |
| Major | “917 features” is presented as a count, not a review strategy. | Users face an unbounded review queue with no clear starting point. | Lead with strategy sets and the engine recommendation; provide search/filter and progressive disclosure. |
| Major | Human attention is distributed through the long page. | Scope confirmation, feedback, revision and approval can all appear far from the stage indicator. | Reserve a top-of-workspace **Needs your decision** area for the one current human action. |
| Moderate | The selected-feature tray appears at the end of the candidate list. | The consequence of selection may disappear after scrolling. | Keep a sticky decision tray beside results on desktop and a sticky bottom bar on narrow screens. |
| Moderate | Generation completion has no deliberate focus transition. | Keyboard and assistive-technology users remain at the submit control while new output appears elsewhere. | Move focus to the current-stage heading and announce state/count changes through a polite live region. |

## Proposed information architecture

The screen has two shells.

### 1. Draft shell — before submission

Keep the existing intake capability, but present it as a focused brief builder:

- hypothesis and prediction goal;
- catalog/source, entity and target scope;
- engine generation and manual-definition paths;
- concise explanation of what will happen next.

There are no empty result panels and no four-card progress strip. A short “1. Brief → 2. Scope →
3. Compare → 4. Approve” line is sufficient orientation.

### 2. Run workspace — after submission

The first viewport contains three pieces of information in this order:

1. **Submitted brief** — the exact hypothesis/objective and scope used by the run, collapsed to a
   compact summary with **Revise brief**.
2. **Current stage** — planning, scope confirmation, compare/refine, or approval; one sentence says
   what is happening or what the user must do.
3. **Output so far** — set count, candidate count, rejection availability, selection count and the
   next action.

Below that, the workspace is split into:

- **Main work area:** stage-specific content—planning activity, scope confirmation, results, or
  approval outcome.
- **Decision rail:** recommendation, current selections, warnings and the primary action. It is
  sticky but never overlays content.

On narrow screens the decision rail moves into the document and its primary action becomes a sticky
bottom bar.

## Phase behavior

| Phase | Top status | Main content | Human action |
| --- | --- | --- | --- |
| Draft | Brief not submitted | Intake form | Generate or write definitions |
| Planning | Engine working | Stage activity and honest partial/error state | Wait, or cancel only if cancellation exists |
| Scope confirmation | Needs your confirmation | Recognized primary/secondary scope and soft context | Confirm, adjust, or broaden |
| Results ready | Compare and refine | Set cards, recommendation, filters and candidate list | Take a set, mix features, or give feedback |
| Selection ready | Ready for approval | Same results with selected state | Register or govern selected features |
| Completed | Saved/governed outcome | Outcome summary and per-item errors | View registered features or start a new round |
| Empty/error | No result / run failed | Explanation, rejections or error details | Revise the brief or retry |

Only one phase owns the page headline and primary action. Earlier phases remain inspectable in the
compact timeline; they do not remain fully expanded above the current work.

## The post-submit header

The proposed **run deck** replaces the expanded form and gate cards after submission.

```text
Feature generation                                      Results ready

Brief ✓  ── Scope ✓  ── Compare & refine ●  ── Approve ○

┌ Submitted brief ─────────────┬ Engine output ──────────┬ Your next action ┐
│ “Activity accelerates…”      │ 2 sets · 917 candidates│ Review Temporal  │
│ Predict churn in 90 days     │ Target screened        │ or mix features  │
│ CIB · customer · target      │ Rejections available   │ [Review results] │
│ [Revise brief]               │                         │                  │
└──────────────────────────────┴─────────────────────────┴──────────────────┘
```

This gives the user the answer to four questions without scrolling: What did I ask? Where is the
workflow? What came back? What do I do now?

## Results and human feedback

### Start with sets, not 917 rows

The current set model is the right abstraction. Preserve it and make it the entry point:

- open the backend-recommended set by default;
- compare the set thesis, feature count and caveat;
- let the user take a set or switch sets;
- search/filter within the active set;
- disclose lower-ranked candidates rather than rendering the whole universe as equally important.

The UI must not invent a quality score. “Recommended” remains an advisory backend decision, and the
backend caveat remains visible beside it.

### Put feedback where comparison happens

Whole-round feedback should sit above the active candidate list, close to the recommendation—not
after hundreds of rows. Per-candidate feedback stays on the candidate being revised. Recorded
feedback remains visible in a collapsible activity trail.

### Keep the approval consequence visible

The decision rail shows:

- selected count and source sets;
- what **Approve and register** will write;
- which selections are governable;
- per-item failures without hiding successful items.

The UI must preserve the existing distinction between registering and governing. The redesign must
not turn design-checked into predictive validation or imply that a generated proposal is already a
model input.

## Revising the brief

**Revise brief** opens a drawer or modal populated from the submitted snapshot. It does not silently
mutate the meaning of the visible result.

The action has two explicit outcomes:

- **Cancel:** return to the current run unchanged.
- **Generate revised round:** submit a new round. Keep the old result visible until the new request
  succeeds, then replace it according to the existing candidate-pinning rules.

If the product chooses to clear current candidates immediately instead, the confirmation copy must
say so before submission. Either policy is acceptable; silent invalidation is not.

## Content rules

1. Draft copy explains the process; run copy reports facts and decisions.
2. “Stage” labels use verbs: **Planning**, **Confirm scope**, **Compare and refine**, **Approve**.
3. Use “candidate” for generated output, “selected” for the human’s tray, “registered” for a saved
   feature, and “governed” only for a signed contract.
4. Do not repeat “nothing is saved without your click” in four places. Show it once beside the
   approval action where it matters.
5. Counts always say what they count: sets, unique candidates, visible candidates, selected
   candidates and rejected recipes are different numbers.
6. The submitted brief always comes from the round snapshot.

## Accessibility and responsive constraints

- After generation, focus the `<h2>` for the active stage with `tabIndex={-1}`; do not force a page
  scroll while the user is typing.
- Announce “Results ready: N sets, M candidates” in an `aria-live="polite"` region.
- Stage state needs text/icons as well as color.
- The compact submitted brief must have a complete accessible name even when long refs are visually
  truncated.
- The decision rail must participate in normal document order and must not overlay candidate text.
- At 320–767px, stack the run-deck cards, keep the current stage first, and expose the primary action
  in a sticky bottom bar with enough page padding to avoid covering the last row.
- Respect reduced motion; stage changes do not require animation to be understood.

## Mapping to the existing frontend state

Most of this redesign requires no backend contract change.

| UI concept | Existing source |
| --- | --- |
| Submitted hypothesis/objective | `roundHypothesis`, `roundObjective` |
| Draft hypothesis/objective | `hypothesis`, `goal` |
| Scope | `source`, `entity`, `target`, and the confirmed recognition state |
| Planning | `generating` |
| Scope confirmation | `recognition !== null` when the confirmation UI is enabled |
| Result counts | `setLenses`, `generated`, `rejections` |
| Recommendation | `recommendation` |
| Compare/approval state | `selectedCount`, `registered`, `governed` |
| Blocking notice | `notice` |

One frontend contract should be made explicit: derive a single discriminated view phase (for
example `draft | planning | scope_review | compare | approve | complete | empty | error`) and render
the shell from it. Do not scatter separate visibility decisions across every panel.

## Implementation sequence

### Slice 1 — hierarchy correction

- Add a derived view phase.
- Render the full form only in draft/revise mode.
- Add the compact run deck using submitted snapshots.
- Move the result heading and set cards into the first post-submit viewport.
- Move focus and announce results on successful generation.

This slice fixes the screenshot’s main failure without changing APIs or candidate behavior.

### Slice 2 — decision workspace

- Move whole-round feedback above the candidate list.
- Add the sticky decision rail/bottom action bar.
- Add search and existing-metadata filters within the active set.
- Put detailed stage history, rejections and feedback history behind disclosures.

### Slice 3 — revised-round interaction

- Add the populated revise drawer.
- Make invalidation/replacement behavior explicit and tested.
- Preserve or intentionally reset active set, filters, selections and scroll according to the
  existing generation rules.

## Acceptance scenarios

The redesign is incomplete until all of these are exercised:

- first visit with an empty brief;
- generation in progress;
- recognition awaiting scope confirmation;
- two sets with hundreds of candidates;
- one set and one candidate;
- no grounded candidates with rejections;
- provider/API error;
- whole-round feedback in progress and exhausted;
- selected candidates from one set and mixed across sets;
- registration/governance partial success;
- revise then cancel;
- revise and generate a replacement round;
- 1280 × 720 laptop viewport, 200% zoom, and 390px mobile width;
- keyboard-only and screen-reader result transition.

## What this proposal deliberately does not change

- It does not remove or weaken any human confirmation.
- It does not change candidate generation, safety screening, ranking, registration or governance.
- It does not claim that recommendations prove predictive value.
- It does not add a new backend workflow engine.
- It does not hide rejected or blocked work; it moves detail behind honest, count-bearing
  disclosures.

---

# Amendments — 2026-08-15, after verification against the code and a live run

The review above was written from a screenshot and a reading. Every claim below was re-checked
against the implementation, and one live incident on the kind sandbox changed what the scope area
has to express. The original text is left intact; these are the corrections and additions.

## A1. The critical truthfulness finding is CONFIRMED

Verified, not inferred. `WorkbenchScreen.tsx:1991-1992` binds the hypothesis input as
`value={hypothesis} onChange={e => setHypothesis(e.target.value)}` with no disabling once a round
has completed — only the submit control is gated (`:2056`). The displayed results were produced
from the `roundHypothesis` / `roundObjective` snapshots taken at submit (`:896-897`, consumed at
`:1361` and `:1407`).

So a user can edit the request text and read old results beside it, with nothing on screen saying
they disagree. The proposed fix — an immutable submitted brief plus an explicit revise flow — is
the right shape and should stay Critical.

## A2. The entity chip is stale — entity is no longer part of the brief

The concept's submitted-brief card shows a `entity · customer` chip. **The intake form no longer
collects an entity.** Since the E4 cutover the engine plans over ONE frozen catalog context, and an
entity-only request is refused typed by the route (`422 SEMANTIC_REQUIRES_CATALOG_SOURCE`), so the
field was removed — the reasoning is recorded at `WorkbenchScreen.tsx:15-20`: *"a field whose only
reachable outcome is a refusal is an invitation to fail."*

Entity survives only as `scopeEntity` in the **scope confirmation** step (`:874`, input at `:2364`),
which happens *after* recognition and is optional and skippable.

**Amendment:** remove the entity chip from the submitted-brief card, or move it into the scope
summary and label it as confirmed scope rather than submitted request. The review's own mapping
table (§ "Mapping to the existing frontend state") already files entity under Scope and is correct;
only the mockup conflates the two moments.

## A3. The scope stage needs five states, not a tick — this is the material amendment

The concept renders scope as `Brief ✓ — Scope ✓ — Compare ● — Approve`, and the phase table has one
row for scope confirmation. That models a two-outcome world: recognition either worked or did not.

A live run on 2026-08-15 produced a third outcome that neither artifact can express. The recognizer
returned the CORRECT classification (`customer.relationship_attrition.churn`, high confidence, with
evidence quoting the user) **and** a second candidate literally labelled `"rationale": "placeholder"`.
The platform discarded both, recorded a technical failure, told the user *"No use-case was
recognised for this objective"*, and served 917 unscoped candidates. The user was informed of an
absence that had not occurred.

The remediation in flight (`docs/superpowers/plans/2026-08-15-recognition-repair-seam.md`) replaces
that single boolean with a persisted `recognition_quality` contract:

```
disposition: clean | repaired | partially_recovered | unscoped | technical_failure
repair_attempts: int
dropped_candidate_count: int
drop_reason_codes: [...]
```

These are not five shades of one state. Two of them are **successes that keep their scope**:

| Disposition | What the user must understand | Does scope survive? |
| --- | --- | --- |
| `clean` | Recognised normally | Yes |
| `repaired` | The model's first answer was invalid; it corrected itself | Yes |
| `partially_recovered` | An invalid proposal was discarded; the rest stands | **Yes — do NOT offer "show all recipes"** |
| `unscoped` | No governed use case clearly matched | No, honestly |
| `technical_failure` | Recognition could not be validated — a platform outcome, not an absence | No, and say so |

**Amendments required before Slice 1 is built:**

1. The scope stage carries a **state**, not a tick. A green ✓ can represent `clean` and `repaired`;
   `partially_recovered` needs a distinct advisory treatment; `unscoped` and `technical_failure`
   are different from each other and neither is a success.
2. `partially_recovered` **keeps its surviving scope**. The obvious copy — "…showing all buildable
   recipes" — is wrong here and would discard a scope the platform successfully recovered.
3. `technical_failure` must never render as absence. That equivalence is the exact defect the live
   incident produced, and repeating it in the redesign would re-ship it with better typography.
4. Add a phase-table row for **recognition attempted and rejected**, distinct from both
   "scope confirmation" and "empty/error".
5. Extend § "Acceptance scenarios" with one case per disposition; today's list has none of them.

## A4. "917 features" is a symptom, not a queue-management problem

The review treats the 917 count as a review-burden to solve with sets, filters and progressive
disclosure. That is good UI advice built on a number that is about to change meaning.

Measured on the live run: **774 of the 917 candidates report missing operands**, and the set is that
large precisely because recognition failed and fell back to unscoped — 306 of 317 registry recipes,
96% of the library. The remaining shortfall is structural: the two catalogs hold a VERIFIED customer
bridge with zero join realizations, so transaction behaviour cannot be joined to customer
attributes, and most banking recipes cannot bind.

**Amendment:** keep the sets-first entry point, but do not design the results area around an
unbounded queue. State the honest composition — how many candidates bound, how many are missing
operands and why — and let the count shrink when scoping is restored. A filter that hides 774
unbuildable candidates and a scoping fix that never generates them are not the same product.

## A5. What still stands unchanged

The phase-aware shell, the immutable submitted brief, results in the first viewport, the decision
rail, the content rules and the accessibility constraints are all endorsed. Slice 1 remains worth
building first — with A2 and A3 folded in, so the scope area is not rebuilt within days of landing.

