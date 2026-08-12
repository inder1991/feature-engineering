# Suggestion card — gap against the artifact

Compared side by side: `docs/design/asset-detail-experience-concept.html` (`.feature` card)
against the deployed `SuggestionCard`.

The card is not a styling gap. The two have **different information architecture**: the
artifact leads with the answer and hides the reasoning; ours leads with the reasoning.

## Artifact order

1. Name
2. Two purple pills: recipe family + journey stage (`DURATION & STREAK`, `CONTEXT STAGE`)
3. One or two lines of plain description
4. **2×2 grid of bordered fact boxes**: Entity & grain / Time binding / Window / Aggregation
5. Two status chips: `DESIGN CHECKED`, `BINDING AMBIGUOUS`
6. The formula, in a light inset
7. Amber safety note
8. Italic "Business value has not been documented for this recipe."
9. `▸ Full recommendation detail` — a closed disclosure holding everything else
10. Footer: `USES BUSINESS_DT` … `OPEN RECOMMENDATION →`

## Ours

1. Name
2. Status chips (solid green + outline) — first, not after the facts
3. `suggested · recipe` + "no limitations recorded"
4. A paragraph explaining what "design checked" means, on **every** card
5. `CATEGORY` + chip + "derived from recipe family" + "partly classified"
6. `BUSINESS DOMAINS` / `USE CASES` — usually "not supplied: no controlled domain
   vocabulary is registered here"
7. `WHAT IT MEASURES` / `WHY IT IS USEFUL`
8. `ENTITY AND GRAIN` / `FAMILY AND STAGE` / `OPERATION AND WINDOW` / `AS-OF BINDING` /
   `OUTPUT ADDITIVITY` / `SOURCES` — six label/value rows
9. `INPUTS` chips
10. Formula

Result: roughly three times the height for the same information, and the reader meets six
"not supplied" statements before reaching a single fact about the feature.

## What to change

1. **Family + stage become two pills under the title.** Today they are a `FAMILY AND STAGE`
   row two thirds of the way down.
2. **Description moves to the top**, as prose. Today it is `WHAT IT MEASURES`, buried.
3. **Build the 2×2 boxed fact grid** — Entity & grain, Time binding, Window, Aggregation —
   replacing four of the six label/value rows. This is the single biggest visual difference.
4. **Status chips move below the grid** and lose the solid fill.
5. **Everything absent moves into the disclosure.** "Not supplied", "partly classified",
   "no controlled domain vocabulary is registered here" and the standing explanation of
   "design checked" are all reasoning, not answers. The honesty rule this project holds is
   that an absence must be *stated*, not that it must be stated *first* — a closed
   `Full recommendation detail` still states it.
6. **Add the footer**: `USES <column>` on the left, `OPEN RECOMMENDATION →` on the right.

## Caution

`SuggestionCard` is shared by the asset dossier and `SuggestedFeaturesScreen`, and is covered
by tests in `SuggestedFeaturesScreen.test.tsx`, `SuggestionCard.capture.test.tsx` and
`SuggestionCard.user-summary.test.tsx`. Several pin the current copy verbatim. Restructuring
means updating them to assert the new positions with equal strength, not deleting them.


## BLOCKER found while implementing (2026-08-05)

The card the screenshots show is the **asset dossier's** "Suggested features using this
column" panel — but `SuggestionCard` is the SAME component the standalone
`SuggestedFeaturesScreen` renders. Restructuring it changes both surfaces.

And the restructure collides with a deliberate, test-pinned product decision:

    SuggestedFeaturesScreen.test.tsx:151
    'keeps the fine-grained recipe family OFF the compact card and inside the detail'
      expect(within(card).queryByText('Balance trend')).toBeNull()

The artifact puts the recipe family on the compact card as a pill (`DURATION & STREAK`).
Someone previously decided the opposite and pinned it. That is a product question, not a
styling one, and it must be answered before the restructure proceeds:

1. **The artifact wins** — family and stage become compact-card pills; update that test to
   assert the new position with equal strength, and record why the earlier decision was
   reversed.
