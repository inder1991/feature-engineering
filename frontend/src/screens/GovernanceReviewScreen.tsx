import { type FormEvent, type ReactNode, useRef, useState } from 'react'
import {
  ApiError,
  type JoinProposal,
  type RejectCategory,
  REJECT_CATEGORIES,
  confirmJoin,
  listJoinProposals,
  rejectJoin,
} from '../api'

// Governance review: the two-admin confirmation surface over the joins Pass C discovered.
// Evidence-forward cards (score demoted to advisory), a consequence line, a matched-on-metadata
// caution, and a what-to-verify checklist that GATES the Approve button. Reject is structured
// (category + optional note). Follows ReviewQueueScreen's shape: source input -> load() ->
// per-card action handlers -> a session-local decided Map (the durable state lives server-side;
// a reload refetches the open queue, which no longer contains what was decided).

// A decision made this session, kept for display on the (now-closed) card.
interface Decision {
  badge: string
  tone: 'gj-partial' | 'gj-verified' | 'gj-rejected'
  note: string
}

const STATUS_BADGE: Record<JoinProposal['status'], { label: string; tone: string }> = {
  PROPOSED: { label: 'proposed', tone: 'gj-proposed' },
  PARTIALLY_CONFIRMED: { label: 'awaiting 2nd', tone: 'gj-partial' },
}

function categoryLabel(category: RejectCategory): string {
  return category.replaceAll('_', ' ')
}

function approvedNote(status: string, projection: string): string {
  if (status === 'VERIFIED') {
    return projection === 'projected'
      ? 'Verified — projected to an operational graph edge. The planner can use it (revocable).'
      : 'Verified — the operational projection is deferred to the next caught-up ingest.'
  }
  return 'You approved — a different, second admin must confirm before it goes live.'
}

export function GovernanceReviewScreen() {
  const [source, setSource] = useState('')
  const [proposals, setProposals] = useState<JoinProposal[] | null>(null)
  const [loadedSource, setLoadedSource] = useState('')
  const [error, setError] = useState('')
  // Conflict banner (409): survives the reload that follows it, unlike `error`.
  const [notice, setNotice] = useState('')
  // Session-only DISPLAY state for cards decided this session, keyed by fact_key. The durable
  // state is server-side; clearing on (re)load is correct — a decided join leaves the open queue.
  const [decided, setDecided] = useState<Map<string, Decision>>(new Map())
  // Bumped per successful load: keys the cards so a reload REMOUNTS them (a 409 means the
  // proposal changed under the reviewer — stale checklist ticks must not survive).
  const [generation, setGeneration] = useState(0)

  // Monotonic id per load(): a late response from an older load must never overwrite newer data.
  const loadSeq = useRef(0)

  async function load(name: string) {
    if (!name.trim()) return
    const id = ++loadSeq.current
    setError('')
    setNotice('')
    try {
      const res = await listJoinProposals(name.trim())
      if (id !== loadSeq.current) return
      setProposals(res.proposals)
      setLoadedSource(name.trim())
      setDecided(new Map())
      setGeneration(g => g + 1)
    } catch (err) {
      if (id !== loadSeq.current) return
      setProposals(null)
      setLoadedSource('')
      setDecided(new Map())
      setError(err instanceof ApiError ? err.detail : String(err))
    }
  }

  function submit(e: FormEvent) {
    e.preventDefault()
    void load(source)
  }

  function onDecided(factKey: string, decision: Decision) {
    setDecided(prev => new Map(prev).set(factKey, decision))
  }

  // 409 = the proposal moved since it was loaded (already-approved-by-you, CAS-stale). Show the
  // server's detail and reload the current queue — never blind-retry the command.
  async function onConflict(detail: string) {
    await load(loadedSource)
    setNotice(detail)
  }

  return (
    <section>
      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="governance-source">Source</label>
          <input
            id="governance-source"
            value={source}
            onChange={e => setSource(e.target.value)}
            placeholder="source name"
          />
        </div>
        <button type="submit" className="btn">
          Load proposals
        </button>
      </form>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {notice && (
        <p role="alert" className="error">
          {notice}
        </p>
      )}
      {proposals?.length === 0 && (
        <p className="empty" role="status">
          No open join proposals for this source.
        </p>
      )}
      {proposals && proposals.length > 0 && (
        <>
          <div className="callout callout--accent">
            <div className="callout-body">
              <p>
                <strong>Approve deliberately.</strong> The match score is advisory, not a verdict
                — a plausible-but-wrong join can score high, which is exactly why two different
                admins must confirm every join before the planner can use it. Nothing here touches
                the live graph until it is verified.
              </p>
            </div>
          </div>
          <p className="tabular-nums" role="status">
            {proposals.length} open proposal{proposals.length === 1 ? '' : 's'} · {decided.size}{' '}
            decided this session
          </p>
          <ul className="rows">
            {proposals.map(p => {
              const decision = decided.get(p.fact_key)
              if (decision) {
                return (
                  <li className="row q-item q-item--resolved" key={p.fact_key}>
                    <div className="q-head">
                      <span className="mono">
                        Join · {p.from.table}.{p.from.column} → {p.to.table}.{p.to.column}
                      </span>
                      <span className={`badge ${decision.tone}`}>{decision.badge}</span>
                    </div>
                    <p className="q-note">{decision.note}</p>
                  </li>
                )
              }
              return (
                <JoinCard
                  key={`${generation}:${p.fact_key}`}
                  proposal={p}
                  onDecided={onDecided}
                  onConflict={onConflict}
                />
              )
            })}
          </ul>
        </>
      )}
    </section>
  )
}

