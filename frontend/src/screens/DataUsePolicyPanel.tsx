// DATA-USE POLICIES — may a personal-data concept be used to BUILD a feature, and for what
// purpose (verified-interfaces D14; DEFERRED-WORK A.34).
//
// WHY THIS PANEL EXISTS. The feature flow refuses a personal-data operand with "no active
// personal-data use policy covers X … a governance owner must declare one under Governance ->
// Data-use policies". Until this panel there was no such place, so five shipped financial-crime
// recipes were permanently refused and the reviewer was sent somewhere that did not exist.
//
// Four rules it follows:
//   * NO-BLOCKED FRAMING. A concept with no policy is NOT an error, NOT a gap and NOT a failure —
//     nobody has had to decide about it yet, and the row says exactly that in neutral ink. The
//     three states are rendered as three states: not approved for feature use / approved, with its
//     purpose and approver / withdrawn. "Withdrawn" is never collapsed into "not approved",
//     because somebody decided that, and the record of who is the whole control.
//   * SINGLE APPROVER, and the panel says so — TOGETHER WITH THE SCOPE, because the two decisions
//     compound. One platform-admin declares purpose and the policy is live; there is no second
//     signature to wait for and no pending state to render (an explicit user decision, D14). And a
//     policy licenses a CONCEPT for the whole platform, so that one signature covers every catalog
//     that exists and every catalog uploaded afterwards. Either decision is defensible on its own;
//     their product is a fact the approver has to be told at the moment they decide, which is why
//     it is in the approve form and not only in the panel's introduction.
//   * CAS IN THE BODY. `pointer_version` is echoed back exactly as it was read (0 claims the first
//     declaration). A 409 means someone else decided first — the panel reloads and asks for a
//     re-read rather than silently retrying over their decision.
//   * REVOKING ASKS FIRST. Approving licenses a feature; revoking takes one away from whoever is
//     using it. The confirmation dialog names the concept and says what happens next, because an
//     accidental single click here breaks a running model rather than merely being undone.
//
// Only a platform-admin may write, and the server enforces it. A caller without the claim still
// SEES everything (transparency is the point — the refusal is shown to them too); their write
// attempt is refused by the server and reported honestly rather than the panel pretending to know
// their roles.
import { useCallback, useEffect, useState } from 'react'
import type { FormEvent } from 'react'
import {
  ApiError, approveDataUsePolicy, getDataUsePolicies, revokeDataUsePolicy,
} from '../api'
import type { DataUsePolicyListing, DataUsePolicyState } from '../api'

// Neutral words for each state. NONE is the one that matters: it must read as "nobody decided",
// never as "blocked", "missing", "required" or "failed".
const STATE_LABEL: Record<string, string> = {
  none: 'Not approved for feature use',
  active: 'Approved for feature use',
  revoked: 'Approval withdrawn',
}

function humanDate(iso: string | null): string {
  if (!iso) return ''
  const at = new Date(iso)
  return Number.isNaN(at.getTime()) ? '' : at.toLocaleDateString()
}

function conceptLabel(name: string): string {
  return name.replaceAll('_', ' ')
}

