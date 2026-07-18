import { type FormEvent, useEffect, useState } from 'react'
import {
  ApiError,
  type GateCohort,
  type GateEvaluation,
  type GateVerdict,
  evaluateGate,
  listGateCohorts,
} from '../api'

// Gate evaluation console (Phase 3C.1): the AUTHORITY-ONLY internal surface where an operator
// picks a shadow cohort + date window, triggers the machine gate, and reads the verdict. This
// screen ONLY triggers + displays — the request body carries {cohort, since, until} and nothing
// else; every count and PASS/FAIL is assembled server-side from the persisted WORM stores. There
// is deliberately NO sign/approve affordance here: a machine PASS is necessary but not
// sufficient, and the go-live decision lives with a human reading the population, off this
// screen. Follows GovernanceReviewScreen's shape: form -> submit handler -> result panels.

// The five named conditions behind the verdict, in gate order. Chip text stays lowercase
// (ok/failed) so the uppercase PASS/FAIL badge is the single verdict on screen.
const GATE_CONDITIONS: { key: keyof GateVerdict; label: string }[] = [
  { key: 'gate1_capture', label: 'Gate 1 · capture' },
  { key: 'gate2a_map', label: 'Gate 2a · map' },
  { key: 'gate3_gold', label: 'Gate 3 · gold suite' },
  { key: 'gate5_stability', label: 'Gate 5 · stability' },
  { key: 'gate6_drift', label: 'Gate 6 · drift' },
]

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err)
}

function isoDate(d: Date): string {
  return d.toISOString().slice(0, 10)
}

// A cohort is a producer commit sha — show it truncated with its run span for picking.
function cohortLabel(c: GateCohort): string {
  return `${c.cohort.slice(0, 12)} · ${c.run_count} run${c.run_count === 1 ? '' : 's'}`
}

