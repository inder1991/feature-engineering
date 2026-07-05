import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { UploadScreen } from './UploadScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, uploadFile: vi.fn() }
})
const uploadFile = vi.mocked(api.uploadFile)

// Block body (not `() => uploadFile.mockReset()`): mockReset() returns the mock fn, and Vitest
// treats a function returned from beforeEach as a per-test teardown — it would then call the mock
// after each test, producing an unawaited rejected promise (unhandled rejection) in the reject case.
beforeEach(() => {
  uploadFile.mockReset()
})

const result = (over: Partial<api.IngestResult>): api.IngestResult => ({
  status: 'ingested', reason: null, asserted: 0, staled: 0, quarantined: 0, flagged: null, ...over })

async function submit(source = 'deposits') {
  await userEvent.type(screen.getByLabelText(/source name/i), source)
  await userEvent.upload(
    screen.getByLabelText(/file/i), new File(['x'], 'd.csv', { type: 'text/csv' }))
  await userEvent.click(screen.getByRole('button', { name: 'Upload' }))
}

describe('upload screen', () => {
  it('shows the ingest summary with the first-upload flag', async () => {
    uploadFile.mockResolvedValue(result({
      asserted: 4, staled: 1,
      flagged: "first upload of 'deposits' (9 objects) — review recommended" }))
    render(<UploadScreen onReviewQueue={() => {}} />)
    await submit()
    expect(await screen.findByText(/4 facts asserted · 1 staled · 0 quarantined/))
      .toBeInTheDocument()
    expect(screen.getByText(/first upload of 'deposits'/)).toBeInTheDocument()
  })

  it('renders held as a brake with the reason, not an error', async () => {
    uploadFile.mockResolvedValue(result({
      status: 'held', reason: 'overlap 20% < 60% (possible wrong source)' }))
    render(<UploadScreen onReviewQueue={() => {}} />)
    await submit()
    expect(await screen.findByText(/held — confirm this large change/i)).toBeInTheDocument()
    expect(screen.getByText(/overlap 20%/)).toBeInTheDocument()
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('renders rejected with the structural reason', async () => {
    uploadFile.mockResolvedValue(result({ status: 'rejected', reason: 'empty upload: no rows' }))
    render(<UploadScreen onReviewQueue={() => {}} />)
    await submit()
    expect(await screen.findByText(/empty upload: no rows/)).toBeInTheDocument()
  })

  it('links quarantined rows to the review queue', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4, quarantined: 3 }))
    const onReviewQueue = vi.fn()
    render(<UploadScreen onReviewQueue={onReviewQueue} />)
    await submit()
    await userEvent.click(
      await screen.findByRole('button', { name: /review 3 quarantined rows/i }))
    expect(onReviewQueue).toHaveBeenCalledWith('deposits')
  })

  it('shows transport errors as an alert', async () => {
    uploadFile.mockRejectedValue(new api.ApiError(400, 'unsupported file type (expected .csv or .xlsx)'))
    render(<UploadScreen onReviewQueue={() => {}} />)
    await submit()
    expect(await screen.findByRole('alert')).toHaveTextContent(/unsupported file type/)
  })
})
