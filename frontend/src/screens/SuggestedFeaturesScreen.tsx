import { type ReactNode, useEffect, useId, useState } from 'react'
import {
  ApiError,
  type AttributedLabel,
  type AttributedText,
  type EvidenceAuthority,
  type FeatureSuggestionHit,
  type FeatureSuggestionPageV2,
  type FeatureSuggestionV2,
  type JoinNeighbourhood,
  PROFILE_STATUS_UNAVAILABLE,
  SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION,
  type SuggestionGroupV2,
  type SuggestionOmittedCounts,
  type SuggestionProjectionState,
  type SuggestionRelationshipDependency,
  type SuggestionRequirement,
  type SuggestionSourceDataset,
  type SuggestionWarningCode,
  getTableSuggestionsV2,
} from '../api'
import { useIdentityKey } from '../session'

// Suggested features: what the deterministic engine can already build on ONE table — no hypothesis,
// no intent, no LLM. STRICTLY READ-ONLY: there is deliberately no accept / edit / dismiss control,
// because the route writes nothing. The only control on the surface is a disclosure toggle.
//
// Honesty rules the mockup does not get to override:
//   * no relevance percentage — no percentage scorer exists in this system, so the only signal shown
//     is the engine's own `binding_quality` string;
//   * DESIGN_CHECKED is "design checked", never "clean & ready" or "ready". Design checking proves
//     the inputs pass the catalog's design rules. It proves nothing about predictive usefulness and
//     nothing about production execution, and the card says so;
//   * an empty or all-unvalidated screen names its CAUSE, and names what is MISSING rather than
//     instructing the user to approve something that is not a gate. A proposal from enrichment or
//     from the source file is usable context; the absence here is an absent PROPOSAL, not an absent
//     approval;
//   * absent metadata is rendered, not omitted. "not supplied", "unavailable", "unclassified" and
//     "proposed" are values a reader must be able to see;
//   * Release A serves `read_mode=on_demand` with `projection: null`. Nothing here invents a
//     "current" badge out of an absent projection.

// ── vocabularies in words ───────────────────────────────────────────────────────────────────────

// The closed requirement vocabulary in plain words. A card says what is missing, not an enum; the
// code still renders beside it so it stays traceable to the gauntlet. An unknown code (a newer
// backend) degrades to its de-underscored words rather than disappearing.
const REQUIREMENT_WORDS: Record<string, string> = {
  TYPE_IS_NUMERIC: 'needs a confirmed numeric type',
  GRAIN_IS_UNIQUE: 'needs a confirmed unique grain',
  TEMPORAL_IS_POPULATED: 'needs a populated as-of column',
  TEMPORAL_LAG_BOUNDED: 'needs a bounded as-of lag',
  JOIN_CONNECTIVITY: 'needs a confirmed join path',
  UNIT_CONSISTENT: 'needs a declared unit',
  CURRENCY_CONSISTENT: 'needs a declared currency',
  ADDITIVITY_SUPPORTS_OPERATION: 'needs additivity that supports this operation',
}

function requirementWords(code: string): string {
  return REQUIREMENT_WORDS[code] ?? code.replaceAll('_', ' ').toLowerCase()
}

// WHAT KIND of limitation this is. The class is a WORD on every row, never a colour alone, and the
// three relationship concerns are deliberately different classes: a link nobody has confirmed is a
// REVIEW fact that leaves the suggestion worth exploring, while absent directional safety evidence
// is an EXECUTION fact about what would happen if it ran.
type LimitationClass = 'safety' | 'review' | 'fact' | 'design' | 'handling' | 'context' | 'other'

const LIMITATION_CLASS_WORDS: Record<LimitationClass, string> = {
  safety: 'Execution safety',
  review: 'Review',
  fact: 'Missing fact',
  design: 'Design caution',
  handling: 'Data handling',
  context: 'Proposed context',
  other: 'Limitation',
}

const WARNING_PRESENTATION: Record<string, { kind: LimitationClass; words: string }> = {
  NEAR_LABEL: {
    kind: 'design',
    words: 'The recipe declares this feature borders the outcome label, so it can leak the answer '
      + 'into training.',
  },
  SENSITIVE_INPUT: {
    kind: 'handling',
    words: 'An input column carries a visibility restriction, so this feature inherits it.',
  },
  MISSING_TEMPORAL_EVIDENCE: {
    kind: 'fact',
    words: 'No populated as-of date is declared for an input, so the point-in-time cut cannot be '
      + 'checked.',
  },
  MISSING_UNIT: {
    kind: 'fact',
    words: 'An input has no declared unit, so its numbers cannot be compared or added safely.',
  },
  MISSING_CURRENCY: {
    kind: 'fact',
    words: 'An input has no declared currency, so amounts may be mixed across currencies.',
  },
  RELATIONSHIP_UNCONFIRMED: {
    kind: 'review',
    words: 'A join this feature crosses was declared by an upload and confirmed by nobody. It stays '
      + 'usable for exploration; nobody has taken accountability for it.',
  },
  RELATIONSHIP_SAFETY_UNPROVEN: {
    kind: 'safety',
    words: 'A join this feature crosses has no governed-verified safety evidence, so running it '
      + 'could duplicate or drop rows.',
  },
  DIRECTIONAL_CARDINALITY_UNAVAILABLE: {
    kind: 'safety',
    words: 'A join declares no cardinality in the direction travelled, so row multiplication is '
      + 'unknown.',
  },
  PROFILE_PROPOSED: {
    kind: 'context',
    words: 'A dataset profile behind this suggestion is proposed, not confirmed.',
  },
}

const REQUIREMENT_CLASS: Record<string, LimitationClass> = {
  TYPE_IS_NUMERIC: 'fact',
  GRAIN_IS_UNIQUE: 'fact',
  TEMPORAL_IS_POPULATED: 'fact',
  TEMPORAL_LAG_BOUNDED: 'fact',
  JOIN_CONNECTIVITY: 'safety',
  UNIT_CONSISTENT: 'fact',
  CURRENCY_CONSISTENT: 'fact',
  ADDITIVITY_SUPPORTS_OPERATION: 'fact',
}

// Which typed requirement the server ALSO raises as a closed warning code. Listing both would say
// the same thing twice on one card; the requirement still appears in full in the detail drawer.
const REQUIREMENT_COVERED_BY: Record<string, SuggestionWarningCode> = {
  TEMPORAL_IS_POPULATED: 'MISSING_TEMPORAL_EVIDENCE',
  UNIT_CONSISTENT: 'MISSING_UNIT',
  CURRENCY_CONSISTENT: 'MISSING_CURRENCY',
  JOIN_CONNECTIVITY: 'RELATIONSHIP_SAFETY_UNPROVEN',
}

