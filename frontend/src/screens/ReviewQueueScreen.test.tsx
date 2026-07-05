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
    // Copy changed: the count line now reports quarantined + session-resolved counts.
    expect(
      screen.getByText(/1 quarantined · 0 resolved this session \(mock\)/i),
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

  it('highlights the offending cell and revalidates a missing field without re-querying', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 4, reason: 'missing required field(s): type',
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    // Offending cell is highlighted; its blank value renders as "blank".
    expect(await screen.findByText('type')).toHaveClass('q-off')
    expect(screen.getByText('blank')).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /fix inline/i }))
    await userEvent.type(screen.getByLabelText('type'), 'timestamp')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))

    expect(
      await screen.findByText(/revalidated locally\. not persisted/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/1 quarantined · 1 resolved this session \(mock\)/i),
    ).toBeInTheDocument()
    // Mock resolution: no new server query.
    expect(listQuarantine).toHaveBeenCalledTimes(1)
  })

  it('shows the specific check when revalidate leaves a required field blank', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 4, reason: 'missing required field(s): type',
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await userEvent.click(await screen.findByRole('button', { name: /fix inline/i }))
    // Leave the type input blank, then revalidate.
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))

    expect(screen.getByText(/type is required/i)).toBeInTheDocument()
    // Still pending: no resolved note, the fix affordance remains.
    expect(screen.queryByText(/revalidated locally/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /fix inline/i })).toBeInTheDocument()
  })

  it('resolves a conflict row with a one-click keep-first-seen', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 2,
      reason: "conflicting type for ('deposits', 'accounts', 'opened_at'): date vs timestamp",
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: 'timestamp' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await userEvent.click(await screen.findByRole('button', { name: /fix inline/i }))
    await userEvent.click(screen.getByRole('button', { name: 'Keep date' }))

    expect(screen.getByText(/kept the first-seen type 'date' locally/i)).toBeInTheDocument()
    expect(
      screen.getByText(/1 quarantined · 1 resolved this session \(mock\)/i),
    ).toBeInTheDocument()
  })

  it('offers a mapping rule for a repeated unrecognized value, applies and removes it', async () => {
    const reason = "unrecognized sensitivity 'secret' (expected one of: pii, restricted)"
    listQuarantine.mockResolvedValue([
      { row_index: 1, reason, raw: { source: 'd', table: 't', column: 'a', type: 'int', sensitivity: 'secret' } },
      { row_index: 2, reason, raw: { source: 'd', table: 't', column: 'b', type: 'int', sensitivity: 'secret' } },
    ])
    render(<ReviewQueueScreen initialSource="deposits" />)
    expect(await screen.findByText(/'secret' appears in 2 rows/i)).toBeInTheDocument()

    await userEvent.type(screen.getByLabelText(/replacement value for secret/i), 'pii')
    await userEvent.click(screen.getByRole('button', { name: /add mapping rule/i }))

    // Both rows resolved by the rule; the rule shows as a removable chip.
    expect(screen.getAllByText(/resolved by a local mapping rule/i)).toHaveLength(2)
    expect(screen.getByText(/secret → pii · 2 rows/)).toBeInTheDocument()
    expect(
      screen.getByText(/2 quarantined · 2 resolved this session \(mock\)/i),
    ).toBeInTheDocument()
    // The group is fully resolved, so its callout is gone.
    expect(screen.queryByText(/'secret' appears in 2 rows/i)).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /remove mapping rule secret to pii/i }))
    // Removing un-resolves the rows: the callout returns and the notes are gone.
    expect(await screen.findByText(/'secret' appears in 2 rows/i)).toBeInTheDocument()
    expect(screen.queryByText(/resolved by a local mapping rule/i)).not.toBeInTheDocument()
  })

  it('shows the honesty preview callout only after the first mock resolution', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 7, reason: 'missing required field(s): type',
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await screen.findByText(/missing required field/i)
    expect(screen.queryByText(/inline fixes are a preview/i)).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /dismiss/i }))
    expect(screen.getByText(/inline fixes are a preview/i)).toBeInTheDocument()
    expect(screen.getByText(/dismissed locally\. retained in the queue on the server/i)).toBeInTheDocument()
  })
})
