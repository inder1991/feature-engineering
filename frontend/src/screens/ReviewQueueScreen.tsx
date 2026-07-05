import { type FormEvent, useEffect, useRef, useState } from 'react'
import { ApiError, type QuarantineItem, listQuarantine } from '../api'

// --- Reason parsing -------------------------------------------------------------------------
// Mirrors the exact backend quarantine messages from
// src/featuregen/overlay/upload/canonical.py. Every inline fix below is a CLIENT-SIDE MOCK: it
// re-runs these same rules in the browser and marks rows resolved locally. Nothing is persisted;
// the durable fix is still correcting the source file and re-uploading.

type Classification =
  | { kind: 'missing'; fields: string[] }
  | { kind: 'unrecognized'; fields: ['sensitivity']; badValue: string }
  | { kind: 'conflict'; fields: ['type']; kept: string; incoming: string }
  | { kind: 'mismatch'; fields: ['source']; expected: string }
  | { kind: 'other'; fields: [] }

function classify(reason: string): Classification {
  let m = reason.match(/^missing required field\(s\): (.+)$/)
  if (m) return { kind: 'missing', fields: m[1].split(',').map(s => s.trim()).filter(Boolean) }
  m = reason.match(/^unrecognized sensitivity '(.*)' \(expected one of: /)
  if (m) return { kind: 'unrecognized', fields: ['sensitivity'], badValue: m[1] }
  m = reason.match(/^conflicting type for .+?: (.+) vs (.+)$/)
  if (m) return { kind: 'conflict', fields: ['type'], kept: m[1].trim(), incoming: m[2].trim() }
  m = reason.match(/^row source '(.*)' does not match upload source '(.*)'$/)
  if (m) return { kind: 'mismatch', fields: ['source'], expected: m[2] }
  return { kind: 'other', fields: [] }
}

const REQUIRED = ['source', 'table', 'column', 'type'] as const

function blank(v: unknown): boolean {
  return v === '' || v === null || v === undefined || v === false
}

// Raw row flattened to strings, with the reviewer's inline edits overlaid, for revalidation.
function mergedRecord(item: QuarantineItem, edits: Record<string, string>): Record<string, string> {
  const base: Record<string, string> = {}
  for (const [k, v] of Object.entries(item.raw)) base[k] = blank(v) ? '' : String(v)
  return { ...base, ...edits }
}

// Client-side mirror of the backend validation. Returns '' on pass, or the failing check.
// uploadSource is the loaded queue's source name: the backend rejects any row whose source
// differs from it, whatever the row was quarantined for.
function validate(
  merged: Record<string, string>,
  cls: Classification,
  uploadSource: string,
): string {
  for (const f of REQUIRED) {
    if (!merged[f]?.trim()) return `${f} is required. Fill it in before revalidating.`
  }
  const sens = (merged.sensitivity ?? '').trim()
  if (sens !== '' && sens !== 'pii' && sens !== 'restricted') {
    return `sensitivity must be blank, pii, or restricted. '${sens}' is not recognized.`
  }
  if (uploadSource && merged.source?.trim() !== uploadSource) {
    return `source must equal the upload source '${uploadSource}'.`
  }
  if (cls.kind === 'mismatch' && merged.source?.trim() !== cls.expected) {
    return `source must equal the upload source '${cls.expected}'.`
  }
  if (cls.kind === 'conflict' && merged.type?.trim() !== cls.kept) {
    return (
      `type must match the first-seen type '${cls.kept}' (first upload wins). ` +
      `Keep '${cls.kept}', or this row stays quarantined until the source file is fixed ` +
      'and re-uploaded.'
    )
  }
  return ''
}

// Same rule, applied to a single sensitivity replacement value (the mapping-rule form).
function validateSensitivity(value: string): string {
  const v = value.trim()
  if (v !== '' && v !== 'pii' && v !== 'restricted') {
    return `Replacement must be blank, pii, or restricted. '${v}' is not recognized.`
  }
  return ''
}

interface Resolution {
  via: 'revalidate' | 'dismiss' | 'rule'
  note: string
  ruleId?: string
}

interface Rule {
  id: string // groupKey; unique while active (an applied group has no pending rows left)
  badValue: string
  replacement: string
  rowIndexes: number[]
}

const REVALIDATED_NOTE =
  'Revalidated locally. Not persisted: fix the source file and re-upload to clear it for real.'
const DISMISSED_NOTE = 'Dismissed locally. Retained in the queue on the server.'
const HONESTY_NOTE =
  'Inline fixes are a preview. Nothing is persisted yet; the durable fix is still correcting ' +
  'the file and re-uploading. Persistence endpoints are a tracked follow-up.'

function InfoGlyph() {
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
        <circle cx="8" cy="8" r="6.25" />
        <path d="M8 7.25v3.5M8 5.25v.01" />
      </svg>
    </span>
  )
}

