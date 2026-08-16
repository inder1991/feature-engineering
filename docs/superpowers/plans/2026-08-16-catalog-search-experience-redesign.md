# Catalog Search Experience Redesign — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild the catalog search result experience so a banker scanning twenty rows can tell what each asset *means*, what it can *do*, and where to go next — without the screen inventing a single fact the catalog does not hold.

**Architecture:** Frontend-only. `SearchScreen.tsx` (586 lines) splits into a screen shell plus three focused units: a pure display-contract module (`searchHitDisplay.ts`) that owns every honesty rule, a result row (`SearchHitRow.tsx`) with one primary action and an overflow disclosure, and a server-driven facet panel (`SearchFacetPanel.tsx`) that renders whatever facet groups `/search` actually returns instead of a hardcoded seven. No migration, no API contract change except widening the client's facet-key list to the six profile facets the backend already accepts.

**Tech Stack:** React 19, TypeScript, Vite, Vitest + Testing Library + user-event, hand-written CSS on the `frontend/DESIGN.md` token system (OKLCH, IBM Plex Sans/Mono). No new dependencies.

**Spec:** This document, §0. The source artifact is `~/Downloads/catalog-search-experience-concept.html` (a static mockup, not a contract — §0.2 records exactly which of its promises are adopted and which are refused as unbacked).

**Baseline:** worktree `.claude/worktrees/search-experience-redesign`, branch `worktree-search-experience-redesign`, off `origin/main` @ `3367ee3b`. Frontend suite verified green before any change: **40 files, 870 tests, 0 failures** (`cd frontend && npm test`).

## Global Constraints

- **Never fabricate a value.** Every string a row renders must be traceable to a field in the `SearchHit` the server sent. No prettified identifiers (`business_dt` must never render as "Business Date"), no invented owners, rates, review counts, or SLAs. Absence renders as absence.
- **App vocabulary beats mockup vocabulary.** The app says **"Suggested features"** (route `#/suggested`); the mockup says "Recommended features". Use the app's word.
- **Design tokens only.** No new colors, fonts, radii, or shadows. Everything comes from `frontend/src/index.css` `:root`. Binding reference: `frontend/DESIGN.md`.
- **Voice:** plain declarative microcopy. No exclamation marks, no "oops", no emoji. Copy states the fact and the next action.
- **Typography:** body 14px/1.5, secondary 13px, micro-labels 11px uppercase +0.06em/600. Every `object_ref`, column name and feature id renders in IBM Plex Mono. `font-variant-numeric: tabular-nums` on every count.
- **CSS is append-only.** New rules go in one block at the END of `frontend/src/index.css` under a `/* ---- catalog search (2026-08-16 redesign) ---- */` banner. Do not edit rules above it.
- **Accessibility floor:** every interactive control reachable and operable by keyboard; focus ring 2px `--accent-line` at 2px offset; text contrast ≥ 4.5:1; non-text UI contrast ≥ 3:1; `prefers-reduced-motion` respected.
- **Every task ends green:** `cd frontend && npm test` (all 870+ tests), `npm run typecheck`, `npm run lint`. A task is not done on its own test alone.

---

## §0 Design

### §0.1 The problem with today's screen

Today every result row is a mono `object_ref` headline, a grey definition paragraph, a dot-joined
meta string, and **four visually identical ghost buttons** (Details, Suggested features, Graph,
Impact). Four equal buttons is zero guidance. The physical address leads, so twenty rows read as
twenty strings of `public.<table>.<column>` and the reader has to parse identifiers to find
meaning. The facet panel hardcodes seven groups and renders `(none)` — which on a 150K-column
catalog is usually the largest bucket in the group — as a first-class value. And the `/search`
response carries a `projection` status (`ready` | `lagged`) that the screen **never renders**: when
the semantic projection is behind, search silently serves possibly-stale meaning with no
disclosure, while the asset page discloses exactly that condition.

### §0.2 What we take from the concept, and what we refuse

**Adopted:**

| Concept idea | Why it holds |
|---|---|
| Meaning leads the row; physical ref demoted to a breadcrumb | The reader is looking for a business fact, not an address |
| One primary action, one secondary, the rest in an overflow | Four equal buttons is no hierarchy |
| Per-row "Explore relationships"; no global Graph toggle | A lineage graph needs an anchor; a global toggle has none |
| Capability lines ("Grain key for Account") | Says what the asset can *do*, which is the actual question |
| Filters in user language; "not classified" demoted | `(none)` dominating every group is noise, not information |
| "permitted assets" in the pager copy | Names read-scope honestly |
| Search affordances: examples, clear button, `/` to focus | Cheap, standard, missing today |

**Refused — the mockup writes cheques our data cannot cash:**

| Concept element | Verified reason it cannot ship |
|---|---|
| Row titles like "Business Date", "Customer Number" | `graph_node` has **no** business-name column (checked every migration) and `_select_hit` projects none. The mockup's own example is a CIB physical column, which has no attested glossary term at all — that title is invented. Deriving it from `business_dt` would be a machine-written label standing where the platform's authority model expects an attested value. See §0.4 (deferred charter). |
| "2 mappings need review", "Temporal role needs review" | The search response carries no review state of any kind |
| "Snapshot cutoff", "Customer entity key" chips | No backing field; they are prose written by the mockup's author |
| "concept: as_of_date · **AI proposed**" | `graph_node.concept` is advisory enrichment (migration 0951), but the hit carries no authority marker, so search cannot prove *proposed* any more than it can prove *confirmed*. Render the concept, claim no authority. |
| Sort dropdown (Business relevance / Physical name / Recently updated) | `search()` orders by `score DESC, object_ref, catalog_source` only. There is no sort parameter. A dropdown whose options do nothing is worse than no dropdown. |
| The mockup's palette (`#007e90`, hex shadows) | Our system is already the same petrol/IBM Plex family in OKLCH. Take the structure, keep the tokens. |

**Found while verifying, not in the concept:** the backend already accepts six more facets —
`data_role`, `authority_role`, `temporal_storage_model`, `bian_path`, `process_path`, `sub_domain`
(`_PROFILE_FACETS`, active when `FEATUREGEN_DATASET_PROFILES` is on). The client hardcodes seven
facet keys and cannot even *send* the others, so those groups are invisible and unusable today.
The concept's "Data role" group is one of them. A server-driven panel gets all six for free.

### §0.3 Decisions

- **D1 — What leads a row.** Title = `hit.column ?? hit.table`, IBM Plex Mono 14px/600. Under it a
  breadcrumb `deposits › public.accounts › balance` at 11px `--ink-faint`. Under that, the
  `definition` promoted from grey afterthought to the reading position: 13px `--ink-soft`, clamped
  to two lines. The definition is the business meaning we genuinely have.
  *One function*, `hitDisplayName()`, owns this, so §0.4 lights it up with a one-line change.
- **D2 — Capability lines are derived, never asserted.** Only from fields the hit carries:
  `is_grain` → "Grain key" / "Grain key for {entity}"; `is_as_of` → "As-of field"; `additivity`
  → "Semi-additive · USD". Plus one link: "Suggested features". Nothing else.
- **D3 — Concept without authority claim.** Render `concept: customer_id` in the meta line. Never
  append "AI proposed" or "confirmed" — search cannot tell.
