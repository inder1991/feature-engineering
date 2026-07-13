import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { GovernanceReviewScreen } from './GovernanceReviewScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    listJoinProposals: vi.fn(),
    confirmJoin: vi.fn(),
    rejectJoin: vi.fn(),
  }
})
const listJoinProposals = vi.mocked(api.listJoinProposals)
const confirmJoin = vi.mocked(api.confirmJoin)
const rejectJoin = vi.mocked(api.rejectJoin)

// Block body (not an arrow returning the reset): a function returned from beforeEach is treated
// as a per-test teardown by Vitest (same convention as ReviewQueueScreen.test.tsx).
beforeEach(() => {
  listJoinProposals.mockReset()
  confirmJoin.mockReset()
  confirmJoin.mockResolvedValue({
    governance_status: 'PARTIALLY_CONFIRMED',
    operational_projection: 'not_applicable',
    approvals: [],
  })
  rejectJoin.mockReset()
  rejectJoin.mockResolvedValue({ governance_status: 'REJECTED', category: 'different_entity' })
})

// One PROPOSED proposal with parsed evidence: 4 baseline checklist items + 2 derived (signals).
const PROPOSAL: api.JoinProposal = {
  fact_key: 'fact:approved_join:tx.cif->cust.cif',
  tasks: [{ task_id: 't1', side: 'from', status: 'open' }],
  from: { table: 'COMP_FINANCIAL_TRAN_REPOS_DLY', column: 'CIF_ID' },
  to: { table: 'CUSTOMER_MASTER_DLY', column: 'CIF_ID' },
  cardinality: 'N:1',
  proposed_direction: 'COMP_FINANCIAL_TRAN_REPOS_DLY.CIF_ID -> CUSTOMER_MASTER_DLY.CIF_ID',
  status: 'PROPOSED',
  approvals: [],
  evidence: {
    score: 85,
    positive_signals: [
      { signal_name: 'same_identifier_concept', score_delta: 40 },
      { signal_name: 'same_column_name', score_delta: 30 },
    ],
    negative_signals: [],
    namespace_compatibility: 'compatible',
    namespace_reason_codes: [],
    grain_status: 'inferred_from_confirmed_grain',
    grain_evidence: [],
    explanation: 'strong candidate',
    warnings: [],
  },
  evidence_version: 'passc-algo-v1',
  evidence_parse_status: 'parsed',
}

async function loadQueue() {
  render(<GovernanceReviewScreen />)
  await userEvent.type(screen.getByLabelText('Source'), 'compliance')
  await userEvent.click(screen.getByRole('button', { name: /load proposals/i }))
}

