import { type FormEvent, useState } from 'react'
import { ApiError, type IngestResult, uploadFile } from '../api'

export function UploadScreen({ onReviewQueue }: { onReviewQueue: (source: string) => void }) {
  const [source, setSource] = useState('')
  const [file, setFile] = useState<File | null>(null)
  const [result, setResult] = useState<IngestResult | null>(null)
  const [error, setError] = useState('')
  const [busy, setBusy] = useState(false)

  async function submit(e: FormEvent) {
    e.preventDefault()
    if (!file || !source.trim()) return
    setBusy(true)
    setError('')
    setResult(null)
    try {
      setResult(await uploadFile(file, source.trim()))
    } catch (err) {
      setError(err instanceof ApiError ? err.detail : String(err))
    } finally {
      setBusy(false)
    }
  }

  return (
    <section>
      <h2>Upload a schema + facts file</h2>
      <form onSubmit={submit}>
        <label>
          Source name
          <input
            value={source}
            onChange={e => setSource(e.target.value)}
            placeholder="e.g. deposits"
            required
          />
        </label>
        <label>
          File (.csv / .xlsx)
          {/* No `required` here: jsdom never counts an uploaded FileList toward a required
              file input's validity (valueMissing stays true), which blocks programmatic form
              submission in tests. Empty submission is already prevented by the disabled button
              and the `if (!file …) return` guard below, so this is redundant defense anyway. */}
          <input
            type="file"
            accept=".csv,.xlsx"
            onChange={e => setFile(e.target.files?.[0] ?? null)}
          />
        </label>
        <button type="submit" disabled={busy || !file || !source.trim()}>
          {busy ? 'Uploading…' : 'Upload'}
        </button>
      </form>
      {error && (
        <p role="alert" className="error">
          {error}
        </p>
      )}
      {result && (
        <IngestResultPanel result={result} source={source.trim()} onReviewQueue={onReviewQueue} />
      )}
    </section>
  )
}

function IngestResultPanel({
  result,
  source,
  onReviewQueue,
}: {
  result: IngestResult
  source: string
  onReviewQueue: (source: string) => void
}) {
  if (result.status === 'held') {
    return (
      <section className="result held" role="status">
        <strong>Held — confirm this large change.</strong>
        <p>{result.reason}</p>
        <p>Nothing was applied. Check the file targets the right source, then re-upload to confirm.</p>
      </section>
    )
  }
  if (result.status === 'rejected') {
    return (
      <section className="result rejected" role="status">
        <strong>Rejected.</strong>
        <p>{result.reason}</p>
      </section>
    )
  }
  return (
    <section className="result ingested" role="status">
      <strong>Ingested.</strong>
      <p>
        {result.asserted} facts asserted · {result.staled} staled · {result.quarantined} quarantined
      </p>
      {result.flagged && <p className="flagged">⚠ {result.flagged}</p>}
      {result.quarantined > 0 && (
        <button onClick={() => onReviewQueue(source)}>
          Review {result.quarantined} quarantined row{result.quarantined === 1 ? '' : 's'}
        </button>
      )}
    </section>
  )
}
