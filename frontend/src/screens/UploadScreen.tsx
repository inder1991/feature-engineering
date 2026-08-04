// The Ingest screen: two peer paths into the same pipeline. Path 1 uploads a schema+facts file
// (today's flow, unchanged); Path 2 pulls from a configured sync (preview -> review -> approve).
// The connection itself (URL, token, scope) is configured upstream in Integrations, so this path
// is now just a sync picker. The gates strip at the top names who holds each step of the sync
// path only — the file path's single-shot flow is untouched and needs no strip.
import { useEffect, useId, useRef, useState } from 'react'
import type { CSSProperties, DragEvent, FormEvent, KeyboardEvent } from 'react'
import { ApiError, getCatalogProfile, listCatalogs, uploadFile } from '../api'
import type { IngestResult } from '../api'
import { ConnectorPanel } from './ConnectorPanel'
import type { ConnectorStage } from './ConnectorPanel'
import { CalloutGlyph, IngestResultCallout } from './IngestResultCallout'
import { RunDetailPanel } from './RunDetailPanel'

// Mirrors the backend contract exactly (#28): uploads.py caps at 25 MiB and accepts
// .csv/.xlsx/.xlsm — the pre-flight must not refuse what the server would take.
const MAX_UPLOAD_BYTES = 25 * 1024 * 1024

// Client-side pre-flight only; the server remains the authoritative control. Both the picker
// and the drop path go through this (the `accept` attribute filters the picker dialog but does
// nothing for drops).
function describeInvalidFile(candidate: File): string {
  if (!/\.(csv|xlsx|xlsm)$/i.test(candidate.name)) {
    return `Unsupported file type: ${candidate.name}. Choose a .csv, .xlsx, or .xlsm file.`
  }
  if (candidate.size > MAX_UPLOAD_BYTES) {
    return `${candidate.name} is larger than the 25 MiB upload limit. Split it into smaller uploads.`
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
  onSemanticsQueue,
  onManageIntegrations,
}: {
  onReviewQueue: (source: string) => void
  onSemanticsQueue: (source: string) => void
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
            onSemanticsQueue={onSemanticsQueue}
            onStage={setStage}
            onManageIntegrations={onManageIntegrations}
          />
        </div>
      )}
    </section>
  )
}

// ---------------------------------------------------------------- the catalog narrative

// The catalog's OWN words, authored at the moment of upload. Structured fields, NOT a raw-JSON
// textarea: the person who has just been asked for a "source name" is the one person who knows
// what this catalog is, and asking them for hand-written JSON asked them for the one thing they
// could get syntactically wrong. Same field shapes, same wording and the same no-blocked framing
// as CatalogNarrativePanel (the post-upload editor) — one language, not two.
//
// The wire format is unchanged: the form serializes to the exact `catalog_profile_json` part the
// textarea used to carry, which `parse_narrative_payload` already accepts.

interface NarrativeDraft {
  displayName: string
  description: string
  businessContext: string
  businessDomains: string[]
}

const EMPTY_NARRATIVE: NarrativeDraft = {
  displayName: '',
  description: '',
  businessContext: '',
  businessDomains: [],
}

function trimmedDomains(draft: NarrativeDraft): string[] {
  return draft.businessDomains.map(d => d.trim()).filter(Boolean)
}

// The JSON object the multipart part carries. ONLY non-empty fields ride: `parse_narrative_payload`
// reads every key with `.get(...)` and maps a missing key and an explicit null to the same None, so
// omitting an empty field is byte-equivalent to sending it null — and it keeps the payload equal to
// what a person would have hand-written in the old textarea.
function narrativePayload(draft: NarrativeDraft): Record<string, unknown> {
  const payload: Record<string, unknown> = {}
  const displayName = draft.displayName.trim()
  if (displayName) payload.display_name = displayName
  const description = draft.description.trim()
  if (description) payload.description = description
  const businessContext = draft.businessContext.trim()
  if (businessContext) payload.business_context = businessContext
  const domains = trimmedDomains(draft)
  if (domains.length > 0) payload.business_domains = domains
  return payload
}

// ABSENCE MUST STAY ABSENCE. A present-but-empty part is NOT the same as no part: uploads.py treats
// any parsed part as authored narrative and writes a revision for it, so an all-empty `{}` would
// commit a catalog narrative made of nothing (and re-key every dataset profile under it). Every
// field empty therefore means no part at all — `undefined`, which `uploadFile` never appends.
function serializeNarrative(draft: NarrativeDraft): string | undefined {
  const payload = narrativePayload(draft)
  return Object.keys(payload).length === 0 ? undefined : JSON.stringify(payload)
}

