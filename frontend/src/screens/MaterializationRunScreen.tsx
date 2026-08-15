import { useEffect, useState } from 'react'
import {
  ApiError,
  type MaterializationOutcome,
  type MaterializationRunDetail,
  getMaterializationRun,
} from '../api'

// What became of one materialization request (Phase G, task D4). The FIRST screen for these routes:
// `grep -rn 'materialization' frontend/src/` found nothing before this file, so the ingress prefix
// was added ahead of any caller and there was no UI to modify.
//
// THIS SCREEN RENDERS THE SERVER'S ANSWER AND HOLDS NO POLICY OF ITS OWN. It does not decide which
// terminal is good news, does not derive a status from a lifecycle, and does not compose a message
// out of parts. Every sentence below is either a literal label or a string the server sent, because
// the server is the only place that can know: `run_status` is FOLDED from an append-only event
// stream, and a second fold here would be a second answer.
//
// **`refused` IS AN OUTCOME, NOT AN ERROR** — the one thing this screen would most easily get wrong.
// `PUBLICATION_REFUSED` is G-1's SUCCESS terminal: the project was compiled, rendered, sealed and
// PROVED to import and construct its pipeline in a separate interpreter, and the request lands
// `committed` because that evidence is on the control plane. What is refused is PUBLICATION, and
// until an environment has a passing capability attestation that is the truthful end of every run.
// A red badge here would tell an operator their feature was rejected when nothing about it was.
//
// **HONEST ABSENCE, NEVER A FABRICATED VALUE.** Where the server sends `null` this screen says what
// the absence MEANS and shows nothing else. "Not published yet" is the load-bearing one: the
// platform names a physical target (`sandbox_feature.<group>`) for every group whether or not
// anything was ever published there, so showing that name would hand an operator a table they
// cannot query. No invented owners, no invented timestamps, no invented table names.
//
// **READ SCOPE IS SHOWN ON EVERY RUN, INCLUDING THE ONES WHERE IT WAS FINE** (SUCCESSOR 5, the
// user's decision of 2026-08-15). A deployment may declare that its engine has no authorization
// model, and a run under that declaration passes L1 without anyone having verified that the
// authorized roles may read its inputs. That is a legitimate, operator-accepted state — and it must
// be legible here, because the alternative is a screen on which a verified run and an unverified one
// look identical. Rendering it only when it is bad would make its absence meaningless, so the
// section is unconditional and the tone carries the difference.

// The server's vocabulary, and the tone each word is allowed to carry. Four are settled outcomes
// and two are in flight; NONE is styled as an error, which is why the tone is a deliberate three-way
// (`good` / `neutral` / `bad`) rather than a boolean.
const OUTCOMES: Record<MaterializationOutcome, { label: string; tone: string; blurb: string }> = {
  published: {
    label: 'Published',
    tone: 'good',
    blurb: 'The run completed and the feature group is readable at the object named below.',
  },
  refused: {
    label: 'Publication refused',
    tone: 'neutral',
    blurb:
      'The project was compiled, rendered, sealed and proved to build. What was refused is '
      + 'publication — not the feature. The reason is the refusal code below.',
  },
  failed: {
    label: 'Run failed',
    tone: 'bad',
    blurb: 'The run did not complete. What stopped it is in the reason below.',
  },
  rejected: {
    label: 'Gates rejected the output',
    tone: 'bad',
    blurb: 'A §9 gate failed inside the pipeline. The previously published partition is untouched.',
  },
  pending: {
    label: 'In progress',
    tone: 'neutral',
    blurb: 'A worker is compiling this request, or is about to.',
  },
  never_accepted: {
    label: 'Never accepted',
    tone: 'bad',
    blurb:
      'No worker ever claimed this request, so nothing ran and nothing was recorded about it. '
      + 'Check the lane configuration (FEATUREGEN_MATERIALIZE_* on the worker) and re-trigger with '
      + 'a fresh idempotency key — a re-run is a new request, and this row stays as it is.',
  },
}

