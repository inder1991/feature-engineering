import { render, screen, within } from '@testing-library/react'
import { describe, expect, it } from 'vitest'
import { SuggestionCard } from './SuggestionCard'
import { hit, label, operand, text } from './SuggestedFeaturesScreen.fixture'

describe('SuggestionCard end-user summary', () => {
  it('keeps entity, stage, additivity, inputs, time authority and safety visible before expansion', () => {
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
    expect(within(card).getByText('n/a')).toBeInTheDocument()

    const asOf = within(card).getByText((_, element) =>
      element?.tagName === 'DD'
      && element.textContent?.includes('business_dt') === true
      && element.textContent?.includes('governed time anchor unresolved') === true)
    expect(asOf).toBeInTheDocument()

    const inputs = card.querySelector('.sfc-inputs')
    expect(inputs).not.toBeNull()
    expect(within(inputs as HTMLElement).getByText('cust_cnsnt_mod_dt')).toBeInTheDocument()
    expect(within(inputs as HTMLElement).getByText('business_dt')).toBeInTheDocument()
    expect(within(inputs as HTMLElement).getByText('cust_num')).toBeInTheDocument()
    expect(within(inputs as HTMLElement).getByText('origination')).toBeInTheDocument()
    expect(within(inputs as HTMLElement).getByText('asof')).toBeInTheDocument()
    expect(within(inputs as HTMLElement).getByText('entity')).toBeInTheDocument()

    expect(within(card).getByText('Point-in-time')).toBeInTheDocument()
    expect(within(card).getByText(/not runtime-enforced/)).toBeInTheDocument()
    expect(within(card).getByText('Eligibility and leakage')).toBeInTheDocument()
    expect(within(card).getByText(/never use the purchased outcome/)).toBeInTheDocument()
  })
})