export function ReviewQueueScreen({ initialSource }: { initialSource: string }) {
  const [source, setSource] = useState(initialSource)
  const [items, setItems] = useState<QuarantineItem[] | null>(null)
  // Source name of the currently loaded queue: revalidation checks rows against it.
  const [loadedSource, setLoadedSource] = useState('')
  const [error, setError] = useState('')

  // Monotonic id per load() call: a late response from an older load must never overwrite
  // newer queue data (or wipe the reviewer's session resolutions against the wrong item set).
  const loadSeq = useRef(0)
  // Element id to focus after the next render. Resolution actions unmount the focused button;
  // without an explicit move, keyboard focus falls back to <body>.
  const focusTarget = useRef<string | null>(null)

  // Local, session-only mock state. Cleared on every (re)load — reloading resets resolutions, which
  // is correct: the server never saw them.
  const [resolved, setResolved] = useState<Map<number, Resolution>>(new Map())
  const [editing, setEditing] = useState<number | null>(null)
  const [edits, setEdits] = useState<Record<string, string>>({})
  const [editError, setEditError] = useState('')
  const [rules, setRules] = useState<Rule[]>([])
  const [ruleDrafts, setRuleDrafts] = useState<Record<string, string>>({})
  const [ruleErrors, setRuleErrors] = useState<Record<string, string>>({})

  function resetLocal() {
    setResolved(new Map())
    setEditing(null)
    setEdits({})
    setEditError('')
    setRules([])
    setRuleDrafts({})
    setRuleErrors({})
  }

  async function load(name: string) {
    if (!name.trim()) return
    const id = ++loadSeq.current
    setError('')
    try {
      const next = await listQuarantine(name.trim())
      if (id !== loadSeq.current) return
      setItems(next)
      setLoadedSource(name.trim())
      resetLocal()
    } catch (err) {
      if (id !== loadSeq.current) return
      setItems(null)
      setLoadedSource('')
      resetLocal()
      setError(err instanceof ApiError ? err.detail : String(err))
    }
  }

  useEffect(() => {
    // Arriving via the upload screen's "review quarantined rows" handoff, or a param-only hash
    // change while mounted: sync the input with the new source and load immediately.
    setSource(initialSource)
    if (initialSource.trim()) void load(initialSource)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSource])

  useEffect(() => {
    if (!focusTarget.current) return
    const el = document.getElementById(focusTarget.current)
    focusTarget.current = null
    el?.focus()
  })

  function submit(e: FormEvent) {
    e.preventDefault()
    void load(source)
  }

  function openEditor(item: QuarantineItem, cls: Classification) {
    const init: Record<string, string> = {}
    for (const f of cls.fields) init[f] = blank(item.raw[f]) ? '' : String(item.raw[f])
    setEdits(init)
    setEditError('')
    setEditing(item.row_index)
  }

  function closeEditor() {
    setEditing(null)
    setEdits({})
    setEditError('')
  }

  function resolve(rowIndex: number, res: Resolution) {
    setResolved(prev => new Map(prev).set(rowIndex, res))
  }

  function revalidate(item: QuarantineItem, cls: Classification) {
    const err = validate(mergedRecord(item, edits), cls, loadedSource)
    if (err) {
      setEditError(err)
      return
    }
    resolve(item.row_index, { via: 'revalidate', note: REVALIDATED_NOTE })
    closeEditor()
    focusTarget.current = `q-resolved-${item.row_index}`
  }

  function keepFirstSeen(item: QuarantineItem, cls: Extract<Classification, { kind: 'conflict' }>) {
    // Backend is first-seen-wins: keeping value A is exactly what a clean re-upload would resolve to.
    resolve(item.row_index, {
      via: 'revalidate',
      note: `Kept the first-seen type '${cls.kept}' locally. ${REVALIDATED_NOTE}`,
    })
    closeEditor()
    focusTarget.current = `q-resolved-${item.row_index}`
  }

  function dismiss(item: QuarantineItem) {
    resolve(item.row_index, { via: 'dismiss', note: DISMISSED_NOTE })
    if (editing === item.row_index) closeEditor()
    focusTarget.current = `q-resolved-${item.row_index}`
  }

  function applyRule(key: string, badValue: string, rows: QuarantineItem[]) {
    const replacement = (ruleDrafts[key] ?? '').trim()
    const err = validateSensitivity(replacement)
    if (err) {
      setRuleErrors(prev => ({ ...prev, [key]: err }))
      return
    }
    const rowIndexes = rows.map(r => r.row_index)
    // The rule resolves these rows; an editor left open on one of them must not survive as
    // stale state (it would pop back open, draft intact, if the rule is later removed).
    if (editing !== null && rowIndexes.includes(editing)) closeEditor()
    const shown = replacement || 'blank'
    setRules(prev => [...prev, { id: key, badValue, replacement, rowIndexes }])
    setResolved(prev => {
      const next = new Map(prev)
      for (const idx of rowIndexes) {
        next.set(idx, {
          via: 'rule',
          ruleId: key,
          note: `Resolved by a local mapping rule ('${badValue}' to '${shown}'). Not persisted: fix the source file and re-upload.`,
        })
      }
      return next
    })
    setRuleErrors(prev => {
      const n = { ...prev }
      delete n[key]
      return n
    })
    setRuleDrafts(prev => {
      const n = { ...prev }
      delete n[key]
      return n
    })
    focusTarget.current = 'q-rules-strip'
  }

  function removeRule(rule: Rule) {
    // Defensive: never let an editor reappear on a row this rule covered (see applyRule).
    if (editing !== null && rule.rowIndexes.includes(editing)) closeEditor()
    setResolved(prev => {
      const next = new Map(prev)
      for (const [idx, res] of prev) if (res.ruleId === rule.id) next.delete(idx)
      return next
    })
    setRules(prev => prev.filter(r => r.id !== rule.id))
    focusTarget.current = 'q-count'
  }

  return (
    <section>
      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="review-source">Source</label>
          <input
            id="review-source"
            value={source}
            onChange={e => setSource(e.target.value)}
            placeholder="source name"
          />
        </div>
        <button type="submit" className="btn">
          Load queue
        </button>
      </form>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {items?.length === 0 && (
        <p className="empty" role="status">
          Queue clear. No quarantined rows for this source.
        </p>
      )}
      {items && items.length > 0 && (
        <QueueBody
          items={items}
          resolved={resolved}
          editing={editing}
          edits={edits}
          setEdits={setEdits}
          editError={editError}
          rules={rules}
          ruleDrafts={ruleDrafts}
          setRuleDrafts={setRuleDrafts}
          ruleErrors={ruleErrors}
          onFixInline={openEditor}
          onRevalidate={revalidate}
          onKeepFirstSeen={keepFirstSeen}
          onCancelEdit={closeEditor}
          onDismiss={dismiss}
          onApplyRule={applyRule}
          onRemoveRule={removeRule}
        />
      )}
    </section>
  )
}

