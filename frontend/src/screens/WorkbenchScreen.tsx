import { type FormEvent, useState } from 'react'
import {
  ApiError, type FeatureIdea, type LeakageWarning, type Recipe,
  featureRecipe, leakageCheck, recommendFeatures, registerFeature,
} from '../api'

const HELP_STYLE = { fontSize: 12 } as const
const CLUSTER_STYLE = { display: 'flex', flexWrap: 'wrap', gap: 8 } as const

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

export function WorkbenchScreen() {
  const [source, setSource] = useState('')
  const [objective, setObjective] = useState('')
  const [proposals, setProposals] = useState<FeatureIdea[] | null>(null)
  const [nlQuery, setNlQuery] = useState('')
  const [recipe, setRecipe] = useState<Recipe | null>(null)
  const [target, setTarget] = useState('')
  const [warnings, setWarnings] = useState<LeakageWarning[] | null>(null)
  const [confirming, setConfirming] = useState<string | null>(null)
  const [registered, setRegistered] = useState<Record<string, string>>({})
  const [notice, setNotice] = useState('')

  function fail(err: unknown) {
    setNotice(
      err instanceof ApiError && err.status === 503
        ? 'AI assist is not configured on this deployment: no LLM provider is enabled.'
        : err instanceof ApiError
          ? err.detail
          : String(err),
    )
  }

  async function suggest(e: FormEvent) {
    e.preventDefault()
    setNotice('')
    try {
      setProposals(await recommendFeatures(
        objective.trim(), source.trim() || null, target.trim() || null))
    } catch (err) {
      setProposals(null)
      fail(err)
    }
  }

  async function buildRecipe(e: FormEvent) {
    e.preventDefault()
    setNotice('')
    try {
      setRecipe(await featureRecipe(nlQuery.trim(), source.trim()))
    } catch (err) {
      setRecipe(null)
      fail(err)
    }
  }

  async function checkLeakage(p: FeatureIdea) {
    setNotice('')
    try {
      setWarnings(await leakageCheck(p.derives_from, target.trim()))
    } catch (err) {
      setWarnings(null)
      fail(err)
    }
  }

  async function confirmRegister(p: FeatureIdea) {
    setNotice('')
    try {
      const id = await registerFeature({
        name: p.name, description: p.description, grain_table: p.grain_table,
        aggregation: p.aggregation, as_of_column: null,
        derives_from: p.derives_from.map(ref => ({
          catalog_source: source.trim(), object_ref: ref })),
      })
      setRegistered(prev => ({ ...prev, [p.name]: id }))
      setConfirming(null)
    } catch (err) {
      fail(err)
    }
  }

  const fansOut = recipe?.join_path.some(s => s.cardinality === '1:N') ?? false

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
              onChange={e => setSource(e.target.value)}
              placeholder="e.g. deposits"
            />
            <p className="hint" style={HELP_STYLE}>
              Scopes suggestions to one source and stamps lineage on anything you register.
            </p>
          </div>
          <div className="field" style={{ flex: '1 1 260px' }}>
            <label htmlFor="wb-target">Target column</label>
            <input
              id="wb-target"
              aria-label="target column"
              value={target}
              onChange={e => setTarget(e.target.value)}
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
          {proposals.map(p => (
            <li className="row" key={p.name} style={{ alignItems: 'flex-start' }}>
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
                {registered[p.name] ? (
                  <p style={{ color: 'var(--ok)', fontWeight: 500 }}>
                    Registered as <span className="mono">{registered[p.name]}</span>
                  </p>
                ) : confirming === p.name ? (
                  <div style={CLUSTER_STYLE}>
                    <button
                      type="button"
                      className="btn btn--proposal-confirm"
                      onClick={() => confirmRegister(p)}
                    >
                      Confirm register
                    </button>
                    <button type="button" className="btn" onClick={() => setConfirming(null)}>
                      Cancel
                    </button>
                  </div>
                ) : (
                  <div style={CLUSTER_STYLE}>
                    <button
                      type="button"
                      className="btn"
                      disabled={!source.trim()}
                      onClick={() => setConfirming(p.name)}
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
          ))}
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
                        s.cardinality === '1:N'
                          ? { color: 'var(--warn)', fontWeight: 600 }
                          : { color: 'var(--ink-soft)' }
                      }
                    >
                      ({s.cardinality ?? 'unknown'})
                    </span>
                  </li>
                ))}
              </ol>
              {fansOut && (
                <div className="callout callout--warn">
                  <CalloutGlyph d={WARN_GLYPH} />
                  <div className="callout-body">
                    <p>
                      <strong>Fan-out.</strong> A 1:N hop multiplies rows. Aggregate before joining
                      or the feature will double-count.
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
