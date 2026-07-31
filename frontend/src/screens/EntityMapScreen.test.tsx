import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { EntityMapScreen } from './EntityMapScreen'

// Entity map v0 honesty rules (ingestion-richness Task 3D):
//   * an empty map SAYS "no governed links yet" — never a blank canvas;
//   * a proposed link renders as proposed — the usable-not-failure badge, never a failure tone;
//   * clicks route to the surfaces that own the data: node -> search filtered to the entity,
//     edge endpoint -> asset detail, review -> governance.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getEntityMap: vi.fn() }
})
const getEntityMap = vi.mocked(api.getEntityMap)

const navigate = vi.fn()

beforeEach(() => {
  getEntityMap.mockReset()
  navigate.mockReset()
})

function endpoint(over: Partial<api.EntityMapEndpoint> = {}): api.EntityMapEndpoint {
  return {
    catalog_source: 'cib',
    table_ref: 'public.bo_cib_customer',
    column_refs: ['public.bo_cib_customer.cust_num'],
    entity_id: 'customer',
    concept: null,
    namespace: null,
    ...over,
  }
}

function link(over: Partial<api.EntityMapLink> = {}): api.EntityMapLink {
  return {
    candidate_id: 'cand-1',
    candidate_revision_id: 'rev-1',
    bridge_fact_key: 'fk-1',
    status: 'proposed',
    folded_status: 'DRAFT',
    strength: 15,
    left: endpoint(),
    right: endpoint({
      catalog_source: 'ftr',
      table_ref: 'public.comp_financial_tran_repos_dly',
      column_refs: ['public.comp_financial_tran_repos_dly.cif_id'],
    }),
    realizations: [],
    ...over,
  }
}

function node(over: Partial<api.EntityMapNode> = {}): api.EntityMapNode {
  return {
    entity_id: 'customer',
    registered: true,
    column_count: 3,
    catalogs: [
      { catalog_source: 'cib', column_count: 2, sample_refs: ['public.bo_cib_customer.cust_num'] },
      {
        catalog_source: 'ftr',
        column_count: 1,
        sample_refs: ['public.comp_financial_tran_repos_dly.cif_id'],
      },
    ],
    ...over,
  }
}

function payload(over: Partial<api.EntityMap> = {}): api.EntityMap {
  return { entities: [node()], links: [link()], ...over }
}

function renderMap(map: api.EntityMap = payload()) {
  getEntityMap.mockResolvedValue(map)
  render(<EntityMapScreen navigate={navigate} />)
}

describe('EntityMapScreen', () => {
  it('an empty map states "no governed links yet" — never a blank canvas', async () => {
    renderMap({ entities: [], links: [] })
    expect(await screen.findByText(/no governed links yet/i)).toBeInTheDocument()
    expect(screen.getByText(/no entities in the graph yet/i)).toBeInTheDocument()
  })

  it('a proposed link renders as proposed, never as a failure', async () => {
    renderMap()
    const badge = await screen.findByText('proposed')
    expect(badge.className).toContain('gj-proposed')
    expect(screen.queryByText(/blocked|unavailable|error|failed/i)).toBeNull()
  })

  it('a confirmed link gets the verified tone', async () => {
    renderMap(payload({ links: [link({ status: 'confirmed' })] }))
    const badge = await screen.findByText('confirmed')
    expect(badge.className).toContain('gj-verified')
  })

  it('renders entity nodes with their read-scoped per-catalog counts', async () => {
    renderMap()
    // 'customer' also labels the edge, so scope to the node button (named by its full content).
    const nodeBtn = await screen.findByRole('button', { name: /3 columns/ })
    expect(within(nodeBtn).getByText('customer')).toBeInTheDocument()
    expect(within(nodeBtn).getByText('cib · 2')).toBeInTheDocument()
    expect(within(nodeBtn).getByText('ftr · 1')).toBeInTheDocument()
  })

  it('an unregistered entity is shown and flagged, not dropped', async () => {
    renderMap(payload({
      entities: [node({ entity_id: 'segment', registered: false, column_count: 1 })],
    }))
    expect(await screen.findByText('segment')).toBeInTheDocument()
    expect(screen.getByText(/not in the concept registry/)).toBeInTheDocument()
  })

  it('clicking a node opens search filtered to that entity', async () => {
    renderMap()
    await userEvent.click(await screen.findByRole('button', { name: /3 columns/ }))
    expect(navigate).toHaveBeenCalledWith('search', { entity: 'customer' })
  })

  it('clicking an edge endpoint opens its asset detail', async () => {
    renderMap()
    await userEvent.click(await screen.findByRole('button', { name: 'ftr · cif_id' }))
    expect(navigate).toHaveBeenCalledWith('asset', {
      source: 'ftr',
      object_ref: 'public.comp_financial_tran_repos_dly.cif_id',
    })
  })

  it('an edge routes to the governance queue for review', async () => {
    renderMap()
    await userEvent.click(await screen.findByRole('button', { name: 'Review on Governance' }))
    expect(navigate).toHaveBeenCalledWith('governance', { source: 'cib' })
  })

  it('shows the registry namespace and strength on the edge', async () => {
    renderMap(payload({
      links: [link({
        strength: 26,
        left: endpoint({ concept: 'customer_id', namespace: 'cif' }),
      })],
    }))
    expect(await screen.findByText('namespace cif')).toBeInTheDocument()
    expect(screen.getByText('strength 26')).toBeInTheDocument()
  })

  it('renders direction-specific eligibility where a realization exists', async () => {
    renderMap(payload({
      links: [link({
        realizations: [{
          from_catalog_source: 'cib',
          from_table_ref: 'public.bo_cib_customer',
          to_catalog_source: 'ftr',
          to_table_ref: 'public.comp_financial_tran_repos_dly',
          lifecycle: 'active',
          safety_status: 'deterministically_validated',
          sandbox_eligible: true,
          production_eligible: false,
        }],
      })],
    }))
    const row = await screen.findByText(/sandbox-eligible/)
    expect(row.textContent).toContain('sandbox-eligible')
    expect(row.textContent).toContain('not production-eligible')
  })

  it('a 403 names the missing permission instead of a red failure', async () => {
    getEntityMap.mockRejectedValue(new api.ApiError(403, 'forbidden'))
    render(<EntityMapScreen navigate={navigate} />)
    expect(await screen.findByText(/don’t have access to the entity map/i)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })
})
