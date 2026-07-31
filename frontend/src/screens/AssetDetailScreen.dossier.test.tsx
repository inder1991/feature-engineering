import { render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { AssetDetailScreen } from './AssetDetailScreen'
import { fixture, suggestionsFixture } from './AssetDetailScreen.fixture'

// The column dossier (richness Task 3C): clicking a column answers everything end-to-end.
// This file pins the plan's contracts:
//   * the "From the source glossary" section renders the Step-6b fields with source provenance,
//     and an asset without them shows NO fabricated section;
//   * an LLM-proposed unit renders (with the AI-proposed · unconfirmed chip) instead of a blank,
//     and a governed unit replaces it;
//   * an axis with nothing is an explicit "nothing known yet" — a NULL axis is distinguishable
//     from a hidden one;
//   * suggested features appear for a column a suggestion USES; a 403 renders an honest access
//     message, never an empty list;
//   * the AI summary renders BESIDE the definition, labelled AI-drafted — never in its place.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getAssetDetail: vi.fn(),
    postFieldDecision: vi.fn(),
    getTableSuggestions: vi.fn(),
  }
})
const getAssetDetail = vi.mocked(api.getAssetDetail)
const getTableSuggestions = vi.mocked(api.getTableSuggestions)

beforeEach(() => {
  getAssetDetail.mockReset()
  getTableSuggestions.mockReset()
  getTableSuggestions.mockResolvedValue(suggestionsFixture())
})

function detail(over: (d: api.AssetDetail) => void = () => {}): api.AssetDetail {
  const base = fixture()
  over(base)
  return base
}

async function renderDossier(d: api.AssetDetail) {
  getAssetDetail.mockResolvedValue({ detail: d, etag: 'etag-1' })
  render(<AssetDetailScreen source="deposits" objectRef="public.accounts.balance" />)
  await screen.findByRole('group', { name: /asset sections/i })
}

// ── From the source glossary ─────────────────────────────────────────────────────────────────────

const GLOSSARY = {
  business_term: { value: 'Transaction Amount', provenance: 'source attested' },
  term_type: { value: 'measure', provenance: 'source attested' },
  process_path: { value: 'Payments > Screening > Reporting', provenance: 'source attested' },
  related_terms: { value: 'Transaction Currency, Value Date', provenance: 'source attested' },
  bian_path: { value: 'Payment Order', provenance: 'source attested' },
  fibo_path: { value: 'fibo-fnd:MonetaryAmount', provenance: 'source attested' },
  physical_fqn: { value: 'DPL.COMP_TRAN.TRAN_AMT', provenance: 'source attested' },
}

it('renders the source-glossary fields under product names, each with its source chip', async () => {
  await renderDossier(detail(d => {
    d.source_glossary = { fields: { ...GLOSSARY } }
  }))
  const section = screen.getByTestId('source-glossary')
  expect(within(section).getByText('From the source glossary')).toBeInTheDocument()
  // Product names, not CSV headers.
  expect(within(section).getByText('Business term')).toBeInTheDocument()
  expect(within(section).getByText('Term type')).toBeInTheDocument()
  expect(within(section).getByText('Business processes')).toBeInTheDocument()
  expect(within(section).getByText('Related terms')).toBeInTheDocument()
  expect(within(section).getByText('BIAN classification')).toBeInTheDocument()
  expect(within(section).getByText('FIBO classification')).toBeInTheDocument()
  expect(within(section).getByText('Physical path')).toBeInTheDocument()
  expect(within(section).getByText('Declared type')).toBeInTheDocument()
  // Values, including the L1 → L2 → L3 path rendered as ONE path.
  expect(within(section).getByText('Transaction Amount')).toBeInTheDocument()
  expect(within(section).getByText('Payments → Screening → Reporting')).toBeInTheDocument()
  expect(within(section).getByText('DPL.COMP_TRAN.TRAN_AMT')).toBeInTheDocument()
  // Every value carries the source provenance chip.
  expect(within(section).getAllByText('source attested').length).toBeGreaterThanOrEqual(7)
})

it('shows no fabricated glossary section when the upload declared none of it', async () => {
  await renderDossier(detail(d => {
    d.source_glossary = { fields: {} }
    d.identity.declared_type = null
  }))
  expect(screen.queryByTestId('source-glossary')).toBeNull()
  expect(screen.queryByText('From the source glossary')).toBeNull()
})

