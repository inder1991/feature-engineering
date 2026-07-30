import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { readFileSync } from 'node:fs'
// Aliased: `join` is a fixture builder in this file (a discovered-join queue item).
import { join as joinPath } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { GovernanceReviewScreen } from './GovernanceReviewScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getGovernanceQueue: vi.fn(),
    confirmEntityBridge: vi.fn(),
    rejectEntityBridge: vi.fn(),
    bulkRejectEntityBridges: vi.fn(),
    confirmJoin: vi.fn(),
    rejectJoin: vi.fn(),
    confirmTableFact: vi.fn(),
    rejectTableFact: vi.fn(),
  }
})
const getGovernanceQueue = vi.mocked(api.getGovernanceQueue)
const confirmEntityBridge = vi.mocked(api.confirmEntityBridge)
const rejectEntityBridge = vi.mocked(api.rejectEntityBridge)
const bulkRejectEntityBridges = vi.mocked(api.bulkRejectEntityBridges)
const confirmJoin = vi.mocked(api.confirmJoin)
const confirmTableFact = vi.mocked(api.confirmTableFact)

// ── fixtures ─────────────────────────────────────────────────────────────────────────────────────
// Shaped exactly as overlay/upload/governance_queue.py emits them, including the two INDEPENDENT
// code fields and the per-category usage tri-state.

// The five real "already depended on by" categories, deliberately mixing a REAL 0 (the store holds
// observations and none of them names this bridge) with two `not_tracked_yet` (nothing was recorded
// at all, so a number would be a lie). Both must render, and differently.
const USAGE: api.GovernanceQueueUsage[] = [
  {
    category: 'planned_candidates', state: 'counted', count: 0, display: '0',
    store: 'multisource_assembly_shadow_operand_obs.crossings[].bridge_fact_key',
    basis: 'an assembled candidate plan whose operand path crossed this bridge', reason: '',
  },
  {
    category: 'selected_plans', state: 'counted', count: 3, display: '3',
    store: 'multisource_assembly_shadow_intent_result.selected_plan_id',
    basis: 'the plan an intent selected, whose operand path crossed this bridge', reason: '',
  },
  {
    category: 'generated_artifacts', state: 'not_tracked_yet', count: null,
    display: 'not tracked yet', store: 'materialization control plane (migration 1034)',
    basis: 'a rendered generation whose lineage names this bridge',
    reason: 'the control plane identifies a generation by group/project HASH only; no table in it '
      + 'records a bridge fact_key',
  },
  {
    category: 'published_features', state: 'counted', count: 1, display: '1',
    store: 'feature_current_contract + contract_metadata_dependency.logical_ref',
    basis: 'a registered feature whose CURRENT governed contract depends on this bridge',
    reason: '',
  },
  {
    category: 'data_agent_analyses', state: 'not_tracked_yet', count: null,
    display: 'not tracked yet', store: 'none',
    basis: 'an analysis whose plan traversed this bridge',
    reason: 'AnalysisPlan.join_refs is never persisted and there is no analysis-run store, so it '
      + 'cannot answer this',
  },
]

function bridgeDetail(over: Record<string, unknown> = {}): Record<string, unknown> {
  return {
    entity_id: 'customer',
    left: { catalog_source: 'cib', schema: 'public', table: 'customer_master_dly',
            column: 'cif_id' },
    right: { catalog_source: 'ftr', schema: 'public', table: 'party_dly', column: 'cif_id' },
    data_type_family: 'string', type_basis: 'declared', strength: 3, evidence_present: true,
    proposed_by: 'svc:enrichment', proposed_at: '2026-07-20T09:15:00+00:00',
    target_event_id: 'ev-1',
    ...over,
  }
}