// How long after the last keystroke in the source box the catalog is looked up. One lookup per
// name, not one per letter.
const NARRATIVE_PREFILL_DEBOUNCE_MS = 300

// The server's bounds, mirrored: MAX_* in overlay/upload/catalog_profiles.py plus the 64 KiB part
// bound in api/routes/uploads.py. Client-side PRE-FLIGHT only — the server stays the authority
// (same discipline as the 25 MiB file check above) — and it must not refuse what the server would
// take, so the numbers and the wording are copied, not paraphrased. Lengths are measured on the
// TRIMMED text because `_bounded_text` strips before it measures.
const NARRATIVE_CAPS = {
  displayName: 200,
  description: 4000,
  businessContext: 4000,
  domains: 32,
  domainLength: 200,
} as const
const MAX_PROFILE_JSON_BYTES = 64 * 1024

type NarrativeField = 'displayName' | 'description' | 'businessContext' | 'businessDomains'
type NarrativeProblems = Partial<Record<NarrativeField | 'total', string>>

// The server measures with Python's len(), which counts CODE POINTS; JS `.length` counts UTF-16
// units, so one astral character (an emoji in a business context, a rarer CJK ideograph) counts 2
// here and 1 there. Left alone, the mirror would refuse at 2 000 emoji what the server takes at
// 4 000 — a client-side bound stricter than the real one, which is the one failure mode a
// pre-flight is not allowed to have.
function charCount(text: string): number {
  return [...text.trim()].length
}

// Verbatim server wording (CatalogProfileError messages), so the inline text a person fixes is the
// text they would otherwise have met as a 400 that failed the whole upload.
function narrativeProblems(draft: NarrativeDraft): NarrativeProblems {
  const problems: NarrativeProblems = {}
  if (charCount(draft.displayName) > NARRATIVE_CAPS.displayName) {
    problems.displayName = `display_name exceeds the ${NARRATIVE_CAPS.displayName}-character bound`
  }
  if (charCount(draft.description) > NARRATIVE_CAPS.description) {
    problems.description = `description exceeds the ${NARRATIVE_CAPS.description}-character bound`
  }
  if (charCount(draft.businessContext) > NARRATIVE_CAPS.businessContext) {
    problems.businessContext =
      `business_context exceeds the ${NARRATIVE_CAPS.businessContext}-character bound`
  }
  const domains = trimmedDomains(draft)
  if (domains.length > NARRATIVE_CAPS.domains) {
    problems.businessDomains = `business_domains exceeds the ${NARRATIVE_CAPS.domains}-item bound`
  } else if (domains.some(d => charCount(d) > NARRATIVE_CAPS.domainLength)) {
    problems.businessDomains =
      `business_domains item exceeds the ${NARRATIVE_CAPS.domainLength}-character bound`
  }
  // The whole part, measured the way the route measures it: UTF-8 BYTES of the serialized JSON,
  // not characters. The per-field caps do NOT imply this one — JSON escapes a control character
  // (a field separator pasted out of a mainframe extract) to six ASCII bytes, so 10 600 in-bound
  // characters can still serialize past 64 KiB. This is the only check that sees the whole part.
  const json = serializeNarrative(draft)
  if (json !== undefined && new TextEncoder().encode(json).length > MAX_PROFILE_JSON_BYTES) {
    problems.total = 'catalog_profile_json exceeds the 64 KiB bound'
  }
  return problems
}

// One counter + one error under a field. The counter appears only once there is something to
// count: an empty optional field is not a thing to report on.
function FieldBound({
  errorId, text, max, problem,
}: { errorId: string; text: string; max: number; problem?: string }) {
  const used = charCount(text)
  return (
    <>
      {used > 0 && (
        <p className="hint tabular-nums" style={{ margin: 0 }}>
          {used} / {max}
        </p>
      )}
      {problem && <p className="field-error" id={errorId}>{problem}</p>}
    </>
  )
}

