import { useState } from 'react'
import {
  ApiError,
  type AnalysisClarification,
  type AnalysisPlanResponse,
  type AnalysisPreview,
  clarifyAnalysis,
  planAnalysis,
} from '../api'

// The analysis workspace: ask a question, see what WOULD run, and answer what the system could not
// decide. Four honesty rules the design does not get to override:
//
//   * THERE IS NO RUN BUTTON. The API has no execute endpoint — execution needs bindings and an
//     eligibility policy a deployment must configure first. A disabled "Run" would imply the button
//     appears once you tick something; the screen instead says which of the four gaps is open and
//     who closes it.
//   * FINDINGS ARE NOT BEHIND A TOGGLE. They are what the answer would rest on — a currency that
//     varies, an unconfirmed join identity, a definition of "counts" nobody agreed to. Collapsing
//     them by default is how a disclosure becomes decorative.
//   * TRUNCATED RETRIEVAL IS STATED. If the catalog was clipped to fit the prompt, the plan rests on
//     a narrower view than exists, and a silent clip reads as "this considered everything".
//   * THE SQL IS SHOWN AS-IS. It is byte-identical to what the executor would compile, so it is the
//     one artifact that cannot drift from the intent above it.

// The blocked-by vocabulary in plain words, and — the part that matters — WHO acts. A code alone
// sends everyone to the same shrug.
const BLOCKED_WORDS: Record<string, { what: string; who: string }> = {
  PHYSICAL_BINDING_ABSENT: {
    what: 'no physical binding: the catalog knows this table by name but not where it lives',
    who: 'a platform operator registers the connection and binding',
  },
  ELIGIBILITY_ABSENT: {
    what: 'nobody has defined which rows count for this table',
    who: 'a data owner decides which statuses are activity, and whether a reversal counts',
  },
  EXECUTION_INPUTS_ABSENT: {
    what: 'the population is not yet declared, so there is no spine to hang the periods off',
    who: 'answer the population question above',
  },
  SCHEMA_NOT_ALLOWED: {
    what: 'the connection that reaches this table no longer permits its schema',
    who: 'a governance owner restores the grant, or confirms it should stay withdrawn',
  },
  NOT_ACTIVE: {
    what: 'the connection that reaches this table is switched off',
    who: 'a platform operator reactivates it',
  },
}

const FINDING_WORDS: Record<string, string> = {
  TIME_ANCHOR_UNGOVERNED: 'the time column is file-declared, not a governed fact',
  AVAILABILITY_BASIS_UNKNOWN: 'no availability fact — a period may include rows that had not landed',
  AVAILABILITY_LAG_UNAPPLIED: 'the cutoff has not been shifted back by the availability lag',
  CURRENCY_MIXED: 'amounts are aggregated across currencies that vary or are unknown',
  GRAIN_NOT_ESTABLISHED: 'counting per entity without a governed unique grain',
  JOIN_IDENTITY_UNCONFIRMED: 'the identifier link joining the catalogs is unreviewed',
  JOIN_IDENTITY_UNAVAILABLE: 'the identifier link is rejected, stale or expired',
  SMALL_CELL_RISK: 'a group is small enough to identify the individuals in it',
  DIMENSION_UNGOVERNED: 'a group-by column carries no business concept',
  CODE_SET_INCOMPLETE: 'a value came from observed samples, not a known domain',
  MEASURE_NOT_NUMERIC: 'the measure is not a numeric column',
  ELIGIBILITY_UNCONFIRMED: 'which rows count was proposed but nobody has confirmed it',
}

function words(map: Record<string, string>, code: string): string {
  return map[code] ?? code.replaceAll('_', ' ').toLowerCase()
}

function shortRef(ref: string): string {
  // `ftr::dpl_eib.tran_repos.cif_id` -> `tran_repos.cif_id`. The source and schema are noise once a
  // user is looking at one question; the full ref stays in the title attribute.
  const rest = ref.includes('::') ? ref.slice(ref.indexOf('::') + 2) : ref
  const parts = rest.split('.')
  return parts.length > 2 ? parts.slice(-2).join('.') : rest
}

function Findings({ preview }: { preview: AnalysisPreview }) {
  if (preview.findings.length === 0) {
    return (
      <p className="adg-finding-clean">
        Every fact this answer would rest on is governed.
      </p>
    )
  }
  return (
    <ul className="adg-findings">
      {preview.findings.map(f => (
        <li key={`${f.code}:${f.subject}`}>
          <span className="adg-finding-text">{words(FINDING_WORDS, f.code)}</span>
          <span className="adg-finding-subject" title={f.subject}>{shortRef(f.subject)}</span>
          {f.clears_when && <span className="adg-finding-clears">Clears when {f.clears_when}.</span>}
        </li>
      ))}
    </ul>
  )
}

function Blocked({ preview }: { preview: AnalysisPreview }) {
  if (!preview.blocked_by) return null
  const known = BLOCKED_WORDS[preview.blocked_by.code]
  return (
    <section className="adg-blocked" aria-labelledby="adg-blocked-h">
      <h3 id="adg-blocked-h">This cannot run yet</h3>
      <p>{known ? known.what : words({}, preview.blocked_by.code)}</p>
      {known && <p className="adg-blocked-who">Who closes it: {known.who}.</p>}
      {preview.blocked_by.subject && (
        <p className="adg-blocked-subject" title={preview.blocked_by.subject}>
          {shortRef(preview.blocked_by.subject)}
        </p>
      )}
    </section>
  )
}

