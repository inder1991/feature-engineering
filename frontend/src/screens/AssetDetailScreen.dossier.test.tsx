import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { getSession, setSession } from '../session'
import { AssetDetailScreen } from './AssetDetailScreen'
import { fixture, suggestionsFixture } from './AssetDetailScreen.fixture'
import { hit as suggestionHit, label } from './SuggestedFeaturesScreen.fixture'

// The column dossier (richness Task 3C): clicking a column answers everything end-to-end.
// This file pins the plan's contracts:
//   * the "From the source glossary" section renders the Step-6b fields with source provenance,
//     and an asset without them shows NO fabricated section;
//   * an LLM-proposed unit renders (with the AI-proposed · unconfirmed chip) instead of a blank,
//     and a governed unit replaces it;
//   * an axis with nothing is an explicit "nothing known yet" — a NULL axis is distinguishable
//     from a hidden one;
//   * suggested features appear for a column a suggestion USES, in the SAME card the table screen
//     renders (one semantic and warning vocabulary everywhere); a 403 renders an honest access
//     message, never an empty list; a visibility-claim change clears the section;
//   * the AI summary renders BESIDE the definition, labelled AI-drafted — never in its place.

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

// The v2 discovery hit, reusing the table screen's own builders so the dossier can never drift into
// a second fixture shape (and therefore never into a second card vocabulary).
function usingBalance(over: Partial<api.FeatureSuggestionV2> = {}): api.FeatureSuggestionHit {
  return suggestionHit({
    suggestion_id: 'sug-balance',
    name: 'account_balance_avg_30d',
    display_name: 'account_balance_avg_30d',
    operands: [
      {
        catalog_source: 'deposits', logical_ref: 'deposits.accounts.balance',
        graph_object_ref: 'public.accounts.balance', table_ref: 'accounts',
        recipe_role: 'balance', classification: 'measure',
        visibility_requires_current: [], evidence_refs: [],
      },
      {
        catalog_source: 'deposits', logical_ref: 'deposits.accounts.opened_at',
        graph_object_ref: 'public.accounts.opened_at', table_ref: 'accounts',
        recipe_role: 'as_of', classification: 'time',
        visibility_requires_current: [], evidence_refs: [],
      },
    ],
    ...over,
  })
}

function withHits(hits: api.FeatureSuggestionHit[], suggested = hits.length) {
  const base = suggestionsFixture()
  return {
    ...base,
    collection: {
      ...base.collection,
      summary: { ...base.collection.summary, suggested },
    },
    hits,
  }
}

it('shows the suggestions that USE the opened column and filters out the rest', async () => {
  getTableSuggestions.mockResolvedValue(withHits([
    usingBalance(),
    suggestionHit({
      suggestion_id: 'sug-age', name: 'account_age_days', display_name: 'account_age_days',
      operands: [{
        catalog_source: 'deposits', logical_ref: 'deposits.accounts.opened_at',
        graph_object_ref: 'public.accounts.opened_at', table_ref: 'accounts',
        recipe_role: 'as_of', classification: 'time',
        visibility_requires_current: [], evidence_refs: [],
      }],
    }),
  ], 2))
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  expect(await within(section).findByText('account_balance_avg_30d')).toBeInTheDocument()
  // grounded on a DIFFERENT column → not this column's dossier material.
  expect(within(section).queryByText('account_age_days')).toBeNull()
  expect(getTableSuggestions).toHaveBeenCalledWith('deposits', 'accounts')
})

it('renders the SAME card vocabulary the table screen uses, drawer included', async () => {
  getTableSuggestions.mockResolvedValue(withHits([usingBalance({
    business_domains: [label({ id: 'liquidity', display_name: 'Liquidity' })],
    warnings: [{
      code: 'RELATIONSHIP_SAFETY_UNPROVEN',
      operand_refs: [['deposits', 'public.accounts.balance']],
      detail: 'a traversed relationship has no governed-verified safety evidence',
    }],
  })]))
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  // the card's own semantic and warning vocabulary, not a stripped copy
  expect(within(section).getByText('suggested · recipe')).toBeInTheDocument()
  expect(within(section).getByText('Trend & Trajectory')).toBeInTheDocument()
  expect(within(section).getByText('Liquidity')).toBeInTheDocument()
  expect(within(section).getByText('Execution safety')).toBeInTheDocument()
  expect(within(section).getByText(/could duplicate or drop rows/i)).toBeInTheDocument()
  expect(within(section).getByText(/predictive usefulness and production execution are not proven/i))
    .toBeInTheDocument()
  // the card heading sits BELOW the dossier section heading, not beside it
  expect(within(section).getByRole('heading', { level: 4, name: 'account_balance_avg_30d' }))
    .toBeInTheDocument()
  await userEvent.click(within(section).getByRole('button', { name: /show full detail/i }))
  const drawer = within(section).getByRole('group', {
    name: /full detail for account_balance_avg_30d/i,
  })
  expect(within(drawer).getByText('Suggestion id')).toBeInTheDocument()
  expect(within(drawer).getByText('measured quantity')).toBeInTheDocument()
  expect(within(drawer).getByText('profile unavailable')).toBeInTheDocument()
})

