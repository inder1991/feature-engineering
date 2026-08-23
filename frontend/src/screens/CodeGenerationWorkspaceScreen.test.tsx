import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { CodeGenerationWorkspaceScreen } from './CodeGenerationWorkspaceScreen'

// 5b's workspace acceptance: SEVEN stages (sandbox execution ≠ sandbox publication — merging
// them re-creates the evasion parent §4 undid), every word from the server, warnings DISPLAYED,
// honest absence, and cancel as the one write.

const JOB: api.CodeGenerationJob = {
  job_id: 'cgj-1',
  status: 'AUTHORING',
  terminal: false,
  requested_by: 'user:sam',
  build_set_revision_id: null,
  generation_request_id: null,
  sealed_artifact_id: null,
  environment_id: 'hdfc-local',
  logical_group_name: 'grp-1',
  terminal_detail: null,
  members: [{
    position: 0,
    selection_revision_id: 'sel-1',
    option_id: 'opt-a',
    formula_strategy: 'LLM_AUTHORED',
    member_state: 'AUTHORING',
    formula_draft_id: 'fd-1',
    selection_formula_binding_id: null,
    blockers: [],
    warnings: ['REVIEWED_LANE_UNAVAILABLE'],
  }],
  actions: [
    { action: 'AUTHOR_FORMULA', resource_identity_hash: null, authorization_revision_id: null,
      decision_revision_id: null, state: 'PERFORMED' },
    { action: 'GENERATE_PREVIEW', resource_identity_hash: null, authorization_revision_id: null,
      decision_revision_id: null, state: 'PENDING' },
  ],
  events: [{ event_seq: 1, stage: 'REQUESTED', detail: {}, recorded_at: '2026-08-23T00:00:00Z' }],
}

beforeEach(() => {
  vi.restoreAllMocks()
})

describe('the seven-stage journey', () => {
  it('renders SEVEN stages — sandbox execution and publication stay separate acts', async () => {
    vi.spyOn(api, 'getCodeGenerationJob').mockResolvedValue(JOB)
    render(<CodeGenerationWorkspaceScreen jobId="cgj-1" />)
    await screen.findByText('Preparing formulas')
    const stages = screen.getAllByRole('listitem').filter(li => li.closest('.cgw-stages'))
    expect(stages.map(s => s.textContent)).toEqual([
      'Selected', 'Preparing formulas', 'Validating', 'Generating code', 'Code ready',
      'Sandbox executed', 'Sandbox published',
    ])
  })

  it('links to the EXECUTION workspace once code is sealed — the sandbox acts live there', async () => {
    vi.spyOn(api, 'getCodeGenerationJob').mockResolvedValue({
      ...JOB, status: 'PREVIEW_READY', terminal: true, sealed_artifact_id: 'art-9',
      generation_request_id: 'gen-9',
    })
    const navigate = vi.fn()
    render(<CodeGenerationWorkspaceScreen jobId="cgj-1" navigate={navigate} />)
    await userEvent.click(await screen.findByRole('button',
      { name: /open the execution workspace/i }))
    expect(navigate).toHaveBeenCalledWith('feature-execution', {
      artifact_id: 'art-9', environment_id: 'hdfc-local', group: 'grp-1',
    })
  })
})

describe('server-owned words', () => {
  it('DISPLAYS member warnings — a warning computed and dropped is worse than none', async () => {
    vi.spyOn(api, 'getCodeGenerationJob').mockResolvedValue(JOB)
    render(<CodeGenerationWorkspaceScreen jobId="cgj-1" />)
    expect(await screen.findByText('REVIEWED_LANE_UNAVAILABLE')).toBeTruthy()
  })

  it('maps the SERVER strategy vocabulary to the plain-language badge', async () => {
    vi.spyOn(api, 'getCodeGenerationJob').mockResolvedValue(JOB)
    render(<CodeGenerationWorkspaceScreen jobId="cgj-1" />)
    expect(await screen.findByText('AI-authored formula')).toBeTruthy()
  })

  it('renders honest absence for a binding that does not exist yet', async () => {
    vi.spyOn(api, 'getCodeGenerationJob').mockResolvedValue(JOB)
    render(<CodeGenerationWorkspaceScreen jobId="cgj-1" />)
    expect(await screen.findByText('not yet pinned')).toBeTruthy()
  })

  it('a BLOCKED job shows the terminal detail the server sent, verbatim', async () => {
    vi.spyOn(api, 'getCodeGenerationJob').mockResolvedValue({
      ...JOB, status: 'BLOCKED', terminal: true,
      terminal_detail: { members: [{ position: 0, blockers: ['FORMULA_DRAFT_RETIRED'] }] },
    })
    render(<CodeGenerationWorkspaceScreen jobId="cgj-1" />)
    expect((await screen.findByRole('note')).textContent).toContain('FORMULA_DRAFT_RETIRED')
  })
})

describe('the one write', () => {
  it('cancel is a button whose label states what it stops — and what it cannot', async () => {
    vi.spyOn(api, 'getCodeGenerationJob').mockResolvedValue(JOB)
    const cancel = vi.spyOn(api, 'cancelCodeGenerationJob').mockResolvedValue({
      job_id: 'cgj-1', status: 'CANCELLED', detail: 'stopped',
    })
    render(<CodeGenerationWorkspaceScreen jobId="cgj-1" />)
    await userEvent.click(await screen.findByRole('button', { name: /stop future stages/i }))
    expect(cancel).toHaveBeenCalledWith('cgj-1')
  })

  it('a terminal job offers NO cancel button', async () => {
    vi.spyOn(api, 'getCodeGenerationJob').mockResolvedValue({
      ...JOB, status: 'PREVIEW_READY', terminal: true,
    })
    render(<CodeGenerationWorkspaceScreen jobId="cgj-1" />)
    await screen.findByText('Preparing formulas')
    expect(screen.queryByRole('button', { name: /stop future stages/i })).toBeNull()
  })
})

describe('empty and error states', () => {
  it('a missing job id renders an empty workspace that says so', () => {
    render(<CodeGenerationWorkspaceScreen jobId="" />)
    expect(screen.getByText(/no job id was provided/i)).toBeTruthy()
  })
})
