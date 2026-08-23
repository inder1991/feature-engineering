import { type ReactNode, useId, useState } from 'react'
import {
  type AttributedLabel,
  type AttributedText,
  type EvidenceAuthority,
  type FeatureSuggestionHit,
  type FeatureSuggestionV2,
  PROFILE_STATUS_UNAVAILABLE,
  type SuggestionOmittedCounts,
  type SuggestionProjectionState,
  type SuggestionRelationshipDependency,
  type SuggestionRequirement,
  type SuggestionSourceDataset,
  type SuggestionWarningCode,
} from '../api'
import { bounded, columnOf } from './suggestionText'

// ONE suggestion, rendered the same way everywhere it appears: the compact card, its expandable
// audit drawer, and the vocabulary maps and small presentational helpers they are built from.
//
// This module exists so the column dossier does not have to import a SCREEN to get a card. Both
// consumers — `SuggestedFeaturesScreen` (a table's suggestions) and `AssetDetailScreen`'s column
// dossier — import from here, so a suggestion carries one semantic and warning vocabulary on both
// surfaces by construction rather than by discipline. Nothing page-level lives here: the summary
// stats, the cause callouts, the neighbourhood note and the entity grouping belong to the screen.
//
// STRICTLY READ-ONLY: there is deliberately no accept / edit / dismiss control, because the route
// writes nothing. The only control on the surface is a disclosure toggle.
//
// Honesty rules the mockup does not get to override:
//   * no relevance percentage — no percentage scorer exists in this system, so the only signal shown
//     is the engine's own `binding_quality` string;
//   * DESIGN_CHECKED is "design checked", never "clean & ready" or "ready". Design checking proves
//     the inputs pass the catalog's design rules. It proves nothing about predictive usefulness and
//     nothing about production execution, and the card says so;
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

// BOTH states name the same axis, so the badge is self-explaining: a reader who sees "design
// checked" on one card and "design not checked" on another understands the badge answers one
// narrow question about DESIGN, and is not declaring the feature ready. That replaces a
// per-card paragraph nobody read past the second card.
const STATUS_WORDS: Record<string, string> = {
  DESIGN_CHECKED: 'design checked',
  NEEDS_EXTERNAL_VALIDATION: 'design not checked',
}

// Neither state gets the success fill. Solid green reads as "good to go", which is precisely
// the over-trust the removed paragraph existed to prevent — a design check is not an end-to-end
// verification. Quiet chips; the WORDS carry the verdict. Not-checked stays partial, never
// rejected: a card waiting on a declared fact is honest output, not a failure.
const STATUS_TONE: Record<string, string> = {
  DESIGN_CHECKED: 'gj-none',
  NEEDS_EXTERNAL_VALIDATION: 'gj-partial',
}

const DISPOSITION_WORDS: Record<string, string> = {
  complete: 'fully classified',
  partial: 'partly classified',
  // `needs_sme` is UNREPRESENTABLE in this contract version, so no chip for it can ever occur.
  unclassified: 'unclassified',
}

// BR-8 contract v3: execution readiness in words — a SEPARATE axis from the design-check badge
// beside it. "Idea" language for UNASSESSED/CONCEPTUAL_ONLY, because a pattern nobody assessed
// for execution is honest output, not a failure; blocked states name the fact; and NOTHING here
// gets the success fill — even ready-to-materialize stays a quiet chip whose words carry it.
const READINESS_WORDS: Record<string, string> = {
  UNASSESSED: 'idea — execution not assessed',
  CONCEPTUAL_ONLY: 'conceptual pattern',
  FORMULA_BLOCKED: 'formula blocked',
  FORMULA_AUTHORABLE: 'formula authorable',
  FORMULA_VALIDATED: 'formula validated',
  MATERIALIZATION_BLOCKED: 'materialization blocked',
  MATERIALIZATION_READY: 'ready to materialize',
  RETIRED: 'retired',
}

const READINESS_TONE: Record<string, string> = {
  FORMULA_BLOCKED: 'gj-partial',
  MATERIALIZATION_BLOCKED: 'gj-partial',
}

// A newer backend's enum member renders as its de-underscored words, never a crash and never a
// blank chip (the v3 compatibility rule, pinned by test).
function readinessWords(state: string): string {
  return READINESS_WORDS[state] ?? state.toLowerCase().replace(/_/g, ' ')
}

