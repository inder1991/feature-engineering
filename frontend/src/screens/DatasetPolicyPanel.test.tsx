import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { DatasetPolicyPanel } from './DatasetPolicyPanel'

// The Release-B policy editor. It exists because a policy store with no authoring surface is dead
// code — and worse, the D12.7 escape hatch makes the policy the operational declaration for an
// upload-only catalog, so with no way to write one such a catalog stays unselectable.
//
// The rules under test: flag-off renders NOTHING; an undeclared policy is framed as undeclared and
// not as a failure; a 409 keeps the author's draft and shows the other version beside it; ambiguity
// is stated rather than resolved; and nothing about the need is inferred from the dataset.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    getTemporalPolicy: vi.fn(),
    putTemporalPolicy: vi.fn(),
    getServingPolicy: vi.fn(),
    putServingPolicy: vi.fn(),
  }
})
const getTemporalPolicy = vi.mocked(api.getTemporalPolicy)
const putTemporalPolicy = vi.mocked(api.putTemporalPolicy)
const getServingPolicy = vi.mocked(api.getServingPolicy)
const putServingPolicy = vi.mocked(api.putServingPolicy)

beforeEach(() => {
  getTemporalPolicy.mockReset()
  putTemporalPolicy.mockReset()
  getServingPolicy.mockReset()
  putServingPolicy.mockReset()
})

const PROVENANCE: api.PolicyProvenance = {
  evidence: [{
    producer: 'human', strength: 'confirmed', lifecycle: 'active',
    producer_ref: 'user:priya', evidence_id: null,
  }],
  decision_refs: [],
}

function temporalView(over: Partial<api.TemporalPolicyView> = {}): api.TemporalPolicyView {
  return {
    dataset_logical_ref: 'bank::public.customer_segment',
    load_bearing_temporal_storage_model: null,
    displayed_temporal_storage_model: null,
    pointer_version: 0,
    policy: null,
    ...over,
  }
}

function temporalRevision(
  over: Partial<api.TemporalPolicyRevision> = {},
): api.TemporalPolicyRevision {
  return {
    revision_id: `dtp_${'a'.repeat(64)}`,
    content_hash: 'a'.repeat(64),
    temporal_storage_model: 'scd2',
    current_selection: 'current_record',
    historical_selection: 'valid_at_report_cutoff',
    effective_from_ref: 'bank::public.customer_segment.effective_from',
    effective_to_ref: 'bank::public.customer_segment.effective_to',
    snapshot_ref: null,
    current_flag_ref: 'bank::public.customer_segment.is_current',
    availability_ref: null,
    tie_break_refs: [],
    provenance: PROVENANCE,
    ...over,
  }
}

async function renderPanel(view: api.TemporalPolicyView) {
  getTemporalPolicy.mockResolvedValue(view)
  render(<DatasetPolicyPanel source="bank" objectRef="public.customer_segment" kind="table" />)
  await screen.findByTestId('dataset-policies')
}

it('renders nothing at all when the surface is off (404)', async () => {
  getTemporalPolicy.mockRejectedValue(new api.ApiError(404, 'Not Found'))
  const { container } = render(
    <DatasetPolicyPanel source="bank" objectRef="public.customer_segment" kind="table" />)
  await vi.waitFor(() => expect(getTemporalPolicy).toHaveBeenCalled())
  expect(container.querySelector('[data-testid="temporal-policy"]')).toBeNull()
})

it('renders nothing for a column asset — policies are dataset-scoped', () => {
  const { container } = render(
    <DatasetPolicyPanel source="bank" objectRef="public.customer_segment.cif_id" kind="column" />)
  expect(container).toBeEmptyDOMElement()
  expect(getTemporalPolicy).not.toHaveBeenCalled()
})

