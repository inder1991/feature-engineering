import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { AssetDetailScreen } from './AssetDetailScreen'
import { fixture } from './AssetDetailScreen.fixture'

// Metadata & evidence: one line per field, detail on demand.
//
// The tab spent ten lines and four coloured bars per field to say one thing — "concept is
// boolean_flag, the AI proposed it, nobody has reviewed it". Nine fields made ninety lines.
//
// The worst of it was the empty lifecycle buckets: STALE, REJECTED and SUPERSEDED rendered as
// full-width coloured bars EVEN WHEN EMPTY. A red bar labelled REJECTED where nothing was rejected
// does not merely waste space, it states something alarming that is not true.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getAssetDetail: vi.fn(), postFieldDecision: vi.fn() }
})
const getAssetDetail = vi.mocked(api.getAssetDetail)

function detail(over: (d: api.AssetDetail) => void = () => {}): api.AssetDetail {
  const base = fixture()
  over(base)
  return base
}

beforeEach(() => {
  getAssetDetail.mockReset()
})

async function openTab(d: api.AssetDetail) {
  getAssetDetail.mockResolvedValue({ detail: d, etag: 'etag-1' })
  render(<AssetDetailScreen source="deposits" objectRef="public.accounts.balance" />)
  await userEvent.click(await screen.findByRole('button', { name: 'Metadata & evidence' }))
}

const row = (field: string) => screen.getByTestId(`field-row-${field}`)

// ── the empty buckets are gone ───────────────────────────────────────────────────────────────────

it('does not announce REJECTED when nothing was rejected', async () => {
  await openTab(detail(d => {
    d.evidence!.proposals_by_field = { currency: { active: d.evidence!.proposals_by_field.currency!.active } }
  }))
  expect(screen.queryByText(/^rejected$/i)).toBeNull()
  expect(screen.queryByText(/^superseded$/i)).toBeNull()
  expect(screen.queryByText(/^stale$/i)).toBeNull()
})

it('still shows a bucket that genuinely has something in it', async () => {
  await openTab(detail())
  await userEvent.click(within(row('currency')).getByRole('button', { name: /detail/i }))
  expect(within(row('currency')).getByText(/^active$/i)).toBeInTheDocument()
})

// ── one line per field by default ────────────────────────────────────────────────────────────────

it('shows the value and who said it on one line, without machine tokens', async () => {
  await openTab(detail())
  const line = row('currency').textContent ?? ''
  expect(line).toContain('USD')
  expect(line).not.toContain('c1 ')            // `authority hint · c1 no_value`
  expect(line).not.toContain('no_value')
  expect(line).not.toMatch(/llm · proposed/)   // producer/strength is machinery, not an author
})

it('drops the per-row explainer sentence', async () => {
  await openTab(detail())
  expect(screen.queryByText(/not about whether a value is present/i)).toBeNull()
})

it('does not apologise when a field has no correction command', async () => {
  await openTab(detail())
  expect(screen.queryByText(/returned no correction command/i)).toBeNull()
})

// ── the detail is on demand, not deleted ─────────────────────────────────────────────────────────

it('keeps the decision detail behind a disclosure', async () => {
  await openTab(detail())
  expect(screen.queryByText(/latest decision/i)).toBeNull()
  await userEvent.click(within(row('currency')).getByRole('button', { name: /detail/i }))
  expect(within(row('currency')).getByText(/latest decision/i)).toBeInTheDocument()
})

it('an action still shows where the server offered one', async () => {
  await openTab(detail())
  expect(within(row('currency')).getByRole('button', { name: /correct/i })).toBeInTheDocument()
})

// ── every field is present, none dropped ─────────────────────────────────────────────────────────

it('renders a row for every field the server returned', async () => {
  const d = detail()
  await openTab(d)
  for (const name of Object.keys(d.effective_metadata?.fields ?? {})) {
    expect(row(name)).toBeInTheDocument()
  }
})

// ── the fields nobody has set are grouped, not nine rows of nothing ──────────────────────────────

/** The shared fixture sets every field; a real column leaves most unset. */
const withUnset = () => detail(d => {
  d.effective_metadata!.fields.entity = { ...d.effective_metadata!.fields.entity, value: null }
  d.effective_metadata!.fields.unit = { ...d.effective_metadata!.fields.unit, value: null }
})

it('does not spend a row on every unset field', async () => {
  // Five of nine fields on a real column are "not set". Nine rows where five say nothing is why
  // the tab read as noise — the ones that carry information should carry the eye.
  await openTab(withUnset())
  expect(screen.queryAllByText(/^— not set$/)).toHaveLength(0)
  expect(screen.getByRole('button', { name: /not set/i })).toBeInTheDocument()
})

it('still reaches an unset field, and its action, on demand', async () => {
  await openTab(withUnset())
  await userEvent.click(screen.getByRole('button', { name: /not set/i }))
  expect(row('entity')).toBeInTheDocument()
})

it('keeps a field that HAS a value as a top-level row', async () => {
  await openTab(detail())
  expect(row('currency')).toBeInTheDocument()   // USD
})
