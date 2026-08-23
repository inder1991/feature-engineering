// One feature run opened to its record (GET /feature-runs/{id}) — identity, milestones, the
// authoring rows, the stage rail, and ONE gesture.
//
// **EXACTLY ONE CONTROL WRITES, AND EVERY ABSENT CONTROL IS STILL THE DESIGN.** There is no run,
// re-run, retry, execute or fork button here, and none is missing: the spine DERIVES every field
// below from stores that already hold the evidence, and no write endpoint exists behind those words
// to offer. The one that does exist is `Prepare formulas` (spec §R4.4.2) — it hands the candidates a
// person picked to the code-generation coordinator, and it fires from an onClick and nowhere else.
// A button that could not do what its word says would be worse than no button, which is why it is
// DISABLED whenever the server says its entrance would refuse.
//
// **THE SERVER DECIDES WHETHER THAT BUTTON CAN RUN.** `prepare_code.available` is derived beside
// the rail, from the same facts the POST refuses on, and `prepare_code.detail` is the sentence —
// rendered verbatim, never re-worded here. A screen that decided enablement for itself would be a
// second policy, and the day the two disagreed a person would click into a refusal.
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
  type RunJobRow,
  type RunRailStage,
  getFeatureRunDetail,
  prepareRunCode,
} from '../api'
import { codeGenerationEnabled } from '../nav'

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

// The server's `COST_AUTHORIZATION_MISSING` refusal, as this screen needs it: how many members
// author with the LLM and how many provider calls the enforcement will budget for them. Both
// numbers are the SERVER's — a browser that computed a ceiling would be confirming a different
// spend from the one the platform holds itself to.
interface CostQuote {
  llm_members: number
  estimated_provider_calls: number
  detail: string
}

function costQuote(err: unknown): CostQuote | null {
  if (!(err instanceof ApiError) || err.errorCode !== 'COST_AUTHORIZATION_MISSING') return null
  const payload = err.payload ?? {}
  const calls = payload.estimated_provider_calls
  if (typeof calls !== 'number') return null
  return {
    llm_members: typeof payload.llm_members === 'number' ? payload.llm_members : 0,
    estimated_provider_calls: calls,
    detail: err.detail,
  }
}

