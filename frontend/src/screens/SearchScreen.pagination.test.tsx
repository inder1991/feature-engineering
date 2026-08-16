import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import './lineage-test-setup'
import { SearchScreen } from './SearchScreen'

// Paging past the first 20 results.
//
// The screen showed the first page and said "237 results", with no way to reach result 21. The
// backend now takes an offset; these pin the control that drives it.
//
// The subtle requirement is the RESET: a facet toggle shrinks the result set, so carrying the
// offset across it would land the user past the end of the new set and show an empty page for a
// filter that genuinely matches rows.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, searchCatalog: vi.fn(), featureImpact: vi.fn(), lineageGraph: vi.fn() }
})
const searchCatalog = vi.mocked(api.searchCatalog)

function hit(n: number): api.SearchHit {
  return {
    object_ref: `public.t.c${n}`, table: 't', column: `c${n}`, kind: 'column',
    data_type: 'text', definition: '', is_grain: false, is_as_of: false,
    catalog_source: 'wide', concept: null, domain: null, sensitivity: null, sensitivity_display: null,
    additivity: null, unit: null, currency: null, entity: null, score: 1,
  }
}

const FACETS: Record<string, api.FacetBucket[]> = {
  source: [{ value: 'wide', count: 45 }], domain: [], sensitivity: [],
  additivity: [], entity: [], kind: [{ value: 'column', count: 45 }],
  grain: [], as_of: [],
}

/** A page of `size` hits starting at `offset`, out of `total` overall. */
function page(offset: number, size: number, total: number): api.SearchResult {
  return {
    hits: Array.from({ length: size }, (_, i) => hit(offset + i)),
    facets: FACETS,
    total,
    // Task 6: every search read carries the projection marker; `ready` is the normal value.
    projection: { status: 'ready', code: '', detail: '' },
  }
}

const offsetOf = (call: unknown[]) => call[3] as number | undefined

beforeEach(() => {
  window.location.hash = '#/search'
  searchCatalog.mockReset()
  searchCatalog.mockResolvedValue(page(0, 20, 45))
})

// Rows are addressed by the COLUMN NAME they now lead with: Task 2's row demoted the object_ref
// to a breadcrumb, so `public.t.c0` is no longer one text node anywhere in the DOM.
async function mount() {
  render(<SearchScreen />)
  await screen.findByText('c0')
}

// ── the control exists and is honest about where it can go ──────────────────────────────────────

it('offers Next when more results exist than this page shows', async () => {
  await mount()
  expect(screen.getByRole('button', { name: /next/i })).toBeEnabled()
})

it('does not offer a way forward when this page is the whole result set', async () => {
  searchCatalog.mockResolvedValue(page(0, 5, 5))
  render(<SearchScreen />)
  await screen.findByText('c0')
  expect(screen.queryByRole('button', { name: /next/i })).toBeNull()
})

it('cannot go back from the first page', async () => {
  await mount()
  const prev = screen.queryByRole('button', { name: /previous/i })
  if (prev) expect(prev).toBeDisabled()
})

// ── it actually pages ───────────────────────────────────────────────────────────────────────────

it('Next asks the server for the rows after this page', async () => {
  await mount()
  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await waitFor(() => expect(offsetOf(searchCatalog.mock.calls.at(-1)!)).toBe(20))
  expect(await screen.findByText('c20')).toBeInTheDocument()
})

it('Previous returns to the page before', async () => {
  await mount()
  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('c20')
  searchCatalog.mockResolvedValue(page(0, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /previous/i }))
  await waitFor(() => expect(offsetOf(searchCatalog.mock.calls.at(-1)!)).toBe(0))
})