function Clarification(
  { clarification, onAnswer, busy }:
  { clarification: AnalysisClarification; onAnswer: (code: string, chosen: string[]) => void
    busy: boolean },
) {
  const [picked, setPicked] = useState<string[]>([])
  const toggle = (value: string) => {
    setPicked(prev => clarification.allows_multiple
      ? (prev.includes(value) ? prev.filter(v => v !== value) : [...prev, value])
      : [value])
  }
  return (
    <li className="adg-clarification">
      <p className="adg-clarification-q">
        {clarification.question}
        {clarification.optional && <span className="adg-optional"> (optional)</span>}
      </p>
      <div className="adg-clarification-options">
        {clarification.options.map(o => (
          <button
            key={o.value}
            type="button"
            className={picked.includes(o.value) ? 'adg-option selected' : 'adg-option'}
            aria-pressed={picked.includes(o.value)}
            title={o.value}
            onClick={() => toggle(o.value)}
          >
            {shortRef(o.value)}{o.label && o.label !== o.value ? ` — ${o.label}` : ''}
          </button>
        ))}
      </div>
      <button
        type="button"
        className="adg-answer"
        disabled={busy || picked.length === 0}
        onClick={() => onAnswer(clarification.code, picked)}
      >
        Answer
      </button>
    </li>
  )
}

export function AnalysisWorkspaceScreen() {
  const [question, setQuestion] = useState('')
  const [asked, setAsked] = useState('')
  const [result, setResult] = useState<AnalysisPlanResponse | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function run(fn: () => Promise<AnalysisPlanResponse>, q: string) {
    setBusy(true)
    setError('')
    try {
      setResult(await fn())
      setAsked(q)
    } catch (e) {
      // A 422 here is about the QUESTION — nothing matched, or the model could not express it — so
      // it is shown as an answer, not as a failure of the system.
      setError(e instanceof ApiError ? e.detail : String(e))
      setResult(null)
    } finally {
      setBusy(false)
    }
  }

  const preview = result?.preview
  return (
    <div className="adg-workspace">
      <h2>Ask a question</h2>
      <p className="adg-workspace-lede">
        Nothing runs from here. A question is planned and checked against the catalog so you can see
        what it would compute, and what that answer would rest on, before anyone runs it.
      </p>

      <form
        className="adg-ask"
        onSubmit={e => { e.preventDefault(); void run(() => planAnalysis(question), question) }}
      >
        <label htmlFor="adg-question">Your question</label>
        <input
          id="adg-question"
          value={question}
          placeholder="which customers had fewer transactions this month than last"
          onChange={e => setQuestion(e.target.value)}
        />
        <button type="submit" disabled={busy || question.trim() === ''}>Plan it</button>
      </form>

      {error && <p className="adg-error" role="alert">{error}</p>}

      {preview && (
        <>
          <section className="adg-plan" aria-labelledby="adg-plan-h">
            <h3 id="adg-plan-h">What this would compute</h3>
            <dl className="adg-plan-fields">
              <dt>Per</dt><dd>{preview.entity || '—'}</dd>
              <dt>Measure</dt><dd>{preview.measure || '—'}</dd>
              {preview.comparison && (<><dt>Comparison</dt><dd>{preview.comparison}</dd></>)}
              {preview.dimensions.length > 0 && (
                <>
                  <dt>Split by</dt>
                  <dd>{preview.dimensions.map(shortRef).join(', ')}</dd>
                </>
              )}
              {preview.periods.map(p => (
                <div key={p.label} className="adg-period">
                  <dt>{p.label}</dt>
                  <dd>{p.partitions.join(', ')}</dd>
                </div>
              ))}
            </dl>
            {preview.plan_hash && (
              <p className="adg-plan-hash" title="Identity of the computation, not the wording">
                plan {preview.plan_hash}
              </p>
            )}
          </section>

          <section className="adg-rests-on" aria-labelledby="adg-rests-h">
            <h3 id="adg-rests-h">What the answer would rest on</h3>
            <Findings preview={preview} />
          </section>

          <Blocked preview={preview} />

          {result.retrieval && result.retrieval.dropped_columns > 0 && (
            <p className="adg-truncated">
              {result.retrieval.dropped_columns} matching columns did not fit the prompt, so this
              plan rests on a narrower view of the catalog than exists.
            </p>
          )}

          {result.clarifications.length > 0 && (
            <section className="adg-clarifications" aria-labelledby="adg-clar-h">
              <h3 id="adg-clar-h">Questions the system cannot answer for you</h3>
              <ul>
                {result.clarifications.map(c => (
                  <Clarification
                    key={c.code}
                    clarification={c}
                    busy={busy}
                    onAnswer={(code, chosen) =>
                      void run(() => clarifyAnalysis(asked, code, chosen), asked)}
                  />
                ))}
              </ul>
            </section>
          )}

          {preview.sql && (
            <section className="adg-sql" aria-labelledby="adg-sql-h">
              <h3 id="adg-sql-h">The statement that would run</h3>
              <pre>{preview.sql}</pre>
            </section>
          )}
        </>
      )}
    </div>
  )
}
