import { useSyncExternalStore } from 'react'
import { getSession, setSession, subscribe } from './session'

// Two axes of the RBAC model, both exercisable from the dev session (see identity/permissions.py
// + read_scope). Functional roles first (what OPERATIONS you may perform: catalog_viewer and
// feature_engineer both grant feature:read, so the feature-lineage layer and the Registry render),
// then data-sensitivity roles (which sensitive COLUMNS you may see), then `platform-admin` — the
// governance confirmer role the join/table-fact review + divergence screens require (the stub CAN
// assert it: those routes check the role CLAIM, not a real authenticated principal). IAM `/admin`
// routes still need a real principal and stay out. The rail chips wrap (.session-roles: flex-wrap).
const ROLES = ['catalog_viewer', 'data_owner', 'feature_engineer', 'pii_reader', 'restricted_reader',
  'platform-admin']

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
