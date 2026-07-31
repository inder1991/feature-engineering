import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { AnalysisWorkspaceScreen } from './AnalysisWorkspaceScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, planAnalysis: vi.fn(), clarifyAnalysis: vi.fn() }
})
const planAnalysis = vi.mocked(api.planAnalysis)
const clarifyAnalysis = vi.mocked(api.clarifyAnalysis)

const QUESTION = 'which customers had fewer transactions this month than last'

function preview(over: Partial<api.AnalysisPreview> = {}): api.AnalysisPreview {
  return {
    question: QUESTION,
    entity: 'customer',
    measure: 'count of rows',
    comparison: 'decrease',
    dimensions: ['ftr::dpl_eib.cust_dim.segment'],
    periods: [
      { label: 'previous', partitions: ['2026-05'] },
      { label: 'current', partitions: ['2026-06'] },
    ],
    findings: [],
    sql: '',
    plan_hash: 'abc123',
    runnable: false,
    rests_on_unconfirmed_facts: false,
    blocked_by: { code: 'PHYSICAL_BINDING_ABSENT', subject: 'ftr::tran_repos' },
    ...over,
  }
}

function response(over: Partial<api.AnalysisPlanResponse> = {}): api.AnalysisPlanResponse {
  return {
    preview: preview(),
    clarifications: [],
    retrieval: { tables_considered: ['ftr::tran_repos'], dropped_columns: 0 },
    ...over,
  }
}

async function ask(text = QUESTION) {
  const user = userEvent.setup()
  render(<AnalysisWorkspaceScreen />)
  await user.type(screen.getByLabelText(/your question/i), text)
  await user.click(screen.getByRole('button', { name: /plan it/i }))
  return user
}

beforeEach(() => {
  planAnalysis.mockReset()
  clarifyAnalysis.mockReset()
})

describe('what it would compute', () => {
  it('shows the entity, measure and the periods it would read', async () => {
    planAnalysis.mockResolvedValue(response())
    await ask()
    const plan = await screen.findByRole('region', { name: /what this would compute/i })
    expect(within(plan).getByText('customer')).toBeInTheDocument()
    expect(within(plan).getByText('count of rows')).toBeInTheDocument()
    // Partition pruning is part of the plan: a user should see two months, not "recent data".
    expect(within(plan).getByText('2026-05')).toBeInTheDocument()
    expect(within(plan).getByText('2026-06')).toBeInTheDocument()
  })

  it('sends the typed question to the API', async () => {
    planAnalysis.mockResolvedValue(response())
    await ask()
    expect(planAnalysis).toHaveBeenCalledWith(QUESTION)
  })
})

describe('there is no run button', () => {
  it('offers no control that would execute the query', async () => {
    // The API has no execute endpoint. A disabled "Run" would imply it appears once you tick
    // something; the screen names the open gap instead.
    planAnalysis.mockResolvedValue(response())
    await ask()
    await screen.findByRole('region', { name: /what this would compute/i })
    for (const name of [/^run$/i, /execute/i, /run query/i, /run it/i]) {
      expect(screen.queryByRole('button', { name })).not.toBeInTheDocument()
    }
  })
})

describe('what the answer rests on', () => {
  it('states plainly when nothing is in doubt', async () => {
    planAnalysis.mockResolvedValue(response())
    await ask()
    expect(await screen.findByText(/every fact this answer would rest on is governed/i))
      .toBeInTheDocument()
  })

  it('shows each finding in words, not as a code', async () => {
    planAnalysis.mockResolvedValue(response({
      preview: preview({
        rests_on_unconfirmed_facts: true,
        findings: [{
          code: 'ELIGIBILITY_UNCONFIRMED', subject: 'ftr::tran_repos',
          detail: 'proposed by llm', clears_when: 'a human confirms the eligibility policy',
        }],
      }),
    }))
    await ask()
    expect(await screen.findByText(/which rows count was proposed but nobody has confirmed it/i))
      .toBeInTheDocument()
    expect(screen.getByText(/clears when a human confirms the eligibility policy/i))
      .toBeInTheDocument()
  })

  it('renders an unknown finding code as words rather than dropping it', async () => {
    // A newer backend must degrade, never vanish: a disclosure that disappears is worse than an
    // ugly one.
    planAnalysis.mockResolvedValue(response({
      preview: preview({
        findings: [{ code: 'SOME_NEW_DOUBT', subject: 'ftr::t.c', detail: '', clears_when: '' }],
      }),
    }))
    await ask()
    expect(await screen.findByText(/some new doubt/i)).toBeInTheDocument()
  })
})

describe('why it cannot run', () => {
  it('names the gap AND who closes it', async () => {
    // A code alone sends everyone to the same shrug.
    planAnalysis.mockResolvedValue(response())
    await ask()
    const blocked = await screen.findByRole('region', { name: /this cannot run yet/i })
    expect(within(blocked).getByText(/but not where it lives/i)).toBeInTheDocument()
    expect(within(blocked).getByText(/a platform operator registers the connection/i))
      .toBeInTheDocument()
  })

  it('distinguishes a revoked grant from a table nobody bound', async () => {
    planAnalysis.mockResolvedValue(response({
      preview: preview({ blocked_by: { code: 'SCHEMA_NOT_ALLOWED', subject: 'ftr::tran_repos' } }),
    }))
    await ask()
    expect(await screen.findByText(/no longer permits its schema/i)).toBeInTheDocument()
    expect(screen.getByText(/a governance owner restores the grant/i)).toBeInTheDocument()
  })
})

