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

async function openLinks(detail: api.AssetDetail = fixture()) {
  getAssetDetail.mockResolvedValue({ detail, etag: 'etag-1' })
  render(<AssetDetailScreen source="deposits" objectRef="public.accounts.balance" />)
  await userEvent.click(await screen.findByRole('button', { name: 'Relationships' }))
}

it('shows an UNCONFIRMED link rather than withholding it', async () => {
  await openLinks()
  expect(screen.getByText(/cust_num/)).toBeInTheDocument()
  expect(screen.getByText(/cif_id/)).toBeInTheDocument()
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
  const text = screen.getByText(/cust_num/).textContent ?? ''
  expect(text).toContain('cib')
  expect(text).toContain('ftr')
})

it('explains the empty case rather than showing a bare heading', async () => {
  const d = fixture()
  d.relationships!.cross_catalog = []
  await openLinks(d)
  expect(screen.getByText(/no link to another catalog yet/i)).toBeInTheDocument()
})
