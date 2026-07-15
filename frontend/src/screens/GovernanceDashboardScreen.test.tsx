import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { GovernanceDashboardScreen } from './GovernanceDashboardScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getGovernanceDashboard: vi.fn(),
    getSourceGovernanceDashboard: vi.fn(),
  }
})
const getGovernanceDashboard = vi.mocked(api.getGovernanceDashboard)
const getSourceGovernanceDashboard = vi.mocked(api.getSourceGovernanceDashboard)

// The cross-source dashboard: one governed fact type with activity, a 3-deep queue, one
// calibration bucket, and one source row (the scoping entry point).
const DASH: api.GovernanceDashboard = {
  scope: 'catalog',
  source: null,
  generated_at: '2026-07-15T00:00:00+00:00',
  fact_types: [{
    fact_type: 'approved_join',
    pending: 1,
    confirmed: 2,
    rejected: 1,
    needs_attention: 0,
    rejected_by_category: { different_entity: 1 },
  }],
  queue_health: {
    open_depth: 3,
    oldest_pending_age_seconds: 3 * 86400,
    age_buckets: { lt_1d: 1, '1_7d': 1, gt_7d: 1 },
  },
  calibration_seed: {
    confirm_rate_by_bucket: { strong: { confirmed: 2, rejected: 1, rate: 0.66 } },
    reject_category_by_top_signal: { same_column_name: { different_entity: 1 } },
  },
  recent_activity: { days: 30, confirmed: 2, rejected: 1 },
  sources: [{
    source: 'compliance', pending: 1, confirmed: 2, rejected: 1,
    oldest_pending_age_seconds: 7200,
  }],
}

// The same dashboard scoped to one source: the wire omits `sources` on the per-source route.
const SCOPED: api.GovernanceDashboard = {
  ...DASH,
  scope: 'source',
  source: 'compliance',
  sources: undefined,
}

const ZEROS: api.GovernanceDashboard = {
  scope: 'catalog',
  source: null,
  generated_at: '2026-07-15T00:00:00+00:00',
  fact_types: [{
    fact_type: 'approved_join',
    pending: 0, confirmed: 0, rejected: 0, needs_attention: 0, rejected_by_category: {},
  }],
  queue_health: { open_depth: 0, oldest_pending_age_seconds: null, age_buckets: {} },
  calibration_seed: { confirm_rate_by_bucket: {}, reject_category_by_top_signal: {} },
  recent_activity: { days: 30, confirmed: 0, rejected: 0 },
  sources: [],
}

beforeEach(() => {
  getGovernanceDashboard.mockReset()
  getGovernanceDashboard.mockResolvedValue(DASH)
  getSourceGovernanceDashboard.mockReset()
  getSourceGovernanceDashboard.mockResolvedValue(SCOPED)
})

describe('governance dashboard screen', () => {
  it('renders the rollup counts, reject categories, queue health, calibration seed, and sources', async () => {
    render(<GovernanceDashboardScreen />)
    // Summary card: the four folded-status counts for the fact type.
    const rollup = await screen.findByRole('group', { name: 'Joins rollup' })
    expect(rollup).toHaveTextContent('1 pending')
    expect(rollup).toHaveTextContent('2 confirmed')
    expect(rollup).toHaveTextContent('1 rejected')
    expect(rollup).toHaveTextContent('0 needs attention')
    // Rejected-by-category list (underscores humanized).
    expect(screen.getByText(/different entity: 1/)).toBeInTheDocument()
    // Queue health: depth, humanized oldest age, the age buckets.
    const queue = screen.getByRole('group', { name: 'Queue health' })
    expect(queue).toHaveTextContent('3 open tasks')
    expect(queue).toHaveTextContent('3d oldest pending')
    expect(queue).toHaveTextContent('1 open < 1 day')
    expect(queue).toHaveTextContent('1 open 1–7 days')
    expect(queue).toHaveTextContent('1 open > 7 days')
    // Calibration seed: the observation caveat + the strong bucket's tallies and rate.
    expect(screen.getByText(/observation — signal vs\. outcome/i)).toBeInTheDocument()
    expect(screen.getByText(/tuning is a later step/i)).toBeInTheDocument()
    const strongRow = screen.getByText('strong').closest('tr')
    expect(strongRow).toHaveTextContent('2')
    expect(strongRow).toHaveTextContent('66%')
    // Cross-source overview: the source row renders as a scoping button.
    expect(screen.getByRole('button', { name: 'compliance' })).toBeInTheDocument()
    expect(getGovernanceDashboard).toHaveBeenCalledTimes(1)
  })

  it('a source row scopes the whole view; "Back to all catalogs" clears the scope', async () => {
    render(<GovernanceDashboardScreen />)
    await userEvent.click(await screen.findByRole('button', { name: 'compliance' }))
    expect(getSourceGovernanceDashboard).toHaveBeenCalledWith('compliance')
    // Scoped: the scope line + back control appear, the cross-source table is gone.
    expect(await screen.findByText(/scoped to/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'compliance' })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /back to all catalogs/i }))
    expect(getGovernanceDashboard).toHaveBeenCalledTimes(2) // mount + the un-scope refetch
    expect(await screen.findByRole('button', { name: 'compliance' })).toBeInTheDocument()
    expect(screen.queryByText(/scoped to/i)).not.toBeInTheDocument()
  })

  it('shows the empty state when nothing is recorded anywhere', async () => {
    getGovernanceDashboard.mockResolvedValue(ZEROS)
    render(<GovernanceDashboardScreen />)
    expect(await screen.findByText(/nothing recorded yet/i)).toBeInTheDocument()
    expect(screen.queryByRole('group', { name: 'Joins rollup' })).not.toBeInTheDocument()
  })

  it('surfaces an ApiError detail as the alert', async () => {
    getGovernanceDashboard.mockRejectedValue(new api.ApiError(403, 'need catalog:read'))
    render(<GovernanceDashboardScreen />)
    expect(await screen.findByRole('alert')).toHaveTextContent('need catalog:read')
  })
})
