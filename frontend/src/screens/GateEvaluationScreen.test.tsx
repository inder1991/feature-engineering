import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'
import { GateEvaluationScreen } from './GateEvaluationScreen'

describe('gate evaluation screen', () => {
  it('renders a FAIL verdict with the necessary-not-sufficient caveat and coverage', async () => {
    vi.stubGlobal('fetch', vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => [{ cohort: 'sha1', first_run_at: 'x', last_run_at: 'y', run_count: 3 }] })
      .mockResolvedValueOnce({ ok: true, json: async () => ({
        verdict: { passed: false, gate1_capture: false, gate2a_map: true, gate3_gold: true, gate5_stability: true, gate6_drift: true },
        reasons: ['Gate 1: empty qualifying population (no evidence)'],
        necessary_not_sufficient: true,
        coverage: { dispatched_in_range: 0, qualifying: 0, excluded: {} },
        population: { denominator: 0, numerator: 0, headline_by_primary: {}, breakdown_by_category: {}, recipe_outcome_matrix: {} },
        versions: { evaluator: '1.0.0', cohort: 'sha1' },
      }) }))
    render(<GateEvaluationScreen />)
    await userEvent.click(await screen.findByRole('button', { name: /evaluate/i }))
    await waitFor(() => expect(screen.getByText(/FAIL/)).toBeInTheDocument())
    expect(screen.getByText(/necessary.*not.*sufficient/i)).toBeInTheDocument()
  })
})
