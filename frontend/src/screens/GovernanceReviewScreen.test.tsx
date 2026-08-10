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
    confirmSemanticBinding: vi.fn(),
    rejectSemanticBinding: vi.fn(),
    reviewBridgeRealization: vi.fn(),
    getDataUsePolicies: vi.fn(),
  }
})
const getGovernanceQueue = vi.mocked(api.getGovernanceQueue)
const confirmEntityBridge = vi.mocked(api.confirmEntityBridge)
const rejectEntityBridge = vi.mocked(api.rejectEntityBridge)
const bulkRejectEntityBridges = vi.mocked(api.bulkRejectEntityBridges)
const reviewBridgeRealization = vi.mocked(api.reviewBridgeRealization)
const confirmJoin = vi.mocked(api.confirmJoin)
const confirmTableFact = vi.mocked(api.confirmTableFact)
const confirmSemanticBinding = vi.mocked(api.confirmSemanticBinding)
const rejectSemanticBinding = vi.mocked(api.rejectSemanticBinding)
const getDataUsePolicies = vi.mocked(api.getDataUsePolicies)

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

function realization(): api.BridgeRealizationView {
  return {
    realization_id: 'real-customer',
    realization_revision_id: 'real-rev-2',
    bridge_fact_key: 'fact:entity_bridge:customer',
    direction: { from: 'cib::public.customer_master_dly', to: 'ftr::public.party_dly' },
    from_endpoint: {},
    to_endpoint: {},
    column_pairs: [{
      from_logical_column_ref: 'cib::public.customer_master_dly.cif_id',
      to_logical_column_ref: 'ftr::public.party_dly.cif_id',
    }],
    cardinality: 'unknown',
    cardinality_label: 'Unknown — profile required',
    cardinality_basis: 'none',
    predicates: [],
    missing_requirements: [{
      from_logical_column_ref: 'cib::public.customer_master_dly.business_dt',
      to_logical_column_ref: 'ftr::public.party_dly.business_dt',
      reason_code: 'composite_key_member_missing',
    }],
    applicability_scope: {
      scope_id: 'customer-daily',
      execution_tier: 'production',
      purposes: ['feature_generation'],
      environment: 'pilot',
      partition_scope_ref: null,
    },
    dependency_snapshot_id: 'deps-1',
    safety_status: 'unassessed',
    review_status: 'unreviewed',
    lifecycle: 'active',
    pointer_version: 1,
    execution_eligible: false,
    execution_reason_codes: ['directional_cardinality_unknown'],
    evidence_fresh: true,
    evidence: [],
    metrics: [],
    assessment: null,
    available_review_actions: ['confirm', 'reject'],
    profile_action: {
      state: 'external_run_required',
      label: 'Run bounded profile in the data environment',
    },
    review_controls_execution: false,
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
      // The ONLY origin the backend emits: `table_fact_governance._ORIGIN`. This fixture used to
      // say `llm_enrichment`, a plausible-looking string no code path produces — which is exactly
      // what hid the "Proposed by llm_proposed_not_profiled" rendering from these tests.
      origin: 'llm_proposed_not_profiled', advisory: {}, task_id: 'tf-1', target_event_id: 'ev-2',
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
    governance_status: 'VERIFIED', review_projection: 'projected',
    review_controls_availability: false, review_controls_execution: false,
  })
  rejectEntityBridge.mockReset()
  rejectEntityBridge.mockResolvedValue({
    governance_status: 'REJECTED', category: 'not_the_same_entity',
    review_projection: 'not_applicable',
    review_controls_availability: false, review_controls_execution: false,
  })
  reviewBridgeRealization.mockReset()
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

  it('shows assessment truth separately from the directional realization', async () => {
    const exact = realization()
    const item = bridge({
      production_eligibility: 'Unknown — profile required',
      production_eligibility_code: 'cardinality_unknown',
      detail: bridgeDetail({
        cardinality_label: exact.cardinality_label,
        authority: { role: 'platform-admin', confirmation_count: 1, dual: false },
        assessment: {
          namespace_verdict: 'possible',
          governed_population_relation: 'unknown',
          population_hypothesis: 'CIB customer IDs likely overlap the FTR transaction population',
          left_endpoint: { concept_authority: 'llm' },
          right_endpoint: { concept_authority: 'source' },
          proposal_reasons: ['same_customer_concept', 'compatible_type'],
          strongest_contradiction: 'CIB key is (business_dt, cif_id), not cif_id alone',
        },
        realizations: [exact],
      }),
    })
    getGovernanceQueue.mockResolvedValue(queue({ items: [item] }))
    render(<GovernanceReviewScreen />)

    const card = await screen.findByTestId(`row-${item.fact_key}`)
    expect(within(card).getAllByText('Unknown — profile required')).toHaveLength(3)
    expect(within(card).getByText('possible')).toBeInTheDocument()
    expect(within(card).getByText('unknown')).toBeInTheDocument()
    expect(within(card).getByText('llm / source')).toBeInTheDocument()
    expect(within(card).getByText('platform-admin · 1 confirmer')).toBeInTheDocument()
    expect(within(card).getByText(/Advisory population hypothesis/)).toHaveTextContent(
      'CIB customer IDs likely overlap the FTR transaction population',
    )
    expect(within(card).getByText(/Strongest contradiction/)).toHaveTextContent(
      'CIB key is (business_dt, cif_id), not cif_id alone',
    )
    expect(within(card).getByText(/Next action/)).toHaveTextContent(
      'Run bounded profile in the data environment',
    )
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

describe('governance review — the basis of the claim being endorsed', () => {
  it('shows that a declared type match rests on two spreadsheets, not on the schema', async () => {
    // Every bridge on the live catalogs is `declared`: bridge_candidates._resolve_family falls back
    // to the glossary-declared type only when nothing was attested. Confirming that is a materially
    // different act, so the basis is on screen.
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge()], items_visible_to_you_by_kind: { entity_bridge: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const cells = within(await screen.findByTestId(`row-${bridge().fact_key}`))
    const basis = cells.getByTestId('gq-basis')
    expect(basis).toHaveAttribute('data-type-basis', 'declared')
    const type = within(basis).getByTestId('basis-type')
    // The words, not just the code: a reviewer must be able to read what `declared` means.
    expect(type).toHaveTextContent(/spreadsheets/i)
    expect(type).toHaveTextContent(/nothing read the physical schema/i)
    // The other two fields the payload carried and the screen used to swallow.
    expect(within(basis).getByTestId('basis-family')).toHaveTextContent('string')
    expect(within(basis).getByTestId('basis-strength')).toHaveTextContent('3')
    // A ranking score is not a probability, and it is not a claim about the values.
    expect(within(basis).getByTestId('basis-strength')).toHaveTextContent(/not a probability/i)
  })

  it('repeats the declared basis where the agreement is actually ticked', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge()], items_visible_to_you_by_kind: { entity_bridge: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const cells = within(await screen.findByTestId(`row-${bridge().fact_key}`))
    await userEvent.click(cells.getByRole('button', { name: /^confirm/i }))
    const caution = cells.getByTestId('gq-confirm-basis')
    expect(caution).toHaveTextContent(/two glossary spreadsheets/i)
    expect(caution).toHaveTextContent(/nothing read the physical schema/i)
    // Beside the statement being agreed to, not somewhere else on the page.
    expect(cells.getByRole('checkbox').closest('.gq-panel')).toContainElement(caution)
  })

  it('states an unstated cardinality honestly in the join agreement, never as fact', async () => {
    // Task 5 codegen-review remediation (M3): a blank uploaded cardinality is proposed as null
    // now, and this sentence is the agreement the confirmation records. The old fallback
    // ("at the stated cardinality") asserted a cardinality that does not exist.
    getGovernanceQueue.mockResolvedValue(queue({
      items: [join({ detail: { ...join().detail, cardinality: null } })],
      items_visible_to_you_by_kind: { entity_bridge: 0, approved_join: 1 },
    }))
    render(<GovernanceReviewScreen />)
    const cells = within(await screen.findByTestId(`row-${join().fact_key}`))
    await userEvent.click(cells.getByRole('button', { name: /^confirm/i }))
    const label = cells.getByRole('checkbox').closest('label')!
    expect(label).toHaveTextContent(/at an unstated cardinality/i)
    expect(label).toHaveTextContent(/stays unusable until one is supplied/i)
    expect(label).not.toHaveTextContent(/the stated cardinality/i)
  })

  it('reports an attested basis differently, and a missing one as missing', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [
        bridge({ fact_key: 'fact:entity_bridge:attested',
                 detail: bridgeDetail({ entity_id: 'account', type_basis: 'attested' }) }),
        // W4: bridge_propose skipped the ledger row, so there is no derivation evidence at all.
        bridge({ fact_key: 'fact:entity_bridge:blank',
                 detail: bridgeDetail({ entity_id: 'product', type_basis: '', data_type_family: '',
                                        strength: null, evidence_present: false }) }),
      ],
      items_visible_to_you_by_kind: { entity_bridge: 2, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const attested = within(await screen.findByTestId('row-fact:entity_bridge:attested'))
    expect(attested.getByTestId('gq-basis')).toHaveAttribute('data-type-basis', 'attested')
    expect(attested.getByTestId('basis-type')).toHaveTextContent(/read from the data/i)
    expect(attested.getByTestId('basis-type')).not.toHaveTextContent(/spreadsheet/i)
    // An attested basis carries no caveat, so nothing is manufactured next to the agreement.
    await userEvent.click(attested.getByRole('button', { name: /^confirm/i }))
    expect(attested.queryByTestId('gq-confirm-basis')).toBeNull()

    const blank = within(screen.getByTestId('row-fact:entity_bridge:blank'))
    const basis = blank.getByTestId('gq-basis')
    expect(basis).toHaveAttribute('data-type-basis', 'not_recorded')
    expect(within(basis).getByTestId('basis-type')).toHaveTextContent(/not recorded/i)
    expect(within(basis).getByTestId('basis-family')).toHaveTextContent(/not recorded/i)
    expect(within(basis).getByTestId('basis-strength')).toHaveTextContent(/not recorded/i)
    // Never dressed up as a weak pass: the type cell claims no basis it does not have.
    expect(within(basis).getByTestId('basis-type')).not.toHaveTextContent(/read from the data/i)
    expect(within(basis).getByTestId('basis-type')).not.toHaveTextContent(/spreadsheet/i)
  })

  it('renders the join side-tasks and the table advisory, and neither on the wrong kind',
    async () => {
      getGovernanceQueue.mockResolvedValue(queue({
        items: [bridge(), join(), grain({
          detail: { ...grain().detail,
                    advisory: { table_role: 'dimension', primary_entity: 'party',
                                event_or_snapshot: 'snapshot' } },
        })],
        items_visible_to_you_by_kind: {
          entity_bridge: 1, approved_join: 1, grain: 1, availability_time: 0,
        },
      }))
      render(<GovernanceReviewScreen />)
      const joinRow = within(await screen.findByTestId(`row-${join().fact_key}`))
      const tasks = joinRow.getByTestId('gq-tasks')
      expect(tasks).toHaveTextContent(/from side/i)
      expect(tasks).toHaveTextContent(/open/i)
      expect(tasks).toHaveTextContent('t1')

      const grainRow = within(screen.getByTestId(`row-${grain().fact_key}`))
      const advisory = grainRow.getByTestId('gq-advisory')
      expect(advisory).toHaveTextContent('dimension')
      expect(advisory).toHaveTextContent('party')
      // Advisory is context, never part of the claim — and it says so.
      expect(advisory).toHaveTextContent(/not part of what you are agreeing to/i)

      // A kind whose payload carries none of these renders no empty shell.
      const bridgeRow = within(screen.getByTestId(`row-${bridge().fact_key}`))
      expect(bridgeRow.queryByTestId('gq-tasks')).toBeNull()
      expect(bridgeRow.queryByTestId('gq-advisory')).toBeNull()
      expect(joinRow.queryByTestId('gq-basis')).toBeNull()
      // The grain fixture with an EMPTY advisory map must render nothing either.
      expect(grainRow.queryByTestId('gq-tasks')).toBeNull()
    })

  it('renders no advisory block when the enrichment recorded nothing', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [grain()], items_visible_to_you_by_kind: { grain: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const grainRow = within(await screen.findByTestId(`row-${grain().fact_key}`))
    expect(grainRow.queryByTestId('gq-advisory')).toBeNull()
  })
})

describe('governance review — provenance is provenance, never a person', () => {
  it('reads a table fact origin as a method, not as its proposer', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [grain()], items_visible_to_you_by_kind: { grain: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const grainRow = await screen.findByTestId(`row-${grain().fact_key}`)
    // A table fact carries no proposer at all, and `origin` is a constant describing HOW it was
    // proposed. Naming it as the proposer invented a person called llm_proposed_not_profiled.
    expect(grainRow).not.toHaveTextContent(/proposed by llm/i)
    expect(grainRow).not.toHaveTextContent('llm_proposed_not_profiled')
    expect(grainRow).toHaveTextContent(/proposed automatically/i)
    // And the honest limit of the method travels with it: schema-read, never profiled.
    expect(grainRow).toHaveTextContent(/not profiled against the data/i)
  })

  it('still names a real proposer as the person they are', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge({ detail: bridgeDetail({ proposed_by: 'alice@bank' }) })],
      items_visible_to_you_by_kind: { entity_bridge: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    expect(await screen.findByTestId(`row-${bridge().fact_key}`))
      .toHaveTextContent(/proposed by alice@bank/i)
  })

  it('de-underscores an origin it has never seen rather than guessing at it', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [grain({ detail: { ...grain().detail, origin: 'some_future_origin' } })],
      items_visible_to_you_by_kind: { grain: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const grainRow = await screen.findByTestId(`row-${grain().fact_key}`)
    expect(grainRow).toHaveTextContent(/some future origin/i)
    expect(grainRow).not.toHaveTextContent(/proposed by some/i)
    // No claim about profiling is made for a method this client does not know.
    expect(grainRow).not.toHaveTextContent(/not profiled against the data/i)
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
    // And it is NOT counted as work: the bridge listing carries VERIFIED facts too, so a summary
    // built from the list length would claim a decision is waiting when none is.
    const summary = screen.getByTestId('gq-summary')
    expect(summary).toHaveTextContent(/0 waiting for a person/i)
    expect(summary).toHaveTextContent(/1 already endorsed/i)
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

  it('shows what settling the group would land on, not just the count of links', async () => {
    // The one place the consequence matters most: a reviewer settling eight at once never opens the
    // individual rows, so the usage that lives on them has to be on the card too.
    getGovernanceQueue.mockResolvedValue(queue({
      items: BRANCH, items_visible_to_you_by_kind: { entity_bridge: 8, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const group = await screen.findByTestId('queue-entry')
    // Collapsed, so this usage block is the CARD's own — not one leaking out of a member row.
    expect(within(group).queryAllByTestId(/^row-fact:entity_bridge:branch:/)).toHaveLength(0)
    const usage = within(within(group).getByTestId('usage'))
    // Summed across the eight members (3 selected plans each, 1 published feature each).
    expect(usage.getByTestId('usage-value-selected_plans')).toHaveTextContent(/^24$/)
    expect(usage.getByTestId('usage-value-published_features')).toHaveTextContent(/^8$/)
    // A real 0 stays 0 and an unmeasurable category stays words, exactly as on a single row.
    expect(usage.getByTestId('usage-value-planned_candidates')).toHaveTextContent(/^0$/)
    const untracked = usage.getByTestId('usage-value-generated_artifacts')
    expect(untracked).toHaveTextContent('not tracked yet')
    expect(untracked.textContent).not.toMatch(/\d/)
    // How it was aggregated is stated where it is read.
    expect(within(group).getByTestId('usage')).toHaveTextContent(/across all 8 links/i)
  })

  it('reports a category as words when one member of the group could not be measured', async () => {
    // A sum over the members that COULD be counted would understate the consequence of settling
    // the set, so the whole category falls back to the words.
    getGovernanceQueue.mockResolvedValue(queue({
      items: BRANCH.map((item, i) => (i === 0
        ? { ...item,
            already_depended_on_by: item.already_depended_on_by.map(usage =>
              (usage.category === 'selected_plans'
                ? { ...usage, state: 'unreadable', count: null, display: 'unreadable' }
                : usage)) }
        : item)),
      items_visible_to_you_by_kind: { entity_bridge: 8, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const usage = within(within(await screen.findByTestId('queue-entry')).getByTestId('usage'))
    const plans = usage.getByTestId('usage-value-selected_plans')
    expect(plans).toHaveTextContent('unreadable')
    expect(plans.textContent).not.toMatch(/\d/)
    // The categories that WERE fully measured are unaffected.
    expect(usage.getByTestId('usage-value-published_features')).toHaveTextContent(/^8$/)
  })

  it('offers the group reject only over the members the server sanctions it on', async () => {
    // The ordinary end state after confirming one member of a cross-product: two endorsed bridges
    // sit in the same entity/catalog bucket as the open ones, and `_ACTIONS_VERIFIED = ()`.
    const mixed = BRANCH.map((item, i) => (i < 2
      ? { ...item, state: 'Human endorsed', state_code: 'human_endorsed', available_actions: [] }
      : item))
    getGovernanceQueue.mockResolvedValue(queue({
      items: mixed, items_visible_to_you_by_kind: { entity_bridge: 8, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const group = within(await screen.findByTestId('queue-entry'))
    // Still ONE card — they are still the same cross-product.
    expect(screen.getAllByTestId('queue-entry')).toHaveLength(1)
    // The card no longer claims to settle the whole group, and says what it will leave alone.
    expect(group.queryByRole('button', { name: /reject the whole group/i })).toBeNull()
    expect(group.getByTestId('gq-group-settled')).toHaveTextContent(/2 of these 8 are already/i)
    await userEvent.click(group.getByRole('button', { name: /reject the 6 still open/i }))
    expect(group.getByText(/not sent at all/i)).toBeInTheDocument()
    await userEvent.click(group.getByRole('button', { name: /not the same entity/i }))
    await userEvent.click(group.getByRole('button', { name: /^reject the 6 still open$/i }))
    // ONLY the six the server sanctions are sent — never the two it would refuse.
    await waitFor(() => expect(bulkRejectEntityBridges).toHaveBeenCalledWith(
      mixed.slice(2).map(b => b.fact_key), 'not_the_same_entity', undefined))
    // And the card and its members agree: an endorsed member offers nothing inside either.
    await userEvent.click(group.getByRole('button', { name: /show the 8 candidates/i }))
    const endorsed = within(screen.getByTestId(`row-${mixed[0].fact_key}`))
    expect(endorsed.queryByRole('button', { name: /^reject/i })).toBeNull()
  })

  it('offers no group action at all when every member is already endorsed', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: BRANCH.map(item => ({ ...item, state: 'Human endorsed',
                                   state_code: 'human_endorsed', available_actions: [] })),
      items_visible_to_you_by_kind: { entity_bridge: 8, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const group = within(await screen.findByTestId('queue-entry'))
    // The card must not offer a reject every one of its members would deny.
    expect(group.queryByRole('button', { name: /reject/i })).toBeNull()
    expect(group.getByTestId('gq-group-why')).toHaveTextContent(/already endorsed every one/i)
    expect(group.getByTestId('gq-group-why')).toHaveTextContent(/own flow/i)
    // The candidates are still reachable — hiding them would be a different lie.
    expect(group.getByRole('button', { name: /show the 8 candidates/i })).toBeInTheDocument()
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
    // A GENUINE failure. This case used to be written with a 403, which is not a failure at all:
    // the queue route is gated on the platform-admin claim and nothing gates the navigation to it,
    // so a 403 is the ordinary answer for three of the five roles — see the refusal test below.
    getGovernanceQueue.mockRejectedValue(new api.ApiError(500, 'the queue could not be built'))
    render(<GovernanceReviewScreen />)
    expect(await screen.findByRole('alert')).toHaveTextContent('the queue could not be built')
    expect(screen.queryByText(/nothing to review/i)).toBeNull()
  })
})

describe('governance review — a refusal is not a breakage', () => {
  // App.tsx gates the nav on nothing and LineageView links here in prose, so a catalog_viewer,
  // data_owner or feature_engineer clicking "Governance" is an ordinary visit the server declines.
  it('reads a 403 as an explanation, never as an error page', async () => {
    getGovernanceQueue.mockRejectedValue(
      new api.ApiError(403, 'requires the platform-admin role'))
    render(<GovernanceReviewScreen />)
    const explained = await screen.findByTestId('gq-not-yours')
    // Calm, not alarming: nothing on this page claims something went wrong.
    expect(screen.queryByRole('alert')).toBeNull()
    expect(explained).toHaveTextContent(/not open to your role/i)
    // The server's own sentence, quoted rather than paraphrased into a rule this client invented.
    expect(explained).toHaveTextContent('requires the platform-admin role')
    // And the true consequence: nothing is broken, and nothing is waiting on this visitor.
    expect(explained).toHaveTextContent(/nothing is wrong/i)
    expect(explained).toHaveTextContent(/platform-admin role/i)
    // Never presented as an empty queue either — that would say "everything is settled".
    expect(screen.queryByText(/nothing to review/i)).toBeNull()
  })

  it('keeps a 401 an error: a missing session is not a role answer', async () => {
    getGovernanceQueue.mockRejectedValue(new api.ApiError(401, 'not authenticated'))
    render(<GovernanceReviewScreen />)
    expect(await screen.findByRole('alert')).toHaveTextContent('not authenticated')
    expect(screen.queryByTestId('gq-not-yours')).toBeNull()
  })

  it('says nothing about roles when the queue loads', async () => {
    // The refusal state is reactive, not a prediction: a caller the server serves never sees it.
    getGovernanceQueue.mockResolvedValue(FULL)
    render(<GovernanceReviewScreen />)
    await screen.findByTestId(`row-${FULL.items[0].fact_key}`)
    expect(screen.queryByTestId('gq-not-yours')).toBeNull()
    expect(screen.queryByText(/platform-admin/i)).toBeNull()
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

// ── the accessibility properties that are actually assertable here ───────────────────────────────
//
// The CONTRAST of a tone is measured against its token and recorded in index.css; jsdom resolves no
// stylesheet, so an assertion about a colour here would be confidently untrue. What jsdom can prove
// is the property that matters more: the tone travels as an ATTRIBUTE, so it survives without colour
// vision — and the words are always there beside it.

describe('governance review — state travels as an attribute, never as colour alone', () => {
  // Every state code the read model emits, one row each, each a DIFFERENT entity so they render as
  // their own findings instead of collapsing into a candidate group.
  //   state_code, entity, expected availability tone, expected review tone
  const STATES: [string, string, string, string][] = [
    ['unreviewed_available', 'customer', 'ok', 'open'],
    ['partially_endorsed_available', 'account', 'ok', 'open'],
    ['human_endorsed', 'product', 'ok', 'ok'],
    ['stale_unavailable', 'branch', 'warn', 'warn'],
    ['rejected', 'merchant', 'off', 'off'],
  ]

  it('gives each state its own tone on each axis, not one tone per row', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: STATES.map(([code, entity]) => bridge({
        fact_key: `fact:entity_bridge:${entity}`,
        state_code: code,
        state: `state ${code}`,
        detail: bridgeDetail({ entity_id: entity }),
      })),
      items_visible_to_you_by_kind: { entity_bridge: STATES.length, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    await screen.findByTestId('row-fact:entity_bridge:customer')

    for (const [code, entity, availabilityTone, reviewTone] of STATES) {
      const cells = within(screen.getByTestId(`row-fact:entity_bridge:${entity}`))
      const availability = cells.getByTestId('axis-availability')
      const review = cells.getByTestId('axis-review')
      expect(availability).toHaveAttribute('data-tone', availabilityTone)
      expect(review).toHaveAttribute('data-tone', reviewTone)
      // The tone is redundant, never the carrier: the cell says it in words as well.
      expect(availability.textContent).toMatch(/\S/)
      expect(review).toHaveTextContent(`state ${code}`)
    }

    // The mapping is a mapping: a screen that tagged every row the same way would satisfy any
    // "has a data-tone" check and still tell a reader nothing.
    const reviewTones = screen.getAllByTestId('axis-review')
      .map(cell => cell.getAttribute('data-tone'))
    expect(new Set(reviewTones).size).toBe(4)
  })

  it('tones the two axes of ONE row independently', async () => {
    // The row this screen exists to render honestly: no person has looked at it (open) and the
    // machine has already validated it (ok). One tone per row could not say this.
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge()], items_visible_to_you_by_kind: { entity_bridge: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const cells = within(await screen.findByTestId(`row-${bridge().fact_key}`))
    expect(cells.getByTestId('axis-review')).toHaveAttribute('data-tone', 'open')
    expect(cells.getByTestId('axis-execution')).toHaveAttribute('data-tone', 'ok')
    expect(cells.getByTestId('axis-availability')).toHaveAttribute('data-tone', 'ok')
  })

  it('maps each execution code to its own tone, and an unknown code to the quiet one', async () => {
    //   production_eligibility_code, entity, expected tone
    const CODES: [string, string, string][] = [
      ['grain_resolved', 'customer', 'ok'],
      ['cardinality_unresolved', 'account', 'warn'],
      ['not_observed', 'product', 'quiet'],
      ['not_applicable', 'branch', 'quiet'],
      // A code from a newer backend must not be dressed up as a pass or as a failure.
      ['a_code_this_client_has_never_seen', 'merchant', 'quiet'],
    ]
    getGovernanceQueue.mockResolvedValue(queue({
      items: CODES.map(([code, entity]) => bridge({
        fact_key: `fact:entity_bridge:${entity}`,
        production_eligibility_code: code,
        production_eligibility: null,
        detail: bridgeDetail({ entity_id: entity }),
      })),
      items_visible_to_you_by_kind: { entity_bridge: CODES.length, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    await screen.findByTestId('row-fact:entity_bridge:customer')
    for (const [, entity, tone] of CODES) {
      expect(within(screen.getByTestId(`row-fact:entity_bridge:${entity}`))
        .getByTestId('axis-execution')).toHaveAttribute('data-tone', tone)
    }
    const tones = screen.getAllByTestId('axis-execution')
      .map(cell => cell.getAttribute('data-tone'))
    expect(new Set(tones).size).toBe(3)
  })

  it('says nothing about availability for a state code it cannot map', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [bridge({ state_code: 'invented_state', state: 'Something new' })],
      items_visible_to_you_by_kind: { entity_bridge: 1, approved_join: 0 },
    }))
    render(<GovernanceReviewScreen />)
    const cells = within(await screen.findByTestId(`row-${bridge().fact_key}`))
    // No guessed cell, and the human axis drops to the tone that signals nothing.
    expect(cells.queryByTestId('axis-availability')).toBeNull()
    expect(cells.getByTestId('axis-review')).toHaveAttribute('data-tone', 'quiet')
    expect(cells.getByTestId('axis-review')).toHaveTextContent('Something new')
  })

  it('is operable from the keyboard: every control has a name and takes focus', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [...BRANCH, bridge(), join(), grain(),
              bridge({ fact_key: 'fact:entity_bridge:w', available_actions: ['reject'],
                       detail: bridgeDetail({ entity_id: 'product',
                                              proposed_by: 'alice@bank' }) })],
      items_visible_to_you_by_catalog: { cib: 3, ftr: 10 },
      items_visible_to_you_by_kind: {
        entity_bridge: 10, approved_join: 1, grain: 1, availability_time: 0,
      },
      truncated: true,
    }))
    const { container } = render(<GovernanceReviewScreen />)
    await screen.findByTestId(`row-${bridge().fact_key}`)
    // Expand everything transient, so the group's members, a confirm panel and a reject panel (with
    // its chips and its note field) are all on screen at once.
    await userEvent.click(screen.getByRole('button', { name: /show the 8 candidates/i }))
    await userEvent.click(within(screen.getByTestId(`row-${bridge().fact_key}`))
      .getByRole('button', { name: /^confirm/i }))
    await userEvent.click(within(screen.getByTestId(`row-${join().fact_key}`))
      .getByRole('button', { name: /^reject/i }))

    const controls = [...container.querySelectorAll<HTMLElement>(
      'button, input, select, textarea, a[href], [tabindex]')]
    // Guards the guard: a selector that matched nothing would pass every assertion below.
    expect(controls.length).toBeGreaterThan(25)
    for (const control of controls) {
      // A control with no name is unusable by anyone who cannot see where it sits.
      expect(control).toHaveAccessibleName()
      // Nothing is removed from the tab order, and nothing here is a click-handling div.
      expect(control).not.toHaveAttribute('tabindex', '-1')
      if (!(control as HTMLButtonElement).disabled) {
        control.focus()
        expect(document.activeElement).toBe(control)
      }
    }
    // The one control that cannot take focus is a withheld confirm, disabled by the SERVER's own
    // action list — with its reason on screen and wired to it, so the row still reads.
    const withheld = within(screen.getByTestId('row-fact:entity_bridge:w'))
    const confirm = withheld.getByRole('button', { name: /^confirm/i })
    expect(confirm).toBeDisabled()
    expect(confirm).toHaveAttribute('aria-describedby', withheld.getByTestId('gq-action-why').id)
  })
})

// ── the destination the feature flow's refusal names (D14) ──────────────────────────────────────

it('carries the data-use policy panel the feature refusal points at', async () => {
  // `feature_assist._use_gate` refuses a personal-data operand with "... must declare one (purpose)
  // under Governance -> Data-use policies ...". This is the assertion that the pointer LANDS: the
  // heading a reader is sent to has to exist on the screen the refusal names, and it has to be
  // the DECIDING screen rather than the read-only dashboard.
  getGovernanceQueue.mockResolvedValue(FULL)
  getDataUsePolicies.mockResolvedValue({
    concepts: [{
      concept_name: 'pep_flag', description: 'Politically exposed person marker.', group: 'flag',
      status: 'none', pointer_version: 0, purpose: null, revision_id: null, approved_by: null,
      approved_at: null, declared_by: null, updated_at: null,
    }],
    purpose_bounds: { min: 8, max: 300 },
  })
  render(<GovernanceReviewScreen />)

  const panel = await screen.findByTestId('data-use-policies')
  expect(within(panel).getByRole('heading', { name: 'Data-use policies' })).toBeInTheDocument()
  expect(within(panel).getByTestId('dup-pep_flag')).toBeInTheDocument()
})

// ── semantic bindings in the queue (the 2026-08-10 currency-review gap) ─────────────────────────
// Six service-proposed currency bindings sat behind a four-eyes confirm while this screen — the one
// place humans review — never listed the kind. These tests pin the kind end to end: section,
// headline in the reviewer's language, its OWN confirm command and reject vocabulary.

function currencyBinding(over: Partial<api.GovernanceQueueItem> = {}): api.GovernanceQueueItem {
  return {
    kind: 'currency_binding',
    fact_key: 'fact:currency:tran_amt',
    catalogs: ['ftr'],
    subject: 'ftr.comp_financial_tran_repos_dly.tran_amt',
    state: 'Unreviewed — available for use',
    state_code: 'unreviewed_available',
    production_eligibility: null,
    production_eligibility_code: 'not_applicable',
    available_actions: ['confirm', 'reject'],
    detail: {
      target: { schema: 'public', table: 'comp_financial_tran_repos_dly', column: 'tran_crncy' },
      value: { currency_column: { column: 'tran_crncy' } },
      entity_id: null, prior_value: null, target_event_id: 'ev-ccy-1',
      disposition: 'strong', reason_codes: [],
    },
    already_depended_on_by: [],
    ...over,
  }
}

describe('semantic bindings in the queue', () => {
  it('renders a per-row currency binding in the reviewer\'s language', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [currencyBinding()],
      items_visible_to_you_by_kind: { currency_binding: 1 },
    }))
    render(<GovernanceReviewScreen />)
    const row = within(await screen.findByTestId('row-fact:currency:tran_amt'))
    expect(row.getByText(/tran_amt is in the currency named by tran_crncy/i)).toBeInTheDocument()
    expect(screen.getByTestId('kind-currency_binding')).toBeInTheDocument()
  })

  it('confirm goes to the SEMANTIC-BINDING command with the binding agreement', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [currencyBinding()],
      items_visible_to_you_by_kind: { currency_binding: 1 },
    }))
    confirmSemanticBinding.mockResolvedValue(
      { governance_status: 'VERIFIED', operational_projection: 'projected' })
    render(<GovernanceReviewScreen />)
    const row = within(await screen.findByTestId('row-fact:currency:tran_amt'))
    await userEvent.click(row.getByRole('button', { name: /^confirm/i }))
    const label = row.getByRole('checkbox').closest('label')!
    expect(label).toHaveTextContent(/currency varies per row and is named by tran_crncy/i)
    await userEvent.click(row.getByRole('checkbox'))
    await userEvent.click(row.getByRole('button', { name: /record my confirmation/i }))
    await waitFor(() => expect(confirmSemanticBinding)
      .toHaveBeenCalledWith('fact:currency:tran_amt', {}))
    expect(confirmTableFact).not.toHaveBeenCalled()
    expect(confirmJoin).not.toHaveBeenCalled()
  })

  it('reject offers the semantic-binding vocabulary, and dispatches to its own command', async () => {
    getGovernanceQueue.mockResolvedValue(queue({
      items: [currencyBinding()],
      items_visible_to_you_by_kind: { currency_binding: 1 },
    }))
    rejectSemanticBinding.mockResolvedValue({ governance_status: 'REJECTED', category: 'wrong_currency_column' })
    render(<GovernanceReviewScreen />)
    const row = within(await screen.findByTestId('row-fact:currency:tran_amt'))
    await userEvent.click(row.getByRole('button', { name: /^reject/i }))
    // categories render as pressed-state chips (CategoryChips), not radios
    await userEvent.click(row.getByRole('button', { name: /^wrong currency column$/i }))
    await userEvent.click(row.getByRole('button', { name: /record my rejection/i }))
    await waitFor(() => expect(rejectSemanticBinding).toHaveBeenCalledWith(
      'fact:currency:tran_amt', { category: 'wrong_currency_column' }))
  })

  it('an uploader whose confirm the server would 409 gets a DISABLED confirm, not a live one', async () => {
    // The queue projects four-eyes into available_actions (backend: the uploading principal's
    // confirm is dropped). The screen's existing pattern for an unsanctioned action is a
    // disabled button — never a live button that invites a guaranteed 409.
    getGovernanceQueue.mockResolvedValue(queue({
      items: [currencyBinding({ available_actions: ['reject'] })],
      items_visible_to_you_by_kind: { currency_binding: 1 },
    }))
    render(<GovernanceReviewScreen />)
    const row = within(await screen.findByTestId('row-fact:currency:tran_amt'))
    expect(row.getByRole('button', { name: /^confirm/i })).toBeDisabled()
    expect(row.getByRole('button', { name: /^reject/i })).toBeEnabled()
  })
})