// WHERE a value came from, as authorship. Deliberately separate from evidence STRENGTH, which is
// how hard its producer asserts it and is rendered per occurrence in the drawer.
const BASIS_WORDS: Record<string, string> = {
  template_authored: 'recipe-authored',
  catalog_resolved: 'from catalog wording',
  human: 'human-authored',
  llm_proposed: 'AI-proposed',
}

const STATUS_WORDS: Record<string, string> = {
  DESIGN_CHECKED: 'design checked',
  NEEDS_EXTERNAL_VALIDATION: 'needs external validation',
}

// design checked reads as settled-for-design; needs external validation as partial (warn) — never
// as rejected. A card waiting on a declared fact is honest output, not a failure.
const STATUS_TONE: Record<string, string> = {
  DESIGN_CHECKED: 'gj-verified',
  NEEDS_EXTERNAL_VALIDATION: 'gj-partial',
}

const DISPOSITION_WORDS: Record<string, string> = {
  complete: 'fully classified',
  partial: 'partly classified',
  // `needs_sme` is UNREPRESENTABLE in this contract version, so no chip for it can ever occur.
  unclassified: 'unclassified',
}

const OPERAND_ROLE_WORDS: Record<string, string> = {
  measure: 'measured quantity',
  grain: 'grain key',
  time: 'time anchor',
  grouping: 'grouping key',
  other: 'other input',
}

// ── small helpers ───────────────────────────────────────────────────────────────────────────────

// The column name — the ref's last segment. A full schema.table.column ref is unreadable inline,
// and the full ref stays available in the drawer and in the element's title.
function columnOf(ref: string): string {
  return ref.split('.').pop() ?? ref
}

// Tooltips and accessible names are BOUNDED. Catalog prose can be arbitrarily long, and a 4,000
// character `title` or `aria-label` is unusable in a screen reader and unreadable as a tooltip.
// The complete value is never lost: it renders as text in the detail drawer.
const TITLE_LIMIT = 140
function bounded(value: string, limit = TITLE_LIMIT): string {
  return value.length <= limit ? value : `${value.slice(0, limit - 1)}…`
}

// An explicit absent value. Words, never a colour or an omitted section: "nobody has decided this"
// must be readable, and it must not look like a failure.
function Absent({ children }: { children: ReactNode }) {
  return <span className="sfc-absent">{children}</span>
}

function Micro({ children }: { children: ReactNode }) {
  return <h4 className="micro-label sfc-h">{children}</h4>
}

// ── attributed values ───────────────────────────────────────────────────────────────────────────

// A controlled label as a chip: the HUMAN name is what you read, the STABLE ID rides in the title
// and renders in full in the drawer. `derived` marks a value produced by a taxonomy mapping rather
// than authored — a distinction `basis` alone cannot make, because it says `template_authored` for
// both, so the signal comes from the mapping citation the server publishes.
function LabelChip({
  label,
  tone = '',
  derived = false,
}: {
  label: AttributedLabel
  tone?: string
  derived?: boolean
}) {
  return (
    <span className="sfc-chip">
      <span className={`badge ${tone}`} title={bounded(`id: ${label.id}`)}>
        {label.display_name}
      </span>
      <ProvenanceChip basis={label.basis} derived={derived} />
    </span>
  )
}

// Free catalog wording. It is NOT badged like a controlled value: it renders as quoted text with
// its authority, because "looks like a business domain" is not the same fact as "is one".
function TermChip({ text }: { text: AttributedText }) {
  return (
    <span className="sfc-chip">
      <span className="sfc-term" title={bounded(text.value)}>{text.value}</span>
      <ProvenanceChip basis={text.basis} evidence={text.evidence} />
    </span>
  )
}

function ProvenanceChip({
  basis,
  derived = false,
  evidence = [],
}: {
  basis: string
  derived?: boolean
  evidence?: EvidenceAuthority[]
}) {
  const words = derived
    ? 'derived from recipe family'
    : (BASIS_WORDS[basis] ?? basis.replaceAll('_', ' '))
  // A proposed-strength occurrence is the weakest thing a value can carry, and the reader must see
  // it beside the basis rather than have to open the drawer to find out.
  const proposed = evidence.some(e => e.strength === 'proposed')
  const tone = basis === 'llm_proposed' || proposed ? 'sfc-prov--proposed' : ''
  return (
    <span className={`sfc-prov ${tone}`}>
      {words}{proposed && basis !== 'llm_proposed' ? ' · proposed' : ''}
    </span>
  )
}

function EvidenceAxes({ evidence }: { evidence: EvidenceAuthority[] }) {
  if (evidence.length === 0) {
    return <Absent>no evidence occurrence was recorded</Absent>
  }
  return (
    <ul className="sfc-evidence">
      {evidence.map((e, i) => (
        <li key={`${e.producer}:${e.strength}:${e.lifecycle}:${e.evidence_id ?? i}`}>
          <span className="mono">{e.producer}</span> · {e.strength} · {e.lifecycle}
          {e.producer_ref && <> · cites <span className="mono">{e.producer_ref}</span></>}
          {e.evidence_id && <> · event <span className="mono">{e.evidence_id}</span></>}
        </li>
      ))}
    </ul>
  )
}

// One attributed value in the drawer: the value, its authorship, its stable id and every evidence
// occurrence behind it. Collapsing several occurrences into one "best authority" is forbidden.
function AttributedDetail({
  label,
  value,
  id,
  basis,
  evidence,
  sourceRefs,
  derived = false,
}: {
  label: string
  value: ReactNode
  id?: string | null
  basis: string
  evidence: EvidenceAuthority[]
  sourceRefs: string[]
  derived?: boolean
}) {
  return (
    <div className="sfc-attr">
      <div className="sfc-attr-head">
        <span className="sfc-attr-label">{label}</span>
        <ProvenanceChip basis={basis} derived={derived} evidence={evidence} />
      </div>
      <div className="sfc-attr-value">{value}</div>
      {id && <p className="hint mono sfc-attr-id">id: {id}</p>}
      <EvidenceAxes evidence={evidence} />
      {sourceRefs.length > 0 && (
        <p className="hint mono sfc-attr-id">cites {sourceRefs.join(', ')}</p>
      )}
    </div>
  )
}

function TextDetail({ label, text }: { label: string; text: AttributedText | null }) {
  if (text === null) {
    return (
      <div className="sfc-attr">
        <div className="sfc-attr-head"><span className="sfc-attr-label">{label}</span></div>
        <div className="sfc-attr-value"><Absent>the recipe author wrote none</Absent></div>
      </div>
    )
  }
  return (
    <AttributedDetail
      label={label} value={text.value} basis={text.basis} evidence={text.evidence}
      sourceRefs={text.source_refs}
    />
  )
}

