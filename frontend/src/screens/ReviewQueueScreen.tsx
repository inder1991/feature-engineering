import { type FormEvent, useEffect, useState } from 'react'
import { ApiError, type QuarantineItem, listQuarantine } from '../api'

export function ReviewQueueScreen({ initialSource }: { initialSource: string }) {
  const [source, setSource] = useState(initialSource)
  const [items, setItems] = useState<QuarantineItem[] | null>(null)
  const [error, setError] = useState('')

  async function load(name: string) {
    if (!name.trim()) return
    setError('')
    try {
      setItems(await listQuarantine(name.trim()))
    } catch (err) {
      setItems(null)
      setError(err instanceof ApiError ? err.detail : String(err))
    }
  }

  useEffect(() => {
    // Arriving via the upload screen's "review quarantined rows" handoff: load immediately.
    if (initialSource.trim()) void load(initialSource)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [initialSource])

  function submit(e: FormEvent) {
    e.preventDefault()
    void load(source)
  }

  return (
    <section>
      <h2>Review queue</h2>
      <form onSubmit={submit}>
        <input
          aria-label="source"
          value={source}
          onChange={e => setSource(e.target.value)}
          placeholder="source name"
        />
        <button type="submit">Load queue</button>
      </form>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {items?.length === 0 && (
        <p className="empty">Queue clear — no quarantined rows for this source.</p>
      )}
      {items && items.length > 0 && (
        <>
          <p>
            {items.length} quarantined row{items.length === 1 ? '' : 's'}. Fix them in the source
            file and re-upload — a clean upload clears this queue.
          </p>
          {items.map(item => (
            <article className="card quarantine" key={item.row_index}>
              <header>
                <span className="badge rejected">row {item.row_index}</span>
                <strong>{item.reason}</strong>
              </header>
              <dl>
                {Object.entries(item.raw)
                  .filter(([, v]) => v !== '' && v !== null && v !== false)
                  .map(([k, v]) => (
                    <div key={k}>
                      <dt>{k}</dt>
                      <dd>{String(v)}</dd>
                    </div>
                  ))}
              </dl>
            </article>
          ))}
        </>
      )}
    </section>
  )
}
