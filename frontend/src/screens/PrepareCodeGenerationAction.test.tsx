import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { PrepareCodeGenerationAction } from './PrepareCodeGenerationAction'

// 5b's action acceptance: the plan is a QUESTION, the cost confirmation appears exactly when an
// LLM member exists, and the one write happens from the one button whose label says what it
// records and spends.

const BODY = {
  considered_revision_id: 'crev-1',
  target_reading_revision_id: 'trr-1',
  selection_revision_ids: ['sel-1', 'sel-2'],
  environment_id: 'hdfc-local',
  logical_group_name: 'grp-1',
  declaration: {},
  execution_parameters: { engine_id: 'kedro-pyspark' },
}

function plan(overrides: Partial<api.CodeGenerationPlan> = {}): api.CodeGenerationPlan {
  return {
    job_content_identity_hash: 'jid-1',
    members: [
      { position: 0, selection_revision_id: 'sel-1', considered_revision_id: 'crev-1',
        option_id: 'opt-a', formula_strategy: 'REVIEWED_RECIPE_BLUEPRINT',
        blockers: [], warnings: [] },
      { position: 1, selection_revision_id: 'sel-2', considered_revision_id: 'crev-1',
        option_id: 'opt-b', formula_strategy: 'LLM_AUTHORED',
        blockers: [], warnings: ['LLM_AUTHORING_REQUIRED'] },
    ],
    deterministic_members: 1,
    llm_members: 1,
    estimated_provider_calls: 5,
    call_envelope_per_llm_member: 5,
    spend_approval_required: true,
    spend_approval: null,
    decision_preview: { allowed: true, blockers: [], warnings: [] },
    detail: 'preview',
    ...overrides,
  }
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('the plan is a question', () => {
  it('the first button says it reads only, and calls ONLY the plan endpoint', async () => {
    const planCall = vi.spyOn(api, 'planCodeGeneration').mockResolvedValue(plan())
    const requestCall = vi.spyOn(api, 'requestCodeGeneration')
    render(<PrepareCodeGenerationAction body={BODY} onRequested={vi.fn()} />)

    await userEvent.click(screen.getByRole('button', { name: /reads only/i }))

    expect(planCall).toHaveBeenCalledWith(BODY)
    expect(requestCall).not.toHaveBeenCalled()
  })

  it('renders the rail wording from server counts — methods, not readiness', async () => {
    vi.spyOn(api, 'planCodeGeneration').mockResolvedValue(plan())
    render(<PrepareCodeGenerationAction body={BODY} onRequested={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: /reads only/i }))

    expect(await screen.findByText('1 reviewed formula · no AI cost')).toBeTruthy()
    expect(screen.getByText(/1 AI formula required · up to 5 provider calls/)).toBeTruthy()
    // The route announcement is DISPLAYED (parent D3), never silently dropped.
    expect(screen.getByText(/LLM_AUTHORING_REQUIRED/)).toBeTruthy()
  })
})

describe('the cost confirmation', () => {
  it('appears exactly when an LLM member exists, and gates the act until filled', async () => {
    vi.spyOn(api, 'planCodeGeneration').mockResolvedValue(plan())
    render(<PrepareCodeGenerationAction body={BODY} onRequested={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: /reads only/i }))

    expect(await screen.findByText(/confirm the ai cost ceiling/i)).toBeTruthy()
    const act = screen.getByRole('button', { name: /prepare formulas and code/i })
    expect(act).toHaveProperty('disabled', true)

    await userEvent.type(screen.getByLabelText(/token ceiling/i), '200000')
    await userEvent.type(screen.getByLabelText(/cost ceiling/i), '12.50')
    expect(act).toHaveProperty('disabled', false)
  })

  it('does NOT appear for an all-deterministic plan, and the label says no AI spend', async () => {
    vi.spyOn(api, 'planCodeGeneration').mockResolvedValue(plan({
      llm_members: 0, estimated_provider_calls: 0, spend_approval_required: false,
      members: [plan().members[0]],
    }))
    render(<PrepareCodeGenerationAction body={BODY} onRequested={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: /reads only/i }))

    expect(screen.queryByText(/confirm the ai cost ceiling/i)).toBeNull()
    expect(await screen.findByRole('button', { name: /no AI spend/i })).toBeTruthy()
  })
})

describe('the act', () => {
  it('sends the confirmed ceiling with the request and reports the job id', async () => {
    vi.spyOn(api, 'planCodeGeneration').mockResolvedValue(plan())
    const requestCall = vi.spyOn(api, 'requestCodeGeneration').mockResolvedValue({
      job_id: 'cgj-9', created: true, detail: 'recorded',
    })
    const onRequested = vi.fn()
    render(<PrepareCodeGenerationAction body={BODY} onRequested={onRequested} />)
    await userEvent.click(screen.getByRole('button', { name: /reads only/i }))
    await userEvent.type(await screen.findByLabelText(/token ceiling/i), '200000')
    await userEvent.type(screen.getByLabelText(/cost ceiling/i), '12.50')
    await userEvent.click(screen.getByRole('button', { name: /prepare formulas and code/i }))

    expect(onRequested).toHaveBeenCalledWith('cgj-9')
    const sent = requestCall.mock.calls[0][0]
    expect(sent.spend_approval?.max_calls).toBe(5)
    expect(sent.spend_approval?.max_tokens).toBe(200000)
    expect(sent.spend_approval?.max_cost).toBe('12.50')
  })

  it('a server refusal is shown, not swallowed', async () => {
    vi.spyOn(api, 'planCodeGeneration').mockResolvedValue(plan({
      llm_members: 0, spend_approval_required: false, members: [plan().members[0]],
    }))
    vi.spyOn(api, 'requestCodeGeneration').mockRejectedValue(
      new api.ApiError(409, 'these members are refused before any spend'))
    render(<PrepareCodeGenerationAction body={BODY} onRequested={vi.fn()} />)
    await userEvent.click(screen.getByRole('button', { name: /reads only/i }))
    await userEvent.click(await screen.findByRole('button',
      { name: /prepare formulas and code/i }))

    expect((await screen.findByRole('alert')).textContent)
      .toContain('refused before any spend')
  })
})
