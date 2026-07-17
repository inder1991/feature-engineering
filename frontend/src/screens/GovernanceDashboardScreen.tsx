import { useEffect, useRef, useState } from 'react'
import {
  ApiError,
  type FactTypeRollup,
  type GovernanceDashboard,
  getGovernanceDashboard,
  getSourceGovernanceDashboard,
} from '../api'

// Governance dashboard: READ-ONLY rollups over the recorded governance outcomes — what the
// enrichment passes proposed and how reviewers decided it. Four panels: per-fact-type rollups
// (pending/confirmed/rejected/needs-attention + the reject categories), queue health (open
// depth + ages), the calibration SEED (confirm rate by evidence bucket — an OBSERVATION of
// signal vs. outcome; tuning is a later step, nothing here changes scoring), and the
// cross-source overview. A source row scopes every panel to that source (a refetch of the
// single-source route); "Back to all catalogs" clears the scope. Deciding stays on the
// Governance review screen — this dashboard is the LAUNCHPAD into it: every source row has a
// Review action, and in a scoped view the open counts (pending / needs attention / open tasks)
// open that source's review queue via onReview. Cross-source counts stay static — they span
// sources, so the per-source rows are the launch point there.

const FACT_TYPE_LABELS: Record<string, string> = {
  approved_join: 'Joins',
  grain: 'Grain',
  availability_time: 'As-of',
}

// The read model's fixed age buckets (lt_1d / 1_7d / gt_7d); unknown keys from a newer
// backend still render under their raw name rather than break the strip.
const AGE_BUCKET_LABELS: Record<string, string> = {
  lt_1d: 'open < 1 day',
  '1_7d': 'open 1–7 days',
  gt_7d: 'open > 7 days',
}

function factTypeLabel(factType: string): string {
  return FACT_TYPE_LABELS[factType] ?? factType.replaceAll('_', ' ')
}

function categoryLabel(category: string): string {
  return category.replaceAll('_', ' ')
}

// "3d" / "5h" / "12m" — coarse on purpose: queue age is a triage signal, not a stopwatch.
function humanizeAge(seconds: number | null): string {
  if (seconds === null) return '—'
  const minutes = Math.floor(seconds / 60)
  if (minutes < 60) return `${minutes}m`
  const hours = Math.floor(minutes / 60)
  if (hours < 24) return `${hours}h`
  return `${Math.floor(hours / 24)}d`
}

function ratePct(rate: number | null): string {
  return rate === null ? '—' : `${Math.round(rate * 100)}%`
}

// Same semantics as ConnectorPanel's Stat: the tone colors the NUMBER only; ok is always
// colored, the alert tones (accent/warn/danger) only when nonzero — a plain 0 stays quiet ink.
// With `action` the whole tile renders as a real <button> (keyboard-accessible launch into the
// review screen); without it, the same static tile as before.
function Stat({
  n,
  label,
  tone,
  action,
}: {
  n: number
  label: string
  tone?: 'ok' | 'warn' | 'danger' | 'accent'
  action?: { onClick: () => void; ariaLabel: string }
}) {
  const colored = tone === 'ok' || (tone !== undefined && n > 0)
  const body = (
    <>
      <b className={colored ? `tone-${tone}` : undefined}>{n}</b> {label}
    </>
  )
  if (action) {
    return (
      <button type="button" className="stat" onClick={action.onClick} aria-label={action.ariaLabel}>
        {body}
      </button>
    )
  }
  return <div className="stat">{body}</div>
}

