// The standard ingest-result vocabulary, shared by both ingest paths (file upload and the
// OpenMetadata connector). The SAME pipeline produces the result either way, so the SAME
// callout renders it — held, rejected, and ingested keep one voice across vehicles.
import type { ReactNode } from 'react'
import type { IngestResult } from '../api'

export function CalloutGlyph({ children }: { children: ReactNode }) {
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

// Semantic count: asserted/live reads in --ok; changed/quarantined in --warn, but only when
// nonzero (a plain 0 stays quiet ink). Presentational only; the number and text are unchanged.
export function Count({ value, tone }: { value: number; tone: 'ok' | 'warn' }) {
  const colored = tone === 'ok' || value > 0
  return (
    <span style={colored ? { color: `var(--${tone})`, fontWeight: 600 } : undefined}>{value}</span>
  )
}

export function IngestResultCallout({
  result,
  source,
  onReviewQueue,
  heldAdvice,
}: {
  result: IngestResult
  source: string
  onReviewQueue: (source: string) => void
  // The held state's "what now" sentence differs by vehicle: a file the owner edits, a
  // connector whose scope they narrow. Defaults to the file-upload advice.
  heldAdvice?: string
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
            <strong>
              Held: this change removes too much of the existing catalog to apply automatically.
            </strong>
          </p>
          <p>{result.reason}</p>
          <p>
            Nothing was applied. There is no override yet.{' '}
            {heldAdvice ??
              'Adjust the file so it keeps most existing objects, or split the change into smaller uploads.'}
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
          <Count value={result.changed_objects} tone="warn" /> objects changed,{' '}
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
