import { fireEvent, render, screen } from '@testing-library/react'
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
    // Counts are wrapped in semantic-color spans; assert the full line via the status container,
    // which also pins the callout's role=status announcement contract.
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent('4 facts asserted, 1 staled, 0 quarantined')
    expect(status).toHaveTextContent(/first upload of 'deposits'/)
  })

  it('shows the chosen filename in the drop target', async () => {
    render(<UploadScreen onReviewQueue={() => {}} />)
    await userEvent.upload(
      screen.getByLabelText(/file/i), new File(['x'], 'deposits-q3.csv', { type: 'text/csv' }))
    expect(screen.getByText('deposits-q3.csv')).toBeInTheDocument()
  })

  it('renders held as a brake with the reason, not an error', async () => {
    uploadFile.mockResolvedValue(result({
      status: 'held', reason: 'overlap 20% < 60% (possible wrong source)' }))
    render(<UploadScreen onReviewQueue={() => {}} />)
    await submit()
    const held = await screen.findByRole('status')
    expect(held).toHaveTextContent(/held: this change removes too much of the existing catalog/i)
    expect(held).toHaveTextContent(/overlap 20%/)
    expect(held).toHaveTextContent(/nothing was applied/i)
    expect(held).toHaveTextContent(/no override yet/i)
    // The backend has no confirm path: an identical re-upload is held again. The copy must not
    // promise one.
    expect(held).not.toHaveTextContent(/re-upload/i)
    expect(screen.queryByRole('alert')).not.toBeInTheDocument()
  })

  it('renders rejected with the structural reason', async () => {
    uploadFile.mockResolvedValue(result({ status: 'rejected', reason: 'empty upload: no rows' }))
    render(<UploadScreen onReviewQueue={() => {}} />)
    await submit()
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent(/rejected/i)
    expect(status).toHaveTextContent(/empty upload: no rows/)
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

  it('hands off the uploaded source even after the input is edited for the next upload', async () => {
    uploadFile.mockResolvedValue(result({ asserted: 4, quarantined: 3 }))
    const onReviewQueue = vi.fn()
    render(<UploadScreen onReviewQueue={onReviewQueue} />)
    await submit()
    const input = screen.getByLabelText(/source name/i)
    await userEvent.clear(input)
    await userEvent.type(input, 'x')
    await userEvent.click(
      await screen.findByRole('button', { name: /review 3 quarantined rows/i }))
    expect(onReviewQueue).toHaveBeenCalledWith('deposits')
  })

  it('rejects a dropped file with an unsupported extension before any request', async () => {
    render(<UploadScreen onReviewQueue={() => {}} />)
    const dropZone = screen.getByLabelText(/file/i).closest('label')
    if (!dropZone) throw new Error('drop zone label not found')
    fireEvent.drop(dropZone, { dataTransfer: { files: [new File(['x'], 'export.bak')] } })
    expect(await screen.findByRole('alert')).toHaveTextContent(/unsupported file type/i)
    expect(screen.queryByText('export.bak')).not.toBeInTheDocument()
    expect(uploadFile).not.toHaveBeenCalled()
  })

  it('rejects a file over 20 MB before any request', async () => {
    render(<UploadScreen onReviewQueue={() => {}} />)
    await userEvent.type(screen.getByLabelText(/source name/i), 'deposits')
    const big = new File(['x'], 'big.csv', { type: 'text/csv' })
    Object.defineProperty(big, 'size', { value: 20 * 1024 * 1024 + 1 })
    await userEvent.upload(screen.getByLabelText(/file/i), big)
    expect(await screen.findByRole('alert')).toHaveTextContent(/20 MB/)
    expect(screen.getByRole('button', { name: 'Upload' })).toBeDisabled()
    expect(uploadFile).not.toHaveBeenCalled()
  })

  it('shows transport errors as an alert', async () => {
    uploadFile.mockRejectedValue(new api.ApiError(400, 'unsupported file type (expected .csv or .xlsx)'))
    render(<UploadScreen onReviewQueue={() => {}} />)
    await submit()
    expect(await screen.findByRole('alert')).toHaveTextContent(/unsupported file type/)
  })
})
