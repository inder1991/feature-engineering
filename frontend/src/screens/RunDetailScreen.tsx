// One feature run opened to its record (GET /feature-runs/{id}) — identity, milestones, the
// authoring rows and the stage rail.
//
// **READ-ONLY BY CONSTRUCTION, AND THE ABSENCE OF BUTTONS IS THE DESIGN.** There is no run, re-run,
// retry, generate, execute or fork control here, and none is missing: the spine DERIVES every field
// below from stores that already hold the evidence, and no write endpoint exists behind this route
// to offer. A button that could not do what its word says would be worse than no button.
//
// **THIS SCREEN HOLDS NO POLICY** (the FeatureExecutionScreen rule). A stage is UNAVAILABLE because
// its machinery does not exist, and WHICH machinery is the server's sentence: `reason_code` renders
// verbatim, never translated into words of this file's own. The same code must not be explained one
// way here and another way in the corpus report.
//
// **TWO AXES, NEVER ONE** (spec §6.7). A draft's `state`/`rail_state` is the immutable historical
// outcome; `eligibility` is derived at read time from the retirement row. The rail's AUTHOR_FORMULA
// stage folds the OUTCOME axis only — a READY-then-withdrawn run reads SUCCEEDED up there — so the
// authoring table below is the one place both axes are visible, and it renders each row's stored
// tokens as they are rather than rewriting a withdrawn draft into a failure it never was.
//
// **AND TWO READINGS, NEVER ONE LIST** (spec §R4.4.1). Since migration 1107 a governed retry writes
// a second draft against the same formula identity, so a candidate can hold BOTH a failure and the
// answer that replaced it. The table renders the whole ATTEMPT HISTORY — nothing is hidden, because
// what was tried is part of the record — and marks the row that is each candidate's CURRENT answer.
// The server decides which that is; this screen only says which rows it named, and never folds a
// second opinion of its own out of the states in the table.
//
// **HONEST ABSENCE.** A pre-spine run has no identity record, an unnamed run has no name, and a
// run with no chosen candidates has no milestones. Each says so; none is filled in.
import { useEffect, useState } from 'react'
import {
  ApiError,
  type FeatureRunDetail,
  type RunAuthoringCurrent,
  type RunAuthoringRow,
  type RunRailStage,
  getFeatureRunDetail,
} from '../api'

// How much of an opaque run id is shown before the ellipsis — the RunsScreen row's rule, so a run
// reads the same in the list and in its own header.
const ID_HEAD = 13

// Truncate ONLY when there is something to truncate: an ellipsis on a short legacy id
// ('fgr_legacy01') would claim the reader is missing characters that do not exist.
function shortId(id: string): string {
  return id.length > ID_HEAD ? `${id.slice(0, ID_HEAD)}…` : id
}

// Wire timestamps are ISO8601 UTC; show them as-is minus the T and sub-second noise — honest and
// locale-independent (this is an audit record, not a friendly date). RunDetailPanel's convention.
function fmtWhen(iso: string): string {
  return iso.replace('T', ' ').replace(/\.\d+/, '')
}

// The rail's closed state vocabulary, in the reader's words. The RAW token stays reachable on the
// chip's title, and anything this map does not know — a state a newer backend adds — renders as
// itself rather than being folded into a neighbour it might not mean.
//
// The authoring table deliberately does NOT use this: that table shows a draft row's STORED tokens
// (READY, SUCCEEDED, CANDIDATE_SUPERSEDED), which is what an operator quotes when they ask why a
// draft was withdrawn. The rail is the progress spine; the table is the record.
const STATE_LABEL: Record<string, string> = {
  SUCCEEDED: 'Succeeded',
  IN_PROGRESS: 'In progress',
  NOT_STARTED: 'Not started',
  UNAVAILABLE: 'Unavailable',
  BLOCKED: 'Blocked',
  FAILED: 'Failed',
  CANCELLED: 'Cancelled',
}

// Chip tone: ok for a stage that got there, warn for one that stopped badly. UNAVAILABLE and
// NOT_STARTED stay muted — neither is trouble, and colouring a socket red would read as a fault
// where there is only unbuilt machinery. The chip always carries the state word: colour never
// works alone.
function stateChipClass(state: string): string {
  if (state === 'SUCCEEDED') return 'badge ok'
  if (state === 'BLOCKED' || state === 'FAILED') return 'badge rejected'
  if (state === 'CANCELLED') return 'badge held'
  return 'badge'
}