it('frames an undeclared row policy as undeclared, not as an error', async () => {
  await renderPanel(temporalView())
  const empty = screen.getByTestId('temporal-policy-empty')
  expect(empty).toHaveTextContent(/nobody has declared how this dataset stores history yet/i)
  expect(empty.textContent).not.toMatch(/error|failed|blocked|invalid|missing/i)
  expect(screen.getByRole('button', { name: /declare row policy/i })).toBeInTheDocument()
})

it('says the policy IS the declaration when nothing governs the model yet', async () => {
  await renderPanel(temporalView())
  await userEvent.click(screen.getByRole('button', { name: /declare row policy/i }))
  expect(screen.getByTestId('temporal-policy-declares'))
    .toHaveTextContent(/nobody has reviewed a storage model/i)
  expect(screen.queryByTestId('temporal-policy-governed')).toBeNull()
})

it('says a reviewed classification wins when one exists', async () => {
  await renderPanel(temporalView({ load_bearing_temporal_storage_model: 'snapshot' }))
  await userEvent.click(screen.getByRole('button', { name: /declare row policy/i }))
  const governed = screen.getByTestId('temporal-policy-governed')
  expect(governed).toHaveTextContent(/snapshot/)
  expect(governed).toHaveTextContent(/refused/i)
})

it('names explicit_only as a limitation of the dataset, not a failure of the question', async () => {
  await renderPanel(temporalView({
    pointer_version: 1,
    declared_by: 'user:priya',
    policy: temporalRevision({ temporal_storage_model: 'current_only',
                               historical_selection: 'explicit_only' }),
  }))
  expect(screen.getByTestId('temporal-policy'))
    .toHaveTextContent(/cannot answer one on its own/i)
})

it('echoes the pointer version it read, and 0 claims the first write', async () => {
  await renderPanel(temporalView())
  putTemporalPolicy.mockResolvedValue({
    dataset_logical_ref: 'bank::public.customer_segment',
    revision_id: `dtp_${'b'.repeat(64)}`, pointer_version: 1,
  })
  await userEvent.click(screen.getByRole('button', { name: /declare row policy/i }))
  await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
  await vi.waitFor(() => expect(putTemporalPolicy).toHaveBeenCalled())
  expect(putTemporalPolicy.mock.calls[0][2].expectedPointerVersion).toBe(0)
})

it('keeps the author draft on a 409 and shows the other version beside it', async () => {
  await renderPanel(temporalView({ pointer_version: 1, policy: temporalRevision() }))
  await userEvent.click(screen.getByRole('button', { name: /edit row policy/i }))
  const availability = screen.getByLabelText(/available-from column/i)
  await userEvent.type(availability, 'bank::public.customer_segment.loaded_on')

  putTemporalPolicy.mockRejectedValue(new api.ApiError(409, 'pointer advanced'))
  // The reload behind the conflict returns THEIR version at a newer pointer.
  getTemporalPolicy.mockResolvedValue(temporalView({
    pointer_version: 2,
    policy: temporalRevision({ temporal_storage_model: 'snapshot' }),
  }))
  await userEvent.click(screen.getByRole('button', { name: /^save$/i }))

  await screen.findByTestId('temporal-policy-theirs')
  expect(screen.getByRole('status')).toHaveTextContent(/neither were your words/i)
  // THE POINT: the draft survived the reload that refreshed the CAS anchor.
  expect(screen.getByLabelText(/available-from column/i))
    .toHaveValue('bank::public.customer_segment.loaded_on')
  expect(screen.getByTestId('temporal-policy-theirs')).toHaveTextContent(/snapshot/)
})

it('re-anchors the next save on the version it just read, not the one that lost', async () => {
  await renderPanel(temporalView({ pointer_version: 1, policy: temporalRevision() }))
  await userEvent.click(screen.getByRole('button', { name: /edit row policy/i }))
  putTemporalPolicy.mockRejectedValueOnce(new api.ApiError(409, 'pointer advanced'))
  getTemporalPolicy.mockResolvedValue(temporalView({
    pointer_version: 2, policy: temporalRevision(),
  }))
  await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
  await screen.findByTestId('temporal-policy-theirs')

  putTemporalPolicy.mockResolvedValue({
    dataset_logical_ref: 'bank::public.customer_segment',
    revision_id: `dtp_${'c'.repeat(64)}`, pointer_version: 3,
  })
  await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
  await vi.waitFor(() => expect(putTemporalPolicy).toHaveBeenCalledTimes(2))
  expect(putTemporalPolicy.mock.calls[1][2].expectedPointerVersion).toBe(2)
})

