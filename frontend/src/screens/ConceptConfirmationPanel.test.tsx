import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ConceptConfirmationPanel } from './ConceptConfirmationPanel'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    listCatalogs: vi.fn(),
    getConceptConfirmations: vi.fn(),
    postConceptConfirmations: vi.fn(),
  }
})
const listCatalogs = vi.mocked(api.listCatalogs)
const getConceptConfirmations = vi.mocked(api.getConceptConfirmations)
const postConceptConfirmations = vi.mocked(api.postConceptConfirmations)

function column(over: Partial<api.ConceptConfirmationColumn> = {}): api.ConceptConfirmationColumn {
  return {
    object_ref: 'public.customers.cust_no', table: 'customers', column: 'cust_no',
    evidence_id: 'ev-1', producer: 'llm', strength: 'proposed',
    latest_decision_id: null, evidence_set_hash: 'hash-1', policy_version: 'v1',
    ...over,
  }
}

const QUEUE: api.ConceptConfirmationQueue = {
  catalog_source: 'bank',
  unreferenced_groups_omitted: 1,
  funnel: { active: 3, human_confirmed: 0, confirmed_share: 0 },
  groups: [
    {
      concept: 'customer_id', operand_reference_count: 214,
      columns: [
        column(),
        column({ object_ref: 'public.accounts.cust_ref', table: 'accounts',
                 column: 'cust_ref', evidence_id: 'ev-2', evidence_set_hash: 'hash-2' }),
      ],
    },
    { concept: 'monetary_flow', operand_reference_count: 60, columns: [column({
        object_ref: 'public.transactions.amount', table: 'transactions', column: 'amount',
        evidence_id: 'ev-3', evidence_set_hash: 'hash-3' })] },
  ],
}

beforeEach(() => {
  listCatalogs.mockReset()
  getConceptConfirmations.mockReset()
  postConceptConfirmations.mockReset()
  listCatalogs.mockResolvedValue({ catalogs: [
    { source: 'bank', tables: 3, columns: 9 } as api.VisibleCatalog] })
  getConceptConfirmations.mockResolvedValue(QUEUE)
})

describe('ConceptConfirmationPanel', () => {
  it('renders groups load-bearing first with the funnel and the omission named', async () => {
    render(<ConceptConfirmationPanel />)
    expect(await screen.findByText(/3 of.*proposals settled|0 of 3 proposals settled/))
      .toBeInTheDocument()
    expect(screen.getByText('customer_id')).toBeInTheDocument()
    expect(screen.getByText(/used by 214 recipe operands/)).toBeInTheDocument()
    expect(screen.getByText(/1 concept group\(s\) not referenced/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Confirm 2 as customer_id' })).toBeEnabled()
  })

  it('confirms the batch minus unticked exceptions, echoing each CAS anchor', async () => {
    postConceptConfirmations.mockResolvedValue({
      results: [{ object_ref: 'public.customers.cust_no', accepted: true, status_code: 200 }],
      accepted_count: 1, declined_count: 0,
      funnel: { active: 3, human_confirmed: 1, confirmed_share: 0.3333 },
    })
    render(<ConceptConfirmationPanel />)
    await screen.findByText('customer_id')
    // Untick the exception…
    await userEvent.click(screen.getByRole('checkbox', { name: /accounts\.cust_ref/ }))
    await userEvent.click(screen.getByRole('button', { name: 'Confirm 1 as customer_id' }))
    await waitFor(() => expect(postConceptConfirmations).toHaveBeenCalledWith(
      'bank',
      [{
        object_ref: 'public.customers.cust_no', action: 'confirm_existing',
        evidence_id: 'ev-1', expected_latest_decision_id: null,
        expected_evidence_set_hash: 'hash-1', expected_policy_version: 'v1',
      }],
      'bulk confirm: customer_id',
    ))
    expect(await screen.findByText(/1 recorded, 0 declined/)).toBeInTheDocument()
    expect(getConceptConfirmations).toHaveBeenCalledTimes(2)   // reloaded after the batch
  })

  it('shows per-item declines without hiding the batch siblings that landed', async () => {
    postConceptConfirmations.mockResolvedValue({
      results: [
        { object_ref: 'public.customers.cust_no', accepted: true, status_code: 200 },
        { object_ref: 'public.accounts.cust_ref', accepted: false, status_code: 409,
          detail: 'evidence set drifted — reload and re-review' },
      ],
      accepted_count: 1, declined_count: 1,
      funnel: { active: 3, human_confirmed: 1, confirmed_share: 0.3333 },
    })
    render(<ConceptConfirmationPanel />)
    await screen.findByText('customer_id')
    await userEvent.click(screen.getByRole('button', { name: 'Confirm 2 as customer_id' }))
    const declined = await screen.findByRole('list', { name: 'Declined items' })
    expect(within(declined).getByText('public.accounts.cust_ref')).toBeInTheDocument()
    expect(within(declined).getByText(/evidence set drifted/)).toBeInTheDocument()
    expect(screen.getByText(/1 recorded, 1 declined/)).toBeInTheDocument()
  })

  it('an empty queue says settled, never broken', async () => {
    getConceptConfirmations.mockResolvedValue({
      catalog_source: 'bank', unreferenced_groups_omitted: 0,
      funnel: { active: 5, human_confirmed: 5, confirmed_share: 1 }, groups: [],
    })
    render(<ConceptConfirmationPanel />)
    expect(await screen.findByText(/every load-bearing proposal on this catalog is\s+settled/))
      .toBeInTheDocument()
  })
})
