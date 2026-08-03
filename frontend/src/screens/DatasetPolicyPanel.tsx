// Release-B dataset policies: how this dataset's rows are chosen, and which dataset serves a need.
//
// Minimal on purpose — the same reasoning as CatalogNarrativePanel: a store with no authoring
// surface is dead code, and this is the smallest thing that makes the two policy stores reachable
// and honest.
//
// Four rules it follows:
//   * CAS IN THE BODY. `pointer_version` is echoed back exactly as it was read (0 claims the first
//     write). A 409 means someone else declared first — and THE AUTHOR'S DRAFT SURVIVES: the reload
//     refreshes the CAS anchor and the comparison copy, never the form.
//   * NO-BLOCKED FRAMING. A dataset with no policy is not an error and not a gap — it is a dataset
//     nobody has had to declare anything about yet. An AMBIGUOUS policy says so plainly instead of
//     looking broken, because "two datasets are equally preferred" is a decision waiting to be made.
//   * NOTHING IS INFERRED. The serving policy is keyed by (entity, need, purpose) and this screen
//     is about a DATASET, so the operator names the need. Guessing it from `primary_entity` is
//     exactly the inference the population rule forbids.
//   * The routes 404 while the flag (or its DATASET_PROFILES dependency) is off, and the panel then
//     renders NOTHING — a flag-off screen looks exactly like today's.
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  ApiError, getServingPolicy, getTemporalPolicy, putServingPolicy, putTemporalPolicy,
} from '../api'
import type { ServingPolicyView, TemporalPolicyView } from '../api'

// Closed server vocabularies (profile_vocab.py §6.1, temporal_policy.py §6.5).
const STORAGE_MODELS = ['current_only', 'scd1', 'scd2', 'snapshot', 'event_log', 'unknown']
const SELECTION_KINDS = ['current_record', 'valid_at_report_cutoff', 'latest_snapshot_as_of',
  'explicit_only']
const NEED_ROLES = ['population', 'event_source', 'dimension_source', 'reference', 'mapping']
const SERVING_PURPOSES = ['analytical', 'feature_serving']

function conflictNotice(what: string) {
  return `Someone else declared this ${what} while you were editing — nothing was overwritten, and `
    + 'neither were your words. Their version is shown beside yours; re-review it, then save again.'
}

export function DatasetPolicyPanel(
  { source, objectRef, kind }: { source: string; objectRef: string; kind: string },
) {
  if (kind !== 'table') return null
  return (
    <section className="adg-section" data-testid="dataset-policies">
      <TemporalPolicySection source={source} objectRef={objectRef} />
      <ServingPolicySection source={source} objectRef={objectRef} />
    </section>
  )
}

// ── which ROWS answer a question ────────────────────────────────────────────────────────────────