// ── limitations (visible on the compact card, never colour-only) ────────────────────────────────

interface Limitation {
  key: string
  kind: LimitationClass
  words: string
  code: string
  refs: string[]
}

function refWords(refs: string[]): string {
  return refs.map(columnOf).join(', ')
}

// Every closed warning, plus every typed requirement the warnings do not already say. The order is
// the server's own, so two readers of the same card see the same list in the same order.
function limitationsOf(s: FeatureSuggestionV2): Limitation[] {
  const codes = new Set<string>(s.warnings.map(w => w.code))
  const out: Limitation[] = s.warnings.map((w, i) => {
    const shown = WARNING_PRESENTATION[w.code]
    return {
      key: `w:${w.code}:${i}`,
      kind: shown?.kind ?? 'other',
      // The server's `detail` is a RENDERING of the code, so it is only a fallback for a code this
      // build does not know; it is never allowed to replace the words a known code owns.
      words: shown?.words ?? (w.detail || w.code.replaceAll('_', ' ').toLowerCase()),
      code: w.code,
      refs: w.operand_refs.map(r => r[1]),
    }
  })
  for (const [i, r] of s.requirements.entries()) {
    const covered = REQUIREMENT_COVERED_BY[r.code]
    if (covered && codes.has(covered)) continue
    out.push({
      key: `r:${r.code}:${i}`,
      kind: REQUIREMENT_CLASS[r.code] ?? 'fact',
      words: requirementWords(r.code),
      code: r.code,
      refs: r.operand.slice(1),
    })
  }
  return out
}

function LimitationRow({ item }: { item: Limitation }) {
  return (
    <li className={`sfc-lim sfc-lim--${item.kind}`}>
      <span className="sfc-lim-class">{LIMITATION_CLASS_WORDS[item.kind]}</span>
      <span className="sfc-lim-words">
        {item.words}
        {item.refs.length > 0 && (
          <> <span className="mono sfc-lim-refs">{refWords(item.refs)}</span></>
        )}
      </span>
      <span className="mono sfc-lim-code">{item.code}</span>
    </li>
  )
}

// ── the compact card ────────────────────────────────────────────────────────────────────────────

// How many controlled values fit before the card starts counting instead of listing. Small on
// purpose: the card is a scan target, and the drawer lists every value with its provenance.
const CHIP_PREVIEW = 3

function ChipRow({
  label,
  labels,
  absent,
  tone,
}: {
  label: string
  labels: AttributedLabel[]
  absent: string
  tone?: string
}) {
  const shown = labels.slice(0, CHIP_PREVIEW)
  const more = labels.length - shown.length
  return (
    <div className="sfc-chiprow">
      <span className="sfc-chiprow-label">{label}</span>
      {labels.length === 0 ? (
        <Absent>{absent}</Absent>
      ) : (
        <>
          {shown.map(l => <LabelChip key={l.id} label={l} tone={tone} />)}
          {more > 0 && <span className="sfc-more">+{more} more</span>}
        </>
      )}
    </div>
  )
}

function Fact({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="sfc-fact">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  )
}

function entityWords(s: FeatureSuggestionV2): ReactNode {
  const grain = s.grain_refs.map(r => columnOf(r[1])).join(', ')
  if (s.entity === null && !grain) return <Absent>no entity or grain resolved</Absent>
  return (
    <>
      {s.entity === null
        ? <Absent>entity not named</Absent>
        : <span title={bounded(`id: ${s.entity.id}`)}>{s.entity.display_name}</span>}
      {' per '}
      {grain
        ? <span className="mono">{grain}</span>
        : <Absent>no governed grain column</Absent>}
    </>
  )
}

function sourceRoleSummary(datasets: SuggestionSourceDataset[]): string | null {
  const roles = datasets.map(d => d.data_role?.display_name).filter((r): r is string => !!r)
  return roles.length > 0 ? roles.join(' + ') : null
}

// Exported for the asset-detail column dossier, which renders the SAME card for the suggestions
// that use the opened column — one semantic and warning vocabulary for a suggestion everywhere.
export function SuggestionCard({
  hit,
  omitted,
  headingLevel = 3,
}: {
  hit: FeatureSuggestionHit
  omitted?: SuggestionOmittedCounts
  headingLevel?: 3 | 4
}) {
  const s = hit.suggestion
  const [open, setOpen] = useState(false)
  const detailId = useId()
  const limitations = limitationsOf(s)
  const roles = sourceRoleSummary(s.source_datasets)
  const Heading = headingLevel === 4 ? 'h4' : 'h3'
  const tables = s.source_datasets.length

  return (
    <li className="row q-item sfc">
      <div className="sfc-head">
        <Heading className="sfc-name mono">{s.display_name}</Heading>
        <span className={`badge ${STATUS_TONE[s.validation_status] ?? 'gj-none'}`}>
          {STATUS_WORDS[s.validation_status] ?? s.validation_status}
        </span>
        <span
          className="badge sfc-origin"
          title="Generated by a governed recipe. It is a suggestion, not a registered feature."
        >
          suggested · {s.generation_source}
        </span>
        {limitations.length > 0 ? (
          <span
            className={`badge sfc-limcount${
              limitations.some(l => l.kind === 'safety') ? ' sfc-limcount--safety' : ''}`}
          >
            {limitations.length} {limitations.length === 1 ? 'limitation' : 'limitations'}
          </span>
        ) : (
          <span className="sfc-nolim">no limitations recorded</span>
        )}
      </div>

      {/* The one place the words could mislead. "Design checked" describes the INPUTS, and the
          reader is told so where the badge is, because this card also renders on the column
          dossier where the page-level note does not exist. */}
      {s.validation_status === 'DESIGN_CHECKED' && (
        <p className="sfc-limit-note">
          Design checked means the inputs pass the catalog&apos;s design rules. Predictive
          usefulness and production execution are not proven.
        </p>
      )}

      <div className="sfc-tax">
        <div className="sfc-chiprow">
          <span className="sfc-chiprow-label">Category</span>
          {s.feature_category === null ? (
            <Absent>no category mapped yet</Absent>
          ) : (
            <LabelChip
              label={s.feature_category}
              tone="sfc-cat"
              derived={s.feature_category_derived_from_family_mapping}
            />
          )}
          <span className="sfc-more">
            {DISPOSITION_WORDS[s.discovery_disposition] ?? s.discovery_disposition}
          </span>
        </div>
        <ChipRow
          label="Business domains" labels={s.business_domains}
          absent="not supplied: no controlled domain vocabulary is registered here"
        />
        <ChipRow label="Use cases" labels={s.use_cases} absent="not supplied" />
      </div>

      <dl className="sfc-meaning">
        <Fact label="What it measures">
          {s.business_interpretation === null
            ? <Absent>the recipe carries no interpretation</Absent>
            : <span className="sfc-clamp">{s.business_interpretation.value}</span>}
        </Fact>
        <Fact label="Why it is useful">
          {s.business_value === null
            ? <Absent>no business value has been written for this recipe yet</Absent>
            : <span className="sfc-clamp">{s.business_value.value}</span>}
        </Fact>
      </dl>

      <dl className="sfc-facts">
        <Fact label="Entity and grain">{entityWords(s)}</Fact>
        <Fact label="Operation, window, time">
          <span className="mono">{s.operation_kind}</span>
          {' over '}
          {s.window ? <span className="mono">{s.window}</span> : <Absent>no window</Absent>}
          {' at '}
          {s.time_ref
            ? <span className="mono">{columnOf(s.time_ref[1])}</span>
            : <Absent>no time anchor</Absent>}
        </Fact>
        <Fact label="Sources">
          {tables} {tables === 1 ? 'table' : 'tables'}, {s.operands.length}{' '}
          {s.operands.length === 1 ? 'column' : 'columns'}
          {' · '}
          {roles ?? <Absent>data roles not supplied: dataset profiles are unavailable</Absent>}
        </Fact>
      </dl>

      <p className="mono sfc-recipe">{s.recipe}</p>

      {limitations.length > 0 && (
        <ul className="sfc-lims" aria-label="Requirements and limitations">
          {limitations.map(item => <LimitationRow key={item.key} item={item} />)}
        </ul>
      )}

      <div className="sfc-foot">
        {s.binding_quality && (
          <span className="gj-score">binding {s.binding_quality}</span>
        )}
        <button
          type="button"
          className="sfc-toggle"
          aria-expanded={open}
          aria-controls={detailId}
          aria-label={`${open ? 'Hide' : 'Show'} full detail for ${bounded(s.display_name, 80)}`}
          onClick={() => setOpen(v => !v)}
        >
          {open ? 'Hide full detail' : 'Full detail'}
        </button>
      </div>

      {open && <SuggestionDetail hit={hit} id={detailId} omitted={omitted} />}
    </li>
  )
}

