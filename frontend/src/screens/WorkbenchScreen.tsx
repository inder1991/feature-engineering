import { type FormEvent, useRef, useState } from 'react'
import {
  ApiError, type FeatureFreshness, type FeatureIdea, type LeakageWarning, type Recipe,
  featureFreshness, featureRecipe, leakageCheck, recommendFeatures, registerFeature,
} from '../api'

const HELP_STYLE = { fontSize: 12 } as const
const CLUSTER_STYLE = { display: 'flex', flexWrap: 'wrap', gap: 8 } as const
// Solid ok chip (index.css has no fresh badge class; mirrors .badge.stale's solid treatment).
const OK_SOLID_CHIP_STYLE = {
  background: 'var(--ok-solid)', borderColor: 'transparent', color: 'var(--chip-ink)',
} as const

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

const WARN_GLYPH = 'M8 2.5 1.5 13.25h13L8 2.5ZM8 6.75v3M8 12v.01'
const DANGER_GLYPH = 'M8 1.75a6.25 6.25 0 1 0 0 12.5 6.25 6.25 0 0 0 0-12.5ZM8 5v3.5M8 11v.01'

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

// Proposals carry a per-suggest key (suggest sequence + index + name): LLM-chosen names are not
// unique across rounds, so keying registered/confirming state by name alone shows phantom
// "Registered as" on a fresh, never-registered proposal that reuses an old name.
interface Proposal {
  key: string
  idea: FeatureIdea
}

interface Registration {
  id: string
  freshness: FeatureFreshness | null
}

