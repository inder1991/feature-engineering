import { render, screen } from '@testing-library/react'
import { describe, expect, it, vi } from 'vitest'
import { MaterializationRunScreen } from './MaterializationRunScreen'

// One render test per outcome the server can send, because each is a different SENTENCE an operator
// acts on — and two of them are the ones this screen exists to stop getting wrong:
//
//   * `refused` must read as an OUTCOME. PUBLICATION_REFUSED is G-1's success terminal (the project
//     compiled, rendered, sealed and PROVED its build; the request is `committed`), so a screen that
//     painted it as a failure would tell an operator their feature was rejected when nothing about
//     it was.
//   * `published_object === null` must say "not published yet" and NEVER a table name. The platform
//     names `sandbox_feature.<group>` for every group whether or not anything was published there,
//     and showing it would hand an operator a table that does not resolve.

const BASE = {
  request_id: 'mreq_1',
  logical_group_name: 'cif_daily',
  requested_by: 'analyst@bank',
  authorized_roles: ['feature_engineer'],
  idempotency_key: 'key-1',
  activation_state: {},
  considered_revision_id: null,
  option_id: null,
  lifecycle_state: 'committed',
  terminal: true,
  generation_id: 'gen_1',
  run_id: 'mrun_1',
  run_status: 'PUBLICATION_REFUSED',
  run_status_reason: null,
  terminal_event: 'PUBLICATION_REFUSED',
  outcome: 'refused' as const,
  refusal_code: 'CAPABILITY_UNPROVEN',
  published_object: null,
  published_generation_id: null,
  published_at: null,
  resolved_input_digest: null,
  requested_at: '2026-08-15T10:00:00+00:00',
  accepted_at: '2026-08-15T10:00:01+00:00',
  lease_expires_at: null,
  updated_at: '2026-08-15T10:05:00+00:00',
}

function serve(overrides: Record<string, unknown> = {}) {
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
    ok: true, json: async () => ({ ...BASE, ...overrides }), headers: { get: () => null },
  }))
}

describe('materialization run screen', () => {
  it('renders a REFUSED run as an outcome, never as an error', async () => {
    serve()

    render(<MaterializationRunScreen requestId="mreq_1" />)

    const outcome = await screen.findByTestId('outcome')
    expect(outcome).toHaveTextContent('Publication refused')
    // Neutral, not `bad`: the tone IS the claim, and the tone is what a stylesheet paints red.
    expect(outcome).toHaveAttribute('data-tone', 'neutral')
    expect(screen.getByText(/What was refused is publication/)).toBeInTheDocument()
    expect(screen.getByText('CAPABILITY_UNPROVEN')).toBeInTheDocument()
    expect(screen.queryByRole('alert')).toBeNull()
  })

  it('says "not published yet" and invents no table name', async () => {
    serve()

    render(<MaterializationRunScreen requestId="mreq_1" />)

    expect(await screen.findByTestId('not-published')).toHaveTextContent(/Not published yet/)
    expect(screen.queryByText(/sandbox_feature/)).toBeNull()
    expect(screen.queryByText(/cif_daily\./)).toBeNull()
  })

  it('renders a PUBLISHED run with the object the server named', async () => {
    serve({
      outcome: 'published',
      terminal_event: 'PUBLISHED',
      run_status: 'PUBLISHED',
      refusal_code: null,
      published_object: 'sandbox_feature.cif_daily',
      published_generation_id: 'gen_1',
      published_at: '2026-08-15T10:04:00+00:00',
    })

    render(<MaterializationRunScreen requestId="mreq_1" />)

    expect(await screen.findByTestId('outcome')).toHaveAttribute('data-tone', 'good')
    expect(screen.getByText('sandbox_feature.cif_daily')).toBeInTheDocument()
    expect(screen.queryByTestId('not-published')).toBeNull()
    expect(screen.getByText('nothing was refused')).toBeInTheDocument()
  })

  it('renders a FAILED run and shows no refusal code for it', async () => {
    serve({
      outcome: 'failed', lifecycle_state: 'failed', terminal_event: 'RUN_FAILED',
      run_status: 'RUN_FAILED', refusal_code: null,
    })

    render(<MaterializationRunScreen requestId="mreq_1" />)

    expect(await screen.findByTestId('outcome')).toHaveAttribute('data-tone', 'bad')
    expect(screen.getByText('nothing was refused')).toBeInTheDocument()
  })

  it('renders a PENDING run and reports the SERVER\'s reason for the missing status', async () => {
    serve({
      outcome: 'pending', lifecycle_state: 'requested', terminal: false,
      generation_id: null, run_id: null, run_status: null,
      run_status_reason: 'no run exists yet: this request is \'requested\'',
      terminal_event: null, refusal_code: null, accepted_at: null,
    })

    render(<MaterializationRunScreen requestId="mreq_1" />)

    expect(await screen.findByTestId('outcome')).toHaveTextContent('In progress')
    // The reason is the SERVER's sentence, verbatim — this screen composes none of its own.
    expect(screen.getByText(/no run exists yet/)).toBeInTheDocument()
    expect(screen.getByText('no project was sealed')).toBeInTheDocument()
  })

  it('renders a NEVER-ACCEPTED request as legible, naming the lane configuration', async () => {
    serve({
      outcome: 'never_accepted', lifecycle_state: 'requested', terminal: false,
      generation_id: null, run_id: null, run_status: null,
      run_status_reason: 'no run exists yet: this request is \'requested\'',
      terminal_event: null, refusal_code: null, accepted_at: null,
    })

    render(<MaterializationRunScreen requestId="mreq_1" />)

    expect(await screen.findByTestId('outcome')).toHaveTextContent('Never accepted')
    expect(screen.getByText(/Check the lane configuration/)).toBeInTheDocument()
    expect(screen.getByText(/fresh idempotency key/)).toBeInTheDocument()
  })

  it('shows an honest empty state when no request is named', () => {
    render(<MaterializationRunScreen requestId="" />)

    expect(screen.getByText(/No request named/)).toBeInTheDocument()
    expect(screen.queryByTestId('outcome')).toBeNull()
    expect(screen.queryByTestId('not-published')).toBeNull()
  })

  it('surfaces the server\'s own detail when the read fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({
      ok: false, status: 404, statusText: 'Not Found',
      json: async () => ({ detail: 'materialization request not found' }),
      headers: { get: () => null },
    }))

    render(<MaterializationRunScreen requestId="mreq_missing" />)

    expect(await screen.findByRole('alert')).toHaveTextContent('materialization request not found')
  })
})