function bridge(over: Partial<api.GovernanceQueueItem> = {}): api.GovernanceQueueItem {
  return {
    kind: 'entity_bridge',
    fact_key: 'fact:entity_bridge:customer',
    catalogs: ['cib', 'ftr'],
    subject: 'cib.customer_master_dly.cif_id <-> ftr.party_dly.cif_id',
    // UNREVIEWED and yet PRODUCTION-ELIGIBLE — the combination that proves the two axes move
    // independently. A screen that fused them could not render this row honestly.
    state: 'Unreviewed — available for use',
    state_code: 'unreviewed_available',
    production_eligibility: 'Automatically validated for production',
    production_eligibility_code: 'grain_resolved',
    available_actions: ['confirm', 'reject'],
    detail: bridgeDetail(),
    already_depended_on_by: USAGE,
    ...over,
  }
}

function join(over: Partial<api.GovernanceQueueItem> = {}): api.GovernanceQueueItem {
  return {
    kind: 'approved_join',
    fact_key: 'fact:approved_join:tran.cif->cust.cif',
    // ONE catalog: list_open_approved_join_proposals filters on from_ref.catalog_source only and is
    // NOT endpoint-symmetric, so a join is never a cross-catalog finding.
    catalogs: ['cib'],
    subject: 'COMP_FINANCIAL_TRAN_REPOS_DLY.CIF_ID -> CUSTOMER_MASTER_DLY.CIF_ID',
    state: 'Unreviewed — available for use',
    state_code: 'unreviewed_available',
    production_eligibility: 'Cardinality unresolved — sandbox only',
    production_eligibility_code: 'cardinality_unresolved',
    available_actions: ['confirm', 'reject'],
    detail: {
      from: { table: 'COMP_FINANCIAL_TRAN_REPOS_DLY', column: 'CIF_ID' },
      to: { table: 'CUSTOMER_MASTER_DLY', column: 'CIF_ID' },
      cardinality: 'N:1', approvals: [], tasks: [{ task_id: 't1', side: 'from', status: 'open' }],
      evidence_parse_status: 'parsed',
    },
    // Joins have no bridge anchor, so the read model sends NO usage — and an empty usage block
    // must never be rendered as "0 dependencies".
    already_depended_on_by: [],
    ...over,
  }
}

function grain(over: Partial<api.GovernanceQueueItem> = {}): api.GovernanceQueueItem {
  return {
    kind: 'grain',
    fact_key: 'fact:grain:ftr.party_dly',
    catalogs: ['ftr'],
    subject: 'ftr.party_dly',
    state: 'Unreviewed — available for use',
    state_code: 'unreviewed_available',
    production_eligibility: null,
    production_eligibility_code: 'not_applicable',
    available_actions: ['confirm', 'reject'],
    detail: {
      table: 'party_dly', proposed_value: { columns: ['cif_id'], is_unique: true },
      origin: 'llm_enrichment', advisory: {}, task_id: 'tf-1', target_event_id: 'ev-2',
      evidence_parse_status: 'parsed',
    },
    already_depended_on_by: [],
    ...over,
  }
}

function queue(over: Partial<api.GovernanceQueue> = {}): api.GovernanceQueue {
  return {
    items: [],
    catalogs: ['cib', 'ftr'],
    items_visible_to_you_by_catalog: { cib: 0, ftr: 0 },
    items_visible_to_you_by_kind: {
      entity_bridge: 0, approved_join: 0, grain: 0, availability_time: 0,
    },
    unreadable: [],
    complete: true,
    truncated: false,
    counts_are_scope_relative: true,
    next_cursor: null,
    ...over,
  }
}

// The whole cross-catalog list: one bridge, one join, one grain fact.
const FULL = queue({
  items: [bridge(), join(), grain()],
  items_visible_to_you_by_catalog: { cib: 2, ftr: 2 },
  items_visible_to_you_by_kind: {
    entity_bridge: 1, approved_join: 1, grain: 1, availability_time: 0,
  },
})

