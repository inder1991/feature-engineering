import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { AssetProfilePanel } from './AssetProfilePanel'
import { CatalogNarrativePanel } from './CatalogNarrativePanel'
import { DatasetPolicyPanel } from './DatasetPolicyPanel'
import {
  ApiError,
  type AssetApprovedJoin,
  type AssetDetail,
  type AssetHistoryRun,
  type AssetIdentity,
  type AuditSummary,
  type EffectiveMetadataField,
  type EvidenceProposal,
  type FieldDecisionAction,
  type RoleUsability,
  type TableRollup,
  type ContextRelationship,
  type ContextValue,
  type CrossCatalogLink,
  type EffectiveMetadataSection,
  type LatestFieldDecision,
  type Relationships,
  type SemanticCandidate,
  type SemanticDivergence,
  type SemanticSubsection,
  type SemanticVerifiedEdge,
  type TableSuggestions,
  getAssetDetail,
  getTableSuggestions,
  postFieldDecision,
} from '../api'
import { SuggestionCard } from './SuggestedFeaturesScreen'

// Asset detail: ONE catalog asset opened to its bounded sections (identity + metadata + evidence +
// relationships + readiness + history + audit), reached via a Details action on a search hit. Every
// value renders FROM the GET /catalog/assets response — authority and lifecycle come from the
// response fields, never inferred from a value being present. The loaded ETag is the OCC token a
// field-correction command echoes back; a CAS conflict (409) reloads the asset and asks the user to
// re-review rather than blind-retrying. Mirrors GovernanceReviewScreen's structure/CSS vocabulary.

// Tab order follows the dossier's arc (Task 3C): identity + meaning + semantics summary
// (Overview) → the semantic neighbourhood (Relationships) → governance — evidence, decisions,
// corrections (Metadata & evidence) → usage (Readiness) → History.
// Context (semantic Task 7) sits straight after Overview: it answers "how does everything the
// platform knows about this column connect" — source meaning, resolved meaning, concept ancestry,
// identity axes, related columns, cross-catalog links, the profile it was assembled under, the
// adjudicator's alternatives, and what is honestly not known. It is fed from the DOSSIER's own
// `context` section, so opening the tab issues no second request and shows no second snapshot.
const TABS = [
  ['overview', 'Overview'],
  ['context', 'Context'],
  ['relationships', 'Relationships'],
  ['metadata', 'Metadata & evidence'],
  ['readiness', 'Readiness'],
  ['history', 'History'],
] as const
type Tab = (typeof TABS)[number][0]

// ---- authority / provenance rendering (driven by the field's fields, NEVER by value presence) ----
// The four named authorities map from provenance; tone comes from the C1 authority level. A field
// with a non-empty value but authority "missing" still reads as unattested — the badge is a fact
// about who attested the value, not about whether a value exists.
const PROVENANCE_LABEL: Record<string, string> = {
  source_declared: 'source declared',
  system_derived: 'system derived',
  llm_proposed: 'llm proposed',
  human_staged: 'human staged',
}

// Returns a LABEL only for a value that genuinely is one. `provenance` carries
// `decision_event_id or fact_event_id` (asset_detail.py) — an opaque audit id such as
// `fde_01KYM…` (fde = field decision event). The old fallback prettified any unrecognised string by
// swapping underscores for spaces, and the badge CSS uppercases it, so an id rendered as
// `FDE 01KYMVECPH7ER0VPC0A9DW4HSB` and looked entirely deliberate. Anything unrecognised is now
// treated as an id, not a label: null, so the caller falls through to the real author.
function provenanceLabel(provenance: string | null): string | null {
  if (!provenance) return null
  return PROVENANCE_LABEL[provenance] ?? null
}

// The badge shows the value's author: the governed decision provenance if any, else the evidence-layer
// author (source attested / AI proposed / rulebook proposed), else "unattested" only when truly nothing.
function attestedByLabel(field: EffectiveMetadataField): string {
  return provenanceLabel(field.provenance) ?? field.evidence_provenance ?? 'unattested'
}

// The decision id is NOT on the badge — not as the label, not as a tooltip. An opaque
// `fde_01KYM…` on hover is still an opaque id on screen. It lives in the Detail disclosure, where
// someone tracing a decision is actually looking, and in the payload for anyone querying it.
function attributionTitle(field: EffectiveMetadataField): string {
  return `authority: ${field.authority} · c1: ${field.c1_status}`
}

// governed = a verified, load-bearing attestation (solid ok); hint = a proposal not yet governed
// (accent); missing = nothing attested (quiet). Unknown authorities stay quiet, never break.
const AUTHORITY_TONE: Record<string, string> = {
  governed: 'gj-verified',
  hint: 'gj-proposed',
  missing: 'gj-none',
}

function authorityTone(authority: string): string {
  return AUTHORITY_TONE[authority] ?? 'gj-none'
}

// A relationship row's tone/border come from the row's OWN `status` field, never from which list it
// arrived in — the delivery's thesis is that authority is a response fact, not a position. VERIFIED
// reads as governed (solid ok); anything else (e.g. PARTIALLY_CONFIRMED) reads as partial (warn).
function verifiedBadgeTone(status: string): string {
  return status === 'VERIFIED' ? 'gj-verified' : 'gj-partial'
}

function verifiedRowClass(status: string): string {
  return status === 'VERIFIED' ? 'adg-rel-verified' : 'adg-rel-partial'
}

function humanizeField(name: string): string {
  return name.replaceAll('_', ' ')
}

// ---- field-correction commands (Delivery F) ----
// AssetDetail.actions is typed unknown[]: the read model leaves the command list open, and F0 emits
// none. A correction drawer is offered for a field ONLY when the server returned a command for it
// here — editability is never inferred from a value or a role guess. Each command names the field,
// the verbs the caller may run, and the CAS anchor to echo (the expected_* triple the field was
// loaded at). The screen reads exactly this shape and ignores anything else the array carries.
interface FieldAction {
  field: string
  available_actions: FieldDecisionAction[]
  expected_latest_decision_id: string | null
  expected_evidence_set_hash: string
  expected_policy_version: string
}

const DECISION_ACTIONS: readonly FieldDecisionAction[] = [
  'confirm_existing', 'propose_override', 'confirm_override', 'reject',
]

const ACTION_LABEL: Record<FieldDecisionAction, string> = {
  confirm_existing: 'Confirm current',
  propose_override: 'Propose override',
  confirm_override: 'Confirm override',
  reject: 'Reject',
}

// A verb carries a replacement value only when it stages a new value for the field.
function actionNeedsValue(action: FieldDecisionAction): boolean {
  return action === 'propose_override' || action === 'confirm_override'
}

function asFieldAction(raw: unknown): FieldAction | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (typeof o.field !== 'string') return null
  if (!Array.isArray(o.available_actions)) return null
  const verbs = o.available_actions.filter(
    (v): v is FieldDecisionAction =>
      typeof v === 'string' && (DECISION_ACTIONS as readonly string[]).includes(v),
  )
  if (verbs.length === 0) return null
  if (typeof o.expected_evidence_set_hash !== 'string') return null
  if (typeof o.expected_policy_version !== 'string') return null
  const decId = o.expected_latest_decision_id
  return {
    field: o.field,
    available_actions: verbs,
    expected_latest_decision_id: typeof decId === 'string' ? decId : null,
    expected_evidence_set_hash: o.expected_evidence_set_hash,
    expected_policy_version: o.expected_policy_version,
  }
}

