import { act, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { getSession, setSession } from '../session'
import { SuggestedFeaturesScreen } from './SuggestedFeaturesScreen'
import { NEEDS_VALIDATION, evidence, hit, label, operand, page, text }
  from './SuggestedFeaturesScreen.fixture'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getTableSuggestionsV2: vi.fn() }
})
const getTableSuggestionsV2 = vi.mocked(api.getTableSuggestionsV2)

const SOURCE = 'core_banking'
const TABLE = 'public.comp_fin_tran'

const BASE_SESSION = getSession()

beforeEach(() => {
  getTableSuggestionsV2.mockReset()
  setSession({ user: 'dev', roles: ['data_owner'] })
})
afterEach(() => setSession(BASE_SESSION))

function renderScreen() {
  render(<SuggestedFeaturesScreen source={SOURCE} table={TABLE} />)
}

async function openDetail(name: string): Promise<HTMLElement> {
  const card = (await screen.findByText(name)).closest('li') as HTMLElement
  await userEvent.click(within(card).getByRole('button', { name: /show full detail/i }))
  return card
}

describe('SuggestedFeaturesScreen', () => {
  // ── the contract it asks for ──────────────────────────────────────────────────────────────────
  it('asks for the v2 discovery contract explicitly, never by sniffing a v1 body', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    await screen.findByText('account_balance_trend_90d')
    expect(getTableSuggestionsV2).toHaveBeenCalledWith(SOURCE, TABLE)
  })

  it('says the deployment does not serve the contract on the typed 422, not "broken"', async () => {
    getTableSuggestionsV2.mockRejectedValue(new api.ApiError(
      422, 'unsupported contract_version 2; this deployment serves [1]', null,
      api.SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION,
    ))
    renderScreen()
    expect(await screen.findByText(/does not serve the discovery contract/i)).toBeInTheDocument()
    // not an alert, and no invented empty-catalog diagnosis
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryByText(/business meaning a recipe needs/i)).not.toBeInTheDocument()
  })

  it('tolerates FastAPI’s native list-detail 422, which carries no error_code', async () => {
    // A NON-integer contract_version fails before the handler runs, so `detail` is a LIST and the
    // typed code is absent. It must degrade to the ordinary error path, never crash.
    getTableSuggestionsV2.mockRejectedValue(new api.ApiError(
      422, 'query.contract_version: Input should be a valid integer',
    ))
    renderScreen()
    expect(await screen.findByRole('alert')).toHaveTextContent(/valid integer/i)
    expect(screen.queryByText(/does not serve the discovery contract/i)).not.toBeInTheDocument()
  })

  // ── the copy that was load-bearing wrong ──────────────────────────────────────────────────────
  it('counts design checked, never "clean & ready", and explains what design checked is not',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page({
        summary: { suggested: 14, design_checked: 11, needs_external_validation: 3, groups: 3 },
      }))
      renderScreen()
      const summary = await screen.findByRole('group', { name: /suggestion summary/i })
      expect(summary).toHaveTextContent('14 suggested')
      expect(summary).toHaveTextContent('11 design checked')
      expect(summary).toHaveTextContent('3 need external validation')
      expect(summary).toHaveTextContent('3 entities')
      expect(document.body.textContent).not.toMatch(/clean & ready/i)
      expect(document.body.textContent).not.toMatch(/\bready\b(?! rules)/i)
      // the limit is stated, not implied
      expect(screen.getAllByText(/not proof that a feature predicts anything/i).length)
        .toBeGreaterThan(0)
      expect(screen.getByText(/not proof that it can run in production/i)).toBeInTheDocument()
    })

  it('repeats the design-checked limit on the card itself, where the badge could mislead',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page())
      renderScreen()
      const checked = (await screen.findByText('account_balance_trend_90d')).closest('li')!
      expect(within(checked).getByText('design checked')).toBeInTheDocument()
      expect(within(checked).getByText(/predictive usefulness and production execution are not proven/i))
        .toBeInTheDocument()
      // the card that is NOT design checked does not carry the clarification of a word it never uses
      const review = screen.getByText('customer_inflow_30d').closest('li')!
      expect(within(review).getByText('needs external validation')).toBeInTheDocument()
      expect(within(review).queryByText(/predictive usefulness/i)).toBeNull()
    })

  it('names what is MISSING for an unsuggestable table, never an approval that is not a gate',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page({
        summary: { suggested: 0, design_checked: 0, needs_external_validation: 0, groups: 0 },
        groups: [], rejections: [],
      }, []))
      renderScreen()
      expect(await screen.findByText(/no suggestions yet/i)).toBeInTheDocument()
      expect(screen.getByText(/an entity to compute per/i)).toBeInTheDocument()
      // the stale instruction is GONE: confirmation is not a gate for grounding
      expect(document.body.textContent).not.toMatch(/confirmed on the semantics screen/i)
      expect(screen.getByText(/does not have to be confirmed first/i)).toBeInTheDocument()
      expect(screen.getByText(/what is\s+missing is a proposal/i)).toBeInTheDocument()
    })

  it('distinguishes suggested from registered in the page copy and on the card badge', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    expect(await screen.findByText(/none of them is in the feature registry/i)).toBeInTheDocument()
    const card = screen.getByText('account_balance_trend_90d').closest('li')!
    // the generation source IS the badge text: recipe, not LLM, not user-authored
    expect(within(card).getByText('suggested · recipe')).toBeInTheDocument()
  })

  // ── the compact card ──────────────────────────────────────────────────────────────────────────
  it('puts name, status, category, meaning, entity/grain, window/time and sources on the card',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page())
      renderScreen()
      const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
      expect(within(card).getByRole('heading', { name: 'account_balance_trend_90d' }))
        .toBeInTheDocument()
      expect(within(card).getByText('design checked')).toBeInTheDocument()
      expect(within(card).getByText('Trend & Trajectory')).toBeInTheDocument()
      expect(within(card).getByText(/balance has trended over the last 90 days/i)).toBeInTheDocument()
      expect(within(card).getByText(/leads attrition and hardship/i)).toBeInTheDocument()
      expect(within(card).getByText('What it measures')).toBeInTheDocument()
      expect(within(card).getByText('Why it is useful')).toBeInTheDocument()
      expect(within(card).getByText('Entity and grain')).toBeInTheDocument()
      expect(within(card).getByText('acct_id')).toBeInTheDocument()
      expect(within(card).getByText('trend_90d')).toBeInTheDocument()
      expect(within(card).getByText('90d')).toBeInTheDocument()
      expect(within(card).getByText('as_of_dt')).toBeInTheDocument()
      expect(within(card).getByText(/1 table, 1 column/i)).toBeInTheDocument()
      expect(within(card).getByText('trend_90d(bal_amt) BY acct_id OVER 90d [as_of_dt]'))
        .toBeInTheDocument()
    })

  it('keeps the fine-grained recipe family OFF the compact card and inside the detail', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
    expect(within(card).queryByText('Balance trend')).toBeNull()
    await userEvent.click(within(card).getByRole('button', { name: /show full detail/i }))
    expect(within(card).getByText('Balance trend')).toBeInTheDocument()
    expect(within(card).getByText('Recipe family')).toBeInTheDocument()
  })

  it('lists the first domains and counts the rest rather than growing the card', async () => {
    const many = Array.from({ length: 5 }, (_u, i) =>
      label({ id: `d${i}`, display_name: `Domain ${i}` }))
    getTableSuggestionsV2.mockResolvedValue(page({}, [hit({ business_domains: many })]))
    renderScreen()
    const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
    expect(within(card).getByText('Domain 0')).toBeInTheDocument()
    expect(within(card).getByText('Domain 2')).toBeInTheDocument()
    expect(within(card).queryByText('Domain 3')).toBeNull()
    expect(within(card).getByText('+2 more')).toBeInTheDocument()
    // ...and the drawer carries every one WITH its provenance
    await userEvent.click(within(card).getByRole('button', { name: /show full detail/i }))
    expect(within(card).getByText('Domain 3')).toBeInTheDocument()
    expect(within(card).getAllByText('Business domain')).toHaveLength(5)
  })

  it('renders an absent controlled vocabulary as "not supplied", never as an omitted section',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page())
      renderScreen()
      const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
      expect(within(card).getByText('Business domains')).toBeInTheDocument()
      expect(within(card).getByText(/no controlled domain vocabulary is registered here/i))
        .toBeInTheDocument()
      expect(within(card).getByText('Use cases')).toBeInTheDocument()
      expect(within(card).getAllByText('not supplied').length).toBeGreaterThan(0)
    })

  it('renders an unclassified recipe and an absent business value honestly', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({}, [hit({
      feature_category: null, discovery_disposition: 'unclassified', business_value: null,
    })]))
    renderScreen()
    const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
    // the two absences are DIFFERENT facts and are worded apart: no category was mapped to this
    // recipe, and the recipe's discovery coverage summary is `unclassified`.
    expect(within(card).getByText('no category mapped yet')).toBeInTheDocument()
    expect(within(card).getByText('unclassified')).toBeInTheDocument()
    expect(within(card).getByText(/no business value has been written/i)).toBeInTheDocument()
  })

  // ── provenance ────────────────────────────────────────────────────────────────────────────────
  it('drives the category provenance badge off the family mapping, not off basis', async () => {
    // Both cards say basis=template_authored. Only the mapping citation distinguishes a taxonomy
    // DERIVATION from a value an author wrote, so a badge that read `basis` would call both the
    // same thing.
    getTableSuggestionsV2.mockResolvedValue(page({
      groups: [{
        entity: label({ id: 'account', display_name: 'account' }),
        contextual_entity_terms: [],
        grain_refs: [[SOURCE, 'public.comp_fin_tran.acct_id']],
        suggestion_ids: ['a', 'b'],
      }],
    }, [
      hit({ suggestion_id: 'a', display_name: 'authored_one' }),
      hit({
        suggestion_id: 'b', display_name: 'derived_one',
        feature_category_derived_from_family_mapping: true,
      }),
    ]))
    renderScreen()
    const authored = (await screen.findByText('authored_one')).closest('li')!
    const derived = screen.getByText('derived_one').closest('li')!
    expect(within(authored).getAllByText('recipe-authored').length).toBeGreaterThan(0)
    expect(within(authored).queryByText('derived from recipe family')).toBeNull()
    expect(within(derived).getByText('derived from recipe family')).toBeInTheDocument()
  })

  it('marks an AI-proposed value as proposed wherever it renders', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({}, [hit({
      use_cases: [label({
        id: 'attrition', display_name: 'Attrition', basis: 'llm_proposed',
        evidence: [evidence({ producer: 'llm', strength: 'proposed', producer_ref: null })],
      })],
    })]))
    renderScreen()
    const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
    expect(within(card).getByText('Attrition')).toBeInTheDocument()
    expect(within(card).getAllByText('AI-proposed').length).toBeGreaterThan(0)
  })

  // ── catalog terms are terms, not controlled labels ────────────────────────────────────────────
  it('shows unmapped catalog wording as catalog terms with authority, never as a business domain',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page({}, [hit({
        contextual_domain_terms: [text({ value: 'payments' })],
        contextual_entity_terms: [text({ value: 'counterparty' })],
      })]))
      renderScreen()
      const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
      // NOT on the controlled-domain row: the compact card still says the vocabulary is absent.
      expect(within(card).getByText(/no controlled domain vocabulary is registered here/i))
        .toBeInTheDocument()
      await userEvent.click(within(card).getByRole('button', { name: /show full detail/i }))
      const terms = within(card).getByTestId('sfc-catalog-terms')
      expect(within(terms).getByText(/not controlled business domains or entities/i))
        .toBeInTheDocument()
      expect(within(terms).getAllByText('payments').length).toBeGreaterThan(0)
      expect(within(terms).getAllByText('counterparty').length).toBeGreaterThan(0)
      // ...with the authority that produced the wording
      expect(within(terms).getAllByText(/from catalog wording/i).length).toBeGreaterThan(0)
      // the three evidence axes travel verbatim; an LLM proposal must not read like an attestation
      const axes = Array.from(terms.querySelectorAll('.sfc-evidence')).map(e => e.textContent)
      expect(axes.some(a => /llm · proposed · active/i.test(a ?? ''))).toBe(true)
    })

  // ── limitations, visible without opening anything ─────────────────────────────────────────────
  it('shows missing unit, currency, temporal, grain, join and near-label without opening a drawer',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page({}, [hit({
        validation_status: 'NEEDS_EXTERNAL_VALIDATION',
        requirements: [
          { code: 'GRAIN_IS_UNIQUE', operand: [SOURCE, 'public.comp_fin_tran.acct_id'], detail: '' },
          { code: 'JOIN_CONNECTIVITY', operand: [SOURCE, 'public.comp_fin_tran.cif_id'], detail: '' },
          { code: 'UNIT_CONSISTENT', operand: [SOURCE, 'public.comp_fin_tran.bal_amt'], detail: '' },
          { code: 'CURRENCY_CONSISTENT', operand: [SOURCE, 'public.comp_fin_tran.bal_amt'], detail: '' },
          { code: 'TEMPORAL_IS_POPULATED', operand: [SOURCE, 'public.comp_fin_tran.as_of_dt'], detail: '' },
        ],
        warnings: [
          { code: 'NEAR_LABEL', operand_refs: [], detail: 'x' },
          { code: 'MISSING_UNIT', operand_refs: [[SOURCE, 'public.comp_fin_tran.bal_amt']], detail: 'x' },
          { code: 'MISSING_CURRENCY', operand_refs: [[SOURCE, 'public.comp_fin_tran.bal_amt']], detail: 'x' },
          { code: 'MISSING_TEMPORAL_EVIDENCE', operand_refs: [[SOURCE, 'public.comp_fin_tran.as_of_dt']], detail: 'x' },
          { code: 'RELATIONSHIP_SAFETY_UNPROVEN', operand_refs: [[SOURCE, 'public.comp_fin_tran.cif_id']], detail: 'x' },
        ],
      })]))
      renderScreen()
      const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
      const list = within(card).getByRole('list', { name: /requirements and limitations/i })
      expect(within(list).getByText(/borders the outcome label/i)).toBeInTheDocument()
      expect(within(list).getByText(/no declared unit/i)).toBeInTheDocument()
      expect(within(list).getByText(/no declared currency/i)).toBeInTheDocument()
      expect(within(list).getByText(/no populated as-of date is declared/i)).toBeInTheDocument()
      expect(within(list).getByText(/needs a confirmed unique grain/i)).toBeInTheDocument()
      expect(within(list).getByText(/no governed-verified safety evidence/i)).toBeInTheDocument()
      // the drawer is still CLOSED
      expect(within(card).getByRole('button', { name: /show full detail/i }))
        .toHaveAttribute('aria-expanded', 'false')
      // and the count is prominent: 5 warnings + the one requirement no warning covers
      expect(within(card).getByText('6 limitations')).toBeInTheDocument()
    })

  it('says the same fact once: a requirement the server also raised as a code is not repeated',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page())
      renderScreen()
      const card = (await screen.findByText('customer_inflow_30d')).closest('li')!
      expect(within(card).getByText('1 limitation')).toBeInTheDocument()
      const list = within(card).getByRole('list', { name: /requirements and limitations/i })
      expect(within(list).getAllByRole('listitem')).toHaveLength(1)
      expect(within(list).getByText(/no declared unit/i)).toBeInTheDocument()
      // ...and the underlying typed requirement is still auditable in the drawer
      await userEvent.click(within(card).getByRole('button', { name: /show full detail/i }))
      expect(within(card).getByText('UNIT_CONSISTENT')).toBeInTheDocument()
    })

  it('keeps review accountability visually and semantically apart from execution safety',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page({}, [hit({
        warnings: [
          { code: 'RELATIONSHIP_UNCONFIRMED', operand_refs: [], detail: 'x' },
          { code: 'RELATIONSHIP_SAFETY_UNPROVEN', operand_refs: [], detail: 'x' },
          { code: 'DIRECTIONAL_CARDINALITY_UNAVAILABLE', operand_refs: [], detail: 'x' },
        ],
      })]))
      renderScreen()
      const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
      const rows = within(card).getByRole('list', { name: /requirements and limitations/i })
      const items = within(rows).getAllByRole('listitem')
      expect(items[0]).toHaveTextContent('Review')
      expect(items[0]).toHaveTextContent(/confirmed by nobody/i)
      expect(items[0].className).toContain('sfc-lim--review')
      expect(items[1]).toHaveTextContent('Execution safety')
      expect(items[1].className).toContain('sfc-lim--safety')
      expect(items[2]).toHaveTextContent('Execution safety')
      expect(items[2].className).toContain('sfc-lim--safety')
      // ...and the classes are DIFFERENT, not one shared relationship tone
      expect(items[0].className).not.toBe(items[1].className)
    })

  it('never relies on colour alone: every limitation row names its class in words', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({}, [hit({
      warnings: [
        { code: 'SENSITIVE_INPUT', operand_refs: [], detail: 'x' },
        { code: 'PROFILE_PROPOSED', operand_refs: [], detail: 'x' },
      ],
    })]))
    renderScreen()
    const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
    const items = within(card).getAllByRole('listitem')
      .filter(li => li.className.includes('sfc-lim'))
    for (const li of items) {
      expect(li.querySelector('.sfc-lim-class')?.textContent?.trim()).toBeTruthy()
      // the closed code travels beside the words, so prose is never the decision field
      expect(li.querySelector('.sfc-lim-code')?.textContent?.trim()).toMatch(/^[A-Z_]+$/)
    }
    expect(within(card).getByText('Data handling')).toBeInTheDocument()
    expect(within(card).getByText('Proposed context')).toBeInTheDocument()
  })

  it('says so explicitly when a suggestion has no limitations at all', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
    expect(within(card).getByText('no limitations recorded')).toBeInTheDocument()
  })

  // ── the expanded detail ───────────────────────────────────────────────────────────────────────
  it('opens an accessible drawer with every operand, role, dataset and revision', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({}, [hit({
      operands: [
        operand(),
        operand({
          graph_object_ref: 'public.comp_fin_tran.as_of_dt', recipe_role: 'as_of',
          classification: 'time', visibility_requires_current: ['pii'], evidence_refs: [],
        }),
      ],
      relationship_dependencies: [{
        relationship_ref: 'rel-1', relationship_kind: 'identifier_link',
        from_ref: [SOURCE, 'public.comp_fin_tran.cif_id'],
        to_ref: [SOURCE, 'public.cust.cif_id'],
        realization_content_hash: 'real-1', cardinality: 'unknown',
        safety_status: 'unverified', review_status: 'file_declared',
      }],
    })]))
    renderScreen()
    const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
    const toggle = within(card).getByRole('button', { name: /show full detail/i })
    expect(toggle).toHaveAttribute('aria-expanded', 'false')
    await userEvent.click(toggle)
    expect(within(card).getByRole('button', { name: /hide full detail/i }))
      .toHaveAttribute('aria-expanded', 'true')
    const drawer = within(card).getByRole('group', {
      name: /full detail for account_balance_trend_90d/i,
    })
    // aria-controls points at the region the button reveals
    expect(toggle).toHaveAttribute('aria-controls', drawer.id)
    // operands with their role AND recipe slot
    expect(within(drawer).getByText('measured quantity')).toBeInTheDocument()
    expect(within(drawer).getByText('time anchor')).toBeInTheDocument()
    expect(within(drawer).getByText(/recipe slot balance/i)).toBeInTheDocument()
    expect(within(drawer).getByText(/restricted: pii/i)).toBeInTheDocument()
    expect(within(drawer).getByText(/no evidence pin recorded/i)).toBeInTheDocument()
    // relationship legs, with the direction travelled and every unproven axis
    expect(within(drawer).getByText(/public.comp_fin_tran.cif_id → public.cust.cif_id/))
      .toBeInTheDocument()
    // Every axis of the leg reads as WORDS — this is the row a reader consults to find out which
    // join is unproven — with the server's own raw member beside it for audit.
    const leg = drawer.querySelector('.sfc-rel') as HTMLElement
    expect(within(leg).getByText(/not declared, so row multiplication is unknown/i))
      .toBeInTheDocument()
    expect(within(leg).getByText(/not cleared to run/i)).toBeInTheDocument()
    expect(within(leg).getByText(/declared by an upload, confirmed by nobody/i)).toBeInTheDocument()
    for (const raw of ['unknown', 'unverified', 'file_declared', 'identifier_link']) {
      expect(within(leg).getByText(raw)).toBeInTheDocument()
    }
    // revisions
    expect(within(drawer).getByText('Suggestion id')).toBeInTheDocument()
    expect(within(drawer).getByText('sug-1')).toBeInTheDocument()
    expect(within(drawer).getByText('rev-1')).toBeInTheDocument()
    expect(within(drawer).getByText('trace-1')).toBeInTheDocument()
    expect(within(drawer).getByText('d1')).toBeInTheDocument()
  })

  it('keys repeatable lists without collision: one column in two recipe slots, repeated words',
    async () => {
      // None of these lists is unique by value. A column bound to TWO recipe slots is the real
      // collision — React would drop one of the two operands and the drawer would under-report the
      // inputs, which is precisely the thing "every input column" promises not to do.
      const errors = vi.spyOn(console, 'error').mockImplementation(() => {})
      try {
        getTableSuggestionsV2.mockResolvedValue(page({}, [hit({
          keywords: [text({ value: 'balance' }), text({ value: 'balance' })],
          authoring_notes: [text({ value: 'check the sign' }), text({ value: 'check the sign' })],
          operands: [
            operand({ recipe_role: 'balance' }),
            operand({ recipe_role: 'comparison' }),
          ],
        })]))
        renderScreen()
        const card = await openDetail('account_balance_trend_90d')
        // both operands survive — same column, two slots
        expect(within(card).getByText(/recipe slot balance/i)).toBeInTheDocument()
        expect(within(card).getByText(/recipe slot comparison/i)).toBeInTheDocument()
        const operands = card.querySelector('.sfc-operands') as HTMLElement
        expect(within(operands).getAllByText('bal_amt')).toHaveLength(2)
        expect(within(card).getAllByText('balance')).toHaveLength(2)
        expect(within(card).getAllByText('check the sign')).toHaveLength(2)
        // ...and React never warned about a duplicate key
        const warned = errors.mock.calls.some(args =>
          args.some(a => typeof a === 'string' && /same key/i.test(a)))
        expect(warned).toBe(false)
      } finally {
        errors.mockRestore()
      }
    })

  it('renders the honest unavailable state for source datasets and unconsumed context', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    const card = await openDetail('account_balance_trend_90d')
    expect(within(card).getByText('profile unavailable')).toBeInTheDocument()
    expect(within(card).getAllByText(/unavailable: no dataset profile has been produced/i))
      .toHaveLength(4)
    expect(within(card).getByText(/no semantic context was consumed/i)).toBeInTheDocument()
    expect(within(card).getByText(/no dataset profile was consumed/i)).toBeInTheDocument()
  })

  it('reports what the page truncated or withheld instead of quietly shortening it', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({
      omitted_counts: { operands: 3, use_cases: 2, withheld_missing_trace: 1 },
    }))
    renderScreen()
    const note = await screen.findByTestId('page-truncation')
    expect(note).toHaveTextContent(/3 input columns were not listed/i)
    expect(note).toHaveTextContent(/2 use cases were not listed/i)
    expect(note).toHaveTextContent(/1 candidate was withheld: the engine recorded no decision trace/i)
    // and the same tally is auditable from the card, labelled page-wide
    const card = await openDetail('account_balance_trend_90d')
    expect(within(card).getByText(/counted for the whole page/i)).toBeInTheDocument()
  })

  // ── currentness ───────────────────────────────────────────────────────────────────────────────
  it('never invents a "current" badge from a null projection', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    const note = await screen.findByTestId('currentness')
    expect(note).toHaveTextContent(/no card claims to be current or stale/i)
    expect(document.body.textContent).not.toMatch(/\bup to date\b/i)
    const card = await openDetail('account_balance_trend_90d')
    expect(within(card).getByText(/no stored projection/i)).toBeInTheDocument()
  })

  it('renders a stored projection’s stale state and its reason, not an invented freshness',
    async () => {
      // Release A always sends `projection: null`, so this path is the one a Release-B payload
      // takes. It is covered here because "stale" and "pending" are states the contract requires
      // the UI to render HONESTLY, and an untested branch is where a fabricated "current" hides.
      const projection: api.SuggestionProjectionState = {
        state: 'stale', scope_set_id: null, read_scope_key: 'scope-abc', scope_epoch: 3,
        target_fingerprint: 'fp-target', current_fingerprint: 'fp-built',
        generated_at: '2026-08-01T10:00:00Z',
        stale_reason: 'an input table changed since it was built',
        omitted_counts: {},
      }
      getTableSuggestionsV2.mockResolvedValue(page(
        {}, [{ ...hit(), projection }], { read_mode: 'projected', projection },
      ))
      renderScreen()
      expect(await screen.findByTestId('currentness'))
        .toHaveTextContent(/served from a stored projection, state stale/i)
      const card = await openDetail('account_balance_trend_90d')
      expect(within(card).getByText('stale')).toBeInTheDocument()
      expect(within(card).getByText(/an input table changed since it was built/i))
        .toBeInTheDocument()
      expect(within(card).getByText(/scope-abc · epoch 3/)).toBeInTheDocument()
      // ...and the null-projection copy is not ALSO claimed
      expect(document.body.textContent).not.toMatch(/no stored projection/i)
    })

  // ── read-only ─────────────────────────────────────────────────────────────────────────────────
  it('is strictly read-only: the only control is a disclosure, never accept/edit/dismiss',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page())
      renderScreen()
      await screen.findByText('account_balance_trend_90d')
      expect(screen.queryByRole('button', { name: /accept/i })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument()
      expect(screen.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument()
      expect(screen.queryAllByRole('textbox')).toHaveLength(0)
      // every button on the surface is a full-detail disclosure, and none is dead
      const buttons = screen.getAllByRole('button')
      expect(buttons).toHaveLength(2)
      for (const b of buttons) {
        expect(b).toHaveAttribute('aria-expanded')
        expect(b).toHaveAttribute('aria-controls')
      }
      expect(screen.getByText(/read-only/i)).toBeInTheDocument()
    })

  it('renders the binding-quality signal as the real value, never a fabricated percentage',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page())
      renderScreen()
      expect((await screen.findAllByText(/binding exact/i)).length).toBe(2)
      expect(screen.queryByText(/%/)).not.toBeInTheDocument()
      expect(document.querySelector('progress, meter')).toBeNull()
    })

  // ── read scope: identity is part of the request, not just the URL ─────────────────────────────
  it('clears results and refetches when the session’s visibility claims change', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    await screen.findByText('account_balance_trend_90d')
    expect(getTableSuggestionsV2).toHaveBeenCalledTimes(1)

    // The next read never resolves, so the ONLY way the old card could still be on screen is a
    // cache keyed on the URL — which is exactly what this surface forbids.
    getTableSuggestionsV2.mockReturnValue(new Promise(() => {}))
    await act(async () => {
      setSession({ user: 'dev', roles: ['data_owner', 'pii_reader'] })
    })
    expect(screen.queryByText('account_balance_trend_90d')).not.toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/reading what this catalog can build/i)
    expect(getTableSuggestionsV2).toHaveBeenCalledTimes(2)
  })

  it('clears results when the authenticated principal changes, not only the roles', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    await screen.findByText('account_balance_trend_90d')
    getTableSuggestionsV2.mockReturnValue(new Promise(() => {}))
    await act(async () => {
      setSession({ user: 'someone_else', roles: ['data_owner'] })
    })
    expect(screen.queryByText('account_balance_trend_90d')).not.toBeInTheDocument()
    expect(getTableSuggestionsV2).toHaveBeenCalledTimes(2)
  })

  it('does not refetch when the same claims are merely reordered', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    setSession({ user: 'dev', roles: ['data_owner', 'pii_reader'] })
    renderScreen()
    await screen.findByText('account_balance_trend_90d')
    await act(async () => {
      setSession({ user: 'dev', roles: ['pii_reader', 'data_owner'] })
    })
    expect(getTableSuggestionsV2).toHaveBeenCalledTimes(1)
  })

  // ── accessibility ─────────────────────────────────────────────────────────────────────────────
  it('reaches and operates the disclosure from the keyboard alone', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({}, [hit()]))
    renderScreen()
    await screen.findByText('account_balance_trend_90d')
    const toggle = screen.getByRole('button', { name: /show full detail/i })
    await userEvent.tab()
    expect(toggle).toHaveFocus()
    await userEvent.keyboard('{Enter}')
    expect(screen.getByRole('button', { name: /hide full detail/i }))
      .toHaveAttribute('aria-expanded', 'true')
    await userEvent.keyboard(' ')
    expect(screen.getByRole('button', { name: /show full detail/i }))
      .toHaveAttribute('aria-expanded', 'false')
  })

  it('gives every disclosure a distinct accessible name, and bounds it', async () => {
    const long = `q_${'x'.repeat(400)}`
    getTableSuggestionsV2.mockResolvedValue(page({
      groups: [{
        entity: label({ id: 'account', display_name: 'account' }),
        contextual_entity_terms: [],
        grain_refs: [[SOURCE, 'public.comp_fin_tran.acct_id']],
        suggestion_ids: ['sug-1', 'sug-2'],
      }],
    }, [hit(), hit({ suggestion_id: 'sug-2', display_name: long })]))
    renderScreen()
    await screen.findByText('account_balance_trend_90d')
    const names = screen.getAllByRole('button').map(b => b.getAttribute('aria-label') ?? '')
    expect(new Set(names).size).toBe(2)
    // an unbounded catalog string must not become a 400-character accessible name
    for (const n of names) expect(n.length).toBeLessThanOrEqual(120)
    // ...while the complete value is still on the page as text
    expect(screen.getByText(long)).toBeInTheDocument()
  })

  it('bounds the OPENED drawer’s accessible name too, not only the button’s', async () => {
    // The button and the region it controls are two different accessible names, and a region name
    // is announced on entry. An unbounded catalog display name would make it a 400-character
    // announcement.
    const long = `q_${'x'.repeat(400)}`
    getTableSuggestionsV2.mockResolvedValue(page({}, [hit({ display_name: long })]))
    renderScreen()
    const card = (await screen.findByText(long)).closest('li')!
    await userEvent.click(within(card).getByRole('button', { name: /show full detail/i }))
    const drawer = within(card).getByRole('group')
    const name = drawer.getAttribute('aria-label') ?? ''
    expect(name.length).toBeLessThanOrEqual(120)
    // it is still the SAME suggestion's region: a truncation, not a different string
    expect(name.startsWith('Full detail for q_xxx')).toBe(true)
    expect(name.endsWith('…')).toBe(true)
    // ...and the complete value is never lost — it is the card's own heading, in full, as text
    expect(within(card).getByRole('heading', { name: long })).toBeInTheDocument()
  })

  it('nests the drawer’s sections UNDER the card heading, never beside it', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({}, [hit()]))
    renderScreen()
    const card = await openDetail('account_balance_trend_90d')
    // group h2 → card h3 → drawer sections h4: one unbroken outline, so a screen-reader user
    // reading by heading level is told the audit material sits INSIDE the suggestion.
    expect(within(card).getByRole('heading', { level: 3, name: 'account_balance_trend_90d' }))
      .toBeInTheDocument()
    const drawer = within(card).getByRole('group')
    const tags = within(drawer).getAllByRole('heading').map(h => h.tagName)
    expect(tags.length).toBeGreaterThan(1)
    expect([...new Set(tags)]).toEqual(['H4'])
    expect(within(drawer).getByRole('heading', { level: 4, name: 'Meaning' })).toBeInTheDocument()
    expect(within(drawer).getByRole('heading', { level: 4, name: 'Requirements and limitations' }))
      .toBeInTheDocument()
  })

  it('bounds every tooltip while leaving the whole value readable in the detail', async () => {
    const wording = 'w'.repeat(500)
    getTableSuggestionsV2.mockResolvedValue(page({}, [hit({
      contextual_domain_terms: [text({ value: wording })],
      feature_category: label({ id: 'z'.repeat(300) }),
    })]))
    renderScreen()
    const card = await openDetail('account_balance_trend_90d')
    for (const el of Array.from(card.querySelectorAll('[title]'))) {
      expect((el.getAttribute('title') ?? '').length).toBeLessThanOrEqual(141)
    }
    expect(within(card).getAllByText(wording).length).toBeGreaterThan(0)
  })

  it('renders catalog and recipe prose as TEXT, never as markup', async () => {
    const hostile = '<img src=x onerror="alert(1)"> & <b>bold</b>'
    getTableSuggestionsV2.mockResolvedValue(page({}, [hit({
      business_interpretation: text({ value: hostile, basis: 'template_authored' }),
    })]))
    renderScreen()
    const card = (await screen.findByText('account_balance_trend_90d')).closest('li')!
    expect(within(card).getByText(hostile)).toBeInTheDocument()
    expect(card.querySelector('img')).toBeNull()
    expect(card.querySelector('b')).toBeNull()
  })

  it('gives the card a real heading and the group a level above it', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    expect(await screen.findByRole('heading', { level: 2, name: /account features/i }))
      .toBeInTheDocument()
    expect(screen.getByRole('heading', { level: 3, name: 'account_balance_trend_90d' }))
      .toBeInTheDocument()
  })

  // ── the states P4 already had, preserved ──────────────────────────────────────────────────────
  it('renders one group per entity: the heading is the entity, the suffix is its column', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    expect(await screen.findByRole('heading', { name: /account features/i })).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /customer features/i })).toBeInTheDocument()
    expect(screen.getByText(/per entity acct_id/i)).toBeInTheDocument()
    expect(screen.getByText(/per entity cif_id/i)).toBeInTheDocument()
  })

  it('shows only the column for a group whose entity the catalog could not name', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({
      summary: { suggested: 1, design_checked: 1, needs_external_validation: 0, groups: 1 },
      groups: [{
        entity: null, contextual_entity_terms: [],
        grain_refs: [[SOURCE, 'public.comp_fin_tran.cif_id']], suggestion_ids: ['sug-1'],
      }],
    }, [hit()]))
    renderScreen()
    expect(await screen.findByText('account_balance_trend_90d')).toBeInTheDocument()
    // no heading is invented from the column: 'cif_id features' would claim an unattested entity
    expect(screen.queryAllByRole('heading', { level: 2 })).toHaveLength(0)
    expect(screen.getByText(/per entity cif_id/i)).toBeInTheDocument()
  })

  it('renders no entity heading for a group with no grain ref at all', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({
      summary: { suggested: 1, design_checked: 1, needs_external_validation: 0, groups: 0 },
      groups: [{
        entity: null, contextual_entity_terms: [], grain_refs: [], suggestion_ids: ['sug-1'],
      }],
    }, [hit()]))
    renderScreen()
    expect(await screen.findByText('account_balance_trend_90d')).toBeInTheDocument()
    expect(screen.queryAllByRole('heading', { level: 2 })).toHaveLength(0)
    expect(screen.queryByText(/per entity/i)).not.toBeInTheDocument()
  })

  it('says the table does not exist rather than diagnosing its columns', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({
      table_known: false,
      summary: { suggested: 0, design_checked: 0, needs_external_validation: 0, groups: 0 },
      groups: [], rejections: [],
    }, []))
    renderScreen()
    expect(await screen.findByText(/no such table in this catalog/i)).toBeInTheDocument()
    expect(screen.getByText(SOURCE)).toBeInTheDocument()
    expect(screen.queryByText(/business meaning a recipe needs/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: /suggestion summary/i })).not.toBeInTheDocument()
    expect(screen.queryByTestId('neighbourhood')).not.toBeInTheDocument()
  })

  it('names the missing as-of cause and counts the features it blocks', async () => {
    const blocked = (n: number) => Array.from({ length: n }, (_unused, i) => ({
      template_id: `t${i}`, candidate_name: `blocked_${i}`,
      explanation: 'future-leakage risk: no point-in-time column', code: 'NO_POINT_IN_TIME',
    }))
    getTableSuggestionsV2.mockResolvedValue(page({
      summary: { suggested: 1, design_checked: 0, needs_external_validation: 1, groups: 1 },
      groups: [{
        entity: label({ id: 'account', display_name: 'account' }), contextual_entity_terms: [],
        grain_refs: [[SOURCE, 'public.comp_fin_tran.acct_id']], suggestion_ids: ['sug-2'],
      }],
      rejections: blocked(7),
    }, [NEEDS_VALIDATION]))
    renderScreen()
    expect(await screen.findByText(/7 features are blocked/i)).toBeInTheDocument()
    expect(screen.getByText(/no confirmed as-of column/i)).toBeInTheDocument()
    expect(screen.getByText(/under as-of date/i)).toBeInTheDocument()
  })

  it('treats zero design checked with cards present as the honest normal state, not an error',
    async () => {
      getTableSuggestionsV2.mockResolvedValue(page({
        summary: { suggested: 8, design_checked: 0, needs_external_validation: 8, groups: 1 },
        groups: [{
          entity: label({ id: 'account', display_name: 'account' }), contextual_entity_terms: [],
          grain_refs: [[SOURCE, 'public.comp_fin_tran.acct_id']], suggestion_ids: ['sug-2'],
        }],
      }, [NEEDS_VALIDATION]))
      renderScreen()
      expect(await screen.findByText(/nothing is design checked yet/i)).toBeInTheDocument()
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      const summary = screen.getByRole('group', { name: /suggestion summary/i })
      expect(summary.querySelector('.tone-danger')).toBeNull()
      expect(within(summary).getByText('0')).toBeInTheDocument()
    })

  it('lists refusals that are not the as-of cause under "Not offered"', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({
      rejections: [{
        template_id: 'card_util', candidate_name: 'card_utilisation',
        code: 'NO_NUMERIC_MEASURE', explanation: 'no column carries a numeric measure concept',
      }],
    }))
    renderScreen()
    expect(await screen.findByText('card_utilisation')).toBeInTheDocument()
    expect(screen.getByText(/no column carries a numeric measure concept/i)).toBeInTheDocument()
  })

  // ── what the page did NOT look at ─────────────────────────────────────────────────────────────
  it('says how many joined tables it showed of how many exist when it truncated', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({
      neighbourhood: {
        tables_considered: 20, tables_available: 73, truncated: true, max_hops: 1,
        limit_reason: 'table_cap',
      },
    }))
    renderScreen()
    const note = await screen.findByTestId('neighbourhood')
    expect(note).toHaveTextContent(/Showing 20 of 73 directly joined tables/i)
    expect(note).toHaveTextContent(/Deeper join paths were not automatically considered/i)
    expect(note).toHaveTextContent(/how many joined tables/i)
  })

  it('names the column budget when that is the limit that bit, not the table count', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({
      neighbourhood: {
        tables_considered: 3, tables_available: 41, truncated: true, max_hops: 1,
        limit_reason: 'column_budget',
      },
    }))
    renderScreen()
    expect(await screen.findByTestId('neighbourhood')).toHaveTextContent(/how many columns/i)
  })

  it('states the hop limit even when nothing was truncated', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({
      neighbourhood: {
        tables_considered: 4, tables_available: 4, truncated: false, max_hops: 1,
        limit_reason: null,
      },
    }))
    renderScreen()
    const note = await screen.findByTestId('neighbourhood')
    expect(note).toHaveTextContent(/all 4 directly joined tables/i)
    expect(note).toHaveTextContent(/within 1 join of this one/i)
    expect(note).not.toHaveTextContent(/Showing 4 of 4/i)
  })

  it('still states the hop limit when the payload reports NO neighbourhood at all', async () => {
    // `neighbourhood: null` is an absent fact, not permission to drop the paragraph. Omitting it
    // would turn the empty state below back into "nothing else is buildable here" when the truth
    // is "we did not look" — the exact claim this note exists to prevent.
    getTableSuggestionsV2.mockResolvedValue(page({ neighbourhood: null }))
    renderScreen()
    const note = await screen.findByTestId('neighbourhood')
    expect(note).toHaveTextContent(/join neighbourhood was not reported/i)
    expect(note).toHaveTextContent(/not as a statement about what deeper join paths could build/i)
  })

  it('says so plainly when no confirmed join reaches another table', async () => {
    getTableSuggestionsV2.mockResolvedValue(page())
    renderScreen()
    expect(await screen.findByTestId('neighbourhood'))
      .toHaveTextContent(/no confirmed join reaches another table/i)
  })

  it('takes the hop number from the payload rather than assuming one hop', async () => {
    getTableSuggestionsV2.mockResolvedValue(page({
      neighbourhood: {
        tables_considered: 9, tables_available: 9, truncated: false, max_hops: 2,
        limit_reason: null,
      },
    }))
    renderScreen()
    expect(await screen.findByTestId('neighbourhood'))
      .toHaveTextContent(/within 2 joins of this one/i)
  })

  // ── failure surfaces ──────────────────────────────────────────────────────────────────────────
  it('surfaces a load failure honestly', async () => {
    getTableSuggestionsV2.mockRejectedValue(new api.ApiError(500, 'grounding blew up'))
    renderScreen()
    expect(await screen.findByRole('alert')).toHaveTextContent(/grounding blew up/i)
  })

  it('names the missing permission on a 403 instead of a blank page or a raw error', async () => {
    getTableSuggestionsV2.mockRejectedValue(
      new api.ApiError(403, 'requires permission catalog:read'))
    renderScreen()
    expect(await screen.findByText(/don’t have access to feature suggestions/i)).toBeInTheDocument()
    expect(screen.getByText('catalog:read')).toBeInTheDocument()
    expect(screen.getByText(/data_owner/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })
})
