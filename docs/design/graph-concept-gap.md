# Graph lineage concept — implementation gap

Target artifact: `docs/design/graph-lineage-experience-concept.html` (authoritative; supersedes
`lineage-explorer-concept.html` and its adversarial review, which specify a dedicated route the
newer artifact reverses).

Extracted from the artifact's own DOM, block by block, against what `LineageView` +
`SearchScreen` render after commits `91e367c4`, `bd8cc1ab`, `437928ee`, `2d3d1ae5`.

## What actually landed

Only the **layout skeleton** and one inspector block. The three-column workspace
(`205px · 1fr · 328px`) exists and nothing floats over the canvas. That is roughly one of
twelve content blocks the artifact specifies. The furniture was moved into the right rooms;
the rooms were not furnished.

## Block-by-block

| Artifact block | Contents | State |
| --- | --- | --- |
| `.contextbar` | `COL` kind chip, full ref + `Customer Number · CIB · BO_DPL_CIB`, badges (`Customer identifier`, `2 checks required`), actions `← Results` / `View details` | **missing entirely** |
| `.tools` §1 Relationship layers | swatch + name + **description** per layer (`Approved structural joins`, `Same business entity across catalogs`, `Existing production lineage`) | partial — bare checkboxes, no swatches, no descriptions |
| `.tools` §2 **Line meaning** | legend with line samples: Verified join / Entity mapping / Containment | **missing** |
| `.tools` §3 **Current scope** | `One hop around cust_num. Expand a table to fetch its next neighborhood.` | **missing** |
| `.tools` §4 **Visibility** | `Only objects permitted for the current session are shown…` | **missing** |
| `.canvas` `.map-label` | `Customer identity neighborhood · 3 assets · 1 cross-catalog mapping` | **missing** |
| `.canvas` `.canvas-summary` | 3 stats pinned bottom, `pointer-events:none` (business mapping / registered features / readiness checks) | **missing** |
| Node cards | `node-kind` / `node-title` / grain badge / `node-meta` / freshness chip / column rows | different shape |
| Edge labels | `belongs to`, `customer · strong` | different |
| `.inspector` `.selected-label` + definition | `Selected column` + prose definition | **missing** |
| `.inspector` `.fact-grid` | Business term / Domain / Grain use | **missing** |
| `.inspector-section` Cross-catalog capability | relationship + `.trustline` 3 axes | **DONE** (`2d3d1ae5`) |
| `.inspector-section` Recommended features | 3 `.recommendation` cards + `Recommendations are discovery candidates—not registered lineage.` | **missing** (decision 4) |
| `.inspector-actions` | `View details` / `All recommendations`, pinned bottom via `margin-top:auto` | **missing** |

## Why this went wrong

Each decision was implemented as a minimal edit to the existing `LineageView` rather than
building the artifact's content. That satisfied the decision list read literally (panels do not
float; facets do leave graph mode) while missing that the artifact also specifies *what those
panels contain*. The decision list is a summary of the design, not a substitute for it.

## Next pass

Build from the artifact's DOM outward, not from the existing component inward. Order:

1. `.contextbar` — needs anchor identity, already available from the search hit.
2. Tools column §§2–4 — static/derived copy, no new API.
3. `.map-label` + `.canvas-summary` — counts derived locally (artifact decision 14 explicitly
   forbids requiring a dashboard-summary endpoint).
4. Inspector `.selected-label` / `.fact-grid` / `.inspector-actions`.
5. Decision 4 recommendations — the only block needing a new fetch
   (`getTableSuggestionsV2`, filtered to the selected column's operands).

## Open test gap

The three-axis trust render (`2d3d1ae5`) has **no test**. The negative case (axes do not apply
on a non-bridge edge) is covered. Needs a fixture whose entity-bridge endpoint is a clickable
column on an expanded card, with the entity layer toggled on, asserting all three badges render
simultaneously.
