import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import { FormulaDraftAction } from './FormulaDraftAction'
import * as api from '../api'

// What these tests protect, in the order it costs:
//
// 1. **Nothing spends until the button is pressed.** Rendering a candidate list must not author
//    anything, and neither must polling.
// 2. **A double-click is not a second bill**, and the screen says so rather than showing a fresh
//    start for a request that started nothing.
// 3. **BLOCKED renders as an answer**, FAILED as an outage. They send the user to different people.
// 4. **The stage words come from the SERVER**, so the API and the screen cannot disagree.

const READY: api.FormulaDraftStatus = {
  formula_draft_id: 'fd-1',
  considered_revision_id: 'crev-1',
  option_id: 'opt-a',
  state: 'READY',
  stage: 'Formula ready',
  terminal: true,
  formula_source: 'llm_authored',
  authoring_run_id: 'far-1',
  formula_content_hash: 'sha256:abc',
  formula: { operation: 'sum', window: '90d' },
  blockers: [],
  failure_reason: null,
}

function statusOf(over: Partial<api.FormulaDraftStatus>): api.FormulaDraftStatus {
  return { ...READY, ...over }
}

describe('FormulaDraftAction', () => {
  beforeEach(() => {
    vi.restoreAllMocks()
  })
  afterEach(() => {
    vi.restoreAllMocks()
  })

  function draw(over: Partial<Parameters<typeof FormulaDraftAction>[0]> = {}) {
    return render(
      <FormulaDraftAction
        consideredRevisionId="crev-1"
        optionId="opt-a"
        candidateName="Dormancy 90d"
        pollMs={5}
        {...over}
      />,
    )
  }

  // ══ NOTHING SPENDS WITHOUT A PRESS ═════════════════════════════════════════════════════════════
  it('AUTHORS NOTHING ON RENDER', async () => {
    // The rule that makes a list of 40 candidates affordable: showing them costs nothing. A version
    // that requested on mount would author 40 formulas for a user who only scrolled past.
    const request = vi.spyOn(api, 'requestFormulaDraft')
    const read = vi.spyOn(api, 'getFormulaDraft')

    draw()

    expect(request).not.toHaveBeenCalled()
    expect(read).not.toHaveBeenCalled()
    expect(screen.getByRole('button', { name: 'Draft formula' })).toBeEnabled()
  })

  it('says what the button will do, and what it will NOT do, before it is pressed', async () => {
    // "Does not select it" is the product rule stated where the decision is made. Without it a user
    // reasonably assumes drafting commits them, and stops pressing the button that would inform the
    // choice they are about to make.
    draw()
    expect(screen.getByText(/Does not select it/)).toBeInTheDocument()
  })

  it('POLLS WITH READS ONLY once a draft is in flight', async () => {
    // The read/write split. A polling loop that re-requested would author a formula every few
    // seconds with nobody asking — the same class of bug as a refresh that republishes.
    const request = vi.spyOn(api, 'requestFormulaDraft')
      .mockResolvedValue({ formula_draft_id: 'fd-1', status: 'requested', stage: 'queued',
                           created: true, detail: '' })
    const read = vi.spyOn(api, 'getFormulaDraft')
      .mockResolvedValue(statusOf({ state: 'AUTHORING', stage: 'Authoring formula…',
                                    terminal: false, formula: null }))

    draw()
    await userEvent.click(screen.getByRole('button', { name: 'Draft formula' }))

    await waitFor(() => expect(read.mock.calls.length).toBeGreaterThan(1))
    expect(request).toHaveBeenCalledTimes(1)
  })

  it('stops polling once the row says it has stopped moving', async () => {
    vi.spyOn(api, 'requestFormulaDraft')
      .mockResolvedValue({ formula_draft_id: 'fd-1', status: 'requested', stage: 'queued',
                           created: true, detail: '' })
    const read = vi.spyOn(api, 'getFormulaDraft').mockResolvedValue(READY)

    draw()
    await userEvent.click(screen.getByRole('button', { name: 'Draft formula' }))
    await screen.findByText('Formula ready')

    const settled = read.mock.calls.length
    await new Promise((r) => setTimeout(r, 30))
    expect(read.mock.calls.length).toBe(settled)
  })

  // ══ A DOUBLE-CLICK IS NOT A SECOND BILL ════════════════════════════════════════════════════════
  it('REPORTS A REUSED DRAFT rather than showing a start that did not happen', async () => {
    // `created: false` means the server found an identical draft. Showing the same "started" state
    // for both would tell the user they had just bought something they had not.
    vi.spyOn(api, 'requestFormulaDraft')
      .mockResolvedValue({ formula_draft_id: 'fd-1', status: 'requested', stage: 'queued',
                           created: false, detail: 'an identical draft already exists' })
    vi.spyOn(api, 'getFormulaDraft')
      .mockResolvedValue(statusOf({ state: 'AUTHORING', stage: 'Authoring formula…',
                                    terminal: false, formula: null }))

    draw()
    await userEvent.click(screen.getByRole('button', { name: 'Draft formula' }))

    expect(await screen.findByText('already drafting')).toBeInTheDocument()
  })

  it('DISABLES THE BUTTON while a draft is still moving', async () => {
    vi.spyOn(api, 'requestFormulaDraft')
      .mockResolvedValue({ formula_draft_id: 'fd-1', status: 'requested', stage: 'queued',
                           created: true, detail: '' })
    vi.spyOn(api, 'getFormulaDraft')
      .mockResolvedValue(statusOf({ state: 'CRITIC_REVIEW', stage: 'Critic review…',
                                    terminal: false, formula: null }))

    draw()
    await userEvent.click(screen.getByRole('button', { name: 'Draft formula' }))

    // The server would refuse a second spend anyway; the disabled button is what stops the user
    // WONDERING whether it did. The button says what pressing it does; the stage line says where
    // the work is — so the same sentence never appears twice.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Drafting…' })).toBeDisabled())
    expect(screen.getByText('Critic review…')).toBeInTheDocument()
  })

  it('offers a REDRAFT after a terminal result, because the catalog moves', async () => {
    vi.spyOn(api, 'requestFormulaDraft')
      .mockResolvedValue({ formula_draft_id: 'fd-1', status: 'requested', stage: 'queued',
                           created: true, detail: '' })
    vi.spyOn(api, 'getFormulaDraft').mockResolvedValue(READY)

    draw()
    await userEvent.click(screen.getByRole('button', { name: 'Draft formula' }))

    // Enabled, and named differently: a redraft against a moved catalog is a NEW identity and
    // therefore honestly a new spend, so the label must not pretend it is free.
    await waitFor(() =>
      expect(screen.getByRole('button', { name: 'Redraft formula' })).toBeEnabled())
  })

  // ══ BLOCKED IS AN ANSWER; FAILED IS AN OUTAGE ═════════════════════════════════════════════════
  it('RENDERS BLOCKED AS A RESULT, with the server’s own reasons and no alert', async () => {
    vi.spyOn(api, 'requestFormulaDraft')
      .mockResolvedValue({ formula_draft_id: 'fd-1', status: 'requested', stage: 'queued',
                           created: true, detail: '' })
    vi.spyOn(api, 'getFormulaDraft').mockResolvedValue(statusOf({
      state: 'BLOCKED',
      stage: 'Blocked',
      formula: null,
      blockers: [{ code: 'RENDERER_CANNOT_DISPATCH',
                   reason: 'the engine advertises no as_of_fx_join operator' }],
    }))

    draw()
    await userEvent.click(screen.getByRole('button', { name: 'Draft formula' }))

    // The server's sentence, verbatim — not a client re-explanation of the code.
    expect(await screen.findByText('the engine advertises no as_of_fx_join operator'))
      .toBeInTheDocument()
    expect(screen.getByText('RENDERER_CANNOT_DISPATCH')).toBeInTheDocument()
    // NOT an alert. A blocked formula is somebody's decision to make, not an incident to page on.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('RENDERS FAILED AS AN ALERT, which is the distinction that routes it to the right person',
     async () => {
    vi.spyOn(api, 'requestFormulaDraft')
      .mockResolvedValue({ formula_draft_id: 'fd-1', status: 'requested', stage: 'queued',
                           created: true, detail: '' })
    vi.spyOn(api, 'getFormulaDraft').mockResolvedValue(statusOf({
      state: 'FAILED', stage: 'Could not finish', formula: null,
      failure_reason: 'ProviderUnavailable: the authoring account is over its limit',
    }))

    draw()
    await userEvent.click(screen.getByRole('button', { name: 'Draft formula' }))

    const alert = await screen.findByRole('alert')
    expect(alert).toHaveTextContent('the authoring account is over its limit')
  })

  // ══ THE STAGE WORDS ARE THE SERVER'S ══════════════════════════════════════════════════════════
  it('SHOWS THE SERVER’S STAGE WORDING rather than a client translation of the state', async () => {
    // The same discipline the execution screen applies to blockers. Two vocabularies for one state
    // is how a user ends up reading "Validating…" in one place and "Checking" in another for the
    // identical row.
    vi.spyOn(api, 'requestFormulaDraft')
      .mockResolvedValue({ formula_draft_id: 'fd-1', status: 'requested', stage: 'queued',
                           created: true, detail: '' })
    vi.spyOn(api, 'getFormulaDraft').mockResolvedValue(statusOf({
      state: 'ADMISSION', stage: 'Checking execution support…', terminal: false, formula: null,
    }))

    draw()
    await userEvent.click(screen.getByRole('button', { name: 'Draft formula' }))

    expect(await screen.findByText('Checking execution support…')).toBeInTheDocument()
  })

  it('tells the parent what state it is in, so a tray can count without knowing about drafting',
     async () => {
    // The seam that keeps selection and drafting apart: the tray learns "2 ready, 2 need authoring"
    // from this callback, and never by reaching into the draft API itself.
    const onStateChange = vi.fn()
    vi.spyOn(api, 'requestFormulaDraft')
      .mockResolvedValue({ formula_draft_id: 'fd-1', status: 'requested', stage: 'queued',
                           created: true, detail: '' })
    vi.spyOn(api, 'getFormulaDraft').mockResolvedValue(READY)

    draw({ onStateChange })
    await userEvent.click(screen.getByRole('button', { name: 'Draft formula' }))

    await waitFor(() => expect(onStateChange).toHaveBeenCalledWith('opt-a', 'READY'))
  })

  it('keeps the live stage when a single POLL fails', async () => {
    // A failed read is not a failed draft. Replacing "Authoring formula…" with a network error
    // would say the work stopped when it is still running.
    vi.spyOn(api, 'requestFormulaDraft')
      .mockResolvedValue({ formula_draft_id: 'fd-1', status: 'requested', stage: 'queued',
                           created: true, detail: '' })
    const read = vi.spyOn(api, 'getFormulaDraft')
    read.mockResolvedValueOnce(statusOf({ state: 'AUTHORING', stage: 'Authoring formula…',
                                          terminal: false, formula: null }))
    read.mockRejectedValue(new api.ApiError(0, 'network unreachable'))

    draw()
    await userEvent.click(screen.getByRole('button', { name: 'Draft formula' }))
    await screen.findByText('Authoring formula…')

    await waitFor(() => expect(screen.getByRole('alert')).toBeInTheDocument())
    // The stage survived alongside the error, rather than being replaced by it.
    expect(screen.getByText('Authoring formula…')).toBeInTheDocument()
  })
})
