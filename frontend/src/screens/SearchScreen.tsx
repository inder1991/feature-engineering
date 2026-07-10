import { type FormEvent, useRef, useState } from 'react'
import { ApiError, type SearchHit, featureImpact, searchCatalog } from '../api'
import { LineageView } from './LineageView'

const SUGGESTIONS = ['balance', 'customer', 'email']

export function SearchScreen() {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [error, setError] = useState('')
  // List is today's behavior unchanged; Graph maps lineage around one hit. The anchor is the
  // row the user jumped from, or the first hit when they just flip the toggle.
  const [view, setView] = useState<'list' | 'graph'>('list')
  const [anchor, setAnchor] = useState<SearchHit | null>(null)
  // Monotonic request id: a resolved search only applies if it is still the latest,
  // so a slow older response can never overwrite newer results.
  const seq = useRef(0)

  async function runSearch(term: string) {
    const query = term.trim()
    if (!query) return
    const id = ++seq.current
    setError('')
    try {
      const results = await searchCatalog(query)
      if (id !== seq.current) return
      setHits(results)
      setAnchor(null) // a new result set re-anchors the graph on its first hit
    } catch (err) {
      if (id !== seq.current) return
      setHits(null)
      setAnchor(null)
      setError(err instanceof ApiError ? err.detail : String(err))
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    void runSearch(q)
  }

  function suggest(term: string) {
    setQ(term)
    void runSearch(term)
  }

  function jumpToGraph(hit: SearchHit) {
    setAnchor(hit)
    setView('graph')
  }

  const hasHits = hits !== null && hits.length > 0
  // With no results there is nothing to anchor a graph on: fall back to list behavior
  // (empty states, alerts) and disable the toggle.
  const effectiveView = hasHits ? view : 'list'
  const graphAnchor = hasHits ? (anchor ?? hits[0]) : null

  return (
    <section>
      <h2>Search the catalog</h2>
      <form onSubmit={submit} role="search">
        <div className="field" style={{ flex: '1 1 320px' }}>
          <label htmlFor="search-query">Query</label>
          <input
            id="search-query"
            value={q}
            onChange={e => setQ(e.target.value)}
            placeholder="Column, table, or concept"
            style={{ height: 40 }}
          />
        </div>
        <button type="submit" className="btn btn--primary" style={{ height: 40 }}>
          Search
        </button>
        <div
          className="viewtoggle"
          role="group"
          aria-label="Result view"
          aria-describedby={hasHits ? undefined : 'viewtoggle-hint'}
        >
          <button
            type="button"
            aria-pressed={effectiveView === 'list'}
            disabled={!hasHits}
            onClick={() => setView('list')}
          >
            List
          </button>
          <button
            type="button"
            aria-pressed={effectiveView === 'graph'}
            disabled={!hasHits}
            onClick={() => setView('graph')}
          >
            Graph
          </button>
        </div>
        {!hasHits && (
          <span id="viewtoggle-hint" className="hint">
            Run a search to map lineage.
          </span>
        )}
      </form>

      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      {hits === null && !error && (
        <div className="empty">
          <p>Search the freshness-vouched catalog by column, table, or concept.</p>
          <div
            className="next"
            style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}
          >
            <span>Try</span>
            {SUGGESTIONS.map(term => (
              <button
                key={term}
                type="button"
                className="role-chip"
                onClick={() => suggest(term)}
              >
                {term}
              </button>
            ))}
          </div>
        </div>
      )}

      {hits?.length === 0 && (
        <div className="empty" role="status">
          <p>No fresh results.</p>
          <p className="next">
            Columns your roles cannot see are hidden, and a stale source is not served until it is
            re-uploaded and re-vouched. Nothing is shown that cannot be trusted.
          </p>
        </div>
      )}

      {hasHits && (
        <p className="micro-label tabular-nums" role="status">
          <span style={{ color: 'var(--accent)', fontWeight: 600 }}>{hits.length}</span>{' '}
          {hits.length === 1 ? 'column' : 'columns'}
        </p>
      )}

      {hasHits && effectiveView === 'list' && (
        <ul className="rows">
          {hits.map(hit => (
            <HitRow
              key={`${hit.catalog_source}:${hit.object_ref}`}
              hit={hit}
              onGraph={jumpToGraph}
            />
          ))}
        </ul>
      )}

      {effectiveView === 'graph' && graphAnchor && (
        // Keyed on the anchor: a new anchor remounts the view, resetting expansion, trace,
        // and drawer state cleanly.
        <LineageView
          key={`${graphAnchor.catalog_source}:${graphAnchor.object_ref}`}
          anchor={graphAnchor}
        />
      )}
    </section>
  )
}

function HitRow({ hit, onGraph }: { hit: SearchHit; onGraph: (hit: SearchHit) => void }) {
  const [impact, setImpact] = useState<string[] | null>(null)
  const [impactError, setImpactError] = useState('')
  const [checking, setChecking] = useState(false)

  async function checkImpact() {
    setChecking(true)
    setImpactError('')
    try {
      setImpact(await featureImpact(hit.object_ref, hit.catalog_source))
    } catch (err) {
      setImpact(null)
      setImpactError(err instanceof ApiError ? err.detail : String(err))
    } finally {
      setChecking(false)
    }
  }

  const aggregation = hit.additivity
    ? `${hit.additivity}${hit.unit ? ` · ${hit.unit}` : ''}${hit.currency ? ` (${hit.currency})` : ''}`
    : null
  const meta = [
    hit.data_type ?? hit.kind,
    hit.catalog_source,
    hit.concept,
    hit.domain,
    hit.entity,
    aggregation,
  ]
    .filter((part): part is string => Boolean(part))
    .join(' · ')
  return (
    <li className="row">
      <div style={{ display: 'grid', gap: 2, minWidth: 0, flex: 1 }}>
        <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}>
          <code>{hit.object_ref}</code>
          {hit.is_grain && <span className="badge grain">grain</span>}
          {hit.is_as_of && <span className="badge asof">as-of</span>}
          {hit.sensitivity && <span className="badge sensitivity">{hit.sensitivity}</span>}
        </div>
        {hit.definition && <p style={{ color: 'var(--ink-soft)' }}>{hit.definition}</p>}
        <p className="hint">{meta}</p>
        {checking && <p className="hint">Checking feature impact…</p>}
        {impactError && (
          <p role="alert" className="error">
            Impact check failed: {impactError}
          </p>
        )}
        {impact?.length === 0 && (
          <p className="hint" role="status">
            No features derive from this column.
          </p>
        )}
        {impact && impact.length > 0 && (
          <div>
            <p className="micro-label" style={{ marginTop: 4 }}>
              Derived features
            </p>
            <ul className="mono" style={{ marginTop: 2, paddingLeft: 18, display: 'grid', gap: 2 }}>
              {impact.map(id => (
                <li key={id}>{id}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
      <button
        type="button"
        className="btn btn--ghost"
        aria-label={`Graph for ${hit.object_ref}`}
        onClick={() => onGraph(hit)}
      >
        Graph
      </button>
      <button
        type="button"
        className="btn"
        aria-label={`Impact for ${hit.object_ref}`}
        disabled={checking}
        onClick={() => void checkImpact()}
      >
        Impact
      </button>
    </li>
  )
}
