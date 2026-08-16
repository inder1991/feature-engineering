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

// Braced body, deliberately: a CONCISE arrow returns the mock, and Vitest calls a function
// returned from beforeEach as that test's teardown — which would invoke featureImpact() with
// nobody awaiting it, and a rejecting mock would then fail the test it just passed.
beforeEach(() => {
  featureImpact.mockReset()
})

// What a screen reader is handed: the accessibility tree drops an aria-hidden element AND its
// whole subtree, so anything living inside the hidden separator — spaces included — is simply not
// there. toHaveTextContent reads textContent, which still sees it, and cannot catch this.
function accessibleText(el: HTMLElement): string {
  const clone = el.cloneNode(true) as HTMLElement
  for (const hidden of clone.querySelectorAll('[aria-hidden="true"]')) hidden.remove()
  return (clone.textContent ?? '').replace(/\s+/g, ' ').trim()
}

it('leads with the column name and demotes the physical ref to a breadcrumb', () => {
  renderRow()
  expect(screen.getByTestId('hit-name')).toHaveTextContent('balance')
  const trail = screen.getByTestId('hit-breadcrumb')
  expect(trail).toHaveTextContent('deposits')
  expect(trail).toHaveTextContent('public.accounts')
})

it('keeps the address readable once the separator glyph is hidden', () => {
  renderRow()
  // With the spaces inside the aria-hidden span this reads "depositspublic.accountsbalance" —
  // one run-on token where the old row had a single readable <code>.
  expect(accessibleText(screen.getByTestId('hit-breadcrumb')))
    .toBe('deposits public.accounts balance')
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
  expect(screen.getByText('Derived features')).toBeInTheDocument()
})

it('says plainly when nothing derives from the column', async () => {
  featureImpact.mockResolvedValue([])
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  )
  expect(await screen.findByText('No features derive from this column.')).toBeInTheDocument()
  // The label heads a list; an empty result must not print a heading over nothing.
  expect(screen.queryByText('Derived features')).not.toBeInTheDocument()
})

it('surfaces an impact failure as an alert without losing the row', async () => {
  featureImpact.mockRejectedValue(new api.ApiError(503, 'graph unavailable'))
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent('Impact check failed: graph unavailable')
  expect(screen.getByTestId('hit-name')).toHaveTextContent('balance')
  // A failed check is not an empty result: no heading over a list that was never fetched.
  expect(screen.queryByText('Derived features')).not.toBeInTheDocument()
})
