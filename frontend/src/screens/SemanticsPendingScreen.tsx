// The semantics-pending queue (#22): columns that landed structurally vouched but semantically
// blank — the OpenMetadata connector does this BY DESIGN (structure is vouched, semantics await
// a human owner). This screen lists a source's pending columns grouped by table and lets a data
// owner complete them: additivity, unit, currency, entity, and the is_as_of flag.
//
// The backend is authoritative: values are validated against the same closed vocabularies an
// upload declares under (422 on a bad value), a second as-of axis for a table is refused (409),
// and grain/availability facts stay governed (Pass B) — they are not reachable from here. A
// completed column leaves the queue because it no longer matches the pending predicate; there
// is no separate review record to close.
import { type FormEvent, useEffect, useRef, useState } from 'react'
import { ApiError, completeSemantics, getSemanticsPending } from '../api'
import type { SemanticsPendingItem, SemanticsValues } from '../api'

// Mirrors the backend's closed vocabulary (canonical._VALID_ADDITIVITY); the server re-validates.
const ADDITIVITY_OPTIONS = ['additive', 'semi_additive', 'non_additive'] as const

interface Draft {
  additivity: string
  unit: string
  currency: string
  entity: string
  is_as_of: boolean
}

const EMPTY_DRAFT: Draft = { additivity: '', unit: '', currency: '', entity: '', is_as_of: false }

// Only the fields the owner actually set ride the wire: completion SETS values, never clears
// them, so a blank control must not send an empty string (the backend 422s on present-but-blank).
function draftValues(draft: Draft): SemanticsValues {
  const values: SemanticsValues = {}
  if (draft.additivity) values.additivity = draft.additivity
  if (draft.unit.trim()) values.unit = draft.unit.trim()
  if (draft.currency.trim()) values.currency = draft.currency.trim()
  if (draft.entity.trim()) values.entity = draft.entity.trim()
  if (draft.is_as_of) values.is_as_of = true
  return values
}

export function SemanticsPendingScreen({ initialSource }: { initialSource: string }) {
  const [source, setSource] = useState(initialSource)
  const [items, setItems] = useState<SemanticsPendingItem[] | null>(null)
  // Source name of the currently loaded queue: completions post against it, never the live input.
  const [loadedSource, setLoadedSource] = useState('')
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState('')

  // Monotonic id per load() call: a late response from an older load must never overwrite newer
  // queue data, and an in-flight save against a reloaded queue must drop its result.
  const loadSeq = useRef(0)

  // Per-row form drafts + errors, keyed by object_ref. Cleared on (re)load: the durable state
  // lives on the server (a completed column is simply gone from the next fetch).
  const [drafts, setDrafts] = useState<Record<string, Draft>>({})
  const [rowErrors, setRowErrors] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState<Record<string, boolean>>({})
  const [completedCount, setCompletedCount] = useState(0)

  async function load(name: string) {
    if (!name.trim()) return
    const id = ++loadSeq.current
    setError('')
    setLoading(true)
    try {
      const next = await getSemanticsPending(name.trim())
      if (id !== loadSeq.current) return
      setItems(next)
      setLoadedSource(name.trim())
      setDrafts({})
      setRowErrors({})
      setSaving({})
      setCompletedCount(0)
    } catch (err) {
      if (id !== loadSeq.current) return
      setItems(null)
      setLoadedSource('')
      setError(err instanceof ApiError ? err.detail : String(err))
    } finally {
      if (id === loadSeq.current) setLoading(false)
    }
  }

  useEffect(() => {
    // Arriving via the connector's "complete semantics" handoff (?source= in the hash), or a
    // param-only hash change while mounted: sync the input and load immediately.
    setSource(initialSource)
    if (initialSource.trim()) void load(initialSource)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSource])

  function submit(e: FormEvent) {
    e.preventDefault()
    void load(source)
  }

  function setDraft(ref: string, patch: Partial<Draft>) {
    setDrafts(prev => ({ ...prev, [ref]: { ...(prev[ref] ?? EMPTY_DRAFT), ...patch } }))
  }

  async function save(item: SemanticsPendingItem) {
    const values = draftValues(drafts[item.object_ref] ?? EMPTY_DRAFT)
    if (Object.keys(values).length === 0) return // Save is disabled empty; belt and braces
    const seq = loadSeq.current
    setSaving(prev => ({ ...prev, [item.object_ref]: true }))
    setRowErrors(prev => ({ ...prev, [item.object_ref]: '' }))
    try {
      await completeSemantics(loadedSource, item.object_ref, values)
      if (seq !== loadSeq.current) return // the queue reloaded mid-flight — drop this stale result
      // The column no longer matches the pending predicate: remove the row.
      setItems(prev => (prev ? prev.filter(i => i.object_ref !== item.object_ref) : prev))
      setCompletedCount(n => n + 1)
    } catch (err) {
      if (seq !== loadSeq.current) return
      // 422 (bad value), 409 (the table already has an as-of column), 404 (unknown ref) all
      // arrive as the backend's own sentence — rendered inline on the row, never hidden.
      setRowErrors(prev => ({
        ...prev,
        [item.object_ref]: err instanceof ApiError ? err.detail : String(err),
      }))
    } finally {
      if (seq === loadSeq.current) {
        setSaving(prev => ({ ...prev, [item.object_ref]: false }))
      }
    }
  }

  // Grouped by table, in the order the (object_ref-sorted) queue arrives.
  const groups: { table: string; rows: SemanticsPendingItem[] }[] = []
  for (const item of items ?? []) {
    const last = groups[groups.length - 1]
    if (last && last.table === item.table) last.rows.push(item)
    else groups.push({ table: item.table, rows: [item] })
  }

  return (
    <section>
      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="semantics-source">Source</label>
          <input
            id="semantics-source"
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
      {loading && (
        <p className="hint" role="status">
          Loading the semantics queue…
        </p>
      )}
      {items?.length === 0 && !loading && (
        <p className="empty" role="status">
          No columns need semantics — all set.
        </p>
      )}
      {items && items.length > 0 && !loading && (
        <>
          <p className="tabular-nums" role="status">
            {items.length} column{items.length === 1 ? '' : 's'} pending in{' '}
            <span className="mono">{loadedSource}</span>
            {completedCount > 0 && ` · ${completedCount} completed this session`}
          </p>
          <p className="hint">
            Set what you know — any one value completes the column. Values are validated on the
            server; grain and availability facts stay governed and are confirmed under
            Governance, not here.
          </p>
          {groups.map(g => (
            <div key={g.table}>
              <h2 className="mono">{g.table}</h2>
              <ul className="rows">
                {g.rows.map(item => (
                  <SemanticsRow
                    key={item.object_ref}
                    item={item}
                    draft={drafts[item.object_ref] ?? EMPTY_DRAFT}
                    error={rowErrors[item.object_ref] ?? ''}
                    saving={saving[item.object_ref] ?? false}
                    onChange={patch => setDraft(item.object_ref, patch)}
                    onSave={() => void save(item)}
                  />
                ))}
              </ul>
            </div>
          ))}
        </>
      )}
    </section>
  )
}