// ── the expanded detail ─────────────────────────────────────────────────────────────────────────

// Nouns for a bound that bit. Reported rather than silently truncated: a card that quietly shortens
// a list would be claiming it shows everything it has.
const BOUND_NOUN: Record<string, string> = {
  operands: 'input columns',
  business_domains: 'business domains',
  contextual_domain_terms: 'catalog domain terms',
  contextual_entity_terms: 'catalog entity terms',
  use_cases: 'use cases',
  keywords: 'keywords',
  authoring_notes: 'authoring notes',
  relationship_dependencies: 'relationship legs',
  evidence_refs: 'evidence references',
  term_evidence: 'evidence occurrences behind catalog terms',
}

const WITHHELD_REASON: Record<string, string> = {
  withheld_missing_trace: 'the engine recorded no decision trace',
  withheld_incomplete_trace: 'the decision trace could not explain a design-checked result',
  withheld_missing_context: 'the grounding context was missing',
  withheld_unresolvable_path: 'the relationship path could not be projected onto a logical identity',
  withheld_non_recipe_generation_source: 'it was not generated from a recipe',
}

function TruncationNotes({ omitted }: { omitted: SuggestionOmittedCounts }) {
  const entries = Object.entries(omitted)
    .filter((entry): entry is [string, number] => typeof entry[1] === 'number' && entry[1] > 0)
  if (entries.length === 0) {
    return <p className="hint">Nothing on this page was truncated or withheld.</p>
  }
  return (
    <ul className="sfc-omit">
      {entries.map(([key, n]) => (
        <li key={key}>
          {WITHHELD_REASON[key]
            ? `${n} ${n === 1 ? 'candidate was' : 'candidates were'} withheld: ${
              WITHHELD_REASON[key]}.`
            : `${n} ${BOUND_NOUN[key] ?? key.replaceAll('_', ' ')} were not listed.`}
        </li>
      ))}
    </ul>
  )
}

function Currentness({ projection }: { projection: SuggestionProjectionState | null }) {
  if (projection === null) {
    return (
      <p className="hint">
        No stored projection. This was computed for this request, so nothing claims to be current or
        stale: it is what the catalog said when the page loaded.
      </p>
    )
  }
  return (
    <dl className="kv">
      <div><dt>State</dt><dd>{projection.state}</dd></div>
      <div>
        <dt>Built at</dt>
        <dd>{projection.generated_at ?? <Absent>not recorded</Absent>}</dd>
      </div>
      <div>
        <dt>Why not current</dt>
        <dd>{projection.stale_reason ?? <Absent>no reason recorded</Absent>}</dd>
      </div>
      <div>
        <dt>Read scope</dt>
        <dd className="mono">{projection.read_scope_key} · epoch {projection.scope_epoch}</dd>
      </div>
      <div>
        <dt>Target fingerprint</dt>
        <dd className="mono">{projection.target_fingerprint}</dd>
      </div>
      <div>
        <dt>Built fingerprint</dt>
        <dd className="mono">
          {projection.current_fingerprint ?? <Absent>none</Absent>}
        </dd>
      </div>
    </dl>
  )
}

function Hashes({ label, values, absent }: { label: string; values: string[]; absent: string }) {
  return (
    <div>
      <dt>{label}</dt>
      <dd className={values.length > 0 ? 'mono' : undefined}>
        {values.length > 0 ? values.join(', ') : <Absent>{absent}</Absent>}
      </dd>
    </div>
  )
}

function DatasetDetail({ dataset }: { dataset: SuggestionSourceDataset }) {
  const unavailable = dataset.profile_status === PROFILE_STATUS_UNAVAILABLE
  const role = (label: string, value: AttributedLabel | null) => (
    <div>
      <dt>{label}</dt>
      <dd>
        {value === null
          ? (
            <Absent>
              {unavailable ? 'unavailable: no dataset profile has been produced' : 'not supplied'}
            </Absent>
          )
          : <LabelChip label={value} />}
      </dd>
    </div>
  )
  return (
    <div className="sfc-dataset">
      <p className="sfc-dataset-head">
        <span className="mono">{dataset.table_ref}</span>{' '}
        <span className="hint mono">{dataset.catalog_source}</span>{' '}
        <span className={`badge ${unavailable ? 'sfc-unavailable' : ''}`}>
          profile {dataset.profile_status}
        </span>
      </p>
      <dl className="kv">
        {role('Data role', dataset.data_role)}
        {role('Authority role', dataset.authority_role)}
        {role('Temporal model', dataset.temporal_storage_model)}
        {role('Primary entity', dataset.primary_entity)}
        <div>
          <dt>Profile hash</dt>
          <dd className={dataset.dataset_profile_hash ? 'mono' : undefined}>
            {dataset.dataset_profile_hash ?? <Absent>none</Absent>}
          </dd>
        </div>
      </dl>
    </div>
  )
}

