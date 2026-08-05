import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { render, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it } from 'vitest'
import type { FeatureSuggestionPageV2, FeatureSuggestionHit } from '../api'
import { SuggestionCard } from './SuggestionCard'

// A REAL `page_to_json` body, captured off the engine and checked in — not a hand-written literal.
//
// Every other suite here builds its payload from `SuggestedFeaturesScreen.fixture.ts`, which is a
// set of hand-written object literals. They agree with the server only for as long as someone
// re-reads both, and there is one shape nobody thought to hand-write: a warning about a JOIN. The
// server emitted those with `operand_refs` nested one level too deep — `[[[src, a], [src, b]]]`
// instead of `[[src, a], [src, b]]` — and `limitationsOf` maps `r => r[1]` over it, so `columnOf`
// received an ARRAY, `ref.split` threw, and with no ErrorBoundary anywhere under `src/` the whole
// React root unmounted. The suggestions screen and the column dossier both went blank for any
// catalog that declared a join without an approved-join fact, which is the DEFAULT for an
// upload-declared relationship. Every backend suite, every vitest, tsc and oxlint were green.
//
// So: this file renders the server's own bytes. The other half of the pin lives in
// `test_suggestion_contract.py::test_the_frontends_captured_server_body_is_still_the_body_the_
// server_sends`, which re-derives the capture from the engine and fails if the contract surface
// moved. Regeneration command is in that test's docstring.
const CAPTURE = JSON.parse(readFileSync(
  resolve(process.cwd(), 'src/screens/SuggestedFeaturesScreen.serverCapture.json'),
  'utf8',
)) as FeatureSuggestionPageV2

function cardWithCode(code: string): FeatureSuggestionHit {
  const found = CAPTURE.hits.find(h => h.suggestion.warnings.some(w => w.code === code))
  if (!found) throw new Error(`the captured body carries no ${code} warning`)
  return found
}

describe('SuggestionCard over a captured server body', () => {
  it('carries the join warnings the hand-written fixtures never produced', () => {
    const codes = new Set(CAPTURE.hits.flatMap(h => h.suggestion.warnings.map(w => w.code)))
    expect(codes).toContain('RELATIONSHIP_UNCONFIRMED')
    expect(codes).toContain('DIRECTIONAL_CARDINALITY_UNAVAILABLE')
  })

  it('gives every warning a FLAT (catalog_source, ref) pair — one arity for every code', () => {
    for (const hit of CAPTURE.hits) {
      for (const warning of hit.suggestion.warnings) {
        for (const ref of warning.operand_refs) {
          expect(ref, `${warning.code} operand ref`).toHaveLength(2)
          expect(typeof ref[0]).toBe('string')
          expect(typeof ref[1]).toBe('string')
        }
      }
    }
  })

  it('renders every captured card, compact and expanded, without unmounting', async () => {
    for (const hit of CAPTURE.hits) {
      const { container, unmount } = render(<ul><SuggestionCard hit={hit} /></ul>)
      const card = container.querySelector('li.sfc') as HTMLElement
      expect(within(card).getByRole('heading', { name: hit.suggestion.display_name }))
        .toBeInTheDocument()
      await userEvent.click(within(card).getByRole('button', { name: /show full detail/i }))
      expect(within(card).getByText('Suggestion id')).toBeInTheDocument()
      unmount()
    }
  })

  it('names the columns an unconfirmed-join warning is about, as column names', async () => {
    // THE REGRESSION, said positively. `refWords` runs `columnOf` over each ref's second element,
    // so a nested shape crashed here; a flat one reads out the endpoint column names.
    const hit = cardWithCode('RELATIONSHIP_UNCONFIRMED')
    const { container } = render(<ul><SuggestionCard hit={hit} /></ul>)
    const card = container.querySelector('li.sfc') as HTMLElement
    // Caveat rows live in Full detail now — the card carries only their count, so the panel
    // reads as a hint to pursue rather than a defect list.
    await userEvent.click(within(card).getByRole('button', { name: /show full detail/i }))
    // Scoped to the limitations list: the phrase also appears in the relationship section.
    const lims = within(card).getByRole('list', { name: /requirements and limitations/i })
    const row = within(lims).getByText(/declared by an upload and confirmed by nobody/i)
      .closest('li') as HTMLElement
    const warning = hit.suggestion.warnings.find(w => w.code === 'RELATIONSHIP_UNCONFIRMED')!
    expect(warning.operand_refs.length).toBeGreaterThan(0)
    for (const ref of warning.operand_refs) {
      expect(row.textContent).toContain(ref[1].split('.').pop())
    }
  })
})