export function WorkbenchScreen() {
  const [source, setSource] = useState('')
  const [entity, setEntity] = useState('')
  const [objective, setObjective] = useState('')
  const [proposals, setProposals] = useState<Proposal[] | null>(null)
  const [nlQuery, setNlQuery] = useState('')
  const [recipe, setRecipe] = useState<Recipe | null>(null)
  const [target, setTarget] = useState('')
  const [warnings, setWarnings] = useState<LeakageWarning[] | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [registering, setRegistering] = useState<string | null>(null)
  const [registered, setRegistered] = useState<Record<string, Registration>>({})
  const [notice, setNotice] = useState('')
  // Out-of-order guards: only the latest request per handler may apply its response.
  const suggestSeq = useRef(0)
  const recipeSeq = useRef(0)
  const leakageSeq = useRef(0)
  // Reentry guard for the register mutation: state updates are async, so a double click could
  // otherwise fire two POSTs before the disabled attribute lands.
  const registerInFlight = useRef(false)

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
    // Proposals, registration state, leakage results, and the recipe were produced for the
    // previous source context. Keeping them would let a review of one context silently bind
    // to another.
    setProposals(null)
    setConfirming(null)
    setRegistered({})
    setWarnings(null)
    setRecipe(null)
  }

  function changeTarget(value: string) {
    setTarget(value)
    // Leakage results were computed against the previous target.
    setWarnings(null)
  }

  async function suggest(e: FormEvent) {
    e.preventDefault()
    const seq = ++suggestSeq.current
    setNotice('')
    // A new suggestion round voids everything scoped to the previous proposals.
    setWarnings(null)
    setConfirming(null)
    setRegistered({})
    try {
      const ideas = await recommendFeatures(
        objective.trim(), source.trim() || null, target.trim() || null, entity.trim() || null)
      if (seq !== suggestSeq.current) return
      setProposals(ideas.map((idea, i) => ({ key: `${seq}:${i}:${idea.name}`, idea })))
    } catch (err) {
      if (seq !== suggestSeq.current) return
      setProposals(null)
      fail(err)
    }
  }

  async function buildRecipe(e: FormEvent) {
    e.preventDefault()
    const seq = ++recipeSeq.current
    setNotice('')
    try {
      const next = await featureRecipe(nlQuery.trim(), source.trim())
      if (seq !== recipeSeq.current) return
      setRecipe(next)
    } catch (err) {
      if (seq !== recipeSeq.current) return
      setRecipe(null)
      fail(err)
    }
  }

  async function checkLeakage(p: FeatureIdea) {
    const seq = ++leakageSeq.current
    setNotice('')
    try {
      const next = await leakageCheck(p.derives_from, target.trim())
      if (seq !== leakageSeq.current) return
      setWarnings(next)
    } catch (err) {
      if (seq !== leakageSeq.current) return
      setWarnings(null)
      fail(err)
    }
  }

  async function confirmRegister(key: string, idea: FeatureIdea) {
    if (registerInFlight.current) return
    registerInFlight.current = true
    setRegistering(key)
    setNotice('')
    try {
      const id = await registerFeature({
        name: idea.name, description: idea.description, grain_table: idea.grain_table,
        aggregation: idea.aggregation, as_of_column: null,
        // Lineage comes from the pairs the backend resolved at recommend time, never from the
        // typed source context: the typed source can differ from where a derive actually lives.
        derives_from: idea.derives_pairs.map(([catalog_source, object_ref]) => ({
          catalog_source, object_ref })),
      })
      let freshness: FeatureFreshness | null = null
      try {
        freshness = await featureFreshness(id)
      } catch {
        // Freshness is advisory on this note: omit the chip rather than fail the registration UI.
      }
      setRegistered(prev => ({ ...prev, [key]: { id, freshness } }))
      setConfirming(null)
    } catch (err) {
      fail(err)
    } finally {
      registerInFlight.current = false
      setRegistering(null)
    }
  }

  const joinSteps = recipe?.join_path ?? []
  const fansOut = joinSteps.some(s => stepFansOut(s.cardinality))
  const hasUnknownHop = joinSteps.some(s => normalizeCardinality(s.cardinality) === null)

  return (
    <section>
      <h2 className="visually-hidden">Feature workbench</h2>

      {notice && (
        <div role="alert" className="callout callout--warn">
          <CalloutGlyph d={WARN_GLYPH} />
          <div className="callout-body">
            <p>{notice}</p>
          </div>
        </div>
      )}

      <div className="panel">
        <h2>Context</h2>
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 20 }}>
          <div className="field" style={{ flex: '1 1 260px' }}>
            <label htmlFor="wb-source">Catalog source</label>
            <input
              id="wb-source"
              aria-label="catalog source"
              value={source}
              onChange={e => changeSource(e.target.value)}
              placeholder="e.g. deposits"
            />
            <p className="hint" style={HELP_STYLE}>
              Scopes suggestions and recipes to one source. Leave it blank to gather from every
              catalog.
            </p>
          </div>
          <div className="field" style={{ flex: '1 1 260px' }}>
            <label htmlFor="wb-entity">Entity</label>
            <input
              id="wb-entity"
              aria-label="entity"
              value={entity}
              onChange={e => setEntity(e.target.value)}
              placeholder="e.g. customer"
            />
            <p className="hint" style={HELP_STYLE}>
              Optional. Scopes suggestions across every catalog holding that entity.
            </p>
          </div>
          <div className="field" style={{ flex: '1 1 260px' }}>
            <label htmlFor="wb-target">Target column</label>
            <input
              id="wb-target"
              aria-label="target column"
              value={target}
              onChange={e => changeTarget(e.target.value)}
              placeholder="e.g. public.labels.churned"
            />
            <p className="hint" style={HELP_STYLE}>
              Enables leakage checks. New suggestions are also pre-screened against it server-side.
            </p>
          </div>
        </div>
      </div>

      <h2>Suggest features</h2>
      <p className="hint" style={HELP_STYLE}>
        Everything below is a suggestion until you explicitly register it.
      </p>
      <form onSubmit={suggest}>
        <div className="field" style={{ flex: '1 1 320px', maxWidth: 480 }}>
          <label htmlFor="wb-objective">Objective</label>
          <input
            id="wb-objective"
            aria-label="objective"
            value={objective}
            onChange={e => setObjective(e.target.value)}
            placeholder="e.g. predict churn"
          />
        </div>
        <button type="submit" className="btn btn--primary" disabled={!objective.trim()}>
          Suggest features
        </button>
      </form>

      {proposals?.length === 0 && (
        <div className="empty" role="status">
          <p>No grounded proposals for that objective.</p>
          <p className="next">Try rephrasing the objective, or set a different catalog source.</p>
        </div>
      )}

      {proposals && proposals.length > 0 && (
        <ul className="rows">
          {proposals.map(({ key, idea: p }) => {
            const reg = registered[key]
            return (
              <li className="row" key={key} style={{ alignItems: 'flex-start' }}>
                <div style={{ display: 'grid', gap: 8, flex: 1, minWidth: 0, padding: '6px 0' }}>
                  <div style={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 10 }}>
                    <span className="badge proposal">Proposal</span>
                    <span style={{ fontWeight: 600 }}>{p.name}</span>
                  </div>
                  <p style={{ color: 'var(--ink-soft)' }}>{p.description}</p>
                  <dl className="kv">
                    <div>
                      <dt>derives from</dt>
                      <dd className="mono">{p.derives_from.join(', ')}</dd>
                    </div>
                    {p.aggregation && (
                      <div>
                        <dt>aggregation</dt>
                        <dd>{p.aggregation}</dd>
                      </div>
                    )}
                    {p.grain_table && (
                      <div>
                        <dt>grain</dt>
                        <dd>{p.grain_table}</dd>
                      </div>
                    )}
                  </dl>
                  {reg ? (
                    <p
                      style={{
                        color: 'var(--ok)', fontWeight: 500,
                        display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 8,
                      }}
                    >
                      <span>
                        Registered as <span className="mono">{reg.id}</span>
                      </span>
                      {reg.freshness && (reg.freshness.fresh ? (
                        <span className="badge" style={OK_SOLID_CHIP_STYLE}>fresh</span>
                      ) : (
                        <span className="badge stale">
                          stale: {reg.freshness.stale_sources.join(', ')}
                        </span>
                      ))}
                    </p>
                  ) : confirming === key ? (
                    <div style={CLUSTER_STYLE}>
                      <button
                        type="button"
                        className="btn btn--proposal-confirm"
                        disabled={registering === key}
                        onClick={() => confirmRegister(key, p)}
                      >
                        Confirm register
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={registering === key}
                        onClick={() => setConfirming(null)}
                      >
                        Cancel
                      </button>
                    </div>
                  ) : (
                    <div style={CLUSTER_STYLE}>
                      <button
                        type="button"
                        className="btn"
                        onClick={() => setConfirming(key)}
                      >
                        Register…
                      </button>
                      <button
                        type="button"
                        className="btn"
                        disabled={!target.trim()}
                        onClick={() => checkLeakage(p)}
                      >
                        Check leakage
                      </button>
                    </div>
                  )}
                </div>
              </li>
            )
          })}
        </ul>
      )}

      {warnings?.length === 0 && (
        <p role="status" className="hint">
          No leakage warnings.
        </p>
      )}
      {warnings && warnings.length > 0 && (
        <div className="callout callout--danger" role="alert">
          <CalloutGlyph d={DANGER_GLYPH} />
          <div className="callout-body">
            <p>
              <strong>Possible target leakage</strong>
            </p>
            <ul style={{ paddingLeft: 18, display: 'grid', gap: 4 }}>
              {warnings.map(w => (
                <li key={w.object_ref}>
                  <span className="mono">{w.object_ref}</span>: {w.reason}
                </li>
              ))}
            </ul>
            <p>Review these columns before registering the feature.</p>
          </div>
        </div>
      )}

      <h2>Describe a feature</h2>
      <p className="hint" style={HELP_STYLE}>
        Builds a recipe from real catalog columns and join edges. Needs a catalog source.
      </p>
      <form onSubmit={buildRecipe}>
        <div className="field" style={{ flex: '1 1 320px', maxWidth: 480 }}>
          <label htmlFor="wb-nl">Feature description</label>
          <input
            id="wb-nl"
            aria-label="feature description"
            value={nlQuery}
            onChange={e => setNlQuery(e.target.value)}
            placeholder="e.g. total spend per customer over 90 days"
          />
        </div>
        <button type="submit" className="btn" disabled={!nlQuery.trim() || !source.trim()}>
          Build recipe
        </button>
      </form>

      {recipe && (
        <div className="panel">
          <div style={{ display: 'flex', alignItems: 'center', gap: 10, marginBottom: 12 }}>
            <span className="badge proposal">Proposal</span>
            <h3 style={{ fontSize: 15, fontWeight: 600 }}>Recipe</h3>
          </div>
          <dl className="kv">
            {recipe.grain_table && (
              <div>
                <dt>grain</dt>
                <dd>{recipe.grain_table}</dd>
              </div>
            )}
            {recipe.aggregation && (
              <div>
                <dt>aggregation</dt>
                <dd>{recipe.aggregation}</dd>
              </div>
            )}
            <div>
              <dt>derives from</dt>
              <dd className="mono">{recipe.derives_from.join(', ') || 'none'}</dd>
            </div>
            {recipe.as_of_column && (
              <div>
                <dt>as-of</dt>
                <dd className="mono">{recipe.as_of_column}</dd>
              </div>
            )}
          </dl>
          {recipe.join_path.length > 0 && (
            <>
              <p className="micro-label" style={{ marginTop: 16 }}>
                Join path (real edges)
              </p>
              <ol className="mono" style={{ marginTop: 8, paddingLeft: 22, display: 'grid', gap: 4 }}>
                {recipe.join_path.map(s => (
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
                  Cardinality is missing on at least one hop, so fan-out cannot be ruled out.
                  Confirm the join direction before building on this recipe.
                </p>
              )}
              {fansOut && (
                <div className="callout callout--warn">
                  <CalloutGlyph d={WARN_GLYPH} />
                  <div className="callout-body">
                    <p>
                      <strong>Fan-out.</strong> A one-to-many hop multiplies rows. Aggregate
                      before joining or the feature will double-count.
                    </p>
                  </div>
                </div>
              )}
            </>
          )}
        </div>
      )}
    </section>
  )
}