// ── AI-proposed instead of blank ─────────────────────────────────────────────────────────────────

it('renders an LLM-proposed unit with the AI-proposed · unconfirmed chip instead of a blank', async () => {
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.unit = {
      value: null, authority: 'missing', c1_status: 'no_decision',
      provenance: null, evidence_provenance: 'AI proposed', selected_evidence_ids: [],
      proposed_value: 'AED',
    }
  }))
  const row = screen.getByTestId('axis-unit')
  expect(within(row).getByText('AED')).toBeInTheDocument()
  expect(within(row).getByText('AI proposed · unconfirmed')).toBeInTheDocument()
})

it('a governed unit replaces the proposal — value and author, no unconfirmed chip', async () => {
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.unit = {
      value: 'USD', authority: 'governed', c1_status: 'resolved',
      provenance: 'source_declared', evidence_provenance: null, selected_evidence_ids: [],
      proposed_value: 'AED',   // the display value wins; the stale proposal never renders
    }
  }))
  const row = screen.getByTestId('axis-unit')
  expect(within(row).getByText('USD')).toBeInTheDocument()
  expect(within(row).queryByText('AED')).toBeNull()
  expect(within(row).queryByText(/unconfirmed/)).toBeNull()
  expect(within(row).getByText('source declared')).toBeInTheDocument()
})

it('an axis with nothing at all says "nothing known yet" — explicit, never hidden', async () => {
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.unit = {
      value: null, authority: 'missing', c1_status: 'no_decision',
      provenance: null, evidence_provenance: null, selected_evidence_ids: [],
      proposed_value: null,
    }
  }))
  expect(within(screen.getByTestId('axis-unit')).getByText('nothing known yet')).toBeInTheDocument()
})

it('renders projected display axes (sensitivity / party role) with their system chip', async () => {
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.sensitivity_display = {
      value: 'restricted', authority: 'hint', c1_status: 'no_decision',
      provenance: null, evidence_provenance: 'system projected', selected_evidence_ids: [],
      proposed_value: null,
    }
    d.effective_metadata!.fields.party_role = {
      value: 'sender', authority: 'hint', c1_status: 'no_decision',
      provenance: null, evidence_provenance: 'system projected', selected_evidence_ids: [],
      proposed_value: null,
    }
  }))
  const sens = screen.getByTestId('axis-sensitivity_display')
  expect(within(sens).getByText('Sensitivity')).toBeInTheDocument()
  expect(within(sens).getByText('restricted')).toBeInTheDocument()
  const role = screen.getByTestId('axis-party_role')
  expect(within(role).getByText('Party role')).toBeInTheDocument()
  expect(within(role).getByText('sender')).toBeInTheDocument()
  expect(screen.getAllByText('system projected')).toHaveLength(2)
})

// ── AI summary beside — never instead of — the definition ────────────────────────────────────────

it('shows the AI summary beside the definition, labelled AI-drafted', async () => {
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.definition = {
      value: 'The monetary value of the transaction record.', authority: 'hint',
      c1_status: 'no_decision', provenance: null, evidence_provenance: 'source attested',
      selected_evidence_ids: [],
    }
    d.effective_metadata!.fields.ai_summary = {
      value: 'A screening measure: aggregate for velocity and threshold checks.', authority: 'hint',
      c1_status: 'no_decision', provenance: null, evidence_provenance: 'AI proposed',
      selected_evidence_ids: [],
    }
  }))
  const meaning = screen.getByTestId('meaning')
  // Two visibly distinct slots: the definition column and the summary column.
  const definition = within(meaning).getByTestId('meaning-definition')
  const summary = within(meaning).getByTestId('meaning-summary')
  expect(within(definition).getByText(/monetary value of the transaction/i)).toBeInTheDocument()
  expect(within(summary).getByText(/screening measure/i)).toBeInTheDocument()
  expect(within(summary).getByText('AI-drafted')).toBeInTheDocument()
  // The summary never sits in the definition slot.
  expect(within(definition).queryByText(/screening measure/i)).toBeNull()
})