function TemporalPolicySection({ source, objectRef }: { source: string; objectRef: string }) {
  const [view, setView] = useState<TemporalPolicyView | null>(null)
  const [absent, setAbsent] = useState(false)      // 404: surface off, or asset not visible
  const [editing, setEditing] = useState(false)
  const [conflicted, setConflicted] = useState(false)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [model, setModel] = useState('unknown')
  const [current, setCurrent] = useState('current_record')
  const [historical, setHistorical] = useState('explicit_only')
  const [effectiveFrom, setEffectiveFrom] = useState('')
  const [effectiveTo, setEffectiveTo] = useState('')
  const [snapshot, setSnapshot] = useState('')
  const [currentFlag, setCurrentFlag] = useState('')
  const [availability, setAvailability] = useState('')

  const load = useCallback(async (keepDraft = false) => {
    try {
      const got = await getTemporalPolicy(source, objectRef)
      setView(got)
      setAbsent(false)
      if (keepDraft) return
      const p = got.policy
      setModel(p?.temporal_storage_model
        ?? got.load_bearing_temporal_storage_model ?? 'unknown')
      setCurrent(p?.current_selection ?? 'current_record')
      setHistorical(p?.historical_selection ?? 'explicit_only')
      setEffectiveFrom(p?.effective_from_ref ?? '')
      setEffectiveTo(p?.effective_to_ref ?? '')
      setSnapshot(p?.snapshot_ref ?? '')
      setCurrentFlag(p?.current_flag_ref ?? '')
      setAvailability(p?.availability_ref ?? '')
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) setAbsent(true)
      else setNotice(e instanceof ApiError ? e.detail : String(e))
    }
  }, [source, objectRef])

  useEffect(() => { void load() }, [load])

  if (absent || !view) return null

  const governed = view.load_bearing_temporal_storage_model

  async function save(e: FormEvent) {
    e.preventDefault()
    if (!view) return
    setBusy(true)
    setNotice('')
    try {
      await putTemporalPolicy(source, objectRef, {
        expectedPointerVersion: view.pointer_version,
        temporalStorageModel: model,
        currentSelection: current,
        historicalSelection: historical,
        effectiveFromRef: effectiveFrom.trim() || undefined,
        effectiveToRef: effectiveTo.trim() || undefined,
        snapshotRef: snapshot.trim() || undefined,
        currentFlagRef: currentFlag.trim() || undefined,
        availabilityRef: availability.trim() || undefined,
      })
      setNotice('Saved.')
      setEditing(false)
      setConflicted(false)
      await load()
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotice(conflictNotice('row policy'))
        setConflicted(true)
        await load(true)          // refresh the CAS anchor and their copy; KEEP the draft
      } else {
        setNotice(err instanceof ApiError ? err.detail : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  const p = view.policy
  return (
    <div data-testid="temporal-policy">
      <h3 className="micro-label">Which rows answer a question</h3>
      {notice && <p role="status" className="hint">{notice}</p>}
      {!editing && (
        <>
          {p ? (
            <div className="adg-kv">
              <div className="kv">
                <span className="k">Stores history as</span>
                <span className="v"><span className="mono">{p.temporal_storage_model}</span></span>
              </div>
              <div className="kv">
                <span className="k">A current question uses</span>
                <span className="v"><span className="mono">{p.current_selection}</span></span>
              </div>
              <div className="kv">
                <span className="k">A historical question uses</span>
                <span className="v">
                  <span className="mono">{p.historical_selection}</span>
                  {p.historical_selection === 'explicit_only' && (
                    <> <span className="hint">— this dataset cannot answer one on its own</span></>
                  )}
                </span>
              </div>
              <div className="kv">
                <span className="k">Declared by</span>
                <span className="v">{view.declared_by ?? ''}</span>
              </div>
            </div>
          ) : (
            <p className="hint" data-testid="temporal-policy-empty">
              Nobody has declared how this dataset stores history yet. Declaring it here is what
              lets a question about an earlier date be answered from the right rows instead of
              today's.
            </p>
          )}
          <button type="button" onClick={() => { setConflicted(false); setEditing(true) }}>
            {p ? 'Edit row policy' : 'Declare row policy'}
          </button>
        </>
      )}
      {editing && (
        <form onSubmit={save} style={{ display: 'grid', gap: 8, marginTop: 8 }}>
          {conflicted && p && (
            <div className="adg-kv" data-testid="temporal-policy-theirs">
              <p className="hint">
                Their version — yours is still in the fields below, unchanged. Fold in anything
                worth keeping, then save.
              </p>
              <div className="kv">
                <span className="k">Their storage model</span>
                <span className="v"><span className="mono">{p.temporal_storage_model}</span></span>
              </div>
              <div className="kv">
                <span className="k">Their historical selection</span>
                <span className="v"><span className="mono">{p.historical_selection}</span></span>
              </div>
            </div>
          )}
          <label>
            Stores history as
            <select value={model} onChange={e => setModel(e.target.value)}>
              {STORAGE_MODELS.map(m => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
          {governed ? (
            <p className="hint" data-testid="temporal-policy-governed">
              A reviewed classification already says <span className="mono">{governed}</span>.
              A policy that disagrees with it is refused — correct the profile field first.
            </p>
          ) : (
            <p className="hint" data-testid="temporal-policy-declares">
              Nobody has reviewed a storage model for this dataset, so this declaration is what the
              planner will use.
            </p>
          )}
          <label>
            A current question uses
            <select value={current} onChange={e => setCurrent(e.target.value)}>
              {SELECTION_KINDS.map(k => <option key={k} value={k}>{k}</option>)}
            </select>
          </label>
          <label>
            A historical question uses
            <select value={historical} onChange={e => setHistorical(e.target.value)}>
              {SELECTION_KINDS.map(k => <option key={k} value={k}>{k}</option>)}
            </select>
          </label>
          <label>
            Valid-from column
            <input value={effectiveFrom} onChange={e => setEffectiveFrom(e.target.value)}
                   maxLength={500} placeholder="source::schema.table.column" />
          </label>
          <label>
            Valid-to column
            <input value={effectiveTo} onChange={e => setEffectiveTo(e.target.value)}
                   maxLength={500} placeholder="source::schema.table.column" />
          </label>
          <label>
            Snapshot-date column
            <input value={snapshot} onChange={e => setSnapshot(e.target.value)} maxLength={500} />
          </label>
          <label>
            Current-row flag column
            <input value={currentFlag} onChange={e => setCurrentFlag(e.target.value)}
                   maxLength={500} />
          </label>
          <label>
            Available-from column
            <input value={availability} onChange={e => setAvailability(e.target.value)}
                   maxLength={500} />
          </label>
          <div>
            <button type="submit" disabled={busy}>Save</button>{' '}
            <button type="button"
                    onClick={() => { setEditing(false); setConflicted(false); void load() }}>
              Cancel
            </button>
          </div>
        </form>
      )}
    </div>
  )
}

// ── which DATASET serves a need ─────────────────────────────────────────────────────────────────

function ServingPolicySection({ source, objectRef }: { source: string; objectRef: string }) {
  const [entityId, setEntityId] = useState('')
  const [needRole, setNeedRole] = useState('population')
  const [purpose, setPurpose] = useState('analytical')
  const [view, setView] = useState<ServingPolicyView | null>(null)
  const [absent, setAbsent] = useState(false)
  const [conflicted, setConflicted] = useState(false)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [eligible, setEligible] = useState('')
  const [preferred, setPreferred] = useState('')

  // This dataset's own logical ref, offered as a one-click addition. Derived from the ref by
  // string, never looked up: the flat `public.<table>` object ref IS the table half.
  const thisDataset = `${source}::${objectRef.split('.').slice(0, 2).join('.')}`

  const load = useCallback(async (keepDraft = false) => {
    if (!entityId.trim()) { setView(null); return }
    try {
      const got = await getServingPolicy(entityId.trim(), needRole, purpose)
      setView(got)
      setAbsent(false)
      if (keepDraft) return
      setEligible((got.policy?.eligible_dataset_refs ?? []).join('\n'))
      setPreferred((got.policy?.preferred_dataset_refs ?? []).join('\n'))
    } catch (e) {
      if (e instanceof ApiError && e.status === 404) { setAbsent(true); setView(null) }
      else setNotice(e instanceof ApiError ? e.detail : String(e))
    }
  }, [entityId, needRole, purpose])

  if (absent) return null

  const lines = (text: string) => text.split('\n').map(s => s.trim()).filter(Boolean)

  async function save(e: FormEvent) {
    e.preventDefault()
    if (!view) return
    setBusy(true)
    setNotice('')
    try {
      const res = await putServingPolicy(entityId.trim(), needRole, purpose, {
        expectedPointerVersion: view.pointer_version,
        eligibleDatasetRefs: lines(eligible),
        preferredDatasetRefs: lines(preferred),
      })
      setNotice(res.ambiguous
        ? 'Saved. More than one dataset is equally preferred, so this policy cannot pick one on '
          + 'its own — a question that needs it will ask.'
        : 'Saved.')
      setConflicted(false)
      await load()
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotice(conflictNotice('serving policy'))
        setConflicted(true)
        await load(true)
      } else {
        setNotice(err instanceof ApiError ? err.detail : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  return (
    <div data-testid="serving-policy" style={{ marginTop: 16 }}>
      <h3 className="micro-label">Which dataset serves a need</h3>
      <p className="hint">
        A serving policy answers "which copy of the customer master is this question about". It is
        keyed by the NEED, not by this dataset, so name the need you are declaring for.
      </p>
      {notice && <p role="status" className="hint">{notice}</p>}
      <div style={{ display: 'grid', gap: 8 }}>
        <label>
          Entity
          <input value={entityId} onChange={e => setEntityId(e.target.value)} maxLength={200}
                 placeholder="customer" />
        </label>
        <label>
          Needed as
          <select value={needRole} onChange={e => setNeedRole(e.target.value)}>
            {NEED_ROLES.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label>
          For
          <select value={purpose} onChange={e => setPurpose(e.target.value)}>
            {SERVING_PURPOSES.map(p => <option key={p} value={p}>{p}</option>)}
          </select>
        </label>
        <div>
          <button type="button" disabled={!entityId.trim()} onClick={() => { void load() }}>
            Load policy
          </button>
        </div>
      </div>
      {view && (
        <form onSubmit={save} style={{ display: 'grid', gap: 8, marginTop: 8 }}>
          {!view.policy && (
            <p className="hint" data-testid="serving-policy-empty">
              Nothing has been declared for this need yet. That is normal — a need with one
              obvious dataset never requires a declaration.
            </p>
          )}
          {view.ambiguous && (
            <p className="hint" data-testid="serving-policy-ambiguous">
              This policy cannot pick one dataset on its own: more than one is equally preferred.
              A question that needs it will ask rather than guess.
            </p>
          )}
          {conflicted && view.policy && (
            <div className="adg-kv" data-testid="serving-policy-theirs">
              <p className="hint">
                Their version — yours is still in the fields below, unchanged.
              </p>
              <div className="kv">
                <span className="k">Their eligible</span>
                <span className="v">
                  <span className="mono">{view.policy.eligible_dataset_refs.join(', ')}</span>
                </span>
              </div>
              <div className="kv">
                <span className="k">Their preferred</span>
                <span className="v">
                  <span className="mono">{view.policy.preferred_dataset_refs.join(', ')}</span>
                </span>
              </div>
            </div>
          )}
          <label>
            May serve it (one dataset ref per line)
            <textarea value={eligible} onChange={e => setEligible(e.target.value)} rows={3} />
          </label>
          <label>
            Preferred (must also appear above)
            <textarea value={preferred} onChange={e => setPreferred(e.target.value)} rows={2} />
          </label>
          <div>
            <button type="button" onClick={() => {
              if (!lines(eligible).includes(thisDataset)) {
                setEligible(`${eligible.trimEnd()}\n${thisDataset}`.trim())
              }
            }}>
              Add this dataset
            </button>
          </div>
          <div>
            <button type="submit" disabled={busy}>Save</button>
          </div>
        </form>
      )}
    </div>
  )
}