// The eight `branch` bridge candidates: a 4x2 cross-product of the SAME two facts, not eight
// findings. Same entity, same catalog pair, eight fact_keys.
const BRANCH = Array.from({ length: 8 }, (_, i) => bridge({
  fact_key: `fact:entity_bridge:branch:${i}`,
  subject: `cib.acct_dly.branch_${i % 4} <-> ftr.branch_dly.${i < 4 ? 'branch_id' : 'br_code'}`,
  detail: bridgeDetail({
    entity_id: 'branch',
    left: { catalog_source: 'cib', schema: 'public', table: 'acct_dly',
            column: `branch_${i % 4}` },
    right: { catalog_source: 'ftr', schema: 'public', table: 'branch_dly',
             column: i < 4 ? 'branch_id' : 'br_code' },
  }),
  production_eligibility: 'Cardinality unresolved — sandbox only',
  production_eligibility_code: 'cardinality_unresolved',
}))

beforeEach(() => {
  getGovernanceQueue.mockReset()
  getGovernanceQueue.mockResolvedValue(queue())
  confirmEntityBridge.mockReset()
  confirmEntityBridge.mockResolvedValue({
    governance_status: 'VERIFIED', operational_projection: 'projected',
  })
  rejectEntityBridge.mockReset()
  rejectEntityBridge.mockResolvedValue({
    governance_status: 'REJECTED', category: 'not_the_same_entity',
    operational_projection: 'not_applicable',
  })
  bulkRejectEntityBridges.mockReset()
  bulkRejectEntityBridges.mockResolvedValue({
    category: 'not_the_same_entity',
    counts: { rejected: 8, already_rejected: 0, denied: 0, not_found: 0, failed: 0 },
    results: BRANCH.map(b => ({ fact_key: b.fact_key, outcome: 'rejected' })),
  })
  confirmJoin.mockReset()
  confirmJoin.mockResolvedValue({
    governance_status: 'PARTIALLY_CONFIRMED', operational_projection: 'not_applicable',
    approvals: [],
  })
  confirmTableFact.mockReset()
  confirmTableFact.mockResolvedValue({
    governance_status: 'VERIFIED', operational_projection: 'projected',
  })
})

function row(item: api.GovernanceQueueItem): HTMLElement {
  return screen.getByTestId(`row-${item.fact_key}`)
}

