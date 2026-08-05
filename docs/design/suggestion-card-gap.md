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
