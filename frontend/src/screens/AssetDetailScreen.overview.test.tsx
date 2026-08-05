import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { getSession, setSession } from '../session'
import { AssetDetailScreen } from './AssetDetailScreen'
import { fixture, suggestionsFixture } from './AssetDetailScreen.fixture'

// The Overview rebuild (three tiers: verdict strip → reasoning cards → receipts). This file pins
// the contracts the redesign introduced, each of which is a claim about HONESTY rather than layout:
//   * every semantic axis renders whether or not the response carried it — a server that omits an
//     axis must not be indistinguishable from an axis nobody has an opinion on;
//   * a zero in the verdict strip carries the sentence that stops it reading as a failure;
//   * coverage the platform does not hold is NAMED, and named as absent rather than invented;
//   * the technical refs are reachable but no longer occupy the top of the page.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getAssetDetail: vi.fn(),
    postFieldDecision: vi.fn(),
    getTableSuggestionsV2: vi.fn(),
  }
})
const getAssetDetail = vi.mocked(api.getAssetDetail)
const getTableSuggestions = vi.mocked(api.getTableSuggestionsV2)

const BASE_SESSION = getSession()

beforeEach(() => {
  getAssetDetail.mockReset()
  getTableSuggestions.mockReset()
  getTableSuggestions.mockResolvedValue(suggestionsFixture())
  setSession({ user: 'dev', roles: ['data_owner'] })
})
afterEach(() => setSession(BASE_SESSION))

function detail(over: (d: api.AssetDetail) => void = () => {}): api.AssetDetail {
  const base = fixture()
  over(base)
  return base
}

async function renderOverview(d: api.AssetDetail) {
  getAssetDetail.mockResolvedValue({ detail: d, etag: 'etag-1' })
  render(<AssetDetailScreen source="deposits" objectRef="public.accounts.balance" />)
  await screen.findByRole('group', { name: /asset sections/i })
}

const AXES = [
  'concept', 'domain', 'additivity', 'unit', 'currency', 'entity', 'sensitivity_display',
  'party_role',
]

// ── every axis renders, including the ones the response never mentioned ──────────────────────────

it('renders EVERY semantic axis even when the response omits it entirely', async () => {
  // The response carries exactly one axis. The other seven are absent from `fields` — not null,
  // absent. Before this, the rows were filtered to present keys, so an omitted axis silently
  // vanished and read as "the platform has nothing to say here" when the truth was "the server
  // did not send it".
  await renderOverview(detail(d => {
    d.effective_metadata = {
      fields: {
        concept: {
          value: 'as_of_date', authority: 'hint', c1_status: 'no_decision', provenance: null,
          evidence_provenance: 'llm proposed', selected_evidence_ids: [],
        },
      },
    }
  }))
  const list = await screen.findByTestId('attested-metadata')
  expect(within(list).getAllByRole('listitem')).toHaveLength(AXES.length)
  for (const axis of AXES) {
    expect(screen.getByTestId(`axis-${axis}`)).toBeInTheDocument()
  }
  // The seven the server never sent say so explicitly, and say it the same way a known-empty axis
  // does: nothing known yet. Silence is the one thing this list may not say.
  expect(screen.getAllByText('nothing known yet')).toHaveLength(AXES.length - 1)
  expect(screen.getByTestId('axis-concept')).toHaveTextContent('as_of_date')
})

it('counts populated against unknown so the reader knows the shape without counting rows', async () => {
  await renderOverview(detail())
  const card = screen.getByTestId('attested-metadata').closest('.adg-card') as HTMLElement
  const count = within(card).getByText(/populated ·/i).textContent ?? ''
  const populated = Number(count.match(/(\d+) populated/)?.[1])
  const unknown = Number(count.match(/(\d+) unknown/)?.[1])
  expect(populated + unknown).toBe(AXES.length)
})

// ── the verdict strip ────────────────────────────────────────────────────────────────────────────

