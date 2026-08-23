import { useState } from 'react'
import {
  ApiError,
  type CodeGenerationPlan,
  type CodeGenerationRequestBody,
  planCodeGeneration,
  requestCodeGeneration,
} from '../api'

// Step 5b — "Prepare formulas and code": the EXPLICIT action, with the cost basis shown before
// anything is spent.
//
// **TWO CALLS, TWO MEANINGS.** "Show the plan" asks a question — the server's /plan endpoint
// writes nothing, decides nothing durably, and quotes the per-selection methods and the provider
// call envelope. "Prepare formulas and code" is THE act: it records the job and, when any member
// authors with the LLM, records the confirmed ceiling durably (§11.2 — a modal is not a money
// guard; the confirmation this component collects becomes an `llm_spend_authorization_revision`).
//
// **THE COST CONFIRMATION APPEARS ONLY WHEN AT LEAST ONE MEMBER USES THE LLM** (5b item 3), and
// its numbers come from the SERVER's quote — the envelope per member is the enforcement's own
// budget, not a browser guess. Selection updates client state and spends nothing (child D5);
// nothing here fires from an effect, an interval or a retry.
//
// **RAIL WORDING** (5b item 2): per-selection methods in plain words — "1 reviewed formula · no
// AI cost", "2 AI formulas required · …" — derived string-for-string from server strategies.

interface Props {
  body: Omit<CodeGenerationRequestBody, 'spend_approval'>
  onRequested: (jobId: string) => void
}

function railWording(plan: CodeGenerationPlan): string[] {
  const lines: string[] = []
  if (plan.deterministic_members > 0) {
    lines.push(`${plan.deterministic_members} reviewed formula${
      plan.deterministic_members === 1 ? '' : 's'} · no AI cost`)
  }
  if (plan.llm_members > 0) {
    lines.push(`${plan.llm_members} AI formula${plan.llm_members === 1 ? '' : 's'} required · `
      + `up to ${plan.estimated_provider_calls} provider calls `
      + `(${plan.call_envelope_per_llm_member} per formula — the enforcement's own budget)`)
  }
  const undecided = plan.members.filter(m => m.blockers.length > 0)
  if (undecided.length > 0) {
    lines.push(`${undecided.length} need${undecided.length === 1 ? 's' : ''} attention before `
      + 'this build can include them')
  }
  return lines
}

export function PrepareCodeGenerationAction({ body, onRequested }: Props) {
  const [plan, setPlan] = useState<CodeGenerationPlan | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // The confirmation's editable halves. Calls/tokens come pre-filled from the server's quote;
  // cost is the user's own ceiling — a number THEY set, because this component quotes work, not
  // prices (pricing is the operator's pricing_version, and inventing a default price here would
  // be the browser deciding a spend).
  const [maxTokens, setMaxTokens] = useState('')
  const [maxCost, setMaxCost] = useState('')

  const showPlan = () => {
    setBusy(true)
    planCodeGeneration(body)
      .then((fetched) => { setPlan(fetched); setError(null) })
      .catch((e: unknown) => {
        setError(e instanceof ApiError ? e.message : 'the plan could not be read')
      })
      .finally(() => setBusy(false))
  }

  const prepare = () => {
    if (!plan) return
    setBusy(true)
    const request: CodeGenerationRequestBody = { ...body }
    if (plan.spend_approval_required) {
      request.spend_approval = {
        max_calls: plan.estimated_provider_calls,
        max_tokens: Number(maxTokens),
        max_cost: maxCost,
        currency: 'USD',
        pricing_version: 'development',
        // The approval covers THIS working session, not an open-ended future: a ceiling agreed
        // now must not still authorize a spend months later (the server refuses expired ones).
        expires_at: new Date(Date.now() + 24 * 60 * 60 * 1000).toISOString(),
      }
    }
    requestCodeGeneration(request)
      .then((response) => onRequested(response.job_id))
      .catch((e: unknown) => {
        setError(e instanceof ApiError ? e.message : 'the request was refused')
      })
      .finally(() => setBusy(false))
  }

  if (!plan) {
    return (
      <div className="pcg">
        {/* A question, and labelled as one: nothing is recorded, queued or spent. */}
        <button type="button" disabled={busy} onClick={showPlan}>
          Show build plan (reads only — records and spends nothing)
        </button>
        {error && <p role="alert">{error}</p>}
      </div>
    )
  }

  const confirmationIncomplete = plan.spend_approval_required
    && (!maxTokens.trim() || !maxCost.trim())
  return (
    <div className="pcg">
      <ul className="pcg-rail">
        {railWording(plan).map(line => <li key={line}>{line}</li>)}
      </ul>
      <ul className="pcg-members">
        {plan.members.map(member => (
          <li key={member.selection_revision_id}>
            <span className="mono">{member.option_id}</span>
            {' — '}
            {member.formula_strategy === 'REVIEWED_RECIPE_BLUEPRINT'
              ? 'reviewed recipe formula (deterministic, no provider call)'
              : member.formula_strategy === 'LLM_AUTHORED'
                ? 'AI-authored, deterministically validated'
                : member.formula_strategy.toLowerCase().replace(/_/g, ' ')}
            {member.blockers.map(code => (
              <span key={code} className="pcg-blocker mono"> {code}</span>
            ))}
            {member.warnings.map(code => (
              <span key={code} className="pcg-warning mono"> ⚠ {code}</span>
            ))}
          </li>
        ))}
      </ul>

      {plan.spend_approval_required && (
        <fieldset className="pcg-spend">
          <legend>Confirm the AI cost ceiling</legend>
          <p>
            Up to {plan.estimated_provider_calls} provider calls. Your confirmation is recorded
            durably and every call reserves against it — work stops before the ceiling is
            crossed, never after.
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

      <button type="button" disabled={busy || confirmationIncomplete} onClick={prepare}>
        {plan.spend_approval_required
          ? 'Prepare formulas and code (records the job and your cost ceiling)'
          : 'Prepare formulas and code (records the job; no AI spend)'}
      </button>
      {error && <p role="alert">{error}</p>}
    </div>
  )
}
