// Release-A profile panel (table assets only): the effective semantic profile with provenance,
// plus the data_owner proposal editor for the three new fields.
//
// Framing rules (the no-"blocked" product rule):
//   * a PROPOSED value is rendered as a normal, usable value with a neutral "proposed" label —
//     never error-styled, never hidden pending review;
//   * a field with no load-bearing value renders its FAMILY ("nobody decided yet" / "needs a
//     data check" / "structurally unsuitable"), never a raw failure string;
//   * the routes 404 while FEATUREGEN_DATASET_PROFILES is off (or for a non-table asset), and
//     this panel then renders NOTHING — flag-off screens look exactly like today's.
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import { ApiError, getAssetProfile, putAssetProfile } from '../api'
import type { AssetProfile, EffectiveProfileField } from '../api'

// Closed server vocabularies (profile_vocab.py §6.1) for the proposal selects.
const AUTHORITY_ROLES = ['system_of_record', 'mastered_view', 'authoritative_replica',
  'non_authoritative_replica', 'derived', 'external_reference', 'unknown']
const TEMPORAL_MODELS = ['current_only', 'scd1', 'scd2', 'snapshot', 'event_log', 'unknown']

const FIELD_LABELS: [keyof ProfileFields, string][] = [
  ['description', 'Description'],
  ['business_context', 'Business context'],
  ['domains', 'Domains'],
  ['data_role', 'Data role'],
  ['primary_entity', 'Primary entity'],
  ['authority_role', 'Authority role'],
  ['temporal_storage_model', 'Temporal storage model'],
  ['event_or_snapshot', 'Event or snapshot'],
]
type ProfileFields = Pick<AssetProfile,
  'description' | 'business_context' | 'domains' | 'data_role' | 'primary_entity'
  | 'authority_role' | 'temporal_storage_model' | 'event_or_snapshot'>

// The three-family vocabulary, rendered as plain product language — never a failure string.
const FAMILY_TEXT: Record<string, string> = {
  undecided: 'Nobody has decided yet',
  needs_data_check: 'Needs a data check',
  structurally_unsuitable: 'Structurally unsuitable',
}

function FieldRow({ label, field }: { label: string; field: EffectiveProfileField }) {
  const shown = field.display
  return (
    <div className="kv" data-testid={`profile-${label.toLowerCase().replace(/ /g, '-')}`}>
      <span className="k">{label}</span>
      <span className="v">
        {shown ? (
          <>
            <span className="mono">{shown.value}</span>{' '}
            <span className="badge" title={`lifecycle: ${shown.lifecycle}`}>
              {shown.producer}/{shown.strength}
            </span>{' '}
            {field.load_bearing ? (
              <span className="badge grain">load-bearing</span>
            ) : shown.strength === 'proposed' ? (
              <span className="badge">proposed — usable, awaiting review</span>
            ) : null}
          </>
        ) : (
          <span className="hint">
            {(field.unresolved_family && FAMILY_TEXT[field.unresolved_family])
              ?? FAMILY_TEXT.undecided}
          </span>
        )}
      </span>
    </div>
  )
}

export function AssetProfilePanel({ source, objectRef, kind }: {
  source: string
  objectRef: string
  kind: string
}) {
  const [profile, setProfile] = useState<AssetProfile | null>(null)
  const [absent, setAbsent] = useState(false)   // 404: flag off / not a table — render nothing
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  // Proposal editor state (the three PUT-editable fields only).
  const [businessContext, setBusinessContext] = useState('')
  const [authorityRole, setAuthorityRole] = useState('')
  const [temporalModel, setTemporalModel] = useState('')

  const load = useCallback(async () => {
    try {
      setProfile(await getAssetProfile(source, objectRef))
      setAbsent(false)
    } catch (e) {
      if (e instanceof ApiError && (e.status === 404 || e.status === 400)) {
        setAbsent(true)   // surface absent (flag off) or not table-scoped: show nothing
      } else {
        setError(e instanceof ApiError ? e.detail : String(e))
      }
    }
  }, [source, objectRef])

  useEffect(() => {
    if (kind === 'table') void load()
  }, [kind, load])

  if (kind !== 'table' || absent) return null
  if (error) {
    return (
      <section className="adg-section" data-testid="asset-profile">
        <h3>Profile</h3>
        <p className="hint">Could not load the profile: {error}</p>
      </section>
    )
  }
  if (!profile) return null

  async function propose(e: FormEvent) {
    e.preventDefault()
    if (!profile) return
    setBusy(true)
    setNotice('')
    try {
      await putAssetProfile(source, objectRef, {
        expectedDatasetProfileHash: profile.dataset_profile_hash,
        businessContext: businessContext.trim() || undefined,
        authorityRole: authorityRole || undefined,
        temporalStorageModel: temporalModel || undefined,
      })
      setNotice('Proposed. The value is visible and usable now, labeled as proposed; an '
        + 'operational promotion still needs a separate reviewer to confirm it.')
      setBusinessContext('')
      setAuthorityRole('')
      setTemporalModel('')
      await load()
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotice('The profile changed since you loaded it — nothing was written. Re-review the '
          + 'refreshed values before proposing again.')
        await load()
      } else {
        setNotice(err instanceof ApiError ? err.detail : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  const nothingToPropose = !businessContext.trim() && !authorityRole && !temporalModel
  return (
    <section className="adg-section" data-testid="asset-profile">
      <h3>Profile</h3>
      {notice && (
        <p role="status" className="hint">{notice}</p>
      )}
      <div className="adg-kv">
        {FIELD_LABELS.map(([key, label]) => (
          <FieldRow key={key} label={label} field={profile[key]} />
        ))}
      </div>
      <form onSubmit={propose} style={{ display: 'grid', gap: 8, marginTop: 12 }}>
        <strong>Propose profile values</strong>
        <label>
          Business context
          <textarea
            value={businessContext}
            onChange={e => setBusinessContext(e.target.value)}
            maxLength={4000}
            rows={2}
          />
        </label>
        <label>
          Authority role
          <select value={authorityRole} onChange={e => setAuthorityRole(e.target.value)}>
            <option value="">(no change)</option>
            {AUTHORITY_ROLES.map(r => <option key={r} value={r}>{r}</option>)}
          </select>
        </label>
        <label>
          Temporal storage model
          <select value={temporalModel} onChange={e => setTemporalModel(e.target.value)}>
            <option value="">(no change)</option>
            {TEMPORAL_MODELS.map(m => <option key={m} value={m}>{m}</option>)}
          </select>
        </label>
        <div>
          <button type="submit" disabled={busy || nothingToPropose}>Propose</button>
        </div>
      </form>
    </section>
  )
}