describe('governance review — the decision queue', () => {
  it('renders the queue on mount: no source to type, nothing to click first', async () => {
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen />)

    // The whole point: the work is on screen without a single interaction.
    expect(await screen.findByTestId(`row-${FULL.items[0].fact_key}`)).toBeInTheDocument()
    expect(row(FULL.items[1])).toBeInTheDocument()
    expect(row(FULL.items[2])).toBeInTheDocument()
    expect(getGovernanceQueue).toHaveBeenCalledTimes(1)
    // The source input is DELETED, not defaulted: no text box on the resting screen, nothing
    // labelled "source" to fill in, no load button to press.
    expect(screen.queryByRole('textbox')).toBeNull()
    expect(screen.queryByLabelText(/^source$/i)).toBeNull()
    expect(screen.queryByRole('button', { name: /load proposals/i })).toBeNull()
  })

  it('opens with the purpose line: the system proposes, a person disposes', async () => {
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen />)
    await screen.findByTestId(`row-${FULL.items[0].fact_key}`)
    const purpose = screen.getByTestId('gq-purpose')
    expect(purpose).toHaveTextContent(/only a person can make/i)
    expect(purpose).toHaveTextContent(/proposes/i)
    expect(purpose).toHaveTextContent(/dispose/i)
    // Confirmation means agreement with a semantic relationship, never permission to execute.
    expect(purpose).toHaveTextContent(/agrees/i)
  })

  it('answers "what needs me" in a summary strip before any detail', async () => {
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen />)
    const summary = await screen.findByTestId('gq-summary')
    expect(summary).toHaveTextContent('3')
    expect(summary).toHaveTextContent(/waiting for a person/i)
    // Scope-honest: the counts describe what THIS operator can see, never a catalog total.
    expect(screen.getByTestId('gq-scope-note'))
      .toHaveTextContent(/scope-relative|what you can see/i)
  })

  it('builds the filter chips from the payload — never from a hardcoded slug list', async () => {
    // Slugs and a kind the client has never heard of. A hardcoded cib/ftr chip list fails here.
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge({ catalogs: ['zeta', 'omega'] }),
              bridge({ kind: 'weird_fact', fact_key: 'fact:weird:1', catalogs: ['zeta'],
                       already_depended_on_by: [] })],
      catalogs: ['zeta', 'omega'],
      items_visible_to_you_by_catalog: { zeta: 2, omega: 1 },
      items_visible_to_you_by_kind: { entity_bridge: 1, weird_fact: 1 },
    }))
    render(<GovernanceReviewScreen />)
    const catalogs = within(await screen.findByTestId('gq-catalog-filter'))
    expect(catalogs.getByRole('button', { name: /zeta/i })).toBeInTheDocument()
    expect(catalogs.getByRole('button', { name: /omega/i })).toBeInTheDocument()
    expect(catalogs.queryByRole('button', { name: /cib/i })).toBeNull()
    expect(catalogs.queryByRole('button', { name: /ftr/i })).toBeNull()
    // An unknown kind still gets a chip and a readable label, rather than breaking the client.
    const kinds = within(screen.getByTestId('gq-kind-filter'))
    expect(kinds.getByRole('button', { name: /weird fact/i })).toBeInTheDocument()
  })

  it('narrows the list by catalog chip without ever blanking the screen', async () => {
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen />)
    await screen.findByTestId(`row-${FULL.items[1].fact_key}`)
    await userEvent.click(
      within(screen.getByTestId('gq-catalog-filter')).getByRole('button', { name: /ftr/i }))
    // The bridge (cib+ftr) and the ftr grain fact stay; the cib-only join leaves.
    expect(row(FULL.items[0])).toBeInTheDocument()
    expect(row(FULL.items[2])).toBeInTheDocument()
    expect(screen.queryByTestId(`row-${FULL.items[1].fact_key}`)).toBeNull()
    // Refiltering is client-side over the one fetch — the screen never re-asks for a slug.
    expect(getGovernanceQueue).toHaveBeenCalledTimes(1)
  })

  it('adopts a dashboard handoff source as a preselected filter, with no typing', async () => {
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen initialSource="ftr" />)
    await screen.findByTestId(`row-${FULL.items[0].fact_key}`)
    expect(screen.queryByTestId(`row-${FULL.items[1].fact_key}`)).toBeNull()
    expect(within(screen.getByTestId('gq-catalog-filter'))
      .getByRole('button', { name: /ftr/i })).toHaveAttribute('aria-pressed', 'true')
  })

  it('falls back to the whole queue when the handoff source is not a visible catalog',
    async () => {
      getGovernanceQueue.mockResolvedValue(FULL)
      render(<GovernanceReviewScreen initialSource="not-a-catalog" />)
      expect(await screen.findByTestId(`row-${FULL.items[0].fact_key}`)).toBeInTheDocument()
      expect(row(FULL.items[1])).toBeInTheDocument()
    })
})

