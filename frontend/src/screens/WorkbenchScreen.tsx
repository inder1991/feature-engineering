// One guided feature-generation flow: a goal + scope hero with two peer paths (Generate
// candidates through the engine, or Write definitions myself through the batch composer), one
// shared candidate list, and a selection tray with an explicit confirm before anything registers.
//
// Invariants carried over from the hardening campaign:
// - Lineage comes ONLY from backend-resolved pairs (FeatureIdea.derives_pairs for generated
//   candidates, the drafted-against source snapshot for drafts), never from typed context.
// - registerFeature fires only after the explicit Confirm registration step, exactly once per
//   candidate (batch in-flight ref + per-candidate registered state).
// - Every fetch handler carries an out-of-order guard (monotonic sequence refs).
// - Scope edits invalidate candidates: source edits clear everything (draft snapshots no longer
//   match the context), entity/target edits clear generated candidates only.
import { type FormEvent, type ReactNode, useRef, useState } from 'react'
import {
  ApiError, type FeatureFreshness, type FeatureIdea, type FeatureSpecIn, type JoinStep,
  type Recipe, featureFreshness, featureRecipe, recommendFeatures, registerFeature,
} from '../api'

const HELP_STYLE = { fontSize: 12 } as const
// Solid ok chip (index.css has no fresh badge class; mirrors .badge.stale's solid treatment).
const OK_SOLID_CHIP_STYLE = {
  background: 'var(--ok-solid)', borderColor: 'transparent', color: 'var(--chip-ink)',
} as const
// Sticky-feel selection tray: the last row of the candidate list, pinned while the list scrolls.
const TRAY_STYLE = {
  position: 'sticky', bottom: 0, zIndex: 1, background: 'var(--surface)',
  borderBottomLeftRadius: 'var(--radius-panel)', borderBottomRightRadius: 'var(--radius-panel)',
  flexWrap: 'wrap', gap: 12,
} as const

const EXAMPLE_GOAL = 'predict churn'

// One definition per line. The newline-separated example teaches the batch shape at a glance.
const DESCRIBE_PLACEHOLDER =
  'One feature per line, e.g.\ntotal spend per customer over the last 90 days\ndays since last transaction'

const WARN_GLYPH = 'M8 2.5 1.5 13.25h13L8 2.5ZM8 6.75v3M8 12v.01'

function CalloutGlyph({ d }: { d: string }) {
  return (
    <span className="callout-glyph" aria-hidden="true">
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d={d} />
      </svg>
    </span>
  )
}

// Peer-path card icons (plus-in-circle for Generate, pencil for Write definitions). Decorative:
// the card title text carries the meaning.
function PathGlyph({ children }: { children: ReactNode }) {
  return (
    <svg
      width="16"
      height="16"
      viewBox="0 0 16 16"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.5"
      strokeLinecap="round"
      strokeLinejoin="round"
      aria-hidden="true"
      focusable="false"
    >
      {children}
    </svg>
  )
}

// Registered rows swap their checkbox for this ok mark; the "Registered <id>" text carries the
// state, so the glyph is decorative (color never works alone).
function CheckGlyph() {
  return (
    <span
      aria-hidden="true"
      style={{
        display: 'inline-flex', width: 32, height: 32, flex: 'none',
        alignItems: 'center', justifyContent: 'center', color: 'var(--ok)',
      }}
    >
      <svg
        width="16"
        height="16"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.5"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="m3 8.5 3.5 3.5L13 4.5" />
      </svg>
    </span>
  )
}

// Uploaded cardinality is unvalidated free text. Only a normalized N:1 or 1:1 hop is known-safe;
// anything else ('1:N', '1:n', 'one_to_many', ...) can multiply rows, and a missing value means
// fan-out cannot be ruled out.
const SAFE_CARDINALITIES = new Set(['N:1', '1:1'])

function normalizeCardinality(raw: string | null): string | null {
  const value = raw?.trim().toUpperCase()
  return value ? value : null
}

function stepFansOut(raw: string | null): boolean {
  const norm = normalizeCardinality(raw)
  return norm !== null && !SAFE_CARDINALITIES.has(norm)
}

