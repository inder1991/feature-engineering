// The OpenMetadata connector path of the Ingest screen: configure a connection, run a preview
// (a dry run — the server pulls and translates WITHOUT ingesting), review the mappings the human
// is accountable for (tag map, per-table diff, quarantine, pending semantics), then approve.
// Approval posts the previewed snapshot hash back: if OpenMetadata moved since the preview, the
// server answers 409 and this panel asks for a fresh dry run — the human only ever approves the
// exact snapshot they reviewed.
//
// Remap is a CONFIG change, never a client-side edit of the preview payload: quietly rewriting
// previewed rows would let the UI approve something the server never showed, so changing the map
// invalidates the snapshot by design. v1 has no connector-update endpoint, so a remap replaces
// the configured connection (delete + recreate with the amended tag map) and automatically
// re-previews the new row.
//
// Secrets: the bot token never crosses this panel. The form captures only the env-var REFERENCE
// (the create endpoint 422s any plaintext `token` field), and configured connections render the
// token as sealed/not-set — a masked input pretending to carry the token would fake certainty.
import { useEffect, useRef, useState } from 'react'
import type { FormEvent } from 'react'
import {
  ApiError,
  createConnector,
  deleteConnector,
  importConnector,
  listConnectors,
  previewConnector,
} from '../api'
import type {
  Connector,
  ConnectorImportResult,
  ConnectorPreview,
  PreviewTable,
  TableNaming,
  TagMapEntry,
} from '../api'
import { CalloutGlyph, IngestResultCallout } from './IngestResultCallout'

// Where the connector path stands; the Ingest page's gates strip renders this.
export type ConnectorStage = 'configure' | 'preview' | 'review' | 'approve' | 'done'

// Calm error surface per failure mode: OM unreachable (502) and rejected token (401) get plain
// names; everything else renders the backend's own sentence under the action's fallback lead.
function describeError(err: unknown, fallback: string): { lead: string; detail: string } {
  if (err instanceof ApiError) {
    if (err.status === 502) return { lead: 'OpenMetadata is unreachable.', detail: err.detail }
    if (err.status === 401) {
      return { lead: 'OpenMetadata rejected the connector token.', detail: err.detail }
    }
    return { lead: fallback, detail: err.detail }
  }
  return { lead: fallback, detail: String(err) }
}

function scopeText(filters: Record<string, string>): string {
  const parts = ['service', 'database', 'schema']
    .map(key => filters[key])
    .filter((v): v is string => Boolean(v))
  return parts.length > 0 ? parts.join('.') : 'all tables'
}

// Mirrors the server's default: FEATUREGEN_OM_TOKEN__<NAME uppercased, non-alnum -> _>.
function defaultTokenEnv(name: string): string {
  return `FEATUREGEN_OM_TOKEN__${name.replace(/[^A-Za-z0-9]/g, '_').toUpperCase()}`
}

