import { useCallback, useEffect, useState } from 'react'
import {
  type ConceptConfirmationQueue,
  type ConceptDecisionItem,
  ApiError,
  getConceptConfirmations,
  listCatalogs,
  postConceptConfirmations,
} from '../api'

// CONCEPT CONFIRMATIONS — the authority-bootstrap funnel (SE-4b).
//
// The platform's AI proposes concepts; nothing governed can build on a proposal until a person
// confirms it, and confirming 150K columns one field at a time is a non-starter. This panel is
// bulk BY-EXCEPTION: one group per proposed concept, ordered by how load-bearing the concept is
// (how many governed recipe operands reference it), every column checked by default — confirm
// the batch, untick the exceptions. Each confirmation still lands as ONE attributable decision
// through the same field-decision machinery the asset screen uses; a column whose evidence
// changed underneath fails alone (409) and says so, without touching its batch siblings.

export function ConceptConfirmationPanel() {
  const [sources, setSources] = useState<string[]>([])
  const [source, setSource] = useState('')
  const [queue, setQueue] = useState<ConceptConfirmationQueue | null>(null)
  const [loadError, setLoadError] = useState('')
  const [unticked, setUnticked] = useState<Set<string>>(new Set())
  const [busy, setBusy] = useState(false)
  const [declines, setDeclines] = useState<{ object_ref: string; detail: string }[]>([])
  const [lastOutcome, setLastOutcome] = useState('')

  useEffect(() => {
    listCatalogs()
      .then(res => {
        const names = res.catalogs.map(c => c.source)
        setSources(names)
        if (names.length > 0) setSource(prev => prev || names[0])
      })
      .catch(() => setSources([]))
  }, [])

  const load = useCallback(async (src: string) => {
    setQueue(null)
    setLoadError('')
    setUnticked(new Set())
    try {
      setQueue(await getConceptConfirmations(src))
    } catch (e) {
      setLoadError(e instanceof ApiError ? e.detail : 'The confirmation queue could not be loaded.')
    }
  }, [])

  useEffect(() => {
    if (source) void load(source)
  }, [source, load])

  const toggle = (objectRef: string) => {
    setUnticked(prev => {
      const next = new Set(prev)
      if (next.has(objectRef)) next.delete(objectRef)
      else next.add(objectRef)
      return next
    })
  }

  const decide = async (concept: string, action: 'confirm_existing' | 'reject') => {
    if (!queue) return
    const group = queue.groups.find(g => g.concept === concept)
    if (!group) return
    const items: ConceptDecisionItem[] = group.columns
      .filter(col => !unticked.has(col.object_ref))
      .map(col => ({
        object_ref: col.object_ref,
        action,
        evidence_id: col.evidence_id,
        expected_latest_decision_id: col.latest_decision_id,
        expected_evidence_set_hash: col.evidence_set_hash,
        expected_policy_version: col.policy_version,
      }))
    if (items.length === 0) return
    setBusy(true)
    setDeclines([])
    setLastOutcome('')
    try {
      const outcome = await postConceptConfirmations(source, items,
        `bulk ${action === 'confirm_existing' ? 'confirm' : 'reject'}: ${concept}`)
      setDeclines(outcome.results
        .filter(r => !r.accepted)
        .map(r => ({ object_ref: r.object_ref, detail: r.detail ?? `HTTP ${r.status_code}` })))
      setLastOutcome(
        `${outcome.accepted_count} recorded, ${outcome.declined_count} declined · `
        + `${outcome.funnel.human_confirmed} of ${outcome.funnel.active} proposals now settled`)
      await load(source)
    } catch (e) {
      setLoadError(e instanceof ApiError ? e.detail : 'The batch could not be recorded.')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="panel" aria-label="Concept confirmations">
      <h2>Concept confirmations</h2>
      <p className="hint">
        The AI proposed what these columns mean. Nothing governed builds on a proposal until you
        confirm it — confirm each batch, untick the exceptions. Groups are ordered by how many
        governed recipe operands depend on the concept, so the load-bearing decisions come first.
      </p>
      {/* The same chips the governance queue above uses. This was a native <select> asking the
          same question forty pixels lower — one axis, two idioms. */}
      {sources.length > 1 && (
        <div
          className="gj-chips ccq-filter"
          role="group"
          aria-label="Catalog"
          data-testid="ccq-catalog-filter"
        >
          {sources.map(s => (
            <button
              type="button"
              key={s}
              className={s === source ? 'gj-chip gj-chip--on' : 'gj-chip'}
              aria-pressed={s === source}
              onClick={() => setSource(s)}
            >
              {s}
            </button>
          ))}
        </div>
      )}
      {loadError && <p className="field-error">{loadError}</p>}
      {lastOutcome && <p className="hint ccq-outcome">{lastOutcome}</p>}
      {declines.length > 0 && (
        <ul className="sfc-omit" aria-label="Declined items">
          {declines.map(d => (
            <li key={d.object_ref}>
              <span className="mono">{d.object_ref}</span> — {d.detail}
            </li>
          ))}
        </ul>
      )}
      {queue && (
        <>
          {/* THE ANSWER FIRST, THE SHARE AS CONTEXT. Live on cib these two rendered the other way
              round — "37 of 82 proposals settled (45%)" directly above "Nothing awaiting
              confirmation" — and read as a contradiction. Both are true: the share counts every
              active proposal on the catalog, while this queue lists only the ones a governed
              recipe operand references. The share now says which it is counting. */}
          {queue.groups.length === 0 && (
            <p className="hint" data-testid="ccq-settled">
              Nothing awaiting confirmation — every load-bearing proposal on this catalog is
              settled.
            </p>
          )}
          <p className="hint" data-testid="ccq-share">
            Across every proposal on this catalog, load-bearing or not:{' '}
            {queue.funnel.human_confirmed} of {queue.funnel.active} settled
            {' '}({Math.round(queue.funnel.confirmed_share * 100)}%).
            {queue.unreferenced_groups_omitted > 0
              && ` ${queue.unreferenced_groups_omitted} concept group(s) not referenced by any`
              + ' recipe operand are omitted here.'}
          </p>
          {queue.groups.map(group => {
            const checked = group.columns.filter(c => !unticked.has(c.object_ref)).length
            return (
              <div key={group.concept} className="ccq-group">
                <h3 className="micro-label">
                  <span className="mono">{group.concept}</span>
                  {' '}· used by {group.operand_reference_count} recipe operand
                  {group.operand_reference_count === 1 ? '' : 's'}
                  {' '}· {group.columns.length} column{group.columns.length === 1 ? '' : 's'}
                </h3>
                <ul className="ccq-columns">
                  {group.columns.map(col => (
                    <li key={col.object_ref}>
                      <label className="ccq-column">
                        <input
                          type="checkbox"
                          checked={!unticked.has(col.object_ref)}
                          onChange={() => toggle(col.object_ref)}
                        />
                        <span className="mono">{col.table}.{col.column}</span>
                        <span className="hint">{col.producer}/{col.strength}</span>
                      </label>
                    </li>
                  ))}
                </ul>
                <div className="gj-actions">
                  <button
                    type="button"
                    className="btn btn--primary"
                    disabled={busy || checked === 0}
                    onClick={() => void decide(group.concept, 'confirm_existing')}
                  >
                    Confirm {checked} as {group.concept}
                  </button>
                  <button
                    type="button"
                    className="btn"
                    disabled={busy || checked === 0}
                    onClick={() => void decide(group.concept, 'reject')}
                  >
                    Reject {checked}
                  </button>
                </div>
              </div>
            )
          })}
        </>
      )}
    </section>
  )
}
