import { render, screen, within } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { SuggestedFeaturesScreen } from './SuggestedFeaturesScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getTableSuggestions: vi.fn() }
})
const getTableSuggestions = vi.mocked(api.getTableSuggestions)

const SOURCE = 'core_banking'
const TABLE = 'public.comp_fin_tran'

function suggestion(over: Partial<api.FeatureSuggestion> = {}): api.FeatureSuggestion {
  return {
    name: 'account_balance_trend_90d',
    description: 'How this account’s balance has trended over the last 90 days.',
    recipe: 'trend_90d(bal_amt) BY acct_id OVER 90d [as_of_dt]',
    recipe_parts: {
      operation: 'trend_90d', measures: ['bal_amt'], grain: 'acct_id',
      window: '90d', time: 'as_of_dt',
    },
    validation_status: 'DESIGN_CHECKED',
    requirements: [],
    uses: ['public.comp_fin_tran.bal_amt'],
    binding_quality: 'exact',
    grain_table: TABLE,
    ...over,
  }
}

const NEEDS_REVIEW = suggestion({
  name: 'customer_inflow_30d',
  description: 'Money flowing in to this customer over the last 30 days.',
  recipe: 'inflow_outflow(tran_amt) BY cif_id OVER 30d [as_of_dt]',
  validation_status: 'NEEDS_EXTERNAL_VALIDATION',
  requirements: [
    { code: 'UNIT_CONSISTENT', operand: [SOURCE, 'public.comp_fin_tran.tran_amt'], detail: '' },
  ],
  uses: ['public.comp_fin_tran.tran_amt'],
  binding_quality: 'concept',
})

function payload(over: Partial<api.TableSuggestions> = {}): api.TableSuggestions {
  return {
    catalog_source: SOURCE,
    table: TABLE,
    table_known: true,
    summary: { suggested: 2, clean_ready: 1, needs_review: 1, entities: 2 },
    groups: [
      {
        entity_ref: 'public.comp_fin_tran.acct_id',
        entity_label: 'account',
        suggestions: [suggestion()],
      },
      {
        entity_ref: 'public.comp_fin_tran.cif_id',
        entity_label: 'customer',
        suggestions: [NEEDS_REVIEW],
      },
    ],
    rejections: [],
    neighbourhood: {
      tables_considered: 0, tables_available: 0, truncated: false, max_hops: 1, limit_reason: null,
    },
    ...over,
  }
}

beforeEach(() => {
  getTableSuggestions.mockReset()
})

function renderScreen() {
  render(<SuggestedFeaturesScreen source={SOURCE} table={TABLE} />)
}

