import { render, screen, waitFor } from '@testing-library/react'
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
    catalog_source: 'wide', concept: null, domain: null, sensitivity: null,
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
  }
}

const offsetOf = (call: unknown[]) => call[3] as number | undefined

beforeEach(() => {
  window.location.hash = '#/search'
  searchCatalog.mockReset()
  searchCatalog.mockResolvedValue(page(0, 20, 45))
})

async function mount() {
  render(<SearchScreen />)
  await screen.findByText('public.t.c0')
}

// ── the control exists and is honest about where it can go ──────────────────────────────────────

it('offers Next when more results exist than this page shows', async () => {
  await mount()
  expect(screen.getByRole('button', { name: /next/i })).toBeEnabled()
})

it('does not offer a way forward when this page is the whole result set', async () => {
  searchCatalog.mockResolvedValue(page(0, 5, 5))
  render(<SearchScreen />)
  await screen.findByText('public.t.c0')
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
  expect(await screen.findByText('public.t.c20')).toBeInTheDocument()
})

it('Previous returns to the page before', async () => {
  await mount()
  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('public.t.c20')
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
  await screen.findByText('public.t.c20')
  searchCatalog.mockResolvedValue(page(40, 5, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('public.t.c40')
  expect(screen.queryByRole('button', { name: /next/i })).toBeNull()
  expect(screen.getByRole('button', { name: /previous/i })).toBeEnabled()
})

// ── the reset that keeps a later page from hiding real matches ──────────────────────────────────

it('returns to the first page when a facet changes', async () => {
  await mount()
  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('public.t.c20')

  // A facet toggle narrows the set — carrying offset=20 into a 3-row result would show nothing.
  searchCatalog.mockResolvedValue(page(0, 3, 3))
  await userEvent.click(screen.getByRole('checkbox', { name: /wide/i }))
  await waitFor(() => expect(offsetOf(searchCatalog.mock.calls.at(-1)!)).toBe(0))
  expect(await screen.findByText('public.t.c0')).toBeInTheDocument()
})

it('returns to the first page when a new query is submitted', async () => {
  await mount()
  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('public.t.c20')

  searchCatalog.mockResolvedValue(page(0, 2, 2))
  await userEvent.type(screen.getByLabelText('Query'), 'balance{Enter}')
  await waitFor(() => expect(offsetOf(searchCatalog.mock.calls.at(-1)!)).toBe(0))
})

// ── what the user is told ───────────────────────────────────────────────────────────────────────

it('says which slice of the whole set is on screen', async () => {
  await mount()
  searchCatalog.mockResolvedValue(page(20, 20, 45))
  await userEvent.click(screen.getByRole('button', { name: /next/i }))
  await screen.findByText('public.t.c20')
  // The range is interpolated from several JSX expressions, so assert on the element's text
  // rather than on a single matching text node.
  const line = screen.getAllByRole('status').map(n => n.textContent ?? '').join(' | ')
  expect(line).toMatch(/21/)
  expect(line).toMatch(/40/)
  expect(line).toMatch(/45/)
})