function CatalogNarrativeFields({
  source,
  value,
  onChange,
  problems,
}: {
  source: string
  value: NarrativeDraft
  onChange: (next: NarrativeDraft) => void
  problems: NarrativeProblems
}) {
  const uid = useId()
  const [domainInput, setDomainInput] = useState('')
  const [suggestions, setSuggestions] = useState<string[]>([])
  const suggestionsLoaded = useRef(false)
  // The catalog's CURRENT words, when the typed source names one that already exists. Kept beside
  // the draft (never merged into it) so "did this person change anything?" stays answerable.
  const [current, setCurrent] = useState<NarrativeDraft | null>(null)
  // Set by any edit the AUTHOR makes. A ref, not state: it must not re-run the lookup, and it is
  // read inside an async callback that would otherwise close over a stale value.
  const authored = useRef(false)

  // RE-UPLOAD PREFILL. Uploading again into a catalog someone has already described should show
  // what it says, not an empty form the author has to retype from memory. Debounced on the source
  // name, and it NEVER writes over words this person has already typed — the CatalogNarrativePanel
  // 409 discipline: a reload may refresh what the server says, never the draft only they have.
  useEffect(() => {
    const src = source.trim()
    if (!src) {
      setCurrent(null)
      return
    }
    let cancelled = false
    const timer = setTimeout(() => {
      void (async () => {
        try {
          const got = await getCatalogProfile(src)
          if (cancelled) return
          const described: NarrativeDraft = {
            displayName: got.profile?.display_name ?? '',
            description: got.profile?.description ?? '',
            businessContext: got.profile?.business_context ?? '',
            businessDomains: got.profile?.business_domains ?? [],
          }
          // An undescribed catalog has nothing to prefill — and nothing to call unchanged.
          if (serializeNarrative(described) === undefined) {
            setCurrent(null)
            return
          }
          setCurrent(described)
          if (!authored.current) onChange(described)
        } catch {
          // SILENT. The narrative routes 404 while FEATUREGEN_DATASET_PROFILES is off, and 404 for
          // a catalog this caller cannot see; neither is a problem the uploader caused or can fix,
          // and neither is a reason to put error styling on an optional section. Degrade to the
          // empty form, which is exactly what a first upload sees.
          if (!cancelled) setCurrent(null)
        }
      })()
    }, NARRATIVE_PREFILL_DEBOUNCE_MS)
    return () => {
      cancelled = true
      clearTimeout(timer)
    }
  }, [source, onChange])

  // Truthful, not decorative: the profile store keys a revision by a content hash over these exact
  // values, so re-authoring the same words resolves to the SAME revision id and no new version is
  // written. Comparing the serialized parts is comparing precisely what would be sent.
  const unchanged = current !== null && serializeNarrative(value) === serializeNarrative(current)

  // Suggested domain chips are the catalogs this caller can ALREADY see — the only vocabulary this
  // system can honestly offer (there is no domain taxonomy anywhere in it). Loaded lazily on first
  // open, so a person who never describes a catalog issues no request; a failure leaves the chips
  // empty and free-text entry untouched, because a suggestion nobody could fetch is not an error
  // the uploader caused.
  async function loadSuggestions() {
    if (suggestionsLoaded.current) return
    suggestionsLoaded.current = true
    try {
      const { catalogs } = await listCatalogs()
      // `display_name` when the catalog has been described, else the upper-cased slug. NEVER an
      // invented prose name: overlay/upload/catalogs.py has no per-catalog label to give.
      setSuggestions([
        ...new Set(catalogs.map(c => c.display_name?.trim() || c.source.toUpperCase())),
      ])
    } catch {
      setSuggestions([])
    }
  }

  function edit(next: Partial<NarrativeDraft>) {
    authored.current = true
    onChange({ ...value, ...next })
  }

  function addDomain(domain: string) {
    const text = domain.trim()
    if (!text || value.businessDomains.includes(text)) return
    edit({ businessDomains: [...value.businessDomains, text] })
  }

  // Committed on Enter, on a comma, and on blur. The blur path is not a nicety: without it, text
  // typed into the box and never Entered would be silently dropped at submit — the author would
  // watch a domain they typed fail to arrive.
  function commitDomain() {
    const text = domainInput.trim()
    setDomainInput('')
    addDomain(text)
  }

  function onDomainKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key !== 'Enter' && e.key !== ',') return
    // Enter inside a form submits it; here it means "that is one domain".
    e.preventDefault()
    commitDomain()
  }

  const offered = suggestions.filter(s => !value.businessDomains.includes(s))

  return (
    <details
      onToggle={e => {
        if (e.currentTarget.open) void loadSuggestions()
      }}
    >
      {/* RECOMMENDED, never required: closed by default, and nothing inside it can gate the
          upload button for an empty section. */}
      <summary className="hint" style={{ cursor: 'pointer' }}>
        Describe this catalog (recommended)
      </summary>
      <div style={{ display: 'grid', gap: 12, marginTop: 8, maxWidth: 620 }}>
        <p className="hint" style={{ margin: 0 }}>
          Shown as the catalog&#39;s name and description; used by search and by the AI when it
          interprets tables.
        </p>
        {unchanged && (
          <p className="hint" style={{ margin: 0 }} data-testid="narrative-unchanged">
            These are this catalog&#39;s current words, unchanged — nothing will be re-versioned.
          </p>
        )}
        <div className="field">
          {/* "Name", not "display name": same label as the post-upload editor. No label in here
              may contain the substring "file" — getByLabelText(/file/i) must keep matching only
              the file input (which is why the old textarea said "narrative", not "profile"). */}
          {/* No maxLength on any of these. A cap the browser silently enforces cannot be
              explained, cannot cover the 64 KiB whole-part bound, and would make the inline
              error below dead code — an inert mechanism. The counter and the error carry it. */}
          <label>
            Name
            <input
              value={value.displayName}
              onChange={e => edit({ displayName: e.target.value })}
              placeholder="e.g. Funds transfers"
              aria-invalid={problems.displayName ? true : undefined}
              aria-describedby={problems.displayName ? `${uid}-name-error` : undefined}
            />
          </label>
          <FieldBound errorId={`${uid}-name-error`} text={value.displayName}
                      max={NARRATIVE_CAPS.displayName} problem={problems.displayName} />
        </div>
        <div className="field">
          <label>
            Description
            <textarea
              value={value.description}
              onChange={e => edit({ description: e.target.value })}
              rows={2}
              placeholder="One or two lines: what this catalog holds."
              aria-invalid={problems.description ? true : undefined}
              aria-describedby={problems.description ? `${uid}-description-error` : undefined}
            />
          </label>
          <FieldBound errorId={`${uid}-description-error`} text={value.description}
                      max={NARRATIVE_CAPS.description} problem={problems.description} />
        </div>
        <div className="field">
          <label>
            Business context
            <textarea
              value={value.businessContext}
              onChange={e => edit({ businessContext: e.target.value })}
              rows={4}
              placeholder={
                'What system produces this, what population it covers, who owns it — e.g. '
                + '\'Funds-transfer records from the core banking system; all outbound '
                + 'SWIFT/RTGS payments; Compliance-owned\''
              }
              aria-invalid={problems.businessContext ? true : undefined}
              aria-describedby={problems.businessContext ? `${uid}-context-error` : undefined}
            />
          </label>
          <FieldBound errorId={`${uid}-context-error`} text={value.businessContext}
                      max={NARRATIVE_CAPS.businessContext} problem={problems.businessContext} />
        </div>
        <div className="field">
          <label htmlFor={`${uid}-domains`}>Business domains</label>
          {value.businessDomains.length > 0 && (
            <ul className="q-chips">
              {value.businessDomains.map(domain => (
                <li key={domain} className="q-chip">
                  <span>{domain}</span>
                  <button
                    type="button"
                    className="q-chip-x"
                    aria-label={`Remove domain ${domain}`}
                    onClick={() =>
                      edit({ businessDomains: value.businessDomains.filter(d => d !== domain) })}
                  >
                    ×
                  </button>
                </li>
              ))}
            </ul>
          )}
          <input
            id={`${uid}-domains`}
            value={domainInput}
            onChange={e => setDomainInput(e.target.value)}
            onKeyDown={onDomainKey}
            onBlur={commitDomain}
            placeholder="Type a domain, then press Enter"
            aria-invalid={problems.businessDomains ? true : undefined}
            aria-describedby={problems.businessDomains ? `${uid}-domains-error` : undefined}
          />
          {problems.businessDomains && (
            <p className="field-error" id={`${uid}-domains-error`}>{problems.businessDomains}</p>
          )}
          {offered.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 8 }}>
              <span className="micro-label">Suggested</span>
              <ul className="q-chips">
                {offered.map(s => (
                  <li key={s}>
                    <button
                      type="button"
                      className="q-chip"
                      aria-label={`Add domain ${s}`}
                      onClick={() => addDomain(s)}
                    >
                      {s}
                    </button>
                  </li>
                ))}
              </ul>
            </div>
          )}
        </div>
        {problems.total && <p className="field-error">{problems.total}</p>}
      </div>
    </details>
  )
}