// One governed fact type's rollup card: the folded-status counts + its reject categories.
// When the view is scoped to a source (reviewSource non-null), the OPEN counts (pending /
// needs attention) launch that source's review queue; zero counts and the decided counts
// (confirmed / rejected) stay static — there is nothing to act on.
function RollupCard({
  rollup: ft,
  reviewSource,
  onReview,
}: {
  rollup: FactTypeRollup
  reviewSource: string | null
  onReview: (source: string) => void
}) {
  const rejects = Object.entries(ft.rejected_by_category)
  const launch = (n: number, kind: string) =>
    reviewSource !== null && n > 0
      ? {
          onClick: () => onReview(reviewSource),
          ariaLabel: `Review ${n} ${kind} ${factTypeLabel(ft.fact_type)} for ${reviewSource}`,
        }
      : undefined
  return (
    <li className="row q-item">
      <div className="q-head">
        <span className="gj-kind">{factTypeLabel(ft.fact_type)}</span>
        <span className="gj-score mono">{ft.fact_type}</span>
      </div>
      <div className="stats" role="group" aria-label={`${factTypeLabel(ft.fact_type)} rollup`}>
        <Stat n={ft.pending} label="pending" tone="accent" action={launch(ft.pending, 'pending')} />
        <Stat n={ft.confirmed} label="confirmed" tone="ok" />
        <Stat n={ft.rejected} label="rejected" tone="danger" />
        <Stat
          n={ft.needs_attention}
          label="needs attention"
          tone="warn"
          action={launch(ft.needs_attention, 'needs-attention')}
        />
      </div>
      {rejects.length > 0 && (
        <p className="q-note">
          Rejected by category:{' '}
          {rejects.map(([category, n]) => `${categoryLabel(category)}: ${n}`).join(' · ')}
        </p>
      )}
    </li>
  )
}

