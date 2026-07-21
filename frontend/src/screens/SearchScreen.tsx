import { type FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  SEARCH_FACET_KEYS,
  type SearchFacetKey,
  type SearchFilters,
  type SearchHit,
  type SearchResult,
  featureImpact,
  searchCatalog,
} from '../api'
import { useHashRoute } from '../nav'
import { LineageView } from './LineageView'

const FACET_GROUPS: { key: SearchFacetKey; label: string }[] = [
  { key: 'source', label: 'Source' },
  { key: 'domain', label: 'Domain' },
  { key: 'sensitivity', label: 'Sensitivity' },
  { key: 'additivity', label: 'Additivity' },
  { key: 'entity', label: 'Entity' },
  { key: 'kind', label: 'Kind' },
]

const FLAG_OPTIONS: { key: 'grain' | 'as_of'; label: string }[] = [
  { key: 'grain', label: 'Grain' },
  { key: 'as_of', label: 'As-of' },
]

function paramsToFilters(params: URLSearchParams): SearchFilters {
  const filters: SearchFilters = {}
  for (const key of SEARCH_FACET_KEYS) {
    const values = params.getAll(key)
    if (values.length > 0) filters[key] = values
  }
  if (params.get('grain') === 'true') filters.grain = true
  if (params.get('as_of') === 'true') filters.as_of = true
  return filters
}

// Canonical query string for a search state — the same ordering searchCatalog uses on the wire,
// so the hash mirrors the request (minus limit) and is a shareable link.
function buildSearchHash(q: string, filters: SearchFilters): string {
  const params = new URLSearchParams()
  if (q) params.set('q', q)
  for (const key of SEARCH_FACET_KEYS) {
    for (const value of filters[key] ?? []) params.append(key, value)
  }
  if (filters.grain) params.set('grain', 'true')
  if (filters.as_of) params.set('as_of', 'true')
  return params.toString()
}

// Normalize whatever the hash holds (any order) to the canonical form above, so the own-write
// guard compares apples to apples regardless of how a pasted deep link was ordered.
function canonicalHash(params: URLSearchParams): string {
  return buildSearchHash(params.get('q') ?? '', paramsToFilters(params))
}