interface QueueBodyProps {
  items: QuarantineItem[]
  resolved: Map<number, Resolution>
  editing: number | null
  edits: Record<string, string>
  setEdits: (fn: (v: Record<string, string>) => Record<string, string>) => void
  editError: string
  rules: Rule[]
  ruleDrafts: Record<string, string>
  setRuleDrafts: (fn: (v: Record<string, string>) => Record<string, string>) => void
  ruleErrors: Record<string, string>
  onFixInline: (item: QuarantineItem, cls: Classification) => void
  onRevalidate: (item: QuarantineItem, cls: Classification) => void
  onKeepFirstSeen: (item: QuarantineItem, cls: Extract<Classification, { kind: 'conflict' }>) => void
  onCancelEdit: () => void
  onDismiss: (item: QuarantineItem) => void
  onApplyRule: (key: string, badValue: string, rows: QuarantineItem[]) => void
  onRemoveRule: (rule: Rule) => void
}

function QueueBody(props: QueueBodyProps) {
  const {
    items,
    resolved,
    editing,
    edits,
    setEdits,
    editError,
    rules,
    ruleDrafts,
    setRuleDrafts,
    ruleErrors,
    onFixInline,
    onRevalidate,
    onKeepFirstSeen,
    onCancelEdit,
    onDismiss,
    onApplyRule,
    onRemoveRule,
  } = props

  // Group still-pending "unrecognized" rows by bad value; a group of 2+ earns one mapping-rule
  // callout. Resolved rows (including rows a rule already fixed) drop out of their group.
  const groups = new Map<string, { badValue: string; rows: QuarantineItem[] }>()
  for (const item of items) {
    if (resolved.has(item.row_index)) continue
    const cls = classify(item.reason)
    if (cls.kind !== 'unrecognized') continue
    const key = `sensitivity ${cls.badValue}`
    const g = groups.get(key) ?? { badValue: cls.badValue, rows: [] }
    g.rows.push(item)
    groups.set(key, g)
  }
  const ruleGroups = [...groups.entries()].filter(([, g]) => g.rows.length >= 2)

  return (
    <>
      {resolved.size > 0 && (
        <div className="callout" role="status">
          <InfoGlyph />
          <div className="callout-body">
            <p>
              <strong>Preview.</strong> {HONESTY_NOTE}
            </p>
          </div>
        </div>
      )}

      <p className="tabular-nums" role="status" id="q-count" tabIndex={-1}>
        {items.length} quarantined · {resolved.size} resolved this session (mock)
      </p>
      <p className="hint">
        Correct rows in the source file and re-upload; a clean upload clears this queue for real.
      </p>

      {rules.length > 0 && (
        <div className="q-rules" id="q-rules-strip" tabIndex={-1}>
          <span className="micro-label">Mapping rules (mock)</span>
          <ul className="q-chips">
            {rules.map(rule => (
              <li key={rule.id} className="q-chip">
                <span className="mono">
                  {rule.badValue} → {rule.replacement || 'blank'} · {rule.rowIndexes.length} rows
                </span>
                <button
                  type="button"
                  className="q-chip-x"
                  aria-label={`Remove mapping rule ${rule.badValue} to ${rule.replacement || 'blank'}`}
                  onClick={() => onRemoveRule(rule)}
                >
                  ×
                </button>
              </li>
            ))}
          </ul>
        </div>
      )}

      {ruleGroups.map(([key, g]) => (
        <div className="callout callout--accent" key={key}>
          <InfoGlyph />
          <div className="callout-body">
            <p>
              <strong>Repeated value.</strong>{' '}
              {`'${g.badValue}' appears in ${g.rows.length} rows. Map it once to resolve them together.`}
            </p>
            <div className="q-rule-form">
              <input
                className="mono"
                aria-label={`Replacement value for ${g.badValue}`}
                placeholder="pii, restricted, or blank"
                value={ruleDrafts[key] ?? ''}
                onChange={e => setRuleDrafts(v => ({ ...v, [key]: e.target.value }))}
              />
              <button
                type="button"
                className="btn btn--primary"
                onClick={() => onApplyRule(key, g.badValue, g.rows)}
              >
                Add mapping rule
              </button>
            </div>
            {ruleErrors[key] && (
              <p className="field-error" role="alert">
                {ruleErrors[key]}
              </p>
            )}
          </div>
        </div>
      ))}

      <ul className="rows">
        {items.map(item => {
          const res = resolved.get(item.row_index)
          if (res) {
            return (
              <li
                className="row q-item q-item--resolved"
                key={item.row_index}
                id={`q-resolved-${item.row_index}`}
                tabIndex={-1}
              >
                <div className="q-head">
                  <span className="badge rejected">row {item.row_index}</span>
                  <span className="badge resolved">resolved · mock</span>
                  <span className="q-reason q-reason--muted">{item.reason}</span>
                </div>
                <p className="q-note">{res.note}</p>
              </li>
            )
          }

          const cls = classify(item.reason)
          const offenders = new Set(cls.fields)
          const cells: { k: string; v: unknown; off: boolean }[] = []
          const seen = new Set<string>()
          for (const [k, v] of Object.entries(item.raw)) {
            const off = offenders.has(k)
            if (off || !blank(v)) {
              cells.push({ k, v, off })
              seen.add(k)
            }
          }
          for (const f of offenders) if (!seen.has(f)) cells.push({ k: f, v: undefined, off: true })

          return (
            <li className="row q-item" key={item.row_index}>
              <div className="q-head">
                <span className="badge rejected">row {item.row_index}</span>
                <strong className="q-reason">{item.reason}</strong>
                <div className="q-actions">
                  {cls.kind !== 'other' && (
                    <button type="button" className="btn" onClick={() => onFixInline(item, cls)}>
                      Fix inline
                    </button>
                  )}
                  <button type="button" className="btn q-ghost" onClick={() => onDismiss(item)}>
                    Dismiss
                  </button>
                </div>
              </div>

              <dl className="kv">
                {cells.map(({ k, v, off }) => (
                  <div key={k}>
                    <dt className={off ? 'mono q-off' : 'mono'}>{k}</dt>
                    <dd className={off ? 'q-off' : undefined}>
                      {blank(v) ? <em className="q-blank">blank</em> : String(v)}
                    </dd>
                  </div>
                ))}
              </dl>

              {cls.kind === 'other' && (
                <p className="hint">
                  No inline fix for this reason. Correct the value in the source file and re-upload.
                </p>
              )}

              {editing === item.row_index && cls.kind !== 'other' && (
                <div className="q-editor">
                  {cls.fields.map(f => (
                    <label key={f} className="q-editor-field">
                      <span className="micro-label">{f}</span>
                      <input
                        className="mono"
                        aria-label={f}
                        value={edits[f] ?? ''}
                        onChange={e => setEdits(v => ({ ...v, [f]: e.target.value }))}
                      />
                    </label>
                  ))}
                  <div className="q-editor-actions">
                    {cls.kind === 'conflict' && (
                      <button
                        type="button"
                        className="btn btn--primary"
                        onClick={() => onKeepFirstSeen(item, cls)}
                      >
                        Keep {cls.kept}
                      </button>
                    )}
                    <button
                      type="button"
                      className="btn btn--primary"
                      onClick={() => onRevalidate(item, cls)}
                    >
                      Revalidate
                    </button>
                    <button type="button" className="btn" onClick={onCancelEdit}>
                      Cancel
                    </button>
                  </div>
                  {editError && (
                    <p className="field-error" role="alert">
                      {editError}
                    </p>
                  )}
                </div>
              )}
            </li>
          )
        })}
      </ul>
    </>
  )
}