export function ConnectorPanel({
  onReviewQueue,
  onStage,
}: {
  onReviewQueue: (source: string) => void
  onStage: (stage: ConnectorStage) => void
}) {
  const [connectors, setConnectors] = useState<Connector[] | null>(null)
  const [listError, setListError] = useState('')

  // connection form
  const [name, setName] = useState('')
  const [baseUrl, setBaseUrl] = useState('')
  const [targetSource, setTargetSource] = useState('')
  const [service, setService] = useState('')
  const [database, setDatabase] = useState('')
  const [schema, setSchema] = useState('')
  const [tableNaming, setTableNaming] = useState<TableNaming>('table')
  const [tokenEnv, setTokenEnv] = useState('')
  const [saving, setSaving] = useState(false)
  const [formError, setFormError] = useState('')

  // preview + approve. The preview keeps the exact connector row it was taken against, so remap
  // and import always target what the human saw, never a later edit of the list.
  const [preview, setPreview] = useState<{ connector: Connector; data: ConnectorPreview } | null>(
    null,
  )
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState<{ lead: string; detail: string } | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [importBusy, setImportBusy] = useState(false)
  const [importError, setImportError] = useState<{ lead: string; detail: string } | null>(null)
  const [stale, setStale] = useState(false)
  const [imported, setImported] = useState<(ConnectorImportResult & { source: string }) | null>(
    null,
  )

  // Out-of-order guard: every server round-trip takes a ticket; a response only lands if no
  // newer action started meanwhile. The busy flags already serialize the buttons, so this is
  // defense in depth for anything that slips past them (double events, future parallel rows).
  const seq = useRef(0)

  useEffect(() => {
    let cancelled = false
    listConnectors().then(
      cs => {
        if (!cancelled) setConnectors(cs)
      },
      (err: unknown) => {
        if (cancelled) return
        setConnectors([])
        setListError(err instanceof ApiError ? err.detail : String(err))
      },
    )
    return () => {
      cancelled = true
    }
  }, [])

  // The gates strip is derived state, never a second state machine to keep in sync.
  const stage: ConnectorStage = imported
    ? 'done'
    : confirming || importBusy
      ? 'approve'
      : preview
        ? 'review'
        : previewBusy
          ? 'preview'
          : 'configure'
  useEffect(() => {
    onStage(stage)
  }, [stage, onStage])

  const busy = previewBusy || importBusy

  function refreshList() {
    // Best-effort resync after a partial failure; the next user action re-surfaces errors.
    listConnectors().then(
      cs => setConnectors(cs),
      () => {},
    )
  }

  async function save(e: FormEvent) {
    e.preventDefault()
    if (saving) return
    setSaving(true)
    setFormError('')
    const filters: Record<string, string> = {}
    if (service.trim()) filters.service = service.trim()
    if (database.trim()) filters.database = database.trim()
    if (schema.trim()) filters.schema = schema.trim()
    try {
      const created = await createConnector({
        name: name.trim(),
        base_url: baseUrl.trim(),
        target_source: targetSource.trim(),
        tag_map: {},
        filters,
        table_naming: tableNaming,
        ...(tokenEnv.trim() ? { token_env: tokenEnv.trim() } : {}),
      })
      setConnectors(cs => [...(cs ?? []), created])
      setName('')
      setBaseUrl('')
      setTargetSource('')
      setService('')
      setDatabase('')
      setSchema('')
      setTableNaming('table')
      setTokenEnv('')
    } catch (err) {
      setFormError(err instanceof ApiError ? err.detail : String(err))
    } finally {
      setSaving(false)
    }
  }

  async function runPreview(connector: Connector) {
    if (busy) return
    const ticket = ++seq.current
    setPreviewBusy(true)
    setPreviewError(null)
    setImportError(null)
    setStale(false)
    setConfirming(false)
    setImported(null)
    setPreview(null)
    try {
      const data = await previewConnector(connector.connector_id)
      if (seq.current !== ticket) return
      setPreview({ connector, data })
    } catch (err) {
      if (seq.current !== ticket) return
      setPreviewError(describeError(err, 'Preview failed.'))
    } finally {
      if (seq.current === ticket) setPreviewBusy(false)
    }
  }

  async function remap(tag: string, mappedTo: string) {
    if (!preview || busy) return
    const prior = preview.connector
    const ticket = ++seq.current
    setPreviewBusy(true)
    setPreviewError(null)
    setStale(false)
    setConfirming(false)
    setImported(null)
    try {
      await deleteConnector(prior.connector_id)
      const next = await createConnector({
        name: prior.name,
        base_url: prior.base_url,
        target_source: prior.target_source,
        tag_map: { ...prior.tag_map, [tag]: mappedTo },
        filters: prior.filters,
        table_naming: prior.table_naming,
        token_env: prior.token_env,
      })
      if (seq.current === ticket) {
        setConnectors(cs =>
          (cs ?? []).map(c => (c.connector_id === prior.connector_id ? next : c)),
        )
      }
      const data = await previewConnector(next.connector_id)
      if (seq.current !== ticket) return
      setPreview({ connector: next, data })
    } catch (err) {
      if (seq.current !== ticket) return
      // The old preview may describe a connection that no longer exists: drop it rather than
      // leave a stale dry run approvable, and resync the list.
      setPreview(null)
      setPreviewError(describeError(err, 'The remap did not apply.'))
      refreshList()
    } finally {
      if (seq.current === ticket) setPreviewBusy(false)
    }
  }

  async function removeConnector(connector: Connector) {
    if (busy) return
    setListError('')
    try {
      await deleteConnector(connector.connector_id)
      setConnectors(cs => (cs ?? []).filter(c => c.connector_id !== connector.connector_id))
      if (preview?.connector.connector_id === connector.connector_id) {
        setPreview(null)
        setConfirming(false)
        setStale(false)
      }
    } catch (err) {
      setListError(err instanceof ApiError ? err.detail : String(err))
    }
  }

  function approveClick() {
    if (!preview || busy || stale || imported) return
    if (!confirming) {
      setConfirming(true)
      return
    }
    void confirmImport()
  }

  async function confirmImport() {
    if (!preview || importBusy) return
    const ticket = ++seq.current
    setImportBusy(true)
    setImportError(null)
    try {
      const res = await importConnector(preview.connector.connector_id, preview.data.snapshot_hash)
      if (seq.current !== ticket) return
      setImported({ ...res, source: preview.connector.target_source })
      setConfirming(false)
    } catch (err) {
      if (seq.current !== ticket) return
      if (err instanceof ApiError && err.status === 409) {
        setStale(true)
        setConfirming(false)
      } else {
        setImportError(describeError(err, 'Import failed.'))
      }
    } finally {
      if (seq.current === ticket) setImportBusy(false)
    }
  }

  const envPreview = tokenEnv.trim() || (name.trim() ? defaultTokenEnv(name.trim()) : null)

  return (
    <div>
      <section className="panel">
        <h2>Connect OpenMetadata</h2>
        <p className="hint">
          The connector pulls tables and columns through the same validation, brake, and
          quarantine as an upload. The bot token never travels through this page: the server reads
          it from an environment variable, and no response ever carries it.
        </p>
        <form onSubmit={save} className="conn-form">
          <div className="field">
            <label>
              Connection name
              <input value={name} onChange={e => setName(e.target.value)} placeholder="e.g. cards om" required />
            </label>
          </div>
          <div className="field">
            <label>
              OpenMetadata URL
              <input
                value={baseUrl}
                onChange={e => setBaseUrl(e.target.value)}
                placeholder="https://openmetadata.bank.internal"
                required
              />
            </label>
          </div>
          <div className="field">
            <label>
              Target source
              <input
                value={targetSource}
                onChange={e => setTargetSource(e.target.value)}
                placeholder="e.g. cards"
                required
              />
            </label>
          </div>
          <div className="field">
            <label>
              Service filter
              <input className="mono" value={service} onChange={e => setService(e.target.value)} placeholder="mysql_*" />
            </label>
          </div>
          <div className="field">
            <label>
              Database filter
              <input className="mono" value={database} onChange={e => setDatabase(e.target.value)} placeholder="cards_db" />
            </label>
          </div>
          <div className="field">
            <label>
              Schema filter
              <input className="mono" value={schema} onChange={e => setSchema(e.target.value)} placeholder="public" />
            </label>
          </div>
          <div className="field">
            <label>
              Table naming
              <select value={tableNaming} onChange={e => setTableNaming(e.target.value as TableNaming)}>
                <option value="table">table name only</option>
                <option value="schema_table">schema_table prefix</option>
              </select>
            </label>
          </div>
          <div className="field">
            <label>
              Bot token environment variable
              <input
                className="mono"
                value={tokenEnv}
                onChange={e => setTokenEnv(e.target.value)}
                placeholder={name.trim() ? defaultTokenEnv(name.trim()) : 'FEATUREGEN_OM_TOKEN__<NAME>'}
              />
            </label>
          </div>
          <div className="conn-form-foot">
            <button type="submit" className="btn btn--primary" disabled={saving}>
              {saving ? 'Saving…' : 'Save connection'}
            </button>
            <span className="hint">
              The token itself stays sealed on the server: set{' '}
              <span className="mono">{envPreview ?? 'the referenced environment variable'}</span>{' '}
              where the API runs. Connections show only whether it is set.
            </span>
          </div>
        </form>
        {formError && (
          <div className="callout callout--danger" role="alert">
            <CalloutGlyph>
              <circle cx="8" cy="8" r="6.25" />
              <path d="m5.75 5.75 4.5 4.5m0-4.5-4.5 4.5" />
            </CalloutGlyph>
            <div className="callout-body">
              <p>
                <strong>The connection was not saved.</strong> {formError}
              </p>
            </div>
          </div>
        )}
      </section>

      {listError && (
        <p className="error" role="alert">
          {listError}
        </p>
      )}
      {connectors !== null && connectors.length > 0 && (
        <section className="panel">
          <h2>Configured connections</h2>
          <ul className="rows">
            {connectors.map(c => (
              <li key={c.connector_id} className="row">
                <span className="mono" style={{ fontWeight: 600 }}>
                  {c.name}
                </span>
                <span className="hint">{c.base_url}</span>
                <span className="hint mono">{scopeText(c.filters)}</span>
                <span className="hint">
                  into <span className="mono">{c.target_source}</span>
                </span>
                {c.token_present ? (
                  <span className="badge ok">token sealed</span>
                ) : (
                  <span className="badge held">token not set</span>
                )}
                <span className="tray-spacer" />
                <button
                  type="button"
                  className="btn btn--primary"
                  disabled={busy}
                  onClick={() => void runPreview(c)}
                >
                  Preview import
                </button>
                <button type="button" className="btn" disabled={busy} onClick={() => void removeConnector(c)}>
                  Remove
                </button>
              </li>
            ))}
          </ul>
          <p className="hint">Preview import is a dry run. Nothing enters the catalog until you approve.</p>
        </section>
      )}

      {previewBusy && (
        <p className="hint" role="status">
          Running the dry run against OpenMetadata…
        </p>
      )}
      {previewError && (
        <div className="callout callout--danger" role="alert">
          <CalloutGlyph>
            <circle cx="8" cy="8" r="6.25" />
            <path d="m5.75 5.75 4.5 4.5m0-4.5-4.5 4.5" />
          </CalloutGlyph>
          <div className="callout-body">
            <p>
              <strong>{previewError.lead}</strong> {previewError.detail}
            </p>
            <p>Nothing was touched.</p>
          </div>
        </div>
      )}

      {preview && (
        <section aria-label="Import preview">
          <h2>
            Preview: <span className="mono">{preview.connector.name}</span> into source{' '}
            <span className="mono">{preview.connector.target_source}</span>
          </h2>
          <SummaryStats summary={preview.data.summary} />
          <BrakeCallout brake={preview.data.brake} />
          <TagMapPanel rows={preview.data.tag_map} disabled={busy || stale} onRemap={remap} />
          <TablesPanel tables={preview.data.tables} />
          {preview.data.as_of_suggestions.length > 0 && (
            <p className="conn-asof">
              As-of suggestions attached for the reviewer:{' '}
              {preview.data.as_of_suggestions.map((s, i) => (
                <span key={`${s.table}.${s.column}`}>
                  {i > 0 && '; '}
                  <span className="mono">
                    {s.table}.{s.column}
                  </span>{' '}
                  ({s.hint})
                </span>
              ))}
              . A human confirms as-of and its basis in the review queue; the connector never
              invents semantics.
            </p>
          )}
          {preview.data.summary.semantics_pending > 0 && (
            <div className="callout callout--warn">
              <CalloutGlyph>
                <circle cx="8" cy="8" r="6.25" />
                <path d="M8 4.75v4M8 11.25v.01" />
              </CalloutGlyph>
              <div className="callout-body">
                <p>
                  <strong>
                    {preview.data.summary.semantics_pending === 1
                      ? '1 column arrives'
                      : `${preview.data.summary.semantics_pending} columns arrive`}{' '}
                    without safety facts.
                  </strong>{' '}
                  OpenMetadata does not carry as-of basis, additivity, unit, or currency. These
                  columns import searchable and are routed to the review queue for owner
                  confirmation; feature generation treats their missing facts honestly until
                  confirmed.
                </p>
              </div>
            </div>
          )}

          <div className="tray">
            <span className="tray-line tabular-nums">
              Approve import of {preview.data.summary.columns} columns into source{' '}
              <span className="mono">{preview.connector.target_source}</span>
            </span>
            <span className="tray-note">
              {confirming
                ? `These ${preview.data.summary.columns} columns will enter the catalog in one transaction. Your approval is recorded.`
                : 'Approval runs the import as one transaction, recorded under your name.'}
            </span>
            <span className="tray-spacer" />
            {confirming && !importBusy && (
              <button type="button" className="btn" onClick={() => setConfirming(false)}>
                Cancel
              </button>
            )}
            <button
              type="button"
              className="btn btn--primary"
              disabled={busy || stale || imported !== null}
              onClick={approveClick}
            >
              {imported ? 'Imported' : importBusy ? 'Importing…' : confirming ? 'Confirm approval' : 'Approve import'}
            </button>
          </div>

          {stale && (
            <div className="callout callout--warn" role="alert">
              <CalloutGlyph>
                <path d="M8 2.75 14 13.25H2z" />
                <path d="M8 6.75v2.75M8 11.5v.01" />
              </CalloutGlyph>
              <div className="callout-body">
                <p>
                  <strong>The preview went stale.</strong> OpenMetadata changed since this preview
                  (snapshot hash mismatch). Nothing was imported.
                </p>
                <p>Run the preview again and approve the fresh dry run.</p>
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  onClick={() => void runPreview(preview.connector)}
                >
                  Run preview again
                </button>
              </div>
            </div>
          )}
          {importError && (
            <div className="callout callout--danger" role="alert">
              <CalloutGlyph>
                <circle cx="8" cy="8" r="6.25" />
                <path d="m5.75 5.75 4.5 4.5m0-4.5-4.5 4.5" />
              </CalloutGlyph>
              <div className="callout-body">
                <p>
                  <strong>{importError.lead}</strong> {importError.detail}
                </p>
              </div>
            </div>
          )}
        </section>
      )}

      {imported && (
        <>
          <IngestResultCallout
            result={imported.result}
            source={imported.source}
            onReviewQueue={onReviewQueue}
            heldAdvice="Narrow the connector scope so it keeps most existing objects, then run a fresh preview."
          />
          <p className="hint">
            Import record <span className="mono">{imported.import_id}</span>: your approval is
            recorded on it; vehicle <span className="mono">openmetadata-connector</span>.
          </p>
          {imported.result.status === 'ingested' &&
            imported.review_queue.quarantined + imported.review_queue.semantics_pending > 0 && (
              <div className="callout">
                <CalloutGlyph>
                  <path d="M2.75 8h9M8.5 4.75 11.75 8 8.5 11.25" />
                </CalloutGlyph>
                <div className="callout-body">
                  <p>
                    <strong>
                      {imported.review_queue.quarantined + imported.review_queue.semantics_pending}{' '}
                      item
                      {imported.review_queue.quarantined + imported.review_queue.semantics_pending === 1
                        ? ''
                        : 's'}{' '}
                      now in the review queue for {imported.source}:
                    </strong>{' '}
                    {imported.review_queue.quarantined} quarantined column
                    {imported.review_queue.quarantined === 1 ? '' : 's'} and{' '}
                    {imported.review_queue.semantics_pending} semantics confirmation
                    {imported.review_queue.semantics_pending === 1 ? '' : 's'} (as-of, additivity,
                    unit, currency).
                  </p>
                  <button type="button" className="btn" onClick={() => onReviewQueue(imported.source)}>
                    Open review queue
                  </button>
                </div>
              </div>
            )}
        </>
      )}
    </div>
  )
}