function RelationshipRow({ leg }: { leg: SuggestionRelationshipDependency }) {
  return (
    <li className="sfc-rel">
      <p className="mono sfc-rel-path">
        {leg.from_ref[1]} → {leg.to_ref[1]}
      </p>
      <p className="hint">
        {leg.relationship_kind} · cardinality {leg.cardinality} · safety {leg.safety_status} ·
        review {leg.review_status}
      </p>
      <p className="hint mono">
        {leg.relationship_ref}
        {leg.realization_content_hash ? ` · ${leg.realization_content_hash}` : ''}
      </p>
    </li>
  )
}

function SuggestionDetail({
  hit,
  id,
  omitted,
}: {
  hit: FeatureSuggestionHit
  id: string
  omitted?: SuggestionOmittedCounts
}) {
  const s = hit.suggestion
  const p = hit.provenance
  return (
    <div className="sfc-detail" id={id} role="group" aria-label={`Full detail for ${s.display_name}`}>
      <section className="sfc-sec">
        <Micro>Meaning</Micro>
        <TextDetail label="What it measures" text={s.business_interpretation} />
        <TextDetail label="Why it is useful" text={s.business_value} />
        <div className="sfc-chiprow">
          <span className="sfc-chiprow-label">Keywords</span>
          {s.keywords.length === 0
            ? <Absent>none</Absent>
            : s.keywords.map(k => <TermChip key={k.value} text={k} />)}
        </div>
      </section>

      <section className="sfc-sec">
        <Micro>Classification</Micro>
        <dl className="kv">
          <div>
            <dt>Feature category</dt>
            <dd>
              {s.feature_category === null
                ? <Absent>no category mapped yet</Absent>
                : (
                  <LabelChip
                    label={s.feature_category} tone="sfc-cat"
                    derived={s.feature_category_derived_from_family_mapping}
                  />
                )}
            </dd>
          </div>
          <div>
            <dt>Recipe family</dt>
            <dd>
              {s.recipe_family === null
                ? <Absent>the recipe declares no family</Absent>
                : <LabelChip label={s.recipe_family} />}
            </dd>
          </div>
          <div>
            <dt>Mapping coverage</dt>
            <dd>{DISPOSITION_WORDS[s.discovery_disposition] ?? s.discovery_disposition}</dd>
          </div>
        </dl>
        {s.feature_category !== null && (
          <AttributedDetail
            label="Feature category provenance" value={s.feature_category.display_name}
            id={s.feature_category.id} basis={s.feature_category.basis}
            evidence={s.feature_category.evidence} sourceRefs={s.feature_category.source_refs}
            derived={s.feature_category_derived_from_family_mapping}
          />
        )}
        <Micro>Business domains</Micro>
        {s.business_domains.length === 0 ? (
          <p className="hint">
            <Absent>not supplied.</Absent> No controlled business-domain vocabulary is registered on
            this deployment, so no value here can be a governed domain. The catalog&apos;s own
            wording is listed under Catalog terms below.
          </p>
        ) : (
          s.business_domains.map(d => (
            <AttributedDetail
              key={d.id} label="Business domain" value={d.display_name} id={d.id} basis={d.basis}
              evidence={d.evidence} sourceRefs={d.source_refs}
            />
          ))
        )}
        <Micro>Use cases</Micro>
        {s.use_cases.length === 0 ? (
          <p className="hint"><Absent>not supplied.</Absent> This recipe has no canonical use case
            mapped yet.</p>
        ) : (
          s.use_cases.map(u => (
            <AttributedDetail
              key={u.id} label="Use case" value={u.display_name} id={u.id} basis={u.basis}
              evidence={u.evidence} sourceRefs={u.source_refs}
            />
          ))
        )}
      </section>

      <section className="sfc-sec" data-testid="sfc-catalog-terms">
        <Micro>Catalog terms</Micro>
        <p className="hint">
          The catalog&apos;s own wording on the columns this feature reads. These are terms, not
          controlled business domains or entities: nothing has mapped them to a governed vocabulary,
          so they are shown with their authority and are not filter keys.
        </p>
        <div className="sfc-chiprow">
          <span className="sfc-chiprow-label">Domain wording</span>
          {s.contextual_domain_terms.length === 0
            ? <Absent>the catalog records none on these columns</Absent>
            : s.contextual_domain_terms.map(t => <TermChip key={t.value} text={t} />)}
        </div>
        <div className="sfc-chiprow">
          <span className="sfc-chiprow-label">Entity wording</span>
          {s.contextual_entity_terms.length === 0
            ? <Absent>the catalog records none on these columns</Absent>
            : s.contextual_entity_terms.map(t => <TermChip key={t.value} text={t} />)}
        </div>
        {s.contextual_domain_terms.map(t => (
          <AttributedDetail
            key={t.value} label="Domain wording" value={t.value} basis={t.basis}
            evidence={t.evidence} sourceRefs={t.source_refs}
          />
        ))}
        {s.contextual_entity_terms.map(t => (
          <AttributedDetail
            key={t.value} label="Entity wording" value={t.value} basis={t.basis}
            evidence={t.evidence} sourceRefs={t.source_refs}
          />
        ))}
      </section>

      <section className="sfc-sec">
        <Micro>Entity and grain</Micro>
        {s.entity === null ? (
          <p className="hint">
            <Absent>no entity was named.</Absent> The recipe bound no entity-linked concept here, so
            this feature is computed per column rather than per named entity.
          </p>
        ) : (
          <AttributedDetail
            label="Entity" value={s.entity.display_name} id={s.entity.id} basis={s.entity.basis}
            evidence={s.entity.evidence} sourceRefs={s.entity.source_refs}
          />
        )}
        <dl className="kv">
          <div>
            <dt>Governed grain</dt>
            <dd className="mono">
              {s.grain_refs.length > 0
                ? s.grain_refs.map(r => `${r[0]}:${r[1]}`).join(', ')
                : <Absent>no grain column resolved</Absent>}
            </dd>
          </div>
        </dl>
      </section>

      <section className="sfc-sec">
        <Micro>Computation</Micro>
        <p className="mono sfc-recipe">{s.recipe}</p>
        <dl className="kv">
          <div><dt>Operation</dt><dd className="mono">{s.recipe_parts.operation}</dd></div>
          <div>
            <dt>Measures</dt>
            <dd className="mono">
              {s.recipe_parts.measures.length > 0
                ? s.recipe_parts.measures.join(', ')
                : <Absent>none</Absent>}
            </dd>
          </div>
          <div>
            <dt>Grain clause</dt>
            <dd className="mono">{s.recipe_parts.grain || <Absent>none</Absent>}</dd>
          </div>
          <div>
            <dt>Window</dt>
            <dd className="mono">{s.recipe_parts.window || <Absent>none</Absent>}</dd>
          </div>
          <div>
            <dt>Time anchor</dt>
            <dd className="mono">{s.recipe_parts.time || <Absent>none</Absent>}</dd>
          </div>
        </dl>
        <TextDetail label="Point-in-time intent" text={s.point_in_time_declaration} />
        <TextDetail label="Recipe stage" text={s.recipe_stage} />
        <TextDetail label="Eligibility" text={s.eligibility_note} />
        <TextDetail label="Output additivity" text={s.output_additivity} />
        {s.authoring_notes.length === 0 ? (
          <div className="sfc-attr">
            <div className="sfc-attr-head"><span className="sfc-attr-label">Authoring notes</span></div>
            <div className="sfc-attr-value"><Absent>the recipe author wrote none</Absent></div>
          </div>
        ) : (
          s.authoring_notes.map(n => (
            <AttributedDetail
              key={n.value} label="Authoring note" value={n.value} basis={n.basis}
              evidence={n.evidence} sourceRefs={n.source_refs}
            />
          ))
        )}
      </section>

      <section className="sfc-sec">
        <Micro>Source datasets</Micro>
        {s.source_datasets.length === 0
          ? <p className="hint"><Absent>no dataset was bound</Absent></p>
          : s.source_datasets.map(d => (
            <DatasetDetail key={`${d.catalog_source}:${d.table_ref}`} dataset={d} />
          ))}
      </section>

      <section className="sfc-sec">
        <Micro>Every input column</Micro>
        <ul className="sfc-operands">
          {s.operands.map(o => (
            <li key={`${o.catalog_source}:${o.graph_object_ref}`}>
              <p className="sfc-operand-head">
                <span className="mono">{columnOf(o.graph_object_ref)}</span>{' '}
                <span className="badge">{OPERAND_ROLE_WORDS[o.classification]
                  ?? o.classification}</span>
                {o.recipe_role && <span className="hint"> recipe slot {o.recipe_role}</span>}
                {o.visibility_requires_current.length > 0 && (
                  <span className="badge sensitivity">
                    restricted: {o.visibility_requires_current.join(', ')}
                  </span>
                )}
              </p>
              <p className="hint mono">
                {o.catalog_source} · {o.logical_ref} · in {o.table_ref}
              </p>
              <p className="hint mono">
                {o.evidence_refs.length > 0
                  ? `evidence ${o.evidence_refs.join(', ')}`
                  : <Absent>no evidence pin recorded</Absent>}
              </p>
            </li>
          ))}
        </ul>
      </section>

      <section className="sfc-sec">
        <Micro>Relationships this feature crosses</Micro>
        {s.relationship_dependencies.length === 0 ? (
          <p className="hint">
            No relationship was traversed: every input column is on one table.
          </p>
        ) : (
          <ul className="sfc-rels">
            {s.relationship_dependencies.map(leg => (
              <RelationshipRow key={`${leg.relationship_ref}:${leg.from_ref[1]}:${leg.to_ref[1]}`}
                leg={leg} />
            ))}
          </ul>
        )}
      </section>

      <section className="sfc-sec">
        <Micro>Requirements and limitations</Micro>
        {s.warnings.length === 0 && s.requirements.length === 0 ? (
          <p className="hint">Nothing was raised against this suggestion.</p>
        ) : (
          <>
            <ul className="sfc-lims">
              {limitationsOf(s).map(item => <LimitationRow key={item.key} item={item} />)}
            </ul>
            <ul className="sfc-reqs">
              {s.requirements.map((r: SuggestionRequirement, i) => (
                <li key={`${r.code}:${r.operand.join(':')}:${i}`}>
                  <span className="mono">{r.code}</span>{' '}
                  <span className="mono">{r.operand.join(' · ')}</span>{' '}
                  <span className="hint">{r.detail || requirementWords(r.code)}</span>
                  {r.params && r.params.length > 0 && (
                    <span className="hint mono">
                      {' '}({r.params.map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(', ')})
                    </span>
                  )}
                </li>
              ))}
            </ul>
            <ul className="sfc-reqs">
              {s.warnings.map((w, i) => (
                <li key={`${w.code}:${i}`}>
                  <span className="mono">{w.code}</span>{' '}
                  <span className="hint">{w.detail}</span>
                  {w.operand_refs.length > 0 && (
                    <span className="hint mono">
                      {' '}{w.operand_refs.map(r => r.join(':')).join(', ')}
                    </span>
                  )}
                </li>
              ))}
            </ul>
          </>
        )}
      </section>

      <section className="sfc-sec">
        <Micro>Currentness</Micro>
        <Currentness projection={hit.projection} />
      </section>

      <section className="sfc-sec">
        <Micro>Provenance and revisions</Micro>
        <dl className="kv">
          <div><dt>Suggestion id</dt><dd className="mono">{s.suggestion_id}</dd></div>
          <div><dt>Revision</dt><dd className="mono">{s.suggestion_revision_id}</dd></div>
          <div><dt>Contract</dt><dd className="mono">{s.schema_version}</dd></div>
          <div>
            <dt>Recipe</dt>
            <dd className="mono">{s.template_id ?? <Absent>none</Absent>}</dd>
          </div>
          <div>
            <dt>Recipe revision</dt>
            <dd className="mono">{s.recipe_revision_id ?? <Absent>none</Absent>}</dd>
          </div>
          <div>
            <dt>Discovery revision</dt>
            <dd className="mono">
              {s.discovery_metadata_revision_id
                ?? <Absent>this recipe has no discovery mapping</Absent>}
            </dd>
          </div>
          <div>
            <dt>Grounding trace</dt>
            <dd className="mono">{s.grounding_trace_content_hash}</dd>
          </div>
          <Hashes
            label="Validation rules" values={s.validation_rule_content_hashes}
            absent="no rule hash recorded"
          />
          <Hashes
            label="Read-scope rules" values={s.read_scope_rule_content_hashes}
            absent="no rule hash recorded"
          />
          <Hashes
            label="Semantic context" values={s.semantic_context_hashes}
            absent="unavailable: no semantic context was consumed"
          />
          <Hashes
            label="Dataset profiles" values={s.dataset_profile_hashes}
            absent="unavailable: no dataset profile was consumed"
          />
          <Hashes
            label="Metadata snapshots" values={p.metadata_snapshot_ids} absent="none"
          />
          <Hashes
            label="Dependency revisions" values={p.dependency_revision_ids} absent="none"
          />
          <Hashes label="Evidence events" values={p.evidence_event_ids} absent="none" />
          <Hashes
            label="Realization revisions" values={p.relationship_realization_revision_ids}
            absent="none"
          />
          <div>
            <dt>Built by</dt>
            <dd className="mono">{p.producer_commit ?? <Absent>not recorded</Absent>}</dd>
          </div>
          <div>
            <dt>Built at</dt>
            <dd>{p.generated_at ?? <Absent>not recorded</Absent>}</dd>
          </div>
        </dl>
      </section>

      {omitted && (
        <section className="sfc-sec">
          <Micro>Truncated on this page</Micro>
          <p className="hint">
            Counted for the whole page, not for this card alone.
          </p>
          <TruncationNotes omitted={omitted} />
        </section>
      )}
    </div>
  )
}

