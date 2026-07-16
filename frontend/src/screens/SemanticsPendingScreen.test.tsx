import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { SemanticsPendingScreen } from './SemanticsPendingScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getSemanticsPending: vi.fn(),
    completeSemantics: vi.fn(),
  }
})
const getSemanticsPending = vi.mocked(api.getSemanticsPending)
const completeSemantics = vi.mocked(api.completeSemantics)

beforeEach(() => {
  getSemanticsPending.mockReset()
  completeSemantics.mockReset()
})

const ALL_MISSING = ['as_of', 'additivity', 'unit', 'currency', 'entity']

// Two tables' worth of pending columns, in the backend's object_ref order.
const ITEMS: api.SemanticsPendingItem[] = [
  {
    object_ref: 'accounts.balance', table: 'accounts', column: 'balance',
    data_type: 'numeric', missing: ALL_MISSING,
  },
  {
    object_ref: 'accounts.opened_on', table: 'accounts', column: 'opened_on',
    data_type: 'date', missing: ALL_MISSING,
  },
  {
    object_ref: 'transactions.amount', table: 'transactions', column: 'amount',
    data_type: null, missing: ALL_MISSING,
  },
]

// The row <li> for a column, located by its unique object_ref line.
function rowFor(objectRef: string): HTMLElement {
  const row = screen.getByText(objectRef).closest('li')
  if (!row) throw new Error(`row for ${objectRef} not found`)
  return row
}

describe('semantics-pending queue', () => {
  it('auto-loads the ?source= queue and lists pending columns grouped by table', async () => {
    getSemanticsPending.mockResolvedValue(ITEMS)
    render(<SemanticsPendingScreen initialSource="cards" />)

    expect(await screen.findByText(/3 columns pending in/)).toBeInTheDocument()
    expect(getSemanticsPending).toHaveBeenCalledExactlyOnceWith('cards')

    // Grouped under table headings, in arrival order.
    expect(
      screen.getAllByRole('heading', { level: 2 }).map(h => h.textContent),
    ).toEqual(['accounts', 'transactions'])

    // Each row names the column, its ref, its type, and which fields are missing.
    const balance = rowFor('accounts.balance')
    expect(within(balance).getByText('balance')).toBeInTheDocument()
    expect(within(balance).getByText('numeric')).toBeInTheDocument()
    expect(within(balance).getByText('missing: as_of, additivity, unit, currency, entity'))
      .toBeInTheDocument()
    // Every semantic control is labeled.
    expect(within(balance).getByLabelText('additivity')).toBeInTheDocument()
    expect(within(balance).getByLabelText('unit')).toBeInTheDocument()
    expect(within(balance).getByLabelText('currency')).toBeInTheDocument()
    expect(within(balance).getByLabelText('entity')).toBeInTheDocument()
    expect(within(balance).getByLabelText('as-of column')).toBeInTheDocument()
    expect(rowFor('transactions.amount')).toBeInTheDocument()
  })

  it('completing a column posts only the set values and removes the row', async () => {
    getSemanticsPending.mockResolvedValue(ITEMS)
    completeSemantics.mockResolvedValue({
      completed: true, applied: { additivity: 'additive', unit: 'GBP', currency: 'GBP' },
    })
    render(<SemanticsPendingScreen initialSource="cards" />)
    await screen.findByText('accounts.balance')
    const balance = within(rowFor('accounts.balance'))

    await userEvent.selectOptions(balance.getByLabelText('additivity'), 'additive')
    await userEvent.type(balance.getByLabelText('unit'), 'GBP')
    await userEvent.type(balance.getByLabelText('currency'), 'GBP')
    await userEvent.click(balance.getByRole('button', { name: 'Save' }))

    // Only the fields the owner set ride the wire; blanks are never sent.
    expect(completeSemantics).toHaveBeenCalledExactlyOnceWith('cards', 'accounts.balance', {
      additivity: 'additive', unit: 'GBP', currency: 'GBP',
    })
    // The completed row leaves the queue; its table (with another pending column) stays.
    expect(await screen.findByText(/2 columns pending in/)).toBeInTheDocument()
    expect(screen.queryByText('accounts.balance')).not.toBeInTheDocument()
    expect(screen.getByText('accounts.opened_on')).toBeInTheDocument()
    expect(screen.getByText(/1 completed this session/)).toBeInTheDocument()
  })

  it('save stays disabled until the owner sets at least one value', async () => {
    getSemanticsPending.mockResolvedValue([ITEMS[0]])
    render(<SemanticsPendingScreen initialSource="cards" />)
    await screen.findByText('accounts.balance')
    const balance = within(rowFor('accounts.balance'))
    expect(balance.getByRole('button', { name: 'Save' })).toBeDisabled()
    await userEvent.type(balance.getByLabelText('entity'), 'customer')
    expect(balance.getByRole('button', { name: 'Save' })).toBeEnabled()
  })

  it('surfaces the second-as-of-axis 409 inline and keeps the row', async () => {
    getSemanticsPending.mockResolvedValue([ITEMS[1]])
    completeSemantics.mockRejectedValue(new api.ApiError(409,
      "table 'accounts' already has an as-of axis ('posted_at'); a table asserts ONE "
      + 'availability basis — unset it first or complete that column instead'))
    render(<SemanticsPendingScreen initialSource="cards" />)
    await screen.findByText('accounts.opened_on')
    const row = within(rowFor('accounts.opened_on'))

    await userEvent.click(row.getByLabelText('as-of column'))
    await userEvent.click(row.getByRole('button', { name: 'Save' }))

    const error = await row.findByRole('alert')
    expect(error).toHaveTextContent("table 'accounts' already has an as-of axis ('posted_at')")
    // Nothing was written: the row stays pending.
    expect(screen.getByText('accounts.opened_on')).toBeInTheDocument()
    expect(screen.getByText(/1 column pending in/)).toBeInTheDocument()
  })

  it('surfaces a 422 vocabulary rejection inline', async () => {
    getSemanticsPending.mockResolvedValue([ITEMS[0]])
    completeSemantics.mockRejectedValue(new api.ApiError(422,
      "unrecognized additivity 'sometimes' "
      + '(expected one of: additive, non_additive, semi_additive)'))
    render(<SemanticsPendingScreen initialSource="cards" />)
    await screen.findByText('accounts.balance')
    const row = within(rowFor('accounts.balance'))
    await userEvent.type(row.getByLabelText('unit'), 'GBP')
    await userEvent.click(row.getByRole('button', { name: 'Save' }))
    expect(await row.findByRole('alert')).toHaveTextContent(/unrecognized additivity/)
  })

  it('shows the honest empty state when nothing is pending', async () => {
    getSemanticsPending.mockResolvedValue([])
    render(<SemanticsPendingScreen initialSource="cards" />)
    expect(
      await screen.findByText('No columns need semantics — all set.'),
    ).toBeInTheDocument()
  })

  it('loads a typed source on submit and surfaces a load failure', async () => {
    getSemanticsPending.mockRejectedValue(new api.ApiError(403, 'missing role: catalog_read'))
    render(<SemanticsPendingScreen initialSource="" />)
    expect(getSemanticsPending).not.toHaveBeenCalled()
    await userEvent.type(screen.getByLabelText('Source'), 'cards')
    await userEvent.click(screen.getByRole('button', { name: 'Load queue' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('missing role: catalog_read')
    expect(getSemanticsPending).toHaveBeenCalledExactlyOnceWith('cards')
  })
})