export function RunDetailScreen({ runId }: { runId: string }) {
  const [run, setRun] = useState<FeatureRunDetail | null>(null)
  const [error, setError] = useState('')
  const [copyStatus, setCopyStatus] = useState('')
  // The prepare gesture's own state. `picked` is keyed by option_id because that is what the body
  // carries; `quote` is present only while the server is waiting for a confirmed ceiling.
  const [picked, setPicked] = useState<string[]>([])
  const [busy, setBusy] = useState(false)
  const [quote, setQuote] = useState<CostQuote | null>(null)
  const [maxTokens, setMaxTokens] = useState('')
  const [maxCost, setMaxCost] = useState('')
  const [prepareError, setPrepareError] = useState('')
  const [prepareNotice, setPrepareNotice] = useState('')

  useEffect(() => {
    let live = true
    // App does not key this screen, so a run -> run deep link changes the prop in place: clear the
    // previous run first, or its record would stand under the new address until the fetch lands.
    setRun(null)
    setError('')
    setCopyStatus('')
    setPicked([])
    setQuote(null)
    setPrepareError('')
    setPrepareNotice('')
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

  // THE ONE WRITE, and it fires from an onClick alone — never an effect, an interval or a retry.
  // The first call deliberately carries no ceiling: the server answers with the number its own
  // enforcement will budget, and the confirmation below sends THAT number back.
  function prepare(approved: boolean) {
    if (run === null) return
    setBusy(true)
    setPrepareError('')
    setPrepareNotice('')
    prepareRunCode(runId, {
      option_ids: picked,
      ...(approved && quote
        ? {
          spend_approval: {
            max_calls: quote.estimated_provider_calls,
            max_tokens: Number(maxTokens),
            // The user's own number: this screen quotes WORK, never prices — a default price here
            // would be the browser deciding a spend.
            max_cost: maxCost,
            currency: 'USD',
            pricing_version: 'development',
            // The approval covers this working session, not an open-ended future.
            expires_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
          },
        }
        : {}),
    }).then(
      response => {
        // The run comes back READ from the store, so the jobs list below is the store's and not a
        // patch of this screen's own making.
        setRun(response.run)
        setPicked([])
        setQuote(null)
        setPrepareNotice(`${response.detail} (job ${response.job_id}; declaration from `
          + `${response.declaration_source_job_id})`)
      },
      err => {
        const asked = costQuote(err)
        if (asked) setQuote(asked)
        // The server's sentence, verbatim — this screen owns no vocabulary for a refusal.
        else setPrepareError(err instanceof ApiError ? err.detail : String(err))
      },
    ).finally(() => setBusy(false))
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

  // The candidates a person can still ask for: CHOSEN (the milestone's own rows) and carrying no
  // current answer yet. The candidate is the PAIR — an option id is only meaningful inside the
  // revision that froze it — so membership is tested on both halves; the checkbox and the request
  // then carry the option id, which is what the run's one frozen set makes unambiguous.
  //
  // Deduplicated by candidate: the same option can be chosen twice (a re-choice writes a second
  // row), and that is one thing to prepare, not two checkboxes for one feature.
  const answered = new Set(
    run.authoring.current.map(row => `${row.considered_revision_id}#${row.option_id}`))
  const waiting = [...new Map(
    run.milestones.choose_candidates
      .filter(c => !answered.has(`${c.considered_revision_id}#${c.option_id}`))
      .map(c => [c.option_id, c] as const)).values()]

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
      {/* ▲ ONE NUMBER, NOT TWO. This used to render its own count beside the rail's "2 of 5
          bound", and the two disagreed BY DESIGN: the rail counts CHOICES that carry a formula,
          these are PINS — a selection re-pinned after a re-author is two rows for one choice, and
          a pin on a candidate nobody chose is a row that counts towards nothing. Two numbers under
          one heading make a reader arbitrate between them. So the rail states how many, and this
          states which. */}
      {run.milestones.bind_selections.length === 0 ? (
        <p className="hint">No selection bindings are recorded.</p>
      ) : (
        <p className="hint">
          Formulas are pinned to:{' '}
          {run.milestones.bind_selections.map(pin => (
            <span key={pin.binding_id} className="mono">{pin.option_id} </span>
          ))}
        </p>
      )}

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

      <h3 className="run-section">Prepare code</h3>
      {/* WHY THE CONTROL IS OFF, in the server's words. Not translated, not summarised, and not
          replaced with a cheerful "coming soon": the sentence names which person fixes what. */}
      {!run.prepare_code.available && (
        <p className="hint" data-testid="prepare-unavailable">
          <span className="mono">{run.prepare_code.reason_code}</span> — {run.prepare_code.detail}
        </p>
      )}
      {waiting.length === 0 ? (
        <p className="hint">
          Every candidate this run chose already has a formula attempt; there is nothing left to
          prepare.
        </p>
      ) : (
        <ul className="prepare-candidates">
          {waiting.map(candidate => (
            <li key={candidate.option_id}>
              <label>
                <input
                  type="checkbox"
                  checked={picked.includes(candidate.option_id)}
                  disabled={!run.prepare_code.available || busy}
                  onChange={() => setPicked(current =>
                    current.includes(candidate.option_id)
                      ? current.filter(o => o !== candidate.option_id)
                      : [...current, candidate.option_id])}
                />
                {' '}
                <span className="mono">{candidate.option_id}</span>
              </label>
            </li>
          ))}
        </ul>
      )}

      {quote && (
        <fieldset className="prepare-spend">
          <legend>Confirm the AI cost ceiling</legend>
          {/* The server's own sentence and the server's own numbers. Up to N provider calls is the
              enforcement's budget, not an estimate this browser produced. */}
          <p>{quote.detail}</p>
          <p>
            {quote.llm_members} formula(s) author with the LLM — up to{' '}
            {quote.estimated_provider_calls} provider calls. Your confirmation is recorded durably
            and every call reserves against it.
          </p>
          <label>
            Token ceiling
            <input value={maxTokens} onChange={e => setMaxTokens(e.target.value)}
                   inputMode="numeric" />
          </label>
          <label>
            Cost ceiling (USD)
            <input value={maxCost} onChange={e => setMaxCost(e.target.value)}
                   inputMode="decimal" />
          </label>
        </fieldset>
      )}

      {/* ONE button, and its label states what it does. Disabled whenever the server says the
          entrance would refuse, whenever nothing is picked, and while a request is in flight. */}
      <button
        type="button"
        className="btn"
        disabled={!run.prepare_code.available || picked.length === 0 || busy
          || (quote !== null && (!maxTokens.trim() || !maxCost.trim()))}
        onClick={() => prepare(quote !== null)}
      >
        {quote === null
          ? 'Prepare formulas (records a code-generation job)'
          : 'Prepare formulas (records the job and your cost ceiling)'}
      </button>
      {prepareNotice && (
        <p className="hint" role="status" data-testid="prepare-status">{prepareNotice}</p>
      )}
      {prepareError && <p role="alert" className="error">{prepareError}</p>}

      <h3 className="run-section">Code generation</h3>
      {run.jobs.length === 0 ? (
        <p className="hint">No code-generation job has been requested for this run.</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>Job</th>
              <th>Status</th>
              <th>Acts</th>
              <th>Requested</th>
            </tr>
          </thead>
          <tbody>
            {run.jobs.map(job => <JobRow key={job.job_id} job={job} />)}
          </tbody>
        </table>
      )}
    </section>
  )
}

// One code-generation journey. The status is the store's word, rendered as itself — this screen
// has no vocabulary of its own for a job, and inventing one would be a second reading of a state
// the generation workspace already renders.
function JobRow({ job }: { job: RunJobRow }) {
  return (
    <tr>
      <td className="mono">
        {/* The workspace exists only where its flag is on — that flag mirrors the SERVER's own
            switch, so a link rendered without it would go to a screen that is not routed. The id
            still shows: a person can always quote it. */}
        {codeGenerationEnabled() ? (
          <a href={`#/code-generation?job_id=${encodeURIComponent(job.job_id)}`}>{job.job_id}</a>
        ) : (
          job.job_id
        )}
      </td>
      <td className="mono">{job.status}</td>
      {/* SEVERAL acts per job, because migration 1111 refused to collapse them into one. */}
      <td className="mono stage-detail">
        {job.actions.map(a => `${a.action} ${a.state.toLowerCase()}`).join(' · ') || '—'}
      </td>
      <td className="mono stage-detail">{fmtWhen(job.requested_at)}</td>
    </tr>
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
          absence renders as absence — not as a blank cell the reader has to interpret.

          The stage's own count rides beside it when it has one ("2 of 5 bound — accumulating").
          It was computed and then dropped on the floor here, which is the worse half of the D3
          rule: a number derived and never shown is indistinguishable from one never derived. */}
      <td className="mono">
        {stage.reason_code ?? (stage.detail ? '' : '—')}
        {stage.detail && <span className="stage-detail">{stage.detail}</span>}
      </td>
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
