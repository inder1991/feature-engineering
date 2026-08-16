import { act, fireEvent, render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { afterEach, beforeEach, expect, it, vi } from 'vitest'
import * as api from '../api'
import { COPY_STATUS_MS, SearchHitRow } from './SearchHitRow'

vi.mock('../api', async importOriginal => {
  const actual = await importOriginal<typeof import('../api')>()
  return { ...actual, featureImpact: vi.fn() }
})
const featureImpact = vi.mocked(api.featureImpact)

const HIT: api.SearchHit = {
  object_ref: 'public.accounts.balance', table: 'accounts', column: 'balance', kind: 'column',
  data_type: 'numeric', definition: 'end-of-day ledger balance', is_grain: false, is_as_of: false,
  catalog_source: 'deposits', concept: null, domain: null, sensitivity: null,
  sensitivity_display: null, additivity: null, unit: null, currency: null, entity: null, score: 1,
}

// A TABLE hit. The unfiltered browse lists tables first, so this is the row a reader meets before
// any column — and every action the row offers is offered on it too.
const TABLE_HIT: api.SearchHit = {
  ...HIT, object_ref: 'public.accounts', column: null, kind: 'table', data_type: null,
  definition: 'customer account master',
}

function renderRow(hit: api.SearchHit = HIT) {
  const onOpen = vi.fn()
  const onExplore = vi.fn()
  const onSuggested = vi.fn()
  render(<SearchHitRow hit={hit} onOpen={onOpen} onExplore={onExplore} onSuggested={onSuggested} />)
  return { onOpen, onExplore, onSuggested }
}

// The tertiary actions live behind the row's `···` disclosure, so every test that reaches one has
// to open it first — exactly as a reader does.
async function openOverflow() {
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
}

// Braced body, deliberately: a CONCISE arrow returns the mock, and Vitest calls a function
// returned from beforeEach as that test's teardown — which would invoke featureImpact() with
// nobody awaiting it, and a rejecting mock would then fail the test it just passed.
beforeEach(() => {
  featureImpact.mockReset()
})

// The clipboard tests below install their mock straight onto the SHARED `navigator` and never take
// it off, so the last one installed — a REJECTING writeText — stayed in place for every test that
// ran afterwards, including any test appended to the end of this file. Restore whatever was there
// before (jsdom implements no clipboard, so usually: nothing at all).
const clipboardBefore = Object.getOwnPropertyDescriptor(navigator, 'clipboard')
afterEach(() => {
  if (clipboardBefore) Object.defineProperty(navigator, 'clipboard', clipboardBefore)
  else Reflect.deleteProperty(navigator, 'clipboard')
  // Same lesson as the clipboard, and worse: a fake clock left installed by a test that TIMED OUT
  // (its own try/finally never runs — the runner abandons the promise) hangs every test after it.
  // Restoring here is unconditional and runs whatever happened.
  vi.useRealTimers()
})

// What a screen reader is handed: the accessibility tree drops an aria-hidden element AND its
// whole subtree, so anything living inside the hidden separator — spaces included — is simply not
// there. toHaveTextContent reads textContent, which still sees it, and cannot catch this.
function accessibleText(el: HTMLElement): string {
  const clone = el.cloneNode(true) as HTMLElement
  for (const hidden of clone.querySelectorAll('[aria-hidden="true"]')) hidden.remove()
  return (clone.textContent ?? '').replace(/\s+/g, ' ').trim()
}

it('leads with the column name and demotes the physical ref to a breadcrumb', () => {
  renderRow()
  expect(screen.getByTestId('hit-name')).toHaveTextContent('balance')
  const trail = screen.getByTestId('hit-breadcrumb')
  expect(trail).toHaveTextContent('deposits')
  expect(trail).toHaveTextContent('public.accounts')
})

it('keeps the address readable once the separator glyph is hidden', () => {
  renderRow()
  // With the spaces inside the aria-hidden span this reads "depositspublic.accountsbalance" —
  // one run-on token where the old row had a single readable <code>.
  expect(accessibleText(screen.getByTestId('hit-breadcrumb')))
    .toBe('deposits public.accounts balance')
})

it('puts the definition in the reading position', () => {
  renderRow()
  expect(screen.getByText('end-of-day ledger balance')).toBeInTheDocument()
})

it('renders no definition element at all when the catalog holds none', () => {
  renderRow({ ...HIT, definition: null })
  expect(screen.queryByTestId('hit-definition')).toBeNull()
})

it('shows the roles the hit carries as capability lines', () => {
  renderRow({ ...HIT, is_grain: true, entity: 'Account', is_as_of: true })
  expect(screen.getByText('Grain key for Account')).toBeInTheDocument()
  expect(screen.getByText('As-of field')).toBeInTheDocument()
})

it('badges grain, as-of, table kind and both sensitivity axes', () => {
  renderRow({
    ...HIT, kind: 'table', column: null, is_grain: true, is_as_of: true,
    sensitivity: 'pii', sensitivity_display: 'restricted',
  })
  expect(screen.getByText('table')).toBeInTheDocument()
  expect(screen.getByText('grain')).toBeInTheDocument()
  expect(screen.getByText('as-of')).toBeInTheDocument()
  expect(screen.getByText('pii')).toBeInTheDocument()
  expect(screen.getByText('restricted')).toBeInTheDocument()
})

it('makes Open asset the primary action and Explore relationships the secondary', async () => {
  const { onOpen, onExplore } = renderRow()
  const open = screen.getByRole('button', { name: 'Open asset public.accounts.balance' })
  expect(open).toHaveClass('btn--primary')
  await userEvent.click(open)
  expect(onOpen).toHaveBeenCalledWith(HIT)

  const explore = screen.getByRole('button', {
    name: 'Explore relationships for public.accounts.balance',
  })
  expect(explore).not.toHaveClass('btn--primary')
  await userEvent.click(explore)
  expect(onExplore).toHaveBeenCalledWith(HIT)
})

it('opens suggested features for the hit’s table', async () => {
  const { onSuggested } = renderRow()
  await openOverflow()
  await userEvent.click(screen.getByRole('button', { name: 'Suggested features for accounts' }))
  expect(onSuggested).toHaveBeenCalledWith(HIT)
})

it('lists derived feature ids inline when impact finds features', async () => {
  featureImpact.mockResolvedValue(['feat_01', 'feat_02'])
  renderRow()
  await openOverflow()
  await userEvent.click(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  )
  expect(featureImpact).toHaveBeenCalledWith('public.accounts.balance', 'deposits')
  expect(await screen.findByText('feat_01')).toBeInTheDocument()
  expect(screen.getByText('feat_02')).toBeInTheDocument()
  expect(screen.getByText('Derived features')).toBeInTheDocument()
})

it('says plainly when nothing derives from the column', async () => {
  featureImpact.mockResolvedValue([])
  renderRow()
  await openOverflow()
  await userEvent.click(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  )
  expect(await screen.findByText('No features derive from this column.')).toBeInTheDocument()
  // The label heads a list; an empty result must not print a heading over nothing.
  expect(screen.queryByText('Derived features')).not.toBeInTheDocument()
})

// The same sentence on a TABLE row. Feature impact is offered on every row and tables reach this
// component (the unfiltered browse lists them first), so a hardcoded "column" told the reader the
// catalog holds a column called `public.accounts` — a fact asserted with nothing behind it.
it('says table, not column, when the empty result is a table’s', async () => {
  featureImpact.mockResolvedValue([])
  renderRow(TABLE_HIT)
  await userEvent.click(screen.getByRole('button', { name: 'More actions for public.accounts' }))
  await userEvent.click(screen.getByRole('button', { name: 'Feature impact for public.accounts' }))
  expect(await screen.findByText('No features derive from this table.')).toBeInTheDocument()
  expect(screen.queryByText('No features derive from this column.')).toBeNull()
})

it('surfaces an impact failure as an alert without losing the row', async () => {
  featureImpact.mockRejectedValue(new api.ApiError(503, 'graph unavailable'))
  renderRow()
  await openOverflow()
  await userEvent.click(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  )
  expect(await screen.findByRole('alert')).toHaveTextContent('Impact check failed: graph unavailable')
  expect(screen.getByTestId('hit-name')).toHaveTextContent('balance')
  // A failed check is not an empty result: no heading over a list that was never fetched.
  expect(screen.queryByText('Derived features')).not.toBeInTheDocument()
})

it('hides the tertiary actions behind an overflow disclosure', async () => {
  renderRow()
  expect(screen.queryByRole('button', { name: 'Suggested features for accounts' })).toBeNull()

  const trigger = screen.getByRole('button', { name: 'More actions for public.accounts.balance' })
  expect(trigger).toHaveAttribute('aria-expanded', 'false')
  await userEvent.click(trigger)
  expect(trigger).toHaveAttribute('aria-expanded', 'true')
  expect(screen.getByRole('button', { name: 'Suggested features for accounts' })).toBeInTheDocument()
  expect(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  ).toBeInTheDocument()
})

it('closes the overflow on Escape and returns focus to its trigger', async () => {
  renderRow()
  const trigger = screen.getByRole('button', { name: 'More actions for public.accounts.balance' })
  await userEvent.click(trigger)

  // Tab INTO the popover before pressing Escape. Without this the trigger still holds the focus
  // that opening it gave (userEvent.click focuses its target on mousedown), so the final
  // assertion would hold whether or not Escape returns focus at all — the test would stay green
  // with the component's `trigger.current?.focus()` deleted. The contract only bites when focus
  // is inside the popover, which is the keyboard user's actual path.
  await userEvent.tab()
  const firstItem = screen.getByRole('button', { name: 'Suggested features for accounts' })
  expect(firstItem).toHaveFocus()

  await userEvent.keyboard('{Escape}')
  expect(screen.queryByRole('button', { name: 'Suggested features for accounts' })).toBeNull()
  expect(trigger).toHaveFocus()
})

it('returns focus to the trigger when an item is activated from the keyboard', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.assign(navigator, { clipboard: { writeText } })
  renderRow()
  const trigger = screen.getByRole('button', { name: 'More actions for public.accounts.balance' })
  await userEvent.click(trigger)

  // Three tabs walks the popover's DOM order: Suggested features, Feature impact, Copy reference.
  await userEvent.tab()
  await userEvent.tab()
  await userEvent.tab()
  expect(
    screen.getByRole('button', { name: 'Copy reference for public.accounts.balance' }),
  ).toHaveFocus()

  await userEvent.keyboard('{Enter}')
  // The row is still on screen and has just gained something to read, so focus must land back on
  // the trigger rather than falling to <body> when the chosen button unmounts.
  expect(await screen.findByRole('status')).toHaveTextContent('Reference copied')
  expect(trigger).toHaveFocus()
})