// A fresh idempotency key per composed command: on both success and 409 the drawer closes and the
// asset reloads, so one key lives exactly one command — an accidental resend of the same drawer
// dedupes server-side, never a silent retry across a CAS change.
function newIdempotencyKey(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  return `idem-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

function errorDetail(err: unknown): string {
  return err instanceof ApiError ? err.detail : String(err)
}

// The short (last two dotted segments) label for a graph/object ref, for compact node captions.
function shortRef(ref: string): string {
  const parts = ref.split('.')
  return parts.length <= 2 ? ref : parts.slice(-2).join('.')
}

export function AssetDetailScreen({ source, objectRef }: { source: string; objectRef: string }) {
  const [detail, setDetail] = useState<AssetDetail | null>(null)
  // The OCC token: the loaded snapshot's consistency token (ETag). Echoed on a correction; a 409
  // means it (or the field's CAS) moved, so we reload to a fresh one and ask for a re-review.
  const [etag, setEtag] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [tab, setTab] = useState<Tab>('overview')
  // Which field's correction drawer is open (a field name), or null.
  const [editing, setEditing] = useState<string | null>(null)
  // A cross-reload banner: the 409 re-review message (and the success confirmation) survive the
  // reload that follows, unlike the transient in-drawer error.
  const [notice, setNotice] = useState('')
  // Bumped per successful (re)load so the drawer remounts with a fresh idempotency key / CAS.
  const [generation, setGeneration] = useState(0)

  // Out-of-order guard: only the latest load may apply its result.
  const loadSeq = useRef(0)

  const load = useCallback(async (opts: { keepNotice?: boolean } = {}) => {
    const id = ++loadSeq.current
    setLoading(true)
    setError('')
    if (!opts.keepNotice) setNotice('')
    try {
      const { detail: body, etag: tag } = await getAssetDetail(source, objectRef)
      if (id !== loadSeq.current) return
      setDetail(body)
      setEtag(tag)
      setEditing(null)
      setGeneration(g => g + 1)
    } catch (e) {
      if (id !== loadSeq.current) return
      setError(errorDetail(e))
      setDetail(null)
    } finally {
      if (id === loadSeq.current) setLoading(false)
    }
  }, [source, objectRef])

  useEffect(() => {
    void load()
  }, [load])

  // Success: the correction landed — reload to the fresh evidence/decision state (a proposal is a
  // new human evidence layer, so the field's authority/evidence may now differ) and confirm it.
  const onDone = useCallback(() => {
    setNotice('Correction staged. Reloaded to the current state — it created a new human evidence '
      + 'layer, it did not rewrite the source.')
    void load({ keepNotice: true })
  }, [load])

  // 409: the field's CAS (its evidence set, decision head, or policy version) moved since it was
  // loaded. Reload to the fresh state and tell the user to re-review — NEVER blind-retry the write.
  const onConflict = useCallback((serverDetail: string) => {
    setNotice(`The data changed since you loaded it — your correction was not applied. `
      + `Re-review the current state before deciding again. (${serverDetail})`)
    void load({ keepNotice: true })
  }, [load])

  // The per-field editable command set, keyed by field name. Empty in F0 (no drawers) — a drawer
  // is offered ONLY where the server returned a command, never advertised otherwise.
  const fieldActions = useMemo(() => {
    const m = new Map<string, FieldAction>()
    for (const raw of detail?.actions ?? []) {
      const a = asFieldAction(raw)
      if (a) m.set(a.field, a)
    }
    return m
  }, [detail])

  function isUnavailable(name: string): boolean {
    return detail?.unavailable_sections.includes(name) ?? false
  }

  if (loading && !detail) {
    return (
      <section className="adg">
        <p role="status" className="hint">
          Loading <code>{objectRef}</code>…
        </p>
      </section>
    )
  }

  if (error || !detail) {
    return (
      <section className="adg">
        <p role="alert" className="error">
          Could not load this asset: {error || 'no asset returned'}
        </p>
        <p className="hint">
          An unknown ref and a ref your roles cannot read look the same: not found. Only assets you
          can read are shown.
        </p>
      </section>
    )
  }

  const { identity } = detail
  const editingAction = editing ? fieldActions.get(editing) : undefined
  const editingMeta = editing ? detail.effective_metadata?.fields[editing] : undefined

  return (
    <section className="adg">
      <header className="adg-id-head">
        <div>
          <h2 className="adg-title mono">
            {identity.column
              ? `${identity.table}.${identity.column}`
              : (identity.table ?? identity.object_ref)}
          </h2>
          <p className="hint">
            {identity.source} · {identity.kind}
            {identity.schema_name ? ` · ${identity.schema_name}` : ''}
          </p>
        </div>
        <div className="adg-id-flags">
          {identity.is_grain && <span className="badge grain">grain</span>}
          {identity.is_as_of && <span className="badge asof">as-of</span>}
        </div>
      </header>

      {notice && (
        <p role="alert" className="error">
          {notice}
        </p>
      )}

      <div className="viewtoggle adg-tabs" role="group" aria-label="Asset sections">
        {TABS.map(([id, label]) => (
          <button
            key={id}
            type="button"
            aria-pressed={tab === id}
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>

      <div className="adg-tabpanel">
        {tab === 'overview' && (
          <OverviewTab detail={detail} source={source} isUnavailable={isUnavailable} />
        )}
        {tab === 'metadata' && (
          <MetadataTab
            detail={detail}
            fieldActions={fieldActions}
            onEdit={setEditing}
            isUnavailable={isUnavailable}
          />
        )}
        {tab === 'context' && <ContextTab detail={detail} />}
        {tab === 'relationships' && (
          <RelationshipsTab detail={detail} isUnavailable={isUnavailable} />
        )}
        {tab === 'readiness' && <ReadinessTab detail={detail} isUnavailable={isUnavailable} />}
        {tab === 'history' && <HistoryTab detail={detail} isUnavailable={isUnavailable} />}
      </div>

      {editing && editingAction && (
        <CorrectionDrawer
          key={`${generation}:${editing}`}
          source={source}
          objectRef={objectRef}
          field={editing}
          etag={etag}
          action={editingAction}
          meta={editingMeta}
          onDone={onDone}
          onConflict={onConflict}
          onClose={() => setEditing(null)}
        />
      )}
    </section>
  )
}

// ---- shared field rendering -----------------------------------------------------------------

// A field with no display value but a live proposal RENDERS the proposal (standing product
// direction: AI-proposed is usable, never framed as failure). The badge then says who proposed it
// and that nobody has confirmed it — "AI proposed · unconfirmed" — so the state is legible without
// reading as an error.
function showsProposal(field: EffectiveMetadataField): boolean {
  return field.value == null && field.proposed_value != null
}

function AuthorityBadge({ field }: { field: EffectiveMetadataField }) {
  if (showsProposal(field)) {
    return (
      <span className="badge gj-proposed" title={attributionTitle(field)}>
        {`${field.evidence_provenance ?? 'proposed'} · unconfirmed`}
      </span>
    )
  }
  return (
    <span
      className={`badge ${authorityTone(field.authority)}`}
      title={attributionTitle(field)}
    >
      {attestedByLabel(field)}
    </span>
  )
}

function fieldValueText(field: EffectiveMetadataField): string {
  return field.value ?? field.proposed_value ?? '— not set'
}

// ---- type display policy (Task 3C) ----------------------------------------------------------
// The one word "unknown" appears ONLY when nothing at all is held. An operational (technical)
// type wins; else the source-DECLARED SQL type displays with its basis named; an attested type
// (Task 7) upgrades the basis automatically because it lands in operational_type.
function typeDisplay(identity: AssetIdentity): { value: string; basis: string | null } {
  const op = identity.operational_type
  if (op && op.trim() !== '' && op.trim().toLowerCase() !== 'unknown') {
    return { value: op, basis: 'operational' }
  }
  if (identity.declared_type) return { value: identity.declared_type, basis: 'declared' }
  return { value: 'unknown', basis: null }
}

// ---- overview -------------------------------------------------------------------------------

// The dossier order (Task 3C): identity → meaning (definition + AI summary + the source glossary)
// → semantics (the per-column axes, tri-state) → governance (the decision summary) → usage
// (suggested features that use this column). The deeper tabs keep their homes; this page is the
// answer to "what do we hold about this column?".
function OverviewTab({
  detail,
  source,
  isUnavailable,
}: {
  detail: AssetDetail
  source: string
  isUnavailable: (name: string) => boolean
}) {
  const { identity } = detail
  const metadata = detail.effective_metadata
  const t = typeDisplay(identity)
  const operationalKnown = t.basis === 'operational'
  return (
    <>
      <section className="adg-section">
        <h3 className="micro-label">Identity</h3>
        <dl className="kv adg-kv">
          <div><dt>source</dt><dd className="mono">{identity.source}</dd></div>
          <div><dt>kind</dt><dd>{identity.kind}</dd></div>
          {identity.schema_name && (
            <div><dt>schema</dt><dd className="mono">{identity.schema_name}</dd></div>
          )}
          {identity.table && <div><dt>table</dt><dd className="mono">{identity.table}</dd></div>}
          {identity.column && <div><dt>column</dt><dd className="mono">{identity.column}</dd></div>}
          <div><dt>object ref</dt><dd className="mono">{identity.object_ref}</dd></div>
          <div><dt>logical ref</dt><dd className="mono">{identity.logical_ref}</dd></div>
          <div><dt>graph ref</dt><dd className="mono">{identity.graph_ref}</dd></div>
        </dl>
      </section>

      <section className="adg-section">
        <h3 className="micro-label">Type</h3>
        {/* The headline type + its basis. "unknown" appears ONLY when nothing at all is held —
            a declared SQL type is real information and renders as `varchar(50) · declared`. */}
        <p className="adg-type-line" data-testid="type-display">
          <strong className="mono">{t.value}</strong>
          {t.basis && <span className="badge">{t.basis}</span>}
        </p>
        <dl className="kv adg-kv">
          <div>
            <dt>declared type</dt>
            <dd className="mono">{identity.declared_type ?? '— none declared'}</dd>
          </div>
          <div>
            <dt>operational type</dt>
            <dd className="mono">
              {operationalKnown ? identity.operational_type : '— not attested yet'}
            </dd>
          </div>
        </dl>
        <p className="hint">
          The declared type is what the source's schema calls this column. The operational type is
          whether it is actually numeric-usable — only a technical source (a real database
          connector) attests it. A declared type is never on its own evidence a column is
          operationally numeric.
        </p>
      </section>

      <MeaningSection metadata={metadata} isUnavailable={isUnavailable} />

      <SourceGlossarySection detail={detail} />

      <section className="adg-section">
        <h3 className="micro-label">Semantics — what this column means operationally</h3>
        {isUnavailable('effective_metadata') ? (
          <p className="adg-unavailable" role="status">Not available to your roles.</p>
        ) : !metadata || Object.keys(metadata.fields).length === 0 ? (
          <p className="hint">{metadata?.note ?? 'No per-field metadata on this asset.'}</p>
        ) : (
          // EVERY axis renders, tri-state: a governed/attested value with its author; else a live
          // proposal with "… · unconfirmed"; else an explicit "nothing known yet". A NULL axis must
          // be distinguishable from a hidden one — silence is the one thing this list may not say.
          <ul className="rows adg-fieldsum" data-testid="attested-metadata">
            {AXIS_FIELDS.filter(([name]) => metadata.fields[name]).map(([name, label]) => (
              <AxisRow key={name} name={name} label={label} field={metadata.fields[name]} />
            ))}
          </ul>
        )}
      </section>

      <GovernanceSummary detail={detail} />

      {/* Release-A table profile: renders ONLY for table assets and ONLY while the server offers
          the surface (flag-gated 404 ⟹ the panel renders nothing — flag-off looks like today). */}
      <AssetProfilePanel source={source} objectRef={identity.object_ref} kind={identity.kind} />

      {/* Release-B dataset policies: how this dataset's rows are chosen, and which dataset serves a
          need. Separate flag (FEATUREGEN_SOURCE_TEMPORAL_SELECTION, fail-closed on
          FEATUREGEN_DATASET_PROFILES) ⟹ 404 ⟹ renders nothing. It sits under the profile because
          the profile is what a policy must agree with. */}
      <DatasetPolicyPanel source={source} objectRef={identity.object_ref} kind={identity.kind} />

      {/* The CATALOG narrative this asset lives in. Same flag gate: 404 ⟹ renders nothing. It
          belongs on an asset page because "what is this catalog for" is the first thing anyone
          reading an unfamiliar column needs, and it had no reachable surface at all before. */}
      <CatalogNarrativePanel source={source} />

      {identity.kind === 'column' && identity.table && (
        <ColumnSuggestions source={source} identity={identity} />
      )}
    </>
  )
}

// The dossier's semantics axes, in product words (the validated sample screen's vocabulary).
// `type` lives in the Type section; `definition`/`ai_summary` live in Meaning.
const AXIS_FIELDS: readonly [string, string][] = [
  ['concept', 'Business concept'],
  ['domain', 'Data domain'],
  ['additivity', 'Aggregation behavior'],
  ['unit', 'Unit'],
  ['currency', 'Currency'],
  ['entity', 'Entity'],
  ['sensitivity_display', 'Sensitivity'],
  ['party_role', 'Party role'],
]

function AxisRow({
  name,
  label,
  field,
}: {
  name: string
  label: string
  field: EffectiveMetadataField
}) {
  const hasSomething = field.value != null || field.proposed_value != null
  return (
    <li className="row adg-field" data-testid={`axis-${name}`}>
      <span className="adg-field-label">{label}</span>
      {hasSomething ? (
        <>
          <span className="adg-field-value mono">{fieldValueText(field)}</span>
          <AuthorityBadge field={field} />
        </>
      ) : (
        // Explicit, quiet, and distinguishable from a hidden axis: nothing is known — no
        // governed value, no proposal from anyone. Not an error, not an omission.
        <span className="adg-field-value hint">nothing known yet</span>
      )}
    </li>
  )
}

// Meaning: the source definition and the AI summary, SIDE BY SIDE — the summary never replaces
// the definition, and it is labelled as AI-drafted so nobody mistakes a synthesis for the source.
function MeaningSection({
  metadata,
  isUnavailable,
}: {
  metadata: EffectiveMetadataSection | undefined
  isUnavailable: (name: string) => boolean
}) {
  if (isUnavailable('effective_metadata') || !metadata) return null
  const definition = metadata.fields.definition
  const summary = metadata.fields.ai_summary
  if (!definition && !summary) return null
  return (
    <section className="adg-section" data-testid="meaning">
      <h3 className="micro-label">Meaning</h3>
      <div className="adg-meaning">
        <div className="adg-meaning-col" data-testid="meaning-definition">
          <p className="micro-label adg-sub">Definition</p>
          {definition?.value != null ? (
            <>
              <p className="adg-meaning-text">{definition.value}</p>
              <AuthorityBadge field={definition} />
            </>
          ) : (
            <p className="hint">No definition from the source yet.</p>
          )}
        </div>
        <div className="adg-meaning-col" data-testid="meaning-summary">
          <p className="micro-label adg-sub">
            AI summary <span className="badge gj-proposed">AI-drafted</span>
          </p>
          {summary?.value != null ? (
            <p className="adg-meaning-text">{summary.value}</p>
          ) : (
            <p className="hint">No AI summary yet.</p>
          )}
        </div>
      </div>
    </section>
  )
}

// "From the source glossary" — what the source FILE itself asserted, in product words, each value
// with its source provenance chip. An asset whose upload declared none of these shows NO section
// (nothing is fabricated); the declared SQL type joins the list because the file declared it too.
const GLOSSARY_FIELDS: readonly [string, string][] = [
  ['business_term', 'Business term'],
  ['term_type', 'Term type'],
  ['process_path', 'Business processes'],
  ['related_terms', 'Related terms'],
  ['bian_path', 'BIAN classification'],
  ['fibo_path', 'FIBO classification'],
  ['physical_fqn', 'Physical path'],
]

// The L1 → L2 → L3 process path, one line. The upload stores " > "-joined levels; render with
// arrows so it reads as the path it is.
function processPathText(value: string): string {
  return value.split('>').map(part => part.trim()).filter(Boolean).join(' → ')
}

function SourceGlossarySection({ detail }: { detail: AssetDetail }) {
  const fields = detail.source_glossary?.fields ?? {}
  const declaredType = detail.identity.declared_type
  const rows = GLOSSARY_FIELDS.filter(([key]) => fields[key])
  if (rows.length === 0 && !declaredType) return null
  return (
    <section className="adg-section" data-testid="source-glossary">
      <h3 className="micro-label">From the source glossary</h3>
      <ul className="rows adg-fieldsum">
        {rows.map(([key, label]) => (
          <li className="row adg-field" key={key}>
            <span className="adg-field-label">{label}</span>
            <span className={`adg-field-value ${key === 'physical_fqn' ? 'mono' : ''}`}>
              {key === 'process_path'
                ? processPathText(fields[key].value)
                : fields[key].value}
            </span>
            <span className="badge gj-verified">{fields[key].provenance}</span>
          </li>
        ))}
        {declaredType && (
          <li className="row adg-field" key="declared_type">
            <span className="adg-field-label">Declared type</span>
            <span className="adg-field-value mono">{declaredType}</span>
            <span className="badge gj-verified">source declared</span>
          </li>
        )}
      </ul>
    </section>
  )
}

// Governance, summarised: how many fields carry a decision head, and where to act. The full
// evidence lifecycle + correction commands live in Metadata & evidence — this line keeps the
// dossier's order honest without duplicating that tab.
function GovernanceSummary({ detail }: { detail: AssetDetail }) {
  const evidence = detail.evidence
  if (!evidence) return null
  const decided = Object.keys(evidence.latest_decision_by_field).length
  return (
    <section className="adg-section" data-testid="governance-summary">
      <h3 className="micro-label">Governance</h3>
      <p className="hint">
        {decided === 0
          ? 'No governed decisions on this column yet.'
          : `${decided} ${decided === 1 ? 'field carries' : 'fields carry'} a governed decision.`}
        {' '}Evidence, decisions and corrections live in Metadata &amp; evidence.
      </p>
    </section>
  )
}

// ---- suggested features on the column (usage) -----------------------------------------------
// The P4 per-table suggestions route, filtered to the suggestions that USE the opened column.
// Honesty rules: a 403 renders an access message naming the permission (never an empty section);
// an empty filter result says whether the TABLE has suggestions that simply don't use this column.
function ColumnSuggestions({
  source,
  identity,
}: {
  source: string
  identity: AssetIdentity
}) {
  const [data, setData] = useState<TableSuggestions | null>(null)
  const [loading, setLoading] = useState(true)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState('')
  const table = identity.table
  const objectRef = identity.object_ref

  useEffect(() => {
    if (!table) return
    let live = true
    setLoading(true)
    setError('')
    setForbidden(false)
    getTableSuggestions(source, table)
      .then(body => {
        if (live) setData(body)
      })
      .catch((e: unknown) => {
        if (!live) return
        if (e instanceof ApiError && e.status === 403) setForbidden(true)
        else setError(e instanceof ApiError ? e.detail : String(e))
        setData(null)
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
  }, [source, table])

  const matching = useMemo(() => {
    if (!data) return []
    const ref = objectRef.toLowerCase()
    return data.groups.flatMap(g =>
      g.suggestions.filter(s => s.uses.some(u => u.toLowerCase() === ref)),
    )
  }, [data, objectRef])

  return (
    <section className="adg-section" data-testid="column-suggestions">
      <h3 className="micro-label">Suggested features using this column</h3>
      {loading ? (
        <p className="hint" role="status">Reading what the catalog can build with this column…</p>
      ) : forbidden ? (
        // Honest access message, never silently swallowed into an empty list.
        <p className="adg-unavailable" role="status">
          You don't have access to feature suggestions. This view needs the{' '}
          <code>catalog:read</code> permission and this session's roles don't carry it.
        </p>
      ) : error || !data ? (
        <p role="alert" className="error">
          Could not load suggestions: {error || 'no payload returned'}
        </p>
      ) : matching.length === 0 ? (
        <p className="hint">
          {data.summary.suggested === 0
            ? 'No suggestions on this table yet.'
            : `None of the ${data.summary.suggested} suggestions on this table uses this column.`}
        </p>
      ) : (
        <ul className="rows">
          {matching.map(s => <SuggestionCard key={s.name} suggestion={s} />)}
        </ul>
      )}
    </section>
  )
}

// ---- metadata + evidence --------------------------------------------------------------------

function MetadataTab({
  detail,
  fieldActions,
  onEdit,
  isUnavailable,
}: {
  detail: AssetDetail
  fieldActions: Map<string, FieldAction>
  onEdit: (field: string) => void
  isUnavailable: (name: string) => boolean
}) {
  const metadata = detail.effective_metadata
  if (isUnavailable('effective_metadata')) {
    return <p className="adg-unavailable" role="status">Metadata is not available to your roles.</p>
  }
  if (!metadata) {
    return <p className="hint">No metadata section was returned.</p>
  }
  const fieldNames = Object.keys(metadata.fields)
  if (fieldNames.length === 0) {
    return <p className="hint">{metadata.note ?? 'This asset carries no per-field metadata.'}</p>
  }
  const evidence = detail.evidence
  const evidenceUnavailable = isUnavailable('evidence')
  // A field nobody has set is not the story. On a real column five of nine are unset — `unit` and
  // `currency` on a boolean flag never will be — and nine rows where five say nothing is exactly
  // why the tab read as noise. The set ones get the eye; the rest collapse behind one line that
  // still opens into normal rows, so their Correct… action stays reachable. A field carrying a
  // live PROPOSAL is set — the proposal renders (with its unconfirmed chip), never a blank.
  const isSet = (n: string) =>
    metadata.fields[n]?.value != null || metadata.fields[n]?.proposed_value != null
  const setNames = fieldNames.filter(isSet)
  const unsetNames = fieldNames.filter(n => !isSet(n))
  return (
    <>
    <ul className="rows">
      {setNames.map(name => {
        const field = metadata.fields[name]
        const action = fieldActions.get(name)
        const proposalsByLifecycle = evidence?.proposals_by_field[name]
        const latest = evidence?.latest_decision_by_field[name]
        return (
          <FieldRow
            key={name}
            name={name}
            field={field}
            action={action}
            proposalsByLifecycle={proposalsByLifecycle}
            latest={latest}
            evidenceUnavailable={evidenceUnavailable}
            onEdit={onEdit}
          />
        )
      })}
    </ul>
    {unsetNames.length > 0 && (
      <UnsetFields
        names={unsetNames}
        metadata={metadata}
        fieldActions={fieldActions}
        evidence={evidence}
        evidenceUnavailable={evidenceUnavailable}
        onEdit={onEdit}
      />
    )}
    </>
  )
}

function UnsetFields({
  names,
  metadata,
  fieldActions,
  evidence,
  evidenceUnavailable,
  onEdit,
}: {
  names: string[]
  metadata: EffectiveMetadataSection
  fieldActions: Map<string, FieldAction>
  evidence: AssetDetail['evidence']
  evidenceUnavailable: boolean
  onEdit: (field: string) => void
}) {
  const [open, setOpen] = useState(false)
  return (
    <section className="adg-section">
      <button type="button" className="btn btn--ghost" onClick={() => setOpen(o => !o)}>
        {open ? 'Hide' : 'Show'} {names.length} not set — {names.map(humanizeField).join(', ')}
      </button>
      {open && (
        <ul className="rows">
          {names.map(name => (
            <FieldRow
              key={name}
              name={name}
              field={metadata.fields[name]}
              action={fieldActions.get(name)}
              proposalsByLifecycle={evidence?.proposals_by_field[name]}
              latest={evidence?.latest_decision_by_field[name]}
              evidenceUnavailable={evidenceUnavailable}
              onEdit={onEdit}
            />
          ))}
        </ul>
      )}
    </section>
  )
}

// ONE LINE per field: the value, who said it, and an action if the server offered one. Everything
// else — evidence lifecycle, decision history, ids, timestamps — moves behind "Detail".
//
// What this replaces spent ten lines and four coloured bars per field to say "concept is
// boolean_flag, the AI proposed it, nobody reviewed it", and repeated that for nine fields. The
// worst part was the empty buckets: STALE / REJECTED / SUPERSEDED rendered as full-width coloured
// bars even when EMPTY, so a red REJECTED bar appeared where nothing had been rejected — stating
// something alarming that was not true.
function FieldRow({
  name,
  field,
  action,
  proposalsByLifecycle,
  latest,
  evidenceUnavailable,
  onEdit,
}: {
  name: string
  field: EffectiveMetadataField
  action: FieldAction | undefined
  proposalsByLifecycle: Record<string, EvidenceProposal[]> | undefined
  latest: LatestFieldDecision | undefined
  evidenceUnavailable: boolean
  onEdit: (field: string) => void
}) {
  const [open, setOpen] = useState(false)
  // Only buckets that actually contain something. An empty bucket is not news.
  const buckets = Object.entries(proposalsByLifecycle ?? {}).filter(
    ([, proposals]) => Array.isArray(proposals) && proposals.length > 0,
  )
  return (
    <li className="row q-item adg-field-card" data-testid={`field-row-${name}`} key={name}>
      <div className="adg-field-line">
        <span className="mono gj-kind adg-field-label">{humanizeField(name)}</span>
        <span className="adg-field-value mono">
          {fieldValueText(field)}
          {/* The type field names its basis (`varchar(50) · declared`) — a declared SQL type is
              information, and the basis chip keeps it from impersonating an attested one. */}
          {field.basis && <span className="badge adg-basis">{field.basis}</span>}
        </span>
        {/* Who said it, in words. `authority hint · c1 no_value` is machinery, not an author. */}
        <AuthorityBadge field={field} />
        <span className="adg-field-controls">
          <button type="button" className="btn btn--ghost" onClick={() => setOpen(o => !o)}>
            {open ? 'Hide detail' : 'Detail'}
          </button>
          {/* No action, no affordance — an apology that "the server returned no correction
              command" is implementation talk aimed at the wrong audience. */}
          {action && (
            <button type="button" className="btn q-ghost" onClick={() => onEdit(name)}>
              Correct…
            </button>
          )}
        </span>
      </div>

      {open && (
        <div className="adg-field-detail">
          {evidenceUnavailable ? (
            <p className="adg-unavailable" role="status">
              Evidence is not available to your roles.
            </p>
          ) : buckets.length === 0 ? (
            <p className="hint">No proposals recorded for this field.</p>
          ) : (
            buckets.map(([lifecycle, proposals]) => (
              <div className="adg-lifecycle" key={lifecycle}>
                <span className={`badge ${lifecycleTone(lifecycle)}`}>{lifecycle}</span>
                <ul className="adg-proposals">
                  {proposals.map((p: EvidenceProposal) => (
                    <li key={p.evidence_id}>
                      <span className="mono">{p.proposed_value ?? '—'}</span>{' '}
                      <span className="hint">
                        {p.producer} · {p.strength}
                        {p.confidence_band ? ` · ${p.confidence_band}` : ''}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ))
          )}
          {latest && (
            <p className="hint">
              Latest decision: <strong>{latest.event_type}</strong>
              {latest.conflict_status ? ` · ${latest.conflict_status}` : ''}
              {latest.load_bearing ? ' · load-bearing' : ''} · {latest.decided_at}
              {latest.decision_event_id ? ` · ${latest.decision_event_id}` : ''}
            </p>
          )}
          <p className="hint">
            authority {field.authority} · c1 {field.c1_status}
          </p>
        </div>
      )}
    </li>
  )
}

const LIFECYCLE_TONE: Record<string, string> = {
  active: 'gj-proposed',
  stale: 'gj-partial',
  rejected: 'gj-rejected',
  superseded: 'gj-none',
}

function lifecycleTone(lifecycle: string): string {
  return LIFECYCLE_TONE[lifecycle] ?? 'gj-none'
}

// ---- context (semantic Task 7) ---------------------------------------------------------------
// NO-BLOCKED FRAMING THROUGHOUT. Nothing on this tab is styled as a failure:
//   * a proposed value is a normal, usable value wearing a neutral author chip;
//   * "nobody has decided yet" / "needs a data check" / "structurally unsuitable" are the three
//     product families, rendered as plain sentences;
//   * a missing-context code says what is NOT IN THIS VIEW for THIS reader — it is read-scoped, so
//     it is never presented as a gap in the data or as a coverage number;
//   * ownership / usage / data-product context is "not supplied" — this platform has no producer
//     for it, which is a different statement from "there is none".

// The D2 display labels, in product words. Display only: the payload carries the real
// producer/strength/lifecycle triple beside each value and that is what anything downstream reads.
const AUTHORITY_WORDS: Record<string, string> = {
  source_attested: 'source attested',
  source_proposed: 'source proposed',
  human: 'human confirmed',
  llm_proposed: 'AI proposed',
  deterministic: 'measured',
  governed: 'governed',
  system: 'system',
}

const FAMILY_WORDS: Record<string, string> = {
  undecided: 'Nobody has decided yet',
  needs_data_check: 'Needs a data check',
  structurally_unsuitable: 'Structurally unsuitable',
}

// Codes are machine words; the tab shows them as readable phrases and keeps the code as the title
// so anyone tracing one still has it.
function humanizeCode(code: string): string {
  return code.replaceAll('_', ' ').replaceAll(':', ' — ')
}

function ContextValueRow({ value }: { value: ContextValue }) {
  const shown = value.value
  return (
    <li className="row adg-field" data-testid={`context-value-${value.field}`}>
      <span className="adg-field-label">{humanizeField(value.field)}</span>
      <span className="adg-field-value mono">
        {shown == null || shown === '' ? '—' : String(shown)}
      </span>
      <span
        className="badge"
        title={
          value.producer
            ? `${value.producer}/${value.strength}/${value.lifecycle}`
            : 'no evidence record'
        }
      >
        {AUTHORITY_WORDS[value.authority_label] ?? value.authority_label}
      </span>
      {value.operational_influence === 'governed' && (
        <span className="badge grain">load-bearing</span>
      )}
    </li>
  )
}

function ContextRelationshipRow({ link }: { link: ContextRelationship }) {
  return (
    <li className="row" data-testid={`context-link-${link.relationship_ref}`}>
      <span className="mono">
        {link.left_ref} ↔ {link.right_ref}
      </span>{' '}
      <span className="badge">{humanizeCode(link.kind)}</span>{' '}
      {/* Three SEPARATE facts, never collapsed into one badge: whether the link is available at
          all, whether a human reviewed it, and whether it can be executed right now. A review is
          not permission — the server answers executability from a revalidating reader, and this
          row repeats its answer rather than inferring one from the review. */}
      <span className="badge">{link.availability}</span>{' '}
      <span className="badge">
        {link.review_status ? humanizeCode(link.review_status) : 'unreviewed'}
      </span>{' '}
      <span className="badge" title="revalidated against live dependencies">
        {link.executable_now ? 'executable now' : 'not executable now'}
      </span>
      {link.crosswalk && (
        // A crosswalk reads as a crosswalk only if the mapping table is on the row: without it,
        // "these ids are equal" and "these ids are related through this table" render identically.
        // Both legs are named — showing one would make a two-hop relationship look like a direct
        // one. An unreviewed crosswalk is a usable discovery, so it is described, never styled as
        // a failure: nobody has checked it yet is a state, not a fault.
        <p className="hint" data-testid={`context-crosswalk-${link.relationship_ref}`}>
          Related through <span className="mono">{link.crosswalk.mapping_dataset_ref}</span>{' '}
          ({link.crosswalk.source_to_mapping_refs.join(', ')} ·{' '}
          {link.crosswalk.mapping_to_target_refs.join(', ')}).{' '}
          {link.review_status === 'human_verified'
            ? 'A reviewer has confirmed this mapping.'
            : 'Nobody has reviewed this mapping yet — it is a proposal you can act on.'}{' '}
          Running a crosswalk is not available yet, whatever its review says.
        </p>
      )}
      {link.crosswalk && (
        // MEASUREMENT AND ADMISSION (Release C Task 11). Unmeasured is a STATE, described as
        // "discoverable, unmeasured" and never as a failure or a blocker. When it HAS been
        // measured, both directions appear with their own verdict — showing one would read as a
        // verdict about the pair, and a 1:1 forward with an N:1 reverse is ordinary.
        <p className="hint" data-testid={`crosswalk-measurement-${link.relationship_ref}`}>
          {!link.crosswalk.measurement ? (
            <>Discoverable, unmeasured — nobody has profiled this mapping yet.</>
          ) : (
            <>
              Measured {link.crosswalk.measurement.observed_at.slice(0, 10)} ·{' '}
              {link.crosswalk.measurement.method} /{' '}
              {link.crosswalk.measurement.row_coverage} ·{' '}
              {link.crosswalk.measurement.composed_row_count} joined rows over{' '}
              {link.crosswalk.measurement.mapping_row_count} mapping rows.{' '}
              {(link.crosswalk.directions ?? []).map(d => (
                <span key={d.direction} className="badge" title={d.reason_codes.join(', ')}>
                  {humanizeCode(d.direction)}: {d.cardinality ?? 'cardinality unknown'} ·{' '}
                  {d.production_admissible
                    ? 'production admissible'
                    : d.sandbox_admissible
                      ? 'sandbox only'
                      : 'refused'}
                </span>
              ))}{' '}
              {/* Caveats are never swallowed: they say which question the numbers do not answer. */}
              {link.crosswalk.measurement.caveats.map(code => (
                <span key={code} className="badge" title="what this measurement does not answer">
                  {humanizeCode(code)}
                </span>
              ))}
            </>
          )}
        </p>
      )}
      {link.realizations.length > 0 && (
        <ul className="rows">
          {link.realizations.map(r => (
            <li key={r.realization_revision_id} className="hint">
              {/* Fan-out never renders without its direction and scope. */}
              <span className="mono">
                {r.from_ref} → {r.to_ref}
              </span>{' '}
              · {r.cardinality ?? 'cardinality unknown'} · scope {r.scope_id ?? 'none'} ·{' '}
              {humanizeCode(r.safety_status)}
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

function ContextTab({ detail }: { detail: AssetDetail }) {
  const context = detail.context
  if (!context || context.status === 'unavailable') {
    return (
      <p className="adg-unavailable" role="status" data-testid="context-unavailable">
        Context is not available for this asset right now. Everything else on this page still
        describes the same snapshot.
      </p>
    )
  }
  const adjudication = detail.semantic_adjudication
  const uncertainty = context.uncertainty
  const notSupplied = context.not_supplied ?? uncertainty?.not_supplied ?? []
  const truncation = context.truncation
  const omitted = Object.entries(truncation?.omitted ?? {})

  return (
    <div data-testid="context-tab">
      {context.status === 'projection_unavailable' && (
        <p className="hint" role="status" data-testid="context-projection">
          The catalog projection was behind when this page was assembled, so the resolved meaning
          is not shown. Structural and profile context below is still true of this snapshot.
        </p>
      )}
      {context.status === 'table' && (
        <p className="hint" data-testid="context-table-note">
          {context.note ?? 'Table asset — column meaning is assembled per column.'}
        </p>
      )}

      {context.source_meaning && context.source_meaning.length > 0 && (
        <section className="adg-section" data-testid="context-source-meaning">
          <h3 className="micro-label">What the source file said</h3>
          <ul className="rows adg-fieldsum">
            {context.source_meaning.map(v => (
              <ContextValueRow key={`src-${v.field}`} value={v} />
            ))}
          </ul>
        </section>
      )}

      {context.resolved_meaning && context.resolved_meaning.length > 0 && (
        <section className="adg-section" data-testid="context-resolved-meaning">
          <h3 className="micro-label">What the platform resolved</h3>
          <ul className="rows adg-fieldsum">
            {context.resolved_meaning.map(v => (
              <ContextValueRow key={`res-${v.field}`} value={v} />
            ))}
          </ul>
        </section>
      )}

      {context.concept_path && context.concept_path.length > 0 && (
        <section className="adg-section" data-testid="context-concept-path">
          <h3 className="micro-label">Concept hierarchy</h3>
          <p className="mono">{context.concept_path.join(' → ')}</p>
        </section>
      )}

      <section className="adg-section" data-testid="context-identity">
        <h3 className="micro-label">Identity — entity, namespace and role</h3>
        <ul className="rows adg-fieldsum">
          {(context.resolved_meaning ?? [])
            .filter(v => v.field === 'entity' || v.field === 'party_role')
            .map(v => (
              <ContextValueRow key={`id-${v.field}`} value={v} />
            ))}
          <li className="row adg-field" data-testid="context-namespace">
            <span className="adg-field-label">Identifier namespace</span>
            {context.identifier_namespace ? (
              <>
                <span className="adg-field-value mono">
                  {context.identifier_namespace.scheme}
                </span>
                <span className="badge" title="scheme equality alone is never equality proof">
                  {context.identifier_namespace.issuer_scope
                    ? `issued by ${context.identifier_namespace.issuer_scope}`
                    : 'issuer not resolved yet'}
                </span>
              </>
            ) : (
              <span className="adg-field-value hint">
                not an identifier — no value space to name
              </span>
            )}
          </li>
        </ul>
      </section>

      {context.related_columns && (
        <section className="adg-section" data-testid="context-related-columns">
          <h3 className="micro-label">Related columns in this table</h3>
          {context.related_columns.length === 0 ? (
            <p className="hint">No other columns you can read in this table.</p>
          ) : (
            <ul className="rows">
              {context.related_columns.map(c => (
                <li key={c.object_ref} className="row">
                  <span className="mono">{c.column}</span>
                  {c.concept && <span className="badge">{c.concept}</span>}
                  {c.party_role && <span className="badge">{c.party_role}</span>}
                </li>
              ))}
            </ul>
          )}
        </section>
      )}

      <section className="adg-section" data-testid="context-links">
        <h3 className="micro-label">Cross-catalog links</h3>
        {!context.relationships || context.relationships.length === 0 ? (
          <p className="hint">
            No cross-catalog link is in view for this column and your roles.
          </p>
        ) : (
          <ul className="rows">
            {context.relationships.map(link => (
              <ContextRelationshipRow key={link.relationship_ref} link={link} />
            ))}
          </ul>
        )}
      </section>

      {context.profiles && (
        <section className="adg-section" data-testid="context-profiles">
          <h3 className="micro-label">Assembled under</h3>
          <ul className="rows adg-fieldsum">
            <li className="row adg-field">
              <span className="adg-field-label">Dataset profile</span>
              <span className="adg-field-value mono">
                {context.profiles.dataset_profile_hash ?? 'not assembled'}
              </span>
            </li>
            <li className="row adg-field">
              <span className="adg-field-label">Catalog narrative</span>
              <span className="adg-field-value mono">
                {context.profiles.catalog_profile_revision_id ?? 'none written yet'}
              </span>
            </li>
            <li className="row adg-field" data-testid="context-data-role">
              <span className="adg-field-label">Data role</span>
              <span className="adg-field-value mono">
                {context.profiles.data_role?.value ?? (
                  <span className="hint">
                    {FAMILY_WORDS[context.profiles.data_role?.unresolved_family ?? 'undecided'] ??
                      FAMILY_WORDS.undecided}
                  </span>
                )}
              </span>
            </li>
          </ul>
        </section>
      )}

      {adjudication?.status === 'available' && (
        <section className="adg-section" data-testid="context-adjudication">
          <h3 className="micro-label">Alternatives the adjudicator considered</h3>
          <p>
            <span className="mono">{adjudication.selected_concept}</span>{' '}
            <span className="badge">AI proposed</span>
          </p>
          {adjudication.alternatives && adjudication.alternatives.length > 0 && (
            <ul className="rows">
              {adjudication.alternatives.map(alt => (
                <li key={alt} className="row mono">
                  {alt}
                </li>
              ))}
            </ul>
          )}
          {adjudication.ontology_gap && (
            <p className="hint" data-testid="context-ontology-gap">
              Suggested vocabulary addition: {' '}
              <span className="mono">{adjudication.ontology_gap.proposed_label}</span> — a
              suggestion for a reviewer, never applied automatically.
            </p>
          )}
        </section>
      )}

      <section className="adg-section" data-testid="context-uncertainty">
        <h3 className="micro-label">What this view does not know</h3>
        {notSupplied.length > 0 && (
          <p className="hint" data-testid="context-not-supplied">
            Not supplied: {notSupplied.map(humanizeCode).join(', ')} — nothing in this platform
            produces them yet, which is not the same as there being none.
          </p>
        )}
        {uncertainty && uncertainty.missing_context.length > 0 && (
          <ul className="rows" data-testid="context-missing">
            {uncertainty.missing_context.map(code => (
              <li key={code} className="row hint" title={code}>
                {humanizeCode(code)}
              </li>
            ))}
          </ul>
        )}
        {omitted.length > 0 && (
          <p className="hint" data-testid="context-omitted">
            Left out of this view:{' '}
            {omitted.map(([kind, count]) => `${count} ${humanizeCode(kind)}`).join(', ')}.
          </p>
        )}
        {truncation?.truncated && (
          <p className="hint" data-testid="context-truncated">
            The neighbourhood was larger than this view's budget, so it was cut.
          </p>
        )}
      </section>
    </div>
  )
}

// ---- relationships --------------------------------------------------------------------------

function RelationshipsTab({
  detail,
  isUnavailable,
}: {
  detail: AssetDetail
  isUnavailable: (name: string) => boolean
}) {
  const rel = detail.relationships
  if (isUnavailable('relationships') || !rel) {
    return (
      <p className="adg-unavailable" role="status">
        Relationships are not available to your roles.
      </p>
    )
  }
  return (
    <>
      <NeighborhoodGraph detail={detail} relationships={rel} />

      <CrossCatalogLinks links={rel.cross_catalog ?? []} />

      <section className="adg-section">
        <h3 className="micro-label">Containment</h3>
        <p className="hint">
          Belongs to <span className="mono">{rel.containment.table.object_ref}</span>
          {' · '}
          {rel.containment.columns.length}{' '}
          {rel.containment.columns.length === 1 ? 'other column' : 'other columns'}
        </p>
      </section>

      <section className="adg-section">
        <h3 className="micro-label">Approved joins — verified</h3>
        {rel.approved_joins.length === 0 ? (
          <p className="hint">No verified joins touch this asset.</p>
        ) : (
          <ul className="rows">
            {rel.approved_joins.map(join => <ApprovedJoinRow key={joinKey(join)} join={join} />)}
          </ul>
        )}
      </section>

      <SemanticSection semantic={rel.semantic} isUnavailable={isUnavailable} />
    </>
  )
}

function joinKey(join: AssetApprovedJoin): string {
  return join.approved_join_fact_key ?? `${join.from_ref}->${join.to_ref}`
}

function ApprovedJoinRow({ join }: { join: AssetApprovedJoin }) {
  return (
    <li className={`row q-item ${verifiedRowClass(join.status)}`}>
      <div className="q-head">
        <span className="mono gj-kind">
          {shortRef(join.from_ref)} → {shortRef(join.to_ref)}
        </span>
        <span className={`badge ${verifiedBadgeTone(join.status)}`}>{join.status}</span>
        <span className="gj-score">{join.cardinality ?? 'cardinality unknown'}</span>
      </div>
    </li>
  )
}

function SemanticSection({
  semantic,
  isUnavailable,
}: {
  semantic: SemanticSubsection
  isUnavailable: (name: string) => boolean
}) {
  // Honest unavailability: a caller lacking catalog:read gets {status:'unavailable'} (also named in
  // unavailable_sections), which reads as "not available" — never an empty-success "no links".
  if (semantic.status === 'unavailable' || isUnavailable('relationships.semantic')) {
    return (
      <section className="adg-section">
        <h3 className="micro-label">Semantic links</h3>
        <p className="adg-unavailable" role="status">
          Semantic links are not available to your roles.
        </p>
      </section>
    )
  }
  return (
    <section className="adg-section">
      <h3 className="micro-label">Semantic links</h3>

      <p className="micro-label adg-sub">Verified</p>
      {semantic.verified_edges.length === 0 ? (
        <p className="hint">No verified semantic edges.</p>
      ) : (
        <ul className="rows">
          {semantic.verified_edges.map(edge => (
            <SemanticVerifiedRow key={verifiedEdgeKey(edge)} edge={edge} />
          ))}
        </ul>
      )}

      <p className="micro-label adg-sub">Proposed candidates</p>
      {semantic.candidates.length === 0 ? (
        <p className="hint">No proposed candidates.</p>
      ) : (
        <ul className="rows">
          {semantic.candidates.map(c => <SemanticCandidateRow key={c.candidate_id} candidate={c} />)}
        </ul>
      )}

      {semantic.divergences.length > 0 && (
        <>
          <p className="micro-label adg-sub">Divergences</p>
          <ul className="rows">
            {semantic.divergences.map((d, i) => (
              // eslint-disable-next-line react/no-array-index-key -- divergences carry no stable id
              <SemanticDivergenceRow key={`${d.kind}:${d.object_ref}:${i}`} divergence={d} />
            ))}
          </ul>
        </>
      )}
    </section>
  )
}

// Narrow the union via `'object_ref' in edge` (the entity arm carries object_ref/entity; the column
// arm carries from_ref/to_ref) — the api comment pins this discriminator, since `kind` is an open
// string on the column arm and can't discriminate.
function verifiedEdgeKey(edge: SemanticVerifiedEdge): string {
  if ('object_ref' in edge) {
    return edge.fact_key ?? `entity:${edge.object_ref}:${edge.entity}`
  }
  return edge.fact_key
}

function SemanticVerifiedRow({ edge }: { edge: SemanticVerifiedEdge }) {
  return (
    <li className={`row q-item ${verifiedRowClass(edge.status)}`}>
      <div className="q-head">
        <span className="mono gj-kind">
          {'object_ref' in edge
            ? `${shortRef(edge.object_ref)} — entity ${edge.entity}`
            : edge.to_ref === null
              ? `${shortRef(edge.from_ref)} → ${edge.currency_code ?? '?'} (${edge.kind})`
              : `${shortRef(edge.from_ref)} → ${shortRef(edge.to_ref)} (${edge.kind})`}
        </span>
        <span className={`badge ${verifiedBadgeTone(edge.status)}`}>{edge.status}</span>
      </div>
    </li>
  )
}

function SemanticCandidateRow({ candidate }: { candidate: SemanticCandidate }) {
  return (
    <li className="row q-item adg-rel-proposed">
      <div className="q-head">
        <span className="mono gj-kind">
          {shortRef(candidate.subject_graph_ref)} → {shortRef(candidate.target_graph_ref)}
        </span>
        <span className="badge gj-proposed">{candidate.binding_kind}</span>
        <span className="gj-score">{candidate.disposition}</span>
      </div>
      {candidate.proposed_value && (
        <p className="hint mono">proposed: {candidate.proposed_value}</p>
      )}
      {candidate.reason_codes.length > 0 && (
        <p className="hint">{candidate.reason_codes.join(' · ')}</p>
      )}
    </li>
  )
}

function SemanticDivergenceRow({ divergence }: { divergence: SemanticDivergence }) {
  return (
    <li className="row q-item adg-rel-diverge">
      <div className="q-head">
        <span className="mono gj-kind">{shortRef(divergence.object_ref)}</span>
        <span className="badge gj-partial">{divergence.kind}</span>
      </div>
      <p className="hint">
        declared <strong>{divergence.declared_entity}</strong>, governed{' '}
        <strong>{divergence.governed_entity}</strong>.
      </p>
    </li>
  )
}

// A small read-only neighborhood graph (inline SVG — no graph library). The anchor sits in the
// centre; verified neighbours (approved joins + verified semantic edges) draw as SOLID edges,
// proposed candidates as DASHED edges — visually distinct. The anchor always renders, so the canvas
// is never blank. A parallel text list mirrors every edge for assistive tech.
function NeighborhoodGraph({
  detail,
  relationships,
}: {
  detail: AssetDetail
  relationships: Relationships
}) {
  const anchorLabel = detail.identity.column
    ? `${detail.identity.table}.${detail.identity.column}`
    : (detail.identity.table ?? detail.identity.object_ref)

  // The backend returns edges where the anchor is EITHER endpoint (from_ref = ANY(..) OR to_ref =
  // ANY(..)), so the NEIGHBOR is whichever end is NOT the anchor — for a PK-side anchor of an N:1
  // join the counterparty is the FROM end. Picking `to_ref` unconditionally would draw the anchor as
  // its own neighbor and hide the real counterparty. Joins + semantic column edges carry object_ref-
  // space refs (compared against identity.object_ref); candidates carry graph_ref-space refs.
  const anchorRef = detail.identity.object_ref
  const anchorGraphRef = detail.identity.graph_ref
  const otherEnd = (fromRef: string, toRef: string, self: string): string =>
    toRef === self ? fromRef : toRef

  const neighbors: { id: string; label: string; verified: boolean; edgeLabel: string }[] = []
  const seen = new Set<string>()
  function add(id: string, label: string, verified: boolean, edgeLabel: string) {
    if (seen.has(id)) return
    seen.add(id)
    neighbors.push({ id, label, verified, edgeLabel })
  }
  for (const join of relationships.approved_joins) {
    // Key the dedupe on the CHOSEN neighbor ref, so two inbound joins stay two distinct nodes
    // instead of collapsing onto one phantom self-node.
    const neighborRef = otherEnd(join.from_ref, join.to_ref, anchorRef)
    add(`join:${neighborRef}`, shortRef(neighborRef), join.status === 'VERIFIED',
      `joins (${join.cardinality ?? 'n/a'})`)
  }
  // Cross-catalog links belong ON the graph, not only in a list below it: the whole point of the
  // link is that it is a HOP to another catalog, and a hop is a graph fact. The neighbour label
  // carries the other catalog's name — without it `comp_financial_tran_repos_dly.cif_id` reads as a
  // same-catalog neighbour and the one thing that makes the link interesting is invisible.
  //
  // Drawn whether or not a human confirmed it, for the same reason the list shows it: confirmation
  // annotates, it does not gate. `verified` only drives the stroke, so a proposed link is visually
  // distinct without looking broken.
  for (const link of relationships.cross_catalog ?? []) {
    const anchorIsLeft = link.left_object_ref === anchorRef
    const neighborRef = anchorIsLeft ? link.right_object_ref : link.left_object_ref
    const neighborSource = anchorIsLeft ? link.right_catalog_source : link.left_catalog_source
    add(`xcat:${neighborSource}:${neighborRef}`,
      `${neighborSource}.${shortRef(neighborRef)}`,
      link.status === 'confirmed',
      `${link.entity_id} (cross-catalog)`)
  }
  if (relationships.semantic.status === 'available') {
    for (const edge of relationships.semantic.verified_edges) {
      if ('object_ref' in edge) {
        add(`entity:${edge.entity}`, `entity ${edge.entity}`, edge.status === 'VERIFIED', 'entity')
      } else if (edge.to_ref === null) {
        // A FIXED-CURRENCY binding has no target column — its neighbour is the governed literal.
        const code = edge.currency_code ?? '?'
        add(`sem:code:${code}`, code, edge.status === 'VERIFIED', edge.kind)
      } else {
        const neighborRef = otherEnd(edge.from_ref, edge.to_ref, anchorRef)
        add(`sem:${neighborRef}`, shortRef(neighborRef), edge.status === 'VERIFIED', edge.kind)
      }
    }
    for (const c of relationships.semantic.candidates) {
      const neighborRef =
        c.target_graph_ref === anchorGraphRef ? c.subject_graph_ref : c.target_graph_ref
      add(`cand:${neighborRef}`, shortRef(neighborRef), false, `${c.binding_kind} (candidate)`)
    }
  }

  const cx = 170
  const cy = 130
  const r = 96
  const positioned = neighbors.map((n, i) => {
    const angle = (-Math.PI / 2) + (i * 2 * Math.PI) / Math.max(neighbors.length, 1)
    return { ...n, x: cx + r * Math.cos(angle), y: cy + r * Math.sin(angle) }
  })

  return (
    <section className="adg-section">
      <h3 className="micro-label">Neighborhood</h3>
      <svg
        className="adg-graph"
        viewBox="0 0 340 260"
        role="img"
        aria-label={`Neighborhood graph for ${anchorLabel}`}
      >
        {positioned.map(n => (
          <line
            key={`edge:${n.id}`}
            className={n.verified ? 'adg-edge adg-edge--verified' : 'adg-edge adg-edge--proposed'}
            x1={cx}
            y1={cy}
            x2={n.x}
            y2={n.y}
          />
        ))}
        {positioned.map(n => (
          <g key={`node:${n.id}`}>
            <circle
              className={n.verified ? 'adg-node adg-node--verified' : 'adg-node adg-node--proposed'}
              cx={n.x}
              cy={n.y}
              r={7}
            />
            <text className="adg-node-label" x={n.x} y={n.y - 12} textAnchor="middle">
              {n.label}
            </text>
          </g>
        ))}
        <circle className="adg-node adg-node--anchor" cx={cx} cy={cy} r={9} />
        <text className="adg-node-label adg-node-label--anchor" x={cx} y={cy + 24} textAnchor="middle">
          {anchorLabel}
        </text>
      </svg>
      <ul className="adg-graph-a11y">
        <li>{anchorLabel} (this asset)</li>
        {positioned.map(n => (
          <li key={`a11y:${n.id}`}>
            {anchorLabel} — {n.edgeLabel} — {n.label} · {n.verified ? 'verified' : 'proposed'}
          </li>
        ))}
      </ul>
    </section>
  )
}

// ---- readiness ------------------------------------------------------------------------------

function ReadinessTab({
  detail,
  isUnavailable,
}: {
  detail: AssetDetail
  isUnavailable: (name: string) => boolean
}) {
  const readiness = detail.readiness
  if (isUnavailable('readiness') || !readiness) {
    return (
      <p className="adg-unavailable" role="status">Readiness is not available to your roles.</p>
    )
  }
  const usability = readiness.usability
  const rollup = readiness.table_rollup
  return (
    <>
      {usability && (
        <>
          <p className="tabular-nums adg-usable-head" role="status">{usability.headline}</p>
          <ul className="rows">
            {usability.roles.map(r => (
              <RoleRow key={r.role} role={r} />
            ))}
          </ul>
        </>
      )}
      {rollup && <TableRollupView rollup={rollup} />}
    </>
  )
}

// Tone by STATE, never by "is something outstanding". `ai_proposed` is a normal, usable state — the
// most common one in a freshly-ingested catalog — so it must not read as a warning.
const USABILITY_TONE: Record<string, string> = {
  confirmed: 'gj-verified',
  ai_proposed: 'gj-proposed',
  needs_data_check: 'gj-partial',
  not_set: 'gj-none',
  not_considered: 'gj-none',
  // A structural refusal is not an alarm and not a to-do — a varchar simply is not a measure.
  // Quiet, like not_set: there is nothing here for anyone to act on.
  not_suitable: 'gj-none',
  unavailable: 'gj-none',
}

const ACTION_COPY: Record<string, string> = {
  confirm: 'Confirm',
  run_data_check: 'Run data check',
  assign: 'Assign',
}

function RoleRow({ role }: { role: RoleUsability }) {
  const [open, setOpen] = useState(false)
  const ids = [...role.outstanding, ...role.data_checks]
  return (
    <li className="row q-item" data-testid={`role-${role.role}`}>
      <div className="adg-field-line">
        <span className="mono gj-kind adg-field-label">{role.label}</span>
        <span className={`badge ${USABILITY_TONE[role.state] ?? 'gj-none'}`}>{role.headline}</span>
        <span className="adg-field-controls">
          {ids.length > 0 && (
            <button type="button" className="btn btn--ghost" onClick={() => setOpen(o => !o)}>
              {open ? 'Hide detail' : 'Detail'}
            </button>
          )}
          {role.action && (
            <button type="button" className="btn q-ghost">
              {ACTION_COPY[role.action] ?? role.action}
            </button>
          )}
        </span>
      </div>
      <p className="hint">{role.detail}</p>
      {open && ids.length > 0 && (
        <ul className="adg-reqs">
          {ids.map(id => (
            <li key={id}><span className="mono">{id}</span></li>
          ))}
        </ul>
      )}
    </li>
  )
}

// The parent table in ONE sentence plus a count. This replaces a list of every blocking requirement
// on the table — 341 of them on a real CIB table, all sharing one cause — rendered on every column
// page. The full lists keep their own route (GET /sources/{source}/readiness?subset={table}), so
// the number stays visible and the rows stay reachable without riding on this page.
// The SAME business entity in another catalog — the cross-catalog link. Listed strongest first and
// shown whether or not a human has confirmed it: confirmation annotates, it never gates.
//
// This section exists because the candidate ledger had no readers anywhere in the codebase, so a
// derived link (`cib.cust_num <-> ftr.cif_id`) sat in the database and appeared nowhere.
function CrossCatalogLinks({ links }: { links: CrossCatalogLink[] }) {
  return (
    <section className="adg-section">
      <h3 className="micro-label">Cross-catalog links</h3>
      {links.length === 0 ? (
        <p className="hint">
          No link to another catalog yet. A link is derived when a column in another catalog carries
          the same entity concept and a compatible type.
        </p>
      ) : (
        <ul className="rows">
          {links.map(link => (
            <li className="row q-item" key={`${link.entity_id}:${link.left_object_ref}:${link.right_object_ref}`}>
              <div className="adg-field-line">
                <span className="mono gj-kind adg-field-label">{link.entity_id}</span>
                <span className="adg-field-value mono">
                  {link.left_catalog_source}.{link.left_object_ref}
                  {' \u2194 '}
                  {link.right_catalog_source}.{link.right_object_ref}
                </span>
                <span className={`badge ${link.status === 'confirmed' ? 'gj-verified' : 'gj-proposed'}`}>
                  {link.status === 'confirmed' ? 'approved' : 'proposed'}
                </span>
              </div>
              {/* The reason, not just the verdict — a weak link says so rather than looking equal. */}
              <p className="hint">{link.why}</p>
            </li>
          ))}
        </ul>
      )}
    </section>
  )
}

function TableRollupView({ rollup }: { rollup: TableRollup }) {
  return (
    <section className="adg-section">
      <h3 className="micro-label">Parent table · {rollup.table}</h3>
      <p>{rollup.headline}</p>
      {rollup.requirements_total > 0 && (
        <p className="hint">
          {rollup.requirements_total} individual items across the table.{' '}
          {rollup.columns_needing_decision > 0
            ? `${rollup.columns_needing_decision} columns need a decision.`
            : 'None of them need a decision right now.'}
        </p>
      )}
    </section>
  )
}

// ---- history + audit ------------------------------------------------------------------------

function HistoryTab({
  detail,
  isUnavailable,
}: {
  detail: AssetDetail
  isUnavailable: (name: string) => boolean
}) {
  const history = detail.history
  const audit = detail.audit
  return (
    <>
      <section className="adg-section">
        <h3 className="micro-label">Ingestion runs</h3>
        {isUnavailable('history') || !history ? (
          <p className="adg-unavailable" role="status">History is not available to your roles.</p>
        ) : history.runs.length === 0 ? (
          <p className="hint">No ingestion runs recorded for this asset.</p>
        ) : (
          <ul className="rows">
            {history.runs.map(run => <RunRow key={run.ingestion_run_id} run={run} />)}
          </ul>
        )}
      </section>

      <section className="adg-section">
        <h3 className="micro-label">Audit — LLM summaries</h3>
        {/* Audit is separately gated by audit:read. Absent (and named in unavailable_sections) for a
            caller without it — show "not available" honestly, NEVER an invented empty state. */}
        {!audit || isUnavailable('audit') ? (
          <p className="adg-unavailable" role="status">
            Audit summaries are not available (requires audit:read).
          </p>
        ) : audit.summaries.length === 0 ? (
          <p className="hint">No audit summaries recorded.</p>
        ) : (
          <ul className="rows">
            {audit.summaries.map((s: AuditSummary) => (
              <li className="row adg-field" key={s.dispatch_ref}>
                <span className="mono">{s.task}</span>
                <span className="hint">
                  {s.stage} · {s.provider}/{s.model} · {s.prompt_version}
                  {s.outcome ? ` · ${s.outcome}` : ''}
                </span>
              </li>
            ))}
          </ul>
        )}
      </section>
    </>
  )
}

function RunRow({ run }: { run: AssetHistoryRun }) {
  return (
    <li className="row q-item" key={run.ingestion_run_id}>
      <div className="q-head">
        <span className="mono gj-kind">{run.ingestion_run_id}</span>
        <span className={`badge ${run.status === 'ingested' ? 'gj-verified' : 'gj-partial'}`}>
          {run.status}
        </span>
        <span className="gj-score">{run.relation} · {run.origin_type}</span>
      </div>
      {run.stages.length > 0 && (
        <ul className="adg-reqs">
          {run.stages.map(stage => (
            <li key={`${run.ingestion_run_id}:${stage.stage}:${stage.attempt}`}>
              <span className="mono">{stage.stage}</span>{' '}
              <span className="hint">
                {stage.state}
                {stage.reason_code ? ` · ${stage.reason_code}` : ''}
              </span>
            </li>
          ))}
        </ul>
      )}
    </li>
  )
}

// ---- correction drawer ----------------------------------------------------------------------

interface CorrectionDrawerProps {
  source: string
  objectRef: string
  field: string
  etag: string
  action: FieldAction
  meta: EffectiveMetadataField | undefined
  onDone: () => void
  onConflict: (detail: string) => void
  onClose: () => void
}

function CorrectionDrawer({
  source,
  objectRef,
  field,
  etag,
  action,
  meta,
  onDone,
  onConflict,
  onClose,
}: CorrectionDrawerProps) {
  const [verb, setVerb] = useState<FieldDecisionAction>(action.available_actions[0])
  const [replacementValue, setReplacementValue] = useState('')
  const [reason, setReason] = useState('')
  const [busy, setBusy] = useState(false)
  const [cardError, setCardError] = useState('')
  // One key per drawer session (see newIdempotencyKey). Stable across a re-render of this drawer.
  const idempotencyKey = useRef(newIdempotencyKey())

  const needsValue = actionNeedsValue(verb)
  const canSubmit = !busy && (!needsValue || replacementValue.trim().length > 0)

  async function submit() {
    setBusy(true)
    setCardError('')
    try {
      await postFieldDecision(source, objectRef, field, {
        action: verb,
        // confirm_existing pins the field's currently selected evidence; other verbs carry none.
        selectedEvidenceIds: verb === 'confirm_existing' ? (meta?.selected_evidence_ids ?? []) : [],
        replacementValue: needsValue ? replacementValue.trim() : null,
        reason: reason.trim() || null,
        idempotencyKey: idempotencyKey.current,
        // The OCC CAS triple the field was LOADED at — any drift fails closed with 409.
        expectedLatestDecisionId: action.expected_latest_decision_id,
        expectedEvidenceSetHash: action.expected_evidence_set_hash,
        expectedPolicyVersion: action.expected_policy_version,
      })
      onDone()
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        // The CAS moved under us — reload + re-review, never a silent retry with a stale anchor.
        onConflict(e.detail)
        return
      }
      setCardError(errorDetail(e))
      setBusy(false)
    }
  }

  return (
    <aside className="adg-drawer" aria-label={`Correct ${humanizeField(field)}`}>
      <div className="adg-drawer-head">
        <h3 className="adg-drawer-title">Correct {humanizeField(field)}</h3>
        <button type="button" className="btn q-ghost" onClick={onClose}>
          Close
        </button>
      </div>

      <p className="adg-honesty">
        Stage correction creates a new human evidence layer, it does not rewrite the source.
      </p>

      <div className="gj-chips" role="group" aria-label="Correction action">
        {action.available_actions.map(a => (
          <button
            type="button"
            key={a}
            className={a === verb ? 'gj-chip gj-chip--on' : 'gj-chip'}
            aria-pressed={a === verb}
            onClick={() => setVerb(a)}
          >
            {ACTION_LABEL[a]}
          </button>
        ))}
      </div>

      {needsValue && (
        <div className="field">
          <label htmlFor="adg-replacement">New value</label>
          <input
            id="adg-replacement"
            value={replacementValue}
            onChange={e => setReplacementValue(e.target.value)}
            placeholder="the value to stage"
          />
        </div>
      )}

      <div className="field">
        <label htmlFor="adg-reason">Reason (optional)</label>
        <input
          id="adg-reason"
          value={reason}
          onChange={e => setReason(e.target.value)}
          placeholder="what you checked; recorded for audit"
        />
      </div>

      {/* The OCC CAS triple + idempotency key echoed on this command — shown so the anchor being
          submitted is auditable, and the ETag it was loaded under. */}
      <dl className="kv adg-cas">
        <div><dt>etag</dt><dd className="mono">{etag || '—'}</dd></div>
        <div>
          <dt>expected decision</dt>
          <dd className="mono">{action.expected_latest_decision_id ?? 'none'}</dd>
        </div>
        <div>
          <dt>expected evidence hash</dt>
          <dd className="mono">{action.expected_evidence_set_hash}</dd>
        </div>
        <div>
          <dt>expected policy</dt>
          <dd className="mono">{action.expected_policy_version}</dd>
        </div>
        <div><dt>idempotency key</dt><dd className="mono">{idempotencyKey.current}</dd></div>
      </dl>

      <div className="gj-actions">
        <button
          type="button"
          className="btn btn--primary"
          disabled={!canSubmit}
          onClick={() => void submit()}
        >
          {busy ? 'Submitting…' : 'Stage correction'}
        </button>
        {needsValue && replacementValue.trim().length === 0 && (
          <span className="gj-gate-hint">enter a value to enable</span>
        )}
      </div>

      {cardError && (
        <p className="field-error" role="alert">
          {cardError}
        </p>
      )}
    </aside>
  )
}
