// The durable ingestion-run detail view (#14): every upload/import opens a run record —
// ingested, held, rejected, AND failed — and this panel renders its full manifest from
// GET /ingestion-runs/{id}: the header facts (who ran what, under which authorization), the
// append-only status history, and the per-stage reports. Read-only; reached from the ingest
// result callout and from a failed upload's error callout.
import { useEffect, useState } from 'react'
import { ApiError, getIngestionRun } from '../api'
import type { IngestionRun } from '../api'

// Stage-state tone: warn for trouble (failed/partial/audit_degraded) and behind (lagged/
// deferred); ok for succeeded; everything else — not_run, skipped_no_client, disabled,
// not_applicable, and any unknown state from a newer backend — stays muted. The chip always
// carries the state word: color never works alone.
const STAGE_WARN_STATES = new Set(['failed', 'partial', 'audit_degraded', 'lagged', 'deferred'])

function stageChipClass(state: string): string {
  if (state === 'succeeded') return 'badge ok'
  if (STAGE_WARN_STATES.has(state)) return 'badge stage-warn'
  return 'badge'
}

// Run-status chip: reuses the existing held/rejected badge tones; failed gets the danger fill.
// Unknown/in-flight statuses stay a muted badge — the word still renders.
function runChipClass(status: string): string {
  if (status === 'ingested') return 'badge ok'
  if (status === 'held') return 'badge held'
  if (status === 'rejected') return 'badge rejected'
  if (status === 'failed') return 'badge run-failed'
  return 'badge'
}

function label(token: string): string {
  return token.replace(/_/g, ' ')
}

// Wire timestamps are ISO8601 UTC; show them as-is minus the T and sub-second noise — honest
// and locale-independent (this is an audit record, not a friendly date).
function fmtWhen(iso: string | null): string {
  return iso ? iso.replace('T', ' ').replace(/\.\d+/, '') : '—'
}

function fmtDuration(started: string | null, completed: string | null): string {
  if (!started || !completed) return '—'
  const ms = new Date(completed).getTime() - new Date(started).getTime()
  if (!Number.isFinite(ms) || ms < 0) return '—'
  return ms < 1000 ? `${ms} ms` : `${(ms / 1000).toFixed(1)} s`
}

// A stage's `detail` is a small dict of honest counts/flags (no_items_resolved, resolved/expected,
// sanitized_clauses, definitions_suppressed, table_schema_mismatch_skipped, …). Render it as a
// compact key:value list so those signals are visible instead of silently dropped. Values are
// stringified defensively — a newer backend can add any JSON-scalar we haven't typed.
function detailEntries(detail: Record<string, unknown> | null): [string, string][] {
  if (!detail) return []
  return Object.entries(detail).map(([k, v]) => [k, String(v)])
}

export function RunDetailPanel({ runId, onClose }: { runId: string; onClose: () => void }) {
  const [run, setRun] = useState<IngestionRun | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let cancelled = false
    setRun(null)
    setError('')
    getIngestionRun(runId).then(
      r => {
        if (!cancelled) setRun(r)
      },
      err => {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 404) {
          setError(`Run ${runId} was not found — nothing is recorded under this id.`)
        } else {
          setError(err instanceof ApiError ? err.detail : String(err))
        }
      },
    )
    return () => {
      cancelled = true
    }
  }, [runId])

  return (
    <section className="panel" aria-label="Ingestion run details">
      <div className="run-head">
        <h2 style={{ margin: 0 }}>Ingestion run</h2>
        <span className="mono">{runId}</span>
        <button type="button" className="btn" onClick={onClose}>
          Close
        </button>
      </div>

      {!run && !error && <p className="hint">Loading run…</p>}
      {error && (
        <div className="callout callout--danger" role="alert">
          <div className="callout-body">
            <p>
              <strong>Could not load the run.</strong> {error}
            </p>
          </div>
        </div>
      )}

      {run && (
        <>
          <dl className="kv" style={{ marginTop: 12 }}>
            <div>
              <dt>Source</dt>
              <dd>
                <span className="mono">{run.catalog_source}</span> · {label(run.origin_type)}
              </dd>
            </div>
            <div>
              <dt>File</dt>
              <dd className="mono">{run.filename ?? '—'}</dd>
            </div>
            <div>
              <dt>Actor</dt>
              <dd>
                <span className="mono">{run.actor_subject ?? '—'}</span>
                {run.actor_role_claims.length > 0 && ` — roles: ${run.actor_role_claims.join(', ')}`}
              </dd>
            </div>
            <div>
              <dt>Authorization</dt>
              <dd className="mono">{run.authorization_decision ?? '—'}</dd>
            </div>
            <div>
              <dt>Status</dt>
              <dd>
                <span className={runChipClass(run.status)}>{label(run.status)}</span>
              </dd>
            </div>
            <div>
              <dt>Rows</dt>
              <dd className="tabular-nums">
                {run.row_count ?? '—'} rows · {run.quarantined_count ?? '—'} quarantined
              </dd>
            </div>
            <div>
              <dt>Started</dt>
              <dd className="mono">{fmtWhen(run.started_at)}</dd>
            </div>
            <div>
              <dt>Completed</dt>
              <dd className="mono">{fmtWhen(run.completed_at)}</dd>
            </div>
            {run.redacted_failure_code && (
              <div>
                <dt>Failure code</dt>
                <dd className="mono">{run.redacted_failure_code}</dd>
              </div>
            )}
          </dl>

          <h3 className="run-section">Status history</h3>
          {run.status_history.length === 0 ? (
            <p className="hint">No status transitions were recorded.</p>
          ) : (
            <ol className="run-history">
              {run.status_history.map((e, i) => (
                <li key={`${e.at}-${e.status}-${i}`}>
                  {fmtWhen(e.at)} · {label(e.status)}
                  {e.reason_code ? ` · ${e.reason_code}` : ''}
                </li>
              ))}
            </ol>
          )}

          <h3 className="run-section">Stages</h3>
          {run.stages.length === 0 ? (
            <p className="hint">No stage reports were recorded for this run.</p>
          ) : (
            <table>
              <thead>
                <tr>
                  <th>Stage</th>
                  <th>State</th>
                  <th>Reason</th>
                  <th>Detail</th>
                  <th className="num">Duration</th>
                </tr>
              </thead>
              <tbody>
                {run.stages.map((s, i) => {
                  const detail = detailEntries(s.detail)
                  return (
                    <tr key={`${s.stage}-${s.attempt}-${i}`}>
                      <td className="mono">
                        {s.stage}
                        {s.attempt > 1 ? ` (attempt ${s.attempt})` : ''}
                      </td>
                      <td>
                        <span className={stageChipClass(s.state)}>{label(s.state)}</span>
                      </td>
                      <td className="mono">{s.reason_code ?? '—'}</td>
                      <td className="mono stage-detail">
                        {detail.length === 0
                          ? '—'
                          : detail.map(([k, v]) => (
                              <div key={k}>
                                {k}: {v}
                              </div>
                            ))}
                      </td>
                      <td className="num tabular-nums">
                        {fmtDuration(s.started_at, s.completed_at)}
                      </td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
          )}
        </>
      )}
    </section>
  )
}
