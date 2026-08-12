import { readFileSync } from 'node:fs'
import { act, render, screen, waitFor, within } from '@testing-library/react'
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
    getTableSuggestionsV4: vi.fn(),
    getAssetProfile: vi.fn(),
  }
})
const getAssetDetail = vi.mocked(api.getAssetDetail)
const getTableSuggestions = vi.mocked(api.getTableSuggestionsV2)
const getTableSuggestionsV4 = vi.mocked(api.getTableSuggestionsV4)
const getAssetProfile = vi.mocked(api.getAssetProfile)

const BASE_SESSION = getSession()

beforeEach(() => {
  getAssetDetail.mockReset()
  getTableSuggestions.mockReset()
  getTableSuggestions.mockResolvedValue(suggestionsFixture())
  getTableSuggestionsV4.mockReset()
  // Default: the older-backend answer — the screen steps down to v2, so every existing test
  // keeps exercising the page exactly as before. v4-specific tests override per-case.
  getTableSuggestionsV4.mockRejectedValue(new api.ApiError(
    422, 'unsupported contract_version 4; this deployment serves [1, 2, 3]', null,
    api.SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION,
  ))
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
  // the card's own semantic and warning vocabulary, not a stripped copy. Category and family
  // live in Full recommendation detail now, so they are asserted after it is opened below.
  expect(within(section).getByText('account_balance_avg_30d')).toBeInTheDocument()
  // The caveat moved off every card and onto the panel header, once. The guarantee is unchanged
  // -- a reader meets it before the badges -- and the badges now name both states of the axis
  // themselves, which is what made the per-card paragraph redundant.
  expect(within(section).getByText(
    /a design check tests the inputs, not whether the feature predicts anything/i,
  )).toBeInTheDocument()
  // the card heading sits BELOW the dossier section heading, not beside it
  expect(within(section).getByRole('heading', { level: 4, name: 'account_balance_avg_30d' }))
    .toBeInTheDocument()
  await userEvent.click(within(section).getByRole('button', { name: /show full detail/i }))
  // Generation source moved into the detail's Classification section when the card head was
  // reduced to the name. Same guarantee: a reader can always learn this is a recipe suggestion.
  expect(within(section).getByText('suggested · recipe')).toBeInTheDocument()
  expect(within(section).getAllByText('Trend & Trajectory').length).toBeGreaterThan(0)
  expect(within(section).getAllByText('Execution safety').length).toBeGreaterThan(0)
  expect(within(section).getAllByText(/could duplicate or drop rows/i).length)
    .toBeGreaterThan(0)
  expect(within(section).getByText('Liquidity')).toBeInTheDocument()
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

it('renders the sub-domain axis when the server DOES send it', async () => {
  // The positive half of the axis, which nothing covered: `main`'s test asserts the row is ABSENT
  // for an older backend, and the dossier fixture never sends the field — so the axis could have
  // been dropped by the Overview restructure and every test would still have passed. It nearly was.
  //
  // Absence is keyed on the KEY, not the value: a backend that sends `sub_domain: null` has an
  // opinion (nobody has classified it yet) and must get a row saying so.
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.sub_domain = {
      value: 'Sanctions Screening', authority: 'hint', c1_status: 'proposed',
      provenance: 'llm_proposed', evidence_provenance: null, selected_evidence_ids: [],
    }
  }))
  const row = screen.getByTestId('axis-sub_domain')
  expect(row).toBeInTheDocument()
  expect(within(row).getByText('Sanctions Screening')).toBeInTheDocument()
})

it('renders the sub-domain row when the server sends the key with a NULL value', async () => {
  // "The backend has never heard of this axis" and "the backend knows the axis and nobody has
  // classified this column" are different facts, and only the first may hide the row.
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.sub_domain = {
      value: null, authority: 'missing', c1_status: 'unset',
      provenance: null, evidence_provenance: null, selected_evidence_ids: [],
    }
  }))
  expect(screen.getByTestId('axis-sub_domain')).toBeInTheDocument()
})

it('omits the sub-domain row entirely when the server does not send the field', async () => {
  // A deployment whose backend predates the axis must look exactly like today's, not like a
  // column whose sub-domain is missing.
  await renderDossier(detail())
  expect(screen.queryByTestId('axis-sub_domain')).toBeNull()
})

// ── the AI's search terms ────────────────────────────────────────────────────────────────────────

