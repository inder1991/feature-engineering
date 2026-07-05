import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import * as api from '../api'
import { ReviewQueueScreen } from './ReviewQueueScreen'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, listQuarantine: vi.fn() }
})
const listQuarantine = vi.mocked(api.listQuarantine)

// Block body (not `() => listQuarantine.mockReset()`): mockReset() returns the mock fn, and Vitest
// treats a function returned from beforeEach as a per-test teardown — it would then call the mock
// after each test, producing an unawaited rejected promise (unhandled rejection) in the reject case.
beforeEach(() => {
  listQuarantine.mockReset()
})

describe('review queue screen', () => {
  it('lists quarantined rows with reason and raw values', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 9, reason: 'missing required field(s): type',
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' } }])
    render(<ReviewQueueScreen initialSource="" />)
    await userEvent.type(screen.getByLabelText('Source'), 'deposits')
    await userEvent.click(screen.getByRole('button', { name: /load queue/i }))
    expect(await screen.findByText(/missing required field\(s\): type/)).toBeInTheDocument()
    expect(screen.getByText('opened_at')).toBeInTheDocument()
    expect(screen.getByText(/row 9/)).toBeInTheDocument()
    expect(
      screen.getByText(/1 quarantined row\. fix them in the source file and re-upload/i),
    ).toBeInTheDocument()
  })

  it('auto-loads when arriving with a source from the upload screen', async () => {
    listQuarantine.mockResolvedValue([])
    render(<ReviewQueueScreen initialSource="deposits" />)
    expect(
      await screen.findByText('Queue clear. No quarantined rows for this source.'),
    ).toBeInTheDocument()
    expect(listQuarantine).toHaveBeenCalledWith('deposits')
  })
})
