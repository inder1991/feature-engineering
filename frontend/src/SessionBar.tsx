import { useSyncExternalStore } from 'react'
import { getSession, setSession, subscribe } from './session'

const ROLES = ['data_owner', 'pii_reader', 'restricted_reader']

// Rail-footer session controls. The chips are real checkboxes (visually hidden inputs) so
// assistive tech and tests keep the checkbox role; the label renders the pressed chip.
export function SessionBar() {
  const session = useSyncExternalStore(subscribe, getSession)
  const toggle = (role: string) => {
    setSession({
      ...session,
      roles: session.roles.includes(role)
        ? session.roles.filter(r => r !== role)
        : [...session.roles, role],
    })
  }
  return (
    <div className="session">
      <span className="micro-label">Dev session (stub auth)</span>
      <input
        className="session-user"
        aria-label="user"
        value={session.user}
        onChange={e => setSession({ ...session, user: e.target.value })}
      />
      <div className="session-roles">
        {ROLES.map(role => {
          const on = session.roles.includes(role)
          return (
            <label key={role} className={on ? 'role-chip on' : 'role-chip'}>
              <input
                type="checkbox"
                className="visually-hidden"
                checked={on}
                onChange={() => toggle(role)}
              />
              {role}
            </label>
          )
        })}
      </div>
    </div>
  )
}
