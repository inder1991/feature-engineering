import { useState } from 'react'
import type { CSSProperties, DragEvent, FormEvent, ReactNode } from 'react'
import { ApiError, uploadFile } from '../api'
import type { IngestResult } from '../api'

export function UploadScreen({ onReviewQueue }: { onReviewQueue: (source: string) => void }) {
  const [source, setSource] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<IngestResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [hover, setHover] = useState(false)
  const [focus, setFocus] = useState(false)
  const [dragging, setDragging] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!file || !source.trim()) return
    setBusy(true)
    setError('')
    setResult(null)
    try {
      setResult(await uploadFile(file, source.trim()))
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err))
    } finally {
      setBusy(false)
    }
  }

  function onDrop(e: DragEvent<HTMLLabelElement>) {
    e.preventDefault()
    setDragging(false)
    const dropped = e.dataTransfer.files?.[0]
    if (dropped) setFile(dropped)
  }

  // Drop-target styling is inline because index.css has no drop-target class and this screen
  // may not add one. Hover/drag/focus states are tracked in React for the same reason (the
  // hidden input's focus ring is surfaced on the label, mirroring the global focus style).
  const active = hover || dragging
  const dropStyle: CSSProperties = {
    justifyItems: 'start',
    gap: 8,
    padding: '24px 20px',
    border: `1px ${file ? 'solid' : 'dashed'} ${active ? 'var(--accent-line)' : 'var(--line-strong)'}`,
    borderRadius: 'var(--radius-panel)',
    background: dragging ? 'var(--accent-soft)' : 'var(--surface)',
    cursor: 'pointer',
    outline: focus ? '2px solid var(--accent-line)' : 'none',
    outlineOffset: 2,
    transition:
      'border-color var(--dur-fast) var(--ease-out-quart), background-color var(--dur-fast) var(--ease-out-quart)',
  }

  return (
    <section>
      <div className="panel">
        <form onSubmit={submit} style={{ display: 'grid', gap: 16, margin: 0 }}>
          <div className="field" style={{ maxWidth: 320 }}>
            <label>
              Source name
              <input
                value={source}
                onChange={e => setSource(e.target.value)}
                placeholder="e.g. deposits"
                required
              />
            </label>
          </div>
          <div className="field">
            <label
              style={dropStyle}
              onMouseEnter={() => setHover(true)}
              onMouseLeave={() => setHover(false)}
              onDragOver={e => {
                e.preventDefault()
                setDragging(true)
              }}
              onDragLeave={() => setDragging(false)}
              onDrop={onDrop}
            >
              File (.csv / .xlsx)
              {/* No `required` here: jsdom never counts an uploaded FileList toward a required
                  file input's validity (valueMissing stays true), which blocks programmatic form
                  submission in tests. It would also fight the drop path (dropping sets React
                  state, not input.files). Empty submission is already prevented by the disabled
                  button and the `if (!file …) return` guard above. */}
              <input
                type="file"
                accept=".csv,.xlsx"
                className="visually-hidden"
                onChange={e => setFile(e.target.files?.[0] ?? null)}
                onFocus={() => setFocus(true)}
                onBlur={() => setFocus(false)}
              />
              {file ? (
                <span className="mono" style={{ fontWeight: 400, color: 'var(--ink)' }}>
                  {file.name}
                </span>
              ) : (
                <span className="hint" style={{ fontWeight: 400 }}>
                  Drop a file here, or click to choose one.
                </span>
              )}
            </label>
          </div>
          <button
            type="submit"
            className="btn btn--primary"
            style={{ justifySelf: 'start' }}
            disabled={busy || !file || !source.trim()}
          >
            {busy ? 'Uploading…' : 'Upload'}
          </button>
        </form>
      </div>
      {error && (
        <div className="callout callout--danger" role="alert">
          <CalloutGlyph>
            <circle cx="8" cy="8" r="6.25" />
            <path d="m5.75 5.75 4.5 4.5m0-4.5-4.5 4.5" />
          </CalloutGlyph>
          <div className="callout-body">
            <p>
              <strong>Upload failed.</strong> {error}
            </p>
          </div>
        </div>
      )}
      {result && (
        <IngestResultCallout result={result} source={source.trim()} onReviewQueue={onReviewQueue} />
      )}
    </section>
  )
}

// Semantic count: asserted/live reads in --ok; staled/quarantined in --warn, but only when
// nonzero (a plain 0 stays quiet ink). Presentational only; the number and text are unchanged.
function Count({ value, tone }: { value: number; tone: 'ok' | 'warn' }) {
  const colored = tone === 'ok' || value > 0
  return (
    <span style={colored ? { color: `var(--${tone})`, fontWeight: 600 } : undefined}>{value}</span>
  )
}

function CalloutGlyph({ children }: { children: ReactNode }) {
  return (
    <span className="callout-glyph" aria-hidden="true">
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        {children}
      </svg>
    </span>
  )
}

function IngestResultCallout({
  result,
  source,
  onReviewQueue,
}: {
  result: IngestResult
  source: string
  onReviewQueue: (source: string) => void
}) {
  if (result.status === 'held') {
    return (
      <div className="callout callout--warn" role="status">
        <CalloutGlyph>
          <path d="M8 2.75 14 13.25H2z" />
          <path d="M8 6.75v2.75M8 11.5v.01" />
        </CalloutGlyph>
        <div className="callout-body">
          <p>
            <strong>Held: confirm this large change.</strong>
          </p>
          <p>{result.reason}</p>
          <p>
            Nothing was applied. Check the file targets the right source, then re-upload to
            confirm.
          </p>
        </div>
      </div>
    )
  }
  if (result.status === 'rejected') {
    return (
      <div className="callout callout--danger" role="status">
        <CalloutGlyph>
          <circle cx="8" cy="8" r="6.25" />
          <path d="m5.75 5.75 4.5 4.5m0-4.5-4.5 4.5" />
        </CalloutGlyph>
        <div className="callout-body">
          <p>
            <strong>Rejected.</strong>
          </p>
          <p>{result.reason}</p>
        </div>
      </div>
    )
  }
  return (
    <div className="callout callout--ok" role="status">
      <CalloutGlyph>
        <circle cx="8" cy="8" r="6.25" />
        <path d="m5.25 8.25 2 2 3.5-4.5" />
      </CalloutGlyph>
      <div className="callout-body">
        <p>
          <strong>Ingested.</strong>
        </p>
        <p className="tabular-nums">
          <Count value={result.asserted} tone="ok" /> facts asserted,{' '}
          <Count value={result.staled} tone="warn" /> staled,{' '}
          <Count value={result.quarantined} tone="warn" /> quarantined
        </p>
        {result.flagged && (
          <p style={{ color: 'var(--warn)' }}>
            <span style={{ fontWeight: 600 }}>Flagged:</span> {result.flagged}
          </p>
        )}
        {result.quarantined > 0 && (
          <button type="button" className="btn" onClick={() => onReviewQueue(source)}>
            Review {result.quarantined} quarantined row{result.quarantined === 1 ? '' : 's'}
          </button>
        )}
      </div>
    </div>
  )
}