it('shows the AI-proposed search terms as separate terms, each with its author', async () => {
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

it('counts a draft retired into any lifecycle, not only into stale', async () => {
  // `stale` is the only bucket reachable for this field TODAY (`_reconcile_llm_field_evidence` →
  // `stale_all_llm_field_evidence`; `superseded` is human-producer-scoped; `rejected` needs
  // `apply_field_decision`, which 400s for `semantic_terms` — it has no `field_policies` entry).
  // But `_evidence_section` creates a bucket for whatever lifecycle a row carries, so reading one
  // name would turn a future lifecycle into a clean "nothing was ever drafted" claim over drafts
  // that do exist.
  await renderDossier(detail(d => {
    d.evidence!.proposals_by_field.semantic_terms = {
      active: [],
      quarantined: [{
        evidence_id: 'ev-q', producer: 'llm', strength: 'proposed',
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
    // The dossier reads in three tiers: the verdict strip, then the reasoning cards, then the
    // receipts. Technical identity is LAST — it used to be first, which put the object/logical/
    // graph refs in the most valuable position on the page and the business meaning below them.
    const order = [
      'Potential uses',
      'Business meaning',
      'From the source glossary',
      'Operational semantics',
      'Trust and coverage',
      'Suggested features using this column',
      'Technical identity',
    ].map(h => text.indexOf(h))
    for (const idx of order) expect(idx).toBeGreaterThan(-1)
    for (let i = 1; i < order.length; i++) expect(order[i]).toBeGreaterThan(order[i - 1])
  })
})


// ── the three flag-gated panels are MOUNTED by the page ───────────────────────────────────────────

it('mounts the asset-profile panel on the dossier', async () => {
  // Proof of WIRING rather than of rendering: the panel self-gates to table assets and renders
  // NOTHING on a 404, so a DOM assertion cannot tell "not mounted" from "flag off". Its read
  // effect firing can — if the page does not mount it, `getAssetProfile` is never called.
  //
  // The rejection is the flag-off path, so this asserts the wiring without asserting any markup
  // the panel's own test already owns.
  getAssetProfile.mockRejectedValue(new api.ApiError(404, 'not found'))
  await renderDossier(detail(d => { d.identity.kind = 'table' }))
  await waitFor(() => expect(getAssetProfile).toHaveBeenCalled())
})

it('keeps all three flag-gated panels wired into the overview', async () => {
  // Structural, deliberately: these panels self-gate on a 404 (flag-off renders nothing), so a
  // DOM assertion cannot distinguish "not wired" from "flag off" for the two without a mockable
  // read. What must never silently become true is that the page stopped referencing them at all —
  // which is exactly what happened.
  const overview = readFileSync('src/screens/AssetDetailOverview.tsx', 'utf8')
  for (const panel of ['AssetProfilePanel', 'DatasetPolicyPanel', 'CatalogNarrativePanel']) {
    expect(overview).toContain(`<${panel}`)
  }
})


it('gives the search-terms card the full grid width', async () => {
  // It carries 15-20 chips (Task 4c widened the synonyms ask) plus a four-line rationale. In a
  // half-width cell the chip row overflowed the card and the paragraph was clipped mid-sentence —
  // visible on the deployed page, invisible to jsdom, which does not lay anything out. Asserting
  // the CLASS is what a test can honestly check: `.adg-card--full` is `grid-column: 1 / -1`.
  await renderDossier(detail(d => {
    d.evidence!.proposals_by_field.semantic_terms = {
      active: [{
        evidence_id: 'ev-syn', producer: 'llm', strength: 'proposed',
        proposed_value: 'customer id, customer number, cust number, client id, customer identifier',
        confidence_band: null,
      }],
    }
  }))
  expect(await screen.findByTestId('search-terms')).toHaveClass('adg-card--full')
})


// ── review fixes: the three verified defects from the 2026-08-09 UX review ────────────────────────

it('the readiness action reads as an ACTION, not as a verdict', async () => {
  // It is a <button> that swaps to the Readiness tab, but it was labelled "Full readiness" in
  // `.btn--ghost` — transparent border AND background at rest, so it rendered as plain grey text.
  // Two failures at once: no resting affordance, and a label that reads as a STATUS claim which
  // contradicted the strip above it ("2 of 5 potential uses") for this very column.
  await renderDossier(detail())
  // Scoped to the CARD: the tab strip also has a "Readiness" button, and an ambiguous query here
  // would fail for a reason that has nothing to do with what this test is about.
  const card = screen.getByTestId('capabilities')
  const action = within(card).getByRole('button', { name: 'View readiness' })
  expect(action).toHaveClass('btn--link')       // the page's own underlined-action style
  expect(within(card).queryByText('Full readiness')).toBeNull()
})

it('counts of one are not labelled in the plural', async () => {
  // "1 Direct relationships". The suggestion card already pluralises its operand count, so the
  // codebase knows the rule; the stat strip did not apply it.
  await renderDossier(detail(d => {
    d.relationships!.approved_joins = []
    d.relationships!.cross_catalog = [{
      relationship_ref: 'r1', kind: 'crosswalk', status: 'confirmed',
      object_ref: 'public.other.col', catalog_source: 'ftr',
    } as unknown as never]
  }))
  const strip = screen.getByLabelText('Asset decision summary')
  expect(within(strip).getByText('Direct relationship')).toBeInTheDocument()
  expect(within(strip).queryByText('Direct relationships')).toBeNull()
})


it('the two DESIGN states are not painted the same colour', async () => {
  // `SuggestionCard.STATUS_TONE` maps DESIGN_CHECKED -> `gj-none` and design-NOT-checked ->
  // `gj-partial` on purpose. A single CSS rule listed BOTH selectors together and painted them the
  // same amber, so the two opposite verdicts were indistinguishable on a grid of cards — the one
  // signal a reader triaging them needs.
  //
  // Structural because jsdom computes no styles: the class assignment was always correct, so no
  // DOM assertion could ever have caught this. The stylesheet is the artifact under test.
  const css = readFileSync('src/index.css', 'utf8')
  expect(css).not.toMatch(/\.sfc-status \.badge\.gj-partial,\s*\n\.sfc-status \.badge\.gj-none/)
  const none = css.match(/\.sfc-status \.badge\.gj-none \{[^}]*\}/)?.[0] ?? ''
  const partial = css.match(/\.sfc-status \.badge\.gj-partial \{[^}]*\}/)?.[0] ?? ''
  expect(none).toBeTruthy()
  expect(partial).toBeTruthy()
  expect(none).not.toEqual(partial)
  expect(partial).toContain('--warn')       // not-checked keeps the amber
  expect(none).not.toContain('--warn')      // checked goes quiet
  expect(none).not.toContain('--ok')        // …but never the success fill, on either
  expect(partial).not.toContain('--ok')
})


// ── typography: monospace means "a literal machine value", and nothing else ───────────────────────

const axisField = (value: string) => ({
  value, authority: 'hint', c1_status: 'proposed',
  provenance: 'llm_proposed', evidence_provenance: null, selected_evidence_ids: [],
})

it('reserves monospace for the registry identifier and not for a business label', async () => {
  // `customer_id` is a snake_case registry name a reader matches on: exact, copyable, machine
  // facing. `Customer` is a domain label nobody pastes anywhere. Rendering both as code made the
  // distinction invisible, which is the whole reason monospace exists on this page.
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.concept = axisField('customer_id') as never
    d.effective_metadata!.fields.domain = axisField('Customer') as never
  }))
  expect(within(screen.getByTestId('axis-concept')).getByText('customer_id')).toHaveClass('mono')
  expect(within(screen.getByTestId('axis-domain')).getByText('Customer')).not.toHaveClass('mono')
})

it('sets the capability role labels as prose', async () => {
  // "Measure", "Grain key" and friends are UI labels this interface chose, not values the catalog
  // holds. The symptom was visible on the page: "Measure" rendered monospace while "Catalog
  // ingestion", the same size and weight in the card beside it, rendered sans.
  await renderDossier(detail())
  const role = within(screen.getByTestId('capabilities')).getByText('Measure')
  expect(role).not.toHaveClass('mono')
})

it('does not make EVERY field value monospace at the stylesheet level', () => {
  // The root cause, and it is not reachable from the DOM: `.adg-field-value` set
  // `font-family: var(--font-mono)` for every value, so an absence message ("nothing known yet")
  // and an identifier (`customer_id`) were typographically identical no matter what classes the
  // JSX applied. jsdom computes no stylesheet, so the stylesheet is the artifact under test.
  const css = readFileSync('src/index.css', 'utf8')
  const base = css.match(/\.adg-field-value \{[^}]*\}/)?.[0] ?? ''
  expect(base).toBeTruthy()
  expect(base).not.toContain('--font-mono')
  // …and the opt-in still exists, so a genuine literal can ask for it.
  expect(css).toMatch(/\.adg-field-value\.mono \{[^}]*--font-mono/)
})


