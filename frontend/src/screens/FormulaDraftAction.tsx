import { useEffect, useRef, useState } from 'react'
import {
  ApiError,
  type FormulaDraftStatus,
  getFormulaDraft,
  requestFormulaDraft,
} from '../api'

// "Draft formula" for ONE candidate — the button, the live stage, and the result.
//
// **THE BUTTON IS THE ONLY THING THAT SPENDS.** Not a convention, the whole design: authoring a
// formula is two model calls against a paid account, so `requestFormulaDraft` appears exactly once,
// inside an onClick, and never in an effect, an interval or a retry. The polling below calls only
// `getFormulaDraft`, which reads a row and changes nothing. Selecting this candidate elsewhere on
// the screen does not reach this component at all — that separation is the product rule ("selecting
// a checkbox must not call an LLM"), and it holds here because there is no code path from a
// selection to this request.
//
// **A SECOND CLICK IS NOT A SECOND BILL.** The server is idempotent on the formula identity rather
// than on a key this component could regenerate, so a double-click returns the first draft with
// `created: false`. That is reported honestly — "already drafting" — instead of showing a fresh
// start for a request that started nothing.
//
// **POLLING, NOT STREAMING, ON PURPOSE.** It holds no connection open, survives a reload, and reads
// the same durable row the worker advances. The stage WORDS come from the server for the same
// reason the execution screen renders server blockers verbatim: one state must not be described one
// way here and another way in the API.
//
// **BLOCKED IS AN ANSWER, NOT AN ERROR.** A formula can be perfectly good and still name an
// operator this engine has never proved. It renders as a result with its named blockers — not as a
// red failure, which would send the user to look for an outage that is not happening.

interface Props {
  consideredRevisionId: string
  optionId: string
  // Shown so the user knows which candidate this formula belongs to when several are in flight.
  candidateName: string
  // How often to re-read the row while it is still moving. Injectable so tests do not sleep.
  pollMs?: number
  // Told to the parent so a selection tray can count "2 formulas ready · 2 require authoring"
  // WITHOUT this component knowing anything about selection.
  onStateChange?: (optionId: string, state: FormulaDraftStatus['state'] | null) => void
}

const DEFAULT_POLL_MS = 2000

export function FormulaDraftAction(props: Props) {
  const { consideredRevisionId, optionId, candidateName, pollMs = DEFAULT_POLL_MS } = props
  const [draftId, setDraftId] = useState<string | null>(null)
  const [status, setStatus] = useState<FormulaDraftStatus | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [requesting, setRequesting] = useState(false)
  const [reused, setReused] = useState(false)
  const [open, setOpen] = useState(false)

  // Held in a ref so the polling effect does not re-subscribe every time the parent re-renders with
  // a new closure — a fresh interval per render would multiply the reads.
  const notify = useRef(props.onStateChange)
  notify.current = props.onStateChange

  useEffect(() => {
    notify.current?.(optionId, status?.state ?? null)
  }, [optionId, status?.state])

  // READS ONLY. Nothing here starts, retries or pays for anything: it re-reads a row until that row
  // says it has stopped moving.
  useEffect(() => {
    if (draftId === null || status?.terminal) return
    let live = true
    const tick = async () => {
      try {
        const next = await getFormulaDraft(draftId)
        if (live) setStatus(next)
      } catch (err) {
        // A failed READ is not a failed draft. The worker is still going; say so rather than
        // replacing a live stage with an error that describes only this poll.
        if (live && err instanceof ApiError) setError(err.message)
      }
    }
    void tick()
    const timer = setInterval(() => void tick(), pollMs)
    return () => { live = false; clearInterval(timer) }
  }, [draftId, status?.terminal, pollMs])

  // THE ONLY CALLER of requestFormulaDraft in this application.
  async function onDraft() {
    setError(null)
    setRequesting(true)
    try {
      const started = await requestFormulaDraft(consideredRevisionId, optionId)
      setDraftId(started.formula_draft_id)
      setReused(!started.created)
      setOpen(true)
    } catch (err) {
      if (err instanceof ApiError) setError(err.message)
    } finally {
      setRequesting(false)
    }
  }

  const inFlight = draftId !== null && !status?.terminal
  // The button says what PRESSING it does; the stage line below says where the work has got to.
  // An earlier version put the server's stage wording on the button too, which rendered the same
  // sentence twice a few pixels apart and made the live region announce a control rather than a
  // change. One fact, one place.
  const label = draftId === null
    ? 'Draft formula'
    : inFlight
      ? 'Drafting…'
      : 'Redraft formula'

  return (
    <div className="formula-draft" style={{ display: 'grid', gap: 8 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexWrap: 'wrap' }}>
        <button
          type="button"
          className="btn"
          // Disabled only while a request is in flight or a draft is still moving. NOT disabled
          // after a terminal result: redrafting after the catalog moves is a legitimate act, and it
          // is a new identity, so it is honestly a new spend.
          disabled={requesting || inFlight}
          aria-describedby={status ? `draft-stage-${optionId}` : undefined}
          onClick={() => void onDraft()}
        >
          {label}
        </button>
        {/* WHAT IT COSTS, said before it is spent. A user pressing a button that calls a paid model
            twice deserves to know that is what the button does. */}
        {draftId === null && (
          <span className="hint" style={{ fontSize: 12, color: 'var(--ink-soft)' }}>
            Writes a formula for {candidateName} using the assistant. Does not select it.
          </span>
        )}
        {reused && (
          <span className="badge" title="an identical draft already existed">
            already drafting
          </span>
        )}
      </div>

      {status && (
        <div id={`draft-stage-${optionId}`} style={{ fontSize: 13 }}>
          {/* The SERVER's wording for the stage. */}
          <p style={{ margin: 0 }} aria-live="polite">{status.stage}</p>

          {status.state === 'READY' && status.formula && (
            <details open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
              <summary>Formula — {status.formula_content_hash}</summary>
              <pre style={{ overflowX: 'auto' }}>
                {JSON.stringify(status.formula, null, 2)}
              </pre>
            </details>
          )}

          {/* A PRODUCT RESULT, rendered as one. Each blocker carries the server's own sentence, so
              a code is never explained one way here and another way on the execution screen. */}
          {status.state === 'BLOCKED' && (
            <div role="note">
              <p style={{ margin: '4px 0' }}>
                This formula cannot run on the configured engine yet:
              </p>
              <ul>
                {status.blockers.map((blocker) => (
                  <li key={blocker.code}>
                    <code>{blocker.code}</code> <span>{blocker.reason}</span>
                  </li>
                ))}
              </ul>
            </div>
          )}

          {/* FAILED is the only red one — it means the platform could not finish, which is somebody
              else's problem to fix and worth distinguishing from a blocked formula. */}
          {status.state === 'FAILED' && (
            <p className="error" role="alert">{status.failure_reason}</p>
          )}
        </div>
      )}

      {error && <p className="error" role="alert">{error}</p>}
    </div>
  )
}
