import { type FormEvent, type ReactElement, useState } from 'react'
import {
  ApiError,
  type ConsideredSetResp,
  type Contract,
  type DraftResp,
  type Idea,
  contractConfirm,
  contractConsideredSet,
  contractDraft,
} from '../api'

// The governed two-gate feature-contract flow, driven over the existing backend endpoints:
//   brief (Gate #1 intake) -> considered set (pick one) -> draft review -> confirm (Gate #2 govern).
// The flow is stateless server-side; the screen carries intent_id + the transient draft between phases,
// and the server re-validates at draft and confirm, so nothing here can govern an unsafe contract.
type Phase = 'brief' | 'set' | 'draft' | 'done'
interface Choice {
  source: 'anchor' | 'alternative'
  optionId: string
}

const SAFE_NOT_PROVEN =
  'Every option below passed the safety gauntlet. This is a design check, not a performance claim — '
  + 'safe, not proven.'

export function ContractScreen(): ReactElement {
  const [phase, setPhase] = useState<Phase>('brief')
  const [hypothesis, setHypothesis] = useState('')
  const [objective, setObjective] = useState('')
  const [entity, setEntity] = useState('')
  const [cset, setCset] = useState<ConsideredSetResp | null>(null)
  const [choice, setChoice] = useState<Choice | null>(null)
  const [draftResp, setDraftResp] = useState<DraftResp | null>(null)
  const [contract, setContract] = useState<Contract | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  function fail(e: unknown) {
    setError(e instanceof ApiError ? e.detail : String(e))
  }

  async function generate(e: FormEvent) {
    e.preventDefault()
    setBusy(true)
    setError(null)
    try {
      const resp = await contractConsideredSet(hypothesis, objective, entity ? { entity } : {})
      setCset(resp)
      setChoice(null)
      setPhase('set')
    } catch (err) {
      fail(err)
    } finally {
      setBusy(false)
    }
  }

  async function draftSelected() {
    if (!cset || !choice) return
    setBusy(true)
    setError(null)
    try {
      const resp = await contractDraft(cset.intent_id, choice.source, choice.optionId)
      setDraftResp(resp)
      setPhase('draft')
    } catch (err) {
      fail(err)
    } finally {
      setBusy(false)
    }
  }

  async function confirm() {
    if (!draftResp) return
    setBusy(true)
    setError(null)
    try {
      const c = await contractConfirm(draftResp.draft, draftResp.intent_id)
      setContract(c)
      setPhase('done')
    } catch (err) {
      fail(err)
    } finally {
      setBusy(false)
    }
  }

  function reset() {
    setPhase('brief')
    setHypothesis('')
    setObjective('')
    setEntity('')
    setCset(null)
    setChoice(null)
    setDraftResp(null)
    setContract(null)
    setError(null)
  }

  return (
    <section className="stack">
      {error && <p role="alert" className="notice notice-error">{error}</p>}

      {phase === 'brief' && (
        <form className="stack" onSubmit={generate}>
          <label>
            Hypothesis
            <textarea
              value={hypothesis}
              onChange={e => setHypothesis(e.target.value)}
              placeholder="e.g. customers whose balance is steadily draining are about to leave"
            />
          </label>
          <label>
            Objective
            <input
              value={objective}
              onChange={e => setObjective(e.target.value)}
              placeholder="e.g. predict retail churn"
            />
          </label>
          <label>
            Entity (optional)
            <input
              value={entity}
              onChange={e => setEntity(e.target.value)}
              placeholder="e.g. customer"
            />
          </label>
          <button type="submit" disabled={busy || !hypothesis.trim() || !objective.trim()}>
            {busy ? 'Generating…' : 'Generate considered set'}
          </button>
        </form>
      )}

      {phase === 'set' && cset && (
        <div className="stack">
          <p className="muted">{SAFE_NOT_PROVEN}</p>
          {cset.recommendation && (
            <p className="muted">
              Recommended: <strong>{cset.recommendation.recommended_lens}</strong> —{' '}
              {cset.recommendation.reasoning}. {cset.recommendation.caveat}
            </p>
          )}
          <fieldset>
            <legend>Choose one feature to govern</legend>
            {cset.anchor && (
              <OptionRow idea={cset.anchor} source="anchor" choice={choice} onSelect={setChoice} />
            )}
            {cset.alternatives.map(alt => (
              <div key={alt.lens} className="stack">
                <h3>{alt.lens}</h3>
                {alt.features.map(f => (
                  <OptionRow
                    key={f.name}
                    idea={f}
                    source="alternative"
                    choice={choice}
                    onSelect={setChoice}
                  />
                ))}
              </div>
            ))}
          </fieldset>
          <button onClick={draftSelected} disabled={busy || !choice}>
            {busy ? 'Drafting…' : 'Draft selected'}
          </button>
        </div>
      )}

      {phase === 'draft' && draftResp && (
        <div className="stack">
          <h2>Review the draft contract</h2>
          <p>
            <strong>{draftResp.draft.feature_name}</strong>
          </p>
          <p>{draftResp.draft.definition}</p>
          <p className="muted">Derives from: {draftResp.draft.derives_from.join(', ') || '(none)'}</p>
          {draftResp.unresolved.length > 0 && (
            <p role="alert" className="notice">
              {draftResp.unresolved.length} unresolved warning(s) — review before governing.
            </p>
          )}
          <p className="muted">
            Approving mints a signed contract stamped DESIGN-CHECKED (gauntlet-passed) — a design check,
            not a proof it predicts well.
          </p>
          <button onClick={confirm} disabled={busy}>
            {busy ? 'Governing…' : 'Confirm & govern'}
          </button>
        </div>
      )}

      {phase === 'done' && contract && (
        <div className="stack">
          <p className="notice notice-ok">Governed contract minted.</p>
          <p>
            <strong>{contract.feature_name}</strong> v{contract.version} — DESIGN-CHECKED
          </p>
          <p className="muted">
            Contract {contract.contract_id} · feature {contract.feature_id}
          </p>
          <button onClick={reset}>Govern another</button>
        </div>
      )}
    </section>
  )
}

function OptionRow({ idea, source, choice, onSelect }: {
  idea: Idea
  source: 'anchor' | 'alternative'
  choice: Choice | null
  onSelect: (c: Choice) => void
}): ReactElement {
  const selected = choice?.source === source && choice.optionId === idea.name
  return (
    <label className="option">
      <input
        type="radio"
        name="considered-option"
        checked={selected}
        onChange={() => onSelect({ source, optionId: idea.name })}
      />
      <span>{idea.name}</span> — <span className="muted">{idea.rationale || idea.description}</span>
    </label>
  )
}