it('stops offering Next on the last page', async () => {
  // Walked to, not mocked into: the offset is client state, so the only honest way to be on the
  // last page is to page there.
  await mount()
  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('c20')
  searchCatalog.mockResolvedValue(page(40, 5, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('c40')
  expect(screen.queryByRole('button', { name: /next/i })).toBeNull()
  expect(screen.getByRole('button', { name: /previous/i })).toBeEnabled()
})

// ── the reset that keeps a later page from hiding real matches ──────────────────────────────────

it('returns to the first page when a facet changes', async () => {
  await mount()
  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('c20')

  // A facet toggle narrows the set — carrying offset=20 into a 3-row result would show nothing.
  searchCatalog.mockResolvedValue(page(0, 3, 3))
  await userEvent.click(screen.getByRole('checkbox', { name: /wide/i }))
  await waitFor(() => expect(offsetOf(searchCatalog.mock.calls.at(-1)!)).toBe(0))
  expect(await screen.findByText('c0')).toBeInTheDocument()
})

it('returns to the first page when a new query is submitted', async () => {
  await mount()
  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('c20')

  searchCatalog.mockResolvedValue(page(0, 2, 2))
  await userEvent.type(screen.getByLabelText('Query'), 'balance{Enter}')
  await waitFor(() => expect(offsetOf(searchCatalog.mock.calls.at(-1)!)).toBe(0))
})

// ── what the user is told ───────────────────────────────────────────────────────────────────────

it('says which slice of the whole set is on screen', async () => {
  await mount()
  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('c20')
  // The pager is now the ONE statement of the slice, and this is the only test that exercises its
  // arithmetic anywhere but the first page. Exact text, not /21/ + /40/ + /45/ over the joined
  // status nodes: those loose patterns were satisfiable by a row name or by the bare total, and
  // "of 45" quietly stopped being what matched /45/ once the count line read "45 assets".
  expect(screen.getByText('Showing 21–40 of 45 matching assets')).toBeInTheDocument()
  // The total is a property of the query, not of the page: it must not drift as the user walks.
  // Addressed by its testid, not by role=status: the live region moved to the pager copy (the
  // only text on this screen that changes as the reader pages), and the count line is now a plain
  // statement. Anchored, so a count line that also restated the slice would not match.
  expect(screen.getByTestId('result-count')).toHaveTextContent(/^45 assets$/)

  // One more page, because a FULL page cannot tell `offset + hits.length` from `offset + 20`.
  // The last page is short (5 of 45), so only the honest expression reads 41–45 here.
  searchCatalog.mockResolvedValue(page(40, 5, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('c40')
  expect(screen.getByText('Showing 41–45 of 45 matching assets')).toBeInTheDocument()
})

// Paging is a SILENT change for a screen reader unless the text that changes sits in a live
// region. Collapsing the two slice statements into one left the survivor — the pager copy —
// outside any live region, while the count line reads the same "45 assets" on every page. The
// announcement has to live where the changing text lives.
//
// Scoped to the pager: a multi-page result set carries TWO live regions (the count, which speaks
// on a new query, and this one, which speaks on a page change), so an unscoped getByRole('status')
// would throw on the ambiguity rather than pin either of them.
function pagerRegion(): HTMLElement {
  return within(screen.getByRole('navigation', { name: 'Result pages' })).getByRole('status')
}

it('announces the new slice to a screen reader when the reader pages', async () => {
  await mount()
  const region = pagerRegion()
  expect(region).toHaveTextContent('Showing 1–20 of 45 matching assets')

  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('c20')

  // The SAME node, re-read: a live region that unmounts and remounts is not reliably announced,
  // so node identity is part of the contract, not an incidental detail of this render.
  expect(pagerRegion()).toBe(region)
  expect(region).toHaveTextContent('Showing 21–40 of 45 matching assets')
})

// The count line keeps its own region, and keeps it UNCONDITIONALLY: a role that appeared only on
// a multi-page set would mount a fresh region at the single-page → multi-page transition, and a
// region that arrives already populated is not reliably announced at all.
it('keeps the count line a live region beside the pager’s', async () => {
  await mount()
  const count = screen.getByTestId('result-count')
  expect(count).toHaveAttribute('role', 'status')
  expect(count).toHaveTextContent(/^45 assets$/)
  // Two, and exactly two, in the successful multi-page state — the count and the pager copy.
  expect(screen.getAllByRole('status')).toHaveLength(2)
})
