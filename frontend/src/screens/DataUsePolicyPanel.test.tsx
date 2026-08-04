import { render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { DataUsePolicyPanel } from './DataUsePolicyPanel'

// The data-use policy panel (D14). It is the place the feature flow's refusal sends people, so the
// two things it MUST get right are the framing (absence is "nobody decided", never a fault) and the
// revocation guard (one click here breaks a running model).

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getDataUsePolicies: vi.fn(),
    approveDataUsePolicy: vi.fn(),
    revokeDataUsePolicy: vi.fn(),
  }
})
const getDataUsePolicies = vi.mocked(api.getDataUsePolicies)
const approveDataUsePolicy = vi.mocked(api.approveDataUsePolicy)
const revokeDataUsePolicy = vi.mocked(api.revokeDataUsePolicy)

beforeEach(() => {
  getDataUsePolicies.mockReset()
  approveDataUsePolicy.mockReset()
  revokeDataUsePolicy.mockReset()
})

function state(over: Partial<api.DataUsePolicyState> = {}): api.DataUsePolicyState {
  return {
    concept_name: 'pep_flag',
    description: 'Politically exposed person marker.',
    group: 'flag',
    status: 'none',
    pointer_version: 0,
    purpose: null,
    revision_id: null,
    approved_by: null,
    approved_at: null,
    declared_by: null,
    updated_at: null,
    ...over,
  }
}

function listing(concepts: api.DataUsePolicyState[]): api.DataUsePolicyListing {
  return { concepts, purpose_bounds: { min: 8, max: 300 } }
}

async function renderPanel(body: api.DataUsePolicyListing) {
  getDataUsePolicies.mockResolvedValue(body)
  render(<DataUsePolicyPanel />)
  await screen.findByTestId('data-use-policies')
}

it('frames an undecided concept as undecided, never as a fault', async () => {
  await renderPanel(listing([state()]))

  const row = screen.getByTestId('dup-pep_flag')
  expect(row).toHaveTextContent(/not approved for feature use/i)
  // the words that would turn "nobody decided yet" into "something is broken"
  expect(row.textContent).not.toMatch(/error|failed|blocked|missing|invalid|violation/i)
  expect(within(row).getByRole('button', { name: /approve for feature use/i }))
    .toBeInTheDocument()
  expect(screen.getByTestId('data-use-policies').textContent)
    .toMatch(/nobody has had to decide about yet/i)
})

it('separates "nobody decided" from "somebody withdrew a decision"', async () => {
  await renderPanel(listing([
    state({ concept_name: 'geolocation', status: 'revoked', purpose: 'impossible travel',
            pointer_version: 2, declared_by: 'user:priya', updated_at: '2026-08-04T09:00:00Z' }),
    state(),
  ]))

  const revoked = screen.getByTestId('dup-geolocation')
  expect(revoked).toHaveTextContent(/approval withdrawn/i)
  expect(revoked).toHaveTextContent(/impossible travel/i)      // what was withdrawn is shown
  expect(revoked).toHaveTextContent(/user:priya/)
  // it sits under "Decided", not beside the concepts nobody ever ruled on
  expect(within(screen.getByTestId('dup-decided')).getByTestId('dup-geolocation'))
    .toBeInTheDocument()
  expect(within(screen.getByTestId('dup-undecided')).getByTestId('dup-pep_flag'))
    .toBeInTheDocument()
})

it('shows an active policy with its purpose, approver and date', async () => {
  await renderPanel(listing([
    state({ status: 'active', purpose: 'AML transaction monitoring', pointer_version: 1,
            approved_by: 'user:priya', declared_by: 'user:priya',
            updated_at: '2026-08-04T09:00:00Z',
            revision_id: `pup_${'a'.repeat(64)}` }),
  ]))

  const row = screen.getByTestId('dup-pep_flag')
  expect(row).toHaveTextContent(/approved for feature use/i)
  expect(row).toHaveTextContent(/AML transaction monitoring/)
  expect(row).toHaveTextContent(/user:priya/)
  expect(within(row).getByRole('button', { name: /withdraw approval/i })).toBeInTheDocument()
})

it('approves with a purpose and echoes the CAS version exactly as it was read', async () => {
  await renderPanel(listing([state()]))
  approveDataUsePolicy.mockResolvedValue({
    concept_name: 'pep_flag', revision_id: `pup_${'a'.repeat(64)}`, pointer_version: 1,
    status: 'active',
  })
  getDataUsePolicies.mockResolvedValue(listing([
    state({ status: 'active', purpose: 'AML transaction monitoring', pointer_version: 1,
            declared_by: 'user:priya' }),
  ]))

  await userEvent.click(screen.getByRole('button', { name: /approve for feature use/i }))
  await userEvent.type(screen.getByLabelText(/what will this data be used for/i),
                       'AML transaction monitoring')
  await userEvent.click(screen.getByRole('button', { name: /^approve for feature use$/i }))

  expect(approveDataUsePolicy).toHaveBeenCalledWith('pep_flag', {
    expectedPointerVersion: 0, purpose: 'AML transaction monitoring',
  })
  expect(await screen.findByTestId('dup-notice')).toHaveTextContent(/now approved/i)
})