- **D4 — Action hierarchy.** Primary: **Open asset**. Secondary: **Explore relationships**.
  Overflow (`···`): Suggested features, Feature impact, Copy reference. Impact results keep
  rendering inline in the row (today's behavior; it is honest and useful).
- **D5 — Retire the global List/Graph toggle.** Graph is entered from a row only; exit stays
  `LineageView`'s existing `onBackToResults`. This deletes the disabled-toggle + hint and the whole
  class of "which hit is this graph anchored on?" bugs.
- **D6 — Facet panel follows the server.** Render groups from `result.facets` keys: known keys in a
  declared order with curated labels, unknown keys after them, humanized. `(none)` renders as
  **"Not classified"**, pinned last in its group, never inside the first six. Groups show six
  values with **"Show all (N)"** to expand. Flags (grain/as-of) stay their own group titled
  **"Column role"** — deliberately distinct from the backend's `data_role`, which is a *table*
  role and is labelled **"Table role"**.
- **D7 — Projection disclosure.** `ready` → a quiet "Catalog projection current" line in the
  toolbar. `lagged` → a warn callout above the rows: *"The catalog projection was behind when these
  results were read, so they may not yet reflect the newest resolved semantics."* Rows are always
  still served — disclosure, never refusal.
- **D8 — Count copy.** Status line: "221 assets · showing 1–20". Pager line: "Showing 1–20 of 221
  permitted assets".
- **D9 — Empty state** keeps today's fail-closed sentence and gains a "Clear search and filters"
  button.
- **D10 — File split.** `searchHitDisplay.ts` (pure rules + unit tests), `SearchHitRow.tsx`,
  `SearchFacetPanel.tsx`, and `SearchScreen.tsx` reduced to state, routing and layout.

### §0.4 Deferred charter — NOT part of this plan, do not start

Two items need backend work and an explicit go from the user before anyone opens a file:

1. **Attested business names in search.** Project `business_term` onto `graph_node` (migration +
   ingest write + `_select_hit` + `SearchHit`), so a glossary-backed source like FTR can lead rows
   with its attested term while CIB-style physical catalogs keep falling back to the column name.
   Lands as one line inside `hitDisplayName()`.
2. **Server-side sort.** A `sort` parameter on `/search` (business relevance | physical name |
   recently updated) plus the UI control. Requires deciding what "recently updated" means against
   the drift watermark.

---

## File Structure

| File | Responsibility |
|---|---|
| `frontend/src/screens/searchHitDisplay.ts` **(new)** | Pure functions: display name, breadcrumb, capabilities, meta. Every §0.3 honesty rule lives here and nowhere else. |
| `frontend/src/screens/searchHitDisplay.test.ts` **(new)** | Unit tests for those rules, including the "never invent" cases. |
| `frontend/src/screens/SearchHitRow.tsx` **(new)** | One result row: anatomy, badges, capability lines, action hierarchy, overflow disclosure, inline impact. |
| `frontend/src/screens/SearchHitRow.test.tsx` **(new)** | Row behavior in isolation. |
| `frontend/src/screens/SearchFacetPanel.tsx` **(new)** | Server-driven facet groups, collapse/expand, not-classified demotion, flags group. |
| `frontend/src/screens/SearchFacetPanel.test.tsx` **(new)** | Panel behavior in isolation. |
| `frontend/src/screens/SearchScreen.tsx` **(modify)** | Search state, hash sync, paging, toolbar, projection disclosure, empty state, layout. Row and panel markup move out. |
| `frontend/src/screens/SearchScreen.test.tsx` **(modify)** | Existing assertions updated to the new labels and copy. |
| `frontend/src/api.ts` **(modify)** | `SEARCH_FACET_KEYS` widened by the six profile facets the backend already accepts. |
| `frontend/src/index.css` **(modify, append-only)** | The `catalog search` block at the end of the file. |

---

### Task 1: The display contract

The honesty rules, as pure functions, before any pixel exists.

**Files:**
- Create: `frontend/src/screens/searchHitDisplay.ts`
- Test: `frontend/src/screens/searchHitDisplay.test.ts`

**Interfaces:**
- Consumes: `SearchHit` from `frontend/src/api.ts` (fields: `object_ref`, `table`, `column`, `kind`, `data_type`, `definition`, `is_grain`, `is_as_of`, `catalog_source`, `concept`, `domain`, `sensitivity`, `sensitivity_display`, `additivity`, `unit`, `currency`, `entity`, `score`).
- Produces, used by Tasks 2 and 3:
  - `hitDisplayName(hit: SearchHit): string`
  - `hitBreadcrumb(hit: SearchHit): string[]`
  - `hitCapabilities(hit: SearchHit): HitCapability[]` where `interface HitCapability { key: 'grain' | 'as_of' | 'measure'; label: string }`
  - `hitMeta(hit: SearchHit): string[]`

- [ ] **Step 1: Write the failing test**

Create `frontend/src/screens/searchHitDisplay.test.ts`:

```ts
import { describe, expect, it } from 'vitest'
import type { SearchHit } from '../api'
import { hitBreadcrumb, hitCapabilities, hitDisplayName, hitMeta } from './searchHitDisplay'

const COLUMN: SearchHit = {
  object_ref: 'public.accounts.balance', table: 'accounts', column: 'balance', kind: 'column',
  data_type: 'numeric', definition: 'end-of-day ledger balance', is_grain: false, is_as_of: false,
  catalog_source: 'deposits', concept: null, domain: null, sensitivity: null,
  sensitivity_display: null, additivity: null, unit: null, currency: null, entity: null, score: 1,
}
const TABLE: SearchHit = {
  ...COLUMN, object_ref: 'public.accounts', table: 'accounts', column: null, kind: 'table',
  data_type: null, definition: 'customer account master',
}

describe('hitDisplayName', () => {
  it('leads a column hit with its column name', () => {
    expect(hitDisplayName(COLUMN)).toBe('balance')
  })

  it('leads a table hit with its table name', () => {
    expect(hitDisplayName(TABLE)).toBe('accounts')
  })

  // The honesty rule: the catalog carries no attested business name in the search projection, so
  // an identifier is rendered AS the identifier. Expanding `business_dt` to "Business Date" would
  // put a machine-written label where an attested value belongs.
  it('never prettifies an identifier into prose', () => {
    expect(hitDisplayName({ ...COLUMN, column: 'business_dt' })).toBe('business_dt')
    expect(hitDisplayName({ ...COLUMN, column: 'cust_num' })).toBe('cust_num')
  })
})

describe('hitBreadcrumb', () => {
  it('reads source › schema-qualified table › column for a column hit', () => {
    expect(hitBreadcrumb(COLUMN)).toEqual(['deposits', 'public.accounts', 'balance'])
  })

  it('reads source › table for a table hit', () => {
    expect(hitBreadcrumb(TABLE)).toEqual(['deposits', 'public.accounts'])
  })

  it('falls back to the table field when the ref carries no schema', () => {
    expect(hitBreadcrumb({ ...COLUMN, object_ref: 'balance' }))
      .toEqual(['deposits', 'accounts', 'balance'])
  })
})

describe('hitCapabilities', () => {
  it('names the entity a grain key identifies', () => {
    expect(hitCapabilities({ ...COLUMN, is_grain: true, entity: 'Account' }))
      .toEqual([{ key: 'grain', label: 'Grain key for Account' }])
  })

  it('says only "Grain key" when no entity is resolved', () => {
    expect(hitCapabilities({ ...COLUMN, is_grain: true }))
      .toEqual([{ key: 'grain', label: 'Grain key' }])
  })

  it('reports an as-of field', () => {
    expect(hitCapabilities({ ...COLUMN, is_as_of: true }))
      .toEqual([{ key: 'as_of', label: 'As-of field' }])
  })

  it('renders additivity as prose, qualified by currency', () => {
    expect(hitCapabilities({ ...COLUMN, additivity: 'semi_additive', currency: 'USD', unit: 'dollars' }))
      .toEqual([{ key: 'measure', label: 'Semi-additive · USD' }])
  })

  it('falls back to the unit when there is no currency', () => {
    expect(hitCapabilities({ ...COLUMN, additivity: 'additive', unit: 'days' }))
      .toEqual([{ key: 'measure', label: 'Additive · days' }])
  })

  it('humanizes an additivity value it does not know', () => {
    expect(hitCapabilities({ ...COLUMN, additivity: 'ratio_like' }))
      .toEqual([{ key: 'measure', label: 'Ratio like' }])
  })

  it('lists grain, as-of and measure together in that order', () => {
    const caps = hitCapabilities({
      ...COLUMN, is_grain: true, is_as_of: true, entity: 'Account', additivity: 'additive',
    })
    expect(caps.map(c => c.key)).toEqual(['grain', 'as_of', 'measure'])
  })

  // The search response carries no review state, so a row can never claim one.
  it('claims nothing when the hit carries no roles', () => {
    expect(hitCapabilities(COLUMN)).toEqual([])
  })
})

describe('hitMeta', () => {
  it('carries data type, domain and the concept without an authority claim', () => {
    expect(hitMeta({ ...COLUMN, domain: 'retail', concept: 'account_balance' }))
      .toEqual(['numeric', 'retail', 'concept: account_balance'])
  })

  // The row renders a `table` badge of its own, so repeating the word here would print it twice.
  it('omits the kind — the row badges it', () => {
    expect(hitMeta(TABLE)).toEqual([])
  })

  it('names the entity when no grain line already does', () => {
    expect(hitMeta({ ...COLUMN, entity: 'Account' })).toEqual(['numeric', 'entity: Account'])
  })

  // "Grain key for Account" already names it; saying it twice on one row is noise.
  it('leaves the entity to the grain capability line when there is one', () => {
    expect(hitMeta({ ...COLUMN, is_grain: true, entity: 'Account' })).toEqual(['numeric'])
  })

  it('drops every field the catalog does not hold', () => {
    expect(hitMeta({ ...COLUMN, data_type: null })).toEqual([])
  })
})
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `cd frontend && npx vitest run src/screens/searchHitDisplay.test.ts`
Expected: FAIL — `Failed to resolve import "./searchHitDisplay"`.

- [ ] **Step 3: Write the implementation**

Create `frontend/src/screens/searchHitDisplay.ts`:

```ts
import type { SearchHit } from '../api'

/**
 * The name a result row leads with.
 *
 * NEVER a prettified identifier: `business_dt` stays `business_dt`. The search projection carries
 * no attested business name (`graph_node` has no business_term column), and inventing one —
 * "Business Date" — would put a machine-written label exactly where this platform's authority
 * model expects an attested value. When a business term is projected onto the search index (see
 * the deferred charter in the plan's §0.4), this is the ONE function that changes.
 */
export function hitDisplayName(hit: SearchHit): string {
  return hit.column ?? hit.table
}

/**
 * source › schema-qualified table › column — the physical address, demoted under the name but
 * never hidden: it is what a reader pastes into a query.
 */
export function hitBreadcrumb(hit: SearchHit): string[] {
  if (hit.column) {
    const cut = hit.object_ref.lastIndexOf('.')
    return [hit.catalog_source, cut > 0 ? hit.object_ref.slice(0, cut) : hit.table, hit.column]
  }
  return [hit.catalog_source, hit.object_ref]
}

export interface HitCapability {
  key: 'grain' | 'as_of' | 'measure'
  label: string
}

const ADDITIVITY_PROSE: Record<string, string> = {
  additive: 'Additive',
  semi_additive: 'Semi-additive',
  non_additive: 'Non-additive',
}

// An unknown additivity value is shown as it arrived, only made readable: underscores to spaces,
// first letter capitalized. Never dropped — a value we do not recognize is still a value the
// catalog asserted.
function additivityLabel(value: string): string {
  const known = ADDITIVITY_PROSE[value]
  if (known) return known
  const spaced = value.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

/**
 * What this asset can DO, derived only from fields the search hit carries.
 *
 * Deliberately absent: "N mappings need review", "temporal role needs review", "snapshot cutoff".
 * The /search response carries no review state, so those lines would be invented.
 */
export function hitCapabilities(hit: SearchHit): HitCapability[] {
  const capabilities: HitCapability[] = []
  if (hit.is_grain) {
    capabilities.push({
      key: 'grain',
      label: hit.entity ? `Grain key for ${hit.entity}` : 'Grain key',
    })
  }
  if (hit.is_as_of) capabilities.push({ key: 'as_of', label: 'As-of field' })
  if (hit.additivity) {
    const qualifier = hit.currency ?? hit.unit
    const label = additivityLabel(hit.additivity)
    capabilities.push({ key: 'measure', label: qualifier ? `${label} · ${qualifier}` : label })
  }
  return capabilities
}

/**
 * The quiet meta line: what the row still owes the reader after name, definition and capability.
 *
 * `concept` rides WITHOUT an authority claim. It is advisory enrichment (migration 0951), but the
 * hit carries no authority marker, so search can prove neither "AI proposed" nor "confirmed".
 */
export function hitMeta(hit: SearchHit): string[] {
  // The grain capability line reads "Grain key for Account", so repeating the entity here would
  // say it twice. On every OTHER row the entity has nowhere else to go — and `entity` is a facet
  // people filter by, so a row that matched `entity=Account` has to be able to say so.
  const namedByCapability = hit.is_grain && Boolean(hit.entity)
  return [
    // The kind is deliberately absent: the row badges a table as `table` already.
    hit.data_type,
    hit.domain,
    hit.entity && !namedByCapability ? `entity: ${hit.entity}` : null,
    hit.concept ? `concept: ${hit.concept}` : null,
  ].filter((part): part is string => Boolean(part))
}
```

- [ ] **Step 4: Run the test and watch it pass**

Run: `cd frontend && npx vitest run src/screens/searchHitDisplay.test.ts`
Expected: PASS (18 tests).

- [ ] **Step 5: Typecheck and lint**

Run: `cd frontend && npm run typecheck && npm run lint`
Expected: both clean.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/screens/searchHitDisplay.ts frontend/src/screens/searchHitDisplay.test.ts
git commit -m "feat(search): the display contract — what a result row may honestly say"
```

---

### Task 2: The result row — anatomy and action hierarchy

Meaning leads; the address is a breadcrumb; one primary action.

**Files:**
- Create: `frontend/src/screens/SearchHitRow.tsx`
- Create: `frontend/src/screens/SearchHitRow.test.tsx`
- Modify: `frontend/src/screens/SearchScreen.tsx` — delete the `HitRow` function (the last ~110 lines of the file) and render `SearchHitRow` in its place
- Modify: `frontend/src/screens/SearchScreen.test.tsx` — update the action-button names the screen-level tests query
- Modify: `frontend/src/index.css` — append the row styles

**Interfaces:**
- Consumes: `hitDisplayName`, `hitBreadcrumb`, `hitCapabilities`, `hitMeta` (Task 1); `featureImpact`, `ApiError`, `SearchHit` from `../api`.
- Produces, used by Task 3 (which adds the overflow) and by `SearchScreen`:
  ```ts
  export function SearchHitRow(props: {
    hit: SearchHit
    onOpen: (hit: SearchHit) => void
    onExplore: (hit: SearchHit) => void
    onSuggested: (hit: SearchHit) => void
  }): JSX.Element
  ```
  Accessible names of its controls: `Open asset {object_ref}`, `Explore relationships for {object_ref}`, `Suggested features for {table}`, `Feature impact for {object_ref}`.

- [ ] **Step 1: Write the failing test**

Create `frontend/src/screens/SearchHitRow.test.tsx`:

```tsx
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { SearchHitRow } from './SearchHitRow'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, featureImpact: vi.fn() }
})
const featureImpact = vi.mocked(api.featureImpact)

const HIT: api.SearchHit = {
  object_ref: 'public.accounts.balance', table: 'accounts', column: 'balance', kind: 'column',
  data_type: 'numeric', definition: 'end-of-day ledger balance', is_grain: false, is_as_of: false,
  catalog_source: 'deposits', concept: null, domain: null, sensitivity: null,
  sensitivity_display: null, additivity: null, unit: null, currency: null, entity: null, score: 1,
}

function renderRow(hit: api.SearchHit = HIT) {
  const onOpen = vi.fn()
  const onExplore = vi.fn()
  const onSuggested = vi.fn()
  render(<SearchHitRow hit={hit} onOpen={onOpen} onExplore={onExplore} onSuggested={onSuggested} />)
  return { onOpen, onExplore, onSuggested }
}

beforeEach(() => featureImpact.mockReset())

it('leads with the column name and demotes the physical ref to a breadcrumb', () => {
  renderRow()
  expect(screen.getByTestId('hit-name')).toHaveTextContent('balance')
  const trail = screen.getByTestId('hit-breadcrumb')
  expect(trail).toHaveTextContent('deposits')
  expect(trail).toHaveTextContent('public.accounts')
})

it('puts the definition in the reading position', () => {
  renderRow()
  expect(screen.getByText('end-of-day ledger balance')).toBeInTheDocument()
})

it('renders no definition element at all when the catalog holds none', () => {
  renderRow({ ...HIT, definition: null })
  expect(screen.queryByTestId('hit-definition')).toBeNull()
})

it('shows the roles the hit carries as capability lines', () => {
  renderRow({ ...HIT, is_grain: true, entity: 'Account', is_as_of: true })
  expect(screen.getByText('Grain key for Account')).toBeInTheDocument()
  expect(screen.getByText('As-of field')).toBeInTheDocument()
})

it('badges grain, as-of, table kind and both sensitivity axes', () => {
  renderRow({
    ...HIT, kind: 'table', column: null, is_grain: true, is_as_of: true,
    sensitivity: 'pii', sensitivity_display: 'restricted',
  })
  expect(screen.getByText('table')).toBeInTheDocument()
  expect(screen.getByText('grain')).toBeInTheDocument()
  expect(screen.getByText('as-of')).toBeInTheDocument()
  expect(screen.getByText('pii')).toBeInTheDocument()
  expect(screen.getByText('restricted')).toBeInTheDocument()
})

it('makes Open asset the primary action and Explore relationships the secondary', async () => {
  const { onOpen, onExplore } = renderRow()
  const open = screen.getByRole('button', { name: 'Open asset public.accounts.balance' })
  expect(open).toHaveClass('btn--primary')
  await userEvent.click(open)
  expect(onOpen).toHaveBeenCalledWith(HIT)

  const explore = screen.getByRole('button', {
    name: 'Explore relationships for public.accounts.balance',
  })
  expect(explore).not.toHaveClass('btn--primary')
  await userEvent.click(explore)
  expect(onExplore).toHaveBeenCalledWith(HIT)
})

it('opens suggested features for the hit’s table', async () => {
  const { onSuggested } = renderRow()
  await userEvent.click(screen.getByRole('button', { name: 'Suggested features for accounts' }))
  expect(onSuggested).toHaveBeenCalledWith(HIT)
})

it('lists derived feature ids inline when impact finds features', async () => {
  featureImpact.mockResolvedValue(['feat_01', 'feat_02'])
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  )
  expect(featureImpact).toHaveBeenCalledWith('public.accounts.balance', 'deposits')
  expect(await screen.findByText('feat_01')).toBeInTheDocument()
  expect(screen.getByText('feat_02')).toBeInTheDocument()
})

it('says plainly when nothing derives from the column', async () => {
  featureImpact.mockResolvedValue([])
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  )
  expect(await screen.findByText('No features derive from this column.')).toBeInTheDocument()
})

it('surfaces an impact failure as an alert without losing the row', async () => {
  featureImpact.mockRejectedValue(new api.ApiError(503, 'graph unavailable'))
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent('Impact check failed: graph unavailable')
  expect(screen.getByTestId('hit-name')).toHaveTextContent('balance')
})
```

- [ ] **Step 2: Run the test and watch it fail**

Run: `cd frontend && npx vitest run src/screens/SearchHitRow.test.tsx`
Expected: FAIL — `Failed to resolve import "./SearchHitRow"`.

- [ ] **Step 3: Write the row**

Create `frontend/src/screens/SearchHitRow.tsx`:

```tsx
import { Fragment, useState } from 'react'
import { ApiError, type SearchHit, featureImpact } from '../api'
import { hitBreadcrumb, hitCapabilities, hitDisplayName, hitMeta } from './searchHitDisplay'

/**
 * One search result.
 *
 * Anatomy, top to bottom: the name the reader is looking for, the physical address that name
 * resolves to, the definition (the business meaning the catalog actually holds), what the asset
 * can do, and the quiet remainder. Actions carry ONE primary — four equal buttons, which is what
 * this row used to be, is no hierarchy at all.
 */
export function SearchHitRow({
  hit,
  onOpen,
  onExplore,
  onSuggested,
}: {
  hit: SearchHit
  onOpen: (hit: SearchHit) => void
  onExplore: (hit: SearchHit) => void
  onSuggested: (hit: SearchHit) => void
}) {
  const [impact, setImpact] = useState<string[] | null>(null)
  const [impactError, setImpactError] = useState('')
  const [checking, setChecking] = useState(false)

  async function checkImpact() {
    setChecking(true)
    setImpactError('')
    try {
      setImpact(await featureImpact(hit.object_ref, hit.catalog_source))
    } catch (err) {
      setImpact(null)
      setImpactError(err instanceof ApiError ? err.detail : String(err))
    } finally {
      setChecking(false)
    }
  }

  const breadcrumb = hitBreadcrumb(hit)
  const capabilities = hitCapabilities(hit)
  const meta = hitMeta(hit)

  return (
    <li className="row hit">
      <div className="hit-main">
        <div className="hit-title">
          <span className="hit-name mono" data-testid="hit-name">{hitDisplayName(hit)}</span>
          {hit.kind === 'table' && <span className="badge kindtable">table</span>}
          {hit.is_grain && <span className="badge grain">grain</span>}
          {hit.is_as_of && <span className="badge asof">as-of</span>}
          {hit.sensitivity && <span className="badge sensitivity">{hit.sensitivity}</span>}
          {/* The projected display label, its OWN badge — never merged with the tag above: the two
              speak different vocabularies ('pii' vs 'restricted'), and on a catalog that declares
              no tag this is the only sensitivity a column has. */}
          {hit.sensitivity_display && (
            <span className="badge sensitivity">{hit.sensitivity_display}</span>
          )}
        </div>

        <p className="hit-breadcrumb mono" data-testid="hit-breadcrumb">
          {breadcrumb.map((part, i) => (
            <Fragment key={part}>
              {i > 0 && <span aria-hidden="true"> › </span>}
              {part}
            </Fragment>
          ))}
        </p>

        {hit.definition && (
          <p className="hit-definition" data-testid="hit-definition">{hit.definition}</p>
        )}

        {meta.length > 0 && <p className="hint hit-meta">{meta.join(' · ')}</p>}

        {checking && <p className="hint">Checking feature impact…</p>}
        {impactError && (
          <p role="alert" className="error">Impact check failed: {impactError}</p>
        )}
        {impact?.length === 0 && (
          <p className="hint" role="status">No features derive from this column.</p>
        )}
        {impact && impact.length > 0 && (
          <div className="hit-impact">
            <p className="micro-label">Derived features</p>
            <ul className="mono">
              {impact.map(id => <li key={id}>{id}</li>)}
            </ul>
          </div>
        )}
      </div>

      {capabilities.length > 0 && (
        <ul className="hit-capabilities">
          {capabilities.map(capability => (
            <li key={capability.key}>{capability.label}</li>
          ))}
        </ul>
      )}

      <div className="hit-actions">
        <button
          type="button"
          className="btn btn--primary"
          aria-label={`Open asset ${hit.object_ref}`}
          onClick={() => onOpen(hit)}
        >
          Open asset
        </button>
        <button
          type="button"
          className="btn"
          aria-label={`Explore relationships for ${hit.object_ref}`}
          onClick={() => onExplore(hit)}
        >
          Explore relationships
        </button>
        <button
          type="button"
          className="btn btn--ghost"
          aria-label={`Suggested features for ${hit.table}`}
          onClick={() => onSuggested(hit)}
        >
          Suggested features
        </button>
        <button
          type="button"
          className="btn btn--ghost"
          aria-label={`Feature impact for ${hit.object_ref}`}
          disabled={checking}
          onClick={() => void checkImpact()}
        >
          Feature impact
        </button>
      </div>
    </li>
  )
}
```

- [ ] **Step 4: Run the row test and watch it pass**

Run: `cd frontend && npx vitest run src/screens/SearchHitRow.test.tsx`
Expected: PASS (10 tests).

- [ ] **Step 5: Wire the row into the screen and delete the old one**

In `frontend/src/screens/SearchScreen.tsx`:

1. Replace the `HitRow` import surface — add `import { SearchHitRow } from './SearchHitRow'` and drop `featureImpact` from the `../api` import list (the row owns it now).
2. In the list branch, replace the `<HitRow …>` element with:

```tsx
<SearchHitRow
  key={`${hit.catalog_source}:${hit.object_ref}`}
  hit={hit}
  onOpen={openDetails}
  onExplore={jumpToGraph}
  onSuggested={openSuggested}
/>
```

3. Delete the entire `function HitRow({ … }) { … }` declaration at the bottom of the file.

- [ ] **Step 6: Update the screen-level tests to the new action names**

In `frontend/src/screens/SearchScreen.test.tsx`, rename every queried action:

| Old accessible name | New accessible name |
|---|---|
| `Details for public.accounts.balance` | `Open asset public.accounts.balance` |
| `Graph for public.accounts.balance` | `Explore relationships for public.accounts.balance` |
| `Impact for public.accounts.balance` | `Feature impact for public.accounts.balance` |
| `Suggested features for accounts` | unchanged |

The impact-behavior tests (`lists derived feature ids inline…`, the empty case, the failure case)
are now covered by `SearchHitRow.test.tsx`. Delete those three from `SearchScreen.test.tsx` rather
than maintaining them twice; keep the screen-level test that asserts the *navigation* wiring
(`Open asset` → `#/asset`, `Suggested features` → `#/suggested`, `Explore relationships` → graph).

- [ ] **Step 7: Append the row CSS**

At the END of `frontend/src/index.css`:

```css
/* ---- catalog search (2026-08-16 redesign) --------------------------------------------------
   The result row: meaning leads, the physical address is a breadcrumb, one primary action.
   Existing tokens only; composes the .row / .badge / .btn vocabulary. Nothing above is modified. */

.rows .row.hit {
  display: grid;
  grid-template-columns: minmax(0, 1fr) minmax(0, 210px) auto;
  gap: 16px;
  align-items: start;
  padding: 14px 16px;
}

.hit-main { min-width: 0; display: grid; gap: 4px; }

.hit-title { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }

.hit-name {
  font-size: 14px;
  font-weight: 600;
  color: var(--ink);
  overflow-wrap: anywhere;
}

.hit-breadcrumb {
  margin: 0;
  font-size: 11px;
  color: var(--ink-faint);
  overflow-wrap: anywhere;
}

.hit-definition {
  margin: 4px 0 0;
  font-size: 13px;
  line-height: 1.5;
  color: var(--ink-soft);
  display: -webkit-box;
  -webkit-box-orient: vertical;
  -webkit-line-clamp: 2;
  line-clamp: 2;
  overflow: hidden;
}

.hit-meta { margin: 2px 0 0; }

.hit-capabilities {
  margin: 0;
  padding: 0;
  list-style: none;
  display: grid;
  gap: 6px;
  align-content: start;
  font-size: 12px;
  color: var(--ink-soft);
}

.hit-capabilities li { display: flex; align-items: baseline; gap: 7px; }

.hit-capabilities li::before {
  content: '';
  flex: none;
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent-line);
}

.hit-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; }

.hit-impact { margin-top: 6px; }
.hit-impact ul { margin: 2px 0 0; padding-left: 18px; display: grid; gap: 2px; }
```

- [ ] **Step 8: Run the full frontend suite**

Run: `cd frontend && npm test && npm run typecheck && npm run lint`
Expected: all green — 870 baseline tests minus the 3 relocated impact tests, plus 10 row tests.

- [ ] **Step 9: Commit**

```bash
git add frontend/src/screens/SearchHitRow.tsx frontend/src/screens/SearchHitRow.test.tsx \
        frontend/src/screens/SearchScreen.tsx frontend/src/screens/SearchScreen.test.tsx \
        frontend/src/index.css
git commit -m "feat(search): the result row leads with meaning and one primary action"
```

---

### Task 3: Fold the tertiary actions into an overflow disclosure

Two buttons stay on the row; the rest move behind `···`.

**Files:**
- Modify: `frontend/src/screens/SearchHitRow.tsx`
- Modify: `frontend/src/screens/SearchHitRow.test.tsx`
- Modify: `frontend/src/index.css` (append to the search block)

**Interfaces:**
- Consumes: `SearchHitRow` from Task 2 (same props — the signature does not change).
- Produces: the same accessible names as Task 2 for Suggested features / Feature impact, now
  inside a disclosure whose trigger is named `More actions for {object_ref}`. Adds a
  `Copy reference for {object_ref}` item.
- Deliberately a **disclosure**, not an ARIA `menu`: `role="menu"` promises arrow-key roving that
  a three-item popover does not need. `aria-expanded` on the trigger plus Escape-to-close and
  focus return is the honest contract.

- [ ] **Step 1: Write the failing tests**

Append to `frontend/src/screens/SearchHitRow.test.tsx`:

```tsx
it('hides the tertiary actions behind an overflow disclosure', async () => {
  renderRow()
  expect(screen.queryByRole('button', { name: 'Suggested features for accounts' })).toBeNull()

  const trigger = screen.getByRole('button', { name: 'More actions for public.accounts.balance' })
  expect(trigger).toHaveAttribute('aria-expanded', 'false')
  await userEvent.click(trigger)
  expect(trigger).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByRole('button', { name: 'Suggested features for accounts' })).toBeInTheDocument()
  expect(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  ).toBeInTheDocument()
})

it('closes the overflow on Escape and returns focus to its trigger', async () => {
  renderRow()
  const trigger = screen.getByRole('button', { name: 'More actions for public.accounts.balance' })
  await userEvent.click(trigger)
  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('button', { name: 'Suggested features for accounts' })).toBeNull()
  expect(trigger).toHaveFocus()
})

it('closes the overflow when the pointer goes elsewhere', async () => {
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
  await userEvent.click(document.body)
  expect(screen.queryByRole('button', { name: 'Suggested features for accounts' })).toBeNull()
})

it('closes the overflow after an item is chosen', async () => {
  const { onSuggested } = renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
  await userEvent.click(screen.getByRole('button', { name: 'Suggested features for accounts' }))
  expect(onSuggested).toHaveBeenCalledWith(HIT)
  expect(screen.queryByRole('button', { name: 'Suggested features for accounts' })).toBeNull()
})

it('copies the object ref and says so', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.assign(navigator, { clipboard: { writeText } })
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
  await userEvent.click(
    screen.getByRole('button', { name: 'Copy reference for public.accounts.balance' }),
  )
  expect(writeText).toHaveBeenCalledWith('public.accounts.balance')
  expect(await screen.findByRole('status')).toHaveTextContent('Reference copied')
})

it('shows the reference when the browser refuses to copy it', async () => {
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
  })
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
  await userEvent.click(
    screen.getByRole('button', { name: 'Copy reference for public.accounts.balance' }),
  )
  expect(await screen.findByRole('status'))
    .toHaveTextContent('Could not copy. The reference is public.accounts.balance')
})
```

Also update the Task 2 test `makes Open asset the primary action…` — it stays as written (both
buttons remain on the row) — and update `opens suggested features for the hit’s table` and the
three impact tests to open the overflow first, e.g.:

```tsx
async function openOverflow() {
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
}
```

- [ ] **Step 2: Run the tests and watch them fail**

Run: `cd frontend && npx vitest run src/screens/SearchHitRow.test.tsx`
Expected: FAIL — no button named `More actions for public.accounts.balance`.

- [ ] **Step 3: Add the disclosure**

In `frontend/src/screens/SearchHitRow.tsx`, add the imports `useEffect`, `useRef`, and
`type ReactNode`, then add this component below `SearchHitRow`:

```tsx
/**
 * The row's overflow. A DISCLOSURE, not an ARIA menu: `role="menu"` promises arrow-key roving
 * that a three-item popover does not need and we would not implement honestly. What it does
 * promise it keeps — Escape closes and returns focus, a pointer elsewhere closes, and choosing an
 * item closes.
 */
function RowOverflow({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false)
  const wrap = useRef<HTMLDivElement>(null)
  const trigger = useRef<HTMLButtonElement>(null)

  useEffect(() => {
    if (!open) return
    function onPointerDown(event: PointerEvent) {
      if (!wrap.current?.contains(event.target as Node)) setOpen(false)
    }
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== 'Escape') return
      setOpen(false)
      trigger.current?.focus()
    }
    document.addEventListener('pointerdown', onPointerDown)
    document.addEventListener('keydown', onKeyDown)
    return () => {
      document.removeEventListener('pointerdown', onPointerDown)
      document.removeEventListener('keydown', onKeyDown)
    }
  }, [open])

  return (
    <div className="hit-overflow" ref={wrap}>
      <button
        type="button"
        ref={trigger}
        className="btn btn--ghost hit-overflow-trigger"
        aria-label={label}
        aria-expanded={open}
        onClick={() => setOpen(value => !value)}
      >
        ···
      </button>
      {open && (
        // Choosing anything closes the popover: the click bubbles to this wrapper, so one handler
        // covers every item without each item having to remember to close.
        <div className="hit-overflow-items" onClick={() => setOpen(false)}>
          {children}
        </div>
      )}
    </div>
  )
}
```

Replace the two tertiary buttons in `.hit-actions` with:

```tsx
<RowOverflow label={`More actions for ${hit.object_ref}`}>
  <button
    type="button"
    aria-label={`Suggested features for ${hit.table}`}
    onClick={() => onSuggested(hit)}
  >
    Suggested features
  </button>
  <button
    type="button"
    aria-label={`Feature impact for ${hit.object_ref}`}
    disabled={checking}
    onClick={() => void checkImpact()}
  >
    Feature impact
  </button>
  <button
    type="button"
    aria-label={`Copy reference for ${hit.object_ref}`}
    onClick={() => void copyReference()}
  >
    Copy reference
  </button>
</RowOverflow>
```

And add the copy behavior to `SearchHitRow`'s body:

```tsx
  const [copyStatus, setCopyStatus] = useState('')

  async function copyReference() {
    try {
      await navigator.clipboard.writeText(hit.object_ref)
      setCopyStatus('Reference copied')
    } catch {
      // A browser may refuse clipboard access outright. Saying "copied" would be a lie, and
      // saying only "failed" leaves the reader with nothing, so hand them the reference itself.
      setCopyStatus(`Could not copy. The reference is ${hit.object_ref}`)
    }
  }
```

Render it inside `.hit-main`, after the meta line:

```tsx
        {copyStatus && <p className="hint" role="status">{copyStatus}</p>}
```

- [ ] **Step 4: Run the tests and watch them pass**

Run: `cd frontend && npx vitest run src/screens/SearchHitRow.test.tsx`
Expected: PASS (16 tests).

- [ ] **Step 5: Style the disclosure**

Append to the search block in `frontend/src/index.css`:

```css
.hit-overflow { position: relative; }

.hit-overflow-trigger { min-width: 34px; letter-spacing: 0.08em; }

.hit-overflow-items {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  z-index: 20;
  display: grid;
  gap: 2px;
  min-width: 200px;
  padding: 6px;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: var(--surface);
  box-shadow: var(--shadow);
}

.hit-overflow-items button {
  display: flex;
  align-items: center;
  width: 100%;
  min-height: 32px;
  padding: 0 10px;
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ink);
  font-size: 13px;
  text-align: left;
}

.hit-overflow-items button:hover:not(:disabled) { background: var(--surface-2); }
.hit-overflow-items button:disabled { color: var(--ink-faint); cursor: not-allowed; }
```

- [ ] **Step 6: Full suite, typecheck, lint**

Run: `cd frontend && npm test && npm run typecheck && npm run lint`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/screens/SearchHitRow.tsx frontend/src/screens/SearchHitRow.test.tsx \
        frontend/src/index.css
git commit -m "feat(search): tertiary row actions move behind an overflow disclosure"
```

---

### Task 4: A facet panel that follows the server

Render the facet groups `/search` actually returns — including the six the client cannot even send today.

**Files:**
- Modify: `frontend/src/api.ts` — widen `SEARCH_FACET_KEYS`
- Create: `frontend/src/screens/SearchFacetPanel.tsx`
- Create: `frontend/src/screens/SearchFacetPanel.test.tsx`
- Modify: `frontend/src/screens/SearchScreen.tsx` — replace the inline `<aside className="facet-panel">` block with the component
- Modify: `frontend/src/index.css` (append)

**Interfaces:**
- Consumes: `FacetBucket`, `SearchFacetKey`, `SearchFilters` from `../api`.
- Produces:
  ```ts
  export function SearchFacetPanel(props: {
    facets: Record<string, FacetBucket[]>
    filters: SearchFilters
    onToggleFacet: (key: SearchFacetKey, value: string) => void
    onToggleFlag: (key: 'grain' | 'as_of') => void
  }): JSX.Element | null
  ```
  Returns `null` when there is nothing to filter by.

- [ ] **Step 1: Widen the client's facet keys**

In `frontend/src/api.ts`, replace the `SEARCH_FACET_KEYS` declaration with:

```ts
// The repeated-value facet groups, in the order they ride the /search query string. AND across
// groups, OR within one. grain/as_of are boolean flags carried separately (=true restricts).
//
// The last six are the dataset-profile facets. The route ACCEPTS them unconditionally and applies
// them only while `FEATUREGEN_DATASET_PROFILES` is on (`column_facets()` is the one definition of
// what is active), so sending them is safe on every deployment: with the flag off the server
// ignores them exactly as it ignores any facet it does not have. Before this they were absent from
// the client entirely, which made the groups the server returns for them un-selectable.
export const SEARCH_FACET_KEYS = [
  'source', 'domain', 'sensitivity', 'sensitivity_display', 'additivity', 'entity', 'kind',
  'data_role', 'authority_role', 'temporal_storage_model', 'bian_path', 'process_path',
  'sub_domain',
] as const
```

- [ ] **Step 2: Write the failing panel test**

Create `frontend/src/screens/SearchFacetPanel.test.tsx`:

```tsx
import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { expect, it, vi } from 'vitest'
import type { FacetBucket } from '../api'
import { SearchFacetPanel } from './SearchFacetPanel'

function renderPanel(
  facets: Record<string, FacetBucket[]>,
  filters: Parameters<typeof SearchFacetPanel>[0]['filters'] = {},
) {
  const onToggleFacet = vi.fn()
  const onToggleFlag = vi.fn()
  render(
    <SearchFacetPanel
      facets={facets}
      filters={filters}
      onToggleFacet={onToggleFacet}
      onToggleFlag={onToggleFlag}
    />,
  )
  return { onToggleFacet, onToggleFlag }
}

function group(name: string) {
  return within(screen.getByRole('group', { name }))
}

it('renders a group for every facet the server returned', () => {
  renderPanel({
    source: [{ value: 'deposits', count: 3 }],
    data_role: [{ value: 'crosswalk', count: 2 }],
  })
  expect(screen.getByRole('group', { name: 'Source' })).toBeInTheDocument()
  // The backend's data_role is a TABLE role; the grain/as-of flags are the column axis. Two
  // different questions, so two different labels.
  expect(screen.getByRole('group', { name: 'Table role' })).toBeInTheDocument()
})

it('humanizes a facet key it has no label for', () => {
  renderPanel({ risk_tier: [{ value: 'high', count: 1 }] })
  expect(screen.getByRole('group', { name: 'Risk tier' })).toBeInTheDocument()
})

it('reports each value with its count and toggles it', async () => {
  const { onToggleFacet } = renderPanel({ source: [{ value: 'deposits', count: 3 }] })
  const option = group('Source').getByRole('checkbox', { name: /deposits/ })
  expect(group('Source').getByText('3')).toBeInTheDocument()
  await userEvent.click(option)
  expect(onToggleFacet).toHaveBeenCalledWith('source', 'deposits')
})

it('renders the NULL bucket as "Not classified", pinned last', () => {
  renderPanel({
    domain: [{ value: '(none)', count: 90 }, { value: 'retail', count: 3 }],
  })
  const labels = group('Domain').getAllByRole('checkbox').map(box => box.closest('label')?.textContent)
  expect(labels?.[0]).toMatch(/retail/)
  expect(labels?.[1]).toMatch(/Not classified/)
})

it('shows six values and expands the rest on request', async () => {
  renderPanel({
    domain: Array.from({ length: 9 }, (_, i) => ({ value: `d${i}`, count: 9 - i })),
  })
  expect(group('Domain').getAllByRole('checkbox')).toHaveLength(6)
  await userEvent.click(group('Domain').getByRole('button', { name: 'Show all 9' }))
  expect(group('Domain').getAllByRole('checkbox')).toHaveLength(9)
})

it('keeps "Not classified" out of the collapsed window but still reachable', () => {
  renderPanel({
    domain: [
      { value: '(none)', count: 400 },
      ...Array.from({ length: 8 }, (_, i) => ({ value: `d${i}`, count: 8 - i })),
    ],
  })
  const labels = group('Domain').getAllByRole('checkbox').map(box => box.closest('label')?.textContent)
  // Six NAMED values plus the NULL bucket pinned after them: the collapsed window is six, and
  // "Not classified" never eats one of those six — but it stays selectable without expanding,
  // because "show me the unclassified columns" is a real question a steward asks.
  expect(labels).toHaveLength(7)
  expect(labels?.slice(0, 6).some(text => text?.includes('Not classified'))).toBe(false)
  expect(labels?.[6]).toMatch(/Not classified/)
  expect(group('Domain').getByRole('button', { name: 'Show all 9' })).toBeInTheDocument()
})

it('reflects the selected state of a filter', () => {
  renderPanel({ source: [{ value: 'deposits', count: 3 }] }, { source: ['deposits'] })
  expect(group('Source').getByRole('checkbox', { name: /deposits/ })).toBeChecked()
})

it('offers the column-role flags with their counts', async () => {
  const { onToggleFlag } = renderPanel({
    grain: [{ value: 'true', count: 2 }],
    as_of: [{ value: 'true', count: 1 }],
  })
  const flags = group('Column role')
  await userEvent.click(flags.getByRole('checkbox', { name: /Grain key/ }))
  expect(onToggleFlag).toHaveBeenCalledWith('grain')
  expect(flags.getByRole('checkbox', { name: /As-of field/ })).toBeInTheDocument()
})

it('disables a flag that cannot narrow anything and is not already picked', () => {
  renderPanel({ grain: [{ value: 'true', count: 0 }] })
  expect(group('Column role').getByRole('checkbox', { name: /Grain key/ })).toBeDisabled()
})

it('renders nothing at all when the server returned no facets', () => {
  const { container } = render(
    <SearchFacetPanel facets={{}} filters={{}} onToggleFacet={vi.fn()} onToggleFlag={vi.fn()} />,
  )
  expect(container).toBeEmptyDOMElement()
})
```

- [ ] **Step 3: Run it and watch it fail**

Run: `cd frontend && npx vitest run src/screens/SearchFacetPanel.test.tsx`
Expected: FAIL — `Failed to resolve import "./SearchFacetPanel"`.

- [ ] **Step 4: Write the panel**

Create `frontend/src/screens/SearchFacetPanel.tsx`:

```tsx
import { useState } from 'react'
import { SEARCH_FACET_KEYS, type FacetBucket, type SearchFacetKey, type SearchFilters } from '../api'

// The label a facet key wears in the sidebar. A key that is not here still renders — humanized —
// because the server is allowed to grow a facet without the UI shipping first.
const FACET_LABELS: Record<string, string> = {
  source: 'Source',
  kind: 'Kind',
  domain: 'Domain',
  sub_domain: 'Sub-domain',
  entity: 'Entity',
  // The backend's `data_role` is the TABLE role projection (crosswalk, reference, event…). The
  // column axis — grain / as-of — is the flags group below, titled "Column role". Two different
  // questions must not share one word.
  data_role: 'Table role',
  authority_role: 'Authority role',
  temporal_storage_model: 'Temporal storage',
  additivity: 'Additivity',
  // The projected display axis a user means by "sensitivity", and the raw tag a source file
  // declared, which is empty on catalogs that declare none. Named apart on purpose.
  sensitivity_display: 'Sensitivity',
  sensitivity: 'Declared tag',
  bian_path: 'BIAN path',
  process_path: 'Business process',
}

// Known keys lead, in reading order; anything the server adds later follows, alphabetically.
const FACET_ORDER: string[] = [
  'source', 'kind', 'domain', 'sub_domain', 'entity', 'data_role', 'authority_role',
  'temporal_storage_model', 'additivity', 'sensitivity_display', 'sensitivity', 'bian_path',
  'process_path',
]

const NONE = '(none)'          // the server's NULL bucket, and the token that selects IS NULL
const COLLAPSED = 6            // values shown before "Show all"

const FLAG_OPTIONS: { key: 'grain' | 'as_of'; label: string }[] = [
  { key: 'grain', label: 'Grain key' },
  { key: 'as_of', label: 'As-of field' },
]

/**
 * The sidebar label for a facet key. EXPORTED because the screen's active-filter chips name the
 * same facets: without one owner of this vocabulary a chip reads "sensitivity display: restricted"
 * while the group above it reads "Sensitivity".
 */
export function facetLabel(key: string): string {
  const known = FACET_LABELS[key]
  if (known) return known
  const spaced = key.replace(/_/g, ' ')
  return spaced.charAt(0).toUpperCase() + spaced.slice(1)
}

function isFacetKey(key: string): key is SearchFacetKey {
  return (SEARCH_FACET_KEYS as readonly string[]).includes(key)
}

/**
 * The filter sidebar, driven by the facet map the SERVER returned rather than a hardcoded list.
 *
 * Two rules make it readable on a real catalog. First, the NULL bucket is a value, not a
 * headline: on a 150K-column catalog "(none)" is usually the largest count in the group, and
 * letting it lead makes every group look like it holds nothing. It is pinned after the named
 * values, labelled "Not classified", and never consumes one of the six collapsed slots — but it
 * stays selectable without expanding, because "show me the unclassified columns" is a real
 * question. Second, groups collapse to six named values, because a fifty-value group is a wall,
 * not a filter.
 */
export function SearchFacetPanel({
  facets,
  filters,
  onToggleFacet,
  onToggleFlag,
}: {
  facets: Record<string, FacetBucket[]>
  filters: SearchFilters
  onToggleFacet: (key: SearchFacetKey, value: string) => void
  onToggleFlag: (key: 'grain' | 'as_of') => void
}) {
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})

  const valueKeys = Object.keys(facets)
    .filter(key => key !== 'grain' && key !== 'as_of' && (facets[key]?.length ?? 0) > 0)
    .sort((a, b) => {
      const ai = FACET_ORDER.indexOf(a)
      const bi = FACET_ORDER.indexOf(b)
      if (ai !== -1 && bi !== -1) return ai - bi
      if (ai !== -1) return -1
      if (bi !== -1) return 1
      return a.localeCompare(b)
    })

  const flagBuckets = { grain: facets.grain?.[0], as_of: facets.as_of?.[0] }
  const showFlags = Boolean(flagBuckets.grain || flagBuckets.as_of)
  if (valueKeys.length === 0 && !showFlags) return null

  return (
    <aside className="facet-panel" aria-label="Filters">
      {valueKeys.map(key => {
        const buckets = facets[key] ?? []
        // The server orders by count desc; the NULL bucket is pulled out of that order entirely.
        const named = buckets.filter(bucket => bucket.value !== NONE)
        const none = buckets.find(bucket => bucket.value === NONE)
        const isOpen = expanded[key] ?? false
        const shown = isOpen ? named : named.slice(0, COLLAPSED)
        const hidden = named.length - shown.length
        const selected = isFacetKey(key) ? (filters[key] ?? []) : []
        return (
          <fieldset className="facet-group" key={key}>
            <legend className="facet-group-title">{facetLabel(key)}</legend>
            {[...shown, ...(none ? [none] : [])].map(bucket => (
              <label className="facet-option" key={bucket.value}>
                <input
                  type="checkbox"
                  checked={selected.includes(bucket.value)}
                  disabled={!isFacetKey(key)}
                  onChange={() => isFacetKey(key) && onToggleFacet(key, bucket.value)}
                />
                <span className="facet-name">
                  {bucket.value === NONE ? 'Not classified' : bucket.value}
                </span>{' '}
                <span className="facet-count tabular-nums">{bucket.count}</span>
              </label>
            ))}
            {!isOpen && hidden > 0 && (
              <button
                type="button"
                className="facet-more"
                onClick={() => setExpanded(state => ({ ...state, [key]: true }))}
              >
                {/* The count names the whole group, NULL bucket included, because that is what
                    expanding reveals. */}
                Show all {named.length + (none ? 1 : 0)}
              </button>
            )}
          </fieldset>
        )
      })}

      {showFlags && (
        <fieldset className="facet-group">
          {/* The COLUMN axis. Named apart from the server's `data_role`, which is a table role. */}
          <legend className="facet-group-title">Column role</legend>
          {FLAG_OPTIONS.map(flag => {
            const count = flagBuckets[flag.key]?.count ?? 0
            const checked = Boolean(filters[flag.key])
            // A flag with no matching rows and not already picked cannot narrow further.
            const disabled = count === 0 && !checked
            return (
              <label
                className={disabled ? 'facet-option facet-option--disabled' : 'facet-option'}
                key={flag.key}
              >
                <input
                  type="checkbox"
                  checked={checked}
                  disabled={disabled}
                  onChange={() => onToggleFlag(flag.key)}
                />
                <span className="facet-name">{flag.label}</span>{' '}
                <span className="facet-count tabular-nums">{count}</span>
              </label>
            )
          })}
        </fieldset>
      )}
    </aside>
  )
}
```

- [ ] **Step 5: Run the panel test and watch it pass**

Run: `cd frontend && npx vitest run src/screens/SearchFacetPanel.test.tsx`
Expected: PASS (10 tests).

- [ ] **Step 6: Wire it into the screen**

In `frontend/src/screens/SearchScreen.tsx`:

1. Add `import { SearchFacetPanel } from './SearchFacetPanel'`.
2. Delete the `FACET_GROUPS` and `FLAG_OPTIONS` constants and the `flagBuckets` / `showFlags`
   locals — the panel owns all of that now.
3. Replace the whole `<aside className="facet-panel" …>…</aside>` block with:

```tsx
        {effectiveView === 'list' && (
          <SearchFacetPanel
            facets={result?.facets ?? {}}
            filters={filters}
            onToggleFacet={toggleFacet}
            onToggleFlag={toggleFlag}
          />
        )}