// How the read-scope answer READS, per state the server can send. The sentence is always the
// server's; what this table holds is the tone, which is the same division of labour `OUTCOMES`
// makes — and getting it wrong in either direction is the whole risk here.
//
// `false` is a WARNING and not an error: the run is legitimate, its deployment declared in advance
// that its engine has no authorization model, and L1 recorded that acceptance per table. Painting it
// red would tell an operator something went wrong when the platform did exactly what it was told.
// Hiding it is worse and is the failure this whole surface exists to prevent — a reader must be able
// to tell a run whose read scope was verified from one where nobody could ask.
const READ_SCOPE: Record<'verified' | 'unverified' | 'unknown', { label: string; tone: string }> = {
  verified: { label: 'Verified by the engine', tone: 'good' },
  unverified: { label: 'NOT verified', tone: 'warning' },
  unknown: { label: 'Not recorded', tone: 'neutral' },
}

function readScopeState(verified: boolean | null): 'verified' | 'unverified' | 'unknown' {
  if (verified === null) return 'unknown'
  return verified ? 'verified' : 'unverified'
}

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err)
}

// One row of the record. `value === null` renders the ABSENCE's own meaning and nothing else — the
// caller supplies that sentence, because only the caller knows which silence it is.
function Field({ label, value, absent }: { label: string; value: string | null; absent: string }) {
  return (
    <tr>
      <th scope="row">{label}</th>
      <td>{value === null ? <span className="hint">{absent}</span> : <code>{value}</code>}</td>
    </tr>
  )
}

export function MaterializationRunScreen({ requestId }: { requestId: string }) {
  const [run, setRun] = useState<MaterializationRunDetail | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  useEffect(() => {
    if (!requestId) {
      setRun(null)
      setError(null)
      return
    }
    let live = true
    setLoading(true)
    setError(null)
    getMaterializationRun(requestId)
      .then((detail) => { if (live) setRun(detail) })
      .catch((err) => { if (live) { setRun(null); setError(errorDetail(err)) } })
      .finally(() => { if (live) setLoading(false) })
    return () => { live = false }
  }, [requestId])

  if (!requestId) {
    return (
      <section>
        <h2>Materialization run</h2>
        <p className="hint">
          No request named. Open this screen from a materialization request to see what became of it.
        </p>
      </section>
    )
  }
  if (loading) return <section><h2>Materialization run</h2><p className="hint">Loading…</p></section>
  if (error !== null) {
    return (
      <section>
        <h2>Materialization run</h2>
        <p role="alert">{error}</p>
      </section>
    )
  }
  if (run === null) return null

  const outcome = OUTCOMES[run.outcome]
  const readScope = READ_SCOPE[readScopeState(run.read_scope_verified)]
  return (
    <section>
      <h2>Materialization run</h2>
      <p>
        <strong data-testid="outcome" data-tone={outcome.tone}>{outcome.label}</strong>
        {' · '}
        <code>{run.logical_group_name}</code>
      </p>
      <p>{outcome.blurb}</p>

      <h3>What the platform recorded</h3>
      <table>
        <tbody>
          <Field label="Request" value={run.request_id} absent="—" />
          <Field
            label="Lifecycle"
            value={run.lifecycle_state}
            absent="—"
          />
          <Field
            label="Run status"
            value={run.run_status}
            // The server sends exactly one of `run_status` and `run_status_reason`, so the reason
            // IS the absence's meaning — this screen never composes one.
            absent={run.run_status_reason ?? 'no run status was recorded'}
          />
          <Field
            label="Terminal event"
            value={run.terminal_event}
            absent="this run reached no terminal event"
          />
          <Field
            label="Refusal code"
            value={run.refusal_code}
            absent="nothing was refused"
          />
          <Field label="Generation" value={run.generation_id} absent="no project was sealed" />
          <Field label="Run id" value={run.run_id} absent="no run was named" />
        </tbody>
      </table>

      <h3>Read scope</h3>
      <p>
        <strong data-testid="read-scope" data-tone={readScope.tone}>{readScope.label}</strong>
      </p>
      <p className={run.read_scope_verified === null ? 'hint' : undefined}>
        {run.read_scope_detail ?? 'nothing about read scope was recorded for this run'}
      </p>

      <h3>Published object</h3>
      {run.published_object === null ? (
        <p className="hint" data-testid="not-published">
          Not published yet — there is no object to query. The platform names a physical target for
          every group, and showing it here before a publication happened would name a table that
          does not resolve.
        </p>
      ) : (
        <table>
          <tbody>
            <Field label="Object" value={run.published_object} absent="—" />
            <Field
              label="Generation"
              value={run.published_generation_id}
              absent="not recorded"
            />
            <Field label="Published at" value={run.published_at} absent="not recorded" />
          </tbody>
        </table>
      )}
    </section>
  )
}