describe('truncated retrieval is stated', () => {
  it('says so when the catalog was clipped to fit the prompt', async () => {
    planAnalysis.mockResolvedValue(response({
      retrieval: { tables_considered: ['ftr::tran_repos'], dropped_columns: 12 },
    }))
    await ask()
    expect(await screen.findByText(/12 matching columns did not fit/i)).toBeInTheDocument()
    expect(screen.getByText(/narrower view of the catalog than exists/i)).toBeInTheDocument()
  })

  it('says nothing when everything fit', async () => {
    planAnalysis.mockResolvedValue(response())
    await ask()
    await screen.findByRole('region', { name: /what this would compute/i })
    expect(screen.queryByText(/did not fit/i)).not.toBeInTheDocument()
  })
})

describe('clarifications', () => {
  const population: api.AnalysisClarification = {
    code: 'population',
    question: 'Which table holds the population this question is about?',
    optional: false,
    allows_multiple: false,
    options: [
      { value: 'ftr::dpl_eib.customer_master.cif_id', label: 'customer identifier' },
      { value: 'ftr::dpl_eib.kyc_customers.cif_id', label: '' },
    ],
  }

  it('offers the bounded options and sends the chosen one', async () => {
    planAnalysis.mockResolvedValue(response({ clarifications: [population] }))
    clarifyAnalysis.mockResolvedValue(response())
    const user = await ask()

    await screen.findByText(/which table holds the population/i)
    await user.click(screen.getByRole('button', { name: /customer_master\.cif_id/i }))
    await user.click(screen.getByRole('button', { name: /^answer$/i }))

    await waitFor(() => expect(clarifyAnalysis).toHaveBeenCalledWith(
      QUESTION, 'population', ['ftr::dpl_eib.customer_master.cif_id']))
  })

  it('cannot be answered before something is chosen', async () => {
    planAnalysis.mockResolvedValue(response({ clarifications: [population] }))
    await ask()
    await screen.findByText(/which table holds the population/i)
    expect(screen.getByRole('button', { name: /^answer$/i })).toBeDisabled()
  })

  it('marks an optional question as optional', async () => {
    planAnalysis.mockResolvedValue(response({
      clarifications: [{ ...population, code: 'dimensions', optional: true,
        allows_multiple: true, question: 'Which attributes should the answer be split by?' }],
    }))
    await ask()
    expect(await screen.findByText(/\(optional\)/i)).toBeInTheDocument()
  })

  it('collects several answers when the question allows it', async () => {
    planAnalysis.mockResolvedValue(response({
      clarifications: [{ ...population, code: 'dimensions', allows_multiple: true }],
    }))
    clarifyAnalysis.mockResolvedValue(response())
    const user = await ask()
    await screen.findByText(/which table holds the population/i)
    await user.click(screen.getByRole('button', { name: /customer_master\.cif_id/i }))
    await user.click(screen.getByRole('button', { name: /kyc_customers\.cif_id/i }))
    await user.click(screen.getByRole('button', { name: /^answer$/i }))
    await waitFor(() => expect(clarifyAnalysis).toHaveBeenCalledWith(QUESTION, 'dimensions', [
      'ftr::dpl_eib.customer_master.cif_id', 'ftr::dpl_eib.kyc_customers.cif_id']))
  })

  it('answers against the question that was ASKED, not what is in the box now', async () => {
    // The input is editable while a plan is on screen. Sending the edited text would answer a
    // clarification about a different question.
    planAnalysis.mockResolvedValue(response({ clarifications: [population] }))
    clarifyAnalysis.mockResolvedValue(response())
    const user = await ask()
    await screen.findByText(/which table holds the population/i)
    await user.type(screen.getByLabelText(/your question/i), ' and by sector')
    await user.click(screen.getByRole('button', { name: /customer_master\.cif_id/i }))
    await user.click(screen.getByRole('button', { name: /^answer$/i }))
    await waitFor(() => expect(clarifyAnalysis).toHaveBeenCalledWith(
      QUESTION, 'population', ['ftr::dpl_eib.customer_master.cif_id']))
  })
})

describe('the statement', () => {
  it('is shown verbatim when there is one', async () => {
    const sql = 'WITH prev AS (SELECT "cif_id" AS k FROM "dpl_eib"."tran_repos")'
    planAnalysis.mockResolvedValue(response({ preview: preview({ sql, blocked_by: null,
      runnable: true }) }))
    await ask()
    expect(await screen.findByText(sql)).toBeInTheDocument()
  })

  it('is absent when the plan cannot be compiled', async () => {
    planAnalysis.mockResolvedValue(response())
    await ask()
    await screen.findByRole('region', { name: /what this would compute/i })
    expect(screen.queryByRole('region', { name: /the statement that would run/i }))
      .not.toBeInTheDocument()
  })
})

describe('failures are about the question', () => {
  it('shows a 422 as an answer, not as a broken system', async () => {
    planAnalysis.mockRejectedValue(
      new api.ApiError(422, 'no readable catalog column matched this question'))
    await ask()
    expect(await screen.findByRole('alert'))
      .toHaveTextContent(/no readable catalog column matched/i)
  })

  it('clears a previous plan rather than leaving a stale one beside the error', async () => {
    planAnalysis.mockResolvedValueOnce(response())
    const user = await ask()
    await screen.findByRole('region', { name: /what this would compute/i })

    planAnalysis.mockRejectedValueOnce(new api.ApiError(422, 'nothing matched'))
    await user.click(screen.getByRole('button', { name: /plan it/i }))
    await screen.findByRole('alert')
    expect(screen.queryByRole('region', { name: /what this would compute/i }))
      .not.toBeInTheDocument()
  })
})