```

4. The active-filter chips still need group labels. Replace the `FACET_GROUPS` loop that builds
   `chips` with an iteration over the selected filter keys:

```tsx
  // Active-filter chips, in the order the facet keys ride the query string, then flags. The chip
  // wears the SAME label the sidebar group wears — `facetLabel` is the one owner of that
  // vocabulary, so a chip can never read "sensitivity display" under a group reading "Sensitivity".
  const chips: { id: string; label: string; pii: boolean; remove: () => void }[] = []
  for (const key of SEARCH_FACET_KEYS) {
    for (const value of filters[key] ?? []) {
      chips.push({
        id: `${key}:${value}`,
        label: `${facetLabel(key).toLowerCase()}: ${value === '(none)' ? 'not classified' : value}`,
        pii: key === 'sensitivity' && value === 'pii',
        remove: () => toggleFacet(key, value),
      })
    }
  }
```

Import it alongside the panel: `import { SearchFacetPanel, facetLabel } from './SearchFacetPanel'`.

- [ ] **Step 7: Update the screen tests for the new group labels**

In `frontend/src/screens/SearchScreen.test.tsx`, the sidebar assertions now read
`Sensitivity` / `Declared tag` / `Column role` (was `Flags`), and the flag options are named
`Grain key` / `As-of field` (was `Grain` / `As-of`). Update those queries. The `(none)` bucket in
the `sensitivity` fixture now renders as `Not classified` — it is pinned last in its group and
needs no expanding (the group has one named value, so no "Show all" button appears at all).
Active-filter chips built from it read `declared tag: not classified`.

- [ ] **Step 8: Style the panel additions**

Append to the search block in `frontend/src/index.css`:

```css
.facet-more {
  margin-top: 4px;
  padding: 2px 0;
  border: 0;
  background: transparent;
  color: var(--accent);
  font-size: 12px;
  font-weight: 600;
  text-align: left;
}