export function GovernanceDashboardScreen({
  onReview,
}: {
  // Launch the Governance review screen scoped to a source (App navigates with ?source=, the
  // same URL-borne handoff as the upload -> review-queue and connector -> semantics links).
  onReview: (source: string) => void
}) {
  const [dash, setDash] = useState<GovernanceDashboard | null>(null)
  // Which source the whole view is scoped to; null = cross-source (every catalog).
  const [scopedSource, setScopedSource] = useState<string | null>(null)
  const [error, setError] = useState('')
  const [loading, setLoading] = useState(true)
  // Monotonic id per load(): a late response from an older load must never overwrite newer data.
  const loadSeq = useRef(0)

  async function load(source: string | null) {
    const id = ++loadSeq.current
    setLoading(true)
    setError('')
    try {
      const next =
        source === null ? await getGovernanceDashboard() : await getSourceGovernanceDashboard(source)
      if (id !== loadSeq.current) return
      setDash(next)
      setScopedSource(source)
    } catch (e) {
      if (id !== loadSeq.current) return
      setError(e instanceof ApiError ? e.detail : String(e))
    } finally {
      if (id === loadSeq.current) setLoading(false)
    }
  }

  useEffect(() => {
    // Mount-only: the dashboard always opens cross-source; scoping happens via the source rows.
    void load(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  // The honest empty state: nothing proposed, nothing decided, nothing queued — anywhere the
  // current scope can see.
  const allZero =
    dash !== null &&
    dash.fact_types.every(
      ft => ft.pending + ft.confirmed + ft.rejected + ft.needs_attention === 0,
    ) &&
    dash.queue_health.open_depth === 0 &&
    Object.keys(dash.calibration_seed.confirm_rate_by_bucket).length === 0 &&
    (dash.sources ?? []).every(s => s.pending + s.confirmed + s.rejected === 0)

  return (
    <section>
      {scopedSource !== null && (
        <p className="tabular-nums">
          Scoped to <span className="mono">{scopedSource}</span>{' '}
          <button
            type="button"
            className="btn q-ghost"
            disabled={loading}
            onClick={() => void load(null)}
          >
            Back to all catalogs
          </button>
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {loading && dash === null && (
        <p className="hint" role="status">
          Loading dashboard…
        </p>
      )}
      {dash && allZero && (
        <p className="empty" role="status">
          Nothing recorded yet{scopedSource !== null ? ` for ${scopedSource}` : ''} — rollups
          appear once the enrichment passes propose facts and reviewers decide them.
        </p>
      )}
      {dash && !allZero && (
        <>
          <h2>Pipeline rollups</h2>
          <ul className="rows">
            {dash.fact_types.map(ft => (
              <RollupCard
                key={ft.fact_type}
                rollup={ft}
                reviewSource={scopedSource}
                onReview={onReview}
              />
            ))}
          </ul>

          <h2>Queue health</h2>
          <div className="stats" role="group" aria-label="Queue health">
            <Stat
              n={dash.queue_health.open_depth}
              label="open tasks"
              tone="accent"
              action={
                scopedSource !== null && dash.queue_health.open_depth > 0
                  ? {
                      onClick: () => onReview(scopedSource),
                      ariaLabel: `Review ${dash.queue_health.open_depth} open tasks for ${scopedSource}`,
                    }
                  : undefined
              }
            />
            <div className="stat">
              <b>{humanizeAge(dash.queue_health.oldest_pending_age_seconds)}</b> oldest pending
            </div>
            {Object.entries(dash.queue_health.age_buckets).map(([bucket, n]) => (
              <Stat
                key={bucket}
                n={n}
                label={AGE_BUCKET_LABELS[bucket] ?? bucket}
                tone={bucket === 'gt_7d' ? 'warn' : undefined}
              />
            ))}
          </div>
          <p className="hint tabular-nums">
            Last {dash.recent_activity.days} days: {dash.recent_activity.confirmed} confirmed ·{' '}
            {dash.recent_activity.rejected} rejected.
          </p>

          <h2>Calibration seed</h2>
          <div className="callout callout--accent">
            <div className="callout-body">
              <p>
                <strong>Observation — signal vs. outcome. Tuning is a later step.</strong> How
                reviewers actually decided, split by the evidence bucket a proposal arrived in.
                Read-only: nothing here changes how candidates are scored.
              </p>
            </div>
          </div>
          {Object.keys(dash.calibration_seed.confirm_rate_by_bucket).length === 0 ? (
            <p className="hint">No decided proposals with recorded evidence yet.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Evidence bucket</th>
                  <th className="num">Confirmed</th>
                  <th className="num">Rejected</th>
                  <th className="num">Confirm rate</th>
                </tr>
              </thead>
              <tbody>
                {Object.entries(dash.calibration_seed.confirm_rate_by_bucket).map(
                  ([bucket, tally]) => (
                    <tr key={bucket}>
                      <td className="mono">{bucket}</td>
                      <td className="num tabular-nums">{tally.confirmed}</td>
                      <td className="num tabular-nums">{tally.rejected}</td>
                      <td className="num tabular-nums">{ratePct(tally.rate)}</td>
                    </tr>
                  ),
                )}
              </tbody>
            </table>
          )}

          {scopedSource === null && dash.sources && dash.sources.length > 0 && (
            <>
              <h2>Catalogs</h2>
              <p className="hint">
                Pick a source to scope every panel above to it, or Review to open its queue.
              </p>
              <table>
                <thead>
                  <tr>
                    <th>Source</th>
                    <th className="num">Pending</th>
                    <th className="num">Confirmed</th>
                    <th className="num">Rejected</th>
                    <th className="num">Oldest pending</th>
                    <th className="num">
                      <span className="visually-hidden">Actions</span>
                    </th>
                  </tr>
                </thead>
                <tbody>
                  {dash.sources.map(s => (
                    <tr key={s.source}>
                      <td>
                        <button
                          type="button"
                          className="btn q-ghost mono"
                          disabled={loading}
                          onClick={() => void load(s.source)}
                        >
                          {s.source}
                        </button>
                      </td>
                      <td className="num tabular-nums">{s.pending}</td>
                      <td className="num tabular-nums">{s.confirmed}</td>
                      <td className="num tabular-nums">{s.rejected}</td>
                      <td className="num tabular-nums">
                        {humanizeAge(s.oldest_pending_age_seconds)}
                      </td>
                      <td className="num">
                        <button
                          type="button"
                          className="btn q-ghost"
                          aria-label={`Review ${s.source}`}
                          onClick={() => onReview(s.source)}
                        >
                          Review
                        </button>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
        </>
      )}
    </section>
  )
}
