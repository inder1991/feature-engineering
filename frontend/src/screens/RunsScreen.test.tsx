import { render, screen, waitFor } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { RunsScreen } from './RunsScreen'

// importOriginal rather than a bare factory: the screen narrows its catch with `instanceof
// ApiError`, so the REAL class has to survive the mock or the error test would exercise the
// String(err) fallback instead of the server's own message.
vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, listFeatureRuns: vi.fn() }
})
const listFeatureRuns = vi.mocked(api.listFeatureRuns)

beforeEach(() => {
  listFeatureRuns.mockReset()
})

// jsdom implements no clipboard, so the copy test installs one onto the SHARED navigator. Put back
// whatever was there (usually: nothing) so a later test never inherits this mock.
const clipboardBefore = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
afterEach(() => {
  if (clipboardBefore) Object.defineProperty(navigator, 'clipboard', clipboardBefore)
  else Reflect.deleteProperty(navigator, 'clipboard')
})

const SPINE_RUN: api.FeatureRunSummary = {
  generation_run_id: 'grun_01M02SAZQQQQ',
  display_name: 'August build',
  pre_spine: false,
  owner_subject: 'priya',
  created_at: '2026-08-23T00:00:00+00:00',
}

const LEGACY_RUN: api.FeatureRunSummary = {
  generation_run_id: 'fgr_legacy01',
  display_name: null,
  pre_spine: true,
  owner_subject: null,
  created_at: '2026-08-01T00:00:00+00:00',
}

const PAGE: api.FeatureRunList = {
  groups: [
    { intent_id: 'i1', hypothesis: 'Retail churn', runs: [SPINE_RUN] },
    { intent_id: null, hypothesis: null, runs: [LEGACY_RUN] },
  ],
  next_cursor: null,
}

it('groups by hypothesis with an honest ungrouped bucket', async () => {
  listFeatureRuns.mockResolvedValue(PAGE)
  render(<RunsScreen navigate={() => {}} />)

  await waitFor(() => expect(screen.getByText('Retail churn')).toBeInTheDocument())
  expect(screen.getByText('No hypothesis recorded')).toBeInTheDocument()
  expect(screen.getByText('August build')).toBeInTheDocument()
  // The unset name renders as absence, never as an invented label.
  expect(screen.getAllByText('—').length).toBeGreaterThan(0)
  expect(screen.getByText(/Pre-spine/i)).toBeInTheDocument()
  // The first page asks for no cursor at all.
  expect(listFeatureRuns).toHaveBeenCalledWith()
})

it('heads the ungrouped bucket by the missing hypothesis, not by the missing intent', async () => {
  // A dangling intent id with no hypothesis behind it: there is no FK, so the server can hand back
  // an intent_id whose intent row is gone. The heading answers what the reader can actually see.
  listFeatureRuns.mockResolvedValue({
    groups: [{ intent_id: 'i_dangling', hypothesis: null, runs: [SPINE_RUN] }],
    next_cursor: null,
  })
  render(<RunsScreen navigate={() => {}} />)

  await waitFor(() => expect(screen.getByText('No hypothesis recorded')).toBeInTheDocument())
  expect(screen.queryByText('i_dangling')).toBeNull()
})

it('truncates the opaque id but keeps the whole one reachable', async () => {
  listFeatureRuns.mockResolvedValue(PAGE)
  render(<RunsScreen navigate={() => {}} />)

  await waitFor(() => expect(screen.getByText('grun_01M02SAZ…')).toBeInTheDocument())
  expect(screen.getByText('grun_01M02SAZ…')).toHaveAttribute('title', 'grun_01M02SAZQQQQ')
  // Short enough to show whole — an ellipsis here would claim a truncation that never happened.
  expect(screen.getByText('fgr_legacy01')).toBeInTheDocument()
})

it('copies the full id, not the truncation on screen', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  listFeatureRuns.mockResolvedValue(PAGE)
  render(<RunsScreen navigate={() => {}} />)

  await waitFor(() => expect(screen.getByText('August build')).toBeInTheDocument())
  await userEvent.click(screen.getByRole('button', { name: 'Copy run id grun_01M02SAZQQQQ' }))

  expect(writeText).toHaveBeenCalledWith('grun_01M02SAZQQQQ')
  await waitFor(() => expect(screen.getByTestId('copy-status')).toHaveTextContent('grun_01M02SAZQQQQ'))
})

it('links each run at the canonical shareable path', async () => {
  listFeatureRuns.mockResolvedValue(PAGE)
  render(<RunsScreen navigate={() => {}} />)

  await waitFor(() => expect(screen.getByText('August build')).toBeInTheDocument())
  expect(screen.getByRole('link', { name: 'Open run grun_01M02SAZQQQQ' })).toHaveAttribute(
    'href',
    '#/runs/grun_01M02SAZQQQQ',
  )
})

it('opens the run the reader clicked', async () => {
  const navigate = vi.fn()
  listFeatureRuns.mockResolvedValue(PAGE)
  render(<RunsScreen navigate={navigate} />)

  await waitFor(() => expect(screen.getByText('August build')).toBeInTheDocument())
  await userEvent.click(screen.getByText('August build'))

  expect(navigate).toHaveBeenCalledWith('runs', { run_id: 'grun_01M02SAZQQQQ' })
})

it('offers no Load more when the server said this was the last page', async () => {
  listFeatureRuns.mockResolvedValue(PAGE)
  render(<RunsScreen navigate={() => {}} />)

  await waitFor(() => expect(screen.getByText('August build')).toBeInTheDocument())
  expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
})

it('appends the next page on Load more and stops offering it at the end', async () => {
  listFeatureRuns
    .mockResolvedValueOnce({ ...PAGE, next_cursor: 'cur_1' })
    .mockResolvedValueOnce({
      groups: [
        {
          intent_id: 'i2',
          hypothesis: 'Card fraud',
          runs: [{ ...SPINE_RUN, generation_run_id: 'grun_02', display_name: 'July build' }],
        },
      ],
      next_cursor: null,
    })
  render(<RunsScreen navigate={() => {}} />)

  await waitFor(() => expect(screen.getByText('August build')).toBeInTheDocument())
  await userEvent.click(screen.getByRole('button', { name: 'Load more' }))

  await waitFor(() => expect(screen.getByText('July build')).toBeInTheDocument())
  expect(listFeatureRuns).toHaveBeenLastCalledWith('cur_1')
  // The first page is still there — Load more appends, it does not replace.
  expect(screen.getByText('August build')).toBeInTheDocument()
  expect(screen.queryByRole('button', { name: 'Load more' })).toBeNull()
})

it('says what the server said when the list refuses', async () => {
  listFeatureRuns.mockRejectedValue(new api.ApiError(403, 'no policy grants you feature runs'))
  render(<RunsScreen navigate={() => {}} />)

  await waitFor(() =>
    expect(screen.getByRole('alert')).toHaveTextContent('no policy grants you feature runs'),
  )
})

it('renders an honest empty list rather than a blank screen', async () => {
  listFeatureRuns.mockResolvedValue({ groups: [], next_cursor: null })
  render(<RunsScreen navigate={() => {}} />)

  await waitFor(() => expect(screen.getByText('No feature runs yet.')).toBeInTheDocument())
})

it('says it is loading before the first page lands', () => {
  listFeatureRuns.mockReturnValue(new Promise(() => {}))
  render(<RunsScreen navigate={() => {}} />)

  expect(screen.getByText('Loading runs…')).toBeInTheDocument()
})