.facet-more:hover { color: var(--accent-hover); text-decoration: underline; }
```

- [ ] **Step 9: Full suite, typecheck, lint**

Run: `cd frontend && npm test && npm run typecheck && npm run lint`
Expected: green.

- [ ] **Step 10: Commit**

```bash
git add frontend/src/api.ts frontend/src/screens/SearchFacetPanel.tsx \
        frontend/src/screens/SearchFacetPanel.test.tsx frontend/src/screens/SearchScreen.tsx \
        frontend/src/screens/SearchScreen.test.tsx frontend/src/index.css
git commit -m "feat(search): the filter panel follows the server's facet map"
```

---

### Task 5: The results toolbar — honest counts and a projection that is finally visible

**Files:**
- Modify: `frontend/src/screens/SearchScreen.tsx`
- Modify: `frontend/src/screens/SearchScreen.test.tsx`
- Modify: `frontend/src/index.css` (append)

**Interfaces:**
- Consumes: `SearchResult.projection` (`{ status: 'ready' | 'lagged'; code: string; detail: string }`),
  already on the response and already in `SearchResult`.
- Produces: no new exports. Test hooks: `data-testid="search-projection"` on the ready line,
  `role="status"` on the count line, and the lagged callout addressable by its text.

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/screens/SearchScreen.test.tsx` (inside the results describe block):

