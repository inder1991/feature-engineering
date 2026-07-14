import { type FormEvent, type ReactNode, useRef, useState } from 'react'
import {
  ApiError,
  type JoinProposal,
  type RejectCategory,
  REJECT_CATEGORIES,
  type RelationshipReadiness,
  TABLE_FACT_REJECT_CATEGORIES,
  type TableFactProposal,
  type TableFactRejectCategory,
  confirmJoin,
  confirmTableFact,
  listJoinProposals,
  listRelationshipReadiness,
  listTableFactProposals,
  rejectJoin,
  rejectTableFact,
} from '../api'

// Governance review: the confirmation surface over what the enrichment passes proposed. Three
// tabs share one source queue: Joins (Pass C, TWO distinct admins), Grain & availability
// (Pass B table facts, SINGLE confirmer — one approve reaches VERIFIED and projects), and
// Readiness (READ-ONLY: the per-table relationship diagnostic — what joins were discovered and
// where they stand — no actions). The two action tabs are evidence-forward cards with a
// consequence line, an LLM/metadata caution, and a what-to-verify checklist that GATES the
// Approve button. Reject is structured (category + optional note).
// Follows ReviewQueueScreen's shape: source input -> load() -> per-card action handlers -> a
// session-local decided Map (the durable state lives server-side; a reload refetches the open
// queue, which no longer contains what was decided).

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

// Readiness-tab status chips: strong outcomes reuse the solid governance tones (verified green,
// conflicting red, proposed accent); the two nothing-actionable states stay quiet (muted/faint).
const READINESS_BADGE: Record<RelationshipReadiness['status'], { label: string; tone: string }> = {
  confirmed: { label: 'confirmed', tone: 'gj-verified' },
  conflicting: { label: 'conflicting', tone: 'gj-rejected' },
  candidate_proposed: { label: 'candidate proposed', tone: 'gj-proposed' },
  weak_candidates_only: { label: 'weak candidates only', tone: 'gj-weak' },
  no_candidates: { label: 'no candidates', tone: 'gj-none' },
}

// "2 confirmed · 1 proposed" — only the non-zero pair categories, in precedence-adjacent order.
function pairCounts(r: RelationshipReadiness): string {
  const parts = [
    [r.confirmed_pairs.length, 'confirmed'] as const,
    [r.proposed_pairs.length, 'proposed'] as const,
    [r.weak_pairs.length, 'weak'] as const,
    [r.conflicting_pairs.length, 'conflicting'] as const,
  ]
    .filter(([n]) => n > 0)
    .map(([n, label]) => `${n} ${label}`)
  return parts.length > 0 ? parts.join(' · ') : 'no candidate pairs'
}

function categoryLabel(category: string): string {
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

// Single-confirmer table facts VERIFY on the one approve — only the projection outcome varies.
function approvedFactNote(projection: string): string {
  return projection === 'projected'
    ? 'Verified — projected to the operational table facts. Planners read it now (revocable).'
    : 'Verified — the operational projection is deferred to the next caught-up ingest.'
}

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err)
}