const COMPUTATION_KIND_WORDS: Record<string, string> = {
  conceptual_pattern: 'a conceptual pattern — a useful idea, not an exact computation',
  deterministic_formula: 'an exact deterministic formula',
  governed_model_output: 'a governed model output',
}

// Each blocker in banking language; the machine code stays beside it for the auditor. An unknown
// code renders its de-underscored words — same rule as the states.
// ▲ Codes that are WARNING-shaped, not blocker-shaped (child 5b item 7 / the gold ruling): gold
// evidence withholds the production CERTIFICATION, never the artifact — building, previewing and
// sandbox-running all proceed. Rendering it as a blocker line told users a build was stopped
// that nothing stops. The line does not disappear; it changes shape.
const WARNING_SHAPED_CODES = new Set(['gold_evaluation_unproven'])

const BLOCKER_WORDS: Record<string, string> = {
  gold_evaluation_unproven:
    'the worked examples that prove this formula have not been run yet — this withholds '
    + 'production certification, not building or sandbox runs',
  ambiguous_operand_binding:
    'more than one column could supply an input and nobody has picked one',
  no_reviewed_formula_expectation:
    'no reviewed formula definition exists for this recipe',
  formula_outside_grammar_capability:
    'the formula needs an operation the platform grammar does not support yet',
  engine_capability_unproven:
    'the selected execution engine has not proven it can run this',
  model_feature_spec_owns_readiness:
    'this is a model output — the model-feature contract governs its readiness',
}

const BLOCKER_GROUP_WORDS: Record<string, string> = {
  data_meaning: 'data meaning',
  time: 'time',
  currency: 'currency',
  relationship: 'relationships',
  formula_capability: 'formula capability',
  governance: 'governance',
  execution: 'execution',
}

const OPERAND_ROLE_WORDS: Record<string, string> = {
  measure: 'measured quantity',
  grain: 'grain key',
  time: 'time anchor',
  grouping: 'grouping key',
  other: 'other input',
}

// A traversed join leg, in words. This is the row a reader consults to find out WHICH join is
// unproven, so it may not be the one place raw enums reach the page.
//
// Cardinality arrives in TWO disjoint notations because two producers write this contract: the
// join-path walker passes the `graph_edge` column through verbatim ('1:1' / '1:N' / 'N:1', the DB
// CHECK), while the planner emits the taxonomy's `Cardinality` StrEnum ('one_to_one' …). Nothing
// normalizes between them, so BOTH are mapped here and each falls back to its own words.
const CARDINALITY_WORDS: Record<string, string> = {
  '1:1': 'one row each way',
  one_to_one: 'one row each way',
  '1:N': 'one row fans out to many — this join can multiply rows',
  one_to_many: 'one row fans out to many — this join can multiply rows',
  'N:1': 'many rows collapse to one',
  many_to_one: 'many rows collapse to one',
  many_to_many: 'many rows fan out both ways — this join can multiply rows',
  unknown: 'not declared, so row multiplication is unknown',
}

// Whether running this leg is cleared. `clearing` is declared-with-no-contradicting-fact OR
// governed-verified; `unverified` is a fact-linked edge nobody verified.
const SAFETY_WORDS: Record<string, string> = {
  clearing: 'cleared to run',
  unverified: 'not cleared to run',
}

// WHO took accountability for the link. `file_declared` (nobody did) and the governed approval
// statuses are different facts, and "confirmed by nobody" is a meaningful answer rather than a
// missing one. The rejected/stale/reverify members are reachable on the column's CHECK constraint
// even though today's writers demote such an edge out of the operational path.
const REVIEW_WORDS: Record<string, string> = {
  file_declared: 'declared by an upload, confirmed by nobody',
  VERIFIED: 'confirmed by a governed approval',
  DRAFT: 'an approval is drafted, not confirmed',
  PARTIALLY_CONFIRMED: 'partly confirmed',
  REJECTED: 'the approval was rejected',
  STALE: 'the approval has gone stale',
  REVERIFY: 'the approval needs re-verification',
  governed_bridge: 'governed by a bridge fact',
  unlinked: 'no governing fact',
}

// An unknown member from a newer backend degrades to its de-underscored words rather than
// disappearing — the same rule every other vocabulary in this file follows.
function vocabWords(words: Record<string, string>, value: string): string {
  return words[value] ?? value.replaceAll('_', ' ').toLowerCase()
}