```tsx
it('counts the permitted set and names the slice on screen', async () => {
  searchCatalog.mockResolvedValue(result([HIT], FACETS, 42))
  render(<SearchScreen />)
  const status = await screen.findByRole('status')
  expect(status).toHaveTextContent('42 assets')
  expect(status).toHaveTextContent('showing 1–1')
  expect(screen.getByText('Showing 1–1 of 42 permitted assets')).toBeInTheDocument()
})

it('says the projection is current when the server says it is', async () => {
  render(<SearchScreen />)
  expect(await screen.findByTestId('search-projection'))
    .toHaveTextContent('Catalog projection current')
})

it('discloses a lagged projection above the results without hiding them', async () => {
  searchCatalog.mockResolvedValue({
    hits: [HIT], facets: FACETS, total: 1,
    projection: { status: 'lagged', code: 'CATALOG_PROJECTION_BEHIND', detail: 'overlay is behind' },
  })
  render(<SearchScreen />)
  expect(await screen.findByText(/catalog projection was behind/i)).toBeInTheDocument()
  // The rows are still served: a disclosure, never a refusal.
  expect(screen.getByTestId('hit-name')).toHaveTextContent('balance')
  expect(screen.queryByTestId('search-projection')).toBeNull()
})
```

Update the two existing count assertions: `'2 results'` becomes `'2 assets'`, and
`'42 results'` / `'showing 1–1 of 42'` are replaced by the test above (delete the old one).