it('closes the overflow when the pointer goes elsewhere', async () => {
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
  await userEvent.click(document.body)
  expect(screen.queryByRole('button', { name: 'Suggested features for accounts' })).toBeNull()
})

it('closes the overflow after an item is chosen', async () => {
  const { onSuggested } = renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
  await userEvent.click(screen.getByRole('button', { name: 'Suggested features for accounts' }))
  expect(onSuggested).toHaveBeenCalledWith(HIT)
  expect(screen.queryByRole('button', { name: 'Suggested features for accounts' })).toBeNull()
})

it('copies the object ref and says so', async () => {
  const writeText = vi.fn().mockResolvedValue(undefined)
  Object.assign(navigator, { clipboard: { writeText } })
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
  await userEvent.click(
    screen.getByRole('button', { name: 'Copy reference for public.accounts.balance' }),
  )
  expect(writeText).toHaveBeenCalledWith('public.accounts.balance')
  expect(await screen.findByRole('status')).toHaveTextContent('Reference copied')
})

it('shows the reference when the browser refuses to copy it', async () => {
  Object.assign(navigator, {
    clipboard: { writeText: vi.fn().mockRejectedValue(new Error('denied')) },
  })
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
  await userEvent.click(
    screen.getByRole('button', { name: 'Copy reference for public.accounts.balance' }),
  )
  expect(await screen.findByRole('status'))
    .toHaveTextContent('Could not copy. The reference is public.accounts.balance')
})