describe('governance review screen', () => {
  it('renders a proposal card with from/to, cardinality, and the advisory score', async () => {
    listJoinProposals.mockResolvedValue({
      source: 'compliance', proposals: [PROPOSAL], next_cursor: null,
    })
    await loadQueue()
    // The table names appear in both the join strip and the consequence line.
    expect(await screen.findAllByText('COMP_FINANCIAL_TRAN_REPOS_DLY')).not.toHaveLength(0)
    expect(screen.getAllByText('CUSTOMER_MASTER_DLY')).not.toHaveLength(0)
    expect(screen.getAllByText('N:1')).not.toHaveLength(0)
    expect(screen.getByText('85')).toBeInTheDocument() // demoted advisory score pill
    // The metadata-only caution appears both as the caution line and a checklist item.
    expect(screen.getAllByText(/no sample rows were compared/i)).not.toHaveLength(0)
    expect(screen.getByText(/if wrong/i)).toBeInTheDocument()
    expect(listJoinProposals).toHaveBeenCalledWith('compliance')
  })

  it('gates Approve on the checklist: disabled until every item is ticked, then confirms', async () => {
    listJoinProposals.mockResolvedValue({
      source: 'compliance', proposals: [PROPOSAL], next_cursor: null,
    })
    await loadQueue()
    const approveBtn = await screen.findByRole('button', { name: /^approve$/i })
    expect(approveBtn).toBeDisabled()
    // 4 baseline items + one per positive signal (evidence parsed) = 6 checkboxes
    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(6)
    for (const box of boxes.slice(0, -1)) await userEvent.click(box)
    expect(approveBtn).toBeDisabled() // one still unticked -> still gated
    await userEvent.click(boxes[boxes.length - 1])
    expect(approveBtn).toBeEnabled()
    await userEvent.click(approveBtn)
    expect(confirmJoin).toHaveBeenCalledWith(PROPOSAL.fact_key, {})
    expect(
      await screen.findByText(/a different, second admin must confirm/i),
    ).toBeInTheDocument()
  })

  it('rejects with a structured category + note; the confirm button is gated on a category', async () => {
    listJoinProposals.mockResolvedValue({
      source: 'compliance', proposals: [PROPOSAL], next_cursor: null,
    })
    await loadQueue()
    await userEvent.click(await screen.findByRole('button', { name: /reject…/i }))
    const confirmReject = screen.getByRole('button', { name: /confirm rejection/i })
    expect(confirmReject).toBeDisabled() // no category picked yet
    await userEvent.click(screen.getByRole('button', { name: /different entity/i }))
    await userEvent.type(screen.getByLabelText(/rejection note/i), 'watchlist CIF, not customer')
    await userEvent.click(confirmReject)
    expect(rejectJoin).toHaveBeenCalledWith(PROPOSAL.fact_key, {
      category: 'different_entity',
      note: 'watchlist CIF, not customer',
    })
    expect(await screen.findByText(/rejected \(different entity\)/i)).toBeInTheDocument()
    expect(confirmJoin).not.toHaveBeenCalled()
  })

  it('shows the first approver\'s note on a PARTIALLY_CONFIRMED card', async () => {
    listJoinProposals.mockResolvedValue({
      source: 'compliance',
      proposals: [{
        ...PROPOSAL,
        status: 'PARTIALLY_CONFIRMED',
        approvals: [{
          subject: 'a.rahman', display_name: null, role: 'platform-admin',
          note: 'Check the account namespace.', confirmed_at: '2026-07-10T00:00:00Z',
        }],
      }],
      next_cursor: null,
    })
    await loadQueue()
    expect(await screen.findByText(/a\.rahman approved/i)).toBeInTheDocument()
    expect(screen.getByText(/"Check the account namespace\."/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /approve as 2nd approver/i })).toBeDisabled()
    // No note input on the second-approver card: this approval VERIFIES the join — a note "for
    // the next approver" would have no next reader. Only the PROPOSED card offers it.
    expect(screen.queryByLabelText(/note for the next approver/i)).not.toBeInTheDocument()
  })

  it('with missing evidence still renders the 4 baseline checklist items and keeps Approve gateable', async () => {
    // The "gate stays gateable" property: an absent/unreadable evidence record must neither
    // auto-enable Approve (an ungated approval) nor permanently disable it (an unapprovable
    // proposal) — the 4 BASELINE items still render and still gate, with no derived signal items.
    listJoinProposals.mockResolvedValue({
      source: 'compliance',
      proposals: [{ ...PROPOSAL, evidence: {}, evidence_version: null, evidence_parse_status: 'missing' }],
      next_cursor: null,
    })
    await loadQueue()
    const approveBtn = await screen.findByRole('button', { name: /^approve$/i })
    expect(approveBtn).toBeDisabled() // never auto-enabled
    expect(screen.getByText(/score unavailable \(missing\)/i)).toBeInTheDocument()
    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(4) // exactly the baseline — no signal items without parsed evidence
    for (const box of boxes.slice(0, -1)) await userEvent.click(box)
    expect(approveBtn).toBeDisabled() // one still unticked -> still gated
    await userEvent.click(boxes[boxes.length - 1])
    expect(approveBtn).toBeEnabled() // never permanently disabled
  })

  it('on a 409 conflict shows the server detail and reloads the list, never blind-retrying', async () => {
    listJoinProposals
      .mockResolvedValueOnce({ source: 'compliance', proposals: [PROPOSAL], next_cursor: null })
      .mockResolvedValueOnce({ source: 'compliance', proposals: [], next_cursor: null })
    confirmJoin.mockRejectedValue(new api.ApiError(409, 'Changed since you loaded it — refresh.'))
    await loadQueue()
    for (const box of await screen.findAllByRole('checkbox')) await userEvent.click(box)
    await userEvent.click(screen.getByRole('button', { name: /^approve$/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/changed since you loaded it/i)
    expect(listJoinProposals).toHaveBeenCalledTimes(2) // reloaded
    expect(confirmJoin).toHaveBeenCalledTimes(1) // never blind-retried
    expect(screen.getByText(/no open join proposals/i)).toBeInTheDocument()
  })
})
