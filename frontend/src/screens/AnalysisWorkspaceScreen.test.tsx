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

// ── Release B: where the answer comes from ──────────────────────────────────────────────────────

function selection(over: Partial<api.AnalysisSelection> = {}): api.AnalysisSelection {
  return {
    resolved: true,
    sources: [
      { need_role: 'population', withheld: false,
        dataset_ref: 'ftr::dpl_eib.customer_master', selection_basis: 'explicit_request',
        authority_basis: 'load_bearing_profile', authority_role: 'master',
        considered: [{ dataset_ref: 'ftr::dpl_eib.customer_master', disposition: 'selected',
                       reason_codes: ['explicit_request'] }],
        considered_withheld: 0, considered_total: 1 },
      { need_role: 'event_source', withheld: false,
        dataset_ref: 'ftr::dpl_eib.tran_repos', selection_basis: 'serving_policy',
        authority_basis: 'policy_declaration', authority_role: 'master',
        considered: [{ dataset_ref: 'ftr::dpl_eib.tran_repos', disposition: 'selected',
                       reason_codes: ['policy_preferred'] }],
        considered_withheld: 1, considered_total: 2 },
    ],
    rows: [{ dataset_ref: 'ftr::dpl_eib.cust_dim', selection_kind: 'valid_at_report_cutoff',
             cutoff_value_ref: 'report_cutoff_param',
             predicates: [{ column_ref: 'ftr::dpl_eib.cust_dim.effective_from', operator: '<=' },
                          { column_ref: 'ftr::dpl_eib.cust_dim.effective_to', operator: '>' }],
             predicates_withheld: false }],
    refusals: [],
    warnings: [],
    ...over,
  }
}

describe('where the answer comes from', () => {
  it('names the source chosen for each need and how it was chosen', async () => {
    planAnalysis.mockResolvedValue(response({ selection: selection() }))
    await ask()
    const block = await screen.findByRole('region', { name: /where this answer comes from/i })
    expect(within(block).getByText(/who is in the answer/i)).toBeInTheDocument()
    expect(within(block).getByText('dpl_eib.customer_master')).toBeInTheDocument()
    expect(within(block).getByText(/declared in the request/i)).toBeInTheDocument()
    expect(within(block).getByText(/named by the serving policy/i)).toBeInTheDocument()
  })

  it('states the ROW rule and the predicates under it, not just its label', async () => {
    planAnalysis.mockResolvedValue(response({ selection: selection() }))
    await ask()
    const block = await screen.findByRole('region', { name: /where this answer comes from/i })
    expect(within(block).getByText(/valid at the report cutoff/i)).toBeInTheDocument()
    expect(within(block).getByText(/effective_from <=/)).toBeInTheDocument()
  })

  it('reports a hidden alternative as a COUNT and never as a name', async () => {
    planAnalysis.mockResolvedValue(response({ selection: selection() }))
    await ask()
    const block = await screen.findByRole('region', { name: /where this answer comes from/i })
    expect(within(block).getByText(/1 you cannot see/i)).toBeInTheDocument()
    // The server already withheld it; the screen must not invent a way to say what it was.
    expect(block).not.toHaveTextContent(/archive/i)
  })

  it('shows a PROPOSED authority warning beside the decision it rode on', async () => {
    planAnalysis.mockResolvedValue(response({
      selection: selection({ warnings: ['PROPOSED_AUTHORITY_USED'] }) }))
    await ask()
    const block = await screen.findByRole('region', { name: /where this answer comes from/i })
    expect(within(block).getByText(/nobody has confirmed/i)).toBeInTheDocument()
  })

  it('frames an undecided source as a choice, never as a failure', async () => {
    planAnalysis.mockResolvedValue(response({
      selection: selection({
        resolved: false,
        refusals: [{ code: 'SELECTION_POPULATION_UNDECLARED',
                     subjects: ['ftr::dpl_eib.tran_repos'], subjects_withheld: 0,
                     detail: 'no population is declared for this plan', family: 'undecided' }],
      }) }))
    await ask()
    const block = await screen.findByRole('region', { name: /where this answer comes from/i })
    expect(within(block).getByText(/nobody has decided this yet/i)).toBeInTheDocument()
    expect(within(block).getByText(/a person chooses; nothing is wrong with the data/i))
      .toBeInTheDocument()
    // Not an alert, not an error region: an undecided thing is not a broken thing.
    expect(within(block).queryByRole('alert')).not.toBeInTheDocument()
  })

  // T9: an unrecognised family used to fall back to FAMILY_WORDS.undecided, so a newer backend's
  // family arrived under the heading "Nobody has decided this yet" AND the line "nothing is wrong
  // with the data" — a positive assertion about data health manufactured by a missing map entry.
  // The refusal's own `detail` is still the answer; the family line simply says nothing.
  it('a family this screen has never heard of asserts nothing about the data', async () => {
    planAnalysis.mockResolvedValue(response({
      selection: selection({
        resolved: false,
        refusals: [{ code: 'SOME_NEWER_REFUSAL', subjects: ['ftr::dpl_eib.tran_repos'],
                     subjects_withheld: 0, detail: 'the newer backend explained it here',
                     family: 'awaiting_operator' }],
      }) }))
    await ask()
    const block = await screen.findByRole('region', { name: /where this answer comes from/i })
    // The server's own sentence still lands.
    expect(within(block).getByText('the newer backend explained it here')).toBeInTheDocument()
    // …and nothing is claimed on top of it.
    expect(within(block).queryByText(/nobody has decided this yet/i)).toBeNull()
    expect(within(block).queryByText(/nothing is wrong with the data/i)).toBeNull()
  })

  it('separates a data check from a decision nobody has made', async () => {
    planAnalysis.mockResolvedValue(response({
      selection: selection({
        resolved: false,
        refusals: [{ code: 'TEMPORAL_SCD_OVERLAP', subjects: ['ftr::dpl_eib.cust_dim'],
                     subjects_withheld: 0, detail: 'two rows claim the same instant',
                     family: 'needs_data_check' }],
      }) }))
    await ask()
    const block = await screen.findByRole('region', { name: /where this answer comes from/i })
    expect(within(block).getByText(/needs a look at the data/i)).toBeInTheDocument()
  })

  it('is absent entirely while the selection flag is off', async () => {
    planAnalysis.mockResolvedValue(response({ selection: null }))
    await ask()
    await screen.findByRole('region', { name: /what this would compute/i })
    expect(screen.queryByRole('region', { name: /where this answer comes from/i }))
      .not.toBeInTheDocument()
  })
})