// ── serving policy ──────────────────────────────────────────────────────────────────────────────

it('never guesses the need — nothing is loaded until the operator names it', async () => {
  await renderPanel(temporalView())
  expect(getServingPolicy).not.toHaveBeenCalled()
  expect(screen.getByTestId('serving-policy'))
    .toHaveTextContent(/keyed by the NEED, not by this dataset/i)
})

it('frames an undeclared serving policy as normal, and states ambiguity plainly', async () => {
  await renderPanel(temporalView())
  getServingPolicy.mockResolvedValue({
    entity_id: 'customer', need_role: 'population', serving_purpose: 'analytical',
    pointer_version: 0, ambiguous: false, policy: null,
  })
  await userEvent.type(screen.getByLabelText(/^entity$/i), 'customer')
  await userEvent.click(screen.getByRole('button', { name: /load policy/i }))
  const empty = await screen.findByTestId('serving-policy-empty')
  expect(empty).toHaveTextContent(/that is normal/i)
  expect(empty.textContent).not.toMatch(/error|failed|blocked/i)

  getServingPolicy.mockResolvedValue({
    entity_id: 'customer', need_role: 'population', serving_purpose: 'analytical',
    pointer_version: 1, ambiguous: true,
    policy: {
      revision_id: `dsp_${'a'.repeat(64)}`, content_hash: 'a'.repeat(64),
      eligible_dataset_refs: ['bank::public.customer_master', 'bank::public.customer_ods'],
      preferred_dataset_refs: ['bank::public.customer_master', 'bank::public.customer_ods'],
      provenance: PROVENANCE,
    },
  })
  await userEvent.click(screen.getByRole('button', { name: /load policy/i }))
  const ambiguous = await screen.findByTestId('serving-policy-ambiguous')
  expect(ambiguous).toHaveTextContent(/cannot pick one dataset on its own/i)
  expect(ambiguous).toHaveTextContent(/will ask rather than guess/i)
})

it('sends the eligible and preferred lists as typed, with the read pointer version', async () => {
  await renderPanel(temporalView())
  getServingPolicy.mockResolvedValue({
    entity_id: 'customer', need_role: 'population', serving_purpose: 'analytical',
    pointer_version: 3, ambiguous: false,
    policy: {
      revision_id: `dsp_${'a'.repeat(64)}`, content_hash: 'a'.repeat(64),
      eligible_dataset_refs: ['bank::public.customer_master'],
      preferred_dataset_refs: ['bank::public.customer_master'],
      provenance: PROVENANCE,
    },
  })
  await userEvent.type(screen.getByLabelText(/^entity$/i), 'customer')
  await userEvent.click(screen.getByRole('button', { name: /load policy/i }))
  await screen.findByLabelText(/may serve it/i)

  putServingPolicy.mockResolvedValue({
    revision_id: `dsp_${'b'.repeat(64)}`, pointer_version: 4, ambiguous: false,
  })
  await userEvent.click(screen.getByRole('button', { name: /add this dataset/i }))
  await userEvent.click(screen.getByRole('button', { name: /^save$/i }))
  await vi.waitFor(() => expect(putServingPolicy).toHaveBeenCalled())
  const [entity, role, purpose, body] = putServingPolicy.mock.calls[0]
  expect([entity, role, purpose]).toEqual(['customer', 'population', 'analytical'])
  expect(body.expectedPointerVersion).toBe(3)
  expect(body.eligibleDatasetRefs).toContain('bank::public.customer_segment')
})
