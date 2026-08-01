import { render, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ApiError } from '../api'
import { AssetProfilePanel } from './AssetProfilePanel'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getAssetProfile: vi.fn(), putAssetProfile: vi.fn() }
})
const getAssetProfile = vi.mocked(api.getAssetProfile)
const putAssetProfile = vi.mocked(api.putAssetProfile)

beforeEach(() => {
  getAssetProfile.mockReset()
  putAssetProfile.mockReset()
})

const emptyField = (): api.EffectiveProfileField => ({
  display: null, load_bearing: null, state: 'no_evidence',
  unresolved_reason: 'no_evidence', unresolved_family: 'undecided', reason_codes: [],
})

const value = (v: string, producer: string, strength: string): api.ProfileSemanticValue => ({
  value: v, producer, strength, lifecycle: 'active', evidence_ids: ['fev_1'],
})

function profile(over: Partial<api.AssetProfile> = {}): api.AssetProfile {
  return {
    dataset_logical_ref: 'bank::public.orders',
    catalog_profile_revision_id: null,
    description: emptyField(),
    business_context: emptyField(),
    domains: emptyField(),
    data_role: {
      ...emptyField(), state: 'display_only', unresolved_reason: null, unresolved_family: null,
      display: value('crosswalk', 'llm', 'proposed'),
    },
    primary_entity: emptyField(),
    authority_role: {
      ...emptyField(), state: 'display_only',
      unresolved_reason: 'authority_insufficient', unresolved_family: 'undecided',
      display: value('system_of_record', 'human', 'proposed'),
    },
    temporal_storage_model: emptyField(),
    event_or_snapshot: emptyField(),
    grain_fact: null,
    availability_fact: null,
    missing_context: ['catalog_narrative:absent'],
    dataset_profile_hash: 'hash-1',
    ...over,
  }
}

function renderPanel() {
  render(<AssetProfilePanel source="bank" objectRef="public.orders" kind="table" />)
}

describe('asset profile panel', () => {
  it('renders values with their authority label; proposed is usable, never failure-styled', async () => {
    getAssetProfile.mockResolvedValue(profile())
    renderPanel()
    // Scope to the field rows: the closed-vocabulary SELECT also contains these words as options.
    const authority = within(await screen.findByTestId('profile-authority-role'))
    expect(authority.getByText('system_of_record')).toBeInTheDocument()
    expect(authority.getByText('human/proposed')).toBeInTheDocument()
    expect(authority.getByText('proposed — usable, awaiting review')).toBeInTheDocument()
    const dataRole = within(screen.getByTestId('profile-data-role'))
    expect(dataRole.getByText('crosswalk')).toBeInTheDocument()   // derived data role display
    // Nothing about a proposed value renders as an alert/failure.
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('renders the three-family language for unresolved fields, never a raw failure string', async () => {
    getAssetProfile.mockResolvedValue(profile({
      temporal_storage_model: {
        ...emptyField(), state: 'conflict',
        unresolved_reason: 'conflict', unresolved_family: 'needs_data_check',
      },
    }))
    renderPanel()
    expect((await screen.findAllByText('Nobody has decided yet')).length).toBeGreaterThan(0)
    expect(screen.getByText('Needs a data check')).toBeInTheDocument()
    expect(screen.queryByText('conflict')).not.toBeInTheDocument()
    expect(screen.queryByText('no_evidence')).not.toBeInTheDocument()
  })

  it('renders nothing when the surface 404s (flag off) and for non-table assets', async () => {
    getAssetProfile.mockRejectedValue(new ApiError(404, 'Not Found'))
    const { container } = render(
      <AssetProfilePanel source="bank" objectRef="public.orders" kind="table" />)
    await waitFor(() => expect(getAssetProfile).toHaveBeenCalled())
    expect(container).toBeEmptyDOMElement()

    getAssetProfile.mockClear()
    const { container: col } = render(
      <AssetProfilePanel source="bank" objectRef="public.orders.amount" kind="column" />)
    expect(getAssetProfile).not.toHaveBeenCalled()
    expect(col).toBeEmptyDOMElement()
  })

  it('proposes through the PUT with the aggregate CAS hash', async () => {
    getAssetProfile.mockResolvedValue(profile())
    putAssetProfile.mockResolvedValue({
      written: ['authority_role'], dataset_profile_hash: 'hash-2', profile: profile(),
    })
    renderPanel()
    await screen.findByText('Profile')
    await userEvent.selectOptions(screen.getByLabelText(/authority role/i), 'derived')
    await userEvent.click(screen.getByRole('button', { name: 'Propose' }))
    expect(putAssetProfile).toHaveBeenCalledWith('bank', 'public.orders', {
      expectedDatasetProfileHash: 'hash-1',
      businessContext: undefined,
      authorityRole: 'derived',
      temporalStorageModel: undefined,
    })
    expect(await screen.findByRole('status')).toHaveTextContent(/visible and usable now/i)
    expect(getAssetProfile).toHaveBeenCalledTimes(2)   // reloaded after the propose
  })

  it('on a 409 reloads and asks for a re-review instead of retrying blind', async () => {
    getAssetProfile.mockResolvedValue(profile())
    putAssetProfile.mockRejectedValue(new ApiError(409, 'the profile changed'))
    renderPanel()
    await screen.findByText('Profile')
    await userEvent.type(screen.getByLabelText(/business context/i), 'Retail orders.')
    await userEvent.click(screen.getByRole('button', { name: 'Propose' }))
    expect(await screen.findByRole('status')).toHaveTextContent(/changed since you loaded it/i)
    expect(getAssetProfile).toHaveBeenCalledTimes(2)
  })
})