export function RunDetailScreen({ runId }: { runId: string }) {
  const [run, setRun] = useState<FeatureRunDetail | null>(null)
  const [error, setError] = useState('')
  const [copyStatus, setCopyStatus] = useState('')

  useEffect(() => {
    let live = true
    // App does not key this screen, so a run -> run deep link changes the prop in place: clear the
    // previous run first, or its record would stand under the new address until the fetch lands.
    setRun(null)
    setError('')
    setCopyStatus('')
    getFeatureRunDetail(runId).then(
      detail => {
        if (live) setRun(detail)
      },
      err => {
        if (!live) return
        // 404 covers absence AND denial on this route, deliberately — a distinguishable answer
        // would confirm the id exists. The server refuses to tell them apart, so this screen must
        // not invent the distinction either: it says what the server said and speculates nothing
        // about whose run this is.
        if (err instanceof ApiError && err.status === 404) setError('Run not found.')
        else setError(err instanceof ApiError ? err.detail : String(err))
      },
    )
    return () => {
      live = false
    }
  }, [runId])

  async function copyId() {
    try {
      await navigator.clipboard.writeText(runId)
      setCopyStatus(`Run id copied: ${runId}`)
    } catch {
      // A browser may refuse clipboard access outright. Saying "copied" would be a lie, and saying
      // only "failed" leaves the reader with nothing — so hand them the id itself.
      setCopyStatus(`Could not copy. The run id is ${runId}`)
    }
  }

  if (error)
    return (
      <p role="alert" className="error">
        {error}
      </p>
    )

  if (run === null)
    return (
      <p className="hint" role="status">
        Loading run…
      </p>
    )

  // WHICH attempt is a candidate's current answer is the SERVER's fold (it alone knows the money
  // guard's rule and the candidate key); this is only a lookup of the list it sent, by draft id. A
  // client that re-derived "current" from the states in the table would be a second opinion, and
  // two folds of one question is how the rail and the table start disagreeing.
  const currentAnswers = new Map(run.authoring.current.map(row => [row.formula_draft_id, row]))

  return (
    <section className="panel" aria-label="Feature run">
      <div className="run-head">
        {/* The unset name renders as absence, never as an invented label. */}
        <h2 style={{ margin: 0 }}>{run.display_name ?? '—'}</h2>
        {run.pre_spine && (
          <span
            className="badge"
            title="This run predates the identity spine, so it has no run identity record. An honest gap in the record, never a failure of the run."
          >
            Pre-spine
          </span>
        )}
        <span className="mono" title={runId}>
          {shortId(runId)}
        </span>
        <button type="button" className="btn" aria-label={`Copy run id ${runId}`} onClick={copyId}>
          Copy id
        </button>
      </div>
      <p className="hint" role="status" data-testid="copy-status">
        {copyStatus}
      </p>
      {run.description && <p>{run.description}</p>}

      <dl className="kv" style={{ marginTop: 12 }}>
        <div>
          <dt>Owner</dt>
          <dd className="mono">{run.owner_subject ?? '—'}</dd>
        </div>
        <div>
          <dt>Hypothesis</dt>
          {/* A run can carry an intent id whose intent row is gone (there is no FK), so the
              hypothesis is stated as missing on its own terms — the id below never stands in for
              words nobody wrote. */}
          <dd>{run.intent?.hypothesis ?? 'No hypothesis recorded'}</dd>
        </div>
        <div>
          <dt>Intent</dt>
          <dd className="mono">{run.intent?.intent_id ?? '—'}</dd>
        </div>
      </dl>

      <h3 className="run-section">Identity</h3>
      {run.identity ? (
        <dl className="kv">
          <div>
            <dt>Run identity hash</dt>
            <dd className="mono">{run.identity.run_identity_hash}</dd>
          </div>
          <div>
            <dt>Considered revision</dt>
            <dd className="mono">{run.identity.considered_revision_id}</dd>
          </div>
          <div>
            <dt>Metadata snapshot</dt>
            <dd className="mono">{run.identity.metadata_snapshot_id}</dd>
          </div>
        </dl>
      ) : (
        <p className="hint">
          This run predates the identity spine, so no run identity was recorded. An honest gap in
          the record, never a failure of the run.
        </p>
      )}

      <h3 className="run-section">Stages</h3>
      <table>
        <thead>
          <tr>
            <th>Stage</th>
            <th>State</th>
            <th>Why</th>
          </tr>
        </thead>
        <tbody>
          {run.rail.map(stage => (
            <RailRow key={stage.stage} stage={stage} />
          ))}
        </tbody>
      </table>

      <h3 className="run-section">Milestones</h3>
      {run.milestones.choose_candidates.length === 0 ? (
        <p className="hint">No candidates are recorded as chosen for this run.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Candidate</th>
              <th>Considered revision</th>
              <th>Chosen</th>
            </tr>
          </thead>
          <tbody>
            {/* The same candidate can be chosen twice, so neither field alone is a key and the
                pair is not guaranteed unique either — the position is what distinguishes the two
                rows a re-choice writes. */}
            {run.milestones.choose_candidates.map((choice, index) => (
              <tr key={`${choice.option_id}#${index}`}>
                <td className="mono">{choice.option_id}</td>
                <td className="mono">{choice.considered_revision_id}</td>
                <td className="mono stage-detail">{fmtWhen(choice.chosen_at)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
      {/* The client types a binding as `unknown` because nothing can write one yet, so its fields
          have no settled shape to render — the count is everything that can honestly be said. */}
      <p className="hint">
        {run.milestones.bind_selections.length === 0
          ? 'No selection bindings are recorded.'
          : `${run.milestones.bind_selections.length} selection binding(s) recorded.`}
      </p>

      <h3 className="run-section">Authoring</h3>
      {run.authoring.history.length === 0 ? (
        <p className="hint">No formula drafts are recorded for this run.</p>
      ) : (
        <>
          <p className="hint">
            Every attempt, in the order it was requested. Two axes, never one: outcome is what
            happened and never changes; eligibility is read now — a succeeded draft can still have
            been withdrawn. And two readings, never one: the row marked as a candidate&rsquo;s
            current answer is where that candidate stands, while the rest are the attempts that got
            it there. The stage rail above folds the current answers alone.
          </p>
          <table>
            <thead>
              <tr>
                <th>Draft</th>
                <th>Candidate</th>
                <th>State</th>
                <th>Outcome</th>
                <th>Eligibility</th>
                <th>Reading</th>
              </tr>
            </thead>
            <tbody>
              {run.authoring.history.map(row => (
                <AuthoringRow
                  key={row.formula_draft_id}
                  row={row}
                  current={currentAnswers.get(row.formula_draft_id)}
                />
              ))}
            </tbody>
          </table>
        </>
      )}
    </section>
  )
}

function RailRow({ stage }: { stage: RunRailStage }) {
  return (
    <tr>
      <td className="mono">{stage.stage}</td>
      <td>
        <span className={stateChipClass(stage.state)} title={stage.state}>
          {STATE_LABEL[stage.state] ?? stage.state}
        </span>
      </td>
      {/* The server's code, verbatim. A stage that could actually run carries none, and that
          absence renders as absence — not as a blank cell the reader has to interpret. */}
      <td className="mono">{stage.reason_code ?? '—'}</td>
    </tr>
  )
}

// One attempt. `current` is the server's row for this draft when this draft IS its candidate's
// current answer, and undefined when it is an earlier attempt — the distinction the whole §R4.4.1
// split exists to make, so it is carried as presence/absence rather than as a boolean nobody can
// trace back to a row.
function AuthoringRow({ row, current }: { row: RunAuthoringRow; current?: RunAuthoringCurrent }) {
  return (
    <tr className={current ? 'attempt-current' : 'attempt-earlier'}>
      <td className="mono">{row.formula_draft_id}</td>
      <td className="mono">
        {/* The candidate is the PAIR. The option id is what a person chose, so it leads; the
            considered revision is what makes it a candidate, and two rows reading `o1` under
            different revisions are not the same candidate. */}
        <span>{row.option_id}</span>
        <div className="stage-detail">{row.considered_revision_id}</div>
      </td>
      <td className="mono">{row.state}</td>
      <td className="mono">{row.rail_state}</td>
      <td>
        {row.eligibility === 'withdrawn' ? (
          // The reason is the retirement row's own word — the same verbatim rule the rail follows.
          // A withdrawal with no reason recorded says only that it was withdrawn.
          <span className="badge held">
            {row.retirement_reason ? `Withdrawn — ${row.retirement_reason}` : 'Withdrawn'}
          </span>
        ) : (
          <span className="badge ok">Current</span>
        )}
      </td>
      <td>
        {current === undefined ? (
          // Not a failure and not a demotion: an attempt a later one superseded. It stays on the
          // record, quietly, because what was tried is part of what happened.
          <span className="stage-detail">Earlier attempt</span>
        ) : current.resolved ? (
          <span className="badge ok">Current answer</span>
        ) : (
          // The candidate's latest word, and it bought nothing — every attempt failed or was
          // cancelled. Saying "current answer" here would report a purchase that never happened.
          <span className="badge held">Current — no answer bought</span>
        )}
      </td>
    </tr>
  )
}