- [ ] **Step 2: Run and watch them fail**

Run: `cd frontend && npx vitest run src/screens/SearchScreen.test.tsx`
Expected: FAIL — no `search-projection` testid; count reads "results".

- [ ] **Step 3: Rewrite the toolbar**

In `frontend/src/screens/SearchScreen.tsx`, replace the result-count paragraph with a toolbar that
carries the count, the slice and the projection state, and add the lagged callout above the rows:

```tsx
          {!error && hasHits && effectiveView === 'list' && (
            <div className="results-toolbar">
              <p className="micro-label tabular-nums result-count" role="status">
                <span style={{ color: 'var(--accent)', fontWeight: 600 }}>{result.total}</span>{' '}
                {result.total === 1 ? 'asset' : 'assets'}
                {result.total > result.hits.length && (
                  // The SLICE, not just a count: with paging, "showing the first 20" stopped being
                  // true the moment the user moved forward, and a bare count gives no way to tell
                  // which 20 are on screen.
                  <span className="result-count-note">
                    {' '}· showing {offset + 1}–{offset + result.hits.length}
                  </span>
                )}
              </p>
              {/* Task 6's projection marker, finally rendered. Search reads the projected display
                  columns, so a lagged projection means these rows may not reflect the newest
                  resolved semantics — and until now the screen said nothing at all about it. */}
              {result.projection.status === 'ready' && (
                <p className="projection-ok" data-testid="search-projection">
                  Catalog projection current
                </p>
              )}
            </div>
          )}

          {!error && hasHits && effectiveView === 'list'
            && result.projection.status === 'lagged' && (
            <div className="callout callout--warn" role="status">
              <CalloutGlyph>
                <circle cx="8" cy="8" r="6.25" />
                <path d="M8 4.75v4M8 11.25v.01" />
              </CalloutGlyph>
              <div className="callout-body">
                <p>
                  The catalog projection was behind when these results were read, so they may not
                  yet reflect the newest resolved semantics.
                </p>
              </div>
            </div>
          )}
```

