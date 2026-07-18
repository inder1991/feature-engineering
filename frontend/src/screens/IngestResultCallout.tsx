// The standard ingest-result vocabulary, shared by both ingest paths (file upload and the
// OpenMetadata connector). The SAME pipeline produces the result either way, so the SAME
// callout renders it — held, rejected, and ingested keep one voice across vehicles.
import { useEffect, useState, type ReactNode } from 'react'
import { getIngestionRun, type IngestionStage, type IngestResult } from '../api'
import { RunDetailPanel } from './RunDetailPanel'

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

// ---- per-stage summary line (one honest sentence, never a stage dump) ------------------------

// The states that read as a soft warning wherever they appear.
const WARN_STATES = new Set(['failed', 'partial', 'audit_degraded'])

// Stages the dedicated rules below already voice; the warn catch-all skips them.
const VOICED_STAGES = new Set(['pass_b', 'pass_c', 'projection_drain'])

export interface StageSummarySegment {
  text: string
  warn: boolean
}

function humanize(token: string): string {
  return token === 'audit_degraded' ? 'audit-degraded' : token.replace(/_/g, ' ')
}

// Fold the run's stages into a few segments: enrichment (the enrich_* stages) speaks as one
// word, Pass B/C report on/off/skipped, the projection drain speaks only when it is behind, and
// any OTHER stage surfaces only in a warn state (failed | partial | audit_degraded). Unknown
// stages/states from a newer backend stay quiet — this line summarizes, it never breaks.
// eslint-disable-next-line react-refresh/only-export-components -- pure summarizer, exported for tests
export function summarizeStages(stages: IngestionStage[]): StageSummarySegment[] {
  const segments: StageSummarySegment[] = []

  const enrich = stages.filter(s => s.stage.startsWith('enrich_'))
  if (enrich.some(s => s.state === 'failed')) {
    segments.push({ text: 'enrichment failed', warn: true })
  } else if (enrich.some(s => s.state === 'partial')) {
    segments.push({ text: 'enrichment partial', warn: true })
  } else if (enrich.some(s => s.state === 'succeeded')) {
    segments.push({ text: 'Enriched', warn: false })
  } else if (enrich.some(s => s.state === 'skipped_no_client')) {
    segments.push({ text: 'enrichment skipped', warn: false })
  } else if (enrich.some(s => s.state === 'disabled')) {
    segments.push({ text: 'enrichment off', warn: false })
  }

  for (const [stage, label] of [['pass_b', 'Pass B'], ['pass_c', 'Pass C']] as const) {
    const s = stages.find(x => x.stage === stage)
    if (!s) continue
    if (s.state === 'succeeded') segments.push({ text: `${label} on`, warn: false })
    else if (s.state === 'disabled') segments.push({ text: `${label} off`, warn: false })
    else if (s.state === 'skipped_no_client') segments.push({ text: `${label} skipped`, warn: false })
    else if (WARN_STATES.has(s.state)) {
      segments.push({ text: `${label} ${humanize(s.state)}`, warn: true })
    }
    // not_applicable / not_run / unknown: quiet
  }

  const drain = stages.find(s => s.stage === 'projection_drain')
  if (drain) {
    if (drain.state === 'lagged' || drain.state === 'deferred') {
      segments.push({ text: `projection ${drain.state}`, warn: true })
    } else if (WARN_STATES.has(drain.state)) {
      segments.push({ text: `projection ${humanize(drain.state)}`, warn: true })
    }
  }

  for (const s of stages) {
    if (s.stage.startsWith('enrich_') || VOICED_STAGES.has(s.stage)) continue
    if (WARN_STATES.has(s.state)) {
      segments.push({ text: `${humanize(s.stage)} ${humanize(s.state)}`, warn: true })
    }
  }
  return segments
}