interface JoinCardProps {
  proposal: JoinProposal
  onDecided: (factKey: string, decision: Decision) => void
  onConflict: (detail: string) => void
}

function JoinCard({ proposal: p, onDecided, onConflict }: JoinCardProps) {
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [noteDraft, setNoteDraft] = useState('')
  const [rejectOpen, setRejectOpen] = useState(false)
  const [category, setCategory] = useState<RejectCategory | null>(null)
  const [rejectNote, setRejectNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [cardError, setCardError] = useState('')

  const badge = STATUS_BADGE[p.status]
  const score = typeof p.evidence.score === 'number' ? p.evidence.score : null
  const cardinality = p.cardinality ?? 'unknown'
  const firstApproval = p.status === 'PARTIALLY_CONFIRMED' ? p.approvals[0] : undefined
  const approverName = firstApproval ? (firstApproval.display_name ?? firstApproval.subject) : null

  // The what-to-verify checklist that gates Approve: 4 baseline items, plus one derived item per
  // positive signal — but ONLY when the evidence actually parsed (a missing/invalid record must
  // not silently shrink what the reviewer confirms; the baseline still gates).
  const signals =
    p.evidence_parse_status === 'parsed' || p.evidence_parse_status === 'partial'
      ? (p.evidence.positive_signals ?? [])
      : []
  const items: ReactNode[] = [
    <>
      I reviewed the join <strong>direction</strong> —{' '}
      <span className="mono">
        {p.from.table}.{p.from.column}
      </span>{' '}
      joins into{' '}
      <span className="mono">
        {p.to.table}.{p.to.column}
      </span>
      , not the reverse.
    </>,
    <>
      I reviewed the <strong>cardinality</strong> (<span className="mono">{cardinality}</span>) —
      it matches how these tables actually relate.
    </>,
    <>
      I understand this join was <strong>matched on metadata, not value-verified</strong> — no
      sample rows were compared.
    </>,
    <>
      I confirm this join becomes <strong>operational</strong> once a second admin approves — the
      feature planner will traverse it.
    </>,
    ...signals.map(s => (
      <>
        Signal <span className="mono">{s.signal_name.replaceAll('_', ' ')}</span>{' '}
        <span className="gj-signal-w">(+{s.score_delta})</span> — I checked it actually holds
        here.
      </>
    )),
  ]
  const allChecked = checked.size === items.length

  function toggle(i: number) {
    setChecked(prev => {
      const next = new Set(prev)
      if (next.has(i)) next.delete(i)
      else next.add(i)
      return next
    })
  }

  async function approve() {
    setBusy(true)
    setCardError('')
    try {
      const note = noteDraft.trim()
      const res = await confirmJoin(p.fact_key, note ? { note } : {})
      onDecided(p.fact_key, {
        badge: res.governance_status === 'VERIFIED' ? 'verified · live' : 'awaiting 2nd',
        tone: res.governance_status === 'VERIFIED' ? 'gj-verified' : 'gj-partial',
        note: approvedNote(res.governance_status, res.operational_projection),
      })
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        onConflict(e.detail) // reloads the list; this card remounts with fresh data
        return
      }
      setCardError(e instanceof ApiError ? e.detail : String(e))
      setBusy(false)
    }
  }

  async function reject() {
    if (!category) return
    setBusy(true)
    setCardError('')
    try {
      const note = rejectNote.trim()
      await rejectJoin(p.fact_key, note ? { category, note } : { category })
      onDecided(p.fact_key, {
        badge: `rejected · ${categoryLabel(category)}`,
        tone: 'gj-rejected',
        note: `Rejected (${categoryLabel(category)}) — recorded for audit; the category feeds back into re-proposal.`,
      })
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        onConflict(e.detail)
        return
      }
      setCardError(e instanceof ApiError ? e.detail : String(e))
      setBusy(false)
    }
  }

  return (
    <li className="row q-item">
      <div className="q-head">
        <span className="mono gj-kind">Join · {p.from.column}</span>
        <span className={`badge ${badge.tone}`}>{badge.label}</span>
        <span className="gj-score">
          {score !== null ? (
            <>
              match <b>{score}</b>/100 · advisory
            </>
          ) : (
            <>score unavailable ({p.evidence_parse_status})</>
          )}
        </span>
      </div>

      <div className="gj-join">
        <div className="gj-endp">
          <span className="k">from</span>
          <span className="tbl">{p.from.table}</span>
          <span className="col">.{p.from.column}</span>
        </div>
        <div className="gj-arrow" aria-hidden="true">
          <span className="g">→</span>
          <span className="cr">{cardinality}</span>
        </div>
        <div className="gj-endp">
          <span className="k">to</span>
          <span className="tbl">{p.to.table}</span>
          <span className="col">.{p.to.column}</span>
        </div>
      </div>

      {firstApproval && (
        <div className="gj-prior">
          <span>
            <span className="who">{approverName ?? 'A first admin'} approved</span>
            {firstApproval.note ? (
              <>
                {' '}
                — <span className="q">"{firstApproval.note}"</span>
              </>
            ) : (
              <> — no note left.</>
            )}
          </span>
        </div>
      )}

      <div className="gj-consequence">
        <span>
          <b>If approved:</b> the planner can join <span className="mono">{p.from.table}</span> to{' '}
          <span className="mono">{p.to.table}</span> ({cardinality}) in every feature it builds.{' '}
          <span className="gj-risk">
            <b>If wrong:</b> every feature crossing this join attaches {p.from.table} rows to the
            wrong {p.to.table} row.
          </span>
        </span>
      </div>

      <p className="gj-caution">
        Matched on metadata (column names + business concepts). No sample rows were compared —
        you are verifying meaning, not data.
      </p>

      <div className="gj-verify">
        <p className="gj-verify-h">
          {p.status === 'PARTIALLY_CONFIRMED'
            ? 'Confirm before you complete the approval'
            : 'Confirm before approving'}
        </p>
        {items.map((body, i) => (
          // eslint-disable-next-line react/no-array-index-key -- positional: items never reorder
          <label className="gj-check" key={i}>
            <input type="checkbox" checked={checked.has(i)} onChange={() => toggle(i)} />
            <span>{body}</span>
          </label>
        ))}
      </div>

      {firstApproval && (
        <p className="gj-approvals">
          1 of 2 · approved by <b>{approverName ?? 'unknown'}</b> · a different admin must confirm
        </p>
      )}

      <div className="gj-approve-area">
        {p.status === 'PROPOSED' && (
          // Only the FIRST approver leaves a note "for the next approver" — once this (second)
          // approval VERIFIES the join there is no next reader, so the partial card omits it.
          <input
            aria-label="Note for the next approver (optional)"
            placeholder="Note for the 2nd approver (optional) — what you checked, what to watch"
            value={noteDraft}
            onChange={e => setNoteDraft(e.target.value)}
          />
        )}
        <div className="gj-actions">
          <button
            type="button"
            className="btn btn--primary"
            disabled={!allChecked || busy}
            onClick={() => void approve()}
          >
            {busy
              ? 'Submitting…'
              : p.status === 'PARTIALLY_CONFIRMED'
                ? 'Approve as 2nd approver'
                : 'Approve'}
          </button>
          {!allChecked && <span className="gj-gate-hint">tick the checklist to enable</span>}
          <button
            type="button"
            className="btn q-ghost"
            disabled={busy}
            onClick={() => setRejectOpen(o => !o)}
          >
            {rejectOpen ? 'Cancel reject' : 'Reject…'}
          </button>
        </div>
      </div>

      {rejectOpen && (
        <div className="gj-rejectbox">
          <span className="gj-verify-h">Reason (recorded + fed back to re-proposal)</span>
          <div className="gj-chips" role="group" aria-label="Rejection reason">
            {REJECT_CATEGORIES.map(c => (
              <button
                type="button"
                key={c}
                className={c === category ? 'gj-chip gj-chip--on' : 'gj-chip'}
                aria-pressed={c === category}
                onClick={() => setCategory(c)}
              >
                {categoryLabel(c)}
              </button>
            ))}
          </div>
          <input
            aria-label="Rejection note (optional)"
            placeholder="Optional note…"
            value={rejectNote}
            onChange={e => setRejectNote(e.target.value)}
          />
          <div className="gj-actions">
            <button
              type="button"
              className="btn btn--danger"
              disabled={!category || busy}
              onClick={() => void reject()}
            >
              {busy ? 'Submitting…' : 'Confirm rejection'}
            </button>
            {!category && <span className="gj-gate-hint">pick a reason to enable</span>}
          </div>
        </div>
      )}

      {cardError && (
        <p className="field-error" role="alert">
          {cardError}
        </p>
      )}
    </li>
  )
}
