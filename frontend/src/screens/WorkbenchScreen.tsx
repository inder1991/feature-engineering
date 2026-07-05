import { type FormEvent, useState } from 'react'
import {
  ApiError, type FeatureIdea, type LeakageWarning, type Recipe,
  featureRecipe, leakageCheck, recommendFeatures, registerFeature,
} from '../api'

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
        ? 'AI assist is not configured on this deployment — no LLM provider is enabled.'
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
      <h2>Feature workbench</h2>
      <p className="hint">Everything below is a suggestion until you explicitly register it.</p>
      <label>
        Catalog source
        <input
          aria-label="catalog source"
          value={source}
          onChange={e => setSource(e.target.value)}
          placeholder="e.g. deposits"
        />
      </label>
      {notice && (
        <p role="alert" className="notice">
          {notice}
        </p>
      )}

      <form onSubmit={suggest}>
        <h3>Recommend features for an objective</h3>
        <input
          aria-label="objective"
          value={objective}
          onChange={e => setObjective(e.target.value)}
          placeholder="e.g. predict churn"
        />
        <button type="submit" disabled={!objective.trim()}>Suggest features</button>
      </form>
      {proposals?.length === 0 && (
        <p className="empty">No grounded proposals for that objective.</p>
      )}
      {proposals?.map(p => (
        <article className="card proposal" key={p.name}>
          <header>
            <span className="badge proposal">proposal</span>
            <strong>{p.name}</strong>
          </header>
          <p>{p.description}</p>
          <dl>
            <div><dt>derives from</dt><dd>{p.derives_from.join(', ')}</dd></div>
            {p.aggregation && <div><dt>aggregation</dt><dd>{p.aggregation}</dd></div>}
            {p.grain_table && <div><dt>grain</dt><dd>{p.grain_table}</dd></div>}
          </dl>
          {registered[p.name] ? (
            <p className="registered">
              Registered as <code>{registered[p.name]}</code>
            </p>
          ) : confirming === p.name ? (
            <span>
              <button onClick={() => confirmRegister(p)}>Confirm register</button>
              <button onClick={() => setConfirming(null)}>Cancel</button>
            </span>
          ) : (
            <span>
              <button disabled={!source.trim()} onClick={() => setConfirming(p.name)}>
                Register…
              </button>
              <button disabled={!target.trim()} onClick={() => checkLeakage(p)}>
                Check leakage
              </button>
            </span>
          )}
        </article>
      ))}

      <form onSubmit={buildRecipe}>
        <h3>Describe a feature in plain language</h3>
        <input
          aria-label="feature description"
          value={nlQuery}
          onChange={e => setNlQuery(e.target.value)}
          placeholder="e.g. total spend per customer over 90 days"
        />
        <button type="submit" disabled={!nlQuery.trim() || !source.trim()}>Build recipe</button>
      </form>
      {recipe && (
        <article className="card recipe">
          <header>
            <span className="badge proposal">proposal</span>
            <strong>Recipe</strong>
          </header>
          <dl>
            {recipe.grain_table && <div><dt>grain</dt><dd>{recipe.grain_table}</dd></div>}
            {recipe.aggregation && <div><dt>aggregation</dt><dd>{recipe.aggregation}</dd></div>}
            <div><dt>derives from</dt><dd>{recipe.derives_from.join(', ') || '—'}</dd></div>
            {recipe.as_of_column && <div><dt>as-of</dt><dd>{recipe.as_of_column}</dd></div>}
          </dl>
          {recipe.join_path.length > 0 && (
            <>
              <h4>Join path (real edges)</h4>
              <ol>
                {recipe.join_path.map(s => (
                  <li key={`${s.from_ref}->${s.to_ref}`}>
                    <code>{s.from_ref}</code> → <code>{s.to_ref}</code> ({s.cardinality ?? 'unknown'})
                  </li>
                ))}
              </ol>
              {fansOut && (
                <p className="warning">
                  ⚠ a 1:N hop fans out — aggregate before joining or the feature will double-count.
                </p>
              )}
            </>
          )}
        </article>
      )}

      <section aria-label="leakage">
        <h3>Leakage check</h3>
        <label>
          Target column
          <input
            aria-label="target column"
            value={target}
            onChange={e => setTarget(e.target.value)}
            placeholder="e.g. public.labels.churned"
          />
        </label>
        <p className="hint">
          Set a target, then use “Check leakage” on a proposal.
          {target.trim() &&
            ' With a target set, new suggestions are also pre-screened against it server-side.'}
        </p>
        {warnings?.length === 0 && <p className="empty">No leakage warnings.</p>}
        {warnings && warnings.length > 0 && (
          <div className="warning-banner" role="alert">
            <strong>Possible target leakage</strong>
            <ul>
              {warnings.map(w => (
                <li key={w.object_ref}>
                  <code>{w.object_ref}</code> — {w.reason}
                </li>
              ))}
            </ul>
          </div>
        )}
      </section>
    </section>
  )
}