// ── page-level notes ────────────────────────────────────────────────────────────────────────────

// WHICH bound dropped the tables that were dropped, in words. Named rather than numbered on purpose:
// the numbers live in ONE place (join_path.MAX_NEIGHBOUR_TABLES / MAX_COLUMNS_CONSIDERED, quoted in
// the payload's own terms), so a copy that printed "20" here would be free to drift from the cap the
// server actually applied. An unknown reason from a newer backend degrades to no clause at all
// rather than to a guess.
const LIMIT_WORDS: Record<string, string> = {
  table_cap: 'the automatic limit on how many joined tables one page loads',
  column_budget: 'the automatic limit on how many columns one page grounds against',
}

// What the page did NOT look at. Suggestions are grounded on this table plus the tables joined
// DIRECTLY to it, capped — walking the join graph transitively has no resource bound on a real
// catalog, where almost everything reaches the customer/account hub. Saying nothing would turn
// every empty state on this screen into a claim the page never actually checked ("nothing else is
// buildable here" when the truth is "we did not look"). So the limit is stated ALWAYS, truncated or
// not: deeper join paths exist, and they were deliberately not loaded.
function NeighbourhoodNote({ n }: { n: JoinNeighbourhood }) {
  const joins = n.max_hops === 1 ? '1 join' : `${n.max_hops} joins`
  const because = n.limit_reason ? LIMIT_WORDS[n.limit_reason] : ''
  return (
    <p className="hint sug-neighbourhood" data-testid="neighbourhood">
      {n.truncated ? (
        <>
          Showing <strong>{n.tables_considered} of {n.tables_available}</strong> directly joined
          tables{because ? ` — ${because}` : ''}.{' '}
        </>
      ) : n.tables_available > 0 ? (
        <>
          Grounded on this table and all {n.tables_available} directly joined{' '}
          {n.tables_available === 1 ? 'table' : 'tables'}.{' '}
        </>
      ) : (
        <>
          Grounded on this table alone — no confirmed join reaches another table from here.{' '}
        </>
      )}
      Deeper join paths were not automatically considered: only tables within {joins} of this one are
      loaded on a page view.
    </p>
  )
}