describe('SuggestedFeaturesScreen', () => {
  it('renders the summary counts from the response', async () => {
    getTableSuggestions.mockResolvedValue(
      payload({ summary: { suggested: 14, clean_ready: 11, needs_review: 3, entities: 3 } }),
    )
    renderScreen()
    const summary = await screen.findByRole('group', { name: /suggestion summary/i })
    expect(summary).toHaveTextContent('14 suggested')
    expect(summary).toHaveTextContent('11 clean & ready')
    expect(summary).toHaveTextContent('3 need review')
    expect(summary).toHaveTextContent('3 entities')
    expect(getTableSuggestions).toHaveBeenCalledWith(SOURCE, TABLE)
  })

  it('renders one group per entity: the heading is the entity, the suffix is its column', async () => {
    getTableSuggestions.mockResolvedValue(payload())
    renderScreen()
    // entity_label is the ENTITY ('account'); entity_ref is the COLUMN it is computed per.
    const account = await screen.findByRole('heading', { name: /account features/i })
    expect(account).toBeInTheDocument()
    expect(screen.getByRole('heading', { name: /customer features/i })).toBeInTheDocument()
    expect(screen.getByText(/per entity acct_id/i)).toBeInTheDocument()
    expect(screen.getByText(/per entity cif_id/i)).toBeInTheDocument()
  })

  it('renders no entity heading for a group whose entity_ref is empty', async () => {
    getTableSuggestions.mockResolvedValue(payload({
      summary: { suggested: 1, clean_ready: 1, needs_review: 0, entities: 0 },
      groups: [{ entity_ref: '', entity_label: '', suggestions: [suggestion()] }],
    }))
    renderScreen()
    expect(await screen.findByText('account_balance_trend_90d')).toBeInTheDocument()
    expect(screen.queryAllByRole('heading', { level: 2 })).toHaveLength(0)
    expect(screen.queryByText(/per entity/i)).not.toBeInTheDocument()
  })

  it('shows only the column for a group whose entity the catalog could not name', async () => {
    getTableSuggestions.mockResolvedValue(payload({
      summary: { suggested: 1, clean_ready: 1, needs_review: 0, entities: 1 },
      groups: [{
        entity_ref: 'public.comp_fin_tran.cif_id', entity_label: '', suggestions: [suggestion()],
      }],
    }))
    renderScreen()
    expect(await screen.findByText('account_balance_trend_90d')).toBeInTheDocument()
    // no heading is invented from the column: 'cif_id features' would claim an unattested entity
    expect(screen.queryAllByRole('heading', { level: 2 })).toHaveLength(0)
    expect(screen.getByText(/per entity cif_id/i)).toBeInTheDocument()
  })

  it('shows the real recipe line and the columns a suggestion uses', async () => {
    getTableSuggestions.mockResolvedValue(payload())
    renderScreen()
    expect(
      await screen.findByText('trend_90d(bal_amt) BY acct_id OVER 90d [as_of_dt]'),
    ).toBeInTheDocument()
    expect(screen.getByText(/uses bal_amt/i)).toBeInTheDocument()
  })

  it('chips the tri-state honestly: clean & ready vs needs review with its requirement in words',
    async () => {
      getTableSuggestions.mockResolvedValue(payload())
      renderScreen()
      const clean = (await screen.findByText('account_balance_trend_90d')).closest('li')
      expect(within(clean as HTMLElement).getByText('clean & ready')).toBeInTheDocument()
      const review = screen.getByText('customer_inflow_30d').closest('li')
      expect(within(review as HTMLElement).getByText('needs review')).toBeInTheDocument()
      // The requirement code is rendered in plain words, not as a raw enum only.
      expect(within(review as HTMLElement).getByText(/needs a declared unit/i)).toBeInTheDocument()
      // the requirement names the operand column it concerns
      expect(within(review as HTMLElement).getByRole('listitem'))
        .toHaveTextContent(/needs a declared unit\s+tran_amt/i)
    })

  it('renders the binding-quality signal as the real value, never a fabricated percentage',
    async () => {
      getTableSuggestions.mockResolvedValue(payload())
      renderScreen()
      expect(await screen.findByText(/binding exact/i)).toBeInTheDocument()
      expect(screen.queryByText(/%/)).not.toBeInTheDocument()
      expect(document.querySelector('progress, meter')).toBeNull()
    })

  it('is strictly read-only: no accept, edit or dismiss control is rendered', async () => {
    getTableSuggestions.mockResolvedValue(payload())
    renderScreen()
    await screen.findByText('account_balance_trend_90d')
    expect(screen.queryByRole('button', { name: /accept/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /dismiss/i })).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /edit/i })).not.toBeInTheDocument()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
    expect(screen.queryAllByRole('textbox')).toHaveLength(0)
    expect(screen.getByText(/read-only/i)).toBeInTheDocument()
  })

  it('says the table does not exist rather than diagnosing its columns', async () => {
    // The trap: an unknown table returns the SAME zero payload as a table with no concepts, so
    // without table_known the screen tells the user a nonexistent table's columns carry no meaning.
    getTableSuggestions.mockResolvedValue(payload({
      table_known: false,
      summary: { suggested: 0, clean_ready: 0, needs_review: 0, entities: 0 },
      groups: [],
      rejections: [],
    }))
    renderScreen()
    expect(await screen.findByText(/no such table in this catalog/i)).toBeInTheDocument()
    expect(screen.getByText(SOURCE)).toBeInTheDocument()
    expect(screen.queryByText(/business concepts/i)).not.toBeInTheDocument()
    expect(screen.queryByRole('group', { name: /suggestion summary/i })).not.toBeInTheDocument()
  })

  it('names the no-concepts cause when there is nothing suggested and nothing blocked', async () => {
    getTableSuggestions.mockResolvedValue(payload({
      summary: { suggested: 0, clean_ready: 0, needs_review: 0, entities: 0 },
      groups: [],
      rejections: [],
    }))
    renderScreen()
    expect(await screen.findByText(/no suggestions yet/i)).toBeInTheDocument()
    expect(screen.getByText(/business concepts/i)).toBeInTheDocument()
    // points at where the cause is fixed
    expect(screen.getByText(/semantics/i)).toBeInTheDocument()
  })

  it('names the missing as-of cause and counts the features it blocks', async () => {
    const blocked = (n: number) => Array.from({ length: n }, (_unused, i) => ({
      name: `blocked_${i}`, reason: 'future-leakage risk: no point-in-time column',
      code: 'NO_POINT_IN_TIME',
    }))
    getTableSuggestions.mockResolvedValue(payload({
      summary: { suggested: 1, clean_ready: 0, needs_review: 1, entities: 1 },
      groups: [{
        entity_ref: 'public.comp_fin_tran.acct_id', entity_label: 'account',
        suggestions: [NEEDS_REVIEW],
      }],
      rejections: blocked(7),
    }))
    renderScreen()
    expect(await screen.findByText(/7 features are blocked/i)).toBeInTheDocument()
    expect(screen.getByText(/no confirmed as-of column/i)).toBeInTheDocument()
    expect(screen.getByText(/grain & availability/i)).toBeInTheDocument()
  })

  it('treats zero clean & ready with cards present as the honest normal state, not an error',
    async () => {
      getTableSuggestions.mockResolvedValue(payload({
        summary: { suggested: 8, clean_ready: 0, needs_review: 8, entities: 1 },
        groups: [{
          entity_ref: 'public.comp_fin_tran.acct_id', entity_label: 'account',
          suggestions: [NEEDS_REVIEW],
        }],
      }))
      renderScreen()
      expect(await screen.findByText(/every suggestion below needs one more declared fact/i))
        .toBeInTheDocument()
      // not styled as a failure: no alert, and the zero tile carries no danger tone
      expect(screen.queryByRole('alert')).not.toBeInTheDocument()
      const summary = screen.getByRole('group', { name: /suggestion summary/i })
      expect(summary.querySelector('.tone-danger')).toBeNull()
      expect(within(summary).getByText('0')).toBeInTheDocument()
    })

  // ── what the page did NOT look at ─────────────────────────────────────────────────────────────
  // Suggestions are grounded on this table plus the tables joined DIRECTLY to it, capped. A page
  // that shows a bounded slice of the join neighbourhood and says nothing turns every empty state
  // into a false claim ("nothing else is buildable here" when the truth is "we did not look").
  it('says how many joined tables it showed of how many exist when it truncated', async () => {
    getTableSuggestions.mockResolvedValue(payload({
      neighbourhood: {
        tables_considered: 20, tables_available: 73, truncated: true, max_hops: 1,
        limit_reason: 'table_cap',
      },
    }))
    renderScreen()
    const note = await screen.findByTestId('neighbourhood')
    expect(note).toHaveTextContent(/Showing 20 of 73 directly joined tables/i)
    expect(note).toHaveTextContent(/Deeper join paths were not automatically considered/i)
    // and WHICH limit did it — never a bare number the reader cannot account for
    expect(note).toHaveTextContent(/how many joined tables/i)
  })

  it('names the column budget when that is the limit that bit, not the table count', async () => {
    getTableSuggestions.mockResolvedValue(payload({
      neighbourhood: {
        tables_considered: 3, tables_available: 41, truncated: true, max_hops: 1,
        limit_reason: 'column_budget',
      },
    }))
    renderScreen()
    const note = await screen.findByTestId('neighbourhood')
    expect(note).toHaveTextContent(/Showing 3 of 41 directly joined tables/i)
    expect(note).toHaveTextContent(/how many columns/i)
  })

  it('states the hop limit even when nothing was truncated', async () => {
    // The point of saying it always: deeper join paths EXIST and were deliberately not loaded, so a
    // user who sees "all 4 joined tables" must not read that as "everything reachable was tried".
    getTableSuggestions.mockResolvedValue(payload({
      neighbourhood: {
        tables_considered: 4, tables_available: 4, truncated: false, max_hops: 1,
        limit_reason: null,
      },
    }))
    renderScreen()
    const note = await screen.findByTestId('neighbourhood')
    expect(note).toHaveTextContent(/all 4 directly joined tables/i)
    expect(note).toHaveTextContent(/Deeper join paths were not automatically considered/i)
    expect(note).toHaveTextContent(/within 1 join of this one/i)
    expect(note).not.toHaveTextContent(/Showing 4 of 4/i)
  })

  it('says so plainly when no confirmed join reaches another table', async () => {
    getTableSuggestions.mockResolvedValue(payload())        // 0 available, 0 considered
    renderScreen()
    const note = await screen.findByTestId('neighbourhood')
    expect(note).toHaveTextContent(/no confirmed join reaches another table/i)
    expect(note).not.toHaveTextContent(/Showing 0 of 0/i)
  })

  it('takes the hop number from the payload rather than assuming one hop', async () => {
    // An explicit wider request is a supported (deliberate) call, and the copy must not then lie
    // about what was loaded.
    getTableSuggestions.mockResolvedValue(payload({
      neighbourhood: {
        tables_considered: 9, tables_available: 9, truncated: false, max_hops: 2,
        limit_reason: null,
      },
    }))
    renderScreen()
    expect(await screen.findByTestId('neighbourhood')).toHaveTextContent(/within 2 joins of this one/i)
  })

  it('says nothing about joins for a table this catalog does not hold', async () => {
    getTableSuggestions.mockResolvedValue(payload({ table_known: false }))
    renderScreen()
    await screen.findByText(/no such table in this catalog/i)
    expect(screen.queryByTestId('neighbourhood')).not.toBeInTheDocument()
  })

  it('surfaces a load failure honestly', async () => {
    getTableSuggestions.mockRejectedValue(new api.ApiError(500, 'grounding blew up'))
    renderScreen()
    expect(await screen.findByRole('alert')).toHaveTextContent(/grounding blew up/i)
  })

  it('names the missing permission on a 403 instead of a blank page or a raw error', async () => {
    // The route is gated on catalog:read; a session lacking it (e.g. access_admin) gets a 403 that
    // not hold it. That is a product decision this screen does not get to change — but it must not
    // present as a broken page.
    getTableSuggestions.mockRejectedValue(new api.ApiError(403, 'requires permission catalog:read'))
    renderScreen()
    expect(await screen.findByText(/don’t have access to feature suggestions/i)).toBeInTheDocument()
    expect(screen.getByText('catalog:read')).toBeInTheDocument()
    expect(screen.getByText(/data_owner/)).toBeInTheDocument()
    // not a red failure, and no control that would pretend the user can grant it here
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
    expect(screen.queryAllByRole('button')).toHaveLength(0)
  })
})
