// The Ingest screen: two peer paths into the same pipeline. Path 1 uploads a schema+facts file
// (today's flow, unchanged); Path 2 pulls from a configured sync (preview -> review -> approve).
// The connection itself (URL, token, scope) is configured upstream in Integrations, so this path
// is now just a sync picker. The gates strip at the top names who holds each step of the sync
// path only — the file path's single-shot flow is untouched and needs no strip.
import { useState } from 'react'
import type { CSSProperties, DragEvent, FormEvent } from 'react'
import { ApiError, uploadFile } from '../api'
import type { IngestResult } from '../api'
import { ConnectorPanel } from './ConnectorPanel'
import type { ConnectorStage } from './ConnectorPanel'
import { CalloutGlyph, IngestResultCallout } from './IngestResultCallout'

const MAX_UPLOAD_BYTES = 20 * 1024 * 1024

// Client-side pre-flight only; the server remains the authoritative control. Both the picker
// and the drop path go through this (the `accept` attribute filters the picker dialog but does
// nothing for drops).
function describeInvalidFile(candidate: File): string {
  if (!/\.(csv|xlsx)$/i.test(candidate.name)) {
    return `Unsupported file type: ${candidate.name}. Choose a .csv or .xlsx file.`
  }
  if (candidate.size > MAX_UPLOAD_BYTES) {
    return `${candidate.name} is larger than the 20 MB upload limit. Split it into smaller uploads.`
  }
  return ''
}

// ---------------------------------------------------------------- gates strip (connector path)

type GateState = 'done' | 'active' | 'todo'

// Text form of each gate state for assistive tech: the visual encoding (check glyph, wash,
// dimming) never works alone. Same vocabulary as the Workbench strip.
const GATE_STATE_WORDS: Record<GateState, string> = {
  done: 'done',
  active: 'current step',
  todo: 'upcoming',
}

// Four gates. Configuration moved upstream to Integrations, so the first gate is now "Pick a
// sync" (not "Configure the connection"): the human chooses which configured sync to pull from.
const GATES: { who: 'You' | 'Connector'; title: string; sub: string }[] = [
  {
    who: 'You',
    title: 'Pick a sync',
    sub: 'Which configured sync to pull from.',
  },
  { who: 'Connector', title: 'Preview the import', sub: 'A dry run: nothing enters the catalog.' },
  { who: 'You', title: 'Review mappings', sub: 'Tags, diffs, quarantine, pending semantics.' },
  { who: 'You', title: 'You approve', sub: 'One transaction, under your name.' },
]

const STAGE_INDEX: Record<ConnectorStage, number> = {
  configure: 0,
  preview: 1,
  review: 2,
  approve: 3,
  done: 4,
}

function GatesStrip({ stage }: { stage: ConnectorStage }) {
  const active = STAGE_INDEX[stage]
  return (
    <div className="gates" role="list" aria-label="The connector path, step by step">
      {GATES.map((g, i) => {
        const state: GateState = i < active ? 'done' : i === active ? 'active' : 'todo'
        return (
          <div
            key={g.title}
            className="gate"
            role="listitem"
            data-state={state}
            aria-current={state === 'active' ? 'step' : undefined}
          >
            <span className={g.who === 'You' ? 'gate-who you' : 'gate-who engine'}>{g.who}</span>
            <div className="gate-title">
              {g.title}
              {state === 'done' && (
                <span className="gate-check" aria-hidden="true">
                  ✓
                </span>
              )}
            </div>
            <div className="gate-sub">{g.sub}</div>
            <span className="visually-hidden">{GATE_STATE_WORDS[state]}</span>
          </div>
        )
      })}
    </div>
  )
}

// ---------------------------------------------------------------- the Ingest screen

export function UploadScreen({
  onReviewQueue,
  onManageIntegrations,
}: {
  onReviewQueue: (source: string) => void
  onManageIntegrations: () => void
}) {
  const [path, setPath] = useState<'file' | 'connector'>('file')
  // Mounted on first visit and kept mounted (hidden) afterwards, so toggling paths never
  // destroys an in-flight preview. Lazy so the file-only flow issues no connector requests.
  const [connectorMounted, setConnectorMounted] = useState(false)
  const [stage, setStage] = useState<ConnectorStage>('configure')

  return (
    <section>
      <GatesStrip stage={stage} />
      <div className="paths">
        <button
          type="button"
          className="path path-file"
          aria-pressed={path === 'file'}
          onClick={() => setPath('file')}
        >
          <span className="k">Path 1 · A file</span>
          <span className="t">Upload a schema and facts file</span>
          <span className="d">CSV or Excel, declared by the owner. Today&#39;s flow, unchanged.</span>
        </button>
        <button
          type="button"
          className="path path-conn"
          aria-pressed={path === 'connector'}
          onClick={() => {
            setPath('connector')
            setConnectorMounted(true)
          }}
        >
          <span className="k">Path 2 · A sync</span>
          <span className="t">Pull from a metadata service</span>
          <span className="d">
            Preview and approve an import from a sync you configured under an integration.
          </span>
        </button>
      </div>
      <div hidden={path !== 'file'}>
        <FileUploadPath onReviewQueue={onReviewQueue} />
      </div>
      {connectorMounted && (
        <div hidden={path !== 'connector'}>
          <ConnectorPanel
            onReviewQueue={onReviewQueue}
            onStage={setStage}
            onManageIntegrations={onManageIntegrations}
          />
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------- path 1: the file flow

function FileUploadPath({ onReviewQueue }: { onReviewQueue: (source: string) => void }) {
  const [source, setSource] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [fileError, setFileError] = useState('')
  // The result is stored with the source it was uploaded to, so the result panel and the
  // review-queue handoff never read the live input (which the user may already have edited
  // for the next upload).
  const [uploaded, setUploaded] = useState<{ result: IngestResult; source: string } | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)
  const [hover, setHover] = useState(false)
  const [focus, setFocus] = useState(false)
  const [dragging, setDragging] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    const submittedSource = source.trim()
    if (!file || !submittedSource) return
    setBusy(true)
    setError('')
    setUploaded(null)
    try {
      setUploaded({ result: await uploadFile(file, submittedSource), source: submittedSource })
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err))
    } finally {
      setBusy(false)
    }
  }

  function selectFile(candidate: File) {
    const problem = describeInvalidFile(candidate)
    if (problem) {
      setFile(null)
      setFileError(problem)
      return
    }
    setFileError('')
    setFile(candidate)
  }

  function onDrop(e: DragEvent<HTMLLabelElement>) {
    e.preventDefault()
    setDragging(false)
    const dropped = e.dataTransfer.files?.[0]
    if (dropped) selectFile(dropped)
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
    <>
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
                onChange={e => {
                  const chosen = e.target.files?.[0]
                  if (chosen) selectFile(chosen)
                  else setFile(null)
                }}
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
            {fileError && (
              <div className="callout callout--danger" role="alert" style={{ marginBlock: 0 }}>
                <CalloutGlyph>
                  <circle cx="8" cy="8" r="6.25" />
                  <path d="m5.75 5.75 4.5 4.5m0-4.5-4.5 4.5" />
                </CalloutGlyph>
                <div className="callout-body">
                  <p>
                    <strong>File not accepted.</strong> {fileError}
                  </p>
                </div>
              </div>
            )}
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
      {uploaded && (
        <IngestResultCallout
          result={uploaded.result}
          source={uploaded.source}
          onReviewQueue={onReviewQueue}
        />
      )}
    </>
  )
}