it('threads the heading level one deeper here: section h3 → card h4 → drawer h5', async () => {
  // The dossier's own section heading is already h3, so a drawer with FIXED h4 sections would put
  // the audit material at the same level as the card — telling a screen-reader user it sits beside
  // the suggestion rather than inside it.
  getTableSuggestions.mockResolvedValue(withHits([usingBalance()]))
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  expect(
    within(section).getByRole('heading', { level: 3, name: /suggested features using this column/i }),
  ).toBeInTheDocument()
  expect(await within(section).findByRole('heading', { level: 4, name: 'account_balance_avg_30d' }))
    .toBeInTheDocument()
  await userEvent.click(within(section).getByRole('button', { name: /show full detail/i }))
  const drawer = within(section).getByRole('group', { name: /full detail for/i })
  const tags = within(drawer).getAllByRole('heading').map(h => h.tagName)
  expect(tags.length).toBeGreaterThan(1)
  expect([...new Set(tags)]).toEqual(['H5'])
  expect(within(drawer).getByRole('heading', { level: 5, name: 'Meaning' })).toBeInTheDocument()
})

it('bounds the drawer’s accessible name on this surface too', async () => {
  const long = `q_${'x'.repeat(400)}`
  getTableSuggestions.mockResolvedValue(withHits([usingBalance({ display_name: long })]))
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  await within(section).findByText(long)
  await userEvent.click(within(section).getByRole('button', { name: /show full detail/i }))
  const drawer = within(section).getByRole('group', { name: /full detail for/i })
  expect((drawer.getAttribute('aria-label') ?? '').length).toBeLessThanOrEqual(120)
  expect(within(section).getByRole('heading', { name: long })).toBeInTheDocument()
})

it('is read-only here too: the only control is the disclosure', async () => {
  getTableSuggestions.mockResolvedValue(withHits([usingBalance()]))
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  const buttons = within(section).getAllByRole('button')
  expect(buttons).toHaveLength(1)
  expect(buttons[0]).toHaveAttribute('aria-expanded')
  expect(within(section).queryByRole('button', { name: /accept|dismiss|edit/i })).toBeNull()
})

it('clears the section and refetches when the session visibility claims change', async () => {
  getTableSuggestions.mockResolvedValue(withHits([usingBalance()]))
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  expect(await within(section).findByText('account_balance_avg_30d')).toBeInTheDocument()
  expect(getTableSuggestions).toHaveBeenCalledTimes(1)
  // the next read never resolves: a card still on screen would mean a URL-keyed cache
  getTableSuggestions.mockReturnValue(new Promise(() => {}))
  await act(async () => {
    setSession({ user: 'dev', roles: ['data_owner', 'pii_reader'] })
  })
  expect(within(section).queryByText('account_balance_avg_30d')).toBeNull()
  expect(getTableSuggestions).toHaveBeenCalledTimes(2)
})