The house callout is a two-column grid (`auto 1fr`) that expects a glyph and a body — a bare `<p>`
with those classes renders wrong. `CalloutGlyph` is the shared component the other screens use, so
add its import at the top of `SearchScreen.tsx`:

```tsx
import { CalloutGlyph } from './IngestResultCallout'
```

And in the pager, add the slice copy before the buttons:

```tsx
            <nav className="pager" aria-label="Result pages">
              <span className="pager-copy tabular-nums">
                Showing {offset + 1}–{offset + result.hits.length} of {result.total} permitted assets
              </span>
```

Note the pager only renders when there is more than one page; the `Showing …` line above therefore
appears exactly when paging is possible, which is when it earns its space.

- [ ] **Step 4: Run and watch them pass**

Run: `cd frontend && npx vitest run src/screens/SearchScreen.test.tsx`
Expected: PASS.

- [ ] **Step 5: Style the toolbar**

Append to the search block in `frontend/src/index.css`:

```css
.results-toolbar {
  display: flex;
  flex-wrap: wrap;
  align-items: baseline;
  justify-content: space-between;
  gap: 12px;
}

.projection-ok {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  margin: 0;
  color: var(--ink-faint);
  font-size: 11px;
}

.projection-ok::before {
  content: '';
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--ok);
}

.pager-copy { color: var(--ink-faint); font-size: 12px; margin-right: auto; }
```

Do **not** add callout styles: `.callout`, `.callout-glyph`, `.callout-body` and `.callout--warn`
already exist (`frontend/src/index.css:427` and `:510`). The markup above composes them as-is.

- [ ] **Step 6: Full suite, typecheck, lint**

Run: `cd frontend && npm test && npm run typecheck && npm run lint`
Expected: green.

- [ ] **Step 7: Commit**

```bash
git add frontend/src/screens/SearchScreen.tsx frontend/src/screens/SearchScreen.test.tsx \
        frontend/src/index.css
git commit -m "feat(search): honest counts, and the projection state the screen never showed"
```

---

### Task 6: The search bar, the empty state, and retiring the global Graph toggle

**Files:**
- Modify: `frontend/src/screens/SearchScreen.tsx`
- Modify: `frontend/src/screens/SearchScreen.test.tsx`
- Modify: `frontend/src/index.css` (append)

**Interfaces:**
- Consumes: `apply(nextQ, nextFilters)`, `jumpToGraph(hit)`, `setView`, already in the screen.
- Produces: no new exports. The `view` state stops being user-settable to `graph` from the toolbar;
  it is set only by `jumpToGraph` and cleared by `LineageView`'s `onBackToResults`.

- [ ] **Step 1: Write the failing tests**

Add to `frontend/src/screens/SearchScreen.test.tsx`:

```tsx
it('offers no global view toggle — a graph needs an anchor, so it is entered from a row', async () => {
  render(<SearchScreen />)
  await screen.findByTestId('hit-name')
  expect(screen.queryByRole('group', { name: 'Result view' })).toBeNull()
  expect(screen.queryByRole('button', { name: 'Graph' })).toBeNull()
})

it('clears the query from the search field with one control', async () => {
  render(<SearchScreen />)
  const field = await screen.findByRole('searchbox', { name: 'Query' })
  await userEvent.type(field, 'balance')
  await userEvent.click(screen.getByRole('button', { name: 'Clear search' }))
  expect(field).toHaveValue('')
})

it('focuses the search field on "/" from anywhere on the page', async () => {
  render(<SearchScreen />)
  const field = await screen.findByRole('searchbox', { name: 'Query' })
  field.blur()
  await userEvent.keyboard('/')
  expect(field).toHaveFocus()
})

it('does not steal a "/" typed into the field itself', async () => {
  render(<SearchScreen />)
  const field = await screen.findByRole('searchbox', { name: 'Query' })
  await userEvent.type(field, 'a/b')
  expect(field).toHaveValue('a/b')
})

it('gives the empty state a way out', async () => {
  searchCatalog.mockResolvedValue(result([], FACETS, 0))
  render(<SearchScreen />)
  await screen.findByText(/no results match/i)
  searchCatalog.mockResolvedValue(result([HIT], FACETS, 1))
  await userEvent.click(screen.getByRole('button', { name: 'Clear search and filters' }))
  expect(await screen.findByTestId('hit-name')).toHaveTextContent('balance')
  expect(window.location.hash).toBe('#/search')
})
```

- [ ] **Step 2: Run and watch them fail**

Run: `cd frontend && npx vitest run src/screens/SearchScreen.test.tsx`
Expected: FAIL — the view toggle still exists; no clear button; no `/` handler.

- [ ] **Step 3: Rebuild the search bar**

In `frontend/src/screens/SearchScreen.tsx`, replace the whole `<form … className="search-bar">`
block with:

