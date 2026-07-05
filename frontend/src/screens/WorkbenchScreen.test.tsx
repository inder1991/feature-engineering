import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { WorkbenchScreen } from './WorkbenchScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    recommendFeatures: vi.fn(),
    featureRecipe: vi.fn(),
    leakageCheck: vi.fn(),
    registerFeature: vi.fn(),
  }
})
const recommendFeatures = vi.mocked(api.recommendFeatures)
const featureRecipe = vi.mocked(api.featureRecipe)
const leakageCheck = vi.mocked(api.leakageCheck)
const registerFeature = vi.mocked(api.registerFeature)

beforeEach(() => {
  recommendFeatures.mockReset()
  featureRecipe.mockReset()
  leakageCheck.mockReset()
  registerFeature.mockReset()
})

const IDEA: api.FeatureIdea = {
  name: 'avg_balance', description: 'average balance per customer',
  derives_from: ['public.accounts.balance'], aggregation: 'avg', grain_table: 'customers',
}

async function suggest() {
  await userEvent.type(screen.getByLabelText('catalog source'), 'deposits')
  await userEvent.type(screen.getByLabelText('objective'), 'predict churn')
  await userEvent.click(screen.getByRole('button', { name: /suggest features/i }))
}

describe('workbench screen', () => {
  it('registers a proposal only after an explicit confirm', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    registerFeature.mockResolvedValue('feat_01')
    render(<WorkbenchScreen />)
    await suggest()
    await userEvent.click(await screen.findByRole('button', { name: 'Register…' }))
    expect(registerFeature).not.toHaveBeenCalled()
    await userEvent.click(screen.getByRole('button', { name: 'Confirm register' }))
    expect(registerFeature).toHaveBeenCalledWith({
      name: 'avg_balance', description: 'average balance per customer',
      grain_table: 'customers', aggregation: 'avg', as_of_column: null,
      derives_from: [{ catalog_source: 'deposits', object_ref: 'public.accounts.balance' }],
    })
    expect(await screen.findByText(/registered as/i)).toBeInTheDocument()
  })

  it('cancel backs out of the confirm step without registering', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    render(<WorkbenchScreen />)
    await suggest()
    await userEvent.click(await screen.findByRole('button', { name: 'Register…' }))
    await userEvent.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(registerFeature).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Register…' })).toBeInTheDocument()
  })

  it('shows the honest 503 state when assist is unconfigured', async () => {
    recommendFeatures.mockRejectedValue(new api.ApiError(503, 'not configured'))
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('objective'), 'churn')
    await userEvent.click(screen.getByRole('button', { name: /suggest features/i }))
    expect(await screen.findByText(/ai assist is not configured/i)).toBeInTheDocument()
  })

  it('renders the recipe join path with a fan-out warning', async () => {
    featureRecipe.mockResolvedValue({
      intent: 'total spend per customer', grain_table: 'customers',
      derives_from: ['public.transactions.amount'], aggregation: 'sum', as_of_column: null,
      join_path: [
        { from_ref: 'public.customers.cust_id', to_ref: 'public.accounts.cust_id', cardinality: '1:N' },
        { from_ref: 'public.accounts.id', to_ref: 'public.transactions.account_id', cardinality: '1:N' },
      ],
    })
    render(<WorkbenchScreen />)
    await userEvent.type(screen.getByLabelText('catalog source'), 'deposits')
    await userEvent.type(screen.getByLabelText('feature description'), 'total spend per customer')
    await userEvent.click(screen.getByRole('button', { name: /build recipe/i }))
    expect(await screen.findByText(/join path/i)).toBeInTheDocument()
    expect(screen.getAllByRole('listitem')).toHaveLength(2)
    expect(screen.getByText(/aggregate before joining/i)).toBeInTheDocument()
  })

  it('surfaces leakage warnings as a banner', async () => {
    recommendFeatures.mockResolvedValue([IDEA])
    leakageCheck.mockResolvedValue([
      { object_ref: 'public.accounts.balance', reason: 'target-adjacent' }])
    render(<WorkbenchScreen />)
    await suggest()
    await userEvent.type(screen.getByLabelText('target column'), 'public.labels.churned')
    await userEvent.click(await screen.findByRole('button', { name: /check leakage/i }))
    expect(await screen.findByText(/possible target leakage/i)).toBeInTheDocument()
    expect(leakageCheck).toHaveBeenCalledWith(
      ['public.accounts.balance'], 'public.labels.churned')
    expect(screen.getByText(/target-adjacent/)).toBeInTheDocument()
  })
})
