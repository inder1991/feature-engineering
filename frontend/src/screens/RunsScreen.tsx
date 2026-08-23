import { type MouseEvent, useEffect, useState } from 'react'
import {
  ApiError,
  type FeatureRunGroup,
  type FeatureRunSummary,
  listFeatureRuns,
} from '../api'
import type { Route } from '../nav'

type Navigate = (r: Route, params?: Record<string, string> | URLSearchParams) => void

// How much of an opaque run id is shown before the ellipsis. Long enough to tell two runs apart at
// a glance ('grun_01M02SAZ…'), short enough not to swallow the row.
const ID_HEAD = 13

// Truncate ONLY when there is something to truncate: an ellipsis on a short legacy id
// ('fgr_legacy01') would claim the reader is missing characters that do not exist.
function shortId(id: string): string {
  return id.length > ID_HEAD ? `${id.slice(0, ID_HEAD)}…` : id
}

// Falls back to the raw value rather than rendering "Invalid Date" if the server ever sends a
// timestamp Date cannot parse.
function readableAt(iso: string): string {
  const d = new Date(iso)
  if (Number.isNaN(d.getTime())) return iso
  return d.toLocaleDateString(undefined, { day: 'numeric', month: 'short', year: 'numeric' })
}

// The feature-run list (GET /feature-runs), grouped by the hypothesis each run was generated
// under. Read-only by construction: the spine DERIVES every field from stores that already hold
// the evidence, and there is no write endpoint to offer.
export function RunsScreen({ navigate }: { navigate: Navigate }) {
  const [groups, setGroups] = useState<FeatureRunGroup[] | null>(null)
  const [cursor, setCursor] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [loadingMore, setLoadingMore] = useState(false)
  const [copyStatus, setCopyStatus] = useState('')

  useEffect(() => {
    let live = true
    listFeatureRuns()
      .then(page => {
        if (!live) return
        setGroups(page.groups)
        setCursor(page.next_cursor)
      })
      .catch(err => live && setError(err instanceof ApiError ? err.detail : String(err)))
    return () => {
      live = false
    }
  }, [])

  async function loadMore() {
    if (cursor === null) return
    setLoadingMore(true)
    // A failed page must not leave its message standing over a page that then succeeds.
    setError('')
    try {
      const page = await listFeatureRuns(cursor)
      // Pages are APPENDED, never merged. The server groups WITHIN a page, so one intent whose
      // runs straddle a page boundary legitimately opens a second section — folding the two
      // together here would invent a grouping the server did not make, and folding two null
      // buckets would assert a shared intent that does not exist.
      setGroups(prev => [...(prev ?? []), ...page.groups])
      setCursor(page.next_cursor)
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err))
    } finally {
      setLoadingMore(false)
    }
  }

  async function copyId(id: string) {
    try {
      await navigator.clipboard.writeText(id)
      setCopyStatus(`Run id copied: ${id}`)
    } catch {
      // A browser may refuse clipboard access outright. Saying "copied" would be a lie, and saying
      // only "failed" leaves the reader with nothing — so hand them the id itself.
      setCopyStatus(`Could not copy. The run id is ${id}`)
    }
  }

  // The row is the click target, but the run name inside it is a real anchor at the canonical
  // '#/runs/<id>' path so the link is copyable and openable in a new tab. A MODIFIED click is the
  // reader asking the browser for that new tab: leave the default alone and navigate nothing here.
  function openRun(event: MouseEvent, id: string) {
    if (event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
    event.preventDefault()
    navigate('runs', { run_id: id })
  }

  if (groups === null)
    return error ? (
      <p role="alert" className="error">
        {error}
      </p>
    ) : (
      <p className="hint" role="status">
        Loading runs…
      </p>
    )

  if (groups.length === 0)
    return (
      <div className="empty" role="status">
        <p>No feature runs yet.</p>
        <p className="next">A run appears here as soon as feature generation produces one.</p>
      </div>
    )

  return (
    <section>
      <h2>Feature runs</h2>
      <p className="hint" role="status" data-testid="copy-status">
        {copyStatus}
      </p>
      {groups.map((group, index) => (
        // The intent id alone is not a key: grouping happens within a page, so the same intent can
        // open two sections and two different groups can both carry a null intent.
        <div key={`${group.intent_id ?? 'no-intent'}#${index}`}>
          {/* The heading answers the hypothesis, not the intent id — a run can carry an intent id
              whose intent row is gone (there is no FK), and the reader can only act on what is
              actually recorded. */}
          <h3>{group.hypothesis ?? 'No hypothesis recorded'}</h3>
          {group.hypothesis === null && (
            <p className="hint">These runs are not linked to a recorded hypothesis.</p>
          )}
          <ul className="rows">
            {group.runs.map(run => (
              <RunRow
                key={run.generation_run_id}
                run={run}
                onOpen={openRun}
                onCopy={copyId}
              />
            ))}
          </ul>
        </div>
      ))}
      {/* Re-firing a read is safe — nothing on this screen mutates — so the button needs no
          idempotency guard beyond not stacking two in-flight pages. */}
      {cursor !== null && (
        <button type="button" className="btn" onClick={loadMore} disabled={loadingMore}>
          {loadingMore ? 'Loading…' : 'Load more'}
        </button>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
    </section>
  )
}

function RunRow({
  run,
  onOpen,
  onCopy,
}: {
  run: FeatureRunSummary
  onOpen: (event: MouseEvent, id: string) => void
  onCopy: (id: string) => void
}) {
  const id = run.generation_run_id
  return (
    // The row is a click shortcut only; the anchor inside it is the focusable, keyboard-operable
    // control, and Enter on it produces the very click event handled here.
    <li className="row" onClick={event => onOpen(event, id)}>
      <div style={{ display: 'grid', gap: 2, minWidth: 0, flex: 1 }}>
        <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
          {/* aria-label carries the id because the visible name can be the absence dash, and
              "Open run —" would name nothing. */}
          <a href={`#/runs/${encodeURIComponent(id)}`} aria-label={`Open run ${id}`}>
            <strong>{run.display_name ?? '—'}</strong>
          </a>
          {run.pre_spine && (
            <span
              className="badge"
              title="This run predates the identity spine, so it has no run identity record. An honest gap in the record, never a failure of the run."
            >
              Pre-spine
            </span>
          )}
        </div>
        <p className="hint">
          <span className="mono" title={id}>
            {shortId(id)}
          </span>
          {' · '}
          <span>{run.owner_subject ?? '—'}</span>
          {' · '}
          <span>{readableAt(run.created_at)}</span>
        </p>
      </div>
      <button
        type="button"
        className="btn"
        aria-label={`Copy run id ${id}`}
        // The copy is an action ON the row, not a way into it: without this, copying would also
        // navigate away from the list the reader is still reading.
        onClick={event => {
          event.stopPropagation()
          void onCopy(id)
        }}
      >
        Copy id
      </button>
    </li>
  )
}