// ── small helpers ───────────────────────────────────────────────────────────────────────────────

// An explicit absent value. Words, never a colour or an omitted section: "nobody has decided this"
// must be readable, and it must not look like a failure.
function Absent({ children }: { children: ReactNode }) {
  return <span className="sfc-absent">{children}</span>
}

// A drawer section heading. The level is THREADED from the card so the drawer's sections nest
// UNDER the suggestion's own heading on both surfaces: the table screen heads a card at h3 and its
// sections at h4, and the column dossier (whose own section heading is already h3) heads the card
// at h4 and its sections at h5. A fixed level would flatten the dossier into two sibling h4s and
// tell a screen-reader user the audit material sits beside the suggestion rather than inside it.
function Micro({ level, children }: { level: 4 | 5; children: ReactNode }) {
  const Tag = level === 5 ? 'h5' : 'h4'
  return <Tag className="micro-label sfc-h">{children}</Tag>
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
export function TermChip({ text }: { text: AttributedText }) {
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
      {' · per '}
      {grain
        ? <span className="mono">{grain}</span>
        : <Absent>no governed grain column</Absent>}
    </>
  )
}


// A recipe may bind an `asof` operand without resolving the governed `time_ref`. Those are different
// truths: the former explains the computation, while the latter is the time authority downstream
// execution can rely on. Keep both visible instead of promoting a recipe slot into governance.
function timeBindingWords(s: FeatureSuggestionV2): ReactNode {
  if (s.time_ref) return <span className="mono">{columnOf(s.time_ref[1])}</span>
  const recipeAsOf = s.operands.find(o => o.recipe_role === 'asof')
  if (recipeAsOf) {
    return (
      <>
        <span className="mono">{columnOf(recipeAsOf.graph_object_ref)}</span>{' '}
        <Absent>recipe as-of role; governed time anchor unresolved</Absent>
      </>
    )
  }
  return <Absent>no time anchor</Absent>
}


// Exported for the asset-detail column dossier, which renders the SAME card for the suggestions
// that use the opened column — one semantic and warning vocabulary for a suggestion everywhere.
// The payload speaks in storage enums. "365d" and "non_additive" are not what a banker reads;
// unknown values fall through verbatim rather than being swallowed.
function windowWords(w: string): string {
  const m = /^(\d+)d$/.exec(w)
  return m ? `Trailing ${m[1]} days` : w
}

const ADDITIVITY_WORDS: Record<string, string> = {
  additive: 'Additive', non_additive: 'Non-additive',
  semi_additive: 'Semi-additive', 'n/a': 'Not summable · n/a',
}

