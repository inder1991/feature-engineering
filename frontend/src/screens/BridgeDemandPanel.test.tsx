import { render, screen, within } from '@testing-library/react'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { BridgeDemandPanel } from './BridgeDemandPanel'

// The bridge-demand panel (S1B-4). It renders a MEASUREMENT — what governed planning needed and
// did not have — so the three things it must get right are the framing (a demand row is not a
// failure and nothing is "blocked"), the arithmetic (demand rows, questions and runs are three
// different questions and must not be fused), and the two absences (an empty queue is not a
// refused read, and a 403 is not an error).

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getBridgeDemand: vi.fn() }
})
const getBridgeDemand = vi.mocked(api.getBridgeDemand)

// A BLOCK body, deliberately. `beforeEach(() => mock.mockReset())` returns the mock, and Vitest
// registers a hook's returned FUNCTION as that test's teardown — so it would CALL the mock after
// every test and leave an unhandled rejection behind the two refusal cases below.
beforeEach(() => {
  getBridgeDemand.mockReset()
})

function counts(over: Partial<api.BridgeDemandCounts> = {}): api.BridgeDemandCounts {
  return {
    demand_rows: 1,
    distinct_intents: 1,
    distinct_runs: 1,
    live_observations: 1,
    telemetry_observations: 0,
    recent_observations: 1,
    historical_observations: 0,
    ...over,
  }
}

function group(over: Partial<api.BridgeDemandGroup> = {}): api.BridgeDemandGroup {
  return {
    ...counts(),
    relationship_id: 'transaction_to_account',
    from_entity: 'transaction',
    to_entity: 'account',
    suggested_endpoints: ['rev::public.accounts.account_id',
                          'ops::public.transactions.account_id'],
    nested: [{
      ...counts(),
      position_catalog: 'ops',
      position_table_ref: 'public.transactions',
      suggested_endpoints: ['rev::public.accounts.account_id'],
    }],
    ...over,
  }
}

function payload(over: Partial<api.BridgeDemandResp> = {}): api.BridgeDemandResp {
  return {
    as_of: '2026-08-24T00:00:00Z',
    limit: 50,
    cursor: null,
    next_cursor: null,
    queues: { bridge_demand: [], realization_gap: [], planner_capacity: [] },
    resolution_summary: {
      as_of: '2026-08-24T00:00:00Z',
      resolved_statuses: ['resolved'],
      by_origin: [],
      by_anchor_catalog: [],
      totals: { observations: 0, resolved: 0, resolution_rate: 0 },
    },
    ...over,
  }
}

async function renderPanel(body: api.BridgeDemandResp) {
  getBridgeDemand.mockResolvedValue(body)
  render(<BridgeDemandPanel />)
  await screen.findByTestId('bridge-demand')
}

function rejectWith(status: number, detail: string) {
  getBridgeDemand.mockImplementation(() => Promise.reject(new api.ApiError(status, detail)))
}

it('renders a bridge-demand crossing with its counts, mode and endpoints', async () => {
  await renderPanel(payload({
    queues: {
      bridge_demand: [group({
        ...counts({ demand_rows: 5, distinct_intents: 2, distinct_runs: 5,
                    live_observations: 3, telemetry_observations: 2,
                    recent_observations: 4, historical_observations: 1 }),
      })],
      realization_gap: [],
      planner_capacity: [],
    },
  }))

  const row = screen.getByTestId('bd-bridge_demand-group')
  // the crossing reads in the direction a roll-up travels
  expect(row).toHaveTextContent('account ← transaction')
  expect(row).toHaveTextContent('transaction_to_account')
  // THREE counts, three questions — five attempts from two questions across five runs is a retry
  // storm, and the panel must show it as one rather than as five demands
  expect(within(row).getByTestId('bd-counts')).toHaveTextContent(
    '5 planning attempts could not complete here · 2 questions asked · 5 runs')
  expect(within(row).getByTestId('bd-mode')).toHaveTextContent(
    'Recorded by: 3 live, 2 telemetry · 4 in the last 7 days, 1 older')
  expect(within(row).getByTestId('bd-endpoints')).toHaveTextContent(
    'Endpoints: rev::public.accounts.account_id, ops::public.transactions.account_id')
  // the nested position row: where an engineer would actually build it
  const position = within(row).getByTestId('bd-position')
  expect(position).toHaveTextContent('ops · public.transactions')
  expect(position).toHaveTextContent('1 attempt, 1 question')
})