describe('governance review — the two axes stay separate', () => {
  it('renders human review and automatic execution safety as two distinct signals', async () => {
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen />)
    const bridgeRow = within(await screen.findByTestId(`row-${FULL.items[0].fact_key}`))
    const review = bridgeRow.getByTestId('axis-review')
    const execution = bridgeRow.getByTestId('axis-execution')

    expect(review).not.toBe(execution)
    expect(review.contains(execution)).toBe(false)
    expect(execution.contains(review)).toBe(false)
    // Each signal is bound to its OWN code field — the contract, not the prose.
    expect(review).toHaveAttribute('data-code', 'unreviewed_available')
    expect(execution).toHaveAttribute('data-code', 'grain_resolved')
    expect(review).toHaveTextContent(/human review/i)
    expect(review).toHaveTextContent('Unreviewed — available for use')
    expect(execution).toHaveTextContent(/execution safety/i)
    expect(execution).toHaveTextContent('Automatically validated for production')
    // Neither axis leaks into the other's element.
    expect(review).not.toHaveTextContent(/validated for production/i)
    expect(execution).not.toHaveTextContent(/unreviewed/i)
  })

  it('renders unreviewed-and-eligible and endorsed-and-sandbox-only side by side', async () => {
    // The pair no single fused badge can express. If review and eligibility shared one code, one
    // colour or one sort key, one of these two rows would have to be rendered as a lie.
    const endorsedSandbox = bridge({
      fact_key: 'fact:entity_bridge:endorsed',
      // A DIFFERENT entity, so this is a second finding rather than another candidate for the
      // same crossing (those group — see the candidate-cluster tests).
      detail: bridgeDetail({ entity_id: 'account' }),
      state: 'Human endorsed', state_code: 'human_endorsed',
      production_eligibility: 'Cardinality unresolved — sandbox only',
      production_eligibility_code: 'cardinality_unresolved',
      available_actions: [],
    })
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge(), endorsedSandbox],
      items_visible_to_you_by_kind: { entity_bridge: 2, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const unreviewed = within(await screen.findByTestId('row-fact:entity_bridge:customer'))
    const endorsed = within(screen.getByTestId('row-fact:entity_bridge:endorsed'))

    expect(unreviewed.getByTestId('axis-review'))
      .toHaveAttribute('data-code', 'unreviewed_available')
    expect(unreviewed.getByTestId('axis-execution')).toHaveAttribute('data-code', 'grain_resolved')
    expect(endorsed.getByTestId('axis-review')).toHaveAttribute('data-code', 'human_endorsed')
    expect(endorsed.getByTestId('axis-execution'))
      .toHaveAttribute('data-code', 'cardinality_unresolved')
  })

  it('reports the automatic axis honestly when the payload has nothing to derive it from',
    async () => {
      getGovernanceQueue.mockResolvedValue(queue({
        items: [grain()], items_visible_to_you_by_kind: { grain: 1, approved_join: 0 },
      }))
      render(<GovernanceReviewScreen />)
      const execution = within(await screen.findByTestId(`row-${grain().fact_key}`))
        .getByTestId('axis-execution')
      expect(execution).toHaveAttribute('data-code', 'not_applicable')
      expect(execution).toHaveTextContent(/not applicable/i)
      // Never invented as a pass or a fail.
      expect(execution).not.toHaveTextContent(/validated for production/i)
      expect(execution).not.toHaveTextContent(/sandbox only/i)
    })
})