it('a summary without a definition still leaves the definition slot honest, not replaced', async () => {
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.definition = {
      value: null, authority: 'missing', c1_status: 'no_decision',
      provenance: null, evidence_provenance: null, selected_evidence_ids: [],
    }
    d.effective_metadata!.fields.ai_summary = {
      value: 'A drafted synthesis.', authority: 'hint', c1_status: 'no_decision',
      provenance: null, evidence_provenance: 'AI proposed', selected_evidence_ids: [],
    }
  }))
  const definition = screen.getByTestId('meaning-definition')
  expect(within(definition).getByText(/no definition from the source yet/i)).toBeInTheDocument()
  expect(within(definition).queryByText('A drafted synthesis.')).toBeNull()
  expect(within(screen.getByTestId('meaning-summary')).getByText('A drafted synthesis.'))
    .toBeInTheDocument()
})

// ── suggested features on the column ─────────────────────────────────────────────────────────────

function suggestion(over: Partial<api.FeatureSuggestion> = {}): api.FeatureSuggestion {
  return {
    name: 'account_balance_avg_30d',
    description: 'Average balance per account over 30 days.',
    recipe: 'AVG(balance) OVER account / 30d',
    recipe_parts: { operation: 'AVG', measures: ['balance'], grain: 'account', window: '30d', time: 'as_of' },
    validation_status: 'DESIGN_CHECKED',
    requirements: [],
    uses: ['public.accounts.balance', 'public.accounts.opened_at'],
    binding_quality: 'direct',
    grain_table: 'accounts',
    ...over,
  }
}

it('shows the suggestions that USE the opened column and filters out the rest', async () => {
  getTableSuggestions.mockResolvedValue({
    ...suggestionsFixture(),
    summary: { suggested: 2, clean_ready: 1, needs_review: 1, entities: 1 },
    groups: [{
      entity_ref: 'public.accounts.id',
      entity_label: 'account',
      suggestions: [
        suggestion(),
        suggestion({ name: 'account_age_days', uses: ['public.accounts.opened_at'] }),
      ],
    }],
  })
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  expect(await within(section).findByText('account_balance_avg_30d')).toBeInTheDocument()
  // grounded on a DIFFERENT column → not this column's dossier material.
  expect(within(section).queryByText('account_age_days')).toBeNull()
  expect(getTableSuggestions).toHaveBeenCalledWith('deposits', 'accounts')
})

it('says which suggestions exist when none uses this column — not a bare empty', async () => {
  getTableSuggestions.mockResolvedValue({
    ...suggestionsFixture(),
    summary: { suggested: 3, clean_ready: 0, needs_review: 3, entities: 1 },
    groups: [{
      entity_ref: 'public.accounts.id',
      entity_label: 'account',
      suggestions: [suggestion({ name: 'other', uses: ['public.accounts.opened_at'] })],
    }],
  })
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  expect(
    await within(section).findByText(/none of the 3 suggestions on this table uses this column/i),
  ).toBeInTheDocument()
})

it('a 403 renders an honest access message naming the permission — never an empty list', async () => {
  getTableSuggestions.mockRejectedValue(new api.ApiError(403, 'forbidden'))
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  const message = await within(section).findByRole('status')
  expect(message).toHaveTextContent(/you don't have access to feature suggestions/i)
  expect(message).toHaveTextContent('catalog:read')
  expect(within(section).queryByText(/no suggestions/i)).toBeNull()
})

it('a non-403 failure is an error, not an invented empty state', async () => {
  getTableSuggestions.mockRejectedValue(new api.ApiError(500, 'boom'))
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  expect(await within(section).findByRole('alert')).toHaveTextContent(/could not load suggestions/i)
})

// ── dossier order: identity → meaning → semantics → governance → usage ──────────────────────────

describe('section order', () => {
  it('renders the dossier sections in the plan order on the overview', async () => {
    await renderDossier(detail(d => {
      d.source_glossary = { fields: { term_type: { value: 'measure', provenance: 'source attested' } } }
      d.effective_metadata!.fields.definition = {
        value: 'The monetary value of the transaction record.', authority: 'hint',
        c1_status: 'no_decision', provenance: null, evidence_provenance: 'source attested',
        selected_evidence_ids: [],
      }
    }))
    await screen.findByTestId('column-suggestions')
    const text = document.body.textContent ?? ''
    const order = [
      'Identity',
      'Meaning',
      'From the source glossary',
      'Semantics',
      'Governance',
      'Suggested features using this column',
    ].map(h => text.indexOf(h))
    for (const idx of order) expect(idx).toBeGreaterThan(-1)
    for (let i = 1; i < order.length; i++) expect(order[i]).toBeGreaterThan(order[i - 1])
  })
})