// ── authority colour must not over-claim ─────────────────────────────────────────────────────────

it('does not paint a source PROPOSAL in the same tone as a source ATTESTATION', async () => {
  // `SourceGlossaryField.provenance` is "source attested" OR "source proposed" — two different
  // strengths — and the card hardcoded `gj-verified` for both. On a product whose whole value is
  // knowing who asserted what, painting a proposal with the attested tone is the worst class of
  // error available: it over-claims authority, and it does so silently.
  await renderDossier(detail(d => {
    d.source_glossary = { fields: {
      business_term: { value: 'Customer Number', provenance: 'source attested' },
      // `term_type`, not `domain`: only keys in `GLOSSARY_FIELDS` render, and `domain` is not one
      // of them — a variant the card never draws proves nothing.
      term_type: { value: 'Business term', provenance: 'source proposed' },
    } } as never
  }))
  const card = screen.getByTestId('source-glossary')
  const attested = within(card).getByText('source attested')
  const proposed = within(card).getByText('source proposed')
  expect(attested).toHaveClass('gj-verified')
  expect(proposed).not.toHaveClass('gj-verified')
})


// ── the badge's colour agrees with its own words ─────────────────────────────────────────────────

const attestedField = (over: Record<string, unknown> = {}) => ({
  value: 'x', authority: 'hint', c1_status: 'confirmed',
  provenance: null, evidence_provenance: 'source attested', selected_evidence_ids: [], ...over,
})

