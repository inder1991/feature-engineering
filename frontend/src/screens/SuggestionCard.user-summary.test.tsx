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
    // The safety block dropped its uppercase label column — the artifact's note is the
    // sentence on an amber field, not a labelled row. The DECLARATION is what must survive,
    // and it does; the word "Point-in-time" was chrome.
    expect(within(card).getAllByText(/point-in-time/i).length).toBeGreaterThan(0)
    expect(within(card).getAllByText(/not runtime-enforced/).length).toBeGreaterThan(0)
    // The detail labels it "Eligibility"; the card's block was "Eligibility and leakage".
    expect(within(card).getAllByText(/Eligibility/).length).toBeGreaterThan(0)
    expect(within(card).getAllByText(/never use the purchased outcome/).length)
      .toBeGreaterThan(0)
  })
})

// ── contract v3 (BR-8): the execution-readiness axis ────────────────────────────────────────────
describe('SuggestionCard execution readiness (contract v3)', () => {
  const block = (over: Partial<NonNullable<import('../api').SuggestionExecutionBlock>> = {}) => ({
    recipe_contract_version: 'legacy-template',
    computation_kind: 'conceptual_pattern',
    execution_readiness: 'UNASSESSED',
    readiness_blockers: [],
    binding_ambiguity: false,
    ...over,
  })

  it('renders NOTHING readiness-shaped on a v2 page, where the block is absent', () => {
    render(<ul><SuggestionCard hit={hit({ display_name: 'v2_card' })} /></ul>)
    const card = screen.getByRole('heading', { name: 'v2_card' }).closest('.sfc') as HTMLElement
    expect(within(card).queryByText(/execution not assessed/)).toBeNull()
    expect(within(card).queryByText(/Execution readiness/)).toBeNull()
  })

  it('renders UNASSESSED as an idea — never a failure, never the success fill', async () => {
    render(
      <ul>
        <SuggestionCard hit={hit({ display_name: 'idea_card', execution: block() })} />
      </ul>,
    )
    const card = screen.getByRole('heading', { name: 'idea_card' }).closest('.sfc') as HTMLElement
    const chip = within(card).getByText('idea — execution not assessed')
    // The quiet tone: no green "good to go" for a state that only means "nobody decided yet",
    // and no amber either — an idea is not a defect.
    expect(chip.className).toContain('gj-none')
    expect(chip.className).not.toContain('gj-verified')
    await userEvent.click(within(card).getByRole('button', { name: /full detail/i }))
    expect(within(card).getByText(/that is a to-do, not/)).toBeInTheDocument()
  })

  it('a multi-output card says the choice is pending — never one atom\'s readiness', async () => {
    render(
      <ul>
        <SuggestionCard
          hit={hit({
            display_name: 'trend_card',
            execution: block({
              recipe_contract_version: 'recipe-contract-v2',
              computation_kind: 'deterministic_formula',
              execution_readiness: 'FORMULA_AUTHORABLE',   // the best atom — a ceiling
              output_selection_required: true,
              v2_replacements: ['balance_slope', 'normalized_balance_slope'],
              replacement_readiness: [
                { recipe_id: 'balance_slope', execution_readiness: 'FORMULA_AUTHORABLE',
                  computation_kind: 'deterministic_formula' },
                { recipe_id: 'normalized_balance_slope', execution_readiness: 'FORMULA_BLOCKED',
                  computation_kind: 'deterministic_formula' },
              ],
            }),
          })}
        />
      </ul>,
    )
    const card = screen.getByRole('heading', { name: 'trend_card' }).closest('.sfc') as HTMLElement
    // The chip refuses to headline the best atom's state as if it were the card's.
    const chip = within(card).getByText('varies by output')
    expect(chip.className).toContain('gj-none')
    expect(within(card).queryByText('formula ready to author')).toBeNull()
    // The drawer names each atom's OWN state and says whose choice it is.
    await userEvent.click(within(card).getByRole('button', { name: /full detail/i }))
    expect(within(card).getByText(/Choosing which output to build is your call/))
      .toBeInTheDocument()
    expect(within(card).getByText('balance_slope')).toBeInTheDocument()
    expect(within(card).getByText('normalized_balance_slope')).toBeInTheDocument()
    expect(within(card).getByText(/formula blocked/)).toBeInTheDocument()
  })

  it('a single-output card keeps the plain readiness chip untouched', () => {
    render(
      <ul>
        <SuggestionCard
          hit={hit({
            display_name: 'single_card',
            execution: block({
              execution_readiness: 'FORMULA_AUTHORABLE',
              output_selection_required: false,
              replacement_readiness: [
                { recipe_id: 'one_atom', execution_readiness: 'FORMULA_AUTHORABLE',
                  computation_kind: 'deterministic_formula' },
              ],
            }),
          })}
        />
      </ul>,
    )
    const card = screen.getByRole('heading', { name: 'single_card' })
      .closest('.sfc') as HTMLElement
    expect(within(card).queryByText('varies by output')).toBeNull()
  })

  it('explains each blocker in banking words and keeps the machine code beside it', async () => {
    render(
      <ul>
        <SuggestionCard
          hit={hit({
            display_name: 'authorable_card',
            execution: block({
              computation_kind: 'deterministic_formula',
              execution_readiness: 'FORMULA_AUTHORABLE',
              readiness_blockers: [
                { code: 'gold_evaluation_unproven', group: 'formula_capability' },
              ],
            }),
          })}
        />
      </ul>,
    )
    const card = screen.getByRole('heading', { name: 'authorable_card' })
      .closest('.sfc') as HTMLElement
    expect(within(card).getByText('formula authorable')).toBeInTheDocument()
    await userEvent.click(within(card).getByRole('button', { name: /full detail/i }))
    expect(within(card)
      .getByText(/worked examples that prove this formula have not been run/))
      .toBeInTheDocument()
    // The audit half of the rule: the machine code never disappears behind the words.
    expect(within(card).getByText('gold_evaluation_unproven')).toBeInTheDocument()
    expect(within(card).getByText(/formula capability/)).toBeInTheDocument()
  })

  it('renders an unknown future state as words rather than crashing', () => {
    render(
      <ul>
        <SuggestionCard
          hit={hit({
            display_name: 'future_card',
            execution: block({ execution_readiness: 'SHADOW_PROVEN' }),
          })}
        />
      </ul>,
    )
    const card = screen.getByRole('heading', { name: 'future_card' })
      .closest('.sfc') as HTMLElement
    expect(within(card).getByText('shadow proven')).toBeInTheDocument()
  })
})