```tsx
      <form onSubmit={submit} role="search" className="search-bar">
        <div className="search-field">
          <input
            aria-label="Query"
            type="search"
            className="search-input"
            ref={queryField}
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder="Business term, physical name, or concept"
          />
          {draft && (
            <button
              type="button"
              className="search-clear"
              aria-label="Clear search"
              onClick={() => {
                setDraft('')
                queryField.current?.focus()
              }}
            >
              ×
            </button>
          )}
        </div>
        <button type="submit" className="btn btn--primary search-submit">
          Search
        </button>
      </form>
      <p className="hint search-help">
        Try a business term, a physical name such as <code>cust_num</code>, or a concept.
        Press <kbd>/</kbd> to search from anywhere on the page.
      </p>
```

Add the ref and the shortcut near the other hooks:

```tsx
  const queryField = useRef<HTMLInputElement>(null)

  // "/" focuses search from anywhere on the page — except while the user is typing, where it is
  // just a character (`a/b` must stay `a/b`).
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return
      const active = document.activeElement
      const typing = active instanceof HTMLInputElement
        || active instanceof HTMLTextAreaElement
        || (active instanceof HTMLElement && active.isContentEditable)
      if (typing) return
      event.preventDefault()
      queryField.current?.focus()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])
```

Delete the `<div className="viewtoggle" …>` block and the `viewtoggle-hint` span entirely. Keep the
`view` state, `jumpToGraph`, `effectiveView` and the `LineageView` branch exactly as they are —
graph mode is still real, it is simply only ever entered from a row.

- [ ] **Step 4: Give the empty state its exit**

Replace the empty-state block with:

```tsx
          {!error && result && result.hits.length === 0 && (
            <div className="empty" role="status">
              <p>No results match these filters.</p>
              <p className="next">
                Loosen or clear a facet. A stale source is withheld until it is re-uploaded and
                re-vouched, and columns your roles cannot see are never shown. Nothing is shown that
                cannot be trusted.
              </p>
              <button type="button" className="btn" onClick={() => apply('', {})}>
                Clear search and filters
              </button>
            </div>
          )}
```

- [ ] **Step 5: Run and watch them pass**

Run: `cd frontend && npx vitest run src/screens/SearchScreen.test.tsx`
Expected: PASS.

- [ ] **Step 6: Style the bar**

Append to the search block in `frontend/src/index.css`:

```css
.search-field { position: relative; flex: 1; min-width: 0; }
.search-field .search-input { width: 100%; padding-right: 36px; }

.search-clear {
  position: absolute;
  top: 50%;
  right: 6px;
  width: 24px;
  height: 24px;
  transform: translateY(-50%);
  border: 0;
  border-radius: 6px;
  background: transparent;
  color: var(--ink-faint);
  font-size: 16px;
  line-height: 1;
}

.search-clear:hover { background: var(--surface-2); color: var(--ink); }

.search-help { margin: 6px 2px 0; }

.search-help kbd {
  padding: 1px 5px;
  border: 1px solid var(--line);
  border-radius: 4px;
  background: var(--surface-2);
  font-family: var(--font-mono);
  font-size: 11px;
}

.empty .btn { margin-top: 12px; }
```

- [ ] **Step 7: Full suite, typecheck, lint**

Run: `cd frontend && npm test && npm run typecheck && npm run lint`
Expected: green.

- [ ] **Step 8: Commit**

```bash
git add frontend/src/screens/SearchScreen.tsx frontend/src/screens/SearchScreen.test.tsx \
        frontend/src/index.css
git commit -m "feat(search): search-bar affordances, an exit from empty, no anchorless graph toggle"
```

---

### Task 7: Responsive density and the accessibility sweep

The concept's three breakpoints, plus the checks a mockup never has to pass.

**Files:**
- Modify: `frontend/src/index.css` (append)
- Modify: `frontend/src/screens/SearchHitRow.test.tsx` (focus-order assertion)

**Interfaces:**
- Consumes: every class introduced in Tasks 2–6.
- Produces: no new exports.

- [ ] **Step 1: Write the failing accessibility test**

Append to `frontend/src/screens/SearchHitRow.test.tsx`:

```tsx
it('walks the row’s controls in priority order on Tab', async () => {
  renderRow()
  await userEvent.tab()
  expect(screen.getByRole('button', { name: 'Open asset public.accounts.balance' })).toHaveFocus()
  await userEvent.tab()
  expect(
    screen.getByRole('button', { name: 'Explore relationships for public.accounts.balance' }),
  ).toHaveFocus()
  await userEvent.tab()
  expect(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  ).toHaveFocus()
})
```

- [ ] **Step 2: Run it**

Run: `cd frontend && npx vitest run src/screens/SearchHitRow.test.tsx`
Expected: PASS if the DOM order already matches the visual order (it does, from Task 3). If it
FAILS, the actions are out of order in the markup — fix the JSX order, not the test.

- [ ] **Step 3: Add the responsive rules**

Append to the search block in `frontend/src/index.css`:

```css
/* Laptop: the capability column stops competing with the definition for width and moves under it. */
@media (max-width: 1180px) {
  .rows .row.hit { grid-template-columns: minmax(0, 1fr) auto; }
  .hit-capabilities { grid-column: 1; grid-row: 2; }
  .hit-actions { grid-column: 2; grid-row: 1 / 3; align-items: start; }
}

/* Narrow: one column, actions wrap under the content, facets become a horizontal tray.
   900px, NOT a new breakpoint: index.css:3994 already unsticks the sidebar and collapses
   .facet-cols to one column at exactly this width. This block appends to that decision (equal
   specificity, later in the file) rather than introducing a second, competing narrow width. */
@media (max-width: 900px) {
  .rows .row.hit { grid-template-columns: minmax(0, 1fr); }
  .hit-capabilities,
  .hit-actions { grid-column: 1; grid-row: auto; }
  .hit-actions { flex-wrap: wrap; }

  .facet-panel {
    flex-direction: row;
    gap: 8px;
    overflow-x: auto;
  }
  .facet-group { display: flex; flex: none; align-items: center; gap: 6px; border-bottom: 0; }
  .facet-group-title { margin-bottom: 0; white-space: nowrap; }
  .facet-option { white-space: nowrap; }
  .facet-more { margin-top: 0; white-space: nowrap; }
}

/* Phone: the overflow popover cannot hang off the right edge of a 360px screen. */
@media (max-width: 620px) {
  .hit-overflow-items { right: auto; left: 0; min-width: min(240px, calc(100vw - 48px)); }
  .results-toolbar { flex-direction: column; align-items: flex-start; gap: 4px; }
}

/* Motion: the popover is the only thing that animates, and only when motion is welcome. */
@media (prefers-reduced-motion: no-preference) {
  .hit-overflow-items { animation: hit-overflow-in 150ms cubic-bezier(0.22, 1, 0.36, 1); }
  @keyframes hit-overflow-in {
    from { opacity: 0; transform: translateY(-2px); }
    to { opacity: 1; transform: none; }
  }
}
```

- [ ] **Step 4: Verify the contrast and focus claims by hand**

Check each new color pairing against the tokens already documented in `frontend/DESIGN.md`:

- `.hit-breadcrumb` uses `--ink-faint` on `--surface` — DESIGN.md records 5.89:1. Passes 4.5:1.
- `.hit-definition` uses `--ink-soft` on `--surface` — darker than `--ink-faint`. Passes.
- `.hit-capabilities li::before` uses `--accent-line`; it is decorative (the label carries the
  meaning), so the 3:1 non-text rule does not gate it. Confirm the label text is `--ink-soft`.
- `.projection-ok::before` uses `--ok` as a 6px dot beside `--ink-faint` text — again decorative,
  with the words "Catalog projection current" carrying the state. No color-only signalling.
- Every button in `.hit-overflow-items` inherits the app's global `:focus-visible` ring
  (`frontend/src/index.css:107`, which selects bare `:focus-visible`). The popover rules set only
  `border`, `background` and `color`, so nothing cancels the outline — verify by tabbing through an
  open popover in the browser during Step 7's visual pass.

Record the outcome of each check in the commit message.

- [ ] **Step 5: Build and run everything**

Run: `cd frontend && npm test && npm run typecheck && npm run lint && npm run build`
Expected: all green, build succeeds.

- [ ] **Step 6: Commit**

```bash
git add frontend/src/index.css frontend/src/screens/SearchHitRow.test.tsx
git commit -m "feat(search): responsive density and the accessibility sweep"
```

- [ ] **Step 7: Hand back for a real-browser look**

The suite proves behavior, not appearance. Report to the user that the screen is ready for a visual
pass and ask whether to run the app (`cd frontend && npm run dev`, backend on `:8000`) — this needs
their environment, so do not start services without being asked.

---

## Self-Review

**Spec coverage** — every §0.3 decision maps to a task: D1 → Tasks 1–2; D2 → Task 1; D3 → Task 1
(`hitMeta`); D4 → Tasks 2–3; D5 → Task 6; D6 → Task 4; D7 → Task 5; D8 → Task 5; D9 → Task 6;
D10 → the file-structure table, realized across Tasks 1–4. §0.4 is explicitly deferred and has no
task, by design.

**Placeholders** — none. Every code step carries the actual code; every test step carries the
actual assertions; every copy string is written out.

**Type consistency** — `hitDisplayName` / `hitBreadcrumb` / `hitCapabilities` / `hitMeta` and the
`HitCapability` interface are defined in Task 1 and used under exactly those names in Task 2.
`SearchHitRow`'s props (`hit`, `onOpen`, `onExplore`, `onSuggested`) are declared in Task 2 and
wired with those names in Task 2 Step 5. `SearchFacetPanel`'s props (`facets`, `filters`,
`onToggleFacet`, `onToggleFlag`) are declared in Task 4 and wired with those names in Task 4 Step 6.
`SEARCH_FACET_KEYS` is widened in Task 4 Step 1 before `isFacetKey` relies on it in Step 4.

**Known behavior changes a reviewer should expect** — accessible names change (`Details` →
`Open asset`, `Graph` → `Explore relationships`, `Impact` → `Feature impact`); count copy changes
(`N results` → `N assets`); the sidebar's flags group is renamed (`Flags` → `Column role`) and its
options relabelled (`Grain` → `Grain key`, `As-of` → `As-of field`); the global List/Graph toggle is
removed. Each is updated in the same task that causes it.