it('colours an attestation green even when its operational authority is only a hint', async () => {
  // THE REPORTED CONFUSION. The badge used to take its tone from the OPERATIONAL axis
  // (`governed`/`hint`/`missing`) while taking its WORDS from provenance, so "source attested"
  // rendered teal in one card and green in another and read as arbitrary. Colour now follows the
  // words. `authority: 'hint'` here is the case that used to force teal.
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.entity = attestedField() as never
  }))
  const row = screen.getByTestId('axis-entity')
  expect(within(row).getByText('source attested')).toHaveClass('gj-verified')
})

it('colours a proposal teal, never green', async () => {
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.entity =
      attestedField({ evidence_provenance: 'llm proposed', authority: 'governed' }) as never
  }))
  const row = screen.getByTestId('axis-entity')
  const badge = within(row).getByText('llm proposed')
  expect(badge).toHaveClass('gj-proposed')
  expect(badge).not.toHaveClass('gj-verified')
})

it('leaves an unattested value quiet', async () => {
  await renderDossier(detail(d => {
    d.effective_metadata!.fields.entity =
      attestedField({ evidence_provenance: null, provenance: null, authority: 'governed' }) as never
  }))
  const row = screen.getByTestId('axis-entity')
  expect(within(row).getByText('unattested')).toHaveClass('gj-none')
})


it('does not dress the search terms as authority claims', async () => {
  // The terms are DATA — the words the model drafted — not statements about who vouched for them.
  // They carried `gj-proposed`, the same class and the same fill as the "AI proposed" badge sitting
  // beneath them, so eight content chips were indistinguishable from an authority verdict. That
  // undoes the rule the badge tones were just unified under: a tone means authority strength, and
  // it can only mean that if nothing else wears it.
  //
  // The card states the authority ONCE, in its own badge and its rationale. The chips do not each
  // need to re-assert it.
  await renderDossier(detail(d => {
    d.evidence!.proposals_by_field.semantic_terms = {
      active: [{
        evidence_id: 'ev-syn', producer: 'llm', strength: 'proposed',
        proposed_value: 'customer id, client id', confidence_band: null,
      }],
    }
  }))
  const card = screen.getByTestId('search-terms')
  const term = within(card).getByText('customer id')
  const authority = within(card).getByText('AI proposed')
  expect(authority).toHaveClass('gj-proposed')
  expect(term).not.toHaveClass('gj-proposed')
})


it('does not dress a COUNT as an authority claim', async () => {
  // Same class of defect as the search terms, found by sweeping every authority-tone use rather
  // than fixing the one that was reported. "2 populated · 7 unknown" is a tally of the card's own
  // rows. It wore `gj-proposed`, so a summary count was indistinguishable from an AI proposal
  // sitting a few pixels below it.
  await renderDossier(detail())
  const count = within(screen.getByTestId('operational-semantics')).getByText(/populated ·/)
  expect(count).not.toHaveClass('gj-proposed')
})
