// Tiny hash router: '#/search' or '#/review?source=deposits'. Empty or unknown hash
// resolves to 'overview' so every entry point lands on the orientation screen.
import { useCallback, useMemo, useSyncExternalStore } from 'react'

export type Route =
  | 'overview' | 'upload' | 'search' | 'review' | 'semantics' | 'workbench' | 'registry'
  | 'integrations' | 'governance' | 'dashboard' | 'gate' | 'asset'

// 'asset' is the catalog asset-detail screen (Delivery G). It carries source + object_ref via the
// existing params mechanism (a Details action on a search hit navigates('asset', {source,
// object_ref})); object_ref rides the query string, so URLSearchParams handles its dots/slashes.
const ROUTES: readonly string[] =
  ['overview', 'upload', 'search', 'review', 'semantics', 'workbench', 'registry',
    'integrations', 'governance', 'dashboard', 'asset']

// The internal gate console (Phase 3C.1) is an authority-only surface behind its own Vite flag.
// Checked at CALL time (not module scope) so vi.stubEnv works per-test, mirroring the
// WorkbenchScreen intent-flag helpers. With the flag off, '#/gate' parses like any unknown hash
// — the route falls back to 'overview' and the screen is unreachable.
export function gateConsoleEnabled(): boolean {
  return import.meta.env.VITE_INTENT_GATE_CONSOLE === '1'
}

export function parseHash(hash: string): { route: Route; params: URLSearchParams } {
  const raw = hash.replace(/^#\/?/, '')
  const q = raw.indexOf('?')
  const path = q === -1 ? raw : raw.slice(0, q)
  const query = q === -1 ? '' : raw.slice(q + 1)
  const known = ROUTES.includes(path) || (path === 'gate' && gateConsoleEnabled())
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