// ---------------------------------------------------------------- path 1: the file flow

function FileUploadPath({ onReviewQueue }: { onReviewQueue: (source: string) => void }) {
  const [source, setSource] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [fileError, setFileError] = useState('')
  // The optional catalog narrative (Release-A profiles), authored as structured fields and
  // serialized into the multipart catalog_profile_json part; validated server-side before any
  // write and committed atomically with a successful ingest. All-empty = not sent; never required.
  //
  // NOTHING CLEARS THIS — not a failed upload, not a held or rejected one. The most likely reason
  // to see the error callout is a problem with the FILE, and the retry is one click away; wiping
  // the paragraph the author just wrote about their own catalog would charge them for a failure
  // that had nothing to do with it. Same discipline as CatalogNarrativePanel's 409 path.
  const [narrative, setNarrative] = useState<NarrativeDraft>(EMPTY_NARRATIVE)
  // The result is stored with the source it was uploaded to, so the result panel and the
  // review-queue handoff never read the live input (which the user may already have edited
  // for the next upload).
  const [uploaded, setUploaded] = useState<{ result: IngestResult; source: string } | null>(null)
  // A failed upload still opened a run record: the ApiError carries its id (from the
  // X-Ingestion-Run-Id header), so the error keeps it and the callout can open the run-detail
  // panel — a FAILED run stays inspectable (#14). runId is null when the server sent no header.
  const [error, setError] = useState<{ detail: string; runId: string | null } | null>(null)
  const [showFailedRun, setShowFailedRun] = useState(false)
  const [busy, setBusy] = useState(false)
  const [hover, setHover] = useState(false)
  const [focus, setFocus] = useState(false)
  const [dragging, setDragging] = useState(false)

  // ADJUDICATED: an over-cap narrative BLOCKS the upload. An EMPTY section never can — absence is
  // valid and rides as no part at all — but a non-empty one that breaks a bound would be posted,
  // rejected by the route BEFORE any write, and 400 the WHOLE upload: the file would not land
  // either. Letting the click through would mean spending an upload to deliver a message the
  // screen already knows how to say, next to the field that has to change. So the section shows
  // the error inline, the button is disabled, and the reason sits beside it (the section can be
  // collapsed, and a disabled button with no visible cause is its own defect).
  const problems = narrativeProblems(narrative)
  const narrativeBlocks = Object.keys(problems).length > 0

  async function submit(e: FormEvent) {
    e.preventDefault()
    const submittedSource = source.trim()
    // Guarded here as well as on the button: a form can be submitted by Enter in a text field.
    if (!file || !submittedSource || narrativeBlocks) return
    setBusy(true)
    setError(null)
    setShowFailedRun(false)
    setUploaded(null)
    try {
      setUploaded({
        result: await uploadFile(file, submittedSource, serializeNarrative(narrative)),
        source: submittedSource,
      })
    } catch (err) {
      setError(
        err instanceof ApiError
          ? { detail: err.detail, runId: err.ingestionRunId }
          : { detail: String(err), runId: null },
      )
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
              File (.csv / .xlsx / .xlsm)
              {/* No `required` here: jsdom never counts an uploaded FileList toward a required
                  file input's validity (valueMissing stays true), which blocks programmatic form
                  submission in tests. It would also fight the drop path (dropping sets React
                  state, not input.files). Empty submission is already prevented by the disabled
                  button and the `if (!file …) return` guard above. */}
              <input
                type="file"
                accept=".csv,.xlsx,.xlsm"
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
          <CatalogNarrativeFields
            source={source}
            value={narrative}
            onChange={setNarrative}
            problems={problems}
          />
          <div style={{ display: 'grid', gap: 6, justifyItems: 'start' }}>
            <button
              type="submit"
              className="btn btn--primary"
              disabled={busy || !file || !source.trim() || narrativeBlocks}
              aria-describedby={narrativeBlocks ? 'upload-narrative-blocked' : undefined}
            >
              {busy ? 'Uploading…' : 'Upload'}
            </button>
            {narrativeBlocks && (
              <p className="field-error" id="upload-narrative-blocked" style={{ margin: 0 }}>
                Shorten the catalog description above — the server refuses the upload while it is
                over the bound, and the file would not land either.
              </p>
            )}
          </div>
        </form>
      </div>
      {error && (
        <>
          <div className="callout callout--danger" role="alert">
            <CalloutGlyph>
              <circle cx="8" cy="8" r="6.25" />
              <path d="m5.75 5.75 4.5 4.5m0-4.5-4.5 4.5" />
            </CalloutGlyph>
            <div className="callout-body">
              <p>
                <strong>Upload failed.</strong> {error.detail}
              </p>
              {error.runId && (
                <button
                  type="button"
                  className="btn"
                  aria-expanded={showFailedRun}
                  onClick={() => setShowFailedRun(v => !v)}
                >
                  {showFailedRun ? 'Hide run details' : 'View run details'}
                </button>
              )}
            </div>
          </div>
          {showFailedRun && error.runId && (
            <RunDetailPanel runId={error.runId} onClose={() => setShowFailedRun(false)} />
          )}
        </>
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