describe('governance review — available_actions drives the buttons', () => {
  it('renders confirm disabled, with the server reason, when the action is withheld', async () => {
    const withheld = bridge({
      fact_key: 'fact:entity_bridge:mine', available_actions: ['reject'],
      // A different entity from the control row below, so both render as their own findings.
      detail: bridgeDetail({ entity_id: 'account', proposed_by: 'alice@bank' }),
    })
    getGovernanceQueue.mockResolvedValue(queue({
      items: [withheld, bridge()],
      items_visible_to_you_by_kind: { entity_bridge: 2, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const blocked = within(await screen.findByTestId('row-fact:entity_bridge:mine'))
    const confirmButton = blocked.getByRole('button', { name: /^confirm/i })
    expect(confirmButton).toBeDisabled()
    // The REASON is on screen, not just an inert grey button.
    const why = blocked.getByTestId('gq-action-why')
    expect(why).toHaveTextContent(/proposer/i)
    expect(why).toHaveTextContent('alice@bank')
    expect(confirmButton).toHaveAttribute('aria-describedby', why.id)
    // Reject is still offered, because the server still offers it.
    expect(blocked.getByRole('button', { name: /^reject/i })).toBeEnabled()
    // And a row the server DOES offer confirm on is enabled — the difference comes from the
    // payload, never from anything computed here.
    expect(within(screen.getByTestId('row-fact:entity_bridge:customer'))
      .getByRole('button', { name: /^confirm/i })).toBeEnabled()
  })

  it('offers no action at all on an endorsed row, and says why', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge({ state: 'Human endorsed', state_code: 'human_endorsed',
                       available_actions: [] })],
      items_visible_to_you_by_kind: { entity_bridge: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const endorsed = within(await screen.findByTestId('row-fact:entity_bridge:customer'))
    expect(endorsed.getByRole('button', { name: /^confirm/i })).toBeDisabled()
    expect(endorsed.getByTestId('gq-action-why')).toHaveTextContent(/nothing left to decide/i)
    expect(endorsed.queryByRole('button', { name: /^reject/i })).toBeNull()
  })

  it('confirms a bridge behind an explicit agreement, then refetches the queue', async () => {
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen />)
    const bridgeRow = within(await screen.findByTestId(`row-${FULL.items[0].fact_key}`))
    await userEvent.click(bridgeRow.getByRole('button', { name: /^confirm/i }))
    // The agreement is the confirmation's MEANING, so it is stated and ticked, not implied.
    const record = bridgeRow.getByRole('button', { name: /record my confirmation/i })
    expect(record).toBeDisabled()
    await userEvent.click(bridgeRow.getByRole('checkbox'))
    expect(record).toBeEnabled()
    await userEvent.click(record)
    await waitFor(() => expect(confirmEntityBridge)
      .toHaveBeenCalledWith(FULL.items[0].fact_key, {}))
    await waitFor(() => expect(getGovernanceQueue).toHaveBeenCalledTimes(2))
    expect(await screen.findByTestId('gq-notice')).toBeInTheDocument()
  })

  it('routes each kind to its own command and its own reject vocabulary', async () => {
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen />)
    // A join confirm goes to the join command...
    const joinRow = within(await screen.findByTestId(`row-${FULL.items[1].fact_key}`))
    await userEvent.click(joinRow.getByRole('button', { name: /^confirm/i }))
    await userEvent.click(joinRow.getByRole('checkbox'))
    await userEvent.click(joinRow.getByRole('button', { name: /record my confirmation/i }))
    await waitFor(() => expect(confirmJoin).toHaveBeenCalledWith(FULL.items[1].fact_key, {}))
    expect(confirmEntityBridge).not.toHaveBeenCalled()
    expect(confirmTableFact).not.toHaveBeenCalled()
    // ...and a bridge reject offers the BRIDGE vocabulary, not the join's.
    const bridgeRow = within(screen.getByTestId(`row-${FULL.items[0].fact_key}`))
    await userEvent.click(bridgeRow.getByRole('button', { name: /^reject/i }))
    expect(bridgeRow.getByRole('button', { name: /not the same entity/i })).toBeInTheDocument()
    expect(bridgeRow.queryByRole('button', { name: /wrong cardinality/i })).toBeNull()
    const record = bridgeRow.getByRole('button', { name: /record my rejection/i })
    expect(record).toBeDisabled()
    await userEvent.click(bridgeRow.getByRole('button', { name: /not the same entity/i }))
    await userEvent.click(record)
    await waitFor(() => expect(rejectEntityBridge).toHaveBeenCalledWith(
      FULL.items[0].fact_key, { category: 'not_the_same_entity' }))
  })
})

describe('governance review — the low-value candidate cluster', () => {
  it('renders the eight branch candidates as ONE group, not eight findings', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: BRANCH,
      items_visible_to_you_by_kind: { entity_bridge: 8, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const section = within(await screen.findByTestId('kind-entity_bridge'))
    // One entry in the list, not eight.
    expect(section.getAllByTestId('queue-entry')).toHaveLength(1)
    const group = section.getByTestId('queue-entry')
    expect(group).toHaveTextContent(/8 candidate links/i)
    expect(group).toHaveTextContent(/branch/i)
    expect(group).toHaveTextContent(/same two facts/i)
    // Collapsed by default: eight rows would BE eight findings on screen.
    expect(within(group).queryAllByTestId(/^row-fact:entity_bridge:branch:/)).toHaveLength(0)
    // Grouped, not hidden — the individual candidates stay reachable in one click.
    await userEvent.click(within(group).getByRole('button', { name: /show the 8 candidates/i }))
    expect(within(group).getByRole('group', { name: /candidate/i })).toBeInTheDocument()
    expect(within(group).getAllByTestId(/^row-fact:entity_bridge:branch:/)).toHaveLength(8)
  })

  it('settles the whole group in one reviewer action, with every fact_key in one call',
    async () => {
      getGovernanceQueue.mockResolvedValue(queue({
        items: BRANCH,
        items_visible_to_you_by_kind: { entity_bridge: 8, approved_join: 0 },
      }))
      render(<GovernanceReviewScreen />)
      const group = within(await screen.findByTestId('queue-entry'))
      await userEvent.click(group.getByRole('button', { name: /reject the whole group/i }))
      const record = group.getByRole('button', { name: /reject all 8 candidates/i })
      expect(record).toBeDisabled()
      await userEvent.click(group.getByRole('button', { name: /not the same entity/i }))
      await userEvent.click(record)
      await waitFor(() => expect(bulkRejectEntityBridges).toHaveBeenCalledWith(
        BRANCH.map(b => b.fact_key), 'not_the_same_entity', undefined))
      // Partial outcomes are the ordinary case, so the split is reported back.
      expect(await screen.findByTestId('gq-notice')).toHaveTextContent(/8/)
    })

  it('keeps a lone candidate an ordinary row, never a group of one', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge()], items_visible_to_you_by_kind: { entity_bridge: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    await screen.findByTestId(`row-${bridge().fact_key}`)
    expect(screen.queryByRole('button', { name: /reject the whole group/i })).toBeNull()
    expect(screen.queryByText(/candidate links for the same/i)).toBeNull()
  })
})