export function SearchScreen() {
  const { params, navigate } = useHashRoute()
  // Committed search state lives in React; the hash mirrors it (a shareable output) and seeds it
  // (on mount and on external navigation). `draft` is the live input; `q` is the searched term.
  const [draft, setDraft] = useState(() => params.get('q') ?? '')
  const [q, setQ] = useState(() => params.get('q') ?? '')
  const [filters, setFilters] = useState<SearchFilters>(() => paramsToFilters(params))
  const [result, setResult] = useState<SearchResult | null>(null)
  const [error, setError] = useState('')
  // List is today's behavior unchanged; Graph maps lineage around one hit. The anchor is the row
  // the user jumped from, or the first hit of the current (facet-narrowed) set otherwise.
  const [view, setView] = useState<'list' | 'graph'>('list')
  const [anchor, setAnchor] = useState<SearchHit | null>(null)
  // Monotonic request id: a resolved search only applies if it is still the latest, so a slow
  // older response can never overwrite newer results.
  const seq = useRef(0)
  // The hash we last originated. Guards the sync-from-hash effect from reacting to our own writes.
  const appliedHash = useRef<string | null>(null)

  const runSearch = useCallback((nextQ: string, nextFilters: SearchFilters) => {
    const id = ++seq.current
    setError('')
    searchCatalog(nextQ, nextFilters)
      .then(res => {
        if (id !== seq.current) return
        setResult(res)
        // A clicked anchor survives re-searches while its hit is still in the set; only when it
        // drops out does the graph fall back to the first hit. (Silently resetting to hits[0] made
        // an unfiltered browse — where the TABLE is the first hit — hijack column anchors.)
        setAnchor(prev =>
          prev &&
          res.hits.some(
            h => h.catalog_source === prev.catalog_source && h.object_ref === prev.object_ref,
          )
            ? prev
            : null,
        )
      })
      .catch(err => {
        if (id !== seq.current) return
        setResult(null)
        setAnchor(null)
        setError(err instanceof ApiError ? err.detail : String(err))
      })
  }, [])

  // Commit a search: reflect it in state, mirror it to the hash (shareable), and fetch. Facet
  // toggles keep the committed query; submit commits the draft.
  const apply = useCallback(
    (nextQ: string, nextFilters: SearchFilters) => {
      const hash = buildSearchHash(nextQ, nextFilters)
      appliedHash.current = hash
      setQ(nextQ)
      setDraft(nextQ)
      setFilters(nextFilters)
      runSearch(nextQ, nextFilters)
      navigate('search', new URLSearchParams(hash))
    },
    [navigate, runSearch],
  )

  // Mount + external navigation (deep-link paste, back/forward): adopt the hash and search. Our
  // own apply() sets appliedHash before navigating, so this skips writes we originated. The empty
  // hash browses the whole read-scoped set.
  const currentHash = canonicalHash(params)
  useEffect(() => {
    if (appliedHash.current === currentHash) return
    appliedHash.current = currentHash
    const parsed = new URLSearchParams(currentHash)
    const parsedQ = parsed.get('q') ?? ''
    const parsedFilters = paramsToFilters(parsed)
    setQ(parsedQ)
    setDraft(parsedQ)
    setFilters(parsedFilters)
    runSearch(parsedQ, parsedFilters)
  }, [currentHash, runSearch])

  function submit(e: FormEvent) {
    e.preventDefault()
    apply(draft.trim(), filters)
  }

  function toggleFacet(key: SearchFacetKey, value: string) {
    const current = filters[key] ?? []
    const nextValues = current.includes(value)
      ? current.filter(v => v !== value)
      : [...current, value]
    const next: SearchFilters = { ...filters }
    if (nextValues.length > 0) next[key] = nextValues
    else delete next[key]
    apply(q, next)
  }

  function toggleFlag(key: 'grain' | 'as_of') {
    const next: SearchFilters = { ...filters }
    if (filters[key]) delete next[key]
    else next[key] = true
    apply(q, next)
  }

  function clearAll() {
    apply(q, {})
  }

  const hasHits = result !== null && result.hits.length > 0
  // With no results there is nothing to anchor a graph on: fall back to list behavior (empty
  // states, alerts) and disable the toggle.
  const effectiveView = hasHits ? view : 'list'
  const graphAnchor = hasHits ? (anchor ?? result.hits[0]) : null

  function jumpToGraph(hit: SearchHit) {
    setAnchor(hit)
    setView('graph')
  }

  // Open the asset-detail screen for a hit. The catalog source is the read/registration lineage
  // key, so the asset route carries the hit's own catalog_source (never a client-side default).
  function openDetails(hit: SearchHit) {
    navigate('asset', { source: hit.catalog_source, object_ref: hit.object_ref })
  }

  // Active-filter chips, in facet-group order, then flags.
  const chips: { id: string; label: string; pii: boolean; remove: () => void }[] = []
  for (const group of FACET_GROUPS) {
    for (const value of filters[group.key] ?? []) {
      chips.push({
        id: `${group.key}:${value}`,
        label: `${group.label.toLowerCase()}: ${value}`,
        pii: group.key === 'sensitivity' && value === 'pii',
        remove: () => toggleFacet(group.key, value),
      })
    }
  }
  if (filters.grain) {
    chips.push({ id: 'grain', label: 'grain', pii: false, remove: () => toggleFlag('grain') })
  }
  if (filters.as_of) {
    chips.push({ id: 'as_of', label: 'as-of', pii: false, remove: () => toggleFlag('as_of') })
  }
  const hasFilters = chips.length > 0

  const flagBuckets = result
    ? { grain: result.facets.grain?.[0], as_of: result.facets.as_of?.[0] }
    : null
  const showFlags = Boolean(flagBuckets && (flagBuckets.grain || flagBuckets.as_of))

  return (
    <section className="search-screen">
      {/* Section landmark for assistive tech; the visible page title lives in the app page-head,
          so the mockup's layout stays clean (no repeated visible heading). */}
      <h2 className="visually-hidden">Search the catalog</h2>
      <form onSubmit={submit} role="search" className="search-bar">
        <input
          aria-label="Query"
          className="search-input"
          value={draft}
          onChange={e => setDraft(e.target.value)}
          placeholder="Column, table, or concept"
        />
        <button type="submit" className="btn btn--primary search-submit">
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

      <div className="active-filters">
        <span className="active-filters-label">{hasFilters ? 'Filters' : 'No filters'}</span>
        {chips.map(chip => (
          <span
            key={chip.id}
            className={chip.pii ? 'filter-chip filter-chip--pii' : 'filter-chip'}
          >
            {chip.label}
            <button type="button" aria-label={`Remove ${chip.label}`} onClick={chip.remove}>
              ×
            </button>
          </span>
        ))}
        {hasFilters && (
          <button type="button" className="clear-filters" onClick={clearAll}>
            Clear all
          </button>
        )}
      </div>

      <div className="facet-cols">
        <aside className="facet-panel" aria-label="Filters">
          {FACET_GROUPS.map(group => {
            const buckets = result?.facets[group.key] ?? []
            if (buckets.length === 0) return null
            return (
              <fieldset className="facet-group" key={group.key}>
                <legend className="facet-group-title">{group.label}</legend>
                {buckets.map(bucket => {
                  const checked = (filters[group.key] ?? []).includes(bucket.value)
                  const isPii = group.key === 'sensitivity' && bucket.value === 'pii'
                  return (
                    <label className="facet-option" key={bucket.value}>
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggleFacet(group.key, bucket.value)}
                      />
                      {isPii && <span className="facet-pii-dot" aria-hidden="true" />}
                      <span className="facet-name">{bucket.value}</span>{' '}
                      <span className="facet-count tabular-nums">{bucket.count}</span>
                    </label>
                  )
                })}
              </fieldset>
            )
          })}
          {showFlags && (
            <fieldset className="facet-group">
              <legend className="facet-group-title">Flags</legend>
              {FLAG_OPTIONS.map(flag => {
                const count = flagBuckets?.[flag.key]?.count ?? 0
                const checked = Boolean(filters[flag.key])
                // A flag with no matching rows and not already picked cannot narrow further.
                const disabled = count === 0 && !checked
                return (
                  <label
                    className={disabled ? 'facet-option facet-option--disabled' : 'facet-option'}
                    key={flag.key}
                  >
                    <input
                      type="checkbox"
                      checked={checked}
                      disabled={disabled}
                      onChange={() => toggleFlag(flag.key)}
                    />
                    <span className="facet-name">{flag.label}</span>{' '}
                    <span className="facet-count tabular-nums">{count}</span>
                  </label>
                )
              })}
            </fieldset>
          )}
        </aside>

        <div className="search-results">
          {error && (
            <p role="alert" className="error">
              {error}
            </p>
          )}

          {!error && !result && (
            <p className="hint">Searching the catalog…</p>
          )}

          {!error && result && result.hits.length === 0 && (
            <div className="empty" role="status">
              <p>No results match these filters.</p>
              <p className="next">
                Loosen or clear a facet. A stale source is withheld until it is re-uploaded and
                re-vouched, and columns your roles cannot see are never shown. Nothing is shown that
                cannot be trusted.
              </p>
            </div>
          )}

          {!error && hasHits && (
            <p className="micro-label tabular-nums result-count" role="status">
              <span style={{ color: 'var(--accent)', fontWeight: 600 }}>{result.total}</span>{' '}
              {result.total === 1 ? 'result' : 'results'}
              {result.total > result.hits.length && (
                <span className="result-count-note"> · showing the first {result.hits.length}</span>
              )}
            </p>
          )}

          {!error && hasHits && effectiveView === 'list' && (
            <ul className="rows">
              {result.hits.map(hit => (
                <HitRow
                  key={`${hit.catalog_source}:${hit.object_ref}`}
                  hit={hit}
                  onGraph={jumpToGraph}
                  onDetails={openDetails}
                />
              ))}
            </ul>
          )}

          {effectiveView === 'graph' && graphAnchor && (
            // Keyed on the anchor: a new anchor remounts the view, resetting expansion, trace, and
            // drawer state cleanly. The caption names the anchor: without it, users could not tell
            // a table-anchored graph from a column-anchored one (the unfiltered browse lists the
            // table itself first, and its Graph action was mistaken for a column's).
            <>
              <p className="hint" role="status">
                Graph of: <code>{graphAnchor.object_ref}</code> (
                {graphAnchor.column ? 'column' : 'table'}). Click Graph on any result row to
                re-anchor.
              </p>
              <LineageView
                key={`${graphAnchor.catalog_source}:${graphAnchor.object_ref}`}
                anchor={graphAnchor}
              />
            </>
          )}
        </div>
      </div>
    </section>
  )
}

function HitRow({
  hit,
  onGraph,
  onDetails,
}: {
  hit: SearchHit
  onGraph: (hit: SearchHit) => void
  onDetails: (hit: SearchHit) => void
}) {
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
          {hit.kind === 'table' && <span className="badge kindtable">table</span>}
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
        aria-label={`Details for ${hit.object_ref}`}
        onClick={() => onDetails(hit)}
      >
        Details
      </button>
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
