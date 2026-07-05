// Tiny hash router: '#/search' or '#/review?source=deposits'. Empty or unknown hash
// resolves to 'overview' so every entry point lands on the orientation screen.
import { useCallback, useMemo, useSyncExternalStore } from 'react'

export type Route = 'overview' | 'upload' | 'search' | 'review' | 'workbench'

const ROUTES: readonly string[] = ['overview', 'upload', 'search', 'review', 'workbench']

export function parseHash(hash: string): { route: Route; params: URLSearchParams } {
  const raw = hash.replace(/^#\/?/, '')
  const q = raw.indexOf('?')
  const path = q === -1 ? raw : raw.slice(0, q)
  const query = q === -1 ? '' : raw.slice(q + 1)
  const route = ROUTES.includes(path) ? (path as Route) : 'overview'
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
  navigate: (r: Route, params?: Record<string, string>) => void
  params: URLSearchParams
} {
  const hash = useSyncExternalStore(subscribeToHash, readHash)
  const { route, params } = useMemo(() => parseHash(hash), [hash])
  const navigate = useCallback((r: Route, next?: Record<string, string>) => {
    const query = next ? new URLSearchParams(next).toString() : ''
    window.location.hash = `#/${r}${query ? `?${query}` : ''}`
    // Browsers fire hashchange asynchronously (and not at all if the hash is unchanged);
    // dispatch synchronously so the route store is consistent right after navigate().
    window.dispatchEvent(new HashChangeEvent('hashchange'))
  }, [])
  return { route, navigate, params }
}