function SemanticsRow({
  item,
  draft,
  error,
  saving,
  onChange,
  onSave,
}: {
  item: SemanticsPendingItem
  draft: Draft
  error: string
  saving: boolean
  onChange: (patch: Partial<Draft>) => void
  onSave: () => void
}) {
  const empty = Object.keys(draftValues(draft)).length === 0
  return (
    <li className="row q-item">
      <div className="q-head">
        <span className="mono" style={{ fontWeight: 600 }}>
          {item.column}
        </span>
        <span className="hint mono">{item.object_ref}</span>
        {item.data_type && <span className="badge">{item.data_type}</span>}
        <span className="hint">missing: {item.missing.join(', ')}</span>
      </div>
      <div className="q-editor">
        <label className="q-editor-field">
          <span className="micro-label">additivity</span>
          <select
            value={draft.additivity}
            disabled={saving}
            onChange={e => onChange({ additivity: e.target.value })}
          >
            <option value="">not set</option>
            {ADDITIVITY_OPTIONS.map(v => (
              <option key={v} value={v}>
                {v}
              </option>
            ))}
          </select>
        </label>
        <label className="q-editor-field">
          <span className="micro-label">unit</span>
          <input
            className="mono"
            value={draft.unit}
            disabled={saving}
            placeholder="e.g. GBP, count"
            onChange={e => onChange({ unit: e.target.value })}
          />
        </label>
        <label className="q-editor-field">
          <span className="micro-label">currency</span>
          <input
            className="mono"
            value={draft.currency}
            disabled={saving}
            placeholder="e.g. GBP"
            onChange={e => onChange({ currency: e.target.value })}
          />
        </label>
        <label className="q-editor-field">
          <span className="micro-label">entity</span>
          <input
            className="mono"
            value={draft.entity}
            disabled={saving}
            placeholder="e.g. customer"
            onChange={e => onChange({ entity: e.target.value })}
          />
        </label>
        <label className="q-editor-field">
          <span className="micro-label">as-of column</span>
          <input
            type="checkbox"
            checked={draft.is_as_of}
            disabled={saving}
            onChange={e => onChange({ is_as_of: e.target.checked })}
          />
        </label>
        <div className="q-editor-actions">
          <button
            type="button"
            className="btn btn--primary"
            disabled={saving || empty}
            onClick={onSave}
          >
            {saving ? 'Saving…' : 'Save'}
          </button>
        </div>
        {error && (
          <p className="field-error" role="alert">
            {error}
          </p>
        )}
      </div>
    </li>
  )
}
