import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { AssetDetailScreen } from './AssetDetailScreen'
import { fixture } from './AssetDetailScreen.fixture'

// Readiness, reframed.
//
// The tab led with "2 / 5 ready · 3 blocked", painted three roles red, and then listed the parent
// table's 341 blocking requirements — all sharing ONE cause — on every column page.
//
// Under the product rule (the tool uses AI-proposed fields whether or not a human has reviewed
// them) an unreviewed AI value is usable. "Blocked" is not a state this product has.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getAssetDetail: vi.fn(), postFieldDecision: vi.fn() }
})
const getAssetDetail = vi.mocked(api.getAssetDetail)

function role(over: Partial<api.RoleUsability> = {}): api.RoleUsability {
  return {
    role: 'as_event_time', label: 'Event time', state: 'ai_proposed',
    headline: 'AI proposed',
    detail: 'Proposed by AI and not yet reviewed by a person. The tool will use it as-is.',
    action: 'confirm', outstanding: ['event_time'], data_checks: [], ...over,
  }
}

function detail(roles: api.RoleUsability[], rollup: Partial<api.TableRollup> = {}): api.AssetDetail {
  const base = fixture()
  base.readiness = {
    column_capabilities: base.readiness?.column_capabilities ?? null,
    usability: {
      object_ref: 'public.accounts.balance', roles,
      usable_roles: roles.filter(r => r.state !== 'not_set' && r.state !== 'unavailable').length,
      total_roles: roles.length,
      headline: `Usable for ${roles.filter(r => r.state !== 'not_set').length} of ${roles.length} roles`,
    },
    table_rollup: {
      table: 'accounts', headline: '109 columns are AI-proposed and not yet reviewed.',
      columns_unreviewed: 109, columns_needing_decision: 0, requirements_total: 341,
      dominant_cause: 'unresolved_authority', dominant_cause_plain: 'waiting on a review decision',
      columns_outstanding: 109, ...rollup,
    },
  } as api.ReadinessSection
  return base
}

beforeEach(() => {
  getAssetDetail.mockReset()
})

async function openTab(d: api.AssetDetail) {
  getAssetDetail.mockResolvedValue({ detail: d, etag: 'etag-1' })
  render(<AssetDetailScreen source="deposits" objectRef="public.accounts.balance" />)
  await userEvent.click(await screen.findByRole('button', { name: 'Readiness' }))
}

// ── the word is gone ─────────────────────────────────────────────────────────────────────────────

it('never says blocked anywhere on the tab', async () => {
  await openTab(detail([role(), role({ role: 'as_entity_key', label: 'Entity key',
                                       state: 'not_set', headline: 'Not set', action: 'assign' })]))
  expect(screen.queryByText(/blocked/i)).toBeNull()
})

it('leads with what the column can be used for, not what is missing', async () => {
  await openTab(detail([role()]))
  expect(screen.getByText(/usable for/i)).toBeInTheDocument()
})

// ── each role says its state in plain words, and what to do ──────────────────────────────────────

it('shows an AI-proposed role as usable, with a confirm action', async () => {
  await openTab(detail([role()]))
  expect(screen.getByText('AI proposed')).toBeInTheDocument()
  expect(screen.getByRole('button', { name: /confirm/i })).toBeInTheDocument()
})

it('shows a data check as a question about the data, not a failure', async () => {
  await openTab(detail([role({
    role: 'as_grain_key', label: 'Grain key', state: 'needs_data_check',
    headline: 'Needs a data check',
    detail: 'The metadata is settled. Needs a data check first: is this key unique per row.',
    action: 'run_data_check', outstanding: [], data_checks: ['external:GRAIN_IS_UNIQUE'],
  })]))
  expect(screen.getByText(/is this key unique per row/i)).toBeInTheDocument()
})

it('shows a not-set role as needing a decision rather than as an error', async () => {
  await openTab(detail([role({
    role: 'as_entity_key', label: 'Entity key', state: 'not_set', headline: 'Not set',
    detail: 'Nothing — person or AI — has proposed entity_assignment for this column.',
    action: 'assign', outstanding: ['entity_assignment'], data_checks: [],
  })]))
  expect(screen.getByText('Not set')).toBeInTheDocument()
  expect(screen.getByText(/has proposed entity_assignment/i)).toBeInTheDocument()
})

// ── the parent table is one line, not 341 rows ───────────────────────────────────────────────────

it('summarises the parent table in a sentence', async () => {
  await openTab(detail([role()]))
  expect(screen.getByText(/109 columns are AI-proposed/i)).toBeInTheDocument()
})

it('does not render the parent table requirement rows', async () => {
  await openTab(detail([role()]))
  // The old panel rendered one <li> per requirement — 341 of them, all one cause.
  expect(screen.queryByText(/unresolved_authority/)).toBeNull()
  expect(screen.queryByText(/needs any\(/)).toBeNull()
})

it('says how many items sit behind the summary so the number is not hidden', async () => {
  await openTab(detail([role()]))
  expect(screen.getByText(/341/)).toBeInTheDocument()
})

// ── the machine detail is reachable, not deleted ─────────────────────────────────────────────────

it('keeps the raw requirement ids behind a disclosure', async () => {
  await openTab(detail([role()]))
  expect(screen.queryByText('event_time')).toBeNull()
  await userEvent.click(screen.getByRole('button', { name: /detail/i }))
  expect(screen.getByText('event_time')).toBeInTheDocument()
})