2. **The decision stands** — the compact card keeps family in the detail, and the asset
   dossier's panel diverges from the artifact on this one point.
3. **Split the component** — a denser variant for the dossier panel, the current card for
   the standalone screen. Most work, and two cards to keep honest.

An attempt at option 1 was reverted rather than shipped: it broke three tests, one of which
exists specifically to prevent that change.

## Second blocker: trimming the card collides with honesty guards (2026-08-05)

The taxonomy pills and the 2x2 fact grid shipped (commit on this branch), but the card got
LONGER, not shorter, because the verbose blocks above them were never removed. Removing them
fails six tests, and three of those guard deliberate honesty decisions:

| Test | Guards |
| --- | --- |
| `repeats the design-checked limit on the card itself, where the badge could mislead` | The "design checked means the INPUTS pass" note. It exists **because this card renders on the asset dossier**, where the page-level explanation does not. |
| `renders an absent controlled vocabulary as "not supplied", never as an omitted section` | Business domains / use cases stating their own absence on the card |
| `lists the first domains and counts the rest rather than growing the card` | The domain chip cap |

The second and third are arguably satisfied by the detail disclosure, which renders both
(SuggestionCard.tsx ~909-928) — an absence must be STATED, not stated FIRST.

**The first is not.** Removing the design-checked note leaves a green `DESIGN CHECKED` badge
on the dossier with nothing qualifying it, which is the exact misreading the note was written
to prevent. The concept's card has no such note because the concept's page carries the
explanation elsewhere; ours does not on the dossier.

Options:

1. Keep the note on the card, drop only category / domains / use cases into the detail.
   Shorter card, honesty preserved. **Recommended.**
2. Move the note to the dossier's panel header ("Suggested features using this column"), once,
   instead of once per card — then the card can drop it.
3. Accept the concept exactly and lose the qualifier. Not recommended.

An attempt at option 3 was reverted rather than shipped.

## AGREED APPROACH (user decision, 2026-08-05) — supersedes options 1-3 above

Name both states on the badge, and drop the per-card paragraph.

The badge today appears only when things are good (`DESIGN CHECKED`, solid green), so the card
has to carry a sentence explaining what it does NOT mean. Naming the opposite state makes the
axis self-evident from the vocabulary: a reader who sees `design checked` on one card and
`design not checked` on another understands the badge is answering one narrow question about
DESIGN, not declaring readiness. A label that is always present beats a sentence that stops
being read by the third card.

Implement exactly this:

1. **Badge names both states** — `design checked` / `design not checked`. Today only the
   positive state renders a badge.
