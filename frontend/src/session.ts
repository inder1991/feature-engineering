import { useSyncExternalStore } from 'react'

// Dev-session stub (spec build-step 1): mirrors the API's X-User/X-Roles header auth until a
// real IdP lands. One external store so the api client and the SessionBar share state.
export interface Session {
  user: string
  roles: string[]
}

// The dev session stands in for real auth. It carries BOTH roles because the app it ships with
// needs both: `data_owner` uploads a catalog, `feature_engineer` carries `feature:generate`, which
// every /targets route requires. With `data_owner` alone a fresh session 403s on the whole
// prediction-target screen — a permissions wall that reads as a broken feature.
let current: Session = { user: 'dev', roles: ['data_owner', 'feature_engineer'] }
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

// The identity a server response was READ UNDER: the principal plus its visibility claims. Roles
// are sorted so re-ordering the same claims is not a change; the user is included because a
// response is private to the principal, not merely to the role set.
export function identityKey(session: Session): string {
  return JSON.stringify({ user: session.user, roles: [...session.roles].sort() })
}

// Read-scoped surfaces MUST depend on this rather than on the URL alone. Grounding is read-scope
// dependent — a broader caller can change which column wins a binding, and a hidden operand hides
// a whole suggestion — so a result fetched under one identity is not a valid answer under another.
// Returning a STRING (not the session object) keeps it usable as an effect dependency, and equal
// strings compare equal, which is what `useSyncExternalStore` needs from a snapshot.
export function useIdentityKey(): string {
  return useSyncExternalStore(subscribe, () => identityKey(current))
}
