import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { act, render, screen } from '@testing-library/react'
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
// after each test, producing an unawaited rejected promise (unhandled rejection) after tests that
// leave a rejecting implementation installed (the load-failure test below does).
beforeEach(() => {
  listQuarantine.mockReset()
})

describe('review queue screen', () => {
  it('contains no raw control bytes in the screen source', () => {
    // A NUL byte once shipped inside a template literal here: invisible in editors, it made
    // grep treat the file as binary and put a control character in every mapping-rule id.
    // Vitest runs with cwd at the frontend package root; module URLs here are not file-scheme.
    const src = readFileSync(resolve(process.cwd(), 'src/screens/ReviewQueueScreen.tsx'), 'utf8')
    // eslint-disable-next-line no-control-regex
    expect(/[\u0000-\u0008\u000b\u000c\u000e-\u001f]/.test(src)).toBe(false)
  })

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

  it('announces the queue count in a status live region', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 9, reason: 'missing required field(s): type',
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    const status = await screen.findByRole('status')
    expect(status).toHaveTextContent(/1 quarantined · 0 resolved this session \(mock\)/i)
  })

  it('auto-loads when arriving with a source from the upload screen', async () => {
    listQuarantine.mockResolvedValue([])
    render(<ReviewQueueScreen initialSource="deposits" />)
    expect(
      await screen.findByText('Queue clear. No quarantined rows for this source.'),
    ).toBeInTheDocument()
    expect(listQuarantine).toHaveBeenCalledWith('deposits')
    // The clear outcome is announced, not silent: it is the load's status live region.
    expect(screen.getByRole('status')).toHaveTextContent(
      'Queue clear. No quarantined rows for this source.',
    )
  })

  it('syncs the source input and reloads when initialSource changes without a remount', async () => {
    listQuarantine.mockResolvedValue([])
    const { rerender } = render(<ReviewQueueScreen initialSource="deposits" />)
    await screen.findByText(/queue clear/i)
    expect(listQuarantine).toHaveBeenLastCalledWith('deposits')

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

    // The slow response for 'deposits' arrives after 'loans' already rendered: discard it.
    await act(async () => {
      resolveSlow([older])
    })
    expect(screen.getByText(/missing required field\(s\): column/)).toBeInTheDocument()
    expect(screen.queryByText(/missing required field\(s\): type/)).not.toBeInTheDocument()
  })

  it('replaces the loaded queue with an alert when a reload fails', async () => {
    listQuarantine.mockResolvedValueOnce([{
      row_index: 9, reason: 'missing required field(s): type',
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    expect(await screen.findByText(/missing required field\(s\): type/)).toBeInTheDocument()

    listQuarantine.mockRejectedValueOnce(new api.ApiError(500, 'db down'))
    await userEvent.click(screen.getByRole('button', { name: /load queue/i }))
    expect(await screen.findByRole('alert')).toHaveTextContent('db down')
    // No stale rows under the error banner.
    expect(screen.queryByText(/missing required field\(s\): type/)).not.toBeInTheDocument()
    expect(screen.queryByText(/quarantined ·/)).not.toBeInTheDocument()
  })

  it('reload resets local resolutions so fresh rows render as pending', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 4, reason: 'missing required field(s): type',
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await userEvent.click(await screen.findByRole('button', { name: /dismiss/i }))
    expect(
      screen.getByText(/1 quarantined · 1 resolved this session \(mock\)/i),
    ).toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /load queue/i }))
    expect(
      await screen.findByText(/1 quarantined · 0 resolved this session \(mock\)/i),
    ).toBeInTheDocument()
    expect(screen.queryByText(/dismissed locally/i)).not.toBeInTheDocument()
    expect(screen.getByRole('button', { name: /fix inline/i })).toBeInTheDocument()
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

  it('rejects a missing-source fix whose value differs from the upload source', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 8, reason: 'missing required field(s): source',
      raw: { source: '', table: 'accounts', column: 'balance', type: 'number' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await userEvent.click(await screen.findByRole('button', { name: /fix inline/i }))
    await userEvent.type(screen.getByLabelText('source'), 'depositz')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))
    expect(
      screen.getByText(/source must equal the upload source 'deposits'/i),
    ).toBeInTheDocument()
    expect(screen.queryByText(/revalidated locally/i)).not.toBeInTheDocument()

    // The loaded queue's source is the value a clean re-upload would need.
    await userEvent.clear(screen.getByLabelText('source'))
    await userEvent.type(screen.getByLabelText('source'), 'deposits')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))
    expect(await screen.findByText(/revalidated locally/i)).toBeInTheDocument()
  })

  it('classifies a mismatch row and revalidates only when source equals the upload source', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 3,
      reason: "row source 'depositz' does not match upload source 'deposits'",
      raw: { source: 'depositz', table: 'accounts', column: 'balance', type: 'number' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    // The source cell is highlighted as the offender.
    expect(await screen.findByText('source')).toHaveClass('q-off')

    await userEvent.click(screen.getByRole('button', { name: /fix inline/i }))
    // Pre-filled with the mismatched value; revalidating unchanged must fail.
    expect(screen.getByLabelText('source')).toHaveValue('depositz')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))
    expect(
      screen.getByText(/source must equal the upload source 'deposits'/i),
    ).toBeInTheDocument()
    expect(screen.queryByText(/revalidated locally/i)).not.toBeInTheDocument()

    await userEvent.clear(screen.getByLabelText('source'))
    await userEvent.type(screen.getByLabelText('source'), 'deposits')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))
    expect(await screen.findByText(/revalidated locally/i)).toBeInTheDocument()
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

  it('rejects a conflict revalidation whose type still differs from the first-seen type', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 2,
      reason: "conflicting type for ('deposits', 'accounts', 'opened_at'): date vs timestamp",
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: 'timestamp' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await userEvent.click(await screen.findByRole('button', { name: /fix inline/i }))
    // Pre-filled with the conflicting incoming type; the backend is first-seen-wins, so
    // revalidating unchanged must fail, mirroring the re-upload outcome.
    expect(screen.getByLabelText('type')).toHaveValue('timestamp')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))
    expect(
      screen.getByText(/type must match the first-seen type 'date' \(first upload wins\)/i),
    ).toBeInTheDocument()
    expect(
      screen.getByText(/stays quarantined until the source file is fixed/i),
    ).toBeInTheDocument()
    expect(screen.queryByText(/revalidated locally/i)).not.toBeInTheDocument()

    // Matching the first-seen type passes.
    await userEvent.clear(screen.getByLabelText('type'))
    await userEvent.type(screen.getByLabelText('type'), 'date')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))
    expect(await screen.findByText(/revalidated locally/i)).toBeInTheDocument()
  })

  it('fixes a single unrecognized sensitivity inline: bad value rejected, pii accepted', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 5,
      reason: "unrecognized sensitivity 'secret' (expected one of: pii, restricted)",
      raw: { source: 'deposits', table: 't', column: 'a', type: 'int', sensitivity: 'secret' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await screen.findByText(/unrecognized sensitivity 'secret'/i)
    // A single row earns no group callout; the inline editor is the only fix path.
    expect(screen.queryByText(/appears in/i)).not.toBeInTheDocument()

    await userEvent.click(screen.getByRole('button', { name: /fix inline/i }))
    await userEvent.clear(screen.getByLabelText('sensitivity'))
    await userEvent.type(screen.getByLabelText('sensitivity'), 'confidential')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))
    expect(
      screen.getByText(/sensitivity must be blank, pii, or restricted\. 'confidential' is not recognized\./i),
    ).toBeInTheDocument()
    expect(screen.queryByText(/revalidated locally/i)).not.toBeInTheDocument()

    await userEvent.clear(screen.getByLabelText('sensitivity'))
    await userEvent.type(screen.getByLabelText('sensitivity'), 'pii')
    await userEvent.click(screen.getByRole('button', { name: /^revalidate$/i }))
    expect(await screen.findByText(/revalidated locally/i)).toBeInTheDocument()
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

  it('rejects an invalid mapping-rule replacement with an alert and resolves nothing', async () => {
    const reason = "unrecognized sensitivity 'secret' (expected one of: pii, restricted)"
    listQuarantine.mockResolvedValue([
      { row_index: 1, reason, raw: { source: 'd', table: 't', column: 'a', type: 'int', sensitivity: 'secret' } },
      { row_index: 2, reason, raw: { source: 'd', table: 't', column: 'b', type: 'int', sensitivity: 'secret' } },
    ])
    render(<ReviewQueueScreen initialSource="deposits" />)
    await userEvent.type(
      await screen.findByLabelText(/replacement value for secret/i), 'secret2')
    await userEvent.click(screen.getByRole('button', { name: /add mapping rule/i }))

    expect(screen.getByRole('alert')).toHaveTextContent(
      /replacement must be blank, pii, or restricted\. 'secret2' is not recognized\./i,
    )
    expect(
      screen.getByText(/2 quarantined · 0 resolved this session \(mock\)/i),
    ).toBeInTheDocument()
    expect(screen.queryByText(/resolved by a local mapping rule/i)).not.toBeInTheDocument()
  })

  it('does not resurrect a stale editor when a rule covering the edited row is removed', async () => {
    const reason = "unrecognized sensitivity 'secret' (expected one of: pii, restricted)"
    listQuarantine.mockResolvedValue([
      { row_index: 1, reason, raw: { source: 'd', table: 't', column: 'a', type: 'int', sensitivity: 'secret' } },
      { row_index: 2, reason, raw: { source: 'd', table: 't', column: 'b', type: 'int', sensitivity: 'secret' } },
    ])
    render(<ReviewQueueScreen initialSource="deposits" />)
    // Open the inline editor on row 2 and leave a draft in it.
    const fixButtons = await screen.findAllByRole('button', { name: /fix inline/i })
    await userEvent.click(fixButtons[1])
    await userEvent.clear(screen.getByLabelText('sensitivity'))
    await userEvent.type(screen.getByLabelText('sensitivity'), 'draft-value')

    // Apply a rule covering both rows, then remove it.
    await userEvent.type(screen.getByLabelText(/replacement value for secret/i), 'pii')
    await userEvent.click(screen.getByRole('button', { name: /add mapping rule/i }))
    expect(screen.getAllByText(/resolved by a local mapping rule/i)).toHaveLength(2)
    await userEvent.click(screen.getByRole('button', { name: /remove mapping rule secret to pii/i }))

    // Rows are pending again, but no editor pops back open with the stale draft.
    expect(await screen.findAllByRole('button', { name: /fix inline/i })).toHaveLength(2)
    expect(screen.queryByLabelText('sensitivity')).not.toBeInTheDocument()
    expect(screen.queryByDisplayValue('draft-value')).not.toBeInTheDocument()
  })

  it('moves keyboard focus off <body> when a resolution unmounts the focused button', async () => {
    listQuarantine.mockResolvedValue([{
      row_index: 4, reason: 'missing required field(s): type',
      raw: { source: 'deposits', table: 'accounts', column: 'opened_at', type: '' } }])
    render(<ReviewQueueScreen initialSource="deposits" />)
    // Keyboard-style activation: focus lands on the button, then the button unmounts.
    await userEvent.click(await screen.findByRole('button', { name: /dismiss/i }))
    expect(await screen.findByText(/dismissed locally/i)).toBeInTheDocument()

    expect(document.activeElement).not.toBe(document.body)
    expect(document.activeElement).toHaveAttribute('id', 'q-resolved-4')
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