// Best-effort color under the result — ANY result: held/rejected runs carry stages too (incl.
// not_run for what never got a chance). Fetch the run, render one compact line. A fetch failure
// (or an all-quiet run) renders nothing extra — the core result above already told the truth,
// and this line must never block or break it.
function StageSummaryLine({ runId }: { runId: string }) {
  const [segments, setSegments] = useState<StageSummarySegment[] | null>(null)
  useEffect(() => {
    let cancelled = false
    getIngestionRun(runId).then(
      run => {
        if (!cancelled) setSegments(summarizeStages(run.stages))
      },
      () => {}, // degrade silently: the summary is a bonus, never a gate
    )
    return () => {
      cancelled = true
    }
  }, [runId])
  if (!segments || segments.length === 0) return null
  return (
    <p>
      {segments.map((seg, i) => (
        <span key={`${i}-${seg.text}`}>
          {i > 0 && ' · '}
          <span style={seg.warn ? { color: 'var(--warn)', fontWeight: 600 } : undefined}>
            {seg.text}
          </span>
        </span>
      ))}
    </p>
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
  // Every outcome opens a run record now (held/rejected included), so every branch gets the
  // compact stage line AND the same door into the full manifest. The panel renders as a
  // sibling below the callout, never inside it.
  const runId = result.ingestion_run_id ?? null
  const [showRun, setShowRun] = useState(false)
  const runDetailsButton = runId && (
    <button
      type="button"
      className="btn"
      aria-expanded={showRun}
      onClick={() => setShowRun(v => !v)}
    >
      {showRun ? 'Hide run details' : 'View run details'}
    </button>
  )
  const runDetailsPanel = showRun && runId && (
    <RunDetailPanel runId={runId} onClose={() => setShowRun(false)} />
  )
  // The backend PERSISTS quarantine rows even when the catalog change itself is held or
  // rejected, so both branches must surface the queue (#12) — and the copy must not claim
  // "nothing was applied" when the review queue just changed.
  const quarantineHandoff = result.quarantined > 0 && (
    <>
      <p>
        <Count value={result.quarantined} tone="warn" /> row
        {result.quarantined === 1 ? ' was' : 's were'} quarantined for review — the review queue
        changed even though no catalog objects did.
      </p>
      <button type="button" className="btn" onClick={() => onReviewQueue(source)}>
        Review {result.quarantined} quarantined row{result.quarantined === 1 ? '' : 's'}
      </button>
    </>
  )
  if (result.status === 'held') {
    return (
      <>
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
              {result.quarantined > 0
                ? 'No catalog changes were applied. There is no override yet.'
                : 'Nothing was applied. There is no override yet.'}{' '}
              {heldAdvice ??
                'Adjust the file so it keeps most existing objects, or split the change into smaller uploads.'}
            </p>
            {runId && <StageSummaryLine runId={runId} />}
            {quarantineHandoff}
            {runDetailsButton}
          </div>
        </div>
        {runDetailsPanel}
      </>
    )
  }
  if (result.status === 'rejected') {
    return (
      <>
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
            {runId && <StageSummaryLine runId={runId} />}
            {quarantineHandoff}
            {runDetailsButton}
          </div>
        </div>
        {runDetailsPanel}
      </>
    )
  }
  return (
    <>
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
          {/* MF-5 second line — the truthful breakdown. Rendered only when the backend sent the
              additive counts (objects_stored present), so a pre-MF-5 result stays one line. */}
          {result.objects_stored !== undefined && (
            <p className="tabular-nums">
              <Count value={result.objects_stored} tone="ok" /> objects stored (
              {result.tables ?? 0} table{result.tables === 1 ? '' : 's'} ·{' '}
              {result.columns ?? 0} column{result.columns === 1 ? '' : 's'}),{' '}
              <Count value={result.containment_edges ?? 0} tone="ok" /> containment edge
              {result.containment_edges === 1 ? '' : 's'},{' '}
              {result.join_candidates ?? 0} join candidate
              {result.join_candidates === 1 ? '' : 's'} · Pass B:{' '}
              {result.passb_proposed ?? 0} proposed, {result.passb_abstained ?? 0} abstained
            </p>
          )}
          {result.flagged && (
            <p style={{ color: 'var(--warn)' }}>
              <span style={{ fontWeight: 600 }}>Flagged:</span> {result.flagged}
            </p>
          )}
          {runId && <StageSummaryLine runId={runId} />}
          {result.quarantined > 0 && (
            <button type="button" className="btn" onClick={() => onReviewQueue(source)}>
              Review {result.quarantined} quarantined row{result.quarantined === 1 ? '' : 's'}
            </button>
          )}
          {runDetailsButton}
        </div>
      </div>
      {runDetailsPanel}
    </>
  )
}
