import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import { SuggestionCard } from './SuggestionCard'
import { hit, label, operand, text } from './SuggestedFeaturesScreen.fixture'

describe('SuggestionCard end-user summary', () => {
  // Inputs moved into the detail, so the name now says what it actually pins: what is legible
  // on the compact card, plus that every input column is still reachable.
  it('keeps entity, stage, additivity, time authority and safety on the compact card', async () => {
    const suggestion = hit({
      display_name: 'tenure_days',
      recipe_family: label({ id: 'tenure', display_name: 'Tenure' }),
      recipe_stage: text({ value: 'context', basis: 'template_authored' }),
      output_additivity: text({ value: 'n/a', basis: 'template_authored' }),
      point_in_time_declaration: text({
        value: 'origination ≤ as_of; this design-time rule is not runtime-enforced.',
        basis: 'template_authored',
      }),
      eligibility_note: text({
        value: 'Use pre-purchase behaviour only; never use the purchased outcome.',
        basis: 'template_authored',
      }),
      time_ref: null,
      operands: [
        operand({
          graph_object_ref: 'public.customer.cust_cnsnt_mod_dt',
          recipe_role: 'origination',
          classification: 'time',
        }),
        operand({
          graph_object_ref: 'public.customer.business_dt',
          recipe_role: 'asof',
          classification: 'time',
        }),
        operand({
          graph_object_ref: 'public.customer.cust_num',
          recipe_role: 'entity',
          classification: 'grain',
        }),
      ],
      binding_quality: 'ambiguous',
    })

    render(<ul><SuggestionCard hit={suggestion} /></ul>)

    const card = screen.getByRole('heading', { name: 'tenure_days' }).closest('.sfc') as HTMLElement
    expect(card).not.toBeNull()
    expect(within(card).getByText('binding ambiguous')).toBeInTheDocument()
    // Family and stage lead the card as pills now, rather than a "Family and stage" dd two
    // thirds of the way down. Both facts are still on the compact card; only the shape moved.
    const taxonomy = card.querySelector('.sfc-taxonomy') as HTMLElement
    expect(within(taxonomy).getByText('Tenure')).toBeInTheDocument()
    expect(within(taxonomy).getByText(/context stage/i)).toBeInTheDocument()
    // Additivity is one of the four boxed parameters now, under the concept's label.
    expect(within(card).getByText('Aggregation')).toBeInTheDocument()
    // "n/a" is a storage value; the card now reads it out as words.
    expect(within(card).getByText('Not summable · n/a')).toBeInTheDocument()

    const asOf = within(card).getByText((_, element) =>
      element?.tagName === 'DD'
      && element.textContent?.includes('business_dt') === true
      && element.textContent?.includes('governed time anchor unresolved') === true)
    expect(asOf).toBeInTheDocument()

    // Input columns moved into Full recommendation detail with the rest of the evidence. The
    // guarantee is unchanged -- every input column is listed -- so assert the COLUMNS, not the
    // container that happens to hold them.
    await userEvent.click(within(card).getByRole('button', { name: /show full detail/i }))
    expect(within(card).getAllByText(/cust_cnsnt_mod_dt/).length).toBeGreaterThan(0)
    expect(within(card).getAllByText(/business_dt/).length).toBeGreaterThan(0)
    expect(within(card).getAllByText(/cust_num/).length).toBeGreaterThan(0)
    // ...and each column still names the recipe ROLE it fills.
    expect(within(card).getAllByText(/origination/).length).toBeGreaterThan(0)
    expect(within(card).getAllByText(/asof/).length).toBeGreaterThan(0)
    expect(within(card).getAllByText(/entity/).length).toBeGreaterThan(0)

    // The point-in-time note stays on the compact card; the eligibility note moved into the
    // detail (the artifact shows one safety block, not two). Both are still stated -- the
    // guarantee is that a safety constraint is never silently dropped, not where it sits.
    expect(within(card).getAllByText('Point-in-time').length).toBeGreaterThan(0)
    expect(within(card).getAllByText(/not runtime-enforced/).length).toBeGreaterThan(0)
    // The detail labels it "Eligibility"; the card's block was "Eligibility and leakage".
    expect(within(card).getAllByText(/Eligibility/).length).toBeGreaterThan(0)
    expect(within(card).getAllByText(/never use the purchased outcome/).length)
      .toBeGreaterThan(0)
  })
})