it('never frames a demand row as a failure', async () => {
  await renderPanel(payload({
    queues: { bridge_demand: [group()], realization_gap: [], planner_capacity: [] },
  }))
  const panel = screen.getByTestId('bridge-demand')
  // "blocked" is the word this whole surface exists not to say; the others would turn "nobody has
  // built this yet" into "something is broken".
  expect(panel.textContent).not.toMatch(/blocked|error|failure|failed|invalid|violation/i)
  // ...and nothing below VERIFIED is ever called approved
  expect(panel.textContent).not.toMatch(/approved/i)
})

it('names the realization gap as a data-model gap, not a permissions one', async () => {
  await renderPanel(payload({
    queues: {
      bridge_demand: [],
      realization_gap: [group({ suggested_endpoints: ['rev::public.accounts.account_id'] })],
      planner_capacity: [],
    },
  }))
  const queue = screen.getByTestId('bd-queue-realization_gap')
  expect(queue).toHaveTextContent(/data-model gap — no catalog realizes this roll-up/i)
  expect(screen.getByTestId('bd-next-realization_gap')).toHaveTextContent(/building the realization/i)
  expect(within(queue).getByTestId('bd-realization_gap-group')).toBeInTheDocument()
})

it('renders a capacity row without a crossing and offers nothing to decide', async () => {
  // A capacity row drops its hop identity by ruling — the demand is the BUDGET, not whichever hop
  // the frontier was sitting on — so its entity columns are honestly blank.
  await renderPanel(payload({
    queues: {
      bridge_demand: [],
      realization_gap: [],
      planner_capacity: [group({ relationship_id: '', from_entity: '', to_entity: '',
                                 suggested_endpoints: [], nested: [] })],
    },
  }))
  const queue = screen.getByTestId('bd-queue-planner_capacity')
  expect(queue).toHaveTextContent(/planner budget — operational/i)
  expect(within(queue).getByTestId('bd-planner_capacity-group'))
    .toHaveTextContent('No single crossing — a whole search ran out of budget')
  // no next-step prose here: there is nothing for a reviewer to decide about a budget
  expect(screen.queryByTestId('bd-next-planner_capacity')).toBeNull()
})

it('shows the resolution summary as the queues’ denominator', async () => {
  await renderPanel(payload({
    resolution_summary: {
      as_of: '2026-08-24T00:00:00Z',
      resolved_statuses: ['resolved'],
      by_origin: [
        { definition_origin: 'recipe_v2', observations: 8, resolved: 6, resolution_rate: 0.75 },
        { definition_origin: 'llm_intent', observations: 2, resolved: 0, resolution_rate: 0 },
      ],
      by_anchor_catalog: [],
      totals: { observations: 10, resolved: 6, resolution_rate: 0.6 },
    },
  }))
  expect(screen.getByTestId('bd-summary')).toHaveTextContent(
    '10 planning requests recorded · 6 resolved (60%) · by origin: recipe_v2 75% · llm_intent 0%')
})

it('states honest absence when nothing has been recorded', async () => {
  await renderPanel(payload())
  const empty = screen.getByTestId('bd-empty-bridge_demand')
  expect(empty).toHaveAttribute('role', 'status')
  expect(empty).toHaveTextContent(
    'No bridge demand recorded yet — rows appear when governed cross-catalog planning cannot '
    + 'complete a roll-up.')
  // all three queues answer for themselves; an empty realization queue is not the bridge one's zero
  expect(screen.getByTestId('bd-empty-realization_gap')).toBeInTheDocument()
  expect(screen.getByTestId('bd-empty-planner_capacity')).toBeInTheDocument()
})

it('treats a 403 as an ordinary refusal, quoting the server', async () => {
  rejectWith(403, 'platform-admin claim required')
  render(<BridgeDemandPanel />)
  const refusal = await screen.findByTestId('bd-not-yours')
  expect(refusal).toHaveAttribute('role', 'status')
  expect(refusal).toHaveTextContent('platform-admin claim required')
  expect(refusal).toHaveTextContent(/nothing is wrong/i)
  // NOT an alert, and no empty queue that would read as "nothing was ever recorded"
  expect(screen.queryByRole('alert')).toBeNull()
  expect(screen.queryByTestId('bd-empty-bridge_demand')).toBeNull()
})

it('says a failed read is a failed read, never an empty queue', async () => {
  rejectWith(500, 'the ledger is unavailable')
  render(<BridgeDemandPanel />)
  const notice = await screen.findByTestId('bd-notice')
  expect(notice).toHaveTextContent('the ledger is unavailable')
  expect(screen.getByTestId('bridge-demand')).toHaveTextContent(/could not be read/i)
  expect(screen.queryByTestId('bd-empty-bridge_demand')).toBeNull()
})