it('says which suggestions exist when none uses this column — not a bare empty', async () => {
  getTableSuggestions.mockResolvedValue(withHits([
    suggestionHit({
      suggestion_id: 'sug-other', name: 'other', display_name: 'other',
      operands: [{
        catalog_source: 'deposits', logical_ref: 'deposits.accounts.opened_at',
        graph_object_ref: 'public.accounts.opened_at', table_ref: 'accounts',
        recipe_role: 'as_of', classification: 'time',
        visibility_requires_current: [], evidence_refs: [],
      }],
    }),
  ], 3))
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

it('says the deployment does not serve the discovery contract, distinctly from a failure', async () => {
  getTableSuggestions.mockRejectedValue(new api.ApiError(
    422, 'unsupported contract_version 2', null, api.SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION))
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  expect(await within(section).findByRole('status'))
    .toHaveTextContent(/does not serve the discovery contract/i)
  expect(within(section).queryByRole('alert')).toBeNull()
})

it('a non-403 failure is an error, not an invented empty state', async () => {
  getTableSuggestions.mockRejectedValue(new api.ApiError(500, 'boom'))
  await renderDossier(detail())
  const section = await screen.findByTestId('column-suggestions')
  expect(await within(section).findByRole('alert')).toHaveTextContent(/could not load suggestions/i)
})

// ── the finer LLM axis beside the coarse source domain ───────────────────────────────────────────

it('shows the sub-domain as its own axis, with the author of the value', async () => {
  // `sub_domain` reached `effective_metadata` with the rest of the axes and was then dropped by
  // the Overview's own field list, so the LLM's finer classification existed in the payload and
  // appeared on no screen.
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.sub_domain = {
      value: 'Operational Metadata', authority: 'hint', c1_status: 'no_decision',
      provenance: null, evidence_provenance: 'AI proposed', selected_evidence_ids: [],
      proposed_value: null,
    }
  }))
  const row = screen.getByTestId('axis-sub_domain')
  expect(within(row).getByText('Sub-domain')).toBeInTheDocument()
  expect(within(row).getByText('Operational Metadata')).toBeInTheDocument()
  expect(within(row).getByText('AI proposed')).toBeInTheDocument()
})

it('a sub-domain nobody proposed reads "nothing known yet", never a blank row', async () => {
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.sub_domain = {
      value: null, authority: 'missing', c1_status: 'no_decision',
      provenance: null, evidence_provenance: null, selected_evidence_ids: [],
      proposed_value: null,
    }
  }))
  expect(within(screen.getByTestId('axis-sub_domain')).getByText('nothing known yet'))
    .toBeInTheDocument()
})

it('omits the sub-domain row entirely when the server does not send the field', async () => {
  // A deployment whose backend predates the axis must look exactly like today's, not like a
  // column whose sub-domain is missing.
  await renderDossier(detail())
  expect(screen.queryByTestId('axis-sub_domain')).toBeNull()
})

// ── the AI's search terms ────────────────────────────────────────────────────────────────────────

it('shows the AI-drafted search terms as separate terms, each with its author', async () => {
  await renderDossier(detail(d => {
    d.evidence!.proposals_by_field.semantic_terms = {
      active: [{
        evidence_id: 'ev-syn', producer: 'llm', strength: 'proposed',
        proposed_value: 'audit user, created by, record creator, data steward',
        confidence_band: null,
      }],
    }
  }))
  const section = screen.getByTestId('search-terms')
  // Separate terms, not one run-on line: after Task 4c there are 15–20 of them.
  expect(within(section).getByText('audit user')).toBeInTheDocument()
  expect(within(section).getByText('created by')).toBeInTheDocument()
  expect(within(section).getByText('record creator')).toBeInTheDocument()
  expect(within(section).getByText('data steward')).toBeInTheDocument()
  // Who drafted them, at the point they are read — in the word the rest of the platform uses for a
  // `llm`/`proposed` evidence row (`asset_detail._EVIDENCE_PROVENANCE_LABELS`), not a third
  // synonym for it.
  expect(within(section).getByText('AI proposed')).toBeInTheDocument()
  // And never framed as a fault.
  expect(section.textContent).not.toMatch(/blocked|invalid|failed|error/i)
})

it('says a column has no search terms rather than showing an empty list', async () => {
  await renderDossier(detail())
  const section = screen.getByTestId('search-terms')
  expect(within(section).getByText(/no search terms are recorded/i)).toBeInTheDocument()
})

it('does not present a retired draft as a current search term', async () => {
  await renderDossier(detail(d => {
    d.evidence!.proposals_by_field.semantic_terms = {
      active: [],
      stale: [{
        evidence_id: 'ev-old', producer: 'llm', strength: 'proposed',
        proposed_value: 'former alias', confidence_band: null,
      }],
    }
  }))
  const section = screen.getByTestId('search-terms')
  expect(within(section).queryByText('former alias')).toBeNull()
  expect(within(section).getByText(/no longer active/i)).toBeInTheDocument()
})

it('renders no search-terms section at all when the evidence section was not served', async () => {
  await renderDossier(detail(d => { delete d.evidence }))
  expect(screen.queryByTestId('search-terms')).toBeNull()
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
