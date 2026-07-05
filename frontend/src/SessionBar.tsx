import { useSyncExternalStore } from 'react'
import { getSession, setSession, subscribe } from './session'

const ROLES = ['data_owner', 'pii_reader', 'restricted_reader']

export function SessionBar() {
  const session = useSyncExternalStore(subscribe, getSession)
  const toggle = (role: string) =>
    setSession({
      ...session,
      roles: session.roles.includes(role)
        ? session.roles.filter(r => r !== role)
        : [...session.roles, role],
    })
  return (
    <div className="session-bar">
      <span className="session-note">Dev session (stub auth)</span>
      <input
        aria-label="user"
        value={session.user}
        onChange={e => setSession({ ...session, user: e.target.value })}
      />
      {ROLES.map(role => (
        <label key={role}>
          <input
            type="checkbox"
            checked={session.roles.includes(role)}
            onChange={() => toggle(role)}
          />
          {role}
        </label>
      ))}
    </div>
  )
}