describe('governance review — honest emptiness and honest counts', () => {
  it('reads an empty kind as settled, not as a failure', async () => {
    getGovernanceQueue.mockResolvedValue(FULL) // availability_time is 0
    render(<GovernanceReviewScreen />)
    const settled = within(await screen.findByTestId('kind-availability_time'))
    expect(settled.getByRole('status')).toHaveTextContent(/nothing to review/i)
    // And it says WHY there is nothing, rather than showing a bare "(0)".
    expect(settled.getByRole('status')).toHaveTextContent(/no .* is waiting/i)
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('distinguishes "nothing is waiting" from "we could not look"', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge()],
      items_visible_to_you_by_kind: { entity_bridge: 1, approved_join: 0 },
      unreadable: [{ listing: 'approved_join', source: 'cib',
                     reason: 'the join listing could not be read' }],
      complete: false,
    }))
    render(<GovernanceReviewScreen />)
    const joins = within(await screen.findByTestId('kind-approved_join'))
    expect(joins.getByText(/could not be read/i)).toBeInTheDocument()
    expect(joins.queryByText(/nothing to review/i)).toBeNull()
    expect(screen.getByTestId('gq-incomplete')).toHaveTextContent(/could not be read/i)
  })

  it('renders a real 0 as 0 and an unmeasurable category as "not tracked yet"', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge()], items_visible_to_you_by_kind: { entity_bridge: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const usage = within(within(await screen.findByTestId(`row-${bridge().fact_key}`))
      .getByTestId('usage'))
    // A measured zero IS a number: the store holds observations and none names this bridge.
    expect(usage.getByTestId('usage-value-planned_candidates')).toHaveTextContent(/^0$/)
    expect(usage.getByTestId('usage-value-selected_plans')).toHaveTextContent(/^3$/)
    // An unmeasurable category is the WORDS, never 0 and never blank.
    const untracked = usage.getByTestId('usage-value-generated_artifacts')
    expect(untracked).toHaveTextContent('not tracked yet')
    expect(untracked.textContent).not.toMatch(/\d/)
    const analyses = usage.getByTestId('usage-value-data_agent_analyses')
    expect(analyses).toHaveTextContent('not tracked yet')
    expect(analyses.textContent).not.toMatch(/\d/)
    // The honest limits are surfaced, not swallowed: why analyses cannot be counted, and that
    // published_features is feature-registration lineage rather than physical publication.
    expect(usage.getByText(/never persisted/i)).toBeInTheDocument()
    expect(usage.getByText(/current governed contract/i)).toBeInTheDocument()
  })

  it('renders no usage block for a kind with no bridge anchor', async () => {
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen />)
    const joinRow = within(await screen.findByTestId(`row-${FULL.items[1].fact_key}`))
    // An empty already_depended_on_by is an ABSENCE of an anchor, never "0 dependencies".
    expect(joinRow.queryByTestId('usage')).toBeNull()
    expect(joinRow.queryByText(/already depended on by/i)).toBeNull()
  })

  it('never presents a discovered join as cross-catalog', async () => {
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen />)
    const joinRow = await screen.findByTestId(`row-${FULL.items[1].fact_key}`)
    expect(joinRow.textContent ?? '').not.toMatch(/cross-catalog/i)
    // Exactly the one catalog the listing is keyed by.
    expect(within(joinRow).getAllByTestId('gq-catalog')).toHaveLength(1)
    expect(within(joinRow).getByTestId('gq-catalog')).toHaveTextContent('CIB')
    // The bridge, by contrast, is the cross-catalog decision this surface exists for.
    expect(row(FULL.items[0]).textContent ?? '').toMatch(/cross-catalog/i)
  })

  it('surfaces a load failure as the server said it, not as an empty queue', async () => {
    getGovernanceQueue.mockRejectedValue(new api.ApiError(403, 'platform-admin required'))
    render(<GovernanceReviewScreen />)
    expect(await screen.findByRole('alert')).toHaveTextContent('platform-admin required')
    expect(screen.queryByText(/nothing to review/i)).toBeNull()
  })
})

