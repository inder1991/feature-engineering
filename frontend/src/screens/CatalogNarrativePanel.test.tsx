import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { CatalogNarrativePanel } from './CatalogNarrativePanel'

// The catalog-narrative editor (deferred from profile Task 3, landed with Task 5). It exists to
// make two shipped-but-uncalled clients reachable — an unreachable surface is the same inert
// mechanism this programme keeps finding — and it must follow the CAS and no-blocked rules.

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, getCatalogProfile: vi.fn(), putCatalogProfile: vi.fn() }
})
const getCatalogProfile = vi.mocked(api.getCatalogProfile)
const putCatalogProfile = vi.mocked(api.putCatalogProfile)

beforeEach(() => {
  getCatalogProfile.mockReset()
  putCatalogProfile.mockReset()
})

function revision(over: Partial<api.CatalogProfileRevision> = {}): api.CatalogProfileRevision {
  return {
    catalog_source: 'ftr',
    display_name: 'FTR compliance glossary',
    description: 'Financial transaction reporting terms.',
    business_context: 'Owned by financial crime operations.',
    business_domains: ['Compliance'],
    producer: 'human',
    strength: 'confirmed',
    lifecycle: 'active',
    producer_ref: 'user:priya',
    ingestion_run_id: null,
    content_hash: 'a'.repeat(64),
    revision_id: `cpr_${'a'.repeat(64)}`,
    ...over,
  }
}

async function renderPanel(profile: api.CatalogProfile) {
  getCatalogProfile.mockResolvedValue(profile)
  render(<CatalogNarrativePanel source="ftr" />)
  await screen.findByTestId('catalog-narrative')
}

it('renders nothing at all when the surface is off (404)', async () => {
  getCatalogProfile.mockRejectedValue(new api.ApiError(404, 'Not Found'))
  const { container } = render(<CatalogNarrativePanel source="ftr" />)
  await vi.waitFor(() => expect(getCatalogProfile).toHaveBeenCalled())
  expect(container).toBeEmptyDOMElement()
})

it('frames an undescribed catalog as undescribed, not as an error', async () => {
  await renderPanel({ source: 'ftr', pointer_version: 0, profile: null })
  const empty = screen.getByTestId('catalog-narrative-empty')
  expect(empty).toHaveTextContent(/nobody has described this catalog yet/i)
  expect(empty.textContent).not.toMatch(/error|missing|failed|blocked|required/i)
  expect(screen.getByRole('button', { name: /describe this catalog/i })).toBeInTheDocument()
})

it('shows the current narrative with the author of the words', async () => {
  await renderPanel({ source: 'ftr', pointer_version: 3, profile: revision() })
  expect(screen.getByText('FTR compliance glossary')).toBeInTheDocument()
  expect(screen.getByText('Owned by financial crime operations.')).toBeInTheDocument()
  expect(screen.getByText('human/confirmed')).toBeInTheDocument()
})

it('echoes the pointer version it READ back in the body', async () => {
  await renderPanel({ source: 'ftr', pointer_version: 3, profile: revision() })
  putCatalogProfile.mockResolvedValue({ source: 'ftr', revision_id: 'cpr_x', pointer_version: 4 })
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(putCatalogProfile).toHaveBeenCalledWith('ftr', expect.objectContaining({
    expectedPointerVersion: 3,
  }))
})

it('claims a first write with version 0 rather than omitting the anchor', async () => {
  await renderPanel({ source: 'ftr', pointer_version: 0, profile: null })
  putCatalogProfile.mockResolvedValue({ source: 'ftr', revision_id: 'cpr_x', pointer_version: 1 })
  await userEvent.click(screen.getByRole('button', { name: /describe this catalog/i }))
  await userEvent.type(screen.getByLabelText('Description'), 'What this catalog is for.')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  expect(putCatalogProfile).toHaveBeenCalledWith('ftr', expect.objectContaining({
    expectedPointerVersion: 0,
    description: 'What this catalog is for.',
  }))
})

it('a concurrent write reloads and asks for a re-review instead of overwriting', async () => {
  await renderPanel({ source: 'ftr', pointer_version: 3, profile: revision() })
  putCatalogProfile.mockRejectedValue(new api.ApiError(409, 'conflict'))
  getCatalogProfile.mockResolvedValue({
    source: 'ftr', pointer_version: 4,
    profile: revision({ business_context: 'Rewritten by someone else.' }),
  })
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  expect(await screen.findByRole('status')).toHaveTextContent(/nothing was overwritten/i)
  expect(await screen.findByText('Rewritten by someone else.')).toBeInTheDocument()
  // Exactly ONE write attempt: a blind retry would clobber the other person's words.
  expect(putCatalogProfile).toHaveBeenCalledTimes(1)
})

it('a 409 keeps the author DRAFT and shows the server values beside it', async () => {
  // The conflict handler reloaded the profile into the FORM, so the author's unsaved paragraph was
  // replaced by the other person's — protecting the server's copy by destroying the one only this
  // person had. Re-review needs BOTH versions present.
  await renderPanel({ source: 'ftr', pointer_version: 3, profile: revision() })
  putCatalogProfile.mockRejectedValue(new api.ApiError(409, 'conflict'))
  getCatalogProfile.mockResolvedValue({
    source: 'ftr', pointer_version: 4,
    profile: revision({ business_context: 'Rewritten by someone else.' }),
  })

  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  const draft = screen.getByLabelText('Business context')
  await userEvent.clear(draft)
  await userEvent.type(draft, 'My unsaved paragraph.')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  expect(await screen.findByRole('status')).toHaveTextContent(/nothing was overwritten/i)
  // The draft survives, in the field the author typed it into.
  expect(screen.getByLabelText('Business context')).toHaveValue('My unsaved paragraph.')
  // …and the server's current values are shown beside it, to re-review against.
  const theirs = await screen.findByTestId('catalog-narrative-theirs')
  expect(theirs).toHaveTextContent('Rewritten by someone else.')
})

it('a re-save after a 409 anchors on the version it just re-read', async () => {
  await renderPanel({ source: 'ftr', pointer_version: 3, profile: revision() })
  putCatalogProfile.mockRejectedValueOnce(new api.ApiError(409, 'conflict'))
  getCatalogProfile.mockResolvedValue({
    source: 'ftr', pointer_version: 4,
    profile: revision({ business_context: 'Rewritten by someone else.' }),
  })
  await userEvent.click(screen.getByRole('button', { name: 'Edit' }))
  const draft = screen.getByLabelText('Business context')
  await userEvent.clear(draft)
  await userEvent.type(draft, 'My unsaved paragraph.')
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))
  await screen.findByTestId('catalog-narrative-theirs')

  putCatalogProfile.mockResolvedValue({ source: 'ftr', revision_id: 'cpr_y', pointer_version: 5 })
  await userEvent.click(screen.getByRole('button', { name: 'Save' }))

  expect(putCatalogProfile).toHaveBeenLastCalledWith('ftr', expect.objectContaining({
    expectedPointerVersion: 4,
    businessContext: 'My unsaved paragraph.',
  }))
})