2. **Delete the per-card explanation** ("Design checked means the inputs pass the catalog's
   design rules. Predictive usefulness and production execution are not proven.").
3. **One explanation in the panel header**, beside "Suggested features using this column",
   read once instead of once per card.
4. **Tone the badge down.** Solid green reads as "good to go", which is the exact over-trust
   the paragraph existed to prevent. Use the quiet outlined chip treatment (see
   `.ln-trustline .badge` for the pattern). Reserve solid green for something verified end to
   end, which a design check is not.

Then the remaining trim from "What to change" above is safe: category, business domains and
use cases move into `Full recommendation detail`, which already renders all three
(`SuggestionCard.tsx` ~909-928).

### Test to update, not delete

`SuggestedFeaturesScreen.test.tsx` — `repeats the design-checked limit on the card itself,
where the badge could mislead`. Its guarantee is "a reader cannot mistake the badge for
proof". Re-express it as: both states render as badges, the badge is not the success tone,
and the explanation appears exactly once in the panel header. Same guarantee, new mechanism.

### Why this is better than what it replaces

The paragraph was a workaround for a badge that only spoke when the news was good. Fixing the
badge removes the need for the workaround, so the cards reach the concept's proportions
*without* trading away the honesty guard — which is what the three reverted attempts kept
getting wrong.

## Full diff against the artifact (2026-08-05, from a side-by-side)

### A. On the compact card that should not be

1. `suggested · recipe` + "no limitations recorded" row — provenance, belongs in the detail.
2. The whole tinted **CATEGORY block**: category chip, "derived from recipe family",
   "partly classified", **Business domains** ("not supplied: no controlled domain vocabulary
   is registered here"), **Use cases** ("not supplied"). Six lines of absence before one fact.
3. `WHAT IT MEASURES` / `WHY IT IS USEFUL` as LABELLED rows — the artifact runs the
   description as bare prose with no label.
4. The sources line (`tenure · 1 table, 3 columns · data roles not supplied…`).
5. `INPUTS` chips — the artifact keeps every input column in the detail.
6. A SECOND safety note (pink "Eligibility and leakage"). The artifact shows one amber note.

### B. Order

7. Taxonomy pills are 7th; they must be 2nd, directly under the name.
8. Status chips are 2nd; they must come AFTER the fact grid.
9. Description must be 3rd, as prose.

### C. Styling

10. Status chips are plain grey. The artifact uses an outlined green pill and an outlined
    amber pill, uppercase mono — readable as a pair.
11. Card title is mono; the artifact's is sans-bold.
12. Fact values are not bold; the artifact's are bold dark against a grey uppercase label.
13. Value casing: ours "customer per cust_num", artifact "Customer · per cust_num".
14. Window: ours "365d", artifact "Trailing 365 days"; ours "no rolling window", artifact
    "No rolling window".
15. Safety note carries an uppercase "POINT-IN-TIME" label column; the artifact has none —
    just the sentence on an amber field with a left rail.
16. Missing the italic "Business value has not been documented for this recipe."
17. `Full detail` is a boxed button bottom-right; the artifact has a `▸ Full recommendation
    detail` text disclosure, left-aligned.
18. Missing the footer row entirely: `USES BUSINESS_DT` left, `OPEN RECOMMENDATION →` right.
19. Card is roughly 2.5x the artifact's height.

### D. Raw values leaking into the UI

20. `non_additive` renders raw — should be "Non-additive".
21. `n/a` alone — the artifact says "Not summable · n/a".
22. `tenure` as the operation, unlabelled and merged into the sources line.

### Order of work

D first (one mapping table, immediate legibility win), then A (deleting six blocks is what
collapses the height), then B (three moves), then C (chip and type styling).

## The restructure was built and reverted — here is exactly what blocks it (2026-08-05)

Groups A, B and D of the diff above were implemented in full: head reduced to the name,
category block and meaning rows cut, sources line and INPUTS chips cut, second safety note
cut, status chips moved below the fact grid, and `windowWords` / `additivityWords` added so
"365d" reads "Trailing 365 days" and "non_additive" reads "Non-additive".

It typechecks and builds. It fails **eight** tests, every one of them asserting content on the
COMPACT card that the restructure moves into `Full recommendation detail`. None is a real
regression, but each needs its assertion re-pointed deliberately, not deleted:

| Missing string | Where it went | How to re-assert |
| --- | --- | --- |
| `Business domains`, `no controlled domain vocabulary is registered here` | detail | open the detail first, then assert |
| `Domain 0`, `Attrition`, `+N more` | detail | same |
| `no category mapped yet` | detail | same |
| `1 limitation` / `6 limitations` / `no limitations recorded` | head badge, removed | assert the limitation ROWS in the detail instead |
| `suggested · recipe` | head badge, removed | assert in the detail's provenance section |
| `n/a` | now `Not summable · n/a` | update the expected string |
| XSS fixture (`<img src=x onerror=…>`) | detail | open the detail; the escaping guarantee is unchanged |

**The one to be careful with** is the limitation count. It was a badge in the head saying "6
limitations"; the rows themselves still render. Re-assert the ROWS, so the guarantee "a
limitation is never silently dropped" survives — do not simply delete the assertion.

Estimated one focused pass: apply the diff again (it is mechanical), then walk the eight tests
in order. Suite must return to 603.
