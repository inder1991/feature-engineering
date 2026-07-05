import { type FormEvent, useState } from 'react'
import { ApiError, type SearchHit, searchCatalog } from '../api'

export function SearchScreen() {
  const [q, setQ] = useState('')
  const [hits, setHits] = useState<SearchHit[] | null>(null)
  const [error, setError] = useState('')

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!q.trim()) return
    setError('')
    try {
      setHits(await searchCatalog(q.trim()))
    } catch (err) {
      setHits(null)
      setError(err instanceof ApiError ? err.detail : String(err))
    }
  }

  return (
    <section>
      <h2>Search the catalog</h2>
      <form onSubmit={submit} role="search">
        <input
          aria-label="query"
          value={q}
          onChange={e => setQ(e.target.value)}
          placeholder="balance, customer, churn…"
        />
        <button type="submit">Search</button>
      </form>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {hits?.length === 0 && (
        <p className="empty">
          No fresh results. Columns your roles can’t see are hidden, and a stale source is not
          served until it is re-uploaded (re-vouched) — never silently wrong.
        </p>
      )}
      {hits?.map(hit => <HitCard key={hit.object_ref} hit={hit} />)}
    </section>
  )
}

function HitCard({ hit }: { hit: SearchHit }) {
  const aggregation = hit.additivity
    ? `${hit.additivity}${hit.unit ? ` · ${hit.unit}` : ''}${hit.currency ? ` (${hit.currency})` : ''}`
    : null
  return (
    <article className="card hit">
      <header>
        <code>{hit.object_ref}</code>
        {hit.is_grain && <span className="badge grain">grain</span>}
        {hit.is_as_of && <span className="badge asof">as-of</span>}
        {hit.sensitivity && <span className="badge sensitivity">{hit.sensitivity}</span>}
      </header>
      {hit.definition && <p>{hit.definition}</p>}
      <dl>
        <div><dt>type</dt><dd>{hit.data_type ?? hit.kind}</dd></div>
        <div><dt>source</dt><dd>{hit.catalog_source}</dd></div>
        {hit.concept && <div><dt>concept</dt><dd>{hit.concept}</dd></div>}
        {hit.domain && <div><dt>domain</dt><dd>{hit.domain}</dd></div>}
        {hit.entity && <div><dt>entity</dt><dd>{hit.entity}</dd></div>}
        {aggregation && <div><dt>aggregation</dt><dd>{aggregation}</dd></div>}
      </dl>
    </article>
  )
}