// The acknowledgement is TRANSIENT: it acknowledges an action, it is not a property of the asset.
// Left standing, every row a reader had ever copied from stayed one line taller for the rest of
// the session, and the refusal fallback printed the reader's object_ref in the row permanently.
//
// Fake timers, because the real wait is eight seconds. Two constraints shape how:
//
// `toFake` is setTimeout and clearTimeout ONLY. The default set takes `setImmediate` with it,
// which is one of the yields React's async act() uses — the clock nobody is advancing then
// swallows act's own resumption and the test hangs until the runner kills it.
//
// fireEvent, not userEvent, and act() rather than findBy: BOTH of the library's async paths drain
// the microtask queue with a real `setTimeout(…, 0)` and advance the clock afterwards only if they
// can see JEST's fake timers (`typeof jest !== 'undefined'`). Under Vitest they cannot, so every
// await hangs on a timer nobody will fire. The user-level path through these same two clicks is
// covered by the two tests above, which run on a real clock; what is under test here is the timer.
function freezeTheClock() {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout'] })
}

async function copyUnderFakeClock(writeText: ReturnType<typeof vi.fn>) {
  // defineProperty rather than Object.assign, matching what the tests above rely on: navigator's
  // clipboard can be a getter-only property, and assigning over one throws. The shared afterEach
  // puts the original descriptor back either way.
  Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true })
  renderRow()
  fireEvent.click(screen.getByRole('button', { name: 'More actions for public.accounts.balance' }))
  fireEvent.click(
    screen.getByRole('button', { name: 'Copy reference for public.accounts.balance' }),
  )
  // Lets the clipboard promise settle and its state update commit, without touching the clock.
  await act(async () => {})
}

