import { type FormEvent, useCallback, useEffect, useRef, useState } from 'react'
import {
  ApiError,
  SEARCH_FACET_KEYS,
  SEARCH_PAGE_SIZE,
  type SearchFacetKey,
  type SearchFilters,
  type SearchHit,
  type SearchResult,
  searchCatalog,
} from '../api'
import { useHashRoute } from '../nav'
import { CalloutGlyph } from './IngestResultCallout'
import { LineageView } from './LineageView'
import { SearchFacetPanel } from './SearchFacetPanel'
import { SearchHitRow } from './SearchHitRow'
import { facetLabel } from './searchFacetLabels'

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
  // Which slice of the result set is on screen. Deliberately NOT mirrored into the hash: the hash
  // is a shareable description of the QUERY, and a shared link should open at the first page rather
  // than at whatever page its author happened to be on when they copied it.
  const [offset, setOffset] = useState(0)
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
  const queryField = useRef<HTMLInputElement>(null)

  // "/" focuses search from anywhere on the page — except while the user is typing, where it is
  // just a character (`a/b` must stay `a/b`).
  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== '/' || event.metaKey || event.ctrlKey || event.altKey) return
      const active = document.activeElement
      const typing = active instanceof HTMLInputElement
        || active instanceof HTMLTextAreaElement
        || (active instanceof HTMLElement && active.isContentEditable)
      if (typing) return
      event.preventDefault()
      queryField.current?.focus()
    }
    document.addEventListener('keydown', onKeyDown)
    return () => document.removeEventListener('keydown', onKeyDown)
  }, [])

  const runSearch = useCallback((nextQ: string, nextFilters: SearchFilters, nextOffset = 0) => {
    const id = ++seq.current
    setError('')
    setOffset(nextOffset)
    searchCatalog(nextQ, nextFilters, SEARCH_PAGE_SIZE, nextOffset)
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
      // Back to the first page. A facet toggle or a new query CHANGES the result set, so carrying
      // the old offset would land past the end of a smaller one and show an empty page for a
      // filter that genuinely matches rows.
      runSearch(nextQ, nextFilters, 0)
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
    runSearch(parsedQ, parsedFilters, 0)
  }, [currentHash, runSearch])

  // Paging re-runs the SAME committed search at a new window, so it does not touch the hash.
  const goToPage = useCallback(
    (nextOffset: number) => runSearch(q, filters, Math.max(0, nextOffset)),
    [q, filters, runSearch],
  )

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

  // The read-only suggested-features sheet's ONE entry point (P4). Same shape as Details, keyed on
  // the hit's own TABLE: suggestions are per table, so a column hit opens the table it lives on.
  function openSuggested(hit: SearchHit) {
    // Carry the COLUMN so the table-scoped page can say which one you arrived from; without
    // it, every column on a table lands on an identical page.
    navigate('suggested', {
      source: hit.catalog_source,
      table: hit.table,
      ...(hit.column ? { column: hit.column } : {}),
    })
  }

  // Active-filter chips, in the order the facet keys ride the query string, then flags. The chip
  // wears the SAME label the sidebar group wears — `facetLabel` is the one owner of that
  // vocabulary, so a chip can never read "sensitivity display" under a group reading "Sensitivity".
  const chips: { id: string; label: string; pii: boolean; remove: () => void }[] = []
  for (const key of SEARCH_FACET_KEYS) {
    for (const value of filters[key] ?? []) {
      chips.push({
        id: `${key}:${value}`,
        label: `${facetLabel(key).toLowerCase()}: ${value === '(none)' ? 'not classified' : value}`,
        pii: key === 'sensitivity' && value === 'pii',
        remove: () => toggleFacet(key, value),
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

  return (
    <section className="search-screen">
      {/* Section landmark for assistive tech; the visible page title lives in the app page-head,
          so the mockup's layout stays clean (no repeated visible heading). */}
      <h2 className="visually-hidden">Search the catalog</h2>
      {/* No global view toggle. A lineage graph needs an ANCHOR, and a toolbar control has none:
          it sat disabled until results existed, then anchored on hits[0] — which is how an
          unfiltered browse let the TABLE row hijack a column's anchor. Graph mode is still real
          and is entered per row ("Explore relationships"); LineageView owns the way back. */}
      <form onSubmit={submit} role="search" className="search-bar">
        <div className="search-field">
          <input
            aria-label="Query"
            type="search"
            className="search-input"
            ref={queryField}
            value={draft}
            onChange={e => setDraft(e.target.value)}
            placeholder="Business term, physical name, or concept"
          />
          {/* An explicit control, not the browser's own. `type=search` renders a native clear
              affordance in some engines and none in others, and it is invisible to assistive
              tech and to our tests — so the screen owns one that is always there. */}
          {draft && (
            <button
              type="button"
              className="search-clear"
              aria-label="Clear search"
              onClick={() => {
                setDraft('')
                queryField.current?.focus()
              }}
            >
              ×
            </button>
          )}
        </div>
        <button type="submit" className="btn btn--primary search-submit">
          Search
        </button>
      </form>
      <p className="hint search-help">
        Try a business term, a physical name such as <code>cust_num</code>, or a concept.
        Press <kbd>/</kbd> to search from anywhere on the page.
      </p>

      {/* The row reserved 26px + 14px whether or not it had anything in it. Removing the
          "No filters" text left the empty container behind, which is most of the dead band
          between the search bar and the workspace. Render the row only when it has chips. */}
      {hasFilters && (
      <div className="active-filters">
        <span className="active-filters-label">Filters</span>
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
      )}

      <div className={effectiveView === 'graph' ? 'facet-cols facet-cols--graph' : 'facet-cols'}>
        {effectiveView === 'list' && (
          <SearchFacetPanel
            facets={result?.facets ?? {}}
            filters={filters}
            onToggleFacet={toggleFacet}
            onToggleFlag={toggleFlag}
          />
        )}

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
              {/* The exit. The zero-state named the causes but offered no control, so the only way
                  back to a populated catalog was hand-editing the hash or removing chips one at a
                  time. `apply('', {})` clears the query AND every facet in one commit. */}
              <button type="button" className="btn" onClick={() => apply('', {})}>
                Clear search and filters
              </button>
            </div>
          )}

          {/* The result slice is list-mode information. In graph mode it is chrome above a
              workspace that has its own anchor bar, and it was a third of the 300px of dead space
              before the canvas began. */}
          {!error && hasHits && effectiveView === 'list' && (
            <div className="results-toolbar">
              {/* The COUNT only. The slice is stated once, by the pager — the control that
                  navigates it — rather than twice in adjacent lines: the toolbar's slice note
                  rendered exactly when `total > hits.length`, which is exactly when the pager
                  renders too, so it was redundant in 100% of the states it could appear in. */}
              <p className="micro-label tabular-nums result-count" role="status">
                <span style={{ color: 'var(--accent)', fontWeight: 600 }}>{result.total}</span>{' '}
                {result.total === 1 ? 'asset' : 'assets'}
              </p>
              {/* The projection marker, finally rendered. Search reads the projected display
                  columns, so a lagged projection means these rows may not reflect the newest
                  resolved semantics — and until now the screen said nothing at all about it.
                  Optional-chained: an older backend (or a frontend-first deploy) omits the field,
                  and absence must render as absence rather than throw inside render. */}
              {result.projection?.status === 'ready' && (
                <p className="projection-ok" data-testid="search-projection">
                  Catalog projection current
                </p>
              )}
            </div>
          )}

          {/* A disclosure, never a refusal: whatever is below stays on screen either way.
              Deliberately NOT gated on list view. The toolbar above is chrome about a list slice,
              but this is a statement about the truth of data that is still on screen in EITHER
              view: a user already in Graph can submit a new query from the search bar, and
              LineageView renders the anchor's concept, entity, grain and as-of — precisely the
              projected display fields this marker qualifies. */}
          {!error && hasHits && result.projection?.status === 'lagged' && (
            <div className="callout callout--warn" role="status" data-testid="search-projection-lag">
              {/* The house warn triangle. Circle+X is danger and circle+check is ok in this
                  stylesheet's vocabulary, so a circle here would have spoken a third dialect. */}
              <CalloutGlyph>
                <path d="M8 2.75 14 13.25H2z" />
                <path d="M8 6.75v2.75M8 11.5v.01" />
              </CalloutGlyph>
              <div className="callout-body">
                <p>
                  The catalog projection was behind when these results were read, so they may not
                  yet reflect the newest resolved semantics.
                </p>
              </div>
            </div>
          )}

          {/* A1: paging the RESULT LIST while a graph is on screen is chrome for a view that is
              not showing. It cost ~90px directly above the workspace. */}
          {!error && hasHits && effectiveView === 'list'
            && (offset > 0 || offset + result.hits.length < result.total) && (
            <nav className="pager" aria-label="Result pages">
              {/* The ONE statement of the slice, in the control that navigates it. The pager only
                  renders when there is more than one page, so this line appears exactly when
                  paging is possible — which is when it earns its space.

                  "matching", not "permitted": `total` counts the rows matching THIS query and
                  THESE facets. "permitted" would claim the read-scoped universe is 42, which is
                  false whenever a filter is active, and would quietly absorb freshness-withheld
                  assets — which are permitted, and which the empty state names separately. */}
              <span className="pager-copy tabular-nums">
                Showing {offset + 1}–{offset + result.hits.length} of {result.total} matching assets
              </span>
              <button
                type="button"
                className="ghost"
                onClick={() => goToPage(offset - SEARCH_PAGE_SIZE)}
                disabled={offset === 0}
              >
                ← Previous
              </button>
              {/* Absent, not disabled, when there is nothing after this page: a control that can
                  never do anything is noise, and its absence is the clearest possible statement
                  that this is the end of the set. */}
              {offset + result.hits.length < result.total && (
                <button
                  type="button"
                  className="ghost"
                  onClick={() => goToPage(offset + SEARCH_PAGE_SIZE)}
                >
                  Next →
                </button>
              )}
            </nav>
          )}

          {!error && hasHits && effectiveView === 'list' && (
            <ul className="rows">
              {result.hits.map(hit => (
                <SearchHitRow
                  key={`${hit.catalog_source}:${hit.object_ref}`}
                  hit={hit}
                  onOpen={openDetails}
                  onExplore={jumpToGraph}
                  onSuggested={openSuggested}
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
              {/* The anchor used to be named in this sentence; the graph's own context bar names
                  it properly now, with its kind, wording and actions. */}
              <LineageView
                key={`${graphAnchor.catalog_source}:${graphAnchor.object_ref}`}
                anchor={graphAnchor}
                onBackToResults={() => setView('list')}
              />
            </>
          )}
        </div>
      </div>
    </section>
  )
}