it('says a single approval is enough, because that is a deliberate decision (D14)', async () => {
  await renderPanel(listing([state()]))
  await userEvent.click(screen.getByRole('button', { name: /approve for feature use/i }))
  expect(screen.getByText(/one approval is enough/i)).toBeInTheDocument()
  expect(screen.getByText(/no second signature/i)).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: /confirm|second approver/i })).toBeNull()
})

it('will not submit a purpose shorter than the server would accept', async () => {
  await renderPanel(listing([state()]))
  await userEvent.click(screen.getByRole('button', { name: /approve for feature use/i }))
  const submit = screen.getByRole('button', { name: /^approve for feature use$/i })
  expect(submit).toBeDisabled()
  await userEvent.type(screen.getByLabelText(/what will this data be used for/i), 'AML')
  expect(submit).toBeDisabled()
  await userEvent.type(screen.getByLabelText(/what will this data be used for/i), ' monitoring')
  expect(submit).toBeEnabled()
  expect(approveDataUsePolicy).not.toHaveBeenCalled()
})

it('asks before withdrawing, and says what happens next', async () => {
  await renderPanel(listing([
    state({ status: 'active', purpose: 'AML transaction monitoring', pointer_version: 1 }),
  ]))

  await userEvent.click(screen.getByRole('button', { name: /withdraw approval/i }))
  const dialog = screen.getByRole('alertdialog')
  expect(dialog).toHaveTextContent(/refused again/i)
  expect(dialog).toHaveTextContent(/stays on the record/i)
  expect(revokeDataUsePolicy).not.toHaveBeenCalled()

  // and the way out of the dialog does not withdraw anything
  await userEvent.click(within(dialog).getByRole('button', { name: /keep it approved/i }))
  expect(revokeDataUsePolicy).not.toHaveBeenCalled()
  expect(screen.queryByRole('alertdialog')).toBeNull()
})

it('withdraws on confirmation and reports what changed', async () => {
  await renderPanel(listing([
    state({ status: 'active', purpose: 'AML transaction monitoring', pointer_version: 1 }),
  ]))
  revokeDataUsePolicy.mockResolvedValue({
    concept_name: 'pep_flag', revision_id: `pup_${'b'.repeat(64)}`, pointer_version: 2,
    status: 'revoked',
  })
  getDataUsePolicies.mockResolvedValue(listing([
    state({ status: 'revoked', purpose: 'AML transaction monitoring', pointer_version: 2 }),
  ]))

  await userEvent.click(screen.getByRole('button', { name: /withdraw approval/i }))
  await userEvent.click(
    within(screen.getByRole('alertdialog')).getByRole('button', { name: /^withdraw approval$/i }))

  expect(revokeDataUsePolicy).toHaveBeenCalledWith('pep_flag', { expectedPointerVersion: 1 })
  expect(await screen.findByTestId('dup-notice')).toHaveTextContent(/refused again/i)
})

it('keeps the other person\'s decision on a 409 rather than retrying over it', async () => {
  await renderPanel(listing([state()]))
  approveDataUsePolicy.mockRejectedValue(new api.ApiError(409, 'advanced past version 0'))
  getDataUsePolicies.mockResolvedValue(listing([
    state({ status: 'active', purpose: 'sanctions screening', pointer_version: 1,
            declared_by: 'user:sam' }),
  ]))

  await userEvent.click(screen.getByRole('button', { name: /approve for feature use/i }))
  await userEvent.type(screen.getByLabelText(/what will this data be used for/i),
                       'AML transaction monitoring')
  await userEvent.click(screen.getByRole('button', { name: /^approve for feature use$/i }))

  expect(await screen.findByTestId('dup-notice')).toHaveTextContent(/someone else decided/i)
  expect(screen.getByTestId('dup-pep_flag')).toHaveTextContent(/sanctions screening/)
  expect(approveDataUsePolicy).toHaveBeenCalledTimes(1)
})

it('reports a server refusal in the server\'s own words', async () => {
  await renderPanel(listing([state()]))
  approveDataUsePolicy.mockRejectedValue(
    new api.ApiError(403, 'requires the platform-admin role'))

  await userEvent.click(screen.getByRole('button', { name: /approve for feature use/i }))
  await userEvent.type(screen.getByLabelText(/what will this data be used for/i),
                       'AML transaction monitoring')
  await userEvent.click(screen.getByRole('button', { name: /^approve for feature use$/i }))

  expect(await screen.findByTestId('dup-notice'))
    .toHaveTextContent(/requires the platform-admin role/i)
})

it('renders nothing until the listing arrives, and never a half-panel', async () => {
  getDataUsePolicies.mockReturnValue(new Promise(() => {}))
  const { container } = render(<DataUsePolicyPanel />)
  expect(container).toBeEmptyDOMElement()
})
