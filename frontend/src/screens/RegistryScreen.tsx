import { useEffect, useState } from 'react'
import {
  ApiError,
  type FeatureDetail,
  type FeatureListItem,
  featureDetail,
  listFeatures,
} from '../api'
import type { Route } from '../nav'

type Nav = (r: Route, params?: Record<string, string>) => void

// The registry surface: a list of registered features, and — when the hash carries ?id= — the
// Feature 360 for one of them. The hash is the single source of truth, so a deep link to a feature
// (#/registry?id=feat_x) opens straight onto its 360.
export function RegistryScreen({ featureId, navigate }: { featureId: string | null; navigate: Nav }) {
  return featureId ? (
    <FeatureDetailPanel featureId={featureId} navigate={navigate} />
  ) : (
    <RegistryList navigate={navigate} />
  )
}

function RegistryList({ navigate }: { navigate: Nav }) {
  const [items, setItems] = useState<FeatureListItem[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    listFeatures()
      .then(rows => live && setItems(rows))
      .catch(err => live && setError(err instanceof ApiError ? err.detail : String(err)))
    return () => {
      live = false
    }
  }, [])

  if (error)
    return (
      <p role="alert" className="error">
        {error}
      </p>
    )
  if (items === null)
    return (
      <p className="hint" role="status">
        Loading the registry…
      </p>
    )
  if (items.length === 0)
    return (
      <div className="empty" role="status">
        <p>No features registered yet.</p>
        <p className="next">Generate features in the workbench, then confirm them into the registry.</p>
      </div>
    )
  return (
    <section>
      <h2>Feature registry</h2>
      <p className="micro-label tabular-nums" role="status">
        <span style={{ color: 'var(--accent)', fontWeight: 600 }}>{items.length}</span>{' '}
        {items.length === 1 ? 'feature' : 'features'}
      </p>
      <ul className="rows">
        {items.map(f => (
          <li key={f.feature_id} className="row">
            <div style={{ display: 'grid', gap: 2, minWidth: 0, flex: 1 }}>
              <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
                <strong>{f.name}</strong>
                <span className="badge">{f.verification}</span>
              </div>
              <p className="hint">
                {[f.aggregation, f.grain_table && `grain ${f.grain_table}`]
                  .filter(Boolean)
                  .join(' · ')}
              </p>
            </div>
            <button
              type="button"
              className="btn"
              aria-label={`Open ${f.name}`}
              onClick={() => navigate('registry', { id: f.feature_id })}
            >
              Open
            </button>
          </li>
        ))}
      </ul>
    </section>
  )
}

function Section({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div>
      <p className="micro-label">{label}</p>
      {children}
    </div>
  )
}

function FeatureDetailPanel({ featureId, navigate }: { featureId: string; navigate: Nav }) {
  const [detail, setDetail] = useState<FeatureDetail | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let live = true
    setDetail(null)
    setError('')
    featureDetail(featureId)
      .then(d => live && setDetail(d))
      .catch(err => live && setError(err instanceof ApiError ? err.detail : String(err)))
    return () => {
      live = false
    }
  }, [featureId])

  const back = (
    <button type="button" className="btn" onClick={() => navigate('registry')}>
      ← Registry
    </button>
  )
  if (error)
    return (
      <section>
        {back}
        <p role="alert" className="error">
          {error}
        </p>
      </section>
    )
  if (detail === null)
    return (
      <section>
        {back}
        <p className="hint" role="status">
          Loading feature…
        </p>
      </section>
    )

  const meta = [
    detail.aggregation,
    detail.grain_table && `grain ${detail.grain_table}`,
    detail.as_of_column && `as-of ${detail.as_of_column}`,
  ]
    .filter(Boolean)
    .join(' · ')
  return (
    <section style={{ display: 'grid', gap: 20 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap' }}>
        {back}
        <h2 style={{ margin: 0 }}>{detail.name}</h2>
        <span className="badge">{detail.verification}</span>
      </div>

      <Section label="Definition">
        <p style={{ color: 'var(--ink-soft)' }}>
          {detail.contract?.definition || detail.description || 'No definition recorded.'}
        </p>
        {meta && <p className="hint">{meta}</p>}
      </Section>

      <Section label="Hypothesis — why this feature exists">
        {detail.hypothesis ? (
          <>
            <p style={{ color: 'var(--ink-soft)' }}>“{detail.hypothesis.hypothesis}”</p>
            {detail.hypothesis.definition && (
              <p className="hint">Intended definition: {detail.hypothesis.definition}</p>
            )}
            {detail.hypothesis.target_ref && (
              <p className="hint">
                Prediction target: <code>{detail.hypothesis.target_ref}</code>
              </p>
            )}
          </>
        ) : (
          <p className="hint">No hypothesis on record — this feature was registered directly.</p>
        )}
      </Section>

      <Section label="Derives from">
        {detail.derives_from.length ? (
          <ul className="mono" style={{ paddingLeft: 18, display: 'grid', gap: 2 }}>
            {detail.derives_from.map(d => (
              <li key={`${d.catalog_source}:${d.object_ref}`}>
                <code>{d.object_ref}</code> <span className="hint">({d.catalog_source})</span>
              </li>
            ))}
          </ul>
        ) : (
          <p className="hint">No source columns recorded.</p>
        )}
      </Section>

      <Section label="Used by">
        {detail.consumers.length ? (
          <ul className="rows">
            {detail.consumers.map(c => (
              <li key={`${c.model_ref}:${c.environment}`} className="row">
                <div style={{ display: 'grid', gap: 2 }}>
                  <strong>{c.model_ref}</strong>
                  <p className="hint">{[c.purpose, c.environment].filter(Boolean).join(' · ')}</p>
                </div>
              </li>
            ))}
          </ul>
        ) : (
          <p className="hint">No models registered as consumers yet.</p>
        )}
      </Section>
    </section>
  )
}
