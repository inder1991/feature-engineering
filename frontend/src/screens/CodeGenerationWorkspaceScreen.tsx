import { useCallback, useEffect, useState } from 'react'
import {
  ApiError,
  type CodeGenerationJob,
  type CodeGenerationJobMember,
  cancelCodeGenerationJob,
  getCodeGenerationJob,
} from '../api'
import { useIdentityKey } from '../session'

// Step 5b — the generation workspace: one durable journey, watched from a job id.
//
// **SEVEN STAGES, NOT SIX.** Selected · Preparing formulas · Validating · Generating code ·
// Code ready · Sandbox executed · Sandbox published. Sandbox execution and sandbox publication
// are separate acts (parent §2/§3) — a UI that merged them would re-create the merged column
// parent §4 explicitly undid ("Artifact not verified → Sandbox: Depends", the evasion). The two
// sandbox stages live on the EXECUTION workspace; this screen shows where they stand relative to
// this job and links there once a sealed artifact exists.
//
// **EVERY WORD COMES FROM THE SERVER.** Status, member states, blockers, warnings, strategies —
// rendered verbatim or mapped string-for-string to plain language. Nothing here derives an
// "allowed", computes a badge from parts, or invents an owner, a timestamp or a cost (parent §7;
// child 5b item 6: a badge is never computed in the browser).
//
// **WARNINGS ARE DISPLAYED, NOT MERELY RETURNED** (parent D3): a warning that is computed and
// dropped is worse than no warning. Every member renders its strategy warnings.
//
// **THE ONE WRITE IS CANCEL**, in an onClick and nowhere else — the same discipline as
// FeatureExecutionScreen's buttons. Polling only READS: the job is durable, so closing this
// browser interrupts nothing, and reload resumes from the same durable truth.

interface Props {
  jobId: string
  navigate?: (route: string, params?: Record<string, string>) => void
}

// The seven stages in journey order, each derived from SERVER facts alone.
const STAGES = [
  'Selected',
  'Preparing formulas',
  'Validating',
  'Generating code',
  'Code ready',
  'Sandbox executed',
  'Sandbox published',
] as const

type StageState = 'done' | 'active' | 'pending' | 'stopped'

// Which journey stage each server status has REACHED. BLOCKED/FAILED/CANCELLED stop the ladder
// at wherever the job was; the two sandbox stages are never claimed by this aggregate (it is a
// PREVIEW coordinator — child §3.5) so they can only ever be 'pending' here, with the link out.
const REACHED: Record<string, number> = {
  REQUESTED: 0,
  PLANNING_FORMULAS: 1,
  AUTHORING: 1,
  READY_TO_BUILD: 2,
  GENERATING_PREVIEW: 3,
  PREVIEW_READY: 4,
}

function stageStates(job: CodeGenerationJob): StageState[] {
  const stopped = ['BLOCKED', 'FAILED', 'CANCELLED'].includes(job.status)
  const reached = REACHED[job.status] ?? (
    // A terminal job stopped wherever its links say it got to: with a sealed artifact the code
    // was ready; with a generation request it was generating; otherwise it was authoring.
    job.sealed_artifact_id ? 4 : job.generation_request_id ? 3 : 1)
  return STAGES.map((_, index) => {
    if (index < reached) return 'done'
    if (index === reached) return stopped ? 'stopped' : job.terminal ? 'done' : 'active'
    return stopped ? 'stopped' : 'pending'
  })
}

// SERVER strategy vocabulary → the plain-language badge (child 5b item 6). Verbatim keys: an
// unknown value renders its de-underscored words, never a crash and never a guess.
const STRATEGY_BADGES: Record<string, string> = {
  REVIEWED_RECIPE_BLUEPRINT: 'Reviewed recipe formula',
  LLM_AUTHORED: 'AI-authored formula',
}

function strategyBadge(strategy: string): string {
  return STRATEGY_BADGES[strategy] ?? strategy.toLowerCase().replace(/_/g, ' ')
}

const MEMBER_STATE_WORDS: Record<string, string> = {
  SELECTED: 'selected — formula not yet requested',
  AUTHORING: 'formula being authored',
  FORMULA_READY: 'formula ready',
  BOUND: 'formula pinned to this selection',
  BLOCKED: 'blocked',
  FAILED: 'failed',
}

function MemberCard({ member }: { member: CodeGenerationJobMember }) {
  return (
    <li className="cgw-member" data-state={member.member_state}>
      <div className="cgw-member-head">
        <strong>{member.option_id}</strong>
        <span className="badge">{strategyBadge(member.formula_strategy)}</span>
        <span className="cgw-member-state">
          {MEMBER_STATE_WORDS[member.member_state]
            ?? member.member_state.toLowerCase().replace(/_/g, ' ')}
        </span>
      </div>
      <dl className="cgw-member-facts">
        <dt>Selection</dt>
        <dd className="mono">{member.selection_revision_id}</dd>
        <dt>Formula draft</dt>
        {/* Honest absence: no draft yet means the formula was not requested or refused — the
            member state above says which. Never an invented id, never a blank box. */}
        <dd className="mono">{member.formula_draft_id ?? 'not yet requested'}</dd>
        <dt>Binding</dt>
        <dd className="mono">{member.selection_formula_binding_id ?? 'not yet pinned'}</dd>
      </dl>
      {/* Warnings are DISPLAYED (parent D3) — the route announcement, the legacy marks. */}
      {member.warnings.length > 0 && (
        <ul className="cgw-warnings">
          {member.warnings.map(code => (
            <li key={code}>⚠ <span className="mono">{code}</span></li>
          ))}
        </ul>
      )}
      {member.blockers.length > 0 && (
        <ul className="cgw-blockers">
          {member.blockers.map(code => (
            <li key={code}><span className="mono">{code}</span></li>
          ))}
        </ul>
      )}
    </li>
  )
}