// PolicyRow is declared at MODULE scope, not inside the panel. A component defined inside a
// render body gets a NEW function identity every render, so React unmounts and remounts its whole
// subtree — which here meant the purpose input lost focus after every single keystroke. The props
// are explicit for the same reason: what a row needs is a short list, and passing it is cheaper
// than the bug closure capture hides.
function PolicyRow({
state, editing, confirming, purpose, busy, bounds,
onPurposeChange, onStartApproval, onCancelApproval, onSubmitApproval,
onStartRevocation, onCancelRevocation, onSubmitRevocation,
}: {
state: DataUsePolicyState
editing: boolean
confirming: boolean
purpose: string
busy: boolean
bounds: { min: number; max: number }
onPurposeChange: (value: string) => void
onStartApproval: () => void
onCancelApproval: () => void
onSubmitApproval: (e: FormEvent) => void
onStartRevocation: () => void
onCancelRevocation: () => void
onSubmitRevocation: () => void
}) {
return (
    <li className="dup-row" data-testid={`dup-${state.concept_name}`}>
      <div className="dup-head">
        <span className="mono dup-concept">{state.concept_name}</span>
        <span className="dup-state" data-status={state.status}>
          {STATE_LABEL[state.status] ?? state.status}
        </span>
      </div>
      {state.description && <p className="hint dup-desc">{state.description}</p>}

      {/* TWO PEOPLE, TWO QUESTIONS, TWO LABELS. `approved_by` is who first authored this content;
          `declared_by` is who made it the concept's current answer. They differ routinely and for
          a designed reason: the approver is OUTSIDE content identity, so re-approving an identical
          purpose resolves back to the ORIGINAL revision — which means a policy can read
          "approved by A" while B is the person who turned it back on this morning. The old row
          rendered `declared_by ?? approved_by` under one label, so it showed ONE of them under a
          word that was true of the other. The revision id is here for the same reason: it is what
          a governed contract records, and an auditor tracing one needs to see it on the surface
          that shows the decision. */}
      {state.status !== 'none' && (
        <dl className="dup-detail">
          <dt>Purpose</dt>
          <dd>{state.purpose}</dd>
          {state.approved_by && (
            <>
              <dt>First approved by</dt>
              <dd>
                {state.approved_by}
                {humanDate(state.approved_at) && ` · ${humanDate(state.approved_at)}`}
              </dd>
            </>
          )}
          {state.declared_by && (
            <>
              <dt>{state.status === 'active' ? 'Made current by' : 'Withdrawn by'}</dt>
              <dd>
                {state.declared_by}
                {humanDate(state.updated_at) && ` · ${humanDate(state.updated_at)}`}
              </dd>
            </>
          )}
          {state.revision_id && (
            <>
              <dt>Revision</dt>
              <dd className="mono dup-revision">{state.revision_id}</dd>
            </>
          )}
        </dl>
      )}

      {editing ? (
        <form className="dup-form" onSubmit={onSubmitApproval}>
          <label htmlFor={`dup-purpose-${state.concept_name}`}>
            What will this data be used for?
          </label>
          <input
            id={`dup-purpose-${state.concept_name}`}
            value={purpose}
            minLength={bounds.min}
            maxLength={bounds.max}
            placeholder="AML transaction monitoring"
            onChange={e => onPurposeChange(e.target.value)}
          />
          {/* THE BLAST RADIUS, WHERE THE CLICK HAPPENS. A policy licenses a CONCEPT for the whole
              platform, and one signature is the whole control (D14). Those two decisions are
              defensible separately and compound into something a person has to be told at the
              moment they decide: this approval covers every catalog that exists and every catalog
              uploaded afterwards, and nobody else will be asked. Stating it in the panel's prose
              or in a design doc is not the same as stating it here. */}
          <p className="callout callout--warn dup-scope" data-testid="dup-scope-warning">
            This allows <span className="mono">{state.concept_name}</span> in features across{' '}
            <strong>all catalogs, now and in future uploads</strong> — one approval is sufficient.
            It is recorded permanently with your name, and there is no second signature to wait for.
          </p>
          <div className="dup-actions">
            <button type="submit" className="btn" disabled={busy || purpose.trim().length < bounds.min}>
              Approve for feature use
            </button>
            <button
              type="button"
              className="btn q-ghost"
              disabled={busy}
              onClick={onCancelApproval}
            >
              Cancel
            </button>
          </div>
        </form>
      ) : confirming ? (
        <div className="callout callout--warn dup-confirm" role="alertdialog" aria-label={
          `Withdraw approval for ${conceptLabel(state.concept_name)}`}>
          <div className="callout-body">
            <p>
              <strong>Withdraw approval for {state.concept_name}?</strong> Features built from it
              will be refused again from the next check onwards. The approval stays on the record
              — this adds a decision, it does not erase one.
            </p>
            <div className="dup-actions">
              <button
                type="button"
                className="btn btn--danger"
                disabled={busy}
                onClick={onSubmitRevocation}
              >
                Withdraw approval
              </button>
              <button
                type="button"
                className="btn q-ghost"
                disabled={busy}
                onClick={onCancelRevocation}
              >
                Keep it approved
              </button>
            </div>
          </div>
        </div>
      ) : (
        <div className="dup-actions">
          {state.status === 'active' ? (
            <button
              type="button"
              className="btn q-ghost"
              disabled={busy}
              onClick={onStartRevocation}
            >
              Withdraw approval
            </button>
          ) : (
            <button
              type="button"
              className="btn q-ghost"
              disabled={busy}
              onClick={onStartApproval}
            >
              Approve for feature use
            </button>
          )}
        </div>
      )}
    </li>
  )
}


