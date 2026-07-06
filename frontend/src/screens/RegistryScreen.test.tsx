import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { RegistryScreen } from './RegistryScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, listFeatures: vi.fn(), featureDetail: vi.fn() }
})
const listFeatures = vi.mocked(api.listFeatures)
const featureDetail = vi.mocked(api.featureDetail)

beforeEach(() => {
  listFeatures.mockReset()
  featureDetail.mockReset()
})

const ITEM: api.FeatureListItem = {
  feature_id: 'feat_1',
  name: 'avg_balance_90d',
  grain_table: 'accounts',
  aggregation: 'avg_90d',
  as_of_column: 'posted_at',
  verification: 'DESIGN-CHECKED',
  created_at: '2026-07-05T00:00:00+00:00',
}

const DETAIL: api.FeatureDetail = {
  feature_id: 'feat_1',
  name: 'avg_balance_90d',
  description: 'avg balance',
  grain_table: 'accounts',
  aggregation: 'avg_90d',
  as_of_column: 'posted_at',
  verification: 'DESIGN-CHECKED',
  created_at: '2026-07-05T00:00:00+00:00',
  derives_from: [{ catalog_source: 'deposits', object_ref: 'public.accounts.balance' }],
  contract: {
    contract_id: 'c1',
    definition: 'Average balance over 90 days',
    version: 1,
    verification: 'DESIGN-CHECKED',
    join_path: [],
  },
  hypothesis: {
    hypothesis: 'customers churn when balance drops',
    definition: '90-day avg balance',
    intake_mode: 'definition',
    target_ref: 'public.accounts.churned',
  },
  consumers: [
    {
      model_ref: 'churn_model_v3',
      purpose: 'churn',
      environment: 'prod',
      registered_at: '2026-07-05T00:00:00+00:00',
    },
  ],
}

describe('registry screen', () => {
  it('lists registered features and opens one by id', async () => {
    listFeatures.mockResolvedValue([ITEM])
    const navigate = vi.fn()
    render(<RegistryScreen featureId={null} navigate={navigate} />)
    expect(await screen.findByText('avg_balance_90d')).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: 'Open avg_balance_90d' }))
    expect(navigate).toHaveBeenCalledWith('registry', { id: 'feat_1' })
  })

  it('shows the Feature 360 — hypothesis, definition, lineage, and consumers', async () => {
    featureDetail.mockResolvedValue(DETAIL)
    render(<RegistryScreen featureId="feat_1" navigate={vi.fn()} />)
    expect(await screen.findByText(/customers churn when balance drops/)).toBeInTheDocument()
    expect(screen.getByText('Average balance over 90 days')).toBeInTheDocument()
    expect(screen.getByText('public.accounts.balance')).toBeInTheDocument()
    expect(screen.getByText('churn_model_v3')).toBeInTheDocument()
  })

  it('states honestly when a feature has no hypothesis on record', async () => {
    featureDetail.mockResolvedValue({ ...DETAIL, hypothesis: null, contract: null })
    render(<RegistryScreen featureId="feat_1" navigate={vi.fn()} />)
    expect(await screen.findByText(/registered directly/)).toBeInTheDocument()
  })
})
