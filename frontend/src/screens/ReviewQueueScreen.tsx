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
      <form onSubmit={submit}>
        <div className="field">
          <label htmlFor="review-source">Source</label>
          <input
            id="review-source"
            value={source}
            onChange={e => setSource(e.target.value)}
            placeholder="source name"
          />
        </div>
        <button type="submit" className="btn">
          Load queue
        </button>
      </form>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {items?.length === 0 && (
        <p className="empty">Queue clear. No quarantined rows for this source.</p>
      )}
      {items && items.length > 0 && (
        <>
          <p>
            {items.length} quarantined row{items.length === 1 ? '' : 's'}. Fix them in the source
            file and re-upload; a clean upload clears this queue.
          </p>
          <ul className="rows">
            {items.map(item => (
              <li
                className="row"
                key={item.row_index}
                style={{ flexDirection: 'column', alignItems: 'stretch' }}
              >
                <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span className="badge rejected">row {item.row_index}</span>
                  <strong style={{ fontWeight: 600 }}>{item.reason}</strong>
                </div>
                <dl className="kv">
                  {Object.entries(item.raw)
                    .filter(([, v]) => v !== '' && v !== null && v !== false)
                    .map(([k, v]) => (
                      <div key={k}>
                        <dt className="mono">{k}</dt>
                        <dd>{String(v)}</dd>
                      </div>
                    ))}
                </dl>
              </li>
            ))}
          </ul>
        </>
      )}
    </section>
  )
}