// ── the screen ──────────────────────────────────────────────────────────────────────────────────

export function SuggestedFeaturesScreen({ source, table }: { source: string; table: string }) {
  const [data, setData] = useState<FeatureSuggestionPageV2 | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  // A 403 is not a failure to report as one: the route is gated on catalog:read and this session's
  // roles do not carry it. Named separately so the screen can say WHICH permission is missing
  // instead of leaking the server's detail string into a red alert.
  const [forbidden, setForbidden] = useState(false)
  // The deployment does not serve the discovery contract this client asks for. A distinct state:
  // it is not a broken page and not a permission problem.
  const [unsupported, setUnsupported] = useState(false)
  // Read scope decides which columns ground and which suggestions exist at all, so a result read
  // under one identity is not an answer for another. Keyed on principal + claims, NOT on the URL.
  const identity = useIdentityKey()

  useEffect(() => {
    let live = true
    // Clear FIRST, synchronously with the identity/anchor change: a result read under the previous
    // claims must never remain on screen while the new request is in flight.
    setData(null)
    setLoading(true)
    setError('')
    setForbidden(false)
    setUnsupported(false)
    getTableSuggestionsV2(source, table)
      .then(body => {
        if (!live) return
        setData(body)
      })
      .catch((e: unknown) => {
        if (!live) return
        if (e instanceof ApiError && e.status === 403) setForbidden(true)
        else if (e instanceof ApiError
          && e.errorCode === SUGGESTIONS_UNSUPPORTED_CONTRACT_VERSION) setUnsupported(true)
        else setError(e instanceof ApiError ? e.detail : String(e))
        setData(null)
      })
      .finally(() => {
        if (live) setLoading(false)
      })
    return () => {
      live = false
    }
  }, [source, table, identity])

  if (loading) {
    return (
      <section className="sug">
        <p role="status" className="hint">
          Reading what this catalog can build on <code>{table}</code>…
        </p>
      </section>
    )
  }

  // Read-only and permission-gated: the honest answer is which permission is missing, not a red
  // "could not load". Deliberately no fix-it control — the role model is not this screen's to change.
  if (forbidden) {
    return (
      <section className="sug">
        <div className="callout callout--warn">
          <div className="callout-body">
            <p role="status">
              <strong>You don’t have access to feature suggestions.</strong> This view needs the{' '}
              <code>catalog:read</code> permission and this session’s roles don’t carry it.
            </p>
            <p className="hint">
              Roles that hold it: catalog_viewer, data_owner, feature_engineer and platform_admin.
            </p>
          </div>
        </div>
      </section>
    )
  }

  if (unsupported) {
    return (
      <section className="sug">
        <div className="callout callout--warn">
          <div className="callout-body">
            <p role="status">
              <strong>This deployment does not serve the discovery contract.</strong> The screen
              asked for suggestion contract version 2 and the server refused it.
            </p>
            <p className="hint">
              The server is older than this screen. Nothing is wrong with the catalog or with your
              permissions.
            </p>
          </div>
        </div>
      </section>
    )
  }

  if (error || !data) {
    return (
      <section className="sug">
        <p role="alert" className="error">
          Could not load suggestions: {error || 'no payload returned'}
        </p>
      </section>
    )
  }

  const { collection } = data
  const { summary, groups, rejections } = collection

  // Cause #0: there is no such table. Stands alone — every count below would be a claim about a
  // table this catalog does not hold.
  if (collection.table_known === false) {
    return (
      <section className="sug">
        <div className="callout callout--warn">
          <div className="callout-body">
            <p role="status">
              <strong>No such table in this catalog.</strong>{' '}
              <span className="mono">{collection.anchor_catalog_source}</span> holds no table called{' '}
              <span className="mono">{collection.anchor_table_ref}</span>, so there is nothing to
              suggest on — and nothing here is a statement about its columns.
            </p>
            <p className="hint">
              Check the catalog and the table name (the bare name, e.g. <code>comp_fin_tran</code>,
              or its full <code>schema.table</code> ref). Search the catalog to find the table, then
              open Suggested features from one of its hits.
            </p>
          </div>
        </div>
      </section>
    )
  }

  const byId = new Map(data.hits.map(hit => [hit.suggestion.suggestion_id, hit]))
  const noPointInTime = rejections.filter(r => r.code === 'NO_POINT_IN_TIME')
  const hasCards = data.hits.length > 0

  return (
    <section className="sug">
      <p className="hint sug-scope">
        <span className="mono">{collection.anchor_table_ref}</span> ·{' '}
        <span className="mono">{collection.anchor_catalog_source}</span>
      </p>

      <div className="stats" role="group" aria-label="Suggestion summary">
        <Stat n={summary.suggested} label="suggested" />
        {/* deliberately untoned: 0 design checked is an honest state, never an error */}
        <Stat n={summary.design_checked} label="design checked" />
        <Stat n={summary.needs_external_validation} label="need external validation" />
        <Stat n={summary.groups} label="entities" />
      </div>

      <p className="hint sug-note">
        Design checked means the inputs pass the catalog’s design rules. It is not proof that a
        feature predicts anything, and not proof that it can run in production.
      </p>

      {/* Read-only posture and page currentness in one statement: both are facts about THIS view,
          and Release A has no stored projection, so no badge here may claim to be current. */}
      <p className="hint sug-readonly" data-testid="currentness">
        These are suggestions, not registered features. A governed recipe grounded each one against
        this catalog; none of them is in the feature registry, and this view is read-only.{' '}
        {data.read_mode === 'on_demand' && data.projection === null ? (
          <>
            Nothing here is stored: the page was computed for this request, so no card claims to be
            current or stale.
          </>
        ) : (
          <>Served from a stored projection, state {data.projection?.state ?? 'unknown'}.</>
        )}
      </p>

      {collection.neighbourhood && <NeighbourhoodNote n={collection.neighbourhood} />}

      {/* Cause #1: nothing to suggest and nothing blocked — no column carries the meaning a recipe
          needs. The copy names what is MISSING, never an approval the user is told to perform: a
          proposal from enrichment or from the upload is already usable context. */}
      {summary.suggested === 0 && rejections.length === 0 && (
        <div className="callout callout--accent">
          <div className="callout-body">
            <p>
              <strong>No suggestions yet.</strong> No column on this table carries the business
              meaning a recipe needs: an entity to compute per, a quantity to measure, and an as-of
              date to compute at.
            </p>
            <p className="hint">
              A meaning proposed by enrichment or declared in the upload is enough to ground on. It
              does not have to be confirmed first, so nothing here is waiting on an approval. What is
              missing is a proposal. Run enrichment on this source, or declare the columns&apos;
              meaning in the next upload.
            </p>
          </div>
        </div>
      )}

      {/* Cause #2: the highest-value state — a governance to-do, not an empty screen. */}
      {noPointInTime.length > 0 && (
        <div className="callout callout--warn">
          <div className="callout-body">
            <p>
              <strong>
                {noPointInTime.length} {noPointInTime.length === 1 ? 'feature is' : 'features are'}
                {' '}blocked
              </strong>
              : this table has no confirmed as-of column, so every windowed feature would risk
              future leakage.
            </p>
            <p className="hint">
              Confirm the table’s as-of column on Governance review — it waits there under As-of
              date — and these come back on their own.
            </p>
          </div>
        </div>
      )}

      {/* Cause #3: cards exist but none is design checked — the honest NORMAL state, not a failure. */}
      {summary.design_checked === 0 && hasCards && (
        <p className="hint sug-note">
          Nothing is design checked yet: every suggestion below needs one more declared fact before
          its inputs pass the catalog’s design rules. Each card names the fact it is waiting on.
        </p>
      )}

      {groups.map(group => (
        <EntityGroup
          key={group.grain_refs[0]?.join(':') ?? 'unlabelled'}
          group={group} byId={byId} omitted={collection.omitted_counts}
        />
      ))}

      {Object.values(collection.omitted_counts).some(n => (n ?? 0) > 0) && (
        <section className="panel sug-omitted" data-testid="page-truncation">
          <h2>What this page left out</h2>
          <TruncationNotes omitted={collection.omitted_counts} />
        </section>
      )}

      {rejections.length > noPointInTime.length && (
        <section className="panel sug-rejections">
          <h2>Not offered</h2>
          <ul className="rows">
            {rejections
              .filter(r => r.code !== 'NO_POINT_IN_TIME')
              .map(r => (
                <li className="row" key={`${r.code}:${r.candidate_name}`}>
                  <span className="mono">{r.candidate_name}</span>{' '}
                  <span className="hint">{r.explanation}</span>
                </li>
              ))}
          </ul>
        </section>
      )}
    </section>
  )
}