it('never lets a zero in the strip stand alone as a verdict', async () => {
  await renderOverview(detail(d => {
    d.relationships = {
      containment: { table: { object_ref: 'public.accounts', table: 'accounts' }, columns: [] },
      approved_joins: [],
      semantic: d.relationships!.semantic,
      cross_catalog: [],
    }
  }))
  const strip = await screen.findByRole('region', { name: /asset decision summary/i })
  const rel = within(strip).getByText('Direct relationships').closest('.stat') as HTMLElement
  expect(within(rel).getByText('0')).toBeInTheDocument()
  // A bare 0 reads as a failure; the qualifier is what makes it a fact.
  expect(rel).toHaveTextContent(/parent table still provides context/i)
})

it('reports the usable-role count the server calculated, not one the client re-derives', async () => {
  await renderOverview(detail(d => {
    d.readiness!.usability = {
      object_ref: 'public.accounts.balance',
      roles: d.readiness!.usability?.roles ?? [],
      usable_roles: 2,
      total_roles: 5,
      headline: 'usable for 2 of 5 roles',
    }
  }))
  const strip = await screen.findByRole('region', { name: /asset decision summary/i })
  expect(within(strip).getByText('Potential uses').closest('.stat'))
    .toHaveTextContent('2 of 5')
})

// ── trust and coverage ───────────────────────────────────────────────────────────────────────────

it('names coverage the platform does not hold, and never invents a value for it', async () => {
  await renderOverview(detail())
  const panel = await screen.findByTestId('trust-coverage')
  // Profiling, ownership and SLA are real product information with no API behind them yet. The
  // panel exists so their absence is visible instead of vanishing through omission.
  expect(within(panel).getByTestId('coverage-profiling')).toHaveTextContent(/not profiled/i)
  expect(within(panel).getByTestId('coverage-owner')).toHaveTextContent(/not assigned/i)
  expect(within(panel).getByTestId('coverage-sla')).toHaveTextContent(/not defined/i)
  // The guarantee that matters: absence is stated, never filled with a plausible-looking value.
  // A fabricated owner or null-rate in a governance catalog is worse than a blank, because
  // somebody acts on it.
  expect(within(panel).queryByText(/@/)).toBeNull()
  expect(within(panel).getByTestId('coverage-profiling'))
    .not.toHaveTextContent(/\d+(\.\d+)?\s*%/)
})

it('says audit is role-gated rather than implying nothing was ever recorded', async () => {
  await renderOverview(detail(d => {
    d.audit = undefined
    d.unavailable_sections = [...d.unavailable_sections, 'audit']
  }))
  const row = await screen.findByTestId('coverage-audit')
  expect(row).toHaveTextContent(/restricted/i)
  expect(row).toHaveTextContent(/audit:read/i)
})

// ── the receipts, demoted ────────────────────────────────────────────────────────────────────────

it('keeps the technical refs reachable but out of the page-top position', async () => {
  await renderOverview(detail())
  const tech = await screen.findByTestId('technical-identity')
  // Collapsed by default: this is the least business-relevant content on the page and it used to
  // be the first section on it.
  expect(tech).not.toHaveAttribute('open')
  const text = document.body.textContent ?? ''
  expect(text.indexOf('Technical identity')).toBeGreaterThan(text.indexOf('Business meaning'))

  await userEvent.click(within(tech).getByText(/technical identity/i))
  expect(within(tech).getByText('Object reference')).toBeInTheDocument()
  expect(within(tech).getByText('Consistency token')).toBeInTheDocument()
})

// ── capabilities lifted onto Overview ────────────────────────────────────────────────────────────

it('answers "can I use this column" on Overview and hands off to full readiness', async () => {
  await renderOverview(detail())
  const roles = fixture().readiness?.usability?.roles ?? []
  expect(roles.length).toBeGreaterThan(0)
  for (const role of roles) {
    expect(screen.getByTestId(`cap-${role.role}`)).toHaveTextContent(role.headline)
  }
  await userEvent.click(screen.getByRole('button', { name: /full readiness/i }))
  // The jump lands on the Readiness tab rather than duplicating its evidence ids on Overview.
  expect(screen.getByRole('button', { name: 'Readiness' })).toHaveAttribute('aria-pressed', 'true')
})