// ---------------------------------------------------------------- preview sections

function SummaryStats({ summary }: { summary: ConnectorPreview['summary'] }) {
  return (
    <div className="stats" role="group" aria-label="Preview summary">
      <Stat n={summary.tables} label="tables" />
      <Stat n={summary.columns} label="columns" />
      <Stat n={summary.new} label="new tables" tone="ok" />
      <Stat n={summary.changed} label="changed" tone="warn" />
      <Stat n={summary.unchanged} label="unchanged" />
      <Stat n={summary.removed} label="removed" tone="danger" />
      <Stat n={summary.would_quarantine} label="would quarantine" tone="danger" />
      <Stat n={summary.semantics_pending} label="semantics pending" tone="warn" />
    </div>
  )
}

// Same semantics as the ingest result's Count: ok is always colored, warn/danger only when
// nonzero (a plain 0 stays quiet ink). The label always sits beside the number.
function Stat({ n, label, tone }: { n: number; label: string; tone?: 'ok' | 'warn' | 'danger' }) {
  const colored = tone === 'ok' || (tone !== undefined && n > 0)
  return (
    <div className="stat">
      {/* the explicit space keeps "<n> <label>" readable in text form (JSX would drop a
          line-break-only separator); visually the number sits above the label (display:block) */}
      <b className={colored ? `tone-${tone}` : undefined}>{n}</b> {label}
    </div>
  )
}

