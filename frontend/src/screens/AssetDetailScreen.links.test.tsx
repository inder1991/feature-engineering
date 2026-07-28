import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import './lineage-test-setup'
import { AssetDetailScreen } from './AssetDetailScreen'
import { fixture } from './AssetDetailScreen.fixture'

// Cross-catalog links on screen — the thing that existed in the database and appeared nowhere.
//
// The candidate ledger had ZERO readers in the codebase, so `cib.cust_num <-> ftr.cif_id` was
// derived, persisted, and invisible. Owner's direction: show and consume links whether or not a
// human has confirmed them; confirmation marks approved, it does not gate.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getAssetDetail: vi.fn(), postFieldDecision: vi.fn(), lineageGraph: vi.fn() }
})
const getAssetDetail = vi.mocked(api.getAssetDetail)

beforeEach(() => {
  getAssetDetail.mockReset()
  vi.mocked(api.lineageGraph).mockResolvedValue({ nodes: [], edges: [], truncated: false })
})

/** The shared fixture's asset is `accounts.balance`; a cross-catalog link has to TOUCH the anchor,
 *  so this re-anchors it on the CIB customer column the real links are about. */
function linked(): api.AssetDetail {
  const d = fixture()
  d.identity.object_ref = 'public.bo_cib_customer.cust_num'
  d.identity.table = 'bo_cib_customer'
  d.identity.column = 'cust_num'
  return d
}

async function openLinks(detail: api.AssetDetail = linked()) {
  getAssetDetail.mockResolvedValue({ detail, etag: 'etag-1' })
  render(<AssetDetailScreen source="deposits" objectRef="public.accounts.balance" />)
  await userEvent.click(await screen.findByRole('button', { name: 'Relationships' }))
}

it('shows an UNCONFIRMED link rather than withholding it', async () => {
  await openLinks()
  // Present in BOTH the list and the graph, so assert on all matches rather than uniqueness.
  expect(screen.getAllByText(/cif_id/).length).toBeGreaterThan(0)
})

it('marks it proposed without making it look broken', async () => {
  await openLinks()
  const badges = screen.getAllByText(/^proposed$/)
  expect(badges.length).toBeGreaterThan(0)
  expect(screen.queryByText(/blocked|unavailable|error/i)).toBeNull()
})

it('says WHY a weak link is weak instead of showing it as equal', async () => {
  await openLinks()
  expect(screen.getByText(/neither side is a key/i)).toBeInTheDocument()
  expect(screen.getByText(/one side is its table's key/i)).toBeInTheDocument()
})

it('names both catalogs so the link reads as cross-catalog', async () => {
  await openLinks()
  const row = screen.getAllByText(/cust_num/).map(n => n.textContent ?? '').join(' ')
  expect(row).toContain('cib')
  expect(row).toContain('ftr')
})

it('explains the empty case rather than showing a bare heading', async () => {
  const d = linked()
  d.relationships!.cross_catalog = []
  await openLinks(d)
  expect(screen.getByText(/no link to another catalog yet/i)).toBeInTheDocument()
})

// ── the link must be on the GRAPH, not only in the list ──────────────────────────────────────────

it('draws the cross-catalog neighbour on the neighbourhood graph', async () => {
  await openLinks()
  const graph = document.querySelector('svg.adg-graph') as SVGElement
  expect(graph).toBeTruthy()
  expect(graph.textContent ?? '').toMatch(/cif_id/)
})

it('names the OTHER catalog on the graph, so the hop reads as cross-catalog', async () => {
  await openLinks()
  const graph = document.querySelector('svg.adg-graph') as SVGElement
  // Without the source, `comp_financial_tran_repos_dly.cif_id` looks like a same-catalog neighbour.
  expect(graph.textContent ?? '').toMatch(/ftr/)
})

it('labels the edge with the entity it links on', async () => {
  await openLinks()
  const graph = document.querySelector('svg.adg-graph') as SVGElement
  expect(graph.textContent ?? '').toMatch(/customer/)
})

it('draws an UNCONFIRMED link too — the graph is not confirmation-gated either', async () => {
  const d = linked()
  d.relationships!.cross_catalog = d.relationships!.cross_catalog.map(l => ({
    ...l, status: 'proposed' as const,
  }))
  await openLinks(d)
  const graph = document.querySelector('svg.adg-graph') as SVGElement
  expect(graph.textContent ?? '').toMatch(/cif_id/)
})
