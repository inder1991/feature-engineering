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

// Not in the brief. The pii danger dot is shipped behaviour that moved OUT of SearchScreen with
// this refactor; without a test at its new home the only thing holding it is a screen-level test
// that could be deleted as "sidebar detail". The label still carries the meaning — the dot is
// aria-hidden decoration on top of it.
it('marks the declared pii tag with the danger dot, and nothing else', () => {
  renderPanel({ sensitivity: [{ value: 'pii', count: 1 }, { value: 'internal', count: 4 }] })
  const declared = group('Declared tag')
  const pii = declared.getByRole('checkbox', { name: 'pii 1' })
  expect(pii.closest('label')?.querySelector('.facet-pii-dot')).toBeInTheDocument()
  const internal = declared.getByRole('checkbox', { name: 'internal 4' })
  expect(internal.closest('label')?.querySelector('.facet-pii-dot')).toBeNull()
})

// A facet the client cannot SEND (it is not in SEARCH_FACET_KEYS) is shown, because the server
// returned it, but it cannot be selected — the alternative is a checkbox that silently does
// nothing when clicked.
it('shows an unsendable facet without pretending it can be selected', () => {
  const { onToggleFacet } = renderPanel({ risk_tier: [{ value: 'high', count: 1 }] })
  const box = group('Risk tier').getByRole('checkbox', { name: 'high 1' })
  expect(box).toBeDisabled()
  expect(onToggleFacet).not.toHaveBeenCalled()
})