function additivityWords(v: string): string {
  return ADDITIVITY_WORDS[v] ?? v
}

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
  // The column this card is anchored on, for the footer. First operand carrying an as-of role,
  // else the first operand — the artifact's "USES BUSINESS_DT".
  const usesColumn = (s.operands.find(o => o.recipe_role === 'asof') ?? s.operands[0])
    ?.graph_object_ref.split('.').pop()
  const limitations = limitationsOf(s)
  const Heading = headingLevel === 4 ? 'h4' : 'h3'

  return (
    <li className="row q-item sfc">
      <div className="sfc-head">
        <Heading className="sfc-name">{s.display_name}</Heading>
      </div>

      {/* Family and journey stage lead as pills: they say what KIND of feature this is before
          any of its parameters. This REVERSES an earlier decision that kept the fine-grained
          family off the compact card (SuggestedFeaturesScreen.test.tsx) — the reviewed concept
          puts it here, and the family is what a reader scans candidates by. The detail still
          carries it with its attribution. */}
      <div className="sfc-taxonomy">
        {s.recipe_family && <span className="sfc-tax">{s.recipe_family.display_name}</span>}
        {s.recipe_stage && <span className="sfc-tax">{s.recipe_stage.value} stage</span>}
      </div>

      {/* Four boxed parameters a reader actually compares between candidates, two per row —
          not six label/value rows stacked down the card. Sources and data roles drop to the
          quiet line below, and everything absent stays in the detail disclosure. */}
      {/* EVERY optional section still emits a grid row when it has nothing to show. The cards are
          a subgrid (see `.adg-suggestion-grid > .sfc`), so corresponding sections only line up
          across columns while every card contributes the SAME number of rows in the SAME order.
          A conditional that renders nothing collapses one card's rows and knocks every section
          below it out of alignment with its neighbours. */}
      {s.business_interpretation !== null
        ? <p className="sfc-lead sfc-clamp">{s.business_interpretation.value}</p>
        : <div className="sfc-slot" aria-hidden="true" />}

      <dl className="sfc-facts sfc-factgrid">
        <Fact label="Entity & grain">{entityWords(s)}</Fact>
        <Fact label="Time binding">{timeBindingWords(s)}</Fact>
        <Fact label="Window">
          {s.window ? windowWords(s.window) : <Absent>No rolling window</Absent>}
        </Fact>
        <Fact label="Aggregation">
          {s.output_additivity === null
            ? <Absent>not authored</Absent>
            : additivityWords(s.output_additivity.value)}
        </Fact>
      </dl>

      {/* Status AFTER the parameters: the reader sees what the feature IS before how far it
          has been checked. */}
      <div className="sfc-status">
        <span className={`badge ${STATUS_TONE[s.validation_status] ?? 'gj-none'}`}>
          {STATUS_WORDS[s.validation_status] ?? s.validation_status}
        </span>
        {/* v3 ONLY: the execution axis, beside — never merged with — the design-check axis. On a
            v2 page the block is absent and NOTHING readiness-shaped renders: a card must not
            synthesize a state the contract did not carry. */}
        {s.execution && (
          s.execution.output_selection_required
            ? (
              // A multi-output legacy card: its readiness is the BEST atom's — a ceiling, not a
              // property of the card — so the chip says the honest thing: the choice is pending.
              <span
                className="badge gj-none"
                title={`Up to ${readinessWords(s.execution.execution_readiness)} — readiness is `
                  + 'per output; open the card to choose'}
              >
                varies by output
              </span>
            )
            : (
              <span className={`badge ${READINESS_TONE[s.execution.execution_readiness] ?? 'gj-none'}`}>
                {readinessWords(s.execution.execution_readiness)}
              </span>
            )
        )}
        {s.binding_quality && <span className="gj-score">binding {s.binding_quality}</span>}
      </div>

      <p className="mono sfc-recipe">{s.recipe}</p>

      {s.point_in_time_declaration !== null
        ? (
          <div className="sfc-safety-note">
            <span className="sfc-clamp">{s.point_in_time_declaration.value}</span>
          </div>
        )
        // A NEUTRAL placeholder, never an empty `.sfc-safety-note`: that class carries the amber
        // border and tint, so an empty one would render a blank caution box.
        : <div className="sfc-slot" aria-hidden="true" />}
      {/* A caveat COUNT, not a defect list. This panel exists to hand a data scientist a
          candidate worth pursuing; two amber blocks per card reading MISSING_CURRENCY made it
          read as a validation report. The caveats still change whether the feature is
          trustworthy, so they are never dropped — they move into Full detail and the card
          carries one quiet chip saying how many there are. */}
      {limitations.length > 0
        ? (
          <p className="sfc-caveats">
            {limitations.length} {limitations.length === 1 ? 'caveat' : 'caveats'} to check before
            {' '}using this — see full detail.
          </p>
        )
        : <div className="sfc-slot" aria-hidden="true" />}

      {s.business_value === null
        ? <p className="sfc-novalue">Business value has not been documented for this recipe.</p>
        : <div className="sfc-slot" aria-hidden="true" />}

      <div className="sfc-foot">
        <button
          type="button"
          className="sfc-toggle"
          aria-expanded={open}
          // Only while the drawer is actually mounted. `aria-controls` naming an id that is not in
          // the document is an IDREF dangling reference — some AT announce nothing for it, some
          // announce an error — and `aria-expanded={false}` already says the control is collapsed.
          // Rendering the drawer permanently behind `hidden` would be the other valid answer; it
          // costs a full detail subtree per card on a page of them, for no reading benefit.
          aria-controls={open ? detailId : undefined}
          aria-label={`${open ? 'Hide' : 'Show'} full detail for ${bounded(s.display_name, 80)}`}
          onClick={() => setOpen(v => !v)}
        >
          {open ? '▾ Full recommendation detail' : '▸ Full recommendation detail'}
        </button>
      </div>

      {/* The artifact ends every card deliberately: which column it uses on the left, the way
          onward on the right. Ours trailed off after the toggle. */}
      <div className="sfc-usesrow">
        <span className="sfc-uses">
          {usesColumn ? `Uses ${usesColumn}` : `${s.operands.length} input columns`}
        </span>
        {/* NOT a second control. The artifact's "OPEN RECOMMENDATION →" links to a separate
            page; ours has the detail inline, so an action here would both duplicate the
            disclosure and break this card's read-only guarantee (exactly one control, pinned
            by two tests). The count is the ending instead. */}
        <span className="sfc-open">
          {s.operands.length} {s.operands.length === 1 ? 'input' : 'inputs'}
        </span>
      </div>

      {open && (
        <SuggestionDetail
          hit={hit} id={detailId} omitted={omitted}
          headingLevel={headingLevel === 4 ? 5 : 4}
        />
      )}
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

export function TruncationNotes({ omitted }: { omitted: SuggestionOmittedCounts }) {
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
      {/* Words first, then the raw member beside them: a reader must be able to act on this row
          without a glossary, and an auditor must still see exactly what the server said. */}
      <dl className="kv sfc-rel-facts">
        <div>
          <dt>Link kind</dt>
          <dd>{vocabWords({}, leg.relationship_kind)} <span className="mono">
            {leg.relationship_kind}</span></dd>
        </div>
        <div>
          <dt>Rows travelling this way</dt>
          <dd>{vocabWords(CARDINALITY_WORDS, leg.cardinality)} <span className="mono">
            {leg.cardinality}</span></dd>
        </div>
        <div>
          <dt>Execution safety</dt>
          <dd>{vocabWords(SAFETY_WORDS, leg.safety_status)} <span className="mono">
            {leg.safety_status}</span></dd>
        </div>
        <div>
          <dt>Review</dt>
          <dd>{vocabWords(REVIEW_WORDS, leg.review_status)} <span className="mono">
            {leg.review_status}</span></dd>
        </div>
      </dl>
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
  headingLevel,
}: {
  hit: FeatureSuggestionHit
  id: string
  omitted?: SuggestionOmittedCounts
  headingLevel: 4 | 5
}) {
  const s = hit.suggestion
  const p = hit.provenance
  return (
    // The region's accessible name is BOUNDED like every other one: a display name is an unbounded
    // catalog string, and a 400-character region name is unusable in a screen reader. The complete
    // value is never lost — it is the card's own heading, in full, as text.
    <div
      className="sfc-detail" id={id} role="group"
      aria-label={`Full detail for ${bounded(s.display_name, 80)}`}
    >
      <section className="sfc-sec">
        <Micro level={headingLevel}>Meaning</Micro>
        <TextDetail label="What it measures" text={s.business_interpretation} />
        <TextDetail label="Why it is useful" text={s.business_value} />
        <div className="sfc-chiprow">
          <span className="sfc-chiprow-label">Keywords</span>
          {s.keywords.length === 0
            ? <Absent>none</Absent>
            : s.keywords.map((k, i) => <TermChip key={`${k.value}:${i}`} text={k} />)}
        </div>
      </section>

      <section className="sfc-sec">
        <Micro level={headingLevel}>Classification</Micro>
        <dl className="kv">
          {/* How this candidate came to exist. It was a chip in the card head; moving the head to
              the name alone would have dropped it from the UI entirely, which is a loss rather
              than a relocation — a reader must always be able to learn that this is a suggestion
              from a governed recipe, not a registered feature and not an LLM invention. */}
          <div>
            <dt>Generated by</dt>
            <dd>suggested · {s.generation_source}</dd>
          </div>
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
        {/* v3 ONLY. The audit drawer's side of the readiness chip: what this suggestion IS, and
            every blocker in banking words with its machine code beside it. */}
        {s.execution && (
          <>
            <Micro level={headingLevel}>Execution readiness</Micro>
            <p>
              This suggestion is {COMPUTATION_KIND_WORDS[s.execution.computation_kind]
                ?? s.execution.computation_kind.replace(/_/g, ' ')}.
              {' '}Readiness: {readinessWords(s.execution.execution_readiness)}{' '}
              <span className="mono">{s.execution.execution_readiness}</span>.
            </p>
            {s.execution.readiness_blockers.length === 0
              ? (
                s.execution.execution_readiness === 'UNASSESSED' && (
                  <p className="hint">
                    Nobody has assessed this idea for exact execution yet — that is a to-do, not
                    a defect of the suggestion.
                  </p>
                )
              )
              : (
                <ul className="sfc-omit">
                  {s.execution.readiness_blockers.map(b => (
                    <li key={b.code}
                        className={WARNING_SHAPED_CODES.has(b.code) ? 'sfc-warning' : undefined}>
                      {WARNING_SHAPED_CODES.has(b.code) ? '\u26a0 ' : ''}
                      {BLOCKER_WORDS[b.code] ?? b.code.replace(/_/g, ' ')}
                      {' '}({BLOCKER_GROUP_WORDS[b.group] ?? b.group.replace(/_/g, ' ')}
                      {' '}· <span className="mono">{b.code}</span>)
                    </li>
                  ))}
                </ul>
              )}
            {/* BR-17: the legacy idea's atomic V2 replacements, named — a split recipe shows
                its atoms rather than pretending to be one quantity. When the backend carries
                per-replacement readiness, each atom shows its OWN state (nothing inherits a
                sibling's); a multi-output card says the choice is the user's to make. */}
            {s.execution.output_selection_required && (
              <p className="hint">
                This suggestion spans {s.execution.replacement_readiness?.length ?? 'several'}{' '}
                atomic outputs — the readiness above is the best one&rsquo;s. Choosing which
                output to build is your call, not the platform&rsquo;s.
              </p>
            )}
            {(s.execution.replacement_readiness?.length ?? 0) > 0 ? (
              <ul className="sfc-omit">
                {s.execution.replacement_readiness!.map(r => (
                  <li key={r.recipe_id}>
                    <span className="mono">{r.recipe_id}</span>
                    {' '}— {readinessWords(r.execution_readiness)}
                  </li>
                ))}
              </ul>
            ) : (s.execution.v2_replacements?.length ?? 0) > 0 && (
              <p className="hint">
                Governed recipe{(s.execution.v2_replacements!.length > 1) ? 's' : ''}:{' '}
                <span className="mono">{s.execution.v2_replacements!.join(', ')}</span>
              </p>
            )}
          </>
        )}
        <Micro level={headingLevel}>Business domains</Micro>
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
        <Micro level={headingLevel}>Use cases</Micro>
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
        <Micro level={headingLevel}>Catalog terms</Micro>
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
        <Micro level={headingLevel}>Entity and grain</Micro>
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
        <Micro level={headingLevel}>Computation</Micro>
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
          s.authoring_notes.map((n, i) => (
            <AttributedDetail
              key={`${n.value}:${i}`} label="Authoring note" value={n.value} basis={n.basis}
              evidence={n.evidence} sourceRefs={n.source_refs}
            />
          ))
        )}
      </section>

      <section className="sfc-sec">
        <Micro level={headingLevel}>Source datasets</Micro>
        {s.source_datasets.length === 0
          ? <p className="hint"><Absent>no dataset was bound</Absent></p>
          : s.source_datasets.map(d => (
            <DatasetDetail key={`${d.catalog_source}:${d.table_ref}`} dataset={d} />
          ))}
      </section>

      <section className="sfc-sec">
        <Micro level={headingLevel}>Every input column</Micro>
        <ul className="sfc-operands">
          {s.operands.map((o, i) => (
            <li key={`${o.catalog_source}:${o.graph_object_ref}:${i}`}>
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
        <Micro level={headingLevel}>Relationships this feature crosses</Micro>
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
        <Micro level={headingLevel}>Requirements and limitations</Micro>
        {s.warnings.length === 0 && s.requirements.length === 0 ? (
          <p className="hint">Nothing was raised against this suggestion.</p>
        ) : (
          <>
            <ul className="sfc-lims" aria-label="Requirements and limitations">
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
        <Micro level={headingLevel}>Currentness</Micro>
        <Currentness projection={hit.projection} />
      </section>

      <section className="sfc-sec">
        <Micro level={headingLevel}>Provenance and revisions</Micro>
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
          <Micro level={headingLevel}>Truncated on this page</Micro>
          <p className="hint">
            Counted for the whole page, not for this card alone.
          </p>
          <TruncationNotes omitted={omitted} />
        </section>
      )}
    </div>
  )
}
