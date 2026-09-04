// Tiny hash router: '#/search' or '#/review?source=deposits'. Empty or unknown hash
// resolves to 'overview' so every entry point lands on the orientation screen.
import { useCallback, useMemo, useSyncExternalStore } from 'react'

export type Route =
  | 'overview' | 'upload' | 'search' | 'review' | 'semantics' | 'workbench' | 'registry'
  | 'integrations' | 'governance' | 'dashboard' | 'gate' | 'asset' | 'suggested'
  | 'analysis' | 'entity-map' | 'recipes' | 'materialization' | 'feature-execution'
  | 'runs' | 'code-generation'

// 'asset' is the catalog asset-detail screen (Delivery G). It carries source + object_ref via the
// existing params mechanism (a Details action on a search hit navigates('asset', {source,
// object_ref})); object_ref rides the query string, so URLSearchParams handles its dots/slashes.
// 'suggested' is the read-only per-table suggested-features sheet (P4). Same shape: a detail
// destination reached with source + table in the hash, deliberately absent from the left rail.
// 'runs' is the feature-run spine: a rail destination ('#/runs', the list) that also owns the one
// path-param route ('#/runs/<id>', the detail). It is UNFLAGGED because GET /feature-runs is
// always-on — it derives its answers from stores that already exist and writes nothing.
const ROUTES: readonly string[] =
  ['overview', 'upload', 'search', 'review', 'semantics', 'workbench', 'registry',
    'integrations', 'governance', 'dashboard', 'asset', 'suggested', 'analysis', 'recipes',
    'runs']

// The internal gate console (Phase 3C.1) is an authority-only surface behind its own Vite flag.
// Checked at CALL time (not module scope) so vi.stubEnv works per-test, mirroring the
// WorkbenchScreen intent-flag helpers. With the flag off, '#/gate' parses like any unknown hash
// — the route falls back to 'overview' and the screen is unreachable.
export function gateConsoleEnabled(): boolean {
  return import.meta.env.VITE_INTENT_GATE_CONSOLE === '1'
}

// Entity map v0 (ingestion-richness Task 3D) rides its own Vite flag, same call-time pattern as the
// gate console. The plan gates it with the profile read-model flag family; that family has no
// frontend member yet (the profiles plan is unimplemented), so this flag is its first — fold it in
// when the family lands. Flag-off, '#/entity-map' parses like any unknown hash: absent, not broken.
export function entityMapEnabled(): boolean {
  return import.meta.env.VITE_ENTITY_MAP === '1'
}

// The materialization run sheet (Phase G, D4) rides its own Vite flag, same call-time pattern. It
// mirrors the SERVER's switch: `FEATUREGEN_MATERIALIZE_ENABLED` is default-OFF and every
// /materialization-runs route 404s while it is off, so a reachable screen with an unreachable API
// behind it would be a surface that can only ever show an error. Flag-off, '#/materialization'
// parses like any unknown hash: absent, not broken.
export function materializationRunsEnabled(): boolean {
  return import.meta.env.VITE_MATERIALIZATION_RUNS === '1'
}

// S11's execution workspace (generate / code view / verify / publish) rides the SAME server switch
// as the materialization routes — `/feature-execution/...` 404s while
// `FEATUREGEN_MATERIALIZE_ENABLED` is off — so it gets its own call-time Vite flag for the same
// reason: a reachable screen with an unreachable API behind it is a surface that can only ever show
// an error. Flag-off, '#/feature-execution' parses like any unknown hash: absent, not broken.
export function featureExecutionEnabled(): boolean {
  return import.meta.env.VITE_FEATURE_EXECUTION === '1'
}

// Step 5b's generation workspace mirrors the SERVER's generation switch the same way:
// every /code-generation-jobs route 404s while `FEATUREGEN_GENERATION_V2_ENABLED` is off, so a
// reachable screen with an unreachable API behind it would be a surface that can only ever show
// an error. Flag-off, '#/code-generation' parses like any unknown hash: absent, not broken.
export function codeGenerationEnabled(): boolean {
  return import.meta.env.VITE_CODE_GENERATION === '1'
}

// decodeURIComponent THROWS a URIError on a malformed escape ('%zz'), and parseHash runs inside a
// render — an unguarded call would turn a corrupt link into a blank app. URLSearchParams tolerates
// the same input by leaving it literal, so the path param behaves identically: pass the raw
// segment through and let the run route answer 404 for an id that does not exist.
function decodePathSegment(segment: string): string {
  try {
    return decodeURIComponent(segment)
  } catch {
    return segment
  }
}

export function parseHash(hash: string): { route: Route; params: URLSearchParams } {
  const raw = hash.replace(/^#\/?/, '')
  const q = raw.indexOf('?')
  const path = q === -1 ? raw : raw.slice(0, q)
  const query = q === -1 ? '' : raw.slice(q + 1)
  // The runs detail is the one path-param route: '#/runs/<opaque id>'. The id is opaque
  // (grun_/fgr_ alike) and percent-decoded; everything else stays query-string params.
  if (path.startsWith('runs/')) {
    const p = new URLSearchParams(query)
    p.set('run_id', decodePathSegment(path.slice('runs/'.length)))
    return { route: 'runs', params: p }
  }
  const known = ROUTES.includes(path)
    || (path === 'gate' && gateConsoleEnabled())
    || (path === 'entity-map' && entityMapEnabled())
    || (path === 'materialization' && materializationRunsEnabled())
    || (path === 'feature-execution' && featureExecutionEnabled())
    || (path === 'code-generation' && codeGenerationEnabled())
  const route = known ? (path as Route) : 'overview'
  return { route, params: new URLSearchParams(query) }
}

function subscribeToHash(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

function readHash(): string {
  return window.location.hash
}

export function useHashRoute(): {
  route: Route
  // Accepts a plain record OR a URLSearchParams: the latter carries repeated params
  // (?source=a&source=b) that a Record cannot express, for faceted-search deep links. Reads use
  // params.getAll(key) for the repeated groups.
  navigate: (r: Route, params?: Record<string, string> | URLSearchParams) => void
  params: URLSearchParams
} {
  const hash = useSyncExternalStore(subscribeToHash, readHash)
  const { route, params } = useMemo(() => parseHash(hash), [hash])
  const navigate = useCallback((r: Route, next?: Record<string, string> | URLSearchParams) => {
    // new URLSearchParams(next) copies a passed URLSearchParams verbatim (duplicates preserved)
    // and builds one from a record; toString() keeps insertion order for a stable, shareable hash.
    const query = next ? new URLSearchParams(next).toString() : ''
    window.location.hash = `#/${r}${query ? `?${query}` : ''}`
    // Browsers fire hashchange asynchronously (and not at all if the hash is unchanged);
    // dispatch synchronously so the route store is consistent right after navigate().
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  }, [])
  return { route, navigate, params }
}