it('takes the copy acknowledgement back down once it has been read', async () => {
  freezeTheClock()
  await copyUnderFakeClock(vi.fn().mockResolvedValue(undefined))
  expect(screen.getByRole('status')).toHaveTextContent('Reference copied')
  // One tick short of the window it is still there — so the assertion below is the timer firing,
  // not the message having never rendered.
  await act(async () => { vi.advanceTimersByTime(COPY_STATUS_MS - 1) })
  expect(screen.getByRole('status')).toHaveTextContent('Reference copied')

  await act(async () => { vi.advanceTimersByTime(1) })
  expect(screen.queryByRole('status')).toBeNull()
})

// The refusal fallback is the one that matters most: it is the row printing the full object_ref,
// and it must still be handed to the reader while it is up — then taken down like any other.
it('takes the refusal fallback down too, reference and all', async () => {
  freezeTheClock()
  await copyUnderFakeClock(vi.fn().mockRejectedValue(new Error('denied')))
  expect(screen.getByRole('status'))
    .toHaveTextContent('Could not copy. The reference is public.accounts.balance')

  await act(async () => { vi.advanceTimersByTime(COPY_STATUS_MS) })
  expect(screen.queryByRole('status')).toBeNull()
  expect(screen.queryByText(/Could not copy/)).toBeNull()
})

it('walks the row’s controls in priority order on Tab', async () => {
  renderRow()
  await userEvent.tab()
  expect(screen.getByRole('button', { name: 'Open asset public.accounts.balance' })).toHaveFocus()
  await userEvent.tab()
  expect(
    screen.getByRole('button', { name: 'Explore relationships for public.accounts.balance' }),
  ).toHaveFocus()
  await userEvent.tab()
  expect(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  ).toHaveFocus()
})

// The popover's THIRD exit. Escape and an outside pointerdown were its only two, so anything that
// moved focus without a key or a click left it open and floating over the row — the search screen's
// "/" shortcut, which focuses the query field from anywhere on the page, does exactly that.
it('closes the overflow when focus leaves it', async () => {
  renderRow()
  // A control outside the row, appended AFTER it so a Tab off the popover's last item reaches it.
  const outside = document.createElement('button')
  outside.textContent = 'Elsewhere'
  document.body.append(outside)
  try {
    await userEvent.click(
      screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
    )
    // Three tabs walks the popover's items; the fourth leaves the row entirely.
    await userEvent.tab()
    await userEvent.tab()
    await userEvent.tab()
    expect(
      screen.getByRole('button', { name: 'Copy reference for public.accounts.balance' }),
    ).toHaveFocus()

    await userEvent.tab()
    expect(outside).toHaveFocus()
    expect(screen.queryByRole('button', { name: 'Suggested features for accounts' })).toBeNull()
  } finally {
    outside.remove()
  }
})

// The guard on that exit: `focusout` fires on every hop BETWEEN the popover's own items too, so a
// handler that closed on any focus loss would shut the popover the moment a keyboard user tabbed
// from the first item to the second — one Tab after opening it.
it('stays open while focus moves between its own items', async () => {
  renderRow()
  await userEvent.click(
    screen.getByRole('button', { name: 'More actions for public.accounts.balance' }),
  )
  await userEvent.tab()
  await userEvent.tab()
  expect(
    screen.getByRole('button', { name: 'Feature impact for public.accounts.balance' }),
  ).toHaveFocus()
  expect(
    screen.getByRole('button', { name: 'Suggested features for accounts' }),
  ).toBeInTheDocument()
})