// No live-region role here: the brake verdict is part of the preview content the user just
// requested, not a later async update (role=status is reserved for the ingest result).
function BrakeCallout({ brake }: { brake: { would_hold: boolean; reason: string | null } }) {
  if (brake.would_hold) {
    return (
      <div className="callout callout--warn">
        <CalloutGlyph>
          <path d="M8 2.75 14 13.25H2z" />
          <path d="M8 6.75v2.75M8 11.5v.01" />
        </CalloutGlyph>
        <div className="callout-body">
          <p>
            <strong>Brake: this sync would be held.</strong> {brake.reason}
          </p>
          <p>
            Approving runs the same brake inside the transaction: nothing is applied until a human
            resolves the hold.
          </p>
        </div>
      </div>
    )
  }
  return (
    <div className="callout">
      <CalloutGlyph>
        <path d="M4 6h8M4 10h8" />
      </CalloutGlyph>
      <div className="callout-body">
        <p>
          <strong>Brake: clear.</strong> A sync that would drop more than 30 percent of a
          source&#39;s known objects is held for a human, exactly like a hostile upload. This one
          stays within that limit.
        </p>
      </div>
    </div>
  )
}

function TagMapPanel({
  rows,
  disabled,
  onRemap,
}: {
  rows: TagMapEntry[]
  disabled: boolean
  onRemap: (tag: string, mappedTo: string) => void
}) {
  if (rows.length === 0) return null
  return (
    <>
      <h2>Tag map</h2>
      <p className="hint">
        OpenMetadata classifications translate through this map. An unmapped tag quarantines its
        columns; imports cannot silently weaken read-scope. Changing the map re-runs the preview:
        the dry run you approve is always the one the server took.
      </p>
      <div className="panel conn-tagmap">
        <table>
          <thead>
            <tr>
              <th>OpenMetadata tag</th>
              <th>Maps to</th>
              <th>Columns</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {rows.map(r => (
              <tr key={r.om_tag}>
                <td className="mono">{r.om_tag}</td>
                <td>
                  {r.unmapped ? (
                    // The platform's sensitivity vocabulary (read_scope.SENSITIVITY_ROLES) plus
                    // the explicit ignore. The choice updates the connector config server-side
                    // and re-previews; it is never applied to this payload client-side.
                    <select
                      aria-label={`Map ${r.om_tag}`}
                      value=""
                      disabled={disabled}
                      onChange={e => {
                        if (e.target.value) {
                          onRemap(r.om_tag, e.target.value === 'ignore' ? '' : e.target.value)
                        }
                      }}
                    >
                      <option value="">
                        unmapped: quarantines {r.count} column{r.count === 1 ? '' : 's'}
                      </option>
                      <option value="pii">pii</option>
                      <option value="restricted">restricted</option>
                      <option value="ignore">ignore (not a sensitivity)</option>
                    </select>
                  ) : r.mapped_to ? (
                    <span className="mono">{r.mapped_to}</span>
                  ) : (
                    <span className="hint">ignored: not a sensitivity</span>
                  )}
                </td>
                <td className="tabular-nums">{r.count}</td>
                <td>
                  {r.unmapped ? (
                    <span className="badge unmapped">unmapped</span>
                  ) : r.mapped_to ? (
                    <span className="badge ok">mapped</span>
                  ) : (
                    <span className="badge">ignored</span>
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </>
  )
}

const STATUS_BADGE: Record<PreviewTable['status'], string> = {
  new: 'badge new',
  changed: 'badge changed',
  unchanged: 'badge',
  removed: 'badge removed',
}

function TablesPanel({ tables }: { tables: PreviewTable[] }) {
  return (
    <>
      <h2>Tables</h2>
      <ul className="rows">
        {tables.map(t => (
          <li key={t.table} className="conn-table">
            <div className="row">
              <span className={STATUS_BADGE[t.status]}>{t.status}</span>
              <span className="mono" style={{ fontWeight: 600 }}>
                {t.table}
              </span>
              <span className="hint tabular-nums">
                {t.columns} column{t.columns === 1 ? '' : 's'}
              </span>
              {t.status === 'unchanged' && (
                <span className="hint">identical to the current catalog; re-vouches freshness only</span>
              )}
              {t.quarantine.length > 0 && (
                <span className="conn-qcount">
                  {t.quarantine.length} would quarantine
                </span>
              )}
            </div>
            {t.changes.length > 0 && (
              <ul className="conn-changes">
                {t.changes.map(c => (
                  <li key={c} className="mono">
                    {c}
                  </li>
                ))}
              </ul>
            )}
            {t.quarantine.map(q => (
              <div key={q.column} className="qline">
                <span className="badge quarantine">quarantine</span>
                <code>
                  {t.table}.{q.column}
                </code>
                <span>{q.reason}</span>
              </div>
            ))}
          </li>
        ))}
      </ul>
    </>
  )
}