// Suggested feature name for a draft: a slug of the description, editable before selection.
function slugFrom(text: string): string {
  return text
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '_')
    .replace(/^_+|_+$/g, '')
    .slice(0, 63)
}

// Candidates carry per-fetch keys (sequence + index + name): LLM-chosen names are not unique
// across rounds, so keying registered state by name alone would show phantom "Registered" on a
// fresh, never-registered candidate that reuses an old name.
interface GeneratedCandidate {
  kind: 'generated'
  key: string
  idea: FeatureIdea
}

// A described feature drafted through /features/recipe. Recipes are single-catalog by API
// contract, so the source it was drafted against is snapshotted and registration lineage uses
// [snapshotSource, ref] pairs; the live source field may have changed since.
interface DraftCandidate {
  kind: 'draft'
  key: string
  name: string
  description: string
  recipe: Recipe
  snapshotSource: string
}

type Candidate = GeneratedCandidate | DraftCandidate

interface Registration {
  id: string
  freshness: FeatureFreshness | null
}

function specFor(candidate: Candidate): FeatureSpecIn {
  if (candidate.kind === 'generated') {
    const { idea } = candidate
    return {
      name: idea.name, description: idea.description, grain_table: idea.grain_table,
      aggregation: idea.aggregation, as_of_column: null,
      // Lineage comes from the pairs the backend resolved at recommend time, never from the
      // typed source context: the typed source can differ from where a derive actually lives.
      derives_from: idea.derives_pairs.map(([catalog_source, object_ref]) => ({
        catalog_source, object_ref })),
    }
  }
  const { recipe } = candidate
  return {
    name: candidate.name.trim(), description: candidate.description,
    grain_table: recipe.grain_table, aggregation: recipe.aggregation,
    as_of_column: recipe.as_of_column,
    // Recipes are single-catalog: every derive lives in the snapshotted source.
    derives_from: recipe.derives_from.map(object_ref => ({
      catalog_source: candidate.snapshotSource, object_ref })),
  }
}

function JoinPathDetails({ steps }: { steps: JoinStep[] }) {
  if (steps.length === 0) return null
  const fansOut = steps.some(s => stepFansOut(s.cardinality))
  const hasUnknownHop = steps.some(s => normalizeCardinality(s.cardinality) === null)
  return (
    <details>
      <summary style={{ cursor: 'pointer', padding: '6px 0', fontWeight: 500, color: 'var(--ink-soft)' }}>
        Join path ({steps.length} {steps.length === 1 ? 'hop' : 'hops'})
      </summary>
      <ol className="mono" style={{ margin: '8px 0 0', paddingLeft: 22, display: 'grid', gap: 4 }}>
        {steps.map(s => (
          <li key={`${s.from_ref}->${s.to_ref}`}>
            {s.from_ref} → {s.to_ref}{' '}
            <span
              style={
                stepFansOut(s.cardinality)
                  ? { color: 'var(--warn)', fontWeight: 600 }
                  : { color: 'var(--ink-soft)' }
              }
            >
              ({normalizeCardinality(s.cardinality) === null
                ? 'cardinality unknown'
                : s.cardinality})
            </span>
          </li>
        ))}
      </ol>
      {hasUnknownHop && (
        <p className="hint" style={{ marginTop: 8 }}>
          Cardinality is missing on at least one hop, so fan-out cannot be ruled out. Confirm the
          join direction before registering this feature.
        </p>
      )}
      {fansOut && (
        <div className="callout callout--warn">
          <CalloutGlyph d={WARN_GLYPH} />
          <div className="callout-body">
            <p>
              <strong>Fan-out.</strong> A one-to-many hop multiplies rows. Aggregate before
              joining or the feature will double-count.
            </p>
          </div>
        </div>
      )}
    </details>
  )
}