// name -> count maps (excluded reasons, headline, breakdown, outcome matrix) share one renderer.
function CountTable({
  caption,
  keyHeader,
  data,
  emptyLabel,
}: {
  caption: string
  keyHeader: string
  data: Record<string, number>
  emptyLabel: string
}) {
  const entries = Object.entries(data)
  return (
    <>
      <h3>{caption}</h3>
      {entries.length === 0 ? (
        <p className="hint">{emptyLabel}</p>
      ) : (
        <table>
          <thead>
            <tr>
              <th>{keyHeader}</th>
              <th className="num">Runs</th>
            </tr>
          </thead>
          <tbody>
            {entries.map(([name, n]) => (
              <tr key={name}>
                <td className="mono">{name}</td>
                <td className="num tabular-nums">{n}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </>
  )
}

export function GateEvaluationScreen() {
  const [cohorts, setCohorts] = useState<GateCohort[] | null>(null)
  const [cohortsError, setCohortsError] = useState('')
  const [cohort, setCohort] = useState('')
  // Default window: the trailing 30 days INCLUDING today. Sent verbatim as the date-input value
  // (YYYY-MM-DD); the server pins it to midnight UTC. The window is half-open [since, until), so
  // `until` defaults to TOMORROW — defaulting it to today would exclude every same-day run.
  const [since, setSince] = useState(() => isoDate(new Date(Date.now() - 30 * 86_400_000)))
  const [until, setUntil] = useState(() => isoDate(new Date(Date.now() + 86_400_000)))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')
  // The result is kept WITH the window it was evaluated for — editing the form after a run must
  // never relabel a displayed verdict, so the panel echoes its own request, not the live inputs.
  const [evaluated, setEvaluated] = useState<{
    req: { cohort: string; since: string; until: string }
    res: GateEvaluation
  } | null>(null)

  useEffect(() => {
    let cancelled = false
    listGateCohorts()
      .then(list => {
        if (cancelled) return
        setCohorts(list)
        // Pre-select the newest cohort (the route orders by last run, newest first).
        if (list.length > 0) setCohort(prev => (prev === '' ? list[0].cohort : prev))
      })
      .catch((err: unknown) => {
        if (!cancelled) setCohortsError(errorDetail(err))
      })
    return () => {
      cancelled = true
    }
  }, [])

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (busy) return
    if (!cohort) {
      setError('No cohort selected — pick a shadow cohort to evaluate.')
      return
    }
    setBusy(true)
    setError('')
    const req = { cohort, since, until }
    try {
      const res = await evaluateGate(req)
      setEvaluated({ req, res })
    } catch (err) {
      // A stale verdict is worse than none on a go-live console: clear the panel on failure.
      setEvaluated(null)
      setError(errorDetail(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section>
      <div className="callout callout--warn">
        <div className="callout-body">
          <p>
            <strong>
              A machine PASS is necessary but not sufficient — review the population before
              deciding to go live.
            </strong>{' '}
            This console only evaluates and displays. Nothing here signs, approves, or changes
            what the platform serves.
          </p>
        </div>
      </div>

      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="gate-cohort">Cohort</label>
          <select
            id="gate-cohort"
            value={cohort}
            disabled={busy}
            onChange={e => setCohort(e.target.value)}
          >
            {(cohorts === null || cohorts.length === 0) && (
              <option value="">
                {cohorts === null ? 'loading cohorts…' : 'no shadow cohorts recorded'}
              </option>
            )}
            {(cohorts ?? []).map(c => (
              <option key={c.cohort} value={c.cohort}>
                {cohortLabel(c)}
              </option>
            ))}
          </select>
        </div>
        <div className="field">
          <label htmlFor="gate-since">Since</label>
          <input
            id="gate-since"
            type="date"
            value={since}
            disabled={busy}
            onChange={e => setSince(e.target.value)}
          />
        </div>
        <div className="field">
          <label htmlFor="gate-until">Until</label>
          <input
            id="gate-until"
            type="date"
            value={until}
            disabled={busy}
            onChange={e => setUntil(e.target.value)}
          />
        </div>
        <button type="submit" className="btn btn--primary" disabled={busy}>
          {busy ? 'Evaluating…' : 'Evaluate'}
        </button>
      </form>
      {cohortsError && (
        <p role="alert" className="error">
          Could not load cohorts: {cohortsError}
        </p>
      )}
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}

      {evaluated && (
        <>
          <h2>Machine verdict</h2>
          <div className="q-head">
            <span
              className={`badge ${evaluated.res.verdict.passed ? 'gj-verified' : 'gj-rejected'}`}
            >
              {evaluated.res.verdict.passed ? 'PASS' : 'FAIL'}
            </span>
            <span className="gj-score mono">
              {evaluated.req.cohort.slice(0, 12)} · {evaluated.req.since} → {evaluated.req.until}
            </span>
          </div>
          <ul className="rows">
            {GATE_CONDITIONS.map(g => (
              <li className="row q-item" key={g.key}>
                <div className="q-head">
                  <span className="gj-kind">{g.label}</span>
                  <span
                    className={`badge ${evaluated.res.verdict[g.key] ? 'gj-verified' : 'gj-rejected'}`}
                  >
                    {evaluated.res.verdict[g.key] ? 'ok' : 'failed'}
                  </span>
                </div>
              </li>
            ))}
          </ul>
          {evaluated.res.reasons.length > 0 && (
            <>
              <h3>Failed conditions</h3>
              <ul className="rows">
                {evaluated.res.reasons.map(reason => (
                  <li className="row q-item" key={reason}>
                    <p className="q-note">{reason}</p>
                  </li>
                ))}
              </ul>
            </>
          )}

          <h2>Coverage</h2>
          <div className="stats" role="group" aria-label="Window coverage">
            <div className="stat">
              <b className="tabular-nums">{evaluated.res.coverage.dispatched_in_range}</b>{' '}
              dispatched in range
            </div>
            <div className="stat">
              <b className="tabular-nums">{evaluated.res.coverage.qualifying}</b> qualifying
            </div>
          </div>
          <CountTable
            caption="Excluded from the window"
            keyHeader="Reason"
            data={evaluated.res.coverage.excluded}
            emptyLabel="No dispatched runs were excluded."
          />

          <h2>Population</h2>
          <div className="stats" role="group" aria-label="Population totals">
            <div className="stat">
              <b className="tabular-nums">{evaluated.res.population.denominator}</b> denominator
            </div>
            <div className="stat">
              <b className="tabular-nums">{evaluated.res.population.numerator}</b> numerator
            </div>
          </div>
          <CountTable
            caption="Headline (by primary reason)"
            keyHeader="Primary reason"
            data={evaluated.res.population.headline_by_primary}
            emptyLabel="Empty — no numerator observations."
          />
          <CountTable
            caption="Breakdown (by category)"
            keyHeader="Category"
            data={evaluated.res.population.breakdown_by_category}
            emptyLabel="Empty — no numerator observations."
          />
          <CountTable
            caption="Recipe outcome matrix"
            keyHeader="Outcome · compile status"
            data={evaluated.res.population.recipe_outcome_matrix}
            emptyLabel="Empty — no selected observations."
          />
          <p className="hint mono">
            evaluator {evaluated.res.versions.evaluator} · cohort {evaluated.res.versions.cohort}
          </p>
        </>
      )}
    </section>
  )
}