export function GovernanceReviewScreen() {
  const [source, setSource] = useState('')
  const [proposals, setProposals] = useState<JoinProposal[] | null>(null)
  const [tableFacts, setTableFacts] = useState<TableFactProposal[] | null>(null)
  const [readiness, setReadiness] = useState<RelationshipReadiness[] | null>(null)
  const [loadedSource, setLoadedSource] = useState('')
  // Which queue is on screen. Only one renders at a time — per-card state (checklists, reject
  // boxes) is keyed component state, so switching tabs does not leak ticks across kinds.
  const [tab, setTab] = useState<'joins' | 'facts' | 'readiness'>('joins')
  // Per-QUEUE load errors (whole-branch review FIX 2): each tab surfaces its OWN fetch failure
  // without blanking the other tab's data.
  const [joinsError, setJoinsError] = useState('')
  const [factsError, setFactsError] = useState('')
  const [readinessError, setReadinessError] = useState('')
  // Conflict banner (409): survives the reload that follows it, unlike `error`.
  const [notice, setNotice] = useState('')
  // Session-only DISPLAY state for cards decided this session, keyed by fact_key. The durable
  // state is server-side; clearing on (re)load is correct — a decided fact leaves the open
  // queue. Join and table-fact keys never collide, so one map serves both tabs.
  const [decided, setDecided] = useState<Map<string, Decision>>(new Map())
  // Bumped per successful load: keys the cards so a reload REMOUNTS them (a 409 means the
  // proposal changed under the reviewer — stale checklist ticks must not survive).
  const [generation, setGeneration] = useState(0)

  // Monotonic id per load(): a late response from an older load must never overwrite newer data.
  const loadSeq = useRef(0)

  async function load(name: string) {
    if (!name.trim()) return
    const id = ++loadSeq.current
    setNotice('')
    // All three fetches load together so the tab counts are honest and a 409 reload refreshes
    // everything — but they settle INDEPENDENTLY (whole-branch review FIX 2): a table-facts or
    // readiness endpoint failure must not reject the joins load and blank its tab (or any other
    // combination). Each tab renders from its own settled result; a failed fetch shows a
    // per-tab error instead.
    const [joinsRes, factsRes, readinessRes] = await Promise.allSettled([
      listJoinProposals(name.trim()),
      listTableFactProposals(name.trim()),
      listRelationshipReadiness(name.trim()),
    ])
    if (id !== loadSeq.current) return
    setProposals(joinsRes.status === 'fulfilled' ? joinsRes.value.proposals : null)
    setJoinsError(joinsRes.status === 'rejected' ? errorDetail(joinsRes.reason) : '')
    setTableFacts(factsRes.status === 'fulfilled' ? factsRes.value.proposals : null)
    setFactsError(factsRes.status === 'rejected' ? errorDetail(factsRes.reason) : '')
    setReadiness(readinessRes.status === 'fulfilled' ? readinessRes.value.relationships : null)
    setReadinessError(readinessRes.status === 'rejected' ? errorDetail(readinessRes.reason) : '')
    setLoadedSource(
      joinsRes.status === 'fulfilled' ||
        factsRes.status === 'fulfilled' ||
        readinessRes.status === 'fulfilled'
        ? name.trim()
        : '',
    )
    setDecided(new Map())
    setGeneration(g => g + 1)
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
      {notice && (
        <p role="alert" className="error">
          {notice}
        </p>
      )}
      {(proposals || tableFacts || readiness) && (
        <div className="viewtoggle" role="group" aria-label="Proposal kind">
          <button type="button" aria-pressed={tab === 'joins'} onClick={() => setTab('joins')}>
            Joins ({proposals ? proposals.length : '—'})
          </button>
          <button type="button" aria-pressed={tab === 'facts'} onClick={() => setTab('facts')}>
            Grain &amp; availability ({tableFacts ? tableFacts.length : '—'})
          </button>
          <button
            type="button"
            aria-pressed={tab === 'readiness'}
            onClick={() => setTab('readiness')}
          >
            Readiness ({readiness ? readiness.length : '—'})
          </button>
        </div>
      )}
      {tab === 'joins' && joinsError && (
        <p role="alert" className="error">
          {joinsError}
        </p>
      )}
      {tab === 'facts' && factsError && (
        <p role="alert" className="error">
          {factsError}
        </p>
      )}
      {tab === 'readiness' && readinessError && (
        <p role="alert" className="error">
          {readinessError}
        </p>
      )}
      {tab === 'joins' && proposals?.length === 0 && (
        <p className="empty" role="status">
          No open join proposals for this source.
        </p>
      )}
      {tab === 'joins' && proposals && proposals.length > 0 && (
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
            {proposals.length} open proposal{proposals.length === 1 ? '' : 's'} ·{' '}
            {proposals.filter(p => decided.has(p.fact_key)).length} decided this session
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
      {tab === 'facts' && tableFacts?.length === 0 && (
        <p className="empty" role="status">
          No open grain or availability proposals for this source.
        </p>
      )}
      {tab === 'facts' && tableFacts && tableFacts.length > 0 && (
        <>
          <div className="callout callout--accent">
            <div className="callout-body">
              <p>
                <strong>One approval makes it operational.</strong> These grain and as-of facts
                were inferred by the LLM from names and descriptions — no data was profiled.
                Your single confirmation verifies the fact and projects it into what planners
                read, so work the checklist as the verification the pipeline never did.
              </p>
            </div>
          </div>
          <p className="tabular-nums" role="status">
            {tableFacts.length} open proposal{tableFacts.length === 1 ? '' : 's'} ·{' '}
            {tableFacts.filter(p => decided.has(p.fact_key)).length} decided this session
          </p>
          <ul className="rows">
            {tableFacts.map(p => {
              const decision = decided.get(p.fact_key)
              if (decision) {
                return (
                  <li className="row q-item q-item--resolved" key={p.fact_key}>
                    <div className="q-head">
                      <span className="mono">
                        {p.fact_type === 'grain' ? 'Grain' : 'As-of'} · {p.table}
                      </span>
                      <span className={`badge ${decision.tone}`}>{decision.badge}</span>
                    </div>
                    <p className="q-note">{decision.note}</p>
                  </li>
                )
              }
              return (
                <TableFactCard
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
      {tab === 'readiness' && readiness?.length === 0 && (
        <p className="empty" role="status">
          No tables with relationship readiness for this source.
        </p>
      )}
      {tab === 'readiness' && readiness && readiness.length > 0 && (
        // READ-ONLY diagnostic: one compact row per table — where its join relationships stand
        // (the precedence-folded status) plus the per-category pair counts. No actions here;
        // confirming happens on the Joins tab.
        <>
          <p className="tabular-nums" role="status">
            {readiness.length} table{readiness.length === 1 ? '' : 's'} ·{' '}
            {readiness.filter(r => r.status === 'confirmed').length} with a confirmed join
          </p>
          <ul className="rows">
            {readiness.map(r => (
              <li className="row q-item" key={`${r.schema}.${r.table}`}>
                <div className="q-head">
                  <span className="mono gj-kind">
                    {r.schema}.{r.table}
                  </span>
                  <span className={`badge ${READINESS_BADGE[r.status].tone}`}>
                    {READINESS_BADGE[r.status].label}
                  </span>
                  <span className="gj-score">{pairCounts(r)}</span>
                </div>
              </li>
            ))}
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

interface TableFactCardProps {
  proposal: TableFactProposal
  onDecided: (factKey: string, decision: Decision) => void
  onConflict: (detail: string) => void
}

// A Pass B grain / availability_time fact. SINGLE-confirmer: one checklist-gated Approve
// reaches VERIFIED and projects — there is no "1 of 2" partial state on this card.
function TableFactCard({ proposal: p, onDecided, onConflict }: TableFactCardProps) {
  const [checked, setChecked] = useState<Set<number>>(new Set())
  const [noteDraft, setNoteDraft] = useState('')
  const [rejectOpen, setRejectOpen] = useState(false)
  const [category, setCategory] = useState<TableFactRejectCategory | null>(null)
  const [rejectNote, setRejectNote] = useState('')
  const [busy, setBusy] = useState(false)
  const [cardError, setCardError] = useState('')

  const isGrain = p.fact_type === 'grain'
  // Defensive value reads: parse status "missing" means the stored value was unreadable — the
  // card still renders (and the baseline checklist still gates) with an explicit placeholder,
  // mirroring the joins gate-stays-gateable property.
  const value = p.proposed_value ?? {}
  const columns = isGrain && Array.isArray(value.columns) ? value.columns : []
  const columnsLabel = columns.length > 0 ? columns.join(' + ') : '(unreadable)'
  const asOfColumn = (!isGrain && typeof value.column === 'string' && value.column) || '(unreadable)'
  const basis = (!isGrain && typeof value.basis === 'string' && value.basis) || 'unknown basis'
  const advisoryParts = [
    p.advisory.table_role && `role: ${p.advisory.table_role}`,
    p.advisory.primary_entity && `entity: ${p.advisory.primary_entity}`,
    p.advisory.event_or_snapshot && `${p.advisory.event_or_snapshot} table`,
  ].filter((part): part is string => Boolean(part))

  // The what-to-verify checklist that gates Approve: the 4 baseline items per fact_type. Table
  // facts carry no scored signals, so there are no derived items — the baseline is the whole gate.
  const items: ReactNode[] = isGrain
    ? [
        <>
          I reviewed the proposed grain <strong>columns</strong> —{' '}
          <span className="mono">{columnsLabel}</span> — against what one row of{' '}
          <span className="mono">{p.table}</span> actually is.
        </>,
        <>
          I understand <strong>one row = one {columnsLabel}</strong> determines how every feature
          on this table aggregates.
        </>,
        <>
          I understand this grain was <strong>LLM-inferred, not value-profiled</strong> —
          uniqueness was never measured against the data.
        </>,
        <>
          I confirm <span className="mono">{columnsLabel}</span> should be the{' '}
          <strong>grain</strong> of <span className="mono">{p.table}</span>.
        </>,
      ]
    : [
        <>
          I reviewed the as-of <strong>column</strong> (<span className="mono">{asOfColumn}</span>)
          and its <strong>basis</strong> (<span className="mono">{basis}</span>).
        </>,
        <>
          I understand <strong>point-in-time features</strong> will read{' '}
          <span className="mono">{asOfColumn}</span> as the as-of date.
        </>,
        <>
          I understand this column was <strong>LLM-inferred, not value-profiled</strong> — no
          timestamps were sampled.
        </>,
        <>
          I confirm <span className="mono">{asOfColumn}</span> should be the{' '}
          <strong>availability time</strong> of <span className="mono">{p.table}</span>.
        </>,
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
      const res = await confirmTableFact(p.fact_key, note ? { note } : {})
      onDecided(p.fact_key, {
        badge: res.operational_projection === 'projected' ? 'verified · live' : 'verified · pending',
        tone: 'gj-verified',
        note: approvedFactNote(res.operational_projection),
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
      await rejectTableFact(p.fact_key, note ? { category, note } : { category })
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
        <span className="mono gj-kind">
          {isGrain ? 'Grain' : 'As-of'} · {p.table}
        </span>
        <span className="badge gj-proposed">proposed</span>
        <span className="gj-score">
          {p.evidence_parse_status === 'parsed' ? (
            <>LLM-inferred · not profiled</>
          ) : (
            <>value unreadable ({p.evidence_parse_status})</>
          )}
        </span>
      </div>

      <div className="gj-join">
        <div className="gj-endp">
          <span className="k">table</span>
          <span className="tbl">{p.table}</span>
        </div>
        <div className="gj-arrow" aria-hidden="true">
          <span className="g">→</span>
          <span className="cr">{isGrain ? 'grain' : 'as-of'}</span>
        </div>
        {isGrain ? (
          <div className="gj-endp">
            <span className="k">one row per</span>
            <span className="col">{columnsLabel}</span>
            <span className="tbl">{value.is_unique ? 'proposed unique' : 'uniqueness unconfirmed'}</span>
          </div>
        ) : (
          <div className="gj-endp">
            <span className="k">as-of column</span>
            <span className="col">{asOfColumn}</span>
            <span className="tbl">basis: {basis}</span>
          </div>
        )}
      </div>

      {advisoryParts.length > 0 && (
        <p className="q-note">Advisory context (LLM-described): {advisoryParts.join(' · ')}</p>
      )}

      <div className="gj-consequence">
        {isGrain ? (
          <span>
            <b>If approved:</b> one row of <span className="mono">{p.table}</span> = one{' '}
            <span className="mono">{columnsLabel}</span>; features aggregate to this grain.{' '}
            <span className="gj-risk">
              <b>If wrong:</b> counts &amp; per-entity features are miscomputed.
            </span>
          </span>
        ) : (
          <span>
            <b>If approved:</b> point-in-time features read{' '}
            <span className="mono">{asOfColumn}</span> as the as-of date ({basis}).{' '}
            <span className="gj-risk">
              <b>If wrong:</b> features silently leak future data or read stale rows.
            </span>
          </span>
        )}
      </div>

      <p className="gj-caution">
        LLM-inferred from names &amp; descriptions, not value-profiled — no data was scanned. You
        are the verification this fact never had.
      </p>

      <div className="gj-verify">
        <p className="gj-verify-h">Confirm before approving</p>
        {items.map((body, i) => (
          // eslint-disable-next-line react/no-array-index-key -- positional: items never reorder
          <label className="gj-check" key={i}>
            <input type="checkbox" checked={checked.has(i)} onChange={() => toggle(i)} />
            <span>{body}</span>
          </label>
        ))}
      </div>

      <div className="gj-approve-area">
        <input
          aria-label="Approval note (optional)"
          placeholder="Optional note — what you checked; recorded for audit"
          value={noteDraft}
          onChange={e => setNoteDraft(e.target.value)}
        />
        <div className="gj-actions">
          <button
            type="button"
            className="btn btn--primary"
            disabled={!allChecked || busy}
            onClick={() => void approve()}
          >
            {busy ? 'Submitting…' : 'Approve'}
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
            {TABLE_FACT_REJECT_CATEGORIES.map(c => (
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