export function DataUsePolicyPanel() {
  const [listing, setListing] = useState<DataUsePolicyListing | null>(null)
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<string | null>(null)   // concept being approved
  const [purpose, setPurpose] = useState('')
  const [confirming, setConfirming] = useState<string | null>(null)   // concept being revoked

  const load = useCallback(async () => {
    try {
      setListing(await getDataUsePolicies())
    } catch (e) {
      setNotice(e instanceof ApiError ? e.detail : String(e))
    }
  }, [])

  useEffect(() => { void load() }, [load])

  // A FAILED LISTING MUST SAY SO. `if (!listing) return null` rendered nothing whether the request
  // was in flight or had failed, so the notice the catch block sets was written to a component that
  // never drew it — the one error path a reader of this panel most needs (they were sent here by a
  // refusal that names it) was the one that showed a blank page. Absence of a listing plus a notice
  // is a failure and is reported; absence of both is still "not here yet", which stays silent.
  if (!listing) {
    if (!notice) return null
    return (
      <section className="adg-section dup" data-testid="data-use-policies">
        <h2>Data-use policies</h2>
        <p role="status" className="callout callout--warn" data-testid="dup-notice">
          {notice}
        </p>
        <p className="hint">
          The list of personal-data concepts could not be loaded, so nothing here can be trusted to
          be complete. Nothing has been approved or withdrawn by opening this page.
        </p>
      </section>
    )
  }

  const bounds = listing.purpose_bounds
  const declared = listing.concepts.filter(c => c.status !== 'none')
  const undeclared = listing.concepts.filter(c => c.status === 'none')

  async function submitApproval(e: FormEvent, state: DataUsePolicyState) {
    e.preventDefault()
    setBusy(true)
    setNotice('')
    try {
      await approveDataUsePolicy(state.concept_name, {
        // Echoed EXACTLY as read. 0 is not "no version" — it is the claim that nothing was
        // declared when this form was opened, and the server refuses it if something appeared.
        expectedPointerVersion: state.pointer_version,
        purpose: purpose.trim(),
      })
      setEditing(null)
      setPurpose('')
      setNotice(`${conceptLabel(state.concept_name)} is now approved for feature use.`)
      await load()
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotice('Someone else decided this while you were writing — nothing was overwritten. '
          + 'Their decision is shown above; re-read it, then decide again.')
        await load()
      } else {
        setNotice(err instanceof ApiError ? err.detail : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  async function submitRevocation(state: DataUsePolicyState) {
    setBusy(true)
    setNotice('')
    try {
      await revokeDataUsePolicy(state.concept_name, {
        expectedPointerVersion: state.pointer_version,
      })
      setConfirming(null)
      setNotice(`Approval for ${conceptLabel(state.concept_name)} withdrawn. Features that used `
        + 'it are refused again from now on; the approval itself stays on the record.')
      await load()
    } catch (err) {
      if (err instanceof ApiError && err.status === 409) {
        setNotice('Someone else decided this while you were reading — nothing was overwritten.')
        await load()
      } else {
        setNotice(err instanceof ApiError ? err.detail : String(err))
      }
    } finally {
      setBusy(false)
    }
  }

  // One place that binds a row to the panel's state, so the two lists cannot drift apart.
  function renderRow(state: DataUsePolicyState) {
    return (
      <PolicyRow
        key={state.concept_name}
        state={state}
        editing={editing === state.concept_name}
        confirming={confirming === state.concept_name}
        purpose={purpose}
        busy={busy}
        bounds={bounds}
        onPurposeChange={setPurpose}
        onStartApproval={() => { setEditing(state.concept_name); setPurpose(state.purpose ?? '') }}
        onCancelApproval={() => { setEditing(null); setPurpose('') }}
        onSubmitApproval={e => void submitApproval(e, state)}
        onStartRevocation={() => setConfirming(state.concept_name)}
        onCancelRevocation={() => setConfirming(null)}
        onSubmitRevocation={() => void submitRevocation(state)}
      />
    )
  }

  return (
    <section className="adg-section dup" data-testid="data-use-policies">
      <h2>Data-use policies</h2>
      <p className="hint">
        Personal-data concepts may be used to build features only where someone has said what they
        will be used for. Nothing here is a fault report: a concept with no policy is simply one
        nobody has had to decide about yet. One approval from a platform administrator is enough,
        and it is recorded with your name and the date.
      </p>

      {notice && (
        <p role="status" className="callout callout--accent" data-testid="dup-notice">
          {notice}
        </p>
      )}

      {declared.length > 0 && (
        <>
          <h3>Decided</h3>
          <ul className="dup-list" data-testid="dup-decided">
            {declared.map(state => renderRow(state))}
          </ul>
        </>
      )}

      <h3>Not yet decided</h3>
      {undeclared.length === 0 ? (
        <p className="hint" data-testid="dup-none-undecided">
          Every personal-data concept has been decided on.
        </p>
      ) : (
        <ul className="dup-list" data-testid="dup-undecided">
          {undeclared.map(state => renderRow(state))}
        </ul>
      )}
    </section>
  )
}
