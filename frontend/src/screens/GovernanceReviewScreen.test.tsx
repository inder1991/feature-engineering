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
    listTableFactProposals: vi.fn(),
    confirmTableFact: vi.fn(),
    rejectTableFact: vi.fn(),
  }
})
const listJoinProposals = vi.mocked(api.listJoinProposals)
const confirmJoin = vi.mocked(api.confirmJoin)
const rejectJoin = vi.mocked(api.rejectJoin)
const listTableFactProposals = vi.mocked(api.listTableFactProposals)
const confirmTableFact = vi.mocked(api.confirmTableFact)
const rejectTableFact = vi.mocked(api.rejectTableFact)

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
  // The screen loads BOTH queues per source; the joins tests only exercise the joins tab, so
  // the table-facts queue defaults to empty (and vice versa is set explicitly per test).
  listTableFactProposals.mockReset()
  listTableFactProposals.mockResolvedValue({
    source: 'compliance', proposals: [], next_cursor: null,
  })
  confirmTableFact.mockReset()
  confirmTableFact.mockResolvedValue({
    governance_status: 'VERIFIED', operational_projection: 'projected',
  })
  rejectTableFact.mockReset()
  rejectTableFact.mockResolvedValue({ governance_status: 'REJECTED', category: 'not_unique' })
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

  it('grain & availability tab: renders the grain card, gates Approve on the checklist, single confirm', async () => {
    listJoinProposals.mockResolvedValue({ source: 'compliance', proposals: [], next_cursor: null })
    listTableFactProposals.mockResolvedValue({
      source: 'compliance',
      proposals: [{
        fact_key: 'fact:grain:compliance.t',
        task_id: 'tf1',
        target_event_id: 'ev1',
        fact_type: 'grain',
        table: 't',
        proposed_value: { columns: ['cif_id'], is_unique: true },
        status: 'PROPOSED',
        origin: 'llm_proposed_not_profiled',
        advisory: { table_role: null, primary_entity: null, event_or_snapshot: null },
        evidence_parse_status: 'parsed',
      }],
      next_cursor: null,
    })
    await loadQueue()
    await userEvent.click(
      await screen.findByRole('button', { name: /grain & availability \(1\)/i }),
    )
    // The proposed grain column renders (value strip + checklist items + consequence line).
    expect(await screen.findAllByText(/cif_id/)).not.toHaveLength(0)
    expect(screen.getByText(/llm-inferred from names & descriptions/i)).toBeInTheDocument()
    const approveBtn = screen.getByRole('button', { name: /^approve$/i })
    expect(approveBtn).toBeDisabled() // gated until the whole checklist is ticked
    const boxes = screen.getAllByRole('checkbox')
    expect(boxes).toHaveLength(4) // exactly the 4 baseline items — table facts have no signals
    for (const box of boxes.slice(0, -1)) await userEvent.click(box)
    expect(approveBtn).toBeDisabled() // one still unticked -> still gated
    await userEvent.click(boxes[boxes.length - 1])
    expect(approveBtn).toBeEnabled()
    await userEvent.click(approveBtn)
    expect(confirmTableFact).toHaveBeenCalledWith('fact:grain:compliance.t', {})
    // SINGLE-confirmer: one approve verifies + projects — never any "1 of 2" partial UI.
    expect(await screen.findByText(/verified · live/i)).toBeInTheDocument()
    expect(screen.queryByText(/1 of 2|awaiting 2nd|second admin/i)).not.toBeInTheDocument()
    expect(rejectTableFact).not.toHaveBeenCalled()
  })

  it('keeps the joins tab populated when only the table-facts fetch fails (decoupled queues)', async () => {
    // Whole-branch review FIX 2: the two queues settle independently — a table-facts endpoint
    // failure must not blank the joins tab; it surfaces as a per-tab error on the facts tab.
    listJoinProposals.mockResolvedValue({
      source: 'compliance', proposals: [PROPOSAL], next_cursor: null,
    })
    listTableFactProposals.mockRejectedValue(new api.ApiError(500, 'table-facts queue exploded'))
    await loadQueue()
    // The joins queue rendered despite the sibling failure.
    expect(await screen.findAllByText('COMP_FINANCIAL_TRAN_REPOS_DLY')).not.toHaveLength(0)
    expect(screen.getByRole('button', { name: /joins \(1\)/i })).toBeInTheDocument()
    // The facts tab is still reachable and shows ITS error, not a blank screen.
    await userEvent.click(screen.getByRole('button', { name: /grain & availability/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent(/table-facts queue exploded/i)
    // Switching back: the joins queue is still there.
    await userEvent.click(screen.getByRole('button', { name: /joins \(1\)/i }))
    expect(screen.getAllByText('COMP_FINANCIAL_TRAN_REPOS_DLY')).not.toHaveLength(0)
  })

  it('keeps the facts tab loadable when only the joins fetch fails (the reverse decoupling)', async () => {
    listJoinProposals.mockRejectedValue(new api.ApiError(500, 'joins queue exploded'))
    listTableFactProposals.mockResolvedValue({
      source: 'compliance',
      proposals: [{
        fact_key: 'fact:grain:compliance.t',
        task_id: 'tf1',
        target_event_id: 'ev1',
        fact_type: 'grain' as const,
        table: 't',
        proposed_value: { columns: ['cif_id'], is_unique: true },
        status: 'PROPOSED' as const,
        origin: 'llm_proposed_not_profiled',
        advisory: { table_role: null, primary_entity: null, event_or_snapshot: null },
        evidence_parse_status: 'parsed',
      }],
      next_cursor: null,
    })
    await loadQueue()
    // The joins tab (the default) shows its own error…
    expect(await screen.findByRole('alert')).toHaveTextContent(/joins queue exploded/i)
    // …while the facts queue is intact and reachable.
    await userEvent.click(screen.getByRole('button', { name: /grain & availability \(1\)/i }))
    expect(await screen.findAllByText(/cif_id/)).not.toHaveLength(0)
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