// Same semantics as the dashboard's Stat, minus the tones: no count on this screen is an alert.
function Stat({ n, label }: { n: number; label: string }) {
  return (
    <div className="stat">
      <b>{n}</b> {label}
    </div>
  )
}

// One entity's features. The heading is the ENTITY (entity.display_name, e.g. 'account'); the
// suffix is the COLUMN it is computed per. A group bound to a column the catalog could not NAME an
// entity for shows only that column — a heading built from the column would read "cif_id features"
// beside "customer features", claiming an entity the catalog never attested.
function EntityGroup({
  group,
  byId,
  omitted,
}: {
  group: SuggestionGroupV2
  byId: Map<string, FeatureSuggestionHit>
  omitted: SuggestionOmittedCounts
}) {
  const grain = group.grain_refs.map(r => columnOf(r[1])).join(', ')
  return (
    <section className="sug-group">
      {grain && (
        <div className="sug-group-head">
          {group.entity && <h2 className="sug-group-title">{group.entity.display_name} features</h2>}
          <span className="hint mono" title={bounded(group.grain_refs.map(r => r[1]).join(', '))}>
            per entity {grain}
          </span>
          {group.contextual_entity_terms.map(t => <TermChip key={t.value} text={t} />)}
        </div>
      )}
      <ul className="rows">
        {group.suggestion_ids.map(id => {
          const hit = byId.get(id)
          return hit
            ? <SuggestionCard key={id} hit={hit} omitted={omitted} />
            : null
        })}
      </ul>
    </section>
  )
}
