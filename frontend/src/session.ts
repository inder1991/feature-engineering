// Dev-session stub (spec build-step 1): mirrors the API's X-User/X-Roles header auth until a
// real IdP lands. One external store so the api client and the SessionBar share state.
export interface Session {
  user: string
  roles: string[]
}

let current: Session = { user: 'dev', roles: ['data_owner'] }
const listeners = new Set<() => void>()

export function getSession(): Session {
  return current
}

export function setSession(next: Session): void {
  current = next
  listeners.forEach(l => l())
}

export function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}
