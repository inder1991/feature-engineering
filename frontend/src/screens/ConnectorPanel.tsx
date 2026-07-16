// The metadata-service path of the Ingest screen, slimmed to a sync PICKER: pick a configured
// sync (grouped under its OpenMetadata integration), run a preview (a dry run — the server pulls
// and translates WITHOUT ingesting), review the mappings the human is accountable for (tag map,
// per-table diff, quarantine, pending semantics), then approve. There is no URL/token/scope here:
// the sync and its integration carry them, configured once under Integrations.
//
// Approval posts the previewed snapshot hash AND local-baseline hash back: if OpenMetadata moved
// or the local catalog for the source changed since the preview, the server answers 409 and this
// panel asks for a fresh dry run — the human only ever approves the exact diff they reviewed.
//
// Remap is a CONFIG change, never a client-side edit of the preview payload: quietly rewriting
// previewed rows would let the UI approve something the server never showed, so changing the map
// invalidates the snapshot by design. A remap PATCHes the sync's tag_map_override (which wins per
// tag over the integration's map) and automatically re-previews the fresh snapshot.
import { useEffect, useRef, useState } from 'react'
import {
  ApiError,
  importSync,
  listIntegrations,
  listSyncs,
  patchSync,
  previewSync,
} from '../api'
import type {
  Integration,
  PreviewTable,
  Sync,
  SyncImportResult,
  SyncPreview,
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

// The database/schema a sync narrows to, for the picker label; empty when it takes the whole service.
function syncScope(sync: Sync): string {
  return [sync.database_filter, sync.schema_filter].filter(Boolean).join('.')
}

function syncLabel(sync: Sync): string {
  const scope = syncScope(sync)
  return `${sync.service_name}${scope ? ` (${scope})` : ''} → source ${sync.target_source}`
}

export function ConnectorPanel({
  onReviewQueue,
  onStage,
  onManageIntegrations,
}: {
  onReviewQueue: (source: string) => void
  onStage: (stage: ConnectorStage) => void
  onManageIntegrations: () => void
}) {
  const [integrations, setIntegrations] = useState<Integration[] | null>(null)
  const [syncs, setSyncs] = useState<Sync[]>([])
  const [loadError, setLoadError] = useState('')
  const [selectedId, setSelectedId] = useState('')

  // preview + approve. The preview keeps the exact sync it was taken against, so remap and import
  // always target what the human saw, never a later change of the picker.
  const [preview, setPreview] = useState<{ sync: Sync; data: SyncPreview } | null>(null)
  const [previewBusy, setPreviewBusy] = useState(false)
  const [previewError, setPreviewError] = useState<{ lead: string; detail: string } | null>(null)
  const [confirming, setConfirming] = useState(false)
  const [importBusy, setImportBusy] = useState(false)
  const [importError, setImportError] = useState<{ lead: string; detail: string } | null>(null)
  const [stale, setStale] = useState(false)
  const [imported, setImported] = useState<(SyncImportResult & { source: string }) | null>(null)

  // Out-of-order guard: every server round-trip takes a ticket; a response only lands if no newer
  // action started meanwhile. The busy flags already serialize the buttons; this is defense in depth.
  const seq = useRef(0)

  useEffect(() => {
    let cancelled = false
    listIntegrations().then(
      async list => {
        const perIntegration = await Promise.all(
          list.map(i => listSyncs(i.integration_id).catch(() => [] as Sync[])),
        )
        if (cancelled) return
        const flat = perIntegration.flat()
        setIntegrations(list)
        setSyncs(flat)
        setSelectedId(prev => prev || flat[0]?.sync_id || '')
      },
      (err: unknown) => {
        if (cancelled) return
        setIntegrations([])
        setSyncs([])
        setLoadError(err instanceof ApiError ? err.detail : String(err))
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
  const selectedSync = syncs.find(s => s.sync_id === selectedId) ?? null

  // Picker groups: each integration with at least one sync, syncs grouped under its name so the
  // human sees which OpenMetadata instance a pull comes from.
  const groups = (integrations ?? [])
    .map(i => ({ integration: i, syncs: syncs.filter(s => s.integration_id === i.integration_id) }))
    .filter(g => g.syncs.length > 0)

  function onSelect(id: string) {
    setSelectedId(id)
    // A different sync invalidates the current dry run: the human only approves what they previewed.
    setPreview(null)
    setPreviewError(null)
    setImportError(null)
    setStale(false)
    setConfirming(false)
    setImported(null)
  }

  async function runPreview(sync: Sync) {
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
      const data = await previewSync(sync.sync_id)
      if (seq.current !== ticket) return
      setPreview({ sync, data })
    } catch (err) {
      if (seq.current !== ticket) return
      setPreviewError(describeError(err, 'Preview failed.'))
    } finally {
      if (seq.current === ticket) setPreviewBusy(false)
    }
  }

  async function remap(tag: string, mappedTo: string) {
    if (!preview || busy) return
    const prior = preview.sync
    const ticket = ++seq.current
    setPreviewBusy(true)
    setPreviewError(null)
    setStale(false)
    setConfirming(false)
    setImported(null)
    try {
      // The override wins per tag over the integration map. Merge over the sync's current override
      // so earlier remaps survive; the server re-validates the sensitivity value.
      const nextOverride = { ...(prior.tag_map_override ?? {}), [tag]: mappedTo }
      const updated = await patchSync(prior.integration_id, prior.sync_id, {
        tag_map_override: nextOverride,
      })
      if (seq.current === ticket) {
        setSyncs(list => list.map(s => (s.sync_id === updated.sync_id ? updated : s)))
      }
      const data = await previewSync(updated.sync_id)
      if (seq.current !== ticket) return
      setPreview({ sync: updated, data })
    } catch (err) {
      if (seq.current !== ticket) return
      // The old preview described a config that just changed: drop it rather than leave a stale
      // dry run approvable.
      setPreview(null)
      setPreviewError(describeError(err, 'The remap did not apply.'))
    } finally {
      if (seq.current === ticket) setPreviewBusy(false)
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
      const res = await importSync(
        preview.sync.sync_id,
        preview.data.snapshot_hash,
        preview.data.local_baseline_hash,
      )
      if (seq.current !== ticket) return
      setImported({ ...res, source: preview.sync.target_source })
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

  const loaded = integrations !== null
  const noSyncs = loaded && syncs.length === 0

  return (
    <div>
      <section className="panel">
        <h2>Pull from a metadata service</h2>
        <p className="hint">
          Pick a sync configured under an integration and preview a dry run. There is no URL, token,
          or scope here: the sync and its instance carry them. Configure them in{' '}
          <button type="button" className="btn--link" onClick={onManageIntegrations}>
            Integrations
          </button>
          .
        </p>

        {loadError && (
          <p className="error" role="alert">
            {loadError}
          </p>
        )}

        {noSyncs ? (
          <div className="empty">
            <p>No syncs configured.</p>
            <p className="next">
              Add one in Integrations: connect an OpenMetadata instance, then sync a service into a
              catalog source.
            </p>
            <button type="button" className="btn btn--primary" onClick={onManageIntegrations}>
              Go to Integrations
            </button>
          </div>
        ) : (
          loaded && (
            <div className="picker-row">
              <label className="field">
                Sync
                <select value={selectedId} onChange={e => onSelect(e.target.value)}>
                  {groups.map(g => (
                    <optgroup key={g.integration.integration_id} label={g.integration.name}>
                      {g.syncs.map(s => (
                        <option key={s.sync_id} value={s.sync_id}>
                          {syncLabel(s)}
                        </option>
                      ))}
                    </optgroup>
                  ))}
                </select>
              </label>
              <button
                type="button"
                className="btn btn--primary"
                disabled={busy || !selectedSync}
                onClick={() => selectedSync && void runPreview(selectedSync)}
              >
                Preview import
              </button>
            </div>
          )
        )}
        {!noSyncs && loaded && (
          <p className="hint">Preview import is a dry run. Nothing enters the catalog until you approve.</p>
        )}
      </section>

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
            Preview: <span className="mono">{preview.sync.service_name}</span> into source{' '}
            <span className="mono">{preview.sync.target_source}</span>
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
              <span className="mono">{preview.sync.target_source}</span>
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
                  <strong>The preview went stale.</strong> OpenMetadata or the local catalog
                  changed since this preview was taken. Nothing was imported.
                </p>
                <p>Run the preview again and approve the fresh dry run.</p>
                <button
                  type="button"
                  className="btn"
                  disabled={busy}
                  onClick={() => void runPreview(preview.sync)}
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
            heldAdvice="Narrow the sync scope so it keeps most existing objects, then run a fresh preview."
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

function SummaryStats({ summary }: { summary: SyncPreview['summary'] }) {
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
                    // the explicit ignore. The choice PATCHes the sync's tag_map_override and
                    // re-previews; it is never applied to this payload client-side.
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