const STATUS_WORDS: Record<string, string> = {
  REQUESTED: 'Requested — a worker will pick this up',
  PLANNING_FORMULAS: 'Resolving each formula’s authoring method',
  AUTHORING: 'Authoring formulas — waiting on durable draft states',
  READY_TO_BUILD: 'Every formula is ready — declaring the build',
  GENERATING_PREVIEW: 'Generating code',
  PREVIEW_READY: 'Code ready',
  BLOCKED: 'Blocked — the members below say why',
  FAILED: 'Failed — a platform problem, not a verdict about your features',
  CANCELLED: 'Cancelled',
}

export function CodeGenerationWorkspaceScreen({ jobId, navigate }: Props) {
  const [job, setJob] = useState<CodeGenerationJob | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [cancelling, setCancelling] = useState(false)
  const identityKey = useIdentityKey()

  const load = useCallback(() => {
    if (!jobId) return
    getCodeGenerationJob(jobId)
      .then((fetched) => { setJob(fetched); setError(null) })
      .catch((e: unknown) => {
        setError(e instanceof ApiError ? e.message : 'the job could not be read')
      })
  }, [jobId])

  useEffect(() => {
    load()
  }, [load, identityKey])

  // Poll while the job is live. READS ONLY — the journey is durable server-side, so this loop
  // can stop, crash or never run and nothing about the build changes.
  useEffect(() => {
    if (!job || job.terminal) return
    const timer = window.setInterval(load, 4000)
    return () => window.clearInterval(timer)
  }, [job, load])

  if (!jobId) {
    return (
      <section className="cgw">
        <h2>Generation workspace</h2>
        <p>No job id was provided — this workspace shows one build journey, and there is
          nothing to show without knowing which.</p>
      </section>
    )
  }
  if (error) {
    return (
      <section className="cgw">
        <h2>Generation workspace</h2>
        <p role="alert">{error}</p>
      </section>
    )
  }
  if (!job) return <section className="cgw"><h2>Generation workspace</h2><p>Loading…</p></section>

  const states = stageStates(job)
  return (
    <section className="cgw">
      <h2>Generation workspace</h2>
      <p className="cgw-status">
        {STATUS_WORDS[job.status] ?? job.status}
        {' '}<span className="mono">{job.status}</span>
      </p>

      <ol className="cgw-stages">
        {STAGES.map((stage, index) => (
          <li key={stage} data-state={states[index]} aria-current={states[index] === 'active'}>
            {stage}
          </li>
        ))}
      </ol>

      {/* The two sandbox stages belong to the EXECUTION workspace — separate acts, separate
          decisions (parent §3). Once code is sealed, that is where the journey continues. */}
      {job.sealed_artifact_id && (
        <p className="cgw-next">
          Code is sealed as <span className="mono">{job.sealed_artifact_id}</span>.{' '}
          <button
            type="button"
            onClick={() => navigate?.('feature-execution', {
              artifact_id: job.sealed_artifact_id ?? '',
              environment_id: job.environment_id,
              group: job.logical_group_name,
            })}
          >
            Open the execution workspace
          </button>{' '}
          to run it in the sandbox and, separately, publish the sandbox result.
        </p>
      )}

      {job.terminal_detail && (
        <div className="cgw-terminal" role="note">
          {/* The server's own words for what ended the journey — never re-derived here. */}
          <pre>{JSON.stringify(job.terminal_detail, null, 2)}</pre>
        </div>
      )}

      <h3>Selected features</h3>
      <ul className="cgw-members">
        {job.members.map(member => <MemberCard key={member.position} member={member} />)}
      </ul>

      <h3>Decisions</h3>
      {/* One row PER ACTION (parent §0.1.3) — which acts this journey performs, each with its
          own recorded decision. Shown as the server stored them. */}
      <ul className="cgw-actions">
        {job.actions.map(action => (
          <li key={action.action}>
            <span className="mono">{action.action}</span>
            {' — '}{action.state.toLowerCase()}
            {action.decision_revision_id && (
              <>{' · decision '}<span className="mono">{action.decision_revision_id.slice(0, 12)}…</span></>
            )}
          </li>
        ))}
      </ul>

      <h3>History</h3>
      <ol className="cgw-events">
        {job.events.map(event => (
          <li key={event.event_seq}>
            <span className="mono">{event.stage}</span>
            <time>{event.recorded_at}</time>
          </li>
        ))}
      </ol>

      {!job.terminal && (
        <button
          type="button"
          disabled={cancelling}
          onClick={() => {
            setCancelling(true)
            // States what it does, exactly (5b acceptance: every button label states what it
            // writes or spends): stops FUTURE stages; an in-flight provider call finishes on
            // its own lane and its draft remains reusable.
            cancelCodeGenerationJob(job.job_id)
              .then(() => load())
              .catch((e: unknown) => {
                setError(e instanceof ApiError ? e.message : 'cancel failed')
              })
              .finally(() => setCancelling(false))
          }}
        >
          Stop future stages (in-flight formula calls finish on their own)
        </button>
      )}
    </section>
  )
}