describe('governance review — the vocabulary that must never appear', () => {
  // Every one of these asserts that human review gates use, which is false in this product:
  // review is accountability, availability is automatic. Mirrors
  // overlay/upload/governance_queue.FORBIDDEN_PHRASES with the counted variants generalized — a
  // computed "Blocks 8 features" is the same lie as the literal.
  const FORBIDDEN = [
    /blocks\s+(n|\d+)\s+features?/i,
    /approve to enable/i,
    /waiting to become usable/i,
    /production approval required/i,
    // The same framing in the two forms this screen could regress into.
    /approve to unblock/i,
    /blocking \d+/i,
  ]

  it('renders no forbidden phrase anywhere in the output, attributes included', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      // Everything at once: the group, an endorsed row, a withheld confirm, usage, an empty kind,
      // an unreadable listing and a truncated page.
      items: [...BRANCH, bridge(), join(), grain(),
              bridge({ fact_key: 'fact:entity_bridge:e', state: 'Human endorsed',
                       state_code: 'human_endorsed', available_actions: [],
                       detail: bridgeDetail({ entity_id: 'account' }) }),
              bridge({ fact_key: 'fact:entity_bridge:w', available_actions: ['reject'],
                       detail: bridgeDetail({ entity_id: 'product' }) })],
      items_visible_to_you_by_kind: {
        entity_bridge: 11, approved_join: 1, grain: 1, availability_time: 0,
      },
      unreadable: [{ listing: 'table_fact', source: 'ftr', reason: 'unreadable' }],
      complete: false,
      truncated: true,
    }))
    const { container } = render(<GovernanceReviewScreen />)
    await screen.findByTestId(`row-${bridge().fact_key}`)
    // innerHTML, so labels, titles and aria-* are scanned too — not just visible text.
    for (const phrase of FORBIDDEN) {
      expect(container.innerHTML).not.toMatch(phrase)
    }
  })

  // The rendered scan cannot see a comment, and a comment asserting that review gates use would
  // teach the next editor the wrong model. Throws (loudly) rather than silently passing if the
  // file moves — a scan that quietly reads nothing would prove nothing.
  function screenSource(): string {
    const candidates = [
      'src/screens/GovernanceReviewScreen.tsx',
      'frontend/src/screens/GovernanceReviewScreen.tsx',
    ]
    for (const candidate of candidates) {
      try {
        return readFileSync(joinPath(process.cwd(), candidate), 'utf8')
      } catch {
        // try the next root
      }
    }
    throw new Error(`could not read the screen source from ${process.cwd()}`)
  }

  it('carries no forbidden phrase in the source either, comments included', () => {
    const source = screenSource()
    expect(source).toContain('GOVERNANCE REVIEW')
    for (const phrase of FORBIDDEN) {
      expect(source).not.toMatch(phrase)
    }
  })
})
