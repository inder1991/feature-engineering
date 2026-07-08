import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ContractScreen } from './ContractScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    contractConsideredSet: vi.fn(),
    contractDraft: vi.fn(),
    contractConfirm: vi.fn(),
  }
})
const contractConsideredSet = vi.mocked(api.contractConsideredSet)
const contractDraft = vi.mocked(api.contractDraft)
const contractConfirm = vi.mocked(api.contractConfirm)

const idea = (name: string): api.Idea => ({
  name,
  description: `desc of ${name}`,
  derives_from: ['balance_gbp'],
  aggregation: 'trend',
  grain_table: 'accounts',
  derives_pairs: [['retail_core', 'balance_gbp']],
  verification: 'DESIGN-CHECKED',
  critic_note: '',
  rationale: `why ${name}`,
})

const draftResp = (): api.DraftResp => ({
  draft: {
    feature_name: 'balance_trend_90d',
    definition: 'slope of balance over 90d',
    grain_table: 'accounts',
    aggregation: 'trend',
    as_of_column: 'snapshot_date',
    derives_from: ['balance_gbp'],
    target_ref: 'churned',
    derives_pairs: [['retail_core', 'balance_gbp']],
    join_path: [],
  },
  unresolved: [],
  intent_id: 'int_1',
})

beforeEach(() => {
  contractConsideredSet.mockReset()
  contractDraft.mockReset()
  contractConfirm.mockReset()
})

describe('ContractScreen — the governed two-gate flow', () => {
  it('drives brief -> considered set -> draft -> a confirmed contract', async () => {
    contractConsideredSet.mockResolvedValue({
      intent_id: 'int_1',
      anchor: idea('balance_trend_90d'),
      alternatives: [{ lens: 'behavioral', features: [idea('dormancy_days')] }],
      recommendation: { recommended_lens: 'behavioral', reasoning: 'fits the drain',
        caveat: 'advisory only, not a performance prediction' },
    })
    contractDraft.mockResolvedValue(draftResp())
    contractConfirm.mockResolvedValue({
      contract_id: 'contract_1', feature_id: 'feat_1', feature_name: 'balance_trend_90d', version: 1 })

    render(<ContractScreen />)

    // Phase 1 — brief
    await userEvent.type(screen.getByLabelText(/hypothesis/i), 'balance drains then they leave')
    await userEvent.type(screen.getByLabelText(/objective/i), 'predict retail churn')
    await userEvent.click(screen.getByRole('button', { name: /generate considered set/i }))
    expect(contractConsideredSet).toHaveBeenCalledWith(
      'balance drains then they leave', 'predict retail churn', expect.objectContaining({}))

    // Phase 2 — considered set: both options render; pick the anchor
    expect(await screen.findByText('balance_trend_90d')).toBeInTheDocument()
    expect(screen.getByText('dormancy_days')).toBeInTheDocument()
    expect(screen.getByText(/safe, not proven/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('radio', { name: /balance_trend_90d/i }))
    await userEvent.click(screen.getByRole('button', { name: /draft selected/i }))
    expect(contractDraft).toHaveBeenCalledWith('int_1', 'anchor', 'balance_trend_90d')

    // Phase 3 — draft review -> confirm
    expect(await screen.findByText(/slope of balance over 90d/i)).toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /confirm.*govern/i }))
    expect(contractConfirm).toHaveBeenCalledOnce()
    const [draftArg, intentArg] = contractConfirm.mock.calls[0]
    expect(draftArg.feature_name).toBe('balance_trend_90d')
    expect(intentArg).toBe('int_1')

    // Phase 4 — done: the minted contract + honest stamp
    expect(await screen.findByText(/contract_1/)).toBeInTheDocument()
    expect(screen.getByText(/DESIGN-CHECKED/)).toBeInTheDocument()
  })

  it('surfaces an ApiError from generation as an alert', async () => {
    contractConsideredSet.mockRejectedValue(new api.ApiError(422, 'hypothesis must be non-empty'))
    render(<ContractScreen />)
    await userEvent.type(screen.getByLabelText(/hypothesis/i), 'x')
    await userEvent.type(screen.getByLabelText(/objective/i), 'y')
    await userEvent.click(screen.getByRole('button', { name: /generate considered set/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/hypothesis must be non-empty/i)
  })
})
