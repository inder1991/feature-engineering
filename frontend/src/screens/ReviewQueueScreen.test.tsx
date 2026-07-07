import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ReviewQueueScreen } from './ReviewQueueScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return {
    ...actual,
    listQuarantine: vi.fn(),
    resolveQuarantineRow: vi.fn(),
    dismissQuarantineRow: vi.fn(),
  }
})
const listQuarantine = vi.mocked(api.listQuarantine)
const resolveQuarantineRow = vi.mocked(api.resolveQuarantineRow)
const dismissQuarantineRow = vi.mocked(api.dismissQuarantineRow)

// Block body (not an arrow returning the reset): a function returned from beforeEach is treated as a
// per-test teardown by Vitest, which would re-invoke a left-over rejecting mock as an unhandled reject.
beforeEach(() => {
  listQuarantine.mockReset()
  resolveQuarantineRow.mockReset()
  resolveQuarantineRow.mockResolvedValue({ resolved: true, reason: '' }) // backend accepts by default
  dismissQuarantineRow.mockReset()
  dismissQuarantineRow.mockResolvedValue({ dismissed: true })
})

const MISSING_TYPE = {
  row_index: 4,
  reason: 'missing required field(s): type',
  raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' },
}

describe('review queue screen', () => {
  it('contains no raw control bytes in the screen source', () => {
    const src = readFileSync(resolve(process.cwd(), 'src/screens/ReviewQueueScreen.tsx'), 'utf8')
    // eslint-disable-next-line no-control-regex
    expect(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(src)).toBe(false)
  })

  it('lists quarantined rows with reason, raw values, and a live count', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 9, reason: 'missing required field(s): type',
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' } }])
    render(<ReviewQueueScreen initialSource="" />)
    await userEvent.type(screen.getByLabelText('Source'), 'deposits')
    await userEvent.click(screen.getByRole('button', { name: /load queue/i }))
    expect(await screen.findByText(/missing required field\(s\): type/)).toBeInTheDocument()
    expect(screen.getByText('opened_at')).toBeInTheDocument()
    expect(screen.getByText(/row 9/)).toBeInTheDocument()
    expect(screen.getByRole('status')).toHaveTextContent(/1 quarantined · 0 resolved this session/i)
  })

  it('auto-loads when arriving with a source from the upload screen', async () => {
    listQuarantine.mockResolvedValue([])
    render(<ReviewQueueScreen initialSource="deposits" />)
    expect(
      await screen.findByText('Queue clear. No quarantined rows for this source.'),
    ).toBeInTheDocument()
    expect(listQuarantine).toHaveBeenCalledWith('deposits')
  })

  it('syncs the source input and reloads when initialSource changes without a remount', async () => {
    listQuarantine.mockResolvedValue([])
    const { rerender } = render(<ReviewQueueScreen initialSource="deposits" />)
    await screen.findByText(/queue clear/i)
    rerender(<ReviewQueueScreen initialSource="loans" />)
    expect(screen.getByLabelText('Source')).toHaveValue('loans')
    expect(listQuarantine).toHaveBeenLastCalledWith('loans')
  })

  it('ignores a late response from an older load', async () => {
    const older = { row_index: 1, reason: 'missing required field(s): type',
      raw: { source: 'deposits', table: 'a', column: 'x', type: '' } }
    const newer = { row_index: 2, reason: 'missing required field(s): column',
      raw: { source: 'loans', table: 'b', column: '', type: 'int' } }
    let resolveSlow!: (items: api.QuarantineItem[]) => void
    listQuarantine
      .mockImplementationOnce(() => new Promise(res => { resolveSlow = res }))
      .mockResolvedValueOnce([newer])
    render(<ReviewQueueScreen initialSource="" />)
    await userEvent.type(screen.getByLabelText('Source'), 'deposits')
    await userEvent.click(screen.getByRole('button', { name: /load queue/i }))
    await userEvent.clear(screen.getByLabelText('Source'))
    await userEvent.type(screen.getByLabelText('Source'), 'loans')
    await userEvent.click(screen.getByRole('button', { name: /load queue/i }))
    expect(await screen.findByText(/missing required field\(s\): column/)).toBeInTheDocument()
    await act(async () => { resolveSlow([older]) })
    expect(screen.queryByText(/missing required field\(s\): type/)).not.toBeInTheDocument()
  })

  it('replaces the loaded queue with an alert when a reload fails', async () => {
    listQuarantine.mockResolvedValueOnce([MISSING_TYPE])
    render(<ReviewQueueScreen initialSource="deposits" />)
    expect(await screen.findByText(/missing required field\(s\): type/)).toBeInTheDocument()
    listQuarantine.mockRejectedValueOnce(new api.ApiError(500, 'db down'))
    await userEvent.click(screen.getByRole('button', { name: /load queue/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('db down')
    expect(screen.queryByText(/missing required field\(s\): type/)).not.toBeInTheDocument()
  })

  it('revalidates a missing field: fix persists to the backend and the row resolves', async () => {
    listQuarantine.mockResolvedValue([MISSING_TYPE])
    render(<ReviewQueueScreen initialSource="deposits" />)
    expect(await screen.findByText('type')).toHaveClass('q-off')
    await userEvent.click(screen.getByRole('button', { name: /fix inline/i }))
    await userEvent.type(screen.getByLabelText('type'), 'timestamp')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))
    expect(await screen.findByText(/fixed and added to the catalog/i)).toBeInTheDocument()
    expect(resolveQuarantineRow).toHaveBeenCalledWith('deposits', 4, { type: 'timestamp' })
    expect(screen.getByText(/1 quarantined · 1 resolved this session/i)).toBeInTheDocument()
    expect(listQuarantine).toHaveBeenCalledTimes(1) // resolve does not re-query the list
  })

  it('surfaces the backend reason when the corrected row still fails validation', async () => {
    listQuarantine.mockResolvedValue([MISSING_TYPE])
    resolveQuarantineRow.mockResolvedValue({ resolved: false, reason: 'unknown column in catalog x' })
    render(<ReviewQueueScreen initialSource="deposits" />)
    await userEvent.click(await screen.findByRole('button', { name: /fix inline/i }))
    await userEvent.type(screen.getByLabelText('type'), 'timestamp')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))
    expect(await screen.findByText(/unknown column in catalog x/i)).toBeInTheDocument()
    expect(screen.queryByText(/fixed and added to the catalog/i)).not.toBeInTheDocument()
  })

  it('blocks a client-invalid fix before calling the backend', async () => {
    listQuarantine.mockResolvedValue([MISSING_TYPE])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await userEvent.click(await screen.findByRole('button', { name: /fix inline/i }))
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i })) // type left blank
    expect(screen.getByText(/type is required/i)).toBeInTheDocument()
    expect(resolveQuarantineRow).not.toHaveBeenCalled()
  })

  it('dismisses a row via the backend', async () => {
    listQuarantine.mockResolvedValue([MISSING_TYPE])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await userEvent.click(await screen.findByRole('button', { name: /dismiss/i }))
    expect(await screen.findByText(/dismissed from the queue/i)).toBeInTheDocument()
    expect(dismissQuarantineRow).toHaveBeenCalledWith('deposits', 4)
    expect(document.activeElement).toHaveAttribute('id', 'q-resolved-4')
  })

  it('a metadata-conflict row (real backend message) offers only Dismiss, no inline fix', async () => {
    // The backend's conflict message carries no per-type detail; the first-seen column already won, so
    // the only valid action is dismissing the redundant duplicate (a resolve would 409 already-in-catalog).
    listQuarantine.mockResolvedValue([{
      row_index: 2,
      reason: "conflicting metadata for ('deposits', 'accounts', 'opened_at') (rows for the same column disagree)",
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: 'timestamp' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    expect(await screen.findByText(/two rows for this column disagree/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /fix inline/i })).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(dismissQuarantineRow).toHaveBeenCalledWith('deposits', 2)
    expect(await screen.findByText(/dismissed from the queue/i)).toBeInTheDocument()
  })

  it('applies a mapping rule for a repeated value across rows via the backend', async () => {
    const reason = "unrecognized sensitivity 'secret' (expected one of: pii, restricted)"
    listQuarantine.mockResolvedValue([
      { row_index: 1, reason, raw: { source: 'd', table: 't', column: 'a', type: 'int', sensitivity: 'secret' } },
      { row_index: 2, reason, raw: { source: 'd', table: 't', column: 'b', type: 'int', sensitivity: 'secret' } },
    ])
    render(<ReviewQueueScreen initialSource="deposits" />)
    expect(await screen.findByText(/'secret' appears in 2 rows/i)).toBeInTheDocument()
    await userEvent.type(screen.getByLabelText(/replacement value for secret/i), 'pii')
    await userEvent.click(screen.getByRole('button', { name: /add mapping rule/i }))
    expect(await screen.findAllByText(/resolved by a mapping rule/i)).toHaveLength(2)
    expect(resolveQuarantineRow).toHaveBeenCalledWith('deposits', 1, { sensitivity: 'pii' })
    expect(resolveQuarantineRow).toHaveBeenCalledWith('deposits', 2, { sensitivity: 'pii' })
    // Removing the rule chip does NOT un-resolve (the rows are persisted on the server).
    await userEvent.click(screen.getByRole('button', { name: /remove mapping rule secret to pii/i }))
    expect(screen.getAllByText(/resolved by a mapping rule/i)).toHaveLength(2)
    expect(screen.queryByText(/'secret' appears in 2 rows/i)).not.toBeInTheDocument()
  })

  it('rejects an invalid mapping-rule replacement before calling the backend', async () => {
    const reason = "unrecognized sensitivity 'secret' (expected one of: pii, restricted)"
    listQuarantine.mockResolvedValue([
      { row_index: 1, reason, raw: { source: 'd', table: 't', column: 'a', type: 'int', sensitivity: 'secret' } },
      { row_index: 2, reason, raw: { source: 'd', table: 't', column: 'b', type: 'int', sensitivity: 'secret' } },
    ])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await userEvent.type(await screen.findByLabelText(/replacement value for secret/i), 'secret2')
    await userEvent.click(screen.getByRole('button', { name: /add mapping rule/i }))
    expect(screen.getByRole('alert')).toHaveTextContent(/'secret2' is not recognized/i)
    expect(resolveQuarantineRow).not.toHaveBeenCalled()
  })

  it('shows guidance and no inline fix for reasons outside the known classes', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 6, reason: 'row exceeds the maximum column count',
      raw: { source: 'deposits', table: 't', column: 'x', type: 'int' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    expect(await screen.findByText(/no inline fix for this reason/i)).toBeInTheDocument()
    expect(screen.queryByRole('button', { name: /fix inline/i })).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /dismiss/i })).toBeInTheDocument()
  })

  it('shows the persistence callout only after the first resolution', async () => {
    listQuarantine.mockResolvedValue([MISSING_TYPE])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await screen.findByText(/missing required field/i)
    expect(screen.queryByText(/fixes are validated on the server and persist/i)).not.toBeInTheDocument()
    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(await screen.findByText(/fixes are validated on the server and persist/i)).toBeInTheDocument()
  })
})