export function WorkbenchScreen() {
  const [goal, setGoal] = useState('')
  const [source, setSource] = useState('')
  const [entity, setEntity] = useState('')
  const [target, setTarget] = useState('')
  const [generated, setGenerated] = useState<GeneratedCandidate[] | null>(null)
  const [drafts, setDrafts] = useState<DraftCandidate[]>([])
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [registered, setRegistered] = useState<Record<string, Registration>>({})
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [screenedTarget, setScreenedTarget] = useState<string | null>(null)
  const [scopeChanged, setScopeChanged] = useState(false)
  const [describeOpen, setDescribeOpen] = useState(false)
  const [describeText, setDescribeText] = useState('')
  // Per-line draft failures, "Line N: <detail>", shown in the composer while the successful
  // lines still draft. Distinct from the top notice, which carries deployment-level facts.
  const [draftErrors, setDraftErrors] = useState<string[]>([])
  const [generating, setGenerating] = useState(false)
  const [drafting, setDrafting] = useState(false)
  const [confirmingBatch, setConfirmingBatch] = useState(false)
  const [batchBusy, setBatchBusy] = useState(false)
  const [notice, setNotice] = useState('')
  // Out-of-order guards: only the latest request per handler may apply its response.
  const generateSeq = useRef(0)
  const draftSeq = useRef(0)
  // Reentry guard for the draft batch: a second submit while a batch is in flight is a no-op,
  // so each line's recipe fires exactly once even before the disabled attribute lands.
  const draftInFlight = useRef(false)
  // Reentry guard for the register batch: state updates are async, so a double click on
  // Confirm registration could otherwise start two batches before the disabled attribute lands.
  const batchInFlight = useRef(false)

  const candidates: Candidate[] = [...(generated ?? []), ...drafts]
  // One definition per non-empty line: the button label and its gating read this directly.
  const draftLines = describeText.split('\n').map(line => line.trim()).filter(Boolean)
  // Only generated candidates pass the design gauntlet, so the design-checked stamp and its one
  // help line appear only when the list holds at least one generated candidate.
  const hasGenerated = (generated?.length ?? 0) > 0
  // Selection is the intersection of the set and the live candidate list: keys from cleared
  // rounds are inert, and registered candidates can never re-enter a batch.
  const selectedCandidates = candidates.filter(c => selected.has(c.key) && !registered[c.key])

  function fail(err: unknown) {
    setNotice(
      err instanceof ApiError && err.status === 503
        ? 'AI assist is not configured on this deployment: no LLM provider is enabled.'
        : err instanceof ApiError
          ? err.detail
          : String(err),
    )
  }

  function changeSource(value: string) {
    setSource(value)
    // Generated candidates were produced for the previous source context, and draft snapshots
    // no longer match it either: a source edit clears everything.
    const hadCandidates = candidates.length > 0
    setGenerated(null)
    setDrafts([])
    setSelected(new Set())
    setRegistered({})
    setErrors({})
    setDraftErrors([])
    setScreenedTarget(null)
    setConfirmingBatch(false)
    if (hadCandidates) setScopeChanged(true)
  }

  // Entity and target edits invalidate generated candidates (they were gathered and screened
  // for the previous scope). Drafts survive: their snapshot source is unchanged.
  function invalidateGenerated() {
    const hadGenerated = (generated?.length ?? 0) > 0
    setGenerated(null)
    setSelected(new Set())
    setScreenedTarget(null)
    setConfirmingBatch(false)
    if (hadGenerated) setScopeChanged(true)
  }

  function changeEntity(value: string) {
    setEntity(value)
    invalidateGenerated()
  }

  function changeTarget(value: string) {
    setTarget(value)
    invalidateGenerated()
  }

  async function generate(e: FormEvent) {
    e.preventDefault()
    const objective = goal.trim()
    if (!objective) return
    const seq = ++generateSeq.current
    setNotice('')
    setScopeChanged(false)
    setGenerating(true)
    try {
      const ideas = await recommendFeatures(
        objective, source.trim() || null, target.trim() || null, entity.trim() || null)
      if (seq !== generateSeq.current) return
      setGenerated(ideas.map((idea, i) => ({
        kind: 'generated' as const, key: `g${seq}:${i}:${idea.name}`, idea,
      })))
      setScreenedTarget(target.trim() || null)
      setConfirmingBatch(false)
    } catch (err) {
      if (seq !== generateSeq.current) return
      setGenerated(null)
      setScreenedTarget(null)
      fail(err)
    } finally {
      if (seq === generateSeq.current) setGenerating(false)
    }
  }

  async function draftCandidates(e: FormEvent) {
    e.preventDefault()
    if (draftInFlight.current) return
    // Capture the source once for the whole batch: every recipe drafts against one snapshot,
    // so a mid-batch scope edit cannot split the batch across sources.
    const snapshotSource = source.trim()
    const lines = describeText.split('\n').map(line => line.trim()).filter(Boolean)
    if (lines.length === 0 || !snapshotSource) return
    const seq = ++draftSeq.current
    draftInFlight.current = true
    setNotice('')
    setDraftErrors([])
    setDrafting(true)
    const fresh: DraftCandidate[] = []
    const failedLines: string[] = []
    const lineErrors: string[] = []
    let providerErr: ApiError | null = null
    try {
      // Sequential, in line order: deterministic and kind to the backend. A rejected line is
      // isolated; the surviving lines still draft.
      for (let i = 0; i < lines.length; i++) {
        const query = lines[i]
        try {
          const recipe = await featureRecipe(query, snapshotSource)
          fresh.push({
            kind: 'draft' as const, key: `d${seq}:${i}`, name: slugFrom(query),
            description: query, recipe, snapshotSource,
          })
        } catch (err) {
          failedLines.push(query)
          if (err instanceof ApiError && err.status === 503) {
            // A missing provider is a deployment fact, not a per-line problem: it surfaces as
            // the one honest notice the generate path uses, never as N identical line errors.
            providerErr = err
          } else {
            lineErrors.push(
              `Line ${i + 1}: ${err instanceof ApiError ? err.detail : String(err)}`)
          }
        }
      }
      if (seq !== draftSeq.current) return
      if (fresh.length > 0) setDrafts(prev => [...prev, ...fresh])
      // Keep only the failed lines so a retry is one click away; a clean batch clears fully.
      setDescribeText(failedLines.join('\n'))
      setDraftErrors(lineErrors)
      if (providerErr) fail(providerErr)
    } finally {
      draftInFlight.current = false
      if (seq === draftSeq.current) setDrafting(false)
    }
  }

  function renameDraft(key: string, value: string) {
    setDrafts(prev => prev.map(d => (d.key === key ? { ...d, name: value } : d)))
    if (!value.trim()) {
      // A draft without a name cannot be registered: drop it from the selection too.
      setSelected(prev => {
        if (!prev.has(key)) return prev
        const next = new Set(prev)
        next.delete(key)
        return next
      })
    }
  }

  function toggleSelect(key: string) {
    // Changing the selection backs out of the confirm step: the confirm copy must always
    // describe exactly what will be registered.
    setConfirmingBatch(false)
    setSelected(prev => {
      const next = new Set(prev)
      if (next.has(key)) {
        next.delete(key)
      } else {
        next.add(key)
      }
      return next
    })
  }

  async function confirmRegistration() {
    if (batchInFlight.current) return
    const batch = candidates.filter(c =>
      selected.has(c.key) && !registered[c.key] && (c.kind === 'generated' || c.name.trim() !== ''))
    if (batch.length === 0) return
    batchInFlight.current = true
    setBatchBusy(true)
    setNotice('')
    try {
      // Sequential, one request per candidate. A failure marks its candidate and the batch
      // continues; the failed candidate stays selected for retry.
      for (const candidate of batch) {
        try {
          const id = await registerFeature(specFor(candidate))
          let freshness: FeatureFreshness | null = null
          try {
            freshness = await featureFreshness(id)
          } catch {
            // Freshness is advisory on this note: omit the chip rather than fail the
            // registration UI.
          }
          setRegistered(prev => ({ ...prev, [candidate.key]: { id, freshness } }))
          setSelected(prev => {
            const next = new Set(prev)
            next.delete(candidate.key)
            return next
          })
          setErrors(prev => {
            if (!(candidate.key in prev)) return prev
            const next = { ...prev }
            delete next[candidate.key]
            return next
          })
        } catch (err) {
          setErrors(prev => ({
            ...prev,
            [candidate.key]: err instanceof ApiError ? err.detail : String(err),
          }))
        }
      }
    } finally {
      batchInFlight.current = false
      setBatchBusy(false)
      setConfirmingBatch(false)
    }
  }

  const selectedCount = selectedCandidates.length

  return (
    <section>
      <div className="panel">
        {notice && (
          <div role="alert" className="callout callout--warn">
            <CalloutGlyph d={WARN_GLYPH} />
            <div className="callout-body">
              <p>{notice}</p>
            </div>
          </div>
        )}
        <form onSubmit={generate} style={{ display: 'grid', gap: 16, margin: 0 }}>
          <div className="field" style={{ maxWidth: 640 }}>
            <label htmlFor="wb-goal">Prediction goal</label>
            <input
              id="wb-goal"
              value={goal}
              onChange={e => setGoal(e.target.value)}
              placeholder="e.g. predict customer churn in the next 90 days"
              style={{ height: 40 }}
            />
            <div
              className="hint"
              style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8 }}
            >
              <span>
                Both paths use it: the engine generates against it, and written definitions attach
                to it.
              </span>
              <span>Try</span>
              <button type="button" className="role-chip" onClick={() => setGoal(EXAMPLE_GOAL)}>
                {EXAMPLE_GOAL}
              </button>
            </div>
          </div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 20 }}>
            <div className="field" style={{ flex: '1 1 220px' }}>
              <label htmlFor="wb-source">Catalog source</label>
              <input
                id="wb-source"
                value={source}
                onChange={e => changeSource(e.target.value)}
                placeholder="e.g. deposits"
              />
              <p className="hint" style={HELP_STYLE}>
                Optional. Scopes candidates to one upload source; blank searches every catalog.
              </p>
            </div>
            <div className="field" style={{ flex: '1 1 220px' }}>
              <label htmlFor="wb-entity">Entity</label>
              <input
                id="wb-entity"
                value={entity}
                onChange={e => changeEntity(e.target.value)}
                placeholder="e.g. customer"
              />
              <p className="hint" style={HELP_STYLE}>
                Optional. Gathers from every catalog holding this entity, e.g. Customer.
              </p>
            </div>
            <div className="field" style={{ flex: '1 1 220px' }}>
              <label htmlFor="wb-target">Target column</label>
              <input
                id="wb-target"
                value={target}
                onChange={e => changeTarget(e.target.value)}
                placeholder="e.g. public.labels.churned"
              />
              <p className="hint" style={HELP_STYLE}>
                What you are predicting. Candidates are screened against it server-side, so leaky
                features never reach you.
              </p>
            </div>
          </div>
          <div className="paths">
            <button
              type="submit"
              className="path path-generate"
              disabled={!goal.trim() || generating}
            >
              <span className="k">Path 1 · The engine</span>
              <span className="t">
                <PathGlyph>
                  <circle cx="8" cy="8" r="6.2" />
                  <path d="M8 5v6M5 8h6" />
                </PathGlyph>
                {generating ? 'Generating' : 'Generate candidates'}
              </span>
              <span className="d">
                The engine proposes catalog-grounded, design-checked features for your goal, each
                with its causal rationale.
              </span>
            </button>
            <button
              type="button"
              className="path path-describe"
              aria-pressed={describeOpen}
              aria-controls="wb-describe-panel"
              onClick={() => setDescribeOpen(open => !open)}
            >
              <span className="k">Path 2 · Your definitions</span>
              <span className="t">
                <PathGlyph>
                  <path d="M3 13h10M4 10.5 10.8 3.7a1.4 1.4 0 0 1 2 2L6 12.5l-2.8.8z" />
                </PathGlyph>
                Write definitions myself
              </span>
              <span className="d">
                One definition per line; each becomes a draft candidate with its real join path,
                drafted together.
              </span>
            </button>
          </div>
        </form>
      </div>

      {scopeChanged && (
        <p role="status" className="hint">
          Scope changed. Regenerate to refresh candidates.
        </p>
      )}

      {describeOpen && (
        <div className="panel" id="wb-describe-panel">
          <h2>Describe features</h2>
          <form onSubmit={draftCandidates} style={{ display: 'grid', gap: 12, margin: 0 }}>
            <div className="field" style={{ maxWidth: 640 }}>
              <label htmlFor="wb-describe">Describe the feature you want</label>
              <textarea
                id="wb-describe"
                rows={4}
                value={describeText}
                onChange={e => setDescribeText(e.target.value)}
                placeholder={DESCRIBE_PLACEHOLDER}
              />
              <p className="hint" style={HELP_STYLE}>
                Write one definition per line. Each line becomes a draft candidate you can name,
                adjust, and register together.
              </p>
            </div>
            {draftErrors.length > 0 && (
              <div style={{ display: 'grid', gap: 4 }}>
                {draftErrors.map(message => (
                  <p key={message} className="error" role="alert">
                    {message}
                  </p>
                ))}
              </div>
            )}
            <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 12 }}>
              <button
                type="submit"
                className="btn"
                disabled={drafting || draftLines.length === 0 || !source.trim()}
              >
                {drafting
                  ? 'Drafting…'
                  : draftLines.length > 1
                    ? `Draft ${draftLines.length} candidates`
                    : 'Draft candidate'}
              </button>
              {!source.trim() && (
                <p className="hint">
                  Recipes read one catalog source. Set Catalog source above to draft.
                </p>
              )}
            </div>
          </form>
        </div>
      )}

      {generated?.length === 0 && (
        <div className="empty" role="status">
          <p>No grounded candidates for that goal.</p>
          <p className="next">Rephrase the goal, or change the catalog source and generate again.</p>
        </div>
      )}

      {candidates.length > 0 && (
        <>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, marginTop: 32 }}>
            <h2>Proposed features</h2>
            <span className="micro-label tabular-nums">
              <span style={{ color: 'var(--accent)' }}>{candidates.length}</span>{' '}
              {candidates.length === 1 ? 'candidate' : 'candidates'}
            </span>
          </div>
          {hasGenerated && (
            <p className="hint" style={{ marginTop: 4 }}>
              Design-checked: structurally safe against leakage, staleness, and double-counting.
              Predictive value is proven later by backtests.
            </p>
          )}
          {screenedTarget && (
            <p className="hint" style={{ marginTop: 4 }}>
              Screened against <span className="mono">{screenedTarget}</span>: leaky candidates
              were rejected before reaching you.
            </p>
          )}
          <ul className="rows">
            {candidates.map(c => {
              const reg = registered[c.key]
              const error = errors[c.key]
              const rawName = c.kind === 'generated' ? c.idea.name : c.name
              const displayName = rawName.trim() || 'unnamed draft'
              const canSelect = c.kind === 'generated' || c.name.trim() !== ''
              const description = c.kind === 'generated' ? c.idea.description : c.description
              const aggregation = c.kind === 'generated' ? c.idea.aggregation : c.recipe.aggregation
              const grain = c.kind === 'generated' ? c.idea.grain_table : c.recipe.grain_table
              const derives = c.kind === 'generated'
                ? c.idea.derives_pairs.map(([s, ref]) => `${s}:${ref}`).join(', ') || 'none'
                : c.recipe.derives_from.map(ref => `${c.snapshotSource}:${ref}`).join(', ') || 'none'
              return (
                <li className="row" key={c.key} style={{ alignItems: 'flex-start' }}>
                  {reg ? (
                    <CheckGlyph />
                  ) : (
                    <input
                      type="checkbox"
                      aria-label={`Select ${displayName}`}
                      checked={selected.has(c.key)}
                      disabled={batchBusy || !canSelect}
                      onChange={() => toggleSelect(c.key)}
                      style={{ width: 18, height: 18, margin: 10, flex: 'none' }}
                    />
                  )}
                  <div style={{ display: 'grid', gap: 8, flex: 1, minWidth: 0, padding: '6px 0' }}>
                    <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
                      <span className={c.kind === 'draft' ? 'mono' : undefined} style={{ fontWeight: 600 }}>
                        {displayName}
                      </span>
                      <span className="badge proposal">
                        {c.kind === 'generated' ? 'Proposal' : 'Draft'}
                      </span>
                      {/* Honest stamp: soft (not solid) so it never outshouts the selection or
                          registered states. Drafts skip the gauntlet, so they carry no stamp. */}
                      {c.kind === 'generated' && c.idea.verification && (
                        <span className="badge ok">{c.idea.verification.toLowerCase()}</span>
                      )}
                    </div>
                    <p style={{ color: 'var(--ink-soft)' }}>{description}</p>
                    {c.kind === 'generated' && c.idea.rationale && (
                      <p style={{ color: 'var(--ink-soft)' }}>Why: {c.idea.rationale}</p>
                    )}
                    <dl className="kv">
                      <div>
                        <dt>derives from</dt>
                        <dd className="mono">{derives}</dd>
                      </div>
                      {aggregation && (
                        <div>
                          <dt>aggregation</dt>
                          <dd>{aggregation}</dd>
                        </div>
                      )}
                      {grain && (
                        <div>
                          <dt>grain</dt>
                          <dd>{grain}</dd>
                        </div>
                      )}
                      {c.kind === 'draft' && (
                        <div>
                          <dt>drafted against</dt>
                          <dd className="mono">{c.snapshotSource}</dd>
                        </div>
                      )}
                    </dl>
                    {c.kind === 'draft' && !reg && (
                      <div className="field" style={{ maxWidth: 380 }}>
                        <label htmlFor={`wb-name-${c.key}`}>Name</label>
                        <input
                          id={`wb-name-${c.key}`}
                          className="mono"
                          value={c.name}
                          onChange={e => renameDraft(c.key, e.target.value)}
                          placeholder="feature_name"
                        />
                        {!c.name.trim() && (
                          <p className="hint">Name this draft to select it for registration.</p>
                        )}
                      </div>
                    )}
                    {c.kind === 'draft' && <JoinPathDetails steps={c.recipe.join_path} />}
                    {error && (
                      <p className="error" role="alert">
                        {error}
                      </p>
                    )}
                    {reg && (
                      <p
                        style={{
                          color: 'var(--ok)', fontWeight: 500,
                          display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8,
                        }}
                      >
                        <span>
                          Registered <span className="mono">{reg.id}</span>
                        </span>
                        {reg.freshness && (reg.freshness.fresh ? (
                          <span className="badge" style={OK_SOLID_CHIP_STYLE}>fresh</span>
                        ) : (
                          <span className="badge stale">
                            stale: {reg.freshness.stale_sources.join(', ')}
                          </span>
                        ))}
                      </p>
                    )}
                  </div>
                </li>
              )
            })}
            {selectedCount > 0 && (
              <li className="row" style={TRAY_STYLE}>
                {confirmingBatch ? (
                  <>
                    <p style={{ flex: '1 1 260px', fontWeight: 500 }}>
                      {selectedCount === 1
                        ? 'This feature will enter the catalog registry with its lineage.'
                        : `These ${selectedCount} features will enter the catalog registry with their lineage.`}
                    </p>
                    <button
                      type="button"
                      className="btn btn--proposal-confirm"
                      disabled={batchBusy}
                      onClick={() => void confirmRegistration()}
                    >
                      Confirm registration
                    </button>
                    <button
                      type="button"
                      className="btn"
                      disabled={batchBusy}
                      onClick={() => setConfirmingBatch(false)}
                    >
                      Cancel
                    </button>
                  </>
                ) : (
                  <>
                    <span className="tabular-nums" style={{ flex: '1 1 auto', fontWeight: 600 }}>
                      {selectedCount} selected
                    </span>
                    <button
                      type="button"
                      className="btn btn--primary"
                      onClick={() => setConfirmingBatch(true)}
                    >
                      Register {selectedCount} {selectedCount === 1 ? 'feature' : 'features'}
                    </button>
                  </>
                )}
              </li>
            )}
          </ul>
        </>
      )}
    </section>
  )
}
